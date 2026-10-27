from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .memory_store import MemoryStore
from .product_core import ProductError


BANNER = """
==============================================================
 FrameBridge Console
==============================================================
""".strip("\n")


HELP = """
Commands:
  help                 Show this help
  status               Show node/Memory summary
  manifest             Show the AI bootstrap manifest
  memory <domain>      Query normalized Memory
  phone                Show official mobile-app guidance
  provider             Show configured AI provider
  objectives           Show future objective-prompt roadmap
  quit / exit          Leave the console

Anything else is treated as a chat message.
""".strip("\n")


PHONE_HINT = "📱 You can also continue from the official ChatGPT app on your phone."


def _setup(ctx) -> dict[str, Any]:
    cfg = ctx.load_config() or {}
    return cfg.get("setup") or {}


def session_header(ctx) -> str:
    setup = _setup(ctx)
    provider = setup.get("provider") or (ctx.load_config() or {}).get("provider") or "unknown"
    memory = MemoryStore(ctx).verify()
    identity = ctx.load_identity() or {}
    connected = os.environ.get("FRAMEBRIDGE_AGENT_CONNECTED") == "1"
    icon = "🤖" if connected else "○"
    lines = [
        BANNER,
        f"Node: {identity.get('hostname') or 'unknown'} ({identity.get('node_id_short') or '--------'})",
        f"AI provider: {provider}",
        f"Agent: {icon} {'connected' if connected else 'not connected'}",
        f"Memory: {memory.get('protocol')} R/W | events={memory.get('events')}",
        PHONE_HINT,
        "",
        "Type help for commands.",
    ]
    return "\n".join(lines)


def _chat_response(ctx, text: str) -> str:
    setup = _setup(ctx)
    provider = setup.get("provider") or "chatgpt"
    # Chat transcripts are intentionally NOT written to technical Memory.
    # Only normalized technical facts/decisions/incidents/artifacts belong in FBM.
    if provider == "chatgpt":
        if os.environ.get("FRAMEBRIDGE_AGENT_CONNECTED") == "1":
            return (
                "A remote ChatGPT session is connected to this node. "
                "Console-to-agent conversational transport is not enabled in this candidate yet. "
                "Continue in the official ChatGPT app while this console remains available locally."
            )
        return (
            "ChatGPT companion mode is configured, but no remote agent session is currently attached.\n"
            + PHONE_HINT + "\n"
            "Open ChatGPT and connect to this FrameBridge node. The local console will remain active."
        )
    return (
        f"Provider '{provider}' is configured, but direct conversational AgentAdapter transport "
        "is not implemented in this candidate."
    )


def execute_console_command(ctx, line: str) -> tuple[bool, str]:
    raw = str(line).strip()
    if not raw:
        return True, ""
    if raw in {"quit", "exit"}:
        return False, "Goodbye."
    if raw == "help":
        return True, HELP
    if raw == "phone":
        return True, PHONE_HINT
    if raw == "provider":
        setup = _setup(ctx)
        return True, json.dumps({"provider": setup.get("provider"), "mode": "companion" if setup.get("provider") == "chatgpt" else "adapter"}, indent=2)
    if raw == "status":
        return True, json.dumps({"status": ctx.status(), "memory": MemoryStore(ctx).stats()}, indent=2, sort_keys=True)
    if raw == "manifest":
        return True, json.dumps(MemoryStore(ctx).bootstrap(), indent=2, sort_keys=True)
    if raw.startswith("memory "):
        domain = raw.split(None, 1)[1].strip()
        return True, json.dumps(MemoryStore(ctx).query(domain, 20), indent=2, sort_keys=True)
    if raw == "objectives":
        return True, (
            "Objective prompts are planned for a later update. Examples: install a desktop, "
            "create a server, configure a workstation, prepare a NAS. They are NOT active in this candidate."
        )
    return True, _chat_response(ctx, raw)


def run_console_chat(ctx) -> int:
    try:
        MemoryStore(ctx).require_ready()
    except Exception as exc:
        print("FrameBridge Console blocked: mandatory Memory is not healthy.")
        print(str(exc))
        return 2
    print(session_header(ctx))
    while True:
        try:
            line = input("\nYou > ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0
        keep, out = execute_console_command(ctx, line)
        if out:
            print("\n" + ("AI > " if line.strip() not in {"help","status","manifest","phone","provider","objectives"} and not line.strip().startswith("memory ") else "") + out)
        if not keep:
            return 0
