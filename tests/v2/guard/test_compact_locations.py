from copy import deepcopy
import json
import pytest
from agentguard.v2.review.compact import expand, messages, reference_table
from agentguard.v2.review.context import ContextBuilder
from agentguard.v2.review.boundary import payload
from agentguard.v2.review.evidence import evidence_index
from agentguard.v2.review.reviewers import validate_analysis
from agentguard.v2.review.errors import GuardError
from .fakes import fixture


def request():
    req=payload(ContextBuilder().build(fixture('normal_allow')))['request']
    # Keep immutable fixture evidence intact; select its two differently sized strings.
    assert isinstance(req['subject'],str)
    return req


def block(alias='E0',span='whole'):
    return {'decision':'BLOCK','reason_code':'UNTRUSTED_INSTRUCTION','reason_summary':'Untrusted source instruction.',
        'evidence_refs':[{'ref_id':'E0','span':None,'json_pointer':None}],
        'findings':[{'schema_version':'0.1','finding_type':'prompt_injection','affected_action_field':None,
            'evidence_refs':[{'ref_id':alias,'span':span,'json_pointer':None}]}],
        'redaction_targets':[],'requested_evidence_refs':[]}


def test_whole_is_bound_to_the_exact_selected_string_and_old_invalid_ranges_stay_invalid():
    req=request();table=reference_table(req)
    for alias,ref in table.items():
        content=next((e['content'] for e in req['evidence'] if e['ref']==ref),None)
        if isinstance(content,str) and content:
            result=expand(block(alias),req);validate_analysis(result,req)
            assert result['findings'][0]['evidence_refs'][0]['span']==[0,len(content)]
            with pytest.raises(GuardError,match='INVALID_REFERENCE'):
                validate_analysis(expand(block(alias,[0,len(content)+1]),req),req)
    invalid=block();invalid['findings'][0]['evidence_refs'][0]['json_pointer']='/body'
    with pytest.raises(GuardError): expand(invalid,req)
    with pytest.raises(GuardError): expand(block('E999'),req)


def test_alias_metadata_is_lossless_and_evidence_contents_are_unchanged():
    req=request();wire=json.loads(messages(req)[1]['content'])
    original=deepcopy(req)
    table=reference_table(req)
    def restore(value):
        if isinstance(value,dict):
            if set(value)=={'ref_id','span','json_pointer'}:
                return {**table[value['ref_id']],'span':value['span'],'json_pointer':value['json_pointer']}
            return {k:restore(v) for k,v in value.items()}
        if isinstance(value,list): return [restore(v) for v in value]
        return value
    for key in ('mandatory_facts','lineage','host_policy','policy'):
        assert restore(wire['trusted_boundary'][key])==req[key]
    assert restore(wire['untrusted_evidence']['recent_events'])==req['recent_events']
    assert [e['content'] for e in wire['untrusted_evidence']['evidence']]==[e['content'] for e in req['evidence']]
    assert wire['untrusted_evidence']['subject']==req['subject']
    assert req==original


def test_whole_rejects_json_and_unselected_evidence():
    req=request()
    unknown={**req['subject_ref'],'artifact_id':'unselected','sha256':'0'*64}
    req['available_evidence_refs'].append(unknown)
    alias=next(k for k,v in reference_table(req).items() if v==unknown)
    with pytest.raises(GuardError,match='INVALID_REFERENCE'): expand(block(alias),req)
    req['subject']={'body':'json content'}
    with pytest.raises(GuardError,match='INVALID_REFERENCE'): expand(block(),req)
