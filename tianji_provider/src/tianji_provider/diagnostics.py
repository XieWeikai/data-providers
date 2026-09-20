"""Read-only, per-recording diagnostics using the converter's timing policies."""

from dataclasses import asdict
from pathlib import Path
import time

import numpy as np

from . import __version__
from .alignment import COMMAND_STATE, FEEDBACK_TOPICS, align, previous_indices, runs, uniform_grid
from .media import decode_quad, dimensions
from .reader import read_numeric, read_video_times, video_files

REPORT_FORMAT = "tianji-diagnostics-v1"


def _issue(code, severity, message, **details):
    return {"code": code, "severity": severity, "message": message, **details}


def discover_for_check(root):
    """Include recognizable but incomplete recordings that normal discovery skips."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Recording directory does not exist: {root}")
    if (root / "tianji-cache.json").exists():
        raise ValueError("check expects raw Thor recordings, not a prepared cache")
    def recognizable(path):
        return path.name.startswith("my_bag-") or (path / "data").exists() or (path / "video").exists()
    if recognizable(root):
        return (root,)
    recordings = tuple(sorted(p for p in root.iterdir() if p.is_dir() and recognizable(p)))
    if not recordings:
        raise ValueError(f"No recognizable Thor recordings under {root}")
    return recordings


def timing_issues(signals, ranges, video_times, config, alignment=None):
    """Report every sampling gap, including all topics in a shared outage.

    Feedback must support the entire pre-trim interval. Other observations only
    block conversion when they affect retained output points. Command pauses
    are informational because the converter supplies the same-frame state.
    """
    all_ranges = {**ranges, "video": [int(video_times[0]), int(video_times[-1]), len(video_times)]}
    start, end = max(v[0] for v in all_ranges.values()), min(v[1] for v in all_ranges.values())
    grid = uniform_grid(start, end, config.fps)
    output = grid
    if alignment is not None:
        trim = alignment["trim"]
        output = grid[(grid >= trim["output_start_ns"]) & (grid <= trim["output_end_ns"])]
    issues = []
    for topic, times in [*[(k, s.times) for k, s in signals.items()], ("video", video_times)]:
        if topic in COMMAND_STATE:
            age_ms = config.gripper_max_gap_ms if "gripper_target" in topic else config.command_max_age_ms
            indices = previous_indices(times, output)
            fallback = output - times[indices] > round(age_ms * 1e6)
            for a, b in runs(fallback):
                issues.append(_issue("command_pause_filled", "info", "Paused command uses same-frame state",
                    topic=topic, start_ns=int(output[a]), end_ns=int(output[b-1]),
                    start_seconds=float((output[a]-start)/1e9), end_seconds=float((output[b-1]-start)/1e9),
                    affected_frames=b-a, threshold_ms=age_ms,
                    last_message_ns=int(times[indices[a]]), handling="current_aligned_state"))
            continue
        threshold = round(config.max_gap_ms * 1e6)
        scope = grid if topic in FEEDBACK_TOPICS else output
        for index in np.flatnonzero(np.diff(times) > threshold):
            left, right = int(times[index]), int(times[index+1])
            invalid_after = left + threshold if topic == "video" else left
            affected = scope[(scope > invalid_after) & (scope < right)]
            count = len(affected)
            issues.append(_issue("video_gap" if topic == "video" else "observation_gap",
                "error" if count else "warning", "Recorded samples exceed the configured observation gap",
                topic=topic, start_ns=left, end_ns=right, gap_ms=(right-left)/1e6,
                start_seconds=(left-start)/1e9, end_seconds=(right-start)/1e9,
                threshold_ms=config.max_gap_ms, affected_frames=count,
                first_affected_ns=int(affected[0]) if count else None,
                last_affected_ns=int(affected[-1]) if count else None,
                handling="reject_recording" if count else "outside_required_output_samples"))
    return issues


def check_recording(bag, config, *, deep_video=True):
    """Inspect numeric and video branches independently; never change input files."""
    bag = Path(bag).resolve()
    started = time.monotonic()
    issues, stages = [], []
    signals = ranges = video_times = paths = alignment = None
    media = {"full_decode": deep_video, "decoded_frames": None, "quad_size": None}
    try:
        signals, ranges, clocks, calibration = read_numeric(bag)
        stages.append("numeric_schema_values_clocks_calibration")
    except Exception as error:
        issues.append(_issue("numeric_read_error", "error", str(error), exception_type=type(error).__name__))
    try:
        paths = video_files(bag)
        video_times = read_video_times(paths)
        media["message_count"] = len(video_times)
        stages.append("video_schema_and_timestamps")
    except Exception as error:
        issues.append(_issue("video_read_error", "error", str(error), exception_type=type(error).__name__))
    if paths is not None:
        try:
            if deep_video:
                count, size = 0, None
                for _, frame in decode_quad(paths):
                    current = (frame.width, frame.height)
                    if current[0] % 4 or current[1] % 4:
                        raise ValueError("Quad video dimensions must be divisible by four")
                    if size is not None and current != size:
                        raise ValueError(f"Video dimensions changed from {size} to {current}")
                    size = current
                    count += 1
                if not count or video_times is not None and count != len(video_times):
                    raise ValueError("Decoded video frame count differs from timestamped messages")
                media.update(decoded_frames=count, quad_size=list(size))
                stages.append("complete_video_decode_frame_mapping_and_dimensions")
            else:
                width, height = dimensions(paths)
                media["quad_size"] = [width*2, height*2]
                stages.append("first_video_frame_only")
        except Exception as error:
            issues.append(_issue("video_decode_error", "error", str(error), exception_type=type(error).__name__))
    if signals is not None and video_times is not None:
        try:
            _, alignment = align(signals, ranges, video_times, config)
            stages.append("alignment_state_fallback_and_stationary_trim")
        except Exception as error:
            issues.append(_issue("alignment_error", "error", str(error), exception_type=type(error).__name__))
        try:
            issues.extend(timing_issues(signals, ranges, video_times, config, alignment))
        except Exception as error:
            issues.append(_issue("timeline_error", "error", str(error), exception_type=type(error).__name__))
    if alignment is not None and alignment["trimmed_frames"]:
        issues.append(_issue("stationary_edges_trimmed", "info", "Stationary edges trimmed; interior pauses retained",
                             affected_frames=alignment["trimmed_frames"], **alignment["trim"]))
    # A failed alignment is the authoritative verdict. Preserve its complete
    # message alongside structured gaps, rather than hiding non-gap failures.
    valid = not any(i["severity"] == "error" for i in issues)
    return {"bag": bag.name, "path": str(bag), "valid": valid,
            "frames": alignment["kept_frames"] if valid and alignment is not None else 0,
            "issues": issues, "completed_checks": stages, "video": media,
            "alignment": alignment, "seconds": time.monotonic()-started}


def check_dataset(root, config, *, deep_video=True, progress=None):
    """Check all recordings even when one fails; caller owns report persistence."""
    started = time.monotonic()
    bags = discover_for_check(root)
    results, expected_size = [], None
    for index, bag in enumerate(bags):
        result = check_recording(bag, config, deep_video=deep_video)
        size = result["video"]["quad_size"]
        if size is not None:
            if expected_size is None:
                expected_size = size
            elif size != expected_size:
                result["issues"].append(_issue("inconsistent_video_dimensions", "error",
                    f"Expected {expected_size}, got {size}"))
                result.update(valid=False, frames=0)
        results.append(result)
        if progress is not None:
            progress(index+1, len(bags), result)
    valid_count = sum(r["valid"] for r in results)
    return {"format": REPORT_FORMAT, "provider_version": __version__, "root": str(Path(root).resolve()),
            "config": asdict(config), "deep_video": deep_video,
            "valid": valid_count == len(results), "recordings": len(results),
            "valid_recordings": valid_count, "invalid_recordings": len(results)-valid_count,
            "valid_frames": sum(r["frames"] for r in results), "results": results,
            "seconds": time.monotonic()-started}
