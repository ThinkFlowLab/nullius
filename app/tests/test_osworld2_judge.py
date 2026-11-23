"""OSWorld2's LLM grader, simulated user and judge route, and its pinned dataset lists.
With NULLIUS_OSWORLD2_UPSTREAM set, the lists are also checked against the task classes, read as syntax trees."""
from __future__ import annotations

import ast
import contextlib
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nullius import providers
from nullius.standard.calls import relay
from nullius.standard.contract import definitions
from nullius.standard.runner import run

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
OFFLINE36 = ("001 002 004 010 012 022 029 030 040 042 044 045 047 048 049 051 053 054 059 061 063 064 066 067 "
             "076 080 084 085 088 094 103 104 105 106 107 108").split()
MORE27 = "003 006 009 011 014 017 023 024 025 027 032 033 034 071 074 078 083 086 087 089 090 092 093 095 096 097 099".split()
PROXY = "036 037 050 055 056 062 075 098".split()
CATCHING = "003 008 019 025 031 041 077 079 082 090 092".split()
SONNET = "anthropic/claude-sonnet-4.6"


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_judge", os.path.join(HERE, "driver.py"))
    driver = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(driver)
    return driver


def bench():
    return definitions.get("benchmark", "osworld2", root=definitions.CATALOGUE)


class Started(Exception):
    pass


class Driver(unittest.TestCase):
    def setUp(self):
        self.job = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.job, ignore_errors=True)
        trial = os.path.join(self.job, "u1")
        self.tmp = os.path.join(trial, "workspace")
        self.relayed = os.path.join(trial, ".work", "relay", "u1-judge.jsonl")
        os.makedirs(os.path.dirname(self.relayed))
        os.makedirs(self.tmp)
        self.driver = load_driver(self.tmp)
        self.judge = {"model": SONNET, "base_url": "http://127.0.0.1:9/u1-judge/v1", "key": relay.PLACEHOLDER_KEY}
        self.vms, self.envs = [], []

        def vm(dir, domain, eid):
            self.vms.append(eid)
            return os.path.join(dir, "vm.qcow2")

        def run(argv, cwd, env, *a, **kw):
            self.envs.append(env)
            raise Started()

        for name, fake in (("_vm", vm), ("_run", run), ("_task", lambda *_: {"instruction": "", "proxy": False}),
                           ("_assets", lambda *a: "assets")):
            p = mock.patch.object(self.driver, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def answer(self, eid, **kw):
        return self.driver.answer(f"tasks__{eid}", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1",
                                  relay.PLACEHOLDER_KEY, **kw)

    def nop(self, eid, **kw):
        return self.driver.score(f"tasks__{eid}", self.tmp, **kw)

    def test_the_grader_gets_the_judge_route_in_the_original_and_the_null_control(self):
        for start in (self.answer, self.nop):
            with self.subTest(start.__name__), self.assertRaises(Started):
                start("003", judge=self.judge)
            env = self.envs[-1]
            self.assertEqual({k: v for k, v in env.items() if k.startswith("OSWORLD_EVAL_MODEL_")},
                             {"OSWORLD_EVAL_MODEL_PROVIDER": "anthropic", "OSWORLD_EVAL_MODEL_NAME": SONNET,
                              "OSWORLD_EVAL_MODEL_BASE_URL": "http://127.0.0.1:9/u1-judge",
                              "OSWORLD_EVAL_MODEL_API_KEY": relay.PLACEHOLDER_KEY})

    def test_only_a_trailing_v1_leaves_the_route(self):
        with self.assertRaises(Started):
            self.nop("001", judge=dict(self.judge, base_url="http://h:1/v1-judge/v1"))
        self.assertEqual(self.envs[-1]["OSWORLD_EVAL_MODEL_BASE_URL"], "http://h:1/v1-judge")

    def test_the_simulated_user_asks_sonnet_on_the_units_own_route(self):
        with self.assertRaises(Started):
            self.answer("024", judge=self.judge)
        env = self.envs[-1]
        self.assertEqual({k: v for k, v in env.items() if k.startswith("OSWORLD_USER_SIM_")},
                         {"OSWORLD_USER_SIM_PROVIDER": "anthropic", "OSWORLD_USER_SIM_MODEL": "claude-sonnet-4-6",
                          "OSWORLD_USER_SIM_BASE_URL": "http://127.0.0.1:9/u1",
                          "OSWORLD_USER_SIM_API_KEY": relay.PLACEHOLDER_KEY})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9/u1")

    def test_a_graded_task_with_no_judge_route_is_refused_before_its_vm(self):
        for start in (self.answer, self.nop):
            for eid in ("003", "093"):
                with self.subTest(f"{start.__name__} {eid}"), self.assertRaises(RuntimeError) as raised:
                    start(eid)
                self.assertIn(f"task {eid} is scored by the LLM grader", str(raised.exception))
        self.assertEqual(self.vms, [])

    def test_a_bad_judge_route_is_refused_before_the_vm(self):
        with self.assertRaises(RuntimeError) as raised:
            self.nop("001", judge={"model": SONNET, "base_url": ""})
        self.assertIn("judge route needs", str(raised.exception))
        self.assertEqual(self.vms, [])

    def test_an_ungraded_task_runs_with_no_judge_route_and_no_grader_settings(self):
        for start in (self.answer, self.nop):
            with self.subTest(start.__name__), self.assertRaises(Started):
                start("001")
            self.assertFalse([k for k in self.envs[-1] if k.startswith("OSWORLD_EVAL_MODEL_")])
        self.assertEqual(self.vms, ["001", "001"])

    def test_a_simulated_user_with_no_route_is_refused_before_its_vm(self):
        agents = json.loads(json.dumps(self.driver.AGENTS))
        for k in [k for k in agents["agents"]["m3"]["env"] if k.startswith("OSWORLD_USER_SIM_")]:
            del agents["agents"]["m3"]["env"][k]
        with mock.patch.object(self.driver, "AGENTS", agents):
            with self.assertRaises(RuntimeError) as raised:
                self.answer("095", judge=self.judge)
            self.assertIn("task 095 has a simulated user", str(raised.exception))
            self.assertEqual(self.vms, [])
            with self.assertRaises(Started):
                self.answer("001", judge=self.judge)

    def result(self, where, value="0.0"):
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "result.txt"), "w") as f:
            f.write(value)

    def log(self, path, *lines):
        with open(path, "a") as f:
            f.write("".join(ln + "\n" for ln in lines))

    def test_a_failed_grader_is_the_original_scores_error_not_a_0(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)
        self.assertEqual(self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)["reward"], 0.0)
        failed = "\x1b[1;33m[2026-11-06 01:02:03 \x1b[31mERROR \x1b[32mllm_metrics/388-Worker\x1b[1;33m] \x1b[0m" \
                 "Error in compare_text_with_llm: Error code: 402 - insufficient credits"
        for log in (os.path.join(where, "eval.log"), os.path.join(self.tmp, "run_multienv_m3.log")):
            with self.subTest(os.path.basename(log)):
                self.log(log, "[2026-11-06 01:02:00 INFO desktop_env/1] evaluating", failed)
                with self.assertRaises(RuntimeError) as raised:
                    self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)
                said = str(raised.exception)
                self.assertIn("the LLM grader failed in compare_text_with_llm (HTTP 402), and the upstream", said)
                self.assertIn("see the judge's calls", said)
                self.assertNotIn("insufficient credits", said)
                self.assertNotIn("\x1b", said)
                os.remove(log)

    def test_an_earlier_runs_results_are_moved_aside_before_the_runner(self):
        self.result(os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001"), "1.0")
        with self.assertRaises(Started):
            self.answer("001", judge=self.judge)
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "results")))
        (earlier,) = glob.glob(os.path.join(self.tmp, "results.*"))
        self.assertTrue(glob.glob(os.path.join(earlier, "**", "result.txt"), recursive=True))

    def test_an_earlier_runs_logs_are_moved_aside_and_its_grader_failure_is_not_this_runs(self):
        failed = "[2026-11-06 01:02:03 ERROR llm_metrics/388-Worker] Error in compare_text_with_llm: Error code: 402 - x"
        for name in ("run_multienv_m3.log", "nop.log", "setup.log"):
            self.log(os.path.join(self.tmp, name), failed)
        with self.assertRaises(Started):
            self.answer("003", judge=self.judge)
        self.assertEqual(sorted(os.path.basename(p).rsplit(".", 1)[0] for p in glob.glob(os.path.join(self.tmp, "*.log.*"))),
                         ["nop.log", "run_multienv_m3.log", "setup.log"])
        self.result(os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003"), "1.0")
        self.assertEqual(self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)["reward"], 1.0)
        self.log(os.path.join(self.tmp, "nop.log"), failed)
        with contextlib.suppress(RuntimeError):
            self.driver.setup("tasks__003", self.tmp)
        nop = os.path.join(self.tmp, "results", "nop", "tasks", "003")
        with mock.patch.object(self.driver, "_run", lambda *a, **kw: self.result(nop, "1.0")):
            self.assertEqual(self.nop("003", judge=self.judge)["reward"], 1.0)

    def test_a_unit_an_earlier_run_or_its_reaper_still_holds_is_not_begun(self):
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                                   "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
                                   "print(flush=True)\ntime.sleep(30)", os.path.join(self.tmp, "unit.lck")],
                                  stdout=subprocess.PIPE)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        with mock.patch.object(self.driver, "HELD_SECONDS", 1), self.assertRaises(RuntimeError) as raised:
            self.answer("001", judge=self.judge)
        self.assertIn("an earlier run of this unit, or its reaper, still holds its folder", str(raised.exception))
        holder.kill()
        holder.wait()
        with self.assertRaises(Started):
            self.answer("001", judge=self.judge)
        self.assertEqual(self.driver._OWN, [])
        self.driver._fresh(self.tmp)
        with mock.patch.object(self.driver.subprocess, "Popen") as started:
            self.driver._arm(os.getpid(), self.tmp, True)
        os.close(self.driver._ARMED.pop(os.getpid()))
        self.assertIn(self.driver._OWN[0], started.call_args.kwargs["pass_fds"])
        self.result(os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001"), "1.0")
        self.driver.score("tasks__001", self.tmp, setup="original")
        self.assertEqual(self.driver._OWN, [])

    def test_a_result_outside_0_to_1_is_no_score(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001")
        for value in ("nan", "inf", "-inf", "-0.5", "1.5"):
            with self.subTest(value):
                self.result(where, value)
                with self.assertRaises(RuntimeError) as raised:
                    self.driver.score("tasks__001", self.tmp, setup="original")
                self.assertIn("not a score from 0 to 1", str(raised.exception))
        for value in ("0", "0.25", "1.0", "1.0000000000000002"):
            self.result(where, value)
            self.assertEqual(self.driver.score("tasks__001", self.tmp, setup="original")["reward"], float(value))

    def test_a_failed_graders_message_says_its_status_and_class_never_its_words(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)
        words = "the task's own words"
        records = {"Error code: 429 - " + words: "HTTP 429",
                   "RateLimitError: " + words: "RateLimitError",
                   "anthropic.APIStatusError: status code 529 " + words: "anthropic.APIStatusError, HTTP 529",
                   "openai.BadRequestError: " + words: "openai.BadRequestError",
                   "anthropic._exceptions.OverloadedError: " + words: "anthropic.OverloadedError",
                   "httpx.ReadTimeout " + words: "httpx.ReadTimeout",
                   "TimeoutError: " + words: "TimeoutError",
                   words + " 500 times": "no HTTP status or known error class in its record"}
        log = os.path.join(where, "eval.log")
        for record, said in records.items():
            with self.subTest(said):
                self.log(log, f"[2026-11-06 01:02:03 ERROR llm_metrics/388-Worker] Error in compare_text_with_llm: {record}")
                with self.assertRaises(RuntimeError) as raised:
                    self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)
                self.assertIn(f"compare_text_with_llm ({said})", str(raised.exception))
                self.assertNotIn(words, str(raised.exception))
                os.remove(log)

    def test_a_word_shaped_like_a_class_is_never_said(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)
        log = os.path.join(where, "eval.log")
        for record in ("SyntheticPrivateDocumentError: the rest", "PrivateLedgerException the rest",
                       "anthropic.SyntheticPrivateError: x", "SyntheticPrivate.RateLimitError: x",
                       "secret.module.ValueError: x", "RateLimitErrorOfTheTask: x", "rate_limit_error: x",
                       "anthropic.RateLimitErrorXYZ: x"):
            with self.subTest(record):
                self.log(log, f"[2026-11-06 01:02:03 ERROR llm_metrics/388-Worker] Error in compare_text_with_llm: {record}")
                with self.assertRaises(RuntimeError) as raised:
                    self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)
                said = str(raised.exception)
                self.assertIn("compare_text_with_llm (no HTTP status or known error class in its record)", said)
                for word in ("Synthetic", "Private", "secret", "XYZ", "OfTheTask", "rate_limit"):
                    self.assertNotIn(word, said)
                os.remove(log)

    def test_a_refusing_endpoints_error_text_is_not_said(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)
        self.calls({"status": 400, "request": {"n": 1}, "error": "status 400: the task's own words"})
        with self.assertRaises(RuntimeError) as raised:
            self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)
        self.assertIn("answered 400", str(raised.exception))
        self.assertNotIn("own words", str(raised.exception))
        os.remove(self.relayed)

    def test_the_marks_words_outside_the_graders_error_record_change_nothing(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where, "1.0")
        words = "Error in compare_text_with_llm: Error code: 402"
        self.log(os.path.join(self.tmp, "run_multienv_m3.log"),
                 f"\x1b[1;33m[2026-11-06 01:02:03,004 \x1b[31mINFO \x1b[32mrun_multienv_m3/512-Worker\x1b[1;33m] \x1b[0m"
                 f"Step 3: pyautogui.typewrite('{words}')",
                 f"[2026-11-06 01:02:03,004 ERROR lib_run_single/88-Worker] step failed: {words}",
                 f"[2026-11-06 01:02:03,004 ERROR run_multienv_m3/600-Worker] {words}",
                 words)
        self.log(os.path.join(where, "eval.log"), f"[2026-11-06 01:02:03,004 INFO llm_metrics/40-Worker] {words}")
        self.assertEqual(self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)["reward"], 1.0)

        def run(argv, cwd, env, log, *a, **kw):
            self.result(argv[-1])
            self.log(log, f"[2026-11-06 01:02:04,005 INFO desktopenv.env] user said: {words}",
                     f"[2026-11-06 01:02:04,005 ERROR desktopenv.experiment] {words}")

        with mock.patch.object(self.driver, "_run", run):
            self.assertEqual(self.nop("003", judge=self.judge)["reward"], 0.0)

    def calls(self, *calls):
        with open(self.relayed, "w") as f:
            f.write("".join(json.dumps(c) + "\n" for c in calls) + "not json\n")

    def test_a_grader_endpoints_failure_no_retry_recovered_refuses_the_score(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)

        def run(argv, cwd, env, log, *a, **kw):
            self.result(argv[-1])

        asked, other = {"n_messages": 1, "last": ["grade this"], "sha256": "a"}, {"n_messages": 1, "last": ["grade that"], "sha256": "b"}
        undigested = {"n_messages": 1, "last": ["grade this"]}
        ok = lambda req: {"status": 200, "request": req, "reply": {"text": "YES"}}
        bad = lambda status, req: {"status": status, "request": req, "error": f"status {status}: credits"}
        refused = {"402 then another call's 200": [bad(402, asked), ok(other)],
                   "429 three times": [bad(429, asked), bad(429, asked), bad(429, asked)],
                   "500 last": [ok(other), bad(500, asked)],
                   "400 then 200 on another request": [bad(400, other), ok(asked)],
                   "402, another call, then 200 to its words": [bad(402, asked), ok(other), ok(asked)],
                   "no request recorded": [{"status": 500, "error": "x"}, {"status": 200}],
                   "a late 200 recorded after the last failure": [dict(bad(500, asked), t0=2), dict(ok(asked), t0=1)],
                   "no digest of the request": [bad(429, undigested), ok(undigested)],
                   "a failure with no start time": [dict(ok(asked), t0=1), bad(429, asked)]}
        recovered = {"none": [], "429 then 200": [bad(429, asked), ok(asked)],
                     "503, 503, 200": [bad(503, asked), bad(503, asked), ok(asked), ok(other)],
                     "an abandoned 502 recorded after its retry's 200": [dict(ok(asked), t0=2), dict(bad(502, asked), t0=1)]}
        with mock.patch.object(self.driver, "_run", run):
            for name, calls in refused.items():
                self.calls(*calls)
                for setup, start in (("original", lambda: self.driver.score("tasks__003", self.tmp, setup="original",
                                                                            judge=self.judge)),
                                     ("nop", lambda: self.nop("003", judge=self.judge))):
                    with self.subTest(f"{name} {setup}"), self.assertRaises(RuntimeError) as raised:
                        start()
                    self.assertIn("the LLM grader's endpoint answered", str(raised.exception))
                    self.assertIsInstance(raised.exception, self.driver.VerifierError)
                    self.assertIn("see the judge's calls", str(raised.exception))
            for name, calls in recovered.items():
                self.calls(*calls)
                with self.subTest(name):
                    self.assertEqual(self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)["reward"], 0.0)
                    self.assertEqual(self.nop("003", judge=self.judge)["reward"], 0.0)
            os.remove(self.relayed)
            self.assertEqual(self.nop("003", judge=self.judge)["reward"], 0.0)
            self.calls(bad(402, asked))
            self.assertEqual(self.nop("001")["reward"], 0.0)
            self.assertEqual(self.nop("001", judge=dict(self.judge, base_url="http://h:1/u1/v1"))["reward"], 0.0)

    def test_the_agents_file_refused_keeps_the_upstreams_score_by_the_images_sent_never_the_words(self):
        where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "003")
        self.result(where)
        asked = {"n_messages": 1, "sha256": "a"}
        whole, broken = {"type": "image/png", "is": "image/png", "bytes": 4000}, {"type": "image/png", "is": None, "bytes": 4000}
        big = dict(whole, bytes=self.driver.IMAGE_BYTES + 1)
        said = "messages.0.content.1.image.source.base64.data: could not process image, too large"
        bad = lambda status, *images: dict({"status": status, "request": asked, "error": f"HTTP {status}: {said}"},
                                           **({"images": list(images)} if images else {}))
        file, size = bad(400, whole, broken), bad(413, whole, big)
        record = "[2026-11-06 01:02:04 ERROR desktopenv.env] Error in compare_images_with_llm: Error code: {} - x"
        score = lambda: self.driver.score("tasks__003", self.tmp, setup="original", judge=self.judge)

        def run(argv, cwd, env, log, *a, **kw):
            self.result(argv[-1])

        def nop():
            try:
                return self.nop("003", judge=self.judge)
            finally:
                shutil.rmtree(os.path.join(self.tmp, "results", "nop"), ignore_errors=True)

        with mock.patch.object(self.driver, "_run", run):
            for calls, refused in (([file], ["agent file"]), ([size, file], ["agent file", "agent file size"]),
                                   ([bad(422, {"type": "image/bmp", "is": "image/bmp", "bytes": 10})], ["agent file"])):
                self.calls(*calls)
                with open(os.path.join(where, "eval.log"), "w") as f:
                    f.write("".join(record.format(c["status"]) + "\n" for c in calls))
                with self.subTest(refused=refused):
                    self.assertEqual((score()["reward"], score()["grader_refused"]), (0.0, refused))
                    self.assertEqual(dict(nop(), detail=None), {"reward": 0.0, "detail": None, "grader_refused": refused})
            os.remove(os.path.join(where, "eval.log"))
            for name, calls in (("the provider's words alone", [bad(400)]), ("whole images", [bad(400, whole, whole)]),
                                ("a whole image said to be another type", [bad(400, dict(whole, type="image/jpeg"))]),
                                ("a 413 of whole images", [bad(413, whole)]), ("a 413 of a broken one", [bad(413, broken)]),
                                ("a 401", [bad(401, broken)]), ("with a 429", [file, bad(429)])):
                self.calls(*calls)
                for setup, start in (("original", score), ("nop", nop)):
                    with self.subTest(f"{name} {setup}"), self.assertRaises(self.driver.VerifierError) as raised:
                        start()
                    self.assertIn("the LLM grader's endpoint answered", str(raised.exception))
        self.calls(file)
        for status in (429, 400):
            with open(os.path.join(where, "eval.log"), "w") as f:
                f.write(record.format(400) + "\n" + record.format(status) + "\n")
            with self.subTest(status=status), self.assertRaises(self.driver.VerifierError) as raised:
                score()
            self.assertIn(f"compare_images_with_llm (HTTP {status})", str(raised.exception))
        os.remove(os.path.join(where, "eval.log"))
        other = os.path.join(self.job, "u2", "workspace")
        self.result(os.path.join(other, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "005"))
        shutil.copytree(os.path.dirname(os.path.dirname(self.relayed)), os.path.join(self.job, "u2", ".work"))
        for eid, dir in (("005", other), ("003", self.tmp), ("005", other)):
            with self.subTest(eid=eid):
                self.assertEqual(self.driver.score(f"tasks__{eid}", dir, setup="original", judge=self.judge)["grader_refused"],
                                 ["agent file"])

    def test_a_failed_grader_is_the_null_controls_error_not_a_0(self):
        marks = list(self.driver.GRADER_FAILED)

        def run(argv, cwd, env, log, *a, **kw):
            self.result(argv[-1])
            self.log(log, "[2026-11-06 01:02:03 INFO desktopenv.env] evaluating",
                     *([f"[2026-11-06 01:02:04 ERROR desktopenv.env] {marks.pop(0)}Error code: 429"] if marks else []))

        with mock.patch.object(self.driver, "_run", run):
            for mark in list(marks):
                with self.subTest(mark), self.assertRaises(RuntimeError) as raised:
                    self.nop("003", judge=self.judge)
                self.assertIn(f"{mark[len('Error in '):-2]} (HTTP 429)", str(raised.exception))
                os.remove(os.path.join(self.tmp, "nop.log"))
            self.assertEqual(self.nop("003", judge=self.judge)["reward"], 0.0)

    def test_the_lists(self):
        lists = self.driver.LISTS
        self.assertEqual(lists["offline36"], OFFLINE36)
        self.assertEqual(lists["no_services63"], sorted(OFFLINE36 + MORE27))
        self.assertEqual(len(set(lists["no_services63"])), 63)
        self.assertFalse(set(lists["no_services63"]) & set(PROXY))
        self.assertEqual(len(self.driver.JUDGED), 21)
        self.assertEqual(len(self.driver.ASKS_USER), 6)
        self.assertFalse((self.driver.JUDGED | self.driver.ASKS_USER) & set(OFFLINE36))
        with mock.patch.dict(os.environ, {"NULLIUS_DATASET": "", "NULLIUS_DATASET_ID": "no_services63"}):
            self.assertEqual(self.driver.units()["units"], [f"tasks__{e}" for e in sorted(OFFLINE36 + MORE27)])
        counted = {d["id"]: d["units"] for d in bench()["datasets"]}
        self.assertEqual((counted["offline36"], counted["no_services63"]), (36, 63))


class Definition(unittest.TestCase):
    def test_the_judge_and_the_simulated_user_are_sonnet_as_openrouter_names_it(self):
        b = bench()
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        self.assertEqual(providers.served_as(b["score"]["judge"]["model"], "openrouter"), SONNET)
        with open(os.path.join(HERE, "agents.json"), encoding="utf-8") as f:
            asked = json.load(f)["agents"]["m3"]["start"]["driver"]["env"]["OSWORLD_USER_SIM_MODEL"]
        other, why = definitions.setup(b, "original", "MiniMax-M3", "openrouter")
        self.assertEqual(why, "")
        self.assertEqual(other["sent"], "minimax/minimax-m3")
        self.assertEqual(other["model"]["side_models"], [f"{asked}={SONNET}"])


class Provider(BaseHTTPRequestHandler):
    seen = []
    status = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.seen.append((self.path, body["model"]))
        code = int(self.headers.get("X-Status") or 200)
        out = (json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                           "usage": {"input_tokens": 3, "output_tokens": 1}}) if code == 200
               else json.dumps({"error": {"message": f"status {code}"}})).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Routes(unittest.TestCase):
    """The unit's relay as an Original run of OSWorld2 on OpenRouter starts it, before a fake provider."""

    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def setUp(self):
        b = bench()
        other, _ = definitions.setup(b, "original", "MiniMax-M3", "openrouter")
        self.judge_model = providers.served_as(b["score"]["judge"]["model"], "openrouter")
        self.trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.trial, ignore_errors=True)
        self.relay = relay.Relay(f"http://127.0.0.1:{self.up.server_address[1]}/api/v1", "sk-real",
                                 os.path.join(self.trial, ".work", "relay"),
                                 model=other["sent"], side_models=other["model"]["side_models"],
                                 key_header=run.key_header("openrouter", other, {})).start()
        self.addCleanup(self.relay.stop)
        self.judge_url = self.relay.url("u1-judge", model=self.judge_model)

    def post(self, url, model, status=200, words=""):
        messages = [{"role": "user", "content": words}] if words else []
        req = urllib.request.Request(url + "/messages", data=json.dumps({"model": model, "max_tokens": 8,
                                                                          "messages": messages}).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": relay.PLACEHOLDER_KEY,
                                              "X-Status": str(status),
                                              "Authorization": "Bearer " + relay.PLACEHOLDER_KEY})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_sonnet_is_never_sent_as_m3_and_the_graders_calls_are_not_the_agents(self):
        unit = self.relay.url("u1")
        self.post(unit, "MiniMax-M3")
        self.post(unit, "claude-sonnet-4-6")
        self.post(self.judge_url, "claude-sonnet-4-6")
        self.assertEqual([m for _, m in Provider.seen[-3:]], ["minimax/minimax-m3", SONNET, SONNET])
        self.assertTrue(all(p == "/api/v1/messages" for p, _ in Provider.seen[-3:]))
        agent, user = self.relay.calls("u1")
        self.assertEqual((agent["model"], agent.get("side")), ("minimax/minimax-m3", None))
        self.assertEqual((user["model"], user["model_asked"], user.get("side")), (SONNET, "claude-sonnet-4-6", True))
        self.assertEqual([c["model"] for c in self.relay.calls("u1-judge")], [SONNET])

    def test_the_providers_refusals_are_the_judge_routes_failure(self):
        for status, kind in ((402, "ApiUsageLimitError"), (429, "RateLimitError"), (500, "ApiError"), (503, "ApiError")):
            with self.subTest(status):
                self.assertEqual(self.post(self.judge_url, SONNET, status), status)
                self.assertEqual(relay.failure(self.relay.calls("u1-judge"))[0], kind)
        self.assertEqual(self.post(self.judge_url, SONNET), 200)
        self.assertIsNone(relay.failure(self.relay.calls("u1-judge")))
        self.assertEqual(self.relay.calls("u1"), [])

    def test_the_driver_reads_the_relays_own_record_of_the_judge_route(self):
        """The driver reads the relay's own record of the judge route, as run.Run writes it."""
        other, _ = definitions.setup(bench(), "original", "MiniMax-M3", "openrouter")
        unit = types.SimpleNamespace(base=f"http://127.0.0.1:{self.up.server_address[1]}/api/v1", key="sk-real", call_seconds=relay.TIMEOUT, max_calls=None,
                                     other=other, provider="openrouter", backend="local", model=other["sent"],
                                     judge_model=self.judge_model, key_header=run.key_header("openrouter", other, {}),
                                     serving={self.judge_model: ["anthropic"]}, ledger_file=lambda: None)
        trial = os.path.join(self.trial, "unit")
        rl = run.Run.unit_relay(unit, "u1", trial, {})
        self.addCleanup(rl.stop)
        judge = run.Run.judge(unit, rl, "u1")["judge"]
        driver = load_driver(self.trial)
        ws = os.path.join(trial, "workspace")
        driver._grader_refused(ws, judge)
        self.post(judge["base_url"], SONNET, 429, "first")
        self.post(judge["base_url"], SONNET, 200, "first")
        self.post(judge["base_url"], SONNET, 200, "second")
        driver._grader_refused(ws, judge)
        self.assertEqual([c["status"] for c in rl.calls("u1-judge")], [429, 200, 200])
        self.assertEqual({c["request"]["routing"]["order"][0] for c in rl.calls("u1-judge")}, {"anthropic"})
        self.post(judge["base_url"], SONNET, 402, "third")
        self.post(judge["base_url"], SONNET, 200, "fourth")
        with self.assertRaises(RuntimeError) as raised:
            driver._grader_refused(ws, judge)
        self.assertIn("answered 402, and the task was scored without it", str(raised.exception))
        self.assertNotIn("status 402", str(raised.exception))

    def test_a_retry_is_the_whole_request_again_not_only_its_words(self):
        shot = lambda data: [{"type": "text", "text": "the same words"},
                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
        self.post(self.judge_url, SONNET, 429, shot("QQ=="))
        self.post(self.judge_url, SONNET, 200, shot("Qg=="))
        first, second = self.relay.calls("u1-judge")
        self.assertNotEqual(first["request"], second["request"])
        with self.assertRaises(RuntimeError):
            load_driver(self.trial)._grader_refused(os.path.join(self.trial, "workspace"), {"base_url": self.judge_url})


def _task_classes():
    up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
    return sorted(glob.glob(os.path.join(up, "evaluation_examples", "task_class", "task_*.py"))) if up else []


def _metrics():
    up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
    path = os.path.join(up, "desktop_env", "evaluators", "metrics", "llm_metrics.py")
    return path if up and os.path.isfile(path) else ""


@unittest.skipUnless(_metrics(), "NULLIUS_OSWORLD2_UPSTREAM names no upstream")
class Metrics(unittest.TestCase):
    def test_each_mark_is_what_the_upstreams_llm_metrics_log(self):
        with open(_metrics(), encoding="utf-8") as f:
            source = f.read()
        driver = load_driver(os.environ["NULLIUS_OSWORLD2_UPSTREAM"])
        for mark in driver.GRADER_FAILED:
            self.assertEqual(source.count(f'logger.error("{mark}%s", exc)'), 1, mark)
        self.assertEqual(source.count('logger.error("Error in '), len(driver.GRADER_FAILED))


@unittest.skipUnless(_task_classes(), "NULLIUS_OSWORLD2_UPSTREAM names no upstream with the gated task classes")
class TaskClasses(unittest.TestCase):
    """What each task class asks for, read from its syntax tree."""

    def test_the_drivers_lists_are_the_task_classes(self):
        graded, users, hosted, proxied = set(), set(), set(), set()
        for p in _task_classes():
            n = os.path.basename(p)[5:-3]
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    if name in ("generate_text", "generate_chat") or name.endswith("_with_llm"):
                        graded.add(n)
                modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                           else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                if any("llm_metrics" in m or "model_client" in m for m in modules):
                    graded.add(n)
                if any(m.endswith(("controllers.website", "controllers.gitlab")) for m in modules) or \
                        (isinstance(node, ast.ImportFrom) and (node.module or "").endswith("controllers")
                         and {a.name for a in node.names} & {"website", "gitlab"}):
                    hosted.add(n)
                if isinstance(node, ast.ClassDef):
                    for st in node.body:
                        targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                        names = {t.id for t in targets if isinstance(t, ast.Name)}
                        value = getattr(st, "value", None)
                        if "proxy" in names and isinstance(value, ast.Constant) and value.value is True:
                            proxied.add(n)
                        if "user_simulator" in names and isinstance(value, ast.Dict):
                            if any(isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)
                                   and v.value == "llm" for k, v in zip(value.keys, value.values)):
                                users.add(n)
        driver = load_driver(os.environ["NULLIUS_OSWORLD2_UPSTREAM"])
        self.assertEqual(sorted(graded), sorted(driver.JUDGED))
        self.assertEqual(sorted(users), sorted(driver.ASKS_USER))
        self.assertEqual(sorted(proxied), PROXY)
        self.assertFalse(set(driver.LISTS["no_services63"]) & (hosted | proxied))
        self.assertEqual(len(_task_classes()) - len(hosted | proxied), 63)

    def test_the_graded_tasks_that_catch_a_grader_error_themselves(self):
        catching = set()
        for p in _task_classes():
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try) or all(any(isinstance(x, ast.Raise) for x in ast.walk(h))
                                                        for h in node.handlers):
                    continue
                if any(isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute))
                       and (c.func.id if isinstance(c.func, ast.Name) else c.func.attr) in ("generate_text", "generate_chat")
                       for part in node.body for c in ast.walk(part)):
                    catching.add(os.path.basename(p)[5:-3])
        self.assertEqual(sorted(catching), CATCHING)


if __name__ == "__main__":
    unittest.main()
