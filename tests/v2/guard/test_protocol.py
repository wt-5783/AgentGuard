import asyncio
from types import SimpleNamespace
import pytest
from agentguard.v2.review.clients import CloudReply
from agentguard.v2.review.errors import GuardError
from agentguard.v2.review.protocol import complete_validated


class Client:
    config = SimpleNamespace(protocol_retries=2)
    def __init__(self, replies):
        self.replies=iter(replies); self.requests=[]
    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        value=next(self.replies)
        if isinstance(value,Exception): raise value
        return CloudReply(value,dict(provider='test',model='test',input_tokens=10,output_tokens=2,latency_ms=1,attempts=1))


def validate(value):
    if value=='BAD_PRIVATE_MODEL_TEXT': raise GuardError('INVALID_REFERENCE')
    return value


@pytest.mark.asyncio
async def test_repairs_do_not_promote_invalid_text_and_account_all_attempts():
    c=Client(['BAD_PRIVATE_MODEL_TEXT','BLOCK']); attempts=[]
    result,usage=await complete_validated(c,[{'role':'system','content':'review'},{'role':'user','content':'authorized'}],
        purpose='RESULT',prompt_version='test',validate=validate,attempts=attempts)
    assert result=='BLOCK'  # no retry of a valid block
    assert usage['attempts']==2 and usage['input_tokens']==20
    assert len(attempts)==2 and attempts[0]['reason_code']=='INVALID_REFERENCE'
    assert 'BAD_PRIVATE_MODEL_TEXT' not in str(c.requests)
    assert c.requests[0]['messages'][1]==c.requests[1]['messages'][1]


@pytest.mark.asyncio
async def test_repair_exhaustion_never_returns_allow():
    c=Client(['BAD_PRIVATE_MODEL_TEXT']*3)
    with pytest.raises(GuardError) as e:
        await complete_validated(c,[{'role':'system','content':'review'}],purpose='FINAL',prompt_version='test',validate=validate)
    assert e.value.reason_code=='INVALID_REFERENCE'
    assert e.value.usage['attempts']==3 and len(c.requests)==3


@pytest.mark.asyncio
async def test_transport_or_budget_failure_does_not_add_another_retry_layer():
    c=Client([GuardError('BUDGET_EXHAUSTED')])
    with pytest.raises(GuardError,match='BUDGET_EXHAUSTED'):
        await complete_validated(c,[{'role':'system','content':'review'}],purpose='FINAL',prompt_version='test',validate=validate)
    assert len(c.requests)==1


def test_compact_aliases_cannot_choose_foreign_evidence_or_change_bindings():
    from .fakes import fixture
    from agentguard.v2.review.compact import expand, reference_table
    from agentguard.v2.review.context import ContextBuilder
    from agentguard.v2.review.boundary import payload
    from agentguard.v2.review.reviewers import validate_analysis
    req=payload(ContextBuilder().build(fixture('normal_allow')))['request']
    minimal={'decision':'ALLOW','reason_code':'NORMAL_CONTENT','reason_summary':'Task data.',
             'evidence_refs':[{'ref_id':'E0','span':None,'json_pointer':None}],
             'findings':[],'redaction_targets':[],'requested_evidence_refs':[]}
    result=expand(minimal,req)
    validate_analysis(result,req)
    assert result['subject_ref']==req['subject_ref']
    # A real Pro response used null for both reasons on ALLOW. Keep rejecting
    # those responses; the prompt fix must not silently supply authorization.
    for field in ('reason_code', 'reason_summary'):
        invalid = {**result, field: None}
        with pytest.raises(GuardError, match='INVALID_SCHEMA'):
            validate_analysis(invalid, req)
    minimal['evidence_refs'][0]['ref_id']='E999'
    with pytest.raises(GuardError,match='INVALID_REFERENCE'): expand(minimal,req)
    minimal['session_id']='another-session'
    with pytest.raises(GuardError,match='INVALID_SCHEMA'): expand(minimal,req)
