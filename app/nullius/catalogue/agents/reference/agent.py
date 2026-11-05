"""The reference agent: the smallest complete agent in the Nullius standard.

Python's standard library only, so it is also the one to copy. It speaks ACP
on stdin and stdout, keeps one conversation per session, and works in a loop:
ask the model (OpenAI Chat Completions at $OPENAI_BASE_URL), run the tools it
asks for, give it their results, until it answers without a tool. Its tools
are four of its own, working in the session's directory - read_file,
write_file, list_files, run - and every tool of the MCP servers the session
was given, named ``<server>__<tool>``.

Every step is told to the client as it happens: the model's text as
``agent_message_chunk``, each tool call as ``tool_call`` then
``tool_call_update`` with its result.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

MAX_STEPS = int(os.environ.get("REFERENCE_MAX_STEPS") or 30)
OUT_LIMIT = 8000
SYSTEM = ("You are a careful software agent working in the directory {cwd}. Use the tools to inspect and change files "
          "and to run commands there. When the task is done, reply with a short final message and no tool call. "
          "If the task asks for a specific reply, give exactly that reply as your final message.")
OWN_TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file in the working directory.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a whole text file in the working directory.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                                     "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "List the files under a folder of the working directory.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run", "description": "Run a shell command in the working directory (60 s at most).",
                                      "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

lock = threading.Lock()
sessions = {}


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


class Mcp:
    """A stdio MCP server the session was given: started once, asked for its
    tools, called by name."""

    def __init__(self, spec, cwd):
        env = dict(os.environ, **{e["name"]: e["value"] for e in spec.get("env") or []})
        self.name = spec["name"]
        self.p = subprocess.Popen([spec["command"]] + list(spec.get("args") or []), cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.n = 0
        self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "reference", "version": "1"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()
        self.tools = self.ask("tools/list", {}).get("tools") or []

    def ask(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        for line in self.p.stdout:
            m = json.loads(line)
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message"))
                return m.get("result") or {}
        raise RuntimeError(f"MCP server {self.name} ended")

    def call(self, tool, args):
        r = self.ask("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text") or json.dumps(c) for c in r.get("content") or [])
        return ("Error: " if r.get("isError") else "") + text


def inside(cwd, path):
    p = os.path.realpath(os.path.join(cwd, path or "."))
    if p != os.path.realpath(cwd) and not p.startswith(os.path.realpath(cwd) + os.sep):
        raise ValueError(f"{path} is outside the working directory")
    return p


def run_tool(s, name, args):
    cwd = s["cwd"]
    if name == "read_file":
        with open(inside(cwd, args["path"]), errors="replace") as f:
            return f.read()[:OUT_LIMIT]
    if name == "write_file":
        p = inside(cwd, args["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(args.get("content") or "")
        return f"wrote {len(args.get('content') or '')} characters to {args['path']}"
    if name == "list_files":
        base = inside(cwd, args.get("path") or ".")
        out = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            out += [os.path.relpath(os.path.join(root, f), cwd) for f in files]
        return "\n".join(sorted(out)[:500]) or "(empty)"
    if name == "run":
        try:
            r = subprocess.run(args["command"], shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
            out = (r.stdout + r.stderr)[-OUT_LIMIT:]
            return f"exit {r.returncode}\n{out}"
        except subprocess.TimeoutExpired:
            return "Error: the command did not end in 60 s"
    server, _, tool = name.partition("__")
    for m in s["mcp"]:
        if m.name == server:
            return m.call(tool, args)
    raise ValueError(f"no tool {name}")


def model(messages, tools):
    body = {"model": os.environ.get("REFERENCE_MODEL") or "default", "messages": messages, "tools": tools}
    req = urllib.request.Request(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["choices"][0]["message"]


def turn(rid, sid, text):
    s = sessions[sid]
    s["messages"].append({"role": "user", "content": text})
    tools = OWN_TOOLS + [{"type": "function", "function": {"name": f"{m.name}__{t['name']}", "description": t.get("description") or "",
                                                            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
                         for m in s["mcp"] for t in m.tools]
    try:
        for step in range(MAX_STEPS):
            if s["cancelled"]:
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                return
            msg = model(s["messages"], tools)
            calls = msg.get("tool_calls") or []
            s["messages"].append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if msg.get("content"):
                update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": msg["content"]}})
            if not calls:
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
                return
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                kind = {"read_file": "read", "write_file": "edit", "list_files": "search", "run": "execute"}.get(name, "other")
                update(sid, {"sessionUpdate": "tool_call", "toolCallId": c["id"], "title": name, "kind": kind,
                             "status": "in_progress", "rawInput": args})
                try:
                    out, status = run_tool(s, name, args), "completed"
                except Exception as e:
                    out, status = f"Error: {e}", "failed"
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": c["id"], "status": status,
                             "content": [{"type": "content", "content": {"type": "text", "text": out}}]})
                s["messages"].append({"role": "tool", "tool_call_id": c["id"], "content": out})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "max_turn_requests"}})
    except urllib.error.HTTPError as e:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"model: HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"}})
    except Exception as e:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        method, params, rid = m.get("method"), m.get("params") or {}, m.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": False, "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False}},
                "agentInfo": {"name": "reference", "version": "1"}}})
        elif method == "session/new":
            sid = f"ref-{len(sessions) + 1}"
            try:
                servers = [Mcp(x, params["cwd"]) for x in params.get("mcpServers") or []]
            except Exception as e:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"MCP server: {e}"}})
                continue
            sessions[sid] = {"cwd": params["cwd"], "mcp": servers, "cancelled": False,
                             "messages": [{"role": "system", "content": SYSTEM.format(cwd=params["cwd"])}]}
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif method == "session/prompt":
            text = "\n".join(c.get("text") or "" for c in params.get("prompt") or [] if c.get("type") == "text")
            sessions[params["sessionId"]]["cancelled"] = False
            threading.Thread(target=turn, args=(rid, params["sessionId"], text), daemon=True).start()
        elif method == "session/cancel":
            if params.get("sessionId") in sessions:
                sessions[params["sessionId"]]["cancelled"] = True
        elif rid is not None and method:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"{method} is not supported"}})


if __name__ == "__main__":
    main()
