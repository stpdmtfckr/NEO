"""Read-only resource sampling of this extracted build's running Windows process.

No audio, screenshot, file operation, account request or synthetic user action is
performed. This is NOT an end-to-end command benchmark or a GPU measurement.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def distribution(values: list[float]) -> dict:
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return {'samples':0, 'mean':None, 'p95':None, 'peak':None}
    return {'samples':len(values), 'mean':round(statistics.mean(values),3),
            'p95':round(values[max(0,math.ceil(.95*len(values))-1)],3), 'peak':round(max(values),3)}


def matching_root(process, expected_app: Path) -> bool:
    """Require an exact app.py argument from THIS build, not just pythonw.exe."""
    try:
        args = process.cmdline()
        return any(Path(arg).is_absolute() and Path(arg).resolve() == expected_app.resolve()
                   for arg in args[1:] if arg and not arg.startswith('-'))
    except (OSError, ValueError, RuntimeError):
        return False


def find_root(psutil, pid: int | None):
    candidates = [psutil.Process(pid)] if pid else list(psutil.process_iter())
    found=[]
    for process in candidates:
        try:
            if matching_root(process,ROOT/'app.py'):found.append(process)
        except psutil.Error:
            continue
    if len(found)!=1:
        raise ValueError('Start NEO from this extracted folder first; exactly one app.py process is required.')
    return found[0]


def sample_tree(root, psutil, seconds: int, interval: float = 1) -> dict:
    baseline_time=time.monotonic(); deadline=baseline_time+seconds
    old={}; rows=[]; root_identity=(root.pid,root.create_time()); unavailable=0; ended=False
    while time.monotonic()<deadline:
        now=time.monotonic()
        try:
            if not root.is_running() or root.create_time()!=root_identity[1]:
                ended=True;break
            processes=[root,*root.children(recursive=True)]
        except psutil.Error:
            ended=True;break
        current={}; rss=0; cpu=0.; observed=0
        for process in processes:
            try:
                identity=(process.pid,process.create_time())
                with process.oneshot():
                    times=process.cpu_times(); used=float(times.user+times.system)
                    rss+=process.memory_info().rss
                current[identity]=(now,used)
                if identity in old:
                    elapsed=now-old[identity][0]
                    if elapsed>0:cpu+=100*max(0,used-old[identity][1])/elapsed
                observed+=1
            except psutil.Error:
                unavailable+=1
        if old:
            rows.append({'elapsed_seconds':round(now-baseline_time,3),'processes':observed,
                         'rss_sum_mib':round(rss/1024/1024,3), 'cpu_one_core_percent':round(cpu,3),
                         'cpu_machine_percent':round(cpu/max(1,os.cpu_count() or 1),3)})
        old=current
        time.sleep(min(interval,max(0,deadline-time.monotonic())))
    return {'root_pid':root_identity[0], 'root_create_time':root_identity[1],
            'requested_seconds':seconds,'observed_seconds':round(time.monotonic()-baseline_time,3),
            'root_ended_early':ended,'unreadable_process_samples':unavailable,
            'rss_sum_mib':distribution([r['rss_sum_mib'] for r in rows]),
            'cpu_one_core_percent':distribution([r['cpu_one_core_percent'] for r in rows]),
            'cpu_machine_percent':distribution([r['cpu_machine_percent'] for r in rows]),'samples':rows}


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--scenario',choices=['idle','music','game','manual'],default='manual')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    if not 10<=args.seconds<=3600:parser.error('--seconds must be 10..3600')
    if os.name!='nt':
        print('This sampler requires the real Windows app. No Windows result was fabricated.')
        return 2
    import psutil
    try:root=find_root(psutil,args.pid)
    except (ValueError,psutil.Error) as exc:
        print(str(exc));return 2
    print(f'Sampling NEO PID {root.pid} for {args.seconds} seconds. Keep the chosen workload unchanged. Ctrl+C stops.')
    try:result=sample_tree(root,psutil,args.seconds)
    except KeyboardInterrupt:
        print('Sampling cancelled; no complete benchmark has been recorded.');return 2
    result.update(schema_version=1,scenario=args.scenario, scenario_source='user-selected, not inferred',
                  build=(ROOT/'VERSION.txt').read_text(encoding='utf-8').splitlines()[0],
                  generated_at=datetime.now().astimezone().isoformat(), logical_cpus=os.cpu_count(),
                  total_memory_bytes=psutil.virtual_memory().total,
                  scope='Observed root and descendants only; detached Ollama or other servers are not included',
                  rss_note='Sum of per-process RSS; shared pages can be counted more than once',
                  cpu_note='One-core percent may exceed 100; machine percent divides by logical CPU count',
                  unmeasured=['wake latency','ASR','command correctness','command end-to-end latency','GPU memory','game frametime'])
    base=Path(os.environ['LOCALAPPDATA'])/'NeoJarvis/diagnostics'
    target=args.output or base/('windows_resources_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Never overwrite an existing report or unrelated file.
    with target.open('x',encoding='utf-8') as output:json.dump(result,output,ensure_ascii=False,indent=2)
    print('Report:',target)
    return 2 if result['root_ended_early'] or not result['samples'] else 0


if __name__=='__main__':raise SystemExit(main())
