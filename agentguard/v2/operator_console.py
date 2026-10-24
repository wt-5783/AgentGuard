"""Operator controls backed by a separate authenticated localhost service."""
import json
import os
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

LABELS={'QUEUED':'排队中','RUNNING':'执行中','RELEASED':'回答已放行','BLOCKED':'已阻断',
        'REVIEW_PENDING':'审查未完成','FAILED':'执行失败','INTERRUPTED':'进程中断，未自动重跑'}
REASONS={'HOST_REPEATED_CALL_LIMIT':'模型持续重复已完成的操作，已停止，避免重复执行。',
         'HOST_REVIEW_OR_EFFECT_PENDING':'审查或副作用状态未确定。先检查执行记录，不要自动重试。',
         'POLICY_GENERATION_FAILED':'任务策略未能通过校验，请检查任务授权与模型状态。',
         'MODEL_TIMEOUT':'模型请求超时；此前的工具操作可能已经执行。',
         'MODEL_UNAVAILABLE':'云端连接暂时不可用；已停止任务，请先查看已执行的操作。',
         'HOST_POLICY_EXCEEDED':'请求超出了本机配置允许的权限，任务未获批准。',
         'UNRESOLVED_REQUIREMENTS':'任务需要的授权或资源不完整，尚未开始执行。',
         'BUDGET_EXHAUSTED':'本次配置的模型预算已耗尽。',
         'PROCESS_INTERRUPTED_NO_AUTO_RETRY':'服务曾中断。系统保留记录，不自动重放任务。'}


def operator_request(path='/jobs',data=None):
    base=os.environ['AGENTGUARD_OPERATOR_URL']
    # This opt-in operator UI is only for the same machine.
    if base!='http://127.0.0.1:8766': raise ValueError('Local operator URL required')
    req=Request(base+path,data=json.dumps(data).encode() if data is not None else None,
                headers={'Authorization':'Bearer '+os.environ['AGENTGUARD_OPERATOR_TOKEN'],
                         'Content-Type':'application/json'})
    with build_opener(ProxyHandler({})).open(req,timeout=5) as response:
        return json.loads(response.read(2*1024*1024))


def render(st):
    if not os.environ.get('AGENTGUARD_OPERATOR_TOKEN') or not os.environ.get('AGENTGUARD_OPERATOR_URL'): return
    with st.expander('提交任务 / 查看运行状态',expanded=True):
        st.caption('本机操作员入口 · 使用云端模型，提交后产生 API 用量。工具范围固定为演示工作目录。')
        st.session_state.setdefault('submission_id',uuid4().hex)
        with st.form('new_agent_task'):
            task=st.text_area('任务描述',value='请读取工作目录中的 readme.txt，用中文概括内容后直接回复；不写文件，不访问其他路径。',height=90,max_chars=4000)
            submitted=st.form_submit_button('提交新任务',type='primary')
        if submitted:
            try:
                job=operator_request(data={'submission_id':st.session_state['submission_id'],'task':task})
                st.session_state['selected_job']=job['job_id']
                st.session_state['submission_id']=uuid4().hex
                st.success('任务已提交。刷新状态即可查看进度，不需要再次提交。')
            except Exception:
                st.error('提交未确认，请先刷新记录。重复提交同一请求标识不会重复执行。')
        if st.button('刷新任务状态'): st.rerun()
        try:
            jobs=operator_request()
        except Exception:
            st.warning('任务服务未连接；审计记录仍可查看。'); return
        if not jobs:
            st.caption('尚无从网页提交的任务。'); return
        by_id={j['job_id']:j for j in jobs}
        if st.session_state.get('selected_job') not in by_id:
            st.session_state['selected_job']=jobs[0]['job_id']
        chosen=st.selectbox('网页任务',list(by_id),key='selected_job',format_func=lambda key:
            LABELS.get(by_id[key]['status'],by_id[key]['status'])+' · '+by_id[key]['task'][:45]+' · '+key[:8])
        job=by_id[chosen]
        st.write('状态：'+LABELS.get(job['status'],job['status']))
        if job['status']=='RELEASED': st.write(job['content'])
        elif job['status'] not in ('QUEUED','RUNNING'):
            st.warning(REASONS.get(job.get('reason_code'),'任务未完成。请查看审计记录中的失败阶段；已执行的操作不会自动撤回。'))
            st.caption('原因代码：'+str(job.get('reason_code'))+'。不会自动重跑；如需再次执行，明确提交一个新任务。')
        if job.get('session_id') and st.button('查看此任务的审计记录'):
            st.session_state['audit_session']=job['session_id']; st.rerun()
        return job
