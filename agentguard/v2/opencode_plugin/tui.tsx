/** @jsxImportSource @opentui/solid */
import { createSignal } from "solid-js"
import { readFile } from "node:fs/promises"
import { join } from "node:path"

const PHASE = {
  READY: "等待任务", POLICY: "编译最小权限策略", MODEL: "模型推理（输出隔离中）",
  ACTION_RESULT: "动作审查 / 执行 / 结果隔离", FINAL: "最终输出审查",
  COMPLETE: "任务完成", DENIED: "策略拒绝 · 任务超出宿主授权", RECOVERY: "撤销风险来源并重建上下文", RECOVERED: "安全上下文已重建", ERROR: "故障关闭 · 执行已停止",
}

export default {
  id: "agentguard.native.tui",
  tui: async (api, options) => {
    const [state, setState] = createSignal({ healthy: false, default_mode: "on", mode: "on", phase: "READY", reviews: {} })
    let polling = false
    let changing = false
    const sessionID = () => api.route.current.name === "session" ? api.route.current.params?.sessionID : undefined
    async function rpc(path, body) {
      if (!options.url) {
        const resolved = JSON.parse(await readFile(join(options.discoveryDirectory, `${process.pid}.json`), "utf8"))
        if (resolved.parent !== process.pid) throw new Error("AGENTGUARD_WINDOW_BINDING_MISMATCH")
        options = { ...options, url: resolved.url, token: resolved.token }
      }
      const response = await fetch(options.url + path, {
        method: body === undefined ? "GET" : "POST",
        headers: { Authorization: `Bearer ${options.token}`, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(5000),
      })
      const value = await response.json()
      if (!response.ok) throw new Error(value?.error?.message ?? "AGENTGUARD_UNAVAILABLE")
      return value
    }
    async function refresh() {
      if (polling) return
      polling = true
      try {
        const id = sessionID()
        setState(await rpc("/status" + (id ? `?native_id=${encodeURIComponent(id)}` : "")))
      } catch {
        setState(old => ({ ...old, healthy: false, phase: "ERROR", reason_code: "BOUNDARY_SERVICE_UNAVAILABLE" }))
      } finally { polling = false }
    }
    const theme = () => api.theme.current
    const color = () => !state().healthy || state().phase === "ERROR" ? theme().error : state().mode === "on" ? theme().success : theme().warning
    const label = () => !state().healthy ? "● ERROR" : state().mode === "on" ? "● ON  防护开启" : "○ OFF 防护关闭"
    async function toggle() {
      if (changing) return
      if (!state().healthy) {
        api.ui.toast({ variant: "error", title: "AgentGuard", message: "边界服务不可用，保持故障关闭。" })
        return
      }
      const id = sessionID()
      const nativeStatus = id ? api.state.session.status(id) : undefined
      if (state().busy || nativeStatus && nativeStatus.type !== "idle") {
        api.ui.toast({ variant: "warning", title: "AgentGuard", message: "当前任务运行中，请完成或停止后再切换。" })
        return
      }
      changing = true
      const mode = state().mode === "on" ? "off" : "on"
      try {
        setState(await rpc("/mode", { mode, native_id: id }))
        api.ui.dialog.clear()
        api.ui.toast({ variant: mode === "on" ? "success" : "warning", title: "AgentGuard",
          message: mode === "on" ? "防护已开启 · 从下一条输入生效" : "防护已关闭 · 从下一条输入生效", duration: 4000 })
      } catch {
        api.ui.toast({ variant: "error", title: "AgentGuard", message: "切换失败，请重试。" })
      } finally { changing = false }
    }
    function details() {
      api.ui.dialog.replace(() => (
        <api.ui.DialogSelect title="AgentGuard · 防护中心" flat placeholder="搜索防护状态或选择开关"
          options={[
            { title: label(), value: "toggle", description: "保留当前会话，从下一条输入生效", footer: "F6" },
            { title: `模型 · ${state().model ?? "连接中"}`, value: "model", description: "两侧使用同一模型与参数" },
            { title: `项目 · ${state().workspace ?? "连接中"}`, value: "workspace" },
            { title: `状态 · ${PHASE[state().phase] ?? state().phase}`, value: "phase" },
            { title: `执行 ${state().executed ?? 0}  ·  拦截 ${state().blocked ?? 0}  ·  策略拒绝 ${state().policy_rejected ?? 0}`, value: "counts" },
            { title: `脱敏 ${state().redacted ?? 0}  ·  恢复 ${state().recoveries ?? 0}`, value: "recovery" },
            { title: `审查 · ACTION ${state().reviews?.ACTION ?? 0} / RESULT ${state().reviews?.RESULT ?? 0} / FINAL ${state().reviews?.FINAL ?? 0}`,
              value: "reviews" },
            { title: `原因 · ${state().reason_code ?? "—"}`, value: "reason" },
            { title: "保护链路 · 策略 → 上下文 → 原子许可 → 结果隔离 → FINAL → 恢复", value: "scope" },
            { title: "关闭面板", value: "close" },
          ]}
          onSelect={item => item.value === "toggle" ? void toggle() : item.value === "close" ? api.ui.dialog.clear() :
            api.ui.toast({ variant: "info", title: "AgentGuard", message: item.description ?? item.title, duration: 3000 })} />
      ))
      api.ui.dialog.setSize("large")
    }
    api.keymap.registerLayer({
      commands: [
        { name: "agentguard.toggle", title: "AgentGuard: 切换防护", category: "AgentGuard", namespace: "palette", run: toggle },
        { name: "agentguard.panel", title: "AgentGuard: 防护中心", category: "AgentGuard", namespace: "palette", slashName: "guard", run: details },
      ],
    })
    api.keymap.registerLayer({ mode: "base",
      bindings: [{ key: "f6", cmd: "agentguard.toggle", desc: "切换 AgentGuard 防护" }],
    })
    api.slots.register({ order: 50, slots: {
      app_bottom() {
        return (
          <box border={["top"]} borderColor={theme().borderSubtle} backgroundColor={theme().backgroundPanel} paddingLeft={1} paddingRight={1} flexDirection="row" justifyContent="space-between">
            <text fg={color()} onMouseUp={() => void toggle()}><b>AgentGuard [{!state().healthy ? "ERROR" : state().mode === "on" ? "ON 开启" : "OFF 关闭"}]</b></text>
            <text fg={theme().textMuted} onMouseUp={details}>F6 · /guard</text>
          </box>
        )
      },
    } })
    await refresh()
    const timer = setInterval(refresh, 500)
    api.lifecycle.onDispose(() => clearInterval(timer))
  },
}
