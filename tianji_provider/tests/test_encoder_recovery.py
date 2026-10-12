"""Resource failures must recover without hiding data or storage failures."""

import json
import pickle
from pathlib import Path

import av
import pytest

from tianji_provider.encoding import (
    HardwareEncodingError, LIBX264, NVENC, hardware_codec_call, with_encoder_recovery,
)


def test_codec_errors_keep_driver_details_and_survive_worker_transport():
    previous = av.logging.get_level()
    def failed():
        av.logging.log(av.logging.ERROR, "h264_nvenc", "CUDA_ERROR_OUT_OF_MEMORY\n")
        raise av.error.ExternalError(542398533, "avcodec_open2 failed")
    with pytest.raises(HardwareEncodingError, match="CUDA_ERROR_OUT_OF_MEMORY") as caught:
        hardware_codec_call(NVENC, "camera open", failed)
    assert str(pickle.loads(pickle.dumps(caught.value))) == str(caught.value)
    assert av.logging.get_level() == previous
    with pytest.raises(av.error.ExternalError):
        hardware_codec_call(LIBX264, "camera open", failed)
    with pytest.raises(OSError, match="disk full"):
        hardware_codec_call(NVENC, "camera open", lambda: (_ for _ in ()).throw(OSError("disk full")))


@pytest.mark.parametrize("error", [av.error.OSError(28, "No space left"),
                                   av.error.OSError(5, "I/O failure"),
                                   av.error.ValueError(22, "Invalid argument")])
def test_ffmpeg_storage_and_argument_errors_are_not_hardware_failures(error):
    def failed():
        raise error
    with pytest.raises(type(error)) as caught:
        hardware_codec_call(NVENC, "camera open", failed)
    assert caught.value is error


def test_runtime_retry_preserves_failed_files_and_uses_one_software_recipe(tmp_path):
    output = tmp_path / "cache"
    calls = []
    def attempt(codec, recovery):
        calls.append(codec)
        assert not output.exists()
        output.mkdir()
        if codec == "auto":
            (output / "partial.mp4").write_bytes(b"failed hardware attempt")
            raise HardwareEncodingError("CUDA_ERROR_OUT_OF_MEMORY")
        assert recovery["requested"] == "auto" and recovery["selected"] == "libx264"
        assert recovery["fallback_stage"] == "runtime"
        diagnostic = Path(recovery["failed_attempt"])
        assert (diagnostic / "cache/partial.mp4").read_bytes() == b"failed hardware attempt"
        assert json.loads((diagnostic / "failure.json").read_text()) == recovery
        assert not (output / "partial.mp4").exists()
        return recovery
    result = with_encoder_recovery(output, "auto", lambda: ["unchanged"], attempt)
    assert calls == ["auto", "libx264"] and result["failed_attempt_seconds"] >= 0


@pytest.mark.parametrize("choice,error", [
    ("h264_nvenc", HardwareEncodingError("no memory")),
    ("libx264", HardwareEncodingError("unexpected")),
    ("auto", ValueError("corrupt video")),
    ("auto", OSError("disk full")),
    ("auto", RuntimeError("worker process exited")),
])
def test_other_errors_and_explicit_choices_do_not_retry(tmp_path, choice, error):
    calls = []
    def attempt(codec, recovery):
        calls.append(codec)
        raise error
    with pytest.raises(type(error), match=str(error)):
        with_encoder_recovery(tmp_path / "cache", choice, lambda: [], attempt)
    assert calls == [choice] and list(tmp_path.iterdir()) == []


def test_changed_source_prevents_retry(tmp_path):
    snapshots = iter(["before", "after"])
    output = tmp_path / "cache"
    def attempt(codec, recovery):
        output.mkdir()
        raise HardwareEncodingError("no memory")
    with pytest.raises(ValueError, match="Source files or selection changed"):
        with_encoder_recovery(output, "auto", lambda: next(snapshots), attempt)
    assert list(tmp_path.iterdir()) == [output]


def test_second_failure_is_terminal_and_existing_cache_is_never_moved(tmp_path):
    output = tmp_path / "cache"
    calls = []
    def attempt(codec, recovery):
        calls.append(codec)
        output.mkdir(exist_ok=True)
        raise HardwareEncodingError("no memory")
    with pytest.raises(HardwareEncodingError):
        with_encoder_recovery(output, "auto", lambda: [], attempt)
    assert calls == ["auto", "libx264"]
    preserved = list(tmp_path.iterdir())
    calls.clear()
    with pytest.raises(HardwareEncodingError):
        with_encoder_recovery(output, "auto", lambda: [], attempt)
    assert calls == ["auto"] and list(tmp_path.iterdir()) == preserved


def test_source_change_between_attempts_leaves_no_success_cache(tmp_path):
    output = tmp_path / "cache"
    snapshots = iter(["before", "before", "after"])
    def attempt(codec, recovery):
        output.mkdir()
        if codec == "auto":
            raise HardwareEncodingError("no memory")
        (output / "tianji-cache.json").write_text('{}')
        return {}
    with pytest.raises(ValueError, match="changed across encoder attempts"):
        with_encoder_recovery(output, "auto", lambda: next(snapshots), attempt)
    assert not output.exists()
    assert len(list(tmp_path.glob('cache.failed-nvenc-*/software-cache-source-changed/tianji-cache.json'))) == 1
