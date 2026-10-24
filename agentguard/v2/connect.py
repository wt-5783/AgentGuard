"""Installable, task-bound MCP connector and OpenCode launcher. No source paths."""
import argparse
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from .common import RuntimeFault, canonical, strict_loads


def load_environment(path, allowed):
    """Read literal assignments only; never source a shell script or execute it."""
    if path is None:
        return
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, separator, value = line.partition('=')
        if key not in allowed:
            continue
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('Invalid environment assignment')
        parts = shlex.split(value, comments=True)
        if len(parts) != 1 or '$' in value or '`' in value:
            raise ValueError('Environment file must use literal values')
        os.environ.setdefault(key, parts[0])


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def outside_tools(settings, *paths):
    roots = [Path(settings['workspace']).resolve()]
    roots.extend(Path(r['root']).resolve() for r in settings.get('resources', []) if r.get('kind') == 'path')
    for path in paths:
        if path and any(Path(path).resolve().is_relative_to(root) for root in roots):
            raise ValueError('Keep configuration, credentials and trusted task outside tool roots')


def settings_for(args):
    from .application import load_settings
    settings = load_settings(args.config)
    outside_tools(settings, args.config, getattr(args, 'task_file', None), args.env_file)
    allowed = {settings[k]['api_key_env'] for k in ('guard', 'agent')}
    allowed.add('AGENTGUARD_V2_CLOUD_ENABLED')
    load_environment(args.env_file, allowed)
    if args.enable_cloud:
        os.environ['AGENTGUARD_V2_CLOUD_ENABLED'] = '1'
    return settings


def command_for(args):
    # Ignore cwd/PYTHONPATH so a project-local agentguard module cannot shadow
    # the installed runtime, including this workspace's old compatibility copy.
    command = [sys.executable, '-I', '-m', 'agentguard.v2.connect', 'serve',
        '--config', str(Path(args.config).resolve()), '--task-file', str(Path(args.task_file).resolve())]
    if args.env_file:
        command.extend(['--env-file', str(Path(args.env_file).resolve())])
    if args.enable_cloud:
        command.append('--enable-cloud')
    return command


def client_config(client, command):
    if client == 'opencode':
        return {'$schema': 'https://opencode.ai/config.json',
            'default_agent': 'agentguard',
            'mcp': {'agentguard': {'type': 'local', 'command': command, 'enabled': True, 'timeout': 360000}},
            'agent': {'agentguard': {'description': 'AgentGuard task-bound tools', 'mode': 'primary',
                'permission': {'*': 'deny', 'agentguard_*': 'allow'},
                'tools': {'*': False, 'agentguard_*': True},
                'prompt': 'Use only agentguard MCP tools. Use absolute paths. Work only on the host-authorized task. '
                    'Submit the final text to agentguard_guard_finalize and return only its released content. '
                    'If blocked or terminal, stop and report the reason; do not retry with other tools.'}}}
    value = {'command': command[0], 'args': command[1:]}
    if client == 'vscode':
        return {'servers': {'agentguard': {'type': 'stdio', **value}}}
    return {'mcpServers': {'agentguard': value}}


def merge_config(existing, addition):
    """Preserve unrelated providers, agents and MCP servers. Refuse collisions."""
    result = json.loads(json.dumps(existing))
    for key, value in addition.items():
        if key == '$schema':
            result.setdefault(key, value)
        elif key == 'default_agent':
            # Respect an explicit existing default; fresh configurations use
            # the guarded agent without a separate --agent selection.
            result.setdefault(key, value)
        else:
            target = result.setdefault(key, {})
            if not isinstance(target, dict):
                raise ValueError('Incompatible client configuration')
            for name, entry in value.items():
                if name in target and target[name] != entry:
                    raise ValueError('Existing agentguard entry differs; export to a new file and compare')
                target[name] = entry
    return result


async def serve(args):
    from .application import compose
    from .adapters.external import ExternalAgentBridge, create_external_server
    from .state.ownership import exclusive_worker
    from mcp.server.stdio import stdio_server
    settings = settings_for(args)
    task = Path(args.task_file).read_text(encoding='utf-8').strip()
    if not task:
        raise ValueError('Trusted task must not be empty')
    # Independent connection, independent vault. Never share the web worker DB.
    parent = Path(settings['state_directory']) / 'external'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix='task-', dir=parent))
    settings['state_directory'] = str(directory)
    with exclusive_worker(directory):
        app = compose(settings, task)
        bridge = ExternalAgentBridge(app.runtime, app.request,
            model_destination_id=settings['agent']['destination_id'],
            on_change=lambda status: private_write(directory / 'connection.json', canonical(status)),
            on_final=lambda value: private_write(directory / 'final-output.json', canonical(value)))
        bridge.changed()
        server = create_external_server(bridge)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


async def doctor(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    settings_for(args)
    command = command_for(args)
    async with asyncio.timeout(30):
        async with stdio_client(StdioServerParameters(command=command[0], args=command[1:])) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                status = await session.call_tool('guard_status', {})
                if status.isError:
                    raise RuntimeFault('MCP_DOCTOR_FAILED')
                return {'status': 'READY', 'version': version('agentguard'),
                    'tools': [t.name for t in catalog.tools], 'cloud_calls': 0,
                    'scope': 'MCP tools; external model context/final text require host integration'}


def run_opencode(args):
    settings = settings_for(args)
    if args.timeout <= 0:
        raise ValueError('Timeout must be positive')
    binary = shutil.which('opencode')
    if not binary:
        raise ValueError('opencode is not installed')
    directory = Path(settings['state_directory']) / 'opencode'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix='run-', dir=directory))
    args.task_file = str(run / 'task.txt')
    private_write(args.task_file, args.task)
    settings['state_directory'] = str(run / 'runtime')
    args.config = str(run / 'application.json')
    private_write(args.config, json.dumps(settings, ensure_ascii=False, indent=2))
    config = client_config('opencode', command_for(args))
    config.update(default_agent='agentguard', permission={'*': 'deny', 'agentguard_*': 'allow'},
        tools={'*': False, 'agentguard_*': True}, snapshot=False, share='disabled', autoupdate=False)
    model = settings['agent']
    provider = model['provider']
    model_id = model['model']
    config['model'] = config['small_model'] = provider + '/' + model_id
    config['enabled_providers'] = [provider]
    config['provider'] = {provider: {'options': {'apiKey': '{env:' + model['api_key_env'] + '}',
        'baseURL': model['endpoint'].removesuffix('/chat/completions')},
        'models': {model_id: {'name': model_id, 'limit': {'context': 131072, 'output': 8192},
            'options': {'thinking': {'type': model.get('thinking', 'disabled')}}}}}}
    if provider not in ('deepseek', 'openai', 'anthropic'):
        config['provider'][provider]['npm'] = '@ai-sdk/openai-compatible'
    private_write(run / 'opencode.json', json.dumps(config, ensure_ascii=False, indent=2))
    env = dict(os.environ)
    # Launch in a private config context to avoid inherited permissions/plugins.
    for key in list(env):
        if key.startswith('OPENCODE_'):
            del env[key]
    for name in ('CONFIG', 'DATA', 'CACHE', 'STATE'):
        env['XDG_' + name + '_HOME'] = str(run / ('xdg-' + name.lower()))
    env.update(OPENCODE_CONFIG=str(run / 'opencode.json'), OPENCODE_CONFIG_DIR=str(run / 'config'),
        OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_DISABLE_AUTOUPDATE='1',
        OPENCODE_DISABLE_DEFAULT_PLUGINS='1', OPENCODE_DISABLE_CLAUDE_CODE='1',
        OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_DISABLE_LSP_DOWNLOAD='1')
    command = [binary, 'run', '--pure', '--agent', 'agentguard', '--dir', settings['workspace'],
        '--format', 'json', args.task]
    print('AgentGuard run: ' + str(run), file=sys.stderr)
    # Buffer external model output. Only the private Runtime FINAL release may
    # reach stdout; skipping the final tool cannot bypass this launcher gate.
    with (run / 'events.jsonl').open('w', encoding='utf-8') as output, (run / 'stderr.log').open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=errors,
            start_new_session=os.name != 'nt')
        try:
            code = process.wait(timeout=args.timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
            code = 124
    finals = list((run / 'runtime' / 'external').glob('task-*/final-output.json'))
    if code != 0 or len(finals) != 1:
        connections = list((run / 'runtime' / 'external').glob('task-*/connection.json'))
        reason = None
        if len(connections) == 1:
            reason = strict_loads(connections[0].read_text(encoding='utf-8')).get('last_reason_code')
        print(canonical({'status': 'FAILED', 'reason_code': 'OPENCODE_FINAL_NOT_RELEASED',
            'guard_reason_code': reason, 'exit_code': code, 'run_directory': str(run)}))
        return 1
    final = strict_loads(finals[0].read_text(encoding='utf-8'))
    if final.get('status') != 'RELEASED':
        raise RuntimeFault('OPENCODE_FINAL_NOT_RELEASED')
    print(canonical(final) if args.json else final['content'] if isinstance(final['content'], str) else canonical(final['content']))
    return 0


def main():
    parser = argparse.ArgumentParser(description='Task-bound AgentGuard MCP / OpenCode connector')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('serve', 'doctor', 'export', 'run-opencode'):
        p = sub.add_parser(name)
        p.add_argument('--config', required=True, help='Trusted application JSON, outside tool roots')
        p.add_argument('--env-file', help='Private literal KEY=value file; no shell execution')
        p.add_argument('--enable-cloud', action='store_true', help='Explicitly enable configured model API calls')
        if name == 'run-opencode':
            p.add_argument('--task', required=True, help='New host-authorized task; starts a fresh OpenCode chat')
            p.add_argument('--json', action='store_true', help='Print the released final result as JSON')
            p.add_argument('--timeout', type=float, default=600, help='Task timeout in seconds (default: 600)')
        else:
            p.add_argument('--task-file', required=True, help='Trusted task text file, outside tool roots')
        if name == 'export':
            p.add_argument('--client', choices=('opencode', 'mcp', 'vscode'), default='opencode')
            p.add_argument('--output', required=True, help='JSON file; merges unrelated entries, refuses collisions/JSONC')
    args = parser.parse_args()
    try:
        if args.action == 'serve':
            asyncio.run(serve(args))
        elif args.action == 'doctor':
            print(canonical(asyncio.run(doctor(args))))
        elif args.action == 'export':
            settings_for(args)
            output = Path(args.output)
            existing = strict_loads(output.read_text(encoding='utf-8')) if output.exists() else {}
            result = merge_config(existing, client_config(args.client, command_for(args)))
            private_write(output, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(canonical({'status': 'CONFIGURED', 'client': args.client, 'output': str(output.resolve())}))
        else:
            raise SystemExit(run_opencode(args))
    except Exception as exc:
        code = exc.code if isinstance(exc, RuntimeFault) else 'CONNECTOR_CONFIGURATION_OR_SERVICE_ERROR'
        # Do not print exception strings: they can contain credentials or bodies.
        print(canonical({'status': 'ERROR', 'reason_code': code}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
