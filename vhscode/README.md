# vhscode

> Terminal AI coding agent with a real orchestration layer. Forked from [opencode](https://github.com/anomalyco/opencode). Splits work between your **GitHub Copilot subscription** and a **local LLM**, with [headroom](https://github.com/chopratejas/headroom) compressing every token in between.

vhscode is what you get when you take opencode and bolt on:

1. **A `plan → dispatch → verify → settle` orchestrator** that fans subtasks out to the cheapest tier that can do them. Designed to beat the single-loop pattern Claude Code uses on cost and on parallelism.
2. **A two-tier model pool** — GitHub Copilot for heavy reasoning, a 3B local model (Qwen2.5-Coder via Ollama) for everything trivial.
3. **headroom as a sidecar compressor** in front of Copilot, so even the tokens that *do* leave your machine are 60–95% smaller.

Run it like opencode. The wrapper does the rest.

```sh
npm i -g vhscode
vhscode setup   # installs opencode, ollama + qwen2.5-coder:3b, headroom, OAuths Copilot
vhscode         # launch
```

---

## The orchestration layer

This is the part that's actually new.

vhscode ships a primary agent called **vhs-orchestrator** (running on Copilot/Sonnet) plus three subagents:

| Subagent | Model | Job |
|---|---|---|
| `vhs-local` | local qwen2.5-coder:3b | File reads, greps, simple transforms, boilerplate |
| `vhs-copilot` | Copilot/Sonnet | Multi-file refactors, design questions, what escalated from local |
| `vhs-verifier` | local qwen2.5-coder:3b | Reads target files, checks claim against reality, returns APPROVED or DEFECTS |

The orchestrator uses four custom tools (added via an opencode plugin):

- **`vhs_plan`** — record a task graph. Each node declares `tier: local | copilot | verify` and `depends_on: []`.
- **`vhs_dispatch`** — fan out a node to its tier. Validates dependencies first. Dispatches in parallel when nodes are independent.
- **`vhs_verify`** — local verification pass over a diff or claim. Free, fast, mandatory after every code-changing copilot-tier task.
- **`vhs_budget`** — current session token spend by tier + percent saved vs. all-Copilot. Always shown at the end of a turn.

The operating loop (this is in the orchestrator's system prompt verbatim):

```
1. PLAN     → emit a task graph via vhs_plan
2. DISPATCH → fan out all currently-runnable nodes in the same turn
3. VERIFY   → vhs_verify after every code-changing node
4. SETTLE   → vhs_budget; report savings to the user
```

If a `vhs-local` worker returns `ESCALATE: <reason>`, the orchestrator re-plans that node as `tier: copilot`. Local is the default; copilot is the escalation path.

### Why this beats a single-loop agent

A single-loop agent (Claude Code, plain opencode) sends every tool output back into one frontier-model context. That means:

- Reading a 5000-line log costs you 5000 lines × Sonnet rate.
- Greps, listings, and trivial summaries all go through the same expensive model.
- Subtasks that are obviously parallel get serialized in a single conversation.

vhscode's orchestrator:

- Sends 5000-line logs to a local 3B model first, gets back a 200-line summary, and only that summary hits Copilot.
- Always pays local-tier (free) for jobs the local tier can handle.
- Dispatches independent subtasks in the same turn so they run concurrently.
- Adds a free verification pass after every copilot edit — catches dumb mistakes before they reach you.

The result: typical sessions land at **70–90% local-tier tokens**, with the remaining Copilot tokens further compressed by headroom.

## Routing matrix

| Job | Subagent | Tier |
|---|---|---|
| Plan, dispatch, final synthesis | vhs-orchestrator | Copilot/Sonnet |
| File reads, greps, listings | vhs-local | Local |
| Single-file summaries | vhs-local | Local |
| Simple transforms / boilerplate | vhs-local | Local |
| Multi-file refactors | vhs-copilot | Copilot/Sonnet |
| Design / architecture questions | vhs-copilot | Copilot/Sonnet |
| Post-edit verification | vhs-verifier | Local |
| Title, summary, compaction | opencode built-in | Local |

All routes are overridable — drop a `~/.config/opencode/opencode.json` or set `VHSCODE_CONFIG=/path/to/custom.json` and override.

## Headroom: compress everything Copilot sees

[headroom](https://github.com/chopratejas/headroom) is a local content-compression proxy. vhscode launches it as a sidecar on `:8787` and points the Copilot provider's `baseURL` at it. Every Copilot request flows through headroom first, which:

- Routes prose/JSON/code through specialized compressors (Kompress / SmartCrusher / CodeCompressor).
- Keeps originals locally and reversibly so the model can retrieve them on demand.
- Stabilizes prompt prefixes for higher KV-cache hit rates upstream.

Typical compression: **60–95% fewer tokens** on the Copilot leg. Off via `VHSCODE_HEADROOM=off`.

## CLI

| Command | What it does |
|---|---|
| `vhscode` | Launch the orchestrator (flags pass through to opencode) |
| `vhscode setup` | One-shot installer for the whole stack |
| `vhscode doctor` | Health check for every dependency + proxy |
| `vhscode headroom <args>` | Forward to the headroom CLI |
| `vhscode --vhscode-version` | Print vhscode version |

Anything else (e.g. `vhscode auth`, `vhscode run "..."`) goes straight to opencode.

## Env knobs

| Env | Default | Notes |
|---|---|---|
| `VHSCODE_HEADROOM` | _on_ | `off` to skip the compression sidecar |
| `VHSCODE_HEADROOM_PORT` | `8787` | Headroom proxy port |
| `VHSCODE_LOCAL_BASE_URL` | `http://127.0.0.1:11434/v1` | Ollama-compatible endpoint |
| `VHSCODE_COPILOT_BASE_URL` | _auto_ | Defaults to headroom proxy when up |
| `VHSCODE_LOCAL_MODEL` | `qwen2.5-coder:3b` | Model pulled by `vhscode setup` |
| `VHSCODE_CONFIG` | _bundled preset_ | Override the entire opencode preset file |
| `VHSCODE_HOME` | `~/.vhscode` | Rendered configs, logs, opencode config dir |
| `VHSCODE_QUIET` | _off_ | `1` to silence the startup banner |

## What's inside the fork

```
vhscode/
├── bin/vhscode                      # Node wrapper (boots headroom, materializes config dir, spawns opencode)
├── config/vhscode.json              # Lightweight standalone preset (when used with OPENCODE_CONFIG)
├── opencode-dir/                    # Materialized into ~/.vhscode/opencode-dir/ at launch
│   ├── opencode.jsonc               #   - opencode config (model routing, providers, plugin)
│   ├── agent/
│   │   ├── vhs-orchestrator.md      #   - the planner
│   │   ├── vhs-local.md             #   - cheap subagent
│   │   ├── vhs-copilot.md           #   - heavy subagent
│   │   └── vhs-verifier.md          #   - local verification pass
│   └── plugin/
│       └── orchestrator.ts          #   - vhs_plan / vhs_dispatch / vhs_verify / vhs_budget tools
└── scripts/
    ├── setup.sh                     # One-shot installer
    ├── doctor.js                    # Health check
    └── postinstall.js               # Post-install nudge
```

## Relation to opencode

This is a **GitHub fork** of [anomalyco/opencode](https://github.com/anomalyco/opencode), which is itself an opencode-family project. The fork preserves the upstream so you can keep pulling improvements. vhscode adds:

- The `vhscode/` directory documented above.
- No changes to opencode's internals — everything is layered via supported extension points (config, agents, plugins, OPENCODE_CONFIG_DIR).

This means upstream opencode releases merge cleanly, and you can drop vhscode at any time by deleting the `vhscode/` directory and running plain opencode.

## License

MIT. opencode is MIT, headroom is Apache 2.0. See [LICENSE](LICENSE).
