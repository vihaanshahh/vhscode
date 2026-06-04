#!/usr/bin/env node
// Friendly nudge after `npm i -g vhscode` — never fails the install.

const note = (m) => process.stderr.write(`[vhscode] ${m}\n`)

try {
  note("installed. Next steps:")
  note("  1) run `vhscode setup` (once) to install opencode + ollama + headroom + auth Copilot")
  note("  2) launch with `vhscode`")
  note("Docs: https://github.com/vihaanshahh/VHSCode")
} catch {
  // Never block install.
}
