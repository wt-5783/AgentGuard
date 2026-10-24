"""The overview must not turn failures into security wins or expose hidden text."""
import pytest
from agentguard.v2.console import session_summary, visible_released_content


def event(kind, seq=1, **payload):
    return {'event_type': kind, 'seq': seq, 'timestamp': f'2026-10-24T08:00:{seq:02d}Z', 'payload': payload}


def test_failed_review_is_not_an_attack_block():
    result = session_summary([event('REVIEW_FAILED', reason_code='INVALID_REFERENCE')], {})
    assert result['state'] == 'warning'
    assert result['title'] == '流程未完成，需要检查'
    assert not result['blocked'] and not result['decisions']


def test_final_release_does_not_require_session_to_be_closed():
    result = session_summary([event('SESSION_CREATED'), event('FINAL_RELEASED', 2)],
                             {'executions': {'SUCCEEDED': 5}, 'review_attempts': 11})
    assert result['state'] == 'success'
    assert result['elapsed'] == 1
    assert result['executed'] == 5
    assert result['reviews'] == 11


def test_later_unfinished_turn_is_not_hidden_by_earlier_final():
    result = session_summary([event('FINAL_RELEASED'), event('MODEL_TURN_PREPARED', 2)], {})
    assert result['state'] == 'info'
    assert result['final'] is not None  # historical final, not current completion


def test_block_and_failed_review_are_reported_separately():
    result = session_summary([event('REVIEW_DECIDED', decision='BLOCK'),
                              event('CALL_BLOCKED', 2), event('REVIEW_FAILED', 3)], {})
    assert len(result['blocked']) == 1
    assert len(result['decisions']) == 1
    assert len(result['failures']) == 1


@pytest.mark.parametrize(('visible', 'released'), [(False, False), (False, True), (True, False)])
def test_overview_does_not_display_protected_or_unreleased_content(visible, released):
    paths = {'/session/artifacts/a/versions/1/comparison': {
        'synthetic_content_visible': visible, 'released': released, 'content': 'PRIVATE'}}
    assert visible_released_content(paths, '/session', {'artifact_id': 'a', 'version': 1}) is None


def test_visible_content_uses_exact_released_version():
    paths = {'/session/artifacts/a/versions/1/comparison': {
        'synthetic_content_visible': True, 'released': True, 'content': 'public example',
        'versions': [{'version': 2, 'content': 'unreleased revision', 'released': False}]}}
    assert visible_released_content(paths, '/session', {'artifact_id': 'a', 'version': 1}) == 'public example'
    assert visible_released_content(paths, '/session', {'artifact_id': 'a', 'version': 2}) is None
