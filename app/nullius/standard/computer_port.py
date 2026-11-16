"""The computer port, ``agent.computer/v1``, as a desktop benchmark's runner serves it (``docs/ports/agent-computer-v1.md``).

A desktop benchmark's runner serves one MCP server, ``computer``, to the agent's session; its tools are the desktop's
basic operations, and the agent is a client alone. The benchmark's own loop runs as it is: it is handed a ``Bridge``,
which has the loop's ``reset`` and ``predict``. Standard library alone, so that a runner in the upstream's own Python
imports it with the package's folder on its path.

- ``Desktop``: the five operations an entry or a kit gives over its desktop.
- ``Bridge``: the loop's agent. Each ``predict`` hands the loop the actions of the agent's next acting call (one
  step), and serves the reading calls that come before it, in the thread of the loop: one desktop operation at a time,
  in the order the calls arrived. An acting call is held until the loop has run its actions and their pause, then
  answered. Once the steps are used up, every call but ``info`` is refused. The turn's end (``end_turn``) ends the
  wait for the agent's next call; each later ``predict`` gives the loop no action.
- ``Server``: the MCP server over a Unix socket: JSON-RPC 2.0, one message a line; every connection of the unit's
  session accepted, the task's state kept in the bridge, outside them; one line per tool call in the call log (the
  tool, its start and end, whether it failed; no argument and no result).
- ``Held``: content sent once for a run, when the runner is given a folder for it.
- What a runner needs around them: the ready file (``ready``), the lifeline (``lifeline``, which also carries the
  turn's end) and the one stop (``Stop``).

The agent's session reaches the socket through ``computer_pipe.py``, a stdio server that joins its stdin and stdout to
it.
"""
from __future__ import annotations

import base64
import binascii
import collections
import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from typing import Optional, Protocol

PROTOCOL = "2025-06-18"
LINE_BYTES = 24 * 1024 * 1024                        # one message either way, its newline counted
CHUNK_BYTES = 4 * 1024 * 1024                        # upload's data, a call
READ_BYTES = 12 * 1024 * 1024                        # the largest file read returns
TURN_END = b"turn-end"                               # the lifeline's one message: the agent's turn has ended
APPEND = ("import os; s = open({part!r}, 'rb'); d = open({path!r}, 'ab'); d.write(s.read()); d.close(); s.close(); "
          "os.remove({part!r}); print(os.path.getsize({path!r}))")


class Desktop(Protocol):
    """What an entry or a kit gives over its desktop. Each call is one desktop operation; the bridge makes them one at
    a time, from the thread of the benchmark's loop."""

    def python(self, code: str) -> Optional[dict]:
        """Python code run in the desktop as its user: the reply ``{status, output, error, returncode}``, or None when
        the desktop gave none in time."""

    def upload(self, path: str, data: bytes) -> None:
        """The file at ``path`` in the desktop made to hold ``data``."""

    def read(self, path: str) -> Optional[bytes]:
        """The file at ``path`` in the desktop, or None when there is no such file."""

    def capture(self) -> bytes:
        """The screen, as PNG."""

    def password(self) -> str:
        """The desktop user's password."""


POINT = {"x": {"type": "integer", "description": "Pixels from the screen's left edge"},
         "y": {"type": "integer", "description": "Pixels from the screen's top edge"}}
BUTTON = {"button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Default left"}}
TOOLS = [
    ("info", "The screen's size, the steps in all and left, the pause after each action in seconds, the seconds "
             "python waits for the desktop's reply, and the desktop user's password.", {}, []),
    ("screenshot", "The screen now, as a PNG image at its own size.", {}, []),
    ("read", "A file of the desktop: data is its content in base64, or null when there is no such file.",
     {"path": {"type": "string"}}, ["path"]),
    ("python", "Runs Python code in the desktop as its user and returns its reply: status, output, error, returncode. "
               "For reading the desktop and for your own programs there; pointer and keyboard input goes through the "
               "acting tools.", {"code": {"type": "string"}}, ["code"]),
    ("upload", "Writes data to a file of the desktop and returns the file's size so far. data is base64, at most 4 MiB "
               "a call; append false starts the file, true adds to it. sha256, optional, is the whole file's: when "
               "the run already holds a file of that sha256, data may be left out.",
     {"path": {"type": "string"}, "data": {"type": "string"}, "append": {"type": "boolean"},
      "sha256": {"type": "string"}}, ["path"]),
    ("click", "One step: a click at the point.",
     dict(POINT, **BUTTON, count={"type": "integer", "minimum": 1, "maximum": 3, "description": "Default 1"}),
     ["x", "y"]),
    ("move", "One step: the pointer moved to the point.", dict(POINT), ["x", "y"]),
    ("drag", "One step: a press at (x, y), a move, and a release at (to_x, to_y).",
     dict(POINT, to_x={"type": "integer"}, to_y={"type": "integer"}, **BUTTON), ["x", "y", "to_x", "to_y"]),
    ("scroll", "One step: the wheel turned at the point, dy clicks down (negative: up) and dx clicks right "
               "(negative: left).", dict(POINT, dy={"type": "integer"}, dx={"type": "integer"}), ["x", "y", "dy"]),
    ("type", "One step: the text typed key by key.", {"text": {"type": "string"}}, ["text"]),
    ("key", "One step: the keys pressed together, such as [\"ctrl\", \"s\"].",
     {"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]),
    ("wait", "One step: no action, the pause alone.", {}, []),
    ("step", "One step of several actions, run in order: each is a line of Python run in the desktop with pyautogui "
             "and time imported, or WAIT. An empty list is a step with no action. The screen comes back when "
             "screenshot is true.",
     {"actions": {"type": "array", "items": {"type": "string"}}, "response": {"type": "string"},
      "screenshot": {"type": "boolean"}}, ["actions"]),
    ("done", "The last step: the task is complete.", {"message": {"type": "string"}}, []),
    ("fail", "The last step: the task cannot be done.", {"reason": {"type": "string"}}, []),
]
NAMES = [t[0] for t in TOOLS]
SCREEN_TOOLS = ("click", "move", "drag", "scroll", "type", "key", "wait")   # answered with the screen after the pause
ACTING = SCREEN_TOOLS + ("step", "done", "fail")
FINAL = {"done": "DONE", "fail": "FAIL"}


def listed() -> list:
    return [{"name": n, "description": d,
             "inputSchema": dict({"type": "object", "properties": props}, **({"required": req} if req else {}))}
            for n, d, props, req in TOOLS]


class Refused(Exception):
    """A call answered with ``isError`` and these words."""


class Call:
    """One tools/call: its tool and arguments, when it came, and how it is answered (once)."""

    def __init__(self, tool, args, reply):
        self.tool, self.args, self._reply = tool, args, reply
        self.start = time.time()
        self.actions, self.response, self.screen = [], "", False
        self.answered = False
        self._lock = threading.Lock()

    def answer(self, value=None, image=None, error=None):
        with self._lock:
            if self.answered:
                return
            self.answered = True
        self._reply(self, value, image, error)


def _number(args, name, low, high, default=None):
    v = args.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise Refused(f"{name} is a number")
    v = int(round(v))
    if not low <= v <= high:
        raise Refused(f"{name} is from {low} to {high}")
    return v


def _text(args, name, default=None, empty=False):
    v = args.get(name, default)
    if not isinstance(v, str) or not (v or empty):
        raise Refused(f"{name} is a text" + ("" if empty else ", not empty"))
    return v


def checked(call, screen) -> None:
    """The call's arguments checked, and for an acting call the actions it stands for (Python lines run with pyautogui
    and time imported, or WAIT, DONE, FAIL), its response and whether its answer carries the screen. Refused when an
    argument is wrong: the call is then answered at once and is no step."""
    a, (w, h), tool = call.args, screen, call.tool
    if not isinstance(a, dict):
        raise Refused("arguments is an object")
    if tool in ("click", "move", "drag", "scroll"):
        x, y = _number(a, "x", 0, w - 1), _number(a, "y", 0, h - 1)
    button = a.get("button", "left")
    if tool in ("click", "drag") and button not in ("left", "right", "middle"):
        raise Refused("button is left, right or middle")
    if tool == "click":
        call.actions = [f"pyautogui.click({x}, {y}, clicks={_number(a, 'count', 1, 3, 1)}, button={button!r})"]
    elif tool == "move":
        call.actions = [f"pyautogui.moveTo({x}, {y})"]
    elif tool == "drag":
        to = (_number(a, "to_x", 0, w - 1), _number(a, "to_y", 0, h - 1))
        call.actions = [f"pyautogui.moveTo({x}, {y}); pyautogui.dragTo({to[0]}, {to[1]}, duration=0.5, "
                        f"button={button!r})"]
    elif tool == "scroll":
        dy, dx = _number(a, "dy", -100, 100), _number(a, "dx", -100, 100, 0)
        if not (dy or dx):
            raise Refused("dy or dx is not 0")
        call.actions = ["; ".join([f"pyautogui.scroll({-dy}, x={x}, y={y})"] * bool(dy)
                                  + [f"pyautogui.hscroll({dx}, x={x}, y={y})"] * bool(dx))]
    elif tool == "type":
        call.actions = [f"pyautogui.write({_text(a, 'text')!r})"]
    elif tool == "key":
        keys = a.get("keys")
        if not (isinstance(keys, list) and 1 <= len(keys) <= 8
                and all(isinstance(k, str) and 0 < len(k) <= 32 for k in keys)):
            raise Refused("keys is a list of 1 to 8 key names")
        call.actions = [f"pyautogui.hotkey({', '.join(repr(k if len(k) == 1 else k.lower()) for k in keys)})"]
    elif tool == "wait":
        call.actions = ["WAIT"]
    elif tool == "step":
        actions = a.get("actions")
        if not (isinstance(actions, list) and all(isinstance(x, str) and x.strip() for x in actions)):
            raise Refused("actions is a list of texts, each a line of Python or WAIT")
        if any(x.strip() in FINAL.values() for x in actions):
            raise Refused("the task's end is the tool done or fail, not an action of step")
        call.actions, call.response = list(actions), _text(a, "response", "", empty=True)
        call.screen = a.get("screenshot") is True
    elif tool in FINAL:
        call.actions, call.response = [FINAL[tool]], _text(a, "message" if tool == "done" else "reason", "", empty=True)
    elif tool in ("read", "upload"):
        _text(a, "path")
    elif tool == "python":
        _text(a, "code")
    if tool in SCREEN_TOOLS:
        call.screen = True
    if tool == "upload":
        if "data" in a:
            _text(a, "data", empty=True)
        if a.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256"))):
            raise Refused("sha256 is 64 lowercase hexadecimal digits")
        if not isinstance(a.get("append", False), bool):
            raise Refused("append is true or false")


class Held:
    """Content sent once for a run: a file whose chunks, sent in this unit from its start, hash to the sha256 the agent
    gave is kept in ``folder`` (the run's, the same for each of its units) under that name. A later upload of the same
    sha256, in any unit of the run, takes it from there, checked again, and the agent sends no bytes; the unit's
    desktop gets the file at that call, as it would have from the bytes. Nothing else passes from one unit to the
    next."""

    def __init__(self, folder):
        self.folder, self.open = folder, {}

    def get(self, digest):
        try:
            with open(os.path.join(self.folder, digest), "rb") as f:
                data = f.read()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def add(self, path, digest, data, append):
        if not append:
            self.open.pop(path, None)
            if digest:
                self.open[path] = (digest, hashlib.sha256(), bytearray())
        kept = self.open.get(path)
        if kept is None:
            return
        kept[1].update(data)
        kept[2].extend(data)
        if kept[1].hexdigest() == kept[0]:
            del self.open[path]
            part = os.path.join(self.folder, f".{kept[0]}.{os.getpid()}.{threading.get_ident()}")
            with open(part, "wb") as f:
                f.write(kept[2])
            os.replace(part, os.path.join(self.folder, kept[0]))


class Bridge:
    """The benchmark loop's agent, over a Desktop.

    ``steps`` is the loop's budget, ``pause`` the seconds it waits after each action, ``screen`` the screen's size in
    pixels, ``python_seconds`` how long the desktop waits for a reply to python. ``on_first`` is called at the loop's
    first ``predict``, before any call is served (a runner writes its ready file there). ``held`` is a Held, or None."""

    def __init__(self, desktop, steps, pause, screen, python_seconds, on_first=None, held=None):
        self.desktop, self.steps, self.pause = desktop, int(steps), pause
        self.screen, self.python_seconds = (int(screen[0]), int(screen[1])), python_seconds
        self.on_first, self.held = on_first, held
        self.password = desktop.password()
        self.used = 0                                # acting calls handed to the loop: its steps
        self.booked = 0                              # acting calls taken in, handed or waiting
        self.calls = collections.deque()             # calls waiting for the loop, in the order they came
        self.running = None                          # the acting call the loop is running
        self.ended = None                            # why no call is taken any more
        self.turn_ended = False
        self.first = True
        self.change = threading.Condition()

    # -- the loop's agent -------------------------------------------------------------------------------------------
    def reset(self, *_, **__):
        """The loop's reset: nothing. The agent's calls wait for the first predict."""

    def predict(self, instruction, obs):
        """The agent's next acting call, as the loop's (response, actions); reading calls are served until it comes.
        Once the turn has ended, no action."""
        if self.first:
            self.first = False
            if self.on_first:
                self.on_first()
        while True:
            call = self._next()
            if call is None:
                return "", []
            if call.tool not in ACTING:
                self._serve(call)
                continue
            if call.tool in FINAL:
                call.answer({"steps_left": self.steps - self.used, "done": True})
                return call.response, list(call.actions)
            return call.response, self._run(call)

    def _next(self):
        refused = []
        with self.change:
            while not self.calls and self.ended is None:
                self.change.wait(0.5)
            if not self.calls:
                return None
            call = self.calls.popleft()
            if call.tool in FINAL:
                self.used += 1
                refused = self._end(f"the task has ended ({call.tool})")
            elif call.tool in ACTING:
                self.used += 1
                self.running = call
        for c in refused:
            c.answer(error=self.ended)
        return call

    def _run(self, call):
        """The call's actions, one at a time as the loop asks; once the loop asks past the last, it has run them all
        and their pause, and the call is answered."""
        ran = False
        try:
            yield from call.actions
            ran = True
        finally:
            self._ran(call, ran)

    def _ran(self, call, ran):
        with self.change:
            self.running = None
            refused = self._end("the steps are used up") if self.used >= self.steps else []
            value = {"steps_left": self.steps - self.used, "done": self.ended is not None}
            turn_ended = self.turn_ended
        if not ran:
            call.answer(error="the step was not run to its end")
        else:
            try:
                call.answer(value, self.desktop.capture() if call.screen and not turn_ended else None)
            except Exception as e:                   # the screen could not be taken: the step itself was run
                call.answer(error=f"the step was run; the screen could not be taken: {type(e).__name__}: {e}")
        for c in refused:
            c.answer(error=self.ended)

    def _serve(self, call):
        try:
            call.answer(**self._reading(call))
        except Refused as e:
            call.answer(error=str(e))
        except Exception as e:                       # the desktop failed it: its words, and the agent may call again
            call.answer(error=f"the desktop failed {call.tool}: {type(e).__name__}: {e}")

    def _reading(self, call):
        a = call.args
        if call.tool == "screenshot":
            return {"image": self.desktop.capture()}
        if call.tool == "read":
            data = self.desktop.read(a["path"])
            if data is not None and len(data) > READ_BYTES:
                raise Refused(f"{a['path']} is over {READ_BYTES} bytes")
            return {"value": {"data": None if data is None else base64.b64encode(data).decode()}}
        if call.tool == "python":
            reply = self.desktop.python(a["code"])
            if reply is None:
                raise Refused(f"the desktop gave no reply within {self.python_seconds:g} s")
            return {"value": reply}
        return {"value": self._upload(a)}

    def _upload(self, a):
        path, append, digest = a["path"], a.get("append", False), a.get("sha256")
        if "data" not in a:
            data = self.held.get(digest) if self.held is not None and digest else None
            if data is None:
                raise Refused("upload needs data" + (": the run holds no file of that sha256" if digest else ""))
            self.desktop.upload(path, data)
            return {"size": len(data)}
        try:
            data = base64.b64decode(a["data"], validate=True)
        except (binascii.Error, ValueError):
            raise Refused("data is base64")
        if len(data) > CHUNK_BYTES:
            raise Refused(f"data is at most {CHUNK_BYTES} bytes a call")
        if not append:
            self.desktop.upload(path, data)
            size = len(data)
        else:                                        # the chunk beside the file, then added to it in the desktop
            part = path + ".part"
            self.desktop.upload(part, data)
            reply = self.desktop.python(APPEND.format(part=part, path=path))
            try:
                size = int(str(reply.get("output")).strip())
            except (AttributeError, ValueError):
                raise Refused(f"the desktop did not add the data to {path}: {json.dumps(reply)[:300]}")
        if self.held is not None:
            self.held.add(path, digest, data, append)
        return {"size": size}

    # -- what the server and the runner call ------------------------------------------------------------------------
    def submit(self, call):
        """A call that needs the desktop, for the loop's thread; refused at once once the task ended, and an acting
        call once every step is taken."""
        with self.change:
            refusal = self.ended
            if refusal is None and call.tool in ACTING:
                if self.booked >= self.steps:
                    refusal = "the steps are used up"
                else:
                    self.booked += 1
            if refusal is None:
                self.calls.append(call)
                self.change.notify_all()
        if refusal is not None:
            call.answer(error=refusal)

    def info(self):
        with self.change:
            left = self.steps - self.used
        return {"screen": list(self.screen), "steps": self.steps, "steps_left": left, "pause": self.pause,
                "python_seconds": self.python_seconds, "password": self.password}

    def instructions(self):
        w, h = self.screen
        return (f"A desktop seen in screenshots and driven by pointer and keyboard actions. The screen is {w} by {h} "
                f"pixels, counted from its top left corner. Each call of click, move, drag, scroll, type, key, wait, "
                f"step, done or fail is one step, answered once its actions have run and a pause of {self.pause:g} s "
                f"has passed; there are {self.steps} steps in all, and steps_left in each answer. Once they are used "
                f"up, every call but info is refused. Pointer and keyboard input goes through those tools, not through "
                f"python. A turn that ends with neither done nor fail ends the task as the desktop then is.")

    def end_turn(self):
        """The agent's turn has ended: the wait for its next call ends, and no call is taken any more."""
        with self.change:
            self.turn_ended = True
            refused = self._end("the agent's turn has ended")
        for c in refused:
            c.answer(error=self.ended)

    def finish(self):
        """The loop has returned, or failed: every call still waiting, and one the loop was running, is answered."""
        with self.change:
            refused = self._end("the task has ended")
            running, self.running = self.running, None
        for c in refused + ([running] if running is not None else []):
            c.answer(error=self.ended)

    def _end(self, why):
        """No call taken any more (with the condition held); the calls waiting, to refuse once it is let go."""
        if self.ended is None:
            self.ended = why
        refused = list(self.calls)
        self.calls.clear()
        self.change.notify_all()
        return refused


class Wire:
    """One connection of the agent's session: JSON-RPC messages, one a line, each at most LINE_BYTES with its newline.
    Once its input has ended, it is closed when every call it made has been answered."""

    def __init__(self, conn):
        self.conn, self.lock = conn, threading.Lock()
        self.open_calls, self.input_ended, self.closed = 0, False, False

    def lines(self):
        buf, scanned = bytearray(), 0
        while True:
            try:
                chunk = self.conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                i = buf.find(b"\n", scanned)
                if i < 0:
                    scanned = len(buf)
                    break
                line = bytes(buf[:i])
                del buf[:i + 1]
                scanned = 0
                yield line
            if len(buf) >= LINE_BYTES:
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32600, "message": f"a message is at most {LINE_BYTES} bytes"}})
                return

    def send(self, message):
        data = (json.dumps(message) + "\n").encode()
        if len(data) > LINE_BYTES:
            data = (json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "error": {
                "code": -32603, "message": f"the answer is over {LINE_BYTES} bytes"}}) + "\n").encode()
        with self.lock:
            if self.closed:
                return
            try:
                self.conn.sendall(data)
            except OSError:
                self._close()

    def opened(self):
        with self.lock:
            self.open_calls += 1

    def answered(self):
        with self.lock:
            self.open_calls -= 1
            if self.input_ended and not self.open_calls:
                self._close()

    def ended(self):
        with self.lock:
            self.input_ended = True
            if not self.open_calls:
                self._close()

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        if not self.closed:
            self.closed = True
            with contextlib.suppress(OSError):
                self.conn.shutdown(socket.SHUT_RDWR)
            self.conn.close()


class Server:
    """The MCP server ``computer`` at ``path``, a Unix socket (its folder the user's alone), over a Bridge. ``log``,
    when given, gets one JSON line per tool call: ``{tool, start, end, isError}``."""

    def __init__(self, bridge, path, log=None):
        self.bridge, self.path, self.log = bridge, path, log
        self.log_lock = threading.Lock()
        self.wires = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(16)
        threading.Thread(target=self._accept, name="computer-accept", daemon=True).start()

    def close(self):
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        for wire in list(self.wires):
            wire.close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def _accept(self):
        while True:
            try:
                conn = self.listener.accept()[0]
            except OSError:
                return
            wire = Wire(conn)
            self.wires.append(wire)
            threading.Thread(target=self._serve, args=(wire,), name="computer-connection", daemon=True).start()

    def _serve(self, wire):
        for line in wire.lines():
            try:
                message = json.loads(line)
            except ValueError:
                wire.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}})
                continue
            reply = self.handle(message, wire)
            if reply is not None:
                wire.send(reply)
        wire.ended()

    def handle(self, m, wire):
        """One message in; its reply out, None for a notification or a tool call answered on the wire later."""
        if not isinstance(m, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "a message is a JSON object"}}
        if "id" not in m:
            return None
        rid, method, params = m["id"], m.get("method"), m.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "computer", "version": "1"}, "instructions": self.bridge.instructions()}
        elif method == "tools/list":
            result = {"tools": listed()}
        elif method == "tools/call":
            self.call(rid, params, wire)
            return None
        elif method == "ping":
            result = {}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method!r}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def call(self, rid, params, wire):
        name = params.get("name")
        if name not in NAMES:
            self.record({"tool": None, "start": time.time(), "end": time.time(), "isError": True})
            wire.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {str(name)[:100]!r}"}})
            return

        def reply(call, value, image, error):
            content = []
            if error is not None:
                content.append({"type": "text", "text": str(error)})
            if value is not None:
                content.append({"type": "text", "text": json.dumps(value)})
            if image is not None:
                content.append({"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/png"})
            self.record({"tool": call.tool, "start": call.start, "end": time.time(), "isError": error is not None})
            wire.send({"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": error is not None}})
            wire.answered()

        wire.opened()
        call = Call(name, params.get("arguments") or {}, reply)
        if name == "info":
            call.answer(self.bridge.info())
            return
        try:
            checked(call, self.bridge.screen)
        except Refused as e:
            call.answer(error=str(e))
            return
        self.bridge.submit(call)

    def record(self, line):
        if self.log:
            with self.log_lock, open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")


# -- what a runner needs around them ----------------------------------------------------------------------------------
def ready(path, **fields):
    """The ready file, written whole, readable by this user alone: the driver reads it once, then removes it."""
    if not fields.get("ok"):                         # also in the log: the file is gone at the runner's exit
        sys.stderr.write(f"runner: not ready: {fields.get('error')}\n")
    part = path + ".part"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    os.replace(part, path)


def lifeline(fd, bridge=None):
    """The read end of a pipe from the driver. A line TURN_END ends the agent's turn (``bridge.end_turn``); any other
    line is ignored. Once the driver's end closes (the driver ended, was killed, or let the unit go), the runner stops
    as on SIGTERM. Run it in a thread of its own."""
    buf = b""
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if line.strip() == TURN_END and bridge is not None:
                    bridge.end_turn()
            buf = buf[-4096:]
    except OSError:
        pass
    os.kill(os.getpid(), signal.SIGTERM)


class Stop:
    """The runner's one stop: SIGTERM, or the lifeline's end, taken once; a second does not cut the clean-up. While a
    part that must not be cut runs (``deferred``), a stop waits for its end. ``seconds`` after the stop, a runner still
    there calls ``last`` (its last resort) and exits 1."""

    def __init__(self, seconds, last=None):
        self.seconds, self.last = seconds, last
        self.deferring, self.pending = 0, False
        self.taken = threading.Event()

    def install(self):
        """In the main thread, before anything the clean-up holds exists."""
        signal.signal(signal.SIGTERM, self.take)
        threading.Thread(target=self._last_resort, name="last-resort", daemon=True).start()

    def take(self, *_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.taken.set()
        if self.deferring:
            self.pending = True
            return
        sys.exit(1)

    @contextlib.contextmanager
    def deferred(self, then_stop=True):
        """A part no stop cuts. A stop that came meanwhile stops the runner at the part's end (``then_stop``), or is
        dropped when the runner is ending anyway."""
        self.deferring += 1
        try:
            yield
        finally:
            self.deferring -= 1
            if not self.deferring and self.pending:
                self.pending = False
                if then_stop:
                    sys.exit(1)

    def _last_resort(self):
        self.taken.wait()
        time.sleep(self.seconds)
        if self.last is not None:
            with contextlib.suppress(Exception):
                self.last()
        sys.stderr.write(f"runner: the clean-up did not end within {self.seconds:g} s of the stop\n")
        os._exit(1)
