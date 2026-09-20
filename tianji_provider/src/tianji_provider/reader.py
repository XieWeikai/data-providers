"""Read embedded ROS 2 schemas without a ROS installation or robot connection."""

from dataclasses import dataclass
from pathlib import Path
import json
import struct

import numpy as np
import yaml
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

VIDEO_TOPIC = "/recorder/quad_tile/compressed_undistorted"
JOINT_NAMES = tuple(f"Joint{i}_{side}" for side in ("L", "R") for i in range(1, 8))
TYPES = {
    "/tj/joint_states": "sensor_msgs/msg/JointState",
    "/tj/control/joint_cmd_A": "marvin_msgs/msg/JointcmdArm",
    "/tj/control/joint_cmd_B": "marvin_msgs/msg/JointcmdArm",
    **{f"/info/gripper_feedback_{s}": "std_msgs/msg/Float32MultiArray" for s in ("L", "R")},
    **{f"/info/gripper_target_{s}": "std_msgs/msg/Float32" for s in ("L", "R")},
    **{f"/tj/info/eef_{s}": "geometry_msgs/msg/PoseStamped" for s in ("left", "right")},
    **{f"/tj/info/wrench_{s}": "geometry_msgs/msg/WrenchStamped" for s in ("left", "right")},
    "/info/web_gripper": "std_msgs/msg/String",
}


@dataclass
class Signal:
    times: np.ndarray
    values: np.ndarray


def discover(root):
    root = Path(root).resolve()
    if (root / "data/metadata.yaml").is_file():
        return (root,)
    paths = tuple(sorted(p for p in root.iterdir() if p.is_dir() and (p / "data/metadata.yaml").is_file()))
    if not paths:
        raise ValueError(f"No Thor recordings with data/metadata.yaml under {root}")
    return paths


def bag_files(directory):
    """Use the manifest order so split MCAP files cannot be lexically misordered."""
    directory = Path(directory)
    meta = yaml.safe_load((directory / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
    paths = tuple((directory / p).resolve() for p in meta["relative_file_paths"])
    for p in paths:
        if not p.is_relative_to(directory.resolve()) or not p.is_file():
            raise ValueError(f"Missing or unsafe MCAP path: {p}")
    return paths


def video_files(bag):
    manifests = sorted((bag / "video").rglob("metadata.yaml"))
    if len(manifests) != 1:
        raise ValueError(f"{bag.name}: expected exactly one quad-camera bag, got {len(manifests)}")
    return bag_files(manifests[0].parent)


def messages(paths, topics=None):
    """One reader and schema decoder per physical file, reused for all messages."""
    for path in paths:
        with path.open("rb") as handle:
            reader = make_reader(handle, decoder_factories=[DecoderFactory()])
            yield from reader.iter_decoded_messages(topics=topics, log_time_order=False)


def stamp_ns(message, record_ns):
    if hasattr(message, "header"):
        stamp = message.header.stamp
        value = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        if value <= 0 or not 0 <= stamp.nanosec < 1_000_000_000:
            raise ValueError("Invalid/zero ROS header timestamp; refusing a silent clock fallback")
        if abs(value - record_ns) > 1_000_000_000:
            raise ValueError("Header and MCAP clocks differ by more than 1 s")
        return value
    return int(record_ns)


def normalize_signal(times, values, topic):
    t = np.asarray(times, dtype=np.int64)
    v = np.asarray(values, dtype=np.float64)
    if not len(t) or not np.isfinite(v).all():
        raise ValueError(f"{topic}: empty or non-finite signal")
    if np.any(np.diff(t) < 0):
        raise ValueError(f"{topic}: timestamps move backwards; refusing to hide a clock reset")
    # Last message wins at equal timestamps, matching a state update.
    keep = np.r_[t[1:] != t[:-1], True]
    return Signal(t[keep], v[keep])


def read_numeric(bag):
    metadata = yaml.safe_load((bag / "data/metadata.yaml").read_text())["rosbag2_bagfile_information"]
    empty = [v["topic_metadata"]["name"] for v in metadata.get("topics_with_message_count", []) if v["message_count"] == 0]
    if empty:
        raise ValueError(f"{bag.name}: cannot intersect empty recorded topics: {empty}")
    samples = {k: ([], []) for k in TYPES if k != "/info/web_gripper"}
    ranges, calibration, clocks = {}, None, {}
    previous = {}
    for schema, channel, record, msg in messages(bag_files(bag / "data")):
        topic = channel.topic
        t = stamp_ns(msg, record.log_time)
        if t < previous.get(topic, t):
            raise ValueError(f"{bag.name}: {topic} clock reversal")
        previous[topic] = t
        bounds = ranges.setdefault(topic, [t, t, 0])
        bounds[1], bounds[2] = t, bounds[2] + 1
        clocks[topic] = "header" if hasattr(msg, "header") else "mcap_receipt"
        if topic not in TYPES:
            continue
        if schema.name != TYPES[topic]:
            raise ValueError(f"{topic}: expected {TYPES[topic]}, got {schema.name}")
        if topic == "/info/web_gripper":
            status = json.loads(msg.data)
            limits = np.asarray(status.get("calibration"), dtype=np.float64)
            if limits.shape != (2, 2) or not np.isfinite(limits).all() or np.any(limits[:, 1] <= limits[:, 0]):
                raise ValueError(f"{bag.name}: missing/invalid recorded gripper calibration")
            if status.get("position_unit") != "motor_radian" or status.get("target_unit") != "normalized_0_1":
                raise ValueError(f"{bag.name}: unsupported gripper units")
            if calibration is not None and not np.array_equal(calibration, limits):
                raise ValueError(f"{bag.name}: gripper calibration changed within the recording")
            calibration = limits
            continue
        if topic == "/tj/joint_states":
            if set(msg.name) != set(JOINT_NAMES) or len(msg.name) != 14 or len(msg.position) != 14:
                raise ValueError(f"{topic}: expected the 14 named Tianji joints")
            indices = {name: i for i, name in enumerate(msg.name)}
            value = [msg.position[indices[name]] for name in JOINT_NAMES]
        elif "/joint_cmd_" in topic:
            value = list(msg.positions)
            if len(value) != 7:
                raise ValueError(f"{topic}: expected seven joint targets")
        elif "gripper_feedback" in topic:
            if len(msg.data) != 5:
                raise ValueError(f"{topic}: expected five motor feedback values")
            value = [msg.data[0]]
        elif "gripper_target" in topic:
            if not 0 <= msg.data <= 1:
                raise ValueError(f"{topic}: target must be normalized to [0, 1]")
            value = [msg.data]
        elif "/eef_" in topic:
            if msg.header.frame_id != "base_link":
                raise ValueError(f"{topic}: expected base_link, got {msg.header.frame_id!r}")
            p, q = msg.pose.position, msg.pose.orientation
            value = [p.x, p.y, p.z, q.x, q.y, q.z, q.w]
        else:
            frame = "flange_L" if topic.endswith("left") else "flange_R"
            if msg.header.frame_id != frame:
                raise ValueError(f"{topic}: expected {frame}")
            f, q = msg.wrench.force, msg.wrench.torque
            value = [f.x, f.y, f.z, q.x, q.y, q.z]
        samples[topic][0].append(t)
        samples[topic][1].append(value)
    if calibration is None:
        raise ValueError(f"{bag.name}: /info/web_gripper calibration is required; no guessed motor limits")
    result = {k: normalize_signal(*v, k) for k, v in samples.items()}
    # Express feedback in the driver's recorded normalized target coordinate.
    # Do not clip small telemetry overshoots; retain measured information.
    for side, (lo, hi) in zip(("L", "R"), calibration):
        result[f"/info/gripper_feedback_{side}"].values = (
            result[f"/info/gripper_feedback_{side}"].values - lo) / (hi - lo)
    return result, ranges, clocks, calibration.tolist()


def read_video_times(paths):
    times = []
    for t, payload in video_packets(paths):
        times.append(t)
    t = np.asarray(times, dtype=np.int64)
    if len(t) < 2 or np.any(np.diff(t) <= 0):
        raise ValueError("Video timestamps must be strictly increasing with at least two frames")
    return t


def video_packets(paths):
    """Parse the standard CDR envelope without expanding H.264 bytes into ints."""
    for path in paths:
        with path.open("rb") as handle:
            reader = make_reader(handle)
            checked = set()
            for schema, channel, record in reader.iter_messages(topics=[VIDEO_TOPIC], log_time_order=False):
                if schema.name != "sensor_msgs/msg/CompressedImage" or channel.message_encoding != "cdr":
                    raise ValueError("Expected standard ROS 2 CompressedImage CDR")
                if schema.id not in checked:
                    root = schema.data.decode().split("====", 1)[0]
                    declarations = [" ".join(line.split("#", 1)[0].split()) for line in root.splitlines()]
                    declarations = [line.replace("/msg/", "/") for line in declarations if line]
                    if declarations != ["std_msgs/Header header", "string format", "uint8[] data"]:
                        raise ValueError("CompressedImage schema differs from the verified standard layout")
                    checked.add(schema.id)
                raw = memoryview(record.data)
                if bytes(raw[:2]) not in (b"\x00\x01", b"\x00\x00"):
                    raise ValueError("Only CDR v1 is supported")
                endian = "<" if raw[1] == 1 else ">"
                sec, nsec, size = struct.unpack_from(endian + "iII", raw, 4)
                pos = 16 + size
                pos = (pos + 3) & ~3
                size = struct.unpack_from(endian + "I", raw, pos)[0]
                fmt = bytes(raw[pos+4:pos+4+size-1]).decode()
                if "h264" not in fmt.lower():
                    raise ValueError(f"Unsupported video format: {fmt}")
                pos = (pos + 4 + size + 3) & ~3
                size = struct.unpack_from(endian + "I", raw, pos)[0]
                if pos + 4 + size != len(raw):
                    raise ValueError("Malformed CompressedImage payload length")
                t = int(sec) * 1_000_000_000 + nsec
                if t != record.log_time or t <= 0 or nsec >= 1_000_000_000:
                    raise ValueError("Video header/record clock mismatch or invalid timestamp")
                yield t, raw[pos+4:]
