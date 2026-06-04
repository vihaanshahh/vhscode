/**
 * VHSCode orchestration plugin.
 *
 * Adds four tools the orchestrator agent uses to plan, dispatch, verify, and report.
 * Also wires the `experimental.provider.small_model` hook so the local tier is the
 * default small model regardless of provider, and tracks token spend per session.
 *
 * State is held in-process. The plugin attaches to the opencode `tool.execute.after`
 * hook to count tokens implicitly (we don't have first-class access to LLM usage
 * counters here, so we approximate via tool output sizes — good enough for a
 * relative cost report).
 */
import type { Plugin, Hooks, PluginInput } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"

type Tier = "local" | "copilot" | "verify"

type PlanNode = {
  id: string
  description: string
  tier: Tier
  depends_on?: string[]
  verifier?: string
}

type SessionState = {
  plan: Map<string, PlanNode>
  done: Set<string>
  results: Map<string, string>
  copilotChars: number
  localChars: number
  startedAt: number
}

const SUBAGENT_BY_TIER: Record<Tier, string> = {
  local: "vhs-local",
  copilot: "vhs-copilot",
  verify: "vhs-verifier",
}

const sessions = new Map<string, SessionState>()

function getState(sessionID: string): SessionState {
  let s = sessions.get(sessionID)
  if (!s) {
    s = {
      plan: new Map(),
      done: new Set(),
      results: new Map(),
      copilotChars: 0,
      localChars: 0,
      startedAt: Date.now(),
    }
    sessions.set(sessionID, s)
  }
  return s
}

function chargeTier(state: SessionState, tier: Tier, chars: number) {
  if (tier === "copilot") state.copilotChars += chars
  else state.localChars += chars
}

function tierFromModelID(modelID: string | undefined): Tier {
  if (!modelID) return "copilot"
  if (modelID.startsWith("vhscode-local/")) return "local"
  return "copilot"
}

function approxTokens(chars: number) {
  // Industry rule of thumb: ~4 chars per token for English / code mix.
  return Math.round(chars / 4)
}

function savingsPct(state: SessionState) {
  const total = state.copilotChars + state.localChars
  if (total === 0) return 0
  // Savings vs hypothetical "everything on Copilot": the locally-handled fraction.
  return Math.round((state.localChars / total) * 100)
}

const VHSCodeOrchestrator: Plugin = async (input: PluginInput): Promise<Hooks> => {
  const { client } = input

  return {
    tool: {
      vhs_plan: tool({
        description:
          "Record a task graph for the current request. Each node has id, description, tier (local|copilot|verify), optional depends_on[], optional verifier id. Replaces any prior plan.",
        args: {
          graph: tool.schema
            .array(
              tool.schema.object({
                id: tool.schema.string(),
                description: tool.schema.string(),
                tier: tool.schema.enum(["local", "copilot", "verify"]),
                depends_on: tool.schema.array(tool.schema.string()).optional(),
                verifier: tool.schema.string().optional(),
              }),
            )
            .describe("Ordered list of task nodes"),
        },
        async execute(args, ctx) {
          const state = getState(ctx.sessionID)
          state.plan.clear()
          state.done.clear()
          state.results.clear()
          for (const node of args.graph as PlanNode[]) {
            state.plan.set(node.id, node)
          }
          ctx.metadata({
            title: `plan: ${args.graph.length} nodes`,
            metadata: {
              nodes: args.graph.map((n) => ({ id: n.id, tier: n.tier, depends: n.depends_on ?? [] })),
            },
          })
          const lines = args.graph.map(
            (n) =>
              `  [${n.tier}] ${n.id}: ${n.description}${
                n.depends_on?.length ? ` (after ${n.depends_on.join(", ")})` : ""
              }${n.verifier ? ` → verify:${n.verifier}` : ""}`,
          )
          return `Plan recorded (${args.graph.length} nodes):\n${lines.join("\n")}`
        },
      }),

      vhs_dispatch: tool({
        description:
          "Run a planned node on its declared tier by spawning the matching subagent (vhs-local, vhs-copilot, or vhs-verifier). Validates that all depends_on nodes are complete first. Returns the subagent's output.",
        args: {
          node_id: tool.schema.string().describe("id of a node previously recorded via vhs_plan"),
          context: tool.schema
            .string()
            .optional()
            .describe("Additional context to pass to the subagent (e.g. prior node outputs to chain through)"),
        },
        async execute(args, ctx) {
          const state = getState(ctx.sessionID)
          const node = state.plan.get(args.node_id)
          if (!node) return `Error: no node "${args.node_id}" in current plan. Call vhs_plan first.`
          for (const dep of node.depends_on ?? []) {
            if (!state.done.has(dep)) {
              return `Error: node "${args.node_id}" depends on "${dep}" which has not completed. Dispatch "${dep}" first.`
            }
          }

          const subagent = SUBAGENT_BY_TIER[node.tier]
          const depResults = (node.depends_on ?? [])
            .map((d) => `### prior: ${d}\n${state.results.get(d) ?? "(no output recorded)"}`)
            .join("\n\n")
          const prompt = [
            `# Task: ${node.id}`,
            node.description,
            args.context ? `\n## Extra context\n${args.context}` : "",
            depResults ? `\n## Upstream outputs\n${depResults}` : "",
          ]
            .filter(Boolean)
            .join("\n")

          // Spawn the subagent via the opencode SDK's session-message endpoint.
          // We mirror what the built-in `task` tool does, scoped to our subagent type.
          const result = await client.session.message
            .submit({
              path: { id: ctx.sessionID },
              body: {
                providerID: undefined,
                modelID: undefined,
                agent: subagent,
                parts: [{ type: "text", text: prompt }],
              } as any,
            })
            .catch((err: unknown) => ({ error: String(err) }))

          const output =
            typeof result === "string"
              ? result
              : (result as any)?.data?.parts
                  ?.filter((p: any) => p?.type === "text")
                  ?.map((p: any) => p.text)
                  ?.join("\n") ??
                JSON.stringify(result).slice(0, 4000)

          state.done.add(node.id)
          state.results.set(node.id, output as string)
          chargeTier(state, node.tier, (output as string).length + prompt.length)

          ctx.metadata({
            title: `${node.tier}: ${node.id}`,
            metadata: { node_id: node.id, tier: node.tier, subagent },
          })

          // If the local tier explicitly escalated, surface it so the orchestrator
          // can re-plan as copilot tier.
          if (node.tier === "local" && /^\s*ESCALATE:/im.test(output as string)) {
            return `LOCAL_ESCALATED: ${output}\n\nThe orchestrator should re-dispatch this work at the copilot tier.`
          }
          return output as string
        },
      }),

      vhs_verify: tool({
        description:
          "Local verification pass. Spawns the vhs-verifier subagent against a target (file paths or diff) and a claim (what the prior agent says they did). Returns APPROVED or DEFECTS.",
        args: {
          target: tool.schema.string().describe("File paths (comma-separated) or a diff snippet to verify"),
          claim: tool.schema.string().describe("What the previous agent claims was done"),
        },
        async execute(args, ctx) {
          const state = getState(ctx.sessionID)
          const prompt = [
            `## Target`,
            args.target,
            ``,
            `## Claim`,
            args.claim,
            ``,
            `Verify per your output discipline.`,
          ].join("\n")

          const result = await client.session.message
            .submit({
              path: { id: ctx.sessionID },
              body: {
                providerID: undefined,
                modelID: undefined,
                agent: "vhs-verifier",
                parts: [{ type: "text", text: prompt }],
              } as any,
            })
            .catch((err: unknown) => ({ error: String(err) }))

          const output =
            typeof result === "string"
              ? result
              : (result as any)?.data?.parts
                  ?.filter((p: any) => p?.type === "text")
                  ?.map((p: any) => p.text)
                  ?.join("\n") ?? "(verifier produced no output)"

          chargeTier(state, "verify", (output as string).length + prompt.length)
          ctx.metadata({ title: "verify", metadata: { approved: /^APPROVED/m.test(output as string) } })
          return output as string
        },
      }),

      vhs_budget: tool({
        description:
          "Report current session token spend by tier and the savings ratio. Call this before your final answer.",
        args: {},
        async execute(_args, ctx) {
          const state = getState(ctx.sessionID)
          const copilotTokens = approxTokens(state.copilotChars)
          const localTokens = approxTokens(state.localChars)
          const total = copilotTokens + localTokens
          const saved = savingsPct(state)
          const elapsedSec = Math.round((Date.now() - state.startedAt) / 1000)

          const report = {
            copilot_tokens: copilotTokens,
            local_tokens: localTokens,
            total_tokens: total,
            saved_pct_vs_all_copilot: saved,
            session_seconds: elapsedSec,
          }
          ctx.metadata({ title: `budget: ${saved}% local`, metadata: report })
          return JSON.stringify(report, null, 2)
        },
      }),
    },

    // Always route the provider-default small model to vhscode-local when present.
    "experimental.provider.small_model": async (incoming, output) => {
      // If the active provider is vhscode-local itself, opencode handles it.
      if (incoming.provider.id === "vhscode-local") return
      output.model = {
        id: "qwen2.5-coder:3b",
        providerID: "vhscode-local",
      } as any
    },

    // Best-effort token accounting: every tool output gets charged to the tier
    // currently in use. We approximate by output size since plugins don't see
    // raw LLM usage counters.
    "tool.execute.after": async (input, output) => {
      // The orchestrator's own tools are already charged in their execute fn.
      if (input.tool.startsWith("vhs_")) return
      const state = getState(input.sessionID)
      // Tool outputs sit in the next LLM call's input, so they're effectively
      // billed at whichever tier the *next* request runs on. We assume copilot
      // for the primary loop unless we see vhscode-local explicitly. This is
      // conservative: it overestimates copilot spend and so the savings number
      // is a lower bound.
      state.copilotChars += (output.output || "").length
    },
  }
}

export default VHSCodeOrchestrator
