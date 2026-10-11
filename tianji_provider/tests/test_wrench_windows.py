"""Timing and value regressions for causal, unfiltered multi-rate force windows."""

import argparse
from dataclasses import replace
import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from tianji_provider import alignment, reader
from tianji_provider.alignment import WRENCH_TOPICS, align, output_grids
from tianji_provider.diagnostics import timing_issues
from tianji_provider.provider import TianjiConfig, provider
from tianji_provider.reader import Signal
from tianji_provider.source import FORMAT, TianjiSource, arrow_table, features, statistics

from test_command_coverage import T0, fake_recording, fixture, signal


@pytest.mark.parametrize("fps,wrench_fps", [(50, 200), (50, 1000), (30, 210), (1, 1000), (1000, 1000)])
def test_valid_rates_and_cli_round_trip(fps, wrench_fps):
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    config = provider.config_from_args(parser.parse_args([
        "--task", "test", "--fps", str(fps), "--wrench-fps", str(wrench_fps)]), None)
    assert config == TianjiConfig(task="test", fps=fps, wrench_fps=wrench_fps)


def test_defaults():
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser, task_required=False)
    config = provider.config_from_args(parser.parse_args([]), None)
    assert (config.fps, config.wrench_fps) == (50, 200)
    assert not any(a.dest == "wrench_mode" for a in parser._actions)


@pytest.mark.parametrize("fps,wrench_fps", [(50, 199), (50, 25), (60, 200), (50, 0),
                                           (50, 1001), (50, 200.0), (50, True), (0, 200)])
def test_reject_incompatible_rates(fps, wrench_fps):
    with pytest.raises(ValueError, match="fps"):
        TianjiConfig(task="test", fps=fps, wrench_fps=wrench_fps)


@pytest.mark.parametrize("fps,wrench_fps", [(50, 200), (30, 210), (333, 999), (50, 50)])
def test_grid_is_contiguous_rational_windows_without_padding_or_drift(fps, wrench_fps):
    config = TianjiConfig(task="test", fps=fps, wrench_fps=wrench_fps)
    end = T0 + 60_123_456_789
    grid, windows = output_grids(T0, end, config)
    n = wrench_fps // fps
    assert windows.shape == (len(grid), n)
    flat = windows.ravel()
    # Python integer arithmetic is independent of the NumPy implementation.
    expected = np.array([T0 + (k * 10**9 + wrench_fps // 2) // wrench_fps for k in range(len(flat))])
    np.testing.assert_array_equal(flat, expected)
    np.testing.assert_array_equal(grid, windows[:, -1])
    assert flat[0] == T0 and flat[-1] <= end
    assert (end - flat[-1]) < (10**9 / fps) + 1
    assert np.unique(flat).size == flat.size
    # Row timestamps differ from their ideal rational grid by at most one ns.
    np.testing.assert_allclose((grid - grid[0]) / 1e9, np.arange(len(grid)) / fps, rtol=0, atol=1e-9)


def force_fixture():
    times, signals, ranges, config = fixture()
    config = replace(config, wrench_fps=200)
    signals["/tj/control/joint_cmd_B"] = signal(times, 7, .8)
    raw = T0 + np.arange(1001, dtype=np.int64) * 1_000_000
    for side, topic in enumerate(WRENCH_TOPICS):
        values = np.zeros((len(raw), 6))
        values[:, 0] = np.arange(len(raw)) + side * 10000
        values[5, 1] = 12345 + side  # A single-ms impulse at a selected point.
        values[6, 1] = -99999  # A future point must not change the 5 ms sample.
        signals[topic] = Signal(raw.copy(), values)
        ranges[topic] = [int(raw[0]), int(raw[-1]), len(raw)]
    return times, signals, ranges, config


def test_force_is_exact_past_measurements_and_never_filtered(monkeypatch):
    times, signals, ranges, config = force_fixture()
    original = alignment.interpolate
    def guarded(times, values, *args, **kwargs):
        assert values.shape[1] != 6, "Wrench must bypass interpolation and FIR entirely"
        return original(times, values, *args, **kwargs)
    monkeypatch.setattr(alignment, "interpolate", guarded)
    segments, report = align(signals, ranges, times, config)
    grid, columns, _ = segments[0]
    assert "observation.wrench" not in columns and "observation.wrench_history" not in columns
    wrench = columns["observation.wrench_windows"]
    assert wrench.shape == (50, 4, 12) and wrench.dtype == np.float32
    np.testing.assert_array_equal(grid - T0, np.arange(50) * 20_000_000 + 15_000_000)
    sample_ms = np.arange(200).reshape(50, 4) * 5
    np.testing.assert_array_equal(wrench[:, :, :6], signals[WRENCH_TOPICS[0]].values[sample_ms].astype(np.float32))
    np.testing.assert_array_equal(wrench[:, :, 6:], signals[WRENCH_TOPICS[1]].values[sample_ms].astype(np.float32))
    assert wrench[0, 1, 1] == 12345
    other = align(signals, ranges, times, replace(config, state_resampling="linear"))[0][0][1]
    np.testing.assert_array_equal(wrench, other["observation.wrench_windows"])
    assert report["wrench_sampling"]["filtered"] is False


def test_jitter_uses_previous_not_nearest_or_future_sample():
    times, signals, ranges, config = force_fixture()
    for topic in WRENCH_TOPICS:
        signals[topic].times[1:-1] += 100_000
    columns = align(signals, ranges, times, config)[0][0][1]
    actual = columns["observation.wrench_windows"][:, :, 0].ravel()
    np.testing.assert_array_equal(actual, np.r_[0, np.arange(5, 1000, 5) - 1])


@pytest.mark.parametrize("fps,wrench_fps", [(25, 200), (50, 1000), (200, 200), (30, 210)])
def test_alignment_obeys_configured_shape_and_source_values(fps, wrench_fps):
    times, signals, ranges, config = force_fixture()
    config = replace(config, fps=fps, wrench_fps=wrench_fps)
    segments, _ = align(signals, ranges, times, config)
    grid, columns, _ = segments[0]
    _, windows = output_grids(T0, times[-1], config)
    force = columns["observation.wrench_windows"]
    assert force.shape == (len(grid), wrench_fps // fps, 12)
    raw = signals[WRENCH_TOPICS[0]]
    indices = np.searchsorted(raw.times, windows, side="right") - 1
    np.testing.assert_array_equal(force[:, :, :6], raw.values[indices].astype(np.float32))


def test_gap_between_row_anchors_is_checked_at_high_rate():
    times, signals, ranges, config = force_fixture()
    # All low-rate row anchors have exact observations. The 25 ms force sample
    # is inside an 8 ms gap while 15 ms and 35 ms row anchors are both valid.
    config = replace(config, max_gap_ms=5)
    dense = signals[WRENCH_TOPICS[1]].times
    for key in alignment.FEEDBACK_TOPICS:
        signals[key] = signal(dense, signals[key].values.shape[1], .25)
    for key in ("/tj/info/eef_left", "/tj/info/eef_right"):
        signals[key] = signal(dense, 7)
        signals[key].values[:, -1] = 1
    topic = WRENCH_TOPICS[0]
    s = signals[topic]
    keep = ~np.isin(np.arange(len(s.times)), np.arange(22, 29))
    signals[topic] = Signal(s.times[keep], s.values[keep])
    grid, _ = output_grids(T0, times[-1], config)
    assert alignment.valid_mask(signals[topic].times, grid, 5_000_000).all()
    with pytest.raises(ValueError, match="Missing .*observations"):
        align(signals, ranges, dense, config)
    gaps = [i for i in timing_issues(signals, ranges, dense, config) if i.get("topic") == topic]
    assert len(gaps) == 1
    assert gaps[0]["severity"] == "error"
    assert gaps[0]["affected_samples"] == gaps[0]["affected_frames"] == 1
    assert gaps[0]["sampling_fps"] == 200


def test_stationary_trim_keeps_whole_windows_and_interior_pauses():
    times, signals, ranges, config = force_fixture()
    t = np.arange(len(times)) * .02
    signals["/tj/joint_states"].values[:, 7:] += (np.clip(t-.2, 0, .2) + np.clip(t-.6, 0, .2))[:, None]
    config = replace(config, trim_stationary=True, state_resampling="linear")
    segments, report = align(signals, ranges, times, config)
    grid, columns, _ = segments[0]
    ms = columns["observation.wrench_windows"][:, :, 0]
    np.testing.assert_array_equal(ms[:, -1] * 1e6, grid - T0)
    np.testing.assert_array_equal(np.diff(ms.ravel()), 5)
    assert grid[0] < T0 + 400_000_000 < T0 + 600_000_000 < grid[-1]
    assert report["trimmed_frames"] > 0 and report["dropped_frames"] == 0
    assert report["wrench_sampling"]["first_window_index"] == report["trim"]["start_frames"]


def test_nested_arrow_parquet_and_stats_preserve_axes(tmp_path):
    data = np.arange(7 * 4 * 12, dtype=np.float32).reshape(7, 4, 12)
    table = arrow_table({"observation.wrench_windows": data, "timestamp": np.arange(7, dtype=np.float32) / 50})
    pq.write_table(table, tmp_path / "data.parquet")
    restored = pq.read_table(tmp_path / "data.parquet")
    np.testing.assert_array_equal(restored["observation.wrench_windows"].to_pylist(), data)
    assert restored.schema.field("observation.wrench_windows").type.list_size == 4
    assert restored.schema.field("observation.wrench_windows").type.value_type.list_size == 12
    stats = statistics(data)
    assert np.shape(stats["mean"]) == (4, 12) and stats["count"] == [7]
    np.testing.assert_allclose(stats["mean"], data.mean(axis=0))


def test_feature_metadata_documents_internal_clock():
    schema = features(960, 540, 50, wrench_fps=200)
    feature = schema["observation.wrench_windows"]
    assert feature["shape"] == [4, 12]
    assert feature["sample_offsets_seconds"] == [-.015, -.010, -.005, 0.0]
    assert feature["sampling_rate_hz"] == 200 and feature["filtered"] is False
    assert len(feature["component_names"]) == 12
    assert "observation.wrench" not in schema and "observation.wrench_history" not in schema


def test_reader_uses_control_gripper_even_when_legacy_target_disagrees(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    bag = fake_recording(tmp_path, monkeypatch)
    rows = list(reader.messages(()))
    for side in ("L", "R"):
        rows.append((NS(name="std_msgs/msg/Float32"), NS(topic=f"/info/gripper_target_{side}"),
                     NS(log_time=T0), NS(data=.99)))
    monkeypatch.setattr(reader, "messages", lambda _: iter(rows))
    signals, _, _, _ = reader.read_numeric(bag)
    for side in ("L", "R"):
        assert signals[f"/control/gripperValue{side}"].values[0, 0] == .5
        assert f"/info/gripper_target_{side}" not in signals


@pytest.mark.parametrize("value", [-.1, 1.1, float("nan"), float("inf")])
def test_bad_control_gripper_is_not_hidden_by_fallback(tmp_path, monkeypatch, value):
    bag = fake_recording(tmp_path, monkeypatch)
    rows = list(reader.messages(()))
    for _, ch, _, msg in rows:
        if ch.topic == "/control/gripperValueL":
            msg.data = value
    monkeypatch.setattr(reader, "messages", lambda _: iter(rows))
    with pytest.raises(ValueError, match="normalized"):
        reader.read_numeric(bag)


@pytest.mark.parametrize("version", ["tianji-thor-v1", "tianji-thor-v2", "tianji-thor-v3"])
def test_previous_cache_formats_require_raw_rebuild(tmp_path, version):
    (tmp_path / "tianji-cache.json").write_text(json.dumps({"format": version}))
    with pytest.raises(ValueError, match="rebuild from raw MCAP"):
        TianjiSource(tmp_path, TianjiConfig(task="test")).metadata


def test_cache_rejects_different_wrench_rate(tmp_path):
    from dataclasses import asdict
    config = TianjiConfig(task="test")
    saved = asdict(config)
    saved.pop("task")
    saved["wrench_fps"] = 1000
    (tmp_path / "tianji-cache.json").write_text(json.dumps({"format": FORMAT, "alignment_config": saved}))
    with pytest.raises(ValueError, match="options differ"):
        TianjiSource(tmp_path, config).metadata
