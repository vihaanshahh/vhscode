"""Host-owned model preferences and native model discovery for Fleet workspaces."""

from contextlib import closing, contextmanager
from datetime import datetime
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
CATALOG_TIMEOUT = 8
MAX_CATALOG_BYTES = 1024 * 1024
MAX_MODELS = 500
MAX_SESSIONS = 100
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


def _valid_session(session):
    return isinstance(session, str) and 1 <= len(session) <= 512 and session.isprintable() and bool(session.strip())


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
        database.execute(
            """CREATE TABLE IF NOT EXISTS workspace_commands (
                 repo TEXT NOT NULL, agent TEXT NOT NULL, command TEXT NOT NULL,
                 PRIMARY KEY(repo, agent)
               )"""
        )
    except Exception:
        database.close()
        raise
    return database


def _key(repo, agent):
    return str(Path(repo).expanduser().resolve()), _agent(agent)


def cli_command(repo, agent):
    with closing(_database()) as database:
        row = database.execute(
            "SELECT command FROM workspace_commands WHERE repo=? AND agent=?", _key(repo, agent)
        ).fetchone()
    return row[0] if row else ""


def set_cli_command(repo, agent, command):
    from fleet_harness import command_parts

    command_parts(command)
    key = _key(repo, agent)
    command = command.strip()
    with closing(_database()) as database, database:
        if command:
            database.execute(
                """INSERT INTO workspace_commands(repo, agent, command) VALUES (?, ?, ?)
                   ON CONFLICT(repo, agent) DO UPDATE SET command=excluded.command""",
                (*key, command),
            )
        else:
            database.execute("DELETE FROM workspace_commands WHERE repo=? AND agent=?", key)
    return command


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
    if not _valid_session(session):
        return False
    table, columns, key = _session_table(repo, agent, lane)
    names = ", ".join(columns)
    with closing(_database()) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if _session_in_other_lane(database, key[:2], lane, session):
            return False
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


def _assigned_sessions(database, key):
    return database.execute(
        "SELECT 1, session FROM workspace_settings WHERE repo=? AND agent=? AND session IS NOT NULL "
        "UNION ALL SELECT lane, session FROM additional_sessions WHERE repo=? AND agent=? AND session IS NOT NULL",
        (*key, *key),
    ).fetchall()


def _session_in_other_lane(database, key, lane, session):
    return any(identifier == session and assigned_lane != lane
               for assigned_lane, identifier in _assigned_sessions(database, key))


def verify_session_admission(repo, agent, lane, session, generation):
    if get(repo, agent, lane)["generation"] != generation:
        raise ValueError("The agent session changed. Refresh the workspace before sending a prompt.")
    if session is not None:
        with closing(_database()) as settings:
            if _session_in_other_lane(settings, _key(repo, agent), lane, session):
                raise ValueError("This session is already assigned to another agent instance.")


def _list_sessions(repo, agent):
    from fleet_harness import _clean
    from fleet_jobs import _connect

    key = _key(repo, agent)
    sessions = {}
    with _connect() as runs:
        records = runs.execute(
            "SELECT session, title, COALESCE(ended, started) AS updated_at FROM runs "
            "WHERE repo=? AND agent=? AND status='completed' AND session IS NOT NULL "
            "ORDER BY updated_at DESC, id DESC",
            key,
        )
        for row in records:
            identifier = row["session"]
            if _valid_session(identifier):
                sessions.setdefault(identifier, (row["title"], row["updated_at"], "fleet"))
            if len(sessions) >= MAX_SESSIONS:
                break
    with closing(_database()) as settings:
        for _, identifier in _assigned_sessions(settings, key):
            if _valid_session(identifier):
                sessions.setdefault(identifier, ("", 0, "fleet"))
    try:
        threads = _codex_threads(repo) if agent == "codex" else _cursor_sessions(repo)
        for thread in threads:
            identifier = thread["id"] if agent == "codex" else thread["sessionId"]
            previous_title, previous_time, _ = sessions.get(identifier, ("", 0, "fleet"))
            title = next((text for name in ("name", "title", "preview")
                          if isinstance((text := thread.get(name)), str) and text.strip()), previous_title)
            updated = thread.get("updatedAt", thread.get("createdAt"))
            sessions[identifier] = (title, max(_session_time(updated), previous_time), agent)
    except (OSError, ValueError):
        if not sessions and agent == "codex":
            raise
    ordered = sorted(sessions.items(), key=lambda row: (-row[1][1], row[0]))[:MAX_SESSIONS]
    return {"sessions": [
        {"id": identifier, "title": " ".join(_clean(title).split())[:160] or identifier,
         "updated_at": updated, "source": source}
        for identifier, (title, updated, source) in ordered
    ]}


def _resume_session(repo, agent, lane, session):
    from fleet_jobs import _connect, _reap_manual

    if not _valid_session(session):
        raise ValueError("Choose a valid native session.")
    table, columns, key = _session_table(repo, agent, lane)
    workspace = key[:2]
    with _connect() as runs, closing(_database()) as settings:
        known = any(identifier == session for _, identifier in _assigned_sessions(settings, workspace))
        if not known:
            known = runs.execute(
                "SELECT 1 FROM runs WHERE repo=? AND agent=? AND session=? AND status='completed' LIMIT 1",
                (*workspace, session),
            ).fetchone() is not None
    if not known:
        try:
            threads = (_codex_threads(repo, session=session) if agent == "codex"
                       else _cursor_sessions(repo, session=session))
        except ValueError:
            if agent == "codex":
                raise
            threads = []
        if not threads:
            raise ValueError("Choose a session from this agent and workspace.")
    _reap_manual()
    names = ", ".join(columns)
    with _connect() as runs, closing(_database()) as settings, settings:
        runs.execute("BEGIN IMMEDIATE")
        if runs.execute(
            "SELECT 1 FROM runs WHERE repo=? AND agent=? AND status='running' "
            "AND (lane=? OR session=? OR (session IS NULL AND ?=0)) LIMIT 1",
            (*workspace, lane, session, known),
        ).fetchone():
            raise ValueError("Wait for the active task before resuming this session.")
        settings.execute("BEGIN IMMEDIATE")
        if _session_in_other_lane(settings, workspace, lane, session):
            raise ValueError("This session is already assigned to another agent instance.")
        settings.execute(
            f"INSERT INTO {table}({names}, session, generation) VALUES ({','.join('?' for _ in key)}, ?, 1) "
            f"ON CONFLICT({names}) DO UPDATE SET session=excluded.session, generation=generation+1",
            (*key, session),
        )
    return {"session": session}


def workspace(repo, request):
    op = request.get("op")
    if op == "workspace_agents":
        return _instances(repo)
    if op == "workspace_settings":
        settings = {
            agent: {key: value for key, value in get(repo, agent).items() if key != "generation"}
            for agent in AGENTS
        }
        if request.get("include_cursor_trust") is True:
            from fleet_cursor_trust import cursor_trust
            settings["cursor"]["trusted"] = cursor_trust(repo)
        return settings
    agent = _agent(request.get("agent"))
    if op == "cli_command":
        return {"command": cli_command(repo, agent)}
    if op == "set_cli_command":
        return {"command": set_cli_command(repo, agent, request.get("command"))}
    if op == "test_cli_command":
        from fleet_harness import test_command

        return {"executable": test_command(repo, agent, request.get("command"))}
    if op == "models":
        return {"models": _models(repo, agent)}
    if op == "list_sessions":
        return _list_sessions(repo, agent)
    if op == "resume_session":
        return _resume_session(repo, agent, agent_lane(request.get("lane", 1)), request.get("session"))
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
                raise TimeoutError("The agent did not respond in time.")
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
                    raise ValueError("The agent response exceeded Fleet's output limit.")
                key.data.extend(chunk)
                lines = key.data.split(b"\n")
                key.data[:] = lines.pop()
                for line in lines:
                    yield diagnostic, line


def _rpc_client(process, output, deadline, *, agent):
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)

    def send(message):
        data = json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
        if len(data) > 4096:
            raise ValueError(f"The {agent} request exceeded Fleet's input limit.")
        with DefaultSelector() as selector:
            selector.register(descriptor, EVENT_WRITE)
            while data:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError(f"The request did not reach {agent} in time.")
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
                raise ValueError(f"{agent} declined the request.")
            return result
        raise ValueError(f"{agent} returned no response.")

    identifier = 0
    envelope = {"jsonrpc": "2.0"} if agent == "Cursor" else {}

    def call(method, params):
        nonlocal identifier
        identifier += 1
        send({**envelope, "id": identifier, "method": method, "params": params})
        return response(identifier)

    return call, send


def _codex_rpc(process, output, deadline):
    call, send = _rpc_client(process, output, deadline, agent="Codex")
    call("initialize", {"clientInfo": {"name": "vhscode_fleet", "title": "Fleet", "version": "1"}})
    send({"method": "initialized"})
    return call


def _codex_models(process, output, deadline):
    call = _codex_rpc(process, output, deadline)
    models = []
    cursor = None
    seen_cursors = set()
    for _ in range(10):
        params = {"limit": 100, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        result = call("model/list", params)
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


def _session_time(value):
    if type(value) in (int, float) and -1e15 < value < 1e15:
        return value
    if isinstance(value, str) and len(value) <= 64:
        try:
            date = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
            if date.tzinfo is not None:
                return date.timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    return 0


def _matches_workspace(root, cwd):
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return False
    try:
        return Path(cwd).resolve() == root
    except (OSError, ValueError, RuntimeError):
        return False


def _session_pages(call, method, params, root, *, identifier_key, data_key, session=None):
    found = {}
    seen = set()
    for _ in range(4):
        result = call(method, params)
        threads = result.get(data_key)
        if not isinstance(threads, list):
            raise ValueError("The agent returned an unreadable session list.")
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            identifier = thread.get(identifier_key)
            if not _valid_session(identifier) or (session is not None and identifier != session):
                continue
            if _matches_workspace(root, thread.get("cwd")):
                found.setdefault(identifier, thread)
                if session is not None or len(found) >= MAX_SESSIONS:
                    return list(found.values())
        cursor = result.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
            raise ValueError("The agent returned an invalid session list cursor.")
        seen.add(cursor)
        params["cursor"] = cursor
    return list(found.values())


def _cursor_sessions(repo, *, session=None):
    from fleet_harness import _require_harness

    root = Path(repo).resolve()
    deadline = monotonic() + CATALOG_TIMEOUT
    try:
        command, environment = _require_harness(repo, "cursor")
        with _agent_process(command[0], [*command[1:], "acp"], repo, environment, input_required=True) as process, \
                closing(_lines(process, deadline)) as output:
            call, _ = _rpc_client(process, output, deadline, agent="Cursor")
            result = call("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "vhscode_fleet", "version": "1"},
            })
            capabilities = result.get("agentCapabilities")
            sessions = capabilities.get("sessionCapabilities") if isinstance(capabilities, dict) else None
            if not isinstance(sessions, dict) or not isinstance(sessions.get("list"), dict):
                return []
            return _session_pages(call, "session/list", {"cwd": str(root)}, root,
                                  identifier_key="sessionId", data_key="sessions", session=session)
    except (OSError, ValueError):
        # An older CLI, an unavailable account, or a failed ACP session can still have Fleet history.
        return []


def _codex_threads(repo, *, session=None):
    from fleet_harness import _require_harness

    command, environment = _require_harness(repo, "codex")
    root = Path(repo).resolve()

    deadline = monotonic() + CATALOG_TIMEOUT
    try:
        with _agent_process(command[0], [*command[1:], "app-server", "--listen", "stdio://"], repo, environment,
                            input_required=True) as process, closing(_lines(process, deadline)) as output:
            call = _codex_rpc(process, output, deadline)
            if session is not None:
                try:
                    thread = call("thread/read", {"threadId": session, "includeTurns": False}).get("thread")
                except ValueError:
                    return []
                if isinstance(thread, dict) and thread.get("id") == session and _matches_workspace(root, thread.get("cwd")):
                    return [thread]
                return []

            params = {
                "cwd": str(root), "limit": 50, "sortKey": "updated_at", "modelProviders": [],
                "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
            }
            return _session_pages(call, "thread/list", params, root, identifier_key="id", data_key="data")
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        raise ValueError("The Codex session list did not respond in time.") from error
    except OSError as error:
        raise ValueError("The Codex CLI could not read its sessions.") from error


def _cursor_models(process, output, deadline):
    models = []
    found_header = False
    authentication_error = network_error = locked_keychain = False
    for diagnostic, raw in output:
        line = ANSI.sub("", raw.decode("utf-8", errors="replace")).strip()
        if diagnostic or line.lower().startswith("error:"):
            authentication_error |= bool(CURSOR_AUTH_ERROR.search(line))
            network_error |= bool(CURSOR_NETWORK_ERROR.search(line))
            locked_keychain |= "login keychain is locked" in line.lower()
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
        if locked_keychain:
            raise ValueError("Cursor could not access the login keychain. Open Fleet or Terminal on the Mac that runs Cursor and unlock its login keychain.")
        if authentication_error:
            raise ValueError("Sign in to Cursor on this host with `cursor-agent login` or `agent login` to list models.")
        if network_error:
            raise ValueError("Cursor could not reach its model catalog from this host.")
        raise ValueError("Cursor could not list available models.")
    return models


def _models(repo, agent):
    from fleet_harness import _require_harness

    command, environment = _require_harness(repo, agent)
    codex = agent == "codex"
    arguments = ["app-server", "--listen", "stdio://"] if codex else ["--list-models"]
    deadline = monotonic() + CATALOG_TIMEOUT
    try:
        with _agent_process(command[0], [*command[1:], *arguments], repo, environment, input_required=codex) as process:
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
