"""JSON bridge for Fleet's desktop workspace."""

from contextlib import ExitStack, closing, contextmanager
import fcntl
import hashlib
from heapq import nsmallest
import json
import os
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from time import monotonic
from uuid import uuid4


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_PAGE = 200
MAX_SEARCH_ENTRIES = 50000
SEARCH_SECONDS = 2.0
MANUAL_SLOTS = 4
WORKSPACE_OPS = {
    "files", "find_files", "read_file", "save_file", "submit",
    "workspace_settings", "workspace_agents", "set_model", "models", "reset_session", "list_sessions", "resume_session",
    "cursor_trust_status", "set_cursor_trust",
    "cli_command", "set_cli_command", "test_cli_command",
}


def _parts(path, *, allow_root=False):
    if path == "" and allow_root:
        return []
    if not isinstance(path, str) or len(path) > 4096 or "\0" in path:
        raise ValueError("Choose a repository-relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        raise ValueError("Choose a repository-relative path outside Git metadata")
    return parts


@contextmanager
def _directory(root, parts):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _page(root, path, cursor, limit):
    parts = _parts(path, allow_root=True)
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"File page size must be between 1 and {MAX_PAGE}")
    after = ("", "", "")
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError("Invalid file page cursor")
        kind, colon, name = cursor.partition(":")
        if not colon or kind not in {"directory", "file"} or not name or "/" in name:
            raise ValueError("Invalid file page cursor")
        after = (kind, name.casefold(), name)
    with _directory(root, parts) as fd, os.scandir(fd) as children:
        def entries():
            for child in children:
                if child.name.casefold() == ".git":
                    continue
                try:
                    child.name.encode("utf-8")
                    if child.is_dir(follow_symlinks=False):
                        kind = "directory"
                    elif child.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        continue
                except (FileNotFoundError, UnicodeEncodeError):
                    continue
                key = (kind, child.name.casefold(), child.name)
                if key > after:
                    yield key

        ordered = nsmallest(limit + 1, entries())
    page = ordered[:limit]
    results = [
        {"name": name, "path": "/".join([*parts, name]), "kind": kind}
        for kind, _, name in page
    ]
    cursor = f"{page[-1][0]}:{page[-1][2]}" if len(ordered) > limit else None
    return {"entries": results, "cursor": cursor}


def browse_folders(request):
    """List directories on the execution host before a workspace is selected."""
    from fleet_hosts import workspace_root

    directory = workspace_root(request.get("path", "~"))
    cursor = request.get("cursor")
    if cursor is not None and (
        not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or "/" in cursor
        or cursor in (".", "..") or not cursor.isprintable()
    ):
        raise ValueError("Invalid folder page cursor")
    after = ("", "") if cursor is None else (cursor.casefold(), cursor)
    try:
        with os.scandir(directory) as children:
            def entries():
                for child in children:
                    key = (child.name.casefold(), child.name)
                    if key <= after or not child.name.isprintable():
                        continue
                    try:
                        if child.is_dir(follow_symlinks=True):
                            yield key
                    except OSError:
                        continue

            ordered = nsmallest(MAX_PAGE + 1, entries())
    except OSError as error:
        raise ValueError(f"Could not list folders in {directory}: {error.strerror}") from error
    folders = [name for _, name in ordered[:MAX_PAGE]]
    more = len(ordered) > MAX_PAGE
    return {
        "directory": str(directory),
        "parent": str(directory.parent) if directory.parent != directory else None,
        "folders": folders,
        "more": more,
        "cursor": folders[-1] if more else None,
    }


def _git_files(root):
    args = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    process = subprocess.Popen(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = monotonic() + SEARCH_SECONDS
    entries = 0
    buffer = b""
    try:
        with DefaultSelector() as selector:
            selector.register(process.stdout, EVENT_READ)
            while monotonic() < deadline and selector.select(max(0, deadline - monotonic())):
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    code = process.wait(timeout=max(.1, deadline - monotonic()))
                    if code:
                        raise subprocess.CalledProcessError(code, args)
                    return
                paths = (buffer + chunk).split(b"\0")
                buffer = paths.pop()
                if len(buffer) > 4096:
                    raise ValueError("Git returned an invalid workspace path")
                for raw in paths:
                    if entries >= MAX_SEARCH_ENTRIES or monotonic() >= deadline:
                        yield None
                        return
                    entries += 1
                    try:
                        yield raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
        yield None
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _filesystem_files(root):
    deadline = monotonic() + SEARCH_SECONDS
    pending = [()]
    entries = 0
    while pending:
        directory = pending.pop()
        try:
            with _directory(root, directory) as fd, os.scandir(fd) as children:
                for child in children:
                    if entries >= MAX_SEARCH_ENTRIES or monotonic() >= deadline:
                        yield None
                        return
                    entries += 1
                    if child.name.casefold() == ".git":
                        continue
                    try:
                        child.name.encode("utf-8")
                        if child.is_dir(follow_symlinks=False):
                            pending.append((*directory, child.name))
                        elif child.is_file(follow_symlinks=False):
                            yield "/".join((*directory, child.name))
                    except (OSError, UnicodeEncodeError):
                        continue
        except OSError:
            continue


def _find_files(root, query, limit):
    if not isinstance(query, str) or len(query) > 256 or "\0" in query:
        raise ValueError("Search with up to 256 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"File result limit must be between 1 and {MAX_PAGE}")
    query = query.casefold().strip()
    terms = query.split()

    def collect(paths):
        truncated = False
        seen = set()

        def matches(open_parent):
            nonlocal truncated
            parent = None
            fd = None
            for path in paths:
                if path is None:
                    truncated = True
                    return
                if path in seen:
                    continue
                seen.add(path)
                folded = path.casefold()
                name = folded.rsplit("/", 1)[-1]
                if not query or query == name:
                    rank = 0
                elif name.startswith(query):
                    rank = 1
                elif query in name:
                    rank = 2
                elif all(term in folded for term in terms):
                    rank = 3
                elif len(terms) == 1:
                    remaining = iter(name)
                    if not all(letter in remaining for letter in query):
                        continue
                    rank = 4
                else:
                    continue
                try:
                    parts = _parts(path)
                    if parts[:-1] != parent:
                        open_parent.close()
                        parent = parts[:-1]
                        fd = None
                        fd = open_parent.enter_context(_directory(root, parent))
                    if fd is None or not stat.S_ISREG(
                        os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
                    ):
                        continue
                except (ValueError, OSError):
                    continue
                yield rank, folded, path

        with ExitStack() as open_parent:
            top = nsmallest(limit + 1, matches(open_parent))
        return {"files": [path for _, _, path in top[:limit]], "more": truncated or len(top) > limit}

    try:
        with closing(_git_files(root)) as paths:
            return collect(paths)
    except (OSError, subprocess.SubprocessError):
        with closing(_filesystem_files(root)) as paths:
            return collect(paths)


def _revision(info, content):
    identity = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:".encode("ascii")
    return hashlib.sha256(identity + content).hexdigest()


def _read(fd, name):
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Only regular text files can be opened")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("Fleet can edit text files up to 256 KiB")
        content = file.read(MAX_FILE_BYTES + 1)
        after = os.fstat(file.fileno())
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Fleet can edit text files up to 256 KiB")
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("The file changed while it was being read")
    if b"\0" in content:
        raise ValueError("Only UTF-8 text files can be opened")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Only UTF-8 text files can be opened") from error
    return text, _revision(after, content), after


def _save(root, parts, text, revision):
    from fleet_jobs import state_dir

    if not isinstance(text, str) or "\0" in text:
        raise ValueError("Only UTF-8 text files can be saved")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Only UTF-8 text files can be saved") from error
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Fleet can edit text files up to 256 KiB")
    if not isinstance(revision, str) or len(revision) != 64:
        raise ValueError("Open the file before saving changes")
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(directory / "editor.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "wb") as lock, _directory(root, parts[:-1]) as fd:
        fcntl.flock(lock, fcntl.LOCK_EX)
        name = parts[-1]
        _, current, info = _read(fd, name)
        if revision != current:
            return {"conflict": True, "revision": current}
        temporary = f".fleet-save-{uuid4().hex}"
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        try:
            with os.fdopen(file_fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
                os.fsync(out.fileno())
                saved = _revision(os.fstat(out.fileno()), data)
            _, current, _ = _read(fd, name)
            if revision != current:
                return {"conflict": True, "revision": current}
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        return {"revision": saved}


def workspace(request):
    from fleet_hosts import workspace_root
    root = workspace_root(request.get("repo", "."))
    op = request.get("op")
    if op == "files":
        return _page(root, request.get("path", ""), request.get("cursor"), request.get("limit", 100))
    if op == "find_files":
        return _find_files(root, request.get("query", ""), request.get("limit", 100))
    if op == "submit":
        return _submit(root, request)
    parts = _parts(request.get("path"))
    if op == "read_file":
        with _directory(root, parts[:-1]) as fd:
            text, revision, _ = _read(fd, parts[-1])
        return {"text": text, "revision": revision}
    if op == "save_file":
        return _save(root, parts, request.get("text"), request.get("revision"))
    raise ValueError("Unknown Fleet workspace operation")


def _submit(root, request):
    from fleet_jobs import MANUAL_LOCKS, _reap_manual, _try_lock, begin_run, end_run

    agent, prompt = request.get("agent"), request.get("prompt")
    view, session = request.get("view", "actions"), request.get("session")
    tags, quiet = request.get("tags", []), request.get("filter", False)
    if agent not in ("codex", "cursor"):
        raise ValueError("Choose Codex or Cursor")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000 or "\0" in prompt:
        raise ValueError("Enter a prompt of up to 32,000 characters")
    if not isinstance(tags, list) or len(tags) > 32:
        raise ValueError("Choose up to 32 repository context paths")
    if session is not None and (not isinstance(session, str) or not 1 <= len(session) <= 512 or not session.isprintable()):
        raise ValueError("Choose a valid session to continue")
    approval_cards = request.get("approval_cards", False)
    if type(approval_cards) is not bool:
        raise ValueError("Invalid approval card setting")
    yolo = request.get("yolo", False)
    if type(yolo) is not bool or (yolo and (agent != "codex" or approval_cards)):
        raise ValueError("YOLO mode requires Codex with Approval Cards off")
    if type(quiet) is not bool:
        raise ValueError("Invalid classifier setting")
    for tag in tags:
        parts = _parts(tag)
        with _directory(root, parts[:-1]) as fd:
            mode = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Choose context inside the repository: {tag}")

    from fleet_models import agent_lane, get
    lane = agent_lane(request.get("lane", 1))
    settings = get(root, agent, lane)
    if session is None:
        session = settings["session"]
    _reap_manual()
    slot = next((fd for number in range(MANUAL_SLOTS)
                 if (fd := _try_lock(f"desktop-slot-{number}.lock")) is not None), None)
    if slot is None:
        raise ValueError("This host already has four desktop runs in progress")
    try:
        run_id = begin_run(root, agent, prompt, view, session=session, detached=True,
                           known_session=session == settings["session"], lane=lane, generation=settings["generation"])
        fd = MANUAL_LOCKS[run_id]
        try:
            with tempfile.TemporaryFile() as payload:
                payload.write(json.dumps({
                    "repo": str(root), "agent": agent, "prompt": prompt, "view": view,
                    "session": session, "tags": tags, "filter": quiet,
                    "model": settings["model"], "generation": settings["generation"], "lane": lane,
                    "approval_cards": approval_cards and agent == "codex", "yolo": yolo,
                }).encode("ascii"))
                payload.seek(0)
                subprocess.Popen(
                    [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()),
                     "_run", run_id, str(fd), str(slot)],
                    stdin=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, pass_fds=(fd, slot),
                )
        except Exception as error:
            end_run(run_id, "failed", error=f"Could not launch the desktop run: {error}")
            raise
        os.close(MANUAL_LOCKS.pop(run_id))
    finally:
        os.close(slot)
    return {"id": run_id}


def _run(run_id, fd):
    from fleet_harness import HarnessError, run
    from fleet_jobs import MANUAL_LOCKS, TERMINAL, _connect, _watch_run, end_run, event

    MANUAL_LOCKS[run_id] = fd
    done = threading.Event()
    interrupted = []
    watcher = threading.Thread(target=_watch_run, args=(run_id, done, interrupted), daemon=True)
    status, session, error = "failed", None, None

    def on_event(kind, text):
        nonlocal status
        if kind == "status" and text in TERMINAL:
            status = text
        else:
            event(run_id, kind, text)

    try:
        request = json.load(sys.stdin)
        with _connect() as db:
            row = db.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row["cancel_requested"]:
            status = "cancelled"
            return
        watcher.start()
        session = run(
            Path(request["repo"]), request["agent"], request["prompt"], set(request["tags"]),
            request["session"], request["filter"], on_event=on_event, detailed=True, silent=True,
            view=request["view"], model=request["model"],
            interactive_run=run_id if request.get("approval_cards") else None, yolo=request.get("yolo", False),
        )
        if session and status == "completed":
            from fleet_models import remember
            remember(request["repo"], request["agent"], session, request["generation"], request["lane"])
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
                error = "The desktop run reached its 30-minute time limit."
        end_run(run_id, status, error=error, session=session)


def dispatch(message):
    import fleet_hosts

    op = message.get("op")
    if op == "hosts":
        return {"hosts": fleet_hosts.hosts()}
    if op == "import_workspaces":
        return fleet_hosts.import_workspaces(message.get("paths"))
    if op == "save_host":
        host = message.get("host")
        if not isinstance(host, dict):
            raise ValueError("Choose a host to save")
        host = {"id": uuid4().hex, **host}
        _validate_host(host)
        fleet_hosts.save_host(host)
        return {"hosts": fleet_hosts.hosts()}
    if op == "remove_host":
        if not isinstance(message.get("id"), str) or not message["id"]:
            raise ValueError("Choose a host to remove")
        fleet_hosts.remove_host(message["id"])
        return {"hosts": fleet_hosts.hosts()}
    if op == "cursor_login":
        from fleet_cursor_login import login
        host = message.get("host")
        _validate_host(host)
        return login(host["ssh"], host["repo"])
    if op == "connect_host":
        from fleet_ssh import authenticate

        host = message.get("host")
        _validate_host(host)
        authenticate(host["ssh"])
        return {}

    host, request = message.get("host"), message.get("request")
    _validate_host(host)
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        raise ValueError("Invalid Fleet host request")
    request = dict(request)
    if request["op"] in WORKSPACE_OPS:
        request["repo"] = host["repo"]
    elif request["op"] in {"create", "update"}:
        request.setdefault("repo", host["repo"])
    return fleet_hosts.request(host, request)


def _validate_host(host):
    if not isinstance(host, dict) or any(
        not isinstance(host.get(key), str) or "\0" in host[key] for key in ("id", "label", "ssh", "repo")
    ):
        raise ValueError("Invalid Fleet host")
    if not host["id"] or not host["label"].strip() or not host["repo"].strip():
        raise ValueError("Choose a host name and workspace directory")


def _reply(handle, data):
    try:
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Fleet request exceeds 2 MiB")
        message = json.loads(data)
        if not isinstance(message, dict):
            raise ValueError("Invalid Fleet request")
        reply = handle(message)
    except Exception as error:
        reply = {"error": str(error)}
    return json.dumps(reply, ensure_ascii=False).encode("utf-8", errors="backslashreplace")


def serve(handle):
    sys.stdout.buffer.write(_reply(handle, sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)) + b"\n")


def serve_reads():
    # One request is in flight at a time. Exit when the app closes the pipe or goes idle.
    stream = sys.stdin.buffer
    with DefaultSelector() as ready:
        ready.register(stream, EVENT_READ)
        while ready.select(30):
            line = stream.readline(MAX_REQUEST_BYTES + 2)
            if not line or not line.endswith(b"\n"):
                return
            data = _reply(dispatch, line[:-1])
            sys.stdout.buffer.write(len(data).to_bytes(4, "big") + data)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "_run":
        try:
            _run(sys.argv[2], int(sys.argv[3]))
        finally:
            os.close(int(sys.argv[4]))
    elif sys.argv[1:] == ["--read-stream"]:
        serve_reads()
    else:
        serve(dispatch)
