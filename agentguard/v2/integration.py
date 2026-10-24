"""Shared integration capabilities and offline deployment checks.

Checks never import a configured tool factory, compose a Runtime, create a vault,
install dependencies or call a model. Protocol verification belongs to doctor.
"""
from importlib.util import find_spec
import os
from pathlib import Path
import re
import shutil
import sys


PROFILES = {
    'opencode-native': dict(name='OpenCode 原生', model_boundary=True, tools=True,
        final_display=True, recovery=True, switch=True, verified='OpenCode 1.19.4 / 本机 Linux',
        scope='受管 provider、注册工具和 FINAL；每窗口独立服务；未注册原生工具/委派不开放'),
    'opencode': dict(name='OpenCode MCP', model_boundary=False, tools=True,
        final_display=False, recovery=False, switch=False, verified='OpenCode 1.19.4 / 本机 Linux',
        scope='任务绑定的 MCP 工具；run-opencode 启动器额外强制最终显示门禁'),
    'mcp': dict(name='标准 MCP', model_boundary=False, tools=True,
        final_display=False, recovery=False, switch=False, verified='标准 stdio 协议已测；其他客户端待实机验收',
        scope='仅注册工具和显式提交的 guard_finalize；每任务独立连接，宿主关闭旁路'),
    'vscode': dict(name='VS Code MCP 模板', model_boundary=False, tools=True,
        final_display=False, recovery=False, switch=False, verified='配置模板；客户端待实机验收',
        scope='servers/stdio 配置；能力与标准 MCP 相同'),
    'runtime': dict(name='RuntimePort / HostAdapter', model_boundary=True, tools=True,
        final_display=True, recovery=True, switch=False, verified='Python / HTTP / 框架适配回归',
        scope='宿主接管 prepare → record → execute → finalize 和恢复 ACK；自定义宿主须验收清空能力'),
}


def capability_rows():
    return [{'接入方式': p['name'], '模型输入边界': p['model_boundary'], '注册工具': p['tools'],
        '最终显示门禁': p['final_display'], '恢复握手': p['recovery'], '防护开关': p['switch'],
        '验证范围': p['verified']} for p in PROFILES.values()]


def initialize(args):
    """Create a complete starter without relying on source-tree examples."""
    import json
    from .connect import outside_tools, private_write
    workspace, directory = Path(args.workspace).resolve(), Path(args.directory).resolve()
    if not workspace.is_dir():
        raise ValueError('Workspace must already exist')
    outside_tools({'workspace': str(workspace)}, directory)
    settings = {'workspace': str(workspace), 'state_directory': str(directory/'state'),
        'network_destinations': [], 'max_calls': 100, 'max_attempts': 200,
        'max_review_bytes': 512000, 'confirmed_authorizations': [],
        **{role: {'provider': 'deepseek', 'endpoint': 'https://api.deepseek.com/chat/completions',
            'model': 'deepseek-flash', 'api_key_env': 'AGENTGUARD_'+role.upper()+'_API_KEY',
            'destination_id': role, 'thinking': 'disabled', 'compact_protocol': True}
            for role in ('agent','guard')}}
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    private_write(directory/'application.json', json.dumps(settings, ensure_ascii=False, indent=2)+'\n')
    private_write(directory/'task.txt', '请列出 '+str(workspace)+' 中的文件，不修改文件，不访问其他路径。\n')
    private_write(directory/'credentials.env.example',
        "# Copy to a private credentials.env and fill literal values.\n"
        "# AGENTGUARD_AGENT_API_KEY='your-agent-key'\n"
        "# AGENTGUARD_GUARD_API_KEY='your-guard-key'\n")
    return {'status': 'INITIALIZED', 'directory': str(directory), 'cloud_calls': 0,
        'next': 'Configure model IDs and credentials, then run check / bundle / doctor or native launch'}


def preflight(args):
    from .application import load_settings
    from .connect import outside_tools, load_environment
    from .review.clients import CloudConfig
    checks = []

    def add(name, ok, reason):
        checks.append({'check': name, 'status': 'PASS' if ok else 'FAIL', 'reason_code': reason})

    client = getattr(args, 'client', 'mcp')
    if client not in PROFILES:
        raise ValueError('Unknown integration profile')
    add('python', sys.version_info >= (3, 12), 'PYTHON_3_12_REQUIRED')
    try:
        settings = load_settings(args.config)
        outside_tools(settings, args.config, getattr(args, 'env_file', None), getattr(args, 'task_file', None))
    except Exception:
        add('configuration', False, 'INVALID_CONFIGURATION_OR_PRIVATE_PATH_LAYOUT')
        return {'status': 'NEEDS_CONFIGURATION', 'client': client, 'checks': checks, 'cloud_calls': 0}
    add('configuration', True, 'PRIVATE_PATH_LAYOUT_VALID')
    add('workspace', Path(settings['workspace']).is_dir(), 'WORKSPACE_DIRECTORY_REQUIRED')
    try:
        allowed = {settings[k]['api_key_env'] for k in ('agent', 'guard')}
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in allowed):
            raise ValueError()
        load_environment(getattr(args, 'env_file', None), allowed | {'AGENTGUARD_V2_CLOUD_ENABLED'})
        add('environment_file', True, 'LITERAL_ENVIRONMENT_VALID')
    except Exception:
        add('environment_file', False, 'INVALID_LITERAL_ENVIRONMENT_OR_KEY_NAME')
    for role in ('agent', 'guard'):
        try:
            config = dict(settings[role])
            key = config.pop('api_key_env')
            # Validate endpoint/options even when credentials have not been set.
            CloudConfig(**config, api_key=os.environ.get(key) or 'preflight-placeholder', enabled=True)
            add(role + '_model', True, 'MODEL_CONFIGURATION_VALID')
            add(role + '_credential', bool(os.environ.get(key, '').strip()), 'MODEL_CREDENTIAL_REQUIRED')
        except Exception:
            add(role + '_model', False, 'INVALID_MODEL_CONFIGURATION')
    add('model_identities', settings.get('agent', {}).get('destination_id', 'guard_cloud') !=
        settings.get('guard', {}).get('destination_id', 'guard_cloud'), 'DISTINCT_MODEL_DESTINATIONS_REQUIRED')
    if client in ('opencode', 'mcp', 'vscode'):
        try:
            task = Path(args.task_file).read_text(encoding='utf-8').strip()
            add('trusted_task', bool(task), 'NONEMPTY_TRUSTED_TASK_FILE_REQUIRED')
        except Exception:
            add('trusted_task', False, 'NONEMPTY_TRUSTED_TASK_FILE_REQUIRED')
        add('mcp_dependency', find_spec('mcp') is not None, 'INSTALL_CONNECT_EXTRA')
    elif client == 'opencode-native':
        for package in ('fastapi', 'uvicorn', 'httpx'):
            add(package + '_dependency', find_spec(package) is not None, 'INSTALL_OPENCODE_EXTRA')
    if client.startswith('opencode'):
        add('opencode_binary', shutil.which('opencode') is not None, 'OPENCODE_BINARY_REQUIRED')
    warnings = ['OFFLINE_CHECK_ONLY; run doctor for MCP handshake; native launch verifies OpenCode 1.19.4']
    if settings.get('tool_factory'):
        warnings.append('CUSTOM_TOOL_FACTORY_NOT_IMPORTED; validate its catalog with doctor/native launch')
    return {'status': 'CONFIGURATION_READY' if all(c['status'] == 'PASS' for c in checks) else 'NEEDS_CONFIGURATION',
        'client': client, 'checks': checks, 'warnings': warnings, 'cloud_calls': 0,
        'cloud_opt_in': bool(getattr(args, 'enable_cloud', False) or os.environ.get('AGENTGUARD_V2_CLOUD_ENABLED') == '1'),
        'capabilities': PROFILES[client]}


def export_bundle(args):
    """Create a new, machine-local handoff directory; no secrets or task copies."""
    import hashlib
    import json
    from importlib.metadata import version
    from .connect import settings_for, outside_tools, command_for, client_config, private_write
    settings = settings_for(args)
    output = Path(args.output).resolve()
    outside_tools(settings, output)
    # Do not materialize a bundle that cannot be parsed. Missing keys are allowed
    # in a handoff, but malformed paths, tasks and model options are not.
    report = preflight(args)
    failures = [c for c in report['checks'] if c['status'] == 'FAIL'
        and not c['check'].endswith(('_credential', '_dependency', '_binary'))]
    if failures:
        raise ValueError('Invalid integration configuration; run check')
    if args.client == 'opencode-native':
        command = [sys.executable, '-I', '-m', 'agentguard.v2.opencode', 'launch',
            '--config', str(Path(args.config).resolve())]
        if args.env_file:
            command += ['--env-file', str(Path(args.env_file).resolve())]
        content = {'launch': command}
    else:
        content = client_config(args.client, command_for(args))
    files = {'client.json': json.dumps(content, ensure_ascii=False, indent=2) + '\n',
        'check.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        'README.md': '# AgentGuard 接入配置包\n\n'
            '这是绑定本机绝对路径和 Python 环境的配置包，跨机器需重新生成。\n\n'
            + PROFILES[args.client]['scope'] + '\n\n'
            '1. 检查 check.json 的失败项，配置包没有复制密钥、任务正文或运行时数据库。\n'
            '2. OpenCode 原生：client.json 的 launch 是参数数组，用它启动既有原生服务/插件。\n'
            '   MCP：将 client.json 合并到宿主配置；先运行 agentguard-v2-connect doctor 做实际握手。\n'
            '3. 关闭宿主的额外工具、provider 和委派旁路，每任务独立连接。\n'
            '4. 分别验收正常任务、越界拒绝、FINAL 拒绝、断线后不自动重放。\n'
            '5. 标准 MCP 不控制宿主模型输入或直接显示；完整保护使用原生适配或 RuntimePort。\n'}
    manifest = {'format': 'agentguard-integration-bundle-1', 'version': version('agentguard'),
        'client': args.client, 'portable': False, 'capabilities': PROFILES[args.client],
        'sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, text in files.items():
        private_write(output / name, text)
    private_write(output / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'status': 'EXPORTED', 'output': str(output), 'client': args.client,
        'configuration_status': report['status'], 'cloud_calls': 0}
