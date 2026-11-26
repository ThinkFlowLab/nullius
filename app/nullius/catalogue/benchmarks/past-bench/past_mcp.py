"""The current episode's tools, as a stdio MCP server: the benchmark's own tools.

PAST-Bench hands every task its tools through the task file - a name, a JSON
schema and an HTTP endpoint - and its graders read the services' audit logs to
see what the agent actually did. The tools are therefore the benchmark's, not
the agent's, and this server is how they reach an agent that only speaks MCP:

    python past_mcp.py STATE_DIR

STATE_DIR/current.json is written by the driver before each episode and names
that episode's tools, each one already rewritten to the port its mock service
got. This server reads it on every listing and every call, so a new session
sees the new episode's tools; a call is one POST to the endpoint, and both the
request and the answer are appended to STATE_DIR/dispatch.jsonl for the
grader's ``dispatches``.

The protocol is the stdio transport of MCP: JSON-RPC 2.0, one object per line,
the server answering requests and staying silent on notifications.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()
CONTROL = STATE / "current.json"
DISPATCH = STATE / "dispatch.jsonl"
PROTOCOL = "2024-11-05"
_TAG = re.compile(r"[^A-Za-z0-9_-]")


def current() -> dict:
    try:
        return json.loads(CONTROL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tools": []}


def tools() -> list:
    """The episode's tools, in MCP's spelling, each remembering its own name."""
    out, seen = [], set()
    for tool in current().get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        name = _TAG.sub("_", str(tool["name"]))[:64]
        while name in seen:
            name = name[:-1] + "_"
        seen.add(name)
        out.append({"name": name,
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("input_schema") or {"type": "object", "properties": {}},
                    "_upstream": str(tool["name"]), "_url": str(tool.get("url") or ""),
                    "_method": str(tool.get("method") or "POST")})
    return out


def dispatch(kind: str, tool: dict, body, reply: dict, ms: float) -> None:
    line = {"episode": current().get("episode"), "tool": tool.get("_upstream") or tool["name"],
            "endpoint_url": tool.get("_url", ""), "request_body": body,
            "response_status": reply.get("status", 200), "response_body": reply.get("body"),
            "latency_ms": round(ms, 1), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
    try:
        with DISPATCH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def post(url: str, method: str, body) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:                     # unreachable service: a failed call, not a dead session
        return {"status": 599, "body": {"error": f"{type(e).__name__}: {e}"}}
    try:
        return {"status": status, "body": json.loads(raw)}
    except ValueError:
        return {"status": status, "body": raw[:4000]}


def call(params: dict) -> dict:
    name = str(params.get("name") or "")
    body = params.get("arguments") or {}
    tool = next((t for t in tools() if t["name"] == name), None)
    if tool is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"no tool {name!r} in this episode"})}],
                "isError": True}
    started = time.time()
    reply = post(tool["_url"], tool["_method"], body) if tool["_url"] else \
        {"status": 404, "body": {"error": "the tool has no endpoint"}}
    dispatch("tool_call", tool, body, reply, (time.time() - started) * 1000.0)
    text = reply["body"] if isinstance(reply["body"], str) else json.dumps(reply["body"], ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": reply["status"] >= 400}


def answer(request_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": request_id}
    message.update({"result": result} if error is None else {"error": {"code": error[0], "message": error[1]}})
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:                 # a notification: initialized, cancelled, ...
            continue
        try:
            if method == "initialize":
                answer(request_id, {"protocolVersion": PROTOCOL,
                                    "capabilities": {"tools": {"listChanged": True}},
                                    "serverInfo": {"name": "past-bench", "version": "1.0"}})
            elif method == "tools/list":
                answer(request_id, {"tools": [{k: v for k, v in t.items() if not k.startswith("_")}
                                              for t in tools()]})
            elif method == "tools/call":
                answer(request_id, call(request.get("params") or {}))
            elif method == "ping":
                answer(request_id, {})
            else:
                answer(request_id, error=(-32601, f"unknown method {method!r}"))
        except Exception as exc:
            answer(request_id, error=(-32603, f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    main()
