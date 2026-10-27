from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile
import time


class CuaTransportError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = str(code)
        self.message = str(message if message is not None else code)
        super().__init__(self.message)


CUA_PROTOCOL_VERSION = "2"

DANGEROUS = {
    "DELETE",
    "SEND",
    "PURCHASE",
    "AUTH_CONFIRM",
    "SYSTEM_CHANGE",
    "IRREVERSIBLE",
}

SECRET_SEMANTICS = {
    "SECRET_ENTRY",
    "PASSWORD",
    "PRIVATE_KEY",
    "TOKEN",
}

BUTTON_MAP = {
    "left": "1",
    "middle": "2",
    "right": "3",
    "1": "1",
    "2": "2",
    "3": "3",
}

SCROLL_BUTTON = {
    "UP": "4",
    "DOWN": "5",
    "LEFT": "6",
    "RIGHT": "7",
}

KEY_RE = re.compile(r"^[A-Za-z0-9_+:-]{1,64}$")


def _run(argv, timeout=15):
    try:
        p = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        raise CuaTransportError(
            "CUA_BACKEND_ERROR:" + type(exc).__name__
        )

    return p


def _require_x11():
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
        raise CuaTransportError("X11_SESSION_REQUIRED")

    if not os.environ.get("DISPLAY"):
        raise CuaTransportError("DISPLAY_NOT_AVAILABLE")


def _geometry():
    p = _run(
        ["xdotool", "getdisplaygeometry"],
        timeout=5,
    )

    if p.returncode != 0:
        raise CuaTransportError("DISPLAY_GEOMETRY_FAILED")

    parts = p.stdout.strip().split()

    if len(parts) != 2:
        raise CuaTransportError("DISPLAY_GEOMETRY_INVALID")

    try:
        width = int(parts[0])
        height = int(parts[1])
    except Exception:
        raise CuaTransportError("DISPLAY_GEOMETRY_INVALID")

    if width <= 0 or height <= 0:
        raise CuaTransportError("DISPLAY_GEOMETRY_INVALID")

    return width, height


def _cursor():
    p = _run(
        ["xdotool", "getmouselocation", "--shell"],
        timeout=5,
    )

    values = {}

    if p.returncode == 0:
        for line in p.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()

    try:
        return (
            int(values.get("X", "-1")),
            int(values.get("Y", "-1")),
        )
    except Exception:
        return -1, -1


def _safe_title(value):
    return " ".join(str(value).split())[:300]


def _active_window():
    p = _run(
        ["xdotool", "getactivewindow"],
        timeout=5,
    )

    if p.returncode != 0:
        return -1, ""

    try:
        window_id = int(p.stdout.strip(), 0)
    except Exception:
        return -1, ""

    q = _run(
        [
            "xdotool",
            "getwindowname",
            str(window_id),
        ],
        timeout=5,
    )

    title = ""
    if q.returncode == 0:
        title = _safe_title(q.stdout)

    return window_id, title


def _preview_geometry(width, height):
    display_w = 960 if width > 960 else width
    display_h = round(height * display_w / width)
    return display_w, display_h


def _semantic(msg):
    return str(
        msg.get(
            "semantic_action",
            msg.get("semantic", "GENERIC"),
        )
    ).upper().strip() or "GENERIC"


def _require_chat_authorization(msg, semantic):
    if semantic in SECRET_SEMANTICS:
        raise CuaTransportError("SECRET_ENTRY_FORBIDDEN")

    if semantic in DANGEROUS:
        raise CuaTransportError("SPECIFIC_CONFIRMATION_REQUIRED")

    authorized = str(
        msg.get("authorized", "NO")
    ).upper().strip()

    if authorized != "YES":
        raise CuaTransportError("CHAT_AUTHORIZATION_REQUIRED")


def _parse_window_id(value):
    if value is None or value == "":
        return None

    try:
        if isinstance(value, str):
            return int(value.strip(), 0)
        return int(value)
    except Exception:
        raise CuaTransportError("EXPECTED_WINDOW_ID_INVALID")


def _guard_expected_window(msg, current_window_id):
    expected = _parse_window_id(
        msg.get("expected_window_id")
    )

    if expected is None:
        return "NOT_REQUESTED"

    if current_window_id != expected:
        raise CuaTransportError("ACTIVE_WINDOW_CHANGED")

    return "PASS"


def _guard_expected_geometry(msg, width, height):
    ew = msg.get("expected_source_width")
    eh = msg.get("expected_source_height")

    if ew in (None, "") and eh in (None, ""):
        return "NOT_REQUESTED"

    try:
        ew_i = width if ew in (None, "") else int(ew)
        eh_i = height if eh in (None, "") else int(eh)
    except Exception:
        raise CuaTransportError(
            "EXPECTED_SOURCE_GEOMETRY_INVALID"
        )

    if ew_i != width or eh_i != height:
        raise CuaTransportError("DISPLAY_GEOMETRY_CHANGED")

    return "PASS"


def _coord_space(msg):
    space = str(
        msg.get("coord_space", "ROOT")
    ).upper().strip()

    if space not in {"ROOT", "PREVIEW"}:
        raise CuaTransportError(
            "COORDINATE_SPACE_INVALID"
        )

    return space


def _int_coord(msg, key):
    try:
        return int(msg.get(key))
    except Exception:
        raise CuaTransportError(
            key.upper() + "_COORDINATE_INVALID"
        )


def _resolve_point(
    msg,
    width,
    height,
    x_key="x",
    y_key="y",
):
    x = _int_coord(msg, x_key)
    y = _int_coord(msg, y_key)
    space = _coord_space(msg)

    if space == "ROOT":
        if not (0 <= x < width and 0 <= y < height):
            raise CuaTransportError(
                "COORDINATES_OUT_OF_BOUNDS"
            )
        return x, y, x, y, space, width, height

    default_pw, default_ph = _preview_geometry(
        width,
        height,
    )

    try:
        preview_w = int(
            msg.get("preview_width", default_pw)
        )
        preview_h = int(
            msg.get("preview_height", default_ph)
        )
    except Exception:
        raise CuaTransportError(
            "PREVIEW_GEOMETRY_INVALID"
        )

    if preview_w <= 0 or preview_h <= 0:
        raise CuaTransportError(
            "PREVIEW_GEOMETRY_INVALID"
        )

    if not (
        0 <= x < preview_w
        and 0 <= y < preview_h
    ):
        raise CuaTransportError(
            "PREVIEW_COORDINATES_OUT_OF_BOUNDS"
        )

    root_x = min(
        width - 1,
        int(x * width / preview_w),
    )
    root_y = min(
        height - 1,
        int(y * height / preview_h),
    )

    return (
        x,
        y,
        root_x,
        root_y,
        space,
        preview_w,
        preview_h,
    )


def _action_context(msg):
    _require_x11()

    semantic = _semantic(msg)
    _require_chat_authorization(
        msg,
        semantic,
    )

    width, height = _geometry()
    geometry_gate = _guard_expected_geometry(
        msg,
        width,
        height,
    )

    before_id, before_title = _active_window()
    window_gate = _guard_expected_window(
        msg,
        before_id,
    )

    return {
        "semantic": semantic,
        "width": width,
        "height": height,
        "before_id": before_id,
        "before_title": before_title,
        "geometry_gate": geometry_gate,
        "window_gate": window_gate,
    }


def _evidence_lines(ctx):
    after_id, after_title = _active_window()
    cx, cy = _cursor()

    return [
        "CUA_PROTOCOL_VERSION=" + CUA_PROTOCOL_VERSION,
        "CHAT_AUTHORIZATION_GATE=PASS",
        "SEMANTIC=" + ctx["semantic"],
        "SOURCE_WIDTH=" + str(ctx["width"]),
        "SOURCE_HEIGHT=" + str(ctx["height"]),
        "EXPECTED_SOURCE_GEOMETRY_GATE="
        + ctx["geometry_gate"],
        "EXPECTED_WINDOW_GATE=" + ctx["window_gate"],
        "ACTIVE_WINDOW_ID_BEFORE="
        + str(ctx["before_id"]),
        "ACTIVE_WINDOW_TITLE_BEFORE="
        + _safe_title(ctx["before_title"]),
        "ACTIVE_WINDOW_ID_AFTER="
        + str(after_id),
        "ACTIVE_WINDOW_TITLE_AFTER="
        + _safe_title(after_title),
        "CURSOR_X=" + str(cx),
        "CURSOR_Y=" + str(cy),
    ]


def _capture_visual_jpeg():
    _require_x11()

    width, height = _geometry()
    cx, cy = _cursor()
    window_id, title = _active_window()

    with tempfile.TemporaryDirectory(
        prefix="framebridge-cua-"
    ) as td:
        td = Path(td)
        png = td / "screen.png"
        jpg = td / "screen.jpg"

        p = _run(
            [
                "import",
                "-window",
                "root",
                str(png),
            ],
            timeout=15,
        )

        if p.returncode != 0 or not png.is_file():
            raise CuaTransportError(
                "VISUAL_CAPTURE_FAILED"
            )

        p = _run(
            [
                "convert",
                str(png),
                "-resize",
                "960x>",
                "-quality",
                "82",
                str(jpg),
            ],
            timeout=15,
        )

        if p.returncode != 0 or not jpg.is_file():
            raise CuaTransportError(
                "VISUAL_JPEG_ENCODE_FAILED"
            )

        data = jpg.read_bytes()

    encoded = base64.b64encode(
        data
    ).decode("ascii")

    display_w, display_h = _preview_geometry(
        width,
        height,
    )

    lines = [
        "RESULTADO_FINAL=CUA_VISUAL_OBSERVE_PASS",
        "VISUAL_CAPTURE_GATE=PASS",
        "CUA_PROTOCOL_VERSION=" + CUA_PROTOCOL_VERSION,
        "CHAT_AUTHORIZATION_MODE=CHAT",
        "MIME=image/jpeg",
        "BYTES=" + str(len(data)),
        "SHA256="
        + hashlib.sha256(data).hexdigest(),
        "WIDTH=" + str(display_w),
        "HEIGHT=" + str(display_h),
        "SOURCE_WIDTH=" + str(width),
        "SOURCE_HEIGHT=" + str(height),
        "COORD_SPACE_DEFAULT=ROOT",
        "COORD_SPACE_AVAILABLE=ROOT,PREVIEW",
        "CURSOR_X=" + str(cx),
        "CURSOR_Y=" + str(cy),
        "ACTIVE_WINDOW_ID=" + str(window_id),
        "WINDOW_TITLE=" + _safe_title(title),
        "BASE64=" + encoded,
    ]

    return {
        "ok": True,
        "action": "OBSERVE",
        "_frame_output": "\n".join(lines),
    }


def _button(msg):
    button = str(
        msg.get("button", "left")
    ).lower().strip()

    if button not in BUTTON_MAP:
        raise CuaTransportError(
            "CLICK_BUTTON_INVALID"
        )

    return button, BUTTON_MAP[button]


def _click_common(msg, repeat, action_name=None):
    ctx = _action_context(msg)

    (
        requested_x,
        requested_y,
        root_x,
        root_y,
        space,
        space_w,
        space_h,
    ) = _resolve_point(
        msg,
        ctx["width"],
        ctx["height"],
    )

    button, button_number = _button(msg)

    argv = [
        "xdotool",
        "mousemove",
        "--sync",
        str(root_x),
        str(root_y),
        "click",
    ]

    if repeat > 1:
        argv.extend(
            [
                "--repeat",
                str(repeat),
                "--delay",
                "120",
            ]
        )

    argv.append(button_number)

    p = _run(
        argv,
        timeout=10,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "CLICK_EXECUTION_FAILED"
        )

    action = action_name or (
        "DOUBLE_CLICK"
        if repeat == 2
        else "CLICK"
    )

    lines = [
        "RESULTADO_FINAL=CUA_"
        + action
        + "_PASS",
        action + "_GATE=PASS",
        "ACTION=" + action,
        "COORD_SPACE=" + space,
        "COORD_SPACE_WIDTH=" + str(space_w),
        "COORD_SPACE_HEIGHT=" + str(space_h),
        "REQUESTED_X=" + str(requested_x),
        "REQUESTED_Y=" + str(requested_y),
        "ROOT_X=" + str(root_x),
        "ROOT_Y=" + str(root_y),
        "BUTTON=" + button,
    ]
    lines.extend(_evidence_lines(ctx))

    return {
        "ok": True,
        "action": action,
        "_frame_output": "\n".join(lines),
    }


def _click(msg):
    return _click_common(msg, 1)


def _double_click(msg):
    return _click_common(msg, 2)


def _right_click(msg):
    right_msg = dict(msg)
    right_msg["button"] = "right"
    return _click_common(right_msg, 1, "RIGHT_CLICK")


def _type(msg):
    ctx = _action_context(msg)

    text = str(
        msg.get("text", "")
    )

    if not text:
        raise CuaTransportError(
            "TYPE_TEXT_EMPTY"
        )

    if len(text) > 4096:
        raise CuaTransportError(
            "TYPE_TEXT_TOO_LONG"
        )

    p = _run(
        [
            "xdotool",
            "type",
            "--clearmodifiers",
            "--delay",
            "1",
            "--",
            text,
        ],
        timeout=20,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "TYPE_EXECUTION_FAILED"
        )

    lines = [
        "RESULTADO_FINAL=CUA_TYPE_PASS",
        "TYPE_GATE=PASS",
        "ACTION=TYPE",
        "CHAR_COUNT=" + str(len(text)),
    ]
    lines.extend(_evidence_lines(ctx))

    return {
        "ok": True,
        "action": "TYPE",
        "_frame_output": "\n".join(lines),
    }


def _normalize_key_spec(key):
    parts = [part.strip() for part in key.split("+")]
    if not parts or any(not part for part in parts):
        raise CuaTransportError("KEY_INVALID")

    modifier_map = {
        "CTRL": "ctrl",
        "CONTROL": "ctrl",
        "ALT": "alt",
        "SHIFT": "shift",
        "SUPER": "super",
        "META": "super",
    }
    named_key_map = {
        "BACKSPACE": "BackSpace",
        "ENTER": "Return",
        "RETURN": "Return",
        "ESC": "Escape",
        "ESCAPE": "Escape",
        "TAB": "Tab",
        "DELETE": "Delete",
        "DEL": "Delete",
        "INSERT": "Insert",
        "HOME": "Home",
        "END": "End",
        "LEFT": "Left",
        "RIGHT": "Right",
        "UP": "Up",
        "DOWN": "Down",
        "PAGEUP": "Prior",
        "PAGEDOWN": "Next",
        "SPACE": "space",
    }
    normalized = []
    explicit_shift = False
    for part in parts:
        upper = part.upper()
        if upper in modifier_map:
            mapped = modifier_map[upper]
            normalized.append(mapped)
            explicit_shift = explicit_shift or mapped == "shift"
            continue
        if upper in named_key_map:
            normalized.append(named_key_map[upper])
            continue
        if len(part) == 1 and part.isalpha() and not explicit_shift:
            normalized.append(part.lower())
        else:
            normalized.append(part)
    return "+".join(normalized)


def _key(msg):
    ctx = _action_context(msg)

    key = str(
        msg.get("key", "")
    ).strip()

    if not key or not KEY_RE.fullmatch(key):
        raise CuaTransportError("KEY_INVALID")

    normalized_key = _normalize_key_spec(key)

    p = _run(
        [
            "xdotool",
            "key",
            "--clearmodifiers",
            normalized_key,
        ],
        timeout=10,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "KEY_EXECUTION_FAILED"
        )

    lines = [
        "RESULTADO_FINAL=CUA_KEY_PASS",
        "KEY_GATE=PASS",
        "ACTION=KEY",
        "KEY=" + key,
        "KEY_NORMALIZED=" + normalized_key,
    ]
    lines.extend(_evidence_lines(ctx))

    return {
        "ok": True,
        "action": "KEY",
        "_frame_output": "\n".join(lines),
    }


def _scroll(msg):
    ctx = _action_context(msg)

    direction = str(
        msg.get("direction", "DOWN")
    ).upper().strip()

    if direction not in SCROLL_BUTTON:
        raise CuaTransportError(
            "SCROLL_DIRECTION_INVALID"
        )

    # R8: SCROLL follows the public MCP contract: `steps` is canonical.
    # `clicks` remains a backward-compatible fallback for older callers.
    try:
        raw_steps = msg.get("steps")
        if raw_steps in (None, ""):
            raw_steps = msg.get("clicks", 3)
        clicks = int(raw_steps)
    except Exception:
        raise CuaTransportError(
            "SCROLL_STEPS_INVALID"
        )

    if not (1 <= clicks <= 20):
        raise CuaTransportError(
            "SCROLL_STEPS_INVALID"
        )

    point_lines = []

    x_present = msg.get("x") not in (None, "")
    y_present = msg.get("y") not in (None, "")

    if x_present != y_present:
        raise CuaTransportError(
            "SCROLL_POINT_INCOMPLETE"
        )

    if x_present:
        (
            requested_x,
            requested_y,
            root_x,
            root_y,
            space,
            space_w,
            space_h,
        ) = _resolve_point(
            msg,
            ctx["width"],
            ctx["height"],
        )

        p = _run(
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(root_x),
                str(root_y),
            ],
            timeout=10,
        )

        if p.returncode != 0:
            raise CuaTransportError(
                "SCROLL_POINTER_MOVE_FAILED"
            )

        point_lines = [
            "COORD_SPACE=" + space,
            "COORD_SPACE_WIDTH=" + str(space_w),
            "COORD_SPACE_HEIGHT=" + str(space_h),
            "REQUESTED_X=" + str(requested_x),
            "REQUESTED_Y=" + str(requested_y),
            "ROOT_X=" + str(root_x),
            "ROOT_Y=" + str(root_y),
        ]

    p = _run(
        [
            "xdotool",
            "click",
            "--repeat",
            str(clicks),
            "--delay",
            "35",
            SCROLL_BUTTON[direction],
        ],
        timeout=10,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "SCROLL_EXECUTION_FAILED"
        )

    lines = [
        "RESULTADO_FINAL=CUA_SCROLL_PASS",
        "SCROLL_GATE=PASS",
        "ACTION=SCROLL",
        "DIRECTION=" + direction,
        "STEPS=" + str(clicks),
        "CLICKS=" + str(clicks),
    ]
    lines.extend(point_lines)
    lines.extend(_evidence_lines(ctx))

    return {
        "ok": True,
        "action": "SCROLL",
        "_frame_output": "\n".join(lines),
    }


def _drag(msg):
    ctx = _action_context(msg)

    (
        req_x1,
        req_y1,
        root_x1,
        root_y1,
        space1,
        space_w1,
        space_h1,
    ) = _resolve_point(
        msg,
        ctx["width"],
        ctx["height"],
        "x1",
        "y1",
    )

    (
        req_x2,
        req_y2,
        root_x2,
        root_y2,
        space2,
        space_w2,
        space_h2,
    ) = _resolve_point(
        msg,
        ctx["width"],
        ctx["height"],
        "x2",
        "y2",
    )

    if (
        space1 != space2
        or space_w1 != space_w2
        or space_h1 != space_h2
    ):
        raise CuaTransportError(
            "DRAG_COORDINATE_SPACE_MISMATCH"
        )

    button, button_number = _button(msg)

    try:
        steps = int(
            msg.get("steps", 8)
        )
        duration_ms = int(
            msg.get("duration_ms", 320)
        )
    except Exception:
        raise CuaTransportError(
            "DRAG_TIMING_INVALID"
        )

    if not (1 <= steps <= 40):
        raise CuaTransportError(
            "DRAG_STEPS_INVALID"
        )

    if not (0 <= duration_ms <= 5000):
        raise CuaTransportError(
            "DRAG_DURATION_INVALID"
        )

    p = _run(
        [
            "xdotool",
            "mousemove",
            "--sync",
            str(root_x1),
            str(root_y1),
        ],
        timeout=10,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "DRAG_START_MOVE_FAILED"
        )

    p = _run(
        [
            "xdotool",
            "mousedown",
            button_number,
        ],
        timeout=5,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "DRAG_MOUSEDOWN_FAILED"
        )

    failure = None

    try:
        delay = (
            duration_ms / 1000.0 / steps
            if steps
            else 0.0
        )

        for i in range(1, steps + 1):
            x = round(
                root_x1
                + (root_x2 - root_x1) * i / steps
            )
            y = round(
                root_y1
                + (root_y2 - root_y1) * i / steps
            )

            p = _run(
                [
                    "xdotool",
                    "mousemove",
                    "--sync",
                    str(x),
                    str(y),
                ],
                timeout=10,
            )

            if p.returncode != 0:
                failure = "DRAG_MOVE_FAILED"
                break

            if delay > 0:
                time.sleep(delay)
    finally:
        q = _run(
            [
                "xdotool",
                "mouseup",
                button_number,
            ],
            timeout=5,
        )

        if q.returncode != 0 and failure is None:
            failure = "DRAG_MOUSEUP_FAILED"

    if failure:
        raise CuaTransportError(failure)

    lines = [
        "RESULTADO_FINAL=CUA_DRAG_PASS",
        "DRAG_GATE=PASS",
        "ACTION=DRAG",
        "COORD_SPACE=" + space1,
        "COORD_SPACE_WIDTH=" + str(space_w1),
        "COORD_SPACE_HEIGHT=" + str(space_h1),
        "REQUESTED_X1=" + str(req_x1),
        "REQUESTED_Y1=" + str(req_y1),
        "REQUESTED_X2=" + str(req_x2),
        "REQUESTED_Y2=" + str(req_y2),
        "ROOT_X1=" + str(root_x1),
        "ROOT_Y1=" + str(root_y1),
        "ROOT_X2=" + str(root_x2),
        "ROOT_Y2=" + str(root_y2),
        "BUTTON=" + button,
        "STEPS=" + str(steps),
        "DURATION_MS=" + str(duration_ms),
    ]
    lines.extend(_evidence_lines(ctx))

    return {
        "ok": True,
        "action": "DRAG",
        "_frame_output": "\n".join(lines),
    }


def handle_cua_message(msg):
    if not isinstance(msg, dict):
        raise CuaTransportError(
            "CUA_MESSAGE_INVALID"
        )

    target = str(
        msg.get("target", "ASUS")
    ).upper()

    if target != "ASUS":
        raise CuaTransportError(
            "TARGET_MISMATCH"
        )

    action = str(
        msg.get("action", "OBSERVE")
    ).upper().strip()

    if action == "OBSERVE":
        return _capture_visual_jpeg()

    if action == "CLICK":
        return _click(msg)

    if action == "RIGHT_CLICK":
        return _right_click(msg)

    if action == "DOUBLE_CLICK":
        return _double_click(msg)

    if action == "TYPE":
        return _type(msg)

    if action == "KEY":
        return _key(msg)

    if action == "SCROLL":
        return _scroll(msg)

    if action == "DRAG":
        return _drag(msg)

    raise CuaTransportError(
        "CUA_ACTION_UNSUPPORTED"
    )


# ==================================================================
# FRAMEBRIDGE CUA V3 CANDIDATE OVERRIDES
# Window-local coordinate contract + window scoped observation.
# Candidate only: appended definitions override v2 globals at runtime.
# ==================================================================

CUA_PROTOCOL_VERSION = "3"


def _window_geometry(window_id):
    if int(window_id) <= 0:
        raise CuaTransportError(
            "WINDOW_GEOMETRY_WINDOW_INVALID"
        )

    p = _run(
        [
            "xwininfo",
            "-id",
            str(int(window_id)),
        ],
        timeout=5,
    )

    if p.returncode != 0:
        raise CuaTransportError(
            "WINDOW_GEOMETRY_FAILED"
        )

    values = {}

    patterns = {
        "x": r"Absolute upper-left X:\s+(-?\d+)",
        "y": r"Absolute upper-left Y:\s+(-?\d+)",
        "width": r"Width:\s+(\d+)",
        "height": r"Height:\s+(\d+)",
    }

    for key, pattern in patterns.items():
        m = re.search(pattern, p.stdout)

        if not m:
            raise CuaTransportError(
                "WINDOW_GEOMETRY_INVALID"
            )

        values[key] = int(m.group(1))

    if (
        values["width"] <= 0
        or values["height"] <= 0
    ):
        raise CuaTransportError(
            "WINDOW_GEOMETRY_INVALID"
        )

    return values


def _coord_space(msg):
    space = str(
        msg.get("coord_space", "ROOT")
    ).upper().strip()

    if space not in {
        "ROOT",
        "PREVIEW",
        "WINDOW",
        "WINDOW_PREVIEW",
    }:
        raise CuaTransportError(
            "COORDINATE_SPACE_INVALID"
        )

    return space


def _resolve_point(
    msg,
    width,
    height,
    x_key="x",
    y_key="y",
):
    x = _int_coord(msg, x_key)
    y = _int_coord(msg, y_key)
    space = _coord_space(msg)

    if space == "ROOT":
        if not (
            0 <= x < width
            and 0 <= y < height
        ):
            raise CuaTransportError(
                "COORDINATES_OUT_OF_BOUNDS"
            )

        return (
            x,
            y,
            x,
            y,
            space,
            width,
            height,
        )

    if space in {
        "WINDOW",
        "WINDOW_PREVIEW",
    }:
        rect = msg.get(
            "_resolved_window_rect"
        )

        if not isinstance(rect, dict):
            raise CuaTransportError(
                "WINDOW_GEOMETRY_REQUIRED"
            )

        wx = int(rect["x"])
        wy = int(rect["y"])
        ww = int(rect["width"])
        wh = int(rect["height"])

        if space == "WINDOW":
            if not (
                0 <= x < ww
                and 0 <= y < wh
            ):
                raise CuaTransportError(
                    "WINDOW_COORDINATES_OUT_OF_BOUNDS"
                )

            root_x = wx + x
            root_y = wy + y
            space_w = ww
            space_h = wh

        else:
            default_pw, default_ph = (
                _preview_geometry(
                    ww,
                    wh,
                )
            )

            try:
                preview_w = int(
                    msg.get(
                        "preview_width",
                        default_pw,
                    )
                )
                preview_h = int(
                    msg.get(
                        "preview_height",
                        default_ph,
                    )
                )
            except Exception:
                raise CuaTransportError(
                    "PREVIEW_GEOMETRY_INVALID"
                )

            if (
                preview_w <= 0
                or preview_h <= 0
            ):
                raise CuaTransportError(
                    "PREVIEW_GEOMETRY_INVALID"
                )

            if not (
                0 <= x < preview_w
                and 0 <= y < preview_h
            ):
                raise CuaTransportError(
                    "PREVIEW_COORDINATES_OUT_OF_BOUNDS"
                )

            local_x = min(
                ww - 1,
                int(
                    x
                    * ww
                    / preview_w
                ),
            )

            local_y = min(
                wh - 1,
                int(
                    y
                    * wh
                    / preview_h
                ),
            )

            root_x = wx + local_x
            root_y = wy + local_y
            space_w = preview_w
            space_h = preview_h

        if not (
            0 <= root_x < width
            and 0 <= root_y < height
        ):
            raise CuaTransportError(
                "WINDOW_COORDINATES_OFFSCREEN"
            )

        return (
            x,
            y,
            root_x,
            root_y,
            space,
            space_w,
            space_h,
        )

    default_pw, default_ph = (
        _preview_geometry(
            width,
            height,
        )
    )

    try:
        preview_w = int(
            msg.get(
                "preview_width",
                default_pw,
            )
        )
        preview_h = int(
            msg.get(
                "preview_height",
                default_ph,
            )
        )
    except Exception:
        raise CuaTransportError(
            "PREVIEW_GEOMETRY_INVALID"
        )

    if (
        preview_w <= 0
        or preview_h <= 0
    ):
        raise CuaTransportError(
            "PREVIEW_GEOMETRY_INVALID"
        )

    if not (
        0 <= x < preview_w
        and 0 <= y < preview_h
    ):
        raise CuaTransportError(
            "PREVIEW_COORDINATES_OUT_OF_BOUNDS"
        )

    root_x = min(
        width - 1,
        int(
            x
            * width
            / preview_w
        ),
    )

    root_y = min(
        height - 1,
        int(
            y
            * height
            / preview_h
        ),
    )

    return (
        x,
        y,
        root_x,
        root_y,
        space,
        preview_w,
        preview_h,
    )


def _action_context(msg):
    _require_x11()

    semantic = _semantic(msg)

    _require_chat_authorization(
        msg,
        semantic,
    )

    width, height = _geometry()

    before_id, before_title = (
        _active_window()
    )

    window_gate = (
        _guard_expected_window(
            msg,
            before_id,
        )
    )

    space = _coord_space(msg)

    window_rect = None
    source_width = width
    source_height = height

    if space in {
        "WINDOW",
        "WINDOW_PREVIEW",
    }:
        window_rect = (
            _window_geometry(
                before_id
            )
        )

        msg[
            "_resolved_window_rect"
        ] = dict(window_rect)

        source_width = int(
            window_rect["width"]
        )

        source_height = int(
            window_rect["height"]
        )

    geometry_gate = (
        _guard_expected_geometry(
            msg,
            source_width,
            source_height,
        )
    )

    return {
        "semantic": semantic,
        "width": width,
        "height": height,
        "source_width": source_width,
        "source_height": source_height,
        "before_id": before_id,
        "before_title": before_title,
        "geometry_gate": geometry_gate,
        "window_gate": window_gate,
        "window_rect": window_rect,
        "coord_space": space,
    }


def _evidence_lines(ctx):
    after_id, after_title = (
        _active_window()
    )

    cx, cy = _cursor()

    lines = [
        "CUA_PROTOCOL_VERSION="
        + CUA_PROTOCOL_VERSION,
        "CHAT_AUTHORIZATION_GATE=PASS",
        "SEMANTIC="
        + ctx["semantic"],
        "SOURCE_WIDTH="
        + str(ctx["source_width"]),
        "SOURCE_HEIGHT="
        + str(ctx["source_height"]),
        "DISPLAY_WIDTH="
        + str(ctx["width"]),
        "DISPLAY_HEIGHT="
        + str(ctx["height"]),
        "EXPECTED_SOURCE_GEOMETRY_GATE="
        + ctx["geometry_gate"],
        "EXPECTED_WINDOW_GATE="
        + ctx["window_gate"],
    ]

    rect = ctx.get(
        "window_rect"
    )

    if rect:
        lines.extend(
            [
                "WINDOW_X="
                + str(rect["x"]),
                "WINDOW_Y="
                + str(rect["y"]),
                "WINDOW_WIDTH="
                + str(rect["width"]),
                "WINDOW_HEIGHT="
                + str(rect["height"]),
            ]
        )

    lines.extend(
        [
            "ACTIVE_WINDOW_ID_BEFORE="
            + str(ctx["before_id"]),
            "ACTIVE_WINDOW_TITLE_BEFORE="
            + _safe_title(
                ctx["before_title"]
            ),
            "ACTIVE_WINDOW_ID_AFTER="
            + str(after_id),
            "ACTIVE_WINDOW_TITLE_AFTER="
            + _safe_title(
                after_title
            ),
            "CURSOR_X="
            + str(cx),
            "CURSOR_Y="
            + str(cy),
        ]
    )

    return lines


def _capture_visual_jpeg(msg=None):
    _require_x11()

    msg = (
        msg
        if isinstance(msg, dict)
        else {}
    )

    display_width, display_height = (
        _geometry()
    )

    cx, cy = _cursor()

    window_id, title = (
        _active_window()
    )

    capture_scope = str(
        msg.get(
            "capture_scope",
            "ROOT",
        )
    ).upper().strip()

    if capture_scope not in {
        "ROOT",
        "WINDOW",
    }:
        raise CuaTransportError(
            "CAPTURE_SCOPE_INVALID"
        )

    _guard_expected_window(
        msg,
        window_id,
    )

    source_x = 0
    source_y = 0
    source_width = display_width
    source_height = display_height
    window_rect = None

    if capture_scope == "WINDOW":
        window_rect = (
            _window_geometry(
                window_id
            )
        )

        source_x = int(
            window_rect["x"]
        )
        source_y = int(
            window_rect["y"]
        )
        source_width = int(
            window_rect["width"]
        )
        source_height = int(
            window_rect["height"]
        )

    _guard_expected_geometry(
        msg,
        source_width,
        source_height,
    )

    with tempfile.TemporaryDirectory(
        prefix="framebridge-cua-"
    ) as td:
        td = Path(td)

        png = td / "screen.png"
        jpg = td / "screen.jpg"

        capture_target = (
            "root"
            if capture_scope == "ROOT"
            else str(window_id)
        )

        p = _run(
            [
                "import",
                "-window",
                capture_target,
                str(png),
            ],
            timeout=15,
        )

        if (
            p.returncode != 0
            or not png.is_file()
        ):
            raise CuaTransportError(
                "VISUAL_CAPTURE_FAILED"
            )

        q = _run(
            [
                "identify",
                "-format",
                "%w %h",
                str(png),
            ],
            timeout=5,
        )

        if q.returncode != 0:
            raise CuaTransportError(
                "VISUAL_CAPTURE_GEOMETRY_FAILED"
            )

        try:
            actual_w, actual_h = [
                int(v)
                for v
                in q.stdout.strip().split()
            ]
        except Exception:
            raise CuaTransportError(
                "VISUAL_CAPTURE_GEOMETRY_INVALID"
            )

        if (
            actual_w != source_width
            or actual_h != source_height
        ):
            raise CuaTransportError(
                "VISUAL_CAPTURE_GEOMETRY_MISMATCH"
            )

        p = _run(
            [
                "convert",
                str(png),
                "-resize",
                "960x>",
                "-quality",
                "82",
                str(jpg),
            ],
            timeout=15,
        )

        if (
            p.returncode != 0
            or not jpg.is_file()
        ):
            raise CuaTransportError(
                "VISUAL_JPEG_ENCODE_FAILED"
            )

        data = jpg.read_bytes()

    encoded = base64.b64encode(
        data
    ).decode("ascii")

    display_w, display_h = (
        _preview_geometry(
            source_width,
            source_height,
        )
    )

    coord_default = (
        "ROOT"
        if capture_scope == "ROOT"
        else "WINDOW_PREVIEW"
    )

    coord_available = (
        "ROOT,PREVIEW"
        if capture_scope == "ROOT"
        else
        "WINDOW,WINDOW_PREVIEW,ROOT"
    )

    lines = [
        "RESULTADO_FINAL="
        "CUA_VISUAL_OBSERVE_PASS",
        "VISUAL_CAPTURE_GATE=PASS",
        "CUA_PROTOCOL_VERSION="
        + CUA_PROTOCOL_VERSION,
        "MIME=image/jpeg",
        "BYTES="
        + str(len(data)),
        "SHA256="
        + hashlib.sha256(
            data
        ).hexdigest(),
        "WIDTH="
        + str(display_w),
        "HEIGHT="
        + str(display_h),
        "SOURCE_X="
        + str(source_x),
        "SOURCE_Y="
        + str(source_y),
        "SOURCE_WIDTH="
        + str(source_width),
        "SOURCE_HEIGHT="
        + str(source_height),
        "DISPLAY_WIDTH="
        + str(display_width),
        "DISPLAY_HEIGHT="
        + str(display_height),
        "CAPTURE_SCOPE="
        + capture_scope,
        "COORD_SPACE_DEFAULT="
        + coord_default,
        "COORD_SPACE_AVAILABLE="
        + coord_available,
        "CURSOR_X="
        + str(cx),
        "CURSOR_Y="
        + str(cy),
        "ACTIVE_WINDOW_ID="
        + str(window_id),
        "WINDOW_TITLE="
        + _safe_title(title),
    ]

    if window_rect:
        lines.extend(
            [
                "WINDOW_X="
                + str(
                    window_rect["x"]
                ),
                "WINDOW_Y="
                + str(
                    window_rect["y"]
                ),
                "WINDOW_WIDTH="
                + str(
                    window_rect["width"]
                ),
                "WINDOW_HEIGHT="
                + str(
                    window_rect["height"]
                ),
            ]
        )

    lines.append(
        "BASE64=" + encoded
    )

    return {
        "ok": True,
        "action": "OBSERVE",
        "_frame_output":
        "\n".join(lines),
    }


# FRAMEBRIDGE CUA V3 DISPATCH OVERRIDE
def handle_cua_message(msg):
    if not isinstance(msg, dict):
        raise CuaTransportError(
            "CUA_MESSAGE_INVALID"
        )

    target = str(
        msg.get("target", "ASUS")
    ).upper()

    if target != "ASUS":
        raise CuaTransportError(
            "TARGET_MISMATCH"
        )

    action = str(
        msg.get("action", "OBSERVE")
    ).upper().strip()

    if action == "OBSERVE":
        return _capture_visual_jpeg(msg)

    if action == "CLICK":
        return _click(msg)

    if action == "RIGHT_CLICK":
        return _right_click(msg)

    if action == "DOUBLE_CLICK":
        return _double_click(msg)

    if action == "TYPE":
        return _type(msg)

    if action == "KEY":
        return _key(msg)

    if action == "SCROLL":
        return _scroll(msg)

    if action == "DRAG":
        return _drag(msg)

    raise CuaTransportError(
        "CUA_ACTION_UNSUPPORTED"
    )


# FRAMEBRIDGE CUA V3 POINTER-SYNC GUARD
_run_v3_base = _run


def _run(argv, timeout=15):
    args = list(argv)

    if (
        len(args) >= 5
        and args[0] == "xdotool"
        and args[1] == "mousemove"
        and args[2] == "--sync"
    ):
        try:
            target_x = int(args[3])
            target_y = int(args[4])
        except Exception:
            return _run_v3_base(
                args,
                timeout=timeout,
            )

        q = _run_v3_base(
            [
                "xdotool",
                "getmouselocation",
                "--shell",
            ],
            timeout=5,
        )

        values = {}

        if q.returncode == 0:
            for line in q.stdout.splitlines():
                if "=" in line:
                    k, v = line.split(
                        "=",
                        1,
                    )
                    values[
                        k.strip()
                    ] = v.strip()

        try:
            current_x = int(
                values.get(
                    "X",
                    "-999999",
                )
            )
            current_y = int(
                values.get(
                    "Y",
                    "-999999",
                )
            )
        except Exception:
            current_x = -999999
            current_y = -999999

        if (
            current_x == target_x
            and current_y == target_y
        ):
            remainder = args[5:]

            if remainder:
                return _run_v3_base(
                    [
                        "xdotool",
                        *remainder,
                    ],
                    timeout=timeout,
                )

            return subprocess.CompletedProcess(
                args,
                0,
                "",
                "",
            )

    return _run_v3_base(
        args,
        timeout=timeout,
    )


# FRAMEBRIDGE CUA V3 TYPE EXACT OVERRIDE
def _type(msg):
    ctx = _action_context(msg)

    text = str(
        msg.get("text", "")
    )

    if not text:
        raise CuaTransportError(
            "TYPE_TEXT_EMPTY"
        )

    if len(text) > 4096:
        raise CuaTransportError(
            "TYPE_TEXT_TOO_LONG"
        )

    type_mode = str(
        msg.get(
            "type_mode",
            "KEYS",
        )
    ).upper().strip()

    if type_mode not in {
        "KEYS",
        "CLIPBOARD_EXACT",
    }:
        raise CuaTransportError(
            "TYPE_MODE_INVALID"
        )

    if type_mode == "KEYS":
        p = _run(
            [
                "xdotool",
                "type",
                "--clearmodifiers",
                "--delay",
                "10",
                "--",
                text,
            ],
            timeout=20,
        )

        if p.returncode != 0:
            raise CuaTransportError(
                "TYPE_EXECUTION_FAILED"
            )

    else:
        old_clipboard = None
        had_old_clipboard = False
        owner = None

        try:
            old = subprocess.run(
                [
                    "xclip",
                    "-selection",
                    "clipboard",
                    "-o",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )

            if old.returncode == 0:
                old_clipboard = old.stdout
                had_old_clipboard = True

            # R7: mantener un owner X11 en foreground durante todo el
            # Ctrl+V. No usar -loops 1: un clipboard manager puede consumir
            # la primera solicitud antes que la aplicacion destino.
            owner = subprocess.Popen(
                [
                    "xclip",
                    "-quiet",
                    "-selection",
                    "clipboard",
                    "-i",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            if owner.stdin is None:
                raise CuaTransportError(
                    "TYPE_CLIPBOARD_OWNER_STDIN_FAILED"
                )

            owner.stdin.write(
                text.encode("utf-8")
            )
            owner.stdin.close()

            time.sleep(0.05)

            early_rc = owner.poll()

            if early_rc not in (None, 0):
                raise CuaTransportError(
                    "TYPE_CLIPBOARD_SET_FAILED"
                )

            paste = _run(
                [
                    "xdotool",
                    "key",
                    "--clearmodifiers",
                    "ctrl+v",
                ],
                timeout=10,
            )

            if paste.returncode != 0:
                raise CuaTransportError(
                    "TYPE_CLIPBOARD_PASTE_FAILED"
                )

            # Dar tiempo a la aplicacion destino para completar la
            # transferencia antes de retirar el owner.
            time.sleep(0.10)

        finally:
            # El owner temporal nunca puede sobrevivir a TYPE.
            if owner is not None:
                try:
                    if owner.poll() is None:
                        owner.terminate()
                        try:
                            owner.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            owner.kill()
                            owner.wait(timeout=1)
                except Exception:
                    pass

            # Si existia contenido previo, restaurarlo mediante un owner
            # X11 asincrono. El hilo daemon solo hace wait/reap; xclip
            # termina cuando otro cliente toma la seleccion.
            if (
                had_old_clipboard
                and old_clipboard is not None
            ):
                try:
                    restore = subprocess.Popen(
                        [
                            "xclip",
                            "-quiet",
                            "-selection",
                            "clipboard",
                            "-i",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

                    if restore.stdin is not None:
                        restore.stdin.write(
                            old_clipboard
                        )
                        restore.stdin.close()

                        threading.Thread(
                            target=restore.wait,
                            daemon=True,
                        ).start()
                except Exception:
                    pass

    lines = [
        "RESULTADO_FINAL=CUA_TYPE_PASS",
        "TYPE_GATE=PASS",
        "ACTION=TYPE",
        "TYPE_MODE=" + type_mode,
        "CHAR_COUNT=" + str(len(text)),
    ]

    lines.extend(
        _evidence_lines(ctx)
    )

    return {
        "ok": True,
        "action": "TYPE",
        "_frame_output":
        "\n".join(lines),
    }
