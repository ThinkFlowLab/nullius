"""A benchmark's MCP server run on the machine, in the driver's environment.

A benchmark whose tools are its upstream's own code - with the upstream's
dependencies - cannot have that server started inside the agent's
container: the container carries the agent and nothing of the benchmark's.
So a benchmark may declare ``mcp_servers`` (``definitions._mcp_servers``),
each a command of its own (``{here}``, ``{install}``: the driver's
environment, ``driver.install``), and a unit's ``setup`` ask for one in its
``mcp`` entry by name:

    {"name": "airline", "server": "domain", "args": [...], "env": {...}}

The agent's session is still given a stdio MCP server - every agent that
proved ``agent.tools/v1`` takes one - and that server is the benchmark's:

- **local backend**: the entry is the server's own command, rendered
  (:func:`local_entry`): the agent starts it, on this machine, from the
  driver's environment, as it starts any stdio server;
- **container backend**: the run starts a :class:`Listener` per server per unit
  on this machine - a Unix socket in a private folder; each connection to it
  is one start of the server, its stdin and stdout the connection - and the
  unit gets one door to it (``container.Unit``'s ``doors``: ``forward.py``
  on the unit's internal network only, the socket alone mounted into it,
  passing every connection to that socket and nowhere else). The agent's
  entry is ``mcp_bridge.py``, a file of the standard library mounted alone
  into its container, which joins its stdin and stdout to the door
  (:func:`bridge_entry`). Nothing of the server, its environment or its
  data enters the agent's container, and the door reaches nothing else.

Either way a server started twice in a unit is two processes, as a stdio
server is; what it logs, it logs on this machine (the trial's
``verifier/``), out of the agent's reach in a container.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import List, Optional, Tuple

from nullius.standard import install

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_bridge.py")
DOOR_PORT = 9000          # where a door listens, on the unit's network
_ALIAS = re.compile(r"[^a-z0-9-]+")


class Unknown(ValueError):
    """A unit's setup named a server the benchmark does not declare."""


def wanted(setup: dict) -> List[dict]:
    """The setup's ``mcp`` entries that name a server of the benchmark's."""
    return [s for s in setup.get("mcp") or [] if isinstance(s, dict) and s.get("server") is not None]


def argv(bench: dict, entry: dict, inst: Optional[str]) -> List[str]:
    """The server's command, its slots filled, and the entry's ``args``."""
    servers = bench.get("mcp_servers") or {}
    sv = servers.get(entry.get("server"))
    if not isinstance(sv, dict):
        raise Unknown(f"mcp: {entry.get('server')!r} is not one of the benchmark's mcp_servers "
                      f"({', '.join(servers) or 'none declared'})")
    args = entry.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Unknown(f"mcp {entry.get('name')}: args, a list of words")
    return install.words(sv["command"], {"here": bench["_here"], "install": inst or ""}) + list(args)


def server_env(base: dict, entry: dict) -> dict:
    """The server's environment: the driver's (``base``: no secret, the
    upstream and dataset named) and the entry's own ``env``."""
    own = entry.get("env") or {}
    if not isinstance(own, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in own.items()):
        raise Unknown(f"mcp {entry.get('name')}: env, names to strings")
    return dict(base, **own)


def local_entry(bench: dict, entry: dict, inst: Optional[str], base: dict) -> dict:
    """The agent's stdio entry on the local backend: the server's own command."""
    cmd = argv(bench, entry, inst)
    return {"name": str(entry.get("name") or entry["server"]), "command": cmd[0], "args": cmd[1:],
            "env": server_env(base, entry)}


def alias(name: str, i: int) -> str:
    """The name a door answers by on the unit's network."""
    return f"mcp-{i}-" + (_ALIAS.sub("-", name.lower()).strip("-")[:30] or "server")


def bridge_entry(name: str, door: str) -> dict:
    """The agent's stdio entry in a container: the bridge, told its door."""
    return {"name": name, "command": "python3", "args": [BRIDGE], "env": {"NULLIUS_MCP_AT": door}}


class Listener:
    """A server, started once per connection to a Unix socket: the
    connection is its stdin and stdout, its stderr the log. The socket sits
    in a folder of its own (0700), short enough for a socket's path."""

    def __init__(self, cmd: List[str], env: dict, cwd: str, log: str):
        self.cmd, self.env, self.cwd, self.log = cmd, env, cwd, log
        self.dir = tempfile.mkdtemp(prefix="nullius-mcp-")
        self.path = os.path.join(self.dir, "s")
        self.procs: List[subprocess.Popen] = []
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()
        self.closed = False

    def start(self) -> "Listener":
        os.chmod(self.dir, 0o700)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.lock:
                    if self.closed:
                        conn.close()
                        return
                    with open(self.log, "a") as err:
                        self.procs.append(subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=conn.fileno(),
                                                           stdout=conn.fileno(), stderr=err, start_new_session=True))
            except OSError as e:
                with open(self.log, "a") as err:
                    err.write(f"[nullius] could not start the server: {e}\n")
            finally:
                conn.close()                      # the server holds its own copy

    def stop(self):
        with self.lock:
            self.closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def doors(bench: dict, setup: dict, inst: Optional[str], base: dict, cwd: str, log_dir: str
          ) -> Tuple[List[dict], List[Tuple[str, int, str]], List[Listener]]:
    """For the container backend: the setup's ``mcp`` with each machine server's
    entry replaced by the bridge's, the doors the unit opens (alias, port,
    socket) and the listeners started - stopped by the caller, also on an
    error. Raises :class:`Unknown` for a server not declared."""
    out, opened, started = [], [], []
    os.makedirs(log_dir, exist_ok=True)
    try:
        for i, s in enumerate(setup.get("mcp") or [], 1):
            if not (isinstance(s, dict) and s.get("server") is not None):
                out.append(s)
                continue
            cmd, env = argv(bench, s, inst), server_env(base, s)
            name = str(s.get("name") or s["server"])
            ln = Listener(cmd, env, cwd, os.path.join(log_dir, f"mcp-{i}.log")).start()
            started.append(ln)
            a = alias(name, i)
            opened.append((a, DOOR_PORT, ln.path))
            out.append(bridge_entry(name, f"{a}:{DOOR_PORT}"))
    except Exception:
        for ln in started:
            ln.stop()
        raise
    return out, opened, started


def entries(bench: dict, setup: dict, inst: Optional[str], base: dict) -> List[dict]:
    """For the local backend: the setup's ``mcp``, each machine server's entry
    its own command. Raises :class:`Unknown` for a server not declared."""
    return [local_entry(bench, s, inst, base) if isinstance(s, dict) and s.get("server") is not None else s
            for s in setup.get("mcp") or []]

