"""The computer kit's desktop and fixture: the verdicts of ``see``, ``click``
and ``type`` on hand-written logs, the action's times taken from the desktop
and never from this machine, and the image declared as an environment of the
kit. The docker tier starts real desktops: the screen drawn, a click and a
word sent by actions and judged right, the same click sent through the
desktop's Python judged wrong."""
from __future__ import annotations

import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import random
import re
import secrets
import struct
import subprocess
import sys
import tempfile
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
session = _module("computer_kit_session", "session.py")

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

    def test_input_rule_counts_pointer_and_key_events_and_enter_only(self):
        events = [ev("ready", 1.0), ev("enter", 100.6, value="x"), ev("press", 100.5, button=1, hit="OCEAN")]
        self.assertEqual(checks.outside(events, ACTIONS), [])
        stray = [ev("scroll", 105.0, button=4), ev("enter", 105.1, value="x"), ev("ready", 105.2)]
        self.assertEqual([e["kind"] for e in checks.outside(stray, ACTIONS)], ["scroll", "enter"])


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


class EarnedPasses(unittest.TestCase):
    """A pass comes from input sent inside actions and seen by the fixture, nothing else."""

    def test_a_press_of_any_mouse_button_on_another_button_fails_click(self):
        exp = {"target": "OCEAN", "labels": ["OCEAN", "APPLE"]}
        for button in (1, 2, 3):
            events = [ev("press", 100.1, button=button, hit="APPLE"), ev("press", 100.2, button=1, hit="OCEAN")]
            self.assertIn("another button", checks.click(exp, events, [{"start": 100, "end": 101}], "")["why"])

    def test_enter_is_input_whose_time_is_checked(self):
        exp = {"word": "kotabiru"}
        self.assertFalse(checks.type_word(exp, [ev("enter", 100, value="kotabiru")], [], "")["passed"])
        late = [ev("key", 100.1, char="k", hit="field"), ev("enter", 105, value="kotabiru")]
        v = checks.type_word(exp, late, [{"start": 100, "end": 101}], "")
        self.assertIn("outside every action", v["why"])

    def test_a_press_and_its_release_in_two_actions_pass(self):
        events = [ev("press", 100.2, hit="OCEAN", button=1), ev("release", 110.2, hit="OCEAN", button=1)]
        actions = [{"start": 100, "end": 101}, {"start": 110, "end": 111}]
        self.assertTrue(checks.click({"target": "OCEAN", "labels": ["OCEAN", "APPLE"]}, events, actions, "")["passed"])


def _run_in_desktop(request, *, monotonic=None, wall=None, wait=None):
    """The desktop's root process (``desktop._RUN``) run here with its process start faked: its printed reply,
    and what it slept."""
    p = mock.Mock()
    p.wait.side_effect = wait or (lambda **kw: 0)
    slept = []
    out = io.StringIO()
    patches = [mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(json.dumps(request).encode()))),
               mock.patch("subprocess.Popen", return_value=p),
               mock.patch("tempfile.TemporaryFile", side_effect=lambda: io.BytesIO()),
               mock.patch("time.sleep", side_effect=slept.append)]
    if monotonic is not None:
        patches.append(mock.patch("time.monotonic", side_effect=monotonic))
    if wall is not None:
        patches.append(mock.patch("time.time", side_effect=wall))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        with contextlib.redirect_stdout(out):
            exec(desktop._RUN, {})
    return json.loads(out.getvalue()), slept


class ActionTimes(unittest.TestCase):
    def test_an_action_answers_only_once_the_verdicts_slack_has_passed_on_the_desktop(self):
        self.assertGreaterEqual(desktop.SETTLE, checks.SLACK)
        run, sent = _engine_answering({"start": 1.0, "end": 2.0, "timeout": False, "returncode": 0, "output": "", "error": ""})
        with mock.patch.object(desktop, "_engine", run):
            desktop.Desktop("d").act("pass")
            desktop.Desktop("d").python("pass")
        self.assertEqual([json.loads(payload)["settle"] for _, payload in sent], [desktop.SETTLE, 0.0])
        said, slept = _run_in_desktop({"code": "pass", "seconds": 5, "limit": 100, "settle": 0.5},
                                      monotonic=[10.0, 11.0, 11.1])
        self.assertEqual((said["start"], said["end"]), (10.0, 11.0))
        self.assertEqual([round(x, 6) for x in slept], [0.4])                 # the reply waits until 11.5

    def test_a_wall_clock_step_does_not_fail_a_correct_action(self):
        f = fixture.Fixture.__new__(fixture.Fixture)
        f.log_file = io.StringIO()

        def the_click(**kw):
            f.write("press", button=1, hit="OCEAN")
            return 0
        said, _ = _run_in_desktop({"code": "pass", "seconds": 1, "limit": 4096}, wall=[100, 100.1, 95, 95, 95],
                                  wait=the_click)
        events = [json.loads(line) for line in f.log_file.getvalue().splitlines()]
        self.assertTrue(checks.click({"target": "OCEAN", "labels": ["OCEAN"]}, events, [said], "")["passed"])


class _FakeX:
    AutoRepeatModeOff = 0
    AutoRepeatModeOn = 1


class Session(unittest.TestCase):
    def test_the_x_server_runs_with_access_control_on_and_as_its_own_account(self):
        argv = session.server_argv()
        self.assertNotIn("-ac", argv)
        self.assertEqual(argv[argv.index("-auth") + 1], session.SERVER_AUTH)
        self.assertEqual(argv[argv.index("-ardelay") + 1], "65535")             # the longest XKB allows
        self.assertNotIn(session.SERVER_UID, (0, session.UID, 1001))
        with open(os.path.join(KIT, "desktop.Dockerfile")) as f:
            self.assertIn(f"useradd -u {session.SERVER_UID} ", f.read())

    def test_the_servers_key_repeat_is_turned_off(self):
        d = mock.Mock()
        d.get_keyboard_control.return_value = mock.Mock(global_auto_repeat=_FakeX.AutoRepeatModeOff)
        with mock.patch.dict(sys.modules, {"Xlib": mock.Mock(X=_FakeX), "Xlib.X": _FakeX}):
            self.assertTrue(session.repeat_off(d))
        d.change_keyboard_control.assert_called_once_with(auto_repeat_mode=_FakeX.AutoRepeatModeOff)

    def test_the_cookie_file_names_this_host_and_any_host(self):
        data = session.xauthority(b"c" * 16, "desktop")
        self.assertTrue(data.startswith(struct.pack(">H", 256) + struct.pack(">H", 7) + b"desktop"))
        self.assertIn(struct.pack(">H", 0xFFFF) + struct.pack(">H", 0), data)
        self.assertEqual(data.count(b"MIT-MAGIC-COOKIE-1"), 2)


def _response(args, rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args, rc, stdout, stderr)


class Lifecycle(unittest.TestCase):
    def test_a_start_that_times_out_removes_what_it_made(self):
        calls = []

        def engine(args, **kw):
            calls.append(args)
            if args[0] == "run":
                raise subprocess.TimeoutExpired(args, kw["timeout"])
            return _response(args, rc=1, stderr=b"Error: No such container: review")
        with mock.patch.object(desktop, "_engine", engine):
            with self.assertRaises(desktop.DesktopError):
                desktop.Desktop.start("image", "review")
        self.assertIn(["rm", "-f", "review"], calls)

    def test_a_desktop_never_ready_is_removed_even_when_its_log_cannot_be_read(self):
        calls = []

        def engine(args, **kw):
            calls.append(args)
            if args[0] == "logs":
                raise subprocess.TimeoutExpired(args, kw["timeout"])
            if args[0] == "container":
                return _response(args, rc=1, stderr=b"Error: No such container: review")
            return _response(args, rc=1 if args[0] == "exec" else 0)
        with mock.patch.object(desktop, "_engine", engine):
            with self.assertRaises(desktop.DesktopError) as e:
                desktop.Desktop.start("image", "review")
        self.assertIn("not ready", str(e.exception))
        self.assertIn(["rm", "-f", "review"], calls)

    def test_remove_fails_when_the_engine_does_or_the_container_stays(self):
        with mock.patch.object(desktop, "_engine", return_value=_response([], rc=1, stderr=b"busy")):
            with self.assertRaises(desktop.DesktopError):
                desktop.Desktop("review").remove()
        with mock.patch.object(desktop, "_engine", return_value=_response([], rc=0, stdout=b"abc")):
            with self.assertRaises(desktop.DesktopError):                    # removed, yet inspect still finds it
                desktop.Desktop("review").remove()
        gone = _response([], rc=1, stderr=b"Error: No such container: review")

        def busy_then_gone(args, **kw):                                      # the engine's own failure is said
            return _response(args, rc=1, stderr=b"busy") if args[0] == "rm" else gone
        with mock.patch.object(desktop, "_engine", busy_then_gone):
            with self.assertRaises(desktop.DesktopError):
                desktop.Desktop("review").remove()
        with mock.patch.object(desktop, "_engine", return_value=gone):
            desktop.Desktop("review").remove()                               # gone already: no error

    def test_the_fixture_says_ready_only_when_its_window_is_up_in_time(self):
        def fake(viewable, size, focus):
            f = fixture.Fixture.__new__(fixture.Fixture)
            f.root = mock.Mock()
            f.root.winfo_viewable.return_value = viewable
            f.root.winfo_width.return_value, f.root.winfo_height.return_value = size
            f.root.winfo_screenwidth.return_value, f.root.winfo_screenheight.return_value = desktop.SCREEN
            f.root.focus_get.return_value = focus
            f.field, f.names, f.values = None, {}, {"screen": "see"}
            f.log_file = io.StringIO()
            return f
        for viewable, size, focus in ((False, (1, 1), mock.Mock()), (True, (1920, 1), mock.Mock()),
                                      (True, desktop.SCREEN, None)):
            f = fake(viewable, size, focus)
            with self.assertRaises(RuntimeError):
                f.up(seconds=0)
            self.assertNotIn('"ready"', f.log_file.getvalue())
        f = fake(True, desktop.SCREEN, mock.Mock())
        f.up(seconds=0)
        self.assertIn('"ready"', f.log_file.getvalue())

    def test_a_log_that_cannot_be_read_or_holds_a_line_that_is_no_event_is_an_error(self):
        for answer in (_response([], rc=1, stderr=b"No such file"),
                       _response([], stdout=b'{"kind": "ready", "t": 1.0}\n{"kind": "press"\n'),
                       _response([], stdout=b'{"kind": "press"}\n')):
            with mock.patch.object(desktop, "_engine", return_value=answer):
                with self.assertRaises(desktop.DesktopError):
                    desktop.Desktop("d").events()
        with mock.patch.object(desktop, "_engine", return_value=_response([], stdout=b'{"kind": "ready", "t": 1.5}\n')):
            self.assertEqual(desktop.Desktop("d").events(), [{"kind": "ready", "t": 1.5}])


class Reads(unittest.TestCase):
    """``desktop._READ`` run here, as the desktop runs it: a regular file alone, and at most the bound."""

    def read(self, path, limit):
        return subprocess.run([sys.executable, "-I", "-c", desktop._READ, path, str(limit)], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=20)

    def test_a_read_is_bounded_and_of_a_regular_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = os.path.join(tmp, "small")
            with open(small, "wb") as f:
                f.write(b"abcd")
            r = self.read(small, 4)
            self.assertEqual((r.returncode, r.stdout), (0, b"abcd"))
            self.assertEqual(self.read(small, 3).returncode, 5)
            self.assertEqual(self.read(os.path.join(tmp, "none"), 4).returncode, 3)
            self.assertEqual(self.read(tmp, 4).returncode, 4)
            fifo = os.path.join(tmp, "fifo")
            os.mkfifo(fifo)
            self.assertEqual(self.read(fifo, 4).returncode, 4)                # never waits for a writer
        self.assertEqual(self.read("/dev/zero", 4).returncode, 4)
        self.assertEqual(desktop.READ_LIMIT, 12 * 1024 * 1024)

    def test_a_refused_read_is_the_agents_error_and_the_bound_goes_to_the_desktop(self):
        sent = []
        for rc, outcome in ((4, desktop.Refused), (5, desktop.Refused), (3, None)):
            def engine(args, **kw):
                sent.append(args)
                return _response(args, rc=rc)
            with mock.patch.object(desktop, "_engine", engine):
                if outcome is None:
                    self.assertIsNone(desktop.Desktop("d").read("x"))
                else:
                    with self.assertRaises(outcome):
                        desktop.Desktop("d").read("x")
        self.assertEqual(sent[0][-2:], ["x", str(desktop.READ_LIMIT)])


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


_PREFIX = os.environ.get("NULLIUS_TEST_CONTAINER_PREFIX", "nullius-test")


def why_no_desktop() -> str:
    """Why the desktop's tests cannot run: the tier was not asked for, or no engine answers. A missing
    image is no reason: the tests build it."""
    if not os.environ.get(docker_tier.VAR):
        return "the docker tier runs with parallel.py --docker"
    return container.unusable() or ""


class TierGate(unittest.TestCase):
    def test_the_desktop_tests_run_wherever_an_engine_answers_and_build_the_image(self):
        with mock.patch.dict(os.environ, {docker_tier.VAR: "1"}), mock.patch.object(container, "unusable", return_value=None), \
                mock.patch.object(container, "present", return_value=False):
            self.assertEqual(why_no_desktop(), "")
        with mock.patch.object(desktop, "image", side_effect=container.Unavailable("no build")):
            with self.assertRaises(container.Unavailable):
                DockerDesktop.setUpClass()


@unittest.skipIf(why_no_desktop(), why_no_desktop() or "")
class DockerDesktop(unittest.TestCase):
    """Real desktops of the kit's image, one at a time; the image is built first when it is missing."""

    @classmethod
    def setUpClass(cls):
        cls.image = desktop.image()

    def setUp(self):
        self.d = desktop.Desktop.start(self.image, f"{_PREFIX}-desktop-{secrets.token_hex(4)}")
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
        probe = subprocess.run([container.DOCKER, "ps", "-aq", "--filter", f"name=^{self.d.name}$"],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.split(), [])

    def test_the_x_server_is_unprivileged_and_refuses_a_client_without_the_cookie(self):
        said = self.d.python("import os\nfor p in os.listdir('/proc'):\n    if p.isdigit():\n"
                             "        try:\n            s = open(f'/proc/{p}/status').read()\n        except OSError:\n"
                             "            continue\n        if 'Name:\\tXvfb' in s:\n"
                             "            print([l.split()[1] for l in s.splitlines() if l.startswith('Uid:')][0])")
        self.assertEqual(said["output"].split(), [str(session.SERVER_UID)])
        refused = self.d.python("import os\nopen('/home/user/no-cookie', 'w').close()\n"
                                "os.environ['XAUTHORITY'] = '/home/user/no-cookie'\n"
                                "from Xlib import display\ndisplay.Display(':0')")
        self.assertIn("Display", refused["error"])                        # the server's refusal, not a missing file
        self.assertNotEqual(refused["returncode"], 0)
        self.assertEqual(self.d.python("from Xlib import display\ndisplay.Display(':0').close()")["returncode"], 0)

    def test_a_key_held_from_one_action_to_the_next_sends_no_repeat(self):
        values, exp = checks.draw("type")
        self.d.start_fixture(values)
        self.d.act("pyautogui.keyDown('a')\ntime.sleep(1.5)")
        self.d.act("time.sleep(1.0)")
        self.d.act("pyautogui.keyUp('a')\npyautogui.press('enter')")
        events = self.d.events()
        self.assertEqual([e["kind"] for e in events if e["kind"] in ("key", "keyup") and e.get("keysym") == "a"],
                         ["key", "keyup"])
        self.assertEqual(checks.outside(events, self.d.actions), [])

    def test_the_desktop_ends_when_its_window_manager_does(self):
        self.d.python("import os, signal\nfor p in os.listdir('/proc'):\n    if p.isdigit():\n        try:\n"
                      "            name = open(f'/proc/{p}/comm').read().strip()\n        except OSError:\n            continue\n"
                      "        if name == 'openbox':\n            os.kill(int(p), signal.SIGKILL)")
        state = ""
        for _ in range(40):
            r = subprocess.run([container.DOCKER, "container", "inspect", "--format", "{{.State.Running}}", self.d.name],
                               capture_output=True, text=True, timeout=60)
            state = r.stdout.strip()
            if state == "false":
                break
            time.sleep(0.25)
        self.assertEqual(state, "false")


if __name__ == "__main__":
    unittest.main()
