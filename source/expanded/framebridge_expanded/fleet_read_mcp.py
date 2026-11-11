"""Register the existing READ tools over the confirmed Expanded route."""
from __future__ import annotations

from ._execution_receipt import execution_error, execution_receipt


def register_fleet_read_tools(mcp, discover_nodes, node_read):
    from framebridge_linux_chatgpt.fleet_mcp_surface import _ANNOTATIONS, _serializable

    def read(node, capability, args=None):
        try:
            # Expanded resolves the original selector once and dispatches by UUID.
            result = node_read(node, capability, {} if args is None else args)
            if type(result) is not dict:
                return {"ok": False, "error": "FLEET_READ_FAILED", "requested_target": node}
            route = {**execution_receipt(result), "requested_target": node}
            if result.get("ok") is not True:
                return _serializable({"ok": False, "error": "FLEET_READ_FAILED", **route,
                                      "routing_error": execution_error(result, "NODE_READ_FAILED")})
            if type(route.get("node_id")) is not str:
                return {"ok": False, "error": "FLEET_READ_FAILED", "requested_target": node}
            return _serializable({"ok": True, **route, "capability": capability, "data": result})
        except Exception:
            return {"ok": False, "error": "FLEET_READ_FAILED", "requested_target": node}

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_nodes() -> dict:
        """List confirmed aliases and node IDs; online means broker transport readiness."""
        try:
            result = discover_nodes()
            if type(result) is not dict or result.get("ok") is not True or type(result.get("nodes")) is not list:
                return {"ok": False, "error": "FLEET_NODES_FAILED"}
            fields = ("alias", "node_id", "registered_label", "hostname", "transport", "capability_profile",
                      "operational_transport_busy", "heartbeat_fresh", "last_seen_age_seconds", "activity_state")
            return _serializable({"ok": True, "nodes": [
                {**{key: entry[key] for key in fields if key in entry},
                 "registered_label": entry.get("registered_label") or entry["alias"],
                 "hostname_verified": entry.get("hostname_verified", entry.get("transport") == "local") is True,
                 "online": entry.get("operational_transport_ready") is True,
                 "transport_ready": entry.get("operational_transport_ready") is True}
                for entry in sorted(result["nodes"], key=lambda entry: entry["node_id"])
            ]})
        except Exception:
            return {"ok": False, "error": "FLEET_NODES_FAILED"}

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_system_status(node: str) -> dict:
        """Read system status for an enrolled alias or node UUID, or LOCAL."""
        return read(node, "system.status")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_hardware_summary(node: str) -> dict:
        """Read hardware for an enrolled alias or node UUID, or LOCAL."""
        return read(node, "hardware.summary")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_storage_inspect(node: str) -> dict:
        """Read storage for an enrolled alias or node UUID, or LOCAL."""
        return read(node, "storage.inspect")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_network_inspect(node: str) -> dict:
        """Read networking for an enrolled alias or node UUID, or LOCAL."""
        return read(node, "network.inspect")

    @mcp.tool(annotations=dict(_ANNOTATIONS))
    def framebridge_fleet_node_service_status(node: str, service: str) -> dict:
        """Read service status for an enrolled alias or node UUID, or LOCAL."""
        return read(node, "service.status", {"service": service})
