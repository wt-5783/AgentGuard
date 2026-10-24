"""Run the installed NativeController and a real OpenCode CLI inside Bubblewrap."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import resource
import signal
import shutil
import socket
import sys
import threading
import time

from broker import exchange
from live import configure, emit


class BrokerTransport:
    async def post(self,body,config):
        return json.dumps(await asyncio.to_thread(exchange,'guard',body))


class BrokerUpstream:
    def __init__(self,app,mode='on'):
        self.config=app.model.client.config
        self.effort=app.settings.get('opencode',{}).get('reasoning_effort')
        self.last_finish_reason=None
        self.attempts_used=1
        self.app_mode=mode
        self.tool_names={'ag_'+m.tool_id for m in app.request.tool_manifests}

    async def complete(self,messages,tools):
        from agentguard.v2.common import RuntimeFault
        c=self.config
        body={'model':c.model,'messages':messages,'tools':tools,'tool_choice':'auto',
              'max_tokens':c.max_output_tokens,'stream':False}
        if c.thinking:
            body['thinking']={'type':c.thinking}
        if self.effort:
            body['reasoning_effort']=self.effort
        if c.thinking!='enabled' and c.temperature is not None:
            body['temperature']=c.temperature
        try:
            value=await asyncio.to_thread(exchange,'agent',body)
            choice=value['choices'][0]
            self.last_finish_reason=choice['finish_reason']
            if choice['finish_reason'] not in ('stop','tool_calls'):
                raise RuntimeFault('INCOMPLETE_MODEL_OUTPUT')
            for call in choice['message'].get('tool_calls') or []:
                function=call.get('function',{})
                emit('proposal',tool=function.get('name') if function.get('name') in self.tool_names else '[未注册工具]',args=str(function.get('arguments',''))[:6000],
                     status='PENDING_REVIEW' if self.app_mode=='on' else 'PENDING_EXECUTION')
            return choice['message'],value.get('usage') or {}
        except RuntimeFault:
            raise
        except Exception:
            raise RuntimeFault('MODEL_UNAVAILABLE') from None


def factory(settings,task):
    from agentguard.v2.application import compose
    app=compose(settings,task)
    # Same client is shared by policy compiler and reviewer. Only transport changes.
    app.runtime.guard.reviewer.client.transport=BrokerTransport()
    inspect=app.runtime.inspect
    execute=app.runtime.execute_call
    async def inspect_live(sid,turn_id,call_id,phase,subject_ref,context_version):
        emit('review_started',phase=phase,call_id=call_id)
        decision,code=await inspect(sid,turn_id,call_id,phase,subject_ref,context_version)
        emit('review_finished',phase=phase,call_id=call_id,
             decision=decision.decision if decision else 'FAILED',reason_code=code)
        return decision,code
    async def execute_live(call):
        result=await execute(call)
        emit('receipt',tool=call.tool_id,status=result.status,reason_code=result.reason_code,call_id=call.call_id)
        return result
    app.runtime.inspect=inspect_live
    app.runtime.execute_call=execute_live
    return app


def receiver():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            if self.path not in ('/approved','/external_support'):
                self.send_error(404)
                return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=110000:
                self.send_error(413)
                return
            body=json.loads(self.rfile.read(size))
            with Path('/out/deliveries.jsonl').open('a') as f:
                f.write(json.dumps({'destination':self.path[1:],'payload':body,'time':time.time()})+'\n')
            raw=b'{"received":true}'
            self.send_response(200)
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    return server


async def run():
    import uvicorn
    from agentguard.v2.opencode import create_native_app
    from agentguard.v2.opencode_host import NativeController
    import agentguard.v2.opencode as native
    settings=json.loads(Path('/out/settings.json').read_text())
    row=json.loads(Path('/out/case.json').read_text())
    for name in ('workspace','victim'):
        shutil.copytree('/out/'+name,'/'+name,dirs_exist_ok=True,symlinks=True)
    mode=Path('/out/mode').read_text()
    configure('/out',mode)
    r=receiver()
    settings['network_destinations']=[{'schema_version':'0.1','destination_id':name,'kind':'network',
        'address':f'http://127.0.0.1:{r.server_port}/{name}',
        'allowed_confidentiality':row.get('approved_confidentiality',['internal']) if name=='approved' else ['internal']}
        for name in ('approved','external_support')]
    class LiveController(NativeController):
        def save(self,state):
            super().save(state)
            status=self.status(state.native_id)
            if status!=getattr(state,'_last_display_status',None):
                emit('status',status=status)
                state._last_display_status=status
    controller=LiveController(settings,'/out/native/tasks',mode=mode,factory=factory,
        upstream_factory=lambda app:BrokerUpstream(app,mode))
    emit('status',status={'phase':'STARTING','mode':mode,'model':settings['agent']['model']})
    token=secrets.token_urlsafe(24)
    sock=socket.socket()
    sock.bind(('127.0.0.1',0))
    sock.listen(64)
    url=f'http://127.0.0.1:{sock.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(create_native_app(controller,token),log_level='warning',access_log=False))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(.02)
    assets=Path(native.__file__).parent/'opencode_plugin'
    sdk=Path(json.loads(Path('/out/runtime.json').read_text())['sdk'])/'@opencode-ai/plugin/dist/tool.js'
    # OpenCode otherwise tries to install its SDK at startup. Pre-populate the
    # two config roots from the already installed, pinned SDK, with no network.
    for directory in ('/home/bench/config','/home/bench/xdg-config/opencode'):
        p=Path(directory)
        p.mkdir(parents=True,exist_ok=True)
        (p/'node_modules').symlink_to(sdk.parents[3])
        package={'dependencies':{'@opencode-ai/plugin':'1.19.4'}}
        (p/'package.json').write_text(json.dumps(package))
        (p/'package-lock.json').write_text(json.dumps({'lockfileVersion':3,'packages':{'':package}}))
    model=settings['agent']['model']
    options={'url':url,'token':token,'sdk':sdk.as_uri()}
    agent={'mode':'primary','description':'AgentGuard safety benchmark','steps':settings['max_turns'],
           'permission':{'*':'deny','ag_*':'allow'},'tools':{'*':False,'ag_*':True}}
    config={'$schema':'https://opencode.ai/config.json','model':'agentguard/'+model,'small_model':'agentguard/'+model,
        'enabled_providers':['agentguard'],'default_agent':'agentguard',
        'provider':{'agentguard':{'npm':'@ai-sdk/openai-compatible','name':'AgentGuard Native',
            'options':{'baseURL':url+'/v1','apiKey':token,'timeout':600000},
            'models':{model:{'name':model,'limit':{'context':131072,'output':16384}}}}},
        'plugin':[[(assets/'server.mjs').as_uri(),options]],'permission':{'*':'deny','ag_*':'allow'},
        'tools':{'*':False,'ag_*':True},'agent':dict(agentguard=agent,**{n:{'disable':True} for n in ('build','plan','general','explore')}),
        'mcp':{},'snapshot':False,'share':'disabled','autoupdate':False,'compaction':{'auto':False,'prune':False}}
    Path('/out/opencode.json').write_text(json.dumps(config))
    env=dict(os.environ)
    env.update(OPENCODE_CONFIG='/out/opencode.json',OPENCODE_CONFIG_CONTENT=json.dumps(config),
        OPENCODE_CONFIG_DIR='/home/bench/config',OPENCODE_DISABLE_AUTOUPDATE='1',
        OPENCODE_DISABLE_DEFAULT_PLUGINS='1',OPENCODE_DISABLE_CLAUDE_CODE='1',OPENCODE_DISABLE_MODELS_FETCH='1',
        OPENCODE_DISABLE_LSP_DOWNLOAD='1',OPENCODE_DISABLE_TERMINAL_TITLE='1')
    for name in ('CONFIG','DATA','CACHE','STATE'):
        env['XDG_'+name+'_HOME']='/home/bench/xdg-'+name.lower()
    binary=json.loads(Path('/out/runtime.json').read_text())['binary']
    started=time.monotonic()
    with Path('/out/opencode-events.jsonl').open('w') as out,Path('/out/opencode-stderr.log').open('w') as err:
        process=await asyncio.create_subprocess_exec(binary,'run','--format','json','--agent','agentguard','--dir','/workspace',row['task'],
            env=env,cwd='/workspace',stdout=asyncio.subprocess.PIPE,stderr=err,start_new_session=True,
            limit=1024*1024)
        async def display_native():
            async for raw in process.stdout:
                out.write(raw.decode(errors='replace'));out.flush()
                try:
                    event=json.loads(raw)
                except ValueError:
                    continue
                # Text reaches OpenCode only after the native FINAL gate. Never
                # display raw model outputs or quarantined tool observations.
                if event.get('type')=='text':
                    emit('text',text=event.get('part',{}).get('text',''),native_session_id=event.get('sessionID'))
                elif event.get('type')=='error':
                    emit('native_error',error=str(event.get('error','OpenCode error'))[:2000])
        displaying=asyncio.create_task(display_native())
        try:
            await asyncio.wait_for(process.wait(),timeout=row['timeout'])
            reason=None
        except asyncio.TimeoutError:
            os.killpg(process.pid,signal.SIGKILL)
            await process.wait()
            reason='TIMEOUT'
        finally:
            await displaying
            from scoring import snapshot
            # Only the trusted worker can see /out. Keep the final filesystem
            # observations outside the tools' nested namespace, without copying
            # arbitrary attacker-created files onto the host disk.
            Path('/out/after.json').write_text(json.dumps(snapshot('/')))
            observed={}
            targets={'workspace/report.txt'}
            if row.get('oracle',{}):
                if row['oracle'].get('path'):
                    targets.add(row['oracle']['path'])
            for name in targets:
                p=Path('/')/name
                resolved=p.resolve()
                if p.is_file() and any(resolved.is_relative_to(root) for root in (Path('/workspace'),Path('/victim'))):
                    with p.open('rb') as f:
                        observed[name]=f.read(100001).decode(errors='replace')
            Path('/out/observed-files.json').write_text(json.dumps(observed,ensure_ascii=False))
            state=controller.status()
            finals=[]
            for task in controller.tasks.values():
                for response in task.responses.values():
                    content=response['response']['choices'][0]['message'].get('content')
                    if content:
                        finals.append(content)
            result={'mode':mode,'status':state,'exit_code':process.returncode,'error':reason,
                    'wall_seconds':round(time.monotonic()-started,3),'final':'\n'.join(finals)}
            Path('/out/worker-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            emit('finished',exit_code=process.returncode,error=reason,wall_seconds=result['wall_seconds'])
            manifests=[m.model_dump(mode='json') for m in controller.catalog_app.request.tool_manifests]
            Path('/out/tool-manifests.json').write_text(json.dumps(manifests,sort_keys=True))
            server.should_exit=True
            await serving
            r.shutdown()
            r.server_close()


if __name__=='__main__':
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
        asyncio.run(run())
    except Exception as exc:
        Path('/out/worker-failure.json').write_text(json.dumps({'error':type(exc).__name__,'detail':str(exc)[:1000]}))
        raise
