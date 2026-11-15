"""OSWorld2's failures by whose they are: an evaluation that could not read the VM or a site is the score's
error, never a 0; the environment's is a setup error; the upstream's words never reach the message."""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.test_osworld2_judge import load_driver

STEP = "[2026-11-10 22:09:08,778 INFO lib_run_single/1579-EnvProcess-1] Done: False"
RESULT = "[2026-11-10 22:09:38,788 INFO lib_run_single/1622-EnvProcess-1] Result: 0.00"
SITE = ("[2026-11-10 22:09:38,787 ERROR state/82-EnvProcess-1] [GET_STATE_WITH_COOKIE] Failed to fetch state from "
        "http://site.example.nip.io/api/state: HTTPConnectionPool(host='site.example.nip.io', port=80): ")
FILE = "[2026-11-10 22:09:20,000 {} desktopenv.pycontroller] {}"


def tried(status=None):
    return [FILE.format("ERROR", f"Failed to get file. Status code: {status}" if status else
                        "An error occurred while trying to get the file: ('Connection broken: IncompleteRead(0 bytes read)')"),
            FILE.format("INFO", "Retrying to get file.")]


class Evaluation(unittest.TestCase):
    def setUp(self):
        trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, trial, ignore_errors=True)
        self.tmp = os.path.join(trial, "workspace")
        os.makedirs(self.tmp)
        self.driver = load_driver(self.tmp)
        self.where = os.path.join(self.tmp, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "081")
        os.makedirs(self.where)
        with open(os.path.join(self.where, "result.txt"), "w") as f:
            f.write("0.0\n")

    def score(self, *lines):
        with open(os.path.join(self.where, "eval.log"), "w") as f:
            f.write("".join(ln + "\n" for ln in lines))
        return self.driver.score("tasks__081", self.tmp, setup="original")

    def unread(self, *lines):
        with self.assertRaises(self.driver.VerifierError) as raised:
            self.score(*lines)
        return str(raised.exception)

    def test_every_check_of_the_evaluation_is_the_scores_error_by_its_class(self):
        self.assertIn("the LLM grader failed in compare_text_with_llm", str(self.unread(
            STEP, "[2026-11-10 22:09:30,000 ERROR llm_metrics/388-Worker] Error in compare_text_with_llm: x", RESULT)))
        with open(os.path.join(self.where, "result.txt"), "w") as f:
            f.write("2.0\n")
        self.assertIn("not a score from 0 to 1", self.unread(STEP, RESULT))
        with open(os.path.join(self.where, "result.txt"), "w") as f:
            f.write("")
        self.assertEqual("result.txt could not be read as a number", self.unread(STEP, RESULT))
        os.remove(os.path.join(self.where, "result.txt"))
        self.assertIn("no result.txt", self.unread(STEP, RESULT))

    def test_setup_errors_the_last_setup_went_on_from_are_counted_beside_the_score(self):
        at = "[2026-11-10 22:00:0{} {} {}] {}"
        reset = at.format(1, "INFO", "desktop_env/330-EnvProcess-1", "Environment has been used, reverting to snapshot x...")
        clean = at.format(1, "INFO", "desktopenv.env", "Environment is clean, skipping snapshot revert (provider: docker).")
        failed = at.format(2, "ERROR", "setup/467-EnvProcess-1", "Failed to launch application. Status code: x")
        logged = at.format(2, "ERROR", "desktopenv.setup", "An error occurred while trying to send the request: x")
        retried = [at.format(2, "ERROR", "setup/686-EnvProcess-1", "Attempt 1: Failed to connect, retrying. Error: x"),
                   at.format(2, "ERROR", "setup/311-EnvProcess-1", "Upload attempt 1 failed for x: y"),
                   at.format(2, "ERROR", "desktopenv.setup", "Failed to download x caused by y. Retrying... (2 attempts left)")]
        self.assertNotIn("setup_errors", self.score(reset, *retried, STEP, RESULT))
        self.assertNotIn("setup_errors", self.score(failed, failed, reset, STEP, RESULT))
        self.assertEqual(self.score(reset, failed, *retried, logged, STEP, RESULT)["setup_errors"], 2)
        self.assertEqual(self.score(failed, clean, "\x1b[31m" + logged + "\x1b[0m", STEP, RESULT)["setup_errors"], 1)
        muse = "[2026-11-10 22:09:08,778 INFO desktopenv.experiment/1579-EnvProcess-1] Step 3: x"
        self.assertEqual(self.score(reset, failed, STEP, logged, STEP, failed, logged, RESULT)["setup_errors"], 2)
        self.assertNotIn("setup_errors", self.score(reset, muse, failed, RESULT))
        self.assertNotIn("setup_errors", self.score(reset, at.format(2, "ERROR", "setup/306-EnvProcess-1",
                                                                     "Failed to upload file x. Status code: 500"), STEP))
        self.assertEqual(self.score(reset, at.format(2, "ERROR", "setup/1031-EnvProcess-1",
                                                     "Failed to upload file. Status code: 500"), STEP)["setup_errors"], 1)
        done = at.format(3, "INFO", "desktopenv.env", "Environment setup complete.")
        self.assertEqual(self.score(reset, failed, done, logged, RESULT)["setup_errors"], 1)
        self.assertEqual(self.score(reset, failed, done, logged, STEP, RESULT)["setup_errors"], 2)

    def test_a_sites_state_that_timed_out_is_the_scores_error_with_the_upstreams_value(self):
        said = self.unread(STEP, SITE + "Read timed out. (read timeout=30)", RESULT)
        self.assertEqual(said, "the evaluation could not read a site's state (timed out), and the upstream scored the "
                               "task 0.0 without it; see the unit's eval.log")
        self.assertIn("(no answer)", self.unread(STEP, SITE + "Max retries exceeded", RESULT))
        self.assertIn("a site's file (HTTP 502)", self.unread(
            STEP, SITE.replace("fetch state", "download file") + "502 Server Error: Bad Gateway", RESULT))

    def test_a_site_that_answered_leaves_the_upstreams_score(self):
        for said in ("404 Client Error: NOT FOUND for url", "500 Server Error: INTERNAL SERVER ERROR for url"):
            self.assertEqual(self.score(STEP, SITE + said, RESULT)["reward"], 0.0)

    def test_a_sites_read_that_failed_stands_though_a_later_read_succeeds(self):
        read = "[2026-11-10 22:09:39,000 INFO state/96-EnvProcess-1] [GET_STATE_WITH_COOKIE] Returning state JSON directly"
        self.assertIn("a site's state (timed out)", self.unread(STEP, SITE + "Read timed out.", read, RESULT))

    def test_a_vm_read_that_gave_up_on_a_broken_connection_is_the_scores_error(self):
        give_up = FILE.format("ERROR", "Failed to get file.")
        self.assertIn("the VM's file (a broken connection)", self.unread(STEP, *tried(404) * 2, *tried(), give_up, RESULT))
        self.assertEqual(self.score(STEP, *tried() * 2, *tried(500), give_up, RESULT)["reward"], 0.0)

    def test_an_absent_file_or_a_recovered_read_is_no_failure(self):
        give_up = FILE.format("ERROR", "Failed to get file.")
        self.assertEqual(self.score(STEP, *tried(404) * 3, give_up, RESULT)["reward"], 0.0)
        self.assertEqual(self.score(STEP, *tried(), FILE.format("INFO", "File downloaded successfully"),
                                    "[2026-11-10 22:09:30,000 ERROR desktopenv.task081] Failed to get file.",
                                    RESULT)["reward"], 0.0)

    def test_other_loggers_words_neither_fail_it_nor_hide_a_failure(self):
        task = "[2026-11-10 22:09:21,000 {} desktopenv.task081] {}"
        self.assertEqual(self.score(STEP, task.format("ERROR", SITE[SITE.index("] ") + 2:] + "Read timed out."),
                                    task.format("ERROR", "An error occurred while trying to get the words: x"),
                                    task.format("ERROR", "Failed to get the words."),
                                    task.format("INFO", "Result: 1.00"), RESULT)["reward"], 0.0)
        said = self.unread(STEP, *tried(), task.format("INFO", "still waiting"), "  a continued line",
                           FILE.format("ERROR", "Failed to get file."), RESULT)
        self.assertIn("the VM's file (a broken connection)", said)
        self.assertNotIn("words", said)

    def test_only_the_evaluation_after_the_last_step_counts(self):
        self.assertEqual(self.score(SITE + "Read timed out.", STEP, RESULT)["reward"], 0.0)
        self.assertEqual(self.score(STEP, RESULT, SITE + "Read timed out.")["reward"], 0.0)
        self.assertEqual(self.score(SITE + "Read timed out.", RESULT)["reward"], 0.0)

    def test_every_phase_of_the_last_multi_phase_episode_counts(self):
        mark = "[2026-11-10 22:09:{:02},000 INFO lib_run_single/{}-EnvProcess-1] {}"
        step, start = (lambda n: mark.format(n, 776, f"Phase {n} Step 1: x")), mark.format(30, 712, "Starting phase 2 in trajectory 2")
        end = mark.format(40, 845, "Multi-phase result: 0.5000")
        fail, lost = SITE + "Read timed out.", [*tried(), FILE.format("ERROR", "Failed to get file.")]
        self.assertIn("a site's state (timed out)", self.unread(step(1), fail, start, step(2), end))
        self.assertIn("a site's state (timed out)", self.unread(step(1), start, step(2), fail, end))
        self.assertIn("the VM's file (a broken connection)", self.unread(step(1), *lost, start, step(2), end))
        self.assertEqual(self.score(step(1), start, fail, step(2), end)["reward"], 0.0)
        self.assertEqual(self.score(step(1), start, fail, end)["reward"], 0.0)
        self.assertEqual(self.score(step(1), fail, end, step(1), end)["reward"], 0.0)
        self.assertEqual(self.score(step(1), fail, start, step(1), start, step(2), end)["reward"], 0.0)
        asked = mark.format(2, 750, "Phase 1 User simulator Q: x | A: y")
        self.assertEqual(self.score(step(1), fail, start, asked, start, step(2), end)["reward"], 0.0)
        self.assertIn("a site's state (timed out)", self.unread(asked, fail, end))

    def test_a_task_that_retries_its_site_reads_keeps_its_score_but_not_a_lost_vm_read(self):
        mark = "[2026-11-10 22:09:{:02},000 INFO lib_run_single/{}-EnvProcess-1] {}"
        step, end = mark.format(1, 776, "Phase 1 Step 1: x"), mark.format(40, 845, "Multi-phase result: 0.5000")
        self.where = self.where.replace("081", "069")
        os.makedirs(self.where)
        with open(os.path.join(self.where, "result.txt"), "w") as f:
            f.write("0.5\n")
        with open(os.path.join(self.where, "eval.log"), "w") as f:
            f.write("".join(ln + "\n" for ln in (step, SITE + "Read timed out.", end)))
        self.assertEqual(self.driver.score("tasks__069", self.tmp, setup="original")["reward"], 0.5)
        with open(os.path.join(self.where, "eval.log"), "w") as f:
            f.write("".join(ln + "\n" for ln in (step, *tried(), FILE.format("ERROR", "Failed to get file."), end)))
        with self.assertRaises(RuntimeError):
            self.driver.score("tasks__069", self.tmp, setup="original")

    def test_the_null_controls_unread_evaluation_is_its_error(self):
        def run(argv, cwd, env, log, *a, **kw):
            with open(os.path.join(argv[-1], "result.txt"), "w") as f:
                f.write("0.0\n")
            with open(log, "w") as f:
                f.write("\n".join(["[2026-11-10 22:09:08,778 INFO desktopenv.experiment] Done: True",
                                   SITE.replace("state/82-EnvProcess-1", "desktopenv.getters.state") + "Read timed out.",
                                   "[2026-11-10 22:09:38,788 INFO desktopenv.experiment] Result: 0.00"]) + "\n")

        patches = {"_vm": lambda *a: "vm", "_run": run, "_task": lambda *a: {"instruction": "", "proxy": False},
                   "_assets": lambda *a: "assets"}
        with mock.patch.multiple(self.driver, **patches), self.assertRaises(RuntimeError) as raised:
            self.driver.score("tasks__001", self.tmp)
        self.assertIn("a site's state (timed out)", str(raised.exception))
        self.assertIn("see the unit's nop.log", str(raised.exception))

        def set_up(argv, cwd, env, log, *a, **kw):
            with open(os.path.join(argv[-1], "result.txt"), "w") as f:
                f.write("0.0\n")
            with open(log, "w") as f:
                f.write("[2026-11-10 22:00:01,000 INFO desktopenv.env] Environment is clean, skipping snapshot revert.\n"
                        "[2026-11-10 22:00:02,000 ERROR desktopenv.setup] Failed to launch application. Status code: x\n")

        with mock.patch.multiple(self.driver, **dict(patches, _run=set_up)):
            self.assertEqual(self.driver.score("tasks__001", self.tmp)["setup_errors"], 1)


TRACE = ["[2026-11-10 20:00:00,000 INFO lib_run_single/1500-EnvProcess-1] [Instruction]: the task's words",
         "[2026-11-10 20:00:01,000 ERROR run_multienv_m3/668-EnvProcess-1] Exception in EnvProcess-1 tasks/001: words",
         "[2026-11-10 20:00:01,000 ERROR run_multienv_m3/669-EnvProcess-1] Traceback (most recent call last):",
         '  File "/up/lib_run_single.py", line 1518, in run_single_example',
         "    env.reset(task_config=example)"]


class Failures(unittest.TestCase):
    def setUp(self):
        trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, trial, ignore_errors=True)
        self.tmp = os.path.join(trial, "workspace")
        os.makedirs(self.tmp)
        self.driver = load_driver(self.tmp)
        self.logged = []

        def run(argv, cwd, env, log, *a, **kw):
            with open(log, "w") as f:
                f.write("".join(ln + "\n" for ln in self.logged))
            where = os.path.join(cwd, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001")
            os.makedirs(where, exist_ok=True)
            with open(os.path.join(where, "traj.jsonl"), "w") as f:
                f.write('{"Error": "tasks/001 - the task\'s words"}\n')

        self.real_run = self.driver._run
        p = mock.patch.multiple(self.driver, _vm=lambda *a: "vm", _run=run,
                                _task=lambda *a: {"instruction": "", "proxy": False}, _assets=lambda *a: "assets")
        p.start()
        self.addCleanup(p.stop)

    def answer(self, *lines):
        self.logged = list(lines)
        with self.assertRaises(RuntimeError) as raised:
            self.driver.answer("tasks__001", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
        self.assertNotIn("words", str(raised.exception))
        return raised.exception

    def test_what_fails_before_the_upstream_starts_is_the_environments(self):
        for e in (self.driver.NoAnswer("docker create got no answer in 60 s"), OSError("no space"), KeyError("proxy")):
            with self.subTest(type(e).__name__), mock.patch.object(self.driver, "_vm", side_effect=e), \
                    self.assertRaises(self.driver.EnvironmentSetupError) as raised:
                self.driver.answer("tasks__001", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
            self.assertTrue(str(raised.exception).startswith(type(e).__name__ + ": "))

    def test_a_shared_lock_held_past_the_units_turn_limit_is_the_environments(self):
        lock = os.path.join(self.tmp, "port-{}.lck")
        fd = os.open(lock.format(9), os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        spare = self.driver.EVALUATE_SECONDS + self.driver.CLOSE_SECONDS + 1
        closing = threading.Timer(10, self.driver.CLOSED.set)
        closing.start()
        self.addCleanup(closing.cancel)
        with mock.patch.dict(self.driver.GUEST_PORTS, {"001": (9,)}), mock.patch.object(self.driver, "PORT_LOCK", lock), \
                mock.patch.object(self.driver, "_turn_seconds", lambda: spare), \
                self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver.answer("tasks__001", self.tmp, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
        self.assertIn("the unit's time ran out while another unit's VM held port 9", str(raised.exception))
        self.assertFalse(self.driver.CLOSED.is_set())
        with mock.patch.dict(self.driver.GUEST_PORTS, {"001": (9,)}), mock.patch.object(self.driver, "PORT_LOCK", lock), \
                mock.patch.object(self.driver, "_score_seconds", lambda: self.driver.CLOSE_SECONDS + 1), \
                self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver.score("tasks__001", self.tmp)
        self.assertIn("the unit's time ran out while another unit's VM held port 9", str(raised.exception))
        self.assertFalse(self.driver.CLOSED.is_set())
        given = []
        with mock.patch.object(self.driver, "_run", lambda *a, **kw: given.append(kw)), self.assertRaises(RuntimeError):
            self.driver.score("tasks__001", self.tmp)
        self.assertAlmostEqual(given[0]["until"] - time.time(),
                               self.driver._score_seconds() - 2 * self.driver.CLOSE_SECONDS, delta=5)
        self.assertEqual(given[0]["grace"], self.driver.CLOSE_SECONDS)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(p.kill)
        t0 = time.time()
        self.driver._started(p, os.path.join(self.tmp, "none.log"), "the null control's run", until=t0 + 1)
        self.assertLess(time.time() - t0, 5)
        self.assertIsNone(p.poll())

    def test_a_traceback_the_runner_went_on_after_is_not_why_it_ended(self):
        crash = [*TRACE, '  File "/up/desktop_env/desktop_env.py", line 596, in evaluate', "desktop_env.desktop_env.EnvironmentSetupError: x"]
        phase, result = (STEP.replace(STEP[STEP.index("] ") + 2:], said) for said in ("Starting phase 2 of 2", "Result: 0.00"))
        for then in (STEP, phase, result):
            with self.subTest(then):
                env = self.answer(*crash, then)
                self.assertIs(type(env), RuntimeError)
                self.assertIn("wrote no result.txt: no exception named", str(env))

    def test_a_crash_on_the_vms_failed_last_screenshot_is_the_environments_and_no_other(self):
        shot = "[2026-11-10 20:00:0{},000 {} python/{}-EnvProcess-1] {}"
        got, lost = shot.format(2, "INFO", 467, "Got screenshot successfully"), shot.format(3, "ERROR", 480, "Failed to get screenshot.")
        step = "[2026-11-10 20:00:01,000 INFO lib_run_single/1530-EnvProcess-1] Step 7: the task's words"
        crash = [TRACE[2], '  File "/up/lib_run_single.py", line 1586, in run_single_example', "    _f.write(obs['screenshot'])",
                 "TypeError: a bytes-like object is required, not 'NoneType'"]
        env = self.answer(TRACE[0], step, got, lost, *crash)
        self.assertIs(type(env), self.driver.EnvironmentSetupError)
        self.assertIn("wrote no result.txt: the VM stopped answering in step 7: its last screenshot failed", str(env))
        muse = [ln.replace("python/480", "desktopenv.pycontroller/480").replace("lib_run_single/", "desktopenv.experiment/")
                for ln in (step, got, lost)]
        self.assertIn("in step 7: its last screenshot failed", str(self.answer(TRACE[0], *muse, *crash)))
        first = self.answer(TRACE[0], lost, *crash[:-1], "PIL.UnidentifiedImageError: cannot identify image file")
        self.assertIn("the VM stopped answering before the first step", str(first))
        judged = self.answer(TRACE[0], step, lost, *crash[:2], '  File "/up/desktop_env/desktop_env.py", line 596, in evaluate',
                             crash[-1])
        self.assertIs(type(judged), self.driver.VerifierError)
        reset = self.answer(TRACE[0], lost, *crash[:2], '  File "/up/desktop_env/desktop_env.py", line 300, in reset', "KeyError: 'x'")
        self.assertIs(type(reset), self.driver.EnvironmentSetupError)
        muse = self.answer(TRACE[0], step, lost, *crash[:2], '  File "/up/mm_agents/muse_spark_agent.py", line 605, in _screenshot_bytes',
                           "mm_agents.muse_spark_agent.StepError: words")
        self.assertIn("in step 7: its last screenshot failed", str(muse))
        agent = '  File "/up/mm_agents/m3/agent.py", line 9, in predict'
        for name, lines in (("another exception", [step, lost, *crash[:-1], "KeyError: 'screenshot'"]),
                            ("a TypeError of another kind", [step, lost, *crash[:-1], "TypeError: unsupported operand"]),
                            ("the agent's own None", [step, lost, *crash[:2], agent, crash[-1]])):
            with self.subTest(name):
                unknown = self.answer(TRACE[0], *lines)
                self.assertIs(type(unknown), self.driver.CauseUnknownError)
                self.assertIn("after the VM's last screenshot failed in step 7", str(unknown))
        for name, lines in (("a later screenshot came", [step, lost, got, *crash]), ("no screenshot failed", [step, got, *crash])):
            with self.subTest(name):
                self.assertIs(type(self.answer(TRACE[0], *lines)), RuntimeError)

    def test_a_runner_ended_by_a_signal_or_with_a_lost_worker_is_said_by_what_its_log_can_show(self):
        log = os.path.join(self.tmp, "run.log")
        signalled = [sys.executable, "-c", "import os, sys, signal; signal.signal(signal.SIGHUP, signal.SIG_DFL); print('Step 3: words\\nTraceback (most recent call last):\\nKeyError: 1', flush=True); os.kill(os.getpid(), int(sys.argv[1]))"]
        aborted = [sys.executable, "-c", "import os; os.abort()"]
        crashed = [sys.executable, "-c", "raise RuntimeError('words')"]
        with mock.patch.object(self.driver, "_reaper", lambda *a, **kw: None):
            for n in (1, 9, 15):
                with contextlib.suppress(OSError):
                    os.remove(log)
                with self.subTest(n), self.assertRaises(self.driver.EnvironmentSetupError) as raised:
                    self.real_run(signalled + [str(n)], self.tmp, dict(os.environ), log, "the runner")
                self.assertIn(f"the runner exited -{n}: ended by signal {n} (the whole log", str(raised.exception))
            for argv, kind, said in ((aborted, self.driver.CauseUnknownError, "exited -6: ended by signal 6, with no traceback"),
                                     (crashed, RuntimeError, "exited 1: RuntimeError")):
                os.remove(log)
                with self.subTest(said), self.assertRaises(RuntimeError) as raised:
                    self.real_run(argv, self.tmp, dict(os.environ), log, "the runner")
                self.assertIs(type(raised.exception), kind)
                self.assertIn(said, str(raised.exception))
        at = "[2026-11-10 20:00:0{},000 INFO run_multienv_m3/{}] {}"
        started, ended = at.format(0, "612-EnvProcess-1", "Process EnvProcess-1 started."), \
            at.format(5, "774-EnvProcess-1", "EnvProcess-1 cleaning up environment...")
        finished = at.format(1, "879-MainProcess", "All tasks finished.")
        lost = self.answer(started, finished, TRACE[0])
        self.assertIs(type(lost), self.driver.CauseUnknownError)
        self.assertIn("wrote no result.txt: its worker process ended without its cleanup (the whole log", str(lost))
        self.assertIs(type(self.answer(started, *TRACE, "    x", "RuntimeError: words")), self.driver.CauseUnknownError)
        self.assertIs(type(self.answer(started, finished, TRACE[0], ended)), RuntimeError)
        self.assertIs(type(self.answer(started, *TRACE, "    x", "RuntimeError: words", ended)), RuntimeError)
        self.assertIs(type(self.answer(finished, TRACE[0], ended)), RuntimeError)
        term = at.format(6, "799-MainProcess", "Received signal 15. Gracefully shutting down...")
        outside = self.answer(started, term, at.format(6, "799-EnvProcess-1", "Process 1 received signal 15. Shutting down..."))
        self.assertIs(type(outside), self.driver.EnvironmentSetupError)
        self.assertIn("wrote no result.txt: ended by signal 15 (the whole log", str(outside))
        with open(os.path.join(self.tmp, "ended.log"), "w") as f:
            f.write(f"{started}\n{term}\n")
        self.assertIn("ended by signal 15 (", self.driver._tail(os.path.join(self.tmp, "ended.log"), 1)[1])
        own = at.format(6, "1051-MainProcess", "Unexpected error in main process: words")
        self.assertIs(type(self.answer(started, own, *TRACE, "    x", "RuntimeError: words", term, ended)), RuntimeError)
        child = at.format(6, "799-EnvProcess-1", "Received signal 15. Gracefully shutting down...")
        self.assertIs(type(self.answer(started, child, ended)), RuntimeError)

    def test_a_null_control_setup_that_fails_lets_the_units_lock_go(self):
        with mock.patch.object(self.driver, "_task_file", side_effect=RuntimeError("no task")), self.assertRaises(RuntimeError):
            self.driver.setup("tasks__001", self.tmp)
        self.assertEqual(self.driver._OWN, [])

    def test_a_runner_with_no_result_is_said_by_its_last_tracebacks_known_class_only(self):
        env = self.answer(*TRACE, '  File "/up/desktop_env/desktop_env.py", line 407, in reset',
                          "desktop_env.desktop_env.EnvironmentSetupError: Custom task setup failed: the task's words")
        self.assertIs(type(env), self.driver.EnvironmentSetupError)
        self.assertIn("wrote no result.txt: EnvironmentSetupError (the whole log: ", str(env))
        started = self.answer(*TRACE[:4], '  File "/up/desktop_env/desktop_env.py", line 186, in _start_emulator',
                              '  File "/up/desktop_env/providers/docker/provider.py", line 9, in start_emulator',
                              "requests.exceptions.ReadTimeout: the task's words")
        self.assertIs(type(started), self.driver.EnvironmentSetupError)
        self.assertIn("wrote no result.txt: ReadTimeout (the whole log: ", str(started))
        judged = self.answer(*TRACE[:4], '  File "/up/desktop_env/desktop_env.py", line 596, in evaluate',
                             "RuntimeError: Hosted state is unavailable: the task's words")
        self.assertIs(type(judged), self.driver.VerifierError)
        self.assertIn("RuntimeError (the whole log: ", str(judged))
        own = self.answer(*TRACE[:4], '  File "/up/mm_agents/m3.py", line 9, in reset', "PrivateWordsError: words")
        self.assertIs(type(own), RuntimeError)
        self.assertIn("an exception of the upstream's own (the whole log: ", str(own))
        self.assertIn("no exception named (the whole log: ", str(self.answer(*TRACE[:2])))
        quoted = self.answer(*TRACE[:4], '  File "/up/desktop_env/desktop_env.py", line 596, in evaluate',
                             "RuntimeError: words, in reset")
        self.assertIs(type(quoted), self.driver.VerifierError)

    def test_a_runner_that_cannot_start_is_the_environments(self):
        with mock.patch.object(self.driver.subprocess, "Popen", side_effect=BlockingIOError(11, "no process")), \
                self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.real_run([sys.executable, "-c", ""], self.tmp, {}, os.path.join(self.tmp, "x.log"), "the runner")
        self.assertEqual(str(raised.exception), "the runner did not start: BlockingIOError: [Errno 11] no process")

    def test_the_null_controls_failure_after_its_setup_is_said_as_its_class(self):
        def run(argv, cwd, env, log, *a, **kw):
            with open(log, "w") as f:
                f.write("".join(ln + "\n" for ln in TRACE[2:] + [
                    '  File "/up/desktop_env/desktop_env.py", line 131, in __init__',
                    '  File "/up/desktop_env/desktop_env.py", line 186, in _start_emulator',
                    "docker.errors.APIError: the task's words"]))

        with mock.patch.object(self.driver, "_run", run), self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver.score("tasks__001", self.tmp)
        self.assertIn("wrote no result.txt: an exception of the upstream's own", str(raised.exception))
        self.assertNotIn("words", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
