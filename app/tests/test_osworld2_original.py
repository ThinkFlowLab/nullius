"""OSWorld2's Original (osworld2@v2.1): the upstream unchanged, beside the patched osworld2@hardened."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from nullius.standard import definitions

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
SONNET = "anthropic/claude-sonnet-4.6"
GRADER_LINE = "[2026-11-19 10:00:00,000 ERROR llm_metrics/1-MainProcess] Error in compare_text_with_llm: " \
              "anthropic.RateLimitError: Error code: 429\n"


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_original", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


def bench():
    with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
        return json.load(f)


class Started(Exception):
    pass


class Unchanged(unittest.TestCase):
    """Nothing of the hardened version reaches the original's processes."""

    def test_the_original_names_no_patch(self):
        for name in ("driver.py", "nop.py"):
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                code = f.read()
            for patch in ("sitecustomize", "OSWORLD2_STOP", "MAX_RETRIES =", "proxy.py", "PROXY_CONFIG_FILE",
                          "write_mbps", "_signed_out"):
                self.assertNotIn(patch, code, f"{name}: {patch}")
        self.assertFalse(os.path.exists(os.path.join(HERE, "startup")))

    def test_the_versions(self):
        b = bench()
        self.assertEqual(b["default"], "v2.1")
        original, hardened = definitions.resolve(b, "v2.1"), definitions.resolve(b, "hardened")
        self.assertFalse([w for w in original["driver"]["command"] if "hardened" in w])
        self.assertIn("{here}/hardened/startup/sitecustomize.py", hardened["driver"]["command"])
        self.assertNotIn("write_mbps", original["options"])
        self.assertIn("write_mbps", hardened["options"])
        self.assertIs(hardened["setups"]["original"]["comparable"], False)
        self.assertFalse(definitions.comparable(hardened["setups"]["original"], "MiniMax-M3"))
        self.assertEqual(original["setups"]["original"]["comparable"], ["MiniMax-M3"])
        self.assertEqual(original["upstream"]["tested"], hardened["upstream"]["tested"])
        self.assertEqual(original["proven"], {})
        self.assertIn("nop", hardened["proven"]["test_v2"])


class Hardened(unittest.TestCase):
    def test_its_driver_reads_its_own_version_block(self):
        b = bench()
        b["versions"]["hardened"]["setups"]["original"]["turn_seconds"] = 1234
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "standard.json"), "w") as f:
            json.dump(b, f)
        spec = importlib.util.spec_from_file_location("osworld2_driver_hardened_version",
                                                      os.path.join(HERE, "hardened", "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [os.path.join(HERE, "hardened"), *sys.path]), \
                mock.patch.dict(os.environ, NULLIUS_UPSTREAM=tmp):
            spec.loader.exec_module(driver)
        with mock.patch.object(driver, "BENCH", tmp), mock.patch.dict(os.environ, NULLIUS_VERSION="hardened"):
            self.assertEqual(driver._turn_seconds(), 1234)
        with mock.patch.object(driver, "BENCH", tmp), mock.patch.dict(os.environ, NULLIUS_VERSION="v2.1"):
            self.assertEqual(driver._turn_seconds(), 86400)


class Driver(unittest.TestCase):
    def setUp(self):
        self.job = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.job, ignore_errors=True)
        self.dir = os.path.join(self.job, "u1", "workspace")
        os.makedirs(self.dir)
        self.relay = os.path.join(self.job, "u1", ".work", "relay")
        os.makedirs(self.relay)
        self.driver = load_driver(self.dir)
        self.envs, self.task = [], {"instruction": "do it", "proxy": False}

        def run(argv, cwd, env, *a, **kw):
            self.envs.append(env)
            raise Started()

        for name, fake in (("_vm", lambda dir, domain, eid: os.path.join(dir, "vm.qcow2")), ("_run", run),
                           ("_task", lambda *_: dict(self.task))):
            p = mock.patch.object(self.driver, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.judge = {"model": SONNET, "base_url": "http://127.0.0.1:9/u1-judge/v1", "key": "k"}

    def answer(self, eid, **env):
        with mock.patch.dict(os.environ, env):
            return self.driver.answer(f"tasks__{eid}", self.dir, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1",
                                      "k", judge=self.judge)

    def test_the_script_gets_the_upstream_alone_on_its_path(self):
        with self.assertRaises(Started):
            self.answer("001", NULLIUS_OPTION_WEBSITES="172.17.0.1.nip.io")
        env = self.envs[0]
        self.assertEqual(env["PYTHONPATH"], self.dir)
        self.assertNotIn("OSWORLD2_STOP", env)
        self.assertEqual(env["WEBSITE_HOST_SUFFIX"], "172.17.0.1.nip.io")
        self.assertEqual(env["OSWORLD_FILE_BASE_URL"], self.driver.ASSETS)
        self.assertEqual(env["OSWORLD_EVAL_MODEL_NAME"], SONNET)

    def test_gitlab_and_proxy_tasks_are_refused_before_a_vm(self):
        for eid in ("026", "041"):
            with self.assertRaises(RuntimeError) as said:
                self.answer(eid, NULLIUS_OPTION_WEBSITES="172.17.0.1.nip.io")
            self.assertIn("GitLab", str(said.exception))
        self.task["proxy"] = True
        with self.assertRaises(RuntimeError) as said:
            self.answer("036", NULLIUS_OPTION_WEBSITES="172.17.0.1.nip.io")
        self.assertIn("proxy", str(said.exception))
        self.assertEqual(self.envs, [])

    def test_a_website_task_needs_the_suffix(self):
        with mock.patch.dict(os.environ, NULLIUS_OPTION_WEBSITES="none"), self.assertRaises(RuntimeError):
            self.answer("005")
        self.assertEqual(self.envs, [])

    def written(self, value, log=""):
        where = os.path.join(self.dir, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001")
        os.makedirs(where)
        with open(os.path.join(where, "result.txt"), "w") as f:
            f.write(value)
        with open(os.path.join(where, "eval.log"), "w") as f:
            f.write(log)

    def test_a_failed_grader_is_noted_beside_the_upstreams_score(self):
        self.written("0.0", GRADER_LINE)
        out = self.driver.score("tasks__001", self.dir, setup="original", judge=self.judge)
        self.assertEqual(out["reward"], 0.0)
        self.assertIn("LLM grader failed", out["grader_failed"])

    def test_a_refused_grader_call_is_noted_too(self):
        self.written("1")
        with open(os.path.join(self.relay, "u1-judge.jsonl"), "w") as f:
            f.write(json.dumps({"status": 500, "t0": 1}) + "\n")
        out = self.driver.score("tasks__001", self.dir, setup="original", judge=self.judge)
        self.assertEqual(out["reward"], 1.0)
        self.assertIn("500", out["grader_failed"])

    def test_a_score_outside_0_to_1_is_kept_as_written(self):
        self.written("-0.5")
        out = self.driver.score("tasks__001", self.dir, setup="original", judge=self.judge)
        self.assertEqual(out["reward"], -0.5)
        self.assertIn("kept", out["detail"]["out_of_range"])

    def test_no_result_is_no_score(self):
        with self.assertRaises(self.driver.VerifierError):
            self.driver.score("tasks__001", self.dir, setup="original", judge=self.judge)


class Stop(unittest.TestCase):
    """A guard ends the upstream's processes from outside; nothing is asked of its loop."""

    def setUp(self):
        self.job = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.job, ignore_errors=True)
        self.dir = os.path.join(self.job, "u1", "workspace")
        os.makedirs(self.dir)
        self.stop = os.path.join(self.job, "u1", ".work", "relay", "stop")
        os.makedirs(os.path.dirname(self.stop))
        self.driver = load_driver(self.dir)
        self.pid = os.path.join(self.dir, "pid")
        env = mock.patch.dict(os.environ, NULLIUS_UPSTREAM=self.dir)
        env.start()
        self.addCleanup(env.stop)

    def start(self, until=None):
        argv = [sys.executable, "-c", f"import os,time; open({self.pid!r},'w').write(str(os.getpid())); time.sleep(60)"]
        t0 = time.time()
        with self.assertRaises(self.driver.CancelledError) as said:
            self.driver._run(argv, self.dir, dict(os.environ), os.path.join(self.dir, "run.log"), "the run", until=until)
        self.assertLess(time.time() - t0, 30)
        with open(self.pid) as f:
            pid = int(f.read())
        for _ in range(50):
            try:
                with open(f"/proc/{pid}/stat") as f:
                    if f.read().rpartition(")")[2].split()[0] in ("Z", "X"):
                        break
            except OSError:
                break
            time.sleep(0.2)
        else:
            self.fail("the script outlived the stop")
        return str(said.exception)

    def test_a_guards_stop_ends_the_script(self):
        with open(self.stop, "w") as f:
            f.write("context window\nbatch cap\n")
        self.assertIn("batch cap", self.start())

    def test_the_turn_limit_ends_the_script(self):
        self.assertIn("turn limit", self.start(until=time.time() + 2))

    def test_the_context_window_alone_is_not_a_stop(self):
        with open(self.stop, "w") as f:
            f.write("context window\n")
        self.assertIsNone(self.driver._guard(self.dir))


if __name__ == "__main__":
    unittest.main()
