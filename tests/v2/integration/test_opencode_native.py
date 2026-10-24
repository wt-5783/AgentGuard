import json
from types import SimpleNamespace

import httpx
import pytest

from agentguard.v2.common import RuntimeFault
from agentguard.v2.opencode import create_native_app
from agentguard.v2.opencode_host import NativeController
from conftest import build_harness


class ScriptedNativeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.inputs = []

    async def complete(self, messages, tools):
        self.inputs.append(json.loads(json.dumps({'messages': messages, 'tools': tools})))
        return self.replies.pop(0), {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}


def call(name='read_document', args=None, content='PRIVATE_INTERMEDIATE_THOUGHT'):
    return {'content': content, 'tool_calls': [{'id': 'upstream-call', 'type': 'function',
        'function': {'name': 'ag_' + name, 'arguments': json.dumps(args or {'resource': 'docs'})}}]}


def setup(tmp_path, replies, *, mode='on', guard_mode='ALLOW', secret=False, recover=True, send=True, output_labels=None):
    h = build_harness(tmp_path/'vault', mode=guard_mode, secret=secret, send=send, output_labels=output_labels)
    upstream = ScriptedNativeModel(replies)
    def factory(settings, task):
        return SimpleNamespace(runtime=h.runtime, request=h.request.model_copy(update={'trusted_task': task}))
    settings = {'agent': {'model': 'fixture-native', 'provider': 'fixture', 'destination_id': 'agent', 'endpoint': 'https://agent.test'},
        'max_turns': 10, 'recover_on_block': recover}
    controller = NativeController(settings, tmp_path/'native', mode=mode, factory=factory,
        upstream_factory=lambda app: upstream)
    return h, upstream, controller


def payload(**changes):
    return {'model': 'fixture-native', 'messages': [{'role': 'user', 'content': 'FORGED_NATIVE_HISTORY'}],
        'tools': [], 'stream': True, **changes}


@pytest.mark.asyncio
async def test_native_real_runtime_receipts_and_final_no_raw_ui_stream(tmp_path):
    h, upstream, controller = setup(tmp_path, [call(), {'content': 'reviewed final answer'}])
    app = create_native_app(controller, 'native-host-token')
    headers = {'Authorization': 'Bearer native-host-token', 'x-agentguard-role': 'agentguard',
        'x-agentguard-session': 'ses_native', 'x-agentguard-request': 'request-001'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://native') as client:
        assert (await client.get('/status')).status_code == 401
        assert (await client.post('/task', headers=headers, json={'native_id': 'ses_native', 'task': 'Read docs and answer.'})).status_code == 200
        result = await client.post('/v1/chat/completions', headers=headers, json=payload())
        assert result.status_code == 200
        assert 'PRIVATE_INTERMEDIATE_THOUGHT' not in result.text and 'document:docs' not in result.text
        chunks = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: {')]
        receipt = json.loads(chunks[0]['choices'][0]['delta']['tool_calls'][0]['function']['arguments'])['receipt']
        r = await client.post('/receipt', headers=headers, json={'native_id': 'ses_native', 'tool': 'ag_read_document', 'receipt': receipt})
        assert r.json()['status'] == 'RELEASED' and 'document:docs' not in r.text
        headers['x-agentguard-request'] = 'request-002'
        final = await client.post('/v1/chat/completions', headers=headers, json=payload())
        assert 'reviewed final answer' in final.text
    assert 'FORGED_NATIVE_HISTORY' not in json.dumps(upstream.inputs)
    assert 'document:docs' in json.dumps(upstream.inputs[1])
    assert h.reads == ['docs']
    assert controller.status()['reviews'] == {'ACTION': 1, 'RESULT': 1, 'FINAL': 1}


@pytest.mark.asyncio
async def test_response_replay_never_repeats_effect_and_changed_request_rejected(tmp_path):
    h, _, controller = setup(tmp_path, [call('write_report', {'resource': 'reports', 'body': 'real body'})])
    await controller.submit('ses_replay', 'Write a report.')
    first = await controller.turn('ses_replay', 'same-request', payload())
    assert await controller.turn('ses_replay', 'same-request', payload()) == first
    assert h.effects == [('write', 'reports', 'real body')]
    assert 'real body' not in json.dumps(first)
    with pytest.raises(RuntimeFault, match='MODEL_REQUEST_CHANGED'):
        await controller.turn('ses_replay', 'same-request', payload(stream=False))
    receipt = next(iter(controller.get('ses_replay').receipts))
    with pytest.raises(RuntimeFault):
        controller.receipt('ses_other', 'ag_write_report', receipt)
    with pytest.raises(RuntimeFault, match='RECEIPT_BINDING_MISMATCH'):
        controller.receipt('ses_replay', 'ag_read_document', receipt)


@pytest.mark.asyncio
async def test_risk_result_recovery_actually_resets_context_and_records_ack(tmp_path):
    h, upstream, controller = setup(tmp_path, [call(), {'content': 'The source was blocked; no unsupported facts.'}], guard_mode='BLOCK')
    await controller.submit('ses_recover', 'Read docs and answer.')
    first = await controller.turn('ses_recover', 'request-01', payload())
    assert 'document:docs' not in json.dumps(first)
    state = controller.get('ses_recover')
    assert state.recoveries == 1
    events = h.runtime.store.events(state.runtime_id, limit=1000)
    assert any(e.event_type == 'RECOVERY_COMPLETED' for e in events)
    ack = h.runtime.store.all(state.runtime_id, 'recovery_ack')[0]
    assert not ack['unsupported'] and all(ack['completed_resets'].values())
    await controller.turn('ses_recover', 'request-02', payload())
    assert 'document:docs' not in json.dumps(upstream.inputs[-1])
    assert 'CONTEXT_REBUILT' in json.dumps(upstream.inputs[-1])


@pytest.mark.asyncio
async def test_redaction_only_transformed_version_reaches_next_model(tmp_path):
    h, upstream, controller = setup(tmp_path, [call(), {'content': 'Public report.'}], guard_mode='REDACT', secret=True)
    await controller.submit('ses_redact', 'Read docs and produce a public report.')
    await controller.turn('ses_redact', 'request-01', payload())
    await controller.turn('ses_redact', 'request-02', payload())
    assert controller.status()['redacted'] == 1
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(upstream.inputs[-1])


@pytest.mark.asyncio
async def test_on_off_same_model_tools_backend_and_different_host_enforcement(tmp_path):
    catalog = []
    model_inputs = []
    for mode in ('on', 'off'):
        h, upstream, controller = setup(tmp_path/mode, [call('send_report', {'destination': 'receiver', 'body': 'synthetic'})], mode=mode, send=False)
        await controller.submit('ses_compare', 'Send synthetic data to receiver.')
        await controller.turn('ses_compare', 'request-01', payload())
        catalog.append(upstream.inputs[0]['tools'])
        model_inputs.append(upstream.inputs[0]['messages'])
        assert len(h.effects) == (1 if mode == 'off' else 0)
    assert catalog[0] == catalog[1]
    assert model_inputs[0] == model_inputs[1]


@pytest.mark.asyncio
async def test_mode_snapshot_and_unknown_model_or_tool_fail_closed(tmp_path):
    h, _, controller = setup(tmp_path, [call('not_registered')])
    await controller.submit('ses_on', 'Read docs.')
    frozen=json.loads((controller.get('ses_on').directory/'trusted-task.json').read_text())
    assert frozen['trusted_task']=='Read docs.' and frozen['mode']=='on'
    await controller.switch('off')
    assert controller.get('ses_on').mode == 'on'
    await controller.submit('ses_off', 'Read docs.')
    assert controller.get('ses_off').mode == 'off'
    await controller.submit('ses_on', 'Another task in the original protected chat.')
    assert controller.get('ses_on').mode == 'on'
    with pytest.raises(RuntimeFault, match='MODEL_DESTINATION_DENIED'):
        await controller.turn('ses_on', 'request-01', payload(model='another-model'))
    with pytest.raises(RuntimeFault):
        await controller.turn('ses_on', 'request-02', payload())
    assert h.reads == [] and h.effects == []
    assert controller.get('ses_on').terminal == 'HOST_FAILED_CLOSED'


@pytest.mark.asyncio
async def test_final_rejection_never_streams_any_prefix(tmp_path):
    h, _, controller = setup(tmp_path, [{'content': 'PRIVATE_FINAL_NEVER_STREAM'}], guard_mode='BLOCK', recover=False)
    h.guard.phase = 'FINAL'
    await controller.submit('ses_final', 'Answer the task.')
    app = create_native_app(controller, 'native-host-token')
    headers = {'Authorization': 'Bearer native-host-token', 'x-agentguard-role': 'agentguard',
        'x-agentguard-session': 'ses_final', 'x-agentguard-request': 'request-01'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://native') as client:
        result = await client.post('/v1/chat/completions', headers=headers, json=payload())
        assert result.status_code == 400 and 'PRIVATE_FINAL_NEVER_STREAM' not in result.text
    assert controller.get('ses_final').phase == 'ERROR'


@pytest.mark.asyncio
async def test_unknown_effect_is_not_retried_or_exposed(tmp_path):
    h, _, controller = setup(tmp_path, [call('write_report', {'resource': 'reports', 'body': 'PRIVATE'})])
    def uncertain(resource, body):
        h.effects.append(('committed', resource))
        raise ValueError('PRIVATE_BACKEND_DIAGNOSTIC')
    entry = h.runtime.registry.get('write_report')
    h.runtime.registry.register(entry.manifest.model_copy(update={'manifest_version': 'v2'}), uncertain)
    h.request = h.request.model_copy(update={'tool_manifests': h.runtime.registry.manifests()})
    await controller.submit('ses_unknown', 'Write a report.')
    with pytest.raises(RuntimeFault, match='HOST_REVIEW_OR_EFFECT_PENDING'):
        await controller.turn('ses_unknown', 'request-01', payload())
    with pytest.raises(RuntimeFault, match='HOST_FAILED_CLOSED'):
        await controller.turn('ses_unknown', 'request-02', payload())
    assert len(h.effects) == 1
    assert 'PRIVATE_BACKEND_DIAGNOSTIC' not in json.dumps(controller.status())


@pytest.mark.asyncio
async def test_quota_is_enforced_before_native_backend_dispatch(tmp_path):
    h, _, controller = setup(tmp_path, [call()])
    policy = h.request.host_policy.model_copy(update={'rules': [r.model_copy(update={'max_calls': 0}) for r in h.request.host_policy.rules]})
    h.request = h.request.model_copy(update={'host_policy': policy})
    await controller.submit('ses_quota', 'Read docs.')
    result = await controller.turn('ses_quota', 'request-01', payload())
    assert h.reads == []
    assert controller.status()['blocked'] == 1 and controller.status()['executed'] == 0
    assert 'document:docs' not in json.dumps(result)


@pytest.mark.asyncio
async def test_labels_propagate_into_final_even_when_reviewer_allows(tmp_path):
    from agentguard.v2.state.lineage import public_labels
    labels = public_labels('external').model_copy(update={'confidentiality': ['private']})
    h, upstream, controller = setup(tmp_path, [call(), {'content': 'safe-looking but derived final'}],
        output_labels=labels, recover=False)
    await controller.submit('ses_labels', 'Read the private docs for analysis.')
    result = await controller.turn('ses_labels', 'request-01', payload())
    assert 'document:docs' not in json.dumps(result)
    with pytest.raises(RuntimeFault, match='HOST_FINAL_BLOCKED'):
        await controller.turn('ses_labels', 'request-02', payload())
    assert 'document:docs' in json.dumps(upstream.inputs[-1])
    assert not (controller.get('ses_labels').directory/'final.json').exists()


def test_host_switch_configuration_cannot_be_modified_by_workspace_tool(tmp_path):
    from agentguard.v2.application import workspace_tools
    from agentguard.v2.execution.executor import KnownToolFailure
    root=tmp_path/'workspace';root.mkdir()
    protected=root/'opencode.json';protected.write_text('host profile')
    registry=workspace_tools({'workspace':str(root),'opencode':{'protected_paths':[str(protected),str(root/'.opencode')]}})
    write=registry.get('write_text').handler
    with pytest.raises(KnownToolFailure,match='HOST_CONFIGURATION_PROTECTED'):
        write(str(protected),'disable protection')
    with pytest.raises(KnownToolFailure):
        write(str(root/'.opencode/plugins/disable.mjs'),'payload')
    assert protected.read_text()=='host profile'
    write(str(root/'report.txt'),'ordinary report')
    assert (root/'report.txt').read_text()=='ordinary report'


def test_install_native_project_profile_has_no_provider_secret_and_backs_up(tmp_path,monkeypatch,capsys):
    from agentguard.v2.opencode import install
    root=tmp_path/'workspace';root.mkdir()
    (root/'opencode.json').write_text('{"old_profile":true}')
    config=tmp_path/'settings.json'
    config.write_text(json.dumps({'workspace':str(root),'state_directory':str(tmp_path/'state'),
        'guard':{'api_key_env':'TEST_GUARD_KEY'},'agent':{'model':'fixture','api_key_env':'TEST_AGENT_KEY'}}))
    monkeypatch.setenv('AGENTGUARD_V2_CLOUD_ENABLED','0')
    monkeypatch.setattr('agentguard.v2.opencode.shutil.which',lambda _: '/bin/opencode')
    monkeypatch.setattr('agentguard.v2.opencode.subprocess.check_output',lambda *a,**k:'1.19.4\n')
    monkeypatch.setattr('agentguard.v2.opencode.find_sdk',lambda:'file:///private/sdk.js')
    install(SimpleNamespace(config=str(config),env_file=None,project=str(root)))
    profile=json.loads((root/'opencode.json').read_text())
    assert profile['provider']['agentguard']['options']['apiKey']=='BOUNDARY_PLUGIN_REQUIRED'
    assert profile['enabled_providers']==['agentguard']
    assert profile['permission']=={'*':'deny','ag_*':'allow'}
    backup=next((tmp_path/'state/native-project').glob('opencode.json.backup-*'))
    assert json.loads(backup.read_text())=={'old_profile':True}
    private=json.loads((tmp_path/'state/native-project/application.json').read_text())
    assert str(root/'opencode.json') in private['opencode']['protected_paths']
    assert json.loads(capsys.readouterr().out)['per_window_boundary'] is True
