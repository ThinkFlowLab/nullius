"""OSWorld2's Muse Spark agent beside M3: its arguments, its relay route, its tool calls, its call timeout.
With NULLIUS_OSWORLD2_UPSTREAM set, every flag is checked against the runner's parser, read as a syntax tree."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
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

from nullius.standard.backends import container
from nullius.standard.calls import relay
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.protocols import jsonl
from nullius.standard.runner import job, run

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
MUSE, SENT, SONNET = "muse-spark-1.3", "meta/muse-spark-1.3", "anthropic/claude-sonnet-4.6"
UNIT = "http://127.0.0.1:9/u1/v1"


def load_driver(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_muse", os.path.join(HERE, "driver.py"))
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
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        self.vms, self.runs = [], []

        def vm(dir, domain, eid):
            self.vms.append(eid)
            return os.path.join(dir, "vm.qcow2")

        def started(argv, cwd, env, *a, **kw):
            self.runs.append((argv, env))
            raise Started()

        for name, fake in (("_vm", vm), ("_run", started), ("_task", lambda *_: {"instruction": "", "proxy": False})):
            p = mock.patch.object(self.driver, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def answer(self, agent, model, eid="045", **options):
        env = {"NULLIUS_OPTION_AGENT": agent, **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()}}
        with mock.patch.dict(os.environ, env):
            return self.driver.answer(f"tasks__{eid}", self.tmp, "original", model, UNIT, relay.PLACEHOLDER_KEY,
                                      judge={"model": SONNET, "base_url": "http://127.0.0.1:9/u1-judge/v1",
                                             "key": relay.PLACEHOLDER_KEY})

    def test_muse_spark_runs_its_script_with_its_published_runs_settings_on_the_relay(self):
        with self.assertRaises(Started):
            self.answer("muse_spark", MUSE, max_steps="500", thinking="adaptive")
        argv, env = self.runs[-1]
        self.assertEqual(argv[1], os.path.join(self.tmp, "scripts", "python", "run_multienv_muse_spark.py"))
        flags = {w: v for w, v in zip(argv[2:], argv[3:]) if w.startswith("--") and not v.startswith("--")}
        self.assertEqual({k: flags[k] for k in ("--model", "--base_url", "--reasoning_effort", "--max_tokens", "--max_steps",
                                                "--provider_name", "--num_envs")},
                         {"--model": MUSE, "--base_url": UNIT, "--reasoning_effort": "max", "--max_tokens": "262144",
                          "--max_steps": "500", "--provider_name": "docker", "--num_envs": "1"})
        self.assertEqual(argv[-1], "--save_model_eval_raw_info")
        self.assertNotIn("--api_key", argv)
        self.assertNotIn("--truncation", argv)
        self.assertEqual({k: env[k] for k in ("MUSE_SPARK_API_KEY", "MUSE_SPARK_SKIP_INPUT_TOKEN_PREFLIGHT")},
                         {"MUSE_SPARK_API_KEY": relay.PLACEHOLDER_KEY, "MUSE_SPARK_SKIP_INPUT_TOKEN_PREFLIGHT": "1"})
        self.assertFalse([k for k in env if k.startswith("M3_")])

    def test_the_simulated_user_and_the_grader_are_m3s(self):
        envs = []
        for agent, model in (("m3", "MiniMax-M3"), ("muse_spark", MUSE)):
            with self.subTest(agent), self.assertRaises(Started):
                self.answer(agent, model, eid="024")
            envs.append({k: v for k, v in self.runs[-1][1].items() if k.startswith(("OSWORLD_USER_SIM_", "OSWORLD_EVAL_MODEL_"))})
        self.assertEqual(envs[0], envs[1])
        self.assertEqual(envs[1]["OSWORLD_USER_SIM_BASE_URL"], "http://127.0.0.1:9/u1")
        self.assertEqual(envs[1]["OSWORLD_EVAL_MODEL_NAME"], SONNET)

    def test_m3_keeps_its_thinking_option(self):
        with self.assertRaises(Started):
            self.answer("m3", "MiniMax-M3", thinking="adaptive")
        self.assertEqual(self.runs[-1][1]["M3_THINKING_MODE"], "adaptive")

    def test_every_agent_takes_the_steps_option_and_its_own_by_default(self):
        own = bench()["options"]["max_steps"]["by"]["agent"]
        self.assertEqual(own, {"m3": "100", "muse_spark": "500"})
        for agent, model in (("m3", "MiniMax-M3"), ("muse_spark", MUSE)):
            for steps, sent in (("default", own[agent]), ("50", "50")):
                with self.subTest(agent, steps=steps), self.assertRaises(Started):
                    self.answer(agent, model, max_steps=steps)
                argv = self.runs[-1][0]
                self.assertEqual(argv[argv.index("--max_steps") + 1], sent)
        spec = self.driver.AGENTS["agents"]["m3"]
        with mock.patch.dict(spec, argv=[a for a in spec["argv"] if a not in ("--max_steps", "100")]), self.assertRaises(Started):
            self.answer("m3", "MiniMax-M3", max_steps="50")
        self.assertEqual(self.runs[-1][0][-2:], ["--max_steps", "50"])

    def test_a_model_of_the_other_agent_is_refused_before_the_vm_naming_its_agent(self):
        for agent, model, other in (("m3", MUSE, "muse_spark"), ("muse_spark", "MiniMax-M3", "m3")):
            with self.subTest(agent), self.assertRaises(RuntimeError) as raised:
                self.answer(agent, model)
            self.assertIn(f"{model} is not one of the {agent} agent's models", str(raised.exception))
            self.assertIn(f"choose the agent {other} for it", str(raised.exception))
        with self.assertRaises(RuntimeError) as raised:
            self.answer("muse_spark", "gpt-5")
        self.assertTrue(str(raised.exception).endswith("choose one of them"))
        self.assertEqual((self.vms, self.runs), ([], []))

    def test_the_option_offers_every_agent_and_thinking_only_with_m3(self):
        opts = bench()["options"]
        self.assertEqual(opts["agent"]["values"], list(self.driver.AGENTS["agents"]))
        self.assertEqual(opts["thinking"]["when"], {"agent": ["m3"]})
        models = [m for spec in self.driver.AGENTS["agents"].values() for m in spec["models"]]
        self.assertEqual(models, bench()["setups"]["original"]["models"])


class Definition(unittest.TestCase):
    def test_muse_spark_is_sent_as_meta_and_not_compared_with_the_paper(self):
        b = bench()
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        other, why = definitions.setup(b, "original", MUSE, "openrouter")
        self.assertEqual(why, "")
        self.assertEqual(other["sent"], SENT)
        self.assertNotIn("pins", other)
        self.assertEqual(other["model"]["side_models"], [f"claude-sonnet-4-6={SONNET}"])
        self.assertFalse(other["comparable"])
        self.assertTrue(definitions.setup(b, "original", "MiniMax-M3", "openrouter")[0]["comparable"])

    def test_comparable_names_some_of_the_setups_models(self):
        for value, ok in ((["MiniMax-M3"], True), (True, True), (False, True), ([MUSE], False), (["gpt-5"], False), ([], False),
                          (["MiniMax-M3", "MiniMax-M3"], False)):
            b = bench()
            b["setups"]["original"]["comparable"] = value
            with self.subTest(value):
                self.assertEqual(not [p for p in definitions.problems(b, "benchmark") if "comparable" in p], ok)

    def test_the_builder_offers_like_the_paper_with_m3_only_and_runs_muse_spark_as_any_model(self):
        page, = recipe.page_of("osworld2")["setups"]
        self.assertEqual((page["models"], page["sent"]["openrouter"]), (["MiniMax-M3"], {"MiniMax-M3": "minimax/minimax-m3"}))
        r = {"version": 1, "harness": recipe.HARNESS, "name": "x", "title": "x", "benchmark": "osworld2", "datasets": ["test_v2"],
             "others": ["original"], "provider": "openrouter", "limit": 1, "concurrency": 1, "unsandboxed": False,
             "where": "local", "options": {}, "bench_options": {"agent": "muse_spark"}, "model": MUSE}
        self.assertIn("Any of your choice", recipe.compile(dict(r, model_choice="paper"))["errors"][0]["message"])
        out = recipe.compile(dict(r, model_choice="any"))
        self.assertEqual(out["errors"], [])
        self.assertIn(f"run osworld2 original --model {MUSE} --provider openrouter", out["script"])
        self.assertIn("--bench-option agent=muse_spark", out["script"])
        picked = recipe.compile(dict(r, model_choice="any", model=SENT))
        self.assertEqual(picked["errors"], [])
        self.assertIn(f"run osworld2 original --model {MUSE} --provider openrouter", picked["script"])

    def test_the_relay_waits_longer_than_muse_spark(self):
        b = bench()
        self.assertEqual(b["limits"]["call_seconds"], 3300)
        with open(os.path.join(HERE, "agents.json"), encoding="utf-8") as f:
            self.assertNotIn("--request_timeout", json.load(f)["agents"]["muse-spark"]["start"]["driver"]["argv"])
        for bad in (0, -1, 1.5, True, "3300"):
            b["limits"]["call_seconds"] = bad
            with self.subTest(bad):
                self.assertIn("limits.call_seconds: a whole number from 1", definitions.problems(b, "benchmark"))


class OpenRouter(BaseHTTPRequestHandler):
    """A fake OpenRouter /responses; ``slow`` holds the reply."""
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        OpenRouter.seen.append({"path": self.path, "body": body})
        time.sleep(float(body.get("slow") or 0))
        out = {"id": "gen-42", "object": "response", "status": "completed", "model": body["model"],
               "output": [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "e"},
                          {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "computer.stop",
                           "arguments": json.dumps({"answer": "done"})}],
               "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 90}, "output_tokens": 7,
                         "output_tokens_details": {"reasoning_tokens": 5}, "cost": 0.0002}}
        if body.get("no_cost"):
            del out["usage"]["cost"]
        if self.headers.get("X-OpenRouter-Metadata") == "enabled":
            out["openrouter_metadata"] = {"requested": body["model"], "endpoints": {"available": [
                {"provider": "Meta", "model": body["model"], "selected": True}]}}
        data = json.dumps(out).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.PLACEHOLDER_KEY})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        finally:
            e.close()


class Responses(unittest.TestCase):
    """The unit's relay as an Original run of Muse Spark on OpenRouter with --served-by meta starts it, on the local backend."""

    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), OpenRouter)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def unit(self, seconds, prices=None):
        b = bench()
        other, _ = definitions.setup(b, "original", MUSE, "openrouter")
        trial = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, trial, ignore_errors=True)
        ns = types.SimpleNamespace(base=self.base, key="sk-real", other=other, provider="openrouter", backend="local", max_calls=None,
                                   key_header=run.key_header("openrouter", other, {}),
                                   model=other["sent"], judge_model=SONNET, call_seconds=seconds,
                                   serving={SENT: ["meta"]}, prices=prices, ledger_file=lambda: None)
        rl = run.Run.unit_relay(ns, "u1", trial, {})
        self.addCleanup(rl.stop)
        return rl

    def test_a_call_goes_to_responses_served_by_meta_and_its_cache_is_recorded(self):
        OpenRouter.seen.clear()
        rl = self.unit(3300)
        self.assertEqual(rl.pool.timeout, 3300)
        status, reply = post(rl.url("u1") + "/responses", {"model": MUSE, "input": [], "store": False,
                                                            "include": ["reasoning.encrypted_content"],
                                                            "reasoning": {"effort": "max", "summary": "auto"}})
        self.assertEqual((status, [i["type"] for i in reply["output"]]), (200, ["reasoning", "function_call"]))
        sent = OpenRouter.seen[-1]
        self.assertEqual(sent["path"], "/api/v1/responses")
        self.assertEqual((sent["body"]["model"], sent["body"]["provider"]), (SENT, {"order": ["meta"], "allow_fallbacks": False}))
        self.assertEqual(sent["body"]["reasoning"], {"effort": "max", "summary": "auto"})
        call, = rl.calls("u1")
        self.assertEqual(call["request"]["routing"], {"order": ["meta"], "allow_fallbacks": False})
        self.assertEqual({k: call["reply"]["usage"][k] for k in ("input", "cached", "output", "reasoning")},
                         {"input": 100, "cached": 90, "output": 7, "reasoning": 5})
        self.assertEqual(call["reply"]["tool_calls"][0]["name"], "computer.stop")

    def test_a_reply_with_no_cost_is_priced_from_the_price_list_the_run_has(self):
        rl = self.unit(30, {SENT: {"input": 1.25, "cached": 0.15, "output": 4.25}})
        post(rl.url("u1") + "/responses", {"model": MUSE, "input": [], "no_cost": True})
        call = rl.calls("u1")[-1]
        self.assertEqual(call["cost_from"], "estimate")
        self.assertAlmostEqual(call["cost_usd"], (10 * 1.25 + 90 * 0.15 + 7 * 4.25) / 1e6)

    def test_a_reply_slower_than_the_relays_wait_is_a_502_and_one_within_it_goes_through(self):
        status, reply = post(self.unit(0.3).url("u1") + "/responses", {"model": MUSE, "input": [], "slow": 1})
        self.assertEqual(status, 502)
        self.assertIn("could not reach the endpoint", reply["error"]["message"])
        self.assertEqual(post(self.unit(5).url("u1") + "/responses", {"model": MUSE, "input": [], "slow": 1})[0], 200)

    def test_a_relay_container_is_given_the_benchmarks_wait(self):
        argv = container.relay_argv("img", name="n", job="j", app="/app", record="/r", key_file="/k", base="b", model="m",
                                    routes={}, timeout=3300)
        self.assertEqual(argv[argv.index("--timeout") + 1], "3300")
        self.assertNotIn("--timeout", container.relay_argv("img", name="n", job="j", app="/app", record="/r", key_file="/k", base="b",
                                                           model="m", routes={}))

    def test_a_run_reads_the_wait_from_the_benchmarks_limits(self):
        b = definitions.get("benchmark", "osworld2")
        r = run.Run(b, definitions.control(b, "nop"), model="", provider="openai", env={"OPENAI_API_KEY": "k"},
                    which=lambda p: "/bin/" + p)
        self.assertEqual(r.call_seconds, 3300)
        del b["limits"]["call_seconds"]
        r = run.Run(b, definitions.control(b, "nop"), model="", provider="openai", env={"OPENAI_API_KEY": "k"},
                    which=lambda p: "/bin/" + p)
        self.assertEqual(r.call_seconds, relay.TIMEOUT)


class Upstream(unittest.TestCase):
    """What a unit of the Original keeps of its agent's calls: Muse Spark answers with tool calls and no text."""

    ACTION = {"t0": 1, "t1": 2, "status": 200, "model": SENT, "request": {},
              "reply": {"text": "", "tool_calls": [{"name": "computer.computer", "arguments": '{"actions": []}'}],
                        "usage": {"input": 9, "output": 3, "cached": 0, "reasoning": 2}}}

    def drive(self, calls, stop=None, said="answer: RuntimeError: wrote no result.txt"):
        record = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, record, ignore_errors=True)
        if stop is not None:
            with open(os.path.join(record, relay.STOP), "w") as f:
                f.write(stop)
        failing = types.SimpleNamespace(request=mock.Mock(side_effect=jsonl.Failed(said)))
        ns = types.SimpleNamespace(other={"key": "original", "label": "OSWorld2 original"}, asked=MUSE, model=SENT,
                                   name="j", bench={"label": "OSWorld2"}, agent_limit=lambda setup: 10, driver=lambda: failing,
                                   attempt_kw=lambda: {}, simulator=lambda rl, tag: {}, judge=lambda rl, tag: {})
        rl = types.SimpleNamespace(calls=lambda tag: calls, url=lambda tag: "http://127.0.0.1:9/u1/v1", dir=record)
        with self.assertRaises(run._Ended) as ended:
            run.Run.drive_upstream(ns, "tasks__045", "/ws", "u1", [], rl)
        return ended.exception

    def test_its_actions_are_the_trajectorys_tool_calls(self):
        steps = job.trajectory_from_exchanges([], [self.ACTION], {"key": "original"}, SENT, "s")["steps"]
        self.assertEqual(steps[-1]["tool_calls"], [{"tool_call_id": "call-2-1", "function_name": "computer.computer",
                                                    "arguments": {"actions": []}}])
        cut = dict(self.ACTION, reply=dict(self.ACTION["reply"], tool_calls=[{"name": "computer.computer", "arguments": '{"act'}]))
        self.assertEqual(job.trajectory_from_exchanges([], [cut], {"key": "original"}, SENT, "s")["steps"][-1]["tool_calls"][0]["arguments"],
                         {"raw": '{"act'})
        bare = dict(self.ACTION, reply=dict(self.ACTION["reply"], tool_calls=[{"name": "computer.stop", "arguments": None}]))
        self.assertEqual(job.trajectory_from_exchanges([], [bare], {"key": "original"}, SENT, "s")["steps"][-1]["tool_calls"][0]["arguments"], {})

    def test_a_runner_that_fails_after_an_action_is_not_an_answer_the_model_ran_out_of(self):
        ended = self.drive([self.ACTION])
        self.assertEqual(ended.kind, "NonZeroAgentExitCodeError")
        self.assertIn("wrote no result.txt", ended.said)
        silent = dict(self.ACTION, reply=dict(self.ACTION["reply"], tool_calls=[]))
        self.assertEqual(self.drive([self.ACTION, silent]).kind, "OutputLengthExceededError")
        self.assertEqual(self.drive([silent, self.ACTION]).kind, "NonZeroAgentExitCodeError")
        self.assertEqual(self.drive([self.ACTION, silent, dict(self.ACTION, side=True)]).kind, "OutputLengthExceededError")
        self.assertEqual(self.drive([self.ACTION, dict(silent, side=True)]).kind, "NonZeroAgentExitCodeError")
        self.assertEqual(self.drive([self.ACTION, silent], stop=relay.OVERFLOW + "\n").kind, "OutputLengthExceededError")

    def test_the_drivers_setup_or_verifier_error_and_a_failed_last_reply_keep_their_kinds(self):
        silent = dict(self.ACTION, reply=dict(self.ACTION["reply"], tool_calls=[]))
        for kind in ("EnvironmentSetupError", "VerifierError"):
            ended = self.drive([self.ACTION, silent], said=f"answer: {kind}: x")
            self.assertEqual((ended.kind, ended.said), (kind, f"OSWorld2 original: answer: {kind}: x"))
        failed = dict(silent, reply=dict(silent["reply"], end="failed", error={"code": "server_error"}))
        ended = self.drive([self.ACTION, failed, dict(self.ACTION, side=True)])
        self.assertEqual(ended.kind, "ApiError")
        self.assertIn("meta/muse-spark-1.3's last reply failed (server_error)", ended.said)
        self.assertEqual(self.drive([failed, self.ACTION]).kind, "NonZeroAgentExitCodeError")
        for late in (failed, silent):
            self.assertEqual(self.drive([dict(self.ACTION, t0=3), dict(late, t0=2)]).kind, "NonZeroAgentExitCodeError")
        self.assertEqual(run._kind(jsonl.Failed("score: EnvironmentSetupError: x"), "VerifierError"), "EnvironmentSetupError")
        self.assertEqual(run._kind(jsonl.Failed("score: RuntimeError: EnvironmentSetupError: x"), "VerifierError"),
                         "VerifierError")
        self.assertEqual(run._kind(jsonl.Failed("score: CancelledError: x"), "VerifierError"), "CancelledError")
        self.assertEqual(run._kind(jsonl.Failed("answer: CauseUnknownError: x"), "NonZeroAgentExitCodeError"), "CauseUnknownError")
        for kind in ("EnvironmentSetupError", "VerifierError"):
            ended = self.drive([self.ACTION], stop="turn limit\n", said=f"answer: {kind}: x")
            self.assertEqual((ended.kind, ended.said), (kind, f"OSWorld2 original: answer: {kind}: x"))

    def test_a_runner_that_fails_after_a_guards_stop_is_cancelled_not_out_of_output(self):
        silent = dict(self.ACTION, reply=dict(self.ACTION["reply"], tool_calls=[]))
        for stop, said in (("spent $2.39 >= $2.40\n", "spent $2.39 >= $2.40"), ("", relay.STOP)):
            with self.subTest(said):
                ended = self.drive([silent, self.ACTION, silent], stop=stop)
                self.assertEqual(ended.kind, run.CANCELLED)
                self.assertIn(f"a guard asked the unit to stop ({said})", ended.said)
                self.assertIn("wrote no result.txt", ended.said)
                self.assertTrue(ended.trajectory["steps"])


def _runner():
    up = os.environ.get("NULLIUS_OSWORLD2_UPSTREAM", "")
    path = os.path.join(up, "scripts", "python", "run_multienv_muse_spark.py")
    return path if up and os.path.isfile(path) else None


@unittest.skipUnless(_runner(), "NULLIUS_OSWORLD2_UPSTREAM names no upstream with run_multienv_muse_spark.py")
class Runner(unittest.TestCase):
    def test_every_flag_passed_is_one_the_runners_parser_has(self):
        with open(_runner(), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        known = {a.value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant) and str(a.value).startswith("--")}
        with open(os.path.join(HERE, "agents.json"), encoding="utf-8") as f:
            agents = json.load(f)
        passed = {w for w in agents["common"] + agents["agents"]["muse-spark"]["start"]["driver"]["argv"] if w.startswith("--")}
        self.assertEqual(passed - known, set())


if __name__ == "__main__":
    unittest.main()
