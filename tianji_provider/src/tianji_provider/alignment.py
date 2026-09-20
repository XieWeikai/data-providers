"""Offline resampling on one integer-nanosecond clock, with bounded data validity."""

from fractions import Fraction

import numpy as np
from scipy.interpolate import interp1d
from scipy.signal import resample_poly
from scipy.spatial.transform import Rotation, Slerp


def uniform_grid(start, end, fps):
    """Round each rational offset independently: no float epoch or accumulated drift."""
    count = (int(end) - int(start)) * fps // 1_000_000_000 + 1
    if count < 1:
        raise ValueError("Signals have no common time interval")
    k = np.arange(count, dtype=np.int64)
    result = int(start) + (k * 1_000_000_000 + fps // 2) // fps
    return result[result <= end]


def previous_indices(times, grid):
    indices = np.searchsorted(times, grid, side="right") - 1
    if np.any(indices < 0) or np.any(grid > times[-1]):
        raise ValueError("Extrapolation outside a signal's recorded interval is forbidden")
    return indices


def valid_mask(times, grid, max_gap_ns, causal=False):
    left = previous_indices(times, grid)
    if causal:
        return grid - times[left] <= max_gap_ns
    right = np.minimum(left + 1, len(times) - 1)
    exact = grid == times[left]
    return exact | (times[right] - times[left] <= max_gap_ns)


def runs(mask):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    return [(int(a), int(b)) for a, b in edges.reshape(-1, 2)]


def interpolate(times, values, grid, fps, max_gap_ns, mode="lowpass"):
    """Filter only within contiguous measured runs; never filter across outages.

    Irregular input is first interpolated to its median-rate regular grid.
    Polyphase FIR resampling applies zero-phase anti-alias filtering for numeric
    observations. Commands bypass this function and use causal sample-and-hold.
    """
    output = np.empty((len(grid), values.shape[1]), dtype=np.float64)
    assigned = np.zeros(len(grid), dtype=bool)
    boundaries = np.r_[0, np.flatnonzero(np.diff(times) > max_gap_ns) + 1, len(times)]
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        select = (grid >= times[a]) & (grid <= times[b - 1])
        if not np.any(select):
            continue
        assigned[select] = True
        if b - a == 1:
            output[select] = values[a]
            continue
        origin = times[a]
        x = (times[a:b] - origin).astype(np.float64) / 1e9
        q = (grid[select] - origin).astype(np.float64) / 1e9
        y = values[a:b]
        hz = max(1, round(1e9 / np.median(np.diff(times[a:b]))))
        if mode == "lowpass" and hz > fps and b - a >= 8:
            regular = np.arange(int(np.floor(x[-1] * hz)) + 1, dtype=np.float64) / hz
            sampled = interp1d(x, y, axis=0, assume_sorted=True)(regular)
            ratio = Fraction(fps, hz)
            # Pad by one target period using the observed boundary values. This
            # supplies the FIR boundary condition, not new target timestamps.
            pad = max(1, int(np.ceil(hz / fps)))
            sampled = np.pad(sampled, ((pad, pad), (0, 0)), mode="edge")
            y = resample_poly(sampled, ratio.numerator, ratio.denominator, axis=0, padtype="line")
            x = np.arange(len(y), dtype=np.float64) / fps - pad / hz
            output[select] = interp1d(x, y, axis=0, assume_sorted=True)(q)
        else:
            output[select] = interp1d(x, y, axis=0, assume_sorted=True)(q)
    if not assigned.all():
        raise ValueError("Interpolation requested across an unsupported gap")
    return output


def pose_interpolate(signal, grid, fps, max_gap_ns, mode):
    xyz = interpolate(signal.times, signal.values[:, :3], grid, fps, max_gap_ns, mode)
    q = signal.values[:, 3:]
    norms = np.linalg.norm(q, axis=1)
    if np.any(abs(norms - 1) > 0.01):
        raise ValueError("EEF orientation is not a unit quaternion")
    # Slerp handles antipodal representations and avoids invalid componentwise
    # quaternion interpolation. The validity mask has already excluded gaps.
    origin = signal.times[0]
    quat = Slerp((signal.times - origin) / 1e9, Rotation.from_quat(q))((grid - origin) / 1e9).as_quat()
    signs = np.r_[1.0, np.cumprod(np.where(np.sum(quat[1:] * quat[:-1], axis=1) < 0, -1.0, 1.0))]
    return np.column_stack((xyz, quat * signs[:, None]))


def align(signals, ranges, video_times, config):
    ranges = {**ranges, "video": [int(video_times[0]), int(video_times[-1]), len(video_times)]}
    start = max(v[0] for v in ranges.values())
    end = min(v[1] for v in ranges.values())
    grid = uniform_grid(start, end, config.fps)
    valid = np.ones(len(grid), dtype=bool)
    rejected = {}
    for topic, signal in signals.items():
        gap = config.gripper_max_gap_ms if "gripper_target" in topic else config.max_gap_ms
        causal = "joint_cmd" in topic or "gripper_target" in topic
        okay = valid_mask(signal.times, grid, round(gap * 1e6), causal)
        rejected[topic] = int(np.sum(~okay))
        valid &= okay
    video_indices = previous_indices(video_times, grid)
    okay = grid - video_times[video_indices] <= round(config.max_gap_ms * 1e6)
    rejected["video"] = int(np.sum(~okay))
    valid &= okay
    if not valid.all() and config.gap_policy == "error":
        raise ValueError(f"{np.sum(~valid)} grid samples cross missing/stale signals: {rejected}")
    spans = [(a, b) for a, b in runs(valid) if b - a >= 2]
    if not spans:
        raise ValueError("No continuous interval with at least two valid output frames")
    kept = np.concatenate([np.arange(a, b) for a, b in spans])
    query = grid[kept]
    resampled = {}
    for topic, s in signals.items():
        if "joint_cmd" in topic or "gripper_target" in topic:
            value = s.values[previous_indices(s.times, query)]
        elif "/eef_" in topic:
            value = pose_interpolate(s, query, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
        else:
            value = interpolate(s.times, s.values, query, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
        resampled[topic] = value
    r = resampled
    columns = {
        "observation.state": np.column_stack((r["/tj/joint_states"][:, :7], r["/info/gripper_feedback_L"],
                                                r["/tj/joint_states"][:, 7:], r["/info/gripper_feedback_R"])),
        "observation.action": np.column_stack((r["/tj/control/joint_cmd_A"], r["/info/gripper_target_L"],
                                                 r["/tj/control/joint_cmd_B"], r["/info/gripper_target_R"])),
        "observation.wrench": np.column_stack((r["/tj/info/wrench_left"], r["/tj/info/wrench_right"])),
        "observation.eef": np.column_stack((r["/tj/info/eef_left"], r["/tj/info/eef_right"])),
    }
    result, offset = [], 0
    for a, b in spans:
        n = b - a
        result.append((grid[a:b], {k: v[offset:offset+n].astype(np.float32) for k, v in columns.items()}, video_indices[a:b]))
        offset += n
    report = {
        "intersection_start_ns": int(start), "intersection_end_ns": int(end),
        "grid_frames": len(grid), "kept_frames": len(kept), "dropped_frames": len(grid) - len(kept),
        "segments": [{"start_ns": int(grid[a]), "end_ns": int(grid[b-1]), "frames": b-a} for a, b in spans],
        "invalid_frames_by_signal": rejected, "signal_ranges_ns": ranges,
        "video_max_age_ms": float(np.max(query - video_times[video_indices[kept]]) / 1e6),
    }
    return result, report
