"""One-pass checks retain diagnostics, arrays, audit, statistics and video bytes."""

from copy import deepcopy
from dataclasses import replace
import importlib

import av
import numpy as np
import pyarrow.parquet as pq
import pytest
import yaml

from tianji_provider import diagnostics, encoding, fused, source
from test_command_coverage import fixture, signal

prepare_module = importlib.import_module("tianji_provider.prepare")


def _fixture(tmp_path, monkeypatch, count=2, *, bad_numeric=(), changed_tail=(), sizes=None, empty_video=()):
    root = tmp_path / "raw"
    root.mkdir()
    bags = []
    for index in range(count):
        bag = root / f"my_bag-{index}"
        for folder, filename in ((bag / "data", "data.mcap"), (bag / "video" / "quad", "video.mcap")):
            folder.mkdir(parents=True)
            (folder / filename).write_bytes(b"synthetic read-only MCAP placeholder")
            (folder / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
                "relative_file_paths": [filename]}}))
        bags.append(bag)
    counts = {"numeric": 0, "timestamps": 0, "decode": 0}
    def numeric(bag):
        counts["numeric"] += 1
        index = int(bag.name.rsplit("-", 1)[-1])
        if index in bad_numeric:
            raise ValueError("broken numeric fixture")
        times, signals, ranges, _ = fixture()
        signals["/tj/control/joint_cmd_B"] = signal(times, 7, .8)
        signals["/tj/joint_states"].values += index * .1
        return signals, ranges, {"test": "header"}, [[0, 1], [0, 1]]
    def timestamps(paths):
        counts["timestamps"] += 1
        return fixture()[0]
    def decoded(paths):
        counts["decode"] += 1
        bag = paths[0].parents[2]
        index = int(bag.name.rsplit("-", 1)[-1])
        if index in empty_video:
            return
        width, height = (sizes or {}).get(index, (8, 8))
        for frame_index in range(51):
            w = width + 4 if index in changed_tail and frame_index == 50 else width
            rgb = np.empty((height, w, 3), dtype=np.uint8)
            rgb[:] = (frame_index * 3 % 256, index * 50, 123)
            yield frame_index, av.VideoFrame.from_ndarray(rgb, format="rgb24")
    for module in (source, diagnostics, fused):
        monkeypatch.setattr(module, "read_numeric", numeric)
        monkeypatch.setattr(module, "read_video_times", timestamps)
    for module in (prepare_module, diagnostics, fused):
        monkeypatch.setattr(module, "decode_quad", decoded)
    monkeypatch.setattr(source, "dimensions", lambda _: (4, 4))
    monkeypatch.setattr(fused, "dimensions", lambda _: (4, 4))
    for module in (prepare_module, fused):
        monkeypatch.setattr(module, "select_encoder", lambda requested, *args: (
            encoding.LIBX264, {"requested": requested, "selected": "libx264", "probed": False}))
    config = replace(fixture()[3], wrench_fps=200)
    return root, bags, config, counts


def _report_without_timing(report):
    report = deepcopy(report)
    report.pop("seconds")
    for item in report["results"]:
        item.pop("seconds")
    return report


def _packets(path):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return [(bytes(packet), packet.pts, packet.dts, packet.duration, packet.time_base)
                for packet in container.demux(stream) if packet.pts is not None]


def test_fused_matches_separate_checks_arrays_audit_statistics_and_encoded_packets(tmp_path, monkeypatch):
    root, bags, config, counts = _fixture(tmp_path, monkeypatch)
    baseline_report = diagnostics.check_dataset(root, config, workers=1)
    baseline = tmp_path / "baseline"
    baseline_manifest = prepare_module.prepare(root, baseline, config, workers=1)
    assert counts == {"numeric": 4, "timestamps": 4, "decode": 4}
    counts.update(numeric=0, timestamps=0, decode=0)
    output = tmp_path / "fused"
    progress = []
    result = fused.check_and_prepare(root, output, config, workers=1,
                                    progress=lambda index, total, item: progress.append((index, total, item["valid"])))
    assert counts == {"numeric": 2, "timestamps": 2, "decode": 2}
    assert progress == [(1, 2, True), (2, 2, True)]
    assert _report_without_timing(result["report"]) == _report_without_timing(baseline_report)
    manifest = result["manifest"]
    for key in ("format", "alignment_config", "source_fingerprint", "features", "episodes", "reports",
                "video_encoding", "image_statistics"):
        assert manifest[key] == baseline_manifest[key], key
    for episode in manifest["episodes"]:
        assert pq.read_table(output / episode["data"]).equals(pq.read_table(baseline / episode["data"]))
        with np.load(output / episode["audit"]) as actual, np.load(baseline / episode["audit"]) as expected:
            assert actual.files == expected.files
            for key in actual.files:
                np.testing.assert_array_equal(actual[key], expected[key])
        for video in episode["videos"].values():
            assert _packets(output / video) == _packets(baseline / video)
    # The result is an ordinary v4 cache and requires no special loader.
    loaded = source.TianjiSource(output, config)
    assert loaded.metadata.total_episodes == len(bags)
    assert sum(episode.length for episode in loaded.episodes) == result["report"]["valid_frames"]


@pytest.mark.parametrize("bad_numeric,changed_tail", [((0,), ()), ((), (0,)), ((0,), (0,))])
def test_bad_recording_keeps_both_diagnostic_branches_and_never_publishes(tmp_path, monkeypatch, bad_numeric, changed_tail):
    root, _, config, counts = _fixture(tmp_path, monkeypatch, bad_numeric=bad_numeric, changed_tail=changed_tail)
    expected = diagnostics.check_dataset(root, config, workers=1)
    output = tmp_path / "fused"
    counts.update(numeric=0, timestamps=0, decode=0)
    result = fused.check_and_prepare(root, output, config, workers=1)
    assert not result["report"]["valid"]
    assert result["report"]["valid_recordings"] == 1
    assert _report_without_timing(result["report"]) == _report_without_timing(expected)
    assert counts == {"numeric": 2, "timestamps": 2, "decode": 2}
    assert result["manifest"] is None
    assert not (output / "tianji-cache.json").exists()
    with pytest.raises(FileExistsError, match="incomplete cache"):
        fused.check_and_prepare(root, output, config, workers=1)


def test_empty_video_is_reported_instead_of_stopping_the_batch(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch, empty_video=(0,))
    expected = diagnostics.check_dataset(root, config, workers=1)
    result = fused.check_and_prepare(root, tmp_path / "fused", config, workers=1)
    assert result["manifest"] is None
    assert _report_without_timing(result["report"]) == _report_without_timing(expected)


@pytest.mark.parametrize("api", ["fused", "prepare"])
def test_auto_retries_complete_batch_after_codec_failure(tmp_path, monkeypatch, api):
    root, bags, config, counts = _fixture(tmp_path, monkeypatch)
    encode = prepare_module._encode_group
    calls = []
    def failing_once(job, **kwargs):
        calls.append(job.bag)
        if len(calls) == 2:
            raise encoding.HardwareEncodingError("synthetic NVENC resource failure")
        return encode(job, **kwargs)
    monkeypatch.setattr(prepare_module, "_encode_group", failing_once)
    monkeypatch.setattr(fused, "_encode_group", failing_once)
    output = tmp_path / "cache"
    progress = []
    if api == "fused":
        result = fused.check_and_prepare(root, output, config, workers=1,
            progress=lambda index, total, item: progress.append(index))
        manifest = result["manifest"]
        assert result["report"]["valid_recordings"] == 2
        assert progress == [1, 1, 2]
    else:
        manifest = prepare_module.prepare(root, output, config, workers=1)
    assert len(calls) == 4
    assert manifest["video_encoding"] == encoding.LIBX264.metadata()
    assert manifest["video_encoder_selection"]["fallback_stage"] == "runtime"
    assert manifest["video_encoder_selection"]["requested"] == "auto"
    assert len(source.TianjiSource(output, config).episodes) == 2
    assert len(list(tmp_path.glob("cache.failed-nvenc-*/cache"))) == 1


def test_dimensions_preserve_sorted_baseline_despite_reverse_completion(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch, sizes={1: (16, 8)})
    expected = diagnostics.check_dataset(root, config, workers=1)
    def reverse(function, jobs, workers=None):
        results = [function(job) for job in jobs]
        for index in reversed(range(len(results))):
            yield index, results[index]
    monkeypatch.setattr(fused, "iter_parallel", reverse)
    result = fused.check_and_prepare(root, tmp_path / "fused", config, workers=2)
    assert result["manifest"] is None
    assert _report_without_timing(result["report"]) == _report_without_timing(expected)


def test_source_metadata_change_prevents_complete_manifest(tmp_path, monkeypatch):
    root, bags, config, _ = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "fused"
    def progress(index, total, item):
        if index == total:
            path = bags[0] / "data" / "metadata.yaml"
            path.write_text(path.read_text() + "# metadata changed during work\n")
    with pytest.raises(ValueError, match="Source files or selection changed"):
        fused.check_and_prepare(root, output, config, workers=1, progress=progress)
    assert not (output / "tianji-cache.json").exists()


def test_encoder_failure_is_operational_error_and_does_not_publish(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch)
    def broken(*args, **kwargs):
        raise OSError("disk full fixture")
    monkeypatch.setattr(fused, "_encode_group", broken)
    output = tmp_path / "fused"
    with pytest.raises(OSError, match="disk full fixture"):
        fused.check_and_prepare(root, output, config, workers=1)
    assert not (output / "tianji-cache.json").exists()


def test_selection_symlink_does_not_allow_output_inside_real_raw_bag(tmp_path, monkeypatch):
    root, bags, config, _ = _fixture(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "my_bag-selected").symlink_to(bags[0], target_is_directory=True)
    output = bags[0] / "forbidden-cache"
    with pytest.raises(ValueError, match="outside every selected raw recording"):
        fused.check_and_prepare(selected, output, config, workers=1)
    assert not output.exists()


def test_selected_symlinks_preserve_original_report_paths_and_cache_identity(tmp_path, monkeypatch):
    root, bags, config, _ = _fixture(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    for index, bag in enumerate(reversed(bags)):
        (selected / f"my_bag-{index:06d}-{bag.name}").symlink_to(bag, target_is_directory=True)
    baseline_report = diagnostics.check_dataset(selected, config, workers=1)
    baseline = prepare_module.prepare(selected, tmp_path / "baseline", config, workers=1)
    result = fused.check_and_prepare(selected, tmp_path / "fused", config, workers=1)
    assert _report_without_timing(result["report"]) == _report_without_timing(baseline_report)
    for key in ("source_fingerprint", "reports", "episodes"):
        assert result["manifest"][key] == baseline[key]


def test_explicit_valid_only_keeps_full_failed_report_and_reindexes_output(tmp_path, monkeypatch):
    root, bags, config, _ = _fixture(tmp_path, monkeypatch, count=3, bad_numeric=(0,))
    expected = diagnostics.check_dataset(root, config, workers=1)
    selected = tmp_path / "valid-only-baseline"
    selected.mkdir()
    for bag in bags[1:]:
        (selected / bag.name).symlink_to(bag, target_is_directory=True)
    baseline_root = tmp_path / "baseline"
    baseline = prepare_module.prepare(selected, baseline_root, config, workers=1)
    output = tmp_path / "fused"
    result = fused.check_and_prepare(root, output, config, workers=1, prepare_valid_only=True)
    assert result["report"]["valid"] is False
    assert _report_without_timing(result["report"]) == _report_without_timing(expected)
    manifest = result["manifest"]
    assert manifest is not None and len(manifest["episodes"]) == 2
    selection = manifest["source_selection"]
    assert selection == result["selection"]
    assert selection["mode"] == "explicit_valid_only"
    assert (selection["recordings_checked"], selection["valid_recordings"], selection["invalid_recordings"]) == (3, 2, 1)
    assert selection["rejected"][0]["bag"] == "my_bag-0"
    assert selection["episode_mapping"] == [
        {"source_recording_index": 1, "bag": "my_bag-1", "episode_index": 0},
        {"source_recording_index": 2, "bag": "my_bag-2", "episode_index": 1}]
    assert manifest["source_fingerprint"] == baseline["source_fingerprint"]
    offset = 0
    for current, old in zip(manifest["episodes"], baseline["episodes"], strict=True):
        assert current["index"] == old["index"]
        assert current["length"] == old["length"] and current["stats"] == old["stats"]
        table = pq.read_table(output / current["data"])
        assert table.equals(pq.read_table(baseline_root / old["data"]))
        np.testing.assert_array_equal(table["index"].to_numpy(), np.arange(offset, offset + current["length"]))
        offset += current["length"]
        with np.load(output / current["audit"]) as actual, np.load(baseline_root / old["audit"]) as reference:
            for key in actual.files:
                np.testing.assert_array_equal(actual[key], reference[key])
        assert manifest["reports"][current["index"]]["episode_indices"] == [current["index"]]
    assert source.TianjiSource(output, config).metadata.total_episodes == 2
    # Explicitly reopening through the valid raw selection preserves normal v4
    # cache identity, regardless of the original per-recording directory names.
    assert prepare_module.prepare(selected, output, config, workers=1) == manifest


def test_valid_only_with_no_valid_recordings_never_publishes(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch, bad_numeric=(0, 1))
    output = tmp_path / "fused"
    result = fused.check_and_prepare(root, output, config, workers=1, prepare_valid_only=True)
    assert result["manifest"] is None and result["report"]["valid_recordings"] == 0
    assert not (output / "tianji-cache.json").exists()


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_valid_only_requires_explicit_boolean(tmp_path, monkeypatch, value):
    root, _, config, _ = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="explicit boolean"):
        fused.check_and_prepare(root, tmp_path / "fused", config, prepare_valid_only=value)


def test_one_encoder_selection_hardware_cap_and_longest_first_keep_report_order(tmp_path, monkeypatch):
    root, bags, config, _ = _fixture(tmp_path, monkeypatch, count=3)
    for index, bag in enumerate(bags):
        path = bag / "data/metadata.yaml"
        metadata = yaml.safe_load(path.read_text())
        metadata["rosbag2_bagfile_information"]["duration"] = {"nanoseconds": (index + 1) * 1_000_000_000}
        path.write_text(yaml.safe_dump(metadata))
    calls = []
    def select(requested, width, height, fps):
        calls.append((requested, width, height, fps))
        return encoding.NVENC, {"requested": requested, "selected": encoding.NVENC.name, "probed": True}
    monkeypatch.setattr(fused, "select_encoder", select)
    monkeypatch.setattr(encoding, "NVENC_WORKER_LIMIT", 2)
    # Hardware encoding itself is covered by target-machine tests. This test
    # replaces only the writer to inspect shared recipe/planning without a GPU.
    real_encode = fused._encode_group
    def software_writer(job, **kwargs):
        assert job.encoder == encoding.NVENC
        return real_encode(replace(job, encoder=encoding.LIBX264), **kwargs)
    monkeypatch.setattr(fused, "_encode_group", software_writer)
    def ordered_tasks(function, jobs, workers=None):
        assert workers == 2
        assert [job[1] for job in jobs] == [2, 1, 0]
        for index, job in enumerate(jobs):
            yield index, function(job)
    monkeypatch.setattr(fused, "iter_parallel", ordered_tasks)
    result = fused.check_and_prepare(root, tmp_path / "fused", config, workers=12, video_encoder="h264_nvenc")
    assert calls == [("h264_nvenc", 4, 4, 50)]
    assert [item["bag"] for item in result["report"]["results"]] == [bag.name for bag in bags]
    assert result["manifest"]["timings"]["video_workers"] == 2
    assert result["manifest"]["video_encoding"] == encoding.NVENC.metadata()


def test_cancellation_closes_execution_iterator_and_never_publishes(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch)
    closed = []
    def tasks(function, jobs, workers=None):
        try:
            for index, job in enumerate(jobs):
                yield index, function(job)
        finally:
            closed.append(True)
    monkeypatch.setattr(fused, "iter_parallel", tasks)
    def cancel(*args):
        raise KeyboardInterrupt("cancel fixture")
    output = tmp_path / "fused"
    with pytest.raises(KeyboardInterrupt, match="cancel fixture"):
        fused.check_and_prepare(root, output, config, workers=2, progress=cancel)
    assert closed == [True]
    assert not (output / "tianji-cache.json").exists()


def test_last_snapshot_after_table_writes_still_prevents_manifest(tmp_path, monkeypatch):
    root, _, config, _ = _fixture(tmp_path, monkeypatch)
    original_snapshot = fused._snapshot
    output = tmp_path / "fused"
    def changed(path):
        result = original_snapshot(path)
        return result + [("modified just before publication",)] if (output / "episode_000000/data.parquet").exists() else result
    monkeypatch.setattr(fused, "_snapshot", changed)
    with pytest.raises(ValueError, match="before cache publication"):
        fused.check_and_prepare(root, output, config, workers=1)
    assert (output / "episode_000000/data.parquet").exists()
    assert not (output / "tianji-cache.json").exists()
