"""Original Fleet providers and generic confirmed-node routing."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import uuid
from typing import Any

_BOOTSTRAP_LOCK = threading.RLock()

JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
READ_CAPABILITIES = frozenset({
    "system.status", "hardware.summary", "storage.inspect",
    "network.inspect", "service.status",
})
_UNCERTAIN_EXECUTION = frozenset({"in_flight", "outcome_unknown"})
_PRE_SUBMISSION_ERRORS = frozenset({
    "NODE_BUSY_HEARTBEAT_STALE", "NODE_OFFLINE", "NODE_NOT_CONFIRMED",
    "BROKER_START_DEADLINE_UNSUPPORTED",
})


class FleetError(ValueError):
    """Stable public error codes only."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise FleetError(code)


def select_target(target: str | None, *legacy: str | None, default: str | None = None) -> str:
    """Select one request destination before looking it up or dispatching."""
    supplied = [value for value in (target, *legacy) if value is not None]
    if not supplied and default is not None:
        return default
    _require(supplied, "TARGET_REQUIRED")
    _require(all(type(value) is str and value for value in supplied), "TARGET_SELECTOR_INVALID")
    _require(all(value == supplied[0] for value in supplied), "TARGET_SELECTOR_CONFLICT")
    return supplied[0]


def _require_transport_ready(node):
    if node.get("operational_transport_ready") is True:
        return
    if node.get("operational_transport_busy") is True:
        raise FleetError("NODE_BUSY_HEARTBEAT_STALE")
    raise FleetError("NODE_OFFLINE")


def _routing_error(exc, fallback, *public_types):
    if isinstance(exc, (FleetError, *public_types)):
        return str(exc)
    try:
        from . import broker
    except ImportError:
        return fallback
    if isinstance(exc, broker.BrokerError) and str(exc) in _PRE_SUBMISSION_ERRORS:
        return str(exc)
    return fallback


def _operation_error(exc, fallback, attempted=False, *public_types):
    """Preserve what the authenticated transport knows about execution."""
    from . import broker
    error = _routing_error(exc, fallback, *public_types)
    state = "outcome_unknown" if attempted else "not_started"
    response = {"ok": False, "error": error, "execution_state": state}
    if isinstance(exc, getattr(broker, "BrokerRequestError", ())):
        state = exc.execution_state
        response.update(error=str(exc), execution_state=state)
        if exc.request_id:
            response["request_id"] = exc.request_id
    elif error in _PRE_SUBMISSION_ERRORS:
        state = "not_started"
        response["execution_state"] = state
    if state in _UNCERTAIN_EXECUTION:
        response["do_not_replay"] = True
    return response


def _received_execution_state(result):
    if result.get("ok") is False:
        state = result.get("execution_state")
        if state in ("not_started", "expired_before_start", "outcome_unknown"):
            return state
        if result.get("error") == "REQUEST_EXPIRED":
            return "expired_before_start"
        if (result.get("error") == "INVALID_START_DEADLINE"
                or result.get("adapter_error") == "ValueError:UNSUPPORTED_REQUEST_TYPE"):
            return "not_started"
        if (result.get("do_not_replay") is True or result.get("error") in (
                "NODE_DISPATCH_FAILED", "ADAPTER_RESPONSE_INVALID",
                "CUA_ADAPTER_RESPONSE_UNCERTAIN", "CUA_ADAPTER_RESPONSE_INVALID")):
            return "outcome_unknown"
    return "completed"


def _received_execution_receipt(result):
    state = _received_execution_state(result)
    receipt = {"execution_state": state}
    if state in _UNCERTAIN_EXECUTION:
        receipt["do_not_replay"] = True
    return receipt


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
    # Registration now dispatches in workers; protect shared import paths.
    with _BOOTSTRAP_LOCK:
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
        "registered_label": config["name"],
        "hostname": socket.gethostname(), "transport": "local",
        "target": config["name"], "transport_target": "LOCAL",
        "capability_profile": "original-readonly",
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
            "registered_label": str(item.get("registered_label") or item.get("hostname") or alias),
            "hostname": None,
            "hostname_verified": False,
            "transport": "broker_outbound", "target": alias, "transport_target": alias,
            "capability_profile": "original-readonly",
            "source": "confirmed_pairing",
            "operational_transport_ready": item.get("ready") is True,
            "operational_transport_busy": item.get("busy") is True,
            "heartbeat_fresh": item.get("heartbeat_fresh") is True,
            "last_seen_age_seconds": item.get("last_seen_age_seconds"),
            "activity_state": item.get("activity_state", "idle" if item.get("ready") is True else "offline"),
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


def _target_identity(requested: str, node: dict[str, Any],
                     result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Separate the confirmed route from the receiver and execution hosts."""
    local = node["transport"] == "local"
    executed_host = None
    if result is not None and _received_execution_state(result) == "completed":
        observed = node.get("hostname") if local else result.get("executed_hostname")
        if type(observed) is str and re.fullmatch(r"[A-Za-z0-9._-]{1,253}", observed):
            executed_host = observed
    return {
        "requested_target": requested,
        "resolved_target": node["alias"],
        "node": node["alias"],
        "target": node["alias"],
        "transport_target": "LOCAL" if local else node["alias"],
        "node_id": node["node_id"],
        "registered_label": node.get("registered_label") or node["alias"],
        "receiver_hostname": socket.gethostname(),
        "executed_hostname": executed_host,
        "hostname": node.get("hostname") if local else executed_host,
        "transport": node["transport"],
    }


def local_target_identity() -> dict[str, Any]:
    """Resolve the receiver without making local execution depend on the broker."""
    if os.environ.get("FRAMEBRIDGE_EXPANDED_STATE") or os.environ.get("FRAMEBRIDGE_STATE_DIR"):
        return _target_identity("LOCAL", _local_descriptor(local_config()))
    # The historical standalone front door can be started without a Fleet profile.
    host = socket.gethostname()
    return _target_identity("LOCAL", {"alias": host, "node_id": None,
                                     "hostname": host, "transport": "local"})


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
    node = None
    try:
        normalized = _normalize_read(capability, args)
        node = resolve_node(alias)
        if node["transport"] == "local":
            result = read_local(capability, normalized)
        else:
            _require_transport_ready(node)
            from . import broker
            result = broker.request(node["node_id"], capability, normalized, timeout_s=30)
        _require(type(result) is dict, "NODE_READ_RESPONSE_INVALID")
        response = dict(result)
        response.update(_target_identity(alias, node, result))
        return response
    except Exception as exc:
        response = {"ok": False, "error": _routing_error(exc, "NODE_READ_FAILED")}
        if node is not None:
            response.update(_target_identity(alias, node))
        return response


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
    node = payload = None
    attempted = False
    try:
        payload = _exec_payload(script, authorized, timeout_s, job_id, sha256, include_stdout)
        node = resolve_node(alias)
        if node["transport"] == "local":
            from .runtime import adapter_request
            attempted = True
            result = adapter_request(payload)
        else:
            _require_transport_ready(node)
            from . import broker
            attempted = True
            result = broker.request(node["node_id"], "admin.exec", payload, timeout_s=payload["timeout_s"])
        _require(type(result) is dict, "NODE_EXEC_RESPONSE_INVALID")
        response = dict(result)
        if response.get("ok") is True:
            _require(response.get("mcp_job_id", response.get("job_id")) == payload["job_id"],
                     "NODE_EXEC_JOB_BINDING_INVALID")
            _require(response.get("mcp_script_sha256", response.get("command_sha256")) == payload["sha256"],
                     "NODE_EXEC_HASH_BINDING_INVALID")
        response.update(_target_identity(alias, node, result))
        response.update(_received_execution_receipt(result))
        response.update(mcp_job_id=payload["job_id"],
                        mcp_script_sha256=payload["sha256"])
        if include_stdout is not True:
            response.pop("stdout", None)
        return response
    except Exception as exc:
        response = _operation_error(exc, "NODE_EXEC_FAILED", attempted)
        if payload is not None:
            response.update(mcp_job_id=payload["job_id"], mcp_script_sha256=payload["sha256"])
        if node is not None:
            response.update(_target_identity(alias, node))
        return response


def execute_screen_node(alias: str, script: str, authorized: bool, timeout_s: int = 30,
                        job_id: str = "", sha256: str = "", include_stdout: bool = False) -> dict[str, Any]:
    """Use the same confirmed-node authority with an explicit non-shell screen operation."""
    from .real_screen.screen_transport import REQUEST_TYPE, validate_payload, ScreenTransportError
    node = payload = None
    attempted = False
    try:
        _require(type(timeout_s) is int and 1 <= timeout_s <= 30, "INVALID_SCREEN_TIMEOUT")
        payload = _exec_payload(script, authorized, timeout_s, job_id, sha256, include_stdout)
        payload["request_type"] = REQUEST_TYPE
        validate_payload(payload)
        node = resolve_node(alias)
        if node["transport"] == "local":
            from .runtime import adapter_request
            attempted = True
            result = adapter_request(payload)
        else:
            _require_transport_ready(node)
            from . import broker
            attempted = True
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
            response.update(ok=False, error="SCREEN_RPC_UNSUPPORTED_ON_NODE",
                            exit_code="1", stdout_truncated=False,
                            stdout=encode({"ok": False, "error": {"code": "SCREEN_RPC_UNSUPPORTED_ON_NODE"}}).decode("ascii") + "\n")
        response.update(_target_identity(alias, node, result))
        response.update(_received_execution_receipt(result))
        if (result.get("ok") is False
                and result.get("error") in ("ADMIN_OPERATION_DENIED", "PAIR_CUA_SCOPE_MISSING")
                and set(result) <= {"ok", "error", "request_id", "node_id"}):
            # The Windows client returns a bare rejection before calling its
            # typed adapter. Preserve it without requiring an EXEC exit code.
            response["execution_state"] = "not_started"
        response.update(mcp_job_id=payload["job_id"],
                        mcp_script_sha256=payload["sha256"])
        return response
    except Exception as exc:
        response = _operation_error(exc, "SCREEN_RPC_FAILED", attempted, ScreenTransportError)
        if payload is not None:
            response.update(mcp_job_id=payload["job_id"], mcp_script_sha256=payload["sha256"])
        if node is not None:
            response.update(_target_identity(alias, node))
        return response


def cua_node(alias: str, payload: dict[str, Any], authorized: bool,
             timeout_s: int = 60, job_id: str = "") -> dict[str, Any]:
    node = job = None
    attempted = False
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
            attempted = True
            result = adapter_request(request)
        else:
            _require_transport_ready(node)
            from . import broker
            attempted = True
            result = broker.request(node["node_id"], "admin.cua", request, timeout_s=timeout)
        _require(type(result) is dict, "NODE_CUA_RESPONSE_INVALID")
        response = dict(result)
        if response.get("ok") is True:
            _require(response.get("job_id") == job, "NODE_CUA_JOB_BINDING_INVALID")
        response.update(_target_identity(alias, node, result))
        response.update(_received_execution_receipt(result))
        response["mcp_job_id"] = job
        return response
    except Exception as exc:
        response = _operation_error(exc, "NODE_CUA_FAILED", attempted)
        if type(job) is str and JOB_RE.fullmatch(job):
            response["mcp_job_id"] = job
        if node is not None:
            response.update(_target_identity(alias, node))
        return response


def execution_status(alias: str, request_id: str = "", job_id: str = "") -> dict[str, Any]:
    """Reconcile one previously submitted remote operation without replaying it."""
    node = None
    try:
        _require(bool(request_id or job_id), "EXEC_STATUS_ID_REQUIRED")
        _require(all(type(value) is str and (not value or JOB_RE.fullmatch(value))
                     for value in (request_id, job_id)), "EXEC_STATUS_ID_INVALID")
        node = resolve_node(alias)
        _require(node["transport"] == "broker_outbound", "EXEC_STATUS_REMOTE_REQUIRED")
        from . import broker
        entry = broker.request_status(node["node_id"], request_id=request_id, job_id=job_id)
        states = {
            "pending": "queued",
            "dispatched": "in_flight",
            "complete": "completed",
            "expired": "expired_before_dispatch",
            "cancelled": "expired_before_dispatch",
        }
        state = states.get(entry.get("state"), "outcome_unknown")
        result = entry.get("result")
        if not isinstance(result, dict):
            result = None
        if state == "completed" and result is not None:
            state = _received_execution_state(result)
        response = {"ok": True, "execution_state": state,
                    "request_id": entry.get("request_id") or request_id or None,
                    "mcp_job_id": entry.get("job_id") or job_id or None,
                    "do_not_replay": state in _UNCERTAIN_EXECUTION or state == "queued"}
        response.update(_target_identity(alias, node, result if state == "completed" else None))
        if state == "completed" and result is not None:
            response["operation_ok"] = result.get("ok") is True
            if isinstance(result.get("exit_code"), (str, int)):
                response["exit_code"] = result["exit_code"]
        return response
    except (OSError, ValueError) as exc:
        response = {"ok": False, "error": _routing_error(exc, "EXEC_STATUS_UNAVAILABLE"),
                    "execution_state": "outcome_unknown", "do_not_replay": True}
        if node is not None:
            response.update(_target_identity(alias, node))
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Original six-tool Fleet READ Hub")
    parser.add_argument("command", choices=("serve",), nargs="?", default="serve")
    parser.parse_args()
    bootstrap_original()
    from mcp.server.fastmcp import FastMCP
    from .fleet_read_mcp import register_fleet_read_tools
    host = os.environ.get("FRAMEBRIDGE_FLEET_HUB_HOST", "127.0.0.1")
    _require(host in ("127.0.0.1", "::1"), "FLEET_HUB_HOST_INVALID")
    port = int(os.environ.get("FRAMEBRIDGE_FLEET_HUB_PORT", "20877"))
    _require(1024 <= port <= 65535, "FLEET_HUB_PORT_INVALID")
    server = FastMCP("FrameBridge Original Expanded Fleet", host=host, port=port,
                     streamable_http_path="/mcp")
    register_fleet_read_tools(server, discover_nodes, node_read)
    from .http_server import run_streamable_mcp
    run_streamable_mcp(server)


if __name__ == "__main__":
    main()
