#!/usr/bin/env python3
"""Read-only FBTERM dashboard for a real tmux pane (Python standard library).

The display is an operational summary, not a screenshot or an execution engine.
Job files remain authoritative. No lifecycle recovery or job mutation is done.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import curses
import heapq
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import textwrap
import time

VERSION = "1.0"
MAX_FILE = 32768
MAX_TAIL = 12288
RECENT_LIMIT = 40
CACHE_LIMIT = 2048
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
SENSITIVE = re.compile(
    r"(?i)(password|passwd|passphrase|secret|token|bearer|authorization|cookie|"
    r"api[ _.-]?key|private[ _.-]?key|client[ _.-]?secret|oauth|"
    r"ssh-(?:rsa|ed25519)|BEGIN [A-Z ]*PRIVATE KEY|://[^ /]+:[^ /]+@)"
)
BLOB = re.compile(r"[A-Za-z0-9+/_=-]{160,}")
ENCODED_LINE = re.compile(r"\s*[A-Za-z0-9+/]{48,}={0,2}\s*\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
TRANSPORT = "[transport payload omitted; raw output retained]"
REDACTED = "[potentially sensitive output hidden]"


def ascii_text(value):
    """Discard terminal controls and non-ASCII glyphs for the physical VT."""
    text = ANSI.sub("", str(value)).replace("\r", " ")
    return "".join(c for c in text if 32 <= ord(c) < 127)


def safe_line(value):
    text = ascii_text(value)
    if SENSITIVE.search(text):
        return REDACTED
    low = text.lower()
    if (BLOB.search(text) or ENCODED_LINE.fullmatch(text)
            or "data:image/" in low or "base64" in low
            or '"capture_id"' in low or '"data"' in low and '"encoding"' in low):
        return TRANSPORT
    return text[:400] + (" [truncated]" if len(text) > 400 else "")


def read_small(path, limit=MAX_FILE):
    """Bound reads and reject symlinks/special files (including stdout pipes)."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("not a bounded regular file")
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def read_exit(job):
    try:
        if job.is_symlink():
            return None
        value = read_small(job / "exit.code", 32).decode("ascii").strip()
        return int(value) if re.fullmatch(r"-?[0-9]{1,4}", value) else None
    except (OSError, ValueError, UnicodeError):
        return None


def worker_alive(token, proc_root=Path("/proc")):
    """Three-valued PID identity check: true, false or unavailable."""
    if not isinstance(token, dict) or type(token.get("pid")) is not int or token["pid"] <= 0:
        return None
    try:
        raw = (proc_root / str(token["pid"]) / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        boot = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
        if not all(k in token for k in ("start_ticks", "boot")):
            return None
        return (fields[0] != "Z" and fields[19] == str(token["start_ticks"])
                and boot == token["boot"])
    except FileNotFoundError:
        return False
    except (OSError, IndexError, ValueError):
        return None


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


@dataclass(frozen=True)
class Job:
    name: str
    updated: float
    state: str
    exit_code: int | None = None
    started: float | None = None
    finished: float | None = None
    recorded: bool = False

    def duration(self, now):
        if self.started is None:
            return "--"
        stop = self.finished if self.finished is not None else now if self.state == "RUNNING" else None
        if stop is None or stop < self.started:
            return "--"
        seconds = int(stop - self.started)
        if seconds >= 3600:
            return f"{seconds // 3600}h{seconds % 3600 // 60:02}m"
        if seconds >= 60:
            return f"{seconds // 60}m{seconds % 60:02}s"
        return f"{seconds}s"


def describe(name, receipt, job_path, updated, proc_root=Path("/proc")):
    rc = read_exit(job_path)
    if receipt is None:
        return Job(name, updated, "EXIT" if rc is not None else "UNKNOWN", rc)
    if (not isinstance(receipt, dict) or receipt.get("schema") != 4
            or receipt.get("job_id") != name):
        return Job(name, updated, "INVALID", rc)
    phase = receipt.get("phase")
    started = number(receipt.get("started_at"))
    finished = number(receipt.get("finished_at"))
    if phase == "COMPLETED":
        recorded = (rc is not None and type(receipt.get("exit_code")) is int
                    and rc == receipt["exit_code"] and finished is not None)
        return Job(name, updated, ("DONE" if rc == 0 else "FAILED") if recorded else "MISMATCH",
                   rc, started, finished, recorded)
    if phase == "RUNNING":
        live = worker_alive(receipt.get("worker"), proc_root)
        state = "RUNNING" if live is True else "STALE" if live is False else "UNCONFIRMED"
        return Job(name, updated, state, rc, started)
    if phase in ("RESERVED", "ARMED"):
        return Job(name, updated, phase, rc, started, finished)
    if phase == "INTERRUPTED":
        return Job(name, updated, "INTERRUPTED", rc, started, finished)
    return Job(name, updated, "INVALID", rc, started, finished)


class Catalogue:
    """Stream the full census; keep bounded metadata and bounded output tails."""
    def __init__(self, root, proc_root=Path("/proc")):
        self.root = Path(root)
        self.proc_root = Path(proc_root)
        self.cache = OrderedDict()
        self.recent = []
        self.counts = {}
        self.total = 0
        self.errors = 0
        self.last_scan = None
        self.output_cache = (None, [])

    def receipt(self, path):
        info = path.lstat()
        stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        key = str(path)
        if key in self.cache and self.cache[key][0] == stamp:
            self.cache.move_to_end(key)
            return self.cache[key][1]
        try:
            data = json.loads(read_small(path))
        except (OSError, ValueError, UnicodeError):
            data = {}
        self.cache[key] = (stamp, data)
        self.cache.move_to_end(key)
        while len(self.cache) > CACHE_LIMIT:
            self.cache.popitem(last=False)
        return data

    def scan(self):
        counts, newest, total, errors = {}, [], 0, 0
        receipts = self.root / "receipts"
        jobs = self.root / "jobs"

        def accept(job):
            nonlocal total
            total += 1
            counts[job.state] = counts.get(job.state, 0) + 1
            # A stable tie breaker means equal mtimes cannot compare Job objects.
            key = (job.updated, job.name, job)
            if len(newest) < RECENT_LIMIT:
                heapq.heappush(newest, key)
            elif key[:2] > newest[0][:2]:
                heapq.heapreplace(newest, key)

        # Enumerate every receipt, not only recent directories. Older running
        # jobs are counted using PID identity even when newer jobs have arrived.
        try:
            try:
                receipt_entries = os.scandir(receipts)
            except FileNotFoundError:
                # Legacy producers need no receipts directory. Only opening
                # this optional source may be absent; entry errors still count.
                receipt_entries = nullcontext(())
            with receipt_entries as entries:
                for entry in entries:
                    if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name[:-5]
                    if not IDENTIFIER.fullmatch(name):
                        continue
                    try:
                        path = Path(entry.path)
                        info = entry.stat(follow_symlinks=False)
                        accept(describe(name, self.receipt(path), jobs / name, info.st_mtime,
                                        self.proc_root))
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
        # Legacy jobs with an exit file are useful, but never assumed running.
        try:
            with os.scandir(jobs) as entries:
                for entry in entries:
                    if not IDENTIFIER.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                        continue
                    if (receipts / (entry.name + ".json")).exists():
                        continue
                    try:
                        accept(describe(entry.name, None, Path(entry.path),
                                        entry.stat(follow_symlinks=False).st_mtime, self.proc_root))
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
        self.recent = [row[2] for row in sorted(newest, reverse=True)]
        self.counts, self.total, self.errors = counts, total, errors
        self.last_scan = time.time()

    def output(self, name):
        if not IDENTIFIER.fullmatch(name):
            return []
        job = self.root / "jobs" / name
        if job.is_symlink():
            return []
        stamps = []
        for stream in ("stdout.bin", "stderr.bin"):
            try:
                info = (job / stream).lstat()
                stamps.append((info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
            except OSError:
                stamps.append(None)
        cache_key = (name, *stamps)
        if self.output_cache[0] == cache_key:
            return self.output_cache[1]
        lines = []
        transport_seen = False
        for stream, prefix in (("stdout.bin", "OUT"), ("stderr.bin", "ERR")):
            try:
                fd = os.open(job / stream, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    start = max(0, info.st_size - MAX_TAIL)
                    os.lseek(fd, start, os.SEEK_SET)
                    data = os.read(fd, MAX_TAIL)
                finally:
                    os.close(fd)
                chunks = data.decode("utf-8", errors="replace").splitlines()
                if start and chunks:
                    chunks.pop(0)  # Never expose a partial potentially sensitive line.
                # Multiline credential blocks must not leak short base64 lines.
                secret_block = False
                for text in chunks[-80:]:
                    upper = text.upper()
                    if "-----BEGIN " in upper and "KEY-----" in upper:
                        secret_block = True
                    if secret_block:
                        line = REDACTED
                        if "-----END " in upper and "KEY-----" in upper:
                            secret_block = False
                    else:
                        line = safe_line(text)
                    if line == TRANSPORT:
                        if transport_seen:
                            continue
                        transport_seen = True
                    if line.strip() and (not lines or lines[-1] != (prefix, line)):
                        lines.append((prefix, line))
            except (OSError, ValueError):
                continue
        lines = lines[-100:]
        self.output_cache = (cache_key, lines)
        return lines


def put(screen, row, text, attr=0):
    h, w = screen.getmaxyx()
    if 0 <= row < h and w > 1:
        try:
            screen.addnstr(row, 0, ascii_text(text).ljust(w - 1), w - 1, attr)
        except (curses.error, ValueError):
            pass


def recent_label(job, width):
    # Date-heavy execution IDs are shortened from the left; their unique suffix
    # and observed time remain visible, while the complete ID is shown below.
    name = job.name if len(job.name) <= width else ".." + job.name[-max(1, width - 2):]
    return name


def draw(screen, catalogue, node, selection=0, follow=True, help_open=False):
    screen.erase()
    h, w = screen.getmaxyx()
    now = time.time()
    title = curses.A_REVERSE | curses.A_BOLD
    accent = curses.color_pair(1) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
    good = curses.color_pair(2) if curses.has_colors() else curses.A_NORMAL
    warning = curses.color_pair(3) if curses.has_colors() else curses.A_BOLD
    bad = curses.color_pair(4) if curses.has_colors() else curses.A_BOLD
    put(screen, 0, f" FBTERM / ACTIVITY  |  {node}", title)
    clock = datetime.now().astimezone().strftime("%H:%M:%S %Z")
    running = catalogue.counts.get("RUNNING", 0)
    pending = sum(catalogue.counts.get(k, 0) for k in ("RESERVED", "ARMED"))
    uncertain = sum(catalogue.counts.get(k, 0) for k in ("UNKNOWN", "STALE", "UNCONFIRMED", "INVALID", "MISMATCH"))
    put(screen, 1, f" {clock}  |  RUN {running}  WAIT {pending}  CHECK {uncertain}", accent)
    if h < 10 or w < 35:
        put(screen, 2, f" {catalogue.total} jobs | {'LIVE' if follow else 'REVIEW'}", good)
        put(screen, 3, " Ctrl+B z: zoom | Ctrl+B o: focus")
        return screen.refresh()
    if help_open:
        help_lines = [
            " MONITOR HELP", "", " j/k or Up/Down : review recent jobs",
            " f                : follow newest job", " ?                : close this help", "",
            " TMUX", " Ctrl+B then o    : switch pane",
            " Ctrl+B then arrow: choose pane", " Ctrl+B then z    : zoom / restore", "",
            " RUNNING: worker PID, birth and boot match.",
            " DONE/FAILED: receipt and exit agree.",
            " EXIT: legacy exit file, no receipt.",
            " STALE/CHECK: activity cannot be confirmed.", "",
            " Output is a filtered, bounded tail.",
            " Raw stdout/stderr remain in job files.",
            " Output hashes are not checked by this UI.",
            " q does not close the monitor.",
        ]
        for row, text in enumerate(help_lines, 3):
            if row < h - 2:
                put(screen, row, text)
        put(screen, h - 1, " ?: close help | Ctrl+B o: Bash", title)
        return screen.refresh()
    put(screen, 2, f" {'LIVE' if follow else 'REVIEW'} | {catalogue.total} jobs | receipts + process identity", good)
    if catalogue.errors:
        put(screen, 3, f" Source unavailable/read errors: {catalogue.errors}", warning)
    else:
        put(screen, 3, " Recent jobs / recorded results", accent)
    list_count = min(8 if h >= 40 else 5 if h >= 25 else 2, max(1, h // 6))
    start_index = max(0, selection - list_count + 1)
    rows = catalogue.recent[start_index:start_index + list_count]
    for index, job in enumerate(rows):
        selected = start_index + index == selection
        rc = f" rc={job.exit_code}" if job.exit_code is not None else ""
        state = f"{job.state}{rc} {job.duration(now)}"
        name_width = max(8, w - len(state) - 5)
        line = f"{'>' if selected else ' '} {recent_label(job, name_width):<{name_width}} {state}"
        color = bad if job.state in ("FAILED", "MISMATCH", "INVALID") else accent if selected else good
        put(screen, 4 + index, line, color)
    detail_row = 5 + list_count
    put(screen, detail_row - 1, " " + "-" * max(0, w - 3))
    if catalogue.recent:
        job = catalogue.recent[min(selection, len(catalogue.recent) - 1)]
        wrapped = textwrap.wrap(" Job: " + job.name, width=max(1, w - 2))[:2]
        for line in wrapped:
            put(screen, detail_row, line, accent)
            detail_row += 1
        lines = catalogue.output(job.name)
        available = max(0, h - detail_row - 4)
        wrapped_output = []
        for prefix, line in lines:
            for part in textwrap.wrap(prefix + " " + line, width=max(1, w - 3)) or [prefix]:
                wrapped_output.append((prefix, " " + part))
        if not wrapped_output:
            wrapped_output = [("OUT", " No readable output recorded.")]
        for index, (prefix, text) in enumerate(wrapped_output[-available:] if available else []):
            put(screen, detail_row + index, text, warning if prefix == "ERR" else curses.A_NORMAL)
    else:
        put(screen, detail_row, " Waiting for recorded FBTERM jobs.", good)
    put(screen, h - 3, " " + "-" * max(0, w - 3))
    put(screen, h - 2, " j/k: review | f: live | ?: help", good)
    put(screen, h - 1, " Ctrl+B o: focus | Ctrl+B z: zoom", title)
    screen.refresh()


def main_screen(screen, root, node):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(200)
    screen.keypad(True)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        for pair, fg in ((1, curses.COLOR_CYAN), (2, curses.COLOR_GREEN),
                         (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED)):
            try:
                curses.init_pair(pair, fg, -1)
            except curses.error:
                pass
    catalogue = Catalogue(root)
    previous = 0.0
    selection, follow, help_open = 0, True, False
    selected_name = None
    while True:
        now = time.monotonic()
        if now - previous >= 1.5:
            catalogue.scan()
            previous = now
            if follow:
                selection = 0
            elif selected_name:
                selection = next((i for i, j in enumerate(catalogue.recent) if j.name == selected_name),
                                 min(selection, max(0, len(catalogue.recent) - 1)))
        draw(screen, catalogue, node, selection, follow, help_open)
        ch = screen.getch()
        if ch in (ord("j"), curses.KEY_DOWN):
            selection = min(selection + 1, max(0, len(catalogue.recent) - 1))
            follow = False
        elif ch in (ord("k"), curses.KEY_UP):
            selection = max(0, selection - 1)
            follow = False
        elif ch in (ord("f"), ord("F")):
            selection, follow = 0, True
        elif ch == ord("?"):
            help_open = not help_open
        selected_name = catalogue.recent[selection].name if catalogue.recent else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get(
        "FRAMEBRIDGE_FBTERM_ROOT", str(Path.home() / ".local/share/framebridge/fbterm"))))
    parser.add_argument("--node", default=os.environ.get("FRAMEBRIDGE_NODE", socket.gethostname()))
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    node = ascii_text(args.node)[:64]
    try:
        curses.wrapper(main_screen, args.root, node)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
