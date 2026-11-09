"""One complete episode with strict observations and four recorded action streams."""

from fractions import Fraction
from functools import lru_cache

import mcap_ops as ops
import numpy as np
from scipy.interpolate import interp1d
from scipy.signal import firwin
from scipy.spatial.transform import Rotation, Slerp


def uniform_grid(start, end, fps):
    """Round each rational offset independently: no float epoch or accumulated drift."""
    count = (int(end) - int(start)) * fps // 1_000_000_000 + 1
    if count < 1:
        raise ValueError("Signals have no common time interval")
    points = ops.uniform_grid(int(start), int(end), fps, 1, count)
    # The library grid is half-open. Only add the endpoint if its rational tick exists.
    if len(points) < count:
        points.append(int(end))
    return np.asarray(points, dtype=np.int64)


def _support(times, grid, max_gap_ns=0, *, recorded=True):
    support = ops.sample_support(np.ascontiguousarray(times, dtype=np.int64),
                                 np.ascontiguousarray(grid, dtype=np.int64), max_gap_ns)
    if recorded and ((support["previous"] < 0).any() or (support["next"] < 0).any()):
        raise ValueError("Extrapolation outside a signal's recorded interval is forbidden")
    return support


def previous_indices(times, grid):
    return _support(times, grid)["previous"]


def output_grids(start, end, config):
    """Partition a single rational high-rate grid into complete causal windows.

    A row is anchored at the last sample of its window. No padding, overlapping
    windows, future samples, or phase drift when the periods are fractional ns.
    At most N-1 samples at the end are omitted to keep the feature shape fixed.
    """
    size = config.wrench_fps // config.fps
    high = uniform_grid(start, end, config.wrench_fps)
    windows = high[:len(high) // size * size].reshape(-1, size)
    return windows[:, -1], windows


def valid_mask(times, grid, max_gap_ns, causal=False):
    support = _support(times, grid, max_gap_ns)
    if causal:
        return grid - times[support["previous"]] <= max_gap_ns
    return support["supported"]


def runs(mask):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    return [(int(a), int(b)) for a, b in edges.reshape(-1, 2)]


COMMAND_STATE = {
    "/tj/control/joint_cmd_A": ("/tj/joint_states", slice(0, 7)),
    "/tj/control/joint_cmd_B": ("/tj/joint_states", slice(7, 14)),
    "/control/gripperValueL": ("/info/gripper_feedback_L", slice(None)),
    "/control/gripperValueR": ("/info/gripper_feedback_R", slice(None)),
}
# Feedback may initialize joint targets only before their first recorded command.
JOINT_COMMAND_TOPICS = ("/tj/control/joint_cmd_A", "/tj/control/joint_cmd_B")
FEEDBACK_TOPICS = ("/tj/joint_states", "/info/gripper_feedback_L", "/info/gripper_feedback_R")
WRENCH_TOPICS = ("/tj/info/wrench_left", "/tj/info/wrench_right")
OBSERVATION_TOPICS = (*FEEDBACK_TOPICS, "/tj/info/eef_left", "/tj/info/eef_right", *WRENCH_TOPICS)


def command_age_ns(topic, config):
    # Joint targets persist until replaced; recording gaps do not expire them.
    if topic in JOINT_COMMAND_TOPICS:
        return None
    age = config.gripper_max_gap_ms if topic.startswith("/control/gripperValue") else config.command_max_age_ms
    return round(age * 1e6)


def command_samples(signal, grid, max_age_ns):
    """Causal lookup independent of a command stream's first/last timestamp.

    Empty streams and times before the first command have no usable sample.
    None disables expiry; an explicit age limit remains inclusive.
    """
    indices = _support(signal.times, grid, recorded=False)["previous"]
    valid = indices >= 0
    if max_age_ns is not None:
        valid[valid] = grid[valid] - signal.times[indices[valid]] <= max_age_ns
    return indices, valid


def command_coverage(signal, grid, max_age_ns, topic):
    """Allow same-frame feedback initialization for nonempty joint prefixes."""
    indices, valid = command_samples(signal, grid, max_age_ns)
    prefix = (indices < 0) if topic in JOINT_COMMAND_TOPICS and len(signal.times) else np.zeros(len(grid), dtype=bool)
    return indices, valid | prefix, prefix


def candidate_interval(signals, ranges, video_times, config):
    """Intersect required observations/video only; commands cannot hide gaps.

    Calibration, source status and event topics do not define sampling bounds.
    Command coverage is checked independently at every retained output point.
    """
    observations = [(int(signals[k].times[0]), int(signals[k].times[-1]))
                    for k in OBSERVATION_TOPICS]
    observations.append((int(video_times[0]), int(video_times[-1])))
    return max(v[0] for v in observations), min(v[1] for v in observations)


def moving_bounds(times, feedback, joint_threshold, gripper_threshold):
    """Find motion relative to stable edge poses; retain one boundary sample.

    Endpoint medians over 200 ms reject isolated feedback noise. Displacement
    from the endpoint pose, not per-frame velocity, also detects slow motion.
    Only the outer edges are trimmed; interior pauses remain in the episode.
    """
    threshold = np.r_[np.full(14, joint_threshold), gripper_threshold, gripper_threshold]
    window = 200_000_000
    bounds = ops.motion_bounds(np.ascontiguousarray(times), feedback, threshold, window)
    if bounds is not None:
        return bounds
    # Preserve the two original diagnostic reasons on the rare empty-selection path.
    for edge in (times <= times[0] + window, times >= times[-1] - window):
        if not np.any(np.abs(feedback - np.median(feedback[edge], axis=0)) > threshold):
            raise ValueError("No motion exceeds the joint/gripper thresholds; use --no-trim-stationary or lower the thresholds")
    raise ValueError("Motion interval contains fewer than two output frames")


def command_targets(signal, grid, max_age_ns, topic, *, prefix_values=None):
    """Initialize a joint prefix from aligned feedback, then hold real targets."""
    indices, valid, prefix = command_coverage(signal, grid, max_age_ns, topic)
    if not valid.all():
        raise ValueError(f"Required action command missing/stale: {topic}, {int(np.sum(~valid))} frames")
    if prefix.any() and (prefix_values is None or prefix_values.shape != (len(grid), signal.values.shape[1])):
        raise ValueError(f"Joint command prefix requires matching aligned feedback: {topic}")
    targets = np.empty((len(grid), signal.values.shape[1]), dtype=signal.values.dtype)
    targets[~prefix] = signal.values[indices[~prefix]]
    if prefix.any():
        targets[prefix] = prefix_values[prefix]
    return targets


@lru_cache(maxsize=32)
def _kaiser_taps(rate):
    # Match the existing resample_poly default kernel at the reduced upsampled rate.
    taps = firwin(20 * rate + 1, 1.0 / rate, window=("kaiser", 5.0))
    taps.flags.writeable = False
    return taps


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
            y = ops.polyphase_fir(sampled, ratio.numerator, ratio.denominator,
                                   _kaiser_taps(max(ratio.numerator, ratio.denominator)))
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


def _align_contiguous(signals, ranges, video_times, config, *, bounds=None, grid_only=False):
    missing = [k for k in COMMAND_STATE if k not in signals or not len(signals[k].times)]
    if missing:
        raise ValueError(f"Required action command has no messages: {missing}")
    ranges = {**ranges, "video": [int(video_times[0]), int(video_times[-1]), len(video_times)]}
    start, end = candidate_interval(signals, ranges, video_times, config)
    if bounds is not None:
        start,end=max(start,int(bounds[0])),min(end,int(bounds[1]))
    grid, wrench_grid = output_grids(start, end, config)
    if len(grid) < 2:
        raise ValueError("The common interval must contain at least two output frames")
    masks = {}
    for topic, signal in signals.items():
        if topic in WRENCH_TOPICS:
            masks[topic] = valid_mask(signal.times, wrench_grid.ravel(), round(config.max_gap_ms * 1e6)).reshape(wrench_grid.shape).all(axis=1)
        elif topic not in COMMAND_STATE:
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
    wrench_grid = wrench_grid[trim_start:trim_stop]
    video_indices = video_indices[trim_start:trim_stop]
    rejected = {k: int(np.sum(~mask[trim_start:trim_stop])) for k, mask in masks.items()}
    failures = {topic: count for topic, count in rejected.items() if count}
    if failures:
        raise ValueError(f"Missing observations exceed --max-gap-ms={config.max_gap_ms:g}; "
                         f"refusing to split, drop frames or invent feedback: {failures}")
    if grid_only:
        # Source/command eligibility is resolved before constructing training actions.
        result = [(grid, {}, video_indices)]
    else:
        command_valid = {k: command_coverage(signals[k], grid, command_age_ns(k, config), k)[1]
                         for k in COMMAND_STATE}
        command_failures = {k: int(np.sum(~valid)) for k, valid in command_valid.items() if not valid.all()}
        if command_failures:
            raise ValueError(f"Required action commands missing/stale at output frames: {command_failures}; "
                             "joint prefixes use aligned feedback and later targets do not expire; gripper targets must be fresh; "
                             "refusing whole-stream substitution or dropped frames")
        resampled = {k: v[trim_start:trim_stop] for k, v in feedback_values.items()}
        for topic, s in signals.items():
            if topic in COMMAND_STATE or topic in resampled:
                continue
            if topic in WRENCH_TOPICS:
                # Preserve actual measured values: no FIR, average, or interpolation.
                # Validity was checked at EVERY high-rate point, not only row anchors.
                value = s.values[previous_indices(s.times, wrench_grid.ravel())].reshape(*wrench_grid.shape, 6)
            elif "/eef_" in topic:
                value = pose_interpolate(s, grid, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
            else:
                value = interpolate(s.times, s.values, grid, config.fps, round(config.max_gap_ms * 1e6), config.state_resampling)
            resampled[topic] = value
        for topic in COMMAND_STATE:
            feedback_topic, joints = COMMAND_STATE[topic]
            initial = resampled[feedback_topic][:, joints] if topic in JOINT_COMMAND_TOPICS else None
            resampled[topic] = command_targets(signals[topic], grid, command_age_ns(topic, config), topic,
                                               prefix_values=initial)
        r = resampled
        columns = {
            "observation.state": np.column_stack((r["/tj/joint_states"][:, :7], r["/info/gripper_feedback_L"],
                                                    r["/tj/joint_states"][:, 7:], r["/info/gripper_feedback_R"])),
            "observation.action": np.column_stack((r["/tj/control/joint_cmd_A"], r["/control/gripperValueL"],
                                                     r["/tj/control/joint_cmd_B"], r["/control/gripperValueR"])),
            "observation.wrench_windows": np.concatenate((r[WRENCH_TOPICS[0]], r[WRENCH_TOPICS[1]]), axis=2),
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
        "wrench_sampling": {
            "fps": config.wrench_fps, "samples_per_frame": config.wrench_fps // config.fps,
            "policy": "previous_raw_sample", "filtered": False,
            "window_anchor": "last_sample", "grid_origin_ns": int(start),
            "first_sample_ns": int(wrench_grid[0, 0]), "last_sample_ns": int(wrench_grid[-1, -1]),
            "first_window_index": trim_start,
            "max_sample_age_ms": {k: float(np.max(wrench_grid.ravel() - signals[k].times[
                previous_indices(signals[k].times, wrench_grid.ravel())]) / 1e6) for k in WRENCH_TOPICS},
        },
        "command_policy": "all_four_recorded_commands_required",
        "command_fallback_policy": "joint_prefix_same_frame_feedback_only",
        "command_coverage_policy": "joint_prefix_initialized_then_hold_gripper_fresh",
        "joint_prefix_initialization": {
            k: {"frames": int(np.sum(grid < signals[k].times[0])),
                "start_ns": int(grid[0]),
                "end_ns": int(grid[grid < signals[k].times[0]][-1]),
                "first_command_ns": int(signals[k].times[0]),
                "feedback_topic": COMMAND_STATE[k][0],
                "policy": "same_frame_joint_feedback_hold_target"}
            for k in JOINT_COMMAND_TOPICS if np.any(grid < signals[k].times[0])},
        "command_max_age_ms_by_topic": {k: None if command_age_ns(k, config) is None
                                       else command_age_ns(k, config) / 1e6 for k in COMMAND_STATE},
        "minimum_valid_command_topics": 4,
        "video_max_age_ms": float(np.max(grid - video_times[video_indices]) / 1e6),
    }
    return result, report


def align(signals, ranges, video_times, config):
    if getattr(config, 'processing_mode', 'standard') != 'dagger':
        return _align_contiguous(signals,ranges,video_times,config)
    from dataclasses import replace
    from .dagger import analyze, annotations, review_summary
    analysis=analyze(signals,config)
    segments,report=_align_contiguous(signals,ranges,video_times,config,grid_only=config.dagger_export=='expert_segments')
    (grid,values,indices),=segments
    marks=annotations(signals,grid,config,analysis)
    analysis['supervision_eligibility']={'frames':len(grid),
        'component_valid_frames':[int(marks['dagger.action_valid'][:,i].sum()) for i in (0,7,8,15)],
        'full_supervision_frames':int(marks['dagger.supervision_valid'].sum())}
    report['dagger_analysis']=analysis
    report['dagger_review']=review_summary(analysis,config)
    report['dagger_supervision_frames']=int(marks['dagger.supervision_valid'].sum())
    if config.dagger_export=='annotate':
        values.update(marks)
        report['dagger_training_contract']='annotation_only_requires_explicit_training_filter'
        return segments,report
    selected=[];child_reports=[];selection=[]
    size=config.wrench_fps//config.fps
    # Reconstruct each selected contiguous region separately. Use linear state
    # reconstruction here so an FIR cannot blend across an excluded boundary.
    segment_config=replace(config,trim_stationary=False,state_resampling='linear')
    for a,b in runs(marks['dagger.supervision_valid'][:,0]):
        if b-a<config.dagger_min_segment_frames: continue
        start=int(grid[a])-round((size-1)*1e9/config.wrench_fps)
        end=int(grid[b-1])
        child,detail=_align_contiguous(signals,ranges,video_times,segment_config,bounds=(start,end))
        (cg,cv,ci),=child
        cm=annotations(signals,cg,config,analysis)
        if not cm['dagger.supervision_valid'].all():
            raise ValueError('Reconstructed expert segment crosses an invalid action boundary')
        cv.update(cm);selected.append((cg,cv,ci));child_reports.append(detail)
        selection.append({'start_ns':int(cg[0]),'end_ns':int(cg[-1]),'frames':len(cg),
                          'source_start_frame':a,'source_stop_frame':b})
    if not selected:
        counts=analysis['supervision_eligibility']['component_valid_frames']
        raise ValueError(f'No continuous fully supervised expert segment: valid frames [left arm, left gripper, right arm, right gripper]={counts}; prior-source held targets cannot become expert labels')
    report.update(kept_frames=sum(len(s[0]) for s in selected),segments=selection,
                  segment_alignments=child_reports,
                  dagger_excluded_frames=len(grid)-sum(len(s[0]) for s in selected),
                  dagger_training_contract='physically_separated_expert_episodes_no_invalid_targets')
    report['dagger_supervision_frames']=report['kept_frames']
    return selected,report
