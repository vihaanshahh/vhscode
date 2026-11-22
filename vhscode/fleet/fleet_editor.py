"""Terminal file browsing, editing and prompt tags for Fleet."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from collections import deque
from itertools import islice
from pathlib import Path

try:
    import curses
except ImportError:
    curses = None


EDIT_LIMIT = 10 * 1024 * 1024
SAMPLE_SIZE = 8192
BINARY_CONTROLS = bytes(range(9)) + b"\x0b" + bytes(range(14, 32))


def browse(root: Path, tags: set[str]) -> None:
    """Browse root and toggle repository-relative file paths in tags."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("File browsing requires an interactive terminal.", file=sys.stderr)
        return

    if curses is None:
        print("File browsing requires Python with curses support.", file=sys.stderr)
        return

    try:
        curses.wrapper(_browse, root.resolve(strict=True), tags)
    except KeyboardInterrupt:
        pass
    except (curses.error, OSError, RuntimeError) as error:
        print(f"Could not open the file browser. {error}", file=sys.stderr)


def _inside_root(path: Path, root: Path) -> Path:
    target = path.resolve(strict=True)
    if target != root and root not in target.parents:
        raise ValueError("That path points outside the workspace.")
    return target


class _DirectoryPages:
    def __init__(self, root: Path):
        self.root = root
        self.directory = None
        self.entries = None
        self.next_entry = None
        self.offset = 0

    def close(self) -> None:
        if self.entries is not None:
            self.entries.close()
        self.entries = None
        self.next_entry = None
        self.offset = 0

    def read(self, directory: Path, offset: int, size: int):
        if self.entries is None or directory != self.directory or offset < self.offset:
            self.close()
            self.entries = os.scandir(_inside_root(directory, self.root))
            self.directory = directory

        # Keep the lookahead entry with the scanner so consecutive pages continue
        # where the previous page stopped, retaining only one page of entries.
        skipped = offset - self.offset
        page = []
        if self.next_entry is not None:
            if skipped:
                skipped -= 1
            else:
                page.append(self.next_entry)
        deque(islice(self.entries, skipped), maxlen=0)
        page.extend(islice(self.entries, size - len(page)))
        self.next_entry = next(self.entries, None)
        self.offset = offset + len(page)
        return page, self.next_entry is not None


def _visible(text: str) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in text)


def _directory_label(entry: os.DirEntry) -> str:
    try:
        if entry.is_symlink():
            return "@"
        if entry.is_dir(follow_symlinks=False):
            return "/"
    except OSError:
        pass
    return ""


def _tag_path(path: Path, root: Path, marker: str) -> str | None:
    if marker == "/":
        return None
    if marker == "@":
        try:
            path = _inside_root(path, root)
        except (OSError, RuntimeError, ValueError):
            return None
    return path.relative_to(root).as_posix()


def _edit_warning(path: Path, size: int) -> str:
    if size > EDIT_LIMIT:
        return "File exceeds 10 MiB. Press e again to open it."

    with path.open("rb") as file:
        sample = file.read(SAMPLE_SIZE)
    control_count = len(sample) - len(sample.translate(None, BINARY_CONTROLS))
    if b"\x00" in sample or control_count > len(sample) // 10:
        return "File appears to be binary. Press e again to open it."
    return ""


def _edit(screen, root: Path, path: Path) -> str:
    editor = os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip() or "vi"
    command = shlex.split(editor)
    if not command or not command[0]:
        return "The editor command is empty."

    curses.def_prog_mode()
    curses.endwin()
    try:
        result = subprocess.run([*command, str(path)], cwd=root, check=False)
    finally:
        curses.reset_prog_mode()
        screen.clearok(True)

    if result.returncode:
        return f"Editor exited with status {result.returncode}."
    return f"Closed editor for {path.relative_to(root).as_posix()}."


def _browse(screen, root: Path, tags: set[str]) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    pages = _DirectoryPages(root)
    try:
        _browse_files(screen, root, tags, pages)
    finally:
        pages.close()


def _browse_files(screen, root: Path, tags: set[str], pages: _DirectoryPages) -> None:
    directory = root
    history: list[tuple[Path, int]] = []
    selected = 0
    page_key = None
    notice = ""
    confirmed_path = None

    while True:
        height, width = screen.getmaxyx()
        if height < 5 or width < 24:
            screen.erase()
            _line(screen, 0, width, "Resize or press q", curses.A_BOLD)
            if screen.getch() in (ord("q"), 27):
                return
            continue
        size = max(1, min(height - 4, 128))
        offset = selected // size * size
        if page_key != (directory, offset, size):
            try:
                entries, has_next = pages.read(directory, offset, size)
            except (OSError, RuntimeError, ValueError) as error:
                pages.close()
                entries, has_next = [], False
                notice = str(error)
                selected = 0
                offset = 0
            if offset and not entries:
                selected = offset - 1
                continue
            selected = min(selected, offset + max(0, len(entries) - 1))
            page_key = directory, offset, size

        screen.erase()
        relative = directory.relative_to(root).as_posix()
        heading = f"Files {relative}/" if relative != "." else "Files"
        _line(screen, 0, width, f"{_visible(heading)}   •   {len(tags)} tagged", curses.A_BOLD)
        for row, entry in enumerate(entries):
            marker = _directory_label(entry)
            tag = "*" if _tag_path(Path(entry.path), root, marker) in tags else " "
            style = curses.A_REVERSE if offset + row == selected else curses.A_NORMAL
            _line(screen, row + 1, width, f" {tag} {_visible(entry.name)}{marker}", style)

        if not entries:
            _line(screen, 1, width, "  (no entries)", curses.A_DIM)
        status = notice or f"Page {offset // size + 1}{'  •  more entries' if has_next else ''}"
        _line(screen, height - 3, width, _visible(status), curses.A_DIM)
        _line(screen, height - 2, width, "Up/Down Move   PgUp/PgDn Page   Left Parent   Enter Open", curses.A_NORMAL)
        _line(screen, height - 1, width, "e Edit   Space Tag   q Back to prompt", curses.A_NORMAL)
        try:
            screen.refresh()
        except curses.error:
            # A terminal resize can invalidate the frame before it reaches the screen.
            pass

        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key == curses.KEY_RESIZE:
            continue
        notice = ""
        confirm_edit = confirmed_path if key == ord("e") else None
        confirmed_path = None

        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")) and (selected < offset + len(entries) - 1 or has_next):
            selected += 1
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - size)
        elif key == curses.KEY_NPAGE and has_next:
            selected += size
        elif key in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 8, 127) and history:
            directory, selected = history.pop()
        elif entries and key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13, ord("e"), ord(" ")):
            path = directory / entries[selected - offset].name
            try:
                target = _inside_root(path, root)
                info = target.stat()
                if stat.S_ISDIR(info.st_mode):
                    if key in (ord("e"), ord(" ")):
                        notice = "Select a file to edit or tag."
                        continue
                    history.append((directory, selected))
                    directory, selected = path, 0
                elif not stat.S_ISREG(info.st_mode):
                    notice = "Select a regular file to edit or tag."
                elif key == ord(" "):
                    tag = target.relative_to(root).as_posix()
                    if tag in tags:
                        tags.remove(tag)
                        notice = f"Removed tag for {tag}."
                    else:
                        tags.add(tag)
                        notice = f"Tagged {tag}."
                else:
                    if target != confirm_edit:
                        notice = _edit_warning(target, info.st_size)
                        if notice:
                            confirmed_path = target
                            continue
                    try:
                        notice = _edit(screen, root, target)
                    finally:
                        pages.close()
                        page_key = None
            except (OSError, RuntimeError, ValueError, curses.error) as error:
                notice = str(error)


def _line(screen, row: int, width: int, text: str, style: int) -> None:
    if row < 0 or width < 2:
        return
    try:
        screen.addnstr(row, 0, text, width - 1, style)
    except (curses.error, UnicodeError):
        # A resize or a wide character can reach past the last terminal cell.
        pass
