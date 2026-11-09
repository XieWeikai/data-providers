"""Run identical on-disk recordings with separate installed provider packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

def main():
    parser=argparse.ArgumentParser()
    for name in ('legacy_python','refactor_python','source','output'):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir()
    source=args.source.resolve()
    def fingerprint():
        return {str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.rglob('*')) if p.is_file()}
    expected=fingerprint()
    py={'before':args.legacy_python.absolute(),'after':args.refactor_python.absolute()}
    measure=Path(__file__).with_name('measure.py').resolve()
    env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
    env.pop('PYTHONPATH',None)
    def run(side,case,name,details=False):
        file=root/(name+'.json')
        cmd=[str(py[side]),str(measure),str(source),str(root),str(file),case]
        if details:cmd.append('--details')
        subprocess.run(cmd,env=env,check=True)
        return json.loads(file.read_text())
    for case in ('alignment','direct','fused','dagger-annotate','dagger-expert'):
        before=run('before',case,'check-before-'+case,True)
        after=run('after',case,'check-after-'+case,True)
        if before['evidence']!=after['evidence']:
            raise ValueError(f'{case} outputs differ, inspect retained evidence before timing')
        assert before['frames']==after['frames']>0
        print('verified',case,before['frames'],flush=True)
    runs=[]
    for i in range(4):
        for case in ('alignment','direct','fused'):
            order=['before','after'] if i%2==0 else ['after','before']
            record={'block':i,'case':case,'order':order}
            for side in order:
                r=run(side,case,f'{i}-{case}-{side}')
                record[side]={k:v for k,v in r.items() if k!='evidence'}
            runs.append(record)
            (root/'paired.json').write_text(json.dumps(runs,indent=2)+'\n')
            print(i,case,[(side,round(record[side]['seconds'],3)) for side in order],flush=True)
    assert expected==fingerprint()
    (root/'input-sha256.json').write_text(json.dumps(expected,indent=2)+'\n')
    summary={}
    for case in ('alignment','direct','fused'):
        group=[r for r in runs if r['case']==case]
        summary[case]={side:{unit:statistics.median(r[side][field]/r[side]['seconds'] for r in group)
                       for unit,field in [('frames_per_second','frames'),('recordings_per_second','recordings')]} for side in ('before','after')}
        ratios=[r['before']['seconds']/r['after']['seconds'] for r in group]
        summary[case]['paired_throughput_ratio']=ratios
        summary[case]['median_change_pct']=100*(statistics.median(ratios)-1)
    (root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)
if __name__=='__main__': main()
