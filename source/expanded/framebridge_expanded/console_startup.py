"""Explicit, reversible console startup after an authenticated tty1 Bash login.

Only the caller's owned Bash startup files are changed. No login, sudo policy,
system service, desktop autostart, or authentication configuration is altered.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import tempfile
import uuid

STARTUP_NAMES = (".bash_profile", ".bash_login", ".profile")
BEGIN = b"# BEGIN FRAMEBRIDGE CONSOLE LOGIN v1 "
END = b"# END FRAMEBRIDGE CONSOLE LOGIN v1 "
FB_EXECUTABLE = "/usr/bin/fb"


class ConsoleStartupError(ValueError):
    pass


def _need(value, code):
    if not value:
        raise ConsoleStartupError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _absolute(value):
    try:
        return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    except (TypeError, ValueError):
        raise ConsoleStartupError("CONSOLE_LOGIN_PATH_INVALID") from None


def _no_symlink_parents(path):
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ConsoleStartupError("CONSOLE_LOGIN_SYMLINK_REFUSED")


def _paths(state, home):
    uid = os.getuid()
    _need(uid != 0 and os.geteuid() == uid, "CONSOLE_LOGIN_USER_REQUIRED")
    home = _absolute(home if home is not None else pwd.getpwuid(uid).pw_dir)
    state = _absolute(state)
    _no_symlink_parents(home)
    _no_symlink_parents(state)
    try:
        info = home.lstat()
    except OSError:
        raise ConsoleStartupError("CONSOLE_LOGIN_HOME_UNAVAILABLE") from None
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
          and not info.st_mode & 0o022, "CONSOLE_LOGIN_HOME_NOT_OWNED")
    if state.exists():
        _private_dir(state, create=False)
    return state, home


def _private_dir(path, create=True):
    _no_symlink_parents(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
          and stat.S_IMODE(info.st_mode) == 0o700,
          "CONSOLE_LOGIN_STATE_NOT_PRIVATE")


def _snapshot(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ConsoleStartupError("CONSOLE_LOGIN_SYMLINK_REFUSED") from None
        raise
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode), "CONSOLE_LOGIN_FILE_NOT_REGULAR")
        _need(info.st_uid == os.getuid(), "CONSOLE_LOGIN_FILE_NOT_OWNED")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            data = stream.read()
        return {"data": data, "sha256": _sha(data),
                "mode": stat.S_IMODE(info.st_mode), "gid": info.st_gid,
                "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns}
    finally:
        if fd is not None:
            os.close(fd)


def _startup_files(home):
    return {name: _snapshot(home / name) for name in STARTUP_NAMES}


def _effective(snapshots):
    return next((name for name in STARTUP_NAMES if snapshots[name] is not None), ".profile")


def _markers(snapshots):
    found = []
    for name, snapshot in snapshots.items():
        if snapshot is None:
            continue
        for line in snapshot["data"].splitlines():
            if line.startswith((b"# BEGIN FRAMEBRIDGE CONSOLE LOGIN",
                                b"# END FRAMEBRIDGE CONSOLE LOGIN")):
                found.append((name, line))
    return found


@contextmanager
def _home_lock(home, create):
    path = home / ".framebridge-console-login.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        yield
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ConsoleStartupError("CONSOLE_LOGIN_SYMLINK_REFUSED") from None
        raise
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
              and stat.S_IMODE(info.st_mode) == 0o600, "CONSOLE_LOGIN_LOCK_UNTRUSTED")
        fcntl.flock(fd, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def _atomic_write(path, data, mode=0o600, gid=None, new=False):
    fd, temporary = tempfile.mkstemp(prefix=".fb-console-login-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if gid is not None and os.fstat(stream.fileno()).st_gid != gid:
                os.fchown(stream.fileno(), -1, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if new:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _profile_id(state):
    return _sha(os.fsencode(str(state)))


def _read_manifest(state, home):
    directory = state / "console-login"
    path = directory / "manifest.json"
    if not path.exists() and not path.is_symlink():
        return None
    _private_dir(state, create=False)
    _private_dir(directory, create=False)
    snapshot = _snapshot(path)
    _need(snapshot is not None and snapshot["mode"] == 0o600,
          "CONSOLE_LOGIN_MANIFEST_UNTRUSTED")
    try:
        value = json.loads(snapshot["data"])
        valid = (type(value) is dict and value["schema"] == 1
                 and value["state"] == str(state) and value["home"] == str(home)
                 and value["profile_id"] == _profile_id(state)
                 and value["startup_name"] in STARTUP_NAMES
                 and value["phase"] in {"PREPARED", "ENABLED", "DISABLED"}
                 and type(value["existed_before"]) is bool
                 and type(value["before_mode"]) is int
                 and 0 <= value["before_mode"] <= 0o7777
                 and type(value["before_gid"]) is int
                 and value["before_gid"] >= 0
                 and re.fullmatch(r"[a-f0-9]{32}", value["transaction"]) is not None
                 and re.fullmatch(r"[a-f0-9]{64}", value["after_sha256"]) is not None)
        if value["existed_before"]:
            valid = (valid and re.fullmatch(r"[a-f0-9]{64}", value["before_sha256"]) is not None
                     and value["backup_name"] == "backup-" + value["transaction"] + ".bin")
        else:
            valid = valid and value["before_sha256"] is None and value["backup_name"] is None
        _need(valid, "CONSOLE_LOGIN_MANIFEST_INVALID")
        return value
    except (KeyError, TypeError, ValueError):
        raise ConsoleStartupError("CONSOLE_LOGIN_MANIFEST_INVALID") from None


def _write_manifest(directory, record):
    _atomic_write(directory / "manifest.json",
                  (json.dumps(record, indent=2) + "\n").encode())


def _known_original(snapshot, record):
    if not record["existed_before"]:
        return snapshot is None
    return (snapshot is not None and snapshot["sha256"] == record["before_sha256"]
            and snapshot["mode"] == record["before_mode"]
            and snapshot["gid"] == record["before_gid"])


def _known_installed(snapshot, record):
    return (snapshot is not None and snapshot["sha256"] == record["after_sha256"]
            and snapshot["mode"] == record["before_mode"]
            and snapshot["gid"] == record["before_gid"])


def _hook(state, profile_id):
    # Adjacent literals keep shell parameter expansion in the generated script.
    guard = (
        'if [ -n "$' '{BASH_VERSION-}" ] && (shopt -q login_shell 2>/dev/null) '
        '&& [ -z "$' '{SSH_TTY-}" ] && [ -z "$' '{TMUX-}" ] '
        '&& [ "$(tty 2>/dev/null)" = /dev/tty1 ] '
        '&& [ -x ' + shlex.quote(FB_EXECUTABLE) + ' ]; then\n'
    )
    return (BEGIN.decode() + profile_id + "\n" + guard
            + "    " + shlex.quote(FB_EXECUTABLE) + " --state-dir " + shlex.quote(str(state))
            + " console --mode tty || printf '%s\\n' "
            + "'FrameBridge console did not start; continuing this login.' >&2\n"
            + "fi\n" + END.decode() + profile_id + "\n").encode()


def _result(record, *, enabled, changed=False, **extra):
    return dict({"ok": True, "enabled": enabled, "changed": changed,
                 "startup_file": str(Path(record["home"]) / record["startup_name"]),
                 "profile_id": record["profile_id"], "phase": record["phase"],
                 "before_sha256": record["before_sha256"],
                 "after_sha256": record["after_sha256"]}, **extra)


def _enable(state, home):
    state, home = _paths(state, home)
    with _home_lock(home, create=True):
        snapshots = _startup_files(home)
        record = _read_manifest(state, home)
        if record and record["phase"] != "DISABLED":
            installed = snapshots[record["startup_name"]]
            if _known_installed(installed, record):
                _need(_effective(snapshots) == record["startup_name"],
                      "CONSOLE_LOGIN_STARTUP_PRIORITY_CHANGED")
                expected = [(record["startup_name"], BEGIN + record["profile_id"].encode()),
                            (record["startup_name"], END + record["profile_id"].encode())]
                _need(_markers(snapshots) == expected, "CONSOLE_LOGIN_FOREIGN_BLOCK")
                if record["phase"] == "PREPARED":
                    record["phase"] = "ENABLED"
                    _write_manifest(state / "console-login", record)
                return _result(record, enabled=True)
            _need(record["phase"] == "PREPARED" and _known_original(installed, record),
                  "CONSOLE_LOGIN_STARTUP_CHANGED")
            record["phase"] = "DISABLED"
            _write_manifest(state / "console-login", record)
        markers = _markers(snapshots)
        if markers:
            own = _profile_id(state).encode()
            code = ("CONSOLE_LOGIN_UNTRACKED_BLOCK"
                    if any(own in line for _, line in markers)
                    else "CONSOLE_LOGIN_OTHER_PROFILE_ENABLED")
            raise ConsoleStartupError(code)
        _private_dir(state)
        directory = state / "console-login"
        _private_dir(directory)
        if record:
            history = directory / ("history-" + record["transaction"] + ".json")
            _atomic_write(history, (json.dumps(record, indent=2) + "\n").encode(), new=True)
        name = _effective(snapshots)
        before = snapshots[name]
        data = before["data"] if before else b""
        profile_id = _profile_id(state)
        after = data + (b"\n" if data and not data.endswith(b"\n") else b"") + _hook(state, profile_id)
        transaction = uuid.uuid4().hex
        record = {
            "schema": 1, "transaction": transaction, "phase": "PREPARED",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "state": str(state), "home": str(home), "profile_id": profile_id,
            "startup_name": name, "existed_before": before is not None,
            "before_sha256": before["sha256"] if before else None,
            "before_mode": before["mode"] if before else 0o600,
            "before_gid": before["gid"] if before else os.getgid(),
            "before_atime_ns": before["atime_ns"] if before else None,
            "before_mtime_ns": before["mtime_ns"] if before else None,
            "backup_name": "backup-" + transaction + ".bin" if before else None,
            "after_sha256": _sha(after),
        }
        if before:
            _atomic_write(directory / record["backup_name"], data, new=True)
            backup = _snapshot(directory / record["backup_name"])
            _need(backup["sha256"] == record["before_sha256"], "CONSOLE_LOGIN_BACKUP_MISMATCH")
        _write_manifest(directory, record)
        _need(_known_original(_snapshot(home / name), record), "CONSOLE_LOGIN_STARTUP_CHANGED")
        _atomic_write(home / name, after, record["before_mode"], record["before_gid"], new=before is None)
        _need(_known_installed(_snapshot(home / name), record), "CONSOLE_LOGIN_WRITE_MISMATCH")
        record["phase"] = "ENABLED"
        _write_manifest(directory, record)
        return _result(record, enabled=True, changed=True)


def _disable(state, home):
    state, home = _paths(state, home)
    with _home_lock(home, create=True):
        record = _read_manifest(state, home)
        if record is None:
            return {"ok": True, "enabled": False, "changed": False, "phase": "NOT_CONFIGURED"}
        if record["phase"] == "DISABLED":
            return _result(record, enabled=False)
        path = home / record["startup_name"]
        current = _snapshot(path)
        directory = state / "console-login"
        if record["phase"] == "PREPARED" and _known_original(current, record):
            record["phase"] = "DISABLED"
            _write_manifest(directory, record)
            return _result(record, enabled=False)
        _need(_known_installed(current, record), "CONSOLE_LOGIN_STARTUP_CHANGED")
        if record["existed_before"]:
            backup = _snapshot(directory / record["backup_name"])
            _need(backup is not None and backup["mode"] == 0o600
                  and backup["sha256"] == record["before_sha256"],
                  "CONSOLE_LOGIN_BACKUP_MISMATCH")
            _need(_known_installed(_snapshot(path), record), "CONSOLE_LOGIN_STARTUP_CHANGED")
            _atomic_write(path, backup["data"], record["before_mode"], record["before_gid"])
            os.utime(path, ns=(record["before_atime_ns"], record["before_mtime_ns"]))
            _need(_known_original(_snapshot(path), record), "CONSOLE_LOGIN_RESTORE_MISMATCH")
        else:
            _need(_known_installed(_snapshot(path), record), "CONSOLE_LOGIN_STARTUP_CHANGED")
            path.unlink()
        record["phase"] = "DISABLED"
        _write_manifest(directory, record)
        return _result(record, enabled=False, changed=True)


def _status(state, home):
    state, home = _paths(state, home)
    with _home_lock(home, create=False):
        snapshots = _startup_files(home)
        record = _read_manifest(state, home)
        if record is None:
            return {"ok": True, "enabled": False, "changed": False,
                    "phase": "NOT_CONFIGURED", "startup_file": str(home / _effective(snapshots)),
                    "other_hook_present": bool(_markers(snapshots))}
        if record["phase"] == "DISABLED":
            return _result(record, enabled=False)
        current = snapshots[record["startup_name"]]
        if record["phase"] == "PREPARED" and _known_original(current, record):
            return _result(record, enabled=False, recovery_pending=True)
        _need(_known_installed(current, record), "CONSOLE_LOGIN_STARTUP_CHANGED")
        _need(_effective(snapshots) == record["startup_name"],
              "CONSOLE_LOGIN_STARTUP_PRIORITY_CHANGED")
        expected = [(record["startup_name"], BEGIN + record["profile_id"].encode()),
                    (record["startup_name"], END + record["profile_id"].encode())]
        _need(_markers(snapshots) == expected, "CONSOLE_LOGIN_FOREIGN_BLOCK")
        return _result(record, enabled=True, recovery_pending=record["phase"] == "PREPARED")


def _call(function, state, home):
    try:
        return function(state, home)
    except ConsoleStartupError as error:
        return {"ok": False, "error": str(error)}
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "error": "CONSOLE_LOGIN_IO_ERROR"}


def enable(state, *, home=None):
    return _call(_enable, state, home)


def disable(state, *, home=None):
    return _call(_disable, state, home)


def status(state, *, home=None):
    return _call(_status, state, home)
