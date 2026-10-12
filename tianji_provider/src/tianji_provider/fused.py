"""Explicit checked preparation: read numeric/video data once, publish no failures.

This optional API combines work within one call. It never caches implicit state
between calls and does not change the independent check or prepare interfaces.
With default strict selection, an invalid recording produces the complete
diagnostic report but no cache manifest. Explicit valid-only selection retains
every rejection in the report and manifest. Intermediate files remain in the
new destination for diagnosis.
"""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from itertools import chain
from pathlib import Path
import json
import time

import numpy as np
import pyarrow.parquet as pq
import yaml

from . import __version__
from .alignment import WRENCH_TOPICS, align, output_grids, previous_indices
from .diagnostics import REPORT_FORMAT, _issue, discover_for_check, timing_issues
from .execution import iter_parallel, worker_count
from .encoding import LIBX264, encoding_workers, select_encoder, validate_encoder_request, with_encoder_recovery
from .media import CAMERAS, decode_quad, dimensions
from .prepare import VideoEpisode, VideoJob, _encode_group, fingerprint
from .reader import read_numeric, read_video_times, video_files
from .source import FORMAT, arrow_table, features, statistics


def _snapshot(root):
    """Include metadata, source identities and selection, not just MCAP mtimes."""
    result = []
    for bag in discover_for_check(root):
        resolved = bag.resolve()
        files = []
        for path in sorted(resolved.rglob("*")):
            if path.is_file() and path.suffix in {".mcap", ".yaml", ".json"}:
                stat = path.stat()
                files.append((str(path), str(path.resolve()), stat.st_dev, stat.st_ino,
                              stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        result.append((str(bag), str(resolved), files))
    return result


def _work_hint(bag):
    """A scheduling hint only; bad metadata is still diagnosed by the worker."""
    try:
        info = yaml.safe_load((bag / "data/metadata.yaml").read_text())["rosbag2_bagfile_information"]
        return max(0, int(info.get("duration", {}).get("nanoseconds", 0)))
    except Exception:
        return 0


def _select_batch_encoder(requested, bags, fps):
    if requested == "libx264":
        return select_encoder(requested, 0, 0, fps)
    for bag in bags:
        try:
            width, height = dimensions(video_files(bag))
        except Exception:
            # Broken source videos remain errors in the complete per-bag
            # report; they must not prevent checking later recordings.
            continue
        return select_encoder(requested, width, height, fps)
    # No usable first frame means nothing can be encoded. Still produce the
    # full diagnostic report instead of replacing data errors with a probe.
    return LIBX264, {"requested": requested, "selected": None, "probed": False,
                    "reason": "No usable first video frame; complete diagnostics still required"}


class _VideoInspection:
    """Validate every frame, including frames outside the selected output."""

    def __init__(self, expected_count):
        self.expected_count = expected_count
        self.count = 0
        self.size = None
        self.error = None
        self.complete = False

    def frames(self, paths):
        try:
            for index, frame in decode_quad(paths):
                current = (frame.width, frame.height)
                if current[0] % 4 or current[1] % 4:
                    raise ValueError("Quad video dimensions must be divisible by four")
                if self.size is not None and current != self.size:
                    raise ValueError(f"Video dimensions changed from {self.size} to {current}")
                self.size = current
                self.count += 1
                yield index, frame
            if not self.count or self.expected_count is not None and self.count != self.expected_count:
                raise ValueError("Decoded video frame count differs from timestamped messages")
            self.complete = True
        except Exception as error:
            self.error = error
            raise


def _checked_recording(job):
    """Each worker owns one raw bag, compact aligned arrays and its encoders."""
    original_bag, index, destination, config, encoder = job
    bag = original_bag.resolve()
    started = time.monotonic()
    issues, alignment_issues, stages = [], [], []
    signals = ranges = paths = video_times = alignment = segments = None
    media = {"full_decode": True, "decoded_frames": None, "quad_size": None}
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
    aligned_stage = False
    if signals is not None and video_times is not None:
        try:
            segments, alignment = align(signals, ranges, video_times, config)
            aligned_stage = True
        except Exception as error:
            alignment_issues.append(_issue("alignment_error", "error", str(error), exception_type=type(error).__name__))
        try:
            alignment_issues.extend(timing_issues(signals, ranges, video_times, config, alignment))
        except Exception as error:
            alignment_issues.append(_issue("timeline_error", "error", str(error), exception_type=type(error).__name__))

    payload = None
    if segments is not None and not any(item["severity"] == "error" for item in issues + alignment_issues):
        # The current error-only alignment policy produces exactly one episode
        # per recording. This mirrors the existing no-split format contract.
        (grid, values, indices), = segments
        _, windows = output_grids(alignment["intersection_start_ns"], alignment["intersection_end_ns"], config)
        first = alignment["wrench_sampling"]["first_window_index"]
        windows = windows[first:first + len(grid)]
        audit = {"absolute_time_ns": grid, "video_packet_index": indices,
                 "video_time_ns": video_times[indices], "wrench_time_ns": windows,
                 "wrench_sample_time_ns": np.stack([
                     signals[key].times[previous_indices(signals[key].times, windows.ravel())].reshape(windows.shape)
                     for key in WRENCH_TOPICS], axis=2)}
        report = deepcopy(alignment)
        report.update(bag=str(original_bag), timestamp_sources=clocks, gripper_calibration=calibration,
                      episode_indices=[index])
        payload = {"values": values, "audit": audit, "report": report, "length": len(grid)}
    # Do not hold high-frequency raw messages while encoding or send them back
    # through IPC. Only the retained arrays and necessary audit remain alive.
    signals = ranges = segments = None
    encoding = image_stats = None
    if paths is not None:
        inspection = _VideoInspection(len(video_times) if video_times is not None else None)
        with closing(inspection.frames(paths)) as frames:
            try:
                if payload is None:
                    for _ in frames:
                        pass
                else:
                    # Inspect the first frame in the same iterator used for the
                    # whole video; this avoids a separate dimensions decode.
                    first_frame = next(frames)
                    width, height = inspection.size
                    folder = destination / f"episode_{index:06d}"
                    folder.mkdir()
                    videos = {f"observation.image.{camera}": folder / f"{camera}.mp4" for camera in CAMERAS}
                    episode = VideoEpisode(index, payload["length"], payload["audit"]["video_packet_index"], videos)
                    video_job = VideoJob(original_bag.name, paths, width // 2, height // 2, config.fps, (episode,), encoder)
                    encoding, image_stats = _encode_group(video_job, decoder=lambda _: chain((first_frame,), frames))
                    payload.update(videos=videos, size=(width // 2, height // 2), image_stats=image_stats[index])
                if inspection.complete:
                    media.update(decoded_frames=inspection.count, quad_size=list(inspection.size))
                    stages.append("complete_video_decode_frame_mapping_and_dimensions")
            except Exception:
                if inspection.error is None:
                    # Encoder/filesystem failures are operational failures, not
                    # a fabricated data-quality error or a successful check.
                    raise
        if inspection.error is not None:
            error = inspection.error
            issues.append(_issue("video_decode_error", "error", str(error), exception_type=type(error).__name__))
    if aligned_stage:
        stages.append("alignment_state_fallback_and_stationary_trim")
    issues.extend(alignment_issues)
    if alignment is not None and alignment["trimmed_frames"]:
        issues.append(_issue("stationary_edges_trimmed", "info", "Stationary edges trimmed; interior pauses retained",
                             affected_frames=alignment["trimmed_frames"], **alignment["trim"]))
    valid = not any(item["severity"] == "error" for item in issues)
    result = {"bag": bag.name, "path": str(bag), "valid": valid,
              "frames": alignment["kept_frames"] if valid and alignment is not None else 0,
              "issues": issues, "completed_checks": stages, "video": media,
              "alignment": alignment, "seconds": time.monotonic() - started}
    return result, payload if valid else None, encoding


def check_and_prepare(root, destination, config, *, workers=None, progress=None,
                      video_encoder="auto", prepare_valid_only=False):
    """Check and prepare; auto retries a hardware failure as an all-software batch.

    Progress callbacks may restart at index one after an encoder retry. Only
    the returned complete report is authoritative, never a partial attempt.
    """
    return with_encoder_recovery(destination, video_encoder, lambda: _snapshot(Path(root).expanduser().resolve()),
        lambda codec, recovery: _check_and_prepare(root, destination, config, workers=workers,
            progress=progress, video_encoder=codec, prepare_valid_only=prepare_valid_only, recovery=recovery))


def _check_and_prepare(root, destination, config, *, workers, progress, video_encoder, prepare_valid_only, recovery):
    """Validate and prepare once; return ``report`` and successful ``manifest``.

    A fresh destination is required, including when input data fails checks.
    Report progress is emitted only after that recording's complete video has
    been checked. By default a failed report creates no cache manifest. The
    explicit prepare_valid_only option writes only valid recordings, retaining
    a failed all-input report and a complete selection/rejection record. It
    never changes thresholds or repairs bad data. Existing independent commands
    and the production web worker's strict selection policy are unchanged.
    """
    root, destination = Path(root).expanduser().resolve(), Path(destination).expanduser().resolve()
    worker_count(workers, 0)
    validate_encoder_request(video_encoder)
    if type(prepare_valid_only) is not bool:
        raise ValueError("prepare_valid_only must be an explicit boolean")
    if destination == root or destination.is_relative_to(root):
        raise ValueError("The cache must be outside the raw recording directory")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite an existing/incomplete cache: {destination}")
    started = time.monotonic()
    before = _snapshot(root)
    bags = discover_for_check(root)
    if any(destination == bag.resolve() or destination.is_relative_to(bag.resolve()) for bag in bags):
        raise ValueError("The cache must be outside every selected raw recording")
    encoder, encoder_selection = _select_batch_encoder(video_encoder, bags, config.fps)
    if recovery is not None:
        encoder_selection.update(recovery)
    parallelism = encoding_workers(workers, len(bags), encoder)
    destination.mkdir(parents=True)
    print(f"Checking and preparing recordings in one pass: {encoder.name}, {parallelism} workers", flush=True)
    jobs = [(bag, index, destination, config, encoder) for index, bag in enumerate(bags)]
    schedule = sorted(range(len(bags)), key=lambda index: _work_hint(bags[index]), reverse=True)
    results, payloads, recordings = [], [], []
    ready, expected_size = {}, None
    with closing(iter_parallel(_checked_recording, [jobs[index] for index in schedule], workers=parallelism)) as completed:
        for scheduled_index, item in completed:
            index = schedule[scheduled_index]
            ready[index] = item
            while len(results) in ready:
                result, payload, encoding = ready.pop(len(results))
                size = result["video"]["quad_size"]
                if size is not None:
                    if expected_size is None:
                        expected_size = size
                    elif size != expected_size:
                        result["issues"].append(_issue("inconsistent_video_dimensions", "error",
                            f"Expected {expected_size}, got {size}"))
                        result.update(valid=False, frames=0)
                results.append(result)
                payloads.append(payload)
                recordings.append(encoding)
                if progress is not None:
                    progress(len(results), len(bags), result)
    valid_count = sum(item["valid"] for item in results)
    report = {"format": REPORT_FORMAT, "provider_version": __version__, "root": str(root),
              "config": asdict(config), "deep_video": True, "valid": valid_count == len(results),
              "recordings": len(results), "valid_recordings": valid_count,
              "invalid_recordings": len(results) - valid_count,
              "valid_frames": sum(item["frames"] for item in results), "results": results,
              "seconds": time.monotonic() - started}
    if _snapshot(root) != before:
        raise ValueError("Source files or selection changed during checked preparation; refusing to publish cache")
    selection = {"mode": "explicit_valid_only" if prepare_valid_only else "all_selected_must_pass",
                 "recordings_checked": len(results), "valid_recordings": valid_count,
                 "invalid_recordings": len(results) - valid_count,
                 "rejected": [{"source_recording_index": index, "bag": item["bag"], "issues": item["issues"]}
                              for index, item in enumerate(results) if not item["valid"]]}
    if not valid_count or not report["valid"] and not prepare_valid_only:
        return {"report": report, "manifest": None, "selection": selection}

    entries, alignment_reports, offset = [], [], 0
    included = [(index, payloads[index]) for index, item in enumerate(results) if item["valid"]]
    selection["episode_mapping"] = []
    for index, (original_index, payload) in enumerate(included):
        length, values = payload["length"], payload["values"]
        values.update(timestamp=np.arange(length, dtype=np.float32) / config.fps,
                      frame_index=np.arange(length, dtype=np.int64), episode_index=np.full(length, index, dtype=np.int64),
                      index=np.arange(offset, offset + length, dtype=np.int64), task_index=np.zeros(length, dtype=np.int64))
        # Preserve worker-owned directories (including failed diagnostics)
        # rather than renaming over a rejected recording's partial output.
        folder = next(iter(payload["videos"].values())).parent
        pq.write_table(arrow_table(values), folder / "data.parquet")
        np.savez(folder / "alignment.npz", **payload["audit"])
        entries.append({"index": index, "length": length,
                        "stats": {**{key: statistics(value) for key, value in values.items()}, **payload["image_stats"]},
                        "data": str((folder / "data.parquet").relative_to(destination)),
                        "audit": str((folder / "alignment.npz").relative_to(destination)),
                        "videos": {key: str(value.relative_to(destination)) for key, value in payload["videos"].items()}})
        payload["report"]["episode_indices"] = [index]
        alignment_reports.append(payload["report"])
        selection["episode_mapping"].append({"source_recording_index": original_index,
                                            "bag": results[original_index]["bag"], "episode_index": index})
        offset += length
    options = asdict(config)
    options.pop("task")
    width, height = included[0][1]["size"]
    origin = [entry for original_index, _ in included for entry in fingerprint(bags[original_index])]
    manifest = {"format": FORMAT, "alignment_config": options, "source_fingerprint": origin,
                "features": features(width, height, config.fps, "h264", wrench_fps=config.wrench_fps),
                "episodes": entries, "reports": alignment_reports,
                "video_prepare_seconds": time.monotonic() - started,
                "timings": {"checked_prepare_seconds": time.monotonic() - started,
                            "video_workers": parallelism, "recordings": recordings},
                "video_encoding": encoder.metadata(), "video_encoder_selection": encoder_selection,
                "source_selection": selection,
                "image_statistics": "RGB channel histograms before H.264 encoding, all selected pixels/frames"}
    if _snapshot(root) != before:
        raise ValueError("Source files or selection changed before cache publication")
    (destination / "tianji-cache.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"report": report, "manifest": manifest, "selection": selection}
