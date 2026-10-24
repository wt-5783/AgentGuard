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
      const old = state().default_mode
      const mode = state().mode === "on" ? "off" : "on"
      try {
        await rpc("/mode", { mode })
        const response = await api.client.session.create({ title: `AgentGuard ${mode.toUpperCase()}`, agent: "agentguard" })
        if (response.error || !response.data?.id) throw new Error("NEW_SESSION_FAILED")
        api.route.navigate("session", { sessionID: response.data.id })
        api.ui.dialog.clear()
        await refresh()
        api.ui.toast({ variant: mode === "on" ? "success" : "warning", title: "AgentGuard",
          message: mode === "on" ? "防护已开启 · 已创建干净的新任务" : "防护已关闭 · 已创建独立对照任务", duration: 4000 })
      } catch {
        await rpc("/mode", { mode: old }).catch(() => {})
        api.ui.toast({ variant: "error", title: "AgentGuard", message: "切换未完成；现有任务的防护模式保持不变。" })
      } finally { changing = false }
    }
    function details() {
      api.ui.dialog.replace(() => (
        <api.ui.DialogSelect title="AgentGuard · 防护中心" flat skipFilter
          options={[
            { title: label(), value: "toggle", description: "切换防护并新建任务；已有任务的模式固定", footer: "Ctrl+Shift+G" },
            { title: `模型 · ${state().model ?? "连接中"}`, value: "model", description: "两侧使用同一模型与参数" },
            { title: `状态 · ${PHASE[state().phase] ?? state().phase}`, value: "phase" },
            { title: `执行 ${state().executed ?? 0}  ·  拦截 ${state().blocked ?? 0}  ·  脱敏 ${state().redacted ?? 0}  ·  恢复 ${state().recoveries ?? 0}`,
              value: "counts", description: "真实 Runtime / 后端计数；未触发不记为已验证" },
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
      bindings: [{ key: "ctrl+shift+g", cmd: "agentguard.toggle", desc: "切换 AgentGuard 防护" }],
    })
    api.slots.register({ order: 50, slots: {
      app_bottom() {
        return (
          <box border={["top"]} borderColor={theme().borderSubtle} backgroundColor={theme().backgroundPanel} paddingLeft={2} paddingRight={2}>
            <box flexDirection="row" justifyContent="space-between">
              <text fg={color()} onMouseDown={() => void toggle()}><b> ◈ AgentGuard  {label()} </b></text>
              <text fg={theme().textMuted} onMouseDown={details}>/guard 防护中心 · Ctrl+Shift+G</text>
            </box>
            <text fg={theme().textMuted}>{PHASE[state().phase] ?? state().phase} · 执行 {state().executed ?? 0} / 拦截 {state().blocked ?? 0} / 策略拒绝 {state().policy_rejected ?? 0} / 恢复 {state().recoveries ?? 0}</text>
          </box>
        )
      },
      home_prompt_right() { return <text fg={color()} onMouseDown={() => void toggle()}>{label()}</text> },
      session_prompt_right() { return <text fg={color()} onMouseDown={() => void toggle()}>{label()}</text> },
      sidebar_content() {
        return (
          <box gap={0}>
            <text fg={color()} onMouseDown={details}><b>◈ AgentGuard {state().mode?.toUpperCase()}</b></text>
            <text fg={theme().textMuted}>策略 · 来源传播 · 原子许可</text>
            <text fg={theme().textMuted}>结果隔离 · 脱敏 · 最终门禁</text>
            <text fg={theme().textMuted}>安全恢复 · 完整审计</text>
            <text fg={theme().text}>审查 {Object.values(state().reviews ?? {}).reduce((a: number, b: number) => a + b, 0)}</text>
            <text fg={theme().textMuted}>隔离原文保存在私有审计库</text>
          </box>
        )
      },
    } })
    await refresh()
    const timer = setInterval(refresh, 500)
    api.lifecycle.onDispose(() => clearInterval(timer))
  },
}
