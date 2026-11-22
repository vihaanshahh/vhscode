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
OPERATIONS = {"workspace_settings", "workspace_agents", "set_model", "reset_session", "models"}
CATALOG_TIMEOUT = 8
MAX_CATALOG_BYTES = 1024 * 1024
MAX_MODELS = 500
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CURSOR_MODEL = re.compile(r"(?P<id>[A-Za-z0-9][A-Za-z0-9_./:+@~\-]*)(?:\s+-\s+(?P<label>.+))?")
CURSOR_STATUS = re.compile(r"\s+\((?:current|default)(?:, (?:current|default))?\)$")
CURSOR_AUTH_ERROR = re.compile(r"authentication required|not authenticated|please log in|unauthorized", re.I)
CURSOR_NETWORK_ERROR = re.compile(
    r"ENETUNREACH|ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|"
    r"failed to (?:connect|fetch)|network (?:error|unavailable)",
    re.I,
)


def _agent(agent):
    if agent not in AGENTS:
        raise ValueError("Choose Codex or Cursor")
    return agent


def agent_lane(value):
    if type(value) is not int or not 1 <= value <= 4:
        raise ValueError("Choose an agent instance between 1 and 4.")
    return value


def _session_table(repo, agent, lane):
    key = _key(repo, agent)
    if agent_lane(lane) == 1:
        return "workspace_settings", ("repo", "agent"), key
    return "additional_sessions", ("repo", "agent", "lane"), (*key, lane)


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
        database.execute(
            """CREATE TABLE IF NOT EXISTS additional_sessions (
                 repo TEXT NOT NULL, agent TEXT NOT NULL, lane INTEGER NOT NULL,
                 session TEXT, generation INTEGER NOT NULL DEFAULT 0,
                 PRIMARY KEY(repo, agent, lane)
               )"""
        )
    except Exception:
        database.close()
        raise
    return database


def _key(repo, agent):
    return str(Path(repo).expanduser().resolve()), _agent(agent)


def get(repo, agent, lane=1):
    """Return preferences and the generation a run must present when remembering a session."""
    with closing(_database()) as database:
        row = database.execute(
            "SELECT model, session, generation FROM workspace_settings WHERE repo = ? AND agent = ?",
            _key(repo, agent),
        ).fetchone()
        model, session, generation = row or (None, None, 0)
        if agent_lane(lane) != 1:
            session, generation = database.execute(
                "SELECT session, generation FROM additional_sessions WHERE repo=? AND agent=? AND lane=?",
                (*_key(repo, agent), lane),
            ).fetchone() or (None, 0)
    return {"model": model, "session": session, "generation": generation}


def remember(repo, agent, session, generation, lane=1):
    """Remember a completed session only if no reset happened after its submission."""
    if not isinstance(session, str) or not 1 <= len(session) <= 512 or not session.isprintable():
        return False
    table, columns, key = _session_table(repo, agent, lane)
    names = ", ".join(columns)
    with closing(_database()) as database, database:
        database.execute(f"INSERT OR IGNORE INTO {table}({names}) VALUES ({','.join('?' for _ in key)})", key)
        result = database.execute(
            f"UPDATE {table} SET session=? WHERE " + " AND ".join(f"{name}=?" for name in columns) + " AND generation=?",
            (session, *key, generation),
        )
        return result.rowcount == 1


def _latest_instances(roots):
    from fleet_jobs import RUNS_VIEW, _connect, _reap_manual

    if not roots:
        return {}
    _reap_manual()
    with _connect() as db:
        records = db.execute(
            RUNS_VIEW + f" WHERE detached=1 AND repo IN ({','.join('?' for _ in roots)}) ORDER BY started DESC, id DESC",
            tuple(roots),
        )
        latest = {}
        for row in records:
            latest.setdefault((row["repo"], row["agent"], row["lane"]), dict(row))
    return latest


def sidebar_agents(repos):
    from fleet_hosts import workspace_root

    if not isinstance(repos, list) or len(repos) > 256 or not all(isinstance(path, str) for path in repos):
        raise ValueError("Choose up to 256 workspace folders.")
    mapped = {}
    workspaces = {repo: [] for repo in repos}
    for repo in workspaces:
        try:
            mapped[repo] = str(workspace_root(repo))
        except (ValueError, OSError):
            continue
    roots = set(mapped.values())
    if not roots:
        return {"workspaces": workspaces}
    latest = _latest_instances(roots)
    placeholders = ','.join('?' for _ in roots)
    with closing(_database()) as settings:
        generations = {
            (root, agent, lane): gen for root, agent, lane, gen in settings.execute(
                f"SELECT repo, agent, 1, generation FROM workspace_settings WHERE repo IN ({placeholders}) "
                f"UNION ALL SELECT repo, agent, lane, generation FROM additional_sessions WHERE repo IN ({placeholders})",
                (*roots, *roots),
            )
        }
    from fleet_jobs import _connect, _title
    with _connect() as db:
        schedules = db.execute(
            f"""SELECT s.repo,s.id AS schedule,s.agent,s.prompt,s.enabled,
                    EXISTS(SELECT 1 FROM runs WHERE schedule_id=s.id AND status='running') AS running
                FROM schedules s WHERE repo IN ({','.join('?' for _ in roots)}) ORDER BY s.rowid""",
            tuple(roots),
        ).fetchall()
    for repo, root in mapped.items():
        rows = []
        for key, run in sorted(latest.items()):
            if key[0] != root:
                continue
            row = {field: run[field] for field in ("agent", "lane", "status", "title")}
            if run["generation"] != generations.get(key, 0) and run["status"] != "running":
                row.update(status="ready", title="")
            rows.append(row)
        rows.extend({
            "agent": row["agent"], "lane": 0, "schedule": row["schedule"], "title": _title(row["prompt"]),
            "status": "running" if row["running"] else ("scheduled" if row["enabled"] else "paused"),
        } for row in schedules if row["repo"] == root)
        workspaces[repo] = rows
    return {"workspaces": workspaces}


def _instances(repo):
    root = str(repo)
    latest = _latest_instances([root])
    keys = {(agent, 1) for agent in AGENTS}
    keys.update((agent, lane) for _, agent, lane in latest)
    with closing(_database()) as db:
        keys.update(db.execute("SELECT agent, lane FROM additional_sessions WHERE repo=?", (root,)))
    instances = []
    for agent, lane in sorted(keys):
        settings = get(repo, agent, lane)
        run = latest.get((root, agent, lane))
        if run and run["generation"] != settings["generation"]:
            run = None
        instances.append({"agent": agent, "lane": lane, "session": settings["session"], "run": run})
    return {"instances": instances}


def workspace(repo, request):
    op = request.get("op")
    if op == "workspace_agents":
        return _instances(repo)
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
        table, columns, key = _session_table(repo, agent, request.get("lane", 1))
        names = ", ".join(columns)
        with closing(_database()) as database, database:
            database.execute(
                f"INSERT INTO {table}({names}, generation) VALUES ({','.join('?' for _ in key)}, 1) "
                f"ON CONFLICT({names}) DO UPDATE SET session=NULL, generation=generation+1",
                key,
            )
        return {}
    raise ValueError("Unknown Fleet model operation")


@contextmanager
def _agent_process(executable, arguments, repo, environment, *, input_required):
    process = subprocess.Popen(
        [executable, *arguments],
        cwd=repo,
        stdin=subprocess.PIPE if input_required else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if input_required else subprocess.PIPE,
        env=dict(environment, NO_COLOR="1", FORCE_COLOR="0"),
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
        if process.stderr is not None:
            process.stderr.close()


def _lines(process, deadline):
    total = 0
    with DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                selector.register(stream, EVENT_READ, bytearray())
        while selector.get_map():
            remaining = deadline - monotonic()
            ready = selector.select(remaining) if remaining > 0 else []
            if not ready:
                raise TimeoutError("The model list did not respond in time.")
            for key, _ in ready:
                diagnostic = key.fileobj is process.stderr
                chunk = os.read(key.fd, min(32 * 1024, MAX_CATALOG_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    if key.data:
                        yield diagnostic, key.data
                    continue
                total += len(chunk)
                if total > MAX_CATALOG_BYTES:
                    raise ValueError("The model list exceeded Fleet's output limit.")
                key.data.extend(chunk)
                lines = key.data.split(b"\n")
                key.data[:] = lines.pop()
                for line in lines:
                    yield diagnostic, line


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
        for _, line in output:
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
    authentication_error = network_error = False
    for diagnostic, raw in output:
        line = ANSI.sub("", raw.decode("utf-8", errors="replace")).strip()
        if diagnostic or line.lower().startswith("error:"):
            authentication_error |= bool(CURSOR_AUTH_ERROR.search(line))
            network_error |= bool(CURSOR_NETWORK_ERROR.search(line))
            continue
        if line == "Available models":
            found_header = True
            continue
        if line.startswith("Tip:"):
            found_header = False
        if not found_header:
            continue
        match = CURSOR_MODEL.fullmatch(CURSOR_STATUS.sub("", line))
        if match and len(models) < MAX_MODELS:
            models.append((match["id"], match["label"]))
    if process.wait(timeout=max(0.01, deadline - monotonic())) != 0:
        if authentication_error:
            raise ValueError("Sign in to Cursor on this host with `cursor-agent login` or `agent login` to list models.")
        if network_error:
            raise ValueError("Cursor could not reach its model catalog from this host.")
        raise ValueError("Cursor could not list available models.")
    return models


def _models(repo, agent):
    from fleet_harness import _find_harness

    harness = _find_harness(agent)
    if harness is None:
        raise ValueError(f"The {agent} CLI is unavailable on this host.")
    executable, environment = harness
    codex = agent == "codex"
    arguments = ["app-server", "--listen", "stdio://"] if codex else ["--list-models"]
    deadline = monotonic() + CATALOG_TIMEOUT
    try:
        with _agent_process(executable, arguments, repo, environment, input_required=codex) as process:
            output = _lines(process, deadline)
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
