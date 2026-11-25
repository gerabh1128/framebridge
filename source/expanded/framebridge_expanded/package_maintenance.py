"""Fail-closed, cold package replacement for installed FrameBridge.

The root-owned state file is also a stable flock inode. Installed runtime
participants hold shared leases; maintainer hooks take an exclusive lease.
The persistent marker bridges the separate preinst and postinst processes.
Legacy releases cannot participate: an operator must quiesce them before
upgrade; the process inventory is a refusal check, not a drain or proof of
absence of arbitrary detached effects.
"""
from __future__ import annotations
from contextlib import contextmanager
from functools import wraps
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

PREFIX = Path("/usr/lib/framebridge-expanded")
ROOT = Path("/var/lib/framebridge-package-maintenance")
LEGACY_PENDING = Path("/var/lib/framebridge-expanded/package-upgrade/pending.json")
READY = b"READY\n"
MAINTENANCE = b"MAINTENANCE\n"
MAX_PENDING = 2 * 1024 * 1024

class MaintenanceError(RuntimeError):
    pass

def need(condition, reason):
    if not condition:
        raise MaintenanceError(reason)

def validate_directory(info, owner, *, exact=False):
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner
         and not info.st_mode & 0o022
         and (not exact or stat.S_IMODE(info.st_mode) == 0o755),
         "PACKAGE_MAINTENANCE_DIRECTORY_UNTRUSTED")

@contextmanager
def lease(root=ROOT, owner=0, *, writer=False):
    """Only a maintainer may initialize the stable inode. Never unlink it."""
    root = Path(root)
    directory = fd = None
    try:
        try:
            validate_directory(root.parent.lstat(), owner)
            created_directory = False
            if writer:
                try:
                    root.mkdir(mode=0o755)
                    created_directory = True
                except FileExistsError:
                    pass
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            if created_directory:
                validate_directory(os.fstat(directory), owner)
                os.fchmod(directory, 0o755)
            validate_directory(os.fstat(directory), owner, exact=True)
            flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            created_file = False
            if writer:
                try:
                    fd = os.open("state", os.O_RDWR | os.O_CREAT | os.O_EXCL | flags,
                                 0o644, dir_fd=directory)
                    created_file = True
                    need(os.fstat(fd).st_uid == owner, "PACKAGE_MAINTENANCE_FILE_UNTRUSTED")
                    os.fchmod(fd, 0o644)
                except FileExistsError:
                    fd = os.open("state", os.O_RDWR | flags, dir_fd=directory)
            else:
                fd = os.open("state", os.O_RDONLY | flags, dir_fd=directory)
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == owner
                 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o644,
                 "PACKAGE_MAINTENANCE_FILE_UNTRUSTED")
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if writer else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MaintenanceError("PACKAGE_MAINTENANCE_BUSY: stop FrameBridge profiles normally before upgrading") from None
            current = os.stat("state", dir_fd=directory, follow_symlinks=False)
            need((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
                 "PACKAGE_MAINTENANCE_INODE_CHANGED")
            if created_file:
                os.fsync(directory)
        except OSError as exc:
            raise MaintenanceError("PACKAGE_MAINTENANCE_UNAVAILABLE: " + type(exc).__name__) from None
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)

def read_state(fd):
    return os.pread(fd, 64, 0)

def write_state(fd, value):
    need(value in (READY, MAINTENANCE), "PACKAGE_MAINTENANCE_INVALID_STATE")
    need(os.pwrite(fd, value, 0) == len(value), "PACKAGE_MAINTENANCE_SHORT_WRITE")
    os.ftruncate(fd, len(value))
    os.fsync(fd)

def role(arguments):
    """Classify only recognizable FrameBridge launch shapes, never emit argv."""
    for i, argument in enumerate(arguments[:10]):
        if argument == b"-m" and i + 1 < len(arguments):
            module = arguments[i+1]
            if module in {
                b"framebridge_expanded.runtime", b"framebridge_expanded.adapter",
                b"framebridge_expanded.autostart", b"framebridge_expanded.tty",
                b"framebridge_expanded.fleet", b"framebridge_expanded.viewer",
                b"framebridge_expanded.unified_viewer",
            }:
                # Stop requests/status and read-only probes remain usable.
                if module == b"framebridge_expanded.tty" and arguments[i+2:i+3] not in ([b"serve"], [b"start"]):
                    continue
                return "managed-component"
        if argument == b"-c" and i + 1 < len(arguments):
            code = arguments[i+1]
            if code.startswith((b"from framebridge_expanded.node import run;",
                                b"from framebridge_expanded.broker import serve;",
                                b"import sys;sys.path.insert(0,sys.argv[1]);from framebridge_expanded.connection_background import worker_main;")):
                return "managed-component"
        path = os.fsdecode(argument)
        if not path.startswith("/"):
            continue
        basename = path.rsplit("/", 1)[-1]
        installed = path.startswith(str(PREFIX) + "/")
        profile_engine = "/framebridge/fbterm/" in path and "/bin/" in path
        if (installed or profile_engine) and basename in {
                "fbterm_engine.py", "fbterm-shell", "fbterm-run",
                "framebridge_tool_adapter.py", "front_door_mcp.py",
                "fleet_hub_mcp_server.py", "tunnel-client",
                "framebridge-native-expanded"}:
            return "original-component"
        if installed and basename == "package_upgrade.py" and b"resume" in arguments[i+1:i+3]:
            return "legacy-upgrade-worker"
        if installed and basename == "framebridge-original" and b"start" in arguments[i+1:]:
            return "original-start"
    return None

def active_roles(proc_root=Path("/proc")):
    found = []
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            with (entry / "cmdline").open("rb") as stream:
                arguments = stream.read(65536).rstrip(b"\0").split(b"\0")
            component = role(arguments)
            if component:
                found.append(component)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            raise MaintenanceError("PACKAGE_MAINTENANCE_PROCESS_INVENTORY_UNAVAILABLE: " + type(exc).__name__) from None
    return found

def legacy_pending(path=LEGACY_PENDING, owner=0):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == owner
             and info.st_nlink == 1 and not info.st_mode & 0o077
             and info.st_size <= MAX_PENDING, "PACKAGE_MAINTENANCE_LEGACY_RECORD_UNTRUSTED")
        raw = os.read(fd, MAX_PENDING + 1)
        need(len(raw) <= MAX_PENDING, "PACKAGE_MAINTENANCE_LEGACY_RECORD_UNTRUSTED")
        value = json.loads(raw)
        need(isinstance(value, dict) and value.get("schema") == 1
             and value.get("phase") in {"COMPLETE","FAILED","CAPTURED","SCHEDULED","RUNNING"}
             and isinstance(value.get("profiles"), list),
             "PACKAGE_MAINTENANCE_LEGACY_RECORD_INVALID")
        need(value["phase"] not in {"SCHEDULED", "RUNNING"}
             and not (value["phase"] == "CAPTURED" and value["profiles"]),
             "PACKAGE_MAINTENANCE_LEGACY_TRANSACTION_PENDING")
    finally:
        os.close(fd)

def transition(operation, *, root=ROOT, owner=0, proc_root=Path("/proc"), pending=LEGACY_PENDING):
    need(operation in {"begin", "finish"}, "PACKAGE_MAINTENANCE_INVALID_OPERATION")
    with lease(root, owner, writer=True) as fd:
        try:
            blockers = active_roles(proc_root)
        except OSError as exc:
            raise MaintenanceError("PACKAGE_MAINTENANCE_PROCESS_INVENTORY_UNAVAILABLE: " + type(exc).__name__) from None
        need(not blockers, "PACKAGE_MAINTENANCE_ACTIVE_COMPONENTS: " + str(len(blockers))
             + "; stop FrameBridge profiles normally; do not update from FBEXEC")
        legacy_pending(pending, owner)
        if operation == "finish":
            need(read_state(fd) == MAINTENANCE, "PACKAGE_MAINTENANCE_BEGIN_REQUIRED")
        write_state(fd, MAINTENANCE if operation == "begin" else READY)

@contextmanager
def runtime_lease(*, root=ROOT, owner=0):
    with lease(root, owner) as fd:
        need(read_state(fd) == READY, "PACKAGE_MAINTENANCE_ACTIVE: complete the reviewed package transaction before starting FrameBridge")
        yield

def installed_runtime(prefix=None):
    # Resolve actual module location as well as the declared prefix. An
    # extracted development package must not touch the production gate.
    if Path(__file__).resolve().is_relative_to(PREFIX):
        return True
    return prefix is not None and Path(prefix).resolve() == PREFIX

@contextmanager
def runtime_guard(prefix=None):
    if installed_runtime(prefix):
        with runtime_lease():
            yield
    else:
        yield

def guarded(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with runtime_guard(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX")):
            return function(*args, **kwargs)
    return wrapper

def main(argv=None):
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        need(os.getuid() == os.geteuid() == 0, "PACKAGE_MAINTENANCE_ROOT_REQUIRED")
        need(len(args) == 1, "PACKAGE_MAINTENANCE_INVALID_OPERATION")
        transition(args[0])
        print("FRAMEBRIDGE_PACKAGE_MAINTENANCE=" + ("ACTIVE" if args[0] == "begin" else "READY"))
        return 0
    except (MaintenanceError, OSError, ValueError) as exc:
        print("FrameBridge package maintenance: " + str(exc), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
