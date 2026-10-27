#!/usr/bin/env python3
import json
import os
import stat
import time
from pathlib import Path

_ALLOWED = {"escuchando", "pensando", "trabajando", "listo", "detenido"}
_MAX_BYTES = 16384
_MAX_AGE_SECONDS = 5.0

def read_voice_status(home=None):
    base = Path(home).expanduser() if home else Path.home()
    path = base / ".local/share/framebridge-voice/state-r1.json"

    safe = {
        "state": "listo",
        "available": False,
        "updated_at": None,
    }

    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK

        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)

            if not stat.S_ISREG(st.st_mode):
                return safe

            if st.st_uid != os.getuid():
                return safe

            if st.st_size > _MAX_BYTES:
                return safe

            raw = os.read(fd, _MAX_BYTES + 1)
        finally:
            os.close(fd)

        if len(raw) > _MAX_BYTES:
            return safe

        data = json.loads(raw.decode("utf-8"))

        state = str(data.get("status", "listo")).lower()
        if state not in _ALLOWED:
            state = "listo"

        updated = data.get("updated_at")
        if not isinstance(updated, (int, float)):
            return safe

        age = max(0.0, time.time() - float(updated))

        if age > _MAX_AGE_SECONDS:
            return safe

        if state == "detenido":
            state = "listo"

        return {
            "state": state,
            "available": True,
            "updated_at": float(updated),
        }

    except (
        FileNotFoundError,
        PermissionError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ):
        return safe
