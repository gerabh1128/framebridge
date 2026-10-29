"""Typed real-screen transport over the existing authenticated admin.exec channel.

Only this explicit request type bypasses the visible administrative FBTERM engine.
The script field is canonical protocol data and a hash binding, never shell code.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import time

from .screen_frontdoor import RPC_COMMAND, EXPANDED_STDOUT_BYTES

# This typed route bypasses the legacy 4096-byte administrative sanitizer.
MAX_STDOUT_BYTES = EXPANDED_STDOUT_BYTES
from .screen_rpc import MAX_REQUEST, encode, validate_request, ScreenRpcError

REQUEST_TYPE = "screen_rpc"
MAX_TIMEOUT = 30
MAX_STDERR_BYTES = 4096
JOB = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
FIELDS = {"version", "mode", "authorized", "script", "timeout_s", "job_id",
          "sha256", "include_stdout", "request_type"}


class ScreenTransportError(ValueError):
    pass


class ScreenProcessError(RuntimeError):
    def __init__(self, code, *, started=False):
        super().__init__(code)
        self.started = started


def need(condition, code):
    if not condition:
        raise ScreenTransportError(code)


def parse_script(script):
    need(type(script) is str and script.startswith(RPC_COMMAND)
         and script.endswith("\n"), "SCREEN_RPC_SCRIPT_INVALID")
    encoded = script[len(RPC_COMMAND):-1]
    need(len(encoded) <= MAX_REQUEST * 2, "SCREEN_RPC_REQUEST_TOO_LARGE")
    try:
        words = shlex.split(encoded, posix=True)
        need(len(words) == 1, "SCREEN_RPC_SCRIPT_INVALID")
        raw = base64.b64decode(words[0], validate=True)
        need(len(raw) <= MAX_REQUEST, "SCREEN_RPC_REQUEST_TOO_LARGE")
        request = json.loads(raw)
        validate_request(request)
        canonical = encode(request)
        need(raw == canonical, "SCREEN_RPC_REQUEST_NONCANONICAL")
        argument = base64.b64encode(canonical).decode("ascii")
        need(script == RPC_COMMAND + shlex.quote(argument) + "\n",
             "SCREEN_RPC_SCRIPT_NONCANONICAL")
        return request, argument
    except (ValueError, UnicodeError, binascii.Error, ScreenRpcError) as exc:
        if isinstance(exc, ScreenTransportError):
            raise
        raise ScreenTransportError("SCREEN_RPC_REQUEST_INVALID") from None


def validate_payload(payload):
    need(type(payload) is dict and set(payload) == FIELDS, "SCREEN_RPC_FIELDS_INVALID")
    need(type(payload["version"]) is int and payload["version"] == 1,
         "SCREEN_RPC_VERSION_INVALID")
    need(payload["mode"] == "EXEC" and payload["request_type"] == REQUEST_TYPE,
         "SCREEN_RPC_TYPE_INVALID")
    need(payload["authorized"] is True, "AUTHORIZED_TRUE_REQUIRED")
    need(type(payload["job_id"]) is str and JOB.fullmatch(payload["job_id"]),
         "INVALID_JOB_ID")
    need(type(payload["timeout_s"]) is int and 1 <= payload["timeout_s"] <= MAX_TIMEOUT,
         "INVALID_SCREEN_TIMEOUT")
    need(payload["include_stdout"] is True, "SCREEN_RPC_STDOUT_REQUIRED")
    request, argument = parse_script(payload["script"])
    digest = hashlib.sha256(payload["script"].encode("utf-8")).hexdigest()
    need(type(payload["sha256"]) is str and payload["sha256"] == digest,
         "SCRIPT_SHA256_MISMATCH")
    return request, argument


def _run_rpc(argument, timeout_s):
    """Bound memory while draining both pipes; bound wall time and child lifetime."""
    argv = ["/usr/bin/python3", "-B", "-m",
            "framebridge_expanded.real_screen.screen_rpc", argument]
    process = None
    completed = False
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_s
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, close_fds=True,
                                   start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScreenProcessError("SCREEN_RPC_TIMEOUT", started=True)
            for item, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(item.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(item.fileobj)
                    continue
                name = item.data
                limit = MAX_STDOUT_BYTES if name == "stdout" else MAX_STDERR_BYTES
                if len(buffers[name]) + len(chunk) > limit:
                    raise ScreenProcessError("SCREEN_RPC_OUTPUT_LIMIT", started=True)
                buffers[name].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScreenProcessError("SCREEN_RPC_TIMEOUT", started=True)
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ScreenProcessError("SCREEN_RPC_TIMEOUT", started=True) from None
        completed = True
        return code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    except OSError:
        raise ScreenProcessError("SCREEN_RPC_PROCESS_FAILED", started=process is not None) from None
    finally:
        selector.close()
        if process is not None:
            if not completed or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _response(payload, envelope, code):
    return {"version": 1, "ok": envelope["ok"], "mode": "EXEC",
            "request_type": REQUEST_TYPE, "job_id": payload["job_id"],
            "command_sha256": payload["sha256"], "exit_code": str(code),
            "stdout": encode(envelope).decode("ascii") + "\n",
            "stdout_truncated": False, "stdout_sanitized": False}


def _failure(payload, request, code, *, started=False):
    uncertain = started and request["op"] in {"key", "text"}
    error = {"code": code}
    if uncertain:
        error.update(observe_again=True)
        capture = request.get("capture_id")
        if type(capture) is str and re.fullmatch(r"[0-9a-f]{32}", capture):
            error["capture_id"] = capture
    return _response(payload, {"ok": False, "error": error}, 1)


def handle_request(payload, *, runner=None):
    request, argument = validate_payload(payload)
    runner = _run_rpc if runner is None else runner
    try:
        code, stdout, stderr = runner(argument, payload["timeout_s"])
    except ScreenProcessError as exc:
        return _failure(payload, request, str(exc), started=exc.started)
    if (type(stdout) is not bytes or type(stderr) is not bytes
            or len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES):
        return _failure(payload, request, "SCREEN_RPC_OUTPUT_LIMIT", started=True)
    try:
        envelope = json.loads(stdout)
        valid = (type(code) is int and type(envelope) is dict and type(envelope.get("ok")) is bool
                 and ((code == 0 and envelope["ok"] is True and type(envelope.get("result")) is dict)
                      or (code == 1 and envelope["ok"] is False and type(envelope.get("error")) is dict))
                 and not stderr and stdout == encode(envelope) + b"\n")
        if not valid:
            raise ValueError
    except (ValueError, UnicodeError, TypeError):
        return _failure(payload, request, "SCREEN_RPC_RESPONSE_INVALID", started=True)
    return _response(payload, envelope, code)


def install_adapter_route(adapter):
    original = adapter.handle

    def handle(payload):
        if isinstance(payload, dict) and str(payload.get("request_type", "")).strip().lower() == REQUEST_TYPE:
            return handle_request(payload)
        return original(payload)

    adapter.handle = handle
