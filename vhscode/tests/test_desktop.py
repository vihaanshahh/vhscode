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
arguments = sys.argv[1:]
if name == "agent" and arguments == ["--help"]:
    print("Cursor Agent supports stream-json")
    sys.exit()
assert Path.cwd() == Path(os.environ["FLEET_TEST_REPO"])
if os.environ.get("FLEET_TEST_REQUIRE_PREFIX"):
    assert arguments[:2] == ["--fixture-option", "two words"], arguments
    arguments = arguments[2:]
if name == "agent" and arguments == ["--version"]:
    print("Fixture Cursor")
    sys.exit()
if name == "agent" and arguments == ["--resume=cursor-fixture"]:
    print("Native Cursor resumed")
    sys.exit()


def emit(event_type, **fields):
    print(json.dumps({"type": event_type, **fields}), flush=True)


if name in ("cursor-agent", "agent") and arguments[-2:] == ["--", "Trust fixture"]:
    assert arguments[:3] == ["--print", "--output-format", "stream-json"], arguments
    assert arguments[-3:] == ["agent", "--", "Trust fixture"], arguments
    options = arguments[3:-3]
    assert all(flag in {"--trust", "--resume=cursor-fixture"} for flag in options), arguments
    if "--trust" not in options:
        print("Workspace Trust Required", file=sys.stderr)
        sys.exit(1)
    emit("system", subtype="init", session_id="cursor-fixture")
    emit("result", subtype="success", result="Trusted workspace completed.", session_id="cursor-fixture")
    sys.exit()


if name == "codex" and arguments == ["app-server", "--listen", "stdio://"]:
    if os.environ.get("FLEET_TEST_STALL_CATALOG") == "1":
        Path("catalog.pid").write_text(str(os.getpid()), encoding="ascii")
        time.sleep(30)
    request = json.loads(sys.stdin.readline())
    assert request["method"] == "initialize", request
    print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    assert json.loads(sys.stdin.readline())["method"] == "initialized"
    request = json.loads(sys.stdin.readline())
    if request["method"] in ("thread/list", "thread/read"):
        threads = [
            {"id": "native-codex", "name": "Native Codex title", "cwd": str(Path.cwd()), "updatedAt": 20},
            {"id": "other-workspace", "name": "Other workspace", "cwd": str(Path.cwd().parent), "updatedAt": 30},
        ]
        if request["method"] == "thread/list":
            assert request["params"]["cwd"] == str(Path.cwd()), request
            assert "exec" in request["params"]["sourceKinds"], request
            result = {"data": threads, "nextCursor": None}
        else:
            assert not request["params"]["includeTurns"], request
            result = {"thread": next((item for item in threads if item["id"] == request["params"]["threadId"]), None)}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
        sys.exit()
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

if name in ("cursor-agent", "agent") and arguments == ["--list-models"]:
    if error := os.environ.get("FLEET_TEST_CATALOG_ERROR"):
        print(error, file=sys.stderr)
        sys.exit(1)
    print("Available models\\n\\n\\x1b[36mfixture-cursor\\x1b[0m - Fixture Cursor (default)\\n\\nTip: use --model.")
    sys.exit()

if name in ("cursor-agent", "agent") and arguments == ["acp"]:
    request = json.loads(sys.stdin.readline())
    assert request["method"] == "initialize" and request["jsonrpc"] == "2.0"
    native = os.environ.get("FLEET_TEST_CURSOR_SESSIONS") == "1"
    capabilities = {"sessionCapabilities": {"list": {}}} if native else {}
    print(json.dumps({"id": request["id"], "result": {"agentCapabilities": capabilities}}), flush=True)
    if not native:
        sys.exit()
    request = json.loads(sys.stdin.readline())
    assert request["method"] == "session/list" and request["params"]["cwd"] == str(Path.cwd())
    sessions = [
        {"sessionId": "native-cursor", "title": "Native Cursor title", "cwd": str(Path.cwd()),
         "updatedAt": "2025-10-29T14:22:15Z"},
        {"sessionId": "other-cursor", "cwd": str(Path.cwd().parent)},
    ]
    print(json.dumps({"id": request["id"], "result": {"sessions": sessions}}), flush=True)
    sys.exit()

if name == "codex":
    session = "codex-fixture"
    resumed = arguments == ["exec", "resume", "--json", "--model=fixture-codex", "--", session, "-"]
    assert resumed or arguments == ["exec", "--json", "-"], arguments
    prompt = sys.stdin.read()
else:
    assert name in ("cursor-agent", "agent"), name
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
    assert prompt in ("logout", "Markdown fixture"), prompt

if prompt == "Markdown fixture":
    opening = "Checking **bold** and `inline`."
    final = "# Result\\n\\n- Finished with `literal()`\\n\\n```text\\n**this stays literal**\\n```"
    if name == "codex":
        emit("thread.started", thread_id=session)
        emit("item.updated", item={"type": "agent_message", "id": "progress", "text": opening, "phase": "commentary"})
        emit("item.completed", item={"type": "agent_message", "id": "progress"})
        emit("item.completed", item={"type": "todo_list", "items": [{"text": "Do **work**"}]})
        emit("item.completed", item={"type": "command_execution", "status": "completed", "exit_code": 0,
                                     "aggregated_output": "**terminal text**"})
        emit("item.completed", item={"type": "agent_message", "id": "final", "text": final, "phase": "final_answer"})
        emit("item.completed", item={"type": "mcp_tool_call", "tool": "report", "status": "completed"})
        emit("turn.completed")
    else:
        def assistant(text, **metadata):
            emit("assistant", message={"content": [{"type": "text", "text": text}]}, **metadata)
        emit("system", subtype="init", session_id=session)
        assistant(opening[:11], timestamp_ms=1)
        assistant(opening[11:], timestamp_ms=2)
        assistant(opening, timestamp_ms=3, model_call_id="duplicate")
        emit("tool_call", subtype="started", tool_call={"updateTodosToolCall": {"args": {"todos": [{"content": "Do **work**"}]}}})
        assistant(final[:31], timestamp_ms=4)
        assistant(final[31:], timestamp_ms=5)
        assistant(final)
        emit("result", subtype="success", result=opening + final, session_id=session)
    sys.exit()

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

    def test_local_read_stream_keeps_replies_separate_and_recovers_from_request_errors(self):
        note = "Tabs\tand newlines\n" * 6000
        self.guide.write_text(note)
        with subprocess.Popen([sys.executable, "-B", BRIDGE, "--read-stream"], env=self.environment,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as reader:
            try:
                def request(fields):
                    reader.stdin.write(json.dumps({"host": self.host, "request": fields}).encode() + b"\n")
                    reader.stdin.flush()
                    length = int.from_bytes(reader.stdout.read(4), "big")
                    return json.loads(reader.stdout.read(length))

                self.assertIn("error", request({"op": "invalid operation"}))
                reply = request({"op": "read_file", "path": self.guide.relative_to(self.repo).as_posix()})
                self.assertEqual(reply["text"], note)
                self.assertIn("entries", request({"op": "files"}))
            finally:
                reader.stdin.close()
                reader.wait(timeout=5)

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

    def finished_run(self, agent, *, prompt, tags=(), expected_status="completed"):
        run_id = self.success(self.request(
            "submit", agent=agent, prompt=prompt, tags=list(tags), view="actions"
        ))["id"]
        deadline = time.monotonic() + 8
        while True:
            detail = self.success(self.request("detail", id=run_id))
            row = detail["run"]
            if row is not None and row["status"] != "running":
                self.assertEqual(row["status"], expected_status, detail)
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

    def test_markdown_survives_both_adapter_transcripts_without_duplicate_deltas(self):
        for agent in ("codex", "cursor"):
            with self.subTest(agent=agent):
                row, events = self.finished_run(agent, prompt="Markdown fixture")
                opening = "Checking **bold** and `inline`."
                final = "# Result\n\n- Finished with `literal()`\n\n```text\n**this stays literal**\n```"
                self.assertEqual(row["outcome"], final)
                self.assertEqual([event["text"] for event in events if event["kind"] == "result"], [final])
                self.assertEqual([event["text"] for event in events if event["kind"] == "plan"], ["### Plan\n\n- Do **work**"])
                self.assertEqual([event["text"] for event in events if event["kind"] == "info"], [opening])
                if agent == "codex":
                    self.assertIn("**terminal text**", [event["text"] for event in events if event["kind"] == "log"])

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

    def test_user_bin_cli_and_child_interpreter_work_without_inherited_path_entries(self):
        home = Path(self.environment["HOME"])
        fixture_bin = self.root / "fixture bin"
        npm_bin = home / ".npm-global/bin"
        cursor_bin = home / ".local/bin"
        node_bin = home / ".nvm/versions/node/v22.2.0/bin"
        for directory in (npm_bin, cursor_bin, node_bin):
            directory.mkdir(parents=True)
        (fixture_bin / "codex").rename(npm_bin / "codex")
        (fixture_bin / "cursor-agent").rename(cursor_bin / "agent")
        (fixture_bin / "fleet-test-python").rename(node_bin / "fleet-test-python")
        wrong_alias = fixture_bin / "agent"
        wrong_alias.write_text("#!/bin/sh\necho 'Unrelated agent'\n", encoding="utf-8")
        wrong_alias.chmod(0o700)

        for agent in ("codex", "cursor"):
            with self.subTest(agent=agent):
                models = self.success(self.request("models", agent=agent))["models"]
                self.assertEqual(models[0]["id"], f"fixture-{agent}")
                row, _ = self.finished_run(agent, prompt="logout")
                self.assertEqual(row["outcome"], f"{agent}-fixture initial completed.")

    def test_workspace_command_overrides_runs_catalog_and_native_resume(self):
        fixture_bin = self.root / "fixture bin"
        agent = fixture_bin / "agent"
        (fixture_bin / "cursor-agent").rename(agent)
        (fixture_bin / "cursor-agent").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (fixture_bin / "cursor-agent").chmod(0o700)
        command = f'"{agent}" --fixture-option "two words"'
        self.environment["FLEET_TEST_REQUIRE_PREFIX"] = "1"
        self.assertEqual(self.success(self.request("test_cli_command", agent="cursor", command=command)),
                         {"executable": str(agent)})
        self.assertEqual(self.success(self.request("set_cli_command", agent="cursor", command=command)),
                         {"command": command})
        self.assertIn("quotes", self.request("set_cli_command", agent="cursor", command="'")["error"])
        self.assertEqual(self.success(self.request("cli_command", agent="cursor")), {"command": command})
        self.assertEqual(self.success(self.request("cli_command", agent="codex")), {"command": ""})
        self.assertEqual(self.success(self.request("models", agent="cursor"))["models"][0]["id"], "fixture-cursor")
        row, _ = self.finished_run("cursor", prompt="logout")
        self.assertEqual(row["outcome"], "cursor-fixture initial completed.")
        native = subprocess.run(
            [sys.executable, str(BRIDGE.with_name("fleet.py")), "--native", "--agent", "cursor",
             "--session=cursor-fixture", str(self.repo)], capture_output=True, text=True,
            check=True, timeout=10, env=self.environment,
        )
        self.assertEqual(native.stdout.strip(), "Native Cursor resumed")
        self.success(self.request("set_cli_command", agent="cursor", command=""))
        self.assertIn("returned status 99", self.request("test_cli_command", agent="cursor", command="")["error"])

    def test_cursor_catalog_reports_host_authentication_and_network_errors(self):
        for error, expected in (
            ("Error: Authentication required. Run 'agent login'.", "Sign in to Cursor on this host"),
            ("Error: fetch failed (ENETUNREACH)", "could not reach its model catalog from this host"),
        ):
            with self.subTest(error=error):
                self.environment["FLEET_TEST_CATALOG_ERROR"] = error
                self.assertIn(expected, self.request("models", agent="cursor")["error"])

    def test_resume_native_codex_session_checks_workspace_and_lane(self):
        self.assertEqual(self.success(self.request("list_sessions", agent="codex"))["sessions"], [{
            "id": "native-codex", "title": "Native Codex title", "updated_at": 20, "source": "codex",
        }])
        foreign = self.request("resume_session", agent="codex", session="other-workspace", lane=2)
        self.assertIn("this agent and workspace", foreign["error"])
        self.assertEqual(self.success(self.request("resume_session", agent="codex", session="native-codex", lane=2)),
                         {"session": "native-codex"})
        duplicate = self.request("resume_session", agent="codex", session="native-codex", lane=1)
        self.assertIn("already assigned", duplicate["error"])
        cross_agent = self.request("resume_session", agent="cursor", session="native-codex", lane=2)
        self.assertIn("this agent and workspace", cross_agent["error"])

    def test_cursor_resume_lists_successful_fleet_sessions_after_reset(self):
        self.finished_run("cursor", prompt="logout")
        self.success(self.request("reset_session", agent="cursor"))
        sessions = self.success(self.request("list_sessions", agent="cursor"))["sessions"]
        self.assertEqual([(row["id"], row["title"], row["source"]) for row in sessions],
                         [("cursor-fixture", "logout", "fleet")])
        resume = self.success(self.request("resume_session", agent="cursor", session="cursor-fixture", lane=2))
        self.assertEqual(resume, {"session": "cursor-fixture"})

    def test_cursor_native_acp_sessions_are_filtered_by_workspace(self):
        self.environment["FLEET_TEST_CURSOR_SESSIONS"] = "1"
        sessions = self.success(self.request("list_sessions", agent="cursor"))["sessions"]
        self.assertEqual([(row["id"], row["title"], row["source"]) for row in sessions],
                         [("native-cursor", "Native Cursor title", "cursor")])
        self.assertGreater(sessions[0]["updated_at"], 0)
        wrong_workspace = self.request("resume_session", agent="cursor", session="other-cursor", lane=2)
        self.assertIn("this agent and workspace", wrong_workspace["error"])
        self.assertEqual(self.success(self.request("resume_session", agent="cursor", session="native-cursor", lane=2)),
                         {"session": "native-cursor"})

    def test_cursor_trust_is_confirmed_per_canonical_workspace_and_revocable(self):
        self.assertEqual(self.success(self.request("cursor_trust_status")), {"trusted": False})
        self.assertIn("error", self.request("set_cursor_trust", trusted="true"))
        _, events = self.finished_run("cursor", prompt="Trust fixture", expected_status="failed")
        self.assertTrue(any("Workspace Trust Required" in event["text"] for event in events), events)

        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.success(self.bridge({
            "host": {**self.host, "repo": str(alias)}, "request": {"op": "set_cursor_trust", "trusted": True},
        })), {"trusted": True})
        settings = self.success(self.request("workspace_settings", include_cursor_trust=True))
        self.assertIs(settings["cursor"]["trusted"], True)
        self.assertNotIn("trusted", settings["codex"])
        self.assertEqual(self.success(self.request("cursor_trust_status")), {"trusted": True})
        self.finished_run("cursor", prompt="Trust fixture")
        self.finished_run("codex", prompt="logout")

        other = self.root / "other"
        other.mkdir()
        self.assertEqual(self.success(self.bridge({
            "host": {**self.host, "repo": str(other)}, "request": {"op": "cursor_trust_status"},
        })), {"trusted": False})
        self.assertEqual(self.success(self.request("set_cursor_trust", trusted=False)), {"trusted": False})
        self.finished_run("cursor", prompt="Trust fixture", expected_status="failed")

    def test_resume_blocks_active_lanes_and_rejects_stale_generation(self):
        check = """
from pathlib import Path
import sys
from fleet_jobs import begin_run, end_run
from fleet_models import get, remember, workspace

repo = Path(sys.argv[1])
assert remember(repo, "cursor", "old", 0, 2)
request = {"op": "resume_session", "agent": "cursor", "session": "old", "lane": 2}
run = begin_run(repo, "cursor", "active", detached=True, session="old", known_session=True, lane=2)
try:
    workspace(repo, request)
except ValueError as error:
    assert "active task" in str(error), error
else:
    raise AssertionError("Resume changed an active lane")
finally:
    end_run(run, "completed", session="old")
assert workspace(repo, request) == {"session": "old"}
assert not remember(repo, "cursor", "late", 0, 2)
assert not remember(repo, "cursor", "old", 0)
assert get(repo, "cursor", 2)["session"] == "old"
try:
    begin_run(repo, "cursor", "stale", detached=True, session="old", known_session=True, lane=2)
except ValueError as error:
    assert "session changed" in str(error), error
else:
    raise AssertionError("Stale generation admitted")
"""
        subprocess.run([sys.executable, "-E", "-s", "-B", "-c", check, str(self.repo)],
                       cwd=BRIDGE.parent, env=self.environment, check=True, timeout=10)

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
