# 安装包初始化与 Agent 适配流程

2.0.0a6。统一工作台的「Agent 接入」使用与 CLI 相同的能力表，只生成命令，不在网页后台执行安装或模型调用。

| 方式 | 模型输入边界 | 注册工具 | 最终显示门禁 | 恢复 | 已验证范围 |
|---|---|---|---|---|---|
| OpenCode 原生 | 有 | 有 | 有 | 清空/ACK | OpenCode 1.19.4，本机 Linux |
| OpenCode MCP | 宿主负责 | 有 | 仅 run-opencode 启动器可强制 | 停止连接 | OpenCode 1.19.4，标准 MCP |
| MCP / VS Code 模板 | 宿主负责 | 有 | 仅显式 guard_finalize | 停止连接 | 协议/模板，其他客户端待验收 |
| RuntimePort / HostAdapter | 有 | 有 | 有 | 清空/ACK | Python、HTTP、框架回归；新宿主另验收 |

`agentguard-v2-connect profiles` 输出机器可读能力表。原生链路复用 [现有 OpenCode 实现](OPENCODE_NATIVE.md)，不另外实现一套执行器。通用 MCP 的保护范围见 [详细指南](AGENT_INTEGRATION.md)。

## 从 wheel 开始

安装 Python >= 3.12 和对应扩展：MCP 使用 `[connect]`，OpenCode 原生使用 `[opencode]`，工作台使用 `[gateway,v2-console]`。工具工厂应作为可信模块安装到相同虚拟环境。

```bash
# 不依赖源码 examples；目录必须全新且在工具根目录之外
agentguard-v2-connect init --directory /private/agentguard --workspace /absolute/workspace
```

生成 `application.json`、`task.txt`、`credentials.env.example`，不创建 Runtime 数据库、不调用模型。修改模型 ID、端点、资源和授权，将环境模板复制为私有 `credentials.env` 并填写字面凭据。初始任务只列出目录，更换任务应修改可信任务文件并建立新连接。

```bash
# 原生静态检查
agentguard-v2-connect check --client opencode-native \
  --config /private/agentguard/application.json --env-file /private/agentguard/credentials.env

# MCP 还需非空的可信任务文件；也支持 --client vscode / opencode
agentguard-v2-connect check --client mcp \
  --config /private/agentguard/application.json --task-file /private/agentguard/task.txt \
  --env-file /private/agentguard/credentials.env

# 配置交接包；client 也可选 opencode-native（不需要 task-file）
agentguard-v2-connect bundle --client mcp \
  --config /private/agentguard/application.json --task-file /private/agentguard/task.txt \
  --env-file /private/agentguard/credentials.env --enable-cloud --output /private/mcp-bundle
```

`check` 检查配置/目录隔离、模型选项、凭据存在性和基础依赖，返回逐项 PASS/FAIL 与原因代码，缺失配置退出非零。它不导入工具工厂、不安装依赖、不创建状态、不测试模型网络。自定义工厂需通过 doctor 或原生启动继续验证实际清单。原生启动校验 OpenCode 1.19.4。

配置包包含 `client.json`、`check.json`、`README.md`、`manifest.json`（包版本、能力与 SHA-256）。只引用配置/任务/环境的绝对路径，不复制密钥、任务正文、数据库或工具工厂。它绑定本机路径和 Python 环境；跨机器需安装依赖并重新生成。已有目录、工具根内目录、非法配置和空任务均拒绝；缺失的凭据/依赖/二进制可在目标环境补齐。

`bundle --enable-cloud` 让生成的 MCP 命令能在执行任务时调用模型，导出时模型调用仍为零。未带此参数时，运行连接器须通过私有环境显式启用云端。

## 连接与验收

原生 profile 的 client.json 提供 launch 参数数组，使用已有原生服务和插件。MCP profile 按目标客户端格式合并配置，先实际验证握手：

```bash
agentguard-v2-connect doctor --config /private/agentguard/application.json \
  --task-file /private/agentguard/task.txt --env-file /private/agentguard/credentials.env --enable-cloud
```

doctor 实际 initialize/list/status 握手，模型调用为零。READY 不等于模型服务可用或防护效果已验收。

新宿主依次验收：正常读写、越界不执行、被拒 FINAL 不显示、连接/进程异常不重放副作用。完整宿主还要核对实际模型输入及恢复清空后的 ACK。MCP 每任务独立连接；宿主关闭额外工具/provider/委派旁路。模板兼容不等于所有客户端已实机测试。

新增工具采用可信 `tool_factory: "your_package.tools:registry"`，返回 `ToolRegistry`，清单应真实声明 effects、resource/destination/artifact 参数、版本、数据标签与幂等能力。先验证确定性范围，再做真实良性和攻击对照；不能将任意 Shell 简单声明为 READ。完整宿主参考 `adapters/host.py`，契约以 `ports.py` 为准，恢复必须执行所要求的清空并提交 ACK，不能伪造完成。
