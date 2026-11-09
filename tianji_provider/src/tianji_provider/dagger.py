"""Versioned DAgger evidence, source attribution and supervision eligibility.

No topic publishing and no writes to recordings. All intervals are half-open
absolute nanoseconds; relative seconds always use rosbag metadata origin.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_closing, median_filter

VERSION = "thor-dagger-v1"
UNKNOWN, POLICY, EXPERT, SWITCHING, DISABLED, FAULT = range(6)
SOURCE_NAMES = ["unknown", "policy", "expert", "switching", "disabled", "fault"]
CONTROL_TOPICS = ("/tj/dagger/status", "/tj/dagger/events", "/tj/control/input_mode",
                  "/tj/control/gripper_input_mode")
COMMANDS = ("/tj/control/joint_cmd_A", "/control/gripperValueL",
            "/tj/control/joint_cmd_B", "/control/gripperValueR")
DEFAULTS = dict(dagger_stop_speed=.01, dagger_velocity_limit=.02, dagger_motion_speed=.035,
                dagger_min_pause_s=.10, dagger_max_pause_s=5., dagger_position_range=.004,
                dagger_feedback_gap_ms=40., dagger_command_gap_ms=150., dagger_hold_range=.001,
                dagger_status_ttl_ms=1500., dagger_exclude="reviewed", dagger_exclusion_region="command",
                dagger_min_segment_frames=2, dagger_reviews_json="[]")


def validate_config(config):
    if config.processing_mode not in ("standard", "dagger"):
        raise ValueError("processing_mode must be standard or dagger")
    if config.dagger_export not in ("annotate", "expert_segments"):
        raise ValueError("dagger_export must be annotate or expert_segments")
    if config.processing_mode != "dagger" and config.dagger_export != "annotate":
        raise ValueError("Expert export requires DAgger mode")
    for key in DEFAULTS:
        value = getattr(config, key)
        if key in ("dagger_exclude", "dagger_exclusion_region", "dagger_reviews_json"): continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not np.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a finite positive number")
    if config.dagger_min_pause_s > config.dagger_max_pause_s:
        raise ValueError("Pause minimum exceeds maximum")
    if config.dagger_exclude not in ("none", "reviewed", "suspected"):
        raise ValueError("Invalid DAgger exclusion policy")
    if config.dagger_exclusion_region not in ("motion", "command"):
        raise ValueError("Invalid exclusion region")
    if type(config.dagger_min_segment_frames) is not int or config.dagger_min_segment_frames < 2:
        raise ValueError("A segment needs at least two frames")
    if not isinstance(config.dagger_reviews_json, str) or len(config.dagger_reviews_json) > 1_000_000:
        raise ValueError("Invalid review payload")
    entries = json.loads(config.dagger_reviews_json)
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("Review payload must be a bounded list")
    keys = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"analysis_key", "candidate_id", "decision"}:
            raise ValueError("Review requires analysis_key, candidate_id, decision")
        if entry['decision'] not in ("normal", "abnormal", "uncertain"):
            raise ValueError("Invalid review decision")
        if any(not isinstance(entry[k], str) or len(entry[k]) > 200 for k in entry):
            raise ValueError("Invalid review field")
        key = (entry['analysis_key'], entry['candidate_id'])
        if key in keys: raise ValueError("Duplicate review decision")
        keys.add(key)


def fingerprint(bag):
    root = Path(bag).resolve()
    items = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.mcap', '.yaml', '.json'):
            s = path.stat()
            items.append([str(path.relative_to(root)), s.st_size, s.st_mtime_ns])
    return hashlib.sha256(json.dumps([str(root), items], separators=(',', ':')).encode()).hexdigest()


def _runs(mask):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    return list(zip(edges[::2], edges[1::2]))


def _speed(q):
    sm = median_filter(q, size=(3, 1), mode='nearest')
    d = np.zeros_like(sm)
    d[2:-2] = (sm[4:] - sm[:-4]) / .08
    d[:2] = d[2]; d[-2:] = d[-3]
    return np.max(np.abs(d), axis=1)


def _state_code(status, channel):
    if status.get('fault'): return FAULT
    if status.get('switching'): return SWITCHING
    if status.get('output_enabled') is not True or status.get('source') == 'inhibited': return DISABLED
    mode = status.get('arm_mode' if channel == 'arm' else 'gripper_mode')
    if status.get('source') == 'user' and mode == 3: return POLICY
    if status.get('source') == 'teleop' and mode == 1: return EXPERT
    return UNKNOWN


def source_arrays(evidence, times, ttl_ms):
    """Resolve event boundaries, bounded status age and independently gated modes.

    Successful completion establishes the new mode but never fabricates a fresh
    output-enable or fault heartbeat. A stale/missing heartbeat remains unknown.
    """
    times = np.asarray(times, dtype=np.int64)
    codes = np.zeros((len(times), 4), dtype=np.int64)
    epochs = np.zeros_like(codes)
    records = sorted(evidence.get('records', []), key=lambda r: (r['time_ns'], r.get('order', 0)))
    cursor = 0; status = {}; last_status = 0
    mode_arm = mode_gripper = None; pending = False
    current = np.zeros(4, dtype=np.int64); starts = np.zeros(4, dtype=np.int64)
    for i, t in enumerate(times):
        while cursor < len(records) and records[cursor]['time_ns'] <= t:
            r = records[cursor]; cursor += 1
            if not r.get('valid', True):
                status = {}; last_status = 0
                continue
            topic, value = r['topic'], r['value']
            if topic == CONTROL_TOPICS[0]:
                status = dict(value); last_status = r['time_ns']
                # A fresh stable snapshot also recovers a missing completion event.
                pending = bool(status.get('switching'))
            elif topic == CONTROL_TOPICS[1]:
                event = value.get('event')
                if event == 'switch_requested': pending = True
                elif event == 'switch_completed':
                    pending = False
                    target = value.get('target', value.get('arm_mode'))
                    status = {**status, 'arm_mode': target, 'gripper_mode': value.get('gripper_mode', target),
                              'source': {1:'teleop', 3:'user'}.get(target, 'unknown'), 'switching': False}
                elif event in ('switch_failed', 'fault'):
                    status = {**status, 'fault': value.get('reason', event)}
            elif topic == CONTROL_TOPICS[2]: mode_arm = value
            elif topic == CONTROL_TOPICS[3]: mode_gripper = value
            # Preserve the exact transition timestamp, even between output samples.
            fresh = r['time_ns'] - last_status <= round(ttl_ms * 1e6)
            arm = _state_code(status, 'arm') if fresh else UNKNOWN
            grip = _state_code(status, 'gripper') if fresh else UNKNOWN
            if pending: arm = grip = SWITCHING
            if arm in (POLICY, EXPERT) and mode_arm not in (None, status.get('arm_mode')): arm = UNKNOWN
            if grip in (POLICY, EXPERT) and mode_gripper not in (None, status.get('gripper_mode')): grip = UNKNOWN
            nxt = np.array([arm, grip, arm, grip])
            changed = nxt != current
            starts[changed] = r['time_ns']; current = nxt
        if t - last_status > round(ttl_ms * 1e6):
            nxt = np.zeros(4, dtype=np.int64)
            changed = nxt != current; starts[changed] = last_status + round(ttl_ms * 1e6)
            current = nxt
        codes[i] = current; epochs[i] = starts
    return codes, epochs


def _source_segments(evidence, start, end, ttl_ms):
    edges = {int(start), int(end)}
    for r in evidence.get('records', []):
        t = r['time_ns']
        if start < t < end: edges.add(t)
        if r['topic'] == CONTROL_TOPICS[0] and start < t + round(ttl_ms * 1e6) < end:
            edges.add(t + round(ttl_ms * 1e6) + 1)
    edges = np.array(sorted(edges), dtype=np.int64)
    codes, _ = source_arrays(evidence, edges[:-1], ttl_ms)
    result = []
    for a, b, code in zip(edges[:-1], edges[1:], codes):
        value = [int(x) for x in code]
        if result and result[-1]['sources'] == value and result[-1]['end_ns'] == int(a):
            result[-1]['end_ns'] = int(b)
        else: result.append({'start_ns':int(a), 'end_ns':int(b), 'sources':value})
    return result


def analyze(signals, config):
    evidence = getattr(signals, 'dagger', {})
    semantic = {k:v for k,v in asdict(config).items() if k.startswith('dagger_') and k not in
                ('dagger_export', 'dagger_reviews_json', 'dagger_exclude', 'dagger_exclusion_region', 'dagger_min_segment_frames')}
    source_hash = evidence.get('fingerprint', 'unavailable')
    key = hashlib.sha256(json.dumps([VERSION, source_hash, semantic], sort_keys=True).encode()).hexdigest()
    cache = getattr(signals, '_dagger_analysis', {})
    if key in cache: return cache[key]
    js = signals['/tj/joint_states']
    origin = int(evidence.get('origin_ns', js.times[0]))
    sec = (js.times - origin) / 1e9
    g = np.arange(np.ceil(sec[0]/.02)*.02, np.floor(sec[-1]/.02)*.02, .02)
    result = {'version':VERSION, 'analysis_key':key, 'source_fingerprint':source_hash,
              'origin_ns':origin, 'bag':evidence.get('bag',''), 'parameters':semantic,
              'source_names':SOURCE_NAMES, 'source_components':['left_arm','left_gripper','right_arm','right_gripper'],
              'issues':list(evidence.get('issues', [])), 'candidates':[], 'command_gaps':[],
              'control_evidence':list(evidence.get('records',[])),
              'feedback_gaps':[], 'source_segments':[], 'trace':[], 'long_stationary':[]}
    end_ns = int(js.times[-1]) + 1
    result['source_segments'] = _source_segments(evidence, int(js.times[0]), end_ns, config.dagger_status_ttl_ms)
    durations = {s:0. for s in SOURCE_NAMES}
    for segment in result['source_segments']:
        durations[SOURCE_NAMES[segment['sources'][0]]] += (segment['end_ns']-segment['start_ns'])/1e9
    result['source_duration_s'] = durations
    result['takeovers'] = sum(s['sources'][0] == EXPERT and (i == 0 or result['source_segments'][i-1]['sources'][0] != EXPERT)
                              for i,s in enumerate(result['source_segments']))
    for arm in ('A','B'):
        topic = '/tj/control/joint_cmd_'+arm
        if topic not in signals: continue
        cs = signals[topic].times
        for i in np.flatnonzero(np.diff(cs) >= round(config.dagger_command_gap_ms*1e6)):
            result['command_gaps'].append({'arm':arm,'start_ns':int(cs[i]),'end_ns':int(cs[i+1]),
                                           'duration_s':float((cs[i+1]-cs[i])/1e9)})
    for i in np.flatnonzero(np.diff(js.times) > round(config.dagger_feedback_gap_ms*1e6)):
        result['feedback_gaps'].append({'start_ns':int(js.times[i]),'end_ns':int(js.times[i+1])})
    if len(g) < 50:
        result['issues'].append('insufficient_motion_context')
        result['summary'] = {'candidates':0,'strong_candidates':0,'merged_events':0}
        return result
    t = origin + np.rint(g*1e9).astype(np.int64)
    q = np.column_stack([np.interp(g,sec,js.values[:,i]) for i in range(14)])
    raw_v = evidence.get('velocity')
    if raw_v is None or np.asarray(raw_v).shape != js.values.shape:
        result['issues'].append('joint_velocity_unavailable')
        measured = np.full((len(g),2), np.inf)
    else:
        v = np.column_stack([np.interp(g,sec,np.asarray(raw_v)[:,i]) for i in range(14)])
        measured = np.column_stack([np.max(np.abs(v[:,:7]),axis=1),np.max(np.abs(v[:,7:]),axis=1)])
    speeds = np.column_stack([_speed(q[:,:7]),_speed(q[:,7:])])
    ix = np.clip(np.searchsorted(sec,g,side='right')-1,0,len(sec)-2)
    gaps = sec[ix+1]-sec[ix]
    valid = gaps <= config.dagger_feedback_gap_ms/1000
    valid = ~np.convolve((~valid).astype(int),np.ones(5,dtype=int),mode='same').astype(bool)
    # Frozen baseline mode evidence is retained for reproducible stop counts;
    # training labels use the stricter bounded state resolver above.
    updates = []; switches = []
    for r in evidence.get('records', []):
        if not r.get('valid',True): continue
        rt = (r['record_ns']-origin)/1e9; value = r['value']
        if r['topic'] == CONTROL_TOPICS[0]:
            code = 3 if value.get('source')=='user' and not value.get('switching') and value.get('output_enabled') else 1 if value.get('source')=='teleop' and not value.get('switching') and value.get('output_enabled') else 0
            updates.append((rt,code))
        elif r['topic'] == CONTROL_TOPICS[1] and value.get('event') in ('switch_requested','switch_completed','arm_mode_observed'):
            switches.append(rt)
            mode = value.get('target',value.get('mode',value.get('arm_mode')))
            updates.append((rt,0 if value.get('event')=='switch_requested' else mode if mode in (1,3) else 0))
    updates.sort(); mode = np.zeros(len(g),dtype=int)
    if updates:
        ut,um = np.array([x[0] for x in updates]),np.array([x[1] for x in updates])
        mi = np.searchsorted(ut,g,side='right')-1
        mode = np.where(mi>=0,um[np.clip(mi,0,len(um)-1)],0)
    command_age = np.full((len(g),2), -1.,dtype=float)
    for ai,arm in enumerate(('A','B')):
        s = speeds[:,ai]; cols = slice(ai*7,(ai+1)*7)
        topic = '/tj/control/joint_cmd_'+arm
        cmd = signals.get(topic)
        target = None
        if cmd is not None and len(cmd.times):
            cs = (cmd.times-origin)/1e9
            ci = np.searchsorted(cs,g,side='right')-1
            target = cmd.values[np.clip(ci,0,len(cs)-1)].copy(); target[ci<0] = np.nan
            command_age[:,ai] = np.where(ci>=0,(g-cs[np.clip(ci,0,len(cs)-1)])*1000,-1)
        low = (s<config.dagger_stop_speed)&(measured[:,ai]<config.dagger_velocity_limit)&valid
        low = binary_closing(low,structure=np.ones(2))&valid
        for a,b in _runs(low):
            start = float(g[a]); end = float(g[b]) if b<len(g) else float(g[-1]+.02)
            duration = end-start
            if duration>config.dagger_max_pause_s:
                result['long_stationary'].append({'arm':arm,'start_ns':int(t[a]),'end_ns':origin+round(end*1e9)})
            if not config.dagger_min_pause_s<=duration<=config.dagger_max_pause_s or a<23 or b+23>=len(g): continue
            pre=(g>=start-.40)&(g<start-.06); post=(g>end+.06)&(g<=end+.40)
            if not valid[pre].all() or not valid[post].all(): continue
            vs0=float(np.percentile(s[pre],70));vs1=float(np.percentile(s[post],70))
            if min(vs0,vs1)<config.dagger_motion_speed: continue
            span=float(np.max(np.ptp(q[a:b,cols],axis=0)))
            if span>config.dagger_position_range: continue
            near=any(start-.5<=x<=end+.5 for x in switches)
            window=(g>=start-.4)&(g<=end+.4)
            stable=float(np.mean(mode[window]==3))>.98 and not near
            cmd_range=float(np.max(np.ptp(target[a:b][np.isfinite(target[a:b]).all(axis=1)],axis=0))) if target is not None and np.isfinite(target[a:b]).all(axis=1).any() else None
            overlap=[x for x in result['command_gaps'] if x['arm']==arm and x['start_ns']<origin+round(end*1e9) and x['end_ns']>int(t[a])]
            reason='command_gap_and_hold' if cmd_range is not None and cmd_range<config.dagger_hold_range and overlap else 'command_hold' if cmd_range is not None and cmd_range<config.dagger_hold_range else 'command_progress_during_stop' if cmd_range is not None and cmd_range>.01 else 'motion_pause_only'
            cid=hashlib.sha256(f'{arm}:{int(t[a])}:{origin+round(end*1e9)}'.encode()).hexdigest()[:20]
            result['candidates'].append({'id':cid,'arm':arm,'start_ns':int(t[a]),'end_ns':origin+round(end*1e9),
                'start_s':start,'end_s':end,'duration_s':duration,'reason':reason,'stable_user':stable,
                'strong':bool(stable and reason=='command_gap_and_hold'),'near_switch':near,
                'pre_speed_rad_s':vs0,'pause_speed_rad_s':float(np.median(s[a:b])),
                'post_speed_rad_s':vs1,'position_range_rad':span,'command_range_rad':cmd_range,
                'feedback_max_gap_ms':float(np.max(gaps[a:b])*1000),
                'command_gaps':overlap,'command_gap_s':max((x['duration_s'] for x in overlap),default=0)})
    merged=[]
    for candidate in sorted(result['candidates'],key=lambda c:c['start_ns']):
        if merged and candidate['start_ns']<merged[-1][1]: merged[-1][1]=max(merged[-1][1],candidate['end_ns'])
        else: merged.append([candidate['start_ns'],candidate['end_ns']])
    result['summary']={'candidates':len(result['candidates']),'strong_candidates':sum(c['strong'] for c in result['candidates']),
                       'merged_events':len(merged),'left_candidates':sum(c['arm']=='A' for c in result['candidates']),
                       'right_candidates':sum(c['arm']=='B' for c in result['candidates'])}
    # 20 ms speed and command-age evidence, bounded for long recordings.
    stride=max(1,int(np.ceil(len(g)/15000)))
    result['trace']=[[round(float(g[i]),5),*map(float,speeds[i]),*map(float,command_age[i])] for i in range(0,len(g),stride)]
    cache[key]=result; signals._dagger_analysis=cache
    return result


def annotations(signals, times, config, analysis=None):
    analysis = analysis or analyze(signals,config)
    times=np.asarray(times,dtype=np.int64)
    codes,epochs=source_arrays(getattr(signals,'dagger',{}),times,config.dagger_status_ttl_ms)
    valid=codes==EXPERT; actual=np.zeros_like(valid); ages=np.full((len(times),2),-1.,dtype=np.float32)
    for i,topic in enumerate(COMMANDS):
        cmd=signals.get(topic)
        if cmd is None or not len(cmd.times): valid[:,i]=False;continue
        idx=np.searchsorted(cmd.times,times,side='right')-1
        ok=idx>=0; chosen=cmd.times[np.clip(idx,0,len(cmd.times)-1)]
        actual[:,i]=ok
        valid[:,i]&=ok&(chosen>=epochs[:,i])
        if i in (1,3): valid[:,i]&=(times-chosen<=round(config.gripper_max_gap_ms*1e6))
        else: ages[:,i//2]=np.where(ok,(times-chosen)/1e6,-1)
    review_map={(r['analysis_key'],r['candidate_id']):r['decision'] for r in json.loads(config.dagger_reviews_json)}
    known={c['id'] for c in analysis['candidates']}
    if any(r['analysis_key']==analysis['analysis_key'] and r['candidate_id'] not in known for r in json.loads(config.dagger_reviews_json)):
        raise ValueError('Review names an unknown candidate')
    pause=np.zeros((len(times),2),dtype=bool);excluded=np.zeros(len(times),dtype=bool)
    for c in analysis['candidates']:
        pause[:,0 if c['arm']=='A' else 1]|=(times>=c['start_ns'])&(times<c['end_ns'])
        decision=review_map.get((analysis['analysis_key'],c['id']),'uncertain')
        reject=(decision=='abnormal' and config.dagger_exclude!='none') or (config.dagger_exclude=='suspected' and c['reason']=='command_gap_and_hold' and decision!='normal')
        if reject:
            regions=c['command_gaps'] if config.dagger_exclusion_region=='command' and c['command_gaps'] else [c]
            for region in regions: excluded|=(times>=region['start_ns'])&(times<region['end_ns'])
    valid[excluded]=False
    components=np.repeat(valid,[7,1,7,1],axis=1)
    return {'dagger.source':codes,'dagger.actual_command':actual,'dagger.action_valid':components,
            'dagger.supervision_valid':np.all(valid,axis=1)[:,None],
            'dagger.original_time_ns':times[:,None], 'dagger.pause':pause,
            'dagger.command_age_ms':ages,'dagger.excluded':excluded[:,None]}


def feature_schema():
    return {key:{'dtype':dtype,'shape':[size],'names':None} for key,dtype,size in (
        ('dagger.source','int64',4),('dagger.actual_command','bool',4),('dagger.action_valid','bool',16),
        ('dagger.supervision_valid','bool',1),('dagger.original_time_ns','int64',1),
        ('dagger.pause','bool',2),('dagger.command_age_ms','float32',2),('dagger.excluded','bool',1))}


def review_summary(analysis, config):
    reviews=[r for r in json.loads(config.dagger_reviews_json) if r['analysis_key']==analysis['analysis_key']]
    return {'analysis_key':analysis['analysis_key'],'decisions':reviews,
            'export':config.dagger_export,'exclusion':config.dagger_exclude,'exclusion_region':config.dagger_exclusion_region}
