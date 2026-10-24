# OpenCode 与通用 Agent 接入

正式包提供 `agentguard-v2-connect`，不依赖 `ai/bin`、benchmarks 或源码目录。Python >= 3.12；运行时使用已有 Runtime、PolicyCompiler、GuardService 和工具注册表。MCP stdio 是跨 Agent 接入层，OpenCode 1.19.4 是本机验证目标。

## 安装

从交付 wheel 安装（路径按实际文件替换）：

```bash
python3.12 -m venv ~/.local/share/agentguard/connector-venv
~/.local/share/agentguard/connector-venv/bin/python -m pip install '/absolute/path/agentguard-2.0.0a2-py3-none-any.whl[connect]'
```

源码构建：`python -m build --wheel`。也可在开发虚拟环境执行 `python -m pip install '.[connect]'`。Windows 用虚拟环境的 `Scripts/python.exe` 和 `Scripts/agentguard-v2-connect.exe`；本轮未验证原生 Windows。

## 配置和任务

复制 `examples/v2/connector.json` 到工具目录外，调整 `workspace`、`state_directory` 和两个模型配置；模型 API 使用 OpenAI 兼容的 chat/completions 接口。配置内只填写密钥环境变量名。工具目录、私有状态、配置、密钥文件和宿主任务文件不能重叠。相对 workspace/state 路径基于配置文件目录解析。

私有环境文件只接受字面的 `KEY=value` 或 `export KEY='value'`，不执行 shell，不展开 `$()`、变量或反引号。现有环境变量优先。只读取配置指定的两个密钥和 `AGENTGUARD_V2_CLOUD_ENABLED`；其他环境键忽略。`--enable-cloud` 明确启用模型 API；普通工具执行会产生配置的供应商费用。

默认提供 `read_text`、`list_files`、`write_text`；仅配置 `network_destinations` 后才提供 `send_http`。文件参数必须用绝对路径，目录列表可用 workspace 根路径。任意 shell、执行代码、委派和额外网络通路不在默认工具集中。

## 推荐：每次启动一个受管 OpenCode 任务

```bash
agentguard-v2-connect run-opencode \
  --config ~/.config/agentguard/project-agent.json \
  --env-file ~/.config/agentguard/live.env --enable-cloud \
  --task '读取 /absolute/workspace/readme.txt，生成摘要并写入 /absolute/workspace/summary.txt'
```

在桌面项目中也可以用 `bash ai/bin/run_guarded_opencode.sh '任务'`。此脚本调用安装包，不使用实验桥。

启动器冻结任务到私有运行目录，启动全新的 OpenCode chat，选择 `agentguard` agent，仅开放 `agentguard_*` MCP 工具，拒绝原生工具、其他 MCP 工具和委派。使用 `--pure` 及独立 XDG/配置路径；每次有独立 Runtime 库，不争用网页网关的数据库。默认最多 600 秒，可用 `--timeout` 调整。

OpenCode 的 JSON 事件被缓存在私有目录中。启动器只向 stdout 输出 Runtime **FINAL 已释放**的文本。未调用 `guard_finalize`、FINAL 被拒绝、超时或 OpenCode 异常均退出失败，不输出未经审查的模型最终文本。`--json` 返回已释放结果 JSON，**不是**未经筛选的事件流。日志目录在启动时打印；`events.jsonl`、`stderr.log`、`runtime/external/task-*/state.sqlite3`、`connection.json` 和 `final-output.json` 用于复核。它们可能含任务正文，按私有数据管理。

## 在现有 OpenCode 中配置 MCP

先由宿主写一个固定任务文本，不能让 Agent 通过工具修改任务文件：

```bash
agentguard-v2-connect export --client opencode \
  --config ~/.config/agentguard/project-agent.json \
  --task-file ~/.config/agentguard/opencode-task.txt \
  --env-file ~/.config/agentguard/live.env --enable-cloud \
  --output /absolute/project/opencode.json

opencode mcp list --pure
opencode --pure --agent agentguard
```

导出合并名为 `agentguard` 的 MCP 和 agent 配置，保留已有 provider、其他 agent 和服务器；已有默认 agent 保留，未指定默认时设为 agentguard。同名不同值会拒绝覆盖；JSONC 文件请先导出到新 JSON 文件后人工合并。可明确选择 `--agent agentguard`；普通 Build agent 不自动受管。在普通交互界面里，MCP 只能审查提交到 `guard_finalize` 的文本，不能强制拦截 OpenCode 直接显示的最终回答。需要最终显示门禁时用上面的 `run-opencode`。

一个 MCP 连接绑定一个宿主任务；任务完成后后续工具调用会拒绝。更换任务请启动新进程，不能把编辑任务文件当成当前进程更新授权。OpenCode 如提供 `ai.opencode/sessionID` 元数据，会绑定首个 chat，拒绝另一个 chat 复用；不提供该元数据的客户端必须为每个任务建立独立连接。断线/进程重启也代表新任务，不能依赖它恢复或自动重放有副作用的操作。

## 其他 Agent

```bash
# Claude Desktop、Cursor 等采用 mcpServers 结构的客户端
agentguard-v2-connect export --client mcp --config /private/app.json \
  --task-file /private/task.txt --env-file /private/live.env --enable-cloud \
  --output /private/mcp-config.json

# VS Code MCP 的 servers 结构
agentguard-v2-connect export --client vscode --config /private/app.json \
  --task-file /private/task.txt --env-file /private/live.env --enable-cloud \
  --output /private/vscode-mcp.json
```

客户端实际识别的文件位置由客户端决定；将生成的块放入相应配置。生成的是标准 stdio command/args，路径含空格/中文无需 shell 拼接。其他 Agent 的原生工具、额外 MCP、委派和直接回答不会自动受控，需要宿主自己关闭旁路或接入 RuntimePort。模板兼容不等于所有客户端均已实机测试。

已有工具可用 application 的 `tool_factory: "your_package.tools:registry"` 注册：该可信函数接收 settings，返回带 ToolManifest 和 handler 的 ToolRegistry；MCP 门面自动暴露注册的普通 JSON 工具，无需修改客户端适配器。不得根据远程描述推断权限、把任意工具默认标为 READ，或给任意 shell 伪造资源边界。MCP 名称须匹配 `[A-Za-z0-9_-]{1,64}`，`guard_status`/`guard_finalize` 保留；使用 artifact capability 参数的高级工具须走 RuntimePort。

## 诊断和边界

```bash
agentguard-v2-connect doctor --config /private/app.json \
  --task-file /private/task.txt --env-file /private/live.env --enable-cloud
```

doctor 实际启动子进程，完成 MCP initialize/tools/list/status 握手，不调用云端模型；READY 证明安装、配置和协议可用，不代表模型服务可用或防护效果验收。执行失败保留状态和审计；错误响应不回显异常正文、工具参数或供应商内容。

所有已释放工具观察都持续进入 Runtime 上下文及来源传播；调用串行执行。审查未决、未知副作用、风险阻断、运行时异常或取消后停止该连接，不提供“重置风险继续执行”工具。常规参数校验失败在调模型和执行前拒绝。

MCP 门面发生在工具边界，无法观察外部 Agent 全部推理、隐藏上下文、自动标题/压缩或任意其他消息。即使 run-opencode 对用户显示的最终结果有门禁，也不能宣称模型请求本身获得了全链路保护。需要全链路 model prepare/record/finalize、可验证清空上下文及安全恢复时，使用 [RuntimePort/HostAdapter](RUNTIME.md) 和 [原生宿主接入](OPERATIONS.md)，不要用 MCP 伪装完整宿主。

OpenCode 协议和权限格式参照官方 [MCP servers](https://opencode.ai/docs/en/mcp-servers)、[Agents](https://opencode.ai/docs/agents/)、[Plugins](https://opencode.ai/docs/plugins/)。
