"""Integration checks for the workbench entry point, reporting modes and schedule RPC."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


FLEET = Path(__file__).resolve().parents[1] / "fleet" / "fleet.py"
sys.path.insert(0, str(FLEET.parent))

from fleet_ssh import remote_python


class FleetTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root / "state"),
                        PATH=f"{self.root}:{os.environ['PATH']}")
        (self.root / "fleet-test-python").symlink_to(sys.executable)
        self.agent = self.root / "codex"
        self.agent.write_text(
            "#!/usr/bin/env fleet-test-python\n"
            "import json, sys\n"
            "prompt = sys.stdin.read()\n"
            "print(json.dumps(dict(type='thread.started', thread_id='conversation')))\n"
            "print(json.dumps(dict(type='item.started', item=dict(type='command_execution',command='git status'))))\n"
            "print(json.dumps(dict(type='item.completed', item=dict(type='agent_message',text='Checked the project.'))))\n"
        )
        self.agent.chmod(0o700)

    def invoke(self, args, text):
        return subprocess.run([sys.executable, str(FLEET), *args], input=text, env=self.env,
                              text=True, capture_output=True, check=True, timeout=15)

    def rpc(self, op, **params):
        response = self.invoke(["--rpc"], json.dumps(dict(op=op, **params)))
        reply = json.loads(response.stdout)
        self.assertNotIn("error", reply, reply)
        return reply

    def test_remote_shell_prefers_bundled_python_under_both_app_names(self):
        apps = self.root / "Applications"
        bundle = apps / "Fleet Workspace.app"
        interpreter = Path("Contents/Resources/python/bin/python3")
        python = bundle / interpreter
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nFLEET_FIXTURE_PYTHON="$0" exec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o700)
        command = remote_python(shlex.join(["-c", "import os; print(os.environ['FLEET_FIXTURE_PYTHON'])"]))
        env = dict(self.env, HOME=str(self.root), PATH="/usr/bin:/bin")
        for name in ("Fleet Workspace.app", "Fleet Workbench.app"):
            with self.subTest(name=name):
                bundle = bundle.rename(apps / name) if bundle.name != name else bundle
                result = subprocess.run(["/bin/sh", "-c", command], env=env, text=True,
                                        capture_output=True, check=True, timeout=5)
                self.assertEqual(Path(result.stdout.strip()), bundle / interpreter)

    def test_manual_view_and_one_turn_override(self):
        result = self.invoke([str(self.repo)], "/view summary\nreview\n/actions review again\n/quit\n")
        self.assertEqual(result.stdout.count("Checked the project."), 2, result.stderr)
        self.assertEqual(result.stdout.count("git status"), 1)
        runs = self.rpc("snapshot")["runs"]
        self.assertEqual([(r["view"], r["status"]) for r in runs],
                         [("actions", "completed"), ("summary", "completed")])
        events = self.rpc("detail", id=runs[-1]["id"])["events"]
        self.assertTrue(any(e["kind"] == "action" and "git status" in e["text"] for e in events))
        self.assertIn("Reporting: summary", self.invoke([str(self.repo)], "/view\n/quit\n").stdout)

    def test_risk_view_keeps_breakage_visible_after_a_successful_turn(self):
        self.agent.write_text(
            "#!/usr/bin/env fleet-test-python\n"
            "import json\n"
            "for text in ['The API has a breaking change.', 'Review complete.']:\n"
            "    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))\n"
        )
        result = self.invoke([str(self.repo)], "/risks review API\n/quit\n")
        self.assertIn("breaking change", result.stdout)
        self.assertIn("Review complete.", result.stdout)
        row = self.rpc("snapshot")["runs"][0]
        self.assertEqual((row["title"], row["status"], row["view"]), ("review API", "completed", "risks"))
        self.assertIn("breaking change", row["risk"])
        self.assertIn("Review complete.", row["outcome"])

    def test_schedule_edits_preserve_full_prompt(self):
        params = dict(prompt="Investigate " * 80, minutes=60, agent="cursor", repo=str(self.repo), view="risks")
        self.rpc("create", **params)
        schedule = self.rpc("snapshot")["schedules"][0]
        job = schedule["id"]
        try:
            self.rpc("pause", id=job)
            full = self.rpc("schedule", id=job)["schedule"]
            self.assertEqual(full["prompt"], params["prompt"])
            self.assertFalse(full["enabled"])
            self.rpc("update", id=job, **dict(params, minutes=15, view="summary"))
            self.rpc("resume", id=job)
            edited = self.rpc("snapshot")["schedules"][0]
            self.assertEqual((edited["minutes"], edited["view"], bool(edited["enabled"])), (15, "summary", True))
        finally:
            self.rpc("delete", id=job)
        self.assertEqual(self.rpc("snapshot")["schedules"], [])


if __name__ == "__main__":
    unittest.main()
