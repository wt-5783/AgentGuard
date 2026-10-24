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


def test_pre_session_failure_does_not_show_a_previous_tasks_success(monkeypatch):
    from pathlib import Path
    from agentguard.v2 import console,operator_console
    monkeypatch.setenv('AGENTGUARD_OPERATOR_TOKEN','test-operator')
    monkeypatch.setenv('AGENTGUARD_OPERATOR_URL','http://127.0.0.1:8766')
    monkeypatch.delenv('AGENTGUARD_V2_REPLAY',raising=False)
    monkeypatch.setattr(operator_console,'operator_request',lambda *a,**k:[{
        'job_id':'b'*32,'task':'denied','status':'FAILED','reason_code':'UNRESOLVED_REQUIREMENTS',
        'session_id':None,'content':None}])
    def forbidden(*args): raise AssertionError('Must not fetch another session by default')
    monkeypatch.setattr(console.ConsoleReader,'get',forbidden)
    app=AppTest.from_file(str(Path(console.__file__))).run()
    assert not app.exception and not app.tabs
    assert app.checkbox[0].label=='显示其他历史会话的审计与评测'


def test_identical_tasks_have_distinct_selector_labels_and_keep_audit_target(monkeypatch):
    from agentguard.v2 import operator_console
    monkeypatch.setenv('AGENTGUARD_OPERATOR_TOKEN','test-operator')
    monkeypatch.setenv('AGENTGUARD_OPERATOR_URL','http://127.0.0.1:8766')
    jobs=[{'job_id':key*32,'task':'same task','status':'RELEASED','session_id':'session_'+key,
           'content':'answer '+key} for key in ('a','b')]
    monkeypatch.setattr(operator_console,'operator_request',lambda *a,**k:jobs)
    app=AppTest.from_string('import streamlit as st\nfrom agentguard.v2.operator_console import render\nrender(st)').run()
    assert len(set(app.selectbox[0].options))==2
    app.selectbox[0].select('b'*32).run()
    assert app.session_state['selected_job']=='b'*32
    app.button[2].click().run()
    assert app.session_state['selected_job']=='b'*32
    assert app.session_state['audit_session']=='session_b'
