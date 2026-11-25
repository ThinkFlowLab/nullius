"""VitaBench's tools as a stdio MCP server. It executes nothing: the list of tools
(the upstream's own schemas) and each call go to the driver over the unit's socket
(``VITA_AT``), where the upstream's Orchestrator executes the call in its own
environment. Each request is one connection, a JSON line each way. A call is never
refused before the upstream sees it."""
import json
import os
import socket

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def ask(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.environ["VITA_AT"])
        f = s.makefile("rwb")
        f.write((json.dumps(request) + "\n").encode())
        f.flush()
        return json.loads(f.readline() or b"{}")


def call(name, arguments):
    try:
        got = ask({"name": name, "arguments": arguments})
        return str(got["text"]), bool(got["isError"])
    except (OSError, ValueError, KeyError) as e:
        return f"Error: the benchmark could not be reached ({e})", True


def main():
    server = Server("vita")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**t) for t in (await anyio.to_thread.run_sync(ask, {"list": True}))["tools"]]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        text, error = await anyio.to_thread.run_sync(call, name, {} if arguments is None else arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    main()
