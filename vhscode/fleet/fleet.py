#!/usr/bin/env python3
"""VHSCode terminal workbench. This file also runs directly on an SSH target."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import signal

HELP = """/files                 Browse, edit, or tag repository files
/tag PATH              Add a file to the next prompt and subsequent prompts
/untag PATH            Remove a tag, or use /untag-all
/tags                  List tagged files
/diff                  View unstaged changes
/status                View Git status
/agent codex|cursor    Switch harness (each keeps its own session)
/dashboard             See activity and manage host schedules
/view MODE             Save actions, summary or risks as this repo's default
/summary PROMPT        Use summary reporting for one turn (/risks and /actions also work)
/details               Toggle detailed agent output
/native                Open the harness TUI for interactive commands and approvals
/new                   Start a new conversation with the selected harness
/filter                Toggle local filtering of transient progress
/filter-setup          Download the optional classifier on this machine
/quit                  Exit the workbench
Type a prompt to send it to the selected agent. Ctrl-C interrupts a turn."""


def work(root, agent, quiet, view):
    from fleet_hosts import VIEWS, reporting
    view = view or reporting(root)
    try:
        import readline
        readline.set_auto_history(False)
    except ImportError:
        readline = None
    detailed = False
    sessions = {}
    tags = set()
    print(f"VHSCode fleet   {root}\n/files to browse and edit   /help for commands")
    while True:
        try:
            line = input(f"{agent} [{len(tags)} tagged]> ").strip()
            if not line:
                continue
            if readline:
                if readline.get_current_history_length() >= 100:
                    readline.remove_history_item(0)
                readline.add_history(line)
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
            override = cmd.startswith("/") and cmd[1:] in VIEWS and bool(arg)
            if override or not cmd.startswith("/"):
                turn_view = cmd[1:] if override else view
                prompt = arg if override else line
                from fleet_harness import run
                from fleet_jobs import begin_run, end_run, event
                from fleet_models import get
                run_id = begin_run(root, agent, prompt, view=turn_view)
                status = "failed"

                def record(kind, text):
                    nonlocal status
                    if kind == "status":
                        status = text
                    else:
                        event(run_id, kind, text)

                try:
                    sessions[agent] = run(root, agent, prompt, tags, sessions.get(agent), quiet,
                                          on_event=record, detailed=detailed and not override, view=turn_view,
                                          model=get(root, agent)["model"])
                finally:
                    end_run(run_id, status, session=sessions.get(agent) if status == "completed" else None)
                continue
            if cmd in ("/quit", "/exit"):
                return 0
            if cmd == "/help":
                print(HELP)
            elif cmd == "/dashboard":
                from fleet_dashboard import dashboard
                dashboard()
            elif cmd == "/view":
                if arg:
                    view = reporting(root, arg)
                    detailed = False
                print(f"Reporting: {view}. Available: actions, summary, risks.")
            elif cmd == "/details":
                detailed = not detailed
                print("Detailed output", "enabled" if detailed else "disabled")
            elif cmd == "/files":
                if sys.stdin.isatty() and sys.stdout.isatty():
                    from fleet_editor import browse
                    browse(root, tags)
                else:
                    print("The file browser needs an interactive terminal.")
            elif cmd == "/agent" and arg in ("codex", "cursor"):
                agent = arg
            elif cmd == "/native":
                from fleet_harness import native
                native(root, agent, sessions.get(agent))
            elif cmd == "/new":
                sessions.pop(agent, None)
            elif cmd == "/tags":
                for name in sorted(tags):
                    print(repr(name))
            elif cmd == "/tag" and arg:
                path = (root / arg).resolve(strict=True)
                if path.is_file() and path.is_relative_to(root):
                    tags.add(path.relative_to(root).as_posix())
                else:
                    print("Choose a file inside the repository.")
            elif cmd == "/untag":
                tags.discard(arg)
            elif cmd == "/untag-all":
                tags.clear()
            elif cmd in ("/status", "/diff"):
                subprocess.run(["git", "status", "--short"] if cmd == "/status"
                               else ["git", "diff", "--no-ext-diff"], cwd=root)
            elif cmd == "/filter":
                quiet = not quiet
                print("Progress filtering", "enabled" if quiet else "disabled")
            elif cmd == "/filter-setup":
                subprocess.run([sys.executable, str(Path(__file__).with_name("fleet_filter.py")), "setup"])
            else:
                print("Unknown command or argument. Type /help.")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
        except (OSError, ValueError, RuntimeError) as error:
            print(error, file=sys.stderr)


def main():
    if sys.version_info < (3, 10):
        sys.exit("vhscode fleet requires Python 3.10 or later")
    parser = argparse.ArgumentParser(description="Edit a repository and use its native coding agents.")
    parser.add_argument("repo", default=".", nargs="?", help="repository directory on the target machine")
    parser.add_argument("--ssh", metavar="HOST", help="OpenSSH destination or configured host alias")
    parser.add_argument("--view", choices=["actions", "summary", "risks"], help="override the repository reporting setting")
    parser.add_argument("--agent", choices=["codex", "cursor"], default="codex")
    parser.add_argument("--filter", action="store_true", help="enable optional local progress classifier")
    parser.add_argument("--persist", action="store_true", help="create or attach a tmux workbench for this repo")
    parser.add_argument("--dashboard", action="store_true", help="view fleet activity and manage schedules")
    parser.add_argument("--rpc", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rpc:
        from fleet_desktop import serve
        from fleet_jobs import rpc

        serve(rpc)
        return 0
    if args.dashboard and args.ssh:
        from fleet_hosts import remember
        from fleet_ssh import destination
        remember(destination(args.ssh), args.repo)
    if args.dashboard or len(sys.argv) == 1:
        from fleet_dashboard import dashboard
        return dashboard()
    if args.ssh:
        from fleet_ssh import connect
        return connect(args)
    root = Path(args.repo).expanduser().resolve(strict=True)
    if not root.is_dir():
        parser.error("repository path must be a directory")
    if args.persist:
        if not shutil.which("tmux"):
            parser.error("--persist requires tmux on the target")
        name = "vhscode-" + hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
        command = [sys.executable, str(Path(__file__).resolve()), "--agent", args.agent]
        if args.filter:
            command.append("--filter")
        if args.view:
            command.extend(["--view", args.view])
        command.extend(["--", str(root)])
        os.execlp("tmux", "tmux", "new-session", "-A", "-s", name, "-c", str(root), shlex.join(command))
    # Unwind the active turn so its harness process group is reaped after PTY loss.
    def hangup(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, hangup)
    return work(root, args.agent, args.filter, args.view)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit(f"vhscode fleet: {error}")
    except KeyboardInterrupt:
        sys.exit(130)
