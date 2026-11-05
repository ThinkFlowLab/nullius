"""τ³-bench's setups and settings: the original (the upstream's own agent as
any model, in its own batch runner), the options that set what tau2 run sets
- each where it applies, each defaulting to what every earlier run did - the
attempts told apart by the upstream's seed per trial, and the inventory of
every setting of its evaluation.

The definition checks need nothing installed. The driver's checks need the
upstream and the driver's environment under ``STANDARD_DIR`` (``cli prove
tau3-bench`` installs both) and are skipped without them; their models are a
local fake endpoint, no key, no network."""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.standard import definitions, install, jsonl

APP = os.path.dirname(os.path.dirname(os.path.abspath(definitions.CATALOGUE)))
BENCH = definitions.get("benchmark", "tau3-bench")


def seeds(n):
    """run_tasks's seeds for a run of n trials: random.seed(300), then randint(0, 1000000) per trial."""
    random.seed(300)
    return [random.randint(0, 1000000) for _ in range(n)]


class Definition(unittest.TestCase):
    def test_the_benchmark_and_its_inventory_are_valid(self):
        with open(os.path.join(BENCH["_here"], "standard.json")) as f:
            self.assertEqual(definitions.problems(json.load(f), "benchmark"), [])
        self.assertEqual(definitions.settings_problems([BENCH]), [])

    def test_the_original_takes_any_model_and_sends_it_as_given(self):
        s, why = definitions.setup(BENCH, "original", "gpt-4.1-mini", "openai")
        self.assertEqual((s["asked"], s["sent"], s["comparable"], why), ("gpt-4.1-mini", "gpt-4.1-mini", True, ""))
        self.assertIsNone(definitions.setup(BENCH, "original", "", "openai")[0])
        self.assertIsNone(definitions.setup(BENCH, "model", "gpt-4.1-mini", "openai")[0])

    def test_each_option_applies_where_tau2_run_applies_it_at_what_earlier_runs_did(self):
        opts = lambda ds, setup: definitions.bench_options_of(BENCH, ds, None, setup)[0]
        self.assertEqual(opts("airline", "yours"), {"task_split": "base", "user_reasoning_effort": "default"})
        self.assertEqual(opts("airline", "original"), {"task_split": "base", "user_reasoning_effort": "default",
                                                       "agent_reasoning_effort": "default", "upstream_agent": "llm_agent"})
        self.assertEqual(opts("banking_knowledge", "yours"), {"retrieval": "bm25", "user_reasoning_effort": "default"})
        self.assertEqual(opts("banking_knowledge", "original")["retrieval"], "bm25")

    def test_attempts_are_told_apart(self):
        self.assertIn("seed", BENCH["limits"]["attempts"])

    def test_every_option_and_dataset_is_in_the_inventory(self):
        with open(os.path.join(BENCH["_here"], definitions.SETTINGS_FILE)) as f:
            rows = json.load(f)
        named = {r["nullius"] for r in rows}
        for n in BENCH["options"]:
            self.assertIn(f"option:{n}", named)
        for d in BENCH["datasets"]:
            self.assertIn(f"dataset:{d['id']}", named)
        self.assertIn("setup:original", named)


def _upstream():
    up, drv = install.upstream_dir(BENCH), install.driver_dir(BENCH)
    return "" if install.ready(up) and install.ready(drv) else "τ³-bench's upstream and driver environment are not installed"


@unittest.skipIf(_upstream(), _upstream())
class Variants(unittest.TestCase):
    def test_every_retrieval_variant_of_the_upstream_is_offered_or_recorded(self):
        with open(os.path.join(install.upstream_dir(BENCH), "src", "tau2", "domains", "banking_knowledge", "retrieval.py")) as f:
            upstream = set(re.findall(r'^    "([a-z0-9_-]+)": (?:RetrievalVariant|all_tools_variant)\(', f.read(), re.M))
        with open(os.path.join(BENCH["_here"], definitions.SETTINGS_FILE)) as f:
            rows = json.load(f)
        recorded = {r["setting"].split()[-1] for r in rows if r["setting"].startswith("--retrieval-config ")}
        offered = set(BENCH["options"]["retrieval"]["values"])
        self.assertEqual(len(upstream), 20)
        self.assertEqual(offered | recorded, upstream)
        self.assertFalse(offered & recorded)


class Fake(BaseHTTPRequestHandler):
    """The run's models, OpenAI-compatible, told apart by their route: the agent ends its turn with a
    line, the user says its scripted lines."""
    lines, asked = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        who = self.path.split("/")[1]
        Fake.asked.append((who, body))
        said = "Is there anything else?" if who == "agent" else Fake.lines.pop(0)
        out = json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": body["model"],
                          "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": said}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@unittest.skipIf(_upstream(), _upstream())
class Driver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = install.upstream_dir(BENCH)
        cls.argv, cls.extra = install.driver_argv(BENCH)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        at = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.user = {"model": "gpt-4.1-2025-04-14", "base_url": at + "/user/v1", "key": "placeholder"}
        cls.agent = {"model": "gpt-4.1-mini", "base_url": at + "/agent/v1", "key": "placeholder"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Fake.lines, Fake.asked = [], []

    def driver(self, domain="airline", **options):
        ds = next(d for d in BENCH["datasets"] if d["id"] == domain)
        env = dict(self.extra, PATH=os.environ.get("PATH", os.defpath), HOME=self.tmp, PYTHONPATH=APP, PYTHONDONTWRITEBYTECODE="1",
                   NULLIUS_UPSTREAM=self.up, NULLIUS_DATASET=os.path.join(self.up, ds["path"]) if ds.get("path") else "",
                   NULLIUS_DATASET_ID=domain, **{"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()})
        p = jsonl.Proc(self.argv, BENCH["_here"], env, os.path.join(self.tmp, "driver.log")).start()
        self.addCleanup(p.close)
        return p

    def ws(self, name):
        w = os.path.join(self.tmp, name, "workspace")
        os.makedirs(w)
        return w

    def test_the_units_follow_the_split_and_the_upstream_agents_own_filter(self):
        self.assertEqual(len(self.driver().request("units", dataset="airline")["units"]), 50)
        self.assertEqual(len(self.driver(task_split="test").request("units", dataset="airline")["units"]), 20)
        gt = self.driver(upstream_agent="llm_agent_gt")
        self.assertEqual(len(gt.request("units", dataset="airline")["units"]), 50)          # an agent's run: every task
        self.assertEqual(len(gt.request("units", dataset="airline", setup="original")["units"]), 43)
        with self.assertRaises(jsonl.Failed) as e:                                              # telecom's split only
            self.driver(task_split="small").request("units", dataset="airline")
        self.assertIn("Invalid task split name: small", str(e.exception))

    def test_the_original_is_the_upstreams_agent_in_its_batch_runner(self):
        d = self.driver()
        ws = self.ws("o")
        Fake.lines = ["I want to change my flight.", "No, thanks. ###STOP###"]
        a = d.request("answer", unit="0", dir=ws, setup="original", simulator=self.user, **{k: self.agent[k] for k in ("model", "base_url", "key")})
        self.assertEqual(a, {"answer": "Is there anything else?"})
        agent = [b for who, b in Fake.asked if who == "agent"]
        self.assertEqual((agent[0]["model"], agent[0]["temperature"], agent[0]["seed"]), ("gpt-4.1-mini", 0.0, seeds(1)[0]))
        self.assertTrue(agent[0]["tools"])                                                     # the domain's tools, the upstream's
        r = d.request("score", unit="0", dir=ws, transcript=[], answer=a["answer"], setup="original")
        dt = r["detail"]
        self.assertEqual((dt["termination"], dt["trial"], dt["seed"], dt["agent"], dt["split"]),
                         ("user_stop", 0, seeds(1)[0], "llm_agent", "base"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "o", "verifier", "simulation.json")))

    def test_the_original_without_the_users_route_says_so(self):
        with self.assertRaises(jsonl.Failed) as e:
            self.driver().request("answer", unit="0", dir=self.ws("o"), setup="original", **{k: self.agent[k] for k in ("model", "base_url", "key")})
        self.assertIn("simulator's route", str(e.exception))

    def test_a_reasoning_effort_replaces_the_default_arguments_as_the_cli_does(self):
        d = self.driver(agent_reasoning_effort="high", user_reasoning_effort="low")
        Fake.lines = ["Hello.", "Bye. ###STOP###"]
        # models that reason: litellm, as the upstream sets it (drop_params), drops the effort for one it knows does not
        d.request("answer", unit="0", dir=self.ws("o"), setup="original", simulator=dict(self.user, model="gpt-5-mini"),
                  **dict({k: self.agent[k] for k in ("model", "base_url", "key")}, model="gpt-5-mini"))
        by = {who: b for who, b in reversed(Fake.asked)}
        self.assertEqual((by["agent"].get("reasoning_effort"), "temperature" in by["agent"]), ("high", False))
        self.assertEqual((by["user"].get("reasoning_effort"), "temperature" in by["user"]), ("low", False))

    def test_attempt_n_is_the_upstreams_trial_n_minus_1_at_its_seed(self):
        d = self.driver()
        ws = self.ws("a2")
        Fake.lines = ["Hello.", "Bye. ###STOP###"]
        d.request("answer", unit="0", dir=ws, setup="original", simulator=self.user, attempt=2,
                  **{k: self.agent[k] for k in ("model", "base_url", "key")})
        self.assertEqual(Fake.asked[0][1]["seed"], seeds(2)[1])
        dt = d.request("score", unit="0", dir=ws, transcript=[], answer="", setup="original")["detail"]
        self.assertEqual((dt["trial"], dt["seed"]), (1, seeds(2)[1]))
        # an ACP agent's run the same way: its first turn opens trial 1 at that trial's seed
        Fake.asked = []
        ws = self.ws("acp2")
        d.request("setup", unit="0", dir=ws, attempt=2)
        Fake.lines = ["I want to change my flight."]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=[], simulator=self.user, attempt=2),
                         {"message": "I want to change my flight."})
        self.assertEqual(Fake.asked[0][1]["seed"], seeds(2)[1])


if __name__ == "__main__":
    unittest.main()
