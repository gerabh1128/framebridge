"""Run FrameBridge's HTTP MCP endpoints without optional WebSocket detection.

The FastMCP app owns its routes, middleware and session-manager lifespan.
Other FrameBridge transports do not use this helper.
"""


def run_streamable_mcp(server):
    import anyio
    import uvicorn

    async def serve():
        config = uvicorn.Config(
            server.streamable_http_app(),
            host=server.settings.host,
            port=server.settings.port,
            log_level=server.settings.log_level.lower(),
            ws="none",
        )
        await uvicorn.Server(config).serve()

    anyio.run(serve)
