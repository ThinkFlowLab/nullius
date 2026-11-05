"""τ³-bench's domain tools as a stdio MCP server: the upstream's tools, the
upstream's Orchestrator executing them.

The tools it declares are the upstream's (``tau2-bench`` at the release tag
``standard.json`` pins, installed as the driver's environment,
``driver.install``): the domain's ``Environment`` from the upstream's
registry, each tool's schema its ``Tool.openai_schema``. It executes
nothing: each call goes to the driver (``TAU_AT``, the unit's socket on this
machine), where it becomes the agent's next message to the upstream's
``Orchestrator``, which executes it in its own environment - counting its
steps and errors, ending the conversation by its rules - and the answer it
gives (the result, or ``Error: <what was raised>``) comes back as the
call's result. The MCP side is the official ``mcp`` SDK (its low-level
server, so the schemas go out as the upstream writes them and a call is
never refused before the upstream sees it).

What is ours, glue only: each schema's ``$ref``s written out in place
(``inline``), since not every MCP client follows a reference - the schema
says the same thing; and the pass-through, one connection to the driver per
call, a JSON line each way.

``TAU_DOMAIN`` names the domain (``airline``, ``retail``, ...), ``TAU_RETRIEVAL`` a knowledge domain's retrieval config. It runs on the
machine in the driver's environment, never in the agent's container
(``mcp_servers`` in ``standard.json``; ``nullius.standard.mcp_machine``).
"""
import contextlib
import copy
import json
import os
import socket
import sys

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")     # no download at import: the server calls no model

with contextlib.redirect_stdout(sys.stderr):                        # stdout carries the protocol, nothing else
    from loguru import logger
    from tau2.registry import registry

    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

logger.remove()
logger.add(sys.stderr, level="ERROR")                               # the upstream's own default (config.DEFAULT_LOG_LEVEL)


def inline(schema):
    """A JSON schema with each ``#/$defs/...`` reference replaced by what it names."""
    defs = schema.get("$defs") or {}

    def walk(x):
        if isinstance(x, dict):
            ref = x.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and ref[len("#/$defs/"):] in defs:
                return walk(copy.deepcopy(defs[ref[len("#/$defs/"):]]))
            return {k: walk(v) for k, v in x.items() if k != "$defs"}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(schema)


def tools_of(env):
    """The domain's tools as MCP declares them: the upstream's schemas."""
    out = []
    for t in env.get_tools():
        f = t.openai_schema["function"]
        out.append({"name": f["name"], "description": f["description"], "inputSchema": inline(f["parameters"])})
    return out


def call(at, name, arguments):
    """One call, through the driver: (text, error)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(at)
            f = s.makefile("rwb")
            f.write((json.dumps({"name": name, "arguments": arguments}) + "\n").encode())
            f.flush()
            got = json.loads(f.readline() or b"{}")
        return str(got["text"]), bool(got["isError"])
    except (OSError, ValueError, KeyError) as e:
        return f"Error: the benchmark could not be reached ({e})", True


def main():
    domain, at = os.environ["TAU_DOMAIN"], os.environ["TAU_AT"]
    # a knowledge domain's tools follow its retrieval config (the run's retrieval option): the same search the driver's environment has
    retrieval = os.environ.get("TAU_RETRIEVAL")
    declared = tools_of(registry.get_env_constructor(domain)(**({"retrieval_variant": retrieval} if retrieval else {})))
    server = Server(domain)

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**t) for t in declared]

    @server.call_tool(validate_input=False)                         # the upstream answers a malformed call itself
    async def call_tool(name, arguments):
        text, error = await anyio.to_thread.run_sync(call, at, name, {} if arguments is None else arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    main()
