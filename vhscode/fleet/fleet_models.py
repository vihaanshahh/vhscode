"""Host-owned model preferences and native model discovery for Fleet workspaces."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE
import signal
import sqlite3
import subprocess
from time import monotonic


AGENTS = ("codex", "cursor")
OPERATIONS = {"workspace_settings", "set_model", "reset_session", "models"}
CATALOG_TIMEOUT = 8
MAX_CATALOG_BYTES = 1024 * 1024
MAX_MODELS = 500
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CURSOR_MODEL = re.compile(r"(?P<id>[A-Za-z0-9][A-Za-z0-9_./:+@~\-]*)(?:\s+-\s+(?P<label>.+))?")
CURSOR_STATUS = re.compile(r"\s+\((?:current|default)(?:, (?:current|default))?\)$")


def _agent(agent):
    if agent not in AGENTS:
        raise ValueError("Choose Codex or Cursor")
    return agent


def _database():
    from fleet_jobs import state_dir

    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "workspace-settings.sqlite3"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    database = sqlite3.connect(path, timeout=5)
    try:
        database.execute(
            """CREATE TABLE IF NOT EXISTS workspace_settings (
                 repo TEXT NOT NULL,
                 agent TEXT NOT NULL,
                 model TEXT,
                 session TEXT,
                 generation INTEGER NOT NULL DEFAULT 0,
                 PRIMARY KEY(repo, agent)
               )"""
        )
    except Exception:
        database.close()
        raise
    return database


def _key(repo, agent):
    return str(Path(repo).expanduser().resolve()), _agent(agent)


def get(repo, agent):
    """Return preferences and the generation a run must present when remembering a session."""
    with closing(_database()) as database:
        row = database.execute(
            "SELECT model, session, generation FROM workspace_settings WHERE repo = ? AND agent = ?",
            _key(repo, agent),
        ).fetchone()
    model, session, generation = row or (None, None, 0)
    return {"model": model, "session": session, "generation": generation}


def remember(repo, agent, session, generation):
    """Remember a completed session only if no reset happened after its submission."""
    if not isinstance(session, str) or not 1 <= len(session) <= 512 or not session.isprintable():
        return False
    key = _key(repo, agent)
    with closing(_database()) as database, database:
        result = database.execute(
            """INSERT INTO workspace_settings(repo, agent, session)
               SELECT ?, ?, ? WHERE ? = 0 OR EXISTS (
                 SELECT 1 FROM workspace_settings WHERE repo = ? AND agent = ?
               )
               ON CONFLICT(repo, agent) DO UPDATE SET session = excluded.session
               WHERE workspace_settings.generation = ?""",
            (*key, session, generation, *key, generation),
        )
        return result.rowcount == 1


def workspace(repo, request):
    op = request.get("op")
    if op == "workspace_settings":
        return {
            agent: {key: value for key, value in get(repo, agent).items() if key != "generation"}
            for agent in AGENTS
        }
    agent = _agent(request.get("agent"))
    if op == "models":
        return {"models": _models(repo, agent)}
    key = _key(repo, agent)
    if op == "set_model":
        model = request.get("model")
        if model is not None:
            if not isinstance(model, str):
                raise ValueError("Enter a valid model ID")
            model = model.strip() or None
            if model is not None and (len(model) > 256 or not model.isprintable()):
                raise ValueError("Enter a valid model ID")
        with closing(_database()) as database, database:
            database.execute(
                """INSERT INTO workspace_settings(repo, agent, model) VALUES (?, ?, ?)
                   ON CONFLICT(repo, agent) DO UPDATE SET model = excluded.model""",
                (*key, model),
            )
        return {}
    if op == "reset_session":
        with closing(_database()) as database, database:
            database.execute(
                """INSERT INTO workspace_settings(repo, agent, generation) VALUES (?, ?, 1)
                   ON CONFLICT(repo, agent) DO UPDATE SET session = NULL, generation = generation + 1""",
                key,
            )
        return {}
    raise ValueError("Unknown Fleet model operation")


@contextmanager
def _agent_process(executable, arguments, repo, *, input_required):
    environment = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
    process = subprocess.Popen(
        [executable, *arguments],
        cwd=repo,
        stdin=subprocess.PIPE if input_required else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        bufsize=0,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _lines(stream, deadline):
    total = 0
    buffer = bytearray()
    with DefaultSelector() as selector:
        selector.register(stream, EVENT_READ)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("The model list did not respond in time.")
            chunk = os.read(stream.fileno(), min(32 * 1024, MAX_CATALOG_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CATALOG_BYTES:
                raise ValueError("The model list exceeded Fleet's output limit.")
            buffer.extend(chunk)
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            yield from lines
        if buffer:
            yield bytes(buffer)


def _codex_models(process, output, deadline):
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)

    def send(message):
        data = json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
        if len(data) > 4096:
            raise ValueError("The Codex model list could not be requested.")
        with DefaultSelector() as selector:
            selector.register(descriptor, EVENT_WRITE)
            while data:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("The model list request did not reach Codex in time.")
                try:
                    data = data[os.write(descriptor, data):]
                except BlockingIOError:
                    continue

    def response(identifier):
        for line in output:
            try:
                reply = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(reply, dict) or reply.get("id") != identifier:
                continue
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ValueError("Codex declined the model list request.")
            return result
        raise ValueError("Codex returned no model catalog.")

    send({
        "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "vhscode_fleet", "title": "Fleet", "version": "1"}},
    })
    response(1)
    send({"method": "initialized"})
    models = []
    cursor = None
    seen_cursors = set()
    for identifier in range(2, 12):
        params = {"limit": 100, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        send({"id": identifier, "method": "model/list", "params": params})
        result = response(identifier)
        data = result.get("data")
        if not isinstance(data, list):
            raise ValueError("Codex returned an unreadable model catalog.")
        for item in data:
            if isinstance(item, dict) and not item.get("hidden"):
                model = item.get("model") or item.get("id")
                models.append((model, item.get("displayName")))
        cursor = result.get("nextCursor")
        if cursor is None or len(models) >= MAX_MODELS:
            break
        if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen_cursors:
            raise ValueError("Codex returned an invalid model list cursor.")
        seen_cursors.add(cursor)
    return models


def _cursor_models(process, output, deadline):
    models = []
    found_header = False
    for raw in output:
        line = ANSI.sub("", raw.decode("utf-8", errors="replace")).strip()
        if line == "Available models":
            found_header = True
            continue
        if line.startswith("Tip:"):
            break
        if not found_header:
            continue
        match = CURSOR_MODEL.fullmatch(CURSOR_STATUS.sub("", line))
        if match and len(models) < MAX_MODELS:
            models.append((match["id"], match["label"]))
    if process.wait(timeout=max(0.01, deadline - monotonic())) != 0:
        raise ValueError("Cursor could not list available models.")
    return models


def _models(repo, agent):
    from fleet_harness import _find_harness

    executable = _find_harness(agent)
    if executable is None:
        raise ValueError(f"The {agent} CLI is unavailable on this host.")
    codex = agent == "codex"
    arguments = ["app-server", "--listen", "stdio://"] if codex else ["--list-models"]
    deadline = monotonic() + CATALOG_TIMEOUT
    try:
        with _agent_process(executable, arguments, repo, input_required=codex) as process:
            output = _lines(process.stdout, deadline)
            try:
                candidates = _codex_models(process, output, deadline) if codex else _cursor_models(process, output, deadline)
            finally:
                output.close()
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"The {agent} model list did not respond in time.") from error
    except OSError as error:
        raise ValueError(f"The {agent} CLI could not read its model catalog.") from error

    models = {}
    for identifier, label in candidates[:MAX_MODELS]:
        if isinstance(identifier, str) and 1 <= len(identifier) <= 256 and identifier.isprintable():
            model_label = label if isinstance(label, str) and label.isprintable() else identifier
            models.setdefault(identifier, {"id": identifier, "label": model_label[:256]})
    return list(models.values())
