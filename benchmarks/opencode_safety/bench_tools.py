"""Identical trusted tool pack in both lanes; effects confined to synthetic roots."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib import request

from agentguard.v2.contracts import ToolManifest, make
from agentguard.v2.execution.executor import KnownToolFailure
from agentguard.v2.registry import ToolRegistry
from agentguard.v2.state.lineage import public_labels
from sandbox import tool_command
from live import emit, display_args


def tools(settings):
    registry = ToolRegistry()
    destinations = {d['destination_id']:d['address'] for d in settings['network_destinations']}

    def checked(path):
        p = Path(path)
        p = (Path('/workspace')/p).resolve() if not p.is_absolute() else p.resolve()
        if not any(p.is_relative_to(root) and p!=Path(root) for root in ('/workspace','/victim')):
            raise KnownToolFailure('OUTSIDE_SYNTHETIC_ROOTS')
        return p

    def record(tool, args, fn):
        tick = time.monotonic()
        entry = {'tool':tool,'args':args,'started':time.time()}
        emit('execution_started',tool=tool,args=display_args(args))
        try:
            value = fn()
            entry.update(ok=True,result=value)
            return value
        except Exception as exc:
            entry.update(ok=False,error=type(exc).__name__)
            if isinstance(exc,KnownToolFailure):
                raise
            raise KnownToolFailure('SANDBOX_TOOL_FAILED') from None
        finally:
            entry['latency_ms'] = round((time.monotonic()-tick)*1000,2)
            with Path('/out/effects.jsonl').open('a') as f:
                f.write(json.dumps(entry,ensure_ascii=False)+'\n')
            emit('execution_finished',tool=tool,ok=entry['ok'],latency_ms=entry['latency_ms'],error=entry.get('error'))

    def read_text(path):
        def run():
            with checked(path).open('rb') as f:
                data=f.read(100001)
            if len(data)>100000:
                raise KnownToolFailure('CONTENT_TOO_LARGE')
            return data.decode()
        return record('read_text',{'path':path},run)

    def write_text(path,body):
        def run():
            p=checked(path)
            if len(body.encode())>100000:
                raise KnownToolFailure('CONTENT_TOO_LARGE')
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(body)
            return {'written':True}
        return record('write_text',{'path':path,'body':body},run)

    def delete_path(path):
        def run():
            p=checked(path)
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            return {'deleted':True}
        return record('delete_path',{'path':path},run)

    def run_command(path,command):
        def run():
            if Path(path).resolve()!=Path('/workspace'):
                raise KnownToolFailure('INVALID_COMMAND_CWD')
            # A nested namespace hides the runtime, broker, evidence and credentials.
            argv=tool_command(['/usr/bin/timeout','-k','1','8','/bin/bash','-c',command])
            with Path('/tmp/command-output').open('w+b') as log:
                p=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try:
                    code=p.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid,signal.SIGKILL)
                    p.wait()
                    code=124
                log.seek(0)
                output=log.read(16000).decode(errors='replace')
            return {'exit_code':code,'output':output}
        return record('run_command',{'path':path,'command':command},run)

    def send_http(destination,body):
        def run():
            if destination not in destinations or len(body.encode())>100000:
                raise KnownToolFailure('INVALID_DESTINATION')
            req=request.Request(destinations[destination],data=json.dumps({'body':body}).encode(),
                                headers={'Content-Type':'application/json'})
            with request.build_opener(request.ProxyHandler({})).open(req,timeout=5) as reply:
                return {'status':reply.status,'receipt':reply.read(1000).decode()}
        return record('send_http',{'destination':destination,'body':body},run)

    specs=[
        ('read_text',read_text,['READ'],{'path':{'type':'string'}},'/path',None,'text/plain'),
        ('write_text',write_text,['WRITE'],{'path':{'type':'string'},'body':{'type':'string'}},'/path',None,'application/json'),
        ('delete_path',delete_path,['DELETE'],{'path':{'type':'string'}},'/path',None,'application/json'),
        ('run_command',run_command,['READ','WRITE','DELETE'],{'path':{'type':'string'},'command':{'type':'string'}},'/path',None,'application/json'),
        ('send_http',send_http,['SEND'],{'destination':{'type':'string','enum':list(destinations)},'body':{'type':'string'}},None,'/destination','application/json'),
    ]
    for name,handler,effects,props,scope,dest,media in specs:
        registry.register(make(ToolManifest,tool_id=name,manifest_version='benchmark-v1',effects=effects,
            parameters_schema={'type':'object','properties':props,'required':list(props),'additionalProperties':False},
            resource_parameter=scope,destination_parameter=dest,artifact_parameters={},return_media_type=media,
            supports_idempotency_key=False,output_labels=public_labels('external'),synthetic_demo=True),handler)
    return registry
