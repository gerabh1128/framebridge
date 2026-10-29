"""Injected Node Gateway READ boundary; no transport or live gateway imports."""
from __future__ import annotations

from typing import Callable

from .fleet_registry import FleetNode
from .fleet_routing import Operation, ReadRequest, normalize_read_args


class GatewayReadError(ValueError):
    """Fixed local error codes; remote exceptions are never copied."""


class GatewayReadAdapter:
    def __init__(self, gateway_node_read: Callable[[str, str, dict], dict]) -> None:
        if not callable(gateway_node_read):
            raise GatewayReadError("FLEET_GATEWAY_CONFIG_INVALID")
        self._gateway_node_read = gateway_node_read

    def __call__(self, node: FleetNode, request: ReadRequest) -> dict:
        if type(node) is not FleetNode or type(request) is not ReadRequest:
            raise GatewayReadError("FLEET_GATEWAY_REQUEST_INVALID")
        if request.node_id != node.node_id:
            raise GatewayReadError("FLEET_GATEWAY_NODE_MISMATCH")
        if request.operation is not Operation.READ or not node.online:
            raise GatewayReadError("FLEET_GATEWAY_READ_BLOCKED")
        try:
            args = normalize_read_args(request.capability, request.args)
        except ValueError:
            raise GatewayReadError("FLEET_GATEWAY_REQUEST_INVALID") from None
        try:
            response = self._gateway_node_read(node.alias, request.capability.value, args)
        except Exception:
            raise GatewayReadError("FLEET_GATEWAY_READ_FAILED") from None
        if type(response) is not dict or response.get("ok") is not True:
            raise GatewayReadError("FLEET_GATEWAY_READ_FAILED")
        for key in ("node", "alias"):
            if key in response and (type(response[key]) is not str
                                    or response[key] != node.alias):
                raise GatewayReadError("FLEET_GATEWAY_ALIAS_MISMATCH")
        # Metadata is payload only. It cannot update identity, routing or policy.
        return response
