"""OSWorld2's null control: two setup attempts in nop.py, and a failed setup the score's error, never a 0.
A fake upstream and fake nop.py scripts stand in."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from nullius.standard.contract import definitions
from nullius.standard.runner import run

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
WORDS = "the task's own words"
FAILED = {"error": "EnvironmentSetupError", "cause": "TimeoutError", "attempts": 2}
TASK_LOADER = """def find_task_class_path(*a):
    return True
def resolve_task_json_path(**kw):
    return ""
def load_task_config(path, **kw):
    return {"instruction": "", "proxy": False}
"""
DESKTOP_ENV = """import json, os
MAX_RETRIES = 5
class EnvironmentSetupError(RuntimeError):
    pass
class DesktopEnv:
    def __init__(self, **kw):
        pass
    def close(self):
        out = os.environ["FAKE_OUT"]
        with open(os.path.join(out, "closed"), "w") as f:
            json.dump(os.path.exists(os.path.join(out, "setup_failed.json")), f)
"""
LIB_RUN_SINGLE = """import os
from desktop_env import desktop_env
def run_single_example(agent, env, example, max_steps, instruction, args, out, scores):
    with open(os.path.join(out, "attempts"), "w") as f:
        f.write(str(desktop_env.MAX_RETRIES))
    how = os.environ["FAKE_RUN"]
    if how == "setup":
        try:
            raise TimeoutError(%(words)r)
        except TimeoutError as e:
            raise desktop_env.EnvironmentSetupError(%(words)r) from e
    if how == "other":
        raise ValueError(%(words)r)
    with open(os.path.join(out, "result.txt"), "w") as f:
        f.write("0.0")
""" % {"words": WORDS}
FAKE_NOP = """import json, os, sys, time
print("Started container with ports", flush=True)
print("[2026-11-07 01:02:03,456 ERROR desktopenv.setup] guest said: %(words)s", flush=True)
print("[2026-11-07 01:02:04,567 ERROR desktopenv.env] Environment setup failed with exception, retrying (1/2): "
      "%(words)s", flush=True)
out = sys.argv[-1]
if os.environ.get("FAKE_FAILED"):
    with open(os.path.join(out, "setup_failed.json"), "w") as f:
        json.dump(%(failed)r, f)
with open(os.path.join(out, "result.txt"), "w") as f:
    f.write("0.0")
time.sleep(float(os.environ.get("FAKE_HANG", "0")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
""" % {"failed": FAILED, "words": WORDS}


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_nop", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


class ScoreLimit(unittest.TestCase):
    def test_the_score_step_waits_an_hour(self):
        b = definitions.get("benchmark", "osworld2")
        self.assertEqual(b["limits"]["score_seconds"], 3600)
        self.assertFalse([p for p in definitions.problems(b, "benchmark") if "limits" in p])
        r = run.Run(b, definitions.control(b, "nop"), model="", provider="openai", env={"OPENAI_API_KEY": "k"},
                    which=lambda p: "/bin/" + p)
        self.assertEqual(r.score_seconds, 3600)


class ScoringPrograms(unittest.TestCase):
    def test_the_control_and_the_original_refuse_without_them(self):
        b = definitions.get("benchmark", "osworld2")
        for setup in ("yours", "original"):
            with self.assertRaisesRegex(run.Refused, r"scoring_programs \(tesseract not on PATH\)"):
                run.resolve_bench_options(b, "test_v2", {}, setup, lambda p: None if p == "tesseract" else "/bin/" + p)
            self.assertIn("scoring_programs", run.resolve_bench_options(b, "test_v2", {}, setup, lambda p: "/bin/" + p)[1]["host"])
        self.assertEqual(b["host"]["scoring_programs"]["check"], ["identify", "convert", "ffmpeg", "ffprobe", "tesseract"])


class TwoAttempts(unittest.TestCase):
    """nop.py itself, on a fake upstream."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        up = os.path.join(self.tmp, "up")
        os.makedirs(os.path.join(up, "desktop_env"))
        for name, text in (("task_loader.py", TASK_LOADER), ("lib_run_single.py", LIB_RUN_SINGLE),
                           (os.path.join("desktop_env", "__init__.py"), ""),
                           (os.path.join("desktop_env", "desktop_env.py"), DESKTOP_ENV)):
            with open(os.path.join(up, name), "w") as f:
                f.write(text)
        self.up = up
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def nop(self, how):
        env = dict(os.environ, PYTHONPATH=self.up, FAKE_OUT=self.out, FAKE_RUN=how)
        return subprocess.run([sys.executable, os.path.join(HERE, "nop.py"), "vm", "pw", "examples", "tasks", "001",
                               self.out], env=env, capture_output=True, text=True, timeout=60)

    def read(self, name):
        with open(os.path.join(self.out, name)) as f:
            return f.read()

    def test_a_failed_setup_is_said_before_the_close_with_no_words_of_the_task(self):
        done = self.nop("setup")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(self.read("setup_failed.json")), FAILED)
        self.assertNotIn(WORDS, self.read("setup_failed.json"))
        self.assertEqual(self.read("attempts"), "2")
        self.assertEqual(json.loads(self.read("closed")), True)
        self.assertFalse(os.path.exists(os.path.join(self.out, "result.txt")))
        self.assertIn("the null control's setup of tasks__001 failed: EnvironmentSetupError", done.stderr)

    def test_another_failure_is_not_the_setups(self):
        self.assertEqual(self.nop("other").returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.out, "setup_failed.json")))
        self.assertEqual(json.loads(self.read("closed")), False)

    def test_a_run_that_sets_up_scores(self):
        self.assertEqual(self.nop("ok").returncode, 0)
        self.assertEqual(self.read("attempts"), "2")
        self.assertEqual(self.read("result.txt"), "0.0")
        self.assertFalse(os.path.exists(os.path.join(self.out, "setup_failed.json")))

    def test_the_driver_reads_the_file_nop_writes(self):
        driver = load_driver(self.tmp)
        spec = importlib.util.spec_from_file_location("osworld2_nop_constants", os.path.join(HERE, "nop.py"))
        with open(spec.origin) as f:
            self.assertIn(f'SETUP_FAILED = "{driver.SETUP_FAILED}"', f.read())


class EndsEarly(unittest.TestCase):
    """The driver's score of the null control, with a fake nop.py whose log quotes the task."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        self.driver = load_driver(self.tmp)
        self.driver.START_LOCK = os.path.join(self.tmp, "start.lck")
        self.driver.CLOSE_SECONDS = 1
        fake = os.path.join(self.tmp, "nop.py")
        with open(fake, "w") as f:
            f.write(FAKE_NOP)
        self.reaped, started = [], []

        def popen(*a, **kw):
            started.append(subprocess.Popen(*a, **kw))
            return started[-1]

        def reaper(pgid, *_):
            self.reaped.append(time.time())
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

        def end():
            for p in started:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()

        self.addCleanup(end)
        self.driver.subprocess = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess)
                                                          if not k.startswith("__") and k != "Popen"}, Popen=popen)

        for name, value in (("NOP", fake), ("_reaper", reaper), ("_remove_vms", lambda *a: None),
                            ("_noting", lambda *a: None), ("_vm", lambda *a: os.path.join(self.unit, "vm.qcow2")),
                            ("_task", lambda *_: {"instruction": "", "proxy": False}), ("_assets", lambda *a: "assets")):
            p = mock.patch.object(self.driver, name, value)
            p.start()
            self.addCleanup(p.stop)

    def nop(self, **env):
        with mock.patch.dict(os.environ, **env):
            return self.driver.score("tasks__001", self.unit)

    def test_a_failed_setup_is_the_scores_error_not_a_0(self):
        with self.assertRaises(RuntimeError) as raised:
            self.nop(FAKE_FAILED="1")
        said = str(raised.exception)
        self.assertIn("the environment's setup failed (EnvironmentSetupError from TimeoutError, after 2 attempts), "
                      "and the task was not scored", said)
        self.assertIsInstance(raised.exception, self.driver.EnvironmentSetupError)
        self.assertNotIn(WORDS, said)
        self.assertEqual(len(self.reaped), 1)

    def test_a_process_that_does_not_end_after_its_failure_is_handed_to_the_reaper(self):
        t0 = time.time()
        with self.assertRaises(RuntimeError) as raised:
            self.nop(FAKE_FAILED="1", FAKE_HANG="30")
        self.assertLess(time.time() - t0, 10)
        self.assertIn("did not end 1 s after its failure, and was stopped", str(raised.exception))
        self.assertIn("EnvironmentSetupError", str(raised.exception))
        self.assertNotIn(WORDS, str(raised.exception))
        self.assertTrue(self.reaped)

    def test_a_failure_said_by_an_earlier_run_is_not_this_ones(self):
        where = os.path.join(self.unit, "results", "nop", "tasks", "001")
        os.makedirs(where)
        with open(os.path.join(where, "setup_failed.json"), "w") as f:
            json.dump(FAILED, f)
        self.assertEqual(self.nop()["reward"], 0.0)

    def test_a_setup_that_succeeds_scores(self):
        self.assertEqual(self.nop()["reward"], 0.0)
        self.assertEqual(len(self.reaped), 1)

    def test_the_original_setup_is_never_ended_by_the_null_controls_file(self):
        given = []

        def run(argv, cwd, env, log, what, **kw):
            given.append(kw)
            raise RuntimeError("stop here")

        with mock.patch.object(self.driver, "_run", run), \
                mock.patch.object(self.driver, "_command", lambda *a: ({"script": "run.py", "env": {}}, os.path.join(
                    self.unit, "meta.json"), ["run.py"], {})), \
                self.assertRaises(RuntimeError):
            self.driver.answer("tasks__001", self.unit, "original", "MiniMax-M3", "http://127.0.0.1:9/v1", "k")
        self.assertEqual([{k: v for k, v in kw.items() if k != "until"} for kw in given], [{"stagger": True}])


if __name__ == "__main__":
    unittest.main()
