#!/usr/bin/env python3
import json
import os
import socket
import sys
from pathlib import Path

HOME = Path.home()
ROOT = Path(
    os.environ.get(
        "FRAMEBRIDGE_TOOL_ROOT",
        str(HOME / ".local/share/framebridge/tool-adapter")
    )
)
SOCKET_PATH = ROOT / "adapter.sock"

if len(sys.argv) != 2:
    print("USAGE: framebridge_tool_client.py REQUEST.json")
    raise SystemExit(64)

request_path = Path(sys.argv[1])
request = json.loads(
    request_path.read_text(encoding="utf-8")
)

raw = (
    json.dumps(
        request,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")

sock = socket.socket(
    socket.AF_UNIX,
    socket.SOCK_STREAM,
)

sock.connect(str(SOCKET_PATH))
sock.sendall(raw)
sock.shutdown(socket.SHUT_WR)

data = bytearray()

while True:
    chunk = sock.recv(65536)

    if not chunk:
        break

    data.extend(chunk)

sock.close()

response = json.loads(
    bytes(data).decode("utf-8")
)

print(
    json.dumps(
        response,
        ensure_ascii=True,
        sort_keys=True,
    )
)
