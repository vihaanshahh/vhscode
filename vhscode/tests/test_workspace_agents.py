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
for _ in range(200):
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

            first = call("submit", agent="codex", lane=1, prompt="one")["id"]
            second = call("submit", agent="codex", lane=2, prompt="two")["id"]
            self.assertIn("already running", call("submit", agent="codex", lane=2, prompt="duplicate")["error"])
            listings = call("sidebar_agents", repos=[host["repo"]])["workspaces"][host["repo"]]
            self.assertEqual([(row["lane"], row["status"]) for row in listings], [(1, "running"), (2, "running")])
            self.assertIn("error", call("submit", agent="codex", lane=5, prompt="invalid"))
            (root / "release").touch()
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
            self.assertEqual(restored[2]["run"]["id"], second)
            followup = call("submit", agent="codex", lane=2, prompt="continue")["id"]
            self.assertEqual(completed(followup)["run"]["session"], "thread-two")

            # A paused schedule can coexist with live/manual agents in the same tree.
            db_path = home / "state/vhscode/fleet/activity.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.execute(
                    "INSERT INTO schedules(id,agent,repo,prompt,minutes,enabled) VALUES(?,?,?,?,?,?)",
                    ("review", "cursor", str(root.resolve()), "Review changes", 15, 0),
                )
            summaries = call("sidebar_agents", repos=[host["repo"]])["workspaces"][host["repo"]]
            self.assertEqual([row["status"] for row in summaries], ["ready", "completed", "paused"])
            self.assertEqual(summaries[-1]["schedule"], "review")
