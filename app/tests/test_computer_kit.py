"""The computer kit's desktop and fixture: the verdicts of ``see``, ``click``
and ``type`` on hand-written logs, the action's times taken from the desktop
and never from this machine, and the image declared as an environment of the
kit. The docker tier starts real desktops: the screen drawn, a click and a
word sent by actions and judged right, the same click sent through the
desktop's Python judged wrong."""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import random
import re
import secrets
import struct
import subprocess
import time
import unittest
from unittest import mock

from nullius.standard import container, definitions
from tests import docker_tier

KIT = os.path.join(definitions.KITS, "agent-computer-v1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


checks = _module("computer_kit_checks", "checks.py")
desktop = _module("computer_kit_desktop", "desktop.py")
fixture = _module("computer_kit_fixture", "fixture.py")

ACTIONS = [{"start": 100.0, "end": 101.0}, {"start": 110.0, "end": 112.0}]


def ev(kind, t, **kw):
    return {"kind": kind, "t": t, **kw}


def press(t, hit, button=1):
    return [ev("press", t, button=button, x=10, y=10, hit=hit), ev("release", t + 0.05, button=button, x=10, y=10, hit=hit)]


def keys(t, text):
    out = []
    for i, ch in enumerate(text):
        out += [ev("key", t + i * 0.01, keysym=ch, char=ch, hit="field"), ev("keyup", t + i * 0.01 + 0.005, keysym=ch, hit="field")]
    return out


CLICK = {"target": "OCEAN", "labels": ["APPLE", "BRICK", "OCEAN", "RIVER", "STONE", "TIGER"]}


class Verdicts(unittest.TestCase):
    def test_see_passes_on_the_code_in_any_case_and_spacing_and_fails_without_it(self):
        exp = {"code": "ACDE34679HJK"}
        self.assertTrue(checks.verdict("see", exp, [], [], "The code is acde-3467 9hjk.")["passed"])
        self.assertFalse(checks.verdict("see", exp, [], [], "The code is ACDE34679HJ.")["passed"])
        self.assertFalse(checks.verdict("see", exp, [], [], "")["passed"])
        self.assertFalse(checks.verdict("see", exp, [], [], None)["passed"])

    def test_click_passes_on_a_left_press_on_the_named_button_inside_an_action(self):
        events = [ev("ready", 99.0)] + press(100.5, "OCEAN")
        self.assertEqual(checks.verdict("click", CLICK, events, ACTIONS, "done"), {"passed": True, "why": ""})

    def test_click_fails_without_the_press_or_with_another_button_or_another_mouse_button(self):
        self.assertIn("no left press", checks.verdict("click", CLICK, press(100.5, "RIVER"), ACTIONS, "")["why"])
        self.assertIn("no left press", checks.verdict("click", CLICK, press(100.5, "OCEAN", button=3), ACTIONS, "")["why"])
        both = press(100.2, "RIVER") + press(110.5, "OCEAN")
        self.assertIn("another button", checks.verdict("click", CLICK, both, ACTIONS, "")["why"])
        self.assertFalse(checks.verdict("click", CLICK, [], ACTIONS, "")["passed"])
        # a press on the background is no other button
        self.assertTrue(checks.verdict("click", CLICK, press(100.2, "background") + press(110.5, "OCEAN"), ACTIONS, "")["passed"])

    def test_click_fails_when_any_input_lies_outside_every_action(self):
        by_python = press(105.0, "OCEAN")                                  # between two actions
        v = checks.verdict("click", CLICK, by_python, ACTIONS, "")
        self.assertFalse(v["passed"])
        self.assertIn("outside every action", v["why"])
        before = press(99.0, "OCEAN")                                      # before the first action started
        self.assertFalse(checks.verdict("click", CLICK, before, ACTIONS, "")["passed"])
        stray_key = press(100.5, "OCEAN") + [ev("key", 120.0, keysym="a", char="a", hit="background")]
        self.assertFalse(checks.verdict("click", CLICK, stray_key, ACTIONS, "")["passed"])
        self.assertFalse(checks.verdict("click", CLICK, press(100.5, "OCEAN"), [], "")["passed"])

    def test_an_event_taken_in_just_after_its_action_ended_counts_inside_and_one_later_does_not(self):
        late = [ev("press", 101.0 + checks.SLACK * 0.6, button=1, hit="OCEAN")]
        self.assertTrue(checks.verdict("click", CLICK, late, ACTIONS, "")["passed"])
        later = [ev("press", 101.0 + checks.SLACK * 1.4, button=1, hit="OCEAN")]
        self.assertFalse(checks.verdict("click", CLICK, later, ACTIONS, "")["passed"])
        self.assertLess(checks.SLACK, 1.0)                                 # under the kit's pause after an action

    def test_type_passes_on_exactly_the_word_at_the_last_enter_inside_actions(self):
        exp = {"word": "kotabiru"}
        events = keys(100.1, "kotabiru") + [ev("enter", 100.5, value="kotabiru")]
        self.assertEqual(checks.verdict("type", exp, events, ACTIONS, ""), {"passed": True, "why": ""})
        fixed = keys(100.1, "kotabir") + [ev("enter", 100.3, value="kotabir")] + keys(110.1, "u") + [ev("enter", 110.2, value="kotabiru")]
        self.assertTrue(checks.verdict("type", exp, fixed, ACTIONS, "")["passed"])

    def test_type_fails_on_another_text_a_later_change_no_enter_or_keys_outside_actions(self):
        exp = {"word": "kotabiru"}
        self.assertIn("held 'kotabirux'", checks.verdict("type", exp, [ev("enter", 100.5, value="kotabirux")], ACTIONS, "")["why"])
        self.assertFalse(checks.verdict("type", exp, [ev("enter", 100.5, value="Kotabiru")], ACTIONS, "")["passed"])
        spoiled = [ev("enter", 100.5, value="kotabiru"), ev("enter", 110.5, value="kotabiru2")]
        self.assertFalse(checks.verdict("type", exp, spoiled, ACTIONS, "")["passed"])
        self.assertIn("never pressed", checks.verdict("type", exp, keys(100.1, "kotabiru"), ACTIONS, "")["why"])
        by_python = keys(105.0, "kotabiru") + [ev("enter", 105.2, value="kotabiru")]
        self.assertIn("outside every action", checks.verdict("type", exp, by_python, ACTIONS, "")["why"])

    def test_input_rule_counts_pointer_and_key_events_only(self):
        events = [ev("ready", 1.0), ev("enter", 105.0, value="x"), ev("press", 100.5, button=1, hit="OCEAN")]
        self.assertEqual(checks.outside(events, ACTIONS), [])
        self.assertEqual([e["kind"] for e in checks.outside([ev("scroll", 105.0, button=4)], ACTIONS)], ["scroll"])


class Draw(unittest.TestCase):
    def test_each_check_draws_from_its_own_values_and_expects_what_the_screen_shows(self):
        rng = random.Random(7)
        values, exp = checks.draw("see", rng)
        self.assertEqual(len(exp["code"]), checks.CODE_LENGTH)
        self.assertTrue(set(exp["code"]) <= set(checks.ALPHABET))
        self.assertEqual(values, {"screen": "see", "code": exp["code"]})
        values, exp = checks.draw("click", rng)
        self.assertEqual(len(set(values["labels"])), 6)
        self.assertIn(exp["target"], values["labels"])
        values, exp = checks.draw("type", rng)
        self.assertEqual(values, {"screen": "type"})                     # the word is the prompt's, never the screen's
        self.assertRegex(exp["word"], r"^[a-z]{8}$")
        with self.assertRaises(ValueError):
            checks.draw("drag")

    def test_every_character_drawn_has_a_glyph_of_five_by_seven(self):
        for ch in checks.ALPHABET + "".join(checks.LABELS):
            self.assertIn(ch, fixture.GLYPHS)
        for ch, rows in fixture.GLYPHS.items():
            self.assertEqual((len(rows), {len(r) for r in rows}), (7, {5}), ch)
        cols, rows, cells = fixture.pixels("AC")
        self.assertEqual((cols, rows), (11, 7))
        self.assertIn((0, 3), cells)                                      # A's bar
        self.assertNotIn((5, 3), cells)                                   # the blank column


class Image(unittest.TestCase):
    def test_the_desktop_is_an_environment_of_the_kit_tagged_by_its_text_and_files(self):
        base = "nullius/base:0123456789ab"
        tag = container.env_tag(desktop.KIT, desktop.ENVIRONMENT, base)
        self.assertRegex(tag, r"^nullius/env-agent-computer-v1-desktop:[0-9a-f]{12}$")
        text, files = container.env_context(desktop.KIT, desktop.ENVIRONMENT, base)
        self.assertTrue(text.startswith(f"FROM {base}\n"))
        body = text.split("\n", 1)[1]
        self.assertNotRegex(body, r"(?im)^\s*(FROM|ADD)\b")
        for line in re.findall(r"(?im)^COPY\s+(.+)$", body):
            for src in line.split()[:-1]:
                self.assertIn(src, files)
        self.assertRegex(desktop.tag(), r"^nullius/env-agent-computer-v1-desktop:[0-9a-f]{12}$")

    def test_every_python_package_is_pinned_by_version_and_hash(self):
        with open(os.path.join(KIT, "desktop-requirements.txt")) as f:
            reqs = re.sub(r"\\\n", " ", f.read())
        lines = [ln for ln in reqs.splitlines() if ln.strip() and not ln.startswith("#")]
        names = set()
        for ln in lines:
            self.assertRegex(ln, r"^[A-Za-z0-9_.-]+==[0-9][^ ]*\s+(--hash=sha256:[0-9a-f]{64}\s*)+$", ln)
            names.add(ln.split("==")[0].lower())
        self.assertTrue({"pyautogui", "pillow"} <= names)

    def test_the_package_carries_every_file_the_image_is_built_from(self):
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            line = next(ln for ln in f if ln.startswith('"nullius.standard"'))
        patterns = re.findall(r'"(kits/[^"]+)"', line)
        for rel in [desktop.KIT["environments"]["desktop"]["dockerfile"], *desktop.KIT["environments"]["desktop"]["files"]]:
            self.assertTrue(any(fnmatch.fnmatch(f"kits/agent-computer-v1/{rel}", p) for p in patterns), rel)


def _engine_answering(said):
    """A stand-in for the engine: every exec answers ``said`` as the desktop's root process prints it."""
    sent = []

    def run(args, *, input=None, timeout):
        sent.append((args, input))
        out = b"" if said is None else (json.dumps(said) + "\n").encode()
        return subprocess.CompletedProcess(args, 0, out, b"")
    return run, sent


class Actions(unittest.TestCase):
    def test_an_action_keeps_the_times_the_desktop_read_never_this_machines(self):
        said = {"start": 5000.25, "end": 5001.5, "timeout": False, "returncode": 0, "output": "ok\n", "error": ""}
        run, sent = _engine_answering(said)
        d = desktop.Desktop("d")
        with mock.patch.object(desktop, "_engine", run), mock.patch("time.time", return_value=1.0):
            got = d.act("pyautogui.click(10, 20)")
        self.assertEqual(d.actions, [{"start": 5000.25, "end": 5001.5}])
        self.assertEqual(got["reply"], {"status": "success", "output": "ok\n", "error": "", "returncode": 0})
        args, payload = sent[0]
        self.assertEqual(args[:4], ["exec", "-i", "-u", desktop.ROOT])
        code = json.loads(payload)["code"]
        self.assertTrue(code.startswith(desktop.ACTION_PREFIX))
        self.assertTrue(code.endswith("pyautogui.click(10, 20)"))
        self.assertIn("import pyautogui", code)
        self.assertIn("import time", code)

    def test_python_is_no_action_and_an_action_past_its_bound_keeps_its_times(self):
        said = {"start": 7.0, "end": 9.0, "timeout": True, "returncode": -9, "output": "", "error": ""}
        run, _ = _engine_answering(said)
        d = desktop.Desktop("d")
        with mock.patch.object(desktop, "_engine", run):
            self.assertIsNone(d.python("import time; time.sleep(99)", seconds=2))
            self.assertEqual(d.actions, [])
            self.assertIsNone(d.act("time.sleep(99)", seconds=2)["reply"])
        self.assertEqual(d.actions, [{"start": 7.0, "end": 9.0}])

    def test_a_desktop_that_gives_no_answer_is_an_error_and_keeps_no_time(self):
        run, _ = _engine_answering(None)
        d = desktop.Desktop("d")
        with mock.patch.object(desktop, "_engine", run):
            with self.assertRaises(desktop.DesktopError):
                d.act("pyautogui.press('a')")
        self.assertEqual(d.actions, [])

        def late(args, *, input=None, timeout):
            raise subprocess.TimeoutExpired(args, timeout)
        with mock.patch.object(desktop, "_engine", late):
            self.assertIsNone(d.python("print(1)"))
            self.assertIsNone(d.act("pyautogui.press('a')")["reply"])
        self.assertEqual(d.actions, [])


def _png_size(data):
    return struct.unpack(">II", data[16:24])


# What the desktop's Python reads back from its own screen: the cells of ink, a glyph cell 9 pixels square,
# counted from the ink's top left corner.
_READ_INK = r"""
import json
from PIL import Image, ImageChops, ImageGrab
im = ImageGrab.grab(xdisplay=":0").convert("RGB")
mask = ImageChops.difference(im, Image.new("RGB", im.size, (17, 17, 17))).convert("L").point(lambda v: 255 if v == 0 else 0)
x0, y0, x1, y1 = mask.getbbox()
px = mask.load()
print(json.dumps([[c, r] for r in range((y1 - y0) // 9) for c in range((x1 - x0) // 9) if px[x0 + c * 9 + 4, y0 + r * 9 + 4]]))
"""


def _from_corner(cells):
    c0, r0 = min(c for c, _ in cells), min(r for _, r in cells)
    return sorted((c - c0, r - r0) for c, r in cells)


PAUSE = 1.0
_PREFIX = os.environ.get("NULLIUS_TEST_CONTAINER_PREFIX", "nullius-test")


@unittest.skipIf(docker_tier.why_not(desktop.tag()), docker_tier.why_not(desktop.tag()) or "")
class DockerDesktop(unittest.TestCase):
    """Real desktops of the kit's image, one at a time."""

    def setUp(self):
        self.d = desktop.Desktop.start(desktop.tag(), f"{_PREFIX}-desktop-{secrets.token_hex(4)}")
        self.addCleanup(self.d.remove)

    def test_the_five_operations(self):
        png = self.d.capture()
        self.assertEqual(_png_size(png), desktop.SCREEN)
        said = self.d.python("import os, pyautogui; print(os.getuid(), os.environ['HOME'], pyautogui.size())")
        self.assertEqual(said["status"], "success")
        self.assertEqual(said["returncode"], 0, said["error"])
        self.assertEqual(said["output"].split()[:2], ["1000", "/home/user"])
        self.assertIn("1920", said["output"])
        self.assertEqual(self.d.python("import sys; sys.exit(4)")["returncode"], 4)
        self.assertIsNone(self.d.python("import time; time.sleep(30)", seconds=2))
        self.assertEqual(self.d.upload("dir/file.bin", b"\x00\x01" * 1000), 2000)
        self.assertEqual(self.d.upload("dir/file.bin", b"end", append=True), 2003)
        self.assertEqual(self.d.read("/home/user/dir/file.bin"), b"\x00\x01" * 1000 + b"end")
        self.assertIsNone(self.d.read("no/such/file"))
        self.assertEqual(self.d.password(), "")
        sudo = self.d.python("import shutil; print(shutil.which('sudo'))")
        self.assertEqual(sudo["output"].strip(), "None")

    def test_the_fixture_is_read_from_the_screen_alone(self):
        values, exp = checks.draw("see")
        self.d.start_fixture(values)
        self.assertIsNone(self.d.read(desktop.FIXTURE_LOG))                       # user cannot read the log
        said = self.d.python(f"open({desktop.FIXTURE_LOG!r})")
        self.assertIn("PermissionError", said["error"])
        seen = self.d.python(_READ_INK)
        self.assertEqual(seen["returncode"], 0, seen["error"])
        _, _, cells = fixture.pixels(exp["code"])
        self.assertEqual(_from_corner(json.loads(seen["output"])), _from_corner(cells))
        self.assertTrue(checks.verdict("see", exp, self.d.events(), self.d.actions, exp["code"])["passed"])

    def test_a_click_by_an_action_passes_and_the_same_click_by_python_fails(self):
        values, exp = checks.draw("click")
        self.d.start_fixture(values)
        ready = self.d.events()[0]
        self.assertEqual(ready["kind"], "ready")
        x0, y0, x1, y1 = ready["boxes"][exp["target"]]
        x, y = (x0 + x1) // 2, (y0 + y1) // 2
        self.assertEqual(self.d.act(f"pyautogui.click({x}, {y})")["reply"]["returncode"], 0)
        self.assertEqual(checks.verdict("click", exp, self.d.events(), self.d.actions, ""), {"passed": True, "why": ""})
        time.sleep(PAUSE)                                                 # the kit's pause after an action
        self.d.python(f"import pyautogui; pyautogui.click({x}, {y})")
        v = checks.verdict("click", exp, self.d.events(), self.d.actions, "")
        self.assertFalse(v["passed"])
        self.assertIn("outside every action", v["why"])

    def test_a_word_typed_by_an_action_passes(self):
        values, exp = checks.draw("type")
        self.d.start_fixture(values)
        x0, y0, x1, y1 = self.d.events()[0]["boxes"]["field"]
        self.d.act(f"pyautogui.click({(x0 + x1) // 2}, {(y0 + y1) // 2})")
        self.d.act(f"pyautogui.write({exp['word']!r}, interval=0.02)\npyautogui.press('enter')")
        events = self.d.events()
        self.assertEqual(checks.verdict("type", exp, events, self.d.actions, ""), {"passed": True, "why": ""})
        self.assertEqual([e["value"] for e in events if e["kind"] == "enter"], [exp["word"]])

    def test_removal_leaves_no_container(self):
        self.d.remove()
        left = subprocess.run([container.DOCKER, "ps", "-aq", "--filter", f"name=^{self.d.name}$"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
