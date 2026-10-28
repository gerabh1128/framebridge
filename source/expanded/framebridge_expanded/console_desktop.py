"""Migrate the recognized pre-package desktop console without replacing its Bash."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile

RECORD = "console-desktop.json"
LEGACY_WRAPPER_SHA256 = "71a6436d7411606312b009aee62a6d736a0d31a31194abb445f600740d8174f5"


def _need(value, error):
    if not value:
        raise ValueError(error)


def _owned(path, *, directory=False):
    path = Path(path)
    for parent in [path, *path.parents]:
        _need(not parent.is_symlink(), "CONSOLE_DESKTOP_SYMLINK_REFUSED")
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    _need(kind(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
          "CONSOLE_DESKTOP_PATH_NOT_OWNED")
    return info


def _snapshot(path):
    _owned(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
              and not info.st_mode & 0o022 and info.st_size <= 32768,
              "CONSOLE_DESKTOP_FILE_UNTRUSTED")
        return stream.read(), stat.S_IMODE(info.st_mode)


def _atomic(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".fb-desktop-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _legacy_entry(home):
    return ("[Desktop Entry]\nVersion=1.0\nType=Application\nName=FrameBridge Console\n"
            "Comment=Open the persistent local Bash and FBTERM activity console\n"
            "Exec=" + str(home / ".local/bin/framebridge-console") + "\n"
            "Icon=utilities-terminal\nTerminal=false\nCategories=System;TerminalEmulator;\n"
            "StartupWMClass=Gnome-terminal\n").encode()


def _new_entry(state):
    # Desktop Entry Exec quoting, without a shell or environment expansion.
    _need(not any(ord(char) < 32 or ord(char) == 127 for char in str(state)),
          "CONSOLE_DESKTOP_STATE_PATH_INVALID")
    quoted = str(state).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return ("[Desktop Entry]\nVersion=1.0\nType=Application\nName=FrameBridge Console\n"
            "Comment=Open the persistent local Bash and FBTERM activity console\n"
            'Exec=/usr/bin/fb --state-dir "' + quoted + '" console\n'
            "Icon=utilities-terminal\nTerminal=false\nCategories=System;TerminalEmulator;\n"
            "StartupWMClass=Gnome-terminal\n").encode()


def _legacy_identity(node, runtime):
    return {"socket": runtime / (node.lower() + "-tty1") / "console.sock",
            "session": node.upper() + "-FBTERM"}


def legacy_context(state, node, runtime):
    """Return a user-approved legacy binding; ordinary profiles remain isolated."""
    path = Path(state) / RECORD
    if not path.exists() and not path.is_symlink():
        return None
    state_info = _owned(Path(state), directory=True)
    _need(not state_info.st_mode & 0o077, "CONSOLE_DESKTOP_STATE_NOT_PRIVATE")
    raw, mode = _snapshot(path)
    _need(mode == 0o600, "CONSOLE_DESKTOP_RECORD_NOT_PRIVATE")
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("CONSOLE_DESKTOP_RECORD_INVALID") from None
    expected = _legacy_identity(node, Path(runtime))
    _need(type(record) is dict and record.get("schema") == 1
          and record.get("state") == str(state) and record.get("node") == node
          and record.get("socket") == str(expected["socket"])
          and record.get("session") == expected["session"], "CONSOLE_DESKTOP_RECORD_INVALID")
    return expected


def migrate_legacy(state, *, home=None, node=None, runtime=None):
    """Change only the exact known legacy entry; called under the profile owner's UID."""
    from .config import private_dir, state_path
    import socket
    uid = os.getuid()
    _need(uid != 0 and os.geteuid() == uid, "CONSOLE_DESKTOP_USER_REQUIRED")
    state = state_path(state)
    home = Path(home if home is not None else pwd.getpwuid(uid).pw_dir)
    node = node or re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname())[:64] or "NODE"
    runtime = Path(runtime if runtime is not None else Path("/run/user") / str(uid))
    desktop = home / ".local/share/applications/framebridge-console.desktop"
    if not desktop.exists() and not desktop.is_symlink():
        return {"ok": True, "changed": False, "reason": "NO_LEGACY_DESKTOP_ENTRY"}
    _owned(home, directory=True)
    _owned(desktop.parent, directory=True)
    before, mode = _snapshot(desktop)
    after = _new_entry(state)
    if before == after:
        _need(legacy_context(state, node, runtime) is not None, "CONSOLE_DESKTOP_RECORD_MISSING")
        return {"ok": True, "changed": False, "reason": "ALREADY_MIGRATED"}
    if before != _legacy_entry(home):
        return {"ok": True, "changed": False, "reason": "CUSTOM_DESKTOP_ENTRY_PRESERVED"}
    wrapper, _ = _snapshot(home / ".local/bin/framebridge-console")
    _need(hashlib.sha256(wrapper).hexdigest() == LEGACY_WRAPPER_SHA256,
          "CONSOLE_DESKTOP_LEGACY_WRAPPER_CHANGED")
    identity = _legacy_identity(node, runtime)
    parent = identity["socket"].parent
    _owned(runtime, directory=True)
    _owned(parent, directory=True)
    info = identity["socket"].lstat()
    _need(stat.S_ISSOCK(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077,
          "CONSOLE_DESKTOP_LEGACY_SOCKET_UNTRUSTED")
    result = subprocess.run(["/usr/bin/tmux", "-S", str(identity["socket"]), "list-windows",
                             "-t", "=" + identity["session"], "-F", "#{window_id}|#{window_name}"],
                            capture_output=True, text=True, timeout=5)
    rows = result.stdout.splitlines()
    _need(result.returncode == 0 and len([row for row in rows
          if re.fullmatch(r"@[0-9]+\|Console", row)]) == 1,
          "CONSOLE_DESKTOP_LEGACY_WINDOW_UNVERIFIED")
    window = next(row.split("|", 1)[0] for row in rows if re.fullmatch(r"@[0-9]+\|Console", row))
    panes = subprocess.run(["/usr/bin/tmux", "-S", str(identity["socket"]), "list-panes",
                            "-t", window, "-F", "#{pane_current_command}|#{pane_dead}|#{@framebridge_role}"],
                           capture_output=True, text=True, timeout=5)
    pane_rows = [row.split("|") for row in panes.stdout.splitlines()]
    _need(panes.returncode == 0 and len(pane_rows) == 2
          and all(len(row) == 3 and row[1] == "0" for row in pane_rows)
          and {row[2] for row in pane_rows} == {"shell", "monitor"}
          and any(row == ["bash", "0", "shell"] for row in pane_rows),
          "CONSOLE_DESKTOP_LEGACY_PANES_UNVERIFIED")
    private_dir(state)
    backup = state / "console-desktop-legacy.backup"
    if backup.exists() or backup.is_symlink():
        old, backup_mode = _snapshot(backup)
        _need(old == before and backup_mode == 0o600, "CONSOLE_DESKTOP_BACKUP_CHANGED")
    else:
        _atomic(backup, before)
    record = {"schema": 1, "state": str(state), "node": node,
              "socket": str(identity["socket"]), "session": identity["session"],
              "desktop_before_sha256": hashlib.sha256(before).hexdigest()}
    _atomic(state / RECORD, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
    _need(_snapshot(desktop)[0] == before, "CONSOLE_DESKTOP_CHANGED_DURING_MIGRATION")
    _atomic(desktop, after, mode)
    return {"ok": True, "changed": True, "desktop": str(desktop),
            "session": identity["session"], "socket": str(identity["socket"]),
            "backup": str(backup), "runtime_changed": False}
