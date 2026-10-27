"""Original Fleet providers and generic confirmed-node routing."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import socket
import sys
import time
import uuid
from typing import Any

JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
READ_CAPABILITIES = frozenset({
    "system.status", "hardware.summary", "storage.inspect",
    "network.inspect", "service.status",
})


class FleetError(ValueError):
    """Stable public error codes only."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise FleetError(code)


def original_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    choices = [
        ("FRAMEBRIDGE_BROKER_SOURCE", "FrameBridge-Broker-RC1/src"),
        ("FRAMEBRIDGE_FLEET_SOURCE", "FrameBridge-Fleet-Hub/app"),
    ]
    result = []
    for variable, suffix in choices:
        configured = os.environ.get(variable)
        candidates = [Path(configured)] if configured else [
            root / "original/geranas" / suffix,
            root / "sources/original-geranas" / suffix,
        ]
        normalized = []
        for candidate in candidates:
            nested = candidate / ("src" if variable == "FRAMEBRIDGE_BROKER_SOURCE" else "app")
            if (candidate / "framebridge_linux_chatgpt").is_dir():
                normalized.append(candidate)
            elif (nested / "framebridge_linux_chatgpt").is_dir():
                normalized.append(nested)
        path = next(iter(normalized), None)
        _require(path is not None, "ORIGINAL_MODULES_UNAVAILABLE")
        result.append(path)
    return result[0], result[1]


def bootstrap_original() -> None:
    """Extend the original package search path without copying or merging it."""
    broker_source, fleet_source = original_paths()
    for path in (fleet_source, broker_source):
        if str(path) in sys.path:
            sys.path.remove(str(path))
        sys.path.insert(0, str(path))
    import framebridge_linux_chatgpt
    package_file = Path(framebridge_linux_chatgpt.__file__).resolve()
    _require(package_file.is_relative_to(broker_source.resolve()), "ORIGINAL_PACKAGE_SHADOWED")
    for root in (broker_source, fleet_source):
        path = str(root / "framebridge_linux_chatgpt")
        if path not in framebridge_linux_chatgpt.__path__:
            framebridge_linux_chatgpt.__path__.append(path)


def state_directory() -> Path:
    raw = os.environ.get("FRAMEBRIDGE_EXPANDED_STATE") or os.environ.get("FRAMEBRIDGE_STATE_DIR")
    _require(bool(raw), "EXPANDED_STATE_REQUIRED")
    return Path(raw).expanduser()


def local_config() -> dict[str, Any]:
    from .config import load_config
    config = load_config(state_directory())
    _require(type(config) is dict, "PROFILE_CONFIG_INVALID")
    _require(config.get("role") in ("standalone", "central", "node"), "PROFILE_ROLE_INVALID")
    _require(type(config.get("name")) is str and JOB_RE.fullmatch(config["name"]), "PROFILE_NAME_INVALID")
    try:
        uuid.UUID(str(config["node_id"]))
    except (KeyError, ValueError, TypeError):
        raise FleetError("PROFILE_NODE_ID_INVALID") from None
    return config


def _local_descriptor(config: dict[str, Any]) -> dict[str, Any]:
    bootstrap_original()
    from framebridge_linux_chatgpt.seed_capabilities import execute_capability
    _require(callable(execute_capability), "LOCAL_READ_PROVIDER_UNAVAILABLE")
    return {
        "alias": config["name"], "node_id": str(config["node_id"]),
        "hostname": socket.gethostname(), "transport": "local",
        "target": "LOCAL", "capability_profile": "original-readonly",
        "source": "local", "operational_transport_ready": True,
    }


def _remote_descriptors(config: dict[str, Any]) -> list[dict[str, Any]]:
    if config["role"] != "central":
        return []
    from . import broker
    items = broker.list_confirmed_nodes()
    _require(type(items) is list, "BROKER_NODE_LIST_INVALID")
    result = []
    for item in items:
        _require(type(item) is dict, "BROKER_NODE_INVALID")
        alias = item.get("alias")
        node_id = item.get("node_id")
        _require(type(alias) is str and JOB_RE.fullmatch(alias), "BROKER_NODE_ALIAS_INVALID")
        try:
            uuid.UUID(str(node_id))
        except (ValueError, TypeError):
            raise FleetError("BROKER_NODE_ID_INVALID") from None
        result.append({
            "alias": alias, "node_id": str(node_id),
            "hostname": str(item.get("hostname") or alias),
            "transport": "broker_outbound", "target": alias,
            "capability_profile": "original-readonly",
            "source": "confirmed_pairing",
            "operational_transport_ready": item.get("ready") is True,
        })
    return result


def descriptors() -> list[dict[str, Any]]:
    config = local_config()
    result = [_local_descriptor(config), *_remote_descriptors(config)]
    namespace: set[str] = set()
    for item in result:
        for value in (item["alias"], item["node_id"]):
            _require(value not in namespace and (value != "LOCAL" or item["transport"] == "local"),
                     "FLEET_IDENTITY_COLLISION")
            namespace.add(value)
    return result


def discover_nodes() -> dict[str, Any]:
    try:
        nodes = descriptors()
        return {"ok": True, "nodes": sorted(nodes, key=lambda item: item["alias"]),
                "sources": {"local": 1, "confirmed_pairing": len(nodes) - 1}}
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, FleetError) else "FLEET_DISCOVERY_FAILED"}


def resolve_node(alias: str) -> dict[str, Any]:
    _require(type(alias) is str and JOB_RE.fullmatch(alias), "NODE_ALIAS_INVALID")
    nodes = descriptors()
    if alias == "LOCAL":
        return next(item for item in nodes if item["transport"] == "local")
    matches = [item for item in nodes if alias in (item["alias"], item["node_id"])]
    _require(len(matches) == 1, "NODE_NOT_FOUND")
    return matches[0]


def _normalize_read(capability: str, args: dict[str, Any]) -> dict[str, Any]:
    bootstrap_original()
    from framebridge_linux_chatgpt.fleet_routing import ReadCapability, normalize_read_args
    _require(capability in READ_CAPABILITIES, "FLEET_READ_CAPABILITY_DENIED")
    try:
        return normalize_read_args(ReadCapability(capability), args)
    except (TypeError, ValueError):
        raise FleetError("FLEET_READ_ARGUMENTS_INVALID") from None


def read_local(capability: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_read(capability, args)
    from framebridge_linux_chatgpt.seed_capabilities import execute_capability
    data = execute_capability(capability, normalized)
    _require(type(data) is dict, "LOCAL_READ_RESPONSE_INVALID")
    if data.get("ok") is False:
        return data
    return {"ok": True, "capability": capability, "result": data}


def node_read(alias: str, capability: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = _normalize_read(capability, args)
        node = resolve_node(alias)
        if node["transport"] == "local":
            result = read_local(capability, normalized)
        else:
            _require(node["operational_transport_ready"], "NODE_OFFLINE")
            from . import broker
            result = broker.request(node["node_id"], capability, normalized, timeout_s=30)
        _require(type(result) is dict, "NODE_READ_RESPONSE_INVALID")
        response = dict(result)
        response.update(node=node["alias"], hostname=node["hostname"], transport=node["transport"])
        return response
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, FleetError) else "NODE_READ_FAILED"}


def _exec_payload(script: str, authorized: bool, timeout_s: int, job_id: str,
                  sha256: str, include_stdout: bool) -> dict[str, Any]:
    _require(authorized is True, "AUTHORIZED_TRUE_REQUIRED")
    _require(type(script) is str and bool(script), "SCRIPT_REQUIRED")
    data = script.encode("utf-8")
    _require(len(data) <= 262144, "SCRIPT_TOO_LARGE")
    digest = hashlib.sha256(data).hexdigest()
    _require(not sha256 or (type(sha256) is str and sha256.lower() == digest), "SCRIPT_SHA256_MISMATCH")
    try:
        timeout = int(timeout_s)
    except (TypeError, ValueError):
        raise FleetError("INVALID_TIMEOUT") from None
    _require(1 <= timeout <= 600, "INVALID_TIMEOUT")
    job = job_id or "node-mcp-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
    _require(type(job) is str and JOB_RE.fullmatch(job), "INVALID_JOB_ID")
    return {"version": 1, "mode": "EXEC", "authorized": True, "script": script,
            "timeout_s": timeout, "job_id": job, "sha256": digest,
            "include_stdout": include_stdout is True}


def execute_node(alias: str, script: str, authorized: bool, timeout_s: int = 120,
                 job_id: str = "", sha256: str = "", include_stdout: bool = False) -> dict[str, Any]:
    try:
        payload = _exec_payload(script, authorized, timeout_s, job_id, sha256, include_stdout)
        node = resolve_node(alias)
        if node["transport"] == "local":
            from .runtime import adapter_request
            result = adapter_request(payload)
        else:
            _require(node["operational_transport_ready"], "NODE_OFFLINE")
            from . import broker
            result = broker.request(node["node_id"], "admin.exec", payload, timeout_s=payload["timeout_s"])
        _require(type(result) is dict, "NODE_EXEC_RESPONSE_INVALID")
        response = dict(result)
        if response.get("ok") is True:
            _require(response.get("mcp_job_id", response.get("job_id")) == payload["job_id"],
                     "NODE_EXEC_JOB_BINDING_INVALID")
            _require(response.get("mcp_script_sha256", response.get("command_sha256")) == payload["sha256"],
                     "NODE_EXEC_HASH_BINDING_INVALID")
        response.update(target="LOCAL" if node["transport"] == "local" else node["alias"],
                        node_id=node["node_id"], mcp_job_id=payload["job_id"],
                        mcp_script_sha256=payload["sha256"])
        if include_stdout is not True:
            response.pop("stdout", None)
        return response
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, FleetError) else "NODE_EXEC_FAILED"}


def execute_screen_node(alias: str, script: str, authorized: bool, timeout_s: int = 30,
                        job_id: str = "", sha256: str = "", include_stdout: bool = False) -> dict[str, Any]:
    """Use the same confirmed-node authority with an explicit non-shell screen operation."""
    from .real_screen.screen_transport import REQUEST_TYPE, validate_payload, ScreenTransportError
    try:
        _require(type(timeout_s) is int and 1 <= timeout_s <= 30, "INVALID_SCREEN_TIMEOUT")
        payload = _exec_payload(script, authorized, timeout_s, job_id, sha256, include_stdout)
        payload["request_type"] = REQUEST_TYPE
        validate_payload(payload)
        node = resolve_node(alias)
        if node["transport"] == "local":
            from .runtime import adapter_request
            result = adapter_request(payload)
        else:
            _require(node["operational_transport_ready"], "NODE_OFFLINE")
            from . import broker
            result = broker.request(node["node_id"], "admin.exec", payload, timeout_s=payload["timeout_s"])
        _require(type(result) is dict, "NODE_EXEC_RESPONSE_INVALID")
        response = dict(result)
        if response.get("ok") is True:
            _require(response.get("mcp_job_id", response.get("job_id")) == payload["job_id"],
                     "NODE_EXEC_JOB_BINDING_INVALID")
            _require(response.get("mcp_script_sha256", response.get("command_sha256")) == payload["sha256"],
                     "NODE_EXEC_HASH_BINDING_INVALID")
        if response.get("adapter_error") == "ValueError:UNSUPPORTED_REQUEST_TYPE":
            # An old adapter rejects this type before EXEC. Never retry it as shell code.
            from .real_screen.screen_rpc import encode
            response.update(ok=False, exit_code="1", stdout_truncated=False,
                            stdout=encode({"ok": False, "error": {"code": "SCREEN_RPC_UNSUPPORTED_ON_NODE"}}).decode("ascii") + "\n")
        response.update(target="LOCAL" if node["transport"] == "local" else node["alias"],
                        node_id=node["node_id"], mcp_job_id=payload["job_id"],
                        mcp_script_sha256=payload["sha256"])
        return response
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, (FleetError, ScreenTransportError)) else "SCREEN_RPC_FAILED"}


def cua_node(alias: str, payload: dict[str, Any], authorized: bool,
             timeout_s: int = 60, job_id: str = "") -> dict[str, Any]:
    try:
        _require(authorized is True, "AUTHORIZED_TRUE_REQUIRED")
        _require(type(payload) is dict, "CUA_PAYLOAD_REQUIRED")
        _require(payload.get("action") in {"OBSERVE", "CLICK", "DOUBLE_CLICK", "TYPE", "KEY", "SCROLL", "DRAG"},
                 "CUA_ACTION_UNSUPPORTED")
        try:
            timeout = int(timeout_s)
        except (TypeError, ValueError):
            raise FleetError("INVALID_CUA_TIMEOUT") from None
        _require(1 <= timeout <= 180, "INVALID_CUA_TIMEOUT")
        job = job_id or "cua-node-" + uuid.uuid4().hex
        _require(type(job) is str and JOB_RE.fullmatch(job), "INVALID_JOB_ID")
        request = {"version": 1, "mode": "EXEC", "request_type": "cua",
                   "authorized": True, "timeout_s": timeout, "job_id": job, "cua": dict(payload)}
        node = resolve_node(alias)
        if node["transport"] == "local":
            from .runtime import adapter_request
            result = adapter_request(request)
        else:
            _require(node["operational_transport_ready"], "NODE_OFFLINE")
            from . import broker
            result = broker.request(node["node_id"], "admin.cua", request, timeout_s=timeout)
        _require(type(result) is dict, "NODE_CUA_RESPONSE_INVALID")
        response = dict(result)
        if response.get("ok") is True:
            _require(response.get("job_id") == job, "NODE_CUA_JOB_BINDING_INVALID")
        response.update(target="LOCAL" if node["transport"] == "local" else node["alias"], node_id=node["node_id"])
        return response
    except Exception as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, FleetError) else "NODE_CUA_FAILED"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Original six-tool Fleet READ Hub")
    parser.add_argument("command", choices=("serve",), nargs="?", default="serve")
    parser.parse_args()
    bootstrap_original()
    from mcp.server.fastmcp import FastMCP
    from framebridge_linux_chatgpt.fleet_hub import FleetHub
    from framebridge_linux_chatgpt.fleet_mcp_surface import register_fleet_read_tools
    host = os.environ.get("FRAMEBRIDGE_FLEET_HUB_HOST", "127.0.0.1")
    _require(host in ("127.0.0.1", "::1"), "FLEET_HUB_HOST_INVALID")
    port = int(os.environ.get("FRAMEBRIDGE_FLEET_HUB_PORT", "20877"))
    _require(1024 <= port <= 65535, "FLEET_HUB_PORT_INVALID")
    server = FastMCP("FrameBridge Original Expanded Fleet", host=host, port=port,
                     streamable_http_path="/mcp")
    hub = FleetHub(discover_nodes, node_read)
    register_fleet_read_tools(server, hub)
    from .http_server import run_streamable_mcp
    run_streamable_mcp(server)


if __name__ == "__main__":
    main()
