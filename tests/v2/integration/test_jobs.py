import threading
from uuid import uuid4
import httpx
import pytest
from agentguard.v2.jobs import JobManager, Submission, create_app


@pytest.mark.asyncio
async def test_operator_auth_idempotency_and_fixed_configuration(tmp_path):
    entered=threading.Event(); release=threading.Event(); calls=[]
    def execute(job_id,task):
        calls.append((job_id,task)); entered.set(); release.wait(3)
        manager.update(job_id,status='RELEASED',content='public final')
    manager=JobManager(tmp_path,{},'http://localhost','host-token',execute=execute)
    try:
        app=create_app(manager,'operator-token')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            submission={'submission_id':uuid4().hex,'task':'read fake example'}
            denied=await client.post('/jobs',headers={'Authorization':'Bearer ui-token'},json=submission)
            assert denied.status_code==403 and not calls
            headers={'Authorization':'Bearer operator-token'}
            first=await client.post('/jobs',headers=headers,json=submission)
            second=await client.post('/jobs',headers=headers,json=submission)
            assert first.status_code==second.status_code==200
            assert first.json()['job_id']==second.json()['job_id']
            assert entered.wait(1) and len(calls)==1
            assert (await client.post('/jobs',headers=headers,json={**submission,'task':'different'})).status_code==400
            assert (await client.post('/jobs',headers=headers,json={**submission,'config':'attacker'})).status_code==400
            assert (await client.get('/jobs')).status_code==403
    finally:
        release.set(); manager.close()


def test_restart_preserves_interrupted_work_without_reexecution(tmp_path):
    manager=JobManager(tmp_path,{},'http://localhost','host',execute=lambda *_:None)
    key=uuid4().hex
    manager.update(key,task='never replay',status='RUNNING',created_at='2026-10-24',session_id='session_test')
    manager.close(); calls=[]
    manager=JobManager(tmp_path,{},'http://localhost','host',execute=lambda *a:calls.append(a))
    try:
        assert manager.get(key)['status']=='INTERRUPTED'
        assert not calls
        job=manager.submit(Submission(submission_id=key,task='never replay'))
        assert job['status']=='INTERRUPTED' and not calls
    finally: manager.close()


def test_failure_is_persisted_without_exception_body_or_automatic_replay(tmp_path):
    done=threading.Event(); calls=[]
    def execute(*args):
        calls.append(args); done.set(); raise ValueError('PRIVATE provider payload')
    manager=JobManager(tmp_path,{},'http://localhost','host',execute=execute)
    key=uuid4().hex; submission=Submission(submission_id=key,task='example')
    manager.submit(submission); assert done.wait(1); manager.close()
    job=manager.get(key)
    assert job['status']=='FAILED' and job['reason_code']=='TASK_FAILED'
    assert 'PRIVATE' not in str(job) and job['content'] is None and len(calls)==1
    restarted=JobManager(tmp_path,{},'http://localhost','host',execute=execute)
    try:
        assert restarted.submit(submission)['status']=='FAILED'
        assert len(calls)==1
    finally: restarted.close()
