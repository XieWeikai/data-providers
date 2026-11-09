"""Time one fresh conversion; fingerprint the resulting values outside the timed region."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from time import perf_counter

import av
import numpy as np
from tianji_provider.alignment import align
from tianji_provider.fused import check_and_prepare
from tianji_provider.provider import TianjiConfig
from tianji_provider.reader import discover, read_numeric, read_video_times, video_files
from tianji_provider.source import TianjiSource

def digest(payloads):
    h = hashlib.sha256()
    for data in payloads:
        h.update(data)
    return h.hexdigest()

def signature(array):
    a = np.ascontiguousarray(array)
    return [str(a.dtype), list(a.shape), digest((a.tobytes(),))]

def source_signature(src):
    episodes=[]
    for ep in src.episodes:
        a = src.audit(ep)
        table=src.read_episode(ep)
        cols={}
        for name in table.column_names:
            col=table[name].combine_chunks()
            cols[name]=[str(col.type),digest(b.to_pybytes() for b in col.buffers() if b is not None)]
        episodes.append(dict(length=ep.length, columns=cols,
            audits={k:signature(v) for k,v in a.items() if isinstance(v,np.ndarray)},
            stats=ep.stats))
    return episodes

def run(root, work, case, details, repetitions):
    cfg=TianjiConfig(task='Generated fixture', processing_mode='dagger' if case.startswith('dagger-') else 'standard',
                     dagger_export='expert_segments' if case=='dagger-expert' else 'annotate')
    bags=discover(root)
    if case=='alignment':
        inputs=[]
        for bag in bags:
            signals,ranges,*_=read_numeric(bag)
            inputs.append((signals,ranges,read_video_times(video_files(bag)),cfg))
        t=perf_counter()
        for _ in range(repetitions):
            aligned=[align(*args)[0] for args in inputs]
        duration=perf_counter()-t
        frames=sum(len(grid) for recording in aligned for grid,_,_ in recording)*repetitions
        evidence=[[dict(time=signature(grid),video=signature(indices),values={k:signature(v) for k,v in columns.items()}) for grid,columns,indices in recording] for recording in aligned] if details else None
        count=len(bags)*repetitions
    elif case in ('direct','dagger-annotate','dagger-expert'):
        t=perf_counter()
        src=TianjiSource(root,cfg,_workers=1)
        frames=sum(len(src.read_episode(ep)) for ep in src.episodes)
        duration=perf_counter()-t
        evidence=source_signature(src) if details else None
        count=len(bags)
    else:
        with tempfile.TemporaryDirectory(dir=work) as directory:
            out=Path(directory)/'prepared'
            t=perf_counter()
            result=check_and_prepare(root,out,cfg,workers=1,video_encoder='libx264')
            duration=perf_counter()-t
            if result['manifest'] is None: raise ValueError(result['report'])
            src=TianjiSource(out,cfg)
            frames=sum(ep.length for ep in src.episodes)
            evidence=None
            if details:
                videos={}
                for p in sorted(out.rglob('*.mp4')):
                    with av.open(str(p)) as container:
                        videos[str(p.relative_to(out))]=digest(f.to_ndarray(format='rgb24').tobytes() for f in container.decode(video=0))
                assert len(videos)==4*len(bags), videos
                evidence={'episodes':source_signature(src),'videos':videos,'reports':src.reports}
            count=len(bags)
    return {'case':case,'frames':frames,'recordings':count,'seconds':duration,'evidence':evidence}

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('source',type=Path);p.add_argument('work',type=Path);p.add_argument('output',type=Path)
    p.add_argument('case',choices=['alignment','direct','fused','dagger-annotate','dagger-expert'])
    p.add_argument('--details',action='store_true');p.add_argument('--repetitions',type=int,default=10)
    a=p.parse_args()
    a.output.write_text(json.dumps(run(a.source.resolve(),a.work,a.case,a.details,a.repetitions),sort_keys=True,allow_nan=False)+'\n')
