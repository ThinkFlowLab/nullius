"""The computer kit's eight checks and its driver: each verdict on hand-written records; the driver's wiring
(the port's server and the kit's loop over a desktop, the turn's end at score, the budget, the reference, a unit
let go without its score, the reaper) over a fake desktop, with no container; the docker tier runs the eight
checks on real desktops, the reference passing each and the null control none."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from nullius.standard import computer_port as port
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

    def test_scroll_passes_on_the_code_once_the_list_moved(self):
        exp = {"code": "ACDE34679HJK"}
        moved = [ev("scroll", 100.2, button=5, hit="list"), ev("moved", 100.21, top=0.4)]
        self.assertTrue(checks.verdict("scroll", exp, moved, ACTIONS, "ACDE34679HJK")["passed"])
        self.assertIn("never moved", checks.verdict("scroll", exp, moved[:1], ACTIONS, "ACDE34679HJK")["why"])
        self.assertIn("does not hold", checks.verdict("scroll", exp, moved, ACTIONS, "")["why"])

    def test_recover_passes_on_a_success_after_the_error_and_the_click(self):
        exp = {"target": "OCEAN", "labels": ["OCEAN", "APPLE"]}
        calls = [{"tool": "screenshot", "isError": False}, {"tool": "click", "isError": True},
                 {"tool": "click", "isError": False}]
        events = press(110.2, "OCEAN")
        self.assertTrue(checks.verdict("recover", exp, events, ACTIONS, "", calls)["passed"])
        self.assertIn("no acting call was answered with an error",
                      checks.verdict("recover", exp, events, ACTIONS, "", calls[2:])["why"])
        self.assertIn("succeeded after", checks.verdict("recover", exp, events, ACTIONS, "", calls[:2])["why"])
        self.assertFalse(checks.verdict("recover", exp, [], ACTIONS, "", calls)["passed"])

    def test_batch_passes_when_each_field_holds_its_word(self):
        exp = {"words": {"ONE": "kotabiru", "TWO": "mafodafi", "THREE": "sanepilo"}}
        events = [ev("value", 100.1 + i / 10, field=f, value=w) for i, (f, w) in enumerate(exp["words"].items())]
        self.assertTrue(checks.verdict("batch", exp, events, ACTIONS, "")["passed"])
        wrong = events + [ev("value", 100.5, field="TWO", value="mafodaf")]
        self.assertIn("TWO", checks.verdict("batch", exp, wrong, ACTIONS, "")["why"])

    def test_every_check_fails_on_input_outside_every_action(self):
        passing = {
            "see": ({"code": "ACDE34679HJK"}, [], "ACDE34679HJK", ()),
            "click": ({"target": "OCEAN", "labels": ["OCEAN"]}, press(100.2, "OCEAN"), "", ()),
            "type": ({"word": "kotabiru"}, [ev("enter", 100.3, value="kotabiru")], "", ()),
            "limit": (LIMIT, press(100.2, "ONE"), "", ()),
            "drag": ({}, [ev("ready", 99, boxes={"zone": [500, 500, 700, 700]}), ev("press", 100.2, button=1, hit="handle"),
                          ev("release", 100.8, button=1, hit="handle", x=600, y=600)], "", ()),
            "scroll": ({"code": "ACDE34679HJK"}, [ev("moved", 100.3, top=0.5)], "ACDE34679HJK", ()),
            "recover": ({"target": "OCEAN", "labels": ["OCEAN"]}, press(110.2, "OCEAN"), "",
                        ({"tool": "click", "isError": True}, {"tool": "click", "isError": False})),
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
        pipe = os.path.normpath(kit["mcp_servers"]["computer"]["command"][1].replace("{here}", KIT))
        self.assertEqual(pipe, os.path.join(os.path.dirname(os.path.dirname(KIT)), "computer_pipe.py"))
        self.assertTrue(os.path.isfile(pipe))


# -- the driver over a fake desktop -------------------------------------------------------------------------------------
class FakeDesktop:
    """A desktop with no container: the fixture's boxes, and the input each click, write or key of an action (or
    of a python call) would send, logged at the time it runs, as the fixture would."""
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


class Wiring(unittest.TestCase):
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
        self.addCleanup(driver.close_all)

    def unit(self, name):
        ws = os.path.join(self.tmp, "job", name, "workspace")
        os.makedirs(ws)
        return ws, driver.setup(name, ws)

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
        self.assertEqual([l.split()[0] for l in self.told], ["+container", "+folder", "-container", "-folder"])
        self.assertNotIn("click", driver.OPEN)

    def test_the_turns_end_at_score_ends_the_loop_and_judges_what_is_there(self):
        ws, s = self.unit("click")
        u = driver.OPEN["click"]
        t = time.monotonic()
        got = driver.score("click", ws, [], "")                                 # no call: the null control
        self.assertLess(time.monotonic() - t, 5)
        self.assertFalse(got["passed"])
        self.assertFalse(u.loop.is_alive())
        self.assertEqual(got["detail"]["steps_used"], 0)

    def test_a_client_that_types_through_python_fails(self):
        ws, s = self.unit("type")
        u = driver.OPEN["type"]
        c = driver.Client(s["mcp"][0]["args"][0])
        c.call("python", {"code": f"import pyautogui; pyautogui.write({u.expected['word']!r}); pyautogui.hotkey('enter')"})
        c.close()
        got = driver.score("type", ws, [], "done")
        self.assertFalse(got["passed"])
        self.assertIn("outside every action", got["detail"]["why"])

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


class Reaper(unittest.TestCase):
    def test_what_is_still_named_is_removed_once_the_driver_is_gone(self):
        self.assertEqual(reaper.held(["+container a\n", "+folder /x\n", "+container b\n", "-container a\n", "junk\n"]),
                         {"container": ["b"], "folder": ["/x"]})
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            engine, said, folder = os.path.join(tmp, "engine"), os.path.join(tmp, "said"), os.path.join(tmp, "left")
            os.makedirs(folder)
            with open(engine, "w") as f:
                f.write(f"#!/bin/sh\necho \"$@\" >> {said}\n")
            os.chmod(engine, 0o755)
            p = subprocess.Popen([sys.executable, os.path.join(KIT, "reaper.py"), engine], stdin=subprocess.PIPE, text=True,
                                 start_new_session=True)
            p.stdin.write(f"+container one\n+container two\n-container one\n+folder {folder}\n")
            p.stdin.close()                                                    # the driver is gone
            self.assertEqual(p.wait(timeout=30), 0)
            with open(said) as f:
                self.assertEqual(f.read(), "rm -f two\n")
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
        self.assertEqual(driver.OPEN, {})


# -- the docker tier: the eight checks on real desktops --------------------------------------------------------------
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

    def test_scripted_clients_that_type_through_python_or_act_past_their_budget_fail(self):
        ws, s = self.unit("type")
        u = driver.OPEN["type"]
        c = driver.Client(s["mcp"][0]["args"][0])
        x, y = driver.centre(u.boxes["field"])
        c.call("python", {"code": f"import pyautogui\npyautogui.click({x}, {y})\npyautogui.write({u.expected['word']!r})\n"
                                  "pyautogui.press('enter')"})
        c.close()
        got = driver.score("type", ws, [], "done")
        self.assertFalse(got["passed"])
        self.assertIn("outside every action", got["detail"]["why"])
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
        ws = os.path.join(self.tmp, "job", "see-kill", "workspace")
        os.makedirs(ws)
        env = dict(os.environ, PYTHONPATH=APP, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, os.path.join(KIT, "driver.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env=env, start_new_session=True)
        try:
            p.stdin.write(json.dumps({"op": "setup", "unit": "see", "dir": ws}) + "\n")
            p.stdin.flush()
            reply = json.loads(p.stdout.readline())
        finally:
            os.killpg(p.pid, signal.SIGKILL)                                    # as a run kills a driver's group
            p.wait()
        sock = reply["mcp"][0]["args"][0]
        self.assertTrue(sock.endswith("/s"))
        for _ in range(120):
            left = subprocess.run([container.DOCKER, "ps", "-aq", "--filter", "label=" + container.LABEL + "=job",
                                   "--filter", "name=nullius-desktop-"], capture_output=True, text=True, timeout=60)
            if left.returncode == 0 and not left.stdout.split() and not os.path.exists(os.path.dirname(sock)):
                break
            time.sleep(0.5)
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual(left.stdout.split(), [])
        self.assertFalse(os.path.exists(os.path.dirname(sock)))
        procs = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=30).stdout
        self.assertNotIn(os.path.join(KIT, "reaper.py"), procs)


if __name__ == "__main__":
    unittest.main()
