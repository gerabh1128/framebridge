#!/usr/bin/env python3
"""RPC local CUA-TTY de GERAMEG, invocado por el FBEXEC original firmado."""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import uuid

UID = 1000
HOSTNAME = "GERAMEG"
HOME = Path.home()
SOCKET_PATH = Path("/run/user/1000/framebridge-alpha01-cua/agent.sock")
CLIENT_PATH = Path("/opt/framebridge-alpha-01/vendor/framebridge_cua_tty/client.py")
CLIENT_SHA256 = "6a1fc004f2d50944661fcce67002b04534721bc3118eaf44e3f0694e19d15fe7"
RESULTS_DIR = HOME / ".local/share/framebridge/cua-tty-results"
PROTOCOL = "FB-CUA-TTY-RPC/1"
MAX_BYTES = 65536
INLINE_BYTES = 3000
CHUNK_BYTES = 2100
SID = re.compile(r"[A-Za-z0-9_-]{12,80}\Z")
REPLY_ID = re.compile(r"[0-9a-f]{32}\Z")
KEYS = frozenset(['UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER', 'RETURN', 'TAB', 'SHIFT_TAB', 'ESC', 'BACKSPACE', 'DELETE', 'INSERT', 'HOME', 'END', 'PAGE_UP', 'PAGE_DOWN', 'SPACE', 'CTRL_A', 'CTRL_B', 'CTRL_C', 'CTRL_D', 'CTRL_E', 'CTRL_F', 'CTRL_G', 'CTRL_H', 'CTRL_I', 'CTRL_J', 'CTRL_K', 'CTRL_L', 'CTRL_M', 'CTRL_N', 'CTRL_O', 'CTRL_P', 'CTRL_Q', 'CTRL_R', 'CTRL_S', 'CTRL_T', 'CTRL_U', 'CTRL_V', 'CTRL_W', 'CTRL_X', 'CTRL_Y', 'CTRL_Z', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12'])
SENSITIVE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|authorization|cookie|credential)")
KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


class RpcError(RuntimeError):
    pass


def require(ok, code):
    if not ok:
        raise RpcError(code)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def sanitize(value):
    # Sanear ANTES de persistir o codificar en base64, igual que el stdout original.
    private_key = [False]

    def clean(item):
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val) for val in item]
        if not isinstance(item, str):
            return item
        lines = []
        for line in item.splitlines():
            if KEY_BEGIN.search(line):
                private_key[0] = True
                lines.append("[REDACTED PRIVATE KEY BLOCK]")
            elif private_key[0]:
                if KEY_END.search(line):
                    private_key[0] = False
            elif SENSITIVE.search(line) and ("=" in line or ":" in line):
                lines.append("[REDACTED SENSITIVE LINE]")
            else:
                lines.append(line)
        return "\n".join(lines)

    return clean(value)


def validate_request(req):
    require(type(req) is dict and type(req.get("op")) is str, "REQUEST_INVALID")
    op = req["op"]
    shapes = {
        "status": {"op"}, "open_demo": {"op"}, "open_shell": {"op"},
        "observe": {"op", "session_id"}, "abort": {"op", "session_id"},
        "resize": {"op", "session_id", "rows", "cols"},
        "key_send": {"op", "session_id", "key"},
        "text_type": {"op", "session_id", "text"},
    }
    require(op in shapes and set(req) == shapes[op], "REQUEST_INVALID")
    if "session_id" in req:
        require(type(req["session_id"]) is str and SID.fullmatch(req["session_id"]) is not None, "SESSION_INVALID")
    if op == "resize":
        require(type(req["rows"]) is int and type(req["cols"]) is int and 5 <= req["rows"] <= 100 and 20 <= req["cols"] <= 220, "SIZE_INVALID")
    if op == "key_send":
        require(type(req["key"]) is str and req["key"] in KEYS, "KEY_NOT_ALLOWLISTED")
    if op == "text_type":
        text = req["text"]
        require(type(text) is str and 0 < len(text.encode("utf-8")) <= 16384 and "\x00" not in text, "TEXT_NOT_ALLOWED")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise RpcError("TEXT_NOT_ALLOWED") from None
    args = {key: val for key, val in req.items() if key != "op"}
    if op == "open_shell":
        return "open", {"program": "bash"}
    if op == "open_demo":
        return "open", {"program": "demo-readonly"}
    if op in {"key_send", "text_type"}:
        # Modo local legado autorizado por el propietario; no es una firma Owner.
        args["owner_assertion"] = {}
        op = {"key_send": "key.send", "text_type": "text.type"}[op]
    return op, args


def guard_runtime():
    require(os.getuid() == UID and os.geteuid() == UID, "UID_MISMATCH")
    require(socket.gethostname().upper() == HOSTNAME, "HOSTNAME_MISMATCH")
    require(Path.home().resolve() == HOME.resolve(), "HOME_MISMATCH")


def load_client():
    require(CLIENT_PATH.is_file() and not CLIENT_PATH.is_symlink(), "CLIENT_PATH_INVALID")
    source = CLIENT_PATH.read_bytes()
    require(hashlib.sha256(source).hexdigest() == CLIENT_SHA256, "CLIENT_SHA256_MISMATCH")
    spec = importlib.util.spec_from_file_location("_gerameg_cua_client_pinned", CLIENT_PATH)
    require(spec is not None and spec.loader is not None, "CLIENT_IMPORT_INVALID")
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(CLIENT_PATH), "exec"), module.__dict__)
    return module.rpc


def backend_call(req):
    op, args = validate_request(req)
    try:
        parent, target = SOCKET_PATH.parent.lstat(), SOCKET_PATH.lstat()
    except OSError:
        raise RpcError("CUA_TTY_OFFLINE") from None
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == UID and parent.st_mode & 0o077 == 0, "SOCKET_PARENT_UNTRUSTED")
    require(stat.S_ISSOCK(target.st_mode) and target.st_uid == UID and target.st_mode & 0o077 == 0, "SOCKET_UNTRUSTED")
    reply = load_client()(op, path=SOCKET_PATH, **args)
    require(type(reply) is dict and type(reply.get("ok")) is bool, "BACKEND_RESPONSE_INVALID")
    if reply.get("ok") is True:
        require(type(reply.get("result")) is dict, "BACKEND_RESPONSE_INVALID")
    return sanitize(reply)


def result_directory(create=False):
    if create:
        RESULTS_DIR.mkdir(mode=0o700, exist_ok=True)
    require(RESULTS_DIR.resolve() == RESULTS_DIR.absolute(), "RESULT_DIRECTORY_UNSAFE")
    fd = os.open(RESULTS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if st.st_uid != UID or st.st_mode & 0o077:
        os.close(fd)
        raise RpcError("RESULT_DIRECTORY_UNSAFE")
    return fd


def pack_reply(reply):
    reply = sanitize(reply)
    data = encode(reply)
    require(0 < len(data) <= MAX_BYTES, "RESPONSE_TOO_LARGE")
    meta = {"protocol": PROTOCOL, "ok": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    # El sobre ASCII evita que campos del backend como "authorization_mode"
    # hagan que el saneador de stdout sustituya toda una línea JSON válida.
    inline = {**meta, "kind": "inline", "data_b64": base64.b64encode(data).decode("ascii")}
    if len(encode(inline)) <= INLINE_BYTES:
        return inline
    reply_id = uuid.uuid4().hex
    directory = result_directory(create=True)
    try:
        fd = os.open(reply_id + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return {**meta, "kind": "file", "reply_id": reply_id}


def read_chunk(reply_id, offset):
    require(type(reply_id) is str and REPLY_ID.fullmatch(reply_id) is not None, "REPLY_ID_INVALID")
    require(type(offset) is int and 0 <= offset < MAX_BYTES and offset % CHUNK_BYTES == 0, "OFFSET_INVALID")
    directory = result_directory()
    try:
        fd = os.open(reply_id + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == UID and st.st_mode & 0o077 == 0, "RESULT_FILE_UNSAFE")
            require(0 < st.st_size <= MAX_BYTES, "RESULT_SIZE_INVALID")
            data = stream.read(MAX_BYTES + 1)
            require(len(data) == st.st_size, "RESULT_SIZE_CHANGED")
    finally:
        os.close(directory)
    require(offset < len(data), "OFFSET_INVALID")
    chunk = data[offset:offset + CHUNK_BYTES]
    return {"protocol": PROTOCOL, "ok": True, "kind": "chunk", "reply_id": reply_id,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "offset": offset, "length": len(chunk), "data_b64": base64.b64encode(chunk).decode("ascii")}


def dispatch(argv):
    require(type(argv) is list, "CLI_INVALID")
    if len(argv) == 2 and argv[0] == "invoke":
        require(len(argv[1]) <= 50000, "REQUEST_TOO_LARGE")
        try:
            raw = base64.b64decode(argv[1], validate=True)
            require(len(raw) <= 40000, "REQUEST_TOO_LARGE")
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RpcError("REQUEST_ENCODING_INVALID") from None
        return pack_reply(backend_call(request))
    if len(argv) == 3 and argv[0] == "read":
        require(re.fullmatch(r"0|[1-9][0-9]{0,4}", argv[2]) is not None, "OFFSET_INVALID")
        return read_chunk(argv[1], int(argv[2]))
    raise RpcError("CLI_INVALID")


def main():
    try:
        guard_runtime()
        result = dispatch(sys.argv[1:])
    except RpcError as exc:
        result = {"protocol": PROTOCOL, "ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"protocol": PROTOCOL, "ok": False, "error": "RPC_FAILED", "error_type": type(exc).__name__}
    output = encode(result)
    require(len(output) <= 3500, "ENVELOPE_TOO_LARGE")
    sys.stdout.write(output.decode("ascii") + "\n")


if __name__ == "__main__":
    main()
