"""Run a desktop Codex turn over app-server and relay approvals and follow-ups."""

import json
import os
from selectors import DefaultSelector, EVENT_WRITE
import subprocess
from time import monotonic

from fleet_harness import _stop, prompt_with_tags
from fleet_jobs import approval_decisions, claim_steer, expire_approval, request_approval, settle_steer


def run_approval_turn(root, argv, environment, prompt, session, stream, model, run_id):
    thread_id = turn_id = status = None
    steering = None
    next_steer_poll = 0
    waiting = {}
    process = subprocess.Popen(
        [*argv, "app-server", "--listen", "stdio://"], cwd=root, env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )

    def send(message):
        data = memoryview(json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n")
        deadline = monotonic() + 5
        with DefaultSelector() as selector:
            selector.register(process.stdin, EVENT_WRITE)
            while data:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("Codex did not read Fleet’s request.")
                try:
                    data = data[os.write(process.stdin.fileno(), data):]
                except BlockingIOError:
                    continue

    def reply(identifier, response_key, choice):
        result = {response_key: choice}
        if response_key == "action":
            result.update(content={} if choice == "accept" else None, _meta=None)
        send({"id": identifier, "result": result})

    def poll():
        nonlocal steering, next_steer_poll
        if status is not None:
            return True
        if waiting:
            for identifier, decision in approval_decisions(run_id).items():
                if decision in ("accept", "decline") and identifier in waiting:
                    request_id, response_key = waiting.pop(identifier)
                    reply(request_id, response_key, decision)
        if turn_id and steering is None and monotonic() >= next_steer_poll:
            next_steer_poll = monotonic() + 0.25
            if (row := claim_steer(run_id)) is not None:
                steering = row["id"]
                try:
                    send({"id": f"steer-{steering}", "method": "turn/steer", "params": {
                        "threadId": thread_id, "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": prompt_with_tags(row["prompt"], json.loads(row["tags"]))}],
                    }})
                except BrokenPipeError:
                    settle_steer(run_id, steering, False)
                    steering = None
        return False

    def on_line(line, *, error=False):
        nonlocal thread_id, turn_id, status, steering
        if error:
            stream.on_line(line, error=True)
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict):
            return
        identifier = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if isinstance(method, str) and type(identifier) in (int, str):
            approval = _approval(method, params)
            if approval is None or thread_id is None or params.get("threadId") != thread_id or len(approval[1]) > 4000:
                send({"id": identifier, "error": {"code": -32601, "message": "This interaction is unavailable in Fleet. Use the native Codex terminal."}})
                stream.emit("info", "Codex requested an interaction that can be handled in its native terminal.")
                return
            title, detail, response_key = approval
            if any(identifier == request_id for request_id, _ in waiting.values()):
                return
            card = request_approval(run_id, stream.redact(title)[:120], stream.redact(detail))
            waiting[card] = (identifier, response_key)
            return
        if steering is not None and identifier == f"steer-{steering}":
            result = message.get("result")
            settle_steer(run_id, steering, isinstance(result, dict) and result.get("turnId") == turn_id)
            steering = None
            return
        if type(identifier) is int and identifier in (1, 2, 3):
            result = message.get("result")
            if not isinstance(result, dict):
                stream.diagnostic(message.get("error"), "Codex could not start the approval-enabled turn.")
                status = "failed"
                return
            if identifier == 1:
                send({"method": "initialized"})
                options = {"cwd": str(root), "approvalPolicy": "on-request", "approvalsReviewer": "user"}
                if model:
                    options["model"] = model
                if session:
                    options.update(threadId=session, excludeTurns=True)
                send({"id": 2, "method": "thread/resume" if session else "thread/start", "params": options})
            elif identifier == 2:
                thread_id = result.get("thread", {}).get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise ValueError("Codex returned no thread ID.")
                stream.session = thread_id
                options = {
                    "threadId": thread_id, "input": [{"type": "text", "text": prompt, "text_elements": []}],
                }
                sandbox = result.get("sandbox")
                if session and isinstance(sandbox, dict) and sandbox.get("type") == "dangerFullAccess":
                    # Codex can restore the previous turn's sandbox independently of approvalPolicy.
                    options["sandboxPolicy"] = {
                        "type": "workspaceWrite", "writableRoots": [str(root)], "networkAccess": False,
                        "excludeTmpdirEnvVar": False, "excludeSlashTmp": False,
                    }
                send({"id": 3, "method": "turn/start", "params": options})
            elif turn_id is None:
                turn_id = result.get("turn", {}).get("id")
            return
        if thread_id is None or params.get("threadId") != thread_id:
            return
        if method == "serverRequest/resolved":
            for card, (request_id, _) in tuple(waiting.items()):
                if request_id == params.get("requestId"):
                    waiting.pop(card)
                    expire_approval(run_id, card)
            return
        if method in ("turn/started", "turn/completed"):
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            if turn_id is None:
                turn_id = turn.get("id")
            if method == "turn/completed" and turn.get("id") == turn_id:
                status = turn.get("status") or "failed"
                if status != "completed":
                    stream.diagnostic(turn.get("error"), f"Codex turn {status}.")
            return
        if method in ("item/started", "item/completed") and params.get("turnId") == turn_id:
            _item(stream, method, params.get("item"))
        if method == "turn/plan/updated" and params.get("turnId") == turn_id:
            plan = params.get("plan")
            if isinstance(plan, list):
                stream.codex({"item": {"type": "todo_list", "items": [
                    {"text": step["step"], "completed": step.get("status") == "completed"}
                    for step in plan if isinstance(step, dict) and isinstance(step.get("step"), str)
                ]}})

    try:
        os.set_blocking(process.stdin.fileno(), False)
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "vhscode_fleet", "title": "Fleet", "version": "1"},
            "capabilities": {
                "experimentalApi": True, "requiredHumanApproval": True, "requestAttestation": False,
                "mcpServerOpenaiFormElicitation": True,
            },
        }})
        stream.consume(process.stdout, process.stderr, on_line=on_line, poll=poll)
        if status is None:
            raise ValueError("The Codex app-server closed before the turn finished. Check the CLI or turn off Approval Cards.")
        return 0 if status == "completed" else 1
    finally:
        try:
            _stop(process)
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


def _approval(method, params):
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        reason = params.get("reason")
        cwd = params.get("cwd")
        details = [value for value in (command, reason, cwd) if isinstance(value, str) and value]
        network = params.get("networkApprovalContext")
        if isinstance(network, dict) and isinstance(network.get("host"), str) and network["host"]:
            details.append(network["host"])
            return "Allow network access", "\n\n".join(details), "decision"
        if isinstance(command, str) and command:
            title = "Send terminal input" if params.get("kind") == "writeStdin" else "Run command"
            return title, "\n\n".join(details), "decision"
    if method == "mcpServer/elicitation/request" and params.get("mode") in ("form", "openai/form", "openaiForm"):
        schema = params.get("requestedSchema")
        message = params.get("message")
        server = params.get("serverName")
        if (isinstance(schema, dict) and not schema.get("properties") and not schema.get("required")
                and isinstance(server, str) and isinstance(message, str) and message):
            return f"Approve {server} request", message, "action"
    return None


def _item(stream, method, item):
    if not isinstance(item, dict):
        return
    names = {
        "agentMessage": "agent_message", "commandExecution": "command_execution",
        "fileChange": "file_change", "mcpToolCall": "mcp_tool_call", "webSearch": "web_search",
        "dynamicToolCall": "mcp_tool_call", "collabAgentToolCall": "collab_tool_call",
    }
    kind = names.get(item.get("type"))
    if kind is None:
        return
    normalized = {**item, "type": kind}
    if kind == "command_execution":
        normalized.update(aggregated_output=item.get("aggregatedOutput"), exit_code=item.get("exitCode"))
    stream.codex({"type": method.replace("/", "."), "item": normalized})
