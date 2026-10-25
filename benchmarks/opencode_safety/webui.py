"""Loopback-only demonstration UI. One run at a time; no external dependencies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import os
import re
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

from suite import catalog,materialize
from manual import custom_case
from live import pair_view


def ui_token():
    value = os.environ.get('AGENTGUARD_EVAL_TOKEN')
    if value:
        return value
    path = Path(os.environ.get('AGENTGUARD_EVAL_TOKEN_FILE',
        str(Path.home()/'.config/agentguard/eval-ui-token')))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32))
    value = path.read_text().strip()
    if not value:
        raise ValueError('Empty evaluation token file')
    path.chmod(0o600)
    return value


def create_server(args):
    root=Path(args.output_root).resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    state={'process':None,'output':None,'log':None,'handle':None}
    token=ui_token()
    journal=root/'.submissions'
    journal.mkdir(exist_ok=True,mode=0o700)
    lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def reply(self,value,code=200,content_type='application/json'):
            raw=value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type',content_type+'; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(raw)
        def read_report(self, path):
            if not path.resolve().is_relative_to(root):
                raise ValueError('Report outside output root')
            if path.stat().st_size > 16*1024*1024:
                raise ValueError('Report too large')
            return json.loads(path.read_text())
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{args.port}',f'localhost:{args.port}'):
                return self.reply({'error':'Invalid host'},403)
            parsed=urlsplit(self.path)
            query=parse_qs(parsed.query)
            if parsed.path=='/':
                page=PAGE.replace('__TOKEN__',token)
                if query.get('embed')==['1']:
                    page=page.replace('<body>','<body class="embedded">')
                return self.reply(page,content_type='text/html')
            if parsed.path=='/api/cases':
                return self.reply(catalog())
            if parsed.path=='/api/template':
                case=next((r for r in catalog() if r['id']==query.get('case',['b-report'])[0]),None)
                attack=query.get('attack',['native'])[0]
                if case is None or attack not in ('native','authority','encoded','plan'):
                    return self.reply({'error':'未知样例或变体'},400)
                row=materialize(case,attack)
                return self.reply({'prompt':row['task'],'authorizations':row['authorizations'],
                    'write_paths':row['write_paths'],
                    'files':list(row['files']),'source':row.get('source')})
            if parsed.path=='/api/live':
                output=state['output']
                run=query.get('run',[None])[0]
                if run is not None:
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+',run) or run in ('.','..'):
                        return self.reply({'error':'Unknown run'},404)
                    output=root/run
                    if not output.resolve().is_relative_to(root) or (not output.is_dir() and output!=state['output']):
                        return self.reply({'error':'Unknown run'},404)
                elif output is None:
                    previous=sorted(root.glob('*/report.json'),key=lambda p:p.stat().st_mtime,reverse=True)
                    output=previous[0].parent if previous else None
                try:
                    offsets={m:int(query.get('after_'+m,['0'])[0]) for m in ('off','on')}
                    if query.get('cursor_run',[None])[0]!=str(output):
                        offsets={}
                    value=pair_view(output,query.get('pair',[None])[0],offsets,query.get('cursor_pair',[''])[0]) if output else {'pairs':[],'lanes':{},'run_state':{}}
                except (ValueError,TypeError):
                    return self.reply({'error':'无效双组任务或事件位置'},400)
                p=state['process'];busy=p is not None and p.poll() is None
                value.update(output=str(output) if output else None,busy=busy,
                    running=busy and output==state['output'],exit_code=p.poll() if p else None)
                return self.reply(value)
            if parsed.path=='/api/runs':
                runs=[]
                for path in sorted(root.glob('*/report.json'),key=lambda p:p.stat().st_mtime,reverse=True):
                    try:
                        report=self.read_report(path)
                        runs.append({'id':path.parent.name,'status':report['manifest']['status'],
                            'rows':len(report['rows']),'expected_runs':report['manifest']['expected_runs']})
                    except (OSError,ValueError,KeyError,TypeError):
                        continue
                return self.reply(runs)
            if parsed.path=='/api/status':
                p=state['process']
                running=p is not None and p.poll() is None
                busy=running
                output=state['output']
                run=query.get('run',[None])[0]
                if run is not None:
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+',run) or run in ('.','..'):
                        return self.reply({'error':'Unknown run'},404)
                    output=root/run
                    if not output.resolve().is_relative_to(root) or not (output/'report.json').is_file():
                        return self.reply({'error':'Unknown run'},404)
                    running=running and output==state['output']
                if output is None:
                    previous=sorted(root.glob('*/report.json'),key=lambda p:p.stat().st_mtime,reverse=True)
                    output=previous[0].parent if previous else None
                try:
                    report=self.read_report(output/'report.json') if output and (output/'report.json').exists() else None
                except (OSError,ValueError):
                    return self.reply({'error':'Report temporarily unavailable'},503)
                log=state['log'].read_text()[-18000:] if output==state['output'] and state['log'] and state['log'].exists() else ''
                demo=[]
                if report and report['rows']:
                    latest=report['rows'][-1]
                    for row in report['rows']:
                        if (row['case_id'],row['variant'],row['repeat'])!=(latest['case_id'],latest['variant'],latest['repeat']):
                            continue
                        lane=Path(row['directory']).resolve()
                        if not lane.is_relative_to(root):
                            continue
                        worker=json.loads((lane/'worker-result.json').read_text()) if (lane/'worker-result.json').exists() else {}
                        demo.append({'mode':row['mode'],'id':row['case_id'],'outcome':row['outcome'],
                            'reasons':row['reason_codes'],'final':worker.get('final'),
                            'changed_files':row['changed_files'],'external_deliveries':row['external_deliveries']})
                return self.reply({'running':running,'busy':busy,'exit_code':p.poll() if p else None,'report':report,'log':log,'demo':demo,
                                   'output':str(output) if output else None})
            if self.path.startswith('/api/detail?'):
                query=parse_qs(urlsplit(self.path).query)
                p=Path(query.get('directory',[''])[0]).resolve()
                if not p.is_relative_to(root) or not (p/'case.json').exists():
                    return self.reply({'error':'Unknown run'},404)
                value={'case':json.loads((p/'case.json').read_text()),'result':json.loads((p/'result.json').read_text()) if (p/'result.json').exists() else None}
                for name in ('effects.jsonl','deliveries.jsonl','model-calls.jsonl','opencode-stderr.log','sandbox-stderr.log'):
                    value[name]=(p/name).read_text()[-30000:] if (p/name).exists() else ''
                final=p/'worker-result.json'
                worker=self.read_report(final) if final.exists() else {}
                value['final']=worker.get('final')
                value['mode']=p.name if p.name in ('on','off') else None
                live=p/'live-state.json'
                status=worker.get('status') or (self.read_report(live).get('status',{}) if live.exists() else {})
                value['status']={k:status[k] for k in ('phase','executed','blocked','policy_rejected','recoveries',
                    'redacted','reviews','native_session_id','model','reason_code') if k in status}
                report=p.parents[1]/'report.json'
                manifest=self.read_report(report).get('manifest',{}) if report.is_relative_to(root) and report.exists() else {}
                value['environment']={k:manifest[k] for k in ('isolation','opencode','agentguard',
                    'memory_mb_per_lane','cpu_cores_per_lane') if k in manifest}
                value['guard_trace_files']=[str(f.relative_to(p)) for f in sorted((p/'guard-traces').glob('*.json'))]
                for name in ('observed-files.json','after.json','before.json'):
                    value[name]=json.loads((p/name).read_text()) if (p/name).exists() else None
                return self.reply(value)
            return self.reply({'error':'Not found'},404)
        def do_POST(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{args.port}',f'localhost:{args.port}') or self.path not in ('/api/run','/api/stop') or not hmac.compare_digest(self.headers.get('X-Bench-Token',''),token):
                return self.reply({'error':'Forbidden'},403)
            if self.path=='/api/stop':
                with lock:
                    p=state['process']
                    if p is None or p.poll() is not None:
                        return self.reply({'stopping':False})
                    p.send_signal(signal.SIGINT)
                return self.reply({'stopping':True})
            try:
                size=int(self.headers.get('Content-Length','0'))
            except ValueError:
                return self.reply({'error':'Invalid request'},400)
            if not 0<size<48000:
                return self.reply({'error':'Invalid request'},400)
            try:
                value=json.loads(self.rfile.read(size))
                if not isinstance(value,dict):
                    raise ValueError('请求必须为 JSON 对象')
                if set(value)-{'cases','profile','attack','thinking','submission_id','manual','jobs'}:
                    raise ValueError('未知请求字段')
                ids=value.get('cases',[])
                if not isinstance(ids,list) or not all(isinstance(x,str) and x in {r['id'] for r in catalog()} for x in ids):
                    raise ValueError('无效样例')
                if value.get('profile','full') not in ('selected','smoke','full','manual') or (value.get('profile')=='selected' and not ids):
                    raise ValueError('所选测评必须包含样例')
                manual=value.get('manual')
                if value.get('profile')=='manual':
                    if ids or manual is None:
                        raise ValueError('自定义任务不能同时指定基准样例')
                    custom_case(manual)
                elif manual is not None:
                    raise ValueError('自定义提示词必须使用 manual 模式')
                jobs=value.get('jobs',2 if manual is not None else 1)
                if type(jobs) is not int or jobs not in (1,2):
                    raise ValueError('并行组数只能为 1 或 2')
                attack=value.get('attack','native')
                if attack not in ('native','authority','encoded','plan','all'):
                    raise ValueError('无效变体')
                thinking=value.get('thinking','configured')
                if thinking not in ('configured','enabled','disabled'):
                    raise ValueError('无效思考模式')
                submission=value.get('submission_id')
                if submission is not None and (not isinstance(submission,str) or not re.fullmatch('[a-f0-9]{32}',submission)):
                    raise ValueError('无效请求标识')
                fingerprint=json.dumps({k:v for k,v in value.items() if k!='submission_id'},sort_keys=True)
                with lock:
                    receipt=journal/(submission+'.json') if submission else None
                    if receipt and receipt.exists():
                        saved=json.loads(receipt.read_text())
                        if saved['fingerprint']!=fingerprint:
                            return self.reply({'error':'请求标识已用于其他参数'},409)
                        if not saved['started']:
                            return self.reply({'error':'提交状态未确认；不自动重放，请先检查记录'},409)
                        return self.reply({'started':True,'output':saved['output'],'reused':True})
                    if state['process'] and state['process'].poll() is None:
                        return self.reply({'error':'已有测评正在运行'},409)
                    if state['handle']:
                        state['handle'].close()
                    output=root/('web-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(2))
                    log=root/(output.name+'.log')
                    handle=log.open('w')
                    command=[sys.executable,str(Path(__file__).parent/'cli.py'),'run','--output',str(output),'--attack',attack,'--jobs',str(jobs)]
                    if thinking!='configured':
                        command+=['--thinking',thinking]
                    if manual is not None:
                        manual_path=journal/(output.name+'.input.json')
                        with manual_path.open('x') as recorded:
                            json.dump(manual,recorded,ensure_ascii=False)
                        manual_path.chmod(0o600)
                        command+=['--manual-file',str(manual_path)]
                    elif ids:
                        command+=['--cases',','.join(ids)]
                    elif value.get('profile')=='smoke':
                        command+=['--profile','smoke']
                    saved={'fingerprint':fingerprint,'output':str(output),'started':False}
                    if receipt:
                        with receipt.open('x') as recorded:
                            json.dump(saved,recorded)
                        receipt.chmod(0o600)
                    try:
                        process=subprocess.Popen(command,stdout=handle,stderr=subprocess.STDOUT,start_new_session=True)
                    except OSError:
                        handle.close()
                        return self.reply({'error':'进程启动未确认；先检查记录，不自动重放'},503)
                    state.update(process=process,output=output,log=log,handle=handle)
                    if receipt:
                        saved['started']=True
                        temporary=receipt.with_suffix('.tmp')
                        temporary.write_text(json.dumps(saved))
                        temporary.chmod(0o600)
                        temporary.replace(receipt)
                return self.reply({'started':True,'output':str(output)})
            except (ValueError,TypeError) as exc:
                return self.reply({'error':str(exc)},400)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    args.port=server.server_address[1]
    server.evaluation_state=state
    return server


def serve(args):
    server=create_server(args)
    state=server.evaluation_state
    print(f'测评控制台：http://127.0.0.1:{args.port}（Ctrl+C 退出）',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if state['process'] and state['process'].poll() is None:
            state['process'].send_signal(2)
            state['process'].wait(timeout=150)
        if state['handle']:
            state['handle'].close()


PAGE=(Path(__file__).with_name('console.html').read_text()
    .replace('__PRESENTATION__',Path(__file__).with_name('presentation.js').read_text())
    .replace('__SCRIPT__',Path(__file__).with_name('console.js').read_text()))
