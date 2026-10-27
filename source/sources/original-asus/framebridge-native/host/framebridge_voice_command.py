#!/usr/bin/env python3
import json
import os
import socket
import threading
from pathlib import Path

_lock = threading.Lock()
_active = False

def _socket_path():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "framebridge-voice-r1.sock"

def _listen_worker():
    global _active
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(str(_socket_path()))
        s.sendall(b'{"cmd":"listen_once"}\n')
        f = s.makefile("rb")
        f.readline()   # respuesta local; NO se expone
        s.close()
    except Exception:
        pass
    finally:
        with _lock:
            _active = False

def start_listen_once():
    global _active
    with _lock:
        if _active:
            return {"ok": True, "started": False, "reason": "already_active"}
        if not _socket_path().is_socket():
            return {"ok": False, "error": "VOICE_DAEMON_NOT_AVAILABLE"}
        _active = True
        threading.Thread(target=_listen_worker, daemon=True).start()
        return {"ok": True, "started": True}
