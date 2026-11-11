"""A fake ACP agent for the tests: no model, scripted by the prompt's words.

- ``write NAME CONTENT``: writes the file in its cwd with the client's fs, says done
- ``ask``: asks permission first, then says what it was granted
- ``hang``: never ends the turn
- ``recall``: says the previous prompt of the session
- ``call TOOL``: says it would call TOOL on the session's first MCP server (no MCP client here)
- ``mcp TOOL JSON``: starts the session's first MCP server as given (stdio), calls TOOL with
  the arguments JSON, and says what it answered
- ``model``: calls the model at OPENAI_BASE_URL once and says its reply
- ``swallow``: the same, but a refused call is no error to it: it says so and ends its turn
- ``die``: exits mid-turn
- ``warn``: warns, then says ok - the warning a notice when the client takes them, else text, as an adapter does
- anything else: says it back, upper-cased
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

n = [100]
pending = {}
sessions = {}
caps = [{}]                                   # the client's capabilities, from initialize
lock = threading.Lock()


def send(m):
    with lock:
        sys.stdout.write(json.dumps(m) + "\n")
        sys.stdout.flush()


def ask(method, params):
    n[0] += 1
    ev = threading.Event()
    pending[n[0]] = [ev, None]
    send({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params})
    ev.wait(30)
    return pending.pop(n[0])[1]


def say(sid, text):
    for i in range(0, len(text), 4):
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text[i:i + 4]}}}})


def turn(mid, sid, text):
    s = sessions[sid]
    words = text.split()
    if words and words[0] == "hang":
        return
    if words and words[0] == "die":
        os._exit(3)
    if words and words[0] == "warn":
        if isinstance((caps[0].get("session") or {}).get("notices"), dict):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
                "sessionUpdate": "notice", "severity": "warning", "title": "Model metadata not found"}}})
        else:
            say(sid, "Warning: Model metadata not found.\n\n")
        say(sid, "ok")
    elif words and words[0] == "write":
        path = os.path.join(s["cwd"], words[1])
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "tool_call", "toolCallId": "t1", "title": "write " + words[1], "kind": "edit", "status": "in_progress",
            "rawInput": {"path": path}}}})
        r = ask("fs/write_text_file", {"sessionId": sid, "path": path, "content": " ".join(words[2:])})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed" if "error" not in r else "failed",
            "rawOutput": r}}})
        say(sid, "done")
    elif words and words[0] == "ask":
        r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "t2", "title": "rm"},
                                               "options": [{"optionId": "no", "kind": "reject_once", "name": "No"},
                                                           {"optionId": "yes", "kind": "allow_once", "name": "Yes"}]})
        say(sid, "granted " + (r["result"]["outcome"].get("optionId") or "none"))
    elif words and words[0] == "recall":
        say(sid, s["said"][-1] if s["said"] else "nothing")
    elif words and words[0] == "call":
        say(sid, "servers " + ",".join(m["name"] for m in s["mcp"]))
    elif words and words[0] == "mcp":
        m = s["mcp"][0]
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
        r = rpc(2, "tools/call", {"name": words[1], "arguments": json.loads(" ".join(words[2:]) or "{}")})
        p.stdin.close()
        p.wait(timeout=10)
        say(sid, r["result"]["content"][0]["text"])
    elif words and words[0] in ("model", "swallow"):
        req = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/chat/completions",
                                     data=json.dumps({"model": "fake", "messages": [{"role": "user", "content": text}]}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")})
        try:
            got = json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            if words[0] == "model":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000, "message": f"model HTTP {e.code}"}})
                return
            got = {"choices": [{"message": {"content": "gave up"}}]}
        say(sid, got["choices"][0]["message"]["content"])
    else:
        say(sid, text.upper())
    s["said"].append(text)
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    m = json.loads(line)
    if "method" not in m:
        p = pending.get(m["id"])
        if p:
            p[1] = m
            p[0].set()
        continue
    meth, params = m["method"], m.get("params") or {}
    if meth == "initialize":
        caps[0] = params.get("clientCapabilities") or {}
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif meth == "session/new":
        sid = "s%d" % (len(sessions) + 1)
        sessions[sid] = {"cwd": params["cwd"], "mcp": params.get("mcpServers") or [], "said": []}
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": sid}})
    elif meth == "session/prompt":
        text = " ".join(c.get("text", "") for c in params["prompt"])
        threading.Thread(target=turn, args=(m["id"], params["sessionId"], text), daemon=True).start()
    elif meth == "session/cancel":
        pass
    elif "id" in m:
        send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no " + meth}})
