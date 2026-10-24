"""External agents use ordinary tools; all effects still pass the real Runtime."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentguard.v2.adapters.external import ExternalAgentBridge, create_external_server
from agentguard.v2.connect import (client_config, command_for, load_environment,
    merge_config, outside_tools, private_write, run_opencode)


def bridge(harness):
    return ExternalAgentBridge(harness.runtime, harness.request, model_destination_id='agent')


@pytest.mark.asyncio
async def test_real_mcp_effects_and_submitted_final_gate(harness):
    h = harness
    b = bridge(h)
    async with create_connected_server_and_client_session(create_external_server(b)) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        assert 'read_document' in names and 'guard_finalize' in names
        assert b.session_id is None  # Listing/status must not create a policy/model call.
        assert not (await client.call_tool('guard_status', {})).isError
        read = await client.call_tool('read_document', {'resource': 'docs'})
        assert read.structuredContent['content'] == 'document:docs'
        write = await client.call_tool('write_report', {'resource': 'reports', 'body': 'summary'})
        assert not write.isError and h.effects == [('write', 'reports', 'summary')]
        final = await client.call_tool('guard_finalize', {'text': 'summary'})
        assert final.structuredContent['content'] == 'summary'
        assert any(m.phase == 'FINAL' for m in h.guard.materials)
        later = await client.call_tool('write_report', {'resource': 'reports', 'body': 'again'})
        assert later.isError and len(h.effects) == 1
        assert len(b.messages) == 2


@pytest.mark.asyncio
async def test_result_quarantine_and_terminal_prevents_next_effect(harness):
    h = harness
    h.guard.mode = 'BLOCK'
    b = bridge(h)
    result = await b.call('read_document', {'resource': 'docs'})
    assert result.isError and result.structuredContent['content'] is None
    assert 'document:docs' not in str(result)
    assert b.terminal == 'MCP_NEW_TASK_REQUIRED'
    await b.call('write_report', {'resource': 'reports', 'body': 'never'})
    assert h.effects == []


@pytest.mark.asyncio
async def test_ambiguous_effect_failure_never_replayed(harness):
    h = harness
    def uncertain(resource, body):
        h.effects.append(('committed', resource))
        raise ValueError('PRIVATE_BACKEND_FAILURE')
    entry = h.runtime.registry.get('write_report')
    h.runtime.registry.register(entry.manifest.model_copy(update={'manifest_version': 'v2'}), uncertain)
    h.request = h.request.model_copy(update={'tool_manifests': h.runtime.registry.manifests()})
    b = bridge(h)
    first = await b.call('write_report', {'resource': 'reports', 'body': 'test'})
    assert first.structuredContent['status'] == 'UNKNOWN'
    assert 'PRIVATE_BACKEND_FAILURE' not in str(first)
    await b.call('write_report', {'resource': 'reports', 'body': 'test'})
    assert len(h.effects) == 1


@pytest.mark.asyncio
async def test_validation_and_client_binding_do_not_dispatch(harness):
    b = bridge(harness)
    for name, args in [('read_document', {'resource': 12, 'private': 'SECRET'}),
                       ('guard_finalize', {'text': 'SECRET', 'extra': True}), ('missing', {})]:
        result = await b.call(name, args, client_id='first')
        assert result.isError and 'SECRET' not in str(result)
    assert b.session_id is None
    mismatch = await b.call('read_document', {'resource': 'docs'}, client_id='second')
    assert mismatch.isError and harness.reads == []
    missing_meta = await b.call('read_document', {'resource': 'docs'})
    assert missing_meta.isError


@pytest.mark.asyncio
async def test_parallel_calls_are_serialized_and_keep_all_observations(harness):
    b = bridge(harness)
    values = await asyncio.gather(*(b.call('read_document', {'resource': 'docs'}) for _ in range(3)))
    assert all(not v.isError for v in values)
    assert len(b.messages) == 3


@pytest.mark.asyncio
async def test_real_mcp_metadata_rejects_another_chat(harness):
    b = bridge(harness)
    async with create_connected_server_and_client_session(create_external_server(b)) as client:
        assert not (await client.call_tool('guard_status', {}, meta={'ai.opencode/sessionID': 'first'})).isError
        result = await client.call_tool('read_document', {'resource': 'docs'}, meta={'ai.opencode/sessionID': 'second'})
        assert result.isError and result.structuredContent['reason_code'] == 'MCP_CLIENT_SESSION_MISMATCH'
        assert harness.reads == [] and b.session_id is None


@pytest.mark.asyncio
async def test_rejected_final_is_never_published_or_followed_by_effects(harness):
    harness.guard.mode, harness.guard.phase = 'BLOCK', 'FINAL'
    released = []
    b = ExternalAgentBridge(harness.runtime, harness.request, model_destination_id='agent', on_final=released.append)
    result = await b.call('guard_finalize', {'text': 'UNRELEASED_PRIVATE_FINAL'})
    assert result.isError and result.structuredContent['content'] is None
    assert 'UNRELEASED_PRIVATE_FINAL' not in str(result) and released == []
    assert (await b.call('read_document', {'resource': 'docs'})).isError
    assert harness.reads == []


def test_config_export_preserves_unrelated_entries_and_refuses_collision(tmp_path):
    args = SimpleNamespace(config=tmp_path/'配置 file.json', task_file=tmp_path/'task file.txt', env_file=None, enable_cloud=False)
    command = command_for(args)
    assert command[-1] == str(args.task_file)
    config = client_config('opencode', command)
    existing = {'provider': {'custom': {}}, 'agent': {'build': {'temperature': 0.2}},
        'mcp': {'other': {'enabled': False}}}
    result = merge_config(existing, config)
    assert result['provider'] == existing['provider']
    assert result['agent']['build'] == existing['agent']['build']
    assert result['default_agent'] == 'agentguard'
    assert merge_config({'default_agent': 'build'}, config)['default_agent'] == 'build'
    assert result['agent']['agentguard']['permission'] == {'*': 'deny', 'agentguard_*': 'allow'}
    assert result['mcp']['other'] == existing['mcp']['other']
    assert merge_config(result, config) == result
    with pytest.raises(ValueError):
        merge_config({'mcp': {'agentguard': {}}}, config)
    assert client_config('mcp', command)['mcpServers']['agentguard']['command'] == command[0]
    assert client_config('vscode', command)['servers']['agentguard']['type'] == 'stdio'


def test_private_settings_and_env_file_never_execute_shell(tmp_path, monkeypatch):
    root = tmp_path/'workspace'
    root.mkdir()
    marker = tmp_path/'executed'
    env = tmp_path/'live.env'
    env.write_text(f'TEST_AGENT_KEY=$(touch {marker})\n')
    with pytest.raises(ValueError):
        load_environment(env, {'TEST_AGENT_KEY'})
    assert not marker.exists()
    env.write_text("export TEST_AGENT_KEY='literal-secret'\nUNRELATED=ignored\n")
    monkeypatch.delenv('TEST_AGENT_KEY', raising=False)
    load_environment(env, {'TEST_AGENT_KEY'})
    assert os.environ['TEST_AGENT_KEY'] == 'literal-secret'
    with pytest.raises(ValueError):
        outside_tools({'workspace': str(root)}, root/'task.txt')
    path = tmp_path/'private'/'config.json'
    private_write(path, json.dumps({'key_env': 'TEST_AGENT_KEY'}))
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    assert 'literal-secret' not in path.read_text()


@pytest.mark.parametrize('released', [False, True])
def test_launcher_never_displays_unchecked_external_output(tmp_path, monkeypatch, capsys, released):
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    config = tmp_path/'application.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'state_directory': str(tmp_path/'state'),
        'agent': {'provider': 'deepseek', 'model': 'test', 'endpoint': 'https://example.test/chat/completions',
            'api_key_env': 'TEST_AGENT_KEY'}, 'guard': {'api_key_env': 'TEST_GUARD_KEY'}}))
    # A real child process emits arbitrary text, with an optional private FINAL
    # record standing in for the already separately tested Runtime release.
    child = tmp_path/'opencode'
    child.write_text('#!' + sys.executable + '\n' + '''
import json, os
from pathlib import Path
cfg = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
assert cfg['agent']['agentguard']['permission']['*'] == 'deny'
command = cfg['mcp']['agentguard']['command']
app = json.loads(Path(command[command.index('--config') + 1]).read_text())
print('UNCHECKED_PRIVATE_MODEL_TEXT')
if os.environ.get('TEST_FINAL_RELEASE') == '1':
    path = Path(app['state_directory'])/'external'/'task-test'/'final-output.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'status':'RELEASED','content':'reviewed answer'}))
''')
    child.chmod(0o700)
    monkeypatch.setattr('agentguard.v2.connect.shutil.which', lambda _: str(child))
    monkeypatch.setenv('TEST_FINAL_RELEASE', '1' if released else '0')
    args = SimpleNamespace(config=str(config), env_file=None, enable_cloud=False,
        task='read a file', json=True, timeout=5)
    code = run_opencode(args)
    captured = capsys.readouterr()
    assert 'UNCHECKED_PRIVATE_MODEL_TEXT' not in captured.out
    assert code == (0 if released else 1)
    assert json.loads(captured.out)['status'] == ('RELEASED' if released else 'FAILED')
