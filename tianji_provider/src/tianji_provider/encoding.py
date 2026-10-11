"""Explicit H.264 recipes and crash-isolated optional hardware discovery.

NVENC CQ and x264 CRF are different quality controls. The recipe below must be
validated against real camera sequences before accepting it as a default.
Only the discovery probe may fall back; a failing production encode is fatal.
"""

from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
import json
import subprocess
import sys
import time

from .execution import worker_count

VIDEO_ENCODERS = ("auto", "libx264", "h264_nvenc")
NVENC_WORKER_LIMIT = 12  # Four cameras per recording: at most forty-eight sessions.


@dataclass(frozen=True)
class EncoderSpec:
    name: str
    options: tuple[tuple[str, str], ...]
    hardware: bool = False
    pixel_format: str = "yuv420p"

    def metadata(self):
        options = dict(self.options)
        result = {"codec": "h264", "encoder": self.name, "pixel_format": self.pixel_format,
                  "preset": options["preset"], "b_frames": 0,
                  "intermediate_lossy_encoding": False, "options": options}
        if self.hardware:
            result.update(rate_control=options["rc"], cq=int(options["cq"]),
                          tuning=options["tune"], bit_rate=int(options["b"]), profile=options["profile"])
        else:
            result["crf"] = int(options["crf"])
        return result


LIBX264 = EncoderSpec("libx264", (("preset", "veryfast"), ("crf", "18"), ("bf", "0")))
NVENC = EncoderSpec("h264_nvenc", (("preset", "p4"), ("tune", "hq"), ("rc", "vbr"),
                                  ("cq", "16"), ("b", "0"), ("bf", "0"), ("profile", "main")), hardware=True)


def validate_encoder_request(requested):
    if requested not in VIDEO_ENCODERS:
        raise ValueError(f"video_encoder must be one of {VIDEO_ENCODERS}, got {requested!r}")


def cache_encoder_matches(manifest, requested):
    """Auto reuses either accepted recipe; explicit requests require that one.

    Old v4 software manifests did not store an options map. Their existing
    explicit CRF/preset/pixel-format metadata still identifies the x264 recipe.
    This function does not replace Source's resource/config validation.
    """
    validate_encoder_request(requested)
    if requested == "auto":
        # Auto means choose one of our verified recipes, not trust arbitrary
        # H.264 metadata. A preexisting CQ18/other-profile cache must not bypass
        # a newer quality contract simply because no re-encode is requested.
        return any(cache_encoder_matches(manifest, choice) for choice in ("libx264", "h264_nvenc"))
    expected = (LIBX264 if requested == "libx264" else NVENC).metadata()
    actual = manifest.get("video_encoding", {})
    return all(actual.get(key) == value for key, value in expected.items()
               if key != "options" or "options" in actual or requested == "h264_nvenc")


def encoding_workers(requested, task_count, spec):
    resolved = worker_count(requested, task_count)
    return min(resolved, NVENC_WORKER_LIMIT) if spec.hardware else resolved


def _probe_nvenc(width, height, fps, timeout):
    """Run the risky codec code in another interpreter, inheriting job PGID."""
    started = time.perf_counter()
    command = [sys.executable, "-m", "tianji_provider.encoding", "--probe",
               str(width), str(height), str(fps)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its child on timeout. The probe
        # itself creates no child processes, only codec threads.
        return {"ok": False, "reason": f"NVENC probe timed out after {timeout:g} seconds",
                "seconds": time.perf_counter() - started}
    except OSError as error:
        return {"ok": False, "reason": f"Cannot run NVENC probe: {error}",
                "seconds": time.perf_counter() - started}
    details = {"returncode": result.returncode, "seconds": time.perf_counter() - started,
               "stderr": result.stderr[-4000:]}
    if result.returncode:
        return {**details, "ok": False,
                "reason": f"NVENC probe exited with code {result.returncode}: {result.stderr[-1000:].strip()}"}
    try:
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise ValueError("missing successful probe verdict")
        if report.get("encoder") != NVENC.name or report.get("options") != dict(NVENC.options):
            raise ValueError("probe encoder recipe differs from requested recipe")
    except (ValueError, TypeError) as error:
        return {**details, "ok": False, "reason": f"Invalid NVENC probe result: {error}"}
    return {**details, **report}


def select_encoder(requested, width, height, fps, *, timeout=20):
    """Select once per new prepare; no global availability result is cached."""
    validate_encoder_request(requested)
    if requested == "libx264":
        return LIBX264, {"requested": requested, "selected": LIBX264.name, "probed": False}
    probe = _probe_nvenc(width, height, fps, timeout)
    if probe["ok"]:
        return NVENC, {"requested": requested, "selected": NVENC.name, "probed": True, "probe": probe}
    if requested == "h264_nvenc":
        raise RuntimeError(f"Explicit h264_nvenc request cannot run: {probe['reason']}")
    return LIBX264, {"requested": requested, "selected": LIBX264.name, "probed": True,
                    "fallback_reason": probe["reason"], "probe": probe}


def _run_probe(width, height, fps):
    """Six frames in memory; no file output or changes to device configuration.

    This deliberately runs only in the subprocess. Success checks codec open,
    encode+flush, decode, timestamps and ordinary H.264 profile, not image
    quality or multi-session throughput.
    """
    import av
    import numpy as np

    # Avoid leaving a multi-GB core dump on a driver/native codec crash.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError):
        pass
    data = BytesIO()
    count = 6
    packet_times = []
    with av.open(data, "w", format="mp4") as container:
        stream = container.add_stream(NVENC.name, rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, NVENC.pixel_format
        stream.options = dict(NVENC.options)
        stream.codec_context.thread_count = 1
        stream.codec_context.gop_size = fps
        stream.codec_context.max_b_frames = 0
        container.start_encoding()
        if stream.codec_context.options:
            raise RuntimeError(f"NVENC ignored options: {stream.codec_context.options}")
        for index in range(count):
            pixels = np.full((height, width, 3), 24 + index * 20, dtype=np.uint8)
            pixels[::8, ::8] = [250, 128, 4]
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(format=NVENC.pixel_format)
            frame.pts, frame.time_base = index, Fraction(1, fps)
            for packet in stream.encode(frame):
                packet_times.append((packet.pts * packet.time_base, packet.dts * packet.time_base))
                container.mux(packet)
        for packet in stream.encode(None):
            packet_times.append((packet.pts * packet.time_base, packet.dts * packet.time_base))
            container.mux(packet)
    expected = [Fraction(index, fps) for index in range(count)]
    if packet_times != [(value, value) for value in expected]:
        raise RuntimeError("NVENC probe packet PTS/DTS does not match submitted frames")
    data.seek(0)
    with av.open(data, "r") as container:
        stream = container.streams.video[0]
        profile = stream.codec_context.profile
        if profile not in {"Baseline", "Constrained Baseline", "Main", "High"}:
            raise RuntimeError(f"NVENC probe produced unsupported/default-incompatible H.264 profile: {profile}")
        if stream.codec_context.has_b_frames:
            raise RuntimeError("NVENC probe unexpectedly produced B frames")
        decoded = 0
        for frame in container.decode(video=0):
            if (frame.width, frame.height, frame.format.name) != (width, height, NVENC.pixel_format):
                raise RuntimeError("NVENC probe changed resolution or pixel format")
            if frame.pts * frame.time_base != Fraction(decoded, fps):
                raise RuntimeError("NVENC probe changed frame timestamps")
            decoded += 1
        if decoded != count:
            raise RuntimeError(f"NVENC probe decoded {decoded} frames instead of {count}")
    return {"ok": True, "encoder": NVENC.name, "options": dict(NVENC.options),
            "frames": count, "width": width, "height": height, "fps": fps, "profile": profile}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--probe":
            raise ValueError("This internal entry point requires --probe WIDTH HEIGHT FPS")
        print(json.dumps(_run_probe(*(int(value) for value in sys.argv[2:]))), flush=True)
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
