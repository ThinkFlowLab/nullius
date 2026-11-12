"""Nullius as an ACP client: start an agent, give it a session, prompt it.

ACP (the Agent Client Protocol) is JSON-RPC 2.0 over the agent's stdin and
stdout. Nullius asks ``initialize``, then ``session/new`` with the unit's
workspace as ``cwd`` and the benchmark's MCP servers, then ``session/prompt``
once per turn. While a turn runs the agent streams ``session/update``
notifications - its message in chunks, its tool calls and their results, its
plan - which are kept, timed, as the turn's events: the trajectory is built
from them.

The agent may ask the client things too. Permission is refused unless the
client was made with ``grant=True``; a run does that, answering each request
once (``allow_once``), because no one is there to answer - the agent works in
a directory of its own, and isolation is the backend's business, not a
dialog's. And ``fs/read_text_file`` and ``fs/write_text_file`` are served,
but only inside the session's ``cwd``, and only to an agent running on this
machine (a run offers them to none in a container). No terminal is offered: an agent runs
commands with its own tools.

The client says it takes **notices** (``session.notices``): an adapter that
knows them sends its warnings - a configuration it questions, a model it has
no metadata for - as a ``notice`` update rather than as text in the agent's
message, where a benchmark would read it as part of the answer. An adapter
that does not know them sends text as before, and :func:`last_message` keeps
what it can of the answer apart.

A turn that outlives its timeout is cancelled (``session/cancel``) and, if the
agent does not stop, its process group is killed; :class:`Timeout` is raised
either way, and the events so far are kept on it.
"""
from __future__ import annotations

import json
import os
import queue
from concurrent.futures import CancelledError
from nullius.standard.cancellation import reply
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PROTOCOL_VERSION = 1


class AgentFailed(Exception):
    """The agent ended, or answered a request with an error."""


class Timeout(Exception):
    def __init__(self, msg, events):
        super().__init__(msg)
        self.events = events


@dataclass
class Turn:
    text: str
    stop: str
    events: List[dict] = field(default_factory=list)
    started: float = 0.0
    ended: float = 0.0


# What does not end a message: its own chunks, its thoughts, and the usage
# notes an agent sends while it streams.
_IN_MESSAGE = {"agent_message_chunk", "agent_thought_chunk", "usage_update"}
# Session metadata: a title, the commands on offer, the mode. Between two
# messages it parts them (Codex sends a notice as text, then its commands,
# then the answer); after the last message it ends nothing (Goose names the
# session once it has answered).
_METADATA = {"session_info_update", "available_commands_update", "current_mode_update"}


def last_message(events: List[dict]) -> str:
    """A turn's answer: the agent's last message, separated by ACP messageId
    or another event (a tool call, a plan, a notice). Without IDs, session
    metadata parts two messages only when another message follows it."""
    parts: List[str] = []
    parted = False
    message_id = None
    for e in events:
        u = e.get("update") or {}
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            chunk_id = u.get("messageId")
            new_message = chunk_id != message_id if chunk_id and message_id else parted
            if new_message:
                parts, message_id = [], None
            message_id = chunk_id or message_id
            parted = False
            parts.append(((u.get("content") or {}).get("text")) or "")
        elif kind in _METADATA:
            parted = True
        elif kind not in _IN_MESSAGE:
            parts, parted = [], False
            message_id = None
    return "".join(parts)


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


class Client:
    def __init__(self, cmd: List[str], env: dict, log: str, cwd: Optional[str] = None, grant: bool = False,
                 fs: bool = True, stopped=None):
        """``grant``: answer the agent's permission requests with yes (once
        each). Off unless the caller turns it on: a benchmark run does, since
        no one is there to answer, and records that it did. ``fs``: offer the
        agent this machine's files inside the session's ``cwd``; off when that
        ``cwd`` is a container's own (an own image), which this machine does
        not see, so the agent reads and writes there with its own tools."""
        self.cmd, self.env, self.log, self.cwd, self.grant, self.fs = cmd, env, log, cwd, grant, fs
        self.n = 0
        self.waiting: Dict[int, "queue.Queue"] = {}
        self.events: List[dict] = []
        self.roots: Dict[str, str] = {}          # session id -> its cwd, for fs requests
        self.lock = threading.Lock()
        self.stopped = stopped
        self.p: Optional[subprocess.Popen] = None
        self.ended = threading.Event()
        self.info: dict = {}

    def start(self) -> "Client":
        if self.stopped is not None and self.stopped.is_set():
            raise CancelledError("the run was stopped")
        self.err = open(self.log, "a")
        self.p = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.err, bufsize=0, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        self.info = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                                "clientCapabilities": {"fs": {"readTextFile": self.fs, "writeTextFile": self.fs},
                                                                       "terminal": False, "session": {"notices": {}}},
                                                "clientInfo": {"name": "nullius", "version": "1"}}, timeout=120)
        return self

    def _send(self, msg: dict):
        data = (json.dumps(msg) + "\n").encode()
        with self.lock:
            self.p.stdin.write(data)
            self.p.stdin.flush()

    def _read(self):
        for raw in self.p.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue                          # an agent's stray print is not protocol
            if "method" in m and "id" in m:
                threading.Thread(target=self._answer, args=(m,), daemon=True).start()
            elif "method" in m:
                if m["method"] == "session/update":
                    self.events.append({"t": time.time(), "update": (m.get("params") or {}).get("update") or {}})
            elif m.get("id") in self.waiting:
                self.waiting[m["id"]].put(m)
        self.ended.set()
        for q in list(self.waiting.values()):
            q.put(None)

    def tail(self) -> str:
        try:
            self.err.flush()
            with open(self.log, errors="replace") as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            return " | ".join(lines[-3:])[-400:] if lines else "nothing on stderr"
        except OSError:
            return "no log"

    def request(self, method: str, params: dict, timeout: float = 600):
        if self.stopped is not None and self.stopped.is_set():
            raise CancelledError("the run was stopped")
        self.n += 1
        i, q = self.n, queue.Queue()
        self.waiting[i] = q
        try:
            self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        except (BrokenPipeError, OSError):
            raise AgentFailed(f"{method}: the agent had already ended ({self.tail()})")
        try:
            m = reply(q, timeout, self.stopped)
        except queue.Empty:
            raise TimeoutError(method)
        finally:
            self.waiting.pop(i, None)
        if m is None:
            raise AgentFailed(f"{method}: the agent ended ({self.tail()})")
        if "error" in m:
            e = m["error"] or {}
            raise AgentFailed(f"{method}: {e.get('message')} {json.dumps(e.get('data'))[:300] if e.get('data') else ''}".strip())
        return m.get("result") or {}

    def _answer(self, m: dict):
        method, params = m["method"], m.get("params") or {}
        try:
            result = self._serve(method, params)
            reply = {"jsonrpc": "2.0", "id": m["id"], "result": result}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603 if not isinstance(e, KeyError) else -32601,
                                                              "message": str(e)[:300]}}
        try:
            self._send(reply)
        except OSError:
            pass

    def _serve(self, method: str, params: dict):
        if method == "session/request_permission":
            opts = params.get("options") or []
            pick = None
            if self.grant:
                pick = next((o for o in opts if o.get("kind") == "allow_once"), None) or \
                    next((o for o in opts if str(o.get("kind", "")).startswith("allow")), None)
            self.events.append({"t": time.time(), "update": {"sessionUpdate": "permission", "granted": bool(pick),
                                                             "toolCall": params.get("toolCall")}})
            if not pick:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}
        root = self.roots.get(params.get("sessionId"))
        if method in ("fs/read_text_file", "fs/write_text_file"):
            path = params.get("path") or ""
            if not self.fs:
                raise PermissionError("no files are served: the workspace is the container's own")
            if not root or not os.path.isabs(path) or not _inside(path, root):
                raise PermissionError(f"{path} is outside the session's workspace")
            if method == "fs/read_text_file":
                with open(path, errors="replace") as f:
                    lines = f.read().splitlines(keepends=True)
                start = max(0, (params.get("line") or 1) - 1)
                end = start + params["limit"] if params.get("limit") else None
                return {"content": "".join(lines[start:end])}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(params.get("content") or "")
            return None
        raise KeyError(f"{method} is not served")

    def new_session(self, cwd: str, mcp: Optional[List[dict]] = None) -> str:
        servers = [{"name": s["name"], "command": s["command"], "args": s.get("args") or [],
                    "env": [{"name": k, "value": v} for k, v in (s.get("env") or {}).items()]} for s in mcp or []]
        params = {"cwd": os.path.abspath(cwd), "mcpServers": servers}
        try:
            res = self.request("session/new", params, timeout=180)
        except AgentFailed as e:
            if "auth" not in str(e).lower() or not self.authenticate():
                raise
            res = self.request("session/new", params, timeout=180)
        sid = res["sessionId"]
        self.roots[sid] = os.path.abspath(cwd)
        return sid

    def authenticate(self) -> bool:
        """ACP's ``authenticate`` with the first method of type ``env_var``
        whose variables the agent's environment holds - the relay's address
        and placeholder key, never a real one. False when none is."""
        for m in self.info.get("authMethods") or []:
            names = [v.get("name") for v in m.get("vars") or []]
            if m.get("type") == "env_var" and names and all(self.env.get(n) for n in names):
                self.request("authenticate", {"methodId": m["id"]}, timeout=60)
                return True
        return False

    def prompt(self, sid: str, text: str, timeout: float = 600) -> Turn:
        start = len(self.events)
        t0 = time.time()
        self.events.append({"t": t0, "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}}})
        try:
            res = self.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}, timeout=timeout)
        except TimeoutError:
            try:
                self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
            except OSError:
                pass
            time.sleep(3)
            self.close()
            raise Timeout(f"the turn did not end in {int(timeout)} s", self.events[start:])
        evs = self.events[start:]
        return Turn(text=last_message(evs), stop=res.get("stopReason") or "", events=evs, started=t0, ended=time.time())

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
            except OSError:
                pass
            try:
                self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if self.p:
            try:                                 # the agent, and whatever it left running in its group
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self.p.wait(timeout=3)
                self.p.stdout.close()
            except Exception:
                pass
        try:
            self.err.close()
        except Exception:
            pass
