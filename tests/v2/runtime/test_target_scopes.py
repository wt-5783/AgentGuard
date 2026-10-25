import pytest
from agentguard.v2.contracts import ResourceScope, make
from agentguard.v2.common import RuntimeFault
from agentguard.v2.state.lineage import public_labels
from conftest import proposal, build_harness


def restrict(h, targets):
    h.request=h.request.model_copy(update={'host_policy':h.request.host_policy.model_copy(
        update={'tool_target_scopes':targets})})


@pytest.mark.asyncio
async def test_input_origins_remain_readable_but_cannot_become_write_targets(harness):
    h=harness;restrict(h,{'write_report':['reports']})
    s=await h.session()
    await h.run(s.session_id,[proposal()])
    _,out,b=await h.run(s.session_id,[proposal('write_report',{'resource':'docs','body':'bad'},use='write')])
    assert b.results[out.calls[0].call_id].reason_code=='TARGET_OUT_OF_SCOPE'
    assert h.effects==[]
    _,out,b=await h.run(s.session_id,[proposal('write_report',{'resource':'reports','body':'ok'},use='write')])
    assert b.results[out.calls[0].call_id].status=='RELEASED'
    assert h.effects==[('write','reports','ok')]


@pytest.mark.asyncio
@pytest.mark.parametrize('targets,reason',[({'write_report':[]},None),({'missing':['docs']},'INVALID_TARGET_CONSTRAINT'),
    ({'send_report':['docs']},'INVALID_TARGET_CONSTRAINT'),({'write_report':['foreign']},'UNKNOWN_RESOURCE')])
async def test_target_configuration_is_host_validated(harness,targets,reason):
    h=harness;restrict(h,targets)
    if reason:
        with pytest.raises(RuntimeFault,match=reason): await h.session()
    else:
        s=await h.session()
        _,out,b=await h.run(s.session_id,[proposal('write_report',{'resource':'reports','body':'bad'},use='write')])
        assert b.results[out.calls[0].call_id].reason_code=='TARGET_OUT_OF_SCOPE'
        assert h.effects==[]


@pytest.mark.asyncio
async def test_new_symlink_cannot_move_a_trusted_target_anchor(tmp_path):
    h=build_harness(tmp_path/'private')
    root=tmp_path/'workspace';root.mkdir()
    target=root/'report.txt'
    h.request=h.request.model_copy(update={'resource_catalog':h.request.resource_catalog.model_copy(update={'resources':[
        make(ResourceScope,scope_id='docs',kind='path',root=str(root),labels=public_labels()),
        make(ResourceScope,scope_id='reports',kind='path',root=str(target),labels=public_labels())]})})
    restrict(h,{'write_report':['reports']})
    s=await h.session()
    victim=root/'security.json';victim.write_text('unchanged')
    target.symlink_to(victim)
    _,out,b=await h.run(s.session_id,[proposal('write_report',{'resource':str(target),'body':'bad'},use='write')])
    assert b.results[out.calls[0].call_id].reason_code=='TARGET_OUT_OF_SCOPE'
    assert h.effects==[] and victim.read_text()=='unchanged'
