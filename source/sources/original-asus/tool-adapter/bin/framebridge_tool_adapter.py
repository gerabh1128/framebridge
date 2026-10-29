#!/usr/bin/env python3
import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path

VERSION = 1
MAX_SCRIPT_BYTES = 262144
MAX_REQUEST_BYTES = 393216
JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

HOME = Path.home()
FBROOT = HOME / ".local/share/framebridge/fbterm"
ENGINE = FBROOT / "bin/fbterm_engine.py"

ROOT = Path(
    os.environ.get(
        "FRAMEBRIDGE_TOOL_ROOT",
        str(HOME / ".local/share/framebridge/tool-adapter")
    )
)
SOCKET_PATH = ROOT / "adapter.sock"
INBOX = ROOT / "inbox"
PID_FILE = ROOT / "adapter.pid"
NATIVE_HOST = Path(os.environ.get("FRAMEBRIDGE_NATIVE_HOST", str(HOME / ".local/lib/framebridge-native/host/framebridge_host.py")))


def prepare_root():
    ROOT.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    os.chmod(INBOX, 0o700)


def recv_request(conn):
    data = bytearray()

    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break

        data.extend(chunk)

        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("REQUEST_TOO_LARGE")

        if b"\n" in data:
            break

    raw = bytes(data).split(b"\n", 1)[0]

    if not raw:
        raise ValueError("EMPTY_REQUEST")

    return json.loads(raw.decode("utf-8"))


def send_response(conn, obj):
    raw = (
        json.dumps(
            obj,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    try:
        conn.sendall(raw)
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


def peer_uid(conn):
    fmt = "3i"
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize(fmt),
    )
    _pid, uid, _gid = struct.unpack(fmt, raw)
    return uid


def last_result_final(stdout_path):
    try:
        lines = Path(stdout_path).read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        if line.startswith("RESULTADO_FINAL="):
            return line.split("=", 1)[1].strip()[:2000]

    return None


def engine_result(stdout):
    for line in reversed(stdout.splitlines()):
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(obj, dict):
            return obj

    return None


def native_cua_roundtrip(payload, timeout_s=60):
    if not NATIVE_HOST.is_file():
        raise ValueError("FRAMEBRIDGE_NATIVE_HOST_NOT_FOUND")

    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    frame = struct.pack("<I", len(raw)) + raw

    try:
        proc = subprocess.run(
            [str(NATIVE_HOST)],
            input=frame,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("CUA_NATIVE_HOST_TIMEOUT")

    out = proc.stdout or b""

    if len(out) < 4:
        raise ValueError("CUA_NATIVE_RESPONSE_HEADER_MISSING")

    size = struct.unpack("<I", out[:4])[0]

    if size <= 0 or size > 2_000_000:
        raise ValueError("CUA_NATIVE_RESPONSE_SIZE_INVALID")

    body = out[4:4 + size]

    if len(body) != size:
        raise ValueError("CUA_NATIVE_RESPONSE_TRUNCATED")

    try:
        response = json.loads(body.decode("utf-8"))
    except Exception:
        raise ValueError("CUA_NATIVE_RESPONSE_INVALID_JSON")

    if not isinstance(response, dict):
        raise ValueError("CUA_NATIVE_RESPONSE_NOT_OBJECT")

    return response


def handle_cua_request(req, job_id):
    timeout_s = int(req.get("timeout_s", 60))

    if not 1 <= timeout_s <= 180:
        raise ValueError("INVALID_CUA_TIMEOUT")

    cua = req.get("cua")

    if not isinstance(cua, dict):
        raise ValueError("CUA_PAYLOAD_REQUIRED")

    payload = dict(cua)
    payload["op"] = "cua"
    payload["id"] = job_id
    payload["target"] = "ASUS"
    payload["authorized"] = "YES"

    response = native_cua_roundtrip(
        payload,
        timeout_s=timeout_s,
    )

    return {
        "version": VERSION,
        "ok": bool(response.get("executed")),
        "job_id": job_id,
        "mode": "CUA",
        "action": str(payload.get("action", ""))[:32],
        "channel": str(response.get("channel", "CUA-DIRECT")),
        "exit_code": response.get("exit_code"),
        "frame_output": str(response.get("output", "")),
        "error": str(response.get("error", "")),
    }


def handle(req):
    if not isinstance(req, dict):
        raise ValueError("REQUEST_NOT_OBJECT")

    if req.get("version") != VERSION:
        raise ValueError("UNSUPPORTED_VERSION")

    job_id = str(req.get("job_id", ""))

    if not JOB_RE.fullmatch(job_id):
        raise ValueError("INVALID_JOB_ID")

    mode = str(req.get("mode", "EXEC")).upper()

    if mode != "EXEC":
        raise ValueError("UNSUPPORTED_MODE")

    if req.get("authorized") is not True:
        raise ValueError("AUTHORIZED_TRUE_REQUIRED")

    request_type = str(
        req.get("request_type", "exec")
    ).lower().strip()

    if request_type == "cua":
        return handle_cua_request(req, job_id)

    if request_type != "exec":
        raise ValueError("UNSUPPORTED_REQUEST_TYPE")

    script = req.get("script")

    if not isinstance(script, str) or not script:
        raise ValueError("SCRIPT_REQUIRED")

    script_bytes = script.encode("utf-8")

    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise ValueError("SCRIPT_TOO_LARGE")

    timeout_s = int(req.get("timeout_s", 120))

    if not 1 <= timeout_s <= 600:
        raise ValueError("INVALID_TIMEOUT")

    digest = hashlib.sha256(script_bytes).hexdigest()

    expected = req.get("sha256")

    if expected is not None:
        expected = str(expected).lower()

        if digest != expected:
            raise ValueError("SCRIPT_SHA256_MISMATCH")

    command_path = INBOX / f"{job_id}.bash"
    temp_path = INBOX / f".{job_id}.tmp"

    temp_path.write_bytes(script_bytes)
    os.chmod(temp_path, 0o600)
    temp_path.replace(command_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(ENGINE),
            "exec",
            job_id,
            str(timeout_s),
            str(command_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s + 45,
        check=False,
        env={
            **os.environ,
            "FBTERM_ROOT": str(FBROOT),
            "FBTERM_SESSION": os.environ.get(
                "FBTERM_SESSION",
                "framebridge-fbterm-r1",
            ),
            "FBTERM_TITLE": os.environ.get(
                "FBTERM_TITLE",
                "FrameBridge Terminal R1",
            ),
        },
    )

    result = engine_result(proc.stdout)

    if result is None:
        return {
            "version": VERSION,
            "ok": False,
            "job_id": job_id,
            "adapter_error": "ENGINE_RESULT_NOT_FOUND",
            "engine_rc": proc.returncode,
            "engine_stderr": proc.stderr[-2000:],
            "command_sha256": digest,
        }

    stdout_path = result.get("STDOUT_PATH")
    stderr_path = result.get("STDERR_PATH")

    return {
        "version": VERSION,
        "ok": True,
        "job_id": job_id,
        "mode": mode,
        "command_sha256": digest,
        "engine_rc": proc.returncode,
        "exit_code": result.get("EXIT"),
        "result_final": (
            last_result_final(stdout_path)
            if stdout_path
            else None
        ),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_sha256": result.get("STDOUT_SHA256"),
        "stderr_sha256": result.get("STDERR_SHA256"),
        "command_hash_gate": result.get(
            "COMMAND_HASH_GATE"
        ),
        "pty": result.get("PTY"),
        "pane_pid": result.get("PANE_PID"),
        "pane_tty": result.get("PANE_TTY"),
        "shell_ready": result.get("SHELL_READY"),
    }


def serve():
    prepare_root()

    if not ENGINE.is_file():
        raise RuntimeError("FBTERM_ENGINE_NOT_FOUND")

    if SOCKET_PATH.exists():
        st = SOCKET_PATH.lstat()

        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError("SOCKET_PATH_OCCUPIED")

        SOCKET_PATH.unlink()

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )

    server.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(4)

    PID_FILE.write_text(
        str(os.getpid()) + "\n",
        encoding="ascii",
    )
    os.chmod(PID_FILE, 0o600)

    try:
        while True:
            conn, _ = server.accept()

            with conn:
                try:
                    if peer_uid(conn) != os.getuid():
                        raise PermissionError(
                            "PEER_UID_MISMATCH"
                        )

                    req = recv_request(conn)
                    resp = handle(req)

                except Exception as exc:
                    resp = {
                        "version": VERSION,
                        "ok": False,
                        "adapter_error": (
                            f"{type(exc).__name__}:"
                            f"{exc}"
                        )[:2000],
                    }

                send_response(conn, resp)

    finally:
        server.close()

        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""

    if action == "serve":
        serve()
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "USAGE: serve",
                },
                sort_keys=True,
            )
        )
        raise SystemExit(64)
