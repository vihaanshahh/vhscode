"""Exercise the Explorer Git operations with real repositories and a local remote."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BRIDGE = Path(__file__).resolve().parents[1] / "fleet/fleet_desktop.py"


class GitExplorerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "workspace with spaces"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=temporary.name, XDG_CONFIG_HOME=temporary.name, GIT_CONFIG_NOSYSTEM="1")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "--literal-pathspecs", "-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid", *args],
            cwd=cwd or self.repo, env=self.env, capture_output=True, check=True, timeout=10,
        ).stdout

    def bridge(self, op, *, root=None, **fields):
        host = {"repo": str(root or self.repo), "id": "local", "label": "Local", "ssh": ""}
        output = subprocess.run(
            [sys.executable, "-B", BRIDGE], input=json.dumps({"host": host, "request": {"op": op, **fields}}),
            env=self.env, text=True, capture_output=True, check=True, timeout=18,
        ).stdout
        return json.loads(output)

    def test_status_and_diffs_follow_workspace_changes(self):
        self.assertEqual(self.bridge("git_status"), {"status": None})
        self.git("init", "-q", "-b", "trunk")
        folder = self.repo / "Nested Folder"
        folder.mkdir()
        changed = folder / "working file.txt"
        renamed = folder / "old name.txt"
        deleted = self.repo / "deleted.txt"
        for file in (changed, renamed, deleted):
            file.write_text("Original\n")
        self.git("add", "--", ".")
        self.git("commit", "-qm", "Initial")
        self.assertEqual(self.bridge("git_status")["status"]["changes"], [])

        changed.write_text("Staged version\n")
        self.git("add", "--", str(changed))
        changed.write_text("Working version\n")
        self.git("mv", "Nested Folder/old name.txt", "Nested Folder/new name.txt")
        deleted.unlink()
        untracked = folder / ":(glob) note.txt"
        untracked.write_text("New file\n")

        status = self.bridge("git_status")["status"]
        self.assertEqual((status["branch"], status["upstream"]), ("trunk", None))
        self.assertEqual({entry["path"]: entry["code"] for entry in status["changes"]}, {
            "Nested Folder/:(glob) note.txt": "??",
            "Nested Folder/new name.txt": "R.",
            "Nested Folder/working file.txt": "MM",
            "deleted.txt": ".D",
        })
        nested = self.bridge("git_status", root=folder)["status"]
        self.assertEqual({entry["path"]: entry["original"] for entry in nested["changes"]}, {
            ":(glob) note.txt": None, "new name.txt": "old name.txt", "working file.txt": None,
        })
        sections = self.bridge("git_diff", root=folder, path=changed.name)["sections"]
        self.assertEqual([section["title"] for section in sections], ["Staged changes", "Working tree changes"])
        self.assertIn("+Staged version", sections[0]["text"])
        self.assertIn("+Working version", sections[1]["text"])
        rename = self.bridge("git_diff", root=folder, path="new name.txt", original="old name.txt")["sections"]
        self.assertIn("rename to Nested Folder/new name.txt", rename[0]["text"])
        deletion = self.bridge("git_diff", path="deleted.txt")["sections"]
        self.assertIn("-Original", deletion[0]["text"])
        new_file = self.bridge("git_diff", root=folder, path=untracked.name, untracked=True)["sections"]
        self.assertEqual(new_file, [{"title": "Untracked file", "text": "New file\n", "truncated": False}])
        self.assertIn("error", self.bridge("git_diff", path="../outside.txt", untracked=True))

    def test_fetch_updates_upstream_comparison_without_changing_workspace(self):
        self.git("init", "-q", "-b", "trunk")
        self.git("commit", "--allow-empty", "-qm", "Initial")
        remote = self.root / "remote.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("fetch", "-q", "origin")
        self.git("branch", "--set-upstream-to", "origin/trunk")
        self.git("config", "status.aheadBehind", "false")
        self.git("commit", "--allow-empty", "-qm", "Local change")

        collaborator = self.root / "collaborator"
        self.git("clone", "-q", str(remote), str(collaborator))
        self.git("commit", "--allow-empty", "-qm", "Remote change", cwd=collaborator)
        self.git("push", "-q", "origin", "trunk", cwd=collaborator)
        pending = self.repo / "pending.txt"
        pending.write_text("Local edits\n")
        before = self.bridge("git_status")["status"]
        self.assertEqual((before["upstream"], before["ahead"], before["behind"]), ("origin/trunk", 1, 0))
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.bridge("git_fetch"), {})
        after = self.bridge("git_status")["status"]
        self.assertEqual((after["ahead"], after["behind"]), (1, 1))
        self.assertEqual(after["changes"], before["changes"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(pending.read_text(), "Local edits\n")
