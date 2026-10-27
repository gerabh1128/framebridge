"""Bind real framebuffer observations to the visible, owned tmux terminal.

This module never selects a window, pane or VT. Input is permitted only to the
pane that was active in the client on the observed foreground VT. Capture files
and manifests are stored by the caller in its private, immutable capture store.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from datetime import datetime, timezone


class ScreenControlError(RuntimeError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


ControlError = ScreenControlError


KEYS = {
    "ENTER": "Enter", "RETURN": "Enter", "TAB": "Tab", "SHIFT_TAB": "BTab",
    "ESC": "Escape", "BACKSPACE": "BSpace", "DELETE": "DC", "INSERT": "IC",
    "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
    "HOME": "Home", "END": "End", "PAGE_UP": "PPage", "PAGE_DOWN": "NPage",
    "SPACE": "Space", "CTRL_C": "C-c", "CTRL_D": "C-d", "CTRL_Z": "C-z",
    "CTRL_A": "C-a", "CTRL_E": "C-e", "CTRL_L": "C-l", "CTRL_U": "C-u",
    "CTRL_K": "C-k", "CTRL_R": "C-r", "CTRL_W": "C-w",
    **{"F" + str(n): "F" + str(n) for n in range(1, 13)},
}


def _active_vt():
    value = Path("/sys/class/tty/tty0/active").read_text().strip()
    if re.fullmatch(r"tty[1-9][0-9]*", value) is None:
        raise ScreenControlError("SCREEN_ACTIVE_VT_INVALID")
    return value


def _proc(pid, *, tmux=False):
    p = Path("/proc") / str(int(pid))
    if p.stat().st_uid != os.getuid():
        raise ScreenControlError("SCREEN_PROCESS_OWNER_MISMATCH")
    raw = (p / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    if tmux and os.path.realpath(p / "exe") != os.path.realpath("/usr/bin/tmux"):
        raise ScreenControlError("SCREEN_TMUX_PROCESS_MISMATCH")
    return {"pid": int(pid), "start_ticks": int(fields[19]),
            "uid": p.stat().st_uid}


def _socket_identity(path):
    """Reject symlinks and paths outside the owned runtime directories."""
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise ScreenControlError("SCREEN_SOCKET_PATH_UNTRUSTED")
    roots = [Path("/run/user") / str(os.getuid()),
             Path("/tmp") / ("tmux-" + str(os.getuid()))]
    root = next((r for r in roots if p.is_relative_to(r)), None)
    if root is None:
        raise ScreenControlError("SCREEN_SOCKET_PATH_UNTRUSTED")
    current = root
    for part in ("", *p.relative_to(root).parts):
        if part:
            current /= part
        st = current.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            raise ScreenControlError("SCREEN_SOCKET_OWNER_MISMATCH")
        if current != p:
            if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o022:
                raise ScreenControlError("SCREEN_SOCKET_PARENT_UNTRUSTED")
        elif not stat.S_ISSOCK(st.st_mode):
            raise ScreenControlError("SCREEN_SOCKET_NOT_SOCKET")
    return {"path": str(p), "device": st.st_dev, "inode": st.st_ino,
            "uid": st.st_uid}


def _server_sockets():
    """Only probe sockets actually held by a same-UID tmux server process."""
    unix = {}
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and fields[7].startswith("/"):
            unix[fields[6]] = fields[7]
    found = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != os.getuid() or (p / "comm").read_text().strip() != "tmux: server":
                continue
            process = _proc(int(p.name), tmux=True)
            for fd in (p / "fd").iterdir():
                target = os.readlink(fd)
                match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
                if match and match[1] in unix:
                    try:
                        identity = _socket_identity(unix[match[1]])
                    except (OSError, ScreenControlError):
                        continue
                    found.append((identity, process))
            if _proc(int(p.name), tmux=True) != process:
                raise ScreenControlError("SCREEN_TMUX_SERVER_CHANGED")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    unique = {(s[0]["path"], s[1]["pid"]): s for s in found}
    if len(unique) > 32:
        raise ScreenControlError("SCREEN_TMUX_SERVER_LIMIT")
    return list(unique.values())


def _tmux(socket_path, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update({"LC_ALL": "C.UTF-8", "TERM": "xterm-256color"})
    try:
        result = subprocess.run(["/usr/bin/tmux", "-S", socket_path, *args],
                                capture_output=True, text=True, env=env, timeout=4)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScreenControlError("SCREEN_TMUX_COMMAND_FAILED") from exc
    if result.returncode or len(result.stdout.encode()) > 65536:
        raise ScreenControlError("SCREEN_TMUX_COMMAND_FAILED")
    return result.stdout


def _rows(value, size):
    out = []
    for line in value.splitlines():
        fields = line.split("|")
        if len(fields) != size:
            raise ScreenControlError("SCREEN_TMUX_METADATA_INVALID")
        out.append(fields)
    return out


def snapshot_binding():
    """Read-only snapshot of the active visible terminal; never fall back hidden."""
    vt = _active_vt()
    tty = "/dev/" + vt
    matches = []
    client_format = "|".join(["#{client_pid}", "#{client_tty}", "#{session_id}",
                                  "#{client_width}", "#{client_height}", "#{client_control_mode}"])
    pane_format = "|".join(["#{session_id}", "#{window_id}", "#{window_active}",
                                "#{pane_id}", "#{pane_pid}", "#{pane_tty}", "#{pane_active}",
                                "#{pane_left}", "#{pane_top}", "#{pane_width}", "#{pane_height}",
                                "#{window_width}", "#{window_height}", "#{pane_dead}",
                                "#{window_zoomed_flag}", "#{pane_in_mode}"])
    for socket_identity, server in _server_sockets():
        path = socket_identity["path"]
        try:
            clients = [c for c in _rows(_tmux(path, "list-clients", "-F", client_format), 6)
                       if c[1] == tty and c[5] == "0"]
            if not clients:
                continue
            panes = _rows(_tmux(path, "list-panes", "-a", "-F", pane_format), 16)
            for client in clients:
                visible = [p for p in panes if p[0] == client[2] and p[2] == "1"
                           and p[6] == "1" and p[13] == "0"]
                if len(visible) != 1:
                    raise ScreenControlError("SCREEN_VISIBLE_PANE_AMBIGUOUS")
                p = visible[0]
                binding = {"version": 1, "backend": "visible-tmux", "node": os.uname().nodename,
                           "uid": os.getuid(), "active_vt": vt, "client_tty": tty,
                           "socket": socket_identity, "server": server,
                           "client": _proc(int(client[0]), tmux=True),
                           "session_id": p[0], "window_id": p[1], "pane_id": p[3],
                           "pane": _proc(int(p[4])), "pane_tty": p[5],
                           "client_size": [int(client[3]), int(client[4])],
                           "window_size": [int(p[11]), int(p[12])],
                           "pane_geometry": [int(x) for x in p[7:11]],
                           "window_zoomed": p[14] == "1", "pane_in_mode": p[15] == "1"}
                if _socket_identity(path) != socket_identity or _proc(server["pid"], tmux=True) != server:
                    raise ScreenControlError("SCREEN_TMUX_SERVER_CHANGED")
                matches.append(binding)
        except ScreenControlError as exc:
            if exc.code == "SCREEN_TMUX_COMMAND_FAILED":
                continue
            raise
    if _active_vt() != vt:
        raise ScreenControlError("SCREEN_ACTIVE_VT_CHANGED")
    if not matches:
        raise ScreenControlError("SCREEN_VISIBLE_TMUX_NOT_FOUND")
    if len(matches) != 1:
        raise ScreenControlError("SCREEN_VISIBLE_CLIENT_AMBIGUOUS")
    return matches[0]


def validate_binding(expected):
    if not isinstance(expected, dict) or expected.get("backend") != "visible-tmux":
        raise ScreenControlError("SCREEN_BINDING_REQUIRED")
    current = snapshot_binding()
    if current != expected:
        raise ScreenControlError("SCREEN_BINDING_CHANGED")
    return current


def bind_screen(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("capture_id"), str) or not manifest["capture_id"]:
        raise ScreenControlError("SCREEN_CAPTURE_ID_REQUIRED")
    if manifest.get("source") != "linux-framebuffer" or manifest.get("screen_type") != "tty":
        raise ScreenControlError("SCREEN_CONTROL_MODE_UNSUPPORTED")
    binding = snapshot_binding()
    if manifest.get("active_vt") != binding["active_vt"]:
        raise ScreenControlError("SCREEN_CAPTURE_VT_MISMATCH")
    if manifest.get("node") != binding["node"] or manifest.get("uid") != binding["uid"]:
        raise ScreenControlError("SCREEN_CAPTURE_NODE_MISMATCH")
    return {**manifest, "binding": binding}


def _verify_capture(manifest, max_age_s):
    if not isinstance(manifest, dict) or not manifest.get("capture_id"):
        raise ScreenControlError("SCREEN_CAPTURE_ID_REQUIRED")
    if manifest.get("source") != "linux-framebuffer" or manifest.get("screen_type") != "tty":
        raise ScreenControlError("SCREEN_CONTROL_MODE_UNSUPPORTED")
    if manifest.get("synthetic") is not False or manifest.get("pixel_source_verified") is not True:
        raise ScreenControlError("SCREEN_REAL_CAPTURE_REQUIRED")
    if max_age_s is not None and (not isinstance(max_age_s, (int, float)) or not 0 < max_age_s <= 300):
        raise ScreenControlError("SCREEN_CAPTURE_AGE_LIMIT_INVALID")
    try:
        when = datetime.fromisoformat(manifest["completed_at"])
        if when.tzinfo is None:
            raise ValueError()
        age = (datetime.now(timezone.utc) - when).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise ScreenControlError("SCREEN_CAPTURE_TIME_INVALID") from None
    if age < -5 or (max_age_s is not None and age > max_age_s):
        raise ScreenControlError("SCREEN_CAPTURE_EXPIRED")
    path = Path(manifest.get("path", ""))
    expected = manifest.get("sha256", "")
    if not path.is_absolute() or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
        raise ScreenControlError("SCREEN_CAPTURE_FILE_INVALID")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 64 * 1024 * 1024:
                raise ScreenControlError("SCREEN_CAPTURE_FILE_UNTRUSTED")
            with os.fdopen(fd, "rb", closefd=False) as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if digest != expected:
                raise ScreenControlError("SCREEN_CAPTURE_SHA256_MISMATCH")
        finally:
            os.close(fd)
    except OSError as exc:
        raise ScreenControlError("SCREEN_CAPTURE_FILE_UNAVAILABLE") from exc


def act(capture_manifest, kind, *, text=None, key=None, max_age_s=None):
    """Send literal text (no controls/newline) or one explicit allowlisted key.

    The RPC caller must load this manifest by capture_id from its private store;
    it must not accept a replacement manifest supplied by a remote caller.
    Foreground changes are checked before and after dispatch. An independent
    local user's VT switch concurrent with the actual write cannot be made
    atomic with tmux; postflight failure therefore reports performed=True.
    """
    if kind in ("text", "text.type", "type"):
        if key is not None or not isinstance(text, str) or not text or len(text.encode()) > 16384:
            raise ScreenControlError("SCREEN_TEXT_INVALID")
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ScreenControlError("SCREEN_TEXT_CONTROL_CHARACTER")
        command = ["send-keys", "-l", "--", text]
        action = "text.type"
    elif kind in ("key", "key.send"):
        if text is not None or not isinstance(key, str) or key not in KEYS:
            raise ScreenControlError("SCREEN_KEY_INVALID")
        command = ["send-keys", KEYS[key]]
        action = "key.send"
    else:
        raise ScreenControlError("SCREEN_ACTION_UNSUPPORTED")
    _verify_capture(capture_manifest, max_age_s)
    expected = capture_manifest.get("binding")
    binding = validate_binding(expected)
    if binding.get("pane_in_mode"):
        raise ScreenControlError("SCREEN_TMUX_MODE_INPUT_UNSUPPORTED")
    if capture_manifest.get("active_vt") != binding["active_vt"]:
        raise ScreenControlError("SCREEN_CAPTURE_VT_MISMATCH")
    if capture_manifest.get("node") != binding["node"] or capture_manifest.get("uid") != binding["uid"]:
        raise ScreenControlError("SCREEN_CAPTURE_NODE_MISMATCH")
    # A second check immediately before dispatch prevents using a stale probe.
    binding = validate_binding(expected)
    path, pane = binding["socket"]["path"], binding["pane_id"]
    _tmux(path, command[0], "-t", pane, *command[1:])
    try:
        validate_binding(expected)
    except (ScreenControlError, OSError) as exc:
        raise ScreenControlError("SCREEN_CHANGED_AFTER_INPUT", performed=True,
                                 action=action, capture_id=capture_manifest["capture_id"]) from exc
    return {"ok": True, "performed": True, "action": action,
            "capture_id": capture_manifest["capture_id"], "binding": binding,
            "input_bytes": len(text.encode()) if action == "text.type" else None,
            "key": key if action == "key.send" else None,
            "input_target": "observed-visible-pane"}
