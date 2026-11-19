"""OSWorld2's episode ends with DONE and is scored once the unit's relay holds its stop file: the agent's own call
refused for its context window, or a spend guard that asks. A fake agent and episode stand in for the upstream's,
a fake provider on loopback for the endpoint: nothing starts a VM or calls a model."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nullius.standard import relay

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
STARTUP = os.path.join(HERE, "startup")
EPISODE = """def run_single_example(agent, env, max_steps):
    taken, done, step = [], False, 0
    while not done and step < max_steps:
        response, actions = agent.predict("", {})
        if not actions:
            raise RuntimeError("the simulated user was asked nothing")
        for action in actions:
            taken.append(action)
            if action in ("DONE", "FAIL"):
                done = True
                break
        step += 1
    env.append(taken)
"""
RUNNER = """import json, os, sys
import lib_run_single
class Agent:
    def predict(self, instruction, obs):
        open(os.environ["OSWORLD2_STOP"], "a").close()
        return "", []
env = []
lib_run_single.run_single_example(Agent(), env, 5)
print(json.dumps(env))
"""


def load(name, path, **env):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", [path]), mock.patch.object(sys, "path", [os.path.dirname(path), *sys.path]), \
            mock.patch.dict(os.environ, env):
        spec.loader.exec_module(module)
    return module


class Agent:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    def predict(self, instruction, obs):
        self.calls += 1
        step = self.steps.pop(0)
        return step() if callable(step) else step


class Episode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stop = os.path.join(self.tmp, "stop")
        self.site = load("osworld2_startup_stop", os.path.join(STARTUP, "sitecustomize.py"))
        self.module = types.ModuleType("lib_run_single")
        exec(EPISODE, self.module.__dict__)
        with mock.patch.dict(os.environ, OSWORLD2_STOP=self.stop):
            self.site.stop_when_asked(self.module)

    def asked(self, *_):
        open(self.stop, "a").close()

    def ran(self, agent, steps=5):
        env = []
        self.module.run_single_example(agent, env, steps)
        return env[0]

    def test_with_no_stop_the_agents_steps_run_as_they_are(self):
        agent = Agent(*[("", ["click"])] * 3)
        self.assertEqual(self.ran(agent, 3), ["click"] * 3)
        self.assertEqual(agent.calls, 3)

    def test_an_overflow_during_the_call_ends_the_episode_with_done(self):
        agent = Agent(("", ["click"]), lambda: (self.asked(), ("", []))[1], ("", ["click"]))
        self.assertEqual(self.ran(agent), ["click", "DONE"])
        self.assertEqual(agent.calls, 2)
        self.assertTrue(os.path.exists(self.stop + ".taken"))

    def test_the_action_already_bought_is_taken_before_done(self):
        agent = Agent(lambda: (self.asked(), ("", ["type"]))[1])
        self.assertEqual(self.ran(agent), ["type", "DONE"])
        os.remove(self.stop)
        agent = Agent(lambda: (self.asked(), ("", ["FAIL"]))[1])
        self.assertEqual(self.ran(agent), ["FAIL"])
        os.remove(self.stop)
        agent = Agent(lambda: (self.asked(), ({"text": ""}, [{"action_type": "CLICK"}]))[1])
        self.assertEqual(self.ran(agent), [{"action_type": "CLICK"}, "DONE"])

    def test_a_call_that_raised_after_the_stop_ends_with_done(self):
        def raised():
            self.asked()
            raise RuntimeError("the endpoint failed too many times")
        self.assertEqual(self.ran(Agent(raised)), ["DONE"])
        self.assertTrue(os.path.exists(self.stop + ".taken"))

    def test_a_call_that_raised_with_no_stop_is_the_upstreams(self):
        def raised():
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.ran(Agent(raised))

    def test_only_a_stop_that_ended_a_step_is_recorded_with_its_reasons(self):
        taken = self.stop + ".taken"
        self.assertEqual(self.ran(Agent(("", ["DONE"]))), ["DONE"])
        with open(self.stop, "w") as f:
            f.write("batch cap: spent\n")
        self.assertFalse(os.path.exists(taken))
        self.assertEqual(self.ran(Agent(("", ["click"]))), ["DONE"])
        with open(self.stop, "a") as f:
            f.write("turn limit\n")
        self.assertEqual(self.ran(Agent(("", ["click"]))), ["DONE"])
        with open(taken) as f:
            self.assertEqual(f.read(), "batch cap: spent\n")

    def test_a_stop_before_the_step_asks_the_model_nothing(self):
        self.asked()
        agent = Agent(("", ["click"]))
        self.assertEqual(self.ran(agent), ["DONE"])
        self.assertEqual(agent.calls, 0)

    def test_wrapped_once_and_not_at_all_without_a_stop_path(self):
        wrapped = self.module.run_single_example
        with mock.patch.dict(os.environ, OSWORLD2_STOP=self.stop):
            self.site.stop_when_asked(self.module)
        self.assertIs(self.module.run_single_example, wrapped)
        agent = Agent(("", ["click"]), ("", ["click"]))
        self.ran(agent, 1)
        predict = agent.predict
        self.ran(agent, 1)
        self.assertIs(agent.predict, predict)
        bare = types.ModuleType("lib_run_single")
        exec(EPISODE, bare.__dict__)
        run = bare.run_single_example
        with mock.patch.dict(os.environ, {"OSWORLD2_STOP": ""}):
            self.site.stop_when_asked(bare)
        self.assertIs(bare.run_single_example, run)


class Imported(unittest.TestCase):
    def test_the_runners_import_is_wrapped_as_the_driver_starts_it(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, text in (("lib_run_single.py", EPISODE), ("runner.py", RUNNER)):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(text)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([STARTUP, tmp]), OSWORLD2_STOP=os.path.join(tmp, "stop"),
                   NULLIUS_OPTION_VM_PROVIDER="docker")
        out = subprocess.run([sys.executable, os.path.join(tmp, "runner.py")], env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [["DONE"]])


class Driver(unittest.TestCase):
    def setUp(self):
        trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, trial, ignore_errors=True)
        self.dir = os.path.join(trial, "workspace")
        self.stop = os.path.join(trial, ".work", "relay", "stop")
        os.makedirs(os.path.dirname(self.stop))
        os.makedirs(self.dir)
        self.driver = load("osworld2_driver_stop", os.path.join(HERE, "driver.py"), NULLIUS_UPSTREAM=self.dir)

    def test_the_runner_is_told_the_units_stop_file(self):
        envs = []

        def started(argv, cwd, env, *a, **kw):
            envs.append(env)
            raise KeyboardInterrupt

        with mock.patch.object(self.driver, "_vm", lambda *a: "vm"), mock.patch.object(self.driver, "_run", started), \
                mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": False}), \
                mock.patch.object(self.driver, "_assets", lambda *a: "assets"), self.assertRaises(KeyboardInterrupt):
            self.driver.answer("tasks__001", self.dir, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
        self.assertEqual(envs[0]["OSWORLD2_STOP"], self.stop)

    def test_a_stopped_episodes_score_says_why_and_keeps_its_result(self):
        where = os.path.join(self.dir, "results", "pyautogui", "screenshot", "MiniMax-M3", "tasks", "001")
        os.makedirs(where)
        with open(os.path.join(where, "result.txt"), "w") as f:
            f.write("0.5")
        scored = self.driver.score("tasks__001", self.dir, setup="original")
        self.assertEqual(scored["reward"], 0.5)
        self.assertNotIn("stopped", scored)
        with open(self.stop, "w") as f:
            f.write("turn limit\n")
        self.assertNotIn("stopped", self.driver.score("tasks__001", self.dir, setup="original"))
        with open(self.stop + ".taken", "w") as f:
            f.write("context window\ncontext window\nbatch cap: spent $1\n")
        relayed = os.path.dirname(self.stop)
        for name, calls in (("u0001", []), ("u0001", [{"status": 200, "side": True}]), ("u0001-judge", [{"status": 200}])):
            with open(os.path.join(relayed, name + ".jsonl"), "w") as f:
                f.write("".join(json.dumps(c) + "\n" for c in calls))
            with self.subTest(name=name, calls=calls), self.assertRaises(self.driver.CancelledError) as raised:
                self.driver.score("tasks__001", self.dir, setup="original")
            self.assertIn("stopped (context window) before the agent's first step", str(raised.exception))
        with open(os.path.join(relayed, "u0001.jsonl"), "a") as f:
            f.write(json.dumps({"status": 200}) + '\n{"stat\n')
        scored = self.driver.score("tasks__001", self.dir, setup="original")
        self.assertEqual((scored["reward"], scored["stopped"], scored["stopped_by"]),
                         (0.5, "context window", ["context window", "money"]))
        with open(self.stop + ".taken", "w") as f:
            f.write("forecast $2 >= $1\nturn limit\nsomething else\n")
        self.assertEqual(self.driver.score("tasks__001", self.dir, setup="original")["stopped_by"],
                         ["money", "time", "other"])
        open(self.stop + ".taken", "w").close()
        with open(self.stop, "w") as f:
            f.write("turn limit\n")
        self.assertEqual(self.driver.score("tasks__001", self.dir, setup="original")["stopped_by"], ["time"])
        with open(os.path.join(relayed, "u0001.jsonl"), "w") as f:
            f.write(json.dumps({"status": 200, "side": True, "said": "a\u2028b"}, ensure_ascii=False) + "\n")
        with open(self.stop + ".taken", "w") as f:
            f.write("turn limit\n")
        with self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver.score("tasks__001", self.dir, setup="original")
        self.assertIn("stopped (turn limit) before the agent's first step", str(raised.exception))
        self.assertEqual(len(self.driver._traj(relayed, "u0001.jsonl")), 1)

    def test_the_episode_is_stopped_at_its_own_limit_counted_from_the_runners_start(self):
        argv = [sys.executable, "-c", "import os, sys, time\nt = time.time()\n"
                "while not os.path.exists(sys.argv[1]) and time.time() - t < 20: time.sleep(0.05)", self.stop]
        log = os.path.join(self.dir, "run.log")
        with mock.patch.object(self.driver, "_reaper", lambda *a, **kw: None):
            for episode, until in ((1, time.time() + 600), (600, time.time() + 1), (1, None)):
                if until is None:
                    shutil.rmtree(os.path.dirname(self.stop))
                    until = time.time() + 600
                with self.subTest(episode=episode), mock.patch.object(self.driver, "EPISODE_SECONDS", episode):
                    self.driver._run(argv, self.dir, dict(os.environ), log, "the runner", until=until)
                    with open(self.stop) as f:
                        self.assertEqual(f.read(), "turn limit\n")
                    os.remove(self.stop)

    def test_the_answers_limit_leaves_the_turn_time_to_evaluate(self):
        kws = []

        def started(*a, **kw):
            kws.append(kw)
            raise KeyboardInterrupt

        with mock.patch.object(self.driver, "_vm", lambda *a: "vm"), mock.patch.object(self.driver, "_run", started), \
                mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": False}), \
                mock.patch.object(self.driver, "_assets", lambda *a: "assets"), self.assertRaises(KeyboardInterrupt):
            self.driver.answer("tasks__001", self.dir, "original", "MiniMax-M3", "http://127.0.0.1:9/u1/v1", "k")
        left = kws[0]["until"] - time.time()
        self.assertAlmostEqual(left, self.driver._turn_seconds() - self.driver.EVALUATE_SECONDS - self.driver.CLOSE_SECONDS,
                               delta=5)
        self.assertGreater(left, self.driver.EPISODE_SECONDS)

    def test_a_runner_alive_an_evaluations_time_after_its_turn_limit_is_reaped_unscored(self):
        argv = [sys.executable, "-c", "import time; time.sleep(15)"]
        log, reaped = os.path.join(self.dir, "run.log"), []

        def reaper(pgid, *a, **kw):
            reaped.append(pgid)
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)

        with mock.patch.object(self.driver, "_reaper", reaper):
            for calls, kind in (([{"status": 200, "side": True}], self.driver.EnvironmentSetupError),
                                ([{"status": 200}], self.driver.VerifierError)):
                with open(os.path.join(os.path.dirname(self.stop), "u0001.jsonl"), "w") as f:
                    f.write("".join(json.dumps(c) + "\n" for c in calls))
                t0 = time.time()
                with self.subTest(calls=calls), self.assertRaises(kind) as raised:
                    self.driver._run(argv, self.dir, dict(os.environ), log, "the runner", until=time.time() + 0.5, grace=1)
                self.assertLess(time.time() - t0, 10)
                self.assertIn("the runner did not end 1 s after the unit's turn limit", str(raised.exception))
                self.assertTrue(reaped and self.driver._gone(reaped[-1], 5))
            with open(os.path.join(os.path.dirname(self.stop), "u0001.jsonl"), "w") as f:
                f.write(json.dumps({"status": 200, "side": True}) + "\n")
            done = "[2026-11-10 20:00:00,000 INFO desktopenv.env/1-EnvProcess-1] Environment setup complete."
            for said, kind in (("", self.driver.EnvironmentSetupError), (done, self.driver.VerifierError)):
                with self.subTest(said=said), self.assertRaises(kind):
                    self.driver._run([sys.executable, "-c", f"import time; print({said!r}, flush=True); time.sleep(15)"], self.dir,
                                     dict(os.environ), log, "the null control's run", failed=os.path.join(self.dir, "failed"),
                                     until=time.time() + 0.5, grace=1)

    def test_a_reaper_that_does_not_start_is_a_setup_error_after_the_start_lock_is_let_go(self):
        argv = [sys.executable, "-c", "import time; time.sleep(15)"]
        lock, held, groups = os.path.join(self.dir, "start.lck"), [], []
        for failing in (None, RuntimeError("no thread")):
            def reap(pgid, *a, **kw):
                groups.append(pgid)
                with open(lock) as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held.append(False)
                    except BlockingIOError:
                        held.append(True)
                if failing:
                    raise failing
                os.killpg(pgid, signal.SIGKILL)

            with self.subTest(failing=failing), mock.patch.object(self.driver, "START_LOCK", lock), \
                    mock.patch.object(self.driver, "_remove_vms") as removed, \
                    mock.patch.object(self.driver, "_arm", mock.Mock(side_effect=ValueError("no pipe"))), \
                    mock.patch.object(self.driver, "reap", reap), self.assertRaises(self.driver.EnvironmentSetupError) as raised:
                self.driver._run(argv, self.dir, dict(os.environ), os.path.join(self.dir, "run.log"), "the runner", stagger=True)
            self.assertIn("the runner's reaper did not start: ValueError: no pipe", str(raised.exception))
            self.assertTrue(self.driver._gone(groups[-1], 5))
            self.assertEqual(removed.call_count, 1 if failing else 0)
        self.assertEqual(held, [False, False])

    def test_a_start_lock_that_is_no_plain_file_is_a_setup_error(self):
        lock = os.path.join(self.dir, "start.lck")
        os.mkdir(lock)
        with mock.patch.object(self.driver, "START_LOCK", lock), self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver._run(["true"], self.dir, dict(os.environ), os.path.join(self.dir, "run.log"), "the runner", stagger=True)
        self.assertIn("OSError: [Errno 22] not a plain file", str(raised.exception))

    def test_a_start_lock_held_past_the_units_turn_limit_is_a_setup_error(self):
        lock = os.path.join(self.dir, "start.lck")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        closing = threading.Timer(10, self.driver.CLOSED.set)
        closing.start()
        self.addCleanup(closing.cancel)
        with mock.patch.object(self.driver, "START_LOCK", lock), self.assertRaises(self.driver.EnvironmentSetupError) as raised:
            self.driver._run(["true"], self.dir, dict(os.environ), os.path.join(self.dir, "run.log"), "the runner",
                             stagger=True, until=time.time() + 1)
        self.assertIn("the runner was not started: the unit's time ran out", str(raised.exception))
        self.assertFalse(self.driver.CLOSED.is_set())


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        said = {"long": b'{"error":{"message":"This endpoint\'s maximum context length is 10 tokens"}}',
                "image": b'{"error":{"message":"invalid image"}}'}.get(body["messages"][0]["content"])
        self.send_response(400 if said else 200)
        said = said or json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {}}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(said)))
        self.end_headers()
        self.wfile.write(said)


class Relayed(unittest.TestCase):
    def setUp(self):
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()
        self.relay = relay.Relay(f"http://127.0.0.1:{self.up.server_address[1]}/v1", "sk", self.tmp, model="m",
                                 side_models=["sim"]).start()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.up.shutdown)
        self.addCleanup(self.relay.stop)
        self.stop = os.path.join(self.tmp, relay.STOP)

    def post(self, url, model, text):
        req = urllib.request.Request(url + "/chat/completions", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": model, "messages": [{"role": "user", "content": text}]}).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_only_the_agents_own_overflow_leaves_the_stop_file(self):
        self.assertEqual(self.post(self.relay.url("u1"), "m", "image"), 400)
        self.assertEqual(self.post(self.relay.url("u1"), "sim", "long"), 400)
        self.assertEqual(self.post(self.relay.url("u1-judge", model="j"), "j", "long"), 400)
        self.assertEqual(self.post(self.relay.url("u1"), "m", "fine"), 200)
        self.assertFalse(os.path.exists(self.stop))
        self.assertEqual(self.post(self.relay.url("u1"), "m", "long"), 400)
        with open(self.stop) as f:
            self.assertEqual(f.readline(), "context window\n")
        self.assertEqual(len(self.relay.calls("u1")), 4)
        self.assertEqual(self.post(self.relay.url("u1"), "m", "fine"), 200)

    def test_a_stop_asked_after_an_overflow_is_heeded(self):
        with open(self.stop, "w") as f:
            f.write("context window\nbatch cap\n")
        self.assertEqual(self.post(self.relay.url("u1"), "m", "fine"), 400)
        self.assertEqual(self.relay.calls("u1"), [])

    def test_once_asked_from_outside_the_agents_own_calls_are_not_sent(self):
        with open(self.stop, "w") as f:
            f.write("batch cap\n")
        self.assertEqual(self.post(self.relay.url("u1"), "m", "fine"), 400)
        self.assertEqual(self.relay.calls("u1"), [])
        self.assertEqual(self.post(self.relay.url("u1"), "sim", "fine"), 200)
        self.assertEqual(self.post(self.relay.url("u1-judge", model="j"), "j", "fine"), 200)
        self.assertEqual(len(self.relay.calls("u1")) + len(self.relay.calls("u1-judge")), 2)


if __name__ == "__main__":
    unittest.main()
