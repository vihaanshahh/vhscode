"""Run Cursor's browser login on the workspace host and open its link on the Mac."""
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import shlex
import signal
import subprocess
import sys
from time import monotonic
from urllib.parse import urlsplit


LOGIN_TIMEOUT = 240
URL = re.compile(rb"https://[^\s\x00-\x1f<>\"']+")
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def cursor_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in {"cursor.com", "www.cursor.com", "cursor.sh", "www.cursor.sh"}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and parsed.path != "/" and bool(parsed.query) and len(value) < 8192
                and all(ord(char) > 32 for char in value))
    except (ValueError, TypeError):
        return False


def _lines(args, env, callback, *, cwd=None):
    process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    deadline = monotonic() + LOGIN_TIMEOUT
    pending = b""
    try:
        with DefaultSelector() as selector:
            selector.register(process.stdout, EVENT_READ)
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("Cursor sign-in timed out. Choose Sign In to Cursor to try again.")
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    if pending:
                        callback(pending)
                    break
                fragments = (pending + chunk).replace(b"\r", b"\n").split(b"\n")
                pending = fragments.pop()[-16384:]
                for line in fragments:
                    callback(line)
        return process.wait(timeout=max(1, deadline-monotonic()))
    finally:
        process.stdout.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()
                    process.wait()


def _on_host(repo):
    from fleet_harness import _require_harness
    from fleet_hosts import workspace_root
    from fleet_jobs import _try_lock
    from fleet_models import _models

    lock = _try_lock("cursor-login.lock")
    if lock is None:
        raise ValueError("A Cursor sign-in is already in progress on this machine.")
    try:
        root = workspace_root(repo)
        command, env = _require_harness(root, "cursor")
        sent = False

        def forward(line):
            nonlocal sent
            if sent:
                return
            for match in URL.finditer(ANSI.sub(b"", line)):
                url = match.group().decode("ascii", errors="ignore")
                if cursor_url(url):
                    print(json.dumps({"url": url}), flush=True)
                    sent = True
                    break

        if _lines([*command, "login"], dict(env, NO_OPEN_BROWSER="1"), forward, cwd=root):
            raise ValueError("Cursor did not complete sign-in. Try again and approve the connection in your browser.")
        if not _models(root, "cursor"):
            raise ValueError("Cursor signed in but returned no available models.")
        print(json.dumps({"verified": True}), flush=True)
    finally:
        os.close(lock)


def login(host, repo):
    from fleet_ssh import bundle, destination, install, ssh_options

    if host:
        target = destination(host)
        ssh = ssh_options(target, True)
        if install(target, ssh, True).returncode:
            raise ValueError("Could not connect to the workspace. Use Test SSH in workspace settings.")
        digest = bundle()[1]
        script = f'"$HOME"/.cache/vhscode/fleet/{digest}/fleet_cursor_login.py'
        args = [*ssh, "-T", "--", target, f"exec python3 -B {script} --on-host {shlex.quote(repo)}"]
    else:
        args = [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()), "--on-host", repo]
    verified = opened = False
    issue = None

    def handle(line):
        nonlocal verified, opened, issue
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        if not opened and isinstance(message.get("url"), str) and cursor_url(message["url"]):
            if subprocess.run(["/usr/bin/open", message["url"]], capture_output=True).returncode:
                raise ValueError("The Mac could not open the Cursor sign-in page.")
            opened = True
        elif message.get("verified") is True:
            verified = True
        elif isinstance(message.get("error"), str):
            # Only pass errors from our worker, never unfiltered CLI output.
            issue = message["error"][:320]

    if _lines(args, dict(os.environ), handle) or not verified:
        raise ValueError(issue or "Cursor sign-in did not finish. Check the workspace connection, then try again.")
    return {"verified": True}


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--on-host":
    try:
        _on_host(sys.argv[2])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # A TimeoutExpired can include argv. Keep OS/CLI diagnostics out of the login protocol.
        message = str(error) if isinstance(error, ValueError) else "Could not complete Cursor sign-in on this machine."
        print(json.dumps({"error": message}), flush=True)
        sys.exit(1)
