"""Synchronous, in-memory Fleet READ hub using injected discovery and gateway."""
from __future__ import annotations

from typing import Callable

from .fleet_gateway_adapter import GatewayReadAdapter
from .fleet_registry import FleetNode, FleetRegistry, RegistryError
from .fleet_routing import FleetRequest, FleetRouter, RouteResult


class FleetHubError(ValueError):
    """Sanitized discovery/configuration failure."""


class FleetHub:
    def __init__(self, discover_nodes: Callable[[], dict],
                 gateway_node_read: Callable[[str, str, dict], dict]) -> None:
        if not callable(discover_nodes):
            raise FleetHubError("FLEET_HUB_CONFIG_INVALID")
        self._discover_nodes = discover_nodes
        self._adapter = GatewayReadAdapter(gateway_node_read)
        registry = FleetRegistry()
        self._snapshot = (registry, FleetRouter(registry, self._adapter))

    def refresh(self) -> tuple[FleetNode, ...]:
        """Publish only a complete valid registry; failures preserve the old one.

        Entries with explicit pairing state must be paired. Legacy entries need
        an explicit stable ID too. Missing descriptive metadata stays unknown;
        it never supplies identity or readiness. Invalid entries are omitted,
        but duplicate IDs/aliases and cross-namespace collisions abort refresh.
        """
        try:
            discovery = self._discover_nodes()
            if (type(discovery) is not dict or discovery.get("ok") is not True
                    or type(discovery.get("nodes")) is not list):
                raise FleetHubError("FLEET_REFRESH_FAILED")
            registry = FleetRegistry()
            for entry in discovery["nodes"]:
                if type(entry) is not dict:
                    continue
                if "pairing_state" in entry and entry["pairing_state"] != "paired":
                    continue
                try:
                    node = FleetNode(
                        node_id=entry.get("node_id"), alias=entry.get("alias"),
                        hostname=entry.get("hostname", "unknown"),
                        transport=entry.get("transport", "unknown"),
                        capability_profile=entry.get("capability_profile", "unknown"),
                        online=entry.get("operational_transport_ready") is True,
                    )
                except RegistryError:
                    continue
                registry.register(node)
            router = FleetRouter(registry, self._adapter)
        except Exception:
            raise FleetHubError("FLEET_REFRESH_FAILED") from None
        self._snapshot = (registry, router)
        return registry.list_nodes()

    def read(self, target: str, capability: str, args: dict | None = None) -> RouteResult:
        return self._snapshot[1].route(FleetRequest(target, capability, args=args))

    def nodes(self) -> tuple[FleetNode, ...]:
        return self._snapshot[0].list_nodes()
