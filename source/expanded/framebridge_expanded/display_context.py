"""Bind graphical CUA to a verified physical X11 session or labelled virtual display."""
from __future__ import annotations
import json
import shutil
from .real_screen.screen_capture import CaptureError, foreground, _x11_context

BINDING = "FRAMEBRIDGE_DISPLAY_BINDING"
GRAPHICAL_COMMANDS = ("wmctrl", "xdotool", "xwininfo", "xclip")
VIRTUAL_COMMANDS = ("Xvfb", "openbox", "xauth")


def missing_graphical_commands(env):
    path = env.get("PATH")
    missing = [name for name in GRAPHICAL_COMMANDS if not shutil.which(name, path=path)]
    if not (shutil.which("magick", path=path) or shutil.which("convert", path=path)):
        missing.append("imagemagick")
    return missing


def bind_text(env, profile, *, reason="PHYSICAL_X11_SESSION_UNAVAILABLE"):
    context = {"kind": "text", "physical": False, "explicit": False,
               "selection": "auto", "profile": str(profile), "display": "", "reason": reason}
    for name in ("DISPLAY", "XAUTHORITY", "GDK_BACKEND"):
        env.pop(name, None)
    env["XDG_SESSION_TYPE"] = "tty"
    env[BINDING] = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return context

FIELDS = ("session_id", "uid", "leader", "active_vt", "seat", "vtnr", "display")

class DisplayContextError(RuntimeError):
    pass

def _physical():
    current = foreground(allow_graphics=True)
    if current.session_type != "x11":
        raise DisplayContextError("PHYSICAL_X11_SESSION_UNAVAILABLE")
    display, authority = _x11_context(current, None, None)
    return {
        "kind": "physical-x11", "physical": True,
        "session_id": current.session_id, "uid": current.uid,
        "leader": current.leader, "active_vt": current.active_vt,
        "seat": current.seat, "vtnr": current.vtnr, "display": display,
        "authority": authority,
    }

def describe(env):
    try:
        context = json.loads(env.get(BINDING, "{}"))
    except (TypeError, ValueError):
        return {"kind": "unavailable", "physical": False, "error": "DISPLAY_BINDING_INVALID"}
    if not isinstance(context, dict):
        return {"kind": "unavailable", "physical": False, "error": "DISPLAY_BINDING_INVALID"}
    return {k: v for k, v in context.items() if k != "authority"}

def configure(env, headless, profile, *, refresh=False):
    profile = str(profile)
    try:
        previous = json.loads(env.get(BINDING, "{}"))
    except (TypeError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    # The manager and worker share the same immutable initial binding.
    mode_compatible = ((headless in ("yes", True) and previous.get("kind") == "virtual-x11" and previous.get("explicit") is True)
                       or (headless in ("no", False) and previous.get("kind") == "physical-x11")
                       or (headless == "auto" and previous.get("kind") in ("physical-x11", "text") and previous.get("selection") == "auto"))
    if not refresh and previous.get("profile") == profile and previous.get("kind") in ("physical-x11", "virtual-x11", "text") and mode_compatible:
        return env
    explicit = headless in ("yes", True)
    if explicit:
        context = {"kind": "virtual-x11", "physical": False, "explicit": True,
                   "display": env.get("DISPLAY", "")}
    else:
        try:
            context = _physical()
            if headless == "auto" and missing_graphical_commands(env):
                bind_text(env, profile, reason="GRAPHICAL_CUA_TOOLS_UNAVAILABLE")
                return env
            env["DISPLAY"] = context["display"]
            env["XDG_SESSION_TYPE"] = "x11"
            env["GDK_BACKEND"] = "x11"
            if context.get("authority"):
                env["XAUTHORITY"] = context["authority"]
            else:
                env.pop("XAUTHORITY", None)
        except (CaptureError, DisplayContextError, OSError) as exc:
            code = getattr(exc, "code", str(exc)) or "PHYSICAL_X11_SESSION_UNAVAILABLE"
            if headless == "auto":
                bind_text(env, profile, reason=code)
                return env
            context = {"kind": "unavailable", "physical": False, "explicit": False,
                       "display": "", "reason": code}
    context["selection"] = "yes" if explicit else ("auto" if headless == "auto" else "no")
    context["profile"] = profile
    env[BINDING] = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return env

def bind_virtual(env, profile, *, explicit):
    context = {"kind": "virtual-x11", "physical": False, "explicit": bool(explicit),
               "profile": str(profile), "display": env.get("DISPLAY", "")}
    env[BINDING] = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return context

def prepare(env):
    """Select the current real desktop once per request; guards then pin it."""
    try:
        previous = json.loads(env.get(BINDING, "{}"))
    except (TypeError, ValueError):
        return
    if isinstance(previous, dict) and previous.get("selection") == "auto":
        configure(env, "auto", previous.get("profile", ""), refresh=True)
        current = json.loads(env[BINDING])
        changed = current.get("kind") != previous.get("kind") or any(current.get(k) != previous.get(k) for k in FIELDS)
        if previous.get("needs_observation") or changed:
            current["needs_observation"] = True
            env[BINDING] = json.dumps(current, sort_keys=True, separators=(",", ":"))


def observed(env):
    current = check(env)
    if current.get("needs_observation"):
        current = dict(current)
        current.pop("needs_observation", None)
        env[BINDING] = json.dumps(current, sort_keys=True, separators=(",", ":"))


def check(env):
    try:
        expected = json.loads(env.get(BINDING, "{}"))
    except (TypeError, ValueError):
        raise DisplayContextError("DISPLAY_BINDING_INVALID") from None
    if not isinstance(expected, dict):
        raise DisplayContextError("DISPLAY_BINDING_INVALID")
    if expected.get("kind") == "physical-x11":
        try:
            current = _physical()
        except (CaptureError, OSError, DisplayContextError):
            raise DisplayContextError("CUA_FOREGROUND_SESSION_CHANGED") from None
        if any(current.get(k) != expected.get(k) for k in FIELDS):
            raise DisplayContextError("CUA_FOREGROUND_SESSION_CHANGED")
        if env.get("DISPLAY") != expected.get("display"):
            raise DisplayContextError("CUA_DISPLAY_BINDING_MISMATCH")
    elif expected.get("kind") == "virtual-x11":
        if not expected.get("display") or env.get("DISPLAY") != expected["display"]:
            raise DisplayContextError("VIRTUAL_DISPLAY_BINDING_MISMATCH")
    else:
        raise DisplayContextError("CUA_DISPLAY_UNAVAILABLE")
    return expected


def private_display_required(env, headless, available):
    physical = describe(env).get("kind") == "physical-x11"
    if headless in ("no", False) and (not physical or not available):
        raise DisplayContextError("PHYSICAL_X11_UNAVAILABLE_NO_VIRTUAL_FALLBACK")
    return headless in ("yes", True)
