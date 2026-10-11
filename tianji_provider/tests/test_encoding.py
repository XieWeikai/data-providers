"""Hardware selection must be isolated, explicit, bounded and cache-safe."""

from dataclasses import FrozenInstanceError, asdict
import importlib
import json
import pickle
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from tianji_provider import encoding
from tianji_provider.provider import TianjiConfig
from tianji_provider.source import FORMAT


def successful_probe():
    return {"ok": True, "encoder": encoding.NVENC.name, "options": dict(encoding.NVENC.options),
            "frames": 6, "width": 960, "height": 540, "fps": 50, "profile": "Main"}


def test_recipes_are_immutable_serializable_and_describe_real_quality():
    for spec in (encoding.LIBX264, encoding.NVENC):
        assert pickle.loads(pickle.dumps(spec)) == spec
        with pytest.raises(FrozenInstanceError):
            spec.name = "another codec"
        copy = dict(spec.options)
        copy["preset"] = "wrong"
        assert dict(spec.options)["preset"] != "wrong"
    assert encoding.LIBX264.metadata()["crf"] == 18
    assert encoding.NVENC.metadata()["cq"] == 16
    assert "crf" not in encoding.NVENC.metadata()
    assert encoding.NVENC.metadata()["rate_control"] == "vbr"
    assert "video_encoder" not in asdict(TianjiConfig(task="test"))


@pytest.mark.parametrize("choice", [None, "", "hevc_nvenc", 1])
def test_invalid_choices_are_rejected(choice):
    with pytest.raises(ValueError, match="video_encoder"):
        encoding.select_encoder(choice, 960, 540, 50)


def test_software_choice_never_probes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit software must not query hardware")
    monkeypatch.setattr(encoding, "_probe_nvenc", forbidden)
    selected, details = encoding.select_encoder("libx264", 960, 540, 50)
    assert selected is encoding.LIBX264
    assert details == {"requested": "libx264", "selected": "libx264", "probed": False}


def test_hardware_probe_uses_same_interpreter_and_is_not_cached(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(successful_probe()), stderr="")
    def forbidden(*args):
        raise AssertionError("Hardware probe must not execute in the parent")
    monkeypatch.setattr(encoding.subprocess, "run", run)
    monkeypatch.setattr(encoding, "_run_probe", forbidden)
    for choice in ("auto", "h264_nvenc"):
        selected, details = encoding.select_encoder(choice, 960, 540, 50)
        assert selected is encoding.NVENC and details["probe"]["ok"]
    assert len(calls) == 2
    for command, kwargs in calls:
        assert command == [sys.executable, "-m", "tianji_provider.encoding", "--probe", "960", "540", "50"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20}


@pytest.mark.parametrize("failure", ["crash", "timeout", "unavailable", "invalid_json", "wrong_recipe"])
def test_auto_fallback_and_explicit_failure_never_silently_change_requested_codec(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        if failure == "unavailable":
            raise OSError("Cannot launch interpreter")
        if failure == "crash":
            return SimpleNamespace(returncode=-11, stdout="", stderr="native codec failure")
        if failure == "invalid_json":
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")
        report = successful_probe()
        report["options"] = {"cq": "18"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")
    monkeypatch.setattr(encoding.subprocess, "run", run)
    spec, report = encoding.select_encoder("auto", 960, 540, 50)
    assert spec is encoding.LIBX264 and report["fallback_reason"]
    assert report["probe"]["ok"] is False
    with pytest.raises(RuntimeError, match="Explicit h264_nvenc"):
        encoding.select_encoder("h264_nvenc", 960, 540, 50)


def test_video_session_bound_applies_even_when_explicit_worker_count_is_higher():
    assert encoding.encoding_workers(12, 125, encoding.NVENC) == 12
    assert encoding.encoding_workers(20, 125, encoding.NVENC) == 12
    assert encoding.encoding_workers(1, 125, encoding.NVENC) == 1
    assert encoding.encoding_workers(12, 2, encoding.NVENC) == 2
    assert encoding.encoding_workers(12, 125, encoding.LIBX264) == 12
    assert encoding.encoding_workers(None, 0, encoding.NVENC) == 0


def test_existing_software_v4_and_exact_hardware_recipe_cache_rules():
    legacy = encoding.LIBX264.metadata()
    legacy.pop("options")
    manifest = {"video_encoding": legacy}
    assert encoding.cache_encoder_matches(manifest, "auto")
    assert encoding.cache_encoder_matches(manifest, "libx264")
    assert not encoding.cache_encoder_matches(manifest, "h264_nvenc")
    manifest["video_encoding"] = encoding.NVENC.metadata()
    assert encoding.cache_encoder_matches(manifest, "auto")
    assert encoding.cache_encoder_matches(manifest, "h264_nvenc")
    assert not encoding.cache_encoder_matches(manifest, "libx264")
    # Changing an internal hardware recipe must also invalidate explicit reuse.
    manifest["video_encoding"]["options"]["cq"] = "18"
    assert not encoding.cache_encoder_matches(manifest, "h264_nvenc")
    assert not encoding.cache_encoder_matches(manifest, "auto")


@pytest.mark.parametrize("change", [
    {"encoder": "unknown_h264_encoder"}, {"cq": 18}, {"cq": 12}, {"profile": "high444p"},
    {"preset": "p1"}, {"pixel_format": "yuv444p"}, {"b_frames": 2},
])
def test_auto_cache_rejects_unknown_or_outdated_hardware_recipes(change):
    manifest = {"video_encoding": encoding.NVENC.metadata()}
    manifest["video_encoding"].update(change)
    assert not encoding.cache_encoder_matches(manifest, "auto")


@pytest.mark.parametrize("change", [{"crf": 23}, {"preset": "ultrafast"}, {"b_frames": 2}, {"pixel_format": "yuv444p"}])
def test_auto_cache_rejects_changed_legacy_software_quality(change):
    manifest = {"video_encoding": encoding.LIBX264.metadata()}
    manifest["video_encoding"].pop("options")
    manifest["video_encoding"].update(change)
    assert not encoding.cache_encoder_matches(manifest, "auto")


def test_auto_cache_rejects_absent_encoding_metadata_or_incomplete_hardware_recipe():
    assert not encoding.cache_encoder_matches({}, "auto")
    manifest = {"video_encoding": encoding.NVENC.metadata()}
    manifest["video_encoding"].pop("options")
    assert not encoding.cache_encoder_matches(manifest, "auto")


def test_cache_reuse_does_not_probe_or_require_current_hardware(tmp_path, monkeypatch):
    prep = importlib.import_module("tianji_provider.prepare")
    root, cache = tmp_path / "raw", tmp_path / "cache"
    root.mkdir(); cache.mkdir()
    config = TianjiConfig(task="test")
    options = asdict(config); options.pop("task")
    manifest = {"format": FORMAT, "alignment_config": options, "source_fingerprint": [],
                "video_encoding": encoding.NVENC.metadata()}
    (cache / "tianji-cache.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(prep, "fingerprint", lambda _: [])
    opened = []
    class CachedSource:
        def __init__(self, path, config):
            opened.append(path)
        metadata = object()
    monkeypatch.setattr(prep, "TianjiSource", CachedSource)
    def forbidden(*args):
        raise AssertionError("Existing matching cache must not probe")
    monkeypatch.setattr(prep, "select_encoder", forbidden)
    assert prep.prepare(root, cache, config, video_encoder="auto", workers=12) == manifest
    assert prep.prepare(root, cache, config, video_encoder="h264_nvenc", workers=1) == manifest
    with pytest.raises(ValueError, match="encoder/quality recipe differs"):
        prep.prepare(root, cache, config, video_encoder="libx264")
    assert opened == [cache, cache]


@pytest.mark.parametrize("encode_fails", [False, True])
def test_prepare_selects_once_and_caps_only_video_workers_preserving_schedule(tmp_path, monkeypatch, encode_fails):
    prep = importlib.import_module("tianji_provider.prepare")
    root = tmp_path / "raw"; root.mkdir()
    bags = [root / f"my_bag-{index}" for index in range(6)]
    media = SimpleNamespace(paths=(root / "video.mcap",), width=160, height=96)
    episodes = [SimpleNamespace(index=index, length=index + 2, stats={}, data_path=bag,
                                videos={"observation.image.head_left": media}) for index, bag in enumerate(bags)]
    numeric_workers, selections, video_workers = [], [], []
    class Source:
        reports = []
        metadata = SimpleNamespace(features={"observation.image.head_left": {"shape": [96, 160, 3]}})
        def __init__(self, *args, **kwargs):
            numeric_workers.append(kwargs["_workers"])
            self.episodes = episodes
        def read_episode(self, ep):
            return pa.table({"timestamp": np.arange(ep.length, dtype=np.float32) / 50})
        def audit(self, ep):
            return {key: np.arange(ep.length, dtype=np.int64) for key in (
                "absolute_time_ns", "video_packet_index", "video_time_ns", "wrench_time_ns", "wrench_sample_time_ns")}
    def choose(*args):
        selections.append(args)
        return encoding.NVENC, {"requested": args[0], "selected": encoding.NVENC.name, "probed": True}
    def parallel(function, jobs, workers=None):
        video_workers.append(workers)
        assert [job.episodes[0].index for job in jobs] == [5, 4, 3, 2, 1, 0]
        assert all(job.encoder == encoding.NVENC for job in jobs)
        if encode_fails:
            raise RuntimeError("Hardware encode session failed after successful probe")
        for index, job in enumerate(jobs):
            yield index, ({"bag": job.bag, "frames": job.episodes[0].length, "video_seconds": 1}, {})
    monkeypatch.setattr(prep, "TianjiSource", Source)
    monkeypatch.setattr(prep, "fingerprint", lambda _: [])
    monkeypatch.setattr(prep, "discover", lambda _: bags)
    monkeypatch.setattr(prep, "select_encoder", choose)
    monkeypatch.setattr(prep, "iter_parallel", parallel)
    if encode_fails:
        with pytest.raises(RuntimeError, match="Hardware encode session failed"):
            prep.prepare(root, tmp_path / "new-cache", TianjiConfig(task="test"), workers=12)
        assert selections == [("auto", 160, 96, 50)]
        assert not (tmp_path / "new-cache/tianji-cache.json").exists()
        return
    result = prep.prepare(root, tmp_path / "new-cache", TianjiConfig(task="test"), workers=12)
    assert selections == [("auto", 160, 96, 50)]
    assert numeric_workers == [12] and video_workers == [6]
    assert result["video_encoding"] == encoding.NVENC.metadata()
    assert "crf" not in result["video_encoding"]
    assert [item["bag"] for item in result["timings"]["recordings"]] == [bag.name for bag in bags]


def test_child_smoke_validator_can_be_exercised_with_software_codec(monkeypatch):
    # No GPU dependency in the unit suite: exercise real in-memory encoding,
    # consumption of options, H.264 profile checks, flush and decode with x264.
    monkeypatch.setattr(encoding, "NVENC", encoding.LIBX264)
    report = encoding._run_probe(160, 96, 50)
    assert report["ok"] and report["frames"] == 6


def test_child_smoke_rejects_options_ignored_by_real_codec(monkeypatch):
    invalid = encoding.EncoderSpec("libx264", encoding.LIBX264.options + (("unrecognized_probe_option", "1"),))
    monkeypatch.setattr(encoding, "NVENC", invalid)
    with pytest.raises(RuntimeError, match="ignored options"):
        encoding._run_probe(160, 96, 50)
