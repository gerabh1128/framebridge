"""Typed READ dispatch only. The injected adapter is the sole execution boundary.

No default adapter, shell, SSH, sockets, plan engine, or authority from text.
The embedding application must supply a trusted read-only adapter; this module
does not sandbox arbitrary Python callbacks or authenticate remote callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Callable, Mapping

from .fleet_registry import FleetNode, FleetRegistry, RegistryError


class Operation(Enum):
    READ = "READ"
    WRITE = "WRITE"
    PLAN = "PLAN"
    APPLY = "APPLY"
    ROLLBACK = "ROLLBACK"
    EXEC = "EXEC"


class ReadCapability(Enum):
    SYSTEM_STATUS = "system.status"
    HARDWARE_SUMMARY = "hardware.summary"
    STORAGE_INSPECT = "storage.inspect"
    NETWORK_INSPECT = "network.inspect"
    SERVICE_STATUS = "service.status"


READ_ALLOWLIST = frozenset(item.value for item in ReadCapability)
_FORBIDDEN_EXEC = frozenset({"framebridge_exec", "shell", "bash", "exec"})
_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")


def normalize_read_args(capability: ReadCapability, args: object) -> dict[str, str]:
    """Return a fresh, closed-schema dictionary; never interpret supplied text."""
    if type(capability) is not ReadCapability:
        raise ValueError("FLEET_ARGS_INVALID")
    if args is None:
        args = {}
    if type(args) not in (dict, MappingProxyType):
        raise ValueError("FLEET_ARGS_INVALID")
    if capability is ReadCapability.SERVICE_STATUS:
        if (set(args) != {"service"} or type(args["service"]) is not str
                or _SERVICE_NAME.fullmatch(args["service"]) is None):
            raise ValueError("FLEET_ARGS_INVALID")
        return {"service": args["service"]}
    if args:
        raise ValueError("FLEET_ARGS_INVALID")
    return {}


@dataclass(frozen=True)
class FleetRequest:
    target: str
    capability: str
    operation: Operation = Operation.READ
    context_text: str = ""  # Inert text; never forwarded to an adapter.
    args: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        # Snapshot caller dictionaries before dispatch, including read-only views.
        # Invalid inputs remain available for the router's fixed error result.
        if type(self.args) in (dict, MappingProxyType):
            object.__setattr__(self, "args", MappingProxyType(dict(self.args)))


@dataclass(frozen=True)
class ReadRequest:
    node_id: str
    capability: ReadCapability
    operation: Operation = Operation.READ
    args: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", MappingProxyType(
            normalize_read_args(self.capability, self.args)))


@dataclass(frozen=True)
class RouteResult:
    ok: bool
    error: str | None = None
    node_id: str | None = None
    capability: str | None = None
    data: object = None


class FleetRouter:
    def __init__(self, registry: FleetRegistry,
                 adapter: Callable[[FleetNode, ReadRequest], object]) -> None:
        if not isinstance(registry, FleetRegistry) or not callable(adapter):
            raise ValueError("FLEET_ROUTER_CONFIG_INVALID")
        self._registry = registry
        self._adapter = adapter

    def route(self, request: FleetRequest) -> RouteResult:
        if type(request) is not FleetRequest:
            return RouteResult(False, "FLEET_REQUEST_INVALID")
        # Enum identity deliberately rejects strings and foreign enum values.
        if request.operation is not Operation.READ:
            return RouteResult(False, "FLEET_OPERATION_BLOCKED")
        if type(request.capability) is not str or type(request.context_text) is not str:
            return RouteResult(False, "FLEET_REQUEST_INVALID")
        if request.capability.strip().casefold() in _FORBIDDEN_EXEC:
            return RouteResult(False, "FLEET_EXEC_FORBIDDEN")
        if request.capability not in READ_ALLOWLIST:
            return RouteResult(False, "FLEET_CAPABILITY_NOT_ALLOWED")
        try:
            args = normalize_read_args(ReadCapability(request.capability), request.args)
        except ValueError:
            return RouteResult(False, "FLEET_ARGS_INVALID")
        try:
            node = self._registry.resolve(request.target)
        except RegistryError as exc:
            return RouteResult(False, str(exc))
        if not node.online:
            return RouteResult(False, "FLEET_NODE_OFFLINE", node.node_id)
        typed = ReadRequest(node.node_id, ReadCapability(request.capability), args=args)
        try:
            data = self._adapter(node, typed)
        except Exception:
            return RouteResult(False, "FLEET_ADAPTER_FAILED", node.node_id,
                               request.capability)
        return RouteResult(True, node_id=node.node_id,
                           capability=request.capability, data=data)
