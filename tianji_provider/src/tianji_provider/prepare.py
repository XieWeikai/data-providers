"""Explicit cache writer, separate from the read-only source/provider boundary."""

from dataclasses import asdict, dataclass
from contextlib import closing
from pathlib import Path
from fractions import Fraction
import json
import time

import av
import numpy as np
import pyarrow.parquet as pq

from .media import CAMERAS, RGBStatistics, crop, decode_quad
from .reader import bag_files, discover, video_files
from .source import FORMAT, TianjiSource, features
from .execution import iter_parallel, worker_count

H264_OPTIONS = {"preset": "veryfast", "crf": "18", "bf": "0"}


@dataclass(frozen=True)
class VideoEpisode:
    index: int
    length: int
    indices: np.ndarray
    videos: dict


@dataclass(frozen=True)
class VideoJob:
    bag: str
    paths: tuple
    width: int
    height: int
    fps: int
    episodes: tuple


def fingerprint(root):
    result = []
    for bag in discover(root):
        for path in (*bag_files(bag / "data"), *video_files(bag)):
            stat = path.stat()
            result.append([str(path), stat.st_size, stat.st_mtime_ns])
    return result


def prepare(root, destination, config, *, workers=None):
    """Decode each raw quad stream once for all segments and all four cameras.

    The manifest is written last. An interrupted cache cannot be opened as a
    valid source, and an existing directory is never overwritten automatically.
    """
    root, destination = Path(root).resolve(), Path(destination).resolve()
    worker_count(workers, 0)  # Validate execution options even on a cache hit.
    if destination == root or destination.is_relative_to(root):
        raise ValueError("The cache must be outside the raw recording directory")
    options = asdict(config)
    options.pop("task")
    origin = fingerprint(root)
    manifest_path = destination / "tianji-cache.json"
    if manifest_path.is_file():
        old = json.loads(manifest_path.read_text())
        if old.get("format") == FORMAT and old.get("alignment_config") == options and old.get("source_fingerprint") == origin:
            # Reopen verifies all referenced resources still exist.
            TianjiSource(destination, config).metadata
            return old
        raise ValueError("Existing cache belongs to different data/options; choose a new destination")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite an existing/incomplete cache: {destination}")
    alignment_started = time.perf_counter()
    source = TianjiSource(root, config, _collect_image_stats=False, _workers=workers)
    episodes = source.episodes
    alignment_seconds = time.perf_counter() - alignment_started
    destination.mkdir(parents=True)
    started = time.perf_counter()
    entries = []
    for ep in episodes:
        folder = destination / f"episode_{ep.index:06d}"
        folder.mkdir()
        pq.write_table(source.read_episode(ep), folder / "data.parquet")
        a = source.audit(ep)
        np.savez(folder / "alignment.npz", absolute_time_ns=a["absolute_time_ns"],
                 video_packet_index=a["video_packet_index"], video_time_ns=a["video_time_ns"],
                 wrench_time_ns=a["wrench_time_ns"], wrench_sample_time_ns=a["wrench_sample_time_ns"])
        entries.append({"index": ep.index, "length": ep.length, "stats": dict(ep.stats),
                        "data": str((folder / "data.parquet").relative_to(destination)),
                        "audit": str((folder / "alignment.npz").relative_to(destination)),
                        "videos": {f"observation.image.{c}": str((folder / f"{c}.mp4").relative_to(destination)) for c in CAMERAS}})
    tables_seconds = time.perf_counter() - started
    video_started = time.perf_counter()
    jobs = []
    for bag in discover(root):
        group = [ep for ep in episodes if ep.data_path == bag]
        first = group[0].videos["observation.image.head_left"]
        # Send only this recording's paths and indices. Pickling source would
        # copy the entire batch of numeric tables/audits into every worker.
        jobs.append(VideoJob(bag.name, first.paths, first.width, first.height, config.fps,
            tuple(VideoEpisode(ep.index, ep.length, source.audit(ep)["video_packet_index"],
                               {key: destination / value for key, value in entries[ep.index]["videos"].items()})
                  for ep in group)))
    recordings = [None] * len(jobs)
    video_workers = worker_count(workers, len(jobs))
    # Start expensive videos early to avoid a few long recordings keeping an
    # otherwise idle pool alive at the end. Output and manifest order stay put.
    schedule = sorted(range(len(jobs)), key=lambda i: sum(ep.length for ep in jobs[i].episodes), reverse=True)
    with closing(iter_parallel(_encode_group, [jobs[i] for i in schedule], workers=workers)) as completed:
        for scheduled_index, result in completed:
            job_index = schedule[scheduled_index]
            recording, image_stats = result
            recordings[job_index] = recording
            for ep_index, stats in image_stats.items():
                entries[ep_index]["stats"].update(stats)
            print(f"Encoded {recording['bag']}: {recording['video_seconds']:.2f} s, "
                  f"{recording['frames'] / recording['video_seconds']:.2f} dataset frames/s", flush=True)
    video_seconds = time.perf_counter() - video_started
    w = source.metadata.features["observation.image.head_left"]["shape"][1]
    h = source.metadata.features["observation.image.head_left"]["shape"][0]
    manifest = {"format": FORMAT, "alignment_config": options, "source_fingerprint": origin,
                "features": features(w, h, config.fps, "h264", wrench_fps=config.wrench_fps), "episodes": entries, "reports": source.reports,
                "video_prepare_seconds": time.perf_counter() - started,
                "timings": {"source_alignment_seconds": alignment_seconds,
                            "table_write_seconds": tables_seconds,
                            "video_decode_encode_statistics_seconds": video_seconds,
                            "video_workers": video_workers,
                            "recordings": recordings},
                "video_encoding": {"codec": "h264", "encoder": "libx264", "pixel_format": "yuv420p",
                                   "crf": 18, "preset": "veryfast", "b_frames": 0,
                                   "intermediate_lossy_encoding": False},
                "image_statistics": "RGB channel histograms before H.264 encoding, all selected pixels/frames"}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _encode_group(job):
    """One decoder/four encoders per worker; no numeric source crosses processes."""
    started = time.perf_counter()
    episodes, fps = job.episodes, job.fps
    frames = sum(ep.length for ep in episodes)
    print(f"Encoding {job.bag}: {len(episodes)} episodes, {frames} frames", flush=True)
    ep_cursor, output_index, outputs, histograms = 0, 0, {}, {}
    image_stats = {}
    wanted = episodes[0].indices

    def begin(ep):
        for camera in CAMERAS:
            output = av.open(str(ep.videos[f"observation.image.{camera}"]), "w")
            stream = output.add_stream("libx264", rate=fps)
            stream.width, stream.height, stream.pix_fmt = job.width, job.height, "yuv420p"
            stream.options = H264_OPTIONS.copy()
            stream.codec_context.thread_count = 1
            stream.codec_context.gop_size = fps
            outputs[camera] = (output, stream)
            # Fail on ignored quality options instead of silently using defaults.
            output.start_encoding()
            if stream.codec_context.options:
                raise ValueError(f"H.264 encoder ignored options: {stream.codec_context.options}")
            histograms[camera] = RGBStatistics()

    def finish(ep):
        for camera, (out, stream) in outputs.items():
            for packet in stream.encode(None):
                out.mux(packet)
            out.close()
            image_stats.setdefault(ep.index, {})[f"observation.image.{camera}"] = histograms[camera].result()
        outputs.clear()

    try:
        begin(episodes[ep_cursor])
        # Consume through EOF even after the last selected frame: validate the
        # one-packet/one-frame contract and detect corrupt discarded tails too.
        for frame_index, frame in decode_quad(job.paths):
            if ep_cursor >= len(episodes) or frame_index < wanted[output_index]:
                continue
            rgb = frame.to_ndarray(format="rgb24")
            tiles = {camera: crop(rgb, camera) for camera in CAMERAS}
            while ep_cursor < len(episodes) and wanted[output_index] == frame_index:
                ep = episodes[ep_cursor]
                stop = int(np.searchsorted(wanted, frame_index, side="right"))
                repeats = stop - output_index
                for camera, tile in tiles.items():
                    out, stream = outputs[camera]
                    histograms[camera].add(tile, repeats)
                    for k in range(output_index, stop):
                        image = av.VideoFrame.from_ndarray(tile, format="rgb24")
                        image.pts, image.time_base = k, Fraction(1, fps)
                        for packet in stream.encode(image):
                            out.mux(packet)
                output_index = stop
                if output_index == ep.length:
                    finish(ep)
                    ep_cursor += 1
                    if ep_cursor < len(episodes):
                        wanted = episodes[ep_cursor].indices
                        output_index = 0
                        begin(episodes[ep_cursor])
                    else:
                        break
        if ep_cursor != len(episodes):
            raise ValueError("Video ended before all aligned rows could be encoded")
    finally:
        for out, stream in outputs.values():
            out.close()
    return ({"bag": job.bag, "frames": frames, "video_seconds": time.perf_counter() - started}, image_stats)
