"""Original same-UID adapter with context checks at CUA dispatch."""
from __future__ import annotations
import importlib.util
import base64
import hashlib
import os
from pathlib import Path
import sys
import threading
from . import display_context as display
from .native_host import META

def _blocked(error, may_have_sent=False):
    value = {"ok": False, "error": str(error), "mode": "CUA",
             "input_may_have_been_sent": bool(may_have_sent),
             "do_not_replay": bool(may_have_sent), "result_discarded": True}
    if not may_have_sent:
        value["performed"] = False
    return value

def _successful_observation(native):
    if not isinstance(native, dict) or native.get("executed") is not True:
        return False
    output = native.get("output")
    if not isinstance(output, str) or len(output) > 2_000_000:
        return False
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if (fields.get("RESULTADO_FINAL") != "CUA_VISUAL_OBSERVE_PASS" or fields.get("VISUAL_CAPTURE_GATE") != "PASS" or fields.get("MIME") != "image/jpeg"):
        return False
    try:
        data = base64.b64decode(fields.get("BASE64", ""), validate=True)
        return (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
                and len(data) == int(fields.get("BYTES", "0"))
                and hashlib.sha256(data).hexdigest() == fields.get("SHA256"))
    except (ValueError, TypeError):
        return False


def install_adapter_guard(adapter, *, env=None, checker=None):
    env = os.environ if env is None else env
    checker = display.check if checker is None else checker
    original_handle = adapter.handle_cua_request
    original_roundtrip = adapter.native_cua_roundtrip
    local = threading.local()
    request_lock = threading.RLock()

    def native_roundtrip(payload, timeout_s=60):
        response = original_roundtrip(payload, timeout_s=timeout_s)
        local.native = response
        return response

    def guarded_handle(request, job_id):
        local.native = None
        try:
            display.prepare(env)
            context = checker(env)
        except display.DisplayContextError as exc:
            return _blocked(exc)
        action = str(request.get("cua", {}).get("action", "OBSERVE")).upper().strip()
        if isinstance(context, dict) and context.get("needs_observation") and action != "OBSERVE":
            result = _blocked("CUA_DISPLAY_CHANGED_OBSERVE_REQUIRED")
            result["display_context"] = display.describe(env)
            return result
        try:
            result = original_handle(request, job_id)
        except Exception as exc:
            # The native child may have acted before a timeout or invalid reply.
            # Never invite automatic replay of an uncertain input action.
            is_input = str(request.get("cua", {}).get("action", "")).upper() != "OBSERVE"
            code = str(exc)
            if not code or len(code) > 100 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:" for c in code):
                code = "CUA_NATIVE_EXECUTION_UNCERTAIN"
            return _blocked(code, is_input)
        native = local.native or {}
        result = dict(result)
        for key in META:
            if key in native:
                result[key] = native[key]
        if "display_context" not in result:
            result["display_context"] = display.describe(env)
        try:
            checker(env)
        except display.DisplayContextError as exc:
            is_input = str(request.get("cua", {}).get("action", "")).upper() != "OBSERVE"
            return _blocked(exc, native.get("input_may_have_been_sent", is_input))
        if action == "OBSERVE" and result.get("ok") is True and _successful_observation(native):
            try:
                display.observed(env)
            except display.DisplayContextError as exc:
                return _blocked(exc)
            result["display_context"] = display.describe(env)
        return result

    def handle(request, job_id):
        with request_lock:
            return guarded_handle(request, job_id)

    adapter.native_cua_roundtrip = native_roundtrip
    adapter.handle_cua_request = handle

def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv) != ["serve"]:
        raise SystemExit("Usage: python3 -m framebridge_expanded.adapter serve")
    prefix = Path(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX", Path(__file__).resolve().parents[2]))
    path = prefix / "original/asus/tool-adapter/bin/framebridge_tool_adapter.py"
    if not path.is_file():
        path = prefix / "sources/original-asus/tool-adapter/bin/framebridge_tool_adapter.py"
    spec = importlib.util.spec_from_file_location("_framebridge_original_adapter", path)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    install_adapter_guard(original)
    from .real_screen.screen_transport import install_adapter_route
    install_adapter_route(original)
    original.serve()

if __name__ == "__main__":
    main()
