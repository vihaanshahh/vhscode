#!/usr/bin/env node
// `vhscode doctor` — quick health check across the stack.

const childProcess = require("child_process")
const net = require("net")
const path = require("path")
const fs = require("fs")

function ok(m)  { process.stdout.write(`\x1b[32m✓\x1b[0m ${m}\n`) }
function bad(m) { process.stdout.write(`\x1b[31m✗\x1b[0m ${m}\n`) }
function meh(m) { process.stdout.write(`\x1b[33m·\x1b[0m ${m}\n`) }

function which(cmd) {
  const r = childProcess.spawnSync(process.platform === "win32" ? "where" : "which", [cmd], { encoding: "utf8" })
  return r.status === 0 ? r.stdout.trim().split(/\r?\n/)[0] : null
}

function probe(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const s = net.createConnection({ port, host })
    s.setTimeout(300)
    s.on("connect", () => { s.destroy(); resolve(true) })
    s.on("timeout", () => { s.destroy(); resolve(false) })
    s.on("error", () => resolve(false))
  })
}

async function main() {
  const opencode = which("opencode")
  opencode ? ok(`opencode: ${opencode}`) : bad("opencode not on PATH — run `vhscode setup`")

  const ollama = which("ollama")
  ollama ? ok(`ollama: ${ollama}`) : bad("ollama not on PATH — run `vhscode setup`")
  if (ollama) {
    const up = await probe(11434)
    up ? ok("ollama daemon reachable on :11434") : meh("ollama daemon not running — start with `ollama serve`")
  }

  const headroom = which("headroom")
  headroom ? ok(`headroom: ${headroom}`) : meh("headroom not installed — token compression disabled")

  const port = Number(process.env.VHSCODE_HEADROOM_PORT || 8787)
  const proxyUp = await probe(port)
  proxyUp ? ok(`headroom proxy reachable on :${port}`) : meh(`headroom proxy not listening on :${port} (vhscode will boot one on launch)`)

  if (opencode) {
    const auth = childProcess.spawnSync(opencode, ["auth", "list"], { encoding: "utf8" })
    if (auth.status === 0 && /github-copilot/i.test(auth.stdout)) {
      ok("github-copilot auth present")
    } else {
      bad("github-copilot not authenticated — run `opencode auth login github-copilot`")
    }
  }
}

main().catch((err) => { bad(err.message); process.exit(1) })
