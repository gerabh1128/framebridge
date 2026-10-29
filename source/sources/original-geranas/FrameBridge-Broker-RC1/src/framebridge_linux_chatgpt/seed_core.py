from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .product_core import AuditLog, ProductError
from .node_identity import NodeIdentityError, ensure_node_keypair
from .pairing import FBPAIR_PROTOCOL

SUPPORTED_PROVIDERS = {"chatgpt"}
SEED_VERSION = "0.5.0-candidate.1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    data = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def run_readonly(args: list[str], timeout: float = 3.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "stdout": proc.stdout.strip()[:8192],
            "stderr": proc.stderr.strip()[:2048],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
        }


class SeedContext:
    def __init__(
        self,
        state_dir: Path | None = None,
        config_dir: Path | None = None,
    ):
        self.state_dir = Path(
            state_dir
            or os.environ.get(
                "FRAMEBRIDGE_SEED_STATE_DIR",
                "~/.local/share/framebridge-seed",
            )
        ).expanduser()
        self.config_dir = Path(
            config_dir
            or os.environ.get(
                "FRAMEBRIDGE_SEED_CONFIG_DIR",
                "~/.config/framebridge-seed",
            )
        ).expanduser()

        self.identity_path = self.state_dir / "identity.json"
        self.config_path = self.config_dir / "config.json"
        self.audit = AuditLog(self.state_dir / "audit.jsonl")

    def detect_hostname(self) -> str:
        value = socket.gethostname().strip()
        return value or "linux-node"

    def load_identity(self) -> dict[str, Any] | None:
        if not self.identity_path.is_file():
            return None
        value = json.loads(self.identity_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ProductError("SEED_IDENTITY_INVALID")
        return value

    def load_config(self) -> dict[str, Any] | None:
        if not self.config_path.is_file():
            return None
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ProductError("SEED_CONFIG_INVALID")
        return value

    def init(
        self,
        provider: str = "chatgpt",
        hostname: str = "",
    ) -> dict[str, Any]:
        provider = str(provider).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ProductError("SEED_PROVIDER_UNSUPPORTED")

        existing = self.load_identity()
        host = str(hostname).strip() or self.detect_hostname()

        if existing:
            node_id = str(existing.get("node_id") or "")
            if not node_id:
                raise ProductError("SEED_IDENTITY_NODE_ID_MISSING")
            created_at = str(existing.get("created_at") or utc_now())
        else:
            node_id = str(uuid.uuid4())
            created_at = utc_now()

        try:
            crypto_identity = ensure_node_keypair(
                self.state_dir,
                comment="framebridge-" + node_id.replace("-", "")[:8],
            )
        except NodeIdentityError as exc:
            raise ProductError(str(exc)) from exc

        identity = {
            "schema": 2,
            "node_id": node_id,
            "node_id_short": node_id.replace("-", "")[:8],
            "node_host_id": crypto_identity["host_id"],
            "node_public_jwk": crypto_identity["public_jwk"],
            "node_public_key_sha256": crypto_identity["public_key_sha256"],
            "hostname": host,
            "created_at": created_at,
            "updated_at": utc_now(),
        }

        config = {
            "schema": 1,
            "seed_version": SEED_VERSION,
            "provider": provider,
            "connection_name": f"FrameBridge {host}",
            "pairing": {
                "protocol": FBPAIR_PROTOCOL,
                "state": "not_paired",
                "transport": "unpaired",
            },
            "connection_mode": "PAIRING",
            "capability_profile": "seed-readonly",
            "memory": {
                "required": True,
                "protocol": "FBM/1",
                "manifest_protocol": "FBAM/1",
                "mode": "read_write",
                "authority": os.environ.get("FRAMEBRIDGE_MEMORY_AUTHORITY", "deployment-defined"),
            },
            "hostname": host,
            "node_id": node_id,
            "updated_at": utc_now(),
        }

        atomic_write_json(self.identity_path, identity)
        atomic_write_json(self.config_path, config)

        self.audit.append(
            "seed.init",
            "PASS",
            request={
                "provider": provider,
                "hostname": host,
            },
            result={
                "node_id_short": identity["node_id_short"],
                "pairing_state": "not_paired",
                "capability_profile": "seed-readonly",
                "memory_required": True,
            },
            principal="local-operator",
        )

        from .memory_store import MemoryStore
        memory = MemoryStore(self).initialize()

        return {
            "ok": True,
            "seed_version": SEED_VERSION,
            "identity": identity,
            "config": config,
            "memory": memory,
        }

    def identity(self) -> dict[str, Any]:
        identity = self.load_identity()
        if identity is None:
            return {
                "ok": False,
                "error": "SEED_NOT_INITIALIZED",
            }
        return {
            "ok": True,
            "identity": identity,
        }

    def doctor(self) -> dict[str, Any]:
        os_release = read_os_release()
        route = (
            run_readonly(["ip", "route", "show", "default"])
            if shutil.which("ip")
            else {"ok": False, "error": "IP_COMMAND_MISSING"}
        )
        dns_path = Path("/etc/resolv.conf")
        dns_present = dns_path.exists() and dns_path.stat().st_size > 0
        ca = ssl.get_default_verify_paths()
        ca_present = bool(
            (ca.cafile and Path(ca.cafile).is_file())
            or (ca.capath and Path(ca.capath).is_dir())
        )

        checks = {
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "hostname": self.detect_hostname(),
            "os_id": os_release.get("ID"),
            "os_version": os_release.get("VERSION_ID"),
            "shell": os.environ.get("SHELL"),
            "network_default_route": bool(route.get("ok") and route.get("stdout")),
            "dns_config_present": dns_present,
            "ca_certificates_present": ca_present,
            "systemd_present": shutil.which("systemctl") is not None,
            "ip_command_present": shutil.which("ip") is not None,
            "curl_present": shutil.which("curl") is not None,
            "python_venv_available": self._venv_available(),
        }

        required = [
            checks["network_default_route"],
            checks["dns_config_present"],
            checks["ca_certificates_present"],
            checks["ip_command_present"],
            checks["python_venv_available"],
        ]

        return {
            "ok": all(bool(x) for x in required),
            "seed_version": SEED_VERSION,
            "checks": checks,
            "pairing_ready": all(bool(x) for x in required),
            "readonly_only": True,
        }

    def _venv_available(self) -> bool:
        try:
            import venv  # noqa: F401
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        identity = self.load_identity()
        config = self.load_config()
        doctor = self.doctor()

        if identity is None or config is None:
            return {
                "ok": False,
                "initialized": False,
                "seed_version": SEED_VERSION,
                "doctor": doctor,
            }

        from .memory_store import MemoryStore
        memory_required = bool((config.get("memory") or {}).get("required"))
        try:
            memory = MemoryStore(self).verify() if memory_required else {"ok": True, "required": False}
        except Exception as exc:
            memory = {"ok": False, "required": memory_required, "error": str(exc)}

        return {
            "ok": bool(memory.get("ok")) if memory_required else True,
            "initialized": True,
            "seed_version": SEED_VERSION,
            "node": {
                "hostname": identity.get("hostname"),
                "node_id": identity.get("node_id"),
                "node_id_short": identity.get("node_id_short"),
            },
            "provider": config.get("provider"),
            "connection_name": config.get("connection_name"),
            "pairing": config.get("pairing"),
            "capability_profile": config.get("capability_profile"),
            "memory": memory,
            "doctor": doctor,
            "audit": self.audit.verify(),
        }

    def fingerprint(self) -> dict[str, Any]:
        identity = self.load_identity()
        config = self.load_config()
        if identity is None or config is None:
            raise ProductError("SEED_NOT_INITIALIZED")

        body = json.dumps(
            {
                "node_id": identity.get("node_id"),
                "hostname": identity.get("hostname"),
                "provider": config.get("provider"),
                "connection_name": config.get("connection_name"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        return {
            "ok": True,
            "sha256": hashlib.sha256(body).hexdigest(),
            "node_id_short": identity.get("node_id_short"),
            "hostname": identity.get("hostname"),
            "provider": config.get("provider"),
        }
