"""Strict recorded actions, required observations and optional topic regressions."""
import json
import pickle
from dataclasses import replace
from types import SimpleNamespace as NS
import numpy as np
import pytest
import yaml
from tianji_provider import reader
from tianji_provider.alignment import COMMAND_STATE, JOINT_COMMAND_TOPICS, align, command_samples, command_targets
from tianji_provider.diagnostics import auxiliary_issues, numeric_error, timing_issues
from tianji_provider.provider import TianjiConfig
from tianji_provider.reader import Signal
from tianji_provider.source import TianjiSource

T0 = 1_800_000_000_000_000_000

def test_missing_signal_details_survive_parallel_worker_transport():
    topic = "/tj/control/joint_cmd_B"
    error = pickle.loads(pickle.dumps(reader.RequiredSignalMissing([topic])))
    assert error.topics == [topic] and error.code == "required_command_missing"

def signal(times, width, value=0):
    return Signal(np.asarray(times, dtype=np.int64), np.full((len(times), width), value, dtype=float))

def fixture(frames=51):
    times = T0 + np.arange(frames, dtype=np.int64) * 20_000_000
    signals = {"/tj/joint_states": signal(times, 14, .25)}
    for side in ("L", "R"):
        signals[f"/info/gripper_feedback_{side}"] = signal(times, 1, .4)
    for side in ("left", "right"):
        pose = signal(times, 7)
        pose.values[:, -1] = 1
        signals[f"/tj/info/eef_{side}"] = pose
        signals[f"/tj/info/wrench_{side}"] = signal(times, 6)
    for topic in COMMAND_STATE:
        signals[topic] = signal(times, 7 if "joint_cmd" in topic else 1, .8)
    ranges = {k: [int(v.times[0]), int(v.times[-1]), len(v.times)] for k,v in signals.items()}
    config = TianjiConfig(task="Pick and place", wrench_fps=50, trim_stationary=False)
    return times, signals, ranges, config

def test_action_uses_recorded_targets_even_when_feedback_disagrees():
    times, signals, ranges, config = fixture()
    segments, report = align(signals, ranges, times, config)
    grid, values, _ = segments[0]
    np.testing.assert_array_equal(grid, times)
    np.testing.assert_allclose(values["observation.action"], .8)
    assert not np.array_equal(values["observation.action"], values["observation.state"])
    assert report["command_fallback_policy"] == "joint_prefix_same_frame_feedback_only"
    assert report["minimum_valid_command_topics"] == 4
    assert timing_issues(signals, ranges, times, config) == []

@pytest.mark.parametrize("topic", list(COMMAND_STATE))
def test_any_one_missing_command_rejects_even_when_three_are_fresh(topic):
    times, signals, ranges, config = fixture()
    signals[topic] = signal([], 7 if "joint_cmd" in topic else 1)
    with pytest.raises(ValueError, match="Required action command has no messages"):
        align(signals, ranges, times, config)
    issues=timing_issues(signals, ranges, times, config)
    assert len(issues)==1 and issues[0]["topic"]==topic
    assert issues[0]["severity"]=="error" and issues[0]["affected_frames"]==51

@pytest.mark.parametrize("topic,scope", [(topic,scope) for topic in COMMAND_STATE
    for scope in ("head","middle","tail") if topic not in JOINT_COMMAND_TOPICS])
def test_command_gaps_at_edges_and_interior_are_not_cropped_away(topic, scope):
    times, signals, ranges, config = fixture()
    select={"head":slice(10,None), "tail":slice(None,30), "middle":np.r_[0:10,30:51]}[scope]
    signals[topic] = signal(times[select], 7 if "joint_cmd" in topic else 1, .9)
    ranges[topic]=[int(signals[topic].times[0]), int(signals[topic].times[-1]), len(signals[topic].times)]
    with pytest.raises(ValueError, match="Required action commands missing/stale"):
        align(signals, ranges, times, config)
    gaps=[i for i in timing_issues(signals,ranges,times,config) if i["code"]=="command_coverage_gap"]
    assert len(gaps)==1 and gaps[0]["topic"]==topic
    assert gaps[0]["severity"]=="error" and gaps[0]["affected_frames"]>0

def test_no_future_command_and_inclusive_age_boundary():
    grid=T0+np.array([0,20,120,140],dtype=np.int64)*1_000_000
    command=signal([grid[1]],2,99)
    _,fresh=command_samples(command,grid,100_000_000)
    np.testing.assert_array_equal(fresh,[False,True,True,False])
    np.testing.assert_array_equal(command_targets(command,grid[1:3],100_000_000,"test"),np.full((2,2),99))
    with pytest.raises(ValueError,match="missing/stale"):
        command_targets(command,grid,100_000_000,"test")

def test_sparse_auxiliary_and_single_calibration_do_not_shorten_output():
    times,signals,ranges,config=fixture()
    ranges["/tj/dagger/events"]=[int(times[25]),int(times[25]),1]
    ranges["/info/web_gripper"]=[int(times[-1]),int(times[-1]),1]
    ranges["/info/guiding_session"]=[int(times[10]),int(times[11]),2]
    segments,report=align(signals,ranges,times,config)
    np.testing.assert_array_equal(segments[0][0],times)
    assert report["dropped_frames"]==0

def test_stationary_edges_can_trim_but_middle_pause_is_preserved():
    times,signals,ranges,config=fixture()
    t=np.arange(len(times))*.02
    movement=np.clip(t-.2,0,.2)+np.clip(t-.6,0,.2)
    signals["/tj/joint_states"].values[:,7:]+=movement[:,None]
    segments,report=align(signals,ranges,times,replace(config,trim_stationary=True,state_resampling="linear"))
    grid,values,_=segments[0]
    np.testing.assert_array_equal(grid,times[10:41])
    assert times[25] in grid and report["dropped_frames"]==0
    np.testing.assert_allclose(values["observation.action"],.8)

@pytest.mark.parametrize("topic", ["/tj/joint_states","/info/gripper_feedback_L","/tj/info/wrench_right","video"])
def test_strict_commands_do_not_hide_observation_gaps(topic):
    times,signals,ranges,config=fixture()
    vt=times
    select=np.r_[0:10,25:51]
    if topic=="video":
        vt=times[select]
    else:
        s=signals[topic]
        signals[topic]=Signal(s.times[select],s.values[select])
    with pytest.raises(ValueError,match="Missing .*observations"):
        align(signals,ranges,vt,config)

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
    monkeypatch.setattr(reader, "messages", lambda _, topics=None: iter(rows))
    return tmp_path



@pytest.mark.parametrize("metadata_declared",[False,True])
@pytest.mark.parametrize("topic",list(COMMAND_STATE))
def test_reader_requires_every_command_with_or_without_metadata_entry(tmp_path,monkeypatch,metadata_declared,topic):
    bag=fake_recording(tmp_path,monkeypatch,empty_metadata=[topic] if metadata_declared else (),omit=[topic])
    with pytest.raises(reader.RequiredSignalMissing) as caught:
        reader.read_numeric(bag)
    issue=numeric_error(caught.value)
    assert issue["code"]=="required_command_missing" and issue["topic"]==topic

@pytest.mark.parametrize("topic",["/tj/joint_states","/info/gripper_feedback_R","/tj/info/eef_left"])
def test_reader_still_rejects_absent_observations(tmp_path,monkeypatch,topic):
    bag=fake_recording(tmp_path,monkeypatch,omit=[topic])
    with pytest.raises(ValueError):
        reader.read_numeric(bag)

def test_empty_optional_topics_do_not_invalidate_numeric_data(tmp_path,monkeypatch):
    bag=fake_recording(tmp_path,monkeypatch,empty_metadata=["/tj/replay/gripperValueR","/tj/dagger/events","/tj/control/input_mode"])
    signals,_,_,_=reader.read_numeric(bag)
    assert all(len(signals[k].times) for k in COMMAND_STATE)
    issues=auxiliary_issues(bag)
    assert sum(i["code"]=="optional_topic_empty" for i in issues)==3
    assert any(i["code"]=="control_mode_snapshot_missing" and i["severity"]=="warning" for i in issues)
    assert all(i["severity"]!="error" for i in issues)

def test_optional_bad_schema_or_clock_cannot_reject_required_features(tmp_path,monkeypatch):
    bag=fake_recording(tmp_path,monkeypatch)
    rows=list(reader.messages(()))
    rows.append((NS(name="unknown/custom"),NS(topic="/tj/dagger/events"),NS(log_time=T0),
                 NS(header=NS(stamp=NS(sec=0,nanosec=0)))))
    requested=[]
    def messages(paths,topics=None):
        requested.append(topics)
        return iter(rows)
    monkeypatch.setattr(reader,"messages",messages)
    reader.read_numeric(bag)
    from tianji_provider.dagger import CONTROL_TOPICS
    assert set(requested[0])==set(reader.TYPES)|set(CONTROL_TOPICS)

@pytest.mark.parametrize("positions",[[float("nan")]*7,[0]*6])
def test_invalid_recorded_commands_are_rejected(tmp_path,monkeypatch,positions):
    bag=fake_recording(tmp_path,monkeypatch)
    rows=list(reader.messages(()))
    for _,channel,_,msg in rows:
        if channel.topic=="/tj/control/joint_cmd_A":
            msg.positions=positions
    monkeypatch.setattr(reader,"messages",lambda _,topics=None:iter(rows))
    with pytest.raises(ValueError):
        reader.read_numeric(bag)

@pytest.mark.parametrize("version",["tianji-thor-v2","tianji-thor-v4","tianji-thor-v5","tianji-thor-v6"])
def test_feedback_substitution_caches_cannot_be_reused(tmp_path,version):
    (tmp_path/"tianji-cache.json").write_text(json.dumps({"format":version}))
    with pytest.raises(ValueError,match="rebuild"):
        TianjiSource(tmp_path,TianjiConfig(task="test")).metadata


@pytest.mark.parametrize("topic", JOINT_COMMAND_TOPICS)
def test_joint_targets_survive_13_second_gap_and_recording_tail(topic):
    times, signals, ranges, config = fixture(751)
    command = signal(times[[0, 650]], 7, .9)
    command.values[1] = .7
    signals[topic] = command
    # Historical API/CLI age settings must not re-enable joint expiry.
    config = replace(config, command_max_age_ms=.001)
    segments, report = align(signals, ranges, times, config)
    grid, values, _ = segments[0]
    np.testing.assert_array_equal(grid, times)
    offset = 0 if topic.endswith("A") else 8
    np.testing.assert_allclose(values["observation.action"][:650,offset:offset+7], .9)
    np.testing.assert_allclose(values["observation.action"][650:,offset:offset+7], .7)
    assert report["command_max_age_ms_by_topic"][topic] is None
    assert report["command_max_age_ms_by_topic"]["/control/gripperValueL"] == 150
    assert timing_issues(signals, ranges, times, config, report) == []


def test_unlimited_joint_hold_does_not_use_future_commands_or_fill_empty_streams():
    grid = T0 + np.array([0,20,120,20_000],dtype=np.int64)*1_000_000
    command = signal([grid[1]], 7, .9)
    indices, valid = command_samples(command, grid, None)
    np.testing.assert_array_equal(valid, [False,True,True,True])
    np.testing.assert_array_equal(indices, [-1,0,0,0])
    with pytest.raises(ValueError, match="requires matching aligned feedback"):
        command_targets(command, grid, None, "/tj/control/joint_cmd_A")
    _, empty_valid = command_samples(signal([],7), grid, None)
    assert not empty_valid.any()


@pytest.mark.parametrize("topic", JOINT_COMMAND_TOPICS)
@pytest.mark.parametrize("first_ms", [200,2000])
def test_joint_prefix_uses_same_frame_feedback_then_real_commands(topic, first_ms):
    times, signals, ranges, config = fixture()
    config = replace(config, state_resampling="linear")
    # Different values at each time and joint detect wrong arm, frozen pose,
    # future-command backfill, and an off-by-one at the first command.
    signals["/tj/joint_states"].values += np.arange(51)[:,None]*.001 + np.arange(14)[None,:]*.01
    signals[topic] = signal([T0+first_ms*1_000_000], 7, .9)
    ranges[topic] = [int(signals[topic].times[0]),int(signals[topic].times[0]),1]
    segments, report = align(signals, ranges, times, config)
    grid, values, _ = segments[0]
    np.testing.assert_array_equal(grid, times)
    offset = 0 if topic.endswith("A") else 8
    prefix = grid < signals[topic].times[0]
    np.testing.assert_array_equal(values["observation.action"][prefix,offset:offset+7],
                                  values["observation.state"][prefix,offset:offset+7])
    np.testing.assert_allclose(values["observation.action"][~prefix,offset:offset+7], .9)
    audit = report["joint_prefix_initialization"][topic]
    assert audit["frames"] == int(prefix.sum())
    assert audit["first_command_ns"] == int(signals[topic].times[0])
    assert report["dropped_frames"] == 0
    issue, = timing_issues(signals, ranges, times, config, report)
    assert issue["code"] == "joint_command_prefix_initialized"
    assert issue["severity"] == "info" and issue["affected_frames"] == int(prefix.sum())
    assert issue["scope"] == "retained_output"


@pytest.mark.parametrize("topic", ["/control/gripperValueL", "/control/gripperValueR"])
def test_gripper_age_limit_remains_inclusive_150ms(topic):
    times, signals, ranges, config = fixture()
    grid = T0 + np.array([150,151], dtype=np.int64)*1_000_000
    from tianji_provider.alignment import command_age_ns
    age = command_age_ns(topic, config)
    assert age == 150_000_000
    _, valid = command_samples(signal([T0],1,.9), grid, age)
    np.testing.assert_array_equal(valid, [True,False])
