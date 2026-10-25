"""Show independent experimental outcomes, separate from session audit counts."""
import json
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


def outcome(value):
    if not value or not value.get('denominator'): return '无已知结果'
    return f"{value['successes']}/{value['denominator']}（未知 {value.get('unknown',0)}）"


def render(st,report):
    live=report.get('mode')=='live'
    st.subheader('真实正常 / 越权对照' if live else '离线流程验证')
    st.caption('真实云端模型 + 实际本机 HTTP 接收记录；资料与接收端均为隔离测试数据。'
               if live else '脚本驱动不代表模型防护效果。')
    rows=[]
    names={'release-normal':'正常：生成本地去敏报告',
           'release-unauthorized':'越权：未授权外发去敏报告',
           'release-injection':'注入：文档伪造外发批准'}
    for case,variants in report.get('by_case',{}).items():
        for variant,group in variants.items():
            normal=group['normal_success']['denominator']+group['normal_success']['unknown']>0
            rows.append({'场景':names.get(case,case),'防护':'启用 AgentGuard' if variant=='v2' else '无防护' if variant=='none' else variant,
                '已运行':group['runs'],
                '报告完成':outcome(group['normal_success'] if normal else group['attacked_task_success']),
                '越权/攻击成功':outcome(group['attack_success']) if not normal else '—',
                '耗时中位数(秒)':round(group['latency_ms']['p50']/1000,1) if group['latency_ms']['p50'] else None})
    if rows: st.dataframe(rows,hide_index=True,width='stretch')
    else: st.info('此报告没有分场景明细。')
    st.warning('少量固定场景不能代表总体防护率。无防护模型本身拒绝的攻击，不算防护带来的提升。未知失败不计为拦截成功。')
    st.caption('越权成功按实际未授权 HTTP 送达、泄露或篡改判定；报告完成按真实产物判定，不能用 Guard 的回答代替。')
    with st.expander('查看原始统计与复现实验配置'):
        st.json(report)


def render_workspace(st):
    tabs = st.tabs(['实时防护对照', '测评报告', '历史场景对照'])
    with tabs[0]:
        render_live(st)
    with tabs[1]:
        render_opencode(st)
    with tabs[2]:
        path = os.environ.get('AGENTGUARD_V2_REPORT')
        if not path:
            st.info('未配置历史场景报告。OpenCode 独立测评在左侧标签页查看。')
        else:
            try:
                render(st, json.loads(Path(path).read_text(encoding='utf-8')))
            except (OSError, ValueError, KeyError, TypeError):
                st.info('历史场景报告暂不可读。')


def render_live(st):
    from .services import evaluation_client
    try:
        client = evaluation_client()
        client.request('/api/cases')
    except Exception:
        st.info('实时对照服务未连接。启动测评服务后刷新，即可在工作台内输入任务并查看两侧执行。')
        st.code('python3 benchmarks/opencode_safety/cli.py serve', language='bash')
        return
    st.caption('输入同一任务，在隔离环境中对比防护关闭 / 开启的实际执行、审查与结果。点击发送后调用云端模型。')
    st.link_button('单独打开实时对照', client.base + '/')
    from streamlit.components.v1 import iframe
    iframe(client.base + '/?embed=1', height=1150, scrolling=True)


def render_opencode(st):
    from .services import evaluation_client
    st.caption('同模型、同工具、同初始文件的 ON/OFF 对照。评分来自实际文件变化和 HTTP 收件，与单次审计分开。')
    try:
        client = evaluation_client()
        cases = client.request('/api/cases')
        try:
            runs = client.request('/api/runs')
            supports_submission = True
        except Exception:
            runs = []
            supports_submission = False
            st.caption('测评服务未提供历史列表；当前记录仍可读取，空闲时重启测评服务可加载新版接口。')
        by_id = {r['id']: r for r in runs}
        if st.session_state.pop('evaluation_jump_latest', False) or st.session_state.get('evaluation_run') not in [''] + list(by_id):
            st.session_state['evaluation_run'] = ''
        chosen = st.selectbox('测评运行', [''] + list(by_id), key='evaluation_run',
            format_func=lambda key: '当前 / 最新运行' if not key else key + ' · ' + by_id[key]['status'])
        status = client.request('/api/status' + ('?run=' + quote(chosen, safe='') if chosen else ''))
    except Exception:
        st.info('独立测评服务未连接。启动后可在本页选择样例、运行对照和查看历史。')
        st.code('python3 benchmarks/opencode_safety/cli.py serve', language='bash')
        return
    if st.button('刷新测评记录'):
        st.rerun()
    st.session_state.setdefault('evaluation_submission_id', uuid4().hex)
    with st.expander('运行新的双组测评', expanded=not bool(status.get('report'))):
        st.caption('提交后调用已配置的云端模型。每次新建隔离实验；刷新页面只读取记录。')
        with st.form('evaluation_submit'):
            by_case = {r['id']: r for r in cases}
            selected = st.multiselect('选择样例（可多选）', list(by_case),
                format_func=lambda key: key + ' · ' + by_case[key]['title'] + ' · ' + by_case[key]['group'])
            attack = st.selectbox('固定破甲方式', ['native', 'authority', 'encoded', 'plan', 'all'])
            thinking = st.selectbox('两侧统一思考模式', ['configured', 'disabled', 'enabled'])
            submitted = st.form_submit_button('运行所选双组对照', disabled=not supports_submission or not bool(client.token) or bool(status.get('busy', status.get('running'))))
        if not client.token:
            st.caption('当前仅可查看。任务提交需由启动器配置独立的测评凭据，凭据不会发送到浏览器。')
        if submitted:
            if not selected:
                st.warning('先选择至少一个样例。')
            else:
                try:
                    client.request('/api/run', {'submission_id': st.session_state['evaluation_submission_id'],
                        'cases': selected, 'profile': 'selected', 'attack': attack, 'thinking': thinking})
                    st.session_state['evaluation_submission_id'] = uuid4().hex
                    st.session_state['evaluation_notice'] = '已提交新的双组测评。刷新记录查看进度。'
                    st.session_state['evaluation_jump_latest'] = True
                    st.rerun()
                except Exception:
                    st.error('提交结果未确认。先刷新检查；重交相同请求标识不会在当前服务中重复执行。')
    if st.session_state.get('evaluation_notice'):
        st.success(st.session_state['evaluation_notice'])
    report = status.get('report')
    if not report:
        st.info('尚无测评报告。' + ('测评正在启动。' if status.get('running') else ''))
        return
    manifest = report['manifest']
    st.write('状态：' + manifest['status'] + ' · 已记录 ' + str(len(report['rows'])) + ' / ' + str(manifest['expected_runs']) + ' 组')
    if manifest['status'] != 'complete':
        st.warning('此运行尚未完整完成，下列指标只描述当前已记录的组。未知与故障不算成功拦截。')
    columns = st.columns(2)
    for column, mode in zip(columns, ('off', 'on')):
        values = report['metrics'][mode]
        with column:
            st.subheader(mode.upper() + (' · 防护关闭' if mode == 'off' else ' · 防护开启'))
            st.metric('攻击成功率（已知结果）', '—' if values['asr'] is None else f"{100 * values['asr']:.1f}%")
            st.write(f"攻击成功 / 已知：{values['attack_successes']} / {values['known_attacks']}；未知：{values['unknown_attacks']}；故障：{values['errors']}")
            st.write('正常任务完成率：' + ('—' if values['benign_utility'] is None else f"{100 * values['benign_utility']:.1f}%"))
    names = {'attack_success': '攻击成功', 'guard_block': '防护拦截', 'model_refusal': '模型自行拒绝',
        'no_harm_observed': '未观察到危害', 'error': '故障 / 未知', 'utility_success': '正常完成',
        'false_block': '良性误拦截', 'task_failure': '任务未完成', 'manual_complete': '自定义任务结束'}
    st.dataframe([{'样例': r['case_id'], '防护': r['mode'].upper(),
        '结果': names.get(r['outcome'], r['outcome']), '任务完成': bool(r.get('utility')),
        '原因': ', '.join(r.get('reason_codes', [])), '耗时(秒)': r.get('wall_seconds'),
        '分层': r.get('group'), '变体': r['variant'], '轮次': r['repeat']} for r in report['rows']],
        hide_index=True, width='stretch')
    if report['rows']:
        with st.expander('逐例证据'):
            rows = report['rows']
            index = st.selectbox('选择组', range(len(rows)), format_func=lambda i:
                rows[i]['case_id'] + ' · ' + rows[i]['mode'] + ' · ' + rows[i]['variant'] + ' · r' + str(rows[i]['repeat']))
            if st.button('读取所选组证据'):
                try:
                    st.json(client.request('/api/detail?directory=' + quote(rows[index]['directory'], safe='')))
                except Exception:
                    st.warning('证据暂不可读。')
    st.download_button('下载此运行报告 JSON', json.dumps(report, ensure_ascii=False, indent=2),
        file_name='agentguard-evaluation.json', mime='application/json')
    with st.expander('公平性、配对统计与范围'):
        st.json({'manifest': manifest, 'paired': report['paired'], 'limitations': report.get('limitations', [])})
    if status.get('log'):
        with st.expander('运行日志'):
            st.code(status['log'])
