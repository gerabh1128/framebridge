from __future__ import annotations

import json
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_store import MemoryStore
from .product_core import ProductError


@dataclass(frozen=True)
class SetupChoice:
    privilege_mode: str
    provider: str
    ui_language: str


VALID_PRIVILEGE_MODES = {"totp", "sudo_tty"}
VALID_PROVIDERS = {"chatgpt", "ollama", "lmstudio", "other"}
VALID_LANGUAGES = {"en", "es", "zh", "fr", "de", "pt", "ja", "ko"}


def detect_system() -> dict[str, Any]:
    from .seed_capabilities import hardware_summary, system_status, storage_inspect, network_inspect
    system = system_status({})
    hardware = hardware_summary({})
    storage = storage_inspect({})
    network = network_inspect({})
    return {
        "hostname": socket.gethostname(),
        "system": system,
        "hardware": hardware,
        "storage": storage,
        "network": network,
    }


def validate_choice(choice: SetupChoice) -> SetupChoice:
    pm = str(choice.privilege_mode).strip().lower()
    provider = str(choice.provider).strip().lower()
    lang = str(choice.ui_language).strip().lower()
    if pm not in VALID_PRIVILEGE_MODES:
        raise ProductError("SETUP_PRIVILEGE_MODE_INVALID")
    if provider not in VALID_PROVIDERS:
        raise ProductError("SETUP_PROVIDER_INVALID")
    if lang not in VALID_LANGUAGES:
        raise ProductError("SETUP_LANGUAGE_INVALID")
    return SetupChoice(pm, provider, lang)


def save_setup_config(ctx, choice: SetupChoice) -> dict[str, Any]:
    choice = validate_choice(choice)
    config = ctx.load_config() or {}
    config["provider"] = choice.provider
    config["setup"] = {
        "completed": True,
        "privilege_mode": choice.privilege_mode,
        "provider": choice.provider,
        "ui_language": choice.ui_language,
        "chat_mode": "companion" if choice.provider == "chatgpt" else "agent_adapter",
        "pairing_state": (
            (config.get("pairing") or {}).get("state", "not_paired")
            if choice.provider == "chatgpt"
            else "adapter_pending"
        ),
    }
    path = Path(ctx.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".setup.tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return config


def setup_summary(ctx) -> dict[str, Any]:
    config = ctx.load_config() or {}
    setup = config.get("setup") or {}
    memory = MemoryStore(ctx).verify() if (config.get("memory") or {}).get("required") else {"ok": False, "required": False}
    return {
        "ok": True,
        "setup": setup,
        "memory": memory,
        "provider": config.get("provider"),
        "seed_version": config.get("seed_version"),
    }


def render_privilege_explanation(mode: str) -> list[str]:
    if mode == "totp":
        return [
            "Authenticator / QR TOTP selected.",
            "FrameBridge will show enrollment QR locally.",
            "The TOTP secret and OTP codes never leave this machine.",
            "Privileged CHANGE operations still use typed plans and one-shot approval.",
        ]
    return [
        "Local sudo password selected.",
        "FrameBridge never stores or forwards your sudo password.",
        "sudo prompts are handled directly by the local terminal.",
        "The AI does not receive general sudo or raw root shell access.",
    ]


def provider_instructions(provider: str) -> list[str]:
    if provider == "chatgpt":
        return [
            "ChatGPT companion mode selected.",
            "Use the official ChatGPT app or web interface for your OpenAI account.",
            "FrameBridge does not ask for or store your ChatGPT password.",
            "After pairing, this local console remains the primary on-machine FrameBridge interface.",
        ]
    if provider == "ollama":
        return ["Ollama selected. Local AgentAdapter integration will be configured when available."]
    if provider == "lmstudio":
        return ["LM Studio selected. Local AgentAdapter integration will be configured when available."]
    return ["Custom provider selected. Configure an AgentAdapter after setup."]
