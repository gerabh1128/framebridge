"""Preserve the captured native protocol while guarding actual X11 dispatch."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
from . import display_context as display
from . import x11_text

META = ("input_may_have_been_sent", "do_not_replay", "performed", "display_context")

def _input_primitive(argv):
    args = list(argv)
    if len(args) < 2 or Path(str(args[0])).name != "xdotool":
        return False
    return args[1] not in {"getactivewindow", "getdisplaygeometry", "getmouselocation",
                          "getwindowgeometry", "getwindowname", "getwindowpid",
                          "search", "version", "help"}

def install_cua_guard(cua, *, env=None, checker=None, text_sender=None):
    env = os.environ if env is None else env
    checker = display.check if checker is None else checker
    original_run = cua._run
    original_handle = cua.handle_cua_message
    original_context = getattr(cua, "_action_context", None)
    text_sender = x11_text.bounded_type_text if text_sender is None else text_sender
    state = {"active": False, "input_started": False, "failed_code": "", "action_context": None,
             "type_keys": False}

    def verify():
        try:
            checker(env)
        except display.DisplayContextError as exc:
            state["failed_code"] = str(exc)
            raise cua.CuaTransportError(str(exc), str(exc)) from None

    if original_context is not None:
        def guarded_context(message):
            context = original_context(message)
            if (not isinstance(message, dict) or str(message.get("action", "")).strip().upper() != "TYPE"
                    or str(message.get("type_mode", "KEYS")).upper().strip() != "KEYS"):
                return context
            # Bind the original authorization/window/geometry gate, including
            # when the caller omitted an explicit expected window identifier.
            try:
                bound = (int(context["before_id"]),
                         (int(context["width"]), int(context["height"])))
                if bound[0] <= 0 or min(bound[1]) <= 0:
                    raise ValueError()
                state["action_context"] = bound
            except (KeyError, TypeError, ValueError):
                state["failed_code"] = "TYPE_PRIMITIVE_UNSUPPORTED"
                raise cua.CuaTransportError("TYPE_PRIMITIVE_UNSUPPORTED", "TYPE_PRIMITIVE_UNSUPPORTED") from None
            return context
        cua._action_context = guarded_context

    def before_input():
        verify()
        state["input_started"] = True

    def text_primitive(argv, timeout):
        args = list(argv)
        context = state.get("action_context")
        if (len(args) != 7 or args[:6] != ["xdotool", "type", "--clearmodifiers", "--delay", "10", "--"]
                or type(args[6]) is not str or context is None or not state.get("active")):
            state["failed_code"] = "TYPE_PRIMITIVE_UNSUPPORTED"
            raise cua.CuaTransportError("TYPE_PRIMITIVE_UNSUPPORTED", "TYPE_PRIMITIVE_UNSUPPORTED")
        try:
            text_sender(args[6], expected_window=context[0], expected_geometry=context[1],
                        before_input=before_input, timeout=timeout)
        except x11_text.TextError as error:
            state["failed_code"] = str(error)
            raise cua.CuaTransportError(str(error), str(error)) from None
        except cua.CuaTransportError:
            raise
        except Exception:
            state["failed_code"] = "TYPE_NATIVE_FAILURE"
            raise cua.CuaTransportError("TYPE_NATIVE_FAILURE", "TYPE_NATIVE_FAILURE") from None
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def guard_run(run, track_input):
        def guarded(argv, timeout=15):
            # The captured pointer helper calls its base runner after an internal
            # query. Guard that final runner too, immediately before subprocess.
            verify()
            args = list(argv)
            if (track_input and len(args) >= 2 and Path(str(args[0])).name == "xdotool"
                    and (str(args[1]).lower() == "type" or
                         (state["type_keys"] and any(str(arg).lower() == "type" for arg in args[1:])))):
                return text_primitive(args, timeout)
            if track_input and _input_primitive(argv):
                state["input_started"] = True
            return run(argv, timeout=timeout)
        return guarded

    primitive_run = getattr(cua, "_run_v3_base", None)
    if primitive_run is not None:
        cua._run_v3_base = guard_run(primitive_run, True)
    guarded_run = guard_run(original_run, primitive_run is None)

    def guarded_handle(message):
        state.update(active=True, input_started=False, failed_code="", action_context=None)
        state["type_keys"] = (isinstance(message, dict)
                              and str(message.get("action", "")).strip().upper() == "TYPE"
                              and str(message.get("type_mode", "KEYS")).upper().strip() == "KEYS")
        verify()
        result = original_handle(message)
        verify()
        return result

    cua._run = guarded_run
    cua.handle_cua_message = guarded_handle
    return state

def decorate_response(response, state, env):
    if not state.get("active") or not isinstance(response, dict):
        return response
    response = dict(response)
    may_have_sent = bool(state.get("input_started"))
    response["display_context"] = display.describe(env)
    response["input_may_have_been_sent"] = may_have_sent
    response["do_not_replay"] = bool(may_have_sent and
                                    (state.get("failed_code") or not response.get("executed")))
    if state.get("failed_code"):
        details = {"ok": False, "error_code": state["failed_code"],
                   "input_may_have_been_sent": may_have_sent,
                   "do_not_replay": may_have_sent, "result_discarded": True}
        if not may_have_sent:
            details["performed"] = False
        response.update(executed=False, exit_code=52, output=json.dumps(details),
                        error=json.dumps(details))
        if may_have_sent:
            response.pop("performed", None)
        else:
            response["performed"] = False
    elif response.get("executed"):
        response["performed"] = may_have_sent
    elif not may_have_sent:
        response["performed"] = False
    state["active"] = False
    return response

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def main():
    prefix = Path(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX", Path(__file__).resolve().parents[2]))
    host = prefix / "original/asus/framebridge-native/host/framebridge_host.py"
    if not host.is_file():
        host = prefix / "sources/original-asus/framebridge-native/host/framebridge_host.py"
    if not host.is_file():
        raise SystemExit("CAPTURED_NATIVE_HOST_NOT_FOUND")
    sys.path.insert(0, str(host.parent))
    cua = _load("framebridge_cua_linux", host.with_name("framebridge_cua_linux.py"))
    state = install_cua_guard(cua)
    original = _load("_framebridge_original_native_host", host)
    send = original.send_message
    original.send_message = lambda response: send(decorate_response(response, state, os.environ))
    original.main()

if __name__ == "__main__":
    main()
