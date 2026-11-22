"""Local dashboard inventory. Agents and schedules are owned by their execution host."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid


def _state_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "vhscode/fleet"


def _inventory():
    base = _state_dir()
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    db = sqlite3.connect(base / "inventory.sqlite")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS hosts (id TEXT PRIMARY KEY, label TEXT NOT NULL, "
               "ssh TEXT UNIQUE NOT NULL, repo TEXT NOT NULL)")
    return db


def hosts():
    with closing(_inventory()) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM hosts ORDER BY label")]
    local = next((h for h in rows if h["id"] == "local"), None)
    return [local or dict(id="local", label="Local", ssh="", repo=str(Path.cwd())),
            *[h for h in rows if h["id"] != "local"]]


def save_host(host):
    from fleet_ssh import destination
    ssh = "" if host["id"] == "local" else destination(host["ssh"])
    if not host["repo"].strip() or not host["label"].strip():
        raise ValueError("A display name and default repository are required.")
    with closing(_inventory()) as db, db:
        db.execute("INSERT INTO hosts VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                   "label=excluded.label, ssh=excluded.ssh, repo=excluded.repo",
                   (host["id"], host["label"], ssh, host["repo"]))


def remember(host, repo):
    with closing(_inventory()) as db, db:
        db.execute("INSERT OR IGNORE INTO hosts VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, host, host, repo))


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
