"""Stop only this package's managed user services before Debian removal."""
from pathlib import Path
import hashlib
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys

# This module also runs as an isolated standalone package-removal script.
SYSTEM_PYTHONS = frozenset({"/usr/bin/python3", "/usr/bin/python3.12",
                            "/usr/bin/python3.13", "/usr/bin/python3.14"})
MODULE_ROOT = "/usr/lib/framebridge-expanded/expanded"
UNIT_NAME = re.compile(r"framebridge-autostart-[0-9a-f]{16}\.service\Z")


def managed_unit(path, uid):
    """Inspect a bounded owned regular file; never execute user configuration."""
    if not UNIT_NAME.fullmatch(path.name):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > 16384:
                return False
            raw = os.read(fd, 16385)
        finally:
            os.close(fd)
        lines = raw.decode("utf-8").splitlines()
        if not lines or lines[0] != "# FrameBridge managed autostart v1":
            return False
        if lines.count('Environment="PYTHONPATH=' + MODULE_ROOT + '"') != 1:
            return False
        starts = [line[10:] for line in lines if line.startswith("ExecStart=")]
        if len(starts) != 1:
            return False
        args = shlex.split(starts[0])
        if len(args) != 7 or args[0] not in SYSTEM_PYTHONS:
            return False
        if args[1:6] != ["-B", "-m", "framebridge_expanded.autostart", "_run", "--state"]:
            return False
        state = args[6].replace("%%", "%").replace("$$", "$")
        if not state.startswith("/") or any(ord(c) < 32 for c in state):
            return False
        expected = "framebridge-autostart-" + hashlib.sha256(state.encode()).hexdigest()[:16] + ".service"
        return path.name == expected
    except (OSError, UnicodeError, ValueError):
        return False


def cleanup_user(home=None, *, run=None, bus_available=None):
    run = run or subprocess.run
    home = Path(home) if home is not None else Path.home()
    uid = os.getuid()
    directory = home / ".config/systemd/user"
    if not directory.is_dir():
        return 0
    units = sorted(p.name for p in directory.glob("framebridge-autostart-*.service") if managed_unit(p, uid))
    if not units:
        return 0
    runtime = Path("/run/user") / str(uid)
    if bus_available is None:
        try:
            bus_available = stat.S_ISSOCK((runtime / "bus").stat().st_mode)
        except OSError:
            bus_available = False
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_CONFIG_HOME": str(home / ".config"), "XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus")}
    failure = False
    if bus_available:
        try:
            result = run(["/usr/bin/systemctl", "--user", "stop", *units], env=env, capture_output=True, timeout=35)
            if result.returncode:
                current = run(["/usr/bin/systemctl", "--user", "show", *units, "--property=ActiveState", "--value"], env=env, capture_output=True, timeout=5)
                values = current.stdout.decode().split() if isinstance(current.stdout, bytes) else current.stdout.split()
                failure = current.returncode != 0 or len(values) != len(units) or any(value not in {"inactive", "failed"} for value in values)
        except (OSError, subprocess.TimeoutExpired):
            failure = True
        try:
            result = run(["/usr/bin/systemctl", "--user", "--no-reload", "disable", *units], env=env, capture_output=True, timeout=10)
            failure = result.returncode != 0 or failure
        except (OSError, subprocess.TimeoutExpired):
            failure = True
    # Remove only enablement symlinks targeting these exact managed files.
    # This also works offline: systemctl can delegate file operations to a live
    # user manager with a different environment, and no bus exists at all for
    # an offline owner. Never delete profile data, ciphertext, or unit files.
    for group in directory.iterdir():
        if not group.name.endswith((".wants", ".requires")) or group.is_symlink() or not group.is_dir():
            continue
        for name in units:
            link = group / name
            if link.is_symlink():
                try:
                    if link.resolve() == (directory / name).resolve():
                        link.unlink()
                except OSError:
                    failure = True
    if bus_available:
        try:
            run(["/usr/bin/systemctl", "--user", "daemon-reload"], env=env, capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Units are stopped/disabled; an owner may have just logged out.
    if failure:
        raise RuntimeError("A managed FrameBridge service could not be stopped or disabled.")
    return len(units)


CREDENTIAL_HELPER = Path("/usr/lib/framebridge-expanded/bin/framebridge-credential-helper")
CREDENTIAL_HELPER_MODULE = Path(MODULE_ROOT) / "framebridge_expanded/credential_helper.py"


def _trusted_root_file(path):
    """Trust only fixed package paths with root-owned, non-writable ancestors."""
    for current in (path, *path.parents):
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
        if current == path:
            if not stat.S_ISREG(info.st_mode):
                return False
        elif not stat.S_ISDIR(info.st_mode):
            return False
    return True


def purge_credential_helpers(*, run=None):
    """Revoke only the installed helper's managed metadata during root removal."""
    if os.getuid() != 0:
        raise RuntimeError("Credential-helper cleanup requires package-manager root context.")
    # Old packages did not ship this helper. Never select code from user state.
    if not os.path.lexists(CREDENTIAL_HELPER):
        if os.path.lexists(CREDENTIAL_HELPER_MODULE):
            raise RuntimeError("Installed credential helper is incomplete; package removal was paused.")
        return
    try:
        if not _trusted_root_file(CREDENTIAL_HELPER) or not _trusted_root_file(CREDENTIAL_HELPER_MODULE):
            raise RuntimeError("Installed credential helper has unsafe ownership or permissions.")
        result = (run or subprocess.run)([str(CREDENTIAL_HELPER), "purge-all"],
                                         env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                         capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Credential-helper cleanup failed; package removal was paused.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Credential-helper cleanup failed; package removal was paused.") from exc


def cleanup_all_users(*, run=None, accounts=None):
    if os.getuid() != 0:
        raise RuntimeError("Package service cleanup requires the normal package manager's root context.")
    run = run or subprocess.run
    accounts = pwd.getpwall() if accounts is None else accounts
    seen = set()
    failed = False
    for account in accounts:
        identity = (account.pw_uid, account.pw_dir)
        if identity in seen or account.pw_uid == 65534 or not account.pw_dir.startswith("/") or account.pw_dir in {"/", "/nonexistent", "/dev/null"}:
            continue
        seen.add(identity)
        command = ["/usr/sbin/runuser", "-u", account.pw_name, "--", "/usr/bin/env", "-i", "HOME=" + account.pw_dir, "USER=" + account.pw_name, "LOGNAME=" + account.pw_name, "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "/usr/bin/python3", "-I", "-S", str(Path(__file__).resolve()), "--user"]
        try:
            result = run(command, capture_output=True, timeout=55)
            failed = result.returncode != 0 or failed
        except (OSError, subprocess.TimeoutExpired):
            failed = True
    if failed:
        raise RuntimeError("Some FrameBridge services could not be stopped; package removal was paused.")
    purge_credential_helpers(run=run)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["--all-users"]:
            cleanup_all_users()
        elif args == ["--user"]:
            cleanup_user()
        else:
            return 2
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print("FrameBridge service cleanup did not complete. Package data was retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
