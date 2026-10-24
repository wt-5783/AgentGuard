// Native OpenCode server plugin. No model credentials or raw tool bodies here.
import { randomUUID } from "node:crypto"
import { spawn } from "node:child_process"
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises"
import { join } from "node:path"

export default {
  id: "agentguard.native.server",
  server: async ({ client }, options) => {
    let owned
    if (!options.url) {
      const base = options.discoveryDirectory
      await mkdir(base, { recursive: true, mode: 0o700 })
      const run = await mkdtemp(join(base, "window-"))
      const token = randomUUID() + randomUUID()
      await writeFile(join(run, "token"), token, { mode: 0o600 })
      const args = ["-I", "-m", "agentguard.v2.opencode", "serve", "--config", options.config,
        "--run-directory", run, "--mode", "on", "--parent-pid", String(process.pid)]
      if (options.envFile) args.push("--env-file", options.envFile)
      owned = spawn(options.python, args, { stdio: "ignore" })
      process.once("exit", () => owned?.kill("SIGTERM"))
      let ready
      for (let i = 0; i < 300; i++) {
        if (owned.exitCode !== null) throw new Error("AGENTGUARD_BOUNDARY_START_FAILED")
        try { ready = JSON.parse(await readFile(join(run, "ready.json"), "utf8")); break } catch {}
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      if (!ready) { owned.kill(); throw new Error("AGENTGUARD_BOUNDARY_START_TIMEOUT") }
      options = { ...options, url: ready.url, token }
      await writeFile(join(base, `${process.pid}.json`), JSON.stringify({ url: ready.url, token, parent: process.pid }), { mode: 0o600 })
    }
    const { tool } = await import(options.sdk)
    async function rpc(path, body) {
      const response = await fetch(options.url + path, {
        method: body === undefined ? "GET" : "POST",
        headers: { Authorization: `Bearer ${options.token}`, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(600000),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data?.error?.message ?? "AGENTGUARD_UNAVAILABLE")
      return data
    }
    const catalog = await rpc("/catalog")
    const definitions = {}
    for (const entry of [...catalog.tools, { name: "ag_continue", description: "Continue after a verified security context rebuild." }]) {
      definitions[entry.name] = tool({
        description: entry.description,
        args: { receipt: tool.schema.string() },
        async execute(args, context) {
          const value = await rpc("/receipt", { native_id: context.sessionID, tool: entry.name, receipt: args.receipt })
          return {
            title: `${value.status} · ${value.reason_code}`,
            output: JSON.stringify(value),
            metadata: { agentguard: true, status: value.status, reason_code: value.reason_code },
          }
        },
      })
    }
    return {
      config: async (config) => {
        // Empty maps in JSON deep-merge do not remove inherited MCP servers.
        // Clear them after OpenCode has resolved all project configuration.
        config.mcp = {}
        config.permission = { "*": "deny", "ag_*": "allow" }
        config.tools = { "*": false, "ag_*": true }
        config.enabled_providers = ["agentguard"]
        config.default_agent = "agentguard"
        if (catalog.model) config.model = config.small_model = "agentguard/" + catalog.model
        if (config.provider?.agentguard) config.provider.agentguard.options = {
          ...config.provider.agentguard.options, baseURL: options.url + "/v1", apiKey: options.token,
        }
      },
      dispose: async () => { owned?.kill("SIGTERM") },
      tool: definitions,
      "chat.message": async (input, output) => {
        const visible = output.parts.filter(p => p.type === "text" && !p.synthetic)
        if (output.parts.some(p => p.type !== "text")) throw new Error("AGENTGUARD_TEXT_TASK_ONLY")
        const task = visible.map(p => p.text).join("\n").trim()
        if (!task) throw new Error("AGENTGUARD_EMPTY_TASK")
        await rpc("/task", { native_id: input.sessionID, task })
      },
      "chat.headers": async (input, output) => {
        if (input.model.providerID !== "agentguard") throw new Error("AGENTGUARD_UNTRACKED_PROVIDER")
        output.headers["x-agentguard-session"] = input.sessionID
        output.headers["x-agentguard-request"] = randomUUID()
        output.headers["x-agentguard-role"] = input.agent
      },
      "tool.execute.before": async (input) => {
        if (!Object.hasOwn(definitions, input.tool)) throw new Error("AGENTGUARD_UNREGISTERED_TOOL")
      },
      // The provider bridge owns all actual model context; native compaction is
      // an auxiliary UI operation and must not launch an untracked model call.
      "experimental.compaction.autocontinue": async (_input, output) => { output.enabled = false },
    }
  },
}
