from __future__ import annotations

import sys

from .classic_tty import execute_frame, render_result, render_snapshot
from .product_core import ProductError
from .seed_core import SeedContext


BANNER = """FrameBridge Seed // Classic TTY
Modo: MANUAL / CHAT_ONLY
Seguridad: READONLY typed capabilities
Comandos locales: /snapshot /status /audit /help /quit
Pegue un FRAMEEXEC/1 completo para ejecutarlo.
"""


def _read_frame(first_line: str) -> str:
    lines = [first_line.rstrip("\n")]
    while True:
        raw = sys.stdin.readline()
        if raw == "":
            raise ProductError("CLASSIC_FRAME_EOF_BEFORE_END")
        line = raw.rstrip("\n")
        lines.append(line)
        if line == "FRAMEEND/1":
            return "\n".join(lines)


def run_console(seed: SeedContext) -> int:
    if seed.load_identity() is None:
        print("ERROR: Seed no inicializado. Ejecute: framebridge init --provider chatgpt")
        return 2

    print(BANNER)
    print("--- SNAPSHOT PARA SU IA ---")
    print(render_snapshot(seed))
    print("--- FIN SNAPSHOT ---")

    while True:
        if sys.stdin.isatty():
            print("fb> ", end="", flush=True)

        raw = sys.stdin.readline()
        if raw == "":
            return 0

        line = raw.rstrip("\n")
        stripped = line.strip()

        if not stripped:
            continue

        if stripped == "/quit":
            return 0
        if stripped == "/help":
            print(BANNER)
            continue
        if stripped == "/snapshot":
            print(render_snapshot(seed))
            continue
        if stripped == "/status":
            import json
            print(json.dumps(seed.status(), indent=2, sort_keys=True, ensure_ascii=True))
            continue
        if stripped == "/audit":
            import json
            print(json.dumps(seed.audit.verify(), indent=2, sort_keys=True, ensure_ascii=True))
            continue

        if stripped != "FRAMEEXEC/1":
            print("ERROR: comando desconocido. Use /help o pegue FRAMEEXEC/1.")
            continue

        try:
            frame = _read_frame(line)
            result = execute_frame(seed, frame)
            print(render_result(result))
        except ProductError as exc:
            print("FRAMERESULT/1")
            print("ID=unknown")
            print("EXIT=2")
            print("CHANNEL=SEED-CLASSIC-TTY")
            print("")
            print('{"ok":false,"error":' + __import__("json").dumps(str(exc)) + "}")
            print("FRAMEEND/1")

    return 0
