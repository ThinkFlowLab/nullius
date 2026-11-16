"""Kit for agent.computer/v1: eight checks on a small real desktop, decided by the desktop's own records.

Basic: ``see``, ``click``, ``type``, ``limit``. Skills: ``drag``, ``scroll``, ``recover``, ``batch``
(``checks.py`` gives each one's screen and verdict).

**A unit.** ``setup`` starts a desktop of the kit's image (``desktop.py``) and the fixture on it, then, in this
driver's process, the port's server over that desktop (``computer_port.Bridge`` and ``Server``, its socket in a
folder of this user's alone) and the kit's loop: a thread that asks the bridge for a step, runs its actions with
``Desktop.act`` and waits the pause after each, as a desktop benchmark's own loop does. The session gets the
server through ``computer_pipe.py`` (``mcp_servers.computer``). ``score`` ends the turn, waits for the loop,
reads the fixture's log, the actions' times and the call log, judges, and removes the desktop and the socket.
``oracle`` is the reference: the right calls, sent to the unit's own server as a client.

**What is left when a unit never reaches ``score``.** Nothing: a new ``setup`` ends the unit before it, and the
end of the driver's input (the run is over, or stopped) ends the one still open. A driver that is killed cannot
do that, so it keeps a reaper (``reaper.py``), started in a session of its own, told of each container and
socket folder as it is made and as it goes: when the driver is gone, however it went, the reaper removes what is
left and exits. Each container also carries the job's label, which a run on the container backend sweeps.
"""
import atexit
import base64
import importlib.util
import json
import os
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

UNITS = checks.CHECKS
PAUSE = 1.0                                  # after each action
PYTHON_SECONDS = 90                          # one python call's reply
AGENT_SECONDS = 600                          # the agent's turn
LOOP_SECONDS = PYTHON_SECONDS + desktop.SETTLE + PAUSE + 30     # the loop's end, once the turn has ended
USE = "Use the tools of the MCP server named computer, given to this session: they show and drive a desktop. "
DONE = " Then reply with the word done."


def prompt(unit, expected):
    if unit == "see":
        return USE + "The screen shows a code of twelve characters. Reply with the code and nothing else."
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
        return (USE + "The list on the screen ends with a code of twelve characters, below what it shows at first. "
                      "Scroll the list to find it, and reply with the code and nothing else.")
    if unit == "batch":
        w = expected["words"]
        return (USE + "Fill the three text fields on the screen: " + ", ".join(f"{w[f]} in the field labelled {f}"
                                                                                for f in checks.FIELDS) + "." + DONE)
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


class KitBridge(port.Bridge):
    """The port's bridge, counting the acting calls sent once every step was taken (``past``)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.past = 0

    def submit(self, call):
        if call.tool in port.ACTING:
            with self.change:
                if self.booked >= self.steps:
                    self.past += 1
        super().submit(call)


class Unit:
    """One unit's desktop, server and loop."""

    def __init__(self, unit, dir):
        self.unit, self.trial = unit, os.path.dirname(os.path.abspath(dir))
        self.values, self.expected = checks.draw(unit)
        self.steps = checks.steps(unit)
        self.log = os.path.join(self.trial, "verifier", "computer-calls.jsonl")
        self.desktop = self.bridge = self.server = self.loop = self.folder = self.boxes = None
        self.final, self.error, self.failed_one = "", None, False

    def start(self, job):
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        if os.path.exists(self.log):
            os.remove(self.log)
        image = desktop.image(os.path.dirname(self.log))
        name = f"nullius-desktop-{os.urandom(6).hex()}"
        REAPER.tell(f"+container {name}")            # before it exists: a kill during the start is covered
        self.desktop = desktop.Desktop.start(image, name, job=job)
        self.desktop.start_fixture(self.values)
        self.boxes = self.desktop.events()[0].get("boxes") or {}
        self.folder = tempfile.mkdtemp(prefix="nullius-computer-")
        REAPER.tell(f"+folder {self.folder}")
        self.bridge = KitBridge(KitDesktop(self.desktop), self.steps, PAUSE, desktop.SCREEN, PYTHON_SECONDS)
        self.server = port.Server(self.bridge, self.socket(), log=self.log)
        self.loop = threading.Thread(target=self.run_loop, name=f"kit-loop-{self.unit}", daemon=True)
        self.loop.start()

    def socket(self):
        return os.path.join(self.folder, "s")

    def run_loop(self):
        """The kit's own loop: a step from the bridge, its actions run, the pause after each; the budget is the
        loop's. ``recover``: the first acting call with input is answered with an error, not run."""
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
                        if self.unit == "recover" and not self.failed_one:
                            self.failed_one = True
                            break                    # not run: the call is answered with an error
                        self.desktop.act(action, PYTHON_SECONDS)
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
        if self.bridge is not None:
            self.bridge.end_turn()
        if self.loop is not None:
            self.loop.join(wait)
            if self.loop.is_alive() and self.error is None:
                self.error = f"the kit's loop did not end within {wait:g} s of the turn's end"
        if self.server is not None:
            self.server.close()
            self.server = None

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


def close_all():
    with OPEN_LOCK:
        held = list(OPEN.values())
        OPEN.clear()
    for u in held:
        try:
            u.close()
        except Exception as e:
            sys.stderr.write(f"computer kit: {u.unit} could not be let go: {e}\n")


def job_of(dir):
    """The job a unit's folder belongs to: <job>/<trial>/workspace."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(dir)))) or None


def units(dataset, **_):
    return {"units": list(UNITS)}


def setup(unit, dir, **_):
    if unit not in UNITS:
        raise ValueError(f"no unit {unit!r}")
    close_all()                                  # one unit at a time in a driver: the one before never reached score
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


def score(unit, dir, transcript, answer, **_):
    with OPEN_LOCK:
        u = OPEN.pop(unit, None)
    if u is None:
        raise ValueError(f"{unit} was not set up")
    try:
        u.end()
        events = u.desktop.events()
        actions = list(u.desktop.actions)
        calls = u.calls()
    finally:
        u.remove()
    message = "\n".join(x for x in (answer or "", u.final) if x)
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


def oracle(unit, dir, **_):
    """The reference: the calls a correct agent makes, sent to the unit's own server, through its desktop, to the
    same scorer; the answer they lead to."""
    with OPEN_LOCK:
        u = OPEN.get(unit)
    if u is None:
        raise ValueError(f"{unit} was not set up")
    e, b, answer = u.expected, u.boxes, "done"
    c = Client(u.socket())
    try:
        c.call("info")
        if unit in ("see", "scroll"):
            if unit == "scroll":
                x, y = centre(b["list"])
                c.call("scroll", {"x": x, "y": y, "dy": 100})
            c.call("screenshot")
            answer = e["code"]
        elif unit in ("click", "recover"):
            x, y = centre(b[e["target"]])
            for _ in range(2 if unit == "recover" else 1):         # recover: the first is answered with an error
                c.call("click", {"x": x, "y": y})
        elif unit == "type":
            x, y = centre(b["field"])
            c.call("click", {"x": x, "y": y})
            c.call("type", {"text": e["word"]})
            c.call("key", {"keys": ["enter"]})
        elif unit == "limit":
            for label in e["labels"][:u.steps]:
                x, y = centre(b[label])
                c.call("click", {"x": x, "y": y})
        elif unit == "drag":
            (x, y), (tx, ty) = centre(b["handle"]), centre(b["zone"])
            c.call("drag", {"x": x, "y": y, "to_x": tx, "to_y": ty})
        elif unit == "batch":
            actions = []
            for f in checks.FIELDS:
                x, y = centre(b[f])
                actions += [f"pyautogui.click({x}, {y})", f"pyautogui.write({e['words'][f]!r})"]
            c.call("step", {"actions": actions})
    finally:
        c.close()
    return {"answer": answer}


def main():
    atexit.register(REAPER.close)
    atexit.register(close_all)                   # runs first: the units go, then the reaper is let go with nothing
    serve({"units": units, "setup": setup, "score": score, "oracle": oracle})


if __name__ == "__main__":
    main()
