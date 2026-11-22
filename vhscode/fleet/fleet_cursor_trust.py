"""Persist Cursor workspace trust on the execution host."""
from contextlib import closing
from pathlib import Path


def cursor_trust(repo, *, update=None):
    from fleet_models import _database

    root = str(Path(repo).expanduser().resolve())
    with closing(_database()) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS cursor_trusted_workspaces (repo TEXT PRIMARY KEY)")
        if update is True:
            database.execute("INSERT OR IGNORE INTO cursor_trusted_workspaces(repo) VALUES (?)", (root,))
        elif update is False:
            database.execute("DELETE FROM cursor_trusted_workspaces WHERE repo=?", (root,))
        return database.execute("SELECT 1 FROM cursor_trusted_workspaces WHERE repo=?", (root,)).fetchone() is not None


def workspace_trust(repo, request):
    if request["op"] == "cursor_trust_status":
        return {"trusted": cursor_trust(repo)}
    trusted = request.get("trusted")
    if type(trusted) is not bool:
        raise ValueError("Choose whether to trust this workspace.")
    return {"trusted": cursor_trust(repo, update=trusted)}
