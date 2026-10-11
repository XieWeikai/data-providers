"""Opt-in real MCAP -> cache -> LeRobot acceptance; raw recordings are read-only.

Set TIANJI_TEST_RECORDINGS to colon-separated recording directories and put
pytest's --basetemp outside the originals. Uses the current interpreter's
LeTools installation; does not install packages or touch existing datasets.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from letools import open_dataset

from tianji_provider.alignment import WRENCH_TOPICS
from tianji_provider.diagnostics import check_recording
from tianji_provider.prepare import fingerprint, prepare
from tianji_provider.provider import TianjiConfig
from tianji_provider.reader import read_numeric
from tianji_provider.source import TianjiSource

RECORDINGS = [Path(p) for p in os.environ.get("TIANJI_TEST_RECORDINGS", "").split(os.pathsep) if p]


def run_letools(*args):
    result = subprocess.run([sys.executable, "-m", "letools.cli", *map(str, args)],
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("bag", RECORDINGS or [None], ids=lambda p: p.name if p else "set-TIANJI_TEST_RECORDINGS")
@pytest.mark.skipif(not RECORDINGS, reason="Real MCAP paths not provided")
def test_real_check_prepare_both_lerobot_versions(bag, tmp_path):
    before = fingerprint(bag)
    config = TianjiConfig(task="Pick and place")
    checked = check_recording(bag, config, deep_video=True)
    assert checked["valid"], checked["issues"]
    source = TianjiSource(bag, config, _collect_image_stats=False)
    ep, = source.episodes
    original = source.read_episode(ep)
    audit = source.audit(ep)
    measured, _, _, _ = read_numeric(bag)
    target_times = audit["wrench_time_ns"]
    wrench = np.array(original["observation.wrench_windows"].to_pylist(), dtype=np.float32)
    assert wrench.shape == (ep.length, 4, 12)
    assert np.all(np.diff(target_times.ravel()) == 5_000_000)
    np.testing.assert_array_equal(target_times[:, -1], audit["absolute_time_ns"])
    for side, topic in enumerate(WRENCH_TOPICS):
        raw = measured[topic]
        indices = np.searchsorted(raw.times, target_times, side="right") - 1
        assert np.all(indices >= 0)
        np.testing.assert_array_equal(wrench[:, :, side*6:(side+1)*6], raw.values[indices].astype(np.float32))
        np.testing.assert_array_equal(audit["wrench_sample_time_ns"][:, :, side], raw.times[indices])
        assert np.all(raw.times[indices] <= target_times)
    state = np.array(original["observation.state"].to_pylist(), dtype=np.float32)
    action = np.array(original["observation.action"].to_pylist(), dtype=np.float32)
    for side, column in (("L", 7), ("R", 15)):
        raw = measured[f"/control/gripperValue{side}"]
        points = audit["absolute_time_ns"]
        indices = np.searchsorted(raw.times, points, side="right") - 1
        fresh = indices >= 0
        fresh[fresh] &= points[fresh] - raw.times[indices[fresh]] <= 150_000_000
        expected = state[:, column].copy()
        expected[fresh] = raw.values[indices[fresh], 0]
        np.testing.assert_array_equal(action[:, column], expected)

    cache = tmp_path / "prepared"
    manifest = prepare(bag, cache, config)
    prepared = TianjiSource(cache, config)
    cached_ep, = prepared.episodes
    assert prepared.read_episode(cached_ep).equals(original)
    for key in ("absolute_time_ns", "wrench_time_ns", "wrench_sample_time_ns"):
        np.testing.assert_array_equal(prepared.audit(cached_ep)[key], audit[key])
    assert prepare(bag, cache, replace(config, task="New task")) == manifest
    with pytest.raises(ValueError, match="options differ"):
        TianjiSource(cache, replace(config, wrench_fps=1000)).metadata

    for version in ("v2.1", "v3.0"):
        dest = tmp_path / version
        run_letools("convert", cache, dest, "--source-format", "tianji", "--task", config.task,
                    "--to", version, "--workers", "2", "--video-workers", "2")
        run_letools("validate", dest, "--deep")
        info = json.loads((dest / "meta/info.json").read_text())
        feature = info["features"]["observation.wrench_windows"]
        assert feature["shape"] == [4, 12] and feature["sampling_rate_hz"] == 200
        assert info["fps"] == 50 and info["total_frames"] == ep.length
        assert "observation.wrench" not in info["features"] and "observation.wrench_history" not in info["features"]
        converted = open_dataset(dest)
        result_ep, = converted.episodes
        for stat in ("min", "max", "mean", "std", "count"):
            np.testing.assert_allclose(result_ep.stats["observation.wrench_windows"][stat],
                                       cached_ep.stats["observation.wrench_windows"][stat])
        table = pa.concat_tables([pq.read_table(p) for p in sorted((dest / "data").rglob("*.parquet"))])
        for key in original.column_names:
            np.testing.assert_array_equal(np.array(table[key].to_pylist()), np.array(original[key].to_pylist()))
        # Decode every frame of each output video, not just metadata validation.
        videos = list((dest / "videos").rglob("*.mp4"))
        assert len(videos) == 4
        for path in videos:
            with av.open(str(path)) as container:
                count = 0
                for frame in container.decode(video=0):
                    assert (frame.width, frame.height) == (960, 540)
                    assert abs(float(frame.pts * frame.time_base) - count / 50) < 1e-6
                    count += 1
                assert count == ep.length
    assert fingerprint(bag) == before


@pytest.mark.skipif(not RECORDINGS, reason="Real MCAP paths not provided")
def test_real_direct_conversion_with_1000_hz_wrench(tmp_path):
    bag = RECORDINGS[0]
    before = fingerprint(bag)
    config = TianjiConfig(task="Pick and place", wrench_fps=1000)
    dest = tmp_path / "direct-v21"
    run_letools("convert", bag, dest, "--source-format", "tianji", "--task", config.task,
                "--wrench-fps", "1000", "--to", "v2.1", "--workers", "2", "--video-workers", "2",
                "--video-codec", "libx264", "--video-pixel-format", "yuv420p")
    run_letools("validate", dest, "--deep")
    result = open_dataset(dest)
    ep, = result.episodes
    feature = result.metadata.features["observation.wrench_windows"]
    assert feature["shape"] == [20, 12] and feature["sampling_rate_hz"] == 1000
    source = TianjiSource(bag, config, _collect_image_stats=False)
    source_ep, = source.episodes
    raw_table, output_table = source.read_episode(source_ep), result.read_episode(ep)
    for key in raw_table.column_names:
        np.testing.assert_array_equal(np.array(raw_table[key].to_pylist()), np.array(output_table[key].to_pylist()))
    assert fingerprint(bag) == before
