from dataclasses import replace
import json
import numpy as np
import pytest
from tianji_provider.dagger import source_arrays, annotations, CONTROL_TOPICS, EXPERT, UNKNOWN, SWITCHING
from tianji_provider.reader import NumericSignals
from test_command_coverage import fixture, T0
from tianji_provider.alignment import align


def record(ms,topic,value):
    return dict(time_ns=T0+ms*1000000,record_ns=T0+ms*1000000,topic=topic,value=value,valid=True)


def status(mode=1,enabled=True):
    return dict(source='teleop' if mode==1 else 'user',arm_mode=mode,gripper_mode=mode,
                output_enabled=enabled,switching=False,fault='')


def test_source_heartbeat_expiry_and_switch_event_never_refreshes_output():
    evidence={'records':[record(0,CONTROL_TOPICS[0],status()),
        record(20,CONTROL_TOPICS[1],{'event':'switch_requested','target':3}),
        record(40,CONTROL_TOPICS[1],{'event':'switch_completed','target':3})]}
    codes,epochs=source_arrays(evidence,T0+np.array([0,20,40,1501])*1000000,1500)
    np.testing.assert_array_equal(codes[:,0],[EXPERT,SWITCHING,1,UNKNOWN])
    assert epochs[2,0]==T0+40000000


def test_unknown_before_first_heartbeat_with_real_epoch_time():
    codes,_=source_arrays({'records':[]},np.array([T0,T0+10000000],dtype=np.int64),1500)
    assert np.all(codes==UNKNOWN)


def test_new_source_cannot_reuse_previous_target_or_feedback_prefix():
    times,s,r,c=fixture();s=NumericSignals(s)
    s.dagger={'records':[record(0,CONTROL_TOPICS[0],status(3)),record(110,CONTROL_TOPICS[0],status())]}
    s['/tj/control/joint_cmd_A'].times=times[::10]
    s['/tj/control/joint_cmd_A'].values=s['/tj/control/joint_cmd_A'].values[::10]
    analysis={'analysis_key':'key','candidates':[]}
    marks=annotations(s,times,replace(c,processing_mode='dagger'),analysis)
    assert not marks['dagger.action_valid'][6,0]
    assert marks['dagger.action_valid'][10,0]
    assert marks['dagger.actual_command'][6,0]


def test_expert_export_splits_invalid_boundaries_and_preserves_raw_time():
    times,s,r,c=fixture(151);s=NumericSignals(s)
    s.dagger={'fingerprint':'test','origin_ns':T0,'velocity':np.zeros((151,14)),
        'records':[record(0,CONTROL_TOPICS[0],status()),record(800,CONTROL_TOPICS[0],status(3)),
                   record(1600,CONTROL_TOPICS[0],status())]}
    cfg=replace(c,processing_mode='dagger',dagger_export='expert_segments',dagger_status_ttl_ms=10000)
    segments,report=align(s,r,times,cfg)
    assert len(segments)==2
    assert segments[0][0][-1]<T0+800000000
    assert segments[1][0][0]>=T0+1600000000
    for grid,values,_ in segments:
        assert values['dagger.supervision_valid'].all()
        np.testing.assert_array_equal(values['dagger.original_time_ns'][:,0],grid)
    assert report['dagger_training_contract'].startswith('physically')


def test_review_excludes_full_command_evidence_not_just_motion_interval():
    times,s,r,c=fixture();s=NumericSignals(s)
    s.dagger={'records':[record(0,CONTROL_TOPICS[0],status())]}
    a={'analysis_key':'k','candidates':[dict(id='c',arm='A',start_ns=times[20],end_ns=times[25],
        reason='command_gap_and_hold',command_gaps=[dict(start_ns=times[15],end_ns=times[30])])]}
    cfg=replace(c,processing_mode='dagger',dagger_reviews_json=json.dumps([dict(analysis_key='k',candidate_id='c',decision='abnormal')]))
    marks=annotations(s,times,cfg,a)
    np.testing.assert_array_equal(np.flatnonzero(marks['dagger.excluded'][:,0]),np.arange(15,30))
    assert not marks['dagger.supervision_valid'][15:30].any()
    with pytest.raises(ValueError,match='unknown candidate'):
        annotations(s,times,replace(cfg,dagger_reviews_json=cfg.dagger_reviews_json.replace('"c"','"bad"')),a)


def test_stale_gripper_in_unselected_policy_does_not_reject_expert_export():
    times,s,r,c=fixture(151);s=NumericSignals(s)
    s.dagger={'fingerprint':'test','origin_ns':T0,'velocity':np.zeros((151,14)),
        'records':[record(0,CONTROL_TOPICS[0],status()),record(500,CONTROL_TOPICS[0],status(3)),record(1600,CONTROL_TOPICS[0],status())]}
    for topic in ('/control/gripperValueL','/control/gripperValueR'):
        keep=np.r_[0:30,80:151]
        s[topic].times=times[keep];s[topic].values=s[topic].values[keep]
    cfg=replace(c,processing_mode='dagger',dagger_export='expert_segments',dagger_status_ttl_ms=10000)
    segments,report=align(s,r,times,cfg)
    assert len(segments)==2
    from tianji_provider.diagnostics import timing_issues
    assert not any(i['severity']=='error' for i in timing_issues(s,r,times,cfg,report))
    with pytest.raises(ValueError,match='missing/stale'):
        align(s,r,times,replace(c,processing_mode='standard'))
