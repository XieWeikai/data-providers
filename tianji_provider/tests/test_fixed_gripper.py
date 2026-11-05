"""Fixed thor-v1 gripper mapping does not depend on auxiliary status traffic."""
from types import SimpleNamespace as NS
import json

import numpy as np
import pytest

from tianji_provider import reader
from test_command_coverage import fake_recording, T0


@pytest.mark.parametrize("position", [-0.032, 0.0, 0.8, 1.6, 1.632])
def test_fixed_mapping_without_web_gripper_and_without_clipping(tmp_path, monkeypatch, position):
    bag = fake_recording(tmp_path, monkeypatch)
    rows = list(reader.messages(()))
    for _, channel, _, message in rows:
        if "gripper_feedback" in channel.topic:
            message.data[0] = position
    monkeypatch.setattr(reader, "messages", lambda *args, **kwargs: iter(rows))
    signals, ranges, clocks, calibration = reader.read_numeric(bag)
    assert "/info/web_gripper" not in reader.TYPES
    assert "/info/web_gripper" not in ranges and "/info/web_gripper" not in clocks
    assert calibration == [[0.0, 1.6], [0.0, 1.6]]
    for side in ("L", "R"):
        np.testing.assert_allclose(signals[f"/info/gripper_feedback_{side}"].values, position / 1.6)
        np.testing.assert_array_equal(signals[f"/control/gripperValue{side}"].values, [[0.5]])


def test_old_web_gripper_payload_schema_and_clock_are_ignored(tmp_path, monkeypatch):
    bag = fake_recording(tmp_path, monkeypatch, empty_metadata=["/info/web_gripper"])
    expected = reader.read_numeric(bag)
    rows = list(reader.messages(()))
    rows.append((NS(name="unknown/schema"), NS(topic="/info/web_gripper"),
                 NS(log_time=T0 - 10_000_000_000),
                 NS(data="not JSON", header=NS(stamp=NS(sec=0, nanosec=0)))))
    monkeypatch.setattr(reader, "messages", lambda *args, **kwargs: iter(rows))
    actual = reader.read_numeric(bag)
    assert actual[1:] == expected[1:]
    for topic in expected[0]:
        np.testing.assert_array_equal(actual[0][topic].times, expected[0][topic].times)
        np.testing.assert_array_equal(actual[0][topic].values, expected[0][topic].values)


@pytest.mark.parametrize("old_format", ["tianji-thor-v4", "tianji-thor-v7"])
def test_recorded_calibration_cache_requires_rebuild(tmp_path, old_format):
    from tianji_provider.provider import TianjiConfig
    from tianji_provider.source import FORMAT, TianjiSource
    assert FORMAT.endswith("-fixed-gripper")
    (tmp_path / "tianji-cache.json").write_text(json.dumps({"format": old_format}))
    with pytest.raises(ValueError, match="rebuild"):
        TianjiSource(tmp_path, TianjiConfig(task="test")).metadata
