"""Native OpenCode integration: private boundary service + server/TUI plugins."""
import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from .common import RuntimeFault, canonical, strict_loads
from .connect import load_environment, outside_tools, private_write


def create_native_app(controller, token):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authentication(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'error': {'message': 'UNAUTHORIZED', 'type': 'agentguard'}}, status_code=401)
        if int(request.headers.get('content-length', '0')) > 2_000_000:
            return JSONResponse({'error': {'message': 'CONTENT_TOO_LARGE'}}, status_code=413)
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse({'error': {'message': 'HOST_SERVICE_UNAVAILABLE', 'type': 'agentguard'}}, status_code=503)

    @app.exception_handler(RuntimeFault)
    async def failure(request, exc):
        return JSONResponse({'error': {'message': exc.code, 'type': 'agentguard'}},
            status_code=400 if request.url.path == '/v1/chat/completions' else 409)

    async def body(request):
        raw = await request.body()
        if len(raw) > 2_000_000:
            raise RuntimeFault('CONTENT_TOO_LARGE')
        value = strict_loads(raw)
        if not isinstance(value, dict):
            raise RuntimeFault('INVALID_ARGUMENTS')
        return value

    @app.get('/health')
    async def health():
        return {'healthy': True, 'integration': 'opencode-native-v1'}

    @app.get('/catalog')
    async def catalog():
        return {'tools': controller.catalog(), 'model': controller.settings['agent']['model']}

    @app.get('/status')
    async def status(native_id: str | None = None):
        return controller.status(native_id)

    @app.post('/mode')
    async def mode(request: Request):
        value = await body(request)
        if set(value) != {'mode'}:
            raise RuntimeFault('INVALID_ARGUMENTS')
        return await controller.switch(value['mode'])

    @app.post('/task')
    async def task(request: Request):
        value = await body(request)
        if set(value) != {'native_id', 'task'}:
            raise RuntimeFault('INVALID_ARGUMENTS')
        return await controller.submit(value['native_id'], value['task'])

    @app.post('/receipt')
    async def receipt(request: Request):
        value = await body(request)
        if set(value) != {'native_id', 'tool', 'receipt'}:
            raise RuntimeFault('INVALID_ARGUMENTS')
        return controller.receipt(value['native_id'], value['tool'], value['receipt'])

    @app.get('/v1/models')
    async def models():
        return {'object': 'list', 'data': [{'id': controller.settings['agent']['model'], 'object': 'model', 'owned_by': 'agentguard'}]}

    @app.post('/v1/chat/completions')
    async def completions(request: Request):
        value = await body(request)
        role = request.headers.get('x-agentguard-role')
        if role != 'agentguard':
            # Auxiliary title/compaction requests never get task context or a
            # second uncontrolled provider path. Static title is enough for UI.
            if role not in ('title', 'compaction', 'summary'):
                raise RuntimeFault('UNTRACKED_NATIVE_MODEL_REQUEST')
            response = {'id': 'ag_auxiliary', 'object': 'chat.completion', 'created': int(time.time()),
                'model': controller.settings['agent']['model'], 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': 'AgentGuard · 受管任务'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}
        else:
            response = await controller.turn(request.headers.get('x-agentguard-session'),
                request.headers.get('x-agentguard-request'), value)
        if not value.get('stream'):
            return JSONResponse(response)

        async def events():
            # Only after all checks/effects/finalization have completed.
            choice = response['choices'][0]
            message = choice['message']
            base = {k: response[k] for k in ('id', 'created', 'model')}
            base['object'] = 'chat.completion.chunk'
            delta = {'role': 'assistant'}
            if message.get('content') is not None:
                delta['content'] = message['content']
            if message.get('tool_calls'):
                delta['tool_calls'] = [dict(call, index=i) for i, call in enumerate(message['tool_calls'])]
            yield 'data: ' + canonical(dict(base, choices=[{'index': 0, 'delta': delta, 'finish_reason': None}])) + '\n\n'
            yield 'data: ' + canonical(dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': choice['finish_reason']}], usage=response['usage'])) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})

    return app


def load_native_settings(config, env_file=None):
    from .application import load_settings
    settings = load_settings(config)
    outside_tools(settings, config, env_file)
    load_environment(env_file, {settings[k]['api_key_env'] for k in ('guard', 'agent')})
    os.environ['AGENTGUARD_V2_CLOUD_ENABLED'] = '1'
    options = settings.get('opencode', {})
    allowed = {'allowed_destination_ids', 'max_recoveries', 'reasoning_effort', 'demo_fixture', 'demo_scenario', 'protected_paths'}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError('Unknown native integration option')
    return settings


def serve(args):
    from .opencode_host import NativeController
    import uvicorn
    settings = load_native_settings(args.config, args.env_file)
    directory = Path(args.run_directory).resolve()
    token = (directory / 'token').read_text().strip()
    controller = NativeController(settings, directory / 'tasks', mode=args.mode)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    app = create_native_app(controller, token)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', log_level='warning', access_log=False))
    async def run():
        work = asyncio.create_task(server.serve(sockets=[sock]))
        async def parent_watch():
            while not work.done():
                if args.parent_pid and os.getppid() != args.parent_pid:
                    server.should_exit = True
                    return
                await asyncio.sleep(1)
        watch = asyncio.create_task(parent_watch())
        for _ in range(100):
            if server.started:
                private_write(directory / 'ready.json', canonical({'url': url, 'pid': os.getpid()}))
                break
            if work.done():
                await work
                raise RuntimeFault('NATIVE_SERVICE_START_FAILED')
            await asyncio.sleep(0.05)
        try:
            await work
        finally:
            watch.cancel()
    asyncio.run(run())


def find_sdk():
    expected = Path.home() / '.config/opencode/node_modules/@opencode-ai/plugin/dist/tool.js'
    if expected.exists():
        manifest = strict_loads((expected.parent.parent / 'package.json').read_text())
        if manifest['version'] == '1.19.4':
            return expected.as_uri()
    directory = Path.home() / '.local/share/agentguard/opencode-sdk'
    directory.mkdir(parents=True, exist_ok=True)
    expected = directory / 'node_modules/@opencode-ai/plugin/dist/tool.js'
    if not expected.exists():
        subprocess.run(['npm', 'install', '--prefix', str(directory), '--ignore-scripts', '--omit=dev',
            '@opencode-ai/plugin@1.19.4'], check=True, stdout=sys.stderr)
    return expected.as_uri()


def prepare(config, env_file, mode, *, directory=None):
    settings = load_native_settings(config, env_file)
    binary = shutil.which('opencode')
    if not binary or subprocess.check_output([binary, '--version'], text=True).strip() != '1.19.4':
        raise RuntimeFault('OPENCODE_VERSION_REQUIRES_1_19_4')
    parent = Path(settings['state_directory']) / 'native'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(directory) if directory else Path(tempfile.mkdtemp(prefix='window-', dir=parent))
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(run / 'token', secrets.token_urlsafe(32))
    private_write(run / 'application.json', canonical(settings))
    service_command = [sys.executable, '-I', '-m', 'agentguard.v2.opencode', 'serve', '--config', str(run / 'application.json'),
        '--run-directory', str(run), '--mode', mode]
    if env_file:
        service_command.extend(['--env-file', str(Path(env_file).resolve())])
    log = (run / 'service.log').open('a')
    service = subprocess.Popen(service_command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    try:
        for _ in range(300):
            if service.poll() is not None:
                raise RuntimeFault('NATIVE_SERVICE_START_FAILED')
            if (run / 'ready.json').exists():
                break
            time.sleep(0.05)
        else:
            raise RuntimeFault('NATIVE_SERVICE_START_TIMEOUT')
        ready = strict_loads((run / 'ready.json').read_text())
        options = {'url': ready['url'], 'token': (run / 'token').read_text(), 'sdk': find_sdk()}
        assets = Path(__file__).parent / 'opencode_plugin'
        model = settings['agent']['model']
        config_value = {'$schema': 'https://opencode.ai/config.json', 'model': 'agentguard/' + model,
            'small_model': 'agentguard/' + model, 'enabled_providers': ['agentguard'], 'default_agent': 'agentguard',
            'provider': {'agentguard': {'npm': '@ai-sdk/openai-compatible', 'name': 'AgentGuard Native',
                'options': {'baseURL': ready['url'] + '/v1', 'apiKey': options['token'], 'timeout': 600000},
                'models': {model: {'name': model, 'limit': {'context': 131072, 'output': 16384}}}}},
            'plugin': [[(assets / 'server.mjs').as_uri(), options]],
            'permission': {'*': 'deny', 'ag_*': 'allow'}, 'tools': {'*': False, 'ag_*': True},
            'agent': {'agentguard': {'mode': 'primary', 'description': 'AgentGuard 原生防护开关', 'steps': settings.get('max_turns', 40),
                'permission': {'*': 'deny', 'ag_*': 'allow'}, 'tools': {'*': False, 'ag_*': True}},
                'build': {'disable': True}, 'plan': {'disable': True}, 'general': {'disable': True}, 'explore': {'disable': True}},
            'mcp': {}, 'snapshot': False, 'share': 'disabled', 'autoupdate': False,
            'compaction': {'auto': False, 'prune': False}}
        private_write(run / 'opencode.json', json.dumps(config_value, ensure_ascii=False, indent=2))
        private_write(run / 'tui.json', json.dumps({'$schema': 'https://opencode.ai/tui.json', 'theme': 'opencode',
            'plugin': [[(assets / 'tui.tsx').as_uri(), options]]}, ensure_ascii=False, indent=2))
        private_write(run / 'window.json', canonical({'workspace': settings['workspace'], 'mode': mode,
            'service_pid': service.pid, 'binary': binary, 'version': '1.19.4'}))
        return run, service
    except BaseException:
        service.terminate()
        service.wait(timeout=10)
        raise


def child_environment(run):
    settings = strict_loads((run / 'application.json').read_text())
    env = dict(os.environ)
    for key in list(env):
        if key.startswith('OPENCODE_') or key.startswith('AGENTGUARD_') or key in ('PYTHONPATH', 'PYTHONHOME'):
            del env[key]
    for item in ('agent', 'guard'):
        env.pop(settings[item]['api_key_env'], None)
    for name in ('CONFIG', 'DATA', 'CACHE', 'STATE'):
        env['XDG_' + name + '_HOME'] = str(run / ('xdg-' + name.lower()))
    config_dir = run / 'config'
    config_dir.mkdir(exist_ok=True)
    private_write(config_dir / 'tui.json', (run / 'tui.json').read_text())
    env.update(OPENCODE_CONFIG=str(run / 'opencode.json'), OPENCODE_CONFIG_CONTENT=(run / 'opencode.json').read_text(),
        OPENCODE_CONFIG_DIR=str(config_dir), OPENCODE_DISABLE_AUTOUPDATE='1', OPENCODE_DISABLE_DEFAULT_PLUGINS='1',
        OPENCODE_DISABLE_CLAUDE_CODE='1', OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_DISABLE_LSP_DOWNLOAD='1',
        OPENCODE_DISABLE_TERMINAL_TITLE='1')
    return env


def install(args):
    binary = shutil.which('opencode')
    if not binary or subprocess.check_output([binary,'--version'],text=True).strip() != '1.19.4':
        raise RuntimeFault('OPENCODE_VERSION_REQUIRES_1_19_4')
    settings = load_native_settings(args.config, args.env_file)
    project = Path(settings['workspace']).resolve()
    target_directory = Path.home() / '.config/opencode' if getattr(args, 'global_install', False) else Path(args.project).resolve()
    if not getattr(args, 'global_install', False) and target_directory != project:
        raise RuntimeFault('PROJECT_WORKSPACE_MISMATCH')
    target_directory.mkdir(parents=True, exist_ok=True)
    private = Path(settings['state_directory']) / 'native-project'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = private / 'application.json'
    native_settings = dict(settings)
    native_settings['opencode'] = dict(settings.get('opencode', {}), protected_paths=[str(project/'opencode.json'),
        str(project/'opencode.jsonc'), str(project/'tui.json'), str(project/'tui.jsonc'), str(project/'.opencode')])
    private_write(config, canonical(native_settings))
    assets = Path(__file__).parent / 'opencode_plugin'
    options = {'python': sys.executable, 'sdk': find_sdk(), 'config': str(config),
        'envFile': str(Path(args.env_file).resolve()) if args.env_file else None,
        'discoveryDirectory': str(private / 'connections')}
    model = settings['agent']['model']
    value = {'$schema': 'https://opencode.ai/config.json', 'default_agent': 'agentguard',
        'model': 'agentguard/' + model, 'small_model': 'agentguard/' + model, 'enabled_providers': ['agentguard'],
        'provider': {'agentguard': {'npm': '@ai-sdk/openai-compatible', 'name': 'AgentGuard Native',
            'options': {'baseURL': 'http://127.0.0.1:1/v1', 'apiKey': 'BOUNDARY_PLUGIN_REQUIRED', 'timeout': 600000},
            'models': {model: {'name': model, 'limit': {'context': 131072, 'output': 16384}}}}},
        'plugin': [[(assets/'server.mjs').as_uri(), options]],
        'permission': {'*': 'deny', 'ag_*': 'allow'}, 'tools': {'*': False, 'ag_*': True},
        'agent': {'agentguard': {'mode': 'primary', 'description': 'AgentGuard 原生防护开关',
            'permission': {'*': 'deny', 'ag_*': 'allow'}, 'tools': {'*': False, 'ag_*': True}},
            'build': {'disable': True}, 'plan': {'disable': True}, 'general': {'disable': True}, 'explore': {'disable': True}},
        'mcp': {}, 'share': 'disabled', 'snapshot': False, 'autoupdate': False,
        'compaction': {'auto': False, 'prune': False}}
    for name in ('opencode.json', 'tui.json'):
        target = target_directory/name
        if target.exists():
            private_write(private/(name+'.backup-'+str(time.time_ns())),target.read_text())
    private_write(target_directory/'opencode.json', json.dumps(value,ensure_ascii=False,indent=2))
    private_write(target_directory/'tui.json', json.dumps({'$schema':'https://opencode.ai/tui.json', 'theme':'opencode',
        'plugin':[[(assets/'tui.tsx').as_uri(),{'discoveryDirectory':options['discoveryDirectory']}]]},ensure_ascii=False,indent=2))
    print(canonical({'status':'INSTALLED','project':str(project),'configuration':str(target_directory),'default':'on','per_window_boundary':True}))


def attach(run, task=None):
    run = Path(run).resolve()
    info = strict_loads((run / 'window.json').read_text())
    command = [info['binary']]
    if task:
        command.extend(['run', '--format', 'json', '--agent', 'agentguard', '--dir', info['workspace'], task])
    else:
        command.extend([info['workspace'], '--agent', 'agentguard'])
    return subprocess.call(command, env=child_environment(run), cwd=info['workspace'])


def main():
    parser = argparse.ArgumentParser(description='OpenCode native AgentGuard switch (1.19.4)')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('launch', 'serve'):
        p = sub.add_parser(name)
        p.add_argument('--config', required=True)
        p.add_argument('--env-file')
        p.add_argument('--mode', choices=('on', 'off'), default='on')
        if name == 'launch':
            p.add_argument('--task', help='Run one native OpenCode task and emit actual JSON events')
        else:
            p.add_argument('--run-directory', required=True)
            p.add_argument('--parent-pid', type=int)
    p = sub.add_parser('attach')
    p.add_argument('--run-directory', required=True)
    p.add_argument('--task')
    p = sub.add_parser('install')
    p.add_argument('--config', required=True)
    p.add_argument('--env-file')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--project')
    target.add_argument('--global', dest='global_install', action='store_true', help='Show the switch in every OpenCode window, bound to the configured project workspace')
    args = parser.parse_args()
    try:
        if args.action == 'serve':
            serve(args)
        elif args.action == 'attach':
            raise SystemExit(attach(args.run_directory, args.task))
        elif args.action == 'install':
            install(args)
        else:
            run, service = prepare(args.config, args.env_file, args.mode)
            print('AgentGuard native window: ' + str(run), file=sys.stderr)
            try:
                raise SystemExit(attach(run, args.task))
            finally:
                service.terminate()
                service.wait(timeout=15)
    except Exception as exc:
        code = exc.code if isinstance(exc, RuntimeFault) else 'NATIVE_INTEGRATION_FAILED'
        print(canonical({'status': 'ERROR', 'reason_code': code}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
