"""Register exactly six READ tools on an injected MCP-compatible registrar."""
from __future__ import annotations

from dataclasses import asdict
import json

from .fleet_hub import FleetHub
from .fleet_routing import RouteResult


_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _public_data(value: object) -> object:
    """Copy JSON-only data and omit inert context even in nested gateway payloads."""
    if value is None or type(value) in (str, bool, int, float):
        return value
    if type(value) is list:
        return [_public_data(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _public_data(item) for key, item in value.items()
                if key != "context_text"}
    raise ValueError("FLEET_RESPONSE_INVALID")


def _serializable(value: dict) -> dict:
    # Reject nonfinite numbers as well as custom objects; no repr/str fallback.
    return json.loads(json.dumps(_public_data(value), allow_nan=False))


def register_fleet_read_tools(mcp, hub: FleetHub) -> None:
    def read(node: str, capability: str, args: dict | None = None) -> dict:
        try:
            hub.refresh()
        except Exception:
            return {"ok": False, "error": "FLEET_REFRESH_FAILED"}
        try:
            result = hub.read(node, capability, args=args)
            if type(result) is not RouteResult or result.ok is not True:
                return {"ok": False, "error": "FLEET_READ_FAILED"}
            return _serializable({"ok": True, "node_id": result.node_id,
                                  "capability": result.capability, "data": result.data})
        except Exception:
            return {"ok": False, "error": "FLEET_READ_FAILED"}

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_nodes() -> dict:
        """Refresh and list Fleet nodes with stable identities and readiness."""
        try:
            hub.refresh()
            return _serializable({"ok": True, "nodes": [asdict(node) for node in hub.nodes()]})
        except Exception:
            return {"ok": False, "error": "FLEET_NODES_FAILED"}

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_system_status(node: str) -> dict:
        """Read system status for a Fleet node ID or alias."""
        return read(node, "system.status")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_hardware_summary(node: str) -> dict:
        """Read hardware summary for a Fleet node ID or alias."""
        return read(node, "hardware.summary")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_storage_inspect(node: str) -> dict:
        """Inspect storage for a Fleet node ID or alias."""
        return read(node, "storage.inspect")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_network_inspect(node: str) -> dict:
        """Inspect networking for a Fleet node ID or alias."""
        return read(node, "network.inspect")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_service_status(node: str, service: str) -> dict:
        """Read status of one validated service for a Fleet node ID or alias."""
        return read(node, "service.status", {"service": service})
