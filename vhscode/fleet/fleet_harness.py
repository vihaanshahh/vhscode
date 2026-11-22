"""Run a turn in an installed coding agent and relay its structured output."""

from __future__ import annotations

import atexit
from collections import deque
from concurrent.futures import Future
from functools import cache
import hashlib
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import shutil
import signal
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Callable


MAX_EVENT_BYTES = 256 * 1024
MAX_PENDING_PROGRESS = 16
MAX_TOOL_PREVIEW = 4 * 1024
MAX_OUTCOME = 1200
IMPORTANT = re.compile(
    r"\b(?:warn\w*|error\w*|fail\w*|approv\w*|permission\w*|denied|declined|"
    r"reject\w*|fatal|block\w*|refus\w*|unsafe|requires? confirmation)\b",
    re.IGNORECASE,
)
DIAGNOSTIC = re.compile(r"(?im)^\s*(?:\[?(?:warning|error|fatal)\b|(?:approval|permission)\s+(?:required|denied)\b)")
APPROVAL = re.compile(
    r"(?i)\b(?:(?:requires?|needs?|awaiting|pending) (?:your |user |manual )?(?:approval|permission|confirmation)|"
    r"(?:approval|permission|confirmation) (?:request(?:ed)?|required|denied|declined))\b"
)
BREAKAGE = re.compile(
    r"(?i)\b(?:breaking changes?|backwards? incompatib\w*|requires? (?:a )?(?:data |database |schema )?migration|"
    r"destructive change|data loss|irreversible change|will break|no longer compatible)\b"
)
NEGATION = re.compile(r"(?i)\b(?:no|not|never|without|avoid\w*|prevent\w*)\b[^.,;:]{0,30}$")
FAILED_TESTS = re.compile(
    r"(?i)^\s*(?:=+\s*[1-9]\d* failed\b|tests?(?: suites?)?:\s*[1-9]\d* failed\b|test result:\s*FAILED\b)"
)
TERMINAL_CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f]")
CREDENTIAL = re.compile(
    r"(?i)(\b(?:authorization|(?:x[-_])?api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|"
    r"client[-_ ]?secret|password|cookie)\b[\"']?\s*(?:[:=]|\s)\s*[\"']?(?:(?:bearer|basic)\s+)?)"
    r"[^\s,;\"'<>]+|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w]{12,})\b"
)


class HarnessError(RuntimeError):
    """A silent run could not complete successfully."""


def run(
    root: Path, agent: str, prompt: str, tags: set[str], session: str | None, quiet: bool,
    *, on_event: Callable[[str, str], None] | None = None, detailed: bool = False, silent: bool = False,
    view: str = "actions", model: str | None = None,
) -> str | None:
    """Run one turn and report public events, ending with one status event."""
    if view not in ("actions", "summary", "risks"):
        raise ValueError(f"Unknown output view: {view}")
    stream = _Stream(agent, session, quiet, on_event, detailed, silent, view)
    status = "failed"
    failure = ""
    try:
        executable = _find_harness(agent)
        if executable is None:
            command = "codex" if agent == "codex" else "cursor-agent"
            failure = f"Fleet could not find {command} on PATH."
        else:
            code = _run_process(root, agent, executable, prompt, tags, session, stream, model)
            if code:
                failure = f"{agent} exited with status {code}."
            elif stream.failed:
                failure = f"{agent} reported a failed run."
            else:
                status = "completed"
    except KeyboardInterrupt:
        status = "cancelled"
        failure = f"{agent} cancelled."
    except SystemExit:
        status = "cancelled"
        raise
    except (OSError, ValueError) as error:
        failure = f"Fleet could not run {agent}: {error}"
    finally:
        if failure:
            stream.emit("error", failure)
        stream.finish(status)
    if silent and status != "completed":
        raise HarnessError(_clean(failure))
    return stream.session


def _run_process(root: Path, agent: str, executable: str, prompt: str, tags: set[str], session: str | None,
                 stream: _Stream, model: str | None) -> int:

    if tags:
        paths = "\n".join(f"- {json.dumps(path, ensure_ascii=True)}" for path in sorted(tags))
        prompt = f"{prompt}\n\nRepository-relative context paths:\n{paths}"

    if agent == "codex":
        args = ["exec", "resume"] if session else ["exec"]
        args.append("--json")
        if model:
            args.append(f"--model={model}")
        args.extend(["--", session, "-"] if session else ["-"])
    else:
        args = ["--print", "--output-format", "stream-json"]
        if model:
            args.append(f"--model={model}")
        if session:
            args.append(f"--resume={session}")
        # Use the explicit command so root subcommands cannot be mistaken for prompts.
        args.extend(["agent", "--", prompt])
    with tempfile.TemporaryFile() as stdin:
        if agent == "codex":
            stdin.write(prompt.encode("utf-8"))
            stdin.seek(0)
        process = subprocess.Popen(
            [executable, *args], cwd=root, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stream.consume(process.stdout, process.stderr)
            return process.wait()
        except BaseException:
            _stop(process)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()


def native(root: Path, agent: str, session: str | None) -> None:
    """Give the native TUI the terminal to handle interactive approvals."""
    executable = _find_harness(agent)
    if executable is None:
        command = "codex" if agent == "codex" else "cursor-agent"
        _print(f"Fleet could not find {command} on PATH.", error=True)
        return

    args = []
    if session:
        args = ["resume", "--", session] if agent == "codex" else [f"--resume={session}"]
    try:
        process = subprocess.Popen([executable, *args], cwd=root)
    except OSError as error:
        _print(f"Fleet could not start {agent}: {error}", error=True)
        return
    try:
        code = process.wait()
        if code:
            _print(f"{agent} exited with status {code}.", error=True)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait()


def _find_harness(agent: str) -> str | None:
    if agent == "codex":
        return shutil.which("codex")
    if agent == "cursor":
        return shutil.which("cursor-agent") or _cursor_agent_alias()
    raise ValueError(f"Unknown harness: {agent}")


def _cursor_agent_alias() -> str | None:
    executable = shutil.which("agent")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--help"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0 and b"Cursor Agent" in result.stdout and b"stream-json" in result.stdout:
            return executable
    return None


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    # A tool child can still hold the process group after the harness exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@cache
def _progress_filter():
    from fleet_filter import ProgressFilter

    progress_filter = ProgressFilter(enabled=True)
    atexit.register(progress_filter.close)
    return progress_filter


def _print(text: str, *, error: bool = False) -> None:
    print(text, file=sys.stderr if error else sys.stdout, end="" if text.endswith("\n") else "\n", flush=True)


def _clean(text: str) -> str:
    text = TERMINAL_CONTROL.sub("", text)
    text = CREDENTIAL.sub(lambda match: (match[1] or "") + "[redacted]", text)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _flagged(text: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(text):
        previous = text[max(0, match.start() - 45):match.start()]
        if not NEGATION.search(previous) and not re.match(r"\s*:\s*(?:none|n/a)\b", text[match.end():], re.I):
            return True
    return False


class _Stream:
    def __init__(self, agent, session, quiet, on_event, detailed, silent, view):
        self.agent = agent
        self.session = session
        self.on_event = on_event
        self.detailed = detailed
        self.silent = silent
        self.view = view
        self.failed = False
        self.assistant = ""
        self.outcome = ""
        self.assistant_digest = hashlib.sha256()
        self.credentials = tuple(sorted({
            value for name, value in os.environ.items()
            if len(value) >= 8 and re.search(r"(?i)(?:TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL)", name)
        }, key=len, reverse=True))
        self.pending: deque[tuple[str, str, Future[bool], float]] = deque()
        self.filter = None
        if quiet and detailed and (not silent or on_event is not None):
            try:
                self.filter = _progress_filter()
            except Exception:
                # The optional filter is a presentation aid.
                pass

    def consume(self, stdout, stderr) -> None:
        with DefaultSelector() as selector:
            for pipe in (stdout, stderr):
                selector.register(pipe, EVENT_READ)
            lines = {pipe: bytearray() for pipe in (stdout, stderr)}
            oversized = set()
            while selector.get_map():
                self.flush_progress()
                for key, _ in selector.select(0.05 if self.pending else None):
                    pipe = key.fileobj
                    chunk = os.read(key.fd, 64 * 1024)
                    buffer = lines[pipe]
                    if not chunk:
                        selector.unregister(pipe)
                        if buffer:
                            self.on_line(buffer.decode("utf-8", errors="replace"), error=pipe is stderr)
                        continue
                    while chunk:
                        part, newline, chunk = chunk.partition(b"\n")
                        if pipe not in oversized:
                            if len(buffer) + len(part) > MAX_EVENT_BYTES:
                                if pipe is stderr:
                                    self.emit("error", "An oversized agent diagnostic was omitted.")
                                else:
                                    self.emit("info", "An oversized agent event was omitted.")
                                buffer.clear()
                                oversized.add(pipe)
                            else:
                                buffer.extend(part)
                                if newline:
                                    self.on_line(buffer.decode("utf-8", errors="replace"), error=pipe is stderr)
                                    buffer.clear()
                        if newline:
                            oversized.discard(pipe)

    def flush_progress(self, *, force: bool = False) -> None:
        while self.pending:
            kind, text, future, deadline = self.pending[0]
            if not force and not future.done() and monotonic() < deadline:
                return
            self.pending.popleft()
            if future.done():
                try:
                    if future.result():
                        continue
                except Exception:
                    pass
            if self.silent:
                self.on_event(kind, text)
            else:
                _print(text)

    def defer_progress(self, kind: str, text: str, progress: bool) -> bool:
        if not (kind == "info" and progress and self.filter and len(text) <= 512 and not IMPORTANT.search(text)):
            return False
        if len(self.pending) >= MAX_PENDING_PROGRESS:
            return False
        try:
            self.pending.append((kind, text, self.filter.submit(text), monotonic() + 1.0))
        except Exception:
            return False
        return True

    def emit(self, kind: str, text: str, *, progress: bool = False, visible: bool = True) -> None:
        for credential in self.credentials:
            text = text.replace(credential, "[redacted]")
        text = _preview(_clean(text).strip(), MAX_TOOL_PREVIEW)
        if not text:
            return
        if self.silent:
            if self.on_event is not None:
                self.flush_progress()
                if self.defer_progress(kind, text, progress):
                    return
                self.flush_progress(force=True)
                self.on_event(kind, text)
            return
        if self.on_event is not None:
            self.on_event(kind, text)
        if not visible or kind == "status":
            return
        if kind in ("plan", "info") and not self.detailed:
            return
        if kind == "action" and self.view != "actions" and not self.detailed:
            return
        if kind == "risk" and self.view != "risks" and not self.detailed:
            return
        self.flush_progress()
        if self.defer_progress(kind, text, progress):
            return
        self.flush_progress(force=True)
        if kind == "action":
            text = _preview(" ".join(text.split()), 240)
        elif kind == "result" and not self.detailed:
            text = _preview(text, MAX_OUTCOME)
        _print(text, error=kind == "error")

    def finish(self, status: str) -> None:
        if status == "completed":
            self.emit("result", self.outcome or self.assistant or "Run completed.")
        else:
            self.commentary()
            self.emit("result", f"Run {status}.")
        self.flush_progress(force=True)
        self.emit("status", status)

    def commentary(self) -> None:
        if self.assistant:
            self.emit("info", self.assistant, progress=True)
            self.assistant = ""

    def assistant_message(self, text: str, phase=None) -> None:
        self.commentary()
        if _flagged(text, APPROVAL):
            self.emit("risk", f"Agent reported an approval blocker: {text}", visible=False)
            self.emit("error", text)
        elif DIAGNOSTIC.search(text):
            self.emit("error", text)
        elif phase == "commentary":
            self.emit("info", text, progress=True)
        else:
            self.assistant = text
        for line in text.splitlines():
            if _flagged(line, BREAKAGE):
                self.emit("risk", "Agent flagged: " + line.strip())
                break

    def tool_output(self, text: str) -> None:
        lines = text.splitlines()
        diagnostics = "\n".join(line for line in lines if DIAGNOSTIC.search(line))
        if diagnostics:
            self.emit("error", diagnostics)
        for line in lines:
            if FAILED_TESTS.search(line):
                self.emit("risk", "Test output: " + line.strip())
                break
        self.emit("info", text)

    def on_line(self, line: str, *, error: bool = False) -> None:
        if not line.strip():
            return
        if error:
            if _flagged(line, APPROVAL):
                self.emit("risk", "Agent reported an approval blocker: " + line, visible=False)
            self.emit("error", line)
            return
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            self.emit("error" if DIAGNOSTIC.search(line) else "info", line)
            return
        if not isinstance(event, dict):
            return
        if self.agent == "codex":
            self.codex(event)
        else:
            self.cursor(event)

    def diagnostic(self, detail, fallback: str) -> None:
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        text = detail if isinstance(detail, str) and detail else fallback
        if _flagged(text, APPROVAL) or _flagged(fallback.replace("_", " ").replace(".", " "), APPROVAL):
            self.emit("risk", "Agent reported an approval blocker: " + text, visible=False)
        self.emit("error", text)

    def codex(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.session = thread_id
            return
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            counts = [(name, usage.get(field)) for name, field in (
                ("input", "input_tokens"), ("cached input", "cached_input_tokens"), ("output", "output_tokens"))]
            report = [f"{number:,} {name}" for name, number in counts if type(number) is int]
            if report:
                self.emit("info", "CLI token usage for this turn: " + ", ".join(report))
        if kind == "turn.failed":
            self.failed = True
        if isinstance(kind, str) and IMPORTANT.search(kind.replace("_", " ")):
            self.diagnostic(event.get("error", event), kind)
            return

        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_kind, status = item.get("type"), item.get("status")
        if item_kind in ("reasoning", "thinking"):
            return
        if item_kind == "agent_message":
            if kind == "item.completed" and isinstance(item.get("text"), str):
                self.assistant_message(item["text"], item.get("phase"))
            return
        if item_kind == "todo_list":
            self.commentary()
            items = item.get("items")
            if isinstance(items, list):
                notes = [
                    ("✓ " if row.get("completed") else "• ") + row["text"]
                    for row in items if isinstance(row, dict) and isinstance(row.get("text"), str)
                ]
                if notes:
                    self.emit("plan", "Plan\n" + "\n".join(notes))
            return

        self.commentary()
        failure = status in ("failed", "declined", "denied", "rejected") or bool(item.get("error"))
        if item_kind == "command_execution":
            command = item.get("command")
            if kind == "item.started" and isinstance(command, str):
                self.emit("action", f"Run {command}")
            if kind == "item.completed":
                output = item.get("aggregated_output")
                exit_code = item.get("exit_code")
                suffix = f" (exit {exit_code})" if isinstance(exit_code, int) else ""
                failed = failure or (isinstance(exit_code, int) and exit_code != 0)
                text = f"Command {'failed' if failed else status or 'completed'}{suffix}."
                if failed:
                    context = f" {command}" if isinstance(command, str) and command else ""
                    self.emit("risk", text + context, visible=False)
                self.emit("error" if failed else "action", text)
                if isinstance(output, str) and output:
                    if failed:
                        self.emit("error", _preview(output.strip(), MAX_OUTCOME))
                        self.emit("info", output, visible=False)
                    else:
                        self.tool_output(output)
            return
        if item_kind == "file_change":
            changes = item.get("changes")
            labels = []
            if isinstance(changes, list):
                labels = [
                    f"{change.get('kind', 'change')} {change['path']}"
                    for change in changes if isinstance(change, dict) and isinstance(change.get("path"), str)
                ]
            self.emit("error" if failure else "action", ", ".join(labels) or f"File change {status or 'completed'}.")
            return
        if item_kind == "error" or failure or (isinstance(item_kind, str) and IMPORTANT.search(item_kind.replace("_", " "))):
            self.diagnostic(item.get("error") or item, str(item_kind or "Tool failed").replace("_", " "))
            return
        if item_kind in ("mcp_tool_call", "collab_tool_call", "web_search") and kind in ("item.started", "item.completed"):
            label = item_kind.replace("_", " ")
            name = item.get("tool") or item.get("query")
            if isinstance(name, str):
                label += f" {name}"
            self.emit("action", f"{'Run' if kind == 'item.started' else 'Completed'} {label}")

    def cursor(self, event: dict) -> None:
        kind, subtype = event.get("type"), event.get("subtype")
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session = session_id
        if kind in ("user", "thinking", "reasoning") or (kind == "system" and subtype == "init"):
            return
        if kind == "assistant":
            message = event.get("message")
            parts = message.get("content") if isinstance(message, dict) else None
            if isinstance(parts, list):
                text = "".join(
                    part["text"] for part in parts
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                )
                if text:
                    self.assistant_digest.update(text.encode("utf-8", errors="surrogatepass"))
                    self.assistant_message(text, message.get("phase"))
            return
        if kind == "result":
            result = event.get("result")
            if subtype != "success" or event.get("is_error"):
                self.failed = True
                self.diagnostic(result or event.get("error"), "Cursor reported a failed result.")
            elif isinstance(result, str) and result:
                digest = hashlib.sha256(result.encode("utf-8", errors="surrogatepass")).digest()
                if digest != self.assistant_digest.digest():
                    self.outcome = result
            return
        if kind == "tool_call":
            self.commentary()
            tool_call = event.get("tool_call")
            if isinstance(tool_call, dict) and len(tool_call) == 1:
                name, tool = next(iter(tool_call.items()))
                args = tool.get("args") if isinstance(tool, dict) else None
                target = (args.get("path") or args.get("command")) if isinstance(args, dict) else None
                label = re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower().removesuffix(" tool call")
                if isinstance(target, str) and target:
                    label += f" {target}"
                if subtype == "started":
                    self.emit("action", f"Run {label}")
                    todos = args.get("todos") if name == "updateTodosToolCall" and isinstance(args, dict) else None
                    if isinstance(todos, list):
                        notes = [
                            ("✓ " if row.get("status") == "completed" else "• ") + row["content"]
                            for row in todos if isinstance(row, dict) and isinstance(row.get("content"), str)
                        ]
                        if notes:
                            self.emit("plan", "Plan\n" + "\n".join(notes))
                elif subtype == "completed":
                    result = tool.get("result") if isinstance(tool, dict) else None
                    if isinstance(result, dict) and "success" in result:
                        success = result["success"]
                        exit_code = success.get("exitCode", success.get("exit_code")) if isinstance(success, dict) else None
                        failed = isinstance(exit_code, int) and exit_code != 0
                        if failed:
                            text = f"{label} failed (exit {exit_code})."
                            self.emit("risk", text, visible=False)
                            self.emit("error", text)
                        else:
                            self.emit("action", f"Completed {label}")
                        if isinstance(success, dict):
                            for key in ("stdout", "stderr", "output"):
                                if isinstance(success.get(key), str):
                                    self.tool_output(success[key])
                    else:
                        self.emit("risk", f"Tool did not complete successfully: {label}", visible=False)
                        self.diagnostic(result, f"Tool did not complete successfully: {label}")
            return
        label = " ".join(value.replace("_", " ") for value in (kind, subtype) if isinstance(value, str))
        if IMPORTANT.search(label):
            self.diagnostic(event.get("error") or event, label)
        elif isinstance(event.get("message"), str):
            self.emit("info", event["message"])
