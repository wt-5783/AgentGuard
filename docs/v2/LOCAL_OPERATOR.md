# 本机云端任务与协议稳定性

本机只运行 Python、网关和控制台，无需本地模型或独显。模型仍需可用的云端密钥。配置样例为 `config/v2.example.json`；保留既有 Python 3.12 安装流程。

## 网页提交

已有网关只读凭据不具备任务提交权限。可另行启动本机操作员服务：

```bash
# 先启动现有网关，所有凭据由部署者在环境中提供，彼此不同。
export AGENTGUARD_V2_CONFIG=/absolute/path/to/config.json
export AGENTGUARD_V2_GATEWAY=http://127.0.0.1:8765
python -m agentguard.v2.jobs
```

服务仅监听 `127.0.0.1:8766`，需要独立的 `AGENTGUARD_OPERATOR_TOKEN`，以及连接网关用的 `AGENTGUARD_HOST_TOKEN`；检查它们与 `AGENTGUARD_UI_TOKEN` 不同。控制台进程设置 `AGENTGUARD_OPERATOR_URL=http://127.0.0.1:8766` 和 operator/UI 凭据即可显示表单，不需给控制台供应商密钥或 host 凭据。没有 operator 配置时保留只读审计行为。不要将本机操作员服务公开到网络。

提交只接受 `submission_id`（32 位小写十六进制）及任务文字（1–4000 字符），不允许用户选择工具根、端点或工厂。服务端固定配置、单工作线程、最多 5 个待处理任务。任务状态与最终获准回答持久保存于 `state_directory/operator-jobs`。

同一标识与同一任务的重试返回已有任务，不重复执行；同一标识更换正文被拒绝。进程中断后，QUEUED/RUNNING 标记为 INTERRUPTED，不自动重放。失败原因不返回原始供应商异常。使用者应先核对已执行操作，再显式提交新任务；新任务不撤销旧副作用。RELEASED 只代表最终回答获准展示，任务产物是否完整仍要独立核对。

## 协议及循环控制

- `thinking` 可显式指定 enabled/disabled，`temperature` 可配置；不会暗中切换本地模型。
- `protocol_retries` 为 0–2 次额外格式重生成。仅 INVALID_SCHEMA/INVALID_REFERENCE/MISSING_EVIDENCE/INVALID_MODEL_OUTPUT 可重试；合法 BLOCK 不重试。输入保留原授权证据，仅添加宿主固定格式提示，不把错误模型正文追加成新指令。
- Guard 的 `compact_protocol=true` 使用请求内 E0/E1 引用别名，服务端恢复精确绑定后仍通过原有完整验证器。别名不能引用请求以外对象，也不降低证据定位或权限检查。
- 传输重试仍受共享预算、并发及次数上限约束；认证/证书错误不重试，连接中断、超时、429/5xx 可有界重试。未知用量不能算作零。
- 成功工具观察后追加有限执行回执。连续相同的已完成批次不会重新执行；第一次重复给有限反馈，第二次停止为 HOST_REPEATED_CALL_LIMIT。只针对完全相同的连续批次，不声称能识别所有语义重复或跨任务去重。
- WRITE/SEND 规则同时覆盖任务需要的来源与目标范围；策略提示解释该语义，宿主上限校验不变。没有对应授权时仍拒绝。

## 对照报告

控制台设置 `AGENTGUARD_V2_REPORT=/absolute/path/to/report.json`，可在“真实对照评测”查看分场景统计。评测器的 `by_case` 将正常任务、直接越权与文档注入分开。实际未授权 HTTP 送达即算越权，无需同时泄露凭据；未知结果单列。

评测目录保留 `manifest.json`、`runs.json`、`report.json` 及各次私有审计。manifest 记录源码摘要、Git 提交、配置摘要和场景摘要。源码与场景冻结后再重复运行；开发试跑不得悄悄删除或计入最终固定样本。不要用脚本驱动、协议失败或 Guard 自述代替模型效果。
