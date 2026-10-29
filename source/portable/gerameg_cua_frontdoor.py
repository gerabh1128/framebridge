"""Siete herramientas CUA-TTY ligadas a GERAMEG, sobre FBEXEC original."""
from __future__ import annotations
import base64
import hashlib
import json
import re
import shlex
import uuid

RPC_PATH = ".local/lib/framebridge-gerameg-original/cua_tty_rpc.py"
PROTOCOL = "FB-CUA-TTY-RPC/1"
MAX_BYTES = 65536
CHUNK_BYTES = 2100
SID = re.compile(r"[A-Za-z0-9_-]{12,80}\Z")
REPLY_ID = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
KEYS = frozenset({"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "TAB", "ESC", "BACKSPACE", "CTRL_C"})
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
CHANGE = {**READ, "readOnlyHint": False}
STOP = {**CHANGE, "destructiveHint": True}


class CuaError(RuntimeError):
    pass


def require(ok, code):
    if not ok:
        raise CuaError(code)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _remote_script(argv):
    """Resolve the helper under the remote execution account's home directory."""
    command = shlex.join(["/usr/bin/python3", "-B"])
    command += ' "${HOME:?}/' + RPC_PATH + '"'
    if argv:
        command += " " + shlex.join(argv)
    return command + "\n"


def register_tools(mcp, exec_request):
    """exec_request es DI del bootstrap original, nunca un parámetro MCP."""

    def remote(argv):
        script = _remote_script(argv)
        job_id = "cua-gerameg-" + uuid.uuid4().hex
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        result = exec_request(script=script, authorized=True, timeout_s=15, job_id=job_id, sha256=digest)
        require(type(result) is dict and result.get("ok") is True, "CUA_TTY_EXEC_FAILED")
        exit_code = result.get("exit_code")
        require(type(exit_code) in (int, str) and str(exit_code) == "0", "CUA_TTY_EXEC_FAILED")
        require(result.get("target") == "GERAMEG" and result.get("mcp_job_id") == job_id and result.get("mcp_script_sha256") == digest, "CUA_TTY_EXEC_BINDING_INVALID")
        stdout = result.get("stdout")
        require(type(stdout) is str and len(stdout) <= 4096 and result.get("stdout_truncated") is not True, "CUA_TTY_STDOUT_INVALID")
        try:
            envelope = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise CuaError("CUA_TTY_ENVELOPE_INVALID") from None
        require(type(envelope) is dict and envelope.get("protocol") == PROTOCOL, "CUA_TTY_ENVELOPE_INVALID")
        if envelope.get("ok") is not True:
            code = envelope.get("error")
            if type(code) is str and re.fullmatch(r"[A-Z0-9_]{1,80}", code):
                raise CuaError("CUA_TTY_" + code)
            raise CuaError("CUA_TTY_RPC_FAILED")
        return envelope

    def unpack(envelope):
        total, digest = envelope.get("bytes"), envelope.get("sha256")
        require(type(total) is int and 0 < total <= MAX_BYTES and type(digest) is str and SHA.fullmatch(digest) is not None, "CUA_TTY_MANIFEST_INVALID")
        if envelope.get("kind") == "inline":
            text = envelope.get("data_b64")
            require(type(text) is str and len(text) <= 3000, "CUA_TTY_RESPONSE_INVALID")
            try:
                data = base64.b64decode(text, validate=True)
            except (ValueError, UnicodeError):
                raise CuaError("CUA_TTY_RESPONSE_INVALID") from None
        elif envelope.get("kind") == "file":
            reply_id = envelope.get("reply_id")
            require(type(reply_id) is str and REPLY_ID.fullmatch(reply_id) is not None, "CUA_TTY_REPLY_ID_INVALID")
            pieces = []
            for offset in range(0, total, CHUNK_BYTES):
                part = remote(["read", reply_id, str(offset)])
                require(part.get("kind") == "chunk" and part.get("reply_id") == reply_id and type(part.get("offset")) is int and part["offset"] == offset, "CUA_TTY_CHUNK_BINDING_INVALID")
                require(type(part.get("bytes")) is int and part["bytes"] == total and part.get("sha256") == digest, "CUA_TTY_CHUNK_MANIFEST_INVALID")
                text = part.get("data_b64")
                require(type(text) is str and len(text) <= 2800, "CUA_TTY_CHUNK_INVALID")
                try:
                    chunk = base64.b64decode(text, validate=True)
                except (ValueError, UnicodeError):
                    raise CuaError("CUA_TTY_CHUNK_INVALID") from None
                expected = min(CHUNK_BYTES, total - offset)
                require(type(part.get("length")) is int and part["length"] == expected and len(chunk) == expected, "CUA_TTY_CHUNK_LENGTH_INVALID")
                pieces.append(chunk)
            data = b"".join(pieces)
        else:
            raise CuaError("CUA_TTY_ENVELOPE_KIND_INVALID")
        require(len(data) == total and hashlib.sha256(data).hexdigest() == digest, "CUA_TTY_RESPONSE_HASH_MISMATCH")
        try:
            reply = json.loads(data)
        except (ValueError, UnicodeError):
            raise CuaError("CUA_TTY_RESPONSE_INVALID") from None
        require(type(reply) is dict and type(reply.get("ok")) is bool, "CUA_TTY_RESPONSE_INVALID")
        return reply, total, digest

    def call(op, authorized, **kwargs):
        if authorized is not True:
            return {"ok": False, "target": "GERAMEG", "error": "AUTHORIZED_TRUE_REQUIRED"}
        try:
            payload = base64.b64encode(encode({"op": op, **kwargs})).decode("ascii")
            reply, total, digest = unpack(remote(["invoke", payload]))
            if reply.get("ok") is not True:
                error = reply.get("error")
                code = error if type(error) is str and re.fullmatch(r"[A-Z0-9_]{1,80}", error) else "CUA_TTY_REQUEST_DENIED"
                return {"ok": False, "target": "GERAMEG", "error": code}
            require(type(reply.get("result")) is dict, "CUA_TTY_RESPONSE_INVALID")
            return {"ok": True, "target": "GERAMEG", "result": reply["result"], "response_bytes": total, "response_sha256": digest}
        except CuaError as exc:
            return {"ok": False, "target": "GERAMEG", "error": str(exc)}
        except Exception:
            return {"ok": False, "target": "GERAMEG", "error": "CUA_TTY_BACKEND_UNAVAILABLE"}

    def valid_sid(session_id):
        return type(session_id) is str and SID.fullmatch(session_id) is not None

    def denied(authorized, valid=True, error="CUA_TTY_ARGUMENT_INVALID"):
        if authorized is not True:
            return {"ok": False, "target": "GERAMEG", "error": "AUTHORIZED_TRUE_REQUIRED"}
        if not valid:
            return {"ok": False, "target": "GERAMEG", "error": error}
        return None

    @mcp.tool(annotations=dict(READ))
    def framebridge_cua_tty_status(authorized: bool) -> dict:
        """Estado CUA-TTY de GERAMEG; requiere autorización explícita del chat."""
        return call("status", authorized)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_cua_tty_open_demo(authorized: bool) -> dict:
        """Abre demo-readonly sin privilegios en GERAMEG; no abre una shell general."""
        return call("open_demo", authorized)

    @mcp.tool(annotations=dict(READ))
    def framebridge_cua_tty_observe(session_id: str, authorized: bool) -> dict:
        """Lee la pantalla textual de una sesión CUA-TTY de GERAMEG."""
        return denied(authorized, valid_sid(session_id), "CUA_TTY_SESSION_INVALID") or call("observe", authorized, session_id=session_id)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_cua_tty_resize(session_id: str, rows: int, cols: int, authorized: bool) -> dict:
        """Redimensiona una sesión de GERAMEG dentro de 5–100 filas y 20–220 columnas."""
        valid = valid_sid(session_id) and type(rows) is int and type(cols) is int and 5 <= rows <= 100 and 20 <= cols <= 220
        return denied(authorized, valid) or call("resize", authorized, session_id=session_id, rows=rows, cols=cols)

    @mcp.tool(annotations=dict(STOP))
    def framebridge_cua_tty_abort(session_id: str, authorized: bool) -> dict:
        """Cierra exclusivamente la sesión CUA-TTY indicada de GERAMEG."""
        return denied(authorized, valid_sid(session_id), "CUA_TTY_SESSION_INVALID") or call("abort", authorized, session_id=session_id)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_cua_tty_key_send(session_id: str, key: str, authorized: bool) -> dict:
        """Envía UP/DOWN/LEFT/RIGHT/ENTER/TAB/ESC/BACKSPACE/CTRL_C en GERAMEG."""
        valid = valid_sid(session_id) and type(key) is str and key in KEYS
        return denied(authorized, valid) or call("key_send", authorized, session_id=session_id, key=key)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_cua_tty_text_type(session_id: str, text: str, authorized: bool) -> dict:
        """Escribe 1–64 caracteres sin controles en la demo de GERAMEG."""
        valid = valid_sid(session_id) and type(text) is str and 1 <= len(text) <= 64 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)
        if valid:
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                valid = False
        return denied(authorized, valid) or call("text_type", authorized, session_id=session_id, text=text)
