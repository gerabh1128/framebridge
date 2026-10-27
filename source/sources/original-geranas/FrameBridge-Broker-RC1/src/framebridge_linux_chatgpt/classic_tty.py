from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .product_core import ProductError
from .seed_capabilities import execute_capability, list_capabilities
from .seed_core import SeedContext

FRAME_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class ClassicRequest:
    frame_id: str
    target: str
    mode: str
    lang: str
    body: dict[str, Any]
    body_sha256: str


def parse_frame(text: str) -> ClassicRequest:
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.splitlines()

    try:
        start = lines.index("FRAMEEXEC/1")
    except ValueError:
        raise ProductError("CLASSIC_FRAME_START_MISSING")

    try:
        end = lines.index("FRAMEEND/1", start + 1)
    except ValueError:
        raise ProductError("CLASSIC_FRAME_END_MISSING")

    frame_lines = lines[start + 1:end]
    headers: dict[str, str] = {}
    body_start = None

    for i, line in enumerate(frame_lines):
        if not line.strip():
            body_start = i + 1
            break
        if "=" not in line:
            raise ProductError("CLASSIC_HEADER_INVALID")
        key, value = line.split("=", 1)
        headers[key.strip().upper()] = value.strip()

    if body_start is None:
        raise ProductError("CLASSIC_BODY_SEPARATOR_MISSING")

    frame_id = headers.get("ID", "")
    target = headers.get("TARGET", "")
    mode = headers.get("MODE", "")
    lang = headers.get("LANG", "")

    if not FRAME_ID_RE.fullmatch(frame_id):
        raise ProductError("CLASSIC_ID_INVALID")
    if not target:
        raise ProductError("CLASSIC_TARGET_REQUIRED")
    if mode != "EXEC":
        raise ProductError("CLASSIC_MODE_UNSUPPORTED")
    if lang != "framebridge-cap-v1":
        raise ProductError("CLASSIC_LANG_UNSUPPORTED")

    body_text = "\n".join(frame_lines[body_start:]).strip()
    if not body_text:
        raise ProductError("CLASSIC_BODY_MISSING")
    if len(body_text.encode("utf-8")) > 32768:
        raise ProductError("CLASSIC_BODY_TOO_LARGE")

    try:
        body = json.loads(body_text)
    except Exception:
        raise ProductError("CLASSIC_BODY_JSON_INVALID")

    if not isinstance(body, dict):
        raise ProductError("CLASSIC_BODY_NOT_OBJECT")

    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")

    return ClassicRequest(
        frame_id=frame_id,
        target=target,
        mode=mode,
        lang=lang,
        body=body,
        body_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _target_matches(seed: SeedContext, target: str) -> bool:
    identity = seed.load_identity()
    if identity is None:
        raise ProductError("SEED_NOT_INITIALIZED")

    accepted = {
        str(identity.get("hostname") or ""),
        str(identity.get("node_id") or ""),
        str(identity.get("node_id_short") or ""),
    }
    return target in accepted


def execute_frame(seed: SeedContext, text: str) -> dict[str, Any]:
    request = parse_frame(text)

    if not _target_matches(seed, request.target):
        raise ProductError("CLASSIC_TARGET_MISMATCH")

    capability = str(request.body.get("capability") or "").strip()
    args = request.body.get("args") or {}

    if "script" in request.body or "shell" in request.body or "command" in request.body:
        raise ProductError("CLASSIC_RAW_SHELL_FORBIDDEN")

    started = time.monotonic()
    try:
        result = execute_capability(capability, args)
        status = "PASS"
        exit_code = 0
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        status = "ERROR"
        exit_code = 1

    duration_ms = int((time.monotonic() - started) * 1000)

    seed.audit.append(
        "classic." + capability,
        status,
        request={
            "id": request.frame_id,
            "target": request.target,
            "lang": request.lang,
            "body_sha256": request.body_sha256,
            "capability": capability,
            "args": args,
        },
        result={
            "exit": exit_code,
            "duration_ms": duration_ms,
        },
        principal="chat-manual",
    )

    return {
        "id": request.frame_id,
        "exit": exit_code,
        "channel": "SEED-CLASSIC-TTY",
        "capability": capability,
        "body_sha256": request.body_sha256,
        "duration_ms": duration_ms,
        "result": result,
    }


def render_result(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value.get("result"),
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
    )
    return "\n".join([
        "FRAMERESULT/1",
        "ID=" + str(value.get("id")),
        "EXIT=" + str(value.get("exit")),
        "CHANNEL=" + str(value.get("channel")),
        "CAPABILITY=" + str(value.get("capability")),
        "BODY_SHA256=" + str(value.get("body_sha256")),
        "DURATION_MS=" + str(value.get("duration_ms")),
        "",
        payload,
        "FRAMEEND/1",
    ])


def render_snapshot(seed: SeedContext) -> str:
    identity = seed.load_identity()
    config = seed.load_config()
    if identity is None or config is None:
        raise ProductError("SEED_NOT_INITIALIZED")

    snapshot = {
        "protocol": "framebridge-cap-v1",
        "hostname": identity.get("hostname"),
        "node_id": identity.get("node_id"),
        "node_id_short": identity.get("node_id_short"),
        "provider": config.get("provider"),
        "connection_mode": config.get("connection_mode", "MANUAL"),
        "capability_profile": config.get("capability_profile"),
        "capabilities": list_capabilities(),
        "instruction": (
            "Return only one FRAMEEXEC/1 frame using LANG=framebridge-cap-v1. "
            "Use only a listed READONLY capability. Never include raw shell."
        ),
    }

    payload = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True)

    return "\n".join([
        "FRAMEBRIDGE-SNAPSHOT/1",
        "TARGET=" + str(identity.get("node_id_short")),
        "PROVIDER=" + str(config.get("provider")),
        "CONNECTION=MANUAL",
        "",
        payload,
        "FRAMEEND/1",
    ])
