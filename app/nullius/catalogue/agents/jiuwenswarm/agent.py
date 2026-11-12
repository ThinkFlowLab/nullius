"""JiuwenSwarm as an ACP agent for Nullius: one unit, one JiuwenSwarm.

JiuwenSwarm's ACP entry (``jiuwenswarm-acp``) is a stdio bridge to a running
Gateway, which talks to an AgentServer. This wrapper, run with the install's
own Python, gives the unit all three:

1. a JiuwenSwarm home of its own under ``$HOME`` (Nullius gives every unit its
   own), initialised once as ``jiuwenswarm-init`` does, in English;
2. ``config/.env`` written over with the relay as the model (an
   OpenAI-compatible client: ``MODEL_PROVIDER=OpenAI``, ``API_BASE``,
   ``API_KEY``, ``MODEL_NAME``) and free ports, so units run side by side;
3. the AgentServer and Gateway (``python -m jiuwenswarm.app``) started in the
   background, their output on stderr, and waited for;
4. the ACP bridge run with this process's stdout: stdout carries only ACP.

Between Nullius and the bridge, ACP is passed through line by line with three
changes, each taken from the protocol itself:

- ``session/new``'s ``mcpServers`` are written into ``config.yaml``'s
  ``mcp.servers``: JiuwenSwarm reads MCP servers from its config, not from the
  session (they are loaded when the session's agent is made, after this);
- each ``session/prompt`` carries ``workspace_dir`` = the session's ``cwd``,
  JiuwenSwarm's own field for scoping a prompt's working directory;
- on the way back, an ``fs/read_text_file`` or ``fs/write_text_file`` request
  gets the session's id and an absolute path. On ACP, JiuwenSwarm blocks its
  own file and shell tools and uses the client's, but sends its internal
  session id rather than the one ``session/new`` returned, and the model's
  path as written, often relative; ACP requires the session's id and an
  absolute path, and the client refuses the request without them. A client
  that offers no files (Nullius on docker, where this wrapper runs in the
  agent's container) gets none: ``initialize`` offers them to the bridge, and
  this wrapper answers each request itself, inside the session's cwd.
  Requests for the unit's own JiuwenSwarm home are served by this wrapper
  on both backends, so its config and todo files remain accessible.

The resident worker uses a short, private socket path. When the bridge ends,
the servers and worker are stopped.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

PY = sys.executable
HOME = os.environ.get("HOME") or os.path.expanduser("~")
JHOME = os.path.join(HOME, ".jiuwenswarm")
CONFIG = os.path.join(JHOME, "config")
READY_SECONDS = 240


def say(*a):
    print("[nullius agent.py]", *a, file=sys.stderr, flush=True)


def free_ports(n):
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def write_env(ports):
    agent, gateway, web, front = ports
    values = {
        "MODEL_PROVIDER": "OpenAI",
        "MODEL_NAME": os.environ["NULLIUS_MODEL"],
        "API_BASE": os.environ["NULLIUS_BASE_URL"],
        "API_KEY": os.environ["NULLIUS_KEY"],
        "AGENT_SERVER_PORT": str(agent),
        "AGENT_PORT": str(agent),
        "GATEWAY_PORT": str(gateway),
        "WEB_PORT": str(web),
        "FRONTEND_PORT": str(front),
        "GATEWAY_HOST": "127.0.0.1",
        "AGENT_SERVER_HOST": "127.0.0.1",
    }
    with open(os.path.join(CONFIG, ".env"), "w") as f:      # loaded over the environment: it is the one source
        for k, v in values.items():
            f.write(f"{k}={v}\n")
    os.environ.update(values)


def memory_off():
    """``modes.agent.memory.enabled: false``. A unit starts with no memory, and
    with it on, the direct ``write_memory``/``read_memory`` tools stand in
    for the client's file tools, which JiuwenSwarm offers only behind
    ``tool_search``: the model writes the answer into its memory and stops."""
    import yaml
    path = os.path.join(CONFIG, "config.yaml")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("modes", {}).setdefault("agent", {}).setdefault("memory", {})["enabled"] = False
    with open(path + ".part", "w") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(path + ".part", path)


def reasoning_level():
    """The run's reasoning effort (the option reasoning_effort, NULLIUS_REASONING) as
    JiuwenSwarm's own ``reasoning_level`` of its default model; default leaves it
    unset, so nothing is sent, as JiuwenSwarm does on its own."""
    level = os.environ.get("NULLIUS_REASONING", "default")
    if level in ("", "default"):
        return
    import yaml
    path = os.path.join(CONFIG, "config.yaml")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    model = ((data.get("models") or {}).get("defaults") or [{}])[0]
    model.setdefault("model_config_obj", {})["reasoning_level"] = level
    with open(path + ".part", "w") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(path + ".part", path)


def mcp_into_config(servers):
    """ACP's stdio MCP servers as JiuwenSwarm's ``mcp.servers`` entries."""
    import yaml
    path = os.path.join(CONFIG, "config.yaml")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    entries = []
    for s in servers or []:
        if not s.get("command"):
            continue                                          # stdio only: what the standard sends
        env = s.get("env") or {}
        if isinstance(env, list):
            env = {e["name"]: e["value"] for e in env}
        entries.append({"name": s["name"], "enabled": True, "transport": "stdio",
                        "command": s["command"], "args": list(s.get("args") or []), "env": env})
    data.setdefault("mcp", {})
    if not isinstance(data["mcp"], dict):
        data["mcp"] = {}
    data["mcp"]["servers"] = entries
    with open(path + ".part", "w") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(path + ".part", path)
    say("mcp servers:", [e["name"] for e in entries])


class Sessions:
    """Each session's cwd, from session/new and the bridge's answer to it."""

    def __init__(self):
        self.pending, self.cwd, self.last, self.last_id = {}, {}, None, None
        self.serve_fs, self.agent_in, self.lock = False, None, threading.Lock()

    def of(self, sid):
        return self.cwd.get(sid) or self.last

    def to_bridge(self, line):
        with self.lock:
            self.agent_in.write(line)
            self.agent_in.flush()


def in_home(path):
    home = os.path.realpath(JHOME)
    path = os.path.realpath(path)
    return path == home or path.startswith(home + os.sep)


def serve_fs(msg, cwd):
    """Serve files in the session workspace or this unit's JiuwenSwarm home."""
    params, root = msg["params"], os.path.realpath(cwd or "/nonexistent")
    path = os.path.realpath(str(params.get("path") or ""))
    if path != root and not path.startswith(root + os.sep) and not in_home(path):
        return {"code": -32602, "message": f"{params.get('path')} is outside the session's directory"}, None
    try:
        if msg["method"] == "fs/write_text_file":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(str(params.get("content") or ""))
            return None, {}
        with open(path, errors="replace") as f:
            lines = f.read().splitlines(keepends=True)
        start = max(int(params.get("line") or 1), 1) - 1
        end = start + int(params["limit"]) if params.get("limit") else None
        return None, {"content": "".join(lines[start:end])}
    except (OSError, ValueError) as e:
        return {"code": -32603, "message": str(e)}, None


def to_agent(src, dst, ses):
    """Nullius's requests to the bridge: session/new's MCP servers put into
    the config, each prompt scoped to its session's cwd."""
    try:
        for line in iter(src.readline, b""):
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and isinstance(msg.get("params"), dict):
                method, params = msg.get("method"), msg["params"]
                if method == "initialize":
                    fs = (params.get("clientCapabilities") or {}).get("fs") or {}
                    if not (fs.get("readTextFile") and fs.get("writeTextFile")):
                        ses.serve_fs = True
                        params.setdefault("clientCapabilities", {})["fs"] = {"readTextFile": True, "writeTextFile": True}
                        line = (json.dumps(msg) + "\n").encode()
                elif method == "session/new":
                    mcp_into_config(params.get("mcpServers"))
                    ses.pending[msg.get("id")] = ses.last = params.get("cwd")
                elif method == "session/prompt":
                    cwd = ses.of(params.get("sessionId"))
                    if cwd:
                        params.setdefault("workspace_dir", cwd)
                        line = (json.dumps(msg) + "\n").encode()
            ses.to_bridge(line)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass


def to_client(src, dst, ses):
    """The bridge's messages to Nullius: an fs/ request given the ACP session
    id (JiuwenSwarm sends its own internal one), and a relative path made
    absolute against that session's cwd (ACP paths are absolute)."""
    for line in iter(src.readline, b""):
        try:
            msg = json.loads(line)
        except ValueError:
            say("not protocol, kept off stdout:", line[:200])
            continue
        if isinstance(msg, dict):
            res = msg.get("result")
            if msg.get("id") in ses.pending and isinstance(res, dict) and res.get("sessionId"):
                ses.cwd[res["sessionId"]] = ses.pending.pop(msg["id"])
                ses.last_id = res["sessionId"]
            params = msg.get("params")
            if str(msg.get("method", "")).startswith("fs/") and isinstance(params, dict):
                if params.get("sessionId") not in ses.cwd and ses.last_id:
                    params["sessionId"] = ses.last_id          # its own internal id, or none
                path, cwd = params.get("path"), ses.of(params.get("sessionId"))
                if isinstance(path, str) and path and not os.path.isabs(path) and cwd:
                    params["path"] = os.path.normpath(os.path.join(cwd, path))
                own_home = isinstance(params.get("path"), str) and in_home(params["path"])
                if (ses.serve_fs or own_home) and "id" in msg:
                    err, res = serve_fs(msg, ses.of(params.get("sessionId")))
                    reply = {"jsonrpc": "2.0", "id": msg["id"], **({"error": err} if err else {"result": res})}
                    ses.to_bridge((json.dumps(reply) + "\n").encode())
                    continue
                line = (json.dumps(msg) + "\n").encode()
        dst.write(line)
        dst.flush()


def stop_worker(env):
    """0.2.8's resident worker outlives the frontend; use its shutdown API."""
    path = env["JIUWENSWARM_RUNTIME_SOCKET"]
    if not os.path.exists(path):
        return
    code = ("import asyncio, os, sys\n"
            "from jiuwenswarm.server.supervisor.worker_process import request_resident_worker_shutdown\n"
            "async def stop():\n"
            "    await request_resident_worker_shutdown(sys.argv[1])\n"
            "    while os.path.exists(sys.argv[1]):\n"
            "        await asyncio.sleep(0.05)\n"
            "asyncio.run(asyncio.wait_for(stop(), 25))\n")
    try:
        subprocess.run([PY, "-c", code, path], env=env, stdin=subprocess.DEVNULL,
                       stdout=sys.stderr, stderr=sys.stderr, timeout=30, check=True)
    except (subprocess.SubprocessError, OSError) as e:
        say("runtime worker shutdown failed:", e)


def run(runtime_socket):
    os.makedirs(HOME, exist_ok=True)
    if not os.path.isfile(os.path.join(CONFIG, "config.yaml")):
        r = subprocess.run([PY, "-c", "from jiuwenswarm.common.utils import prepare_workspace\n"
                            "prepare_workspace(False, preferred_language='en')"], stdin=subprocess.DEVNULL,
                           stdout=sys.stderr, stderr=sys.stderr, cwd=HOME)
        if r.returncode:
            say("jiuwenswarm-init failed:", r.returncode)
            return 1
        memory_off()
    reasoning_level()
    ports = free_ports(4)
    write_env(ports)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               JIUWENSWARM_RUNTIME_SOCKET=runtime_socket)
    server = subprocess.Popen([PY, "-m", "jiuwenswarm.app"], stdin=subprocess.DEVNULL,
                              stdout=sys.stderr, stderr=sys.stderr, cwd=HOME, env=env)
    bridge = None

    def stop(*_):
        for p in (bridge, server):
            if p is not None and p.poll() is None:
                p.terminate()
        deadline = time.time() + 10
        for p in (bridge, server):
            while p is not None and p.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if p is not None and p.poll() is None:
                p.kill()

    signal.signal(signal.SIGTERM, lambda *a: (stop(), sys.exit(143)))
    try:
        t0 = time.time()
        while not (listening(ports[0]) and listening(ports[1])):
            if server.poll() is not None:
                say("JiuwenSwarm ended before it was ready:", server.returncode)
                return 1
            if time.time() - t0 > READY_SECONDS:
                say(f"JiuwenSwarm not ready in {READY_SECONDS} s")
                return 1
            time.sleep(0.25)
        say(f"ready in {time.time() - t0:.1f} s: agent server :{ports[0]}, gateway :{ports[1]}")
        bridge = subprocess.Popen([PY, "-m", "jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect",
                                   "--gateway-url", f"ws://127.0.0.1:{ports[1]}/acp"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, env=env, bufsize=0)
        ses = Sessions()
        ses.agent_in = bridge.stdin
        threading.Thread(target=to_agent, args=(sys.stdin.buffer, bridge.stdin, ses), daemon=True).start()
        out = threading.Thread(target=to_client, args=(bridge.stdout, sys.stdout.buffer, ses), daemon=True)
        out.start()
        code = bridge.wait()
        out.join(5)
        return code
    finally:
        stop()
        stop_worker(env)


def main():
    # A long job path exceeds the Unix socket limit. Each unit owns a short,
    # private directory, including on the local backend where /tmp is shared.
    with tempfile.TemporaryDirectory(prefix="nj-", dir="/tmp") as runtime:
        return run(os.path.join(runtime, "worker.sock"))


if __name__ == "__main__":
    sys.exit(main())
