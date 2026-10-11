"""Exact RGB histogram tests for the OpenCV candidate and its integer fallback."""

import numpy as np
import pytest

from tianji_provider import media
from tianji_provider.media import RGBStatistics


def reference_histogram(image):
    return np.stack([
        np.bincount(image[:, :, channel].reshape(-1), minlength=256)
        for channel in range(3)
    ])


def assert_matches_reference(actual, images):
    expected = RGBStatistics()
    for image, weight in images:
        expected.histogram += weight * reference_histogram(image)
        expected.frames += weight
        actual.add(image, weight)
        np.testing.assert_array_equal(actual.histogram, expected.histogram)
        assert actual.frames == expected.frames
        assert actual.result() == expected.result()


def test_exact_weighted_histograms_and_results():
    rng = np.random.default_rng(12345)
    images = [
        (np.zeros((540, 960, 3), dtype=np.uint8), 1),
        (np.full((540, 960, 3), 255, dtype=np.uint8), 2),
        (rng.integers(0, 256, (540, 960, 3), dtype=np.uint8), 3),
        (np.tile(np.arange(256, dtype=np.uint8)[:, None, None], (1, 13, 3)), 7),
    ]
    assert_matches_reference(RGBStatistics(), images)


@pytest.mark.parametrize("view", ["crop", "strided", "reversed_channels", "reversed_rows"])
def test_noncontiguous_rgb_images(view):
    rng = np.random.default_rng(8)
    quad = rng.integers(0, 256, (108, 192, 3), dtype=np.uint8)
    image = {
        "crop": quad[:54, :96],
        "strided": quad[::2, ::2],
        "reversed_channels": quad[:, :, ::-1],
        "reversed_rows": quad[::-1],
    }[view]
    assert not image.flags.c_contiguous
    assert_matches_reference(RGBStatistics(), [(image, 2)])


def test_channel_order_is_rgb():
    image = np.array([[[0, 127, 255], [0, 128, 254]]], dtype=np.uint8)
    stats = RGBStatistics()
    stats.add(image)
    assert stats.histogram[0, 0] == 2
    assert stats.histogram[1, 127] == stats.histogram[1, 128] == 1
    assert stats.histogram[2, 254] == stats.histogram[2, 255] == 1
    assert stats.histogram.sum() == 6


def test_weight_exceeding_uint32_keeps_int64_counts():
    image = np.array([[[0, 127, 255]]], dtype=np.uint8)
    stats = RGBStatistics()
    assert_matches_reference(stats, [(image, 2**32), (image, 13)])
    assert stats.histogram.dtype == np.int64
    assert stats.histogram[0, 0] == 2**32 + 13
    assert stats.result()["count"] == [2**32 + 13]


def test_4k_frame_has_exact_integer_counts():
    image = np.zeros((2160, 3840, 3), dtype=np.uint8)
    assert_matches_reference(RGBStatistics(), [(image, 3)])


def test_float32_integer_boundary_is_exact():
    # Worst-case one-bin count exactly at float32's consecutive-integer limit.
    image = np.zeros((4096, 4096, 3), dtype=np.uint8)
    stats = RGBStatistics()
    stats.add(image, 3)
    expected = np.zeros((3, 256), dtype=np.int64)
    expected[:, 0] = (2**24) * 3
    np.testing.assert_array_equal(stats.histogram, expected)
    assert stats.result()["count"] == [3]
    assert stats.result()["mean"] == [[[0.0]], [[0.0]], [[0.0]]]


def test_above_float32_integer_limit_uses_exact_fallback(monkeypatch):
    # Odd count 2**24+1 is not representable in float32. Broadcasting avoids
    # allocating a giant image; the bincount path still sees every real pixel.
    image = np.broadcast_to(np.array([[[0, 127, 255]]], dtype=np.uint8), (1, 2**24 + 1, 3))

    def forbidden_float_histogram(*args, **kwargs):
        raise AssertionError("A frame larger than 2**24 pixels must use integer bincount")

    monkeypatch.setattr(media.cv2, "calcHist", forbidden_float_histogram)
    stats = RGBStatistics()
    stats.add(image, 2)
    expected = np.zeros((3, 256), dtype=np.int64)
    expected[0, 0] = expected[1, 127] = expected[2, 255] = (2**24 + 1) * 2
    np.testing.assert_array_equal(stats.histogram, expected)
    assert stats.frames == 2


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.uint16, np.int64])
def test_non_uint8_is_rejected_without_updating_statistics(dtype):
    stats = RGBStatistics()
    with pytest.raises(TypeError, match="uint8"):
        stats.add(np.zeros((4, 4, 3), dtype=dtype))
    assert stats.frames == 0
    assert not stats.histogram.any()


@pytest.mark.parametrize("shape", [(4, 4), (4, 4, 1), (4, 4, 4), (1, 4, 4, 3), (0, 4, 3), (4, 0, 3)])
def test_non_rgb_shape_is_rejected_without_updating_statistics(shape):
    stats = RGBStatistics()
    with pytest.raises(ValueError, match="H x W x 3"):
        stats.add(np.zeros(shape, dtype=np.uint8))
    assert stats.frames == 0
    assert not stats.histogram.any()
