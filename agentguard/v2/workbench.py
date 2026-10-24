"""Independent workbench pages; rendering never executes a model or tool."""
import os
import shlex
import sys

PAGES = {'任务工作台': 'tasks', '审计记录': 'audit', '安全测评': 'evaluation',
    'Agent 接入': 'integration', '项目架构': 'architecture', '旧版演示': 'legacy'}


def render_page(st, page):
    if page == '安全测评':
        from .evaluation_console import render_workspace
        render_workspace(st)
    elif page == 'Agent 接入':
        render_integration(st)
    elif page == '项目架构':
        render_architecture(st)
    elif page == '旧版演示':
        st.write('v1 演示用于回放六层防护和权能生命周期，实验数据与 v2 分开保存。')
        address = os.environ.get('AGENTGUARD_LEGACY_URL')
        if address:
            from .services import LocalServiceClient
            try:
                LocalServiceClient(address)
                st.link_button('单独打开旧版演示', address)
                from streamlit.components.v1 import iframe
                iframe(address, height=1000, scrolling=True)
                return
            except ValueError:
                st.warning('旧版服务地址必须是带端口的本机 HTTP 地址。')
        else:
            st.info('旧版演示未配置运行地址。源码目录可用下列命令单独启动，再设置 AGENTGUARD_LEGACY_URL。')
        st.code('python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501', language='bash')


def render_integration(st):
    from .integration import PROFILES, capability_rows
    st.write('以 OpenCode 原生接入为完整宿主参考。其他 Agent 可按可接管的边界选择 MCP 或 RuntimePort。')
    st.dataframe(capability_rows(), hide_index=True, width='stretch')
    client = st.selectbox('目标适配方式', list(PROFILES)[:-1], format_func=lambda key: PROFILES[key]['name'])
    profile = PROFILES[client]
    st.info(profile['scope'])
    st.caption('验证范围：' + profile['verified'])
    config = st.text_input('可信应用配置路径', os.environ.get('AGENTGUARD_CONNECTOR_CONFIG', '/private/application.json'))
    env_file = st.text_input('私有环境文件路径（可选）', os.environ.get('AGENTGUARD_CONNECTOR_ENV', ''))
    task_file = None if client == 'opencode-native' else st.text_input('固定宿主任务文件路径', '/private/task.txt')
    python = os.environ.get('AGENTGUARD_CONNECTOR_PYTHON', sys.executable)
    common = ['--client', client, '--config', config]
    if env_file:
        common += ['--env-file', env_file]
    if task_file is not None:
        common += ['--task-file', task_file]
    prefix = [python, '-I', '-m', 'agentguard.v2.connect']
    st.subheader('1 · 检查配置')
    st.caption('命令只在这里生成。check 不调用模型、不导入工具工厂、不创建运行时数据库。')
    st.code(shlex.join(prefix + ['check'] + common), language='bash')
    st.subheader('2 · 导出与连接')
    output = st.text_input('新配置包目录（工具根目录之外）', '/private/agentguard-bundle')
    st.caption('bundle 本身不调用模型；--enable-cloud 使生成的连接配置能在用户执行任务时调用模型。')
    st.code(shlex.join(prefix + ['bundle'] + common + ['--enable-cloud', '--output', output]), language='bash')
    if client == 'opencode-native':
        command = [python, '-I', '-m', 'agentguard.v2.opencode', 'launch', '--config', config]
        if env_file:
            command += ['--env-file', env_file]
        st.code(shlex.join(command), language='bash')
        st.write('原生窗口默认 ON，F6 或点击切换当前会话，/guard 查看防护状态。原生工具需通过可信注册表接入。')
    else:
        command = prefix + ['doctor', '--config', config, '--task-file', task_file]
        if env_file:
            command += ['--env-file', env_file]
        st.code(shlex.join(command + ['--enable-cloud']), language='bash')
        st.write('doctor 实际验证 MCP 握手，模型调用为零。client.json 按目标客户端格式合并；每任务建立独立连接。')
    st.subheader('3 · 验收宿主边界')
    st.write('依次验证正常读写、越界拒绝、FINAL 拒绝、断线后不自动重放。完整宿主还需验证模型实际输入和恢复清空后的 ACK。')
    st.caption('模板兼容与安装成功不等于客户端已实测或防护效果已成立。标准 MCP 无法强制审查宿主直接显示的回答。')


def render_architecture(st):
    st.write('工作台负责操作与查看；所有实际工具效果由 Runtime 执行。独立评测使用同一适配包，在隔离环境中运行 ON/OFF 两组。')
    st.graphviz_chart('''digraph architecture {
        rankdir=TB; node [shape=box, style=rounded];
        workbench [label="统一工作台 :8502"];
        operator [label="任务服务 :8766"];
        gateway [label="Runtime 网关 :8765"];
        native [label="OpenCode 原生宿主"];
        mcp [label="通用 MCP 宿主"];
        runtime [label="Runtime / Policy / Guard"];
        state [label="私有状态 / 来源 / 恢复"];
        tools [label="注册工具 / 原子许可"];
        eval [label="独立评测 :8510"];
        sandbox [label="隔离 ON / OFF + 副作用评分"];
        workbench -> operator [label="提交任务"];
        workbench -> gateway [label="只读审计"];
        operator -> gateway [label="宿主凭据"];
        gateway -> runtime; native -> runtime; mcp -> runtime;
        runtime -> tools; runtime -> state;
        workbench -> eval [label="测评 API"];
        eval -> sandbox; sandbox -> native;
    }''')
    st.dataframe([
        {'层': '展示', '模块': 'console / workbench / operator_console / evaluation_console', '职责': '导航、提交、只读审计、报告；不持有模型密钥'},
        {'层': '宿主与适配', '模块': 'application / adapters / connect / opencode_host', '职责': '固定任务、模型消息、注册工具、FINAL 和恢复握手'},
        {'层': '安全运行时', '模块': 'runtime / policy / review / execution', '职责': '宿主权限上限、语义审查、原子许可与执行'},
        {'层': '私有存储', '模块': 'state / recovery', '职责': '每执行器独立 SQLite、版本、来源与恢复日志'},
        {'层': '独立评测', '模块': 'ai/eval', '职责': '隔离双组、固定输入、真实文件/HTTP 观测、独立评分'},
        {'层': '兼容层', '模块': 'app.py / agentguard v1', '职责': '历史演示和 SDK；与 v2 数据及成绩分开'},
    ], hide_index=True, width='stretch')
    st.caption('每个执行器只拥有自己的状态库。UI 凭据只读，操作员凭据只能提交固定配置的任务；原生窗口和测评不共用网页 Runtime 库。')
