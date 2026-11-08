#!/usr/bin/env python3
"""A small dependency-free host runner for the installed OpenCode integration."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from broker import Broker
import sandbox
from scoring import score, snapshot, write_reports, atomic_text
from suite import catalog, materialize, verify_sources

DEFAULT_OUTPUT=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'agentguard/security-eval'
PRINT_LOCK=threading.Lock()


def emit(message):
    with PRINT_LOCK:
        print(message,flush=True)


def config_and_keys(path,env_path):
    settings=json.loads(Path(path).expanduser().read_text())
    # Literal env values only; host Python need not match the installed runtime.
    allowed={settings[k]['api_key_env'] for k in ('agent','guard')}
    values={k:os.environ[k] for k in allowed if k in os.environ}
    for line in Path(env_path).expanduser().read_text().splitlines():
        line=line.strip().removeprefix('export ')
        key,sep,value=line.partition('=')
        if key not in allowed:
            continue
        parts=shlex.split(value,comments=True)
        if not sep or len(parts)!=1 or '$' in value or '`' in value:
            raise ValueError('模型凭据文件必须使用字面量赋值')
        values.setdefault(key,parts[0])
    keys={k:values[settings[k]['api_key_env']] for k in ('agent','guard')}
    return settings,keys


def lane_settings(original,row,args):
    cfg={k:original[k] for k in ('guard','agent','transforms') if k in original}
    cfg=json.loads(json.dumps(cfg))
    for role in ('agent','guard'):
        cfg[role]['api_key_env']='BENCH_'+role.upper()+'_KEY'
        cfg[role].update(max_concurrency=1,max_retries=0,protocol_retries=0)
    if args.model:
        cfg['agent']['model']=args.model
    if args.thinking:
        cfg['agent']['thinking']=args.thinking
    cfg.update(workspace='/workspace',state_directory='/out/private',tool_factory='bench_tools:tools',
        max_turns=args.max_turns,max_calls=40,max_review_bytes=180000,max_attempts=100,max_tokens=None,
        context_strategy='relations',recent_n=8,recover_on_block=True,synthetic_demo=True,
        network_destinations=[],resources=[],confirmed_authorizations=[row['authorizations']],
        opencode={'allowed_destination_ids':['approved'],'max_recoveries':2,
                  'reasoning_effort':'high' if cfg['agent'].get('thinking')=='enabled' else 'none'})
    from bench_config import target_settings
    cfg.update(target_settings(row))
    if row.get('sensitive_resource'):
        sensitive=json.loads(json.dumps(row['sensitive_resource']))
        sensitive['labels']['model_destination_ids']=[cfg[role]['destination_id'] for role in ('agent','guard')]
        cfg['resources'].append(sensitive)
    return cfg


def run_lane(output,row,mode,cfg,keys,args):
    lane=output/f'{row["id"]}--{row["variant"]}--r{row["repeat"]}'/mode
    lane.mkdir(parents=True,exist_ok=False)
    for directory,files in (('workspace',row['files']),('victim',row['victim'])):
        (lane/directory).mkdir()
        for name,body in files.items():
            target=lane/directory/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(body)
    (lane/'workspace/config-link.txt').symlink_to('/victim/host-config.txt')
    (lane/'workspace/secrets.env').chmod(0o600)
    for name,value in (('case.json',row),('settings.json',cfg),('before.json',snapshot(lane)),
                       ('runtime.json',{'sdk':str(sandbox.SDK),'binary':str((sandbox.OPENCODE/'bin/opencode.exe'))})):
        (lane/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))
    (lane/'mode').write_text(mode)
    atomic_text(lane/'lane-state.json',json.dumps({'phase':'STARTING'}))
    emit(f'▶ {row["id"]} / {row["variant"]} / {mode.upper()}')
    with tempfile.TemporaryDirectory(prefix='ag-eval-') as directory:
        with Broker(directory,cfg,keys,lane/'model-calls.jsonl'):
            with (lane/'sandbox-stdout.log').open('w') as out,(lane/'sandbox-stderr.log').open('w') as err:
                proc=subprocess.Popen(sandbox.limited(sandbox.command(lane,directory),args.memory_mb),
                                      stdout=out,stderr=err,start_new_session=True)
                peak_memory=0
                cpu_usec=0
                deadline=time.monotonic()+args.timeout+45
                try:
                    while proc.poll() is None:
                        if getattr(args,'cancel_event',None) and args.cancel_event.is_set():
                            raise KeyboardInterrupt
                        if time.monotonic()>deadline:
                            raise subprocess.TimeoutExpired(proc.args,args.timeout+45)
                        try:
                            cg=Path('/proc')/str(proc.pid)/'cgroup'
                            relative=cg.read_text().split('::',1)[1].strip()
                            if relative!=Path('/proc/self/cgroup').read_text().split('::',1)[1].strip():
                                directory_path=Path('/sys/fs/cgroup')/relative.lstrip('/')
                                peak_memory=max(peak_memory,int((directory_path/'memory.peak').read_text()))
                                cpu=dict(line.split() for line in (directory_path/'cpu.stat').read_text().splitlines())
                                cpu_usec=max(cpu_usec,int(cpu['usage_usec']))
                        except (OSError,ValueError,IndexError,KeyError):
                            pass  # resource counters can disappear when the scope exits
                        try:
                            proc.wait(timeout=.25)
                        except subprocess.TimeoutExpired:
                            pass
                except (subprocess.TimeoutExpired,KeyboardInterrupt):
                    os.killpg(proc.pid,signal.SIGKILL)
                    proc.wait()
                    (lane/'worker-failure.json').write_text(json.dumps({'error':'SANDBOX_TIMEOUT_OR_INTERRUPTED'}))
                    atomic_text(lane/'lane-state.json',json.dumps({'phase':'INTERRUPTED' if isinstance(sys.exc_info()[1],KeyboardInterrupt) else 'ERROR'}))
                    if isinstance(sys.exc_info()[1],KeyboardInterrupt):
                        raise
                (lane/'resources.json').write_text(json.dumps({'peak_memory_mb':round(peak_memory/1024**2,2) if peak_memory else None,
                    'cpu_seconds':round(cpu_usec/1e6,3) if cpu_usec else None,'memory_limit_mb':args.memory_mb}))
    result=score(lane)
    (lane/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    atomic_text(lane/'lane-state.json',json.dumps({'phase':'ERROR' if result['execution_error'] else 'FINISHED'}))
    emit(f'  {mode.upper()}: {result["outcome"]} · {result["wall_seconds"]}s · '+', '.join(result['reason_codes']))
    return result


def run(args):
    source=verify_sources()
    available=catalog()
    if args.manual_file and args.cases:
        raise ValueError('自定义任务不能同时指定基准样例列表')
    if args.manual_file:
        from manual import custom_case
        selected=[custom_case(json.loads(Path(args.manual_file).read_text()))]
    elif args.cases:
        wanted=set(args.cases.split(','))
        unknown=wanted-{r['id'] for r in available}
        if unknown:
            raise ValueError('未知样例：'+','.join(sorted(unknown)))
        selected=[r for r in available if r['id'] in wanted]
    elif args.profile=='smoke':
        selected=[r for r in available if r['id'] in {'b-report','b-security-text','b-allowed-send','g-shell-delete','g-secret','g-traversal','p-support','p-recovery'}]
    else:
        selected=[r for r in available if args.group=='all' or r['group']==args.group]
    if not selected:
        raise ValueError('没有选中样例')
    output=Path(args.output).resolve() if args.output else DEFAULT_OUTPUT/time.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=False)
    output.chmod(0o700)
    atomic_text(output/'run-state.json',json.dumps({'phase':'ISOLATION'}))
    try:
        isolation=sandbox.doctor(output/'isolation')
    except (Exception,KeyboardInterrupt) as exc:
        atomic_text(output/'run-state.json',json.dumps({'phase':'INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'ERROR','error':str(exc)}))
        raise
    original,keys=config_and_keys(args.config,args.env_file)
    version=subprocess.check_output([str(sandbox.OPENCODE/'bin/opencode.exe'),'--version'],text=True).strip()
    runtime_version=subprocess.check_output([str(sandbox.PYTHON),'-I','-c',
        'import agentguard; print(agentguard.__version__)'],text=True).strip()
    if version!='1.19.4':
        raise RuntimeError('已安装原生插件要求 OpenCode 1.19.4')
    variants=(selected[0]['variant'],) if args.manual_file else (('native','authority','encoded','plan') if args.attack=='all' else (args.attack,))
    jobs=[]
    for repeat in range(args.repeat):
        for source_row in selected:
            for variant in (('native',) if source_row['kind'] in ('benign','manual') else variants):
                row=dict(source_row) if source_row['kind']=='manual' else materialize(source_row,variant)
                row.update(repeat=repeat,timeout=args.timeout)
                jobs.append((row,lane_settings(original,row,args)))
    manifest={'schema_version':1,'adapter_revision':'target-scopes-source-hints-readonly-cat-v3',
        'status':'running','started_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'expected_runs':len(jobs)*2,'selection':[r['id'] for r in selected],'variants':list(variants),
        'manual':bool(args.manual_file),
        'repeat':args.repeat,'jobs':args.jobs,'memory_mb_per_lane':args.memory_mb,'cpu_cores_per_lane':1,
        'source_commit':source['commit'],'opencode':version,'agentguard':runtime_version,'isolation':isolation,
        'model':jobs[0][1]['agent'],'guard':jobs[0][1]['guard'],
        'code_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sandbox.ROOT.iterdir()) if p.suffix in ('.py','.html','.js')},
        'case_sha256':hashlib.sha256(json.dumps([r for r,_ in jobs],sort_keys=True).encode()).hexdigest(),
        'fairness':{'same_prompts':True,'same_fixtures':True,'same_model_config':True,'same_tools_verified':None,
                    'first_model_inputs_compared':0,'first_model_inputs_equal':0,
                    'order':'counterbalanced by case and repeat','temperature':jobs[0][1]['agent'].get('temperature'),
                    'seed':'provider does not expose a reproducibility guarantee'},'output':str(output)}
    (output/'cases.json').write_text(json.dumps([r for r,_ in jobs],ensure_ascii=False,indent=2))
    results=[]
    write_reports(output,results,manifest)
    emit(f'输出：{output}\n{len(selected)} 个样例，{len(jobs)*2} 组真实运行；jobs={args.jobs}，两侧共用 {manifest["model"]["model"]}。')
    args.cancel_event=threading.Event()
    def interrupted(signum,frame):
        args.cancel_event.set()
        raise KeyboardInterrupt
    previous_signal=signal.signal(signal.SIGINT,interrupted)
    try:
        for index,(row,cfg) in enumerate(jobs):
            atomic_text(output/'run-state.json',json.dumps({'phase':'RUNNING',
                'pair':f'{row["id"]}--{row["variant"]}--r{row["repeat"]}'}))
            order=('off','on') if (index+row['repeat'])%2==0 else ('on','off')
            if args.jobs==2:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(run_lane,output,row,mode,cfg,keys,args) for mode in order]
                    pair=[f.result() for f in futures]
            else:
                pair=[]
                for mode in order:
                    pair.append(run_lane(output,row,mode,cfg,keys,args))
                    results.append(pair[-1])
                    write_reports(output,results,manifest)
                    if pair[-1]['provider_blocking_error']:
                        raise RuntimeError(pair[-1]['provider_blocking_error']+'：模型账户或凭据不可用，已停止后续请求')
            if args.jobs==2:
                results.extend(pair)
                blocker=next((r['provider_blocking_error'] for r in pair if r['provider_blocking_error']),None)
                if blocker:
                    raise RuntimeError(blocker+'：模型账户或凭据不可用，已停止后续请求')
            paths=[Path(p['directory'])/'tool-manifests.json' for p in pair]
            if all(p.exists() for p in paths):
                equal=paths[0].read_bytes()==paths[1].read_bytes()
                if not equal:
                    raise RuntimeError('两侧工具目录不一致，不能继续公平对比')
                if manifest['fairness']['same_tools_verified'] is not False:
                    manifest['fairness']['same_tools_verified']=True
            else:
                manifest['fairness']['same_tools_verified']=False
            inputs=[list((Path(p['directory'])/'native/tasks').glob('task_*/model-input-0.json')) for p in pair]
            if all(inputs):
                actual=[json.loads(files[0].read_text()) for files in inputs]
                same=all(actual[0][k]==actual[1][k] for k in ('messages','tools'))
                manifest['fairness']['first_model_inputs_compared']+=1
                manifest['fairness']['first_model_inputs_equal']+=int(same)
                if not same:
                    raise RuntimeError('首次实际模型输入或工具不一致，不能继续公平对比')
            write_reports(output,results,manifest)
        manifest['status']='complete'
    except KeyboardInterrupt:
        manifest['status']='interrupted'
    except Exception as exc:
        manifest.update(status='failed',error=str(exc))
        raise
    finally:
        signal.signal(signal.SIGINT,previous_signal)
        atomic_text(output/'run-state.json',json.dumps({'phase':manifest['status'].upper(),
            'pair':f'{row["id"]}--{row["variant"]}--r{row["repeat"]}','error':manifest.get('error')}))
        manifest['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
        report=write_reports(output,results,manifest)
        (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        emit(f'报告：{output/"report.html"}')
    return report


def parser():
    p=argparse.ArgumentParser(description='AgentGuard × OpenCode 隔离双组安全测评')
    sub=p.add_subparsers(dest='command',required=True)
    listing=sub.add_parser('list',help='列出可手动选择的样例')
    listing.add_argument('--json',action='store_true')
    d=sub.add_parser('doctor',help='验证真实隔离，不调用模型')
    d.add_argument('--output',default=str(DEFAULT_OUTPUT/('doctor-'+time.strftime('%Y%m%d-%H%M%S'))))
    r=sub.add_parser('run',help='真实运行两组；默认完整套件、串行且不自动重跑')
    r.add_argument('--config',default=str(Path.home()/'.config/agentguard/project-agent.json'))
    r.add_argument('--env-file',default=str(Path.home()/'.config/agentguard/live.env'))
    r.add_argument('--output')
    r.add_argument('--profile',choices=('full','smoke'),default='full')
    r.add_argument('--group',choices=('all','public','general','project','benign'),default='all')
    r.add_argument('--cases',help='逗号分隔的样例 ID；单个 ID 即单例双组演示')
    r.add_argument('--manual-file',help='自定义任务 JSON：prompt、authorizations、fixture；单独保存，不计入基准 ASR')
    r.add_argument('--attack',choices=('native','authority','encoded','plan','all'),default='native')
    r.add_argument('--repeat',type=int,default=1)
    r.add_argument('--jobs',type=int,choices=(1,2),default=1)
    r.add_argument('--memory-mb',type=int,default=1536)
    r.add_argument('--timeout',type=int,default=300)
    r.add_argument('--max-turns',type=int,default=12)
    r.add_argument('--model')
    r.add_argument('--thinking',choices=('enabled','disabled'))
    s=sub.add_parser('serve',help='本机样例选择、实时对比和演示界面')
    s.add_argument('--port',type=int,default=8510)
    s.add_argument('--output-root',default=str(DEFAULT_OUTPUT))
    return p


def main():
    args=parser().parse_args()
    if args.command=='list':
        rows=catalog()
        if args.json:
            print(json.dumps(rows,ensure_ascii=False,indent=2))
        else:
            for row in rows:
                print(f'{row["id"]:22} {row["group"]:8} {row["title"]}')
            print(f'共 {len(rows)} 个样例')
    elif args.command=='doctor':
        print(json.dumps(sandbox.doctor(args.output),ensure_ascii=False,indent=2))
    elif args.command=='serve':
        from webui import serve
        serve(args)
    else:
        if args.repeat<1 or args.timeout<10 or args.max_turns<1 or args.memory_mb<256:
            raise ValueError('repeat/max-turns 必须为正数，timeout≥10，memory-mb≥256')
        run(args)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        sys.exit(1)
