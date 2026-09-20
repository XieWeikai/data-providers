"""Bounded video decoding; repeated 50 Hz frames reuse the selected 30 Hz image."""

from dataclasses import dataclass
from fractions import Fraction

import av
import numpy as np
from letools.model import FrameSequence

from .reader import video_packets

CAMERAS = {"head_left": (0, 0), "head_right": (1, 0), "left_wrist": (0, 1), "right_wrist": (1, 1)}


def decode_quad(paths):
    """Keep H.264 state across packets and flush delayed frames at EOF.

    This preset requires one access unit per ROS message, with monotonically
    ordered presentation timestamps. Never silently associate frame i with an
    unrelated packet if a different recorder encoding is encountered.
    """
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 2
    emitted, count = 0, 0
    for i, (_, payload) in enumerate(video_packets(paths)):
        packet = av.Packet(payload)
        packet.pts = i
        packet.time_base = Fraction(1, 1_000_000)
        count += 1
        for frame in decoder.decode(packet):
            if frame.pts != emitted:
                raise ValueError(f"H.264 frame/packet mapping is ambiguous: expected {emitted}, got {frame.pts}")
            yield emitted, frame
            emitted += 1
    for frame in decoder.decode(None):
        if frame.pts != emitted:
            raise ValueError("Delayed H.264 frame timestamp/order mismatch")
        yield emitted, frame
        emitted += 1
    if emitted != count:
        raise ValueError(f"H.264 decoded {emitted} frames for {count} timestamped packets")


def dimensions(paths):
    it = decode_quad(paths)
    try:
        _, frame = next(it)
        if frame.width % 4 or frame.height % 4:
            raise ValueError("Quad video dimensions must be divisible by four for YUV420 crops")
        return frame.width // 2, frame.height // 2
    finally:
        it.close()


def crop(array, camera):
    col, row = CAMERAS[camera]
    h, w = array.shape[0] // 2, array.shape[1] // 2
    return np.ascontiguousarray(array[row*h:(row+1)*h, col*w:(col+1)*w])


class RGBStatistics:
    """Exact per-channel moments without retaining decoded images."""

    def __init__(self):
        self.histogram = np.zeros((3, 256), dtype=np.int64)
        self.frames = 0

    def add(self, image, weight=1):
        for channel in range(3):
            self.histogram[channel] += weight * np.bincount(image[:, :, channel].reshape(-1), minlength=256)
        self.frames += weight

    def result(self):
        levels = np.arange(256, dtype=np.float64) / 255
        hist = self.histogram
        count = hist.sum(axis=1)
        mean = (hist * levels).sum(axis=1) / count
        var = (hist * levels**2).sum(axis=1) / count - mean**2
        return {"mean": mean[:, None, None].tolist(), "std": np.sqrt(np.maximum(var, 0))[:, None, None].tolist(),
                "min": np.array([levels[np.flatnonzero(row)[0]] for row in hist])[:, None, None].tolist(),
                "max": np.array([levels[np.flatnonzero(row)[-1]] for row in hist])[:, None, None].tolist(), "count": [self.frames]}


def image_statistics(episodes):
    """Collect all segment/camera statistics in one bounded pass over a bag."""
    cursor = 0
    counts = {ep.index: {c: RGBStatistics() for c in CAMERAS} for ep in episodes}
    first = episodes[0].videos["observation.image.head_left"]
    for index, frame in decode_quad(first.paths):
        if cursor >= len(episodes):
            continue
        ep = episodes[cursor]
        wanted = ep.videos["observation.image.head_left"].indices
        if index < wanted[0]:
            continue
        rgb = None
        while cursor < len(episodes):
            ep = episodes[cursor]
            wanted = ep.videos["observation.image.head_left"].indices
            a, b = np.searchsorted(wanted, index, side="left"), np.searchsorted(wanted, index, side="right")
            if b > a:
                if rgb is None:
                    rgb = frame.to_ndarray(format="rgb24")
                for camera in CAMERAS:
                    counts[ep.index][camera].add(crop(rgb, camera), int(b-a))
            if index >= wanted[-1]:
                cursor += 1
            else:
                break
    if cursor < len(episodes):
        raise ValueError("Video ended before statistics covered every selected frame")
    return {i: {f"observation.image.{c}": value.result() for c, value in cams.items()} for i, cams in counts.items()}


@dataclass(frozen=True)
class TianjiFrames(FrameSequence):
    """Lossless RGB transport to the backend; only the target video is lossy."""

    paths: tuple
    indices: np.ndarray
    camera: str
    width: int
    height: int
    estimated_size_bytes: int
    encoded_format: str = "ppm"

    @property
    def frame_count(self):
        return len(self.indices)

    def _iterate(self, start, stop, batch_frames):
        if not 0 <= start <= stop <= self.frame_count:
            raise IndexError("Invalid frame range")
        if start == stop:
            return
        encoder = av.CodecContext.create("ppm", "w")
        encoder.width, encoder.height = self.width, self.height
        encoder.pix_fmt = "rgb24"
        encoder.time_base = Fraction(1, 30)
        encoder.thread_count = 1
        # PPM transports exact RGB bytes with no rate control or chroma loss.
        # It also avoids PNG decoder delta-frame state when a backend repeats
        # an encoded packet without preserving its original keyframe flag.
        encoder.open()
        if encoder.options:
            raise ValueError(f"PPM encoder ignored options: {encoder.options}")
        desired, cursor, batch = self.indices[start:stop], 0, []
        decoded = decode_quad(self.paths)
        try:
            for index, frame in decoded:
                if index < desired[cursor]:
                    continue
                tile = crop(frame.to_ndarray(format="rgb24"), self.camera)
                image = av.VideoFrame.from_ndarray(tile, format="rgb24")
                # Every packet is independently decodable, including repeats.
                image.pict_type = av.video.frame.PictureType.I
                packets = encoder.encode(image)
                if len(packets) != 1:
                    raise ValueError("PPM encoder did not return one independent frame")
                encoded = bytes(packets[0])
                while cursor < len(desired) and desired[cursor] == index:
                    batch.append(encoded)
                    cursor += 1
                    if len(batch) == batch_frames:
                        yield tuple(batch)
                        batch.clear()
                if cursor == len(desired):
                    if batch:
                        yield tuple(batch)
                    return
            raise ValueError("H.264 stream ended before the final selected frame")
        finally:
            decoded.close()

    def read_batch(self, start, stop):
        return tuple(item for batch in self._iterate(start, stop, max(1, stop-start)) for item in batch)

    def iter_batches(self, batch_frames):
        if batch_frames <= 0:
            raise ValueError("batch_frames must be positive")
        yield from self._iterate(0, self.frame_count, batch_frames)
