"""Ordering, cache identity and lightweight process payload contracts."""
from dataclasses import asdict
import pickle
import importlib
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from tianji_provider import diagnostics
from tianji_provider.prepare import VideoEpisode, VideoJob
from tianji_provider.provider import TianjiConfig


def test_check_completion_order_does_not_change_size_baseline_or_progress(tmp_path,monkeypatch):
    bags=[tmp_path/f'my_bag-{i}' for i in range(3)]
    monkeypatch.setattr(diagnostics,'discover_for_check',lambda root:bags)
    def parallel(fn,jobs,workers=None):
        assert len(jobs)==3 and workers==2
        for i in [2,1,0]:
            yield i,{'bag':bags[i].name,'valid':True,'frames':10,'issues':[],
                     'video':{'quad_size':[1920,1080] if i!=1 else [1280,720]}}
    monkeypatch.setattr(diagnostics,'iter_parallel',parallel)
    progress=[]
    report=diagnostics.check_dataset(tmp_path,TianjiConfig(task='test'),workers=2,
                                     progress=lambda i,n,r:progress.append((i,n,r['bag'],r['valid'])))
    assert [r['bag'] for r in report['results']]==[p.name for p in bags]
    assert [r['valid'] for r in report['results']]==[True,False,True]
    assert report['valid_recordings']==2 and report['invalid_recordings']==1
    assert report['results'][1]['issues'][0]['code']=='inconsistent_video_dimensions'
    assert progress==[(1,3,bags[0].name,True),(2,3,bags[1].name,False),(3,3,bags[2].name,True)]


def test_workers_are_execution_options_not_semantic_cache_configuration():
    assert 'workers' not in asdict(TianjiConfig(task='test'))


def test_video_job_serializes_only_paths_indices_and_output_descriptors(tmp_path):
    ep=VideoEpisode(7,100,np.arange(100,dtype=np.int64),{'observation.image.head_left':tmp_path/'left.mp4'})
    job=VideoJob('my_bag-x',(tmp_path/'video.mcap',),960,540,50,(ep,))
    payload=pickle.dumps(job)
    assert len(payload)<4096
    restored=pickle.loads(payload)
    assert restored.bag==job.bag and restored.paths==job.paths
    np.testing.assert_array_equal(restored.episodes[0].indices,ep.indices)


def test_failed_video_batch_never_publishes_complete_manifest(tmp_path,monkeypatch):
    prep=importlib.import_module('tianji_provider.prepare')
    raw=tmp_path/'raw';raw.mkdir()
    output=tmp_path/'cache'
    bags=[raw/'my_bag-0',raw/'my_bag-1']
    config=TianjiConfig(task='test')
    media=SimpleNamespace(paths=(tmp_path/'raw.mcap',),width=960,height=540)
    episodes=[SimpleNamespace(index=i,length=2,stats={},data_path=bag,
                              videos={'observation.image.head_left':media}) for i,bag in enumerate(bags)]
    class Source:
        def __init__(self,*args,**kwargs):self.episodes=episodes
        def read_episode(self,ep):return pa.table({'timestamp':np.array([0,.02],dtype=np.float32)})
        def audit(self,ep):
            return {key:np.array([0,1]) for key in ('absolute_time_ns','video_packet_index','video_time_ns',
                                                   'wrench_time_ns','wrench_sample_time_ns')}
    monkeypatch.setattr(prep,'TianjiSource',Source)
    monkeypatch.setattr(prep,'discover',lambda root:bags)
    monkeypatch.setattr(prep,'fingerprint',lambda root:[])
    closed=[]
    def failing(fn,jobs,workers=None):
        try:
            yield 0,({'bag':jobs[0].bag,'frames':2,'video_seconds':1.0},{0:{}})
            raise RuntimeError('Second video failed')
        finally:closed.append(True)
    monkeypatch.setattr(prep,'iter_parallel',failing)
    with pytest.raises(RuntimeError,match='Second video failed'):
        prep.prepare(raw,output,config,workers=2)
    assert closed==[True]
    assert not (output/'tianji-cache.json').exists()
    assert (output/'episode_000000/data.parquet').exists()
    with pytest.raises(FileExistsError,match='incomplete cache'):
        prep.prepare(raw,output,config,workers=1)
