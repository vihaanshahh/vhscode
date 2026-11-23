"""Local terminal dashboard for Fleet hosts, runs, and schedules."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
from uuid import uuid4

try:
    import curses
except ImportError:
    curses = None

from fleet_hosts import hosts, open_host, remove_host, request, save_host


POLL_INTERVAL = 8
WORKERS = 4
RECENT_COMPLETIONS = 50
REQUEST_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError)
REPORTING = (("actions", "Actions"), ("summary", "Summary"), ("risks", "Risks"))
REPORTING_KEYS = tuple(key for key, _ in REPORTING)
TABS = ("activity", "hosts", "schedules", "risks")


def dashboard() -> int:
    if curses is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("The Fleet dashboard needs an interactive terminal with Python curses.", file=sys.stderr)
        return 1
    try:
        return curses.wrapper(lambda screen: Dashboard(screen, hosts()).run())
    except KeyboardInterrupt:
        return 130
    except (curses.error, *REQUEST_ERRORS) as error:
        print(f"Could not open the Fleet dashboard. {_display(error)}", file=sys.stderr)
        return 1


class Background:
    """Keep transport work off the terminal thread with bounded concurrency."""

    def __init__(self):
        self.jobs = queue.Queue(maxsize=WORKERS)
        self.results = queue.SimpleQueue()
        self.pending = set()
        self.stop = threading.Event()
        for _ in range(WORKERS):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, key, revision, host, payload):
        if key in self.pending:
            return False
        try:
            self.jobs.put_nowait((key, revision, host, payload))
        except queue.Full:
            return False
        self.pending.add(key)
        return True

    def _worker(self):
        while not self.stop.is_set():
            try:
                key, revision, host, payload = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.stop.is_set():
                return
            try:
                result = request(host, payload)
                self.results.put((key, revision, result, None))
            except Exception as error:
                self.results.put((key, revision, None, str(error) or type(error).__name__))

    def drain(self):
        while True:
            try:
                key, revision, result, error = self.results.get_nowait()
            except queue.Empty:
                return
            self.pending.discard(key)
            yield key, revision, result, error


@dataclass
class Field:
    key: str
    label: str
    value: str
    choices: tuple = ()


class Form:
    def __init__(self, title, fields, save):
        self.title = title
        self.fields = fields
        self.save = save
        self.active = 0
        self.cursor = len(fields[0].value)
        self.error = ""

    def values(self):
        return {field.key: field.value for field in self.fields}

    def move(self, index):
        self.active = index % len(self.fields)
        self.cursor = len(self.fields[self.active].value)

    def handle(self, key):
        field = self.fields[self.active]
        if key == "\x1b":
            return "cancel"
        if key in ("\n", "\r", curses.KEY_ENTER):
            if self.active == len(self.fields) - 1:
                return "save"
            self.move(self.active + 1)
        elif key == "\x07":
            return "save"
        elif key in ("\t", curses.KEY_DOWN):
            self.move(self.active + 1)
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            self.move(self.active - 1)
        elif field.choices:
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT, " "):
                current = next(i for i, (value, _) in enumerate(field.choices) if value == field.value)
                step = -1 if key == curses.KEY_LEFT else 1
                field.value = field.choices[(current + step) % len(field.choices)][0]
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(field.value), self.cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            self.cursor = len(field.value)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and self.cursor:
            field.value = field.value[:self.cursor - 1] + field.value[self.cursor:]
            self.cursor -= 1
        elif key == curses.KEY_DC:
            field.value = field.value[:self.cursor] + field.value[self.cursor + 1:]
        elif key == "\x15":
            field.value = ""
            self.cursor = 0
        elif isinstance(key, str) and key.isprintable():
            field.value = field.value[:self.cursor] + key + field.value[self.cursor:]
            self.cursor += len(key)
        self.error = ""
        return None

    def draw(self, screen, height, width):
        put(screen, 0, width, self.title, curses.A_BOLD)
        capacity = max(1, height - 4)
        first = max(0, self.active - capacity + 1)
        cursor_position = None
        for i in range(first, min(len(self.fields), first + capacity)):
            field = self.fields[i]
            row = i - first + 1
            active = i == self.active
            prefix = f"{field.label:<12} "
            style = curses.A_REVERSE if active else curses.A_NORMAL
            if field.choices:
                label = next(label for value, label in field.choices if value == field.value)
                put(screen, row, width, prefix + f"< {label} >", style)
            else:
                limit = max(1, width - len(prefix) - 2)
                start = self.cursor if active else 0
                if active:
                    cells = 0
                    while start and cells + _cell_width(field.value[start - 1]) <= limit:
                        start -= 1
                        cells += _cell_width(field.value[start])
                    cursor_position = row, len(prefix) + cells
                put(screen, row, width, prefix + field.value[start:], style)
        put(screen, height - 3, width, self.error, curses.A_BOLD)
        put(screen, height - 2, width, "Tab next field   Left/Right edit or change choice", curses.A_DIM)
        put(screen, height - 1, width, "Enter next/save   Ctrl-G save   Esc cancel", curses.A_DIM)
        if cursor_position:
            set_cursor(1)
            try:
                screen.move(*cursor_position)
            except curses.error:
                pass
        else:
            set_cursor(0)


class Dashboard:
    def __init__(self, screen, host_list):
        self.screen = screen
        self.hosts = {host["id"]: host for host in host_list}
        self.background = Background()
        self.snapshots = {}
        self.errors = {}
        self.revisions = {}
        self.due = {}
        self.view = "activity"
        self.host_id = next(iter(self.hosts))
        self.run_id = None
        self.activity_key = None
        self.schedule_key = None
        self.risk_key = None
        self.focus = None
        self.index = 0
        self.scroll = 0
        self.form = None
        self.edit_request = None
        self.pending_forms = {}
        self.confirmation = None
        self.detail = None
        self.detail_error = ""
        self.detail_view = "actions"
        self.detail_back = "runs"
        self.detail_due = 0
        self.notice = ""

    def run(self):
        self.screen.timeout(150)
        curses.set_escdelay(80)
        try:
            key = None
            dirty = True
            while True:
                dirty |= self.receive()
                self.poll()
                if dirty:
                    self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    key = None
                dirty = key is not None
                if key is not None and not self.handle(key):
                    return 0
        finally:
            self.background.stop.set()

    def poll(self):
        now = time.monotonic()
        if self.view == "detail" and now >= self.detail_due:
            key = ("detail", self.host_id, self.run_id)
            if self.send(key, {"op": "detail", "id": self.run_id}):
                self.detail_due = now + POLL_INTERVAL
        for host_id in self.hosts:
            if len(self.background.pending) >= WORKERS:
                break
            if now < self.due.get(host_id, 0):
                continue
            self.send(("snapshot", host_id), {"op": "snapshot"})

    def send(self, key, payload):
        host_id = key[1]
        return self.background.submit(key, self.revisions.get(host_id, 0), self.hosts[host_id], payload)

    def receive(self):
        changed = False
        for key, revision, data, error in self.background.drain():
            operation, host_id, *target = key
            current = host_id in self.hosts and revision == self.revisions.get(host_id, 0)
            if operation == "mutation":
                changed = True
                form = self.pending_forms.pop(key, None)
                if error:
                    self.notice = f"Schedule change failed. {error}"
                    if form and current and self.view == "schedules" and not (self.form or self.confirmation):
                        form.error = error
                        self.form = form
                else:
                    self.notice = "Schedule updated."
                    if current:
                        self.invalidate(host_id)
                continue
            edit_ready = key == self.edit_request
            if edit_ready:
                self.edit_request = None
            if not current:
                if edit_ready:
                    self.notice = "The schedule changed. Press e to load it again."
                    changed = True
                continue
            changed = True
            if operation == "snapshot":
                self.due[host_id] = time.monotonic() + POLL_INTERVAL
                if error:
                    self.errors[host_id] = error
                else:
                    self.errors.pop(host_id, None)
                    self.snapshots[host_id] = data
                if self.notice == "Refreshing...":
                    self.notice = ""
            elif operation == "detail":
                if self.view == "detail" and (host_id, *target) == (self.host_id, self.run_id):
                    if error:
                        self.detail_error = error
                    else:
                        self.detail_error = ""
                        self.detail = data.get("events", [])
            elif operation == "schedule":
                if edit_ready:
                    self.notice = error or ""
                    if not error:
                        self.schedule_form(host_id, data["schedule"])
        return changed

    def invalidate(self, host_id):
        self.revisions[host_id] = self.revisions.get(host_id, 0) + 1
        self.due[host_id] = 0

    def rows(self):
        if self.view == "hosts":
            return [
                (host_id, host, self.host_summary(host_id, host))
                for host_id, host in self.hosts.items()
            ]
        if self.view == "runs":
            return [
                (run["id"], run, run_summary(run))
                for run in self.runs(self.host_id)
            ]
        if self.view in ("activity", "risks"):
            return self.activity_rows()
        return [
            ((host_id, item["id"]), item, self.schedule_summary(host_id, item))
            for host_id in self.hosts
            for item in self.snapshots.get(host_id, {}).get("schedules", [])
        ]

    def runs(self, host_id):
        return sorted(self.snapshots.get(host_id, {}).get("runs", []),
                      key=lambda run: run.get("started") or 0, reverse=True)

    def activity_rows(self):
        candidates = [
            (host_id, run) for host_id in self.hosts for run in self.runs(host_id)
        ]
        if self.view == "risks":
            candidates = [(host_id, run) for host_id, run in candidates if reported_risk(run)]
            candidates.sort(key=lambda row: row[1].get("started") or 0, reverse=True)
        else:
            active = [(host_id, run) for host_id, run in candidates if run["status"] in ("running", "needs_attention")]
            completed = [(host_id, run) for host_id, run in candidates if run["status"] not in ("running", "needs_attention")]
            active.sort(key=lambda row: row[1].get("started") or 0, reverse=True)
            completed.sort(key=lambda row: row[1].get("ended") or row[1].get("started") or 0, reverse=True)
            candidates = active + completed[:RECENT_COMPLETIONS]
        rows = []
        for host_id, run in candidates:
            label = abbreviate(self.hosts[host_id]["label"], 16)
            if host_id in self.errors:
                label += " offline"
            summary = run_summary(run, "risks" if self.view == "risks" else None)
            rows.append(((host_id, run["id"]), run, f"{label}  {summary}"))
        return rows

    def host_summary(self, host_id, host):
        runs = self.runs(host_id)
        connection = host["ssh"] or "local"
        prefix = f"{host['label']}  [{connection}]"
        if host_id in self.errors:
            prefix += "  unavailable"
        if runs:
            latest = runs[0]
            return (f"{prefix}  {latest['agent']} {run_status_label(latest)}  "
                    f"{len(runs)} runs  {when(latest.get('started'))}  {run_feedback(latest)}")
        if host_id in self.errors:
            return f"{prefix}  {self.errors[host_id]}"
        if host_id in self.snapshots:
            return f"{prefix}  No runs yet  {host['repo']}"
        return f"{prefix}  Loading  {host['repo']}"

    def schedule_summary(self, host_id, item):
        label = self.hosts[host_id]["label"]
        state = "enabled" if item["enabled"] else "paused"
        if host_id in self.errors:
            state += ", host unavailable"
        view = item.get("view") or "actions"
        return (f"{label}  {item['agent']}  {state}  every {item['minutes']}m  "
                f"{view}  Next {when(item.get('next_run'))}  {item['prompt']}")

    def show(self, view, focus=None):
        self.view = view
        self.focus = focus
        self.index = 0
        self.scroll = 0
        self.edit_request = None
        self.notice = ""

    def selection(self, rows):
        if not rows:
            return None
        index = next((i for i, row in enumerate(rows) if row[0] == self.focus), None)
        self.index = min(self.index, len(rows) - 1) if index is None else index
        self.focus = rows[self.index][0]
        return rows[self.index]

    def draw(self):
        screen = self.screen
        height, width = screen.getmaxyx()
        screen.erase()
        set_cursor(0)
        if height < 5 or width < 24:
            put(screen, 0, width, "Resize or press Esc", curses.A_BOLD)
        elif self.form:
            self.form.draw(screen, height, width)
        elif self.confirmation:
            put(screen, 0, width, "Confirm removal", curses.A_BOLD)
            put(screen, 1, width, self.confirmation[0])
            put(screen, height - 1, width, "y confirm   Esc cancel", curses.A_DIM)
        elif self.view == "detail":
            self.draw_detail(height, width)
        else:
            self.draw_rows(height, width)
        try:
            screen.refresh()
        except curses.error:
            pass

    def draw_rows(self, height, width):
        screen = self.screen
        heading, keys, empty = {
            "activity": ("Fleet   Activity", "Enter run history   w workbench   Up/Down move",
                         "No recent runs. Open a workbench from Hosts or create a scheduled agent."),
            "hosts": ("Fleet   Hosts", "Enter runs   w workbench   a add   e edit   d remove",
                      "No hosts configured."),
            "runs": (f"Fleet   {self.hosts[self.host_id]['label']}   Runs",
                     "Enter run history   w workbench   Esc hosts", "No runs for this host yet."),
            "schedules": ("Fleet   Scheduled agents", "a create   e edit   p pause/resume   d delete",
                          "No schedules found. Press a to create one."),
            "risks": ("Fleet   Risks across hosts", "Enter run history   w workbench   Esc hosts",
                      "No recent risks reported by connected hosts."),
        }[self.view]
        put(screen, 0, width, heading, curses.A_BOLD)
        rows = self.rows()
        selected = self.selection(rows)
        capacity = height - 4
        if selected:
            self.scroll = max(0, min(self.scroll, self.index), self.index - capacity + 1)
            for index, (_, _, summary) in enumerate(rows[self.scroll:self.scroll + capacity], self.scroll):
                style = curses.A_REVERSE if index == self.index else curses.A_NORMAL
                put(screen, index - self.scroll + 1, width, summary, style)
        else:
            put(screen, 1, width, empty, curses.A_DIM)
        status = self.notice
        if not status and self.view == "hosts" and selected:
            status = self.errors.get(selected[0], "")
        if not status and self.view == "runs":
            status = self.errors.get(self.host_id, "")
        if not status and self.view in ("activity", "schedules", "risks"):
            unavailable = sum(host_id in self.errors for host_id in self.hosts)
            loading = sum(host_id not in self.snapshots and host_id not in self.errors for host_id in self.hosts)
            if unavailable or loading:
                status = f"{unavailable} hosts unavailable   {loading} loading"
        put(screen, height - 3, width, status, curses.A_BOLD)
        put(screen, height - 2, width, keys, curses.A_DIM)
        put(screen, height - 1, width, "1 Activity   2 Hosts   3 Schedules   4 Risks   Tab next   r refresh   q quit", curses.A_DIM)

    def draw_detail(self, height, width):
        heading = f"Fleet   {self.hosts[self.host_id]['label']}   Run history   {self.detail_view}"
        put(self.screen, 0, width, heading, curses.A_BOLD)
        run = next((item for item in self.runs(self.host_id) if item["id"] == self.run_id), None)
        if run:
            put(self.screen, 1, width, run_summary(run, self.detail_view), curses.A_BOLD)
        if self.detail_error and self.detail is None:
            events = [self.detail_error]
        elif self.detail is None:
            events = ["Loading action and reason history..."]
        else:
            allowed = None
            if self.detail_view == "summary":
                allowed = {"result", "error", "risk"}
            elif self.detail_view == "risks":
                allowed = {"error", "risk"}
            events = [
                f"{when(event.get('at'))}  {event['kind']}  {event['text']}"
                for event in self.detail if allowed is None or event["kind"] in allowed
            ] or ["No events in this view."]
        lines = []
        for event in events:
            for line in event.splitlines() or [""]:
                lines.extend(textwrap.wrap(line, width=width - 2, replace_whitespace=False) or [""])
        capacity = height - 4
        self.scroll = max(0, min(self.scroll, len(lines) - capacity))
        for row, line in enumerate(lines[self.scroll:self.scroll + capacity], 2):
            put(self.screen, row, width, line)
        put(self.screen, height - 2, width, self.detail_error or self.notice, curses.A_BOLD)
        put(self.screen, height - 1, width, "Up/Down scroll   v change view   r refresh   Esc back", curses.A_DIM)

    def handle(self, key):
        if key == curses.KEY_RESIZE:
            return True
        height, width = self.screen.getmaxyx()
        if height < 5 or width < 24:
            if key not in ("q", "\x1b"):
                return True
        if self.form:
            if height < 5 or width < 24:
                self.form = None
                return True
            self.handle_form(key)
            return True
        if self.confirmation:
            _, action = self.confirmation
            self.confirmation = None
            if key in ("y", "Y"):
                self.attempt(action)
            return True
        if key == "q":
            return not self.can_quit()
        if key == "\x1b":
            if self.view == "detail":
                self.activate(self.detail_back)
            elif self.view == "runs":
                self.show("hosts", self.host_id)
            elif self.view != "activity":
                self.activate("activity")
            else:
                return not self.can_quit()
            return True
        if key in ("1", "2", "3", "4", "\t"):
            if key == "\t":
                tab = self.view if self.view in TABS else ("hosts" if self.view == "runs" else self.detail_back)
                index = TABS.index(tab) if tab in TABS else TABS.index("hosts")
                target = TABS[(index + 1) % len(TABS)]
            else:
                target = TABS[int(key) - 1]
            self.activate(target)
            return True
        if key == "r":
            self.due.clear()
            self.detail_due = 0
            self.notice = "Refreshing..."
            return True

        movement = {
            curses.KEY_UP: -1, "k": -1, curses.KEY_DOWN: 1, "j": 1,
            curses.KEY_PPAGE: -max(1, height - 4), curses.KEY_NPAGE: max(1, height - 4),
        }.get(key, 0)
        if self.view == "detail":
            if key == "v":
                index = REPORTING_KEYS.index(self.detail_view)
                self.detail_view = REPORTING_KEYS[(index + 1) % len(REPORTING_KEYS)]
                self.scroll = 0
            self.scroll += movement
            return True

        rows = self.rows()
        item = self.selection(rows)
        if movement and rows:
            self.index = max(0, min(len(rows) - 1, self.index + movement))
            self.focus = rows[self.index][0]
            item = rows[self.index]
            self.notice = ""
            self.edit_request = None
        if self.view == "hosts":
            if item:
                self.host_id = item[0]
            self.handle_host(key)
        elif self.view == "runs":
            if item and key in ("\n", "\r", curses.KEY_ENTER):
                self.inspect(item[1])
            elif key == "w":
                self.workbench()
        elif self.view in ("activity", "risks"):
            if item:
                if self.view == "activity":
                    self.activity_key = item[0]
                else:
                    self.risk_key = item[0]
                self.host_id = item[0][0]
                if key in ("\n", "\r", curses.KEY_ENTER):
                    self.inspect(item[1])
                elif key == "w":
                    self.workbench()
        else:
            self.handle_schedule(key, item)
        return True

    def activate(self, view):
        focus = {
            "activity": self.activity_key,
            "hosts": self.host_id,
            "runs": self.run_id,
            "schedules": self.schedule_key,
            "risks": self.risk_key,
        }[view]
        self.show(view, focus)

    def can_quit(self):
        if any(pending[0] == "mutation" for pending in self.background.pending):
            self.notice = "Waiting for the schedule change to finish."
            return False
        return True

    def inspect(self, run):
        self.run_id = run["id"]
        self.detail_back = self.view
        self.detail_view = "risks" if self.view == "risks" else run_view(run)
        self.detail = None
        self.detail_error = ""
        self.detail_due = 0
        self.show("detail")

    def handle_host(self, key):
        host = self.hosts[self.host_id]
        if key in ("\n", "\r", curses.KEY_ENTER):
            self.show("runs")
        elif key == "w":
            self.workbench()
        elif key == "a":
            self.host_form()
        elif key == "e":
            self.host_form(host)
        elif key == "d":
            if self.host_id == "local":
                self.notice = "The local host is always available here."
            else:
                self.confirmation = (f"Remove {host['label']} from this dashboard?", lambda: self.delete_host(host))

    def handle_schedule(self, key, item):
        if item:
            self.schedule_key = item[0]
        if key == "a":
            self.edit_request = None
            self.schedule_form(item[0][0] if item else self.host_id)
        elif item and key in ("e", "p", "d"):
            (host_id, schedule_id), schedule, _ = item
            if key == "e":
                edit = ("schedule", host_id, schedule_id)
                if self.send(edit, {"op": "schedule", "id": schedule_id}):
                    self.edit_request = edit
                    self.notice = "Loading schedule..."
                elif self.edit_request != edit:
                    self.notice = "A request is already in progress. Try again shortly."
            elif key == "p":
                self.edit_request = None
                operation = "pause" if schedule["enabled"] else "resume"
                self.attempt(lambda: self.mutate(host_id, {"op": operation, "id": schedule_id}))
            else:
                self.edit_request = None
                message = f"Delete the {schedule['agent']} schedule on {self.hosts[host_id]['label']}?"
                self.confirmation = (message, lambda: self.mutate(host_id, {"op": "delete", "id": schedule_id}))

    def workbench(self):
        curses.def_prog_mode()
        curses.endwin()
        try:
            self.attempt(lambda: open_host(self.hosts[self.host_id]))
        except KeyboardInterrupt:
            self.notice = "Returned to Fleet."
        finally:
            curses.reset_prog_mode()
            self.screen.clearok(True)
            self.due.clear()

    def host_form(self, host=None):
        local = host is not None and host["id"] == "local"
        fields = []
        if not local:
            fields.extend([
                Field("label", "Name", host["label"] if host else ""),
                Field("ssh", "SSH target", host["ssh"] if host else ""),
            ])
        fields.append(Field("repo", "Repository", host["repo"] if host else ""))

        def save(values):
            values = {key: value.strip() for key, value in values.items()}
            if not all(values.values()):
                raise ValueError("Complete each field.")
            ssh = values.get("ssh", "")
            if ssh.startswith("-") or any(char.isspace() for char in ssh):
                raise ValueError("Enter an SSH alias or user@hostname.")
            record = {**(host or {"id": uuid4().hex}), **values}
            try:
                save_host(record)
            except sqlite3.IntegrityError:
                raise ValueError("That SSH target is already configured.") from None
            self.hosts = {current["id"]: current for current in hosts()}
            host_id = record["id"]
            self.snapshots.pop(host_id, None)
            self.errors.pop(host_id, None)
            self.invalidate(host_id)
            self.host_id = host_id
            self.show("hosts", host_id)
            self.notice = "Host saved."

        self.form = Form("Edit host" if host else "Add host", fields, save)

    def delete_host(self, host):
        remove_host(host["id"])
        self.hosts.pop(host["id"])
        self.snapshots.pop(host["id"], None)
        self.errors.pop(host["id"], None)
        self.invalidate(host["id"])
        self.host_id = next(iter(self.hosts))
        self.show("hosts", self.host_id)
        self.notice = "Host removed."

    def schedule_form(self, host_id, schedule=None):
        targets = tuple((key, host["label"]) for key, host in self.hosts.items())

        def save(values):
            repo, prompt = values["repo"].strip(), values["prompt"].strip()
            if not repo or not prompt:
                raise ValueError("Enter a repository and prompt.")
            try:
                minutes = int(values["minutes"])
                if minutes < 1:
                    raise ValueError
            except ValueError:
                raise ValueError("Enter an interval of at least one minute.") from None
            payload = {"op": "create", "agent": values["agent"], "repo": repo,
                       "prompt": prompt, "minutes": minutes, "view": values["view"]}
            if schedule:
                payload.update(op="update", id=schedule["id"])
            return self.mutate(values.get("host", host_id), payload)

        previous = schedule or {}
        fields = [
            Field("agent", "Agent", previous.get("agent", "codex"), (("codex", "codex"), ("cursor", "cursor"))),
            Field("repo", "Repository", previous.get("repo", self.hosts[host_id]["repo"])),
            Field("minutes", "Minutes", str(previous.get("minutes", 60))),
            Field("view", "Reporting", run_view(previous), REPORTING),
            Field("prompt", "Prompt", previous.get("prompt", "")),
        ]
        if not schedule:
            fields.insert(0, Field("host", "Host", host_id, targets))
        self.form = Form("Edit scheduled agent" if schedule else "Create scheduled agent", fields, save)

    def handle_form(self, key):
        form = self.form
        values = form.values()
        result = form.handle(key)
        if "host" in values and form.fields[0].value != values["host"]:
            repo = next(field for field in form.fields if field.key == "repo")
            if repo.value == self.hosts[values["host"]]["repo"]:
                repo.value = self.hosts[form.fields[0].value]["repo"]
        if result == "cancel":
            self.form = None
        elif result == "save":
            try:
                pending = form.save(form.values())
            except REQUEST_ERRORS as error:
                form.error = str(error)
            else:
                if pending is not None:
                    self.pending_forms[pending] = form
                self.form = None

    def mutate(self, host_id, payload):
        key = ("mutation", host_id)
        if not self.send(key, payload):
            raise RuntimeError("A request is already in progress. Try again shortly.")
        self.notice = "Saving schedule change..."
        return key

    def attempt(self, action):
        try:
            action()
        except REQUEST_ERRORS as error:
            self.notice = str(error)


def run_view(run):
    view = run.get("view")
    return view if view in REPORTING_KEYS else "actions"


def reported_risk(run):
    if run.get("risk"):
        return run["risk"]
    if run.get("status") in ("failed", "error"):
        return run.get("outcome") or run.get("action") or "Run failed."
    return ""


def run_feedback(run, view=None):
    view = view or run_view(run)
    risk = reported_risk(run)
    if view == "risks":
        return f"Risk   {risk}" if risk else "No risk reported."
    action = run.get("action") or ""
    if view == "summary" and not run.get("ended"):
        action = ""
    outcome = (run.get("outcome") or action) if view == "summary" else action
    if risk:
        return f"Risk   {risk}  {outcome}" if outcome and outcome != risk else f"Risk   {risk}"
    return outcome


def run_status_label(run):
    return "Needs Attention" if run["status"] == "needs_attention" else run["status"]


def run_summary(run, view=None):
    title = abbreviate(run.get("title") or run["repo"], 28)
    ended = f"  Ended {when(run['ended'])}" if run.get("ended") is not None else ""
    return (f"{title}  {run['agent']} {run_status_label(run)}  {run_feedback(run, view)}  "
            f"{when(run.get('started'))}{ended}")


def abbreviate(value, limit):
    return value if len(value) <= limit else value[:limit - 3] + "..."


def when(value):
    if value is None:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return "-"


def set_cursor(value):
    try:
        curses.curs_set(value)
    except curses.error:
        pass


def _display(value):
    return "".join(char if char.isprintable() else " " for char in str(value))


def _cell_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def put(screen, row, width, value, style=0):
    if width < 2:
        return
    cells = 0
    text = _display(value)
    end = 0
    for char in text:
        cells += _cell_width(char)
        if cells >= width:
            break
        end += 1
    try:
        screen.addstr(row, 0, text[:end], style)
    except (curses.error, UnicodeError):
        pass
