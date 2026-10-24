"""Opt-in localhost operator service. Read-only UI tokens cannot submit jobs.

Tasks only select text, never configuration, endpoint, workspace or factory.
No interrupted job is automatically replayed after a restart.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .application import compose, load_settings
from .adapters.http import GatewayRuntime
from .common import RuntimeFault
from .review.errors import GuardError
from .state.ownership import exclusive_worker


def now():
    return datetime.now(timezone.utc).isoformat()


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    submission_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    task: str = Field(min_length=1, max_length=4000)


class JobManager:
    def __init__(self, directory, settings, gateway, host_token, *, execute=None):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.settings,self.gateway,self.host_token=settings,gateway,host_token
        self.lock=threading.RLock(); self.pool=ThreadPoolExecutor(max_workers=1)
        self.execute=execute or self._execute
        self.closed=False
        # Losing a process is not evidence that a side effect failed.
        for job in self.list(limit=None):
            if job['status'] in ('QUEUED','RUNNING'):
                self.update(job['job_id'],status='INTERRUPTED',reason_code='PROCESS_INTERRUPTED_NO_AUTO_RETRY')

    def _path(self,job_id):
        if not re.fullmatch('[a-f0-9]{32}',job_id): raise ValueError('Invalid job id')
        return self.directory/(job_id+'.json')

    def get(self,job_id):
        with self.lock:
            return json.loads(self._path(job_id).read_text(encoding='utf-8'))

    def list(self,limit=100):
        with self.lock:
            return sorted([json.loads(p.read_text(encoding='utf-8')) for p in self.directory.glob('*.json')],
                          key=lambda j:j['created_at'],reverse=True)[:limit]

    def update(self,job_id,**fields):
        with self.lock:
            path=self._path(job_id)
            job=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'job_id':job_id}
            job.update(fields,updated_at=now())
            temporary=path.with_suffix('.'+uuid4().hex+'.tmp')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as handle:
                json.dump(job,handle,ensure_ascii=False); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary,path)
            return job

    def submit(self,submission):
        with self.lock:
            if self.closed: raise ValueError('Service closed')
            job_id=submission.submission_id
            if self._path(job_id).exists():
                job=self.get(job_id)
                if job['task']!=submission.task: raise ValueError('Submission id conflict')
                return job
            if not submission.task.strip(): raise ValueError('Empty task')
            if sum(j['status'] in ('QUEUED','RUNNING') for j in self.list())>=5:
                raise ValueError('Queue full')
            job=self.update(job_id,task=submission.task,created_at=now(),status='QUEUED',
                            session_id=None,reason_code=None,content=None)
            self.pool.submit(self._work,job_id)
            return job

    def _work(self,job_id):
        self.update(job_id,status='RUNNING')
        try:
            self.execute(job_id,self.get(job_id)['task'])
        except Exception as exc:
            code=exc.code if isinstance(exc,RuntimeFault) else exc.reason_code if isinstance(exc,GuardError) else 'TASK_FAILED'
            self.update(job_id,status='FAILED',reason_code=code,content=None)

    def _execute(self,job_id,task):
        async def run():
            remote=GatewayRuntime(self.gateway,self.host_token,timeout=180)
            try:
                app=compose(self.settings,task,runtime=remote)
                host=app.host(remote)
                session=await host.create(app.request)
                self.update(job_id,session_id=session.session_id)
                result=await host.run(session.session_id)
                self.update(job_id,status=result.status,reason_code=result.reason_code,
                            content=result.content if result.status=='RELEASED' else None)
            finally:
                await remote.aclose()
        asyncio.run(run())

    def close(self):
        self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)


def create_app(manager,operator_token):
    if not operator_token: raise ValueError('Operator token required')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    def authorize(request):
        auth=request.headers.get('Authorization','')
        if not hmac.compare_digest(auth,'Bearer '+operator_token):
            raise HTTPException(403,'Operator permission required')
    @app.get('/jobs')
    async def listing(request:Request):
        authorize(request); return manager.list()
    @app.post('/jobs')
    async def submit(request:Request):
        authorize(request)
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>24000: raise HTTPException(413,'Task too large')
        try:
            submission=Submission.model_validate_json(bytes(raw))
            return manager.submit(submission)
        except (ValueError,TypeError):
            raise HTTPException(400,'Invalid submission, duplicate id or queue full') from None
    return app


def main():
    import uvicorn
    settings=load_settings(os.environ['AGENTGUARD_V2_CONFIG'])
    token=os.environ['AGENTGUARD_OPERATOR_TOKEN']
    if token in (os.environ['AGENTGUARD_HOST_TOKEN'],os.environ['AGENTGUARD_UI_TOKEN']):
        raise ValueError('Use distinct operator, host and UI credentials')
    directory=Path(settings['state_directory'])/'operator-jobs'
    with exclusive_worker(directory):
        manager=JobManager(directory,settings,os.environ.get('AGENTGUARD_V2_GATEWAY','http://127.0.0.1:8765'),
                           os.environ['AGENTGUARD_HOST_TOKEN'])
        try:
            uvicorn.run(create_app(manager,token),host='127.0.0.1',port=8766,access_log=False)
        finally:
            manager.close()


if __name__=='__main__': main()
