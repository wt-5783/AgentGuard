import asyncio
from copy import deepcopy
from types import SimpleNamespace
import pytest
from agentguard.v2.policy.compact import expand, rule_table, FIELDS
from agentguard.v2.policy.compiler import PolicyCompiler
from agentguard.v2.review.errors import GuardError
from .fakes import fixture, FakeCloudClient, FixtureContracts


def compact(req, candidate):
    ids={(r['tool_id'],r['effect']):key for key,r in rule_table(req).items()}
    return {'rules':{ids[(r['tool_id'],r['effect'])]:{k:r[k] for k in FIELDS} for r in candidate['rules']},
        'model_destination_ids':{g['destination_id']:g['resource_scope_ids'] for g in candidate['model_destination_ids']},
        'rationale':candidate['rationale'],'unresolved_requirements':candidate['unresolved_requirements']}


def test_indexed_compilation_preserves_full_validated_candidate():
    req=fixture('policy_local_vs_send','local_request.json')
    candidate=fixture('policy_local_vs_send','local_candidate.json')
    wire=compact(req,candidate)
    assert expand(wire,req)==candidate
    client=FakeCloudClient([wire]);client.config=SimpleNamespace(compact_protocol=True,protocol_retries=0,endpoint=next(d['address'] for d in req['resource_catalog']['destinations'] if d['destination_id']=='guard_cloud'))
    result=asyncio.run(PolicyCompiler(client,contracts=FixtureContracts()).generate(req))
    assert result==candidate
    assert client.requests[0]['prompt_version'].endswith('-indexed-v2')


@pytest.mark.parametrize('mutation',['unknown','override','extra','duplicate_key'])
def test_model_cannot_replace_rule_identity_or_repeat_an_index(mutation):
    req=fixture('policy_local_vs_send','local_request.json')
    wire=compact(req,fixture('policy_local_vs_send','local_candidate.json'))
    if mutation=='unknown': wire['rules']['R999']=wire['rules'].pop('R0')
    elif mutation=='override': wire['rules']['R0']['effect']='SEND'
    elif mutation=='extra': wire['session_id']='foreign-session'
    else: wire='{"rules":{},"rules":{}}'
    client=FakeCloudClient([wire]);client.config=SimpleNamespace(compact_protocol=True,protocol_retries=0,endpoint=next(d['address'] for d in req['resource_catalog']['destinations'] if d['destination_id']=='guard_cloud'))
    with pytest.raises(GuardError):
        asyncio.run(PolicyCompiler(client,contracts=FixtureContracts()).generate(req))
    assert len(client.requests)==1


def test_initial_model_grants_are_explicit_in_example_and_missing_grants_still_fail():
    import json
    from agentguard.v2.policy.compact import messages
    req=fixture('policy_local_vs_send','local_request.json')
    system=messages(req)[0]['content']
    example=json.loads(system.split('Shape (model grants below are real host IDs, retain them): ')[1])
    assert example['model_destination_ids']=={g['destination_id']:g['resource_scope_ids'] for g in req['host_policy']['model_destination_ids']}
    wire=compact(req,fixture('policy_local_vs_send','local_candidate.json'));wire['model_destination_ids']={}
    client=FakeCloudClient([wire]);client.config=SimpleNamespace(compact_protocol=True,protocol_retries=0,endpoint=next(d['address'] for d in req['resource_catalog']['destinations'] if d['destination_id']=='guard_cloud'))
    with pytest.raises(GuardError,match='INVALID_REFERENCE'):
        asyncio.run(PolicyCompiler(client,contracts=FixtureContracts()).generate(req))
    assert len(client.requests)==1
