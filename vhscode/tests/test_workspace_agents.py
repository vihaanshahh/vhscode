"""Exercise overlapping CLI sessions across short-lived desktop bridge processes."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


BRIDGE = Path(__file__).resolve().parents[1] / "fleet/fleet_desktop.py"
CLI = """import json, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read().strip()
session = args[-2] if 'resume' in args else 'thread-' + prompt
def emit(kind, **fields):
    print(json.dumps(dict(type=kind, **fields)), flush=True)
emit('thread.started', thread_id=session)
for _ in range(600):
    if pathlib.Path('release').exists(): break
    time.sleep(.03)
text = '## ' + session + '\\n\\n```python\\n' + ('# sample\\n' * 550) + '```'
emit('item.completed', item={'type': 'agent_message', 'text': text})
emit('turn.completed')
"""


class WorkspaceAgentsTests(unittest.TestCase):
    def test_concurrent_lanes_restore_and_resume_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "work with spaces"
            root.mkdir()
            binary = home / "bin"
            binary.mkdir()
            executable = binary / "codex"
            executable.write_text(f"#!{sys.executable}\n" + CLI)
            executable.chmod(0o700)
            host = dict(id="test", label="Test", ssh="", repo="~/work with spaces")
            environment = dict(os.environ, HOME=temporary, XDG_STATE_HOME=str(home/"state"),
                               PATH=str(binary) + os.pathsep + os.environ["PATH"])
            state = home / "state/vhscode/fleet"
            state.mkdir(parents=True)
            with sqlite3.connect(state / "workspace-settings.sqlite3") as settings:
                settings.executescript("""
                    CREATE TABLE workspace_settings (
                        repo TEXT NOT NULL, agent TEXT NOT NULL, model TEXT, session TEXT,
                        generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(repo, agent)
                    );
                    CREATE TABLE additional_sessions (
                        repo TEXT NOT NULL, agent TEXT NOT NULL, lane INTEGER NOT NULL, session TEXT,
                        generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(repo, agent, lane)
                    );
                """)
                settings.execute("INSERT INTO workspace_settings(repo, agent, model) VALUES (?, 'cursor', 'legacy-model')",
                                 (str(root.resolve()),))

            def call(op, **fields):
                process = subprocess.run([sys.executable, "-B", BRIDGE], input=json.dumps({
                    "host": host, "request": dict(op=op, **fields)}), capture_output=True,
                    text=True, check=True, env=environment)
                return json.loads(process.stdout)

            def completed(run_id):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    detail = call("detail", id=run_id)
                    if detail["run"]["status"] != "running":
                        self.assertEqual(detail["run"]["status"], "completed", detail)
                        return detail
                    time.sleep(.04)
                self.fail("Agent did not finish")

            self.assertEqual(call("workspace_agents")["instances"], [])
            self.assertEqual(call("workspace_settings")["cursor"]["model"], "legacy-model")
            first = call("submit", agent="codex", lane=1, prompt="one")["id"]
            second = call("submit", agent="codex", lane=2, prompt="two")["id"]
            self.assertEqual(call("rename_agent", agent="codex", lane=1, name="Implementation"),
                             {"lane": 1, "name": "Implementation"})
            self.assertIn("already running", call("submit", agent="codex", lane=2, prompt="duplicate")["error"])
            self.assertIn("active task", call("delete_agent", agent="codex", lane=2)["error"])
            self.assertEqual(call("rename_agent", agent="codex", lane=2, name="Review API"),
                             {"lane": 2, "name": "Review API"})
            extra_runs = []
            for lane, name in enumerate(("Build", "Docs", "Vérification"), start=3):
                self.assertEqual(call("add_agent", agent="codex", name=f" {name} "), {"lane": lane, "name": name})
                extra_runs.append(call("submit", agent="codex", lane=lane, prompt=name)["id"])
            listings = call("sidebar_agents", repos=[host["repo"]])["workspaces"][host["repo"]]
            self.assertEqual([(row["lane"], row["name"], row["status"]) for row in listings], [
                (1, "Implementation", "running"), (2, "Review API", "running"), (3, "Build", "running"),
                (4, "Docs", "running"), (5, "Vérification", "running"),
            ])
            self.assertIn("error", call("submit", agent="codex", lane=0, prompt="invalid"))
            (root / "release").touch()
            for run_id in extra_runs:
                completed(run_id)
            self.assertEqual(completed(first)["run"]["session"], "thread-one")
            detail = completed(second)
            full = call("detail", id=second, revision="")
            self.assertEqual(full["events"], detail["events"])
            self.assertEqual(call("detail", id=second, revision=full["revision"]), {"unchanged": True})
            self.assertEqual(detail["run"]["session"], "thread-two")
            result = next(event["text"] for event in detail["events"] if event["kind"] == "result")
            self.assertGreater(len(result), 4096)
            self.assertTrue(result.endswith("```"))
            self.assertEqual(call("reset_session", agent="codex", lane=1), {})
            restored = {row["lane"]: row for row in call("workspace_agents")["instances"] if row["agent"] == "codex"}
            self.assertIsNone(restored[1]["session"])
            self.assertIsNone(restored[1]["run"])
            self.assertEqual(restored[1]["name"], "Implementation")
            self.assertEqual(restored[2]["run"]["id"], second)
            self.assertEqual((restored[2]["name"], restored[2]["session"]), ("Review API", "thread-two"))
            self.assertEqual(restored[5]["name"], "Vérification")
            followup = call("submit", agent="codex", lane=2, prompt="continue")["id"]
            self.assertEqual(completed(followup)["run"]["session"], "thread-two")

            # A paused schedule can coexist with live/manual agents in the same tree.
            with sqlite3.connect(state / "activity.sqlite3") as db:
                db.execute(
                    "INSERT INTO schedules(id,agent,repo,prompt,minutes,enabled) VALUES(?,?,?,?,?,?)",
                    ("review", "cursor", str(root.resolve()), "Review changes", 15, 0),
                )
            summaries = call("sidebar_agents", repos=[host["repo"]])["workspaces"][host["repo"]]
            self.assertEqual([row["status"] for row in summaries], ["ready", *["completed"] * 4, "paused"])
            self.assertEqual(summaries[-1]["schedule"], "review")
            self.assertEqual(call("rename_agent", agent="codex", lane=5, name=""), {"lane": 5, "name": None})
            self.assertIn("single-line", call("rename_agent", agent="codex", lane=5, name="two\nlines")["error"])
            self.assertEqual(call("delete_agent", agent="codex", lane=2), {})
            self.assertIn("no longer exists", call("rename_agent", agent="codex", lane=2, name="Deleted")["error"])
            self.assertEqual(call("add_agent", agent="codex")["lane"], 2)
            instances = call("workspace_agents")["instances"]
            self.assertEqual(instances[1], {"agent": "codex", "lane": 2, "name": None, "session": None, "run": None})
