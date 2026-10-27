"""Portable access to the captured real FrameBridge tmux CUA-TTY engine.

The captured Manager/LocalServer are loaded from original/gerameg, never from
an installed ALPHA. _tty_backend.py preserves RealTerminalMixin with a recorded
portability diff. The public RPC uses same-UID sockets plus authorized=True.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass, fields
import fcntl
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib

_SESSION = re.compile(r"[a-f0-9]{32}\Z")
_ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_WIRE_CHARS = 1800
_STARTED_CHILDREN = {}


def _source_root():
    here = Path(__file__).resolve().parents[2]
    prefix = Path(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX", str(here)))
    for p in (prefix / "original/gerameg", prefix / "sources/original-gerameg",
              here / "original/gerameg", here / "sources/original-gerameg"):
        if (p / "framebridge_cua_tty/core.py").is_file():
            return p
    raise RuntimeError("TTY_ORIGINAL_SOURCE_NOT_FOUND")


def _load_captured():
    name = "framebridge_expanded._captured_cua_tty"
    if name not in sys.modules:
        path = _source_root() / "framebridge_cua_tty"
        spec = importlib.util.spec_from_file_location(
            name, path / "__init__.py", submodule_search_locations=[str(path)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return (importlib.import_module(name + ".core"),
            importlib.import_module(name + ".server"))


_core, _server = _load_captured()
TerminalDenied = _core.TerminalDenied
from ._tty_backend import RealTerminalMixin
from .tty_sudo_control import SudoControlError
from . import tty_cleanup


@dataclass(frozen=True)
class Limits:
    max_sessions: int = 16
    min_rows: int = 2
    max_rows: int = 500
    min_cols: int = 10
    max_cols: int = 1000
    max_text_bytes: int = 65536
    max_screen_bytes: int = 262144
    history_lines: int = 10000
    idle_timeout_s: int = 0
    command_timeout_s: int = 5
    rpc_max_bytes: int = 1048576


def _private(path):
    path = Path(path)
    if path.is_symlink():
        raise TerminalDenied("TTY_PRIVATE_PATH_IS_SYMLINK")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.stat()
    if s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise TerminalDenied("TTY_PRIVATE_DIRECTORY_REQUIRED")
    return path


def state_path(value=None):
    return Path(value or os.environ.get("FRAMEBRIDGE_EXPANDED_STATE",
                str(Path.home() / ".local/state/framebridge-expanded"))).expanduser().absolute()


def runtime_path(state):
    state = state_path(state)
    tag = hashlib.sha256(str(state).encode()).hexdigest()[:16]
    base = Path("/run/user") / str(os.geteuid())
    try:
        safe = base.is_dir() and base.stat().st_uid == os.geteuid() and not base.is_symlink()
    except OSError:
        safe = False
    if not safe:
        base = Path("/tmp")
    return base / ("fbe-tty-" + str(os.geteuid()) + "-" + tag)


def _config(state):
    path = state / "tty.json"
    data = {}
    if path.exists():
        if path.is_symlink() or path.stat().st_uid != os.geteuid():
            raise TerminalDenied("TTY_CONFIG_OWNER_INVALID")
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            raise TerminalDenied("TTY_CONFIG_INVALID") from None
    if type(data) is not dict or set(data) - {"limits", "cwd"}:
        raise TerminalDenied("TTY_CONFIG_INVALID")
    values = data.get("limits", {})
    allowed = {f.name for f in fields(Limits)}
    if type(values) is not dict or set(values) - allowed:
        raise TerminalDenied("TTY_LIMITS_INVALID")
    limits = Limits(**values)
    if any(type(getattr(limits, f.name)) is not int for f in fields(Limits)):
        raise TerminalDenied("TTY_LIMITS_INVALID")
    if not (1 <= limits.max_sessions <= 4096 and
            1 <= limits.min_rows <= limits.max_rows <= 65535 and
            1 <= limits.min_cols <= limits.max_cols <= 65535 and
            1 <= limits.max_text_bytes <= limits.rpc_max_bytes // 2 and
            1024 <= limits.max_screen_bytes <= limits.rpc_max_bytes - 4096 and
            0 <= limits.history_lines <= 1000000 and
            0 <= limits.idle_timeout_s and 1 <= limits.command_timeout_s <= 300 and
            65536 <= limits.rpc_max_bytes <= 16777216):
        raise TerminalDenied("TTY_LIMITS_INVALID")
    cwd = Path(data.get("cwd", str(state / "home"))).expanduser().absolute()
    if not cwd.exists():
        cwd.mkdir(mode=0o700, parents=True)
    if not cwd.is_dir():
        raise TerminalDenied("TTY_CWD_INVALID")
    return limits, cwd


def _identity(pid):
    try:
        p = Path("/proc") / str(pid)
        parts = (p / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": int(pid), "birth": parts[19], "uid": p.stat().st_uid,
                "state": parts[0]}
    except (OSError, ValueError, IndexError):
        return None


def _alive(record):
    now = _identity(record.get("pid", 0))
    return bool(now and now["birth"] == record.get("birth")
                and now["uid"] == os.geteuid() and now["state"] != "Z")


def _write_json(path, value):
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def _safe_error(exc):
    name = str(exc.args[0]) if getattr(exc, "args", ()) else ""
    if name.startswith("OWNER_"):
        return "TTY_INPUT_CONTEXT_INVALID"
    return name if _ERROR.fullmatch(name) else "TTY_INTERNAL_ERROR"


class PortableManager(RealTerminalMixin, _core.Manager):
    """The operative real-terminal mixin with portable runtime configuration."""
    def __init__(self, state, limits, home):
        self.profile = state
        self.limits = limits
        self.home = home
        self.tmux_socket = runtime_path(state) / "tmux.sock"
        self.tmux_bin = shutil.which("tmux")
        if not self.tmux_bin:
            raise TerminalDenied("TMUX_NOT_AVAILABLE")
        # The original constructor initializes its lock and bookkeeping.
        # Session policy is supplied by this profile, not its old staging cap.
        super().__init__(owner_verifier=self._local_context, max_sessions=2,
                         idle_timeout_s=600)
        self.max_sessions = limits.max_sessions
        self._last_used = {}
        self.cleanup_daemon = None

    @staticmethod
    def _local_context(context):
        return (type(context) is dict and context.get("program") == "bash"
                and context.get("operation") in {"text.type", "key.send"}
                and type(context.get("session_id")) is str
                and _SESSION.fullmatch(context["session_id"]) is not None
                and type(context.get("payload_sha256")) is str
                and re.fullmatch(r"[a-f0-9]{64}", context["payload_sha256"]) is not None
                and context.get("owner_assertion") == {})

    def _size(self, rows, cols):
        if (type(rows) is not int or type(cols) is not int or
                not self.limits.min_rows <= rows <= self.limits.max_rows or
                not self.limits.min_cols <= cols <= self.limits.max_cols):
            raise TerminalDenied("SIZE_INVALID")

    def open(self, program="bash", rows=24, cols=80):
        if program != "bash":
            raise TerminalDenied("PROGRAM_NOT_SUPPORTED")
        self._size(rows, cols)
        self._record_cleanup(operation_pending=True)
        try:
            result = super().open("bash", rows, cols)
            self._last_used[result["session_id"]] = time.monotonic()
            return result
        finally:
            self._record_cleanup()

    def reap(self):
        super().reap()
        if self.limits.idle_timeout_s:
            now = time.monotonic()
            for sid, last in list(self._last_used.items()):
                if now - last > self.limits.idle_timeout_s and sid in self._real:
                    try:
                        super().abort(sid)
                    finally:
                        self._last_used.pop(sid, None)

    def touch(self, sid):
        if sid not in self._real:
            raise TerminalDenied("SESSION_NOT_FOUND")
        self._last_used[sid] = time.monotonic()

    def abort(self, sid):
        if sid not in self._real:
            raise TerminalDenied("SESSION_NOT_FOUND")
        result = super().abort(sid)
        self._last_used.pop(sid, None)
        self._record_cleanup()
        return result

    def sudo(self, sid, mode):
        if mode == "status":
            return super().sudo(sid, mode)
        self._record_cleanup(operation_pending=True)
        try:
            return super().sudo(sid, mode)
        finally:
            self._record_cleanup()

    def _cleanup_records(self):
        keys = ("session_id", "pid", "birth", "server_pid", "server_birth",
                "unit_name", "control_group", "pane_control_group")
        records = []
        source = {}
        if self._real:
            source.update({sid: session._sudo_record for sid, session in self._real.items()})
        controller = getattr(self, "_sudo", None)
        if controller is not None:
            source.update(controller._records)
        for record in source.values():
            records.append({key: record.get(key) for key in keys})
            if record.get("pending_old_record"):
                records.append({key: record["pending_old_record"].get(key) for key in keys})
        return records

    def _record_cleanup(self, *, verified=False, errors=(), operation_pending=False):
        if self.cleanup_daemon is not None:
            tty_cleanup.write(self.profile, self.cleanup_daemon, self._cleanup_records(),
                              verified=verified, errors=errors,
                              operation_pending=operation_pending or self._cleanup_uncertain())

    def _cleanup_uncertain(self):
        controller = getattr(self, "_sudo", None)
        return bool(controller is not None and controller._uncertain_launches)

    def shutdown(self):
        with self._lock:
            outcome = {"cleanup_verified": False, "errors": [{"error": "TTY_INTERNAL_ERROR"}]}
            try:
                outcome = super().shutdown()
                if self._cleanup_records() or self._cleanup_uncertain():
                    outcome["cleanup_verified"] = False
                    if not outcome["errors"]:
                        outcome["errors"] = [{"error": "TTY_CLEANUP_PENDING"}]
            finally:
                self._record_cleanup(verified=outcome["cleanup_verified"], errors=outcome["errors"])
            return outcome

    def status(self):
        result = super().status()
        for key in ("owner_device_connected", "owner_signature_required"):
            result.pop(key, None)
        result.update({"input_policy": "same-uid-authorized",
                       "input_enabled": True,
                       "limits": asdict(self.limits),
                       "real_idle_timeout_s": self.limits.idle_timeout_s or None})
        return result


class PortableHandler(socketserver.StreamRequestHandler):
    def send(self, value):
        raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > self.server.limits.rpc_max_bytes:
            raw = b'{"ok":false,"error":"TTY_RESPONSE_TOO_LARGE"}\n'
        self.wfile.write(raw)

    def handle(self):
        try:
            self.request.settimeout(self.server.limits.command_timeout_s + 5)
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server.owner_uid:
                self.send({"ok": False, "error": "SAME_UID_REQUIRED"})
                return
            raw = self.rfile.readline(self.server.limits.rpc_max_bytes + 1)
            if len(raw) > self.server.limits.rpc_max_bytes or not raw.endswith(b"\n"):
                raise TerminalDenied("TTY_REQUEST_TOO_LARGE_OR_TRUNCATED")
            payload = json.loads(raw)
            result = dispatch(self.server.manager, payload)
            self.send(result)
        except (TerminalDenied, SudoControlError, KeyError) as exc:
            self.send({"ok": False, "error": _safe_error(exc)})
        except (ValueError, TypeError, UnicodeError):
            self.send({"ok": False, "error": "TTY_REQUEST_INVALID"})
        except Exception:
            self.send({"ok": False, "error": "TTY_INTERNAL_ERROR"})


class PortableServer(_server.LocalServer):
    def __init__(self, path, manager):
        p = Path(path).absolute()
        _private(p.parent)
        if p.exists() or p.is_symlink():
            raise TerminalDenied("TTY_SOCKET_ALREADY_EXISTS")
        self.owner_uid = os.geteuid()
        self.manager = manager
        self.limits = manager.limits
        self._socket_path = p
        old = os.umask(0o077)
        try:
            socketserver.UnixStreamServer.__init__(self, str(p), PortableHandler)
        finally:
            os.umask(old)
        p.chmod(0o600)

    def server_close(self):
        try:
            self.manager.shutdown()
        finally:
            socketserver.UnixStreamServer.server_close(self)
            self._socket_path.unlink(missing_ok=True)


_SHAPES = {
    "status": (set(), set()),
    "open": (set(), {"program", "rows", "cols"}),
    "observe": ({"session_id"}, set()),
    "key.send": ({"session_id", "key"}, set()),
    "text.type": ({"session_id", "text"}, set()),
    "resize": ({"session_id", "rows", "cols"}, set()),
    "close": ({"session_id"}, set()),
    "sudo": ({"session_id", "mode"}, set()),
}


def dispatch(manager, payload):
    if type(payload) is not dict:
        raise TerminalDenied("TTY_REQUEST_INVALID")
    if payload.get("authorized") is not True:
        raise TerminalDenied("AUTHORIZED_TRUE_REQUIRED")
    op = payload.get("op")
    if op == "abort":
        op = "close"
    if type(op) is not str or op not in _SHAPES:
        raise TerminalDenied("TTY_OPERATION_INVALID")
    required, optional = _SHAPES[op]
    keys = set(payload) - {"op", "authorized"}
    if not required <= keys or keys - required - optional:
        raise TerminalDenied("TTY_REQUEST_INVALID")
    sid = payload.get("session_id")
    if sid is not None and (type(sid) is not str or not _SESSION.fullmatch(sid)):
        raise TerminalDenied("SESSION_ID_INVALID")
    with manager._lock:
        manager.reap()
        if op not in {"status", "open"}:
            manager.touch(sid)
        if op == "status":
            data = manager.status()
        elif op == "open":
            data = manager.open(payload.get("program", "bash"),
                                payload.get("rows", 24), payload.get("cols", 80))
        elif op == "sudo":
            data = manager.sudo(sid, payload["mode"])
            if payload["mode"] != "status" and data.get("cleanup_pending"):
                return {"ok": False, "error": "TTY_SUDO_TRANSITION_CLEANUP_PENDING",
                        "result": data, "transport": "same-uid-unix"}
        elif op == "observe":
            data = manager.observe(sid)
            data = dict(data, observation_kind="terminal_transcript", authentic_pixels=False, full_screen=False)
        elif op == "key.send":
            data = manager.key(sid, payload["key"], approved_context={})
        elif op == "text.type":
            data = manager.text(sid, payload["text"], approved_context={})
        elif op == "resize":
            manager._size(payload["rows"], payload["cols"])
            data = manager.resize(sid, payload["rows"], payload["cols"])
        else:
            data = manager.abort(sid)
    return {"ok": True, "result": data, "transport": "same-uid-unix"}


def request(state, payload):
    """Send one authorized operation to this profile's real TTY daemon."""
    try:
        if type(payload) is not dict or payload.get("authorized") is not True:
            return {"ok": False, "error": "AUTHORIZED_TRUE_REQUIRED"}
        state = state_path(state)
        path = runtime_path(state) / "agent.sock"
        if not path.exists():
            return {"ok": False, "error": "TTY_NOT_RUNNING"}
        s = path.lstat()
        if (not stat.S_ISSOCK(s.st_mode) or s.st_uid != os.geteuid()
                or stat.S_IMODE(s.st_mode) != 0o600):
            return {"ok": False, "error": "TTY_SOCKET_UNTRUSTED"}
        limits, _ = _config(state)
        raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > limits.rpc_max_bytes:
            return {"ok": False, "error": "TTY_REQUEST_TOO_LARGE_OR_TRUNCATED"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(limits.command_timeout_s + 10)
            client.connect(str(path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            received = bytearray()
            while True:
                part = client.recv(65536)
                if not part:
                    break
                received.extend(part)
                if len(received) > limits.rpc_max_bytes:
                    return {"ok": False, "error": "TTY_RESPONSE_TOO_LARGE"}
        value = json.loads(received)
        return value if type(value) is dict else {"ok": False, "error": "TTY_RESPONSE_INVALID"}
    except TerminalDenied as exc:
        return {"ok": False, "error": _safe_error(exc)}
    except (OSError, TimeoutError):
        return {"ok": False, "error": "TTY_UNAVAILABLE"}
    except (ValueError, TypeError, UnicodeError):
        return {"ok": False, "error": "TTY_RESPONSE_INVALID"}


def status(state=None):
    return request(state, {"op": "status", "authorized": True})


def serve(state=None):
    state = _private(state_path(state))
    limits, home = _config(state)
    rt = _private(runtime_path(state))
    lock = (state / "tty.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = rt / "agent.sock"
    if path.exists() or path.is_symlink():
        old = status(state)
        if old.get("ok"):
            raise TerminalDenied("TTY_ALREADY_RUNNING")
        st = path.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
            raise TerminalDenied("TTY_SOCKET_UNTRUSTED")
        path.unlink()
    try:
        tty_cleanup.require_prior_cleanup(state)
    except tty_cleanup.CleanupUnverified:
        raise TerminalDenied("TTY_CLEANUP_PENDING") from None
    manager = PortableManager(state, limits, home)
    manager.cleanup_daemon = tty_cleanup.daemon_identity(_identity(os.getpid()))
    manager._record_cleanup()
    def stopped(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    pidfile = state / "tty-process.json"
    _write_json(pidfile, {**manager.cleanup_daemon, "socket": str(path), "state_dir": str(state)})
    try:
        with PortableServer(path, manager) as server:
            print(json.dumps({"ok": True, "ready": True, "socket": str(path),
                              "policy": "same-uid-authorized", "x11_required": False}), flush=True)
            server.serve_forever(poll_interval=0.2)
    finally:
        pidfile.unlink(missing_ok=True)


def start(state=None):
    state = _private(state_path(state))
    current = status(state)
    if current.get("ok"):
        return current
    try:
        tty_cleanup.require_prior_cleanup(state)
    except tty_cleanup.CleanupUnverified:
        return {"ok": False, "error": "TTY_CLEANUP_PENDING", "cleanup_verified": False}
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = (state / "tty.log").open("ab")
    proc = subprocess.Popen([sys.executable, "-m", "framebridge_expanded.tty",
                             "serve", "--state", str(state)], env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, umask=0o077)
    log.close()
    _STARTED_CHILDREN[str(state)] = proc
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        current = status(state)
        if current.get("ok"):
            return current
        time.sleep(0.1)
    return {"ok": False, "error": "TTY_START_FAILED"}


def stop(state=None):
    state = state_path(state)
    p = state / "tty-process.json"
    expected = None
    already = True
    death_known = False
    try:
        receipt = state / tty_cleanup.NAME
        if not p.exists() and not p.is_symlink() and not receipt.exists() and not receipt.is_symlink():
            return {"ok": True, "state": "STOPPED", "already_stopped": True,
                    "cleanup_verified": True}
        outcome = tty_cleanup.read(state)
        if p.exists() or p.is_symlink():
            record = tty_cleanup.read_private(p)
            if "boot" not in record:
                raise tty_cleanup.CleanupUnverified()
            expected = tty_cleanup.daemon_identity(record)
            if outcome["daemon"] != expected:
                raise tty_cleanup.CleanupUnverified()
            if expected["boot"] == tty_cleanup.current_boot() and _alive(record):
                already = False
                fd = os.pidfd_open(record["pid"])
                try:
                    if _alive(record):
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
                deadline = time.monotonic() + 10
                while _alive(record) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if _alive(record):
                    return {"ok": False, "state": "STOP_TIMEOUT", "cleanup_verified": False}
        elif outcome["daemon"]["boot"] == tty_cleanup.current_boot() and _alive(outcome["daemon"]):
            raise tty_cleanup.CleanupUnverified()
        death_known = True
        outcome = tty_cleanup.read(state)
        if expected is not None and outcome["daemon"] != expected:
            raise tty_cleanup.CleanupUnverified()
        if not outcome["cleanup_verified"] and outcome["daemon"]["boot"] == tty_cleanup.current_boot():
            raise tty_cleanup.CleanupUnverified()
        return {"ok": True, "state": "STOPPED", "already_stopped": already,
                "cleanup_verified": True}
    except (tty_cleanup.CleanupUnverified, ValueError, OSError, KeyError, TypeError, AttributeError):
        return {"ok": False, "state": "STOPPED" if death_known else "STOP_UNVERIFIED",
                "error": "TTY_CLEANUP_UNVERIFIED", "cleanup_verified": False}
    finally:
        child = _STARTED_CHILDREN.get(str(state))
        if child is not None:
            try:
                child.wait(timeout=2)
                _STARTED_CHILDREN.pop(str(state), None)
            except subprocess.TimeoutExpired:
                pass


def _wire_reply(state, result):
    cache = _private(state_path(state) / "tty-replies")
    for old in cache.glob("*.json"):
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    raw = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    encoded = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
    chunks = [encoded[i:i + _WIRE_CHARS] for i in range(0, len(encoded), _WIRE_CHARS)]
    ticket = uuid.uuid4().hex
    item = {"ticket": ticket, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "chunks": chunks}
    _write_json(cache / (ticket + ".json"), item)
    return {"ok": True, "wire": "zlib-base64-v1", "ticket": ticket,
            "sha256": item["sha256"], "bytes": len(raw), "chunks": len(chunks),
            "index": 0, "data": chunks[0]}


def read_reply(state, ticket, index):
    if type(ticket) is not str or not _SESSION.fullmatch(ticket) or type(index) is not int or index < 0:
        return {"ok": False, "error": "TTY_REPLY_INVALID"}
    p = state_path(state) / "tty-replies" / (ticket + ".json")
    try:
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise ValueError()
        item = json.loads(p.read_text())
        return {"ok": True, "ticket": ticket, "index": index, "data": item["chunks"][index],
                "chunks": len(item["chunks"]), "sha256": item["sha256"]}
    except (OSError, ValueError, KeyError, IndexError):
        return {"ok": False, "error": "TTY_REPLY_NOT_FOUND"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Real persistent Bash CUA-TTY over a same-UID Unix socket.")
    parser.add_argument("command", choices=("serve", "start", "stop", "status", "request", "read"))
    parser.add_argument("--state", default=None)
    parser.add_argument("--payload-b64", default=None)
    parser.add_argument("--wire", action="store_true", help="Return a compressed, chunked response for bounded transports.")
    parser.add_argument("--ticket")
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.state)
            return 0
        if args.command == "start":
            result = start(args.state)
        elif args.command == "stop":
            result = stop(args.state)
        elif args.command == "status":
            result = status(args.state)
        elif args.command == "read":
            result = read_reply(args.state, args.ticket, args.index)
        else:
            if args.payload_b64:
                raw = base64.b64decode(args.payload_b64, validate=True)
            else:
                raw = sys.stdin.buffer.read(16777217)
            if len(raw) > 16777216:
                raise TerminalDenied("TTY_REQUEST_TOO_LARGE_OR_TRUNCATED")
            result = request(args.state, json.loads(raw))
            if args.wire:
                result = _wire_reply(args.state, result)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0 if result.get("ok") else 1
    except TerminalDenied as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}))
        return 1
    except (ValueError, TypeError, UnicodeError):
        print(json.dumps({"ok": False, "error": "TTY_REQUEST_INVALID"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
