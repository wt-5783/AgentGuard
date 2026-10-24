"""Six read-only views over the gateway or a labeled, sanitized recording."""
import json
import os
from datetime import datetime
from pathlib import Path
from urllib import request, error


class ConsoleReader:
    def __init__(self, base_url, token):
        from urllib.parse import urlsplit
        address = urlsplit(base_url)
        if address.scheme not in ('http', 'https') or address.username or address.password or not address.hostname:
            raise ValueError('Invalid gateway address')
        if address.scheme == 'http' and address.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Use HTTPS for nonlocal gateway')
        self.base, self.token = base_url.rstrip('/'), token

    def get(self, path):
        req = request.Request(self.base + path, headers={'Authorization': 'Bearer ' + self.token})
        from agentguard.v2.review.clients import _NoRedirect
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=15) as response:
                body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ValueError('Read limit exceeded')
            return json.loads(body)
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError('Gateway HTTP ' + str(code)) from None


def session_summary(events, metrics):
    """Describe observed outcomes; a review error is never attack prevention."""
    final = next((e for e in reversed(events) if e['event_type'] == 'FINAL_RELEASED'), None)
    failures = [e for e in events if e['event_type'] == 'REVIEW_FAILED']
    blocked = [e for e in events if e['event_type'] == 'CALL_BLOCKED']
    decisions = [e for e in events if e['event_type'] == 'REVIEW_DECIDED'
                 and e.get('payload', {}).get('decision') == 'BLOCK']
    # Do not count a review and its corresponding CALL_BLOCKED twice as attacks.
    decisive = [e for e in events if e['event_type'] in
                ('FINAL_RELEASED', 'REVIEW_FAILED', 'CALL_BLOCKED', 'EXECUTION_UNKNOWN', 'MODEL_TURN_PREPARED')
                or (e['event_type'] == 'REVIEW_DECIDED' and e.get('payload', {}).get('decision') == 'BLOCK')]
    last = decisive[-1] if decisive else None
    kind = last['event_type'] if last else None
    if kind == 'FINAL_RELEASED':
        state, title = 'success', '最终输出已放行'
    elif kind in ('REVIEW_FAILED', 'EXECUTION_UNKNOWN'):
        state, title = 'warning', '流程未完成，需要检查'
    elif kind in ('CALL_BLOCKED', 'REVIEW_DECIDED'):
        state, title = 'warning', '操作或内容被阻断'
    else:
        state, title = 'info', '尚无最终结果'
    elapsed = None
    if len(events) >= 2:
        elapsed = max(0, (datetime.fromisoformat(events[-1]['timestamp'].replace('Z', '+00:00')) -
                          datetime.fromisoformat(events[0]['timestamp'].replace('Z', '+00:00'))).total_seconds())
    return dict(state=state, title=title, final=final, failures=failures,
                blocked=blocked, decisions=decisions, elapsed=elapsed,
                executed=metrics.get('executions', {}).get('SUCCEEDED', 0),
                reviews=metrics.get('review_attempts', 0))


def visible_released_content(paths, prefix, ref):
    """Use only content already released and explicitly exposed by the API."""
    if not ref:
        return None
    key = prefix + '/artifacts/' + ref['artifact_id'] + '/versions/' + str(ref['version']) + '/comparison'
    item = paths.get(key, {})
    if item.get('synthetic_content_visible') is True and item.get('released') is True:
        return item.get('content')
    return None


def render_overview(st, paths, prefix, session, metrics):
    events = paths[prefix + '/events']
    summary = session_summary(events, metrics)
    getattr(st, summary['state'])(summary['title'])
    if summary['state'] == 'info':
        st.caption('当前记录没有最终放行。任务可能执行中，也可能已中断；点击刷新记录确认最新事件。')
    elif summary['failures'] and summary['state'] != 'success':
        st.caption('审查请求或模型响应未通过校验，流程停止放行。这不等于成功识别了一次攻击。')
    elif summary['state'] == 'success':
        st.caption('本次最终回答已经通过安全门禁。是否完成用户目标，仍需核对下面的回答内容。')

    a, b, c, d = st.columns(4)
    a.metric('工具执行成功', str(summary['executed']) + ' 次')
    b.metric('安全审查', str(summary['reviews']) + ' 次')
    c.metric('审查失败', str(len(summary['failures'])) + ' 次')
    d.metric('记录时间跨度', '—' if summary['elapsed'] is None else f"{summary['elapsed']:.0f} 秒")
    st.caption('时间跨度从会话创建到最后一条事件，不含创建前的策略生成；不代表当前仍在运行。')

    artifacts = paths[prefix + '/artifacts']
    task = next((a for a in artifacts if a.get('labels', {}).get('integrity') == ['trusted_user']), None)
    st.subheader('本次任务')
    task_content = visible_released_content(paths, prefix, task)
    st.write(task_content if task_content is not None else '任务正文受保护；当前只读界面不展示。')
    st.subheader('最终回答')
    final_ref = summary['final']['payload'] if summary['final'] else None
    content = visible_released_content(paths, prefix, final_ref)
    if content is not None:
        st.write(content)
        if summary['state'] != 'success':
            st.caption('以上是此前已放行的回答；此后还有新事件，请以当前状态为准。')
    else:
        st.write('最终回答已放行，正文受保护。' if final_ref else '尚未记录已放行的最终回答。')

    st.subheader('防护发生了什么')
    st.write(f"工具调用阻断 {len(summary['blocked'])} 次；语义审查 BLOCK 决策 {len(summary['decisions'])} 次。")
    st.caption('两类记录可能属于同一次操作，不能相加作为攻击数量；阻断也可能是误拦，需要独立评测确认。')
    if not summary['blocked'] and not summary['decisions']:
        st.write('本次未记录阻断。这次任务可用于检查正常运行流程，不能据此计算攻击拦截率。')
    names = {'SESSION_CREATED': '建立任务与权限范围', 'MODEL_OUTPUT_RECORDED': '模型提出动作或回答',
             'EXECUTION_SUCCEEDED': '工具实际执行成功', 'RESULT_RELEASED': '工具结果通过审查',
             'FINAL_RELEASED': '最终回答通过审查', 'CALL_BLOCKED': '工具调用被阻断',
             'REVIEW_FAILED': '审查失败，未放行', 'EXECUTION_UNKNOWN': '执行结果未知，需人工检查'}
    rows = []
    for event in events:
        if event['event_type'] in names:
            rows.append({'步骤': event['seq'], '发生的事情': names[event['event_type']],
                         '说明代码': event.get('payload', {}).get('reason_code', '')})
    with st.expander('查看关键过程', expanded=False):
        st.dataframe(rows, hide_index=True, width='stretch')
    with st.expander('这份记录能证明什么', expanded=False):
        st.write('它记录本次执行、审查和放行的实际过程。真实防护效果还需要正常/攻击样本、开启/关闭防护对照和独立评分。')
        st.write('本控制台仅查看记录；创建和执行任务仍通过宿主命令或 SDK。')
        st.caption('会话标识：' + session['session_id'] + '；底层会话状态：' + session['status'] +
                   '。ACTIVE 表示会话可用，不等于任务正在执行。')


def capture(reader, sid):
    from urllib.parse import quote
    prefix = '/v2/sessions/' + quote(sid, safe='')
    data = {'format': 'agentguard-console-0.1', 'mode': 'recorded', 'session_id': sid, 'paths': {}}
    paths = data['paths']
    for suffix in ('', '/policy', '/graph', '/metrics', '/reviews', '/artifacts'):
        paths[prefix + suffix] = reader.get(prefix + suffix)
    events, cursor = [], 0
    while True:
        page = reader.get(prefix + '/events?after_seq=' + str(cursor) + '&limit=1000')
        events.extend(page)
        if len(page) < 1000:
            break
        cursor = page[-1]['seq']
    paths[prefix + '/events'] = events
    for review in paths[prefix + '/reviews']:
        path = prefix + '/reviews/' + review['review_id'] + '/input?attempt=' + str(review['attempt'])
        paths[path] = reader.get(path)
    for item in paths[prefix + '/artifacts']:
        path = prefix + '/artifacts/' + item['artifact_id'] + '/versions/' + str(item['version']) + '/comparison'
        paths[path] = reader.get(path)
    return data


def run():
    import streamlit as st
    st.set_page_config(page_title='AgentGuard v2', layout='wide')
    st.title('AgentGuard v2 安全控制台')
    replay_path = os.environ.get('AGENTGUARD_V2_REPLAY')
    if replay_path:
        record = json.loads(Path(replay_path).read_text(encoding='utf-8'))
        if record.get('format') != 'agentguard-console-0.1' or record.get('mode') != 'recorded':
            st.error('不是受支持的控制台录制包'); return
        st.warning('录制回放 · 不调用模型，不执行工具 · 数字属于录制时的记录')
        sid = record['session_id']
        paths = record['paths']
        get = lambda path: paths[path]
    else:
        st.caption('查看任务结果与防护记录 · 只读界面，刷新不会执行任务')
        base = st.sidebar.text_input('网关', os.environ.get('AGENTGUARD_V2_GATEWAY', 'http://127.0.0.1:8765'))
        token = st.sidebar.text_input('UI 凭据', os.environ.get('AGENTGUARD_UI_TOKEN', ''), type='password')
        if not token:
            st.caption('设置 UI 凭据后读取会话。'); return
        reader = ConsoleReader(base, token)
        try:
            sessions = reader.get('/v2/sessions')
            if not sessions:
                st.caption('尚无会话。通过原生宿主或 SDK 创建任务后显示。'); return
            sid = st.sidebar.selectbox('选择执行记录（最新在前）', [s['session_id'] for s in sessions],
                format_func=lambda value: '执行记录 · ' + value.removeprefix('session_')[:8])
            if st.sidebar.button('刷新记录'):
                st.rerun()
            record = capture(reader, sid)
            paths = record['paths']
            get = lambda path: paths[path]
            st.sidebar.download_button('导出当前只读回放', json.dumps(record, ensure_ascii=False, indent=2),
                file_name='agentguard-recording.json', mime='application/json')
        except Exception as exc:
            st.error('无法读取网关：' + type(exc).__name__); return
    prefix = '/v2/sessions/' + sid
    session, metrics = get(prefix), get(prefix + '/metrics')
    tabs = st.tabs(['结果总览', '任务策略', '执行时间线', '实际 Guard 输入', '内容版本对比', '来源与恢复', '用量与评测'])
    with tabs[0]:
        render_overview(st, paths, prefix, session, metrics)
    with tabs[1]:
        st.json(get(prefix + '/policy'))
        st.caption('仅显示有限授权。任务原文与策略解释默认遮盖；模型候选不能提高宿主上限。')
    with tabs[2]:
        events = get(prefix + '/events')
        st.dataframe([dict(seq=e['seq'], time=e['timestamp'], event=e['event_type'],
            call=e['call_id'], review=e['review_id'], details=json.dumps(e['payload'], ensure_ascii=False)) for e in events], use_container_width=True)
        st.json(metrics['executions'])
        st.caption('批准、已执行、已释放相互独立；UNKNOWN 表示副作用未确定。')
    with tabs[3]:
        reviews = get(prefix + '/reviews')
        if reviews:
            selected = st.selectbox('审查/尝试', range(len(reviews)),
                format_func=lambda i: reviews[i]['phase'] + ' ' + reviews[i]['review_id'] + ' #' + str(reviews[i]['attempt']))
            review = reviews[selected]
            st.json(review)
            st.json(get(prefix + '/reviews/' + review['review_id'] + '/input?attempt=' + str(review['attempt'])))
        else:
            st.caption('尚无审查记录。')
    with tabs[4]:
        objects = get(prefix + '/artifacts')
        if objects:
            selected = st.selectbox('对象版本', range(len(objects)),
                format_func=lambda i: objects[i]['artifact_id'] + ' v' + str(objects[i]['version']))
            item = objects[selected]
            result = get(prefix + '/artifacts/' + item['artifact_id'] + '/versions/' + str(item['version']) + '/comparison')
            if not result.get('synthetic_content_visible'):
                st.caption('原文默认遮盖。只有宿主标记的假数据且服务端显式开启时可见。')
            columns = st.columns(max(1, min(len(result['versions']), 3)))
            for i, version in enumerate(result['versions']):
                with columns[i % len(columns)]:
                    st.write('版本', version['version'], '已释放', version['released'])
                    st.json(version['content']) if isinstance(version['content'], (dict, list)) else st.code(version['content'])
    with tabs[5]:
        edges = get(prefix + '/graph')['edges']
        dot = ['digraph lineage { rankdir=LR;']
        for edge in edges:
            source = edge['source_ref']; target = edge['target_ref']
            dot.append(json.dumps(source['artifact_id'] + ':v' + str(source['version'])) + ' -> ' +
                json.dumps(target['artifact_id'] + ':v' + str(target['version'])) +
                ' [label="' + edge['kind'] + '",style=' + ('dashed' if edge['kind'] == 'possible' else 'solid') + '];')
        st.graphviz_chart('\n'.join(dot + ['}']))
        st.json({'plans': metrics['recovery_plans'], 'acks': metrics['recovery_acks']})
        st.caption('虚线为可能依赖。恢复不会撤回已发生的外发、删除或最终输出。')
    with tabs[6]:
        usage = [u for u in metrics['usage'] + metrics.get('model_usage', []) + metrics.get('policy_usage', []) if u]
        known_in = sum(u.get('input_tokens') or 0 for u in usage)
        known_out = sum(u.get('output_tokens') or 0 for u in usage)
        unknown = any(u.get('input_tokens') is None or u.get('output_tokens') is None for u in usage)
        a, b, c = st.columns(3)
        a.metric('已知输入 token', known_in); b.metric('已知输出 token', known_out)
        c.metric('审查次数', metrics['review_attempts'])
        if unknown:
            st.warning('部分供应商用量未知；已知小计不等于总成本。')
        price_in = st.number_input('输入价格/百万 token（自行填入当前价格）', min_value=0.0)
        price_out = st.number_input('输出价格/百万 token', min_value=0.0)
        st.write('按已知用量估算费用：', (known_in * price_in + known_out * price_out) / 1_000_000)
        st.caption('费用是按填写单价估算。攻击成功率必须来自独立接收器和任务评分；未开展实验时没有效果数字。')
        report = os.environ.get('AGENTGUARD_V2_REPORT')
        if report:
            st.json(json.loads(Path(report).read_text(encoding='utf-8')))


def main():
    import subprocess
    import sys
    raise SystemExit(subprocess.call([sys.executable, '-m', 'streamlit', 'run', str(Path(__file__).resolve()),
        '--server.address', '127.0.0.1', '--server.port', os.environ.get('AGENTGUARD_V2_UI_PORT', '8502'),
        '--browser.gatherUsageStats', 'false']))


if __name__ == '__main__':
    # Streamlit executes this file as a script, so relative imports in capture()
    # are avoided; provider transport is imported by its absolute package name.
    run()
