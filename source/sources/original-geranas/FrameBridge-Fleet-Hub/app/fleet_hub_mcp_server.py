"""Separate loopback-only MCP entry point for the six Fleet READ tools."""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from framebridge_node_gateway import discover_nodes, node_read
from framebridge_linux_chatgpt.fleet_hub import FleetHub
from framebridge_linux_chatgpt.fleet_mcp_surface import register_fleet_read_tools


def _server_address() -> tuple[str, int]:
    host = os.environ.get("FRAMEBRIDGE_FLEET_HUB_HOST", "127.0.0.1")
    if not isinstance(host, str) or host not in ("127.0.0.1", "::1"):
        raise ValueError("FLEET_HUB_HOST_INVALID")
    try:
        port = int(os.environ.get("FRAMEBRIDGE_FLEET_HUB_PORT", "8877"))
    except (TypeError, ValueError):
        raise ValueError("FLEET_HUB_PORT_INVALID") from None
    if not 1024 <= port <= 65535:
        raise ValueError("FLEET_HUB_PORT_INVALID")
    return host, port


host, port = _server_address()
mcp = FastMCP(
    "FrameBridge Fleet Hub",
    host=host,
    port=port,
    streamable_http_path="/mcp",
)
hub = FleetHub(discover_nodes, node_read)
register_fleet_read_tools(mcp, hub)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
