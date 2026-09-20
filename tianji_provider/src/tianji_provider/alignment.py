"""One complete episode per recording, with state targets during command pauses."""

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


COMMAND_STATE = {
    "/tj/control/joint_cmd_A": ("/tj/joint_states", slice(0, 7)),
    "/tj/control/joint_cmd_B": ("/tj/joint_states", slice(7, 14)),
    "/info/gripper_target_L": ("/info/gripper_feedback_L", slice(None)),
    "/info/gripper_target_R": ("/info/gripper_feedback_R", slice(None)),
}
FEEDBACK_TOPICS = ("/tj/joint_states", "/info/gripper_feedback_L", "/info/gripper_feedback_R")


def moving_bounds(times, feedback, joint_threshold, gripper_threshold):
    """Find motion relative to stable edge poses; retain one boundary sample.

    Endpoint medians over 200 ms reject isolated feedback noise. Displacement
    from the endpoint pose, not per-frame velocity, also detects slow motion.
    Only the outer edges are trimmed; interior pauses remain in the episode.
    """
    threshold = np.r_[np.full(14, joint_threshold), gripper_threshold, gripper_threshold]
    first = np.median(feedback[times <= times[0] + 200_000_000], axis=0)
    last = np.median(feedback[times >= times[-1] - 200_000_000], axis=0)
    changed_from_first = np.flatnonzero(np.any(np.abs(feedback - first) > threshold, axis=1))
    changed_from_last = np.flatnonzero(np.any(np.abs(feedback - last) > threshold, axis=1))
    if not len(changed_from_first) or not len(changed_from_last):
        raise ValueError("No motion exceeds the joint/gripper thresholds; use --no-trim-stationary or lower the thresholds")
    a = max(0, int(changed_from_first[0]) - 1)
    b = min(len(times), int(changed_from_last[-1]) + 2)
    if b - a < 2:
        raise ValueError("Motion interval contains fewer than two output frames")
    return a, b


def command_or_state(signal, grid, aligned_state, max_age_ns):
    """Use causal commands while fresh, otherwise target the current feedback.

    A paused teleoperation arm must not inherit an old, potentially unachieved
    target. Each arm/gripper falls back independently, and a new command takes
    effect at its timestamp. Missing command packets never remove output rows.
    """
    indices = previous_indices(signal.times, grid)
    fallback = grid - signal.times[indices] > max_age_ns
    values = signal.values[indices].copy()
    values[fallback] = aligned_state[fallback]
    return values, fallback


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
    if len(grid) < 2:
        raise ValueError("The common interval must contain at least two output frames")
    masks = {}
    for topic, signal in signals.items():
        if topic not in COMMAND_STATE:
            masks[topic] = valid_mask(signal.times, grid, round(config.max_gap_ms * 1e6))
    video_indices = previous_indices(video_times, grid)
    masks["video"] = grid - video_times[video_indices] <= round(config.max_gap_ms * 1e6)

    # Missing feedback cannot prove an edge was stationary. Check the full
    # candidate interval before trimming, including missing heads and tails.
    feedback_ok = np.logical_and.reduce([masks[k] for k in FEEDBACK_TOPICS])
    if not feedback_ok.all():
        failures = {k: int(np.sum(~masks[k])) for k in FEEDBACK_TOPICS if not masks[k].all()}
        raise ValueError(f"Missing joint/gripper observations exceed --max-gap-ms={config.max_gap_ms:g}; "
                         f"refusing to split or label unobserved edges as stationary: {failures}")
    feedback_times = grid
    feedback_values = {k: interpolate(signals[k].times, signals[k].values, feedback_times,
                                     config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
                       for k in FEEDBACK_TOPICS}
    trim_start, trim_stop = 0, len(grid)
    if config.trim_stationary:
        feedback = np.column_stack([feedback_values[k] for k in FEEDBACK_TOPICS])
        trim_start, trim_stop = moving_bounds(feedback_times, feedback, config.motion_joint_threshold_rad,
                                              config.motion_gripper_threshold)
    original_grid = grid
    grid = grid[trim_start:trim_stop]
    video_indices = video_indices[trim_start:trim_stop]
    rejected = {k: int(np.sum(~mask[trim_start:trim_stop])) for k, mask in masks.items()}
    failures = {topic: count for topic, count in rejected.items() if count}
    if failures:
        raise ValueError(f"Missing observations exceed --max-gap-ms={config.max_gap_ms:g}; "
                         f"refusing to split, drop frames or invent feedback: {failures}")
    resampled = {k: v[trim_start:trim_stop] for k, v in feedback_values.items()}
    for topic, s in signals.items():
        if topic in COMMAND_STATE or topic in resampled:
            continue
        if "/eef_" in topic:
            value = pose_interpolate(s, grid, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
        else:
            value = interpolate(s.times, s.values, grid, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
        resampled[topic] = value
    fallback_counts, fallback_ranges = {}, {}
    for topic, (state_topic, columns) in COMMAND_STATE.items():
        age = config.gripper_max_gap_ms if "gripper_target" in topic else config.command_max_age_ms
        resampled[topic], fallback = command_or_state(
            signals[topic], grid, resampled[state_topic][:, columns], round(age * 1e6))
        fallback_counts[topic] = int(np.sum(fallback))
        fallback_ranges[topic] = [{"start_ns": int(grid[a]), "end_ns": int(grid[b-1]), "frames": b-a}
                                  for a, b in runs(fallback)]
    r = resampled
    columns = {
        "observation.state": np.column_stack((r["/tj/joint_states"][:, :7], r["/info/gripper_feedback_L"],
                                                r["/tj/joint_states"][:, 7:], r["/info/gripper_feedback_R"])),
        "observation.action": np.column_stack((r["/tj/control/joint_cmd_A"], r["/info/gripper_target_L"],
                                                 r["/tj/control/joint_cmd_B"], r["/info/gripper_target_R"])),
        "observation.wrench": np.column_stack((r["/tj/info/wrench_left"], r["/tj/info/wrench_right"])),
        "observation.eef": np.column_stack((r["/tj/info/eef_left"], r["/tj/info/eef_right"])),
    }
    result = [(grid, {k: v.astype(np.float32) for k, v in columns.items()}, video_indices)]
    report = {
        "intersection_start_ns": int(start), "intersection_end_ns": int(end),
        "grid_frames": len(original_grid), "kept_frames": len(grid), "dropped_frames": 0,
        "trimmed_frames": len(original_grid) - len(grid),
        "trim": {"enabled": config.trim_stationary, "start_frames": trim_start,
                 "end_frames": len(original_grid) - trim_stop,
                 "start_seconds": trim_start / config.fps,
                 "end_seconds": (len(original_grid) - trim_stop) / config.fps,
                 "joint_threshold_rad": config.motion_joint_threshold_rad,
                 "gripper_threshold": config.motion_gripper_threshold,
                 "output_start_ns": int(grid[0]), "output_end_ns": int(grid[-1])},
        "segments": [{"start_ns": int(grid[0]), "end_ns": int(grid[-1]), "frames": len(grid)}],
        "invalid_frames_by_signal": rejected, "signal_ranges_ns": ranges,
        "command_fallback_policy": "current_aligned_state",
        "command_fallback_frames": fallback_counts, "command_fallback_ranges": fallback_ranges,
        "video_max_age_ms": float(np.max(grid - video_times[video_indices]) / 1e6),
    }
    return result, report
