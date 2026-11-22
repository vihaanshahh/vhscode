"""Exercise the desktop JSON bridge without accounts, network, or installed agents."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


BRIDGE = Path(__file__).resolve().parents[1] / "fleet" / "fleet_desktop.py"

AGENT_FIXTURE = """#!/usr/bin/env fleet-test-python
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
assert Path.cwd() == Path(os.environ["FLEET_TEST_REPO"])
arguments = sys.argv[1:]


def emit(event_type, **fields):
    print(json.dumps({"type": event_type, **fields}), flush=True)


if name == "codex" and arguments == ["app-server", "--listen", "stdio://"]:
    if os.environ.get("FLEET_TEST_STALL_CATALOG") == "1":
        Path("catalog.pid").write_text(str(os.getpid()), encoding="ascii")
        time.sleep(30)
    request = json.loads(sys.stdin.readline())
    assert request["method"] == "initialize", request
    print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    assert json.loads(sys.stdin.readline())["method"] == "initialized"
    request = json.loads(sys.stdin.readline())
    assert request["method"] == "model/list" and not request["params"]["includeHidden"], request
    print(json.dumps({"id": request["id"], "result": {"data": [
        {"id": "fixture-alias", "model": "fixture-codex", "displayName": "Fixture Codex"},
        {"model": "hidden-fixture", "displayName": "Hidden", "hidden": True},
    ], "nextCursor": "fixture-page-two"}}), flush=True)
    request = json.loads(sys.stdin.readline())
    assert request["params"]["cursor"] == "fixture-page-two", request
    print(json.dumps({"id": request["id"], "result": {"data": [
        {"id": "fixture-alias-two", "model": "fixture-codex-two", "displayName": "Second Codex"},
    ], "nextCursor": None}}), flush=True)
    sys.exit()

if name == "cursor-agent" and arguments == ["--list-models"]:
    print("Available models\\n\\n\\x1b[36mfixture-cursor\\x1b[0m - Fixture Cursor (default)\\n\\nTip: use --model.")
    sys.exit()

if name == "codex":
    session = "codex-fixture"
    resumed = arguments == ["exec", "resume", "--json", "--model=fixture-codex", "--", session, "-"]
    assert resumed or arguments == ["exec", "--json", "-"], arguments
    prompt = sys.stdin.read()
else:
    assert name == "cursor-agent", name
    session = "cursor-fixture"
    options = ["--print", "--output-format", "stream-json"]
    follow_up = ["--model=fixture-cursor", "--resume=" + session]
    resumed = arguments[:5] == options + follow_up
    prefix = options + (follow_up if resumed else [])
    assert arguments[:-1] == prefix + ["agent", "--"], arguments
    prompt = arguments[-1]

if resumed:
    expected = "Follow up\\n\\nRepository-relative context paths:\\n- "
    assert prompt == expected + json.dumps("Sources and notes/Architecture Guide.md"), prompt
else:
    assert prompt == "logout", prompt

outcome = f"{session} {'follow-up' if resumed else 'initial'} completed."
if name == "codex":
    emit("thread.started", thread_id=session)
    emit("item.started", item={"type": "command_execution", "command": "git status"})
    emit("item.completed", item={"type": "command_execution", "status": "completed", "exit_code": 0})
    emit("item.completed", item={"type": "agent_message", "text": outcome})
    emit("turn.completed")
else:
    emit("system", subtype="init", session_id=session)
    emit("tool_call", subtype="started",
         tool_call={"readToolCall": {"args": {"path": "Architecture Guide.md"}}})
    emit("tool_call", subtype="completed", tool_call={"readToolCall": {"result": {"success": {}}}})
    emit("assistant", message={"content": [{"type": "text", "text": outcome}]})
    emit("result", subtype="success", result=outcome, session_id=session)
"""


class DesktopBridgeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.notes = self.repo / "Sources and notes"
        self.notes.mkdir(parents=True)
        self.guide = self.notes / "Architecture Guide.md"
        self.guide.write_text("Fleet guide\n", encoding="utf-8")
        self.guide.chmod(0o640)
        self.host = {"id": "local", "label": "Local", "ssh": "", "repo": str(self.repo)}
        home = self.root / "home"
        home.mkdir()
        fixture_bin = self.root / "fixture bin"
        fixture_bin.mkdir()
        self.environment = {
            "HOME": str(home),
            "PATH": f"{fixture_bin}:/usr/bin:/bin",
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "FLEET_TEST_REPO": str(self.repo),
        }
        (fixture_bin / "fleet-test-python").symlink_to(sys.executable)
        for name in ("codex", "cursor-agent"):
            executable = fixture_bin / name
            executable.write_text(AGENT_FIXTURE, encoding="utf-8")
            executable.chmod(0o700)
        ssh = fixture_bin / "ssh"
        ssh.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        ssh.chmod(0o700)

    def bridge(self, message):
        result = subprocess.run(
            [sys.executable, "-E", "-s", "-B", str(BRIDGE)],
            input=json.dumps(message),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
            cwd=self.root,
            env=self.environment,
        )
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def request(self, operation, **parameters):
        return self.bridge({"host": self.host, "request": {"op": operation, **parameters}})

    def success(self, reply):
        self.assertNotIn("error", reply, reply)
        return reply

    def test_import_local_folder_and_code_workspace(self):
        utilities = self.root / "utilities repo"
        utilities.mkdir()
        manifest = self.root / "Product Group.code-workspace"
        manifest.write_text(
            json.dumps({"folders": [{"path": self.repo.name}, {"path": utilities.name, "name": "Internal Tools"}]}),
            encoding="utf-8",
        )

        reply = self.success(self.bridge({"op": "import_workspaces", "paths": [str(manifest)]}))
        imported = {host["repo"]: host for host in reply["hosts"] if host["repo"] in {str(self.repo), str(utilities)}}
        self.assertEqual(set(imported), {str(self.repo), str(utilities)})
        self.assertTrue(all(host["ssh"] == "" and host["group"] == "Product Group" for host in imported.values()))
        self.assertEqual(imported[str(utilities)]["label"], "Internal Tools")
        self.assertIn(reply["selected"], {host["id"] for host in imported.values()})

        individual = self.root / "another local folder"
        individual.mkdir()
        reply = self.success(self.bridge({"op": "import_workspaces", "paths": [str(individual)]}))
        saved = self.success(self.bridge({"op": "hosts"}))["hosts"]
        selection = next(host for host in saved if host["id"] == reply["selected"])
        self.assertEqual((selection["repo"], selection["group"], selection["ssh"]), (str(individual), "", ""))

    def test_file_read_save_and_conflict_in_nested_folder(self):
        relative = self.guide.relative_to(self.repo).as_posix()
        page = self.success(self.request("files", path=self.notes.name))
        self.assertIn({"name": self.guide.name, "path": relative, "kind": "file"}, page["entries"])

        initial = self.success(self.request("read_file", path=relative))
        self.assertEqual(initial["text"], "Fleet guide\n")
        saved = self.success(self.request("save_file", path=relative, revision=initial["revision"], text="Revised café\n"))
        self.assertNotEqual(initial["revision"], saved["revision"])
        self.assertEqual(self.guide.read_text(encoding="utf-8"), "Revised café\n")
        self.assertEqual(self.guide.stat().st_mode & 0o777, 0o640)

        self.guide.write_text("External edit\n", encoding="utf-8")
        conflict = self.success(self.request("save_file", path=relative, revision=saved["revision"], text="Stale save\n"))
        self.assertIs(conflict["conflict"], True)
        self.assertEqual(self.guide.read_text(encoding="utf-8"), "External edit\n")
        fresh = self.success(self.request("read_file", path=relative))
        self.assertEqual(fresh["revision"], conflict["revision"])

        outside = self.root / "outside.txt"
        outside.write_text("Outside the repository", encoding="utf-8")
        (self.notes / "link.txt").symlink_to(outside)
        self.assertIn("error", self.request("read_file", path=f"{self.notes.name}/link.txt"))
        self.assertIn("error", self.request("read_file", path="../outside.txt"))

    def test_quick_find_returns_repository_relative_paths(self):
        hidden = self.repo / ".git"
        hidden.mkdir()
        (hidden / "Architecture Guide.md").write_text("Git metadata", encoding="utf-8")
        (self.repo / "Architecture Guide alias.md").symlink_to(self.guide)
        reply = self.success(self.request("find_files", query="architecture", limit=10))

        self.assertEqual(reply["files"], [self.guide.relative_to(self.repo).as_posix()])
        self.assertIs(reply["more"], False)

    def finished_run(self, agent, *, prompt, tags=()):
        run_id = self.success(self.request(
            "submit", agent=agent, prompt=prompt, tags=list(tags), view="actions"
        ))["id"]
        deadline = time.monotonic() + 8
        while True:
            detail = self.success(self.request("detail", id=run_id))
            row = detail["run"]
            if row is not None and row["status"] != "running":
                self.assertEqual(row["status"], "completed", detail)
                return row, detail["events"]
            if time.monotonic() >= deadline:
                self.fail(f"Desktop run {run_id} did not finish: {detail!r}")
            time.sleep(0.05)

    def test_desktop_submits_and_resumes_both_cli_adapters(self):
        for agent, marker in (("codex", "git status"), ("cursor", "Architecture Guide.md")):
            with self.subTest(agent=agent):
                first, events = self.finished_run(agent, prompt="logout")
                session = agent + "-fixture"
                self.assertEqual((first["agent"], first["repo"], first["session"]), (agent, str(self.repo), session))
                self.assertEqual(first["outcome"], f"{session} initial completed.")
                self.assertTrue(any(event["kind"] == "action" and marker in event["text"] for event in events), events)
                self.assertEqual(self.success(self.request("workspace_settings"))[agent]["session"], session)

                self.success(self.request("set_model", agent=agent, model=f"fixture-{agent}"))
                follow_up, _ = self.finished_run(
                    agent, prompt="Follow up", tags=[self.guide.relative_to(self.repo).as_posix()]
                )
                self.assertNotEqual(first["id"], follow_up["id"])
                self.assertEqual(follow_up["session"], session)
                self.assertEqual(follow_up["outcome"], f"{session} follow-up completed.")

    def test_model_catalogs_and_workspace_settings(self):
        codex = self.success(self.request("models", agent="codex"))
        self.assertEqual(codex["models"], [
            {"id": "fixture-codex", "label": "Fixture Codex"},
            {"id": "fixture-codex-two", "label": "Second Codex"},
        ])
        cursor = self.success(self.request("models", agent="cursor"))
        self.assertEqual(cursor["models"], [{"id": "fixture-cursor", "label": "Fixture Cursor"}])

        empty = {"model": None, "session": None}
        self.assertEqual(self.success(self.request("workspace_settings")), {"codex": empty, "cursor": empty})
        self.success(self.request("set_model", agent="codex", model="fixture-codex"))
        settings = self.success(self.request("workspace_settings"))
        self.assertEqual(settings, {"codex": {"model": "fixture-codex", "session": None}, "cursor": empty})
        self.success(self.request("set_model", agent="codex", model=None))
        self.assertEqual(self.success(self.request("workspace_settings"))["codex"], empty)

    def test_reset_keeps_finishing_old_session_from_returning(self):
        check = """
from fleet_models import get, remember, workspace
import sys
repo = sys.argv[1]
state = get(repo, "codex")
workspace(repo, {"op": "reset_session", "agent": "codex"})
assert not remember(repo, "codex", "old-session", state["generation"])
fresh = get(repo, "codex")
assert fresh["session"] is None
assert remember(repo, "codex", "new-session", fresh["generation"])
assert get(repo, "codex")["session"] == "new-session"
"""
        subprocess.run([sys.executable, "-E", "-s", "-B", "-c", check, str(self.repo)],
                       cwd=BRIDGE.parent, env=self.environment, check=True, timeout=10)

    def test_model_catalog_timeout_reaps_cli_process(self):
        check = """
import fleet_models
import os
from pathlib import Path
import sys
repo = Path(sys.argv[1])
fleet_models.CATALOG_TIMEOUT = 1
try:
    fleet_models.workspace(repo, {"op": "models", "agent": "codex"})
except ValueError as error:
    assert "did not respond in time" in str(error), error
else:
    raise AssertionError("The model catalog request did not time out")
pid = int((repo / "catalog.pid").read_text(encoding="ascii"))
try:
    os.kill(pid, 0)
except ProcessLookupError:
    pass
else:
    raise AssertionError("The timed-out model catalog process is still running")
"""
        subprocess.run([sys.executable, "-E", "-s", "-B", "-c", check, str(self.repo)],
                       cwd=BRIDGE.parent, env=dict(self.environment, FLEET_TEST_STALL_CATALOG="1"),
                       check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
