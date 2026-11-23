"""Host-local activity history and interval scheduler for the Fleet workbench."""

from contextlib import contextmanager
import fcntl
from functools import lru_cache
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4


MAX_FINISHED = 100
MAX_EVENTS = 80
MAX_TEXT = 4096
MAX_RESULT_TEXT = 64 * 1024
MAX_SCHEDULES = 100
SLOTS = 2
POLL_SECONDS = 1
RUN_TIMEOUT = 30 * 60
STOP_GRACE = 8
TERMINAL = {"completed", "failed", "cancelled"}
VIEWS = {"actions", "summary", "risks"}
MANUAL_LOCKS = {}
RUNTIME = str(Path(__file__).resolve())
RUNS_VIEW = """
    WITH display AS (
        SELECT id, agent, repo, status, started, ended, view, action, risk, title, session, detached, lane, generation,
               CASE WHEN status IN ('failed', 'cancelled')
                    THEN COALESCE(diagnostic, outcome) ELSE outcome END AS outcome
        FROM runs
    )
    SELECT id, agent, repo, status, started, ended, view, outcome, session, detached, lane, generation,
           CASE WHEN ended IS NULL THEN action ELSE COALESCE(outcome, action) END AS action,
           COALESCE(risk, '') AS risk, title
    FROM display
"""


def state_dir():
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "vhscode" / "fleet"


@lru_cache
def _database(directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "activity.sqlite3"
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    db = sqlite3.connect(path, timeout=5)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                repo TEXT NOT NULL,
                prompt TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                next_run REAL,
                view TEXT NOT NULL DEFAULT 'actions'
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                repo TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT,
                started REAL NOT NULL,
                ended REAL,
                schedule_id TEXT,
                slot INTEGER,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                view TEXT NOT NULL DEFAULT 'actions',
                risk TEXT,
                outcome TEXT,
                diagnostic TEXT,
                title TEXT NOT NULL DEFAULT '',
                session TEXT,
                detached INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS run_events ON events(run_id, seq);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                decision TEXT
            );
            CREATE INDEX IF NOT EXISTS run_approvals ON approvals(run_id);
            CREATE TABLE IF NOT EXISTS scheduler (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                runtime TEXT NOT NULL
            );
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            for table, additions in (
                ("schedules", {"view": "TEXT NOT NULL DEFAULT 'actions'"}),
                ("runs", {
                    "view": "TEXT NOT NULL DEFAULT 'actions'", "risk": "TEXT",
                    "outcome": "TEXT", "diagnostic": "TEXT", "title": "TEXT NOT NULL DEFAULT ''",
                    "session": "TEXT", "detached": "INTEGER NOT NULL DEFAULT 0",
                    "lane": "INTEGER NOT NULL DEFAULT 1", "generation": "INTEGER NOT NULL DEFAULT 0",
                }),
            ):
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, definition in additions.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                if table == "runs" and "title" not in existing:
                    rows = db.execute("""
                        SELECT id, COALESCE(NULLIF(prompt, ''), (
                            SELECT text FROM events WHERE run_id = runs.id AND kind = 'prompt' ORDER BY seq LIMIT 1
                        ), '') FROM runs
                    """).fetchall()
                    db.executemany("UPDATE runs SET title = ? WHERE id = ?", [(_title(prompt), rid) for rid, prompt in rows])
    finally:
        db.close()
    return path


@contextmanager
def _connect():
    db = sqlite3.connect(_database(state_dir()), timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db
    finally:
        db.close()


def _try_lock(name):
    directory = state_dir()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _record(db, run_id, kind, text):
    limit = MAX_RESULT_TEXT if kind == "result" else MAX_TEXT
    text = text[:limit].encode("utf-8", errors="replace").decode("utf-8")
    db.execute(
        "INSERT INTO events(run_id, kind, text, at) VALUES (?, ?, ?, ?)",
        (run_id, kind[:40], text, time.time()),
    )
    if kind in {"action", "plan"}:
        db.execute("UPDATE runs SET action = ? WHERE id = ? AND status = 'running'", (text[:320], run_id))
    if kind == "risk":
        db.execute("UPDATE runs SET risk = ? WHERE id = ?", (text[:320], run_id))
    if kind == "result":
        db.execute("UPDATE runs SET outcome = ? WHERE id = ?", (text[:320], run_id))
    if kind == "error":
        db.execute("UPDATE runs SET diagnostic = ? WHERE id = ?", (text[:320], run_id))
    db.execute(
        """DELETE FROM events WHERE run_id = ? AND seq <= (
            SELECT seq FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?
        )""",
        (run_id, run_id, MAX_EVENTS),
    )


def _title(prompt):
    lines = prompt.strip().splitlines()
    first_line = lines[0] if lines else ""
    return first_line[:100].encode("utf-8", errors="replace").decode("utf-8")


def _finish(db, run_id, status, error=None, session=None):
    result = db.execute(
        "UPDATE runs SET status = ?, ended = ?, session = COALESCE(?, session) WHERE id = ? AND status = 'running'",
        (status, time.time(), session, run_id),
    )
    if not result.rowcount:
        return
    if error is not None:
        _record(db, run_id, "error", error)
    db.execute(
        """DELETE FROM runs WHERE id IN (
            SELECT id FROM runs WHERE ended IS NOT NULL ORDER BY ended DESC, id DESC LIMIT -1 OFFSET ?
        )""",
        (MAX_FINISHED,),
    )


def begin_run(repo, agent, prompt, view="actions", *, session=None, detached=False, known_session=False, lane=1, generation=0):
    if not isinstance(view, str) or view not in VIEWS:
        raise ValueError("Choose an actions, summary, or risks view")
    run_id = uuid4().hex
    fd = _try_lock(f"manual-{run_id}.lock")
    if fd is None:
        raise RuntimeError("Could not reserve the interactive run")
    try:
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if detached and db.execute(
                "SELECT 1 FROM runs WHERE repo=? AND agent=? AND lane=? AND detached=1 AND status='running' LIMIT 1",
                (str(repo), agent, lane),
            ).fetchone():
                raise ValueError("A task is already running for this workspace and agent")
            if detached:
                from fleet_models import verify_session_admission

                verify_session_admission(repo, agent, lane, session, generation)
            if session is not None:
                previous = db.execute(
                    "SELECT status FROM runs WHERE repo = ? AND agent = ? AND session = ?",
                    (str(repo), agent, session),
                ).fetchall()
                if any(row["status"] == "running" for row in previous):
                    raise ValueError("A follow-up is already running in this session")
                if not known_session and not any(row["status"] == "completed" for row in previous):
                    raise ValueError("Choose a completed session from this agent and repository")
            db.execute(
                """INSERT INTO runs(id, agent, repo, prompt, status, started, view, title, session, detached, lane, generation)
                   VALUES (?, ?, ?, '', 'running', ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, agent, str(repo), time.time(), view, _title(prompt), session, detached, lane, generation),
            )
            _record(db, run_id, "prompt", prompt)
    except BaseException:
        _unlock_manual(run_id, fd)
        raise
    MANUAL_LOCKS[run_id] = fd
    return run_id


def event(run_id, kind, text):
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM runs WHERE id = ? AND status = 'running'", (run_id,)).fetchone():
            _record(db, run_id, kind, text)


def request_approval(run_id, title, detail):
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute("SELECT COUNT(*) FROM approvals WHERE run_id=?", (run_id,)).fetchone()[0]
        if count >= MAX_EVENTS:
            raise ValueError("This run reached Fleet's approval limit.")
        identifier = uuid4().hex
        db.execute("INSERT INTO approvals(id, run_id, title, detail) VALUES (?, ?, ?, ?)",
                   (identifier, run_id, title, detail))
    return identifier


def approval_decisions(run_id):
    with _connect() as db:
        return {row["id"]: row["decision"] for row in db.execute(
            "SELECT id, decision FROM approvals WHERE run_id=? AND decision IS NOT NULL", (run_id,)
        )}


def expire_approval(run_id, identifier):
    with _connect() as db:
        db.execute("UPDATE approvals SET decision='expired' WHERE run_id=? AND id=? AND decision IS NULL",
                   (run_id, identifier))


def end_run(run_id, status, *, error=None, session=None):
    if status not in TERMINAL:
        raise ValueError("Invalid run status")
    with _connect() as db:
        _finish(db, run_id, status, error, session)
    fd = MANUAL_LOCKS.pop(run_id, None)
    if fd is not None:
        _unlock_manual(run_id, fd)


def _unlock_manual(run_id, fd):
    (state_dir() / f"manual-{run_id}.lock").unlink(missing_ok=True)
    os.close(fd)


def _reap_manual():
    with _connect() as db:
        manual_runs = db.execute(
            "SELECT id, detached, cancel_requested FROM runs WHERE slot IS NULL AND status = 'running'"
        ).fetchall()
    for row in manual_runs:
        run_id = row["id"]
        fd = _try_lock(f"manual-{run_id}.lock")
        if fd is None:
            continue
        try:
            with _connect() as db:
                if row["detached"]:
                    status = "cancelled" if row["cancel_requested"] else "failed"
                    _finish(db, run_id, status, "The desktop worker stopped before completing this run.")
                else:
                    _finish(db, run_id, "failed", "The interactive workbench disconnected during this run.")
        finally:
            _unlock_manual(run_id, fd)


def rpc(request):
    op = request.get("op")
    if op == "browse_folders":
        from fleet_desktop import browse_folders

        return browse_folders(request)
    if op == "sidebar_agents":
        from fleet_models import sidebar_agents
        return sidebar_agents(request.get("repos"))
    if op in {"cursor_trust_status", "set_cursor_trust"}:
        from fleet_cursor_trust import workspace_trust
        from fleet_hosts import workspace_root
        return workspace_trust(workspace_root(request["repo"]), request)
    if op in {"workspace_settings", "workspace_agents", "add_agent", "rename_agent", "delete_agent", "set_model",
              "models", "reset_session", "list_sessions", "resume_session", "cli_command", "set_cli_command", "test_cli_command"}:
        from fleet_models import workspace
        from fleet_hosts import workspace_root
        return workspace(workspace_root(request["repo"]), request)
    if op in {"files", "read_file", "save_file", "find_files", "submit", "git_status", "git_diff", "git_fetch"}:
        from fleet_desktop import workspace

        return workspace(request)
    if op == "snapshot":
        _ensure_daemon()
        _reap_manual()
        with _connect() as db:
            db.execute("BEGIN")
            runs = db.execute(RUNS_VIEW + " ORDER BY started DESC, id").fetchall()
            schedules = db.execute(
                """SELECT id, agent, repo, substr(prompt, 1, 256) AS prompt, minutes, enabled, next_run, view
                   FROM schedules ORDER BY rowid DESC"""
            ).fetchall()
        return {"runs": [dict(row, detached=bool(row["detached"])) for row in runs], "schedules": [
            dict(row, enabled=bool(row["enabled"])) for row in schedules
        ]}
    if op == "detail":
        _reap_manual()
        run_id = request.get("id")
        with _connect() as db:
            db.execute("BEGIN")
            run = db.execute(RUNS_VIEW + " WHERE id = ?", (run_id,)).fetchone()
            approvals = db.execute(
                "SELECT id, title, detail, decision FROM approvals WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
            revision = None
            if "revision" in request and run is not None:
                sequence = db.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
                revision = f"{run['status']}:{sequence}:{len(approvals)}:{sum(row['decision'] is not None for row in approvals)}"
                if revision == request["revision"]:
                    return {"unchanged": True}
            rows = db.execute(
                "SELECT kind, text, at FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        metadata = dict(run, detached=bool(run["detached"])) if run is not None else None
        reply = {"events": [dict(row) for row in rows], "run": metadata}
        if approvals:
            reply["approvals"] = [dict(row) for row in approvals]
        if revision is not None:
            reply["revision"] = revision
        return reply
    if op == "decide_approval":
        decision = request.get("decision")
        if decision not in ("accept", "decline"):
            raise ValueError("Choose whether to approve or decline this request.")
        with _connect() as db:
            result = db.execute("""
                UPDATE approvals SET decision=?
                WHERE id=? AND run_id=? AND decision IS NULL
                AND EXISTS (SELECT 1 FROM runs WHERE runs.id=approvals.run_id
                            AND detached=1 AND status='running' AND cancel_requested=0)
            """, (decision, request.get("approval"), request.get("id")))
            if not result.rowcount:
                raise ValueError("This approval is no longer pending.")
        return {}
    if op == "cancel":
        with _connect() as db:
            result = db.execute(
                "UPDATE runs SET cancel_requested = 1 WHERE id = ? AND detached = 1 AND status = 'running'",
                (request.get("id"),),
            )
            if not result.rowcount:
                raise ValueError("Only an active desktop run can be interrupted")
        return {}
    if op == "schedule":
        with _connect() as db:
            row = db.execute("SELECT * FROM schedules WHERE id = ?", (request.get("id"),)).fetchone()
        if row is None:
            raise ValueError("Schedule was not found on this host")
        return {"schedule": dict(row, enabled=bool(row["enabled"]))}
    if op in {"create", "update", "pause", "resume", "delete"}:
        _edit_schedule(op, request)
        if op in {"create", "resume", "update"}:
            _ensure_daemon(prefer_runtime=True)
        return {}
    if op == "wake":
        _ensure_daemon(prefer_runtime=True)
        return {}
    raise ValueError("Unknown Fleet job operation")


def _edit_schedule(op, request):
    schedule_id = request.get("id")
    if op in {"create", "update"}:
        agent, repo, prompt, minutes = (request.get(key) for key in ("agent", "repo", "prompt", "minutes"))
        view = request.get("view", "actions")
        if agent not in ("codex", "cursor"):
            raise ValueError("Choose Codex or Cursor")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("Enter an existing repository directory on this host")
        try:
            repo = Path(repo).expanduser().resolve(strict=True)
        except (ValueError, OSError, RuntimeError) as error:
            raise ValueError("Enter an existing repository directory on this host") from error
        if not repo.is_dir():
            raise ValueError("Enter an existing repository directory on this host")
        repo = str(repo)
        if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt or len(prompt) > 32000:
            raise ValueError("Enter a prompt of up to 32,000 characters")
        if type(minutes) is not int or not 1 <= minutes <= 525600:
            raise ValueError("Interval must be a whole number of minutes between 1 and 525,600")
        if not isinstance(view, str) or view not in VIEWS:
            raise ValueError("Choose an actions, summary, or risks view")
    now = time.time()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if op == "create":
            if db.execute("SELECT count(*) FROM schedules").fetchone()[0] >= MAX_SCHEDULES:
                raise ValueError(f"This host already has {MAX_SCHEDULES} schedules")
            db.execute(
                """INSERT INTO schedules(id, agent, repo, prompt, minutes, enabled, next_run, view)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (uuid4().hex, agent, repo, prompt, minutes, now + minutes * 60, view),
            )
            return
        schedule = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if schedule is None:
            raise ValueError("Schedule was not found on this host")
        if op == "update":
            db.execute(
                "UPDATE schedules SET agent = ?, repo = ?, prompt = ?, minutes = ?, next_run = ?, view = ? WHERE id = ?",
                (agent, repo, prompt, minutes, now + minutes * 60 if schedule["enabled"] else None,
                 request.get("view", schedule["view"]), schedule_id),
            )
        if op == "resume" and not schedule["enabled"]:
            db.execute(
                "UPDATE schedules SET enabled = 1, next_run = ? WHERE id = ?",
                (now + schedule["minutes"] * 60, schedule_id),
            )
        if op in {"pause", "delete"}:
            db.execute(
                "UPDATE runs SET cancel_requested = 1 WHERE schedule_id = ? AND status = 'running'", (schedule_id,)
            )
            if op == "pause":
                db.execute("UPDATE schedules SET enabled = 0, next_run = NULL WHERE id = ?", (schedule_id,))
            if op == "delete":
                db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def _has_work(db):
    return db.execute("""
        SELECT 1 FROM schedules WHERE enabled = 1
        UNION ALL SELECT 1 FROM runs WHERE slot IS NOT NULL AND status = 'running'
        LIMIT 1
    """).fetchone() is not None


def _ensure_daemon(prefer_runtime=False):
    fd = _try_lock("scheduler.lock")
    if fd is None and not prefer_runtime:
        return
    try:
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not _has_work(db):
                return
            db.execute(
                "INSERT INTO scheduler VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET runtime = excluded.runtime",
                (RUNTIME,),
            )
        if fd is not None:
            _spawn(RUNTIME, "_daemon", fd, "restart")
    finally:
        if fd is not None:
            os.close(fd)


def _spawn(runtime, command, fd, argument):
    return subprocess.Popen(
        [sys.executable, runtime, command, str(fd), argument],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, pass_fds=(fd,),
    )


def _take_slot(slot, now):
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        abandoned = db.execute(
            "SELECT id, cancel_requested FROM runs WHERE slot = ? AND status = 'running'", (slot,)
        ).fetchall()
        for row in abandoned:
            _finish(db, row["id"], "cancelled" if row["cancel_requested"] else "failed",
                    "The scheduled worker stopped during this run.")
        schedule = db.execute(
            """SELECT s.* FROM schedules AS s
               WHERE s.enabled = 1 AND s.next_run <= ?
               AND NOT EXISTS (
                   SELECT 1 FROM runs WHERE schedule_id = s.id AND status = 'running'
               )
               ORDER BY s.next_run LIMIT 1""",
            (now,),
        ).fetchone()
        if schedule is None:
            return None
        run_id = uuid4().hex
        db.execute(
            "UPDATE schedules SET next_run = ? WHERE id = ?", (now + schedule["minutes"] * 60, schedule["id"])
        )
        db.execute(
            """INSERT INTO runs(id, agent, repo, prompt, status, started, schedule_id, slot, view, title)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)""",
            (run_id, schedule["agent"], schedule["repo"], schedule["prompt"], now, schedule["id"], slot,
             schedule["view"], _title(schedule["prompt"])),
        )
        _record(db, run_id, "prompt", schedule["prompt"])
        return run_id


def _tick(now):
    with _connect() as db:
        db.execute(
            """UPDATE schedules SET next_run = ? + minutes * 60 WHERE enabled = 1 AND next_run <= ?
               AND (next_run + minutes * 60 <= ?
                    OR EXISTS (SELECT 1 FROM runs WHERE schedule_id = schedules.id AND status = 'running'))""",
            (now, now, now),
        )
    children = []
    for slot in range(SLOTS):
        fd = _try_lock(f"slot-{slot}.lock")
        if fd is None:
            continue
        try:
            run_id = _take_slot(slot, now)
            if run_id is not None:
                try:
                    children.append(_spawn(RUNTIME, "_run", fd, run_id))
                except OSError as error:
                    with _connect() as db:
                        _finish(db, run_id, "failed", f"Could not launch the scheduled worker: {error}")
        finally:
            # The worker inherits the open file description and keeps this lock.
            os.close(fd)
    return children


def _daemon(fd, startup):
    if startup == "restart":
        with _connect() as db:
            now = time.time()
            db.execute(
                "UPDATE schedules SET next_run = ? + minutes * 60 WHERE enabled = 1 AND next_run <= ?",
                (now, now),
            )
    children = []
    while True:
        with _connect() as db:
            target = db.execute("SELECT runtime FROM scheduler WHERE id = 1").fetchone()
        if target and target["runtime"] != RUNTIME and Path(target["runtime"]).is_file():
            try:
                _spawn(target["runtime"], "_daemon", fd, "handoff")
            except OSError:
                pass
            else:
                return
        children = [child for child in children if child.poll() is None]
        children.extend(_tick(time.time()))
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not _has_work(db):
                # A concurrent create/resume acquires the daemon lock after committing its schedule.
                fcntl.flock(fd, fcntl.LOCK_UN)
                return
        time.sleep(POLL_SECONDS)


def _watch_run(run_id, done, interrupted):
    deadline = time.monotonic() + RUN_TIMEOUT
    while not done.wait(POLL_SECONDS):
        cancelled = False
        try:
            with _connect() as db:
                row = db.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
            cancelled = row is None or row["cancel_requested"]
        except sqlite3.OperationalError:
            pass
        if not cancelled and time.monotonic() < deadline:
            continue
        if done.is_set():
            return
        interrupted.append("cancelled" if cancelled else "failed")
        os.kill(os.getpid(), signal.SIGINT)
        if not done.wait(STOP_GRACE):
            os._exit(124)
        return


def _run_scheduled(run_id):
    done = threading.Event()
    interrupted = []
    status = "failed"
    error = session = None
    with _connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id = ? AND status = 'running'", (run_id,)).fetchone()
    if row is None:
        return
    if row["cancel_requested"]:
        end_run(run_id, "cancelled")
        return
    watcher = threading.Thread(target=_watch_run, args=(run_id, done, interrupted), daemon=True)

    def on_event(kind, text):
        nonlocal status
        if kind == "status" and text in TERMINAL:
            status = text
            return
        event(run_id, kind, text)

    try:
        watcher.start()
        from fleet_harness import HarnessError, run

        from fleet_models import get

        try:
            session = run(Path(row["repo"]), row["agent"], row["prompt"], set(), None, False,
                          on_event=on_event, detailed=True, silent=True, view=row["view"],
                          model=get(row["repo"], row["agent"])["model"])
        except HarnessError:
            pass
    except KeyboardInterrupt:
        status = "cancelled"
    except Exception as failure:
        error = str(failure)
    finally:
        done.set()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if interrupted:
            status = interrupted[0]
            if status == "failed":
                error = "The scheduled run reached its 30-minute time limit."
        with _connect() as db:
            _finish(db, run_id, status, error, session if status == "completed" else None)


if __name__ == "__main__" and len(sys.argv) == 4:
    command, fd, argument = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    try:
        if command == "_daemon":
            _daemon(fd, argument)
        if command == "_run":
            _run_scheduled(argument)
    finally:
        os.close(fd)
