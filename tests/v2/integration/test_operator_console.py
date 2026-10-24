from streamlit.testing.v1 import AppTest


def test_failed_submission_keeps_id_and_failed_job_has_no_unreleased_answer(monkeypatch):
    from agentguard.v2 import operator_console
    monkeypatch.setenv('AGENTGUARD_OPERATOR_TOKEN','test-operator')
    monkeypatch.setenv('AGENTGUARD_OPERATOR_URL','http://127.0.0.1:8766')
    calls=[]
    def request(path='/jobs',data=None):
        if data:
            calls.append(data); raise TimeoutError()
        return [{'job_id':'a'*32,'task':'example','status':'FAILED','reason_code':'MODEL_UNAVAILABLE',
                 'session_id':None,'content':'UNRELEASED_PRIVATE_TEXT'}]
    monkeypatch.setattr(operator_console,'operator_request',request)
    app=AppTest.from_string('import streamlit as st\nfrom agentguard.v2.operator_console import render\nrender(st)').run()
    initial=app.session_state['submission_id']
    assert not app.exception
    assert any('云端连接' in w.value for w in app.warning)
    assert 'UNRELEASED_PRIVATE_TEXT' not in str(app.markdown)
    app.button[0].click().run()
    assert app.session_state['submission_id']==initial and len(calls)==1
    assert any('提交未确认' in e.value for e in app.error)
    app.button[1].click().run()
    assert len(calls)==1  # refreshing is read-only, never replay
