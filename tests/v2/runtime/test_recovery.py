import pytest
from agentguard.v2.common import RuntimeFault
from agentguard.v2.contracts import *
from agentguard.v2.state.lineage import public_labels
from conftest import proposal

@pytest.mark.asyncio
async def test_late_risk_and_ack(harness):
    h=harness
    s=await h.session()
    independent=h.runtime.ingest(s.session_id,'independent',public_labels())
    risky=h.runtime.ingest(s.session_id,'risk',public_labels('external'))
    p,out=await h.propose(s.session_id,[proposal()],messages=[make(Message,role='tool',ref=risky)])
    permit=h.runtime.permits.issue(out.calls[0],[])
    plan=await h.runtime.prepare_recovery(make(RecoveryRequest,session_id=s.session_id,risk_refs=[risky],reason_code='LATE_RISK'))
    assert out.output_ref in plan.affected_refs
    assert permit.permit_id in plan.revoked_permit_ids
    assert independent in [m.ref for m in plan.rebuild_messages]
    assert not h.runtime.artifacts.is_released(s.session_id,risky)
    with pytest.raises(RuntimeFault,match='SESSION_NOT_ACTIVE'):
        await h.propose(s.session_id)
    incomplete=make(RecoveryAck,session_id=s.session_id,recovery_id=plan.recovery_id,new_host_context_id='fresh',completed_resets={},unsupported=[])
    with pytest.raises(RuntimeFault,match='RECOVERY_ACK_INCOMPLETE'):
        await h.runtime.complete_recovery(incomplete)
    assert h.runtime.session_view(s.session_id).status=='RECOVERING'
    good=incomplete.model_copy(update={'completed_resets':{k:True for k in plan.required_host_resets}})
    view=await h.runtime.complete_recovery(good)
    assert view.status=='ACTIVE' and view.context_version==plan.safe_context_version
    assert h.runtime.store.all(s.session_id,'recovery_ack_attempt')
    with pytest.raises(RuntimeFault):
        h.runtime.permits.claim(permit,out.calls[0])
    await h.propose(s.session_id,messages=plan.rebuild_messages)

@pytest.mark.asyncio
async def test_external_effect_not_reported_retracted(harness):
    h=harness
    s=await h.session()
    _,out,b=await h.run(s.session_id,[proposal('send_report',{'destination':'receiver','body':'report'},use='send')])
    r=b.results[out.calls[0].call_id]
    assert r.status=='RELEASED' and len(h.effects)==1
    plan=await h.runtime.prepare_recovery(make(RecoveryRequest,session_id=s.session_id,risk_refs=[out.output_ref],reason_code='LATE_RISK'))
    assert r.receipt in plan.irreversible_receipts
    assert len(h.effects)==1

@pytest.mark.asyncio
async def test_inflight_effect_is_visible_and_ack_waits(harness):
    import asyncio
    h=harness
    entry=h.runtime.registry.get('send_report')
    started,finish=asyncio.Event(),asyncio.Event()
    async def sender(destination,body):
        h.effects.append(body)
        started.set()
        await finish.wait()
        return 'sent'
    h.runtime.registry.register(entry.manifest.model_copy(update={'manifest_version':'v2'}),sender)
    h.request=h.request.model_copy(update={'tool_manifests':h.runtime.registry.manifests()})
    s=await h.session()
    p,out=await h.propose(s.session_id,[proposal('send_report',{'destination':'receiver','body':'report'},use='send').model_copy(update={'manifest_version':'v2'})])
    task=asyncio.create_task(h.runtime.execute_call(out.calls[0]))
    await started.wait()
    plan=await h.runtime.prepare_recovery(make(RecoveryRequest,session_id=s.session_id,risk_refs=[out.output_ref],reason_code='LATE_RISK'))
    assert plan.unknown_receipts[0].reason_code=='IN_FLIGHT_AT_RECOVERY'
    ack=make(RecoveryAck,session_id=s.session_id,recovery_id=plan.recovery_id,new_host_context_id='clean',
        completed_resets={k:True for k in plan.required_host_resets},unsupported=[])
    with pytest.raises(RuntimeFault,match='RECOVERY_ACK_INCOMPLETE'):
        await h.runtime.complete_recovery(ack)
    finish.set()
    result=await task
    assert result.content is None and len(h.effects)==1
    assert (await h.runtime.complete_recovery(ack)).status=='ACTIVE'
    assert result.receipt.status=='SUCCEEDED'  # effect cannot be retracted by recovery

@pytest.mark.asyncio
async def test_delivered_final_output_cannot_be_recalled(harness):
    h=harness
    s=await h.session()
    p,out=await h.propose(s.session_id,content='already delivered')
    final=await h.runtime.finalize(make(FinalizeRequest,session_id=s.session_id,turn_id=p.turn_id,context_version=p.context_version,output_ref=out.output_ref))
    plan=await h.runtime.prepare_recovery(make(RecoveryRequest,session_id=s.session_id,risk_refs=[out.output_ref],reason_code='LATE_RISK'))
    assert final.artifact_ref in plan.irreversible_final_refs
    assert not h.runtime.artifacts.is_released(s.session_id,final.artifact_ref)
    assert final.content=='already delivered'

@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['write','scope','final'])
async def test_hard_use_refusal_returns_origins_and_rebuild_preserves_independent_public_work(harness,operation):
    h=harness
    if operation=='scope':
        # The origin-scope check can precede the label refusal.
        resource=make(ResourceScope,scope_id='analysis_only',kind='logical',root='analysis_only',labels=public_labels())
        h.request=h.request.model_copy(update={
            'resource_catalog':h.request.resource_catalog.model_copy(update={'resources':[*h.request.resource_catalog.resources,resource]}),
            'host_policy':h.request.host_policy.model_copy(update={'model_destination_ids':[
                g.model_copy(update={'resource_scope_ids':[*g.resource_scope_ids,'analysis_only']}) for g in h.request.host_policy.model_destination_ids]})})
    s=await h.session()
    public=h.runtime.ingest(s.session_id,'independent public bulletin',public_labels())
    restricted=h.runtime.ingest(s.session_id,'ANALYSIS_ONLY_PRIVATE',public_labels('external').model_copy(
        update={'confidentiality':['private'],'allowed_uses':['analyze','model','review']}),resource_scope_ids=['analysis_only'] if operation=='scope' else [])
    derived=h.runtime.ingest(s.session_id,'derived analysis',h.runtime.artifacts.metadata(s.session_id,restricted).labels,
        resource_scope_ids=['analysis_only'] if operation=='scope' else [])
    event=h.runtime.store.event(s.session_id,'TEST_DERIVATION',{})
    h.runtime.lineage.add(s.session_id,restricted,derived,'possible',event.event_id)
    messages=[make(Message,role='tool',ref=public),make(Message,role='tool',ref=derived)]
    if operation in ('write','scope'):
        _,out,batch=await h.run(s.session_id,[proposal('write_report',{'resource':'reports','body':'attempt'},use='write')],messages)
        result=batch.results[out.calls[0].call_id]
    else:
        p,out=await h.propose(s.session_id,messages=messages,content='not released')
        result=await h.runtime.finalize(make(FinalizeRequest,session_id=s.session_id,turn_id=p.turn_id,
            context_version=p.context_version,output_ref=out.output_ref))
    assert result.status=='BLOCKED' and result.reason_code==('ARTIFACT_SCOPE_DENIED' if operation=='scope' else 'DATA_USE_DENIED')
    assert result.risk_refs==[restricted]
    assert not h.effects
    plan=await h.runtime.prepare_recovery(make(RecoveryRequest,session_id=s.session_id,
        risk_refs=result.risk_refs,reason_code='DATA_USE_DENIED'))
    assert public in [m.ref for m in plan.rebuild_messages]
    assert restricted not in [m.ref for m in plan.rebuild_messages]
    assert out.output_ref in plan.affected_refs and derived in plan.affected_refs
    with pytest.raises(RuntimeFault): h.runtime.artifacts.read(s.session_id,restricted,released=True)
    ack=make(RecoveryAck,session_id=s.session_id,recovery_id=plan.recovery_id,new_host_context_id='new-public-context',
        completed_resets={k:True for k in plan.required_host_resets},unsupported=[])
    await h.runtime.complete_recovery(ack)
    _,out,batch=await h.run(s.session_id,[proposal('write_report',{'resource':'reports','body':'public bulletin'},use='write')])
    assert batch.results[out.calls[0].call_id].status=='RELEASED'
    assert h.effects==[('write','reports','public bulletin')]
