"""The computer kit's eight checks and its driver: each verdict on hand-written records; the driver's wiring
(the port's server and the kit's loop over a desktop, the turn's end at score, the budget, the reference, a unit
let go without its score, the reaper) over a fake desktop, with no container; the docker tier runs the eight
checks on real desktops, the reference passing each and the null control none."""
from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import queue
import random
import re
import secrets
import shutil
import select
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

from nullius.standard import computer_port as port, computer_pipe
from nullius.standard import container, definitions, ports
from tests import docker_tier

KIT = os.path.join(definitions.KITS, "agent-computer-v1")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


driver = _module("computer_kit_driver_under_test", "driver.py")
checks, desktop = driver.checks, driver.desktop
reaper = _module("computer_kit_reaper_under_test", "reaper.py")
ACTIONS = [{"start": 100.0, "end": 101.0}, {"start": 110.0, "end": 111.0}]


def ev(kind, t, **kw):
    return {"kind": kind, "t": t, **kw}


def press(t, hit, button=1, x=10, y=10):
    return [ev("press", t, button=button, x=x, y=y, hit=hit), ev("release", t + 0.05, button=button, x=x, y=y, hit=hit)]


# -- the verdicts ------------------------------------------------------------------------------------------------------
LIMIT = {"labels": list(checks.ORDER)}


class NewVerdicts(unittest.TestCase):
    def test_limit_passes_on_presses_within_the_steps_and_nothing_after(self):
        events = press(100.2, "ONE") + press(110.2, "TWO")
        self.assertEqual(checks.verdict("limit", LIMIT, events, ACTIONS, ""), {"passed": True, "why": ""})

    def test_limit_fails_without_a_press_with_input_after_the_last_step_or_a_call_past_the_budget(self):
        self.assertIn("no press", checks.verdict("limit", LIMIT, [], ACTIONS, "")["why"])
        late = press(100.2, "ONE") + press(112.0, "THREE")
        self.assertIn("after the last step", checks.verdict("limit", LIMIT, late, ACTIONS, "")["why"])
        self.assertIn("once every step was taken",
                      checks.verdict("limit", LIMIT, press(100.2, "ONE"), ACTIONS, "", (), 1)["why"])

    def test_drag_passes_on_a_press_on_the_handle_released_inside_the_zone(self):
        ready = ev("ready", 99.0, boxes={"handle": [0, 0, 50, 50], "zone": [500, 500, 700, 700]})
        good = [ready, ev("press", 100.2, button=1, hit="handle", x=20, y=20),
                ev("release", 100.8, button=1, hit="handle", x=600, y=600)]
        self.assertTrue(checks.verdict("drag", {}, good, ACTIONS, "")["passed"])
        outside_zone = good[:2] + [ev("release", 100.8, button=1, hit="handle", x=400, y=600)]
        self.assertFalse(checks.verdict("drag", {}, outside_zone, ACTIONS, "")["passed"])
        not_handle = [ready, ev("press", 100.2, button=1, hit="background", x=20, y=20),
                      ev("release", 100.8, button=1, hit="background", x=600, y=600)]
        self.assertFalse(checks.verdict("drag", {}, not_handle, ACTIONS, "")["passed"])

    def test_scroll_passes_on_the_words_once_the_list_moved(self):
        exp = {"words": ["ACORN", "BASKET"]}
        moved = [ev("scroll", 100.2, button=5, hit="list"), ev("moved", 100.21, top=0.4)]
        self.assertTrue(checks.verdict("scroll", exp, moved, ACTIONS, "ACORN BASKET")["passed"])
        self.assertIn("never moved", checks.verdict("scroll", exp, moved[:1], ACTIONS, "ACORN BASKET")["why"])
        self.assertEqual(checks.verdict("scroll", exp, moved, ACTIONS, ""),
                         {"passed": False, "why": "the words were not found together in order; expected ACORN, BASKET"})

    def test_recover_passes_on_a_success_after_the_error_and_the_click(self):
        exp = {"target": "OCEAN", "labels": ["OCEAN", "APPLE"]}
        calls = [{"tool": "screenshot", "isError": False, "start": 1.0, "end": 1.5},
                 {"tool": "click", "isError": True, "start": 2.0, "end": 2.5},
                 {"tool": "click", "isError": False, "start": 3.0, "end": 4.5}]
        events = press(110.2, "OCEAN")
        self.assertTrue(checks.verdict("recover", exp, events, ACTIONS, "", calls)["passed"])
        self.assertIn("no acting call was answered with an error",
                      checks.verdict("recover", exp, events, ACTIONS, "", calls[2:])["why"])
        self.assertIn("after the error's answer", checks.verdict("recover", exp, events, ACTIONS, "", calls[:2])["why"])
        sent_before = calls[:2] + [dict(calls[2], start=2.4)]                     # sent before the error's answer came
        self.assertIn("after the error's answer", checks.verdict("recover", exp, events, ACTIONS, "", sent_before)["why"])
        self.assertFalse(checks.verdict("recover", exp, [], ACTIONS, "", calls)["passed"])

    def test_batch_passes_when_each_field_holds_its_word(self):
        exp = {"words": {"ONE": "kotabiru", "TWO": "mafodafi", "THREE": "sanepilo"}}
        events = [ev("value", 100.1 + i / 10, field=f, value=w) for i, (f, w) in enumerate(exp["words"].items())]
        self.assertTrue(checks.verdict("batch", exp, events, ACTIONS, "")["passed"])
        wrong = events + [ev("value", 100.5, field="TWO", value="mafodaf")]
        self.assertIn("TWO", checks.verdict("batch", exp, wrong, ACTIONS, "")["why"])

    def test_every_check_fails_on_input_outside_every_action(self):
        passing = {
            "see": ({"words": ["ACORN", "BASKET", "CANDLE"]}, [], "ACORN BASKET CANDLE", ()),
            "click": ({"target": "OCEAN", "labels": ["OCEAN"]}, press(100.2, "OCEAN"), "", ()),
            "type": ({"word": "kotabiru"}, [ev("enter", 100.3, value="kotabiru")], "", ()),
            "limit": (LIMIT, press(100.2, "ONE"), "", ()),
            "drag": ({}, [ev("ready", 99, boxes={"zone": [500, 500, 700, 700]}), ev("press", 100.2, button=1, hit="handle"),
                          ev("release", 100.8, button=1, hit="handle", x=600, y=600)], "", ()),
            "scroll": ({"words": ["ACORN", "BASKET"]}, [ev("moved", 100.3, top=0.5)], "ACORN BASKET", ()),
            "recover": ({"target": "OCEAN", "labels": ["OCEAN"]}, press(110.2, "OCEAN"), "",
                        ({"tool": "click", "isError": True, "start": 1, "end": 2},
                         {"tool": "click", "isError": False, "start": 3, "end": 4})),
            "batch": ({"words": {"ONE": "a"}}, [ev("value", 100.2, field="ONE", value="a")], "", ()),
        }
        self.assertEqual(sorted(passing), sorted(checks.CHECKS))
        for check, (exp, events, message, calls) in passing.items():
            with self.subTest(check=check):
                self.assertTrue(checks.verdict(check, exp, events, ACTIONS, message, calls)["passed"])
                stray = events + [ev("key", 105.0, keysym="a", char="a", hit="background")]
                self.assertFalse(checks.verdict(check, exp, stray, ACTIONS, message, calls)["passed"])

    def test_each_check_draws_its_own_screen(self):
        for check in checks.CHECKS:
            values, exp = checks.draw(check)
            self.assertIn(values["screen"], ("see", "click", "type", "drag", "scroll", "batch"), check)
            for label in values.get("labels") or []:
                self.assertTrue(re.fullmatch(r"[A-Z]+", label), label)
        self.assertEqual(sorted(checks.draw("limit")[0]["labels"]), sorted(checks.ORDER))
        self.assertEqual((checks.steps("limit"), checks.steps("batch"), checks.steps("see")), (2, 2, 8))


# -- the kit's definition ---------------------------------------------------------------------------------------------
class Definition(unittest.TestCase):
    def test_the_port_names_the_kit_and_the_kit_its_checks(self):
        kit = definitions.kit("agent.computer/v1")
        self.assertEqual(ports.PORTS["agent.computer/v1"]["kit"], "agent-computer-v1")
        self.assertEqual(kit["key"], "agent-computer-v1")
        self.assertEqual(definitions.problems({k: v for k, v in kit.items() if not k.startswith("_")}, "benchmark"), [])
        self.assertEqual(list(kit["check_details"]), list(checks.CHECKS))
        self.assertEqual(kit["basic"], list(checks.BASIC))
        self.assertEqual(set(kit["skills"]), set(checks.CHECKS) - set(checks.BASIC))
        self.assertEqual(kit["environments"]["desktop"], dict(desktop.KIT["environments"]["desktop"],
                                                             summary=kit["environments"]["desktop"]["summary"]))
        self.assertEqual(kit["mcp_servers"]["computer"]["command"], ["python3", "{here}/pipe.py"])     # a file of its folder
        pipe = _module("computer_kit_pipe_under_test", "pipe.py").PIPE
        self.assertEqual(pipe, os.path.join(os.path.dirname(os.path.dirname(KIT)), "computer_pipe.py"))
        self.assertTrue(os.path.isfile(pipe))

    def test_the_pipe_from_the_kits_folder_joins_stdio_to_the_socket(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            path = os.path.join(tmp, "s")
            server = port.Server(port.Bridge(FakeDesktop("d", {}), 1, 0, (1920, 1080), driver.FOLDER), path)
            try:
                p = subprocess.run([sys.executable, os.path.join(KIT, "pipe.py"), path], capture_output=True, timeout=30,
                                   input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode())
            finally:
                server.close()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["result"]["tools"][0]["name"], "info")


# -- the driver over a fake desktop -------------------------------------------------------------------------------------
class FakeDesktop:
    """A desktop with no container: the fixture's boxes, and the input each click, write or key of an action (or
    of the desktop's python) would send, logged at the time it runs, as the fixture would. The agent's folder is made
    as the bridge asks."""
    made = []

    def __init__(self, name, values):
        self.name, self.values, self.actions, self.log, self.removed = name, values, [], [], False
        labels = values.get("labels") or []
        boxes = {label: [100 + 300 * i, 100, 300 + 300 * i, 200] for i, label in enumerate(labels)}
        boxes.update(field=[600, 500, 1200, 560], handle=[200, 500, 260, 560], zone=[1300, 400, 1700, 700],
                     list=[600, 300, 1300, 800])
        self.boxes = boxes
        self.log.append({"kind": "ready", "t": time.monotonic(), "boxes": boxes})
        FakeDesktop.made.append(self)

    def hit(self, x, y):
        return next((k for k, b in self.boxes.items() if b[0] <= x < b[2] and b[1] <= y < b[3]), "background")

    def _input(self, code):
        t = time.monotonic()
        for x, y in re.findall(r"pyautogui\.click\((\d+), (\d+)", code):
            h = self.hit(int(x), int(y))
            self.log += [{"kind": "press", "t": t, "button": 1, "x": int(x), "y": int(y), "hit": h},
                         {"kind": "release", "t": t, "button": 1, "x": int(x), "y": int(y), "hit": h}]
        for text in re.findall(r"pyautogui\.write\('([a-z]+)'", code):
            self.log += [{"kind": "key", "t": t, "keysym": c, "char": c, "hit": "field"} for c in text]
        if "hotkey('enter')" in code:
            self.log.append({"kind": "enter", "t": t, "value": "".join(e["char"] for e in self.log if e["kind"] == "key")})

    def act(self, code, seconds=None):
        start = time.monotonic()
        self._input(code)
        self.actions.append({"start": start, "end": time.monotonic()})
        time.sleep(0.01)
        return {"start": start, "end": self.actions[-1]["end"], "reply": {"status": "success", "returncode": 0}}

    def python(self, code, seconds=None):
        if code == port.MAKE_FOLDER.format(folder=driver.FOLDER):
            return {"status": "success", "output": "made\n", "error": "", "returncode": 0}
        self._input(code)
        return {"status": "success", "output": "", "error": "", "returncode": 0}

    def start_fixture(self, values):
        pass

    def events(self):
        return list(self.log)

    def capture(self):
        return b"\x89PNG\r\n\x1a\nfake"

    def upload(self, path, data, append=False):
        return len(data)

    def read(self, path):
        return None

    def password(self):
        return ""

    def remove(self):
        self.removed = True


class ScoreRecords(unittest.TestCase):
    """The optional copies at score, over a desktop with no socket or running loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(mock.patch.dict(driver.OPEN, {}, clear=True))
        self.enterContext(mock.patch.object(driver.REAPER, "tell"))
        self.addCleanup(driver.close_all)

    def unit(self):
        ws = os.path.join(self.tmp, str(len(FakeDesktop.made)), "workspace")
        os.makedirs(ws)
        u = driver.Unit("see", ws)
        os.makedirs(os.path.dirname(u.log))
        u.expected = {"words": ["ACORN", "BASKET", "CANDLE"]}
        d = u.desktop = FakeDesktop("fake", u.values)
        u.boxes = d.boxes
        u.bridge = driver.KitBridge(driver.KitDesktop(d), u.steps, 0, desktop.SCREEN, driver.FOLDER)
        driver.OPEN["see"] = u
        return ws, u, d

    def test_a_failed_record_write_does_not_change_the_verdict_or_cleanup(self):
        for answer in ("ACORN BASKET CANDLE", ""):
            ws, _, _ = self.unit()
            expected = driver.score("see", ws, [], answer)
            self.assertEqual(expected["passed"], bool(answer))
            for index in (0, 1):
                for when in ("write", "close"):
                    with self.subTest(answer=answer, file=index, when=when):
                        ws, u, d = self.unit()
                        opened = []
                        original, fdopen = open, os.fdopen

                        def opening(file, mode="r", **kw):
                            f = (fdopen if isinstance(file, int) else original)(file, mode, **kw)
                            if mode != "w":
                                return f
                            opened.append(f)
                            if len(opened) != index + 1:
                                return f
                            wrapped = mock.MagicMock(wraps=f)
                            wrapped.__enter__.return_value = wrapped

                            def close(*args):
                                f.__exit__(*args)
                                if when == "close":
                                    raise OSError("disk full at close")

                            wrapped.__exit__.side_effect = close
                            if when == "write":
                                wrapped.write.side_effect = OSError("disk full at write")
                                wrapped.writelines.side_effect = OSError("disk full at write")
                            return wrapped

                        with mock.patch.object(driver, "open", side_effect=opening, create=True), \
                                mock.patch.object(driver.os, "fdopen", side_effect=opening):
                            try:
                                got = driver.score("see", ws, [], answer)
                            finally:
                                self.assertTrue(d.removed)
                                self.assertIsNone(u.desktop)
                                self.assertNotIn("see", driver.OPEN)
                        self.assertEqual(len(opened), index + 1)
                        self.assertTrue(all(f.closed for f in opened))
                        self.assertEqual(got, expected)
                        self.assertFalse(any(n.startswith(".computer-") for n in os.listdir(os.path.dirname(u.log))))

    def test_record_names_replace_a_link_and_a_fifo_without_blocking_score(self):
        names = ("computer-events.jsonl", "computer-boxes.json")
        for linked, piped in (names, names[::-1]):
            with self.subTest(link=linked, fifo=piped):
                ws, u, d = self.unit()
                folder = os.path.dirname(u.log)
                outside = os.path.join(os.path.dirname(ws), "untouched")
                with open(outside, "w") as f:
                    f.write("untouched")
                os.symlink(outside, os.path.join(folder, linked))
                fifo = os.path.join(folder, piped)
                os.mkfifo(fifo)
                replies = queue.Queue()

                def score():
                    try:
                        replies.put(driver.score("see", ws, [], " ".join(u.expected["words"])))
                    except Exception as e:
                        replies.put(e)

                t = threading.Thread(target=score, daemon=True)
                t.start()
                t.join(5)
                blocked = t.is_alive()
                if blocked:                          # release an old writer before the test's folders go
                    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        t.join(5)
                    finally:
                        os.close(fd)
                self.assertFalse(t.is_alive(), "scoring did not end after the FIFO was released")
                self.assertFalse(blocked, "scoring blocked on the FIFO")
                got = replies.get_nowait()
                self.assertIsInstance(got, dict, got)
                self.assertTrue(got["passed"], got)
                self.assertTrue(d.removed)
                self.assertNotIn("see", driver.OPEN)
                self.assertEqual(sorted(os.listdir(folder)), sorted(names))
                for name in names:
                    self.assertTrue(stat.S_ISREG(os.lstat(os.path.join(folder, name)).st_mode))
                with open(outside) as f:
                    self.assertEqual(f.read(), "untouched")
                with open(os.path.join(folder, names[0])) as f:
                    self.assertEqual([json.loads(line) for line in f], d.events())
                with open(os.path.join(folder, names[1])) as f:
                    self.assertEqual(json.load(f), u.boxes)


class FakeUnits(unittest.TestCase):
    """Units of the driver over fake desktops, each in a folder of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.told = []
        values = {}

        def start(image, name, *, job=None, seconds=None):
            return FakeDesktop(name, values["now"])

        def draw(check, rng=None):
            got = self._draw(check)
            values["now"] = got[0]
            return got
        self._draw = checks.draw
        self.enterContext(mock.patch.object(driver.desktop, "image", return_value="img"))
        self.enterContext(mock.patch.object(driver.desktop.Desktop, "start", side_effect=start))
        self.enterContext(mock.patch.object(driver.checks, "draw", side_effect=draw))
        self.enterContext(mock.patch.object(driver.REAPER, "tell", side_effect=self.told.append))
        self.enterContext(mock.patch.object(driver, "PAUSE", 0.01))
        self.enterContext(mock.patch.object(desktop, "_CLIENTS_CLOSED", False))
        self.addCleanup(driver.close_all)

    def unit(self, name):
        ws = os.path.join(self.tmp, "job", f"{name}-{len(FakeDesktop.made)}", "workspace")
        os.makedirs(ws)
        return ws, driver.setup(name, ws)


class Wiring(FakeUnits):
    def test_a_unit_is_served_scored_and_let_go(self):
        ws, s = self.unit("click")
        self.assertEqual(s["mcp"], [{"name": "computer", "server": "computer", "args": [s["mcp"][0]["args"][0]]}])
        self.assertEqual(s["agent_seconds"], 600)
        u = driver.OPEN["click"]
        self.assertIn(u.expected["target"], s["prompt"])
        sock = s["mcp"][0]["args"][0]
        c = driver.Client(sock)
        self.assertEqual(c.ask("tools/list", {})["result"]["tools"][0]["name"], "info")
        x, y = driver.centre(u.boxes[u.expected["target"]])
        self.assertEqual(c.call("click", {"x": x, "y": y})[0], False)
        c.close()
        got = driver.score("click", ws, [], "done")
        self.assertTrue(got["passed"], got)
        self.assertEqual(got["detail"]["calls"], ["click"])
        self.assertFalse(os.path.exists(sock))                                  # the socket and its folder gone
        self.assertTrue(FakeDesktop.made[-1].removed)
        self.assertEqual([l.split()[0] for l in self.told], ["+container", "=container", "+folder", "-container", "-folder"])
        self.assertNotIn("click", driver.OPEN)

    def test_the_units_record_keeps_the_fixtures_events_and_where_its_targets_were(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        c = driver.Client(s["mcp"][0]["args"][0])
        c.call("click", dict(zip("xy", driver.centre(u.boxes[u.expected["target"]]))))
        c.close()
        driver.score("click", ws, [], "done")
        verifier = os.path.join(os.path.dirname(ws), "verifier")
        with open(os.path.join(verifier, "computer-events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        self.assertEqual(events, FakeDesktop.made[-1].events())
        self.assertEqual([e["hit"] for e in events if e["kind"] == "press"], [u.expected["target"]])
        with open(os.path.join(verifier, "computer-boxes.json")) as f:
            self.assertEqual(json.load(f), u.boxes)

    def test_the_turns_end_at_score_ends_the_loop_and_judges_what_is_there(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        t = time.monotonic()
        got = driver.score("click", ws, [], "")                                 # no call: the null control
        self.assertLess(time.monotonic() - t, 5)
        self.assertFalse(got["passed"])
        self.assertFalse(u.loop.is_alive())
        self.assertEqual(got["detail"]["steps_used"], 0)

    def test_a_step_with_an_end_is_one_step_and_ends_the_kits_loop(self):
        for end in ("done", "fail"):
            with self.subTest(end=end):
                ws, s = self.unit("type")
                u = driver.OPEN["type"]
                c = driver.Client(s["mcp"][0]["args"][0])
                x, y = driver.centre(u.boxes["field"])
                actions = [f"pyautogui.click({x}, {y})", f"pyautogui.write({u.expected['word']!r})",
                           "pyautogui.hotkey('enter')"]
                got = c.call("step", {"actions": actions, "response": "typed", "end": end})
                self.assertEqual(got, (False, {"steps_left": u.steps - 1, "done": True}))
                u.loop.join(5)
                self.assertFalse(u.loop.is_alive(), "the kit's loop went on after the step's end")
                self.assertEqual(c.call("click", {"x": x, "y": y}), (True, f"the task has ended ({end})"))
                c.close()
                scored = driver.score("type", ws, [], "")
                self.assertTrue(scored["passed"], scored)
                self.assertEqual((scored["detail"]["steps_used"], scored["detail"]["actions"]), (1, 3))
                self.assertEqual(u.final, "typed")

    def test_a_client_that_acts_past_its_budget_fails(self):
        ws, s = self.unit("limit")
        u = driver.OPEN["limit"]
        c = driver.Client(s["mcp"][0]["args"][0])
        answers = [c.call("click", {"x": x, "y": y})[0] for x, y in
                   (driver.centre(u.boxes[label]) for label in checks.ORDER[:3])]
        c.close()
        self.assertEqual(answers, [False, False, True])                           # the third is refused
        got = driver.score("limit", ws, [], "done")
        self.assertFalse(got["passed"])
        self.assertEqual(got["detail"]["past_budget"], 1)
        self.assertIn("once every step was taken", got["detail"]["why"])

    def test_the_reference_passes_click_type_limit_and_recover(self):
        for unit in ("click", "type", "limit", "recover"):
            with self.subTest(unit=unit):
                ws, s = self.unit(unit)
                answer = driver.oracle(unit, ws)["answer"]
                got = driver.score(unit, ws, [], answer)
                self.assertTrue(got["passed"], got)
        self.assertEqual(got["detail"]["errors"], 1)                           # recover: the first click answered with an error

    def test_a_unit_never_scored_is_let_go_by_the_next_setup_and_by_the_drivers_end(self):
        ws, s = self.unit("see")
        first = FakeDesktop.made[-1]
        self.unit("click")
        self.assertTrue(first.removed)
        self.assertFalse(os.path.exists(s["mcp"][0]["args"][0]))
        self.assertEqual(list(driver.OPEN), ["click"])
        driver.close_all()                                                     # the driver's input ended
        self.assertTrue(FakeDesktop.made[-1].removed)
        self.assertEqual(driver.OPEN, {})


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                          timeout=30).stdout.strip()[:1] not in ("", "Z")


def _png(text, scale, above=()):
    """A screen of 1920 by 1080 with ``text`` drawn as the fixture draws it, as PNG bytes (filter 0)."""
    w, h = 1920, 1080
    cols, rows, cells = driver.fixture.pixels(text)
    x0, y0 = (w - cols * scale) // 2, (h - rows * scale) // 2
    ink = {(x0 + x * scale + dx, y0 + y * scale + dy) for x, y in cells for dx in range(scale) for dy in range(scale)}
    for i, label in enumerate(above):
        _, _, cells = driver.fixture.pixels(label)
        s = driver.fixture.LIST_SCALE
        ink.update((x0 + x * s + dx, y0 - (i + 1) * 60 - 40 + y * s + dy)
                   for x, y in cells for dx in range(s) for dy in range(s))
    paper, mark = bytes.fromhex("eceff1"), bytes.fromhex("111111")
    raw = b"".join(b"\x00" + b"".join(mark if (x, y) in ink else paper for x in range(w)) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Words(unittest.TestCase):
    def test_the_reference_reads_current_vocabulary_and_gaps_at_both_scales(self):
        rng = random.Random(7)
        for check, scale, count in (("see", driver.fixture.SEE_SCALE, 3), ("scroll", driver.fixture.SCROLL_SCALE, 2)):
            with self.subTest(check=check):
                removed = " ".join(["WINDOW", "IGLOO", "INSECT"][:count])
                with self.assertRaises(driver.ReferenceFailed):
                    driver.read_words(_png(removed, scale), scale, count)
                for _ in range(3):
                    values, expected = checks.draw(check, rng)
                    text = " ".join(values["words"])
                    png = _png(text, scale)
                    self.assertEqual(driver.fixture.read_lines(png, scale), [text])
                    self.assertEqual(driver.read_words(png, scale, count), " ".join(expected["words"]))
                text = " ".join(["IGLOO", "WALNUT", "INSECT"][:count])
                self.assertEqual(driver.read_words(_png(text, scale), scale, count), text)

    def test_the_reference_refuses_missing_unreadable_or_repeated_words(self):
        for scale, count in ((driver.fixture.SEE_SCALE, 3), (driver.fixture.SCROLL_SCALE, 2)):
            for text in ("", "ACORN", "ACORN BASKET ZZZZ", " ".join(["ACORN"] * count)):
                with self.subTest(scale=scale, text=text), self.assertRaises(driver.ReferenceFailed):
                    driver.read_words(_png(text, scale), scale, count)

    def test_the_reference_reads_scroll_words_below_smaller_list_labels(self):
        scale = driver.fixture.SCROLL_SCALE
        png = _png("IGLOO BASKET", scale, above=driver.fixture.LIST_WORDS[-3:])
        self.assertEqual(driver.read_words(png, scale, 2), "IGLOO BASKET")

    def test_the_task_asks_for_words_in_order_without_the_answers_or_scoring_rules(self):
        rng = random.Random(7)
        for check in ("see", "scroll"):
            prompts = set()
            for _ in range(1000):
                _, expected = checks.draw(check, rng)
                text = driver.prompt(check, expected)
                prompts.add(text)
                self.assertIn("words", text)
                self.assertIn("order", text)
                self.assertFalse(set(re.findall(r"\b\w+\b", text.upper())) & set(checks.WORDS))
                self.assertNotRegex(text.lower(), r"\b(code|two|three|vocabulary|dictionary|match|checked|scored)\b")
            self.assertEqual(len(prompts), 1)

    def test_the_null_control_still_fails_every_check(self):
        for check in checks.CHECKS:
            _, expected = checks.draw(check, random.Random(7))
            if check in ("see", "scroll"):
                self.assertIn("words", expected)
            self.assertFalse(checks.verdict(check, expected, [], [], "")["passed"], check)

    def test_the_reference_uses_the_screenshot_not_the_expected_words(self):
        for check, scale, count in (("see", driver.fixture.SEE_SCALE, 3), ("scroll", driver.fixture.SCROLL_SCALE, 2)):
            with self.subTest(check=check):
                answer = " ".join(["IGLOO", "BASKET", "CANDLE"][:count])
                png = _png(answer, scale)
                unit = mock.Mock(expected={"words": ["ZEBRA", "PILLOW", "ORANGE"][:count]},
                                 boxes={"list": [0, 0, 760, 540]})
                client = mock.Mock()
                client.call.side_effect = lambda tool, arguments=None: (False, png if tool == "screenshot" else {})
                with mock.patch.dict(driver.OPEN, {check: unit}, clear=True), \
                        mock.patch.object(driver, "Client", return_value=client):
                    self.assertEqual(driver.oracle(check, "unused"), {"answer": answer})
                self.assertEqual([c.args[0] for c in client.call.call_args_list],
                                 ["info", "scroll", "screenshot"] if check == "scroll" else ["info", "screenshot"])
                client.close.assert_called_once()


class FakeWire:
    def __init__(self):
        self.ready, self.reply = threading.Event(), None

    def opened(self):
        pass

    def answered(self):
        pass

    def send(self, data):
        self.reply = json.loads(data) if isinstance(data, (bytes, str)) else data
        self.ready.set()
        return True


def send(server, tool, arguments=None, rid=1):
    wire = FakeWire()
    server.call(rid, {"name": tool, "arguments": arguments or {}}, wire)
    return wire


class Admission(unittest.TestCase):
    def unit(self, name, steps=2):
        u = driver.Unit(name, "/unused/workspace")
        u.desktop = FakeDesktop("fake", {"labels": ["OCEAN"]})
        u.expected = {"target": "OCEAN", "labels": ["OCEAN"]}
        u.steps = steps
        u.bridge = driver.KitBridge(driver.KitDesktop(u.desktop), steps, 0, desktop.SCREEN, driver.FOLDER)
        s = driver.KitServer.__new__(driver.KitServer)
        s._state(u.bridge, "/unused/socket", None)
        calls = []
        s.record = calls.append
        self.addCleanup(u.bridge.finish)
        return u, s, calls

    def test_an_invalid_call_admitted_before_exhaustion_is_not_counted_past_the_budget(self):
        u, s, _ = self.unit("limit")
        send(s, "click", {"x": 150, "y": 150})
        entered, release = threading.Event(), threading.Event()
        original, seen, wires = port.checked, [], []

        def checking(call, screen, folder):
            if call.args.get("x") == "bad":
                seen.append(call)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("validation was not released")
            return original(call, screen, folder)

        t = threading.Thread(target=lambda: wires.append(send(s, "click", {"x": "bad", "y": 150})), daemon=True)
        with mock.patch.object(port, "checked", side_effect=checking):
            t.start()
            try:
                self.assertTrue(entered.wait(5))
                available = u.bridge.change.acquire(blocking=False)
                if available:                       # an unlocked validator lets the other connection take the step
                    try:
                        send(s, "click", {"x": 150, "y": 150})
                    finally:
                        u.bridge.change.release()
            finally:
                release.set()
                t.join(5)
        self.assertFalse(t.is_alive())
        if not available:
            send(s, "click", {"x": 150, "y": 150})
        self.assertTrue(wires[0].reply["result"]["isError"])
        self.assertEqual((u.bridge.booked, u.bridge.past), (2, 0))
        self.assertEqual(len(seen), 1)                # the recorded call itself is checked, once

    def test_validation_cannot_move_a_calls_start_past_the_recovery_error(self):
        u, s, calls = self.unit("recover")
        original, checked, wires = port.checked, [], []
        entered, release = threading.Event(), threading.Event()
        with mock.patch.object(port.time, "time", return_value=1.0) as clock:
            first = send(s, "click", {"x": 150, "y": 150})
            _, actions = u.bridge.predict("", None)

            def checking(call, screen, folder):
                checked.append(call)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("validation was not released")
                return original(call, screen, folder)

            t = threading.Thread(target=lambda: wires.append(send(s, "click", {"x": 150, "y": 150})), daemon=True)
            with mock.patch.object(port, "checked", side_effect=checking):
                t.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(first.ready.is_set())
                    clock.return_value = 2.0
                    u.bridge.running.answer(error=driver.NOT_RUN)
                finally:
                    release.set()
                    t.join(5)
            self.assertFalse(t.is_alive())
            list(actions)
            _, actions = u.bridge.predict("", None)
            for action in actions:
                u.desktop.act(action)
        self.assertFalse(wires[0].reply["result"]["isError"])
        self.assertLess(calls[1]["start"], calls[0]["end"])
        self.assertEqual(len(checked), 1)
        got = checks.recover(u.expected, u.desktop.events(), u.desktop.actions, "done", calls)
        self.assertFalse(got["passed"])
        self.assertIn("after the error's answer", got["why"])

    def test_recovery_refuses_done_and_fail_before_their_terminal_transition(self):
        for name in ("done", "fail"):
            with self.subTest(name=name):
                u, s, calls = self.unit("recover")
                first = send(s, name)
                t = threading.Thread(target=u.run_loop, daemon=True)
                with mock.patch.object(driver, "PAUSE", 0):
                    t.start()
                    try:
                        self.assertTrue(first.ready.wait(5))
                        self.assertTrue(first.reply["result"]["isError"])
                        self.assertIn(driver.NOT_RUN, first.reply["result"]["content"][0]["text"])
                        self.assertIsNone(u.bridge.ended)
                        second = send(s, "click", {"x": 150, "y": 150})
                        self.assertTrue(second.ready.wait(5))
                        self.assertFalse(second.reply["result"]["isError"])
                    finally:
                        u.bridge.finish()
                        t.join(5)
                self.assertFalse(t.is_alive())
                self.assertEqual((u.bridge.used, len(u.desktop.actions)), (2, 1))
                self.assertTrue(checks.recover(u.expected, u.desktop.events(), u.desktop.actions, "done", calls)["passed"])


class EarnedPasses(FakeUnits):
    """What a pass needs, over the fake desktop: every attempt past the budget counted, recover's call sent after
    its error's answer, its error on the first acting call whatever it is, an action not ended in time an error,
    and a reference that reads the screen."""

    def test_a_wrong_call_past_the_budget_is_counted(self):
        ws, s = self.unit("limit")
        u = driver.OPEN["limit"]
        c = driver.Client(s["mcp"][0]["args"][0])
        for label in checks.ORDER[:2]:
            self.assertFalse(c.call("click", dict(zip("xy", driver.centre(u.boxes[label]))))[0])
        self.assertTrue(c.call("click", {"x": "bad", "y": 100})[0])
        c.close()
        self.assertEqual(u.bridge.past, 1)
        self.assertFalse(driver.score("limit", ws, [], "done")["passed"])

    def test_two_calls_sent_together_for_the_last_step_are_one_taken_and_one_counted(self):
        ws, s = self.unit("limit")
        u = driver.OPEN["limit"]
        x, y = driver.centre(u.boxes["ONE"])
        self.assertTrue(send(u.server, "click", {"x": x, "y": y}).ready.wait(10))
        barrier, original, wires = threading.Barrier(2), driver.port.Bridge.submit, []

        def together(bridge, call):                    # both calls reach the bridge at once where the lock lets them
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return original(bridge, call)
        threads = [threading.Thread(target=lambda: wires.append(send(u.server, "click", {"x": x, "y": y}, 2)))
                   for _ in range(2)]
        with mock.patch.object(driver.port.Bridge, "submit", together):
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        for w in wires:
            self.assertTrue(w.ready.wait(10))
        self.assertEqual(sorted(w.reply["result"]["isError"] for w in wires), [False, True])
        self.assertEqual(u.bridge.past, 1)
        self.assertFalse(driver.score("limit", ws, [], "done")["passed"])

    def test_recover_needs_a_call_sent_after_the_errors_answer(self):
        ws, s = self.unit("recover")
        u = driver.OPEN["recover"]
        x, y = driver.centre(u.boxes[u.expected["target"]])
        with u.bridge.change:                            # both queued before either is answered
            first, second = send(u.server, "click", {"x": x, "y": y}), send(u.server, "click", {"x": x, "y": y}, 2)
        self.assertTrue(first.ready.wait(10) and second.ready.wait(10))
        self.assertEqual([c["isError"] for c in u.calls()], [True, False])
        got = driver.score("recover", ws, [], "done")
        self.assertFalse(got["passed"])
        self.assertIn("after the error's answer", got["detail"]["why"])

    def test_recovers_error_goes_to_the_first_acting_call_whatever_it_is(self):
        for first in ("wait", "step"):
            with self.subTest(first=first):
                ws, s = self.unit("recover")
                u = driver.OPEN["recover"]
                c = driver.Client(s["mcp"][0]["args"][0])
                failed, said = c.call(first, {"actions": []} if first == "step" else {})
                self.assertTrue(failed, said)
                self.assertIn("none of it was run", said)
                self.assertFalse(c.call("click", dict(zip("xy", driver.centre(u.boxes[u.expected["target"]]))))[0])
                c.close()
                self.assertTrue(driver.score("recover", ws, [], "done")["passed"])
                driver.close_all()

    def test_an_action_not_ended_in_time_is_answered_with_an_error_and_the_next_goes_on(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        fake = FakeDesktop.made[-1]
        late = [True]

        def act(code, seconds=None):
            if late.pop() if late else False:
                return {"start": 1.0, "end": 2.0, "reply": None}
            return FakeDesktop.act(fake, code, seconds)
        fake.act = act
        c = driver.Client(s["mcp"][0]["args"][0])
        self.assertTrue(c.call("step", {"actions": ["time.sleep(1000)"]})[0])
        self.assertFalse(c.call("click", dict(zip("xy", driver.centre(u.boxes[u.expected["target"]]))))[0])
        c.close()
        self.assertEqual([c["isError"] for c in u.calls()], [True, False])
        self.assertTrue(driver.score("click", ws, [], "done")["passed"])

    def test_a_step_whose_action_did_not_end_in_time_takes_no_end_and_the_next_goes_on(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        fake = FakeDesktop.made[-1]
        late = [True]

        def act(code, seconds=None):
            if late.pop() if late else False:
                return {"start": 1.0, "end": 2.0, "reply": None}
            return FakeDesktop.act(fake, code, seconds)
        fake.act = act
        c = driver.Client(s["mcp"][0]["args"][0])
        self.assertEqual(c.call("step", {"actions": ["time.sleep(1000)", "WAIT"], "end": "done"}),
                         (True, "the step was not run to its end"))
        self.assertTrue(u.loop.is_alive(), "the kit's loop took the end of a step not run to its end")
        self.assertFalse(c.call("click", dict(zip("xy", driver.centre(u.boxes[u.expected["target"]]))))[0])
        c.close()
        got = driver.score("click", ws, [], "done")
        self.assertTrue(got["passed"], got)
        self.assertEqual(got["detail"]["steps_used"], 2)

    def test_the_reference_reads_the_words_from_the_screenshot_and_fails_without_one(self):
        drawn = lambda fake: _png(" ".join(fake.values["words"]), driver.fixture.SEE_SCALE)
        with mock.patch.object(FakeDesktop, "capture", drawn):          # the start picture, taken as the unit starts
            ws, s = self.unit("see")
            text = " ".join(driver.OPEN["see"].expected["words"])
            self.assertEqual(driver.oracle("see", ws)["answer"], text)
        self.assertTrue(driver.score("see", ws, [], text)["passed"])

        def broken(fake):
            raise desktop.DesktopError("the capture failed")
        with mock.patch.object(FakeDesktop, "capture", broken):
            ws, s = self.unit("see")
            with self.assertRaises(driver.ReferenceFailed):
                driver.oracle("see", ws)
        with mock.patch.object(FakeDesktop, "capture", lambda fake: _png("ACORN", 9)):   # a screen with only one word
            ws, s = self.unit("see")
            with self.assertRaises(driver.ReferenceFailed):
                driver.oracle("see", ws)


class Shutdown(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(desktop, "_CLIENTS_CLOSED", False, create=True))
        self.enterContext(mock.patch.object(desktop, "_CLIENTS", set()))

    def test_shutdown_ends_clients_after_every_unit_was_scored(self):
        p = mock.Mock()
        with mock.patch.dict(driver.OPEN, {}, clear=True), mock.patch.object(desktop, "_CLIENTS", {p}):
            driver.close_all()
        p.kill.assert_called_once_with()
        p.wait.assert_called_once_with(timeout=10)

    def test_shutdown_waits_for_launch_registration_and_refuses_later_launches(self):
        launched, release, draining, killed = (threading.Event() for _ in range(4))
        p = mock.Mock(returncode=-9)
        p.kill.side_effect = killed.set

        def communicate(*a, **kw):
            if not killed.wait(5):
                raise RuntimeError("client was not ended")
            return b"", b""

        def popen(*a, **kw):
            launched.set()                           # the child exists before Popen returns
            if not release.wait(5):
                raise RuntimeError("launch was not released")
            return p

        lock = threading.Lock()

        class Lock:
            def __enter__(self):
                if threading.current_thread() is drain:
                    draining.set()
                if not lock.acquire(timeout=5):
                    raise RuntimeError("client lock was not released")

            def __exit__(self, *a):
                lock.release()

        p.communicate.side_effect = communicate
        replies, counts = [], []
        launch = threading.Thread(target=lambda: replies.append(desktop._engine(["exec"], timeout=10)), daemon=True)
        drain = threading.Thread(target=lambda: counts.append(desktop.end_clients()), daemon=True)
        with mock.patch.object(desktop, "_CLIENTS_LOCK", Lock()), \
                mock.patch.object(desktop.container, "client_env", return_value={}), \
                mock.patch.object(desktop.subprocess, "Popen", side_effect=popen) as start:
            launch.start()
            try:
                self.assertTrue(launched.wait(5))
                drain.start()
                self.assertTrue(draining.wait(5))
                release.set()
                drain.join(5)
                self.assertFalse(drain.is_alive())
                self.assertEqual(counts, [1])
                p.kill.assert_called_once_with()
                p.wait.assert_called_once_with(timeout=10)
                with self.assertRaises(desktop.DesktopError):
                    desktop._engine(["exec"], timeout=10)
                start.assert_called_once()
            finally:
                release.set()
                killed.set()
                launch.join(5)
                if drain.ident is not None:
                    drain.join(5)
        self.assertFalse(launch.is_alive())
        self.assertEqual(len(replies), 1)
        self.assertEqual(desktop._CLIENTS, set())

    def test_the_engines_clients_still_running_are_ended_and_waited_for(self):
        done = []
        with mock.patch.object(desktop.container, "DOCKER", sys.executable):
            t = threading.Thread(target=lambda: done.append(desktop._engine(["-c", "import time; time.sleep(60)"], timeout=120)))
            t.start()
            for _ in range(100):
                if desktop._CLIENTS:
                    break
                time.sleep(0.05)
            self.assertEqual(desktop.end_clients(), 1)
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertNotEqual(done[0].returncode, 0)                                # killed, not left
        self.assertEqual(desktop._CLIENTS, set())

    def test_the_drivers_end_lets_the_units_go_then_ends_the_clients(self):
        u = mock.Mock()
        with mock.patch.dict(driver.OPEN, {"see": u}, clear=True), mock.patch.object(desktop, "end_clients") as end:
            driver.close_all()
        u.close.assert_called_once()
        end.assert_called_once()

    def test_the_socket_folder_is_named_to_the_reaper_before_it_is_made(self):
        told, made = [], []

        real = os.mkdir

        def mkdir(path, mode=0o777):
            real(path, mode)
            if "nullius-computer-" in os.path.basename(path):
                made.append(path)
                raise KeyboardInterrupt("killed just after the folder was made")
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            ws = os.path.join(tmp, "job", "see", "workspace")
            os.makedirs(ws)
            u = driver.Unit("see", ws)
            with mock.patch.object(driver.desktop, "image", return_value="img"), \
                    mock.patch.object(driver.desktop.Desktop, "start", return_value=FakeDesktop("d", {})), \
                    mock.patch.object(driver.REAPER, "tell", side_effect=told.append), \
                    mock.patch.object(driver.os, "mkdir", side_effect=mkdir):
                with self.assertRaises(KeyboardInterrupt):
                    u.start("job")
        shutil.rmtree(made[0], ignore_errors=True)
        self.assertIn(f"+folder {made[0]}", told)


class FixtureRecords(unittest.TestCase):
    def test_every_change_of_a_batch_field_is_recorded(self):
        fixture = driver.fixture
        fields, texts = [], []

        class Widget:
            def __init__(self, *a, **kw):
                self.kw = kw

            def place(self, **kw):
                pass

            def grid(self, **kw):
                pass

            def bind(self, *a, **kw):
                pass

        class Entry(Widget):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                fields.append(self)

        class StringVar:
            def __init__(self, master=None):
                self.value, self.traces = "", []
                texts.append(self)

            def trace_add(self, mode, fn):
                self.traces.append(fn)

            def set(self, value):                       # a change by any route: a key held down, a paste
                self.value = value
                for fn in self.traces:
                    fn("", "", "write")

            def get(self):
                return self.value

        f = fixture.Fixture.__new__(fixture.Fixture)
        f.tk = mock.Mock(Frame=Widget, Label=Widget, Entry=Entry, StringVar=StringVar)
        f.root, f.values = object(), {"labels": list(checks.FIELDS)}
        f.images, f.names, f.texts, f.log_file = [], {}, [], __import__("io").StringIO()
        with mock.patch.object(fixture, "bitmap", return_value=object()):
            f._batch()
        self.assertEqual(len(texts), 3)
        self.assertIs(fields[0].kw.get("textvariable"), texts[0])
        texts[2].set("sanepilo")
        texts[2].set("sanepil")                         # the last key took a letter back
        last = [json.loads(line) for line in f.log_file.getvalue().splitlines()][-1]
        self.assertEqual((last["kind"], last["field"], last["value"]), ("value", "THREE", "sanepil"))


class Reaper(unittest.TestCase):
    def test_unsuccessful_starts_remain_pending_through_an_absent_container(self):
        for outcome in ("timeout", "failed"):
            with self.subTest(outcome=outcome):
                told, engine_calls = [], []

                def engine(args, **kw):
                    engine_calls.append(args[0])
                    if args[0] == "run":
                        if outcome == "timeout":
                            raise subprocess.TimeoutExpired(args, kw["timeout"])
                        return subprocess.CompletedProcess(args, 1, b"", b"start failed")
                    return subprocess.CompletedProcess(args, 1, b"", b"Error: No such container: late")

                u = driver.Unit("see", "/unused/workspace")
                with mock.patch.object(driver.os, "makedirs"), \
                        mock.patch.object(driver.os.path, "exists", return_value=False), \
                        mock.patch.object(desktop, "image", return_value="image"), \
                        mock.patch.object(desktop, "_engine", side_effect=engine), \
                        mock.patch.object(desktop.container, "capped", return_value=[]), \
                        mock.patch.object(driver.REAPER, "tell", side_effect=told.append):
                    with self.assertRaises(desktop.DesktopError):
                        u.start("job")
                self.assertEqual(engine_calls, ["run", "rm", "container"])
                held = reaper.held(told)
                name = held["container"][0]
                with mock.patch.object(reaper, "there", side_effect=[False, True, False]) as there, \
                        mock.patch.object(reaper, "_engine") as remove, \
                        mock.patch.object(reaper.time, "monotonic", return_value=0), \
                        mock.patch.object(reaper.time, "sleep"):
                    reaper.remove("docker", name, name not in held["started"], reaper.START_SECONDS)
                self.assertEqual(there.call_count, 3)
                remove.assert_called_once_with("docker", "rm", "-f", name)
                self.assertNotIn(name, held["started"])

    def test_a_container_whose_start_had_not_ended_is_looked_for_until_it_is_there_and_removed(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            engine, said, state = os.path.join(tmp, "engine"), os.path.join(tmp, "said"), os.path.join(tmp, "n")
            with open(engine, "w") as f:                 # the container appears at the third look, as a late start's
                f.write(f"""#!/bin/sh
echo "$@" >> {said}
n=$(cat {state} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {state}
case "$1" in
  container) if [ -e {state}.gone ] || [ $n -lt 3 ]; then echo "Error: No such container: $5" >&2; exit 1; fi; echo id;;
  rm) touch {state}.gone;;
esac
""")
            os.chmod(engine, 0o755)
            p = subprocess.run([sys.executable, os.path.join(KIT, "reaper.py"), engine, "20"], input="+container late\n",
                               text=True, capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(said) as f:
                lines = f.read().splitlines()
            self.assertIn("rm -f late", lines)
            self.assertEqual(lines[-1].split()[:2], ["container", "inspect"])     # and looked again: gone
            os.remove(said)
            os.remove(state)
            os.remove(state + ".gone")
            p = subprocess.run([sys.executable, os.path.join(KIT, "reaper.py"), engine, "20"],
                               input="+container done\n=container done\n", text=True, capture_output=True, timeout=60)
            with open(said) as f:
                self.assertEqual(f.read().splitlines(), ["container inspect --format {{.Id}} done"])   # started: one look
    def test_what_is_still_named_is_removed_once_the_driver_is_gone(self):
        self.assertEqual(reaper.held(["+container a\n", "+folder /x\n", "+container b\n", "=container b\n",
                                      "-container a\n", "junk\n"]),
                         {"container": ["b"], "folder": ["/x"], "started": ["b"]})
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            engine, said, folder = os.path.join(tmp, "engine"), os.path.join(tmp, "said"), os.path.join(tmp, "left")
            os.makedirs(folder)
            with open(engine, "w") as f:                 # a container is there until it is removed
                f.write(f"""#!/bin/sh
echo "$@" >> {said}
case "$1" in
  container) if [ -e {tmp}/gone-$5 ]; then echo "Error: No such container: $5" >&2; exit 1; fi; echo id;;
  rm) touch {tmp}/gone-$3;;
esac
""")
            os.chmod(engine, 0o755)
            p = subprocess.Popen([sys.executable, os.path.join(KIT, "reaper.py"), engine], stdin=subprocess.PIPE, text=True,
                                 start_new_session=True)
            p.stdin.write(f"+container one\n=container one\n+container two\n=container two\n-container one\n"
                          f"+folder {folder}\n")
            p.stdin.close()                                                    # the driver is gone
            self.assertEqual(p.wait(timeout=30), 0)
            with open(said) as f:
                self.assertEqual([l for l in f.read().splitlines() if l.startswith("rm")], ["rm -f two"])
            self.assertFalse(os.path.exists(folder))

    def test_the_driver_tells_the_reaper_before_the_container_exists(self):
        order = []
        with mock.patch.object(driver.REAPER, "tell", side_effect=lambda line: order.append(line)), \
                mock.patch.object(driver.desktop, "image", return_value="img"), \
                mock.patch.object(driver.desktop.Desktop, "start",
                                  side_effect=lambda *a, **k: order.append("start") or (_ for _ in ()).throw(
                                      desktop.DesktopError("no engine"))):
            with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
                ws = os.path.join(tmp, "job", "see", "workspace")
                os.makedirs(ws)
                with self.assertRaises(desktop.DesktopError):
                    driver.setup("see", ws)
        self.assertTrue(order[0].startswith("+container nullius-desktop-"))
        self.assertEqual(order[1], "start")
        self.assertEqual(len(order), 2)                 # an unsuccessful start stays pending
        self.assertEqual(driver.OPEN, {})


# -- the docker tier: the eight checks on real desktops --------------------------------------------------------------
MARGIN = 30                                   # process starts, protocol messages and scheduling


class DockerTestBounds(unittest.TestCase):
    """The docker tests' waits and assertions exercised without a container or a socket."""

    def supported(self, setup=False):
        limits = []

        def engine(args, **kw):
            limits.append(kw["timeout"])
            data = b""
            if desktop._CAPTURE in args:
                data = b"\x89PNG\r\n\x1a\nfake"
            elif desktop._RUN in args:
                data = json.dumps({"start": 1, "end": 2, "timeout": False, "returncode": 0,
                                   "output": "", "error": ""}).encode()
            return subprocess.CompletedProcess(args, 0, data, b"")

        with mock.patch.object(desktop, "_engine", side_effect=engine), \
                mock.patch.object(container, "capped", return_value=[]):
            if setup:
                default = inspect.signature(container._docker).parameters["timeout"].default

                def image_engine(args, *, timeout=default, **kw):
                    limits.append(timeout)
                    return subprocess.CompletedProcess(args, 0, "", "")

                with mock.patch.object(container, "_built", {}), \
                        mock.patch.object(container, "_docker", side_effect=image_engine):
                    desktop.image()                 # a fresh driver inspects both already-built images
                d = desktop.Desktop.start("image", "fake")
                d.start_fixture({})
                d.events()
            else:
                d = desktop.Desktop("fake")
                d.capture()
                d.act("pass", driver.PYTHON_SECONDS)
                d.capture()
        return sum(limits) + (0 if setup else driver.PAUSE + computer_pipe.END_SECONDS)

    def test_the_setup_deadline_covers_start_readiness_fixture_and_events(self):
        supported = self.supported(setup=True)
        case = DockerKit("test_a_driver_killed_during_a_unit_leaves_no_container_socket_or_process")
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            case.tmp = tmp
            p = mock.Mock(pid=123, stdin=io.StringIO(), stdout=io.StringIO(json.dumps(
                {"mcp": [{"args": [os.path.join(tmp, "socket", "s")]}]}) + "\n"))

            def select_reply(read, write, error, seconds):
                self.assertGreater(seconds, supported)
                return read, [], []

            def run(args, **kw):
                return subprocess.CompletedProcess(args, 0, "456 python reaper.py\n" if args[0] == "ps" else "", "")

            with mock.patch.object(subprocess, "Popen", return_value=p), \
                    mock.patch.object(subprocess, "run", side_effect=run), \
                    mock.patch.object(select, "select", side_effect=select_reply), \
                    mock.patch.object(os, "killpg"), mock.patch(__name__ + "._alive", return_value=False):
                case.test_a_driver_killed_during_a_unit_leaves_no_container_socket_or_process()

    def scripted(self, *, click_error=False, prompt=None, check_timeout=None):
        case = DockerKit("test_a_session_of_the_scripted_acp_agent_reaches_the_server_through_the_pipe")
        setup = {"mcp": [{"args": ["/unused/s"]}], "prompt": "click"}
        case.unit = mock.Mock(return_value=("/unused/workspace", setup))
        u = mock.Mock()
        u.calls.return_value = [{"tool": "screenshot", "isError": False}, {"tool": "click", "isError": click_error}]
        messages = [{"id": 1, "result": {}}, {"id": 2, "result": {"sessionId": "s1"}},
                    {"method": "session/update", "params": {"update": {"content": {"text": "screenshot taken"}}}},
                    {"id": 3, **(prompt if prompt is not None else {"result": {"stopReason": "end_turn"}})}]
        p = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
        original = queue.Queue.get

        def get(q, *, timeout):
            if check_timeout is not None:
                check_timeout(timeout)
            return original(q, timeout=timeout)

        with mock.patch.dict(driver.OPEN, {"click": u}, clear=True), \
                mock.patch.object(subprocess, "Popen", return_value=p), \
                mock.patch.object(queue.Queue, "get", get):
            case.test_a_session_of_the_scripted_acp_agent_reaches_the_server_through_the_pipe()

    def test_the_prompt_deadline_covers_both_captures_the_action_and_pipe_exit(self):
        supported = self.supported()
        self.scripted(check_timeout=lambda seconds: self.assertGreater(seconds, supported))

    def test_the_scripted_session_rejects_a_failed_click_or_prompt(self):
        for bad in ({"click_error": True}, {"prompt": {"error": {"code": -32603, "message": "failed"}}},
                    {"prompt": {"result": {}}}):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                self.scripted(**bad)


def why_no_desktop() -> str:
    if not os.environ.get(docker_tier.VAR):
        return "the docker tier runs with parallel.py --docker"
    return container.unusable() or ""


@unittest.skipIf(why_no_desktop(), why_no_desktop() or "")
class DockerKit(unittest.TestCase):
    """The driver's ops on real desktops, one unit at a time."""

    @classmethod
    def setUpClass(cls):
        desktop.image()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(mock.patch.object(desktop, "_CLIENTS_CLOSED", False))
        self.addCleanup(driver.close_all)

    def unit(self, name):
        ws = os.path.join(self.tmp, "job", f"{name}-{secrets.token_hex(3)}", "workspace")
        os.makedirs(ws)
        return ws, driver.setup(name, ws)

    def test_the_reference_passes_every_check_and_the_null_control_none(self):
        for unit in checks.CHECKS:
            with self.subTest(unit=unit, control="oracle"):
                ws, _ = self.unit(unit)
                answer = driver.oracle(unit, ws)["answer"]
                got = driver.score(unit, ws, [], answer)
                self.assertTrue(got["passed"], got)
            with self.subTest(unit=unit, control="nop"):
                ws, _ = self.unit(unit)
                self.assertFalse(driver.score(unit, ws, [], "")["passed"])

    def test_a_scripted_client_that_acts_past_its_budget_fails(self):
        ws, s = self.unit("limit")
        u = driver.OPEN["limit"]
        c = driver.Client(s["mcp"][0]["args"][0])
        for label in checks.ORDER[:3]:
            x, y = driver.centre(u.boxes[label])
            c.call("click", {"x": x, "y": y})
        c.close()
        got = driver.score("limit", ws, [], "done")
        self.assertFalse(got["passed"])
        self.assertEqual(got["detail"]["past_budget"], 1)

    def test_a_driver_killed_during_a_unit_leaves_no_container_socket_or_process(self):
        job = f"kill-{secrets.token_hex(4)}"                                     # this test's own label
        ws = os.path.join(self.tmp, job, "see-kill", "workspace")
        os.makedirs(ws)
        env = dict(os.environ, PYTHONPATH=APP, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, os.path.join(KIT, "driver.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env=env, start_new_session=True)
        reaper_pid, line = None, ""
        try:
            p.stdin.write(json.dumps({"op": "setup", "unit": "see", "dir": ws}) + "\n")
            p.stdin.flush()
            image_seconds = 2 * inspect.signature(container._docker).parameters["timeout"].default
            seconds = (image_seconds + desktop.RUN_SECONDS + desktop.START_SECONDS + desktop.ENGINE_GRACE
                       + desktop.FIXTURE_SECONDS + desktop.EVENTS_SECONDS + MARGIN)
            ready, _, _ = select.select([p.stdout], [], [], seconds)
            line = p.stdout.readline() if ready else ""
            kids = subprocess.run(["ps", "-o", "pid=,args=", "--ppid", str(p.pid)], capture_output=True, text=True, timeout=30)
            reaper_pid = next((int(l.split()[0]) for l in kids.stdout.splitlines() if "reaper.py" in l), None)
        finally:
            os.killpg(p.pid, signal.SIGKILL)                                    # as a run kills a driver's group
            p.wait(timeout=MARGIN)
        self.assertTrue(line, f"no reply to setup within {seconds:g} s")
        sock = json.loads(line)["mcp"][0]["args"][0]
        self.assertTrue(sock.endswith("/s"))
        self.assertIsNotNone(reaper_pid)
        end = time.monotonic() + reaper.START_SECONDS + 2 * reaper.REMOVE_SECONDS + MARGIN
        while time.monotonic() < end:
            if not _alive(reaper_pid):
                break
            time.sleep(0.5)
        self.assertFalse(_alive(reaper_pid), "this driver's reaper did not end")
        left = subprocess.run([container.DOCKER, "ps", "-aq", "--filter", f"label={container.LABEL}={job}"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual(left.stdout.split(), [])
        self.assertFalse(os.path.exists(os.path.dirname(sock)))

    def test_a_session_of_the_scripted_acp_agent_reaches_the_server_through_the_pipe(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        agent = os.path.join(APP, "tests", "fixtures", "standard", "fake_computer_agent.py")
        p = subprocess.Popen([sys.executable, agent], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
        server = {"name": "computer", "command": sys.executable, "args": [os.path.join(KIT, "pipe.py"), s["mcp"][0]["args"][0]],
                  "env": []}
        said, lines = [], queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(l) for l in p.stdout] + [lines.put(None)], daemon=True)
        reader.start()
        try:
            for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
                      {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": ws, "mcpServers": [server]}},
                      {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                       "params": {"sessionId": "s1", "prompt": [{"type": "text", "text": s["prompt"]}]}}):
                p.stdin.write(json.dumps(m) + "\n")
                p.stdin.flush()
                seconds = (2 * desktop.CAPTURE_SECONDS + driver.PYTHON_SECONDS + desktop.SETTLE
                           + desktop.ENGINE_GRACE + driver.PAUSE + computer_pipe.END_SECONDS + MARGIN)
                end = time.monotonic() + seconds
                while True:
                    line = lines.get(timeout=max(0, end - time.monotonic()))
                    self.assertIsNotNone(line, f"the agent ended before it answered {m['method']}")
                    got = json.loads(line)
                    if got.get("method") == "session/update":
                        said.append(got["params"]["update"]["content"]["text"])
                    if got.get("id") == m["id"]:
                        self.assertNotIn("error", got)
                        if m["method"] == "session/prompt":
                            self.assertEqual(got.get("result"), {"stopReason": "end_turn"})
                        break
        finally:
            p.stdin.close()
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)      # also a pipe still waiting for a desktop reply
                p.wait(timeout=MARGIN)
            reader.join(MARGIN)
            p.stdout.close()
        self.assertFalse(reader.is_alive())
        calls = u.calls()
        self.assertEqual([c["tool"] for c in calls], ["screenshot", "click"])
        self.assertTrue(all(not c["isError"] for c in calls), calls)
        self.assertIn("screenshot taken", "".join(said))


if __name__ == "__main__":
    unittest.main()
