"""Explicit cache writer, separate from the read-only source/provider boundary."""

from dataclasses import asdict
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

H264_OPTIONS = {"preset": "veryfast", "crf": "18", "bf": "0"}


def fingerprint(root):
    result = []
    for bag in discover(root):
        for path in (*bag_files(bag / "data"), *video_files(bag)):
            stat = path.stat()
            result.append([str(path), stat.st_size, stat.st_mtime_ns])
    return result


def prepare(root, destination, config):
    """Decode each raw quad stream once for all segments and all four cameras.

    The manifest is written last. An interrupted cache cannot be opened as a
    valid source, and an existing directory is never overwritten automatically.
    """
    root, destination = Path(root).resolve(), Path(destination).resolve()
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
    source = TianjiSource(root, config, _collect_image_stats=False)
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
                 video_packet_index=a["video_packet_index"], video_time_ns=a["video_time_ns"])
        entries.append({"index": ep.index, "length": ep.length, "stats": dict(ep.stats),
                        "data": str((folder / "data.parquet").relative_to(destination)),
                        "audit": str((folder / "alignment.npz").relative_to(destination)),
                        "videos": {f"observation.image.{c}": str((folder / f"{c}.mp4").relative_to(destination)) for c in CAMERAS}})
    tables_seconds = time.perf_counter() - started
    video_started = time.perf_counter()
    recordings = []
    for bag in discover(root):
        group = [ep for ep in episodes if ep.data_path == bag]
        frames = sum(e.length for e in group)
        print(f"Encoding {bag.name}: {len(group)} episodes, {frames} frames", flush=True)
        bag_started = time.perf_counter()
        _encode_group(source, group, destination, entries)
        elapsed = time.perf_counter() - bag_started
        recordings.append({"bag": bag.name, "frames": frames, "video_seconds": elapsed})
        print(f"Encoded {bag.name}: {elapsed:.2f} s, {frames / elapsed:.2f} dataset frames/s", flush=True)
    video_seconds = time.perf_counter() - video_started
    w = source.metadata.features["observation.image.head_left"]["shape"][1]
    h = source.metadata.features["observation.image.head_left"]["shape"][0]
    manifest = {"format": FORMAT, "alignment_config": options, "source_fingerprint": origin,
                "features": features(w, h, config.fps, "h264"), "episodes": entries, "reports": source.reports,
                "video_prepare_seconds": time.perf_counter() - started,
                "timings": {"source_alignment_seconds": alignment_seconds,
                            "table_write_seconds": tables_seconds,
                            "video_decode_encode_statistics_seconds": video_seconds,
                            "recordings": recordings},
                "video_encoding": {"codec": "h264", "encoder": "libx264", "pixel_format": "yuv420p",
                                   "crf": 18, "preset": "veryfast", "b_frames": 0,
                                   "intermediate_lossy_encoding": False},
                "image_statistics": "RGB channel histograms before H.264 encoding, all selected pixels/frames"}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _encode_group(source, episodes, destination, entries):
    """At most four encoders and one decoder live at once, regardless of bag size."""
    fps = source.config.fps
    ep_cursor, output_index, outputs, histograms = 0, 0, {}, {}
    first = episodes[0].videos["observation.image.head_left"]
    wanted = source.audit(episodes[0])["video_packet_index"]

    def begin(ep):
        for camera in CAMERAS:
            output = av.open(str(destination / entries[ep.index]["videos"][f"observation.image.{camera}"]), "w")
            stream = output.add_stream("libx264", rate=fps)
            stream.width, stream.height, stream.pix_fmt = first.width, first.height, "yuv420p"
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
            entries[ep.index]["stats"][f"observation.image.{camera}"] = histograms[camera].result()
        outputs.clear()

    try:
        begin(episodes[ep_cursor])
        # Consume through EOF even after the last selected frame: validate the
        # one-packet/one-frame contract and detect corrupt discarded tails too.
        for frame_index, frame in decode_quad(first.paths):
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
                        wanted = source.audit(episodes[ep_cursor])["video_packet_index"]
                        output_index = 0
                        begin(episodes[ep_cursor])
                    else:
                        break
        if ep_cursor != len(episodes):
            raise ValueError("Video ended before all aligned rows could be encoded")
    finally:
        for out, stream in outputs.values():
            out.close()
