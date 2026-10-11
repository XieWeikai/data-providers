"""Numeric worker payloads and deterministic source assembly across schedules."""

from copy import deepcopy
from dataclasses import replace
import os
import pickle

import numpy as np
import pytest

from tianji_provider import source
from tianji_provider.alignment import WRENCH_TOPICS, align, previous_indices
from test_command_coverage import fixture, signal


def _recordings(tmp_path, monkeypatch):
    bags = [tmp_path / f"my_bag-{i}" for i in range(3)]
    numeric = {}
    video_times = {}
    sizes = {bag: (960, 540) for bag in bags}
    for index, bag in enumerate(bags):
        bag.mkdir()
        (bag / "data.mcap").write_bytes(b"numeric")
        (bag / "video.mcap").write_bytes(b"video")
        times, signals, ranges, config = fixture()
        for value in signals.values():
            value.times = value.times + index * 2_000_000_000
        times = times + index * 2_000_000_000
        ranges = {key: [int(value.times[0]), int(value.times[-1]), len(value.times)]
                  for key, value in signals.items() if len(value.times)}
        signals["/tj/joint_states"].values += index * 0.1
        signals["/tj/control/joint_cmd_B"] = signal(times, 7, .7 + index * .1)
        for side, topic in enumerate(WRENCH_TOPICS):
            signals[topic].values[:] = np.arange(len(times))[:, None] + side * 100 + index * 1000
        numeric[bag] = signals, ranges, {"/tj/joint_states": "header"}, [[0, 1], [0, 2]]
        video_times[bag] = times
    config = replace(config, wrench_fps=200, state_resampling="linear")
    monkeypatch.setattr(source, "discover", lambda root: tuple(bags))
    monkeypatch.setattr(source, "read_numeric", lambda bag: deepcopy(numeric[bag]))
    monkeypatch.setattr(source, "video_files", lambda bag: (bag / "video.mcap",))
    monkeypatch.setattr(source, "read_video_times", lambda paths: video_times[paths[0].parent].copy())
    monkeypatch.setattr(source, "dimensions", lambda paths: sizes[paths[0].parent])
    monkeypatch.setattr(source, "bag_files", lambda folder: (folder.parent / "data.mcap",))
    return bags, numeric, video_times, sizes, config


def _permuted_loader(monkeypatch, order, closed=None):
    def load(function, jobs, workers=None):
        results = [function(job) for job in jobs]
        try:
            for index in order:
                # Enforce the same serialization boundary as spawn; no shared
                # source object or mutation of a worker's arrays can leak in.
                yield index, pickle.loads(pickle.dumps(results[index]))
        finally:
            if closed is not None:
                closed.append(True)
    monkeypatch.setattr(source, "iter_parallel", load)


def test_numeric_payload_contains_retained_arrays_and_exact_wrench_audit(tmp_path, monkeypatch):
    bags, numeric, video_times, _, config = _recordings(tmp_path, monkeypatch)
    payload = source._load_numeric_recording((bags[0], config))
    assert payload["error"] is None
    assert set(payload) == {"bag", "size", "error", "paths", "report", "video_bytes", "segments"}
    assert payload["video_bytes"] == 5
    values, audit = payload["segments"][0]
    assert set(values) == {"observation.state", "observation.action", "observation.eef", "observation.wrench_windows"}
    assert audit["data_physical_bytes"] == 7
    np.testing.assert_array_equal(audit["video_time_ns"], video_times[bags[0]][audit["video_packet_index"]])
    np.testing.assert_array_equal(audit["wrench_time_ns"][:, -1], audit["absolute_time_ns"])
    measured = numeric[bags[0]][0]
    for side, topic in enumerate(WRENCH_TOPICS):
        raw = measured[topic]
        indices = previous_indices(raw.times, audit["wrench_time_ns"].ravel()).reshape(audit["wrench_time_ns"].shape)
        np.testing.assert_array_equal(audit["wrench_sample_time_ns"][:, :, side], raw.times[indices])
        np.testing.assert_array_equal(values["observation.wrench_windows"][:, :, side * 6:(side + 1) * 6],
                                      raw.values[indices].astype(np.float32))
    # Raw high-rate signals are deliberately absent, keeping transfer bounded
    # by retained output length instead of the number of MCAP messages.
    assert len(pickle.dumps(payload)) < 100_000


@pytest.mark.parametrize("collect_images", [False, True])
def test_completion_order_preserves_tables_stats_reports_and_absolute_audits(tmp_path, monkeypatch, collect_images):
    bags, numeric, video_times, _, config = _recordings(tmp_path, monkeypatch)
    image_calls = []
    def images(episodes):
        image_calls.append((os.getpid(), [episode.index for episode in episodes]))
        return {episode.index: {"observation.image.head_left": {"count": [episode.length], "mean": [[[0.25]]]}}
                for episode in episodes}
    monkeypatch.setattr(source, "image_statistics", images)
    serial = source.TianjiSource(tmp_path, config, _collect_image_stats=collect_images, _workers=1)
    serial_episodes = serial.episodes
    _permuted_loader(monkeypatch, [2, 1, 0])
    parallel = source.TianjiSource(tmp_path, config, _collect_image_stats=collect_images, _workers=3)
    parallel_episodes = parallel.episodes
    assert parallel.metadata == serial.metadata
    assert parallel.reports == serial.reports
    assert parallel.planner_identity() == serial.planner_identity()
    assert [episode.data_path for episode in parallel_episodes] == bags
    offset = 0
    for index, (left, right) in enumerate(zip(serial_episodes, parallel_episodes, strict=True)):
        assert left.index == right.index == index
        assert left.stats == right.stats
        assert serial.read_episode(left).equals(parallel.read_episode(right))
        table = parallel.read_episode(right)
        np.testing.assert_array_equal(table["index"].to_numpy(), np.arange(offset, offset + right.length))
        np.testing.assert_array_equal(table["episode_index"].to_numpy(), np.full(right.length, index))
        np.testing.assert_array_equal(table["frame_index"].to_numpy(), np.arange(right.length))
        np.testing.assert_array_equal(table["timestamp"].to_numpy(), np.arange(right.length, dtype=np.float32) / config.fps)
        for key, expected in serial.audit(left).items():
            np.testing.assert_array_equal(parallel.audit(right)[key], expected)
        # Check the assembled output directly against alignment of this bag,
        # independently of completion order and generated global indices.
        signals, ranges, _, _ = deepcopy(numeric[bags[index]])
        segments, _ = align(signals, ranges, video_times[bags[index]], config)
        for key, expected in segments[0][1].items():
            np.testing.assert_array_equal(np.array(table[key].to_pylist()), expected)
        for camera in source.CAMERAS:
            media = right.videos[f"observation.image.{camera}"]
            assert media.paths == (bags[index] / "video.mcap",)
            assert (media.width, media.height) == (960, 540)
            np.testing.assert_array_equal(media.indices, parallel.audit(right)["video_packet_index"])
        offset += right.length
    assert parallel.metadata.total_frames == offset
    assert image_calls == ([(os.getpid(), [index]) for index in range(3)] * 2 if collect_images else [])


def test_dimension_error_precedes_same_bag_alignment_error_and_closes_pool(tmp_path, monkeypatch):
    bags, _, _, sizes, config = _recordings(tmp_path, monkeypatch)
    sizes[bags[1]] = (640, 480)
    original_align = source.align
    def failed_alignment(signals, *args):
        if signals["/tj/joint_states"].values[0, 0] > .3:
            raise ValueError("later alignment error")
        return original_align(signals, *args)
    monkeypatch.setattr(source, "align", failed_alignment)
    closed = []
    _permuted_loader(monkeypatch, [2, 1, 0], closed)
    with pytest.raises(ValueError, match="Camera dimensions vary across recordings"):
        source.TianjiSource(tmp_path, config, _collect_image_stats=False).metadata
    assert closed == [True]


def test_first_recording_error_is_reported_even_when_later_error_finishes_first(tmp_path, monkeypatch):
    bags, _, _, _, config = _recordings(tmp_path, monkeypatch)
    original_read = source.read_numeric
    def failed_read(bag):
        if bag != bags[0]:
            raise ValueError(f"bad numeric data in {bag.name}")
        return original_read(bag)
    monkeypatch.setattr(source, "read_numeric", failed_read)
    _permuted_loader(monkeypatch, [2, 1, 0])
    with pytest.raises(ValueError, match="bad numeric data in my_bag-1"):
        source.TianjiSource(tmp_path, config, _collect_image_stats=False).metadata


def test_construction_stays_lazy_and_prepared_cache_does_not_create_jobs(tmp_path, monkeypatch):
    _, _, _, _, config = _recordings(tmp_path, monkeypatch)
    def unexpected(*args, **kwargs):
        raise AssertionError("prepared cache must not execute raw jobs")
    monkeypatch.setattr(source, "discover", unexpected)
    monkeypatch.setattr(source, "iter_parallel", unexpected)
    cached = source.TianjiSource(tmp_path, config, _workers=4)
    (tmp_path / "tianji-cache.json").write_text("{}")
    expected = object()
    monkeypatch.setattr(source.TianjiSource, "_load_prepared", lambda self: expected)
    assert cached._loaded is expected
