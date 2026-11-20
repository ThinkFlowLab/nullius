"""The computer port's shared code (``nullius.standard.computer_port``): the MCP server ``computer`` and its bridge to a
benchmark's loop, on a fake desktop, with a scripted MCP client over the server's socket; the runner's ready file,
lifeline and one stop. Once the agent is given its task, the desktop is reached through acting calls alone: one
observation for each step, fixed as the step ends, which screenshot returns without a new picture; upload and prepare
in the window before the task alone, inside the agent's folder; a step's files. A loop of a few lines stands in for an
upstream's, counting steps as OSWorld's run_single_example does, handing a constant observation, its own (as OSWorld's)
or none (as the kit's). The class UpstreamLoop drives OSWorld's own run_single_example with the bridge, where a
checkout of the upstream is at hand. No VM, no docker, no network, no model."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from nullius.standard import computer_port as cp

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
FOLDER = "/agent"
STUB = "import sys, types\nsys.modules['pyautogui'] = types.SimpleNamespace(FAILSAFE=True)\n"


class FakeDesktop:
    """A desktop of the test's, after the fake DesktopEnv of fixtures/osworld1_fake_desktop: its files under a folder of
    the test's (a desktop path /x is ROOT/x), python run there in a child process with a stand-in pyautogui and the
    agent's folder's paths so placed, the folder made as the bridge asks, one constant screen. Every operation is
    recorded with its kind and start; two at once are counted as an overlap. ``fail`` names the operations that raise;
    ``silent`` makes python give no reply."""

    def __init__(self, root):
        self.root, self.events, self.busy, self.overlaps = root, [], 0, 0
        self.fail, self.silent = set(), False
        self.gate = threading.Event()                            # the loop's actions wait while it is clear
        self.gate.set()
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def op(self, kind, **fields):
        with self.lock:
            self.busy += 1
            self.overlaps += self.busy > 1
            self.events.append(dict(kind=kind, start=time.time(), **fields))
        try:
            time.sleep(0.01)                                     # long enough for two at once to meet
            if kind in self.fail:
                raise RuntimeError(f"{kind} failed in the desktop")
            yield
        finally:
            with self.lock:
                self.busy -= 1

    def at(self, path):
        assert path.startswith("/") and ".." not in path.split("/"), path
        return os.path.join(self.root, path.lstrip("/"))

    def python(self, code):
        made = code == cp.MAKE_FOLDER.format(folder=FOLDER)
        with self.op("python", code="folder" if made else code):
            if self.silent:
                return None
            if made:
                shutil.rmtree(self.at(FOLDER), True)
                os.makedirs(self.at(FOLDER))
                return {"status": "success", "output": "made\n", "error": "", "returncode": 0}
            code = code.replace(repr(FOLDER + "/")[:-1], repr(self.at(FOLDER) + "/")[:-1])
            p = subprocess.run([sys.executable, "-I", "-c", STUB + code], cwd=self.root, capture_output=True,
                               text=True, timeout=30, stdin=subprocess.DEVNULL)
            return {"status": "success" if p.returncode == 0 else "error", "output": p.stdout, "error": p.stderr,
                    "returncode": p.returncode}

    def upload(self, path, data):
        with self.op("upload", path=path):
            with open(self.at(path), "wb") as f:
                f.write(data)

    def read(self, path):
        with self.op("read", path=path):
            try:
                with open(self.at(path), "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return None

    def capture(self):
        with self.op("capture"):
            return PNG

    def password(self):
        return "password"

    def act(self, action):                                       # the loop's own run of an action
        self.gate.wait(30)
        with self.op("action", action=action):
            pass

    def kinds(self):
        return [e["kind"] for e in self.events]


def stand_in_loop(agent, desktop, steps, pause, ran, go):
    """Stands in for an upstream's loop, counting as OSWorld's run_single_example does: after the agent's reset and the
    environment's wait (``go``), one predict a step, its actions run one at a time with the pause after each, until
    DONE or FAIL or the step budget; then the evaluation, of the desktop as it is."""
    agent.reset(None, vm_ip="127.0.0.1")
    go.wait(30)
    done, step = False, 0
    while not done and step < steps:
        _, actions = agent.predict("the task", {"screenshot": PNG})
        for action in actions:
            desktop.act(action)
            time.sleep(pause)
            ran.append(action)
            done = action in ("DONE", "FAIL")
            if done:
                break
        step += 1
    ran.append(("evaluated", step))


class Client:
    """A scripted MCP client: JSON-RPC lines over the server's socket, each answer waited for a bounded time."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf, self.got, self.last = b"", {}, 0

    def send(self, method, params=None):
        self.last += 1
        message = {"jsonrpc": "2.0", "id": self.last, "method": method}
        if params is not None:
            message["params"] = params
        self.sock.sendall((json.dumps(message) + "\n").encode())
        return self.last

    def answer(self, rid, seconds=10.0):
        deadline = time.monotonic() + seconds
        while rid not in self.got:
            while b"\n" in self.buf:
                line, _, self.buf = self.buf.partition(b"\n")
                message = json.loads(line)
                self.got[message.get("id")] = message
            if rid in self.got:
                break
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                raise AssertionError(f"no answer to {rid} within {seconds} s")
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise AssertionError(f"the server closed the connection before answering {rid}")
            self.buf += chunk
        return self.got.pop(rid)

    def ask(self, method, params=None, seconds=10.0):
        return self.answer(self.send(method, params), seconds)

    def start(self, tool, **arguments):
        return self.send("tools/call", {"name": tool, "arguments": arguments})

    def result(self, rid, seconds=10.0):
        return self.answer(rid, seconds)["result"]

    def call(self, tool, seconds=10.0, **arguments):
        return self.result(self.start(tool, **arguments), seconds)

    def initialize(self):
        return self.ask("initialize", {"protocolVersion": cp.PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t"}})

    def close(self):
        self.sock.close()


def value(result):
    return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))


def image(result):
    found = [base64.b64decode(c["data"]) for c in result["content"] if c["type"] == "image"]
    return found[0] if found else None


def error(result):
    assert result["isError"], result
    return result["content"][0]["text"]


class Port(unittest.TestCase):
    """A unit: a fake desktop, the bridge over it, the server at a socket of the test's and the stand-in loop in a
    thread, as a runner holds them. ``given``: the task is given at once (the window before it closed), as the run
    does just after the agent's session opens."""

    def start(self, steps=3, pause=0.05, held=None, go=True, given=True):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = os.path.join(self.tmp, "desktop")
        os.mkdir(root)
        self.desktop = FakeDesktop(root)
        self.first = threading.Event()
        self.bridge = cp.Bridge(self.desktop, steps, pause, (1920, 1080), FOLDER, on_first=self.first.set, held=held,
                                window=True)
        if given:
            self.bridge.close_window()
        folder = os.path.join(self.tmp, "socket")
        os.mkdir(folder, 0o700)
        self.socket, self.log = os.path.join(folder, "s"), os.path.join(self.tmp, "calls.jsonl")
        self.server = cp.Server(self.bridge, self.socket, self.log)
        self.addCleanup(self.server.close)
        self.ran, self.go = [], threading.Event()
        if go:
            self.go.set()
        self.loop = threading.Thread(target=self._loop, args=(steps, pause), daemon=True)
        self.loop.start()
        self.addCleanup(self.bridge.finish)
        return self.client()

    def _loop(self, steps, pause):
        try:
            stand_in_loop(self.bridge, self.desktop, steps, pause, self.ran, self.go)
        finally:
            self.bridge.finish()

    def client(self):
        c = Client(self.socket)
        self.addCleanup(c.close)
        c.initialize()
        return c

    def evaluated(self, seconds=10):
        self.loop.join(seconds)
        self.assertFalse(self.loop.is_alive(), "the loop did not end")
        return self.ran[-1][1]

    def logged(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]


class Tools(Port):
    def test_initialize_and_the_tool_list_answer_before_the_desktop_is_ready_and_state_the_limits(self):
        c = self.start(steps=3, go=False)                         # the loop is in the environment's wait
        init = c.initialize()["result"]
        self.assertEqual(init["serverInfo"]["name"], "computer")
        self.assertIn("1920 by 1080 pixels", init["instructions"])
        self.assertIn("There are 3 steps in all", init["instructions"])
        self.assertEqual([t["name"] for t in c.ask("tools/list")["result"]["tools"]],
                         ["info", "screenshot", "upload", "prepare", "click", "move", "drag", "scroll", "type", "key",
                          "wait", "step", "done", "fail"])
        self.assertEqual(value(c.call("info")), {"screen": [1920, 1080], "steps": 3, "steps_left": 3, "pause": 0.05,
                                                 "password": "password", "folder": FOLDER})
        shot = c.start("screenshot")
        with self.assertRaises(AssertionError):                   # held until the loop's first predict
            c.answer(shot, 0.5)
        self.assertEqual(self.desktop.events, [])
        self.go.set()
        self.assertEqual(image(c.result(shot)), PNG)
        self.assertTrue(self.first.is_set())

    def test_the_reading_tools_take_no_step(self):
        c = self.start(steps=3, given=False)
        self.assertEqual(image(c.call("screenshot")), PNG)
        self.assertEqual(value(c.call("prepare", actions=[], files=["missing.txt"])), {"files": {"missing.txt": None}})
        self.assertEqual(value(c.call("upload", path="a.txt", data=base64.b64encode(b"abc").decode())), {"size": 3})
        self.assertEqual(value(c.call("prepare", actions=[], files=["a.txt"])),
                         {"files": {"a.txt": base64.b64encode(b"abc").decode()}})
        self.assertEqual(value(c.call("info"))["steps_left"], 3)
        self.assertEqual(self.ran, [])

    def test_each_pointer_and_keyboard_tool_is_one_action_answered_with_the_screen(self):
        c = self.start(steps=8)
        calls = [("click", {"x": 10, "y": 20}, "pyautogui.click(10, 20, clicks=1, button='left')"),
                 ("click", {"x": 5, "y": 6, "button": "right", "count": 2}, "pyautogui.click(5, 6, clicks=2, button='right')"),
                 ("move", {"x": 100, "y": 200}, "pyautogui.moveTo(100, 200)"),
                 ("drag", {"x": 1, "y": 2, "to_x": 30, "to_y": 40},
                  "pyautogui.moveTo(1, 2); pyautogui.dragTo(30, 40, duration=0.5, button='left')"),
                 ("scroll", {"x": 50, "y": 60, "dy": 3, "dx": -2},
                  "pyautogui.scroll(-3, x=50, y=60); pyautogui.hscroll(-2, x=50, y=60)"),
                 ("type", {"text": "it's 'fine'\n"}, "pyautogui.write(\"it's 'fine'\\n\")"),
                 ("key", {"keys": ["Ctrl", "S"]}, "pyautogui.hotkey('ctrl', 'S')"),
                 ("wait", {}, "WAIT")]
        for i, (tool, args, _) in enumerate(calls, 1):
            got = c.call(tool, **args)
            self.assertFalse(got["isError"], got)
            self.assertEqual(value(got), {"steps_left": 8 - i, "done": i == 8}, tool)
            self.assertEqual(image(got), PNG, tool)
        self.assertEqual(self.evaluated(), 8)
        self.assertEqual(self.ran[:-1], [action for _, _, action in calls])

    def test_step_runs_its_actions_in_order_as_one_step_and_an_empty_list_is_a_step(self):
        c = self.start(steps=3)
        got = c.call("step", actions=["pyautogui.press('a')", "WAIT", "pyautogui.press('b')"], response="pressing")
        self.assertEqual((value(got), image(got)), ({"steps_left": 2, "done": False}, None))
        got = c.call("step", actions=[], screenshot=True)
        self.assertEqual((value(got), image(got)), ({"steps_left": 1, "done": False}, PNG))
        self.assertEqual(self.ran, ["pyautogui.press('a')", "WAIT", "pyautogui.press('b')"])

    def test_wrong_arguments_are_refused_at_once_and_take_no_step(self):
        c = self.start(steps=3)
        for tool, args, words in [("click", {"x": 1920, "y": 0}, "x is from 0 to 1919"),
                                  ("click", {"x": 1, "y": 1, "button": "side"}, "button is one of left, right, middle"),
                                  ("click", {"x": True, "y": 1}, "x is an integer"),
                                  ("key", {"keys": []}, "keys holds 1 to 8 items"),
                                  ("type", {"text": ""}, "text is a text, not empty"),
                                  ("scroll", {"x": 1, "y": 1, "dy": 0}, "dy or dx is not 0"),
                                  ("step", {"actions": ["DONE"]}, "the task's end is the tool done or fail"),
                                  ("step", {"actions": ["FAIL"], "end": "fail"}, "the task's end is the tool done or fail"),
                                  ("step", {"actions": "pyautogui.click()"}, "actions is a list"),
                                  ("step", {"actions": [], "end": "stop"}, "end is one of done, fail"),
                                  ("step", {"actions": [], "end": "DONE"}, "end is one of done, fail"),
                                  ("step", {"actions": [], "end": True}, "end is a text"),
                                  ("upload", {"path": "a", "data": "!!"}, "data is base64"),
                                  ("upload", {"path": "a", "sha256": "ABC"}, "sha256 is 64 lowercase")]:
            self.assertIn(words, error(c.call(tool, **args)), tool)
        self.assertEqual(value(c.call("info"))["steps_left"], 3)
        self.assertEqual(self.ran, [])


class Budget(Port):
    def test_the_loop_counts_one_step_for_each_acting_call_and_nothing_else(self):
        c = self.start(steps=3)
        c.call("click", x=1, y=1)
        c.call("screenshot")
        c.call("step", actions=["pyautogui.press('a')", "pyautogui.press('b')"])
        c.call("info")
        last = c.call("key", keys=["enter"])
        self.assertEqual(value(last), {"steps_left": 0, "done": True})
        self.assertEqual(self.evaluated(), 3)
        self.assertEqual(self.ran[:-1], ["pyautogui.click(1, 1, clicks=1, button='left')", "pyautogui.press('a')",
                                         "pyautogui.press('b')", "pyautogui.hotkey('enter')"])

    def test_every_call_but_info_is_refused_past_the_budget_and_the_desktop_is_not_touched(self):
        c = self.start(steps=1)
        c.call("click", x=1, y=1)
        self.evaluated()
        touched = len(self.desktop.events)
        for tool, args in [("click", {"x": 1, "y": 1}), ("screenshot", {}), ("upload", {"path": "a", "data": "YQ=="}),
                           ("prepare", {"actions": []}), ("done", {})]:
            self.assertEqual(error(c.call(tool, **args)), "the steps are used up", tool)
        self.assertEqual(value(c.call("info"))["steps_left"], 0)
        self.assertEqual(len(self.desktop.events), touched)

    def test_acting_calls_sent_together_past_the_budget_are_refused_at_once(self):
        c = self.start(steps=2)
        self.desktop.gate.clear()                                # the first click is held in the loop
        ids = [c.start("click", x=i, y=i) for i in (1, 2, 3)]
        self.assertEqual(error(c.result(ids[2])), "the steps are used up")
        self.assertEqual(c.got, {}, "a click was answered while the first was held")
        self.assertNotIn("action", self.desktop.kinds())
        self.desktop.gate.set()
        self.assertEqual([value(c.result(i))["steps_left"] for i in ids[:2]], [1, 0])
        self.assertEqual(self.evaluated(), 2)
        self.assertEqual(len(self.ran), 3)


class Held(Port):
    def test_an_acting_call_is_answered_once_its_actions_and_their_pause_have_run(self):
        c = self.start(steps=3, pause=0.4)
        rid = c.start("click", x=3, y=4)
        got = c.result(rid)
        answered = time.time()
        acted = next(e["start"] for e in self.desktop.events if e["kind"] == "action")
        self.assertGreaterEqual(answered - acted, 0.4)
        self.assertEqual(self.desktop.kinds(), ["python", "action", "capture"])     # the agent's folder made first
        self.assertEqual(image(got), PNG)

    def test_calls_sent_during_a_step_wait_for_it_one_desktop_operation_at_a_time_in_their_order(self):
        c = self.start(steps=3, pause=0.3)
        ids = [c.start("click", x=1, y=1), c.start("screenshot"), c.start("step", actions=[], files=["none"]),
               c.start("move", x=2, y=2)]
        order = []
        for rid in ids:
            c.answer(rid)
            order.append(rid)
        self.assertEqual(self.desktop.kinds(), ["python", "action", "capture", "read", "action", "capture"])
        self.assertEqual(self.desktop.overlaps, 0)

    def test_the_answers_come_in_the_order_the_desktop_ran_the_calls(self):
        c = self.start(steps=3, pause=0.3)
        first, second = c.start("click", x=1, y=1), c.start("screenshot")
        deadline = time.monotonic() + 10
        while len(c.got) < 2 and time.monotonic() < deadline:   # read both answers as they come
            with contextlib.suppress(AssertionError):
                c.answer(-1, 0.1)
        arrived = list(c.got)
        self.assertEqual(arrived, [first, second])


class TurnEnd(Port):
    def test_a_turn_that_ends_with_no_done_ends_the_task_as_the_desktop_is(self):
        c = self.start(steps=5)
        c.call("click", x=1, y=1)
        waiting = c.start("screenshot")
        c.answer(waiting)
        t0 = time.monotonic()
        self.bridge.end_turn()                                   # the loop is waiting for the agent's next call
        self.assertEqual(self.evaluated(), 5)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(self.ran, ["pyautogui.click(1, 1, clicks=1, button='left')", ("evaluated", 5)])
        self.assertEqual(error(c.call("click", x=1, y=1)), "the agent's turn has ended")

    def test_done_ends_the_task_and_a_later_turn_end_does_nothing(self):
        c = self.start(steps=5)
        c.call("click", x=1, y=1)
        self.assertEqual(value(c.call("done", message="finished")), {"steps_left": 3, "done": True})
        self.assertEqual(self.evaluated(), 2)
        self.assertEqual(self.ran[-2:], ["DONE", ("evaluated", 2)])
        self.bridge.end_turn()
        self.assertEqual(self.ran[-2:], ["DONE", ("evaluated", 2)])
        self.assertIn("the task has ended (done)", error(c.call("screenshot")))

    def test_fail_is_the_last_step(self):
        c = self.start(steps=5)
        self.assertEqual(value(c.call("fail", reason="no such menu")), {"steps_left": 4, "done": True})
        self.assertEqual(self.evaluated(), 1)
        self.assertEqual(self.ran, ["FAIL", ("evaluated", 1)])
        self.assertIn("the task has ended (fail)", error(c.call("click", x=1, y=1)))

    def test_a_step_with_an_end_runs_its_actions_then_ends_the_task_in_that_one_step(self):
        for end, final in (("done", "DONE"), ("fail", "FAIL")):
            with self.subTest(end=end):
                c = self.start(steps=5)
                c.call("click", x=1, y=1)
                got = c.call("step", actions=["pyautogui.press('a')", "WAIT"], response="last", end=end)
                self.assertEqual((value(got), image(got)), ({"steps_left": 3, "done": True}, None))
                self.assertEqual(self.evaluated(), 2)                    # the click's step, then the step and its end
                self.assertEqual(self.ran, ["pyautogui.click(1, 1, clicks=1, button='left')", "pyautogui.press('a')",
                                            "WAIT", final, ("evaluated", 2)])
                self.assertEqual(error(c.call("click", x=1, y=1)), f"the task has ended ({end})")
                self.assertEqual(error(c.call("screenshot")), f"the task has ended ({end})")
                self.assertEqual(value(c.call("info"))["steps_left"], 3)

    def test_a_step_with_an_end_and_no_action_is_the_end_alone(self):
        c = self.start(steps=3)
        got = c.call("step", actions=[], end="fail", screenshot=True)
        self.assertEqual((value(got), image(got)), ({"steps_left": 2, "done": True}, PNG))
        self.assertEqual(self.evaluated(), 1)
        self.assertEqual(self.ran, ["FAIL", ("evaluated", 1)])

    def test_a_step_with_an_end_on_the_last_step_ends_by_its_end_not_by_the_budget(self):
        c = self.start(steps=2)
        c.call("click", x=1, y=1)
        self.assertEqual(value(c.call("step", actions=["pyautogui.press('a')"], end="done")),
                         {"steps_left": 0, "done": True})
        self.assertEqual(self.evaluated(), 2)
        self.assertEqual(self.ran[-2:], ["DONE", ("evaluated", 2)])
        self.assertEqual(error(c.call("click", x=1, y=1)), "the task has ended (done)")

    def test_a_turn_end_on_the_lifeline_ends_the_turn_and_its_close_stops_the_runner(self):
        self.start(steps=2)
        read, write = os.pipe()
        with mock.patch.object(cp.os, "kill") as kill:
            reader = threading.Thread(target=cp.lifeline, args=(read, self.bridge), daemon=True)
            reader.start()
            os.write(write, b"something else\n" + cp.TURN_END + b"\n")
            self.assertEqual(self.evaluated(), 2)               # the turn's end reached the bridge
            kill.assert_not_called()
            os.close(write)
            reader.join(5)
        os.close(read)
        self.assertFalse(reader.is_alive())
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


class Errors(Port):
    def test_a_failed_desktop_call_answers_with_its_words_and_the_next_succeeds(self):
        c = self.start(steps=2, given=False)
        self.desktop.fail = {"upload"}
        self.assertEqual(error(c.call("upload", path="a", data="YQ==")),
                         "the desktop failed upload: RuntimeError: upload failed in the desktop")
        self.desktop.fail = set()
        self.assertEqual(value(c.call("upload", path="a", data="YQ==")), {"size": 1})
        self.bridge.close_window()
        self.desktop.fail = {"capture"}
        got = c.call("click", x=1, y=1)                          # the step ran; its screen could not be taken
        self.assertIn("the step was run; the screen could not be taken", error(got))
        self.assertEqual(value(c.call("info"))["steps_left"], 1)

    def test_an_unknown_tool_a_line_that_is_no_json_and_an_unknown_method_are_answered(self):
        c = self.start()
        self.assertEqual(c.ask("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        c.sock.sendall(b"not json\n")
        self.assertEqual(c.answer(None)["error"]["code"], -32700)
        self.assertEqual(c.ask("sampling/x")["error"]["code"], -32601)
        self.assertEqual(c.ask("ping")["result"], {})
        c.sock.sendall(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        self.assertEqual(image(c.call("screenshot")), PNG)

    def test_a_loop_that_fails_during_a_step_has_the_call_answered(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        desktop = FakeDesktop(self.tmp)
        bridge = cp.Bridge(desktop, 3, 0, (100, 100), FOLDER)
        answers = []
        call = cp.Call("click", {"x": 1, "y": 1}, lambda c, v, i, e: answers.append(e))
        cp.checked(call, bridge.screen, bridge.folder)
        bridge.submit(call)
        _, actions = bridge.predict("x", {})
        next(iter(actions))                                      # the loop takes the action, then fails in it
        bridge.finish()
        self.assertEqual(answers, ["the task has ended"])


class Upload(Port):
    def test_an_upload_in_chunks_is_the_file_with_its_size_so_far(self):
        c = self.start(given=False)
        parts = [b"first", b"-second", b"-third"]
        sizes = [value(c.call("upload", path="f.bin", data=base64.b64encode(p).decode(), append=i > 0))["size"]
                 for i, p in enumerate(parts)]
        self.assertEqual(sizes, [5, 12, 18])
        self.assertEqual(value(c.call("prepare", actions=[], files=["f.bin"])),
                         {"files": {"f.bin": base64.b64encode(b"".join(parts)).decode()}})
        self.assertEqual(sorted(os.listdir(self.desktop.at(FOLDER))), ["f.bin"])
        self.assertEqual(self.ran, [])

    def test_a_chunk_over_four_mib_is_refused(self):
        c = self.start(given=False)
        big = base64.b64encode(b"x" * (cp.CHUNK_BYTES + 1)).decode()
        self.assertEqual(error(c.call("upload", path="f.bin", data=big)), f"data is at most {cp.CHUNK_BYTES} bytes a call")
        self.assertNotIn("upload", self.desktop.kinds())

    def test_a_file_sent_once_for_a_run_reaches_the_next_units_desktop_by_its_sha256(self):
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run, True)
        content = os.urandom(3000)
        digest = hashlib.sha256(content).hexdigest()
        c = self.start(held=cp.Held(run), given=False)          # the first unit sends the bytes
        self.assertIn("the run holds no file of that sha256", error(c.call("upload", path="tool.bin", sha256=digest)))
        c.call("upload", path="tool.bin", data=base64.b64encode(content[:1000]).decode(), sha256=digest)
        c.call("upload", path="tool.bin", data=base64.b64encode(content[1000:]).decode(), append=True)
        self.assertEqual(os.listdir(run), [digest])
        second = Port("run")                                     # the next unit of the same run sends none
        second.addCleanup = self.addCleanup
        c2 = second.start(held=cp.Held(run), given=False)
        self.assertEqual(value(c2.call("upload", path="tool.bin", sha256=digest)), {"size": 3000})
        with open(second.desktop.at(FOLDER + "/tool.bin"), "rb") as f:
            self.assertEqual(f.read(), content)
        self.assertEqual(second.desktop.kinds(), ["python", "upload"])                # the folder made, the upload
        with open(os.path.join(run, digest), "r+b") as f:       # a kept file that changed is not taken
            f.write(b"\0")
        self.assertIn("the run holds no file of that sha256", error(c2.call("upload", path="t2.bin", sha256=digest)))

    def test_chunks_that_do_not_hash_to_the_sha256_given_are_not_kept(self):
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run, True)
        c = self.start(held=cp.Held(run), given=False)
        c.call("upload", path="t.bin", data=base64.b64encode(b"abc").decode(), sha256="0" * 64)
        self.assertEqual(os.listdir(run), [])

    def test_with_no_folder_for_the_run_an_upload_by_sha256_alone_is_refused(self):
        c = self.start(given=False)
        self.assertEqual(error(c.call("upload", path="t.bin", sha256="0" * 64)),
                         "the run holds no file of that sha256: send its data")


class Connections(Port):
    def test_several_connections_share_the_task(self):
        first = self.start(steps=3)
        first.call("click", x=1, y=1)
        first.close()
        second = self.client()
        self.assertEqual(value(second.call("info"))["steps_left"], 2)
        third = self.client()
        a, b = second.start("click", x=2, y=2), third.start("click", x=3, y=3)
        self.assertEqual(sorted([value(second.result(a))["steps_left"], value(third.result(b))["steps_left"]]), [0, 1])
        self.assertEqual(self.evaluated(), 3)

    def test_a_connection_whose_input_ends_is_closed_once_its_calls_are_answered(self):
        c = self.start(steps=3, pause=0.3)
        rid = c.start("click", x=1, y=1)
        c.sock.shutdown(socket.SHUT_WR)
        self.assertEqual(value(c.result(rid))["steps_left"], 2)
        c.sock.settimeout(5)
        self.assertEqual(c.sock.recv(10), b"")


class CallLog(Port):
    def test_every_tool_call_is_recorded_with_its_tool_times_and_failure_and_nothing_else(self):
        c = self.start(steps=3, pause=0.3)
        c.call("info")
        c.call("screenshot")
        c.call("step", actions=["print('a-secret-text')"])
        c.call("click", x=1, y=1)
        c.call("click", x=99999, y=1)
        c.ask("tools/call", {"name": "nope"})
        c.call("done")
        c.call("screenshot")
        lines = self.logged()
        self.assertEqual([(x["tool"], x["isError"]) for x in lines],
                         [("info", False), ("screenshot", False), ("step", False), ("click", False), ("click", True),
                          (None, True), ("done", False), ("screenshot", True)])
        for x in lines:
            self.assertEqual(set(x), {"tool", "start", "end", "isError"})
            self.assertLessEqual(x["start"], x["end"])
        self.assertGreaterEqual(lines[3]["end"] - lines[3]["start"], 0.3)       # held while the loop ran it
        with open(self.log) as f:
            self.assertNotIn("a-secret-text", f.read())


RUNNER_CHILD = """
import os, signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
from nullius.standard import computer_port as cp
mode = sys.argv[2]
stop = cp.Stop(float(sys.argv[3]), last=lambda: print("last resort", flush=True))
stop.install()
if mode == "lifeline":
    try:                                    # as in a runner: the clean-up holds before the lifeline can stop it
        threading.Thread(target=cp.lifeline, args=(int(sys.argv[4]),), daemon=True).start()
        print("waiting", flush=True)
        time.sleep(30)
    finally:
        print("stopped", flush=True)
elif mode == "deferred":
    try:
        with stop.deferred():
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.3)
            print("part ended", flush=True)
        print("after the part", flush=True)
    finally:
        print("cleaned up", flush=True)
elif mode == "hung":
    with stop.deferred():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
elif mode == "stderr-full":
    stop.last = lambda: (sys.stderr.write("x" * 4194304), sys.stderr.flush())
    with stop.deferred():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
elif mode == "stalled-last":
    stop.last = lambda: time.sleep(30)
    with stop.deferred():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
"""


class Runner(unittest.TestCase):
    """The ready file, the lifeline and the one stop, each in a child process of the test's."""

    def child(self, *args, pass_fds=()):
        p = subprocess.Popen([sys.executable, "-c", RUNNER_CHILD, APP, *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and os.killpg(p.pid, signal.SIGKILL), p.wait()))
        return p

    def test_the_ready_file_is_the_users_alone_and_written_whole(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = os.umask(0o022)
        try:
            cp.ready(os.path.join(tmp, "ready.json"), ok=True, password="pw")
        finally:
            os.umask(old)
        self.assertEqual(os.stat(os.path.join(tmp, "ready.json")).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(tmp), ["ready.json"])
        with open(os.path.join(tmp, "ready.json")) as f:
            self.assertEqual(json.load(f), {"ok": True, "password": "pw"})

    def test_the_lifelines_end_stops_the_runner_once(self):
        read, write = os.pipe()
        p = self.child("lifeline", "30", str(read), pass_fds=(read,))
        os.close(read)
        ready = select.select([p.stdout], [], [], 10)[0]        # a bounded wait: the child may stall
        self.assertTrue(ready and os.read(p.stdout.fileno(), 8) == b"waiting\n", "the child did not start")
        os.close(write)
        out, _ = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out), (1, b"stopped\n"))

    def test_a_stop_during_a_deferred_part_lands_at_its_end_and_a_second_does_not_cut_it(self):
        p = self.child("deferred", "30")
        out, _ = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out), (1, b"part ended\ncleaned up\n"))

    def test_a_clean_up_that_hangs_takes_the_last_resort(self):
        p = self.child("hung", "0.3")
        t0 = time.monotonic()
        out, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out), (1, b"last resort\n"))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn(b"did not end within 0.3 s of the stop", err)

    def test_a_last_resort_exits_when_stderr_is_a_pipe_nobody_reads(self):
        p = self.child("stderr-full", "0.05")                    # its stderr is a pipe the test does not read
        try:
            self.assertEqual(p.wait(timeout=5), 1)
        except subprocess.TimeoutExpired:
            self.fail("the last resort waited on stderr")

    def test_a_last_resort_whose_own_callback_hangs_still_exits(self):
        p = self.child("stalled-last", "0.05")
        t0 = time.monotonic()
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("the last resort waited for its callback")
        self.assertEqual(p.returncode, 1)
        self.assertLess(time.monotonic() - t0, 3)


class FakeConnection:
    """A socket of the test's for a Wire: ``chunks`` to read, then the end; what is sent, parsed."""

    def __init__(self, chunks=()):
        self.chunks, self.sent, self.closed = iter(chunks), [], False

    def recv(self, n):
        return next(self.chunks, b"")

    def sendall(self, data):
        self.sent.append(json.loads(data))

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class Bare(unittest.TestCase):
    """A bridge and a server with no socket and no loop: the calls are made and served by the test."""

    def bridge(self, steps=5, held=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.desktop = FakeDesktop(tmp)
        os.makedirs(self.desktop.at(FOLDER))
        b = cp.Bridge(self.desktop, steps, 0.01, (100, 100), FOLDER, held=held)
        self.addCleanup(b.finish)
        return b

    def server(self, b):
        s = cp.Server.__new__(cp.Server)                         # no socket bound
        s._state(b, os.path.join(tempfile.gettempdir(), "no-such-socket"), None)
        s.records = []
        s.record = s.records.append
        return s

    def call(self, b, tool, **args):
        answers = []
        call = cp.Call(tool, args, lambda c, v, i, e: answers.append((v, i, e)))
        call.answers = answers
        cp.checked(call, b.screen, b.folder)
        b.submit(call)
        return call

    def wire(self, conn=None):
        w = cp.Wire(conn if conn is not None else FakeConnection())
        self.addCleanup(w.close)
        return w

    def upload(self, b, **args):
        call = cp.Call("upload", args, None)
        cp.checked(call, b.screen, b.folder)
        return b._upload(call)


class Descriptions(Bare):
    def test_acting_answers_and_screenshot_coordinates_are_described(self):
        listed = {t["name"]: t for t in cp.listed((100, 100))}
        for name in cp.ACTING:
            with self.subTest(tool=name):
                description = listed[name]["description"]
                if name in cp.FINAL:
                    self.assertIn("Returns {steps_left, done}, with no image", description)
                elif name == "step":
                    self.assertIn("Returns the step's observation as a PNG image when screenshot is true", description)
                else:
                    self.assertIn("Returns the screen as a PNG image after the pause", description)
        for name in ("click", "move", "drag", "scroll"):
            props = listed[name]["inputSchema"]["properties"]
            for point, edge in (("x", "left"), ("y", "top"), ("to_x", "left"), ("to_y", "top")):
                if point in props:
                    with self.subTest(tool=name, point=point):
                        self.assertIn("Pixels of the screenshot at the screen's size", props[point]["description"])
                        self.assertIn(f"from its {edge} edge", props[point]["description"])
        self.assertIn("before the first pointing action", listed["screenshot"]["description"])
        instructions = self.bridge().instructions()
        self.assertIn("x and y in screenshot pixels at the screen's size, from its top left corner", instructions)
        self.assertIn("Look at a screenshot before the first pointing action", instructions)
        self.assertIn("After the actions and their 0.01 s pauses", instructions)
        self.assertIn("click, move, drag, scroll, type, key and wait return the step's observation, the screen as a PNG "
                      "image", instructions)
        self.assertIn("step does when screenshot is true", instructions)
        self.assertIn("done and fail answer before they run, with no image", instructions)
        self.assertIn("A step with end, done or fail, runs its actions and then ends the task, in that one step",
                      instructions)
        self.assertIn("With end, done or fail, the actions run and then the task ends, in this one step",
                      listed["step"]["description"])
        self.assertEqual(listed["step"]["inputSchema"]["properties"]["end"]["enum"], ["done", "fail"])
        self.assertEqual(listed["step"]["inputSchema"]["required"], ["actions"])


class Wires(Bare):
    def test_a_client_that_stops_reading_holds_up_neither_the_loop_nor_the_close(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Stalled(FakeConnection):
            def sendall(self, data):
                entered.set()
                release.wait(10)
                raise OSError("let go")

        b = self.bridge()
        wire = self.wire(Stalled())
        self.server(b).call(1, {"name": "screenshot"}, wire)
        served = threading.Thread(target=b._serve, args=(b.calls.popleft(),), daemon=True)
        served.start()
        served.join(2)
        self.assertFalse(served.is_alive(), "the loop's thread waited for the client to read")
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=wire.close, daemon=True)
        closer.start()
        closer.join(2)
        self.assertFalse(closer.is_alive(), "the close waited for a write in progress")

    def test_answers_left_unread_past_the_bound_close_the_connection(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Stalled(FakeConnection):
            def sendall(self, data):
                release.wait(10)

        wire = self.wire(Stalled())
        with mock.patch.object(cp, "OUT_BYTES", 1000):
            self.assertTrue(wire.send({"x": "a" * 400}))           # taken by the writer, which stalls
            self.assertTrue(until(lambda: not wire.out))
            self.assertTrue(wire.send({"x": "b" * 400}))
            self.assertFalse(wire.send({"x": "c" * 400}))
        self.assertTrue(wire.closed and wire.conn.closed)

    def test_a_connection_accepted_as_the_server_closes_is_closed(self):
        accepted, release, shut = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(shut.set)

        class Pending(FakeConnection):                           # a client that sends nothing and stays
            def recv(self, n):
                shut.wait(10)
                return b""

            def shutdown(self, how):
                shut.set()

        conn = Pending()

        class Listener:
            closed = False

            def accept(self):
                if self.closed:
                    raise OSError("closed")
                accepted.set()
                release.wait(10)                                 # a connection taken in, not yet registered
                return conn, None

            def shutdown(self, how):
                self.closed = True

            def close(self):
                self.closed = True

        s = self.server(self.bridge())
        s.listener = Listener()
        acceptor = threading.Thread(target=s._accept, daemon=True)
        acceptor.start()
        self.assertTrue(accepted.wait(5))
        s.close()
        release.set()
        acceptor.join(5)
        self.assertTrue(conn.closed, "a connection accepted as the server closed outlived it")
        self.assertEqual(s.wires, set())

    def test_a_complete_message_over_the_bound_is_refused(self):
        conn = FakeConnection([b" " * 127, b"{}\n"])
        with mock.patch.object(cp, "LINE_BYTES", 128):
            self.assertEqual(list(self.wire(conn).lines()), [])
        self.assertTrue(until(lambda: conn.sent))
        self.assertEqual(conn.sent[0]["error"]["code"], -32600)

    def test_every_outgoing_frame_is_within_the_bound(self):
        class Raw(FakeConnection):
            def sendall(self, data):
                self.sent.append(data)

        with mock.patch.object(cp, "LINE_BYTES", 2000):
            for method in ("tools/list", "initialize", "no/such"):
                with self.subTest(method=method):
                    conn = Raw([(json.dumps({"jsonrpc": "2.0", "id": "x" * 1900, "method": method}) + "\n").encode()])
                    self.server(self.bridge())._serve(self.wire(conn))
                    self.assertTrue(until(lambda: conn.sent))
                    self.assertTrue(all(len(frame) <= 2000 for frame in conn.sent), [len(f) for f in conn.sent])

    def test_json_too_deep_to_read_is_answered_and_the_connection_closed(self):
        s = self.server(self.bridge())
        conn = FakeConnection([b"[" * 100000 + b"]" * 100000 + b"\n"])
        s._serve(self.wire(conn))
        self.assertTrue(until(lambda: conn.closed), "the connection stayed open")
        self.assertEqual(conn.sent[0]["error"]["code"], -32700)


class Envelope(Bare):
    VALID = {"info": {}, "screenshot": {}, "upload": {"path": "a", "data": "YQ=="}, "prepare": {"actions": ["WAIT"]},
             "click": {"x": 1, "y": 2}, "move": {"x": 1, "y": 2},
             "drag": {"x": 1, "y": 2, "to_x": 3, "to_y": 4}, "scroll": {"x": 1, "y": 2, "dy": 1},
             "type": {"text": "a"}, "key": {"keys": ["a"]}, "wait": {}, "step": {"actions": ["WAIT"]}, "done": {},
             "fail": {}}

    def test_a_number_past_any_integer_is_refused_and_recorded(self):
        b = self.bridge()
        s = self.server(b)
        s.handle(json.loads('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "click", '
                            '"arguments": {"x": 1e999, "y": 1}}}'), self.wire())
        self.assertEqual([(r["tool"], r["isError"]) for r in s.records], [("click", True)])
        self.assertEqual(b.booked, 0)

    def test_nan_and_infinity_are_no_json_and_take_no_step(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                b = self.bridge()
                conn = FakeConnection([('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wait", '
                                        '"arguments": {"extra": %s}}}\n' % constant).encode()])
                self.server(b)._serve(self.wire(conn))
                self.assertTrue(until(lambda: conn.sent))
                self.assertEqual((conn.sent[0]["error"]["code"], b.booked), (-32700, 0))

    def test_an_argument_the_schema_does_not_list_is_refused(self):
        b = self.bridge()
        s = self.server(b)
        s.call(1, {"name": "wait", "arguments": {"extra": 1}}, self.wire())
        s.call(2, {"name": "click", "arguments": {"x": 1, "y": 1, "clicks": 2}}, self.wire())
        self.assertEqual(([r["isError"] for r in s.records], b.booked), ([True, True], 0))
        self.assertEqual([t["inputSchema"]["additionalProperties"] for t in cp.listed((100, 100))], [False] * 14)

    def test_the_patterns_mean_the_same_in_python_and_in_the_listed_schema(self):
        for actions in (["\ufeff"], ["\u0085"], ["\u00a0"], ["a"]):              # not blank, read alike either way
            cp.checked(cp.Call("step", {"actions": actions}, None), (100, 100), FOLDER)
        for actions in ([" \t\r\n\f\v"], ["DONE\n"], [" FAIL "]):
            with self.subTest(actions=actions), self.assertRaises(cp.Refused):
                cp.checked(cp.Call("step", {"actions": actions}, None), (100, 100), FOLDER)
        with self.assertRaisesRegex(cp.Refused, "64 lowercase"):                     # $ is the text's end
            cp.checked(cp.Call("upload", {"path": "f", "sha256": "0" * 64 + "\n"}, None), (100, 100), FOLDER)
        patterns = json.dumps(cp.listed((100, 100)))
        self.assertNotRegex(patterns, r"\\\\[sSdDwWbB]", "a class whose meaning differs between engines")

    def test_a_request_that_is_not_json_rpc_2_takes_no_step(self):
        b = self.bridge()
        s = self.server(b)
        for m in [{"jsonrpc": "1.0", "id": 1, "method": "tools/call", "params": {"name": "wait"}},
                  {"jsonrpc": "2.0", "id": [], "method": "tools/call", "params": {"name": "wait"}},
                  {"jsonrpc": "2.0", "id": True, "method": "tools/call", "params": {"name": "wait"}},
                  {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [{"name": "wait"}]},
                  {"id": 1, "method": "tools/call", "params": {"name": "wait"}}]:
            self.assertEqual(s.handle(m, self.wire())["error"]["code"], -32600, m)
        self.assertEqual((b.booked, len(b.calls), s.records), (0, 0, []))

    def test_calls_against_their_schema_are_refused_before_they_are_queued(self):
        for tool, args in [("click", {"x": 1.5, "y": 1}), ("step", {"actions": [], "screenshot": "yes"}),
                           ("wait", []), ("upload", {"path": "a", "data": "!!"}), ("info", [])]:
            with self.subTest(tool=tool, args=args):
                b = self.bridge()
                s = self.server(b)
                s.call(1, {"name": tool, "arguments": args}, self.wire())
                self.assertEqual((b.booked, len(b.calls)), (0, 0))
                self.assertEqual(s.records[0]["isError"], True)

    def test_each_listed_schema_is_what_the_calls_are_checked_against(self):
        listed = {t["name"]: t["inputSchema"] for t in cp.listed((100, 100))}
        self.assertEqual(sorted(listed), sorted(self.VALID))
        wrong = {"integer": "1", "string": 1, "boolean": "true", "array": "a"}
        for tool, schema in listed.items():
            cp.checked(cp.Call(tool, dict(self.VALID[tool]), None), (100, 100), FOLDER)   # the valid call holds
            for name in schema.get("required", []):
                with self.subTest(tool=tool, missing=name), self.assertRaises(cp.Refused):
                    cp.checked(cp.Call(tool, {k: v for k, v in self.VALID[tool].items() if k != name}, None), (100, 100),
                               FOLDER)
            for name, prop in schema["properties"].items():
                with self.subTest(tool=tool, wrong=name), self.assertRaises(cp.Refused):
                    cp.checked(cp.Call(tool, dict(self.VALID[tool], **{name: wrong[prop["type"]]}), None), (100, 100),
                               FOLDER)
                if "maximum" in prop:
                    with self.subTest(tool=tool, over=name), self.assertRaises(cp.Refused):
                        cp.checked(cp.Call(tool, dict(self.VALID[tool], **{name: prop["maximum"] + 1}), None),
                                   (100, 100), FOLDER)
                    cp.checked(cp.Call(tool, dict(self.VALID[tool], **{name: prop["maximum"]}), None), (100, 100), FOLDER)

    def test_an_answer_over_the_bound_is_recorded_as_failed(self):
        b = self.bridge()
        b.seen, b.unseen = b"x" * 2000, ""                       # an observation too large for the bound below
        s, conn = self.server(b), FakeConnection()
        s.call(1, {"name": "screenshot", "arguments": {}}, self.wire(conn))
        with mock.patch.object(cp, "LINE_BYTES", 512):
            b._serve(b.calls.popleft())
        self.assertTrue(until(lambda: conn.sent))
        self.assertIn("error", conn.sent[0])
        self.assertTrue(s.records[0]["isError"])


class TurnEndAndSteps(Bare):
    def test_the_turn_end_stops_the_actions_of_a_step_not_yet_run(self):
        b = self.bridge()
        call = self.call(b, "step", actions=["first()", "second()"])
        _, actions = b.predict("", {})
        self.assertEqual(next(actions), "first()")
        b.end_turn()
        self.assertEqual(list(actions), [])
        self.assertEqual(call.answers, [(None, None, "the agent's turn has ended")])

    def test_a_done_or_fail_taken_before_the_turn_end_reaches_the_loop(self):
        for tool, action in (("done", "DONE"), ("fail", "FAIL")):
            with self.subTest(tool=tool):
                b = self.bridge()
                click, final = self.call(b, "click", x=1, y=1), self.call(b, tool)
                b.end_turn()
                self.assertEqual(click.answers, [(None, None, "the agent's turn has ended")])
                _, actions = b.predict("", {})
                self.assertEqual(list(actions), [action])
                self.assertEqual(final.answers[0][0]["done"], True)
                self.assertEqual(b.predict("", {}), ("", []))


class StepEnd(Bare):
    def test_one_predict_hands_the_actions_then_the_end_once_the_call_is_answered(self):
        b = self.bridge()
        call = self.call(b, "step", actions=["first()", "second()"], response="words", end="done")
        response, actions = b.predict("", {})
        self.assertEqual((response, next(actions), next(actions)), ("words", "first()", "second()"))
        self.assertEqual(call.answers, [])                       # held while the loop runs the second action
        self.assertEqual(next(actions), "DONE")
        self.assertEqual(call.answers, [({"steps_left": 4, "done": True}, None, None)])
        self.assertEqual(list(actions), [])
        self.assertEqual((b.used, b.ended), (1, "the task has ended (done)"))

    def test_a_step_whose_actions_the_loop_did_not_run_to_their_end_hands_no_end(self):
        b = self.bridge()
        call = self.call(b, "step", actions=["first()", "second()"], end="fail")
        _, actions = b.predict("", {})
        self.assertEqual(next(actions), "first()")
        actions.close()                                          # the loop gave the step up: an action failed
        self.assertEqual(call.answers, [(None, None, "the step was not run to its end")])
        self.assertIsNone(b.ended)
        click = self.call(b, "click", x=1, y=1)                  # the task goes on
        _, actions = b.predict("", {})
        self.assertEqual(list(actions), ["pyautogui.click(1, 1, clicks=1, button='left')"])
        self.assertEqual(click.answers[0][0], {"steps_left": 3, "done": False})

    def test_the_turns_end_during_the_actions_refuses_the_step_and_its_end(self):
        b = self.bridge()
        call = self.call(b, "step", actions=["first()", "second()"], end="done")
        _, actions = b.predict("", {})
        self.assertEqual(next(actions), "first()")
        b.end_turn()
        self.assertEqual(list(actions), [])
        self.assertEqual(call.answers, [(None, None, "the agent's turn has ended")])
        self.assertEqual(b.predict("", {}), ("", []))

    def test_a_step_with_no_end_hands_no_end(self):
        b = self.bridge()
        call = self.call(b, "step", actions=["first()"])
        _, actions = b.predict("", {})
        self.assertEqual(list(actions), ["first()"])
        self.assertEqual(call.answers, [({"steps_left": 4, "done": False}, None, None)])
        self.assertIsNone(b.ended)


class Uploads(Bare):
    def test_an_append_leaves_a_file_named_like_its_scratch_alone(self):
        b = self.bridge()
        b64 = lambda d: base64.b64encode(d).decode()
        self.upload(b, path="file", data=b64(b"A"))
        self.upload(b, path="file.part", data=b64(b"unrelated"))
        self.assertEqual(self.upload(b, path="file", data=b64(b"B"), append=True), {"size": 2})
        with open(self.desktop.at(FOLDER + "/file.part"), "rb") as f:
            self.assertEqual(f.read(), b"unrelated")
        self.assertEqual(sorted(os.listdir(self.desktop.at(FOLDER))), ["file", "file.part"])

    def held(self):
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run, True)
        return cp.Held(run)

    def test_a_file_taken_by_its_sha256_starts_the_file_and_append_is_refused(self):
        held = self.held()
        digest = hashlib.sha256(b"B").hexdigest()
        held.keep(digest, b"B")
        b = self.bridge(held=held)
        self.upload(b, path="f", data=base64.b64encode(b"A").decode())
        with self.assertRaisesRegex(cp.Refused, "append needs data"):
            self.upload(b, path="f", sha256=digest, append=True)
        self.assertEqual(self.upload(b, path="f", sha256=digest), {"size": 1})
        with open(self.desktop.at(FOLDER + "/f"), "rb") as f:
            self.assertEqual(f.read(), b"B")

    def test_a_file_taken_by_its_sha256_drops_what_was_being_kept_for_its_path(self):
        held = self.held()
        hash_b, hash_ac = hashlib.sha256(b"B").hexdigest(), hashlib.sha256(b"AC").hexdigest()
        held.keep(hash_b, b"B")
        b = self.bridge(held=held)
        self.upload(b, path="f", data=base64.b64encode(b"A").decode(), sha256=hash_ac)
        self.upload(b, path="f", sha256=hash_b)
        self.upload(b, path="f", data=base64.b64encode(b"C").decode(), append=True)
        with open(self.desktop.at(FOLDER + "/f"), "rb") as f:
            self.assertEqual(f.read(), b"BC")
        self.assertIsNone(held.get(hash_ac))

    def test_what_a_unit_was_keeping_does_not_pass_to_the_next(self):
        held = self.held()
        digest = hashlib.sha256(b"AB").hexdigest()
        first = self.bridge(held=held)
        self.upload(first, path="f", data=base64.b64encode(b"A").decode(), sha256=digest)
        first.finish()
        second = self.bridge(held=held)
        self.upload(second, path="f", data=base64.b64encode(b"B").decode(), append=True)
        self.assertIsNone(held.get(digest))
        self.assertEqual(os.listdir(held.folder), [])

    def test_the_listed_bound_of_data_is_the_checks(self):
        schema = next(t["inputSchema"] for t in cp.listed((100, 100)) if t["name"] == "upload")
        for size in (cp.CHUNK_BYTES - 1, cp.CHUNK_BYTES, cp.CHUNK_BYTES + 1, cp.CHUNK_BYTES + 2):
            args = {"path": "f", "data": base64.b64encode(b"a" * size).decode()}
            listed = cp._passes(args, schema, "upload", "arguments")
            try:
                cp.checked(cp.Call("upload", args, None), (100, 100), FOLDER)
                taken = True
            except cp.Refused:
                taken = False
            self.assertEqual((size, listed, taken), (size, size <= cp.CHUNK_BYTES, size <= cp.CHUNK_BYTES))
        for wrong in ("YQ", "YQ=", "Y===", "YWJj\n"):
            self.assertFalse(cp._passes({"path": "f", "data": wrong}, schema, "upload", "arguments"), wrong)

    def test_a_transport_failure_during_an_append_leaves_no_part(self):
        b = self.bridge()
        b64 = lambda d: base64.b64encode(d).decode()
        self.upload(b, path="f", data=b64(b"A"))
        real, failed = self.desktop.python, []

        def python(code):
            if not failed:
                failed.append(code)
                raise TimeoutError("the desktop's transport failed")
            return real(code)

        self.desktop.python = python
        with self.assertRaises(TimeoutError):
            self.upload(b, path="f", data=b64(b"B"), append=True)
        self.assertEqual(sorted(os.listdir(self.desktop.at(FOLDER))), ["f"])

    def test_a_stop_as_a_file_is_kept_leaves_no_file_and_no_descriptor(self):
        held = self.held()
        opened, real = [], cp.tempfile.mkstemp

        def mkstemp(**kw):
            opened.append(real(**kw))
            return opened[-1]

        with mock.patch.object(cp.tempfile, "mkstemp", mkstemp), \
                mock.patch.object(cp.os, "fdopen", side_effect=SystemExit(1)), self.assertRaises(SystemExit):
            held.keep(hashlib.sha256(b"x").hexdigest(), b"x")
        self.assertEqual(os.listdir(held.folder), [])
        with self.assertRaises(OSError):                    # the descriptor was closed
            os.fstat(opened[0][0])

    def test_a_file_over_the_bound_is_not_kept(self):
        held = self.held()
        digest = hashlib.sha256(b"ABC").hexdigest()
        b = self.bridge(held=held)
        with mock.patch.object(cp, "KEEP_BYTES", 2):
            self.upload(b, path="f", data=base64.b64encode(b"AB").decode(), sha256=digest)
            self.upload(b, path="f", data=base64.b64encode(b"C").decode(), append=True)
        self.assertEqual(os.listdir(held.folder), [])

    def test_a_stop_while_a_file_is_kept_leaves_no_temporary_file(self):
        held = self.held()
        digest = hashlib.sha256(b"content").hexdigest()
        with mock.patch.object(cp.os, "replace", side_effect=SystemExit(1)), self.assertRaises(SystemExit):
            held.keep(digest, b"content")
        self.assertEqual(os.listdir(held.folder), [])

    def test_a_link_in_place_of_a_kept_file_is_not_followed(self):
        held = self.held()
        elsewhere = os.path.join(self.held().folder, "content")
        with open(elsewhere, "wb") as f:
            f.write(b"content")
        digest = hashlib.sha256(b"content").hexdigest()
        os.symlink(elsewhere, os.path.join(held.folder, digest))
        self.assertIsNone(held.get(digest))


class ConnectionsList(Port):
    def test_a_closed_connection_leaves_the_servers_list(self):
        c = self.start()
        self.assertTrue(until(lambda: len(self.server.wires) == 1))
        c.close()
        self.assertTrue(until(lambda: not self.server.wires), "the closed connection stayed in the list")


OSWORLD = os.environ.get("OSWORLD_DIR", "")
OSWORLD_PYTHON = os.environ.get("OSWORLD_PYTHON", "")
LOOP = ("lib_run_single.py", "lib_results_logger.py", "lib_run_errors.py", "mm_agents")
WHY = ("OSWORLD_DIR does not name a checkout of the upstream" if not all(os.path.exists(os.path.join(OSWORLD, n)) for n in LOOP)
       else "OSWORLD_PYTHON does not name the upstream's Python" if not os.path.isfile(OSWORLD_PYTHON)
       else "")
UPSTREAM_CHILD = """
import argparse, os, sys, tempfile, threading
from nullius.standard import computer_port as cp
import lib_run_single
from desktop_env.desktop_env import DesktopEnv

sock, ready, log, results, steps, lifeline = sys.argv[1:7]
env = DesktopEnv(path_to_vm="vm", action_space="pyautogui", provider_name="docker", screen_size=(1920, 1080),
                 headless=True, os_type="Ubuntu", require_a11y_tree=False, client_password="", enable_proxy=False)


class Desktop:                                    # what an entry gives: one line each over the upstream's controllers
    def python(self, code):
        return env.controller.execute_python_command(code)

    def upload(self, path, data):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        try:
            env.setup_controller._upload_file_setup([{"local_path": f.name, "path": path}])
        finally:
            os.unlink(f.name)

    def read(self, path):
        return env.controller.get_file(path)

    def capture(self):
        return env.controller.get_screenshot()

    def password(self):
        return env.client_password


bridge = cp.Bridge(Desktop(), int(steps), 0.1, (1920, 1080), "/tmp/agent", on_first=lambda: cp.ready(ready, ok=True))
server = cp.Server(bridge, sock, log)
threading.Thread(target=cp.lifeline, args=(int(lifeline), bridge), daemon=True).start()
example = {"id": "task-1", "instruction": "Open the terminal.", "config": [], "evaluator": {"func": "check"}}
where = os.path.join(results, "os", "task-1")
os.makedirs(where)
try:
    lib_run_single.run_single_example(bridge, env, example, int(steps), example["instruction"],
                                      argparse.Namespace(sleep_after_execution=0.1, result_dir=results), where, [])
finally:
    bridge.finish()
    server.close()
    env.close()
"""


@unittest.skipIf(WHY, WHY)
class UpstreamLoop(unittest.TestCase):
    """OSWorld's own run_single_example, unmodified, driven to its end with the bridge as its agent, the fake
    DesktopEnv of fixtures/osworld1_fake_desktop as its environment and a scripted client as the agent. It needs the
    upstream's loop ($OSWORLD_DIR: lib_run_single.py, the two modules it imports and the agents' package one of its
    lines imports) and a Python that holds its packages ($OSWORLD_PYTHON). The loop's fixed waits are recorded, not
    slept (the fixture's sitecustomize.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        up = os.path.join(self.tmp, "upstream")
        os.makedirs(up)
        for name in LOOP:
            os.symlink(os.path.join(OSWORLD, name), os.path.join(up, name))
        fake = os.path.join(FIXTURES, "osworld1_fake_desktop")
        shutil.copytree(os.path.join(fake, "desktop_env"), os.path.join(up, "desktop_env"))
        shutil.copyfile(os.path.join(fake, "sitecustomize.py"), os.path.join(up, "sitecustomize.py"))
        folder = os.path.join(self.tmp, "socket")
        os.mkdir(folder, 0o700)
        self.socket, self.ready = os.path.join(folder, "s"), os.path.join(folder, "ready.json")
        self.log, self.events = os.path.join(self.tmp, "calls.jsonl"), os.path.join(self.tmp, "desktop.jsonl")
        self.results = os.path.join(self.tmp, "results")
        self.up = up

    def run_unit(self, steps):
        read, self.lifeline = os.pipe()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp, "LANG": "C.UTF-8",
               "TMPDIR": tempfile.gettempdir(), "PYTHONPATH": os.pathsep.join([self.up, APP]),
               "PYTHONDONTWRITEBYTECODE": "1", "FAKE_DESKTOP_LOG": self.events}
        p = subprocess.Popen([OSWORLD_PYTHON, "-c", UPSTREAM_CHILD, self.socket, self.ready, self.log, self.results,
                              str(steps), str(read)], cwd=self.tmp, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, pass_fds=(read,), start_new_session=True)
        os.close(read)
        self.addCleanup(lambda: (p.poll() is None and os.killpg(p.pid, signal.SIGKILL), p.wait(),
                                 p.stdout.close()))
        self.addCleanup(os.close, self.lifeline)
        deadline = time.monotonic() + 60
        while not os.path.exists(self.ready):
            if p.poll() is not None:
                self.fail("the unit ended before it was ready: " + p.stdout.read().decode()[-3000:])
            self.assertLess(time.monotonic(), deadline, "not ready within 60 s")
            time.sleep(0.1)
        c = Client(self.socket)
        self.addCleanup(c.close)
        c.initialize()
        return p, c

    def ended(self, p):
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out.decode()[-3000:])
        task = os.path.join(self.results, "os", "task-1")
        with open(os.path.join(task, "result.txt")) as f:
            result = f.read()
        with open(os.path.join(task, "traj.jsonl")) as f:
            traj = [json.loads(line) for line in f]
        with open(self.events) as f:
            events = [json.loads(line) for line in f]
        return result, traj, events

    def test_the_upstreams_loop_runs_the_agents_steps_to_done(self):
        p, c = self.run_unit(steps=5)
        self.assertEqual(image(c.call("screenshot")), PNG)
        self.assertEqual(value(c.call("click", x=10, y=20))["steps_left"], 4)
        self.assertEqual(value(c.call("step", actions=["pyautogui.press('enter')"], response="enter"))["steps_left"], 3)
        self.assertEqual(value(c.call("done")), {"steps_left": 2, "done": True})
        result, traj, events = self.ended(p)
        self.assertEqual(result, "1.0\n")
        self.assertEqual([(t["step_num"], t["action"], t["response"]) for t in traj],
                         [(1, "pyautogui.click(10, 20, clicks=1, button='left')", ""),
                          (2, "pyautogui.press('enter')", "enter"), (3, "DONE", "")])
        self.assertEqual([e["action"] for e in events if e["event"] == "step"],
                         ["pyautogui.click(10, 20, clicks=1, button='left')", "pyautogui.press('enter')", "DONE"])
        self.assertEqual([e["seconds"] for e in events if e["event"] == "wait"], [60, 20])
        with open(self.log) as f:
            self.assertEqual([json.loads(line)["tool"] for line in f], ["screenshot", "click", "step", "done"])

    def test_a_step_with_an_end_is_one_step_of_the_upstreams_loop(self):
        p, c = self.run_unit(steps=5)
        self.assertEqual(value(c.call("click", x=10, y=20))["steps_left"], 4)
        self.assertEqual(value(c.call("step", actions=["pyautogui.press('enter')"], response="enter", end="done")),
                         {"steps_left": 3, "done": True})
        result, traj, events = self.ended(p)
        self.assertEqual(result, "1.0\n")
        self.assertEqual([(t["step_num"], t["action"], t["response"]) for t in traj],
                         [(1, "pyautogui.click(10, 20, clicks=1, button='left')", ""),
                          (2, "pyautogui.press('enter')", "enter"), (2, "DONE", "enter")])
        self.assertEqual([e["last"] for e in events if e["event"] == "evaluate"], ["DONE"])

    def test_a_turn_that_ends_with_no_done_has_the_desktop_evaluated_as_it_is(self):
        p, c = self.run_unit(steps=4)
        c.call("click", x=10, y=20)
        os.write(self.lifeline, cp.TURN_END + b"\n")
        result, traj, events = self.ended(p)
        self.assertEqual(result, "0.0\n")                       # the fake gives 1 only to a task ended by DONE
        self.assertEqual([t["action"] for t in traj], ["pyautogui.click(10, 20, clicks=1, button='left')"])
        self.assertEqual([e["last"] for e in events if e["event"] == "evaluate"],
                         ["pyautogui.click(10, 20, clicks=1, button='left')"])


def shot(n):
    return PNG + b"#%d" % n


class ChangingDesktop(FakeDesktop):
    """The fake desktop with a screen that changes with each action run."""

    def __init__(self, root):
        super().__init__(root)
        self.screen_no = 0

    def act(self, action):
        super().act(action)
        self.screen_no += 1                                      # the screen moves on, whatever the action

    def capture(self):
        with self.op("capture"):
            return shot(self.screen_no)

    def captures(self):
        return self.kinds().count("capture")


class Kept(ChangingDesktop):
    """A desktop whose benchmark keeps its own observation, as OSWorld's loop does: the bridge never captures."""
    last = None

    def observation(self):
        return self.last


def observing_loop(agent, desktop, steps, pause, ran, go, hands):
    """OSWorld's loop in a few lines: the start picture, then one predict a step, its actions run with the pause after
    each and, when the loop ``hands`` its observation, a picture after each action that the next predict gets."""
    agent.reset(None, vm_ip="127.0.0.1")
    go.wait(30)
    obs = {"screenshot": desktop.capture()} if hands else None
    done, step = False, 0
    while not done and step < steps:
        _, actions = agent.predict("the task", obs)
        for action in actions:
            desktop.act(action)
            time.sleep(pause)
            ran.append(action)
            if hands:
                obs = {"screenshot": desktop.capture()}
                desktop.last = obs["screenshot"]
            done = action in ("DONE", "FAIL")
            if done:
                break
        step += 1
    ran.append(("evaluated", step))


class Changing(unittest.TestCase):
    def start(self, steps=5, hands=True, window=True, given=True):
        """A unit whose screen changes with each action: the bridge over the desktop, its server and the loop.
        ``given``: the task is given at once (the window closed), as the run does just after the agent's session
        opens."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = os.path.join(self.tmp, "desktop")
        os.mkdir(root)
        self.desktop = (Kept if hands else ChangingDesktop)(root)
        self.prepared = os.path.join(self.tmp, "prepared.jsonl")
        self.bridge = cp.Bridge(self.desktop, steps, 0.01, (1920, 1080), FOLDER, window=window, prepared=self.prepared)
        folder = os.path.join(self.tmp, "socket")
        os.mkdir(folder, 0o700)
        self.socket, self.log = os.path.join(folder, "s"), os.path.join(self.tmp, "calls.jsonl")
        self.server = cp.Server(self.bridge, self.socket, self.log)
        self.addCleanup(self.server.close)
        self.ran, self.go = [], threading.Event()
        self.go.set()
        self.loop = threading.Thread(target=self._loop, args=(steps, hands), daemon=True)
        self.loop.start()
        self.addCleanup(self.bridge.finish)
        c = Client(self.socket)
        self.addCleanup(c.close)
        c.initialize()
        c.call("screenshot")                                     # served once the loop's first predict has begun
        if given:
            self.bridge.close_window()
        return c

    def _loop(self, steps, hands):
        try:
            observing_loop(self.bridge, self.desktop, steps, 0.01, self.ran, self.go, hands)
        finally:
            self.bridge.finish()

    def evaluated(self, seconds=10):
        self.loop.join(seconds)
        self.assertFalse(self.loop.is_alive(), "the loop did not end")
        return self.ran[-1][1]

    def left(self, c):
        return value(c.call("info"))["steps_left"]

    def put(self, path, data):
        with open(self.desktop.at(path), "wb") as f:
            f.write(data)


class ToolList(Changing):
    def test_the_tool_list_has_no_python_and_no_read_and_names_prepare_and_a_steps_files(self):
        c = self.start(steps=3)
        listed = {t["name"]: t for t in c.ask("tools/list")["result"]["tools"]}
        self.assertEqual(list(listed), ["info", "screenshot", "upload", "prepare", "click", "move", "drag", "scroll",
                                        "type", "key", "wait", "step", "done", "fail"])
        self.assertNotIn("maxItems", listed["step"]["inputSchema"]["properties"]["files"])
        self.assertNotIn("maxItems", listed["prepare"]["inputSchema"]["properties"]["files"])
        self.assertEqual(sorted(listed["prepare"]["inputSchema"]["properties"]), ["actions", "files"])
        init = c.initialize()["result"]
        self.assertEqual(init["serverInfo"], {"name": "computer", "version": "1"})
        self.assertIn("upload and prepare work inside folder (/agent)", init["instructions"])
        self.assertEqual(value(c.call("info")), {"screen": [1920, 1080], "steps": 3, "steps_left": 3, "pause": 0.01,
                                                 "password": "password", "folder": FOLDER})
        for gone in ("python", "read"):
            answer = c.ask("tools/call", {"name": gone, "arguments": {}})
            self.assertEqual(answer["error"]["message"], f"no tool {gone!r}")

    def test_a_bridge_needs_a_plain_absolute_folder(self):
        for folder in (None, "", "agent", "/", "/agent/", "/a/../b"):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                cp.Bridge(FakeDesktop(self.id()), 1, 0, (10, 10), folder)


class Observation(Changing):
    def test_before_the_first_step_screenshot_is_the_loops_start_picture_and_takes_no_new_one(self):
        c = self.start(steps=3)
        first, again = image(c.call("screenshot")), image(c.call("screenshot"))
        self.assertEqual((first, again), (shot(0), shot(0)))
        self.assertEqual(self.desktop.captures(), 1)             # the loop's own: the bridge took none

    def test_two_screenshots_with_no_step_between_are_the_same_and_a_step_that_acts_changes_them(self):
        for hands in (True, False):
            with self.subTest(hands=hands):
                c = self.start(steps=5, hands=hands)
                before = image(c.call("screenshot"))
                got = c.call("click", x=1, y=1)
                after = image(c.call("screenshot"))
                self.assertEqual((image(got), after, image(c.call("screenshot"))), (after, after, after))
                self.assertNotEqual(before, after)
                got = c.call("step", actions=["pyautogui.press('a')", "pyautogui.press('b')"], screenshot=True)
                self.assertEqual(image(got), image(c.call("screenshot")))
                self.assertNotEqual(image(got), after)
                self.assertIsNone(image(c.call("step", actions=["pyautogui.press('c')"])))

    def test_an_empty_step_is_one_step_and_leaves_the_observation_as_it_was(self):
        for hands in (True, False):
            with self.subTest(hands=hands):
                c = self.start(steps=5, hands=hands)
                c.call("click", x=1, y=1)
                seen, captures = image(c.call("screenshot")), self.desktop.captures()
                got = c.call("step", actions=[], screenshot=True)
                self.assertEqual((value(got), image(got)), ({"steps_left": 3, "done": False}, seen))
                self.assertEqual(self.desktop.captures(), captures)

    def test_wait_is_one_step_and_a_fresh_observation(self):
        for hands in (True, False):
            with self.subTest(hands=hands):
                c = self.start(steps=5, hands=hands)
                before = image(c.call("screenshot"))
                got = c.call("wait")
                self.assertEqual(value(got), {"steps_left": 4, "done": False})
                self.assertNotEqual(image(got), before)
                self.assertEqual(image(c.call("screenshot")), image(got))
                self.assertEqual(self.ran, ["WAIT"])

    def test_with_its_own_observation_the_bridge_takes_no_picture_and_without_one_it_takes_one_a_step(self):
        c = self.start(steps=5, hands=True)
        for _ in range(3):
            c.call("click", x=1, y=1)
            c.call("screenshot")
        self.assertEqual(self.desktop.captures(), 4)             # the loop's start picture and one per action
        c = self.start(steps=5, hands=False)
        for _ in range(3):
            c.call("step", actions=["pyautogui.press('a')", "pyautogui.press('b')"])
            c.call("screenshot")
        self.assertEqual(self.desktop.captures(), 4)             # the start picture and one as each step ended
        self.assertEqual(self.desktop.kinds()[-1], "capture")

    def test_a_step_whose_picture_could_not_be_taken_says_so_and_screenshot_too(self):
        c = self.start(steps=5, hands=False)
        self.desktop.fail = {"capture"}
        self.assertIn("the step was run; the screen could not be taken", error(c.call("click", x=1, y=1)))
        self.assertIn("there is no observation", error(c.call("screenshot")))
        self.desktop.fail = set()
        self.assertEqual(image(c.call("click", x=1, y=1)), image(c.call("screenshot")))


class Window(Changing):
    def test_upload_and_prepare_work_in_the_window_and_take_no_step(self):
        c = self.start(steps=3, given=False)
        self.assertEqual(value(c.call("upload", path="/agent/run.py", data=base64.b64encode(b"print(1)").decode())),
                         {"size": 8})
        self.assertEqual(value(c.call("upload", path="x", data=base64.b64encode(b"abc").decode())), {"size": 3})
        with open(self.desktop.at("/agent/x"), "rb") as f:
            self.assertEqual(f.read(), b"abc")                    # a relative path is the folder's
        got = c.call("prepare", actions=["open('agent/out.txt', 'w').write(open('agent/run.py').read() * 2)", "WAIT"],
                     files=["/agent/out.txt", "/agent/none"])
        self.assertEqual(value(got), {"files": {"/agent/out.txt": base64.b64encode(b"print(1)print(1)").decode(),
                                                "/agent/none": None}})
        self.assertIsNone(image(got))
        self.assertEqual(value(c.call("prepare", actions=[])), {"files": {}})
        self.assertEqual(self.left(c), 3)
        self.assertEqual(self.ran, [])
        with open(self.prepared) as f:
            said = [json.loads(line) for line in f]
        self.assertEqual([(s["actions"], s["files"]) for s in said],
                         [(["open('agent/out.txt', 'w').write(open('agent/run.py').read() * 2)", "WAIT"],
                           ["/agent/out.txt", "/agent/none"]), ([], [])])

    def test_an_acting_call_in_the_window_is_refused_and_takes_no_step(self):
        c = self.start(steps=3, given=False)
        for tool, args in (("click", {"x": 1, "y": 1}), ("step", {"actions": []}), ("wait", {}), ("done", {})):
            with self.subTest(tool=tool):
                self.assertIn("the task has not been given yet", error(c.call(tool, **args)))
        self.assertEqual(self.left(c), 3)
        self.bridge.close_window()
        self.assertEqual(value(c.call("click", x=1, y=1)), {"steps_left": 2, "done": False})
        self.assertEqual(self.ran, ["pyautogui.click(1, 1, clicks=1, button='left')"])

    def test_once_the_task_is_given_upload_and_prepare_are_refused(self):
        c = self.start(steps=3, given=False)
        c.call("prepare", actions=[])
        self.bridge.close_window()
        self.assertEqual(error(c.call("upload", path="/agent/a", data="YQ==")),
                         "the task has been given: upload is taken only before it")
        self.assertEqual(error(c.call("prepare", actions=["print(1)"])),
                         "the task has been given: prepare is taken only before it")
        self.assertEqual(self.left(c), 3)
        self.assertEqual(len(self.desktop.kinds()), 2)           # the folder made, the start picture

    def test_the_lifelines_task_given_closes_the_window(self):
        c = self.start(steps=3, given=False)
        read, write = os.pipe()
        with mock.patch.object(cp.os, "kill"):
            reader = threading.Thread(target=cp.lifeline, args=(read, self.bridge), daemon=True)
            reader.start()
            os.write(write, cp.TASK_GIVEN + b"\n")
            deadline = time.monotonic() + 5
            while self.bridge.window and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("the task has been given", error(c.call("prepare", actions=[])))
            os.close(write)
            reader.join(5)
        os.close(read)

    def test_with_no_window_upload_and_prepare_are_refused_from_the_start(self):
        c = self.start(steps=3, window=False, given=False)
        self.assertIn("the task has been given", error(c.call("upload", path="/agent/a", data="YQ==")))
        self.assertEqual(value(c.call("click", x=1, y=1)), {"steps_left": 2, "done": False})

    def test_paths_outside_the_folder_are_refused_at_once_and_take_no_step(self):
        c = self.start(steps=3, given=False)
        for path in ("/etc/passwd", "/agent", "/agent/", "/agent/../x", "../x", "./x", "/agentx/y", "/agent//x"):
            with self.subTest(path=path):
                self.assertIn("is a path inside /agent", error(c.call("upload", path=path, data="YQ==")))
                self.assertIn("files[1] is a path inside /agent",
                              error(c.call("prepare", actions=[], files=["/agent/a", path])))
        self.bridge.close_window()
        self.assertIn("files[0] is a path inside /agent", error(c.call("step", actions=["x()"], files=["/tmp/a"])))
        self.assertEqual(self.left(c), 3)
        self.assertEqual((self.ran, len(self.desktop.kinds())), ([], 2))

    def test_a_prepare_action_the_desktop_did_not_end_in_time_stops_it(self):
        c = self.start(steps=3, given=False)
        self.desktop.silent = True
        self.assertEqual(error(c.call("prepare", actions=["print(1)", "print(2)"])),
                         "the desktop did not end actions[0] in time; the actions after it were not run")
        self.assertEqual(self.left(c), 3)


class WindowClosure(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.desktop = ChangingDesktop(tmp)
        self.bridge = cp.Bridge(self.desktop, 3, 0, (100, 100), FOLDER, window=True)
        self.bridge._begin(None)
        self.addCleanup(self.bridge.finish)

    def submit(self, tool):
        args = {"actions": ["print(1)"], "files": ["/agent/a"]} if tool == "prepare" else {
            "path": "/agent/a", "data": "YQ=="}
        replies = []
        call = cp.Call(tool, args, lambda c, v, i, e: replies.append((v, i, e)))
        cp.checked(call, self.bridge.screen, self.bridge.folder)
        self.bridge.submit(call)
        return call, replies

    def test_closing_refuses_queued_upload_and_prepare_as_calls_sent_after_it(self):
        pending = [self.submit(tool) for tool in cp.WINDOW]
        before = list(self.desktop.events)
        self.bridge.close_window()
        for call, replies in pending:
            _, later = self.submit(call.tool)
            self.assertEqual(replies, later)
            self.assertEqual(replies, [(None, None, f"the task has been given: {call.tool} is taken only before it")])
        self.assertEqual(list(self.bridge.calls), [])
        self.assertEqual(self.desktop.events, before)
        self.assertEqual(self.bridge.used, 0)

    def test_a_dequeued_call_is_refused_if_it_has_not_started_before_closure(self):
        for tool in cp.WINDOW:
            with self.subTest(tool=tool):
                self.bridge.window = True
                call, replies = self.submit(tool)
                self.assertIs(self.bridge._next(), call)
                before = list(self.desktop.events)
                self.bridge.close_window()
                self.bridge._serve(call)
                _, later = self.submit(tool)
                self.assertEqual(replies, later)
                self.assertEqual(self.desktop.events, before)

    def test_closing_waits_for_running_upload_and_prepare_even_when_they_fail(self):
        for tool in cp.WINDOW:
            for fails in (False, True):
                with self.subTest(tool=tool, fails=fails):
                    self.bridge.window = True
                    call, replies = self.submit(tool)
                    entered, release, waiting, closed = (threading.Event() for _ in range(4))
                    operation = "upload" if tool == "upload" else ("python" if fails else "read")
                    original = getattr(self.desktop, operation)
                    wait = self.bridge.change.wait

                    def blocked(*args):
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("the test did not release the desktop")
                        if fails:
                            raise RuntimeError("the desktop operation failed")
                        return original(*args)

                    def waited(*args):
                        waiting.set()
                        return wait(*args)

                    def close():
                        self.bridge.close_window()
                        closed.set()

                    with mock.patch.object(self.desktop, operation, blocked), \
                            mock.patch.object(self.bridge.change, "wait", waited):
                        serving = threading.Thread(target=lambda: self.bridge._serve(self.bridge._next()))
                        closing = threading.Thread(target=close)
                        serving.start()
                        try:
                            self.assertTrue(entered.wait(5))
                            closing.start()
                            self.assertTrue(waiting.wait(1), "closure did not wait for the running call")
                            self.assertFalse(closed.is_set())
                            self.assertFalse(self.bridge.window)
                            _, later = self.submit(tool)
                            self.assertIn("the task has been given", later[0][2])
                        finally:
                            release.set()
                            serving.join(5)
                            if closing.ident is not None:
                                closing.join(5)
                        self.assertFalse(serving.is_alive() or closing.is_alive())
                    self.assertTrue(closed.is_set())
                    self.assertEqual(len(replies), 1)
                    self.assertEqual(replies[0][2] is not None, fails)
                    self.assertEqual(self.bridge.used, 0)


class StepFiles(Changing):
    def test_a_step_returns_each_file_null_for_a_missing_one_and_an_error_for_one_too_large(self):
        c = self.start(steps=3)
        self.put("/agent/small", b"hello")
        self.put("/agent/big", b"x" * (cp.READ_BYTES + 1))
        got = c.call("step", actions=["pyautogui.press('a')"], files=["/agent/small", "/agent/missing", "big"])
        self.assertEqual(value(got), {"steps_left": 2, "done": False, "files": {
            "/agent/small": base64.b64encode(b"hello").decode(), "/agent/missing": None,
            "big": {"error": f"over {cp.READ_BYTES} bytes"}}})
        self.desktop.fail = {"read"}
        got = c.call("step", actions=[], files=["/agent/small"])
        self.assertEqual(value(got), {"steps_left": 1, "done": False, "files": {"/agent/small": {
            "error": "the desktop could not read it: RuntimeError: read failed in the desktop"}}})

    def test_forty_small_files_come_back_in_one_step(self):
        c = self.start(steps=3)
        names = [f"/agent/f{i}" for i in range(40)]
        for i, name in enumerate(names):
            self.put(name, b"file %d" % i)
        got = value(c.call("step", actions=["pyautogui.press('a')"], files=names))
        self.assertEqual(got, {"steps_left": 2, "done": False,
                               "files": {name: base64.b64encode(b"file %d" % i).decode()
                                         for i, name in enumerate(names)}})
        self.assertEqual(self.left(c), 2)

    def test_files_that_would_take_the_answer_past_its_bound_are_errors_and_the_others_come(self):
        c = self.start(steps=3)
        big = b"y" * (11 * 1024 * 1024)                          # 11 MiB: two of them pass 24 MiB in base64
        for name in ("a", "b", "c"):
            self.put(f"/agent/{name}", big)
        self.put("/agent/small", b"hi")
        got = c.call("step", seconds=60, actions=["pyautogui.press('a')"], screenshot=True,
                     files=["/agent/a", "/agent/b", "/agent/small", "/agent/c"])
        self.assertFalse(got["isError"])
        over = {"error": f"the answer would pass {cp.LINE_BYTES} bytes with this file"}
        self.assertEqual(value(got)["files"], {"/agent/a": base64.b64encode(big).decode(), "/agent/b": over,
                                               "/agent/small": base64.b64encode(b"hi").decode(), "/agent/c": over})
        self.assertEqual(image(got), shot(1))
        self.assertEqual(self.left(c), 2)

    def test_the_files_are_read_once_the_actions_and_their_pauses_have_run(self):
        c = self.start(steps=3)
        c.call("step", actions=["pyautogui.press('a')", "WAIT"], files=["/agent/a"])
        kinds = self.desktop.kinds()
        self.assertEqual(kinds[kinds.index("action"):], ["action", "capture", "action", "capture", "read"])

    def test_every_call_that_reaches_the_desktop_lowers_steps_left_but_upload_and_prepare_in_the_window(self):
        c = self.start(steps=9, given=False)
        c.call("upload", path="/agent/a", data="YQ==")
        c.call("prepare", actions=["print(1)"])
        c.call("screenshot")
        self.assertEqual(self.left(c), 9)
        self.bridge.close_window()
        calls = [("click", {"x": 1, "y": 1}), ("move", {"x": 1, "y": 1}), ("drag", {"x": 1, "y": 1, "to_x": 2, "to_y": 2}),
                 ("scroll", {"x": 1, "y": 1, "dy": 1}), ("type", {"text": "a"}), ("key", {"keys": ["a"]}),
                 ("wait", {}), ("step", {"actions": [], "files": ["/agent/a"]})]
        for i, (tool, args) in enumerate(calls, 1):
            self.assertEqual(value(c.call(tool, **args))["steps_left"], 9 - i, tool)
            c.call("screenshot")
            self.assertEqual(self.left(c), 9 - i, tool)
        self.assertEqual(value(c.call("done")), {"steps_left": 0, "done": True})
        self.assertEqual(self.evaluated(), 9)

    def test_a_step_with_an_end_and_files_runs_its_actions_and_ends_the_task(self):
        c = self.start(steps=5)
        got = c.call("step", actions=["pyautogui.press('a')"], end="done", files=["/agent/a"], screenshot=True)
        self.assertEqual(value(got), {"steps_left": 4, "done": True, "files": {"/agent/a": None}})
        self.assertEqual(image(got), shot(1))
        self.assertEqual(self.evaluated(), 1)
        self.assertEqual(self.ran, ["pyautogui.press('a')", "DONE", ("evaluated", 1)])
        self.assertEqual(error(c.call("screenshot")), "the task has ended (done)")

    def test_the_budget_and_the_turns_end_hold_when_the_screen_changes(self):
        c = self.start(steps=1)
        self.assertEqual(value(c.call("click", x=1, y=1)), {"steps_left": 0, "done": True})
        self.assertEqual(error(c.call("click", x=1, y=1)), "the steps are used up")
        self.assertEqual(value(c.call("info"))["steps_left"], 0)
        c = self.start(steps=4)
        c.call("click", x=1, y=1)
        self.bridge.end_turn()
        self.assertEqual(self.evaluated(), 4)
        self.assertEqual(error(c.call("click", x=1, y=1)), "the agent's turn has ended")


if __name__ == "__main__":
    unittest.main()
