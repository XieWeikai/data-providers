"""Read-only DatasetSource: raw MCAP or an explicitly prepared reusable cache."""

from dataclasses import asdict
from functools import cached_property
from pathlib import Path
import hashlib
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from letools.model import DatasetMetadata, Episode, EpisodeDataProfile, MediaProfile, VideoSlice
from letools.plugins.base import DatasetSource

from .alignment import align
from .media import CAMERAS, TianjiFrames, dimensions, image_statistics
from .reader import bag_files, discover, read_numeric, read_video_times, video_files

FORMAT = "tianji-thor-v2"
GENERATED = {"timestamp": "float32", "frame_index": "int64", "episode_index": "int64", "index": "int64", "task_index": "int64"}


def statistics(values):
    v = np.asarray(values, dtype=np.float64)
    return {"min": np.atleast_1d(v.min(axis=0)).tolist(), "max": np.atleast_1d(v.max(axis=0)).tolist(),
            "mean": np.atleast_1d(v.mean(axis=0)).tolist(), "std": np.atleast_1d(v.std(axis=0)).tolist(), "count": [len(v)]}


def arrow_table(columns):
    arrays = {}
    for key, values in columns.items():
        values = np.ascontiguousarray(values)
        array = pa.array(values.reshape(-1))
        if values.ndim == 2:
            array = pa.FixedSizeListArray.from_arrays(array, values.shape[1])
        arrays[key] = array
    return pa.table(arrays)


def features(width, height, fps, codec="ppm"):
    joints = [*[f"left_joint_{i}_rad" for i in range(1, 8)], "left_gripper_normalized",
              *[f"right_joint_{i}_rad" for i in range(1, 8)], "right_gripper_normalized"]
    result = {
        "observation.state": {"dtype": "float32", "shape": [16], "names": joints},
        "observation.action": {"dtype": "float32", "shape": [16], "names": joints},
        "observation.wrench": {"dtype": "float32", "shape": [12], "names": [f"{s}_{v}" for s in ("left_flange", "right_flange") for v in ("fx_N", "fy_N", "fz_N", "tx_Nm", "ty_Nm", "tz_Nm")]},
        "observation.eef": {"dtype": "float32", "shape": [14], "names": [f"{s}_{v}" for s in ("left_base_link", "right_base_link") for v in ("x_m", "y_m", "z_m", "qx", "qy", "qz", "qw")]},
    }
    for key, dtype in GENERATED.items():
        result[key] = {"dtype": dtype, "shape": [1], "names": None}
    for camera in CAMERAS:
        vi = {"video.fps": fps, "video.codec": codec, "video.pix_fmt": "rgb24" if codec == "ppm" else "yuv420p", "video.is_depth_map": False, "has_audio": False}
        result[f"observation.image.{camera}"] = {"dtype": "video", "shape": [height, width, 3], "names": ["height", "width", "channels"],
            "info": {"video.height": height, "video.width": width, **vi}, "video_info": vi}
    return result


class TianjiSource(DatasetSource):
    """Construction is lazy; provider.open does no traversal, decoding or writing."""

    def __init__(self, root, config, *, _collect_image_stats=True):
        self.root = Path(root).expanduser().resolve()
        self.config = config
        self._collect_image_stats = _collect_image_stats

    def planner_identity(self):
        return FORMAT + ":ppm-rgb24-v1", hashlib.sha256(json.dumps(asdict(self.config), sort_keys=True).encode()).hexdigest()

    @cached_property
    def _loaded(self):
        if (self.root / "tianji-cache.json").is_file():
            return self._load_prepared()
        rows, descriptors, reports, audit = {}, [], [], {}
        offset, expected_size = 0, None
        for bag in discover(self.root):
            signals, ranges, clocks, calibration = read_numeric(bag)
            paths = video_files(bag)
            vt = read_video_times(paths)
            size = dimensions(paths)
            if expected_size is not None and size != expected_size:
                raise ValueError("Camera dimensions vary across recordings")
            expected_size = size
            try:
                segments, report = align(signals, ranges, vt, self.config)
            except ValueError as error:
                raise ValueError(f"{bag.name}: {error}") from error
            report.update(bag=str(bag), timestamp_sources=clocks, gripper_calibration=calibration)
            report["episode_indices"] = list(range(len(descriptors), len(descriptors)+len(segments)))
            reports.append(report)
            physical = sum(p.stat().st_size for p in bag_files(bag / "data"))
            video_bytes = sum(p.stat().st_size for p in paths)
            for grid, values, indices in segments:
                index, length = len(descriptors), len(grid)
                values.update(timestamp=np.arange(length, dtype=np.float32) / self.config.fps,
                              frame_index=np.arange(length, dtype=np.int64), episode_index=np.full(length, index, dtype=np.int64),
                              index=np.arange(offset, offset+length, dtype=np.int64), task_index=np.zeros(length, dtype=np.int64))
                table = arrow_table(values)
                rows[index] = table
                media = {f"observation.image.{name}": TianjiFrames(paths, indices, name, *size, video_bytes) for name in CAMERAS}
                descriptors.append(Episode(index, length, (self.config.task,), {k: statistics(v) for k, v in values.items()}, bag, data_end=length, videos=media))
                audit[index] = {"bag": str(bag), "absolute_time_ns": grid, "video_packet_index": indices,
                                "video_time_ns": vt[indices], "data_physical_bytes": physical}
                offset += length
            if self._collect_image_stats:
                group = [e for e in descriptors if e.data_path == bag]
                image_stats = image_statistics(group)
                for ep in group:
                    ep.stats.update(image_stats[ep.index])
        assert expected_size is not None
        return self._finish(descriptors, rows, reports, audit, features(*expected_size, self.config.fps))

    def _finish(self, descriptors, rows, reports, audit, schema):
        n = len(descriptors)
        total = sum(e.length for e in descriptors)
        info = {"codebase_version": FORMAT, "robot_type": "tianji_marvin_pro", "fps": self.config.fps,
                "total_episodes": n, "total_frames": total, "total_tasks": 1, "total_videos": n*4, "total_chunks": 1,
                "chunks_size": 1000, "splits": {"train": f"0:{n}"}, "data_path": None, "video_path": None, "features": schema,
                "tianji": {"preset": "thor-v1", "config": asdict(self.config), "alignment_reports": reports}}
        metadata = DatasetMetadata(FORMAT, self.config.fps, schema, "tianji_marvin_pro", info["splits"], total, n, {0: self.config.task}, info)
        return metadata, tuple(descriptors), rows, reports, audit

    def _load_prepared(self):
        manifest = json.loads((self.root / "tianji-cache.json").read_text())
        if manifest.get("format") != FORMAT:
            raise ValueError("Unsupported Tianji cache version; rebuild with state fallback and one episode per recording")
        semantic = asdict(self.config)
        semantic.pop("task")
        if semantic != manifest["alignment_config"]:
            raise ValueError("Prepared cache FPS/alignment options differ; run tianji-provider prepare with the requested options")
        descriptors, rows, audit = [], {}, {}
        for item in manifest["episodes"]:
            index, length = item["index"], item["length"]
            if index != len(descriptors) or length < 2:
                raise ValueError("Malformed prepared episode index/length")
            def path(value):
                p = (self.root / value).resolve()
                if not p.is_relative_to(self.root) or not p.is_file():
                    raise ValueError(f"Missing or unsafe cache resource: {value}")
                return p
            data_path = path(item["data"])
            videos = {k: VideoSlice(path(v), 0.0, length/self.config.fps) for k, v in item["videos"].items()}
            if set(videos) != {f"observation.image.{c}" for c in CAMERAS}:
                raise ValueError("Prepared cache must contain all four cameras")
            descriptors.append(Episode(index, length, (self.config.task,), item["stats"], data_path, data_end=length, videos=videos))
            audit[index] = {"path": path(item["audit"])}
        return self._finish(descriptors, rows, manifest["reports"], audit, manifest["features"])

    @property
    def metadata(self):
        return self._loaded[0]

    @property
    def episodes(self):
        return self._loaded[1]

    @property
    def reports(self):
        return self._loaded[3]

    def audit(self, episode):
        value = self._loaded[4][episode.index]
        if "path" in value:
            with np.load(value["path"], allow_pickle=False) as data:
                return {k: data[k] for k in data.files}
        return value

    def read_episode(self, episode):
        rows = self._loaded[2]
        table = rows.get(episode.index)
        if table is None:
            table = pq.read_table(episode.data_path)
        if len(table) != episode.length:
            raise ValueError("Episode table length differs from metadata")
        return table

    def data_profile(self, episode):
        if episode.index not in self._loaded[2]:
            return super().data_profile(episode)
        siblings = [e for e in self.episodes if e.data_path == episode.data_path]
        return EpisodeDataProfile(str(episode.data_path), self.read_episode(episode).nbytes,
            sum(self.read_episode(e).nbytes for e in siblings), self._loaded[4][episode.index]["data_physical_bytes"], sum(e.length for e in siblings))

    def media_profile(self, episode, key):
        media = episode.videos[key]
        if isinstance(media, VideoSlice):
            return super().media_profile(episode, key)
        return MediaProfile("|".join(map(str, media.paths)), media.estimated_size_bytes, "frame_sequence", True)
