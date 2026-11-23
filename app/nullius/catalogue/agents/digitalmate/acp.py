"""ACP transport for DigitalMate's fixed version: one isolated product instance per session.

No history, tools or model calls live here. Each session's ``run.py --session`` binds the session's
working directory as Hive's project and hands its stdio MCP servers to the product; Hive keeps the
conversation and this returns only its committed answers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import uuid

from run import stop


def mcp_servers(rows) -> list[dict]:
    """ACP stdio servers as the product's session MCP rows; any other transport is refused."""
    servers = []
    for row in rows or []:
        if not isinstance(row, dict) or set(row) - {"name", "command", "args", "env"} \
                or not isinstance(row.get("name"), str) or not isinstance(row.get("command"), str):
            raise ValueError("Only stdio MCP server configuration is supported")
        environment = {}
        for item in row.get("env", []):
            if (not isinstance(item, dict) or set(item) != {"name", "value"}
                    or not isinstance(item["name"], str) or not isinstance(item["value"], str)
                    or item["name"] in environment):
                raise ValueError("Invalid MCP server environment")
            environment[item["name"]] = item["value"]
        servers.append({"name": row["name"], "command": row["command"],
                        "args": list(row.get("args", [])), "env": environment})
    return servers


class Session:
    def __init__(self, install: Path, cwd: str, *, mcp_servers_rows=None):
        root = Path(cwd)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Session cwd must be an existing absolute directory")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.close_lock = threading.Lock()
        self.config_home = None
        extra = []
        servers = mcp_servers(mcp_servers_rows)
        if servers:
            # Outside the workspace, owner-only: the rows may carry the servers' own credentials.
            self.config_home = tempfile.TemporaryDirectory(prefix="digitalmate-mcp-", dir=Path.home())
            config = Path(self.config_home.name) / "servers.json"
            with open(config, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
                json.dump({"cwd": str(root), "servers": servers}, handle)
            extra = ["--mcp-config", str(config)]
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("run.py")), "--install", str(install),
                 "--session", "--port", str(port), "--project", str(root), *extra],
                cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                text=True, bufsize=1, start_new_session=True)
        except BaseException:
            if self.config_home is not None:
                self.config_home.cleanup()
            raise

    def ready(self):
        try:
            if self.read() != {"ready": True}:
                raise RuntimeError("DigitalMate session did not become ready")
        except BaseException:
            self.close()
            raise

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("DigitalMate session ended; inspect agent stderr")
        return json.loads(line)

    def prompt(self, text: str) -> str:
        if not self.lock.acquire(blocking=False):
            raise ValueError("A turn is already running in this session")
        try:
            if self.cancelled.is_set() or self.process.poll() is not None:
                raise RuntimeError("Session ended; create a new session")
            self.process.stdin.write(json.dumps({"prompt": text}) + "\n")
            self.process.stdin.flush()
            answer = self.read().get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("DigitalMate returned no final answer")
            return answer
        finally:
            self.lock.release()

    def close(self):
        # SIGTERM reaches run.py's cleanup, which stops the owned product.
        with self.close_lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            stop(self.process)
            self.process.stdin.close()
            self.process.stdout.close()
            if self.config_home is not None:
                self.config_home.cleanup()


class Server:
    def __init__(self, install: Path, output=None, factory=Session):
        self.install, self.factory = install, factory
        self.output = output or sys.stdout
        self.output_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sessions = {}
        self.closed = False

    def send(self, frame):
        with self.output_lock:
            self.output.write(json.dumps({"jsonrpc": "2.0", **frame}) + "\n")
            self.output.flush()

    def handle(self, request):
        identity = request.get("id")
        method, params = request.get("method"), request.get("params") or {}
        try:
            if method == "initialize":
                result = {"protocolVersion": 1, "agentInfo": {"name": "digitalmate"},
                          "agentCapabilities": {"loadSession": False, "promptCapabilities": {},
                                                "mcpCapabilities": {"http": False, "sse": False}},
                          "authMethods": []}
            elif method == "session/new":
                session = self.factory(self.install, params.get("cwd", ""),
                                       mcp_servers_rows=params.get("mcpServers"))
                sid = uuid.uuid4().hex
                with self.state_lock:
                    if self.closed:
                        session.close()
                        raise RuntimeError("Adapter is closing")
                    self.sessions[sid] = session
                try:
                    session.ready()
                except BaseException:
                    with self.state_lock:
                        self.sessions.pop(sid, None)
                    session.close()
                    raise
                result = {"sessionId": sid}
            elif method in ("session/prompt", "session/cancel"):
                sid = params.get("sessionId")
                with self.state_lock:
                    session = self.sessions.get(sid)
                if session is None:
                    raise ValueError("Unknown session")
                if method == "session/cancel":
                    session.close()
                    return
                blocks = params.get("prompt")
                if not isinstance(blocks, list) or not blocks or any(
                        not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str)
                        for b in blocks):
                    raise ValueError("Only text prompts are supported")
                try:
                    answer = session.prompt("".join(b["text"] for b in blocks))
                except Exception:
                    if not session.cancelled.is_set():
                        raise
                    result = {"stopReason": "cancelled"}
                else:
                    if session.cancelled.is_set():
                        result = {"stopReason": "cancelled"}
                    else:
                        self.send({"method": "session/update", "params": {"sessionId": sid,
                                   "update": {"sessionUpdate": "agent_message_chunk",
                                              "content": {"type": "text", "text": answer}}}})
                        result = {"stopReason": "end_turn"}
            else:
                if identity is not None:
                    self.send({"id": identity, "error": {"code": -32601, "message": "Method not supported"}})
                return
            if identity is not None:
                self.send({"id": identity, "result": result})
        except Exception as error:
            if identity is not None:
                self.send({"id": identity, "error": {"code": -32602 if isinstance(error, ValueError) else -32603,
                                                   "message": str(error)}})

    def close(self):
        with self.state_lock:
            self.closed = True
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, required=True)
    args = parser.parse_args()
    server = Server(args.install.resolve())
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    workers = []
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be an object")
            except ValueError:
                server.send({"id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC request"}})
                continue
            worker = threading.Thread(target=server.handle, args=(request,), daemon=True)
            workers = [item for item in workers if item.is_alive()]
            workers.append(worker)
            worker.start()
    finally:
        server.close()
        for worker in workers:
            worker.join(timeout=1)


if __name__ == "__main__":
    main()
