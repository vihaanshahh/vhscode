#!/usr/bin/env bash
# VHSCode setup: brings up the dependencies you need to run `vhscode`.
#   - opencode (the underlying agent runtime)
#   - ollama  + a small local model
#   - headroom (proxy compressor)
#   - Copilot OAuth via `opencode auth`

set -euo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
note() { printf "  • %s\n" "$1"; }
warn() { printf "\033[33m! %s\033[0m\n" "$1"; }
die()  { printf "\033[31mx %s\033[0m\n" "$1" >&2; exit 1; }

bold "VHSCode setup"

# ── 1. opencode ────────────────────────────────────────────────────────────────
if command -v opencode >/dev/null 2>&1; then
  note "opencode already installed ($(opencode --version 2>/dev/null || echo present))"
else
  bold "Installing opencode"
  if command -v npm >/dev/null 2>&1; then
    npm install -g opencode-ai
  elif command -v bun >/dev/null 2>&1; then
    bun install -g opencode-ai
  else
    die "neither npm nor bun found — install one to continue"
  fi
fi

# ── 2. ollama ──────────────────────────────────────────────────────────────────
if command -v ollama >/dev/null 2>&1; then
  note "ollama already installed"
else
  bold "Installing ollama"
  if [[ "$(uname -s)" == "Darwin" || "$(uname -s)" == "Linux" ]]; then
    curl -fsSL https://ollama.com/install.sh | sh
  else
    warn "automatic ollama install only supports macOS/Linux. Install manually from https://ollama.com"
  fi
fi

LOCAL_MODEL="${VHSCODE_LOCAL_MODEL:-qwen2.5-coder:3b}"
if command -v ollama >/dev/null 2>&1; then
  bold "Pulling local model: ${LOCAL_MODEL}"
  ollama pull "${LOCAL_MODEL}" || warn "could not pull ${LOCAL_MODEL} — pull manually later"
fi

# ── 3. headroom ────────────────────────────────────────────────────────────────
if command -v headroom >/dev/null 2>&1; then
  note "headroom already installed"
else
  bold "Installing headroom (token compressor)"
  if command -v pipx >/dev/null 2>&1; then
    pipx install headroom-ai || warn "pipx install failed — try: pip install --user headroom-ai"
  elif command -v pip >/dev/null 2>&1; then
    pip install --user headroom-ai || warn "pip install failed — see https://github.com/chopratejas/headroom"
  else
    warn "neither pipx nor pip found — install headroom manually from https://github.com/chopratejas/headroom"
  fi
fi

# ── 4. Copilot OAuth ───────────────────────────────────────────────────────────
bold "Authenticating GitHub Copilot"
if opencode auth list 2>/dev/null | grep -qi "github-copilot"; then
  note "Copilot already authenticated"
else
  note "Launching device-code flow — finish in your browser when prompted"
  opencode auth login github-copilot || warn "copilot auth failed — re-run: opencode auth login github-copilot"
fi

bold "All set. Run: vhscode"
