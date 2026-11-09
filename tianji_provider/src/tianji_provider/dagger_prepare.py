"""Checked multi-episode DAgger preparation; one numeric/video read per bag."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from itertools import chain
import json
from pathlib import Path
import time

import numpy as np
import pyarrow.parquet as pq

from . import __version__
from .alignment import WRENCH_TOPICS, align, output_grids, previous_indices
from .dagger import analyze
from .diagnostics import REPORT_FORMAT, _issue, auxiliary_issues, discover_for_check, numeric_error, timing_issues
from .encoding import encoding_workers, validate_encoder_request, with_encoder_recovery
from .execution import iter_parallel, worker_count
from .fused import _VideoInspection, _select_batch_encoder, _snapshot, _work_hint
from .media import CAMERAS
from .prepare import VideoEpisode, VideoJob, _encode_group, fingerprint
from .reader import read_numeric, read_video_times, video_files
from .source import FORMAT, arrow_table, features, statistics


def _recording(job):
    original,index,destination,config,encoder=job
    bag=original.resolve(); started=time.monotonic()
    issues=auxiliary_issues(bag); stages=[]; alignment=analysis=signals=paths=vt=None
    media={'full_decode':True,'decoded_frames':None,'quad_size':None}
    payloads=[]; encoding=None
    try:
        signals,ranges,clocks,calibration=read_numeric(bag)
        stages.append('numeric_schema_values_clocks_calibration')
        analysis=analyze(signals,config)
        stages.append('dagger_segmentation_and_annotation')
    except Exception as error: issues.append(numeric_error(error))
    try:
        paths=video_files(bag);vt=read_video_times(paths)
        media['message_count']=len(vt);stages.append('video_schema_and_timestamps')
    except Exception as error: issues.append(_issue('video_read_error','error',str(error)))
    if signals is not None and vt is not None:
        try:
            segments,alignment=align(signals,ranges,vt,config)
            issues.extend(timing_issues(signals,ranges,vt,config,alignment))
            stages.append('alignment_strict_commands_and_stationary_trim')
            for segment_index,(grid,values,indices) in enumerate(segments):
                detail=alignment.get('segment_alignments',[alignment]*len(segments))[segment_index]
                _,windows=output_grids(detail['intersection_start_ns'],detail['intersection_end_ns'],config)
                first=detail['wrench_sampling']['first_window_index'];windows=windows[first:first+len(grid)]
                audit={'absolute_time_ns':grid,'video_packet_index':indices,'video_time_ns':vt[indices],
                       'wrench_time_ns':windows,'wrench_sample_time_ns':np.stack([
                           signals[k].times[previous_indices(signals[k].times,windows.ravel())].reshape(windows.shape)
                           for k in WRENCH_TOPICS],axis=2)}
                payloads.append({'values':values,'audit':audit,'length':len(grid),
                                 'local_index':index*100000+segment_index,
                                 'source_segment_index':segment_index})
        except Exception as error: issues.append(_issue('alignment_error','error',str(error)))
    if any(i['severity']=='error' for i in issues): payloads=[]
    if paths is not None:
        inspection=_VideoInspection(len(vt) if vt is not None else None)
        with closing(inspection.frames(paths)) as frames:
            try:
                if not payloads:
                    for _ in frames: pass
                else:
                    first_frame=next(frames);w,h=inspection.size;episodes=[]
                    for payload in payloads:
                        folder=destination/f"recording_{index:06d}_segment_{payload['source_segment_index']:06d}"
                        folder.mkdir()
                        videos={f'observation.image.{c}':folder/f'{c}.mp4' for c in CAMERAS}
                        payload.update(videos=videos,size=(w//2,h//2))
                        episodes.append(VideoEpisode(payload['local_index'],payload['length'],payload['audit']['video_packet_index'],videos))
                    encoding,image_stats=_encode_group(VideoJob(bag.name,paths,w//2,h//2,config.fps,tuple(episodes),encoder),
                                                        decoder=lambda _:chain((first_frame,),frames))
                    for payload in payloads: payload['image_stats']=image_stats[payload['local_index']]
                if inspection.complete:
                    media.update(decoded_frames=inspection.count,quad_size=list(inspection.size))
                    stages.append('complete_video_decode_frame_mapping_and_dimensions')
            except Exception:
                if inspection.error is None: raise
        if inspection.error is not None:
            issues.append(_issue('video_decode_error','error',str(inspection.error)))
    valid=not any(i['severity']=='error' for i in issues)
    if alignment is not None:
        alignment.update(bag=str(bag),timestamp_sources=clocks,gripper_calibration=calibration)
    result={'bag':bag.name,'path':str(bag),'valid':valid,'frames':sum(p['length'] for p in payloads) if valid else 0,
            'issues':issues,'completed_checks':stages,'video':media,'alignment':alignment,
            'dagger_analysis':analysis,'seconds':time.monotonic()-started}
    return result,payloads if valid else [],encoding


def check_and_prepare(root,destination,config,*,workers=None,progress=None,video_encoder='auto',prepare_valid_only=False):
    return with_encoder_recovery(destination,video_encoder,lambda:_snapshot(Path(root).expanduser().resolve()),
        lambda codec,recovery:_prepare(root,destination,config,workers,progress,codec,prepare_valid_only,recovery))


def _prepare(root,destination,config,workers,progress,video_encoder,prepare_valid_only,recovery):
    root=Path(root).expanduser().resolve();destination=Path(destination).expanduser().resolve()
    worker_count(workers,0);validate_encoder_request(video_encoder)
    if type(prepare_valid_only) is not bool: raise ValueError('Explicit valid-only selection required')
    bags=discover_for_check(root)
    if destination.exists(): raise FileExistsError(str(destination))
    if destination==root or destination.is_relative_to(root) or any(destination.is_relative_to(b.resolve()) for b in bags):
        raise ValueError('Cache must be outside source recordings')
    started=time.monotonic();before=_snapshot(root)
    encoder,selection=_select_batch_encoder(video_encoder,bags,config.fps)
    if recovery: selection.update(recovery)
    parallel=encoding_workers(workers,len(bags),encoder);destination.mkdir(parents=True)
    jobs=[(b,i,destination,config,encoder) for i,b in enumerate(bags)]
    schedule=sorted(range(len(jobs)),key=lambda i:_work_hint(bags[i]),reverse=True)
    ready={};results=[];payloads=[];encodings=[];size=None
    with closing(iter_parallel(_recording,[jobs[i] for i in schedule],workers=parallel)) as complete:
        for scheduled,item in complete:
            ready[schedule[scheduled]]=item
            while len(results) in ready:
                result,payload,encoding=ready.pop(len(results))
                quad=result['video']['quad_size']
                if quad is not None:
                    if size is None:size=quad
                    elif quad!=size:
                        result['issues'].append(_issue('inconsistent_video_dimensions','error','Video dimensions differ'))
                        result.update(valid=False,frames=0);payload=[]
                results.append(result);payloads.append(payload);encodings.append(encoding)
                if progress:progress(len(results),len(bags),result)
    count=sum(r['valid'] for r in results)
    report={'format':REPORT_FORMAT,'provider_version':__version__,'root':str(root),'config':asdict(config),
            'deep_video':True,'valid':count==len(results),'recordings':len(results),'valid_recordings':count,
            'invalid_recordings':len(results)-count,'valid_frames':sum(r['frames'] for r in results),
            'results':results,'seconds':time.monotonic()-started}
    if before!=_snapshot(root):raise ValueError('Raw data changed during DAgger preparation')
    mapping={'mode':'explicit_valid_only' if prepare_valid_only else 'all_selected_must_pass',
             'recordings_checked':len(results),'valid_recordings':count,'invalid_recordings':len(results)-count,
             'rejected':[{'source_recording_index':i,'bag':r['bag'],'issues':r['issues']} for i,r in enumerate(results) if not r['valid']],
             'episode_mapping':[]}
    if not count or not report['valid'] and not prepare_valid_only:return {'report':report,'manifest':None,'selection':mapping}
    analyses={r['dagger_analysis']['analysis_key']:r['dagger_analysis'] for r in results if r['dagger_analysis']}
    if any(r['analysis_key'] not in analyses for r in json.loads(config.dagger_reviews_json)):
        raise ValueError('Review analysis changed or is outside this selection; run a fresh check')
    entries=[];reports=[];offset=0
    for original_index,(result,parts) in enumerate(zip(results,payloads)):
        if not result['valid']:continue
        alignment=deepcopy(result['alignment']);alignment['episode_indices']=[]
        for payload in parts:
            index=len(entries);length=payload['length'];values=payload['values'];folder=next(iter(payload['videos'].values())).parent
            values.update(timestamp=np.arange(length,dtype=np.float32)/config.fps,frame_index=np.arange(length,dtype=np.int64),
                          episode_index=np.full(length,index,dtype=np.int64),index=np.arange(offset,offset+length,dtype=np.int64),
                          task_index=np.zeros(length,dtype=np.int64))
            pq.write_table(arrow_table(values),folder/'data.parquet');np.savez(folder/'alignment.npz',**payload['audit'])
            entries.append({'index':index,'length':length,'stats':{**{k:statistics(v) for k,v in values.items()},**payload['image_stats']},
                'data':str((folder/'data.parquet').relative_to(destination)), 'audit':str((folder/'alignment.npz').relative_to(destination)),
                'videos':{k:str(v.relative_to(destination)) for k,v in payload['videos'].items()}})
            alignment['episode_indices'].append(index)
            grid=payload['audit']['absolute_time_ns']
            mapping['episode_mapping'].append({'source_recording_index':original_index,'bag':result['bag'],'episode_index':index,
                'source_segment_index':payload['source_segment_index'],'start_ns':int(grid[0]),'end_ns':int(grid[-1]),'frames':length})
            offset+=length
        reports.append(alignment)
    options=asdict(config);options.pop('task')
    manifest={'format':FORMAT,'alignment_config':options,'source_fingerprint':[x for i,b in enumerate(bags) if results[i]['valid'] for x in fingerprint(b)],
        'features':features(size[0]//2,size[1]//2,config.fps,'h264',wrench_fps=config.wrench_fps,dagger=True),
        'episodes':entries,'reports':reports,'source_selection':mapping,'video_encoding':encoder.metadata(),
        'video_encoder_selection':selection,'timings':{'checked_prepare_seconds':time.monotonic()-started,'video_workers':parallel,'recordings':encodings},
        'image_statistics':'RGB histograms of retained raw frames before encoding', 'dagger_version':analyses[next(iter(analyses))]['version']}
    (destination/'dagger_analysis.json').write_text(json.dumps(list(analyses.values()),ensure_ascii=False))
    (destination/'dagger_review.json').write_text(config.dagger_reviews_json)
    (destination/'segment_manifest.json').write_text(json.dumps(mapping,ensure_ascii=False))
    if before!=_snapshot(root):raise ValueError('Raw data changed before cache publication')
    (destination/'tianji-cache.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'report':report,'manifest':manifest,'selection':mapping}
