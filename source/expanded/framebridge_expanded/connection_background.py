"""Profile-scoped detached ChatGPT supervision without a persistent credential."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import resource
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from .config import private_dir, state_path

_BOOTSTRAP = "import sys;sys.path.insert(0,sys.argv[1]);from framebridge_expanded.connection_background import worker_main;worker_main(sys.argv[2],int(sys.argv[3]))"
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
_STATES = {"starting", "checking", "access", "ready", "retrying", "rejected", "unverified", "error", "stopped", "closed"}
_RUNTIME_PARENT = Path("/run/user")
_HEARTBEAT_BACKEND = "user-runtime-v1"
_IDENTITY_FIELDS = ("pid", "uid", "start_ticks", "boot_id", "session_id", "process_group", "state_dir", "lock_fd")
_DURABLE_HEARTBEAT_INTERVAL = 5.0
_MESSAGES = {
    "starting": "[1/3] Starting the background tunnel client.",
    "checking": "[2/3] Verifying the local MCP service.",
    "access": "[3/3] Verifying tunnel access and authenticated polling.",
    "ready": "[OK] Secure tunnel connected; local MCP service verified.",
    "retrying": "[RETRYING] Waiting for current verified readiness; check the private connection log.",
    "rejected": "[ERROR] Tunnel access was rejected; check the runtime key and tunnel permissions.",
    "unverified": "[NOT READY] The connection is not verified yet; it continues retrying in the background.",
    "error": "[ERROR] Background tunnel stopped or could not be verified; check the private connection log.",
    "stopped": "[STOPPED] No background ChatGPT connection is running.",
    "closed": "[CLOSED] The background tunnel client stopped.",
}



def _directory(state, create=True):
    root = state_path(state)
    if create:
        private_dir(root)
        return private_dir(root / "connection")
    path = root / "connection"
    if not path.exists() and not path.is_symlink():
        return None
    for item in (root, path):
        st = item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError("The connection directory is not private and owned by this user.")
    return path


def _checked_fd(path, flags, create=False):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise RuntimeError("The connection state file is not a private regular file.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _heartbeat_directory(state, create=False):
    """Keep disposable liveness updates out of the durable profile filesystem.

    Do not create a login runtime directory, follow symlinks, change existing
    permissions, or trust an arbitrary XDG_RUNTIME_DIR supplied by a caller.
    Hosts without a user runtime directory retain bounded durable heartbeats.
    """
    root = _RUNTIME_PARENT / str(os.getuid())
    for parent in (_RUNTIME_PARENT.parent, _RUNTIME_PARENT):
        try:
            parent_info = parent.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid not in (0, os.getuid()) or stat.S_IMODE(parent_info.st_mode) & 0o022:
            raise RuntimeError("The user runtime parent directory is unsafe.")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("The user runtime directory is not private and owned by this user.")
    name = "framebridge-connection-" + hashlib.sha256(str(state_path(state)).encode()).hexdigest()
    directory = root / name
    if create:
        return private_dir(directory)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("The connection heartbeat directory is not private and owned by this user.")
    return directory


def _read(path):
    try:
        fd = _checked_fd(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    try:
        raw = os.read(fd, 32769)
        if len(raw) > 32768:
            raise RuntimeError("The connection status is too large.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise RuntimeError("The connection status cannot be read safely.") from None
    finally:
        os.close(fd)


def _write(path, data):
    if path.exists() or path.is_symlink():
        fd = _checked_fd(path, os.O_RDONLY)
        os.close(fd)
    fd, name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _supervisor_command(state, lock_fd):
    return [sys.executable, "-c", _BOOTSTRAP, _PACKAGE_PARENT, str(state_path(state)), str(lock_fd)]


def _identity(pid, state, lock_fd):
    proc = Path("/proc") / str(pid)
    fields = (proc / "stat").read_text().split(") ", 1)[1].split()
    return {"pid": pid, "uid": proc.stat().st_uid, "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "session_id": int(fields[3]), "process_group": int(fields[2]),
            "state_dir": str(state_path(state)), "lock_fd": lock_fd}


def _valid_identity(record, state):
    try:
        pid = record["pid"]
        lock_fd = record["lock_fd"]
        if type(pid) is not int or pid < 2 or type(lock_fd) is not int or lock_fd < 3:
            return False
        current = _identity(pid, state, lock_fd)
        if any(current[k] != record.get(k) for k in current):
            return False
        if current["uid"] != os.getuid() or current["session_id"] != pid or current["process_group"] != pid:
            return False
        proc = Path("/proc") / str(pid)
        if (proc / "stat").read_text().split(") ", 1)[1].split()[0] == "Z":
            return False
        if os.path.realpath(proc / "exe") != os.path.realpath(sys.executable):
            return False
        argv = (proc / "cmdline").read_bytes().split(b"\0")[:-1]
        if argv != [os.fsencode(x) for x in _supervisor_command(state, lock_fd)]:
            return False
        lock = _directory(state, False) / "owner.lock"
        return os.stat(proc / "fd" / str(lock_fd)).st_ino == lock.stat().st_ino and os.stat(proc / "fd" / str(lock_fd)).st_dev == lock.stat().st_dev
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def status(state):
    directory = _directory(state, False)
    record = _read(directory / "status.json") if directory else {}
    alive = bool(record) and _valid_identity(record, state)
    phase = record.get("state", "stopped")
    if not isinstance(phase, str) or phase not in _STATES:
        phase = "error"
    if not alive and phase not in {"error", "closed", "stopped"}:
        phase = "stopped"
    checked = record.get("verified_at")
    if record.get("heartbeat_backend") == _HEARTBEAT_BACKEND:
        heartbeat_directory = _heartbeat_directory(state)
        heartbeat = _read(heartbeat_directory / "status.json") if heartbeat_directory else {}
        same_worker = all(heartbeat.get(key) == record.get(key) for key in _IDENTITY_FIELDS)
        checked = heartbeat.get("verified_at") if same_worker and heartbeat.get("state") == phase else None
    verified = alive and phase == "ready" and type(checked) in (int, float) and 0 <= time.time() - checked <= 15
    if alive and phase == "ready" and not verified:
        phase = "retrying"
    return {"ok": True, "alive": alive, "verified": verified, "state": phase,
            "message": _MESSAGES[phase],
            "pid": record.get("pid") if alive else None,
            "log": str(state_path(state) / "logs/chatgpt/connection.log")}


def disconnect(state, *, timeout=16):
    directory = _directory(state, False)
    record = _read(directory / "status.json") if directory else {}
    if not record or not _valid_identity(record, state):
        return {"ok": True, "alive": False, "state": "stopped", "message": "No owned background tunnel is running."}
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return {"ok": False, "alive": True, "state": "error", "message": "Safe process signaling is unavailable; the connection was left unchanged."}
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return {"ok": True, "alive": False, "state": "stopped"}
    try:
        if not _valid_identity(record, state):
            return {"ok": False, "alive": False, "state": "error", "message": "Connection identity changed; no signal was sent."}
        try:
            signal.pidfd_send_signal(fd, signal.SIGINT)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _valid_identity(record, state):
                return {"ok": True, "alive": False, "state": "stopped", "message": "Background ChatGPT connection stopped."}
            time.sleep(0.1)
        return {"ok": False, "alive": True, "state": "error", "message": "Connection cleanup is still running; local services were left unchanged."}
    finally:
        os.close(fd)


class _StatusOutput:
    """Progress destination owned by the worker, with no parent pipe or terminal."""
    def __init__(self, path, identity, heartbeat_path=None):
        self.path = path
        self.record = dict(identity)
        self.heartbeat_path = heartbeat_path
        if heartbeat_path is not None:
            self.record["heartbeat_backend"] = _HEARTBEAT_BACKEND
        self.last_observed = None
        self.last_durable_write = 0.0
        self.pending = ""
        self.change("starting", "[1/3] Starting the background tunnel client.")

    def isatty(self):
        return False

    def flush(self):
        pass

    def change(self, state, message):
        if self.record.get("state") == state and self.record.get("message") == message:
            return
        self.record.update(state=state, message=message, updated_at=time.time())
        _write(self.path, self.record)
        self.last_durable_write = time.monotonic()

    def observe(self, state):
        self.record["checked_at"] = time.time()
        self.record["verified_at"] = time.time() if state == "ready" else None
        if self.heartbeat_path is not None:
            heartbeat = {key: self.record[key] for key in _IDENTITY_FIELDS}
            heartbeat.update(state=state, checked_at=self.record["checked_at"], verified_at=self.record["verified_at"])
            _write(self.heartbeat_path, heartbeat)
        elif state != self.last_observed or time.monotonic() - self.last_durable_write >= _DURABLE_HEARTBEAT_INTERVAL:
            # Legacy/manual sessions can run without systemd's user runtime
            # directory. Preserve the 15-second freshness contract there too.
            _write(self.path, self.record)
            self.last_durable_write = time.monotonic()
        self.last_observed = state

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            phase = next((phase for token, phase in [("[1/3]", "starting"), ("[2/3]", "checking"), ("[3/3]", "access"), ("[OK]", "ready"), ("[RETRYING]", "retrying"), ("[NOT READY]", "unverified"), ("[ERROR]", "error"), ("[STOPPED]", "stopped"), ("[CLOSED]", "closed")] if line.startswith(token)), None)
            if phase:
                self.change(phase, line)
        if len(self.pending) > 4096:
            self.pending = ""
        return len(text)


def worker_main(state, lock_fd):
    """Read one bounded anonymous-pipe payload; the credential is never a file."""
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    payload = credential = None
    sink = None
    rc = 1
    try:
        directory = _directory(state)
        held = os.fstat(lock_fd)
        owned = (directory / "owner.lock").stat()
        if (held.st_dev, held.st_ino) != (owned.st_dev, owned.st_ino):
            return
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            return
        payload = json.loads(raw)
        raw = None
        credential = payload.pop("credential")
        command = payload["command"]
        if not isinstance(credential, str) or not credential or any(c.isspace() for c in credential) or len(credential) > 16384:
            return
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or "\0" in v for v in command):
            return
        heartbeat_directory = _heartbeat_directory(state, create=True)
        heartbeat_path = heartbeat_directory / "status.json" if heartbeat_directory else None
        sink = _StatusOutput(directory / "status.json", _identity(os.getpid(), state, lock_fd), heartbeat_path)
        from .tunnel import run_foreground
        rc = run_foreground(command, credential, state=state, output=sink, observe=sink.observe)
    except BaseException:
        rc = 1
    finally:
        credential = payload = None
        if sink is not None:
            try:
                sink.change("stopped" if rc in (0, 130) else "error", "[STOPPED] Background ChatGPT connection stopped." if rc in (0, 130) else "[ERROR] Background tunnel stopped; check the private connection log.")
            except Exception:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def start(command, credential, state, expected_tunnel=None, expected_mcp=None, *, output=None, wait_timeout=95):
    """Detach the owned supervisor, then wait for verified readiness or a clear result."""
    output = output or sys.stdout
    directory = _directory(state)
    lock_fd = _checked_fd(directory / "owner.lock", os.O_RDWR, create=True)
    child = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current = status(state)
            output.write("A ChatGPT connection already owns this profile. Use framebridge connection status.\n")
            return 0 if current["verified"] else 1
        # No earlier status may be mistaken for this new supervisor.
        old = directory / "status.json"
        if old.exists() or old.is_symlink():
            check = _checked_fd(old, os.O_RDONLY)
            os.close(check)
            old.unlink()
        env = dict(os.environ)
        from .tunnel import KEY_ENV, _RUNTIME_PREFIXES, _RUNTIME_NAMES
        for name in list(env):
            if name.startswith(_RUNTIME_PREFIXES) or name in _RUNTIME_NAMES:
                env.pop(name, None)
        child = subprocess.Popen(_supervisor_command(state, lock_fd), stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 pass_fds=(lock_fd,), close_fds=True, start_new_session=True, env=env)
        threading.Thread(target=child.wait, daemon=True, name="framebridge-tunnel-reaper").start()
        try:
            payload = json.dumps({"credential": credential, "command": list(command)}).encode()
            if len(payload) > 65536:
                raise ValueError("The connection input is too large.")
            child.stdin.write(payload)
            child.stdin.close()
            payload = None
        except (OSError, ValueError):
            child.stdin.close()
            output.write("[ERROR] The background connection could not receive its private input.\n")
            return 1
        finally:
            credential = None
        # Closing, rather than LOCK_UN, preserves the child's inherited flock.
        os.close(lock_fd)
        lock_fd = None
        deadline = time.monotonic() + wait_timeout
        last = None
        while time.monotonic() < deadline:
            current = status(state)
            if current["alive"] and current["message"] != last:
                output.write(current["message"] + "\n")
                output.flush()
                last = current["message"]
            if current["verified"]:
                output.write("The connection is running in the background; this terminal can be closed.\n")
                output.write("Use framebridge connection status or framebridge disconnect chatgpt.\n")
                output.write("Select this tunnel in ChatGPT. A successful tool call verifies that connection.\n")
                return 0
            if child.poll() is not None:
                output.write("[ERROR] The background tunnel stopped before verified readiness. Check the private connection log.\n")
                return 1
            time.sleep(0.2)
        output.write("[NOT READY] Verification is still pending. The connection continues retrying in the background; use framebridge connection status.\n")
        return 1
    except KeyboardInterrupt:
        if child is not None:
            result = disconnect(state)
            # The Popen child remains ours even before it has published status.
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=16)
                except (OSError, subprocess.TimeoutExpired):
                    result = {"ok": False}
            if not result["ok"]:
                output.write("[ERROR] Check framebridge connection status before reconnecting.\n")
        return 130
    finally:
        credential = None
        if lock_fd is not None:
            os.close(lock_fd)


def run_foreground(command, credential, state, *, output=None):
    """Retain explicit diagnostic mode, exclusive with detached connections."""
    directory = _directory(state)
    fd = _checked_fd(directory / "owner.lock", os.O_RDWR, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A ChatGPT connection already owns this profile. Disconnect it before using foreground mode.") from None
        from .tunnel import run_foreground as run
        return run(command, credential, state=state, output=output)
    finally:
        os.close(fd)
