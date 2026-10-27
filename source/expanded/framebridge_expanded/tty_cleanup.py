"""Private evidence for one TTY daemon's cleanup; never authorizes process kills."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import stat
import tempfile

NAME = "tty-cleanup.json"


class CleanupUnverified(RuntimeError):
    pass


def current_boot():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def daemon_identity(record):
    return {"pid": record["pid"], "birth": record["birth"], "uid": record["uid"],
            "boot": record.get("boot", current_boot())}


def _session_valid(record):
    keys = {"session_id", "pid", "birth", "server_pid", "server_birth", "unit_name",
            "control_group", "pane_control_group"}
    if type(record) is not dict or set(record) != keys:
        return False
    if (type(record["session_id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", record["session_id"])
            or any(type(record[key]) is not int or record[key] <= 0 for key in ("pid", "server_pid"))
            or any(type(record[key]) is not str or not record[key].isdigit() for key in ("birth", "server_birth"))
            or type(record["unit_name"]) is not str
            or not re.fullmatch(r"framebridge-tty-[a-f0-9]{12}-" + record["session_id"] + r"-[a-f0-9]{8}\.service", record["unit_name"])):
        return False
    for key in ("control_group", "pane_control_group"):
        group = record[key]
        if type(group) is not str or not group.startswith("/") or ".." in Path(group).parts or "\0" in group:
            return False
    server = Path(record["control_group"])
    pane = Path(record["pane_control_group"])
    return (server.name == record["unit_name"] and (pane == server or (
        pane.parent == server.parent and re.fullmatch(r"tmux-spawn-[a-f0-9-]{36}\.scope", pane.name))))


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1048576):
            raise CleanupUnverified()
        return json.load(stream)


def read(state):
    try:
        value = read_private(Path(state) / NAME)
        daemon = value["daemon"]
        if (type(value) is not dict or value.get("schema") != 1
                or value.get("state_dir") != str(Path(state).absolute())
                or type(value.get("cleanup_verified")) is not bool
                or type(value.get("sessions")) is not list
                or type(value.get("errors")) is not list
                or type(value.get("operation_pending")) is not bool
                or set(daemon) != {"pid", "birth", "uid", "boot"}
                or type(daemon["pid"]) is not int or daemon["pid"] <= 0
                or type(daemon["birth"]) is not str or not daemon["birth"].isdigit()
                or daemon["uid"] != os.geteuid()
                or type(daemon["boot"]) is not str
                or not re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", daemon["boot"])
                or any(not _session_valid(record) for record in value["sessions"])
                or (value["cleanup_verified"] and (value["sessions"] or value["errors"]
                                                   or value["operation_pending"]))):
            raise CleanupUnverified()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise CleanupUnverified() from None


def write(state, daemon, sessions, *, verified=False, errors=(), operation_pending=False):
    state = Path(state)
    value = {"schema": 1, "state_dir": str(state.absolute()), "daemon": daemon,
             "cleanup_verified": verified, "sessions": sessions,
             "errors": list(errors), "operation_pending": operation_pending}
    fd, name = tempfile.mkstemp(prefix=".tty-cleanup-", dir=state)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.write("\n")
        os.replace(name, state / NAME)
    finally:
        Path(name).unlink(missing_ok=True)


def _gone(pid, birth, uid):
    if type(pid) is not int or pid <= 0 or type(birth) is not str or not birth.isdigit():
        return False
    try:
        path = Path("/proc") / str(pid)
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] != birth or (path.stat().st_uid == uid and fields[0] in {"Z", "X"})
    except FileNotFoundError:
        return True
    except (OSError, IndexError):
        return False


def require_prior_cleanup(state):
    """Permit restart only after prior evidence is clean or identities are gone.

    Persisted identities are read-only evidence, never targets for a later kill.
    """
    state = Path(state)
    if not (state / NAME).exists() and not (state / NAME).is_symlink():
        if (state / "tty-process.json").exists() or (state / "tty-process.json").is_symlink():
            raise CleanupUnverified()
        return
    value = read(state)
    # A different kernel boot cannot retain any process or user cgroup from
    # this daemon. Do not compare old PID numbers with this boot's processes.
    if value["daemon"]["boot"] != current_boot() or value["cleanup_verified"]:
        return
    daemon = value["daemon"]
    if value["operation_pending"] or not _gone(daemon["pid"], daemon["birth"], daemon["uid"]):
        raise CleanupUnverified()
    for record in value["sessions"]:
        try:
            if not _gone(record["pid"], record["birth"], daemon["uid"]):
                raise CleanupUnverified()
            if not _gone(record["server_pid"], record["server_birth"], daemon["uid"]):
                raise CleanupUnverified()
            for group in {record["control_group"], record["pane_control_group"]}:
                if not group.startswith("/") or ".." in Path(group).parts:
                    raise CleanupUnverified()
                root = Path("/sys/fs/cgroup") / group.lstrip("/")
                if root.exists():
                    files = [root / "cgroup.procs", *root.glob("**/cgroup.procs")]
                    if any(path.read_text().strip() for path in files):
                        raise CleanupUnverified()
        except (OSError, KeyError, TypeError, AttributeError):
            raise CleanupUnverified() from None
