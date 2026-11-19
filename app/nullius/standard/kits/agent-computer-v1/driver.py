"""Kit for agent.computer/v1: nine checks on a small real desktop, decided by the desktop's own records.

Basic: ``see``, ``click``, ``type``, ``limit``. Skills: ``drag``, ``scroll``, ``recover``, ``batch``
(``checks.py`` gives each one's screen and verdict) and ``late``.

**A unit.** ``setup`` starts a desktop of the kit's image (``desktop.py``) and the fixture on it, then, in this
driver's process, the port's server over that desktop (``computer_port.Bridge`` and ``Server``, its socket in a
folder of this user's alone) and the kit's loop: a thread that asks the bridge for a step, runs its actions with
``Desktop.act`` and waits the pause after each, as a desktop benchmark's own loop does. The session gets the
server through ``computer_pipe.py`` (``mcp_servers.computer``). The server's window before the task is closed by
``task_given`` (which the definition's ``driver.task_given`` asks Nullius to send) or, for the reference, as it
starts; every prepare call is recorded in ``verifier/computer-prepare.jsonl``, the agent's folder is ``FOLDER``.
``score`` ends the turn, waits for the loop, reads the fixture's log, the actions' times and the call log, judges,
and removes the desktop and the socket. ``oracle`` is the reference: the right calls, sent to the unit's own server
as a client.

**late.** One labelled button appears ``LATE_SECONDS`` after the task is given, away from the screen's centre at a
place drawn for the unit (``late_place``), so that no click at a fixed point finds it; the agent clicks it. It passes
when the click is the check ``click``'s, and a picture was taken as a step ended, after the button was up and before
the action holding the press began. Its times are the driver's own clock (when the button was up,
each picture of a step's end, each action's start), the press matched to its action on the desktop's clock.

**What is left when a unit never reaches ``score``.** Nothing: a new ``setup`` ends the unit before it, and the
end of the driver's input (the run is over, or stopped) ends the one still open. A driver that is killed cannot
do that, so it keeps a reaper (``reaper.py``), started in a session of its own, told of each container and
socket folder as it is made and as it goes: when the driver is gone, however it went, the reaper removes what is
left and exits. Each container also carries the job's label, which a run on the container backend sweeps.
"""
import atexit
import base64
import contextlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from nullius.standard import computer_port as port
from nullius.standard import container
from nullius.standard.jsonl import serve

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


desktop = _load("nullius_kit_desktop", "desktop.py")
checks = _load("nullius_kit_checks", "checks.py")
fixture = _load("nullius_kit_fixture", "fixture.py")

UNITS = checks.CHECKS + ("late",)
PAUSE = 1.0                                  # after each action
PYTHON_SECONDS = 90                          # one desktop call's reply
AGENT_SECONDS = 600                          # the agent's turn
FOLDER = "/tmp/agent"                        # the agent's own folder in the desktop
LATE_SECONDS = 3.0                           # late: the button appears this long after the task is given
LATE_PLACES = ((0.15, 0.35), (0.65, 0.85))   # late: where the button's centre may be, as fractions of each side
LOOP_SECONDS = PYTHON_SECONDS + desktop.SETTLE + PAUSE + 30     # the loop's end, once the turn has ended
USE = "Use the tools of the MCP server named computer, given to this session: they show and drive a desktop. "
DONE = " Then reply with the word done."
NOT_RUN = "The desktop could not run this step, and none of it was run. It may be sent again."


def prompt(unit, expected):
    if unit == "see":
        return USE + "Read the words on the screen and reply with them in the order shown."
    if unit in ("click", "recover"):
        return USE + f"Click the button labelled {expected['target']} on the screen, once." + DONE
    if unit == "type":
        return USE + f"Click the text field on the screen, type the word {expected['word']} in it and press Enter." + DONE
    if unit == "limit":
        return (USE + "Click each of the six buttons on the screen, one click each, in this order: "
                + ", ".join(expected["labels"]) + "." + DONE)
    if unit == "drag":
        return USE + "Drag the blue square labelled MOVE into the green box labelled HERE and let it go inside the box." + DONE
    if unit == "scroll":
        return (USE + "Scroll to the end of the list and read the large words there. "
                      "Reply with those words in the order shown.")
    if unit == "batch":
        w = expected["words"]
        return (USE + "Fill the three text fields on the screen: " + ", ".join(f"{w[f]} in the field labelled {f}"
                                                                                for f in checks.FIELDS) + "." + DONE)
    if unit == "late":
        return (USE + f"A button labelled {expected['target']} appears on the screen a few seconds after you are "
                      f"given this task. Click it once." + DONE)
    raise ValueError(f"no unit {unit!r}")


class Reaper:
    """The reaper's stdin, one line a thing made or let go; started at the first thing made."""

    def __init__(self):
        self.p, self.lock = None, threading.Lock()

    def tell(self, line):
        with self.lock:
            if self.p is None:
                self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "reaper.py"), container.DOCKER],
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=container.client_env(
                                              {"PATH": os.environ.get("PATH", os.defpath)}),
                                          text=True, start_new_session=True)
            try:
                self.p.stdin.write(line + "\n")
                self.p.stdin.flush()
            except OSError:
                pass

    def close(self):
        with self.lock:
            if self.p is not None:
                try:
                    self.p.stdin.close()
                except OSError:
                    pass
                try:
                    self.p.wait(timeout=10)          # with nothing named it ends at once; else it ends on its own
                except subprocess.TimeoutExpired:
                    pass
                self.p = None


REAPER = Reaper()


class KitDesktop:
    """The five operations the port's bridge calls, over the unit's desktop."""

    def __init__(self, d):
        self.d = d

    def python(self, code):
        return self.d.python(code, PYTHON_SECONDS)

    def upload(self, path, data):
        self.d.upload(path, data)

    def read(self, path):
        try:
            return self.d.read(path)
        except desktop.Refused as e:                 # the agent's error, said as the port says it
            raise port.Refused(str(e))

    def capture(self):
        return self.d.capture()

    def password(self):
        return self.d.password()


class Pictures(KitDesktop):
    """The unit's desktop, with each step's picture timed on the driver's clock."""

    def __init__(self, d, unit):
        super().__init__(d)
        self.unit = unit

    def capture(self):
        t = time.monotonic()
        png = super().capture()
        if self.unit.bridge.running is not None:
            self.unit.pictures.append((t, time.monotonic()))
        return png


class KitBridge(port.Bridge):
    """The port's bridge, with the kit's budget count and first acting call's recover error."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.past = 0
        self.reject_first = False

    def _next(self):
        if not self.reject_first:
            return super()._next()
        with self.change:
            while not self.calls and self.ended is None:
                self.change.wait(0.5)
            if not self.calls or self.calls[0].tool not in port.ACTING:
                return super()._next()
            call = self.calls.popleft()
            self.reject_first = False
            self.used += 1
        call.answer(error=NOT_RUN)                  # before done or fail can end the task
        self._ran(call, False)
        return None                                # this step ran no action; the next predict goes on


class KitServer(port.Server):
    """Count, timestamp, check and reserve in one admission, including calls whose arguments do not hold."""

    def call(self, rid, params, wire):
        start = time.time()
        name = params.get("name")
        if name not in port.ACTING:
            return super().call(rid, params, wire)

        def reply(call, value, image, error):
            content = []
            if error is not None:
                content.append({"type": "text", "text": str(error)})
            if value is not None:
                content.append({"type": "text", "text": json.dumps(value)})
            if image is not None:
                content.append({"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/png"})
            data, over = port.bounded({"jsonrpc": "2.0", "id": rid, "result": {"content": content,
                                                                                "isError": error is not None}})
            self.record({"tool": call.tool, "start": call.start, "end": time.time(), "isError": error is not None or over})
            wire.send(data)
            wire.answered()

        with self.bridge.change:
            wire.opened()
            call = port.Call(name, params.get("arguments", {}), reply)
            call.start = start                     # includes time waiting for admission's lock
            if self.bridge.booked >= self.bridge.steps:
                self.bridge.past += 1
            try:
                port.checked(call, self.bridge.screen, self.bridge.folder)
            except port.Refused as e:
                call.answer(error=str(e))
                return
            except Exception as e:
                call.answer(error=f"the arguments could not be checked: {type(e).__name__}")
                return
            self.bridge.submit(call)


class Unit:
    """One unit's desktop, server and loop; ``late`` draws its button once the task is given."""

    def __init__(self, unit, dir):
        self.unit, self.trial = unit, os.path.dirname(os.path.abspath(dir))
        if unit == "late":                           # the click screen with one button, drawn later
            label = secrets.SystemRandom().choice(checks.LABELS)
            self.values = {"screen": "click", "labels": [label], "at": late_place()}
            self.expected = {"target": label, "labels": [label]}
        else:
            self.values, self.expected = checks.draw(unit)
        self.steps = checks.steps(unit)
        self.log = os.path.join(self.trial, "verifier", "computer-calls.jsonl")
        self.prepared = os.path.join(self.trial, "verifier", "computer-prepare.jsonl")
        self.desktop = self.bridge = self.server = self.loop = self.folder = self.boxes = None
        self.final, self.error = "", None
        self.pictures, self.acted, self.shown = [], [], None
        self.given, self.up, self.ending = threading.Event(), threading.Event(), threading.Event()

    def start(self, job):
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        for path in (self.log, self.prepared):
            if os.path.exists(path):
                os.remove(path)
        image = desktop.image(os.path.dirname(self.log))
        name = f"nullius-desktop-{os.urandom(6).hex()}"
        REAPER.tell(f"+container {name}")            # before it exists: a kill during the start is covered
        self.desktop = desktop.Desktop.start(image, name, job=job)
        REAPER.tell(f"=container {name}")            # only a successful start rules out late creation
        acting = self.desktop.act

        def act(code, seconds=PYTHON_SECONDS):       # each action's start on the driver's clock too
            t = time.monotonic()
            r = acting(code, seconds)
            if r.get("start") is not None:
                self.acted.append((t, r["start"], r["end"]))
            return r
        self.desktop.act = act
        if self.unit != "late":
            self.desktop.start_fixture(self.values)
            self.boxes = self.desktop.events()[0].get("boxes") or {}
        folder = os.path.join(tempfile.gettempdir(), f"nullius-computer-{os.urandom(6).hex()}")
        REAPER.tell(f"+folder {folder}")             # named before it is made
        self.folder = folder
        os.mkdir(folder, 0o700)
        self.bridge = KitBridge(Pictures(self.desktop, self), self.steps, PAUSE, desktop.SCREEN, FOLDER, window=True,
                                prepared=self.prepared)
        self.server = KitServer(self.bridge, self.socket(), log=self.log)
        self.loop = threading.Thread(target=self.run_loop, name=f"kit-loop-{self.unit}", daemon=True)
        self.loop.start()
        if self.unit == "late":
            threading.Thread(target=self.late, name="kit-late", daemon=True).start()

    def task_given(self):
        self.bridge.close_window()
        self.given.set()

    def late(self):
        """The button drawn ``LATE_SECONDS`` after the task is given, unless the unit ends first."""
        while not self.given.wait(0.5):
            if self.ending.is_set():
                return
        if self.ending.wait(LATE_SECONDS):
            return
        try:
            self.desktop.start_fixture(self.values)
            self.boxes = self.desktop.events()[0].get("boxes") or {}
            self.shown = time.monotonic()
        except Exception as e:                       # the unit says so at score
            self.error = f"the button could not be drawn: {type(e).__name__}: {e}"
        finally:
            self.up.set()

    def events(self):
        """The fixture's log; none for ``late`` while its button was never drawn."""
        if self.unit == "late" and self.shown is None and not self.boxes:
            return []
        return self.desktop.events()

    def socket(self):
        return os.path.join(self.folder, "s")

    def run_loop(self):
        """The kit's own loop: a step from the bridge, its actions run, the pause after each; the budget is the
        loop's. An action the desktop did not end in time leaves its call answered with an error, and the next
        step goes on. ``recover``: the first acting call the loop takes, whatever it is, is answered with an error
        before any part of it runs."""
        self.bridge.reject_first = self.unit == "recover"
        try:
            for _ in range(self.steps):
                response, actions = self.bridge.predict("", None)
                actions = iter(actions)
                stop = False
                try:
                    for action in actions:
                        if action in ("DONE", "FAIL"):
                            self.final, stop = response or "", True
                            break
                        if action == "WAIT":
                            time.sleep(PAUSE)
                            continue
                        if self.desktop.act(action, PYTHON_SECONDS)["reply"] is None:
                            break                    # not ended in time: the call is answered with an error
                        time.sleep(PAUSE)
                finally:
                    close = getattr(actions, "close", None)
                    if close is not None:
                        close()
                if stop:
                    break
        except Exception as e:                       # the desktop failed: the unit says so at score
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.bridge.finish()

    def end(self, wait=LOOP_SECONDS):
        """The turn's end, the loop's (``wait`` seconds at most), the server's: what the verdict reads is
        complete."""
        self.ending.set()
        if self.bridge is not None:
            self.bridge.end_turn()
        if self.loop is not None:
            self.loop.join(wait)
            if self.loop.is_alive() and self.error is None:
                self.error = f"the kit's loop did not end within {wait:g} s of the turn's end"
        if self.server is not None:
            self.server.close()
            self.server = None

    def keep(self, events):
        """A copy of the fixture's events and of where its targets were drawn, beside the call log: what a reader
        of a failed check needs once the desktop is gone."""
        folder = os.path.dirname(self.log)
        for name, lines in (("computer-events.jsonl", (json.dumps(e) + "\n" for e in events)),
                            ("computer-boxes.json", json.JSONEncoder().iterencode(self.boxes or {}))):
            fd = part = None
            try:
                fd, part = tempfile.mkstemp(prefix=".computer-", dir=folder)
                f, fd = os.fdopen(fd, "w", encoding="utf-8"), None
                with f:
                    f.writelines(lines)
                os.replace(part, os.path.join(folder, name))
            finally:
                if fd is not None:
                    os.close(fd)
                if part is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(part)

    def calls(self):
        out = []
        try:
            with open(self.log, encoding="utf-8") as f:
                for line in f:
                    out.append(json.loads(line))
        except (OSError, ValueError):
            pass
        return out

    def remove(self):
        """The unit's desktop and socket folder gone; an error is said once both were tried."""
        failed = None
        if self.desktop is not None:
            try:
                self.desktop.remove()
                REAPER.tell(f"-container {self.desktop.name}")
            except desktop.DesktopError as e:
                failed = e
            self.desktop = None
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
            REAPER.tell(f"-folder {self.folder}")
            self.folder = None
        if failed is not None:
            raise failed

    def close(self):
        """The unit ended without its score: everything it holds let go, the loop not waited for (its desktop
        goes from under it)."""
        try:
            self.end(wait=1)
        finally:
            self.remove()


OPEN = {}                                    # unit -> Unit, while it is set up and not scored
OPEN_LOCK = threading.Lock()


def close_all(*, shutdown=True):
    """Every unit still open let go, then every client of the engine still running (a loop's call that hangs)
    ended and waited for."""
    with OPEN_LOCK:
        held = list(OPEN.values())
        OPEN.clear()
    for u in held:
        try:
            u.close()
        except Exception as e:
            sys.stderr.write(f"computer kit: {u.unit} could not be let go: {e}\n")
    desktop.end_clients(closing=shutdown)


def job_of(dir):
    """The job a unit's folder belongs to: <job>/<trial>/workspace."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(dir)))) or None


def late_place(rng=None):
    """Where ``late`` draws its button: the point its frame is centred on, as fractions of the screen's width and
    height, each in one of ``LATE_PLACES``. The button never covers the screen's centre, and the place is drawn anew
    for each unit."""
    rng = rng or secrets.SystemRandom()
    return [round(rng.uniform(*rng.choice(LATE_PLACES)), 3) for _ in range(2)]


def late_verdict(u, events, actions, message):
    """``late``: the click's verdict, and an observation of the button, taken as a step ended, before the press."""
    v = checks.click(u.expected, events, actions, message)
    if not v["passed"]:
        return v
    if u.shown is None:
        return {"passed": False, "why": "the button was never drawn"}
    press = next(e for e in events if e.get("kind") == "press" and e.get("hit") == u.expected["target"]
                 and e.get("button") == 1)
    sent = next((h for h, s, e in u.acted if s <= press["t"] <= e + checks.SLACK), None)
    if sent is None or not any(start > u.shown and end < sent for start, end in u.pictures):
        return {"passed": False, "why": "no step's observation showed the button before the press: it was pressed "
                                        "without looking again by a step"}
    return {"passed": True, "why": ""}


def units(dataset, **_):
    return {"units": list(UNITS)}


def setup(unit, dir, **_):
    if unit not in UNITS:
        raise ValueError(f"no unit {unit!r}")
    close_all(shutdown=False)                    # one unit at a time: the one before never reached score
    u = Unit(unit, dir)
    with OPEN_LOCK:
        OPEN[unit] = u
    try:
        u.start(job_of(dir))
    except BaseException:
        with OPEN_LOCK:
            OPEN.pop(unit, None)
        u.close()
        raise
    return {"prompt": prompt(unit, u.expected), "mcp": [{"name": "computer", "server": "computer", "args": [u.socket()]}],
            "agent_seconds": AGENT_SECONDS}


def task_given(unit, dir, **_):
    """The agent is being given its task: the window before it closes, and ``late``'s count starts."""
    with OPEN_LOCK:
        u = OPEN.get(unit)
    if u is None:
        raise ValueError(f"{unit} was not set up")
    u.task_given()
    return {}


def score(unit, dir, transcript, answer, **_):
    with OPEN_LOCK:
        u = OPEN.pop(unit, None)
    if u is None:
        raise ValueError(f"{unit} was not set up")
    try:
        u.end()
        events = u.events()
        actions = list(u.desktop.actions)
        calls = u.calls()
        try:
            u.keep(events)
        except OSError:                             # the optional copies cannot change the verdict
            pass
    finally:
        u.remove()
    message = "\n".join(x for x in (answer or "", u.final) if x)
    if unit == "late":
        v = late_verdict(u, events, actions, message)
    else:
        v = checks.verdict(unit, u.expected, events, actions, message, calls, u.bridge.past)
    passed = v["passed"] and u.error is None
    detail = {"said": (answer or "")[:200], "steps": u.steps, "steps_used": u.bridge.used, "past_budget": u.bridge.past,
              "calls": [c.get("tool") for c in calls], "errors": sum(1 for c in calls if c.get("isError")),
              "actions": len(actions), "events": sum(1 for e in events if e.get("kind") in checks.POINTER + checks.KEYS)}
    why = v["why"] or u.error
    if why:
        detail["why"] = why
    return {"reward": 1.0 if passed else 0.0, "passed": passed, "detail": detail}


class Client:
    """An MCP client over the unit's socket: one call at a time, its answer waited for."""

    def __init__(self, path, timeout=AGENT_SECONDS):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.reader, self.n = self.sock.makefile("rb"), 0
        self.ask("initialize", {"protocolVersion": port.PROTOCOL, "capabilities": {},
                                "clientInfo": {"name": "reference", "version": "1"}})

    def ask(self, method, params):
        self.n += 1
        self.sock.sendall(port.frame({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}))
        while True:
            line = self.reader.readline()
            if not line:
                raise ConnectionError("the server closed the connection")
            m = json.loads(line)
            if m.get("id") == self.n:
                return m

    def call(self, tool, arguments=None):
        """``(isError, value)``: the result's JSON text read, its image as bytes."""
        r = self.ask("tools/call", {"name": tool, "arguments": arguments or {}})["result"]
        value = image = None
        for c in r["content"]:
            if c["type"] == "image":
                image = base64.b64decode(c["data"])
            elif value is None:
                try:
                    value = json.loads(c["text"])
                except ValueError:
                    value = c["text"]
        return r["isError"], image if tool == "screenshot" else value

    def close(self):
        self.reader.close()
        self.sock.close()


def centre(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


class ReferenceFailed(Exception):
    """A call of the reference did not go as a correct agent's would."""


def read_words(png, scale, count):
    """The words drawn on the screen, read from a screenshot, as an agent reads them."""
    found = [line for line in fixture.read_lines(png, scale)
             if len(line.split()) == count and len(set(line.split())) == count
             and all(word in checks.WORDS for word in line.split())]
    if len(found) != 1:
        raise ReferenceFailed(f"no one group of words could be read from the screenshot ({len(found)} found)")
    return found[0]


def oracle(unit, dir, **_):
    """The reference: given its task at once, then the calls a correct agent makes, sent to the unit's own server,
    through its desktop, to the same scorer; the answer they lead to. Words are read from the screenshot the server
    gives, and a call that does not answer as a correct agent's would (the one ``recover`` refuses aside) fails the
    reference. For ``late``, steps that look again until the button is up and an observation shows it, then its
    click."""
    with OPEN_LOCK:
        u = OPEN.get(unit)
    if u is None:
        raise ValueError(f"{unit} was not set up")
    u.task_given()
    if unit == "late":
        return late_reference(u)
    e, b, answer = u.expected, u.boxes, "done"
    c = Client(u.socket())

    def call(tool, arguments=None, failing=False):
        failed, value = c.call(tool, arguments)
        if failed != failing:
            raise ReferenceFailed(f"{tool} answered {'with' if failed else 'without'} an error: {str(value)[:200]}")
        return value

    try:
        call("info")
        if unit in ("see", "scroll"):
            if unit == "scroll":
                x, y = centre(b["list"])
                call("scroll", {"x": x, "y": y, "dy": 100})
            png = call("screenshot")
            if not isinstance(png, bytes):
                raise ReferenceFailed("the screenshot held no image")
            answer = read_words(png, fixture.SEE_SCALE if unit == "see" else fixture.SCROLL_SCALE,
                                3 if unit == "see" else 2)
        elif unit in ("click", "recover"):
            x, y = centre(b[e["target"]])
            if unit == "recover":
                call("click", {"x": x, "y": y}, failing=True)          # the kit refuses the first acting call
            call("click", {"x": x, "y": y})
        elif unit == "type":
            x, y = centre(b["field"])
            call("click", {"x": x, "y": y})
            call("type", {"text": e["word"]})
            call("key", {"keys": ["enter"]})
        elif unit == "limit":
            for label in e["labels"][:u.steps]:
                x, y = centre(b[label])
                call("click", {"x": x, "y": y})
        elif unit == "drag":
            (x, y), (tx, ty) = centre(b["handle"]), centre(b["zone"])
            call("drag", {"x": x, "y": y, "to_x": tx, "to_y": ty})
        elif unit == "batch":
            actions = []
            for f in checks.FIELDS:
                x, y = centre(b[f])
                actions += [f"pyautogui.click({x}, {y})", f"pyautogui.write({e['words'][f]!r})"]
            call("step", {"actions": actions})
    finally:
        c.close()
    return {"answer": answer}


def late_reference(u):
    """``late``'s reference: a look by a step (``wait``) until a step's observation was taken with the button up,
    then the click."""
    c = Client(u.socket())
    try:
        target = u.expected["target"]
        for _ in range(u.steps - 1):
            failed, _ = c.call("wait")
            failed_shot, png = c.call("screenshot")
            if failed or failed_shot or not isinstance(png, bytes):
                raise ReferenceFailed("a look by a step did not answer as a correct agent's would")
            if u.up.is_set() and u.pictures[-1][0] > (u.shown or float("inf")):
                break                                # this step's observation was taken with the button up
        else:
            raise ReferenceFailed("the button was not seen within the steps")
        failed, said = c.call("click", dict(zip("xy", centre(u.boxes[target]))))
        if failed:
            raise ReferenceFailed(f"click answered with an error: {str(said)[:200]}")
    finally:
        c.close()
    return {"answer": "done"}


def end(unit, dir, **_):
    """Release the desktop before this attempt is archived for a retry."""
    close_all(shutdown=False)
    return {}


def main():
    atexit.register(REAPER.close)
    atexit.register(close_all)                   # runs first: the units go, then the reaper is let go with nothing
    serve({"units": units, "setup": setup, "task_given": task_given, "score": score, "oracle": oracle, "end": end})


if __name__ == "__main__":
    main()
