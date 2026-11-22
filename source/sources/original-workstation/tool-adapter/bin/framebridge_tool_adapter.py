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
import tempfile
import threading
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = 1
MAX_SCRIPT_BYTES = 262144
MAX_REQUEST_BYTES = 393216
JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ENGINE_FAILURES = {70: "NOT_STARTED", 74: "OUTCOME_UNKNOWN"}

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


PREVIEW_BYTES = 65536
PRIVATE_BEGIN = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PRIVATE_END = re.compile(rb"-----END [A-Z0-9 ]*PRIVATE KEY-----")
SENSITIVE_LINE = re.compile(rb"secret|token|password|passwd|api[_-]?key|private[_-]?key|cookie|authorization|credential", re.I)
API_TOKEN = re.compile(rb"\bsk-(?:proj-|svcacct-)?[a-zA-Z0-9_-]+")
MARKER = b"RESULTADO_FINAL="


class CaptureUnavailable(Exception):
    pass


def read_capture_line(stream, remaining, digest, keep):
    """Scan one logical line, retaining only its bounded prefix and redaction flags."""
    prefix = bytearray()
    tail = b""
    consumed = 0
    sensitive = delimiter = begins = ends = False
    while consumed < remaining:
        chunk = stream.readline(min(PREVIEW_BYTES, remaining - consumed))
        if not chunk:
            raise CaptureUnavailable("STDOUT_INTEGRITY_MISMATCH")
        digest.update(chunk)
        prefix.extend(chunk[:keep - len(prefix)])
        window = tail + chunk
        sensitive = sensitive or bool(SENSITIVE_LINE.search(window))
        delimiter = delimiter or b"=" in chunk or b":" in chunk
        begins = begins or bool(PRIVATE_BEGIN.search(window))
        ends = ends or bool(PRIVATE_END.search(window))
        tail = window[-128:]
        consumed += len(chunk)
        if chunk.endswith(b"\n"):
            break
    newline = b"\r\n" if tail.endswith(b"\r\n") else b"\n" if tail.endswith(b"\n") else b""
    return bytes(prefix), consumed, sensitive and delimiter, begins, ends, newline


def checked_stdout(root, slot, job_id, expected, count, preview):
    """Return a bounded view only after hashing the same bytes on the producing node."""
    if (type(slot) is not int or not 0 <= slot <= 8 or not isinstance(job_id, str)
            or not JOB_RE.fullmatch(job_id) or job_id in (".", "..")):
        raise CaptureUnavailable("STDOUT_RECEIPT_INVALID")
    directories = ["lanes", str(slot)] if slot else []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        parent = os.open(root, flags)
        stack.callback(os.close, parent)
        for part in [*directories, "jobs", job_id]:
            parent = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open("stdout.bin", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CaptureUnavailable("STDOUT_NOT_REGULAR")
        stream = stack.enter_context(os.fdopen(fd, "rb", closefd=False))
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CaptureUnavailable("STDOUT_DIGEST_UNAVAILABLE")
        if count is not None and count != info.st_size:
            raise CaptureUnavailable("STDOUT_INTEGRITY_MISMATCH")

        digest = hashlib.sha256()
        remaining = info.st_size
        output = bytearray()
        marker = None
        private = False
        offset = 0
        output_clipped = False
        while remaining:
            budget = max(0, PREVIEW_BYTES - offset) if preview else 0
            # Keep enough bytes for the legacy result marker, including after the preview.
            line, length, sensitive, begins, ends, newline = read_capture_line(
                stream, remaining, digest, max(budget, 4096))
            remaining -= length
            offset += length
            if begins:
                visible = b"[REDACTED PRIVATE KEY BLOCK]" + newline
            elif private:
                visible = b""
            elif sensitive:
                visible = b"[REDACTED SENSITIVE LINE]" + newline
            else:
                visible = API_TOKEN.sub(b"[REDACTED]", line)
            if line.startswith(MARKER) and not (private or begins):
                marker = ("[REDACTED SENSITIVE LINE]" if sensitive else
                          API_TOKEN.sub(b"[REDACTED]", line[len(MARKER):]).decode("utf-8", "replace").strip()[:2000])
            private = (private or begins) and not ends
            if budget:
                space = min(budget, PREVIEW_BYTES - len(output))
                output.extend(visible[:space])
                output_clipped |= len(visible) > space
        if os.fstat(fd).st_size != info.st_size or digest.hexdigest() != expected:
            raise CaptureUnavailable("STDOUT_INTEGRITY_MISMATCH")
        return bytes(output).decode("utf-8", "replace"), info.st_size > PREVIEW_BYTES or output_clipped, marker


def output_receipt(request, receipt):
    """Build the public response after the authorized handler has selected its job."""
    kind = str(request.get("request_type", "exec")).strip().lower() if isinstance(request, dict) else ""
    if kind not in ("exec", "fbterm_jobs_v2") or not isinstance(receipt, dict) or "jobs" in receipt:
        return receipt
    result = dict(receipt)
    result.pop("stdout_path", None)
    result.pop("stderr_path", None)
    # Older ledgers contain a cached marker. An invalid or missing capture must never expose it.
    result.pop("result_final", None)
    requested = request.get("include_stdout") is True
    if result.get("execution_state") != "completed":
        if requested:
            result.update(stdout_omitted_reason="JOB_NOT_COMPLETED", stdout_integrity="not_checked")
        return result
    if not requested and kind != "exec":
        return result
    try:
        text, truncated, marker = checked_stdout(
            FBROOT, result.get("worker_slot", 0), result["job_id"],
            result.get("stdout_sha256"), result.get("stdout_bytes"), requested)
    except FileNotFoundError:
        reason = "STDOUT_NOT_FOUND"
    except CaptureUnavailable as exc:
        reason = str(exc)
    except OSError:
        reason = "STDOUT_UNREADABLE"
    else:
        result["result_final"] = marker
        if requested:
            result.update(stdout=text, stdout_sanitized=True, stdout_truncated=truncated,
                          stdout_omitted_reason=None, stdout_integrity="verified")
        return result
    result.update(capture_status="partial", result_final=None, stdout_integrity="unverified",
                  stdout_omitted_reason=reason)
    return result


def decode_receipt(proc, job_id, digest, job):
    base = {"version": VERSION, "job_id": job_id, "mode": "EXEC",
            "command_sha256": digest, "engine_rc": proc.returncode}
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError):
        result = None
    result = result if isinstance(result, dict) else {}
    failure = ENGINE_FAILURES.get(proc.returncode)
    if (failure and result.get("ERROR") == failure and set(result) == {"ERROR", "MESSAGE"}
            and isinstance(result["MESSAGE"], str)):
        code = "FBTERM_" + failure
        return dict(base, ok=False, error=code, adapter_error=code, execution_state=failure.lower(),
                    do_not_replay=failure == "OUTCOME_UNKNOWN")
    if proc.returncode == 75 and result.get("ERROR") == "BUSY":
        reason = result.get("REASON")
        code = {"controller_busy":"FBTERM_CONTROLLER_BUSY", "unresolved_job":"FBTERM_UNRESOLVED_JOB"}.get(reason,"FBTERM_BUSY")
        response = dict(base, ok=False, error=code, adapter_error=code, execution_state="not_started")
        blocker = result.get("BLOCKING_JOB_ID")
        if isinstance(blocker, str) and JOB_RE.fullmatch(blocker):
            response["blocking_job_id"] = blocker
        return response
    rc = result.get("EXIT")
    if (proc.returncode != 0 or result.get("JOB_ID") != job_id or result.get("COMMAND_HASH_GATE") != "PASS"
            or result.get("COMMAND_SHA256") != digest or not isinstance(rc, str)
            or not re.fullmatch(r"[0-9]{1,3}", rc) or int(rc) > 255):
        return dict(base, ok=False, error="FBTERM_RECEIPT_INVALID", execution_state="outcome_unknown",
                    do_not_replay=True)
    response = dict(base, ok=True, execution_state="completed", exit_code=rc,
                    command_hash_gate="PASS", capture_status="complete", result_final=None)
    for name in ("stdout", "stderr"):
        prefix = name.upper()
        path = job / (name + ".bin")
        checksum = result.get(prefix + "_SHA256")
        count = result.get(prefix + "_BYTES", "")
        manifest_ok = (result.get(prefix + "_TEE_EXIT") == "0" and isinstance(checksum, str)
                       and re.fullmatch(r"[a-f0-9]{64}", checksum) is not None
                       and isinstance(count, str) and re.fullmatch(r"[0-9]{1,20}", count) is not None)
        expected_bytes = int(count) if manifest_ok else None
        try:
            info = path.lstat()
            present = stat.S_ISREG(info.st_mode) and info.st_size == expected_bytes
        except OSError:
            present = False
        response[name + "_sha256"] = checksum if manifest_ok else None
        response[name + "_bytes"] = expected_bytes
        if not manifest_ok or not present:
            response["capture_status"] = "partial"
    for key in ("PTY","PANE_PID","PANE_TTY","SHELL_READY"):
        response[key.lower()] = result.get(key)
    return response


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
    payload["target"] = "WORKSTATION"
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


def handle(req, *, exec_root=None, worker_slot=0):
    if not isinstance(req, dict):
        raise ValueError("REQUEST_NOT_OBJECT")

    if req.get("version") != VERSION:
        raise ValueError("UNSUPPORTED_VERSION")

    job_id = str(req.get("job_id", ""))

    if not JOB_RE.fullmatch(job_id) or job_id in (".", ".."):
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

    root = FBROOT if exec_root is None else Path(exec_root)
    session = os.environ.get("FBTERM_SESSION", "framebridge-fbterm-r1")
    title = os.environ.get("FBTERM_TITLE", "FrameBridge Terminal R1")
    if worker_slot:
        session += "-jobs-" + str(worker_slot)
        title += " | jobs " + str(worker_slot)
    with tempfile.NamedTemporaryFile(dir=INBOX, suffix=".bash") as command_file:
        command_file.write(script_bytes)
        command_file.flush()
        proc = subprocess.run(
            [sys.executable, str(ENGINE), "exec", job_id, str(timeout_s), command_file.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_s + 45,
            check=False, env={**os.environ, "FBTERM_ROOT": str(root), "FBTERM_SESSION": session,
                              "FBTERM_TITLE": title, "FBTERM_START_BEFORE": str(req.get("start_before", "")),
                              "FBTERM_JOB_VIEWER": "0" if worker_slot else "1"})
    return decode_receipt(proc, job_id, digest, root / "jobs" / job_id)


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

    def respond(conn):
        with conn:
            try:
                conn.settimeout(10)
                if peer_uid(conn) != os.getuid():
                    raise PermissionError("PEER_UID_MISMATCH")
                req = recv_request(conn)
                resp = output_receipt(req, handle(req))
            except Exception as exc:
                resp = {"version": VERSION, "ok": False,
                        "adapter_error": f"{type(exc).__name__}:{exc}"[:2000]}
            send_response(conn, resp)

    # Bound connections before accept; do not build an unbounded executor queue.
    slots = threading.BoundedSemaphore(16)
    def process(conn):
        try:
            respond(conn)
        finally:
            slots.release()

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            while True:
                slots.acquire()
                conn, _ = server.accept()
                pool.submit(process, conn)
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
