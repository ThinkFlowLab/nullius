"""A fake ACP agent for the computer kit's tests: no model; whatever the prompt, it looks and clicks once.

At each prompt it starts the session's MCP server named computer (stdio, as given), lists its tools, takes a
screenshot, clicks at a fixed point (``FAKE_CLICK``, "x,y", the screen's centre by default), and says what the
click answered. Its own calls are the proof that an ACP session reaches the server through the pipe. Started
with the word ``hang``, it never ends its turn after the click: a run can be stopped during the unit.
"""
import json
import os
import subprocess
import sys
import threading
import time

HANG = sys.argv[1:] == ["hang"]
lock = threading.Lock()
sessions = {}


def send(m):
    with lock:
        sys.stdout.write(json.dumps(m) + "\n")
        sys.stdout.flush()


def say(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def turn(mid, sid):
    servers = [m for m in sessions[sid] if m.get("name") == "computer"]
    if not servers:
        say(sid, "no computer server")
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        return
    m = servers[0]
    env = dict(os.environ, **{e["name"]: e["value"] for e in m.get("env") or []})
    p = subprocess.Popen([m["command"]] + list(m.get("args") or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env=env)

    def rpc(rid, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        while True:
            r = json.loads(p.stdout.readline())
            if r.get("id") == rid:
                return r

    rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    tools = [t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]]
    shot = rpc(3, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
    x, y = (int(v) for v in os.environ.get("FAKE_CLICK", "960,540").split(","))
    click = rpc(4, "tools/call", {"name": "click", "arguments": {"x": x, "y": y}})["result"]
    p.stdin.close()
    p.wait(timeout=30)
    said = next((c["text"] for c in click["content"] if c["type"] == "text"), "")
    say(sid, f"tools {len(tools)}; screenshot {'failed' if shot['isError'] else 'taken'}; click {said}")
    while HANG:
        time.sleep(3600)
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    m = json.loads(line)
    if "method" not in m:
        continue
    meth, params = m["method"], m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif meth == "session/new":
        sid = "s%d" % (len(sessions) + 1)
        sessions[sid] = params.get("mcpServers") or []
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": sid}})
    elif meth == "session/prompt":
        threading.Thread(target=turn, args=(m["id"], params["sessionId"]), daemon=True).start()
    elif meth == "session/cancel":
        pass
    elif "id" in m:
        send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no " + meth}})
