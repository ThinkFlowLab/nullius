"""The computer port, ``agent.computer/v1``, as a desktop benchmark's runner serves it (``docs/ports/agent-computer-v1.md``).

A desktop benchmark's runner serves one MCP server, ``computer``, to the agent's session; its tools are the desktop's
basic operations, and the agent is a client alone. The benchmark's own loop runs as it is: it is handed a ``Bridge``,
which has the loop's ``reset`` and ``predict``. Standard library alone, so that a runner in the upstream's own Python
imports it with the package's folder on its path.

Once the agent is given its task, the desktop is reached through acting calls alone: ``screenshot`` returns the
observation the last step fixed (``Bridge.seen``), taken from the benchmark's loop or, for a loop that hands none, as
a picture when the step's last pause has passed; a ``step`` names the files of the agent's folder it wants back, read
once its actions and pauses have run. Before the task, in a window that the benchmark's side closes when the run gives
it (``Bridge.close_window``), ``upload`` writes inside that folder and ``prepare`` runs actions there and reads files
back, neither a step; an acting call is refused until the task is given.

- ``Desktop``: the five operations an entry or a kit gives over its desktop.
- The tools: one table (``tools``) gives both what ``tools/list`` says and what ``checked`` accepts. A call is checked
  in full, its upload's data decoded, before it is queued; a call refused there takes no step and never reaches the
  desktop.
- ``Bridge``: the loop's agent. Each ``predict`` hands the loop the actions of the agent's next acting call (one
  step), and serves the reading calls that come before it, in the thread of the loop: one desktop operation at a time,
  in the order the calls arrived. An acting call is held until the loop has run its actions and their pause, then
  answered; a ``step`` with an ``end`` then hands the loop its DONE or FAIL, in the same step. Once the steps are used
  up, every call but ``info`` is refused. The turn's end (``end_turn``) ends the
  wait for the agent's next call and the actions of a step not yet run; a ``done`` or ``fail`` already taken still
  reaches the loop, and each later ``predict`` gives the loop no action.
- ``Server``: the MCP server over a Unix socket: JSON-RPC 2.0, one message a line; every connection of the unit's
  session accepted, the task's state kept in the bridge, outside them; each connection's answers written by a thread
  of its own, at most OUT_BYTES waiting, so that a client that stops reading holds up nothing else; one line per tool
  call in the call log (the tool, its start and end, whether it failed; no argument and no result).
- ``Held``: whole files kept by their sha256 for a run, when the runner is given a folder for them.
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
import functools
import hashlib
import json
import os
import posixpath
import re
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from typing import Optional, Protocol

PROTOCOL = "2025-06-18"
LINE_BYTES = 24 * 1024 * 1024                        # one message either way, its newline counted
OUT_BYTES = 2 * LINE_BYTES                           # answers waiting for a connection's client to read them
CHUNK_BYTES = 4 * 1024 * 1024                        # upload's data, a call
READ_BYTES = 12 * 1024 * 1024                        # the largest file read returns
KEEP_BYTES = 256 * 1024 * 1024                       # the largest file kept by its sha256
LAST_SECONDS = 0.5                                   # what the last resort gives its own callback
TURN_END = b"turn-end"                               # the lifeline's one message: the agent's turn has ended
APPEND = ("import os\ntry:\n    s = open({part!r}, 'rb'); d = open({path!r}, 'ab'); d.write(s.read()); d.close(); "
          "s.close()\nfinally:\n    os.remove({part!r})\nprint(os.path.getsize({path!r}))")
MAKE_FOLDER = ("import os, shutil\np = {folder!r}\nif os.path.islink(p) or (os.path.lexists(p) and not os.path.isdir(p)):\n"
               "    os.remove(p)\nshutil.rmtree(p, ignore_errors=True)\nos.makedirs(p, 0o700)\nprint('made')")


class Desktop(Protocol):
    """What an entry or a kit gives over its desktop. Each call is one desktop operation; the bridge makes them one at
    a time, from the thread of the benchmark's loop."""

    def python(self, code: str) -> Optional[dict]:
        """Python code run in the desktop as its user (a prepare's actions, the agent's folder made, an upload's
        chunk added): the reply ``{status, output, error, returncode}``, or None when the desktop gave none in time."""

    def upload(self, path: str, data: bytes) -> None:
        """The file at ``path`` in the desktop made to hold ``data``."""

    def read(self, path: str) -> Optional[bytes]:
        """The file at ``path`` in the desktop, or None when there is no such file."""

    def capture(self) -> bytes:
        """The screen, as PNG."""

    def password(self) -> str:
        """The desktop user's password."""

    # The bridge also asks, when the desktop has it: observation() -> bytes, the screenshot the benchmark's own loop
    # took when the step it just ran ended (for a loop that keeps one). Without it, the bridge takes a picture by
    # capture().


# -- the tools: one table for what tools/list says and what a call may hold ------------------------------------------
NAMES = ["info", "screenshot", "upload", "prepare", "click", "move", "drag", "scroll", "type", "key", "wait", "step",
         "done", "fail"]
WINDOW = ("upload", "prepare")                       # the tools of the window before the task
FILES_ROOM = LINE_BYTES - 64 * 1024                  # of an answer, what its files and image may take
TASK_GIVEN = b"task-given"                           # the lifeline's message: the agent is being given its task
PREPARE = "import pyautogui\nimport time\npyautogui.FAILSAFE = False\n{code}"   # a prepare's action, as a step's is
SCREEN_TOOLS = ("click", "move", "drag", "scroll", "type", "key", "wait")   # answered with the screen after the pause
ACTING = SCREEN_TOOLS + ("step", "done", "fail")
FINAL = {"done": "DONE", "fail": "FAIL"}
# The patterns use explicit classes alone, which Python's re and the regular expressions of JSON Schema read alike;
# a final $ is the text's end (Python's \Z)
BLANK = "[\\t\\n\\v\\f\\r ]"
NOT_BLANK = "[^\\t\\n\\v\\f\\r ]"
BASE64 = "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
SHA256 = "^[0-9a-f]{64}$"
B64_CHARS = 4 * -(-CHUNK_BYTES // 3)                 # the longest base64 of CHUNK_BYTES; at that length, its padding
B64_LAST = "=" * (3 - CHUNK_BYTES % 3) + "$" if CHUNK_BYTES % 3 else None   # says the last group is not too full
REFUSALS = {("scroll", "arguments", "anyOf"): "dy or dx is not 0",
            ("upload", "arguments", "anyOf"): "upload needs data, or a sha256",
            ("upload", "arguments", "if"): "append needs data: a file taken by its sha256 starts the file",
            ("upload", "data", "anyOf"): f"data is at most {CHUNK_BYTES} bytes a call"}
PATTERN_WORDS = {NOT_BLANK: "is not blank", SHA256: "is 64 lowercase hexadecimal digits", BASE64: "is base64"}


FILES = {"type": "array",
         "items": {"type": "string", "minLength": 1, "description": "A path inside folder"}}


@functools.lru_cache(maxsize=8)
def tools(screen):
    """The tools for a screen of ``screen`` (width, height) pixels: [(name, description, input schema)]."""
    w, h = screen
    x = {"type": "integer", "minimum": 0, "maximum": w - 1,
         "description": "Pixels of the screenshot at the screen's size, from its left edge"}
    y = {"type": "integer", "minimum": 0, "maximum": h - 1,
         "description": "Pixels of the screenshot at the screen's size, from its top edge"}
    button = {"type": "string", "enum": ["left", "right", "middle"], "description": "Default left"}
    clicks = {"type": "integer", "minimum": -100, "maximum": 100}
    text = {"type": "string", "minLength": 1}
    obj = lambda props, required=(), **more: dict({"type": "object", "properties": props, "additionalProperties": False},
                                                   **({"required": list(required)} if required else {}), **more)
    data = {"type": "string", "contentEncoding": "base64", "pattern": BASE64, "maxLength": B64_CHARS}
    if B64_LAST:
        data["anyOf"] = [{"maxLength": B64_CHARS - 4}, {"pattern": B64_LAST}]
    actions = {"type": "array", "items": {
        "type": "string", "pattern": NOT_BLANK,
        "not": {"pattern": f"^{BLANK}*(DONE|FAIL){BLANK}*$",
                "description": "the task's end is the tool done or fail, or the argument end, not an action of step"}}}
    return [
        ("info", "The screen's size, the steps in all and left, the pause after each action in seconds, the desktop "
                 "user's password, and folder: a folder of the desktop that is yours, empty at the start, for upload "
                 "and for the files a step returns.", obj({})),
        ("screenshot", "The observation of the last step, as a PNG image: the screen as the benchmark took it when "
                       "that step ended, or at the start before the first step. It takes no new picture; wait is the "
                       "way to look again. Look at it before the first pointing action.", obj({})),
        ("upload", "Before the task alone, in the window between the session's start and the task: writes data to a "
                   "file inside folder (see info) and returns the file's size so far. Not a step. data is base64, at "
                   "most 4 MiB a call; append false starts the file, true adds to it. sha256, optional, is the whole "
                   "file's: when the run already holds a file of that sha256, it may be given with no data, and it "
                   "starts the file.",
         obj({"path": text, "data": data,
              "append": {"type": "boolean"}, "sha256": {"type": "string", "pattern": SHA256}}, ["path"],
             anyOf=[{"required": ["data"]}, {"required": ["sha256"]}],
             **{"if": {"not": {"required": ["data"]}}, "then": {"properties": {"append": {"const": False}}}})),
        ("prepare", "Before the task alone, in the window between the session's start and the task: runs actions in "
                    "the desktop, each a line of Python with pyautogui and time imported as a step's are, with no "
                    "pause after them, then reads files, any number of paths inside folder. Returns {files}: each in "
                    "base64, null when there is no such file. Not a step.",
         {"type": "object", "additionalProperties": False, "required": ["actions"],
          "properties": {"actions": actions, "files": FILES}}),
        ("click", "One step: a click at the point. Returns the screen as a PNG image after the pause.",
         obj({"x": x, "y": y, "button": button,
              "count": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Default 1"}}, ["x", "y"])),
        ("move", "One step: the pointer moved to the point. Returns the screen as a PNG image after the pause.",
         obj({"x": x, "y": y}, ["x", "y"])),
        ("drag", "One step: a press at (x, y), a move, and a release at (to_x, to_y). "
                 "Returns the screen as a PNG image after the pause.",
         obj({"x": x, "y": y, "to_x": x, "to_y": y, "button": button}, ["x", "y", "to_x", "to_y"])),
        ("scroll", "One step: the wheel turned at the point, dy clicks down (negative: up) and dx clicks right "
                   "(negative: left); at least one is not 0. Returns the screen as a PNG image after the pause.",
         obj({"x": x, "y": y, "dy": clicks, "dx": clicks}, ["x", "y"],
             anyOf=[{"required": ["dy"], "properties": {"dy": {"not": {"const": 0}}}},
                    {"required": ["dx"], "properties": {"dx": {"not": {"const": 0}}}}])),
        ("type", "One step: the text typed key by key. Returns the screen as a PNG image after the pause.",
         obj({"text": text}, ["text"])),
        ("key", "One step: the keys pressed together, such as [\"ctrl\", \"s\"]. "
                "Returns the screen as a PNG image after the pause.",
         obj({"keys": {"type": "array", "minItems": 1, "maxItems": 8,
                       "items": {"type": "string", "minLength": 1, "maxLength": 32}}}, ["keys"])),
        ("wait", "One step: no action, the pause alone. Returns the screen as a PNG image after the pause.", obj({})),
        ("step", "One step of several actions, run in order: each is a line of Python run in the desktop with "
                 "pyautogui and time imported, or WAIT. An empty list is a step with no action. With end, done or "
                 "fail, the actions run and then the task ends, in this one step. files, any number of paths inside "
                 "folder, are read once the actions and their pauses have run: the answer's files holds each in "
                 "base64, null when there is no such file. Returns the step's observation as a PNG image when "
                 "screenshot is true.",
         obj({"actions": actions, "response": {"type": "string"}, "screenshot": {"type": "boolean"},
              "end": {"type": "string", "enum": ["done", "fail"],
                      "description": "Once the actions have run: done, the task is complete; fail, it cannot be done"},
              "files": FILES},
             ["actions"])),
        ("done", "The last step: the task is complete. Returns {steps_left, done}, with no image.",
         obj({"message": {"type": "string"}})),
        ("fail", "The last step: the task cannot be done. Returns {steps_left, done}, with no image.",
         obj({"reason": {"type": "string"}})),
    ]


def listed(screen) -> list:
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s in tools(tuple(screen))]


def _framed(named, got):
    """The bytes one file takes in an answer: its entry in the files object, framed once more as the JSON text of the
    answer's content (each quote and backslash escaped)."""
    text = json.dumps({named: got})
    return len(text) + text.count('"') + text.count("\\")


def inside(folder, path, name):
    """``path``, a path of the desktop inside ``folder`` (absolute, or relative to the folder), as an absolute path;
    refused when it is not one, or not written in its plain form."""
    full = path if path.startswith("/") else posixpath.join(folder, path)
    if "\x00" in full or posixpath.normpath(full) != full or not full.startswith(folder.rstrip("/") + "/"):
        raise Refused(f"{name} is a path inside {folder}, with no . or .. part")
    return full


class Refused(Exception):
    """A call answered with ``isError`` and these words."""


def _passes(value, schema, tool, name):
    try:
        _valid(value, schema, tool, name)
        return True
    except Refused:
        return False


def _python_pattern(pattern):
    """A pattern of the table as Python's re reads it alike: a final $ is the text's end, never before a newline."""
    return pattern[:-1] + "\\Z" if pattern.endswith("$") and not pattern.endswith("\\$") else pattern


def _no_constant(name):
    """json.loads's parse_constant: NaN, Infinity and -Infinity are no JSON."""
    raise ValueError(f"{name} is not JSON")


def _valid(value, schema, tool, name):
    """``value`` checked against ``schema``, in the part of JSON Schema the tool table uses; the value with integers
    as int. Refused, naming the argument, when it does not hold."""
    kind = schema.get("type")
    if kind == "integer":
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
            raise Refused(f"{name} is an integer")
        value = int(value)
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise Refused(f"{name} is from {schema['minimum']} to {schema['maximum']}")
    elif kind is not None and not isinstance(value, {"string": str, "boolean": bool, "array": list, "object": dict}[kind]):
        raise Refused(f"{name} is " + {"string": "a text", "boolean": "true or false", "array": "a list",
                                       "object": "an object"}[kind])
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise Refused(f"{name} is a text, not empty")
        if len(value) > schema.get("maxLength", len(value)):
            raise Refused(f"{name} is at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(_python_pattern(schema["pattern"]), value):
            raise Refused(f"{name} " + PATTERN_WORDS.get(schema["pattern"], "is not of its form"))
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise Refused(f"{name} holds {schema.get('minItems', 0)} to {schema['maxItems']} items")
        value = [_valid(v, schema.get("items", {}), tool, f"{name}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise Refused(f"{key} is missing")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    raise Refused(f"{key} is no argument of {tool}")
        value = {k: _valid(v, props[k], tool, k) if k in props else v for k, v in value.items()}
    if "anyOf" in schema and not any(_passes(value, s, tool, name) for s in schema["anyOf"]):
        raise Refused(REFUSALS.get((tool, name, "anyOf"), f"{name} is not of its form"))
    if "if" in schema and _passes(value, schema["if"], tool, name) and not _passes(value, schema["then"], tool, name):
        raise Refused(REFUSALS.get((tool, name, "if"), f"{name} is not of its form"))
    if "enum" in schema and value not in schema["enum"]:
        raise Refused(f"{name} is one of {', '.join(map(str, schema['enum']))}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise Refused(f"{name} is {json.dumps(schema['const'])}")
    if "not" in schema and _passes(value, schema["not"], tool, name):
        raise Refused(f"{name}: {schema['not'].get('description', 'not of its form')}")
    return value


class Call:
    """One tools/call: its tool and arguments, when it came, and how it is answered (once). ``checked`` adds what
    the call stands for: its arguments as checked, its actions, its response, whether its answer has the screen, a
    step's end (``done``, ``fail`` or None), and an upload's data, decoded."""

    def __init__(self, tool, args, reply):
        self.tool, self.args, self._reply = tool, args, reply
        self.start = time.time()
        self.clean, self.actions, self.response, self.screen, self.data = {}, [], "", False, None
        self.end = None
        self.files = None                            # a step's or a prepare's files, [(as named, the desktop's path)]
        self.answered = False
        self._lock = threading.Lock()

    def answer(self, value=None, image=None, error=None):
        with self._lock:
            if self.answered:
                return
            self.answered = True
        self._reply(self, value, image, error)


def checked(call, screen, folder) -> None:
    """The call checked against its tool's schema, in full and before it is queued; for an acting call, the actions
    it stands for (Python lines run with pyautogui and time imported, or WAIT, DONE, FAIL). Refused when it does not
    hold: the call is then answered at once, takes no step and never reaches the desktop. An upload's path and the
    files of a step or a prepare are also checked to be inside ``folder``, the agent's."""
    schema = next(s for n, _, s in tools(tuple(screen)) if n == call.tool)
    a = call.clean = _valid(call.args, schema, call.tool, "arguments")
    if call.tool == "upload":
        a["path"] = inside(folder, a["path"], "path")
    if call.tool in ("step", "prepare") and "files" in a:
        call.files = [(p, inside(folder, p, f"files[{i}]")) for i, p in enumerate(a["files"])]
    if call.tool == "prepare":
        call.actions = list(a["actions"])
    tool, button = call.tool, repr(a.get("button", "left"))
    if tool == "click":
        call.actions = [f"pyautogui.click({a['x']}, {a['y']}, clicks={a.get('count', 1)}, button={button})"]
    elif tool == "move":
        call.actions = [f"pyautogui.moveTo({a['x']}, {a['y']})"]
    elif tool == "drag":
        call.actions = [f"pyautogui.moveTo({a['x']}, {a['y']}); pyautogui.dragTo({a['to_x']}, {a['to_y']}, "
                        f"duration=0.5, button={button})"]
    elif tool == "scroll":
        dy, dx, x, y = a.get("dy", 0), a.get("dx", 0), a["x"], a["y"]
        call.actions = ["; ".join([f"pyautogui.scroll({-dy}, x={x}, y={y})"] * bool(dy)
                                  + [f"pyautogui.hscroll({dx}, x={x}, y={y})"] * bool(dx))]
    elif tool == "type":
        call.actions = [f"pyautogui.write({a['text']!r})"]
    elif tool == "key":
        call.actions = [f"pyautogui.hotkey({', '.join(repr(k if len(k) == 1 else k.lower()) for k in a['keys'])})"]
    elif tool == "wait":
        call.actions = ["WAIT"]
    elif tool == "step":
        call.actions, call.response, call.screen = list(a["actions"]), a.get("response", ""), a.get("screenshot", False)
        call.end = a.get("end")
    elif tool in FINAL:
        call.actions, call.response = [FINAL[tool]], a.get("message" if tool == "done" else "reason", "")
    elif tool == "upload" and "data" in a:
        try:
            call.data = base64.b64decode(a["data"], validate=True)
        except (binascii.Error, ValueError):
            raise Refused("data is base64")
        if len(call.data) > CHUNK_BYTES:
            raise Refused(f"data is at most {CHUNK_BYTES} bytes a call")
    if tool in SCREEN_TOOLS:
        call.screen = True


class Held:
    """Whole files kept for a run by their sha256, in ``folder`` (the run's, the same for each of its units): a file
    is kept once the chunks a unit sent for it from its start hash to the sha256 given with the first (the bridge
    holds those chunks, for its unit alone). A later upload of that sha256 with no data, in any unit of the run, takes
    the file from here, checked again, and the agent sends no bytes. Nothing else passes from one unit to the next."""

    def __init__(self, folder):
        self.folder = folder

    def get(self, digest):
        try:
            fd = os.open(os.path.join(self.folder, digest), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                data = f.read(KEEP_BYTES + 1)
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def keep(self, digest, data):
        """``data`` kept under its sha256 ``digest``, through a temporary file of its own in the folder (created
        anew, the user's alone), removed on every path."""
        if hashlib.sha256(data).hexdigest() != digest:
            return
        fd = part = None
        try:
            fd, part = tempfile.mkstemp(prefix=".keep-", dir=self.folder)
            f, fd = os.fdopen(fd, "wb"), None
            with f:
                f.write(data)
            os.replace(part, os.path.join(self.folder, digest))
        finally:
            if fd is not None:
                os.close(fd)
            if part is not None:
                with contextlib.suppress(OSError):
                    os.unlink(part)


class Bridge:
    """The benchmark loop's agent, over a Desktop.

    ``steps`` is the loop's budget, ``pause`` the seconds it waits after each action, ``screen`` the screen's size in
    pixels, ``folder`` the agent's own folder in the desktop (an absolute path), made empty at the loop's first
    ``predict``. ``on_first`` is called at the loop's first ``predict``, before any call is served (a runner writes
    its ready file there). ``held`` is a Held, or None. The window before the task is open when ``window`` is true
    (the benchmark's side is told when the task is given, and calls ``close_window``); with no window, upload and
    prepare are refused from the start. ``prepared``, a file, gets one JSON line per prepare call: its actions and the
    files it names, before they run."""

    def __init__(self, desktop, steps, pause, screen, folder, on_first=None, held=None, window=False, prepared=None):
        self.desktop, self.steps, self.pause = desktop, int(steps), pause
        self.screen = (int(screen[0]), int(screen[1]))
        self.on_first, self.held = on_first, held
        if not (isinstance(folder, str) and folder.startswith("/") and len(folder) > 1
                and posixpath.normpath(folder) == folder):
            raise ValueError("the agent's folder is an absolute path in its plain form")
        self.folder = folder
        self.window, self.prepared = bool(window), prepared   # the window before the task
        self.preparing = False
        self.seen, self.unseen = None, "no step has ended"   # the observation, or why there is none
        self.password = desktop.password()
        self.used = 0                                # acting calls handed to the loop: its steps
        self.booked = 0                              # acting calls taken in, handed or waiting
        self.calls = collections.deque()             # calls waiting for the loop, in the order they came
        self.running = None                          # the acting call the loop is running
        self.ended = None                            # why no call is taken any more
        self.turn_ended = False
        self.first = True
        self.partial = {}                            # this unit's uploads being kept: path -> [sha256, hash, bytes]
        self.change = threading.Condition()

    # -- the loop's agent -------------------------------------------------------------------------------------------
    def reset(self, *_, **__):
        """The loop's reset: nothing. The agent's calls wait for the first predict."""

    def predict(self, instruction, obs):
        """The agent's next acting call, as the loop's (response, actions); reading calls are served until it comes.
        Once the turn has ended, no action, but for a done or fail taken before it."""
        if self.first:
            self.first = False
            self._begin(obs)
            if self.on_first:
                self.on_first()
        else:
            self._handed(obs)
        while True:
            call = self._next()
            if call is None:
                return "", []
            if call.tool not in ACTING:
                self._serve(call)
                continue
            if call.tool in FINAL:                   # the loop asks nothing after them: answered as it takes them
                call.answer({"steps_left": self.steps - self.used, "done": True})
                return call.response, list(call.actions)
            return call.response, self._run(call)

    # -- the agent's folder and the observation -------------------------------------------------------------------
    def _begin(self, obs):
        """The loop's first predict: the agent's folder made empty, and the first observation, the one the loop
        hands or, when it hands none, a picture taken now."""
        reply = self.desktop.python(MAKE_FOLDER.format(folder=self.folder))
        if not isinstance(reply, dict) or str(reply.get("output", "")).strip() != "made":
            raise RuntimeError(f"the agent's folder {self.folder} could not be made: {json.dumps(reply)[:300]}")
        if not self._handed(obs):
            self.seen = self.desktop.capture()

    def _handed(self, obs):
        """The screenshot of the observation the loop hands its agent, kept when there is one."""
        shot = obs.get("screenshot") if isinstance(obs, dict) else None
        if isinstance(shot, (bytes, bytearray)) and shot:
            self.seen, self.unseen = bytes(shot), ""
            return True
        return False

    def _observe(self):
        """The observation of a step that ran an action, fixed as it ends: the benchmark's own when its desktop keeps
        one (``observation``), else a picture taken now, the step's last pause passed."""
        look = getattr(self.desktop, "observation", None)
        shot = look() if look is not None else self.desktop.capture()
        if not shot:
            raise RuntimeError("the benchmark gave no observation")
        return bytes(shot)

    def _step_end(self, call):
        """In the loop's thread, once the step's actions and their pauses have run: its observation fixed, when it
        ran an action, and the files it names read. The files' answer, or None when it names none."""
        if call.actions:
            try:
                self.seen, self.unseen = self._observe(), ""
            except Exception as e:                   # the step was run all the same
                self.seen, self.unseen = None, f"{type(e).__name__}: {e}"
        if call.files is None:
            return None
        return self._files(call.files, self.seen if call.screen else None)

    def _files(self, named_paths, image=None):
        """Each file read, in order, as long as the answer holds it: one that would take the answer past LINE_BYTES,
        with ``image`` in it, is an error, and the files after it are still tried."""
        room, out = FILES_ROOM - (4 * -(-len(image) // 3) if image else 0), {}
        for named, path in named_paths:
            got = self._file(path)
            if isinstance(got, str) and _framed(named, got) > room:
                got = {"error": f"the answer would pass {LINE_BYTES} bytes with this file"}
            room -= _framed(named, got)
            out[named] = got
        return out

    def _file(self, path):
        try:
            data = self.desktop.read(path)
        except Refused as e:
            return {"error": str(e)}
        except Exception as e:                       # that file alone: the step has run
            return {"error": f"the desktop could not read it: {type(e).__name__}: {e}"}
        if data is None:
            return None
        if len(data) > READ_BYTES:
            return {"error": f"over {READ_BYTES} bytes"}
        return base64.b64encode(data).decode()

    def _prepare(self, call):
        """A prepare call, in the loop's thread: its record written, its actions run one at a time with no pause (WAIT
        does nothing), then its files read. An action the desktop did not end in time stops it."""
        if self.prepared:
            with open(self.prepared, "a", encoding="utf-8") as f:
                f.write(json.dumps({"start": time.time(), "actions": call.actions,
                                    "files": [named for named, _ in call.files or []]}) + "\n")
        for i, code in enumerate(call.actions):
            if code.strip() != "WAIT" and self.desktop.python(PREPARE.format(code=code)) is None:
                raise Refused(f"the desktop did not end actions[{i}] in time; the actions after it were not run")
        return {"files": self._files(call.files or [])}

    def close_window(self):
        """Close the window before the task, refuse waiting preparation and wait for active preparation to end."""
        with self.change:
            self.window = False
            refused = [c for c in self.calls if c.tool in WINDOW]
            self.calls = collections.deque(c for c in self.calls if c not in refused)
        for call in refused:
            call.answer(error=f"the task has been given: {call.tool} is taken only before it")
        with self.change:
            while self.preparing:
                self.change.wait()

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
        """The call's actions, one at a time as the loop asks, none once the turn has ended; once the loop asks past
        the last, it has run them all and their pause, and the call is answered. A step with an end then gives the
        loop its DONE or FAIL, in the same step; one not run to its end gives none."""
        ran = False
        try:
            for action in call.actions:
                if self.turn_ended:
                    break
                yield action
            else:
                ran = True
        finally:
            self._ran(call, ran)
        if ran and call.end:
            yield FINAL[call.end]

    def _ran(self, call, ran):
        files = self._step_end(call) if ran and not self.turn_ended else None
        with self.change:
            self.running = None
            if ran and call.end:                     # the task has ended, as after done or fail
                refused = self._end(f"the task has ended ({call.end})")
            else:
                refused = self._end("the steps are used up") if self.used >= self.steps else []
            value = {"steps_left": self.steps - self.used, "done": self.ended is not None}
            turn_ended = self.turn_ended
        if not ran:
            call.answer(error="the agent's turn has ended" if turn_ended else "the step was not run to its end")
        else:
            if files is not None:
                value["files"] = files
            if call.screen and not turn_ended and self.seen is None:
                call.answer(error=f"the step was run; the screen could not be taken: {self.unseen}")
            else:
                call.answer(value, self.seen if call.screen and not turn_ended else None)
        for c in refused:
            c.answer(error=self.ended)

    def _serve(self, call):
        preparing = False
        try:
            if call.tool in WINDOW:
                with self.change:
                    if not self.window:
                        raise Refused(f"the task has been given: {call.tool} is taken only before it")
                    self.preparing = preparing = True
            call.answer(**self._reading(call))
        except Refused as e:
            call.answer(error=str(e))
        except Exception as e:                       # the desktop failed it: its words, and the agent may call again
            call.answer(error=f"the desktop failed {call.tool}: {type(e).__name__}: {e}")
        finally:
            if preparing:
                with self.change:
                    self.preparing = False
                    self.change.notify_all()

    def _reading(self, call):
        if call.tool == "screenshot":
            if self.seen is None:
                raise Refused(f"there is no observation: {self.unseen}")
            return {"image": self.seen}
        if call.tool == "prepare":
            return {"value": self._prepare(call)}
        return {"value": self._upload(call)}

    def _upload(self, call):
        a, data = call.clean, call.data
        path, append, digest = a["path"], a.get("append", False), a.get("sha256")
        if not append:
            self.partial.pop(path, None)             # every start of the file drops what was being kept for it
        if data is None:                             # the run's file of that sha256: it starts the file
            data = self.held.get(digest) if self.held is not None else None
            if data is None:
                raise Refused("the run holds no file of that sha256: send its data")
            self.desktop.upload(path, data)
            return {"size": len(data)}
        if not append:
            self.desktop.upload(path, data)
            size = len(data)
            if digest and self.held is not None:
                self.partial[path] = [digest, hashlib.sha256(), bytearray()]
        else:
            try:
                size = self._append(path, data)
            except BaseException:
                self.partial.pop(path, None)
                raise
        kept = self.partial.get(path)
        if kept is not None:
            kept[1].update(data)
            kept[2].extend(data)
            if len(kept[2]) > KEEP_BYTES:
                del self.partial[path]
            elif kept[1].hexdigest() == kept[0]:
                del self.partial[path]
                self.held.keep(kept[0], bytes(kept[2]))
        return {"size": size}

    def _append(self, path, data):
        """``data`` added to the file at ``path``: written beside it under a name of the server's own, then added to
        it in the desktop; that name is removed on every path."""
        part = posixpath.join(posixpath.dirname(path), f".upload-{secrets.token_hex(8)}")
        try:
            self.desktop.upload(part, data)
            reply = self.desktop.python(APPEND.format(part=part, path=path))
        except BaseException:                        # the part may be there: removed, as far as the desktop answers
            self._drop(part)
            raise
        try:
            return int(str(reply.get("output")).strip())
        except (AttributeError, ValueError):
            if reply is None:                        # the desktop gave no reply: the part may still be there
                self._drop(part)
            raise Refused(f"the desktop did not add the data to {path}: {json.dumps(reply)[:300]}")

    def _drop(self, part):
        with contextlib.suppress(Exception):
            self.desktop.python(f"import os\ntry:\n    os.remove({part!r})\nexcept OSError:\n    pass")

    # -- what the server and the runner call ------------------------------------------------------------------------
    def submit(self, call):
        """A call that needs the desktop, for the loop's thread; refused at once once the task ended, and an acting
        call once every step is taken."""
        with self.change:
            refusal = self.ended
            if refusal is None and self.window and call.tool in ACTING:
                refusal = ("the task has not been given yet: before it, the desktop takes info, screenshot, upload "
                           "and prepare, and no step")
            elif refusal is None and not self.window and call.tool in WINDOW:
                refusal = f"the task has been given: {call.tool} is taken only before it"
            elif refusal is None and call.tool in ACTING:
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
                "password": self.password, "folder": self.folder}

    def instructions(self):
        w, h = self.screen
        return (f"A desktop seen in screenshots and driven by pointer and keyboard actions. The screen is {w} by "
                f"{h} pixels. Points use x and y in screenshot pixels at the screen's size, from its top left "
                f"corner. Look at a screenshot before the first pointing action. Each call of click, move, drag, "
                f"scroll, type, key, wait, step, done or fail is one step, and the desktop is reached through "
                f"these calls alone. After the actions and their {self.pause:g} s pauses, click, move, drag, "
                f"scroll, type, key and wait return the step's observation, the screen as a PNG image; step does "
                f"when screenshot is true, and returns the files of folder it names. screenshot returns the last "
                f"step's observation and takes no new picture: wait is the way to look again. Before the task is "
                f"given, in the window from the session's start, upload and prepare work inside folder "
                f"({self.folder}) and take no step, and no acting call is taken; once it is given, upload and "
                f"prepare are refused. done and fail answer before "
                f"they run, with no image. A step with end, done or fail, runs its actions and then ends the "
                f"task, in that one step. There are {self.steps} steps in all, and steps_left in each acting "
                f"answer. Once they are used up, every call but info is refused. A turn that ends with neither "
                f"done nor fail ends the task as the desktop then is.")

    def end_turn(self):
        """The agent's turn has ended: the wait for its next call ends, the actions of a step not yet run are not
        run, and no call is taken any more. A done or fail already taken stays, for the loop."""
        with self.change:
            self.turn_ended = True
            refused = self._end("the agent's turn has ended", keep_final=True)
        for c in refused:
            c.answer(error=self.ended)

    def finish(self):
        """The loop has returned, or failed: every call still waiting, and one the loop was running, is answered, and
        what this unit was keeping of its uploads is dropped."""
        with self.change:
            refused = self._end("the task has ended")
            running, self.running = self.running, None
            self.partial.clear()
        for c in refused + ([running] if running is not None else []):
            c.answer(error=self.ended)

    def _end(self, why, keep_final=False):
        """No call taken any more (with the condition held); the calls waiting, to refuse once it is let go, but the
        first done or fail when ``keep_final``."""
        if self.ended is None:
            self.ended = why
        final = next((c for c in self.calls if c.tool in FINAL), None) if keep_final else None
        refused = [c for c in self.calls if c is not final]
        self.calls.clear()
        if final is not None:
            self.calls.append(final)
        self.change.notify_all()
        return refused


def frame(message) -> bytes:
    return (json.dumps(message) + "\n").encode()


def bounded(message) -> tuple:
    """``message`` framed, at most LINE_BYTES with its newline, and whether it had to be replaced: one over the bound
    becomes an error with the same id, or with none when even that is over it."""
    data = frame(message)
    if len(data) <= LINE_BYTES:
        return data, False
    error = {"code": -32603, "message": f"the answer is over {LINE_BYTES} bytes"}
    data = frame({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None, "error": error})
    if len(data) > LINE_BYTES:
        data = frame({"jsonrpc": "2.0", "id": None, "error": error})
    return data, True


class Wire:
    """One connection of the agent's session: JSON-RPC messages, one a line, each at most LINE_BYTES with its newline.
    Its answers are written by a thread of its own; at most OUT_BYTES of them wait for the client to read, past which
    the connection is closed. ``close`` never waits for a write. Once its input has ended, it is closed when every
    call it made has been answered and written. ``on_closed`` is called once, when it is closed."""

    def __init__(self, conn, on_closed=None):
        self.conn, self.on_closed = conn, on_closed
        self.lock = threading.Lock()                 # the wire's state; never held across a call on the socket
        self.ready = threading.Condition(self.lock)
        self.out, self.out_bytes = collections.deque(), 0
        self.open_calls, self.input_ended, self.closing, self.closed = 0, False, False, False
        threading.Thread(target=self._write, name="computer-writer", daemon=True).start()

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
                if i + 1 > LINE_BYTES:
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
        """``message`` (an object, or a framed line) queued for the writer; False when the wire is closed, or closed
        now because its client has left OUT_BYTES unread."""
        data = message if isinstance(message, bytes) else bounded(message)[0]
        if len(data) > LINE_BYTES:                   # a framed line over the bound, from a caller that did not bound it
            data = frame({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32603, "message": f"the answer is over {LINE_BYTES} bytes"}})
        with self.lock:
            if self.closed:
                return False
            overflow = self.out_bytes + len(data) > OUT_BYTES
            if not overflow:
                self.out.append(data)
                self.out_bytes += len(data)
                self.ready.notify_all()
        if overflow:
            self.close()
            return False
        return True

    def _write(self):
        while True:
            with self.lock:
                while not self.out and not self.closing and not self.closed:
                    self.ready.wait()
                if self.closed or not self.out:      # closed, or closing with every answer written
                    break
                data = self.out.popleft()
            try:
                self.conn.sendall(data)
            except OSError:
                break
            with self.lock:
                self.out_bytes -= len(data)
        self.close()

    def opened(self):
        with self.lock:
            self.open_calls += 1

    def answered(self):
        with self.lock:
            self.open_calls -= 1
            if self.input_ended and not self.open_calls:
                self.closing = True
                self.ready.notify_all()

    def ended(self):
        with self.lock:
            self.input_ended = True
            if not self.open_calls:
                self.closing = True
                self.ready.notify_all()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.out.clear()
            self.ready.notify_all()
        with contextlib.suppress(OSError):
            self.conn.shutdown(socket.SHUT_RDWR)     # also ends a write in progress on a real socket
        with contextlib.suppress(OSError):
            self.conn.close()
        if self.on_closed is not None:
            self.on_closed(self)


def _good_id(rid):
    return isinstance(rid, str) or (isinstance(rid, int) and not isinstance(rid, bool))


class Server:
    """The MCP server ``computer`` at ``path``, a Unix socket (its folder the user's alone), over a Bridge. ``log``,
    when given, gets one JSON line per tool call: ``{tool, start, end, isError}``."""

    def __init__(self, bridge, path, log=None):
        self._state(bridge, path, log)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(16)
        threading.Thread(target=self._accept, name="computer-accept", daemon=True).start()

    def _state(self, bridge, path, log):
        self.bridge, self.path, self.log = bridge, path, log
        self.log_lock, self.lock = threading.Lock(), threading.Lock()
        self.closing, self.wires = False, set()

    def close(self):
        with self.lock:
            self.closing = True
            wires = list(self.wires)
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.listener.close()
        for wire in wires:
            wire.close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def _accept(self):
        while True:
            try:
                conn = self.listener.accept()[0]
            except OSError:
                return
            with self.lock:
                wire = None if self.closing else Wire(conn, on_closed=self._forget)
                if wire is not None:
                    self.wires.add(wire)
            if wire is None:                         # came as the server closed
                with contextlib.suppress(OSError):
                    conn.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    conn.close()
                return
            threading.Thread(target=self._serve, args=(wire,), name="computer-connection", daemon=True).start()

    def _forget(self, wire):
        with self.lock:
            self.wires.discard(wire)

    def _serve(self, wire):
        try:
            for line in wire.lines():
                try:
                    message = json.loads(line, parse_constant=_no_constant)
                except (ValueError, RecursionError):
                    wire.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}})
                    continue
                reply = self.handle(message, wire)
                if reply is not None:
                    wire.send(reply)
        except Exception as e:                       # a failure of this handler: its connection is closed
            sys.stderr.write(f"computer: a connection's handler failed: {type(e).__name__}: {e}\n")
            wire.close()
            return
        wire.ended()

    def handle(self, m, wire):
        """One message in; its reply out, None for a notification or a tool call answered on the wire later."""
        if not isinstance(m, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "a message is a JSON object"}}
        rid, method, params = m.get("id"), m.get("method"), m.get("params", {})
        if m.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict) or (
                "id" in m and not _good_id(rid)):
            if "id" not in m:
                return None
            return {"jsonrpc": "2.0", "id": rid if _good_id(rid) else None,
                    "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request: jsonrpc \"2.0\", a method, an "
                                                         "id that is a text or an integer, params an object"}}
        if "id" not in m:
            return None
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "computer", "version": "1"},
                      "instructions": self.bridge.instructions()}
        elif method == "tools/list":
            result = {"tools": listed(self.bridge.screen)}
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
        if not isinstance(name, str) or name not in NAMES:
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
            data, over = bounded({"jsonrpc": "2.0", "id": rid, "result": {"content": content,
                                                                          "isError": error is not None}})
            failed = error is not None or over       # the answer's size is known before its outcome is recorded
            self.record({"tool": call.tool, "start": call.start, "end": time.time(), "isError": failed})
            wire.send(data)
            wire.answered()

        wire.opened()
        call = Call(name, params.get("arguments", {}), reply)
        try:
            checked(call, self.bridge.screen, self.bridge.folder)
            if name == "info":
                call.answer(self.bridge.info())
                return
        except Refused as e:
            call.answer(error=str(e))
            return
        except Exception as e:                       # checked failed: the call is refused, and recorded
            call.answer(error=f"the arguments could not be checked: {type(e).__name__}")
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
    """The read end of a pipe from the driver. A line TURN_END ends the agent's turn (``bridge.end_turn``); a line
    TASK_GIVEN closes the window before the task (``bridge.close_window``); any other line is ignored. Once
    the driver's end closes (the driver ended, was killed, or let the unit go), the runner stops as on SIGTERM. Run it
    in a thread of its own."""
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
                elif line.strip() == TASK_GIVEN and bridge is not None:
                    bridge.close_window()
            buf = buf[-4096:]
    except OSError:
        pass
    os.kill(os.getpid(), signal.SIGTERM)


class Stop:
    """The runner's one stop: SIGTERM, or the lifeline's end, taken once; a second does not cut the clean-up. While a
    part that must not be cut runs (``deferred``), a stop waits for its end. ``seconds`` after the stop, a runner still
    there calls ``last`` (its last resort), gives it LAST_SECONDS at most, and exits 1."""

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
            last = threading.Thread(target=self._quietly, name="last-resort-callback", daemon=True)
            last.start()
            last.join(LAST_SECONDS)
        said = threading.Thread(target=self._say, name="last-resort-word", daemon=True)
        said.start()
        said.join(0.1)                               # stderr may be a pipe nobody reads: the exit never waits on it
        os._exit(1)

    def _say(self):
        with contextlib.suppress(OSError):
            os.write(2, f"runner: the clean-up did not end within {self.seconds:g} s of the stop\n".encode())

    def _quietly(self):
        with contextlib.suppress(Exception):
            self.last()
