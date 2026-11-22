"""Local dashboard inventory. Agents and schedules are owned by their execution host."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import uuid


MAX_IMPORT_PATHS = 64
MAX_IMPORT_FOLDERS = 128
MAX_WORKSPACE_BYTES = 512 * 1024
JSONC_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
JSONC_COMMAS = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')
HOSTS_SCHEMA = """
    CREATE TABLE hosts (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        ssh TEXT NOT NULL,
        repo TEXT NOT NULL,
        folder_group TEXT NOT NULL DEFAULT '',
        group_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (ssh, repo)
    )
"""


def _state_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "vhscode/fleet"


def _inventory():
    base = _state_dir()
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    db = sqlite3.connect(base / "inventory.sqlite")
    db.row_factory = sqlite3.Row
    try:
        if "folder_group" not in {row["name"] for row in db.execute("PRAGMA table_info(hosts)")}:
            with db:
                db.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in db.execute("PRAGMA table_info(hosts)")}
                if "folder_group" not in columns:
                    if columns:
                        db.execute("ALTER TABLE hosts RENAME TO previous_hosts")
                    db.execute(HOSTS_SCHEMA)
                    if columns:
                        db.execute("INSERT INTO hosts(id, label, ssh, repo) "
                                   "SELECT id, label, ssh, repo FROM previous_hosts ORDER BY label")
                        db.execute("DROP TABLE previous_hosts")
    except BaseException:
        db.close()
        raise
    return db


def _host_rows(db):
    rows = [dict(row) for row in db.execute("""
        SELECT id, label, ssh, repo, folder_group AS "group" FROM hosts
        ORDER BY CASE WHEN folder_group = '' THEN rowid ELSE min(rowid) OVER (PARTITION BY folder_group) END,
                 group_order, rowid
    """)]
    local = next((row for row in rows if row["id"] == "local"), None)
    return [local or dict(id="local", label="Local", ssh="", repo=str(Path.cwd()), group=""),
            *[row for row in rows if row["id"] != "local"]]


def hosts():
    with closing(_inventory()) as db:
        return _host_rows(db)


def save_host(host):
    from fleet_ssh import destination

    ssh = "" if host["id"] == "local" or not host["ssh"] else destination(host["ssh"])
    if not host["repo"].strip() or not host["label"].strip():
        raise ValueError("A display name and default repository are required.")
    repo = host["repo"] if ssh else str(Path(host["repo"]).expanduser().resolve())
    group = host.get("group")
    if group is not None and (not isinstance(group, str) or len(group) > 255 or "\0" in group):
        raise ValueError("Choose a valid workspace group name.")
    try:
        with closing(_inventory()) as db, db:
            db.execute("""
                INSERT INTO hosts(id, label, ssh, repo, folder_group) VALUES (?, ?, ?, ?, COALESCE(?, ''))
                ON CONFLICT(id) DO UPDATE SET label = excluded.label, ssh = excluded.ssh, repo = excluded.repo,
                    folder_group = COALESCE(?, hosts.folder_group)
            """, (host["id"], host["label"], ssh, repo, group, group))
    except sqlite3.IntegrityError as error:
        raise ValueError("This repository is already saved for this host.") from error


def remember(host, repo):
    with closing(_inventory()) as db, db:
        db.execute("INSERT OR IGNORE INTO hosts(id, label, ssh, repo) VALUES (?, ?, ?, ?)",
                   (uuid.uuid4().hex, host, host, repo))


def _workspace_folders(workspace):
    with workspace.open("rb") as source:
        raw = source.read(MAX_WORKSPACE_BYTES + 1)
    if len(raw) > MAX_WORKSPACE_BYTES:
        raise ValueError(f"Workspace file is larger than 512 KiB: {workspace}")
    try:
        text = JSONC_COMMENTS.sub(
            lambda match: match[0] if match[0].startswith('"') else re.sub(r"[^\r\n]", " ", match[0]),
            raw.decode("utf-8-sig"),
        )
        text = JSONC_COMMAS.sub(lambda match: "" if match[0] == "," else match[0], text)
        document = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Cannot read VS Code workspace settings: {workspace}") from error
    folders = document.get("folders") if isinstance(document, dict) else None
    if not isinstance(folders, list) or not 1 <= len(folders) <= MAX_IMPORT_FOLDERS:
        raise ValueError(f"Expected 1–{MAX_IMPORT_FOLDERS} folders in {workspace}")
    group = workspace.stem if len(folders) > 1 else ""
    for order, folder in enumerate(folders, 1):
        if not isinstance(folder, dict) or ("path" in folder) == ("uri" in folder):
            raise ValueError(f"Each workspace folder needs one local path or file URI: {workspace}")
        value = folder.get("path", folder.get("uri"))
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"Invalid workspace folder path in {workspace}")
        if "uri" in folder or value.lower().startswith("file:"):
            uri = urlsplit(value)
            if uri.scheme != "file" or uri.netloc.lower() not in {"", "localhost"} or uri.query or uri.fragment:
                raise ValueError(f"Only local file URIs can be imported: {value}")
            path = Path(unquote(uri.path, errors="strict"))
            if not path.is_absolute():
                raise ValueError(f"File URI must contain an absolute path: {value}")
        elif re.match(r"(?:[A-Za-z]:[\\/]|[A-Za-z][\w+.-]*://|\\\\)", value):
            raise ValueError(f"Choose a local folder path on this machine: {value}")
        else:
            path = workspace.parent / Path(value).expanduser()
        root = _import_directory(path)
        name = folder.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 255 or "\0" in name):
            raise ValueError(f"Invalid workspace folder name in {workspace}")
        yield root, name or root.name or str(root), group, order


def _import_directory(path):
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {path}")
    return root


def import_workspaces(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_IMPORT_PATHS:
        raise ValueError(f"Choose 1–{MAX_IMPORT_PATHS} folders or .code-workspace files.")
    additions = []
    for raw in paths:
        if not isinstance(raw, str) or len(raw) > 4096 or "\0" in raw or not Path(raw).is_absolute():
            raise ValueError("Choose absolute paths to local folders or .code-workspace files.")
        path = Path(raw).resolve(strict=True)
        if path.is_dir():
            additions.append((path, path.name or str(path), "", 0))
        elif path.is_file() and Path(raw).suffix.lower() == ".code-workspace":
            additions.extend(_workspace_folders(path))
        else:
            raise ValueError(f"Choose a local folder or .code-workspace file: {raw}")
        if len(additions) > MAX_IMPORT_FOLDERS:
            raise ValueError(f"Import at most {MAX_IMPORT_FOLDERS} folders at a time.")

    with closing(_inventory()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = {}
        saved_local = False
        for row in db.execute("SELECT id, repo, folder_group FROM hosts WHERE ssh = '' ORDER BY id = 'local' DESC"):
            saved_local = saved_local or row["id"] == "local"
            try:
                root = Path(row["repo"]).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            current.setdefault(root, (row["id"], row["folder_group"]))
        selected = None
        grouped = set()
        for root, label, group, order in additions:
            existing = current.get(root)
            if existing is None:
                entry_id = "local" if not saved_local and root == Path.cwd() else uuid.uuid4().hex
                if entry_id == "local":
                    saved_local = True
                db.execute("INSERT INTO hosts(id, label, ssh, repo, folder_group, group_order) "
                           "VALUES (?, ?, '', ?, ?, ?)", (entry_id, label, str(root), group, order))
                current[root] = (entry_id, group)
            else:
                entry_id, current_group = existing
                if group and (root, group) not in grouped and current_group in {"", group}:
                    db.execute("UPDATE hosts SET folder_group = ?, group_order = ? WHERE id = ?",
                               (group, order, entry_id))
                    current[root] = (entry_id, group)
            if group:
                grouped.add((root, group))
            if selected is None:
                selected = entry_id
        return {"hosts": _host_rows(db), "selected": selected}


def remove_host(id):
    if id == "local":
        raise ValueError("The local host is always available.")
    with closing(_inventory()) as db, db:
        db.execute("DELETE FROM hosts WHERE id=?", (id,))


def request(host, payload):
    if host["ssh"]:
        from fleet_ssh import query
        return query(host["ssh"], payload)
    from fleet_jobs import rpc
    return rpc(payload)


def open_host(host):
    args = [sys.executable, str(Path(__file__).resolve().with_name("fleet.py"))]
    if host["ssh"]:
        args += ["--ssh", host["ssh"]]
    subprocess.run([*args, "--", host["repo"]])


VIEWS = ("actions", "summary", "risks")


def reporting(root, value=None):
    if value is None and not (_state_dir() / "inventory.sqlite").exists():
        return "actions"
    with closing(_inventory()) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS preferences (repo TEXT PRIMARY KEY, view TEXT NOT NULL)")
        if value is not None:
            if value not in VIEWS:
                raise ValueError("Choose actions, summary or risks.")
            db.execute("INSERT INTO preferences VALUES (?, ?) ON CONFLICT(repo) DO UPDATE SET view=excluded.view",
                       (str(root), value))
            return value
        row = db.execute("SELECT view FROM preferences WHERE repo=?", (str(root),)).fetchone()
        return row["view"] if row and row["view"] in VIEWS else "actions"
