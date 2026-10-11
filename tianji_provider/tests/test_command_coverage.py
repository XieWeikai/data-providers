"""Regression tests for partial command coverage and strict measured observations."""

import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
import yaml

from tianji_provider import reader
from tianji_provider.alignment import COMMAND_STATE, align, command_or_state
from tianji_provider.diagnostics import timing_issues
from tianji_provider.provider import TianjiConfig
from tianji_provider.reader import Signal
from tianji_provider.source import TianjiSource

T0 = 1_800_000_000_000_000_000


def signal(times, width, value=0):
    return Signal(np.asarray(times, dtype=np.int64), np.full((len(times), width), value, dtype=float))


def fixture():
    times = T0 + np.arange(51, dtype=np.int64) * 20_000_000
    signals = {"/tj/joint_states": signal(times, 14, .25)}
    for side in ("L", "R"):
        signals[f"/info/gripper_feedback_{side}"] = signal(times, 1, .4)
    for side in ("left", "right"):
        pose = signal(times, 7)
        pose.values[:, -1] = 1
        signals[f"/tj/info/eef_{side}"] = pose
        signals[f"/tj/info/wrench_{side}"] = signal(times, 6)
    for topic in COMMAND_STATE:
        signals[topic] = signal([], 7 if "joint_cmd" in topic else 1)
    ranges = {k: [int(v.times[0]), int(v.times[-1]), len(v.times)]
              for k, v in signals.items() if len(v.times)}
    config = TianjiConfig(task="Pick and place", wrench_fps=50, trim_stationary=False)
    return times, signals, ranges, config


@pytest.mark.parametrize("topic", list(COMMAND_STATE))
def test_any_one_command_stream_is_sufficient(topic):
    times, signals, ranges, config = fixture()
    signals[topic] = signal(times, 7 if "joint_cmd" in topic else 1, .8)
    (grid, columns, _), = align(signals, ranges, times, config)[0]
    state, action = columns["observation.state"], columns["observation.action"]
    selections = {list(COMMAND_STATE)[0]: slice(0, 7), list(COMMAND_STATE)[1]: slice(8, 15),
                  list(COMMAND_STATE)[2]: slice(7, 8), list(COMMAND_STATE)[3]: slice(15, 16)}
    expected = state.copy()
    expected[:, selections[topic]] = .8
    np.testing.assert_array_equal(action, expected)
    np.testing.assert_array_equal(grid, times)
    issues = timing_issues(signals, ranges, times, config)
    assert len(issues) == 3
    assert all(i["severity"] == "info" and i["command_stream_empty"] and i["last_message_ns"] is None for i in issues)


def test_command_boundaries_do_not_shorten_other_streams():
    times, signals, ranges, config = fixture()
    # A is valid through 0.5 s inclusive; B begins at 0.5 s and continues.
    a, b = list(COMMAND_STATE)[:2]
    signals[a] = signal(times[:21], 7, .7)
    signals[b] = signal(times[25:], 7, .9)
    ranges.update({k: [int(signals[k].times[0]), int(signals[k].times[-1]), len(signals[k].times)] for k in (a, b)})
    segments, report = align(signals, ranges, times, config)
    grid, columns, _ = segments[0]
    assert len(grid) == 51 and report["minimum_fresh_command_topics"] == 1
    action, state = columns["observation.action"], columns["observation.state"]
    np.testing.assert_array_equal(action[:25, 8:15], state[:25, 8:15])
    np.testing.assert_array_equal(action[26:, :7], state[26:, :7])
    np.testing.assert_allclose(action[:26, :7], .7)
    np.testing.assert_allclose(action[25:, 8:15], .9)


def test_no_future_command_and_inclusive_age_boundary():
    grid = T0 + np.array([0, 20, 120, 140], dtype=np.int64) * 1_000_000
    state = np.arange(8, dtype=float).reshape(4, 2)
    values, fallback = command_or_state(signal([grid[1]], 2, 99), grid, state, 100_000_000)
    np.testing.assert_array_equal(fallback, [True, False, False, True])
    np.testing.assert_array_equal(values[[0, 3]], state[[0, 3]])
    np.testing.assert_array_equal(values[[1, 2]], np.full((2, 2), 99))


@pytest.mark.parametrize("topic,columns,max_age_ms", [
    ("/tj/control/joint_cmd_A", slice(0, 7), 100),
    ("/tj/control/joint_cmd_B", slice(8, 15), 100),
    ("/control/gripperValueL", slice(7, 8), 150),
    ("/control/gripperValueR", slice(15, 16), 150),
])
def test_arm_and_gripper_commands_share_state_fallback_at_every_pause(topic, columns, max_age_ms):
    times, signals, ranges, config = fixture()
    backup = "/tj/control/joint_cmd_A" if topic == "/tj/control/joint_cmd_B" else "/tj/control/joint_cmd_B"
    signals[backup] = signal(times, 7, .6)
    signals[topic] = signal(times[[1, 40]], 7 if "joint_cmd" in topic else 1, .8)
    # Legacy info targets cannot keep a missing control stream alive.
    ranges["/info/gripper_target_L"] = [int(times[0]), int(times[-1]), len(times)]
    ranges["/info/gripper_target_R"] = [int(times[0]), int(times[-1]), len(times)]
    segments, report = align(signals, ranges, times, config)
    grid, values, _ = segments[0]
    state = values["observation.state"][:, columns]
    action = values["observation.action"][:, columns]
    fresh = np.zeros(len(times), dtype=bool)
    for stamp in times[[1, 40]]:
        fresh |= (grid >= stamp) & (grid <= stamp + max_age_ms * 1_000_000)
    np.testing.assert_allclose(action[fresh], .8)
    np.testing.assert_array_equal(action[~fresh], state[~fresh])
    assert report["command_fallback_frames"][topic] == int(np.sum(~fresh))
    assert len(report["command_fallback_ranges"][topic]) == 3


def test_all_commands_missing_fails():
    times, signals, ranges, config = fixture()
    with pytest.raises(ValueError, match="at least one"):
        align(signals, ranges, times, config)


def test_all_commands_stale_interior_fails_without_dropping_frames():
    times, signals, ranges, config = fixture()
    signals[list(COMMAND_STATE)[0]] = signal([times[0], times[-1]], 7)
    with pytest.raises(ValueError, match="No fresh control command"):
        align(signals, ranges, times, config)
    gaps = [i for i in timing_issues(signals, ranges, times, config) if i["code"] == "command_coverage_gap"]
    assert len(gaps) == 1 and gaps[0]["affected_frames"] == 44
    assert gaps[0]["severity"] == "error"


def test_command_union_intersects_observations_with_freshness_tail():
    times, signals, ranges, config = fixture()
    signals[list(COMMAND_STATE)[0]] = signal([times[10], times[15]], 7)
    segments, report = align(signals, ranges, times, config)
    np.testing.assert_array_equal(segments[0][0], times[10:21])
    assert report["intersection_end_ns"] == times[20]


def test_empty_arm_does_not_disable_edge_trim_or_remove_middle_pause():
    from dataclasses import replace
    times, signals, ranges, config = fixture()
    t = np.arange(len(times)) * .02
    movement = np.clip(t - .2, 0, .2) + np.clip(t - .6, 0, .2)
    signals["/tj/joint_states"].values[:, 7:] += movement[:, None]
    signals["/tj/control/joint_cmd_B"] = signal(times, 7, .9)
    segments, report = align(signals, ranges, times, replace(config, trim_stationary=True, state_resampling="linear"))
    grid, columns, _ = segments[0]
    np.testing.assert_array_equal(grid, times[10:41])
    assert times[25] in grid and report["dropped_frames"] == 0
    np.testing.assert_array_equal(columns["observation.action"][:, :7], columns["observation.state"][:, :7])


@pytest.mark.parametrize("topic", ["/tj/joint_states", "/info/gripper_feedback_L", "/tj/info/wrench_right", "video"])
def test_command_fallback_does_not_hide_observation_gaps(topic):
    times, signals, ranges, config = fixture()
    signals[list(COMMAND_STATE)[0]] = signal(times, 7)
    if topic == "video":
        vt = times[np.r_[0:10, 25:51]]
    else:
        vt = times
        s = signals[topic]
        signals[topic] = Signal(s.times[np.r_[0:10, 25:51]], s.values[np.r_[0:10, 25:51]])
    with pytest.raises(ValueError, match="Missing .*observations"):
        align(signals, ranges, vt, config)


def fake_recording(tmp_path, monkeypatch, *, empty_metadata=(), omit=()):
    data = tmp_path / "data"
    data.mkdir()
    (data / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
        "topics_with_message_count": [{"topic_metadata": {"name": k}, "message_count": 0} for k in empty_metadata]}}))
    rows = []
    for topic, schema in reader.TYPES.items():
        if topic in omit:
            continue
        if topic == "/info/web_gripper":
            msg = NS(data=json.dumps({"calibration": [[0, 1.6], [0, 1.6]], "position_unit": "motor_radian", "target_unit": "normalized_0_1"}))
        elif topic == "/tj/joint_states":
            msg = NS(name=list(reader.JOINT_NAMES), position=[.25] * 14)
        elif "joint_cmd" in topic:
            msg = NS(positions=[.8] * 7)
        elif "gripper_feedback" in topic:
            msg = NS(data=[.4, 0, 0, 0, 0])
        elif topic.startswith("/control/gripperValue"):
            msg = NS(data=.5)
        elif "eef" in topic:
            msg = NS(pose=NS(position=NS(x=0, y=0, z=0), orientation=NS(x=0, y=0, z=0, w=1)))
        else:
            msg = NS(wrench=NS(force=NS(x=0, y=0, z=0), torque=NS(x=0, y=0, z=0)))
        if topic.startswith("/tj/"):
            msg.header = NS(stamp=NS(sec=T0 // 1_000_000_000, nanosec=0),
                            frame_id="base_link" if "wrench" not in topic else "flange_L" if topic.endswith("left") else "flange_R")
        rows.append((NS(name=schema), NS(topic=topic), NS(log_time=T0), msg))
    monkeypatch.setattr(reader, "bag_files", lambda _: ())
    monkeypatch.setattr(reader, "messages", lambda _: iter(rows))
    return tmp_path


@pytest.mark.parametrize("metadata_declared", [False, True])
@pytest.mark.parametrize("empty", [tuple(COMMAND_STATE)[:1], tuple(COMMAND_STATE)[1:]])
def test_reader_allows_empty_or_absent_command_topics(tmp_path, monkeypatch, metadata_declared, empty):
    bag = fake_recording(tmp_path, monkeypatch, empty_metadata=empty if metadata_declared else (), omit=empty)
    signals, ranges, _, _ = reader.read_numeric(bag)
    for topic in empty:
        assert not len(signals[topic].times) and topic not in ranges
        assert signals[topic].values.shape == (0, 7 if "joint_cmd" in topic else 1)


@pytest.mark.parametrize("topic", ["/tj/joint_states", "/info/gripper_feedback_R", "/info/web_gripper", "/tj/info/eef_left"])
def test_reader_still_rejects_absent_observations(tmp_path, monkeypatch, topic):
    bag = fake_recording(tmp_path, monkeypatch, omit=[topic])
    with pytest.raises(ValueError):
        reader.read_numeric(bag)


def test_reader_still_rejects_empty_auxiliary_topic(tmp_path, monkeypatch):
    bag = fake_recording(tmp_path, monkeypatch, empty_metadata=["/info/guiding_session"])
    with pytest.raises(ValueError, match="cannot intersect empty"):
        reader.read_numeric(bag)


@pytest.mark.parametrize("positions", [[float('nan')] * 7, [0] * 6])
def test_invalid_recorded_commands_are_not_hidden_by_state_fallback(tmp_path, monkeypatch, positions):
    bag = fake_recording(tmp_path, monkeypatch)
    rows = list(reader.messages(()))
    for _, channel, _, msg in rows:
        if channel.topic == "/tj/control/joint_cmd_A":
            msg.positions = positions
    monkeypatch.setattr(reader, "messages", lambda _: iter(rows))
    with pytest.raises(ValueError):
        reader.read_numeric(bag)


def test_old_cache_is_not_silently_reused(tmp_path):
    (tmp_path / "tianji-cache.json").write_text(json.dumps({"format": "tianji-thor-v2"}))
    with pytest.raises(ValueError, match="rebuild"):
        TianjiSource(tmp_path, TianjiConfig(task="Pick and place")).metadata
