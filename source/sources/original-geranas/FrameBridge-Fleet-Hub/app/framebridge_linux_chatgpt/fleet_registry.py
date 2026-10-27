"""In-memory Fleet registry. Identity is explicit; no discovery or transport I/O."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class RegistryError(ValueError):
    """Stable error code only; never includes supplied metadata."""


def _identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


@dataclass(frozen=True)
class FleetNode:
    node_id: str
    alias: str
    hostname: str
    transport: str
    capability_profile: str
    online: bool = False

    def __post_init__(self) -> None:
        if not _identifier(self.node_id) or not _identifier(self.alias):
            raise RegistryError("FLEET_IDENTIFIER_INVALID")
        for value in (self.hostname, self.transport, self.capability_profile):
            if (type(value) is not str or not value or len(value) > 253
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise RegistryError("FLEET_METADATA_INVALID")
        if type(self.online) is not bool:
            raise RegistryError("FLEET_STATE_INVALID")


class FleetRegistry:
    """Case-sensitive identifiers, sorted by node_id; offline unless specified.

    Aliases and IDs share one namespace to prevent ambiguous destination lookup.
    Metadata cannot grant capabilities. Instances are for synchronous local use.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, FleetNode] = {}
        self._aliases: dict[str, str] = {}

    def register(self, node: FleetNode) -> FleetNode:
        if type(node) is not FleetNode:
            raise RegistryError("FLEET_NODE_INVALID")
        if node.node_id in self._nodes:
            raise RegistryError("FLEET_DUPLICATE_NODE_ID")
        if node.alias in self._aliases:
            raise RegistryError("FLEET_DUPLICATE_ALIAS")
        if node.node_id in self._aliases or node.alias in self._nodes:
            raise RegistryError("FLEET_IDENTIFIER_COLLISION")
        self._nodes[node.node_id] = node
        self._aliases[node.alias] = node.node_id
        return node

    def list_nodes(self) -> tuple[FleetNode, ...]:
        return tuple(self._nodes[key] for key in sorted(self._nodes))

    def resolve(self, target: str) -> FleetNode:
        if not _identifier(target):
            raise RegistryError("FLEET_IDENTIFIER_INVALID")
        node_id = target if target in self._nodes else self._aliases.get(target)
        if node_id is None:
            raise RegistryError("FLEET_NODE_NOT_FOUND")
        return self._nodes[node_id]

    def set_online(self, target: str, online: bool) -> FleetNode:
        node = replace(self.resolve(target), online=online)
        self._nodes[node.node_id] = node
        return node
