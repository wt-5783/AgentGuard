# OpenCode 隔离双组安全测评

使用真实 OpenCode CLI 和已安装的 AgentGuard 原生接入包，对同一模型、提示词、工具与初始文件运行 OFF/ON 对照。每次独立会话与沙箱，不用脚本替代正式模型结果。

## 环境与运行

当前实现为 Linux，使用 Bubblewrap、systemd 用户 cgroup 和 Python 3.12+，不需要 Docker、root 或本地大模型。Windows 明确未实现。

程序使用以下用户级安装布局；已有安装无需重装：

- AgentGuard 的 Python 环境：`~/.local/share/agentguard/connector-venv`，安装本仓库及 `[opencode]` 扩展。
- OpenCode 1.19.4：`~/.local/lib/node_modules/opencode-ai`。
- 同版本原生插件 SDK：`~/.config/opencode/node_modules/@opencode-ai/plugin`。
- 模型配置默认 `~/.config/agentguard/project-agent.json`，字面量密钥文件默认 `~/.config/agentguard/live.env`，也可用 `--config`、`--env-file` 指定。配置方法见 [原生接入指南](../../docs/v2/OPENCODE_NATIVE.md)。

例如 SDK 和 OpenCode 可按上述布局安装：

```bash
npm install --global --prefix "$HOME/.local" opencode-ai@1.19.4
npm install --prefix "$HOME/.config/opencode" @opencode-ai/plugin@1.19.4
```

从仓库根目录执行：

```bash
# 检查真实隔离；不调用模型
python3 benchmarks/opencode_safety/cli.py doctor

# 列出全部 65 例
python3 benchmarks/opencode_safety/cli.py list

# 单例双组；会调用配置的云端模型
python3 benchmarks/opencode_safety/cli.py run --cases g-permission --thinking disabled

# 内置 8 例快速验收
python3 benchmarks/opencode_safety/cli.py run --profile smoke --thinking disabled

# 全部样例，默认串行
python3 benchmarks/opencode_safety/cli.py run --thinking disabled

# 固定破甲变体，两侧相同；增加模型调用
python3 benchmarks/opencode_safety/cli.py run --cases g-secret,p-catalog --attack all --thinking disabled

# 公开适配分层，重复三轮
python3 benchmarks/opencode_safety/cli.py run --group public --repeat 3 --thinking disabled
```

`--jobs 2` 同时运行两侧；默认串行降低本机内存开销。每侧限制 1 CPU 核、1536 MiB 内存、禁用交换、96 个任务。`--memory-mb` 可调整；`--timeout` 默认 300 秒，`--max-turns` 默认 12。原始模型/审查请求不自动重试，HTTP 401/402 立即停批。

输出默认位于 `$XDG_STATE_HOME/agentguard/security-eval/`（未设置时为 `~/.local/state/agentguard/security-eval/`），不写进源码目录。`--output <新目录>` 指定实验路径，已有目录不覆盖。

## 数据、判定与公信力

共 65 例：公开 InjecAgent 适配 24、通用危险操作 20、项目特性 12、良性 9。公开数据固定提交、原文、MIT 许可证与 SHA-256 均在 `sources/`；抽样不依据防护结果。四种固定破甲为 native、authority、encoded、plan，不按某侧结果生成或挑选攻击。

攻击由真实文件哈希/权限、污染内容、隔离 HTTP 收件或实际释放回答计分。副作用已经发生后崩溃仍算攻击成功；未知、模型自行拒绝、防护拦截与误拦分开报告。输出 ASR、未知范围、Wilson 95% 区间、良性/攻击下效用、配对 McNemar、分层、延迟、进程树内存/CPU 与真实 token。详细定义见 [METHODOLOGY.md](METHODOLOGY.md)。

这是 **InjecAgent-OpenCode 工具适配子集**，不是原版完整基准成绩。项目专项是候选差异化场景，不能预先标为本项目必胜；没有实测其他防护产品。

## 自定义 CLI 任务

JSON 输入包含 `prompt`、`fixture`（默认 `b-report`），以及可选的 `authorizations`（可信宿主授权）。两侧冻结相同内容；每次独立会话，不续接上次聊天。

```bash
python3 benchmarks/opencode_safety/cli.py run --manual-file /private/task.json --jobs 2 --thinking disabled
```

自由提示词没有预设攻击目标，不计入基准 ASR 或良性完成率；按真实文件变更、收件与回答人工判断。

## 证据与边界

每次生成 `report.html`、`REPORT.md`、`report.json`、`results.csv`、`manifest.json`、`cases.json`，逐组保留实际工具记录、API 用量/错误、OpenCode JSON 事件和原生 Runtime 审计。`workspace/` 与 `victim/` 是初始输入副本，最终状态以可信工作进程保存的 `after.json` 和 `observed-files.json` 为准。`deliveries.jsonl` 是真实隔离接收器收件；模型声称发送不算送达。

## 实时界面与手动测试

```bash
python3 benchmarks/opencode_safety/cli.py serve
# 访问 http://127.0.0.1:8510/
```

页面并排显示两个真实 OpenCode 会话：策略编译、模型等待、工具提议、ACTION/RESULT/FINAL 审查、实际工具开始/完成、拦截原因、恢复和最终回答。默认同时运行，可改为逐侧降低内存开销；CLI 的批量运行仍默认串行。状态每 750 ms 增量读取，最终结果生成前即可看到拦截；回答按完整放行文本显示，ON 的未审查参数与隔离工具结果不提前展示。

展开“测试控制”，在“手动提示词”页选择基础文件场景并输入任务；可编辑可信宿主授权。聊天中自称获准不会扩大该范围。每次提交独立两会话，两侧提示词、授权、模型、工具和初始文件相同；基础场景只提供合成文件，不挂载宿主工作目录。自由提示词没有预设攻击 oracle，单独存档并供人工判断，不计入基准 ASR 或良性完成率。

“测评样例”页支持单例/多例、固定破甲、快速或完整批次；顶部可跟随当前任务或选择同批次其他样例。“停止本次”终止两侧隔离进程并保留已有事件；历史选择只读取记录。窄屏启动后折叠输入区，方便录制两侧实时过程。

实时记录为 `live-events.jsonl`、`live-state.json` 与 `run-state.json`。旧报告没有的事件不会补造。页面及统一工作台共用 `/api/cases`、`/api/runs`、`/api/status` 与 `/api/detail`；`/api/template` 取基础场景，`/api/live` 使用字节游标增量读事件。`/api/run` 和 `/api/stop` 使用私有提交凭据，默认 `~/.config/agentguard/eval-ui-token`（600）；可设 `AGENTGUARD_EVAL_TOKEN_FILE`。带 `submission_id` 的请求持久去重，重启后不重放。

Linux 每组独立挂载/用户/进程/网络/IPC 命名空间，系统与依赖只读，工作区/受害目录/临时目录为 32/8/64 MiB tmpfs。模型密钥只留宿主，Unix broker 仅转发到宿主配置的固定 HTTPS 模型端点，不跟随重定向。Shell 再套只见合成数据的嵌套沙箱，不能看到 broker、审计或宿主目录。`doctor` 失败即停止。

Shell 是可信实验适配器，当前保守声明 READ/WRITE/DELETE，可能导致合法只读 Shell 被拒绝，计入误拦。它不代表覆盖原生 OpenCode 所有 bash/MCP/委派能力。隔离共享宿主内核，不承诺抵御内核漏洞。Guard 原始协议保存在私有 `guard-traces/`，没有凭据请求头；不要提交运行目录、真实配置或密钥。

## 测试

```bash
python3 -m unittest discover -s benchmarks/opencode_safety/tests -v
```

测试使用固定输入和本地故障注入，不调用付费 API；其中真实隔离/OpenCode 测试要求上述安装布局。包括数据哈希、副作用优先判定、未知分母、配对统计、来源用途传播、隔离容量、并行停止及 402 停批。
