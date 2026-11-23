"""JSON bridge for Fleet's desktop workspace."""

from contextlib import ExitStack, closing, contextmanager, suppress
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
MAX_GIT_STATUS_BYTES = 512 * 1024
MAX_GIT_DIFF_BYTES = 64 * 1024
MAX_GIT_CHANGES = 1500
WORKSPACE_OPS = {
    "git_status", "git_diff", "git_fetch",
    "files", "find_files", "read_file", "save_file", "submit", "steer",
    "workspace_settings", "workspace_agents", "add_agent", "rename_agent", "delete_agent",
    "set_model", "models", "reset_session", "list_sessions", "resume_session",
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


def _git_output(root, arguments, *, limit, timeout=SEARCH_SECONDS):
    command = ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false", *arguments]
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    with subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as process:
        deadline = monotonic() + timeout
        output = bytearray()
        try:
            with DefaultSelector() as selector:
                selector.register(process.stdout, EVENT_READ)
                while len(output) <= limit:
                    remaining = deadline - monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ValueError("Git did not respond in time.")
                    chunk = os.read(process.stdout.fileno(), min(64 * 1024, limit + 1 - len(output)))
                    if not chunk:
                        return process.wait(timeout=max(.1, deadline - monotonic())), bytes(output)
                    output.extend(chunk)
            return None, bytes(output[:limit])
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)


def _git_worktree_path(raw, prefix):
    if not raw.startswith(prefix):
        return None
    try:
        path = raw[len(prefix):].decode("utf-8")
        _parts(path)
    except (UnicodeDecodeError, ValueError):
        return None
    return path


def _git_status(root):
    try:
        code, location = _git_output(root, ["rev-parse", "--is-inside-work-tree", "--show-prefix"], limit=4096)
    except FileNotFoundError:
        return {"status": None}
    if code == 128 or location.startswith(b"false\n"):
        return {"status": None}
    if code or not location.startswith(b"true\n"):
        raise ValueError("Could not find the Git working tree.")
    prefix = location.removesuffix(b"\n").partition(b"\n")[2]
    code, data = _git_output(
        root, ["status", "--porcelain=v2", "-z", "--branch", "--ahead-behind",
               "--untracked-files=all", "--renames", "--", "."],
        limit=MAX_GIT_STATUS_BYTES,
    )
    if code not in (0, None):
        raise ValueError("Could not read Git status.")

    # Porcelain paths are repository-relative even when the opened folder is a subdirectory.
    records = iter(data.split(b"\0")[:-1])
    branch, head, upstream, ahead, behind = "", "", None, None, None
    changes = []
    truncated = code is None
    for record in records:
        if record.startswith(b"# branch.oid "):
            head = record[len(b"# branch.oid "):].decode("ascii")
            continue
        if record.startswith(b"# branch.head "):
            branch = record[len(b"# branch.head "):].decode("utf-8", errors="replace")
            continue
        if record.startswith(b"# branch.upstream "):
            upstream = record[len(b"# branch.upstream "):].decode("utf-8", errors="replace")
            continue
        if record.startswith(b"# branch.ab "):
            ahead, behind = map(abs, map(int, record.split()[2:]))
            continue
        kind = record[:1]
        if kind == b"?":
            status, raw, original = "??", record[2:], None
        elif kind in (b"1", b"2", b"u"):
            columns = record.split(b" ", {b"1": 8, b"2": 9, b"u": 10}[kind])
            status, raw = columns[1].decode("ascii"), columns[-1]
            original = _git_worktree_path(next(records, b""), prefix) if kind == b"2" else None
        else:
            continue
        path = _git_worktree_path(raw, prefix)
        if path is not None:
            if len(changes) == MAX_GIT_CHANGES:
                truncated = True
                break
            changes.append({"path": path, "code": status, "original": original})
    return {"status": {
        "branch": "Detached HEAD" if branch == "(detached)" else branch,
        "head": head, "upstream": upstream, "ahead": ahead, "behind": behind,
        "changes": sorted(changes, key=lambda change: change["path"]), "truncated": truncated,
    }}


def _git_diff(root, request):
    path = request.get("path")
    parts = _parts(path)
    original = request.get("original")
    if original is not None:
        _parts(original)
    if request.get("untracked") is True:
        with _directory(root, parts[:-1]) as fd:
            text, _, _ = _read(fd, parts[-1])
        return {"sections": [{"title": "Untracked file", "text": text, "truncated": False}]}

    paths = [path] if original is None else [path, original]
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames"]
    sections = []
    for title, options in (("Staged changes", ["--cached"]), ("Working tree changes", [])):
        code, output = _git_output(root, [*args, *options, "--", *paths], limit=MAX_GIT_DIFF_BYTES)
        if code not in (0, None):
            raise ValueError("Could not read the Git diff.")
        if output:
            sections.append({"title": title, "text": output.decode("utf-8", errors="replace"), "truncated": code is None})
    return {"sections": sections}


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
    if op == "git_status":
        return _git_status(root)
    if op == "git_diff":
        return _git_diff(root, request)
    if op == "git_fetch":
        code, _ = _git_output(root, ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules"], limit=4096, timeout=14)
        if code != 0:
            raise ValueError("Git could not fetch. Check the workspace’s remote and credentials.")
        return {}
    if op == "submit":
        return _submit(root, request)
    if op == "steer":
        from fleet_jobs import queue_steer

        prompt, tags = _prompt(root, request)
        return queue_steer(root, request.get("id"), prompt, json.dumps(tags))
    parts = _parts(request.get("path"))
    if op == "read_file":
        with _directory(root, parts[:-1]) as fd:
            text, revision, _ = _read(fd, parts[-1])
        return {"text": text, "revision": revision}
    if op == "save_file":
        return _save(root, parts, request.get("text"), request.get("revision"))
    raise ValueError("Unknown Fleet workspace operation")


def _prompt(root, request):
    prompt, tags = request.get("prompt"), request.get("tags", [])
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000 or "\0" in prompt:
        raise ValueError("Enter a prompt of up to 32,000 characters")
    if not isinstance(tags, list) or len(tags) > 32:
        raise ValueError("Choose up to 32 repository context paths")
    for tag in tags:
        parts = _parts(tag)
        with _directory(root, parts[:-1]) as fd:
            mode = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Choose context inside the repository: {tag}")
    return prompt, tags


def _submit(root, request):
    from fleet_jobs import MANUAL_LOCKS, _reap_manual, begin_run, end_run

    agent = request.get("agent")
    view, session = request.get("view", "actions"), request.get("session")
    quiet = request.get("filter", False)
    if agent not in ("codex", "cursor"):
        raise ValueError("Choose Codex or Cursor")
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
    prompt, tags = _prompt(root, request)
    approval_cards = approval_cards and agent == "codex"

    from fleet_models import agent_lane, get
    lane = agent_lane(request.get("lane", 1))
    settings = get(root, agent, lane)
    if session is None:
        session = settings["session"]
    _reap_manual()
    run_id = begin_run(root, agent, prompt, view, session=session, detached=True,
                       known_session=session == settings["session"], lane=lane, generation=settings["generation"],
                       steerable=approval_cards)
    fd = MANUAL_LOCKS[run_id]
    try:
        with tempfile.TemporaryFile() as payload:
            payload.write(json.dumps({
                "repo": str(root), "agent": agent, "prompt": prompt, "view": view,
                "session": session, "tags": tags, "filter": quiet,
                "model": settings["model"], "generation": settings["generation"], "lane": lane,
                "approval_cards": approval_cards, "yolo": yolo,
            }).encode("ascii"))
            payload.seek(0)
            subprocess.Popen(
                [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()), "_run", run_id, str(fd)],
                stdin=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(fd,),
            )
    except Exception as error:
        end_run(run_id, "failed", error=f"Could not launch the desktop run: {error}")
        raise
    os.close(MANUAL_LOCKS.pop(run_id))
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
    if len(sys.argv) == 4 and sys.argv[1] == "_run":
        _run(sys.argv[2], int(sys.argv[3]))
    elif sys.argv[1:] == ["--read-stream"]:
        serve_reads()
    else:
        serve(dispatch)
