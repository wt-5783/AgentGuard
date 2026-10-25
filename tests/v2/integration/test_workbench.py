"""Navigation, service credentials and deployment handoff boundaries."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from agentguard.v2 import console, services
from agentguard.v2.integration import preflight, export_bundle, initialize


def configuration(tmp_path, monkeypatch, client='mcp'):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    task = tmp_path / 'task.txt'
    task.write_text('PRIVATE_TASK_BODY')
    for key in ('CHECK_AGENT_KEY', 'CHECK_GUARD_KEY'):
        monkeypatch.setenv(key, 'PRIVATE_CREDENTIAL_' + key)
    config = tmp_path / 'application.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'state_directory': str(tmp_path/'vault'),
        **{role: {'model': 'test', 'api_key_env': 'CHECK_'+role.upper()+'_KEY', 'destination_id': role,
            'endpoint': 'https://example.test/chat/completions'} for role in ('agent','guard')}}))
    return SimpleNamespace(config=str(config), task_file=str(task), env_file=None,
        client=client, enable_cloud=False, output=str(tmp_path/'bundle'))


def test_preflight_never_composes_runtime_or_calls_model(tmp_path, monkeypatch):
    args = configuration(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError('offline check must not compose or send')
    monkeypatch.setattr('agentguard.v2.application.compose', forbidden)
    monkeypatch.setattr('agentguard.v2.review.clients.JsonHTTPTransport._post', forbidden)
    settings = json.loads(Path(args.config).read_text())
    settings['tool_factory'] = 'unimportable.trusted_factory:registry'
    Path(args.config).write_text(json.dumps(settings))
    result = preflight(args)
    assert result['status'] == 'CONFIGURATION_READY' and result['cloud_calls'] == 0
    assert 'CUSTOM_TOOL_FACTORY_NOT_IMPORTED' in str(result)
    assert not (tmp_path/'vault').exists()
    assert 'PRIVATE_CREDENTIAL_' not in json.dumps(result)


def test_preflight_reports_missing_keys_and_invalid_endpoint_without_values(tmp_path, monkeypatch):
    args = configuration(tmp_path, monkeypatch)
    monkeypatch.delenv('CHECK_AGENT_KEY')
    report = preflight(args)
    assert report['status'] == 'NEEDS_CONFIGURATION'
    assert any(c['check'] == 'agent_credential' and c['status'] == 'FAIL' for c in report['checks'])
    settings = json.loads(Path(args.config).read_text())
    settings['guard']['endpoint'] = 'https://user:PRIVATE_PASSWORD@example.test'
    Path(args.config).write_text(json.dumps(settings))
    report = preflight(args)
    assert any(c['check'] == 'guard_model' and c['status'] == 'FAIL' for c in report['checks'])
    assert 'PRIVATE_PASSWORD' not in json.dumps(report)


@pytest.mark.parametrize('client', ['mcp', 'vscode', 'opencode', 'opencode-native'])
def test_bundle_has_hashes_no_secrets_and_never_overwrites(tmp_path, monkeypatch, client):
    args = configuration(tmp_path, monkeypatch, client)
    export_bundle(args)
    root = Path(args.output)
    manifest = json.loads((root/'manifest.json').read_text())
    for name, expected in manifest['sha256'].items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest() == expected
    assert 'PRIVATE_CREDENTIAL_' not in ''.join(p.read_text() for p in root.iterdir())
    assert 'PRIVATE_TASK_BODY' not in ''.join(p.read_text() for p in root.iterdir())
    assert not manifest['portable']
    if os.name != 'nt':
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
    with pytest.raises(FileExistsError):
        export_bundle(args)


def test_bundle_refuses_trusted_files_inside_tools_and_empty_task(tmp_path, monkeypatch):
    args = configuration(tmp_path, monkeypatch)
    args.output = str(tmp_path/'workspace'/'bundle')
    with pytest.raises(ValueError):
        export_bundle(args)
    args.output = str(tmp_path/'bundle')
    Path(args.task_file).write_text(' ')
    with pytest.raises(ValueError):
        export_bundle(args)
    assert not Path(args.output).exists()


def test_navigation_is_independent_of_gateway_sessions_and_replay_is_readonly(monkeypatch, tmp_path):
    monkeypatch.delenv('AGENTGUARD_V2_REPLAY', raising=False)
    monkeypatch.delenv('AGENTGUARD_OPERATOR_TOKEN', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('non-audit pages must not query gateway')
    monkeypatch.setattr(console.ConsoleReader, 'get', forbidden)
    app = AppTest.from_file(str(Path(console.__file__))).run()
    assert not app.exception
    app.sidebar.radio[0].set_value('Agent 接入').run()
    assert not app.exception and any('RuntimePort' in m.value for m in app.markdown)
    app.sidebar.radio[0].set_value('项目架构').run()
    assert not app.exception and len(app.dataframe) == 1
    replay = tmp_path/'bad-record.json'
    replay.write_text('{}')
    monkeypatch.setenv('AGENTGUARD_V2_REPLAY', str(replay))
    replay_app = AppTest.from_file(str(Path(console.__file__))).run()
    assert not replay_app.exception and not replay_app.sidebar.radio
    assert not replay_app.get('form') and not replay_app.button


def test_installed_starter_requires_credentials_and_refuses_workspace_overlap(tmp_path, monkeypatch):
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    for key in ('AGENTGUARD_AGENT_API_KEY', 'AGENTGUARD_GUARD_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    args = SimpleNamespace(directory=str(tmp_path/'private'), workspace=str(workspace))
    assert initialize(args)['cloud_calls'] == 0
    check = SimpleNamespace(config=str(tmp_path/'private/application.json'), task_file=str(tmp_path/'private/task.txt'),
        env_file=None, client='mcp', enable_cloud=False)
    report = preflight(check)
    assert report['status'] == 'NEEDS_CONFIGURATION'
    assert any(c['check'] == 'agent_credential' and c['status'] == 'FAIL' for c in report['checks'])
    assert not (tmp_path/'private/state').exists()
    args.directory = str(workspace/'private')
    with pytest.raises(ValueError):
        initialize(args)


def test_evaluation_without_sessions_and_readonly_refresh(monkeypatch):
    calls = []
    class Client:
        token = None
        base = 'http://127.0.0.1:18510'
        def request(self, path, data=None):
            calls.append((path, data))
            assert data is None
            return {'/api/cases': [], '/api/runs': [], '/api/status': {'report': None}}[path]
    monkeypatch.setattr(services, 'evaluation_client', lambda: Client())
    app = AppTest.from_string('import streamlit as st\nfrom agentguard.v2.evaluation_console import render_workspace\nrender_workspace(st)').run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ['功能实演 · 录制视频', '测评报告', '历史场景对照']
    assert app.get('iframe')[0].proto.src == 'http://127.0.0.1:18510/?embed=1'
    assert app.button[1].disabled  # no evaluation submission credential
    app.button[0].click().run()
    assert not app.exception and all(data is None for _, data in calls)


def test_unavailable_live_evaluator_has_no_broken_embed(monkeypatch):
    class Client:
        base = 'http://127.0.0.1:18510'
        def request(self, *args, **kwargs):
            raise ConnectionError('offline')
    monkeypatch.setattr(services, 'evaluation_client', lambda: Client())
    app = AppTest.from_string('import streamlit as st\nfrom agentguard.v2.evaluation_console import render_workspace\nrender_workspace(st)').run()
    assert not app.exception and not app.get('iframe')
    assert any('实时对照服务未连接' in info.value for info in app.info)


@pytest.mark.parametrize('address', ['https://example.test:8766', 'http://example.test:8766',
    'http://user:secret@127.0.0.1:8766', 'http://127.0.0.1:8766/path', 'http://127.0.0.1'])
def test_local_service_rejects_untrusted_addresses(address):
    with pytest.raises(ValueError):
        services.LocalServiceClient(address)


def test_console_child_cannot_access_model_or_host_credentials():
    env = services.console_environment({'AGENT_KEY': 'secret', 'GUARD_KEY': 'secret',
        'AGENTGUARD_HOST_TOKEN': 'host', 'AGENTGUARD_UI_TOKEN': 'ui', 'AGENTGUARD_OPERATOR_TOKEN': 'operator'},
        {'agent': {'api_key_env': 'AGENT_KEY'}, 'guard': {'api_key_env': 'GUARD_KEY'}})
    assert set(env) == {'AGENTGUARD_UI_TOKEN', 'AGENTGUARD_OPERATOR_TOKEN'}


def test_local_service_never_forwards_credentials_on_redirect():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    visited = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            visited.append((self.path, self.headers.get('Authorization')))
            self.send_response(302 if self.path == '/start' else 200)
            self.send_header('Location', '/sink')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = services.LocalServiceClient('http://127.0.0.1:'+str(server.server_port), 'private-token')
        with pytest.raises(RuntimeError, match='HTTP 302'):
            client.request('/start')
        assert visited == [('/start', 'Bearer private-token')]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
