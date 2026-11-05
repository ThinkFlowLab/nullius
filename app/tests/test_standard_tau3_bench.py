"""τ³-bench in the standard runs its upstream: the upstream's batch runner
(run_tasks) runs the unit's task - its seed for the trial, its retries - and
its Orchestrator holds the conversation - its user simulator, its environment executing each
tool call the agent makes over MCP, its limits and stop rules - and its
evaluator scores the SimulationRun the Orchestrator returns. The MCP server
declares the upstream's tools and passes each call to the driver. The
simulated user and the judge are a local fake endpoint (litellm pointed at
it, as at the relay): no key, no network.

These checks need the upstream and the driver's environment installed under
``STANDARD_DIR`` (``cli prove tau3-bench`` installs both) and are skipped
without them."""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.standard import definitions, install, jsonl

APP = os.path.dirname(os.path.dirname(os.path.abspath(definitions.CATALOGUE)))      # what a driver imports
BENCH = definitions.get("benchmark", "tau3-bench")


def _ready():
    up, drv = install.upstream_dir(BENCH), install.driver_dir(BENCH)
    if not (install.ready(up) and install.ready(drv)):
        return "τ³-bench's upstream and driver environment are not installed (cli prove tau3-bench)"
    return ""


class Fake(BaseHTTPRequestHandler):
    """The simulated user and the judge, OpenAI-compatible: the user says its
    next line from a script; the judge finds every assertion met."""
    lines = []
    asked = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.asked.append(body)
        if "expectedOutcomes" in json.dumps(body["messages"]):
            said = json.dumps({"results": [{"expectedOutcome": "x", "reasoning": "it did", "metExpectation": True}]})
        else:
            said = Fake.lines.pop(0)
        out = json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": body["model"],
                          "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": said}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@unittest.skipIf(_ready(), _ready())
class Upstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = install.upstream_dir(BENCH)
        cls.argv, cls.extra = install.driver_argv(BENCH)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.route = {"model": "gpt-4.1-2025-04-14", "base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1", "key": "placeholder"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Fake.lines, Fake.asked = [], []

    def env(self, domain):
        ds = next(d for d in BENCH["datasets"] if d["id"] == domain)
        return dict(self.extra, PATH=os.environ.get("PATH", os.defpath), HOME=self.tmp, PYTHONPATH=APP,
                    PYTHONDONTWRITEBYTECODE="1", NULLIUS_UPSTREAM=self.up,
                    NULLIUS_DATASET=os.path.join(self.up, ds["path"]), NULLIUS_DATASET_ID=domain)

    def driver(self, domain):
        p = jsonl.Proc(self.argv, BENCH["_here"], self.env(domain), os.path.join(self.tmp, "driver.log")).start()
        self.addCleanup(p.close)
        return p

    def unit_dir(self, name):
        ws = os.path.join(self.tmp, name, "workspace")
        os.makedirs(ws)
        return ws

    def server(self, domain, setup):
        """The server as the run starts it, from the unit's setup, spoken to over stdio in JSON lines."""
        cmd = install.words(BENCH["mcp_servers"]["domain"]["command"], {"here": BENCH["_here"], "install": install.driver_dir(BENCH)})
        env = dict(self.env(domain), **setup["mcp"][0]["env"])
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdin.close(), p.stdout.close()))
        n = [0]

        def ask(method, params=None):
            n[0] += 1
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            while True:
                m = json.loads(p.stdout.readline())
                if m.get("id") == n[0]:
                    return m
        ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        return ask

    def call(self, ask, name, arguments):
        got = ask("tools/call", {"name": name, "arguments": arguments})["result"]
        return got["content"][0]["text"], got["isError"]

    def opened(self, domain, unit, lines, judge=None):
        """A unit set up, its server started, and the conversation opened: the user's first line."""
        d = self.driver(domain)
        ws = self.unit_dir("u" + unit)
        s = d.request("setup", unit=unit, dir=ws)
        ask = self.server(domain, s)
        Fake.lines = list(lines)
        first = d.request("turn", unit=unit, dir=ws, transcript=[], simulator=self.route, **({"judge": judge} if judge else {}))
        return d, ws, ask, first

    def test_the_server_declares_the_upstreams_tools_and_executes_nothing_itself(self):
        d = self.driver("airline")
        s = d.request("setup", unit="0", dir=self.unit_dir("a"))
        ask = self.server("airline", s)
        tools = ask("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 14)
        self.assertTrue(all("$ref" not in json.dumps(t["inputSchema"]) for t in tools))      # references written out
        text, error = self.call(ask, "get_user_details", {"user_id": "mia_li_3668"})          # before the conversation: not made
        self.assertTrue(error)
        self.assertIn("this call was not made", text)

    def test_the_prompt_is_the_upstreams_agent_instructions_and_policy(self):
        d = self.driver("airline")
        s = d.request("setup", unit="0", dir=self.unit_dir("a"))
        self.assertTrue(s["prompt"].startswith("<instructions>\nYou are a customer service agent"))
        self.assertIn("Always make sure you generate valid JSON only.", s["prompt"])
        with open(os.path.join(self.up, "data", "tau2", "domains", "airline", "policy.md")) as f:
            self.assertIn(f.read().strip()[:300], s["prompt"])
        self.assertTrue(s["prompt"].endswith("You said to the user: Hi! How can I help you today?\nThe user says:"))
        [m] = s["mcp"]
        self.assertEqual((m["name"], m["server"], sorted(m["env"])), ("airline", "domain", ["TAU_AT", "TAU_DOMAIN"]))

    def test_the_reference_passes_and_nothing_said_fails(self):
        d = self.driver("airline")
        for unit in ("1", "3"):
            ws = self.unit_dir("ref" + unit)
            d.request("setup", unit=unit, dir=ws)
            answer = d.request("oracle", unit=unit, dir=ws)["answer"]
            r = d.request("score", unit=unit, dir=ws, transcript=[], answer=answer)
            self.assertEqual((r["reward"], r["passed"]), (1.0, True), r["detail"])
            self.assertEqual(r["detail"]["evaluation"], "all_ignore_basis")
        ws = self.unit_dir("nop")
        d.request("setup", unit="3", dir=ws)
        r = d.request("score", unit="3", dir=ws, transcript=[], answer="")
        self.assertEqual((r["reward"], r["detail"]["termination"]), (0.0, "agent_error"))
        self.assertIn("terminated prematurely", r["detail"]["reward_info"]["info"]["note"])

    def test_the_orchestrator_executes_each_call_and_the_users_stop_ends_it(self):
        d, ws, ask, first = self.opened("airline", "0", ["I want to change my flight.", "Fine, thanks. ###STOP###"])
        self.assertEqual(first, {"message": "I want to change my flight."})
        sys_prompt = Fake.asked[0]["messages"][0]["content"]
        self.assertIn("<scenario>", sys_prompt)
        self.assertEqual(Fake.asked[0]["messages"][1], {"role": "user", "content": "Hi! How can I help you today?"})
        self.assertEqual(Fake.asked[0]["temperature"], 0.0)
        # the trial's seed, as run_tasks draws it from its base seed (300) for trial 0 of one
        random.seed(300)
        seed = random.randint(0, 1000000)
        self.assertEqual(Fake.asked[0]["seed"], seed)
        text, error = self.call(ask, "get_user_details", {"user_id": "mia_li_3668"})
        self.assertFalse(error)
        self.assertEqual(json.loads(text)["user_id"], "mia_li_3668")
        self.assertEqual(self.call(ask, "nope", {}), ("Error: Tool 'nope' not found.", True))
        missing, error = self.call(ask, "get_user_details", {})                               # never refused before the upstream sees it
        self.assertIn("missing 1 required positional argument", missing)
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": "I cannot change it."}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"done": True})
        self.assertEqual([m["role"] for m in Fake.asked[1]["messages"]], ["system", "user", "assistant", "user"])  # roles flipped
        r = d.request("score", unit="0", dir=ws, transcript=t, answer="I cannot change it.")
        dt = r["detail"]
        self.assertEqual((dt["termination"], r["reward"], dt["user_turns"]), ("user_stop", 1.0, 2), dt)
        self.assertEqual((dt["tool_calls"], dt["tool_errors"]), (3, 2))
        self.assertEqual((dt["max_steps"], dt["max_errors"], dt["seed"], dt["trial"], dt["attempts"]), (200, 10, seed, 0, 1))
        with open(os.path.join(os.path.dirname(ws), "verifier", "simulation.json")) as f:
            sim = json.load(f)
        self.assertEqual([m["role"] for m in sim["messages"]],
                         ["assistant", "user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant", "user"])
        self.assertEqual(sim["termination_reason"], "user_stop")

    def test_too_many_tool_errors_end_it_by_the_upstreams_limit_and_later_calls_are_not_made(self):
        d, ws, ask, _ = self.opened("airline", "0", ["Hello."])
        for i in range(9):
            self.assertEqual(self.call(ask, "nope", {}), ("Error: Tool 'nope' not found.", True))
        text, error = self.call(ask, "nope", {})                       # the tenth: made, then the Orchestrator ends
        self.assertTrue(error)
        self.assertIn("conversation has ended (too_many_errors)", text)
        text, _ = self.call(ask, "get_user_details", {"user_id": "mia_li_3668"})
        self.assertIn("this call was not made", text)
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": "Sorry."}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"done": True})
        r = d.request("score", unit="0", dir=ws, transcript=t, answer="Sorry.")
        self.assertEqual((r["reward"], r["detail"]["termination"], r["detail"]["tool_calls"]), (0.0, "too_many_errors", 10))

    def test_the_agent_saying_stop_does_not_end_it_as_the_upstreams_llm_agent_does_not(self):
        d, ws, ask, _ = self.opened("airline", "0", ["Hello.", "Goodbye. ###STOP###"])
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": "Done. ###STOP###"}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"done": True})
        self.assertEqual(len(Fake.asked), 2)                                     # the user was still asked
        r = d.request("score", unit="0", dir=ws, transcript=t, answer="Done. ###STOP###")
        self.assertEqual(r["detail"]["termination"], "user_stop")

    def test_a_conversation_the_upstream_has_not_ended_is_not_scored(self):
        d, ws, ask, _ = self.opened("airline", "0", ["Hello.", "Still there?"])
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": "Yes?"}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"message": "Still there?"})
        with self.assertRaises(jsonl.Failed):
            d.request("score", unit="0", dir=ws, transcript=t, answer="Yes?")
        self.assertIn("prompt", d.request("setup", unit="1", dir=self.unit_dir("next")))     # the next unit starts afresh

    def test_an_attempt_that_raises_is_made_again_by_the_upstreams_runner_with_a_new_session(self):
        """An empty final message: the upstream's message check refuses it, its
        run_with_retry builds the conversation again, and the agent is told to
        start a new session with the user's first message."""
        d, ws, ask, _ = self.opened("airline", "0", ["Hello.", "Hello again.", "Bye. ###STOP###"])
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": ""}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route),
                         {"message": "Hello again.", "restart": True})
        text, error = self.call(ask, "get_user_details", {"user_id": "mia_li_3668"})      # the new attempt's environment
        self.assertFalse(error)
        t += [{"role": "user", "text": "..."}, {"role": "agent", "text": "Done."}]
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"done": True})
        r = d.request("score", unit="0", dir=ws, transcript=t, answer="Done.")
        self.assertEqual((r["detail"]["attempts"], r["detail"]["termination"], r["detail"]["tool_calls"]), (2, "user_stop", 1))
        self.assertEqual(len({b["seed"] for b in Fake.asked}), 1)          # every attempt at the trial's seed, as upstream

    def test_after_its_last_retry_the_runners_infrastructure_error_is_the_units_error_not_a_score(self):
        d, ws, ask, _ = self.opened("airline", "0", ["Hello."] * 4)
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": ""}]
        for _ in range(3):                                                  # its three retries
            self.assertTrue(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route)["restart"])
        self.assertEqual(d.request("turn", unit="0", dir=ws, transcript=t, simulator=self.route), {"done": True})
        with self.assertRaises(jsonl.Failed) as e:
            d.request("score", unit="0", dir=ws, transcript=t, answer="")
        self.assertIn("INFRASTRUCTURE_ERROR after 4 attempts", str(e.exception))
        self.assertIn("must have either content or tool_calls", str(e.exception))

    def test_the_judge_is_the_upstreams_nl_evaluator_at_the_judges_route(self):
        d, ws, ask, _ = self.opened("retail", "2", ["I want to return a t-shirt.", "Thanks. ###STOP###"], judge=self.route)
        t = [{"role": "user", "text": "..."}, {"role": "agent", "text": "There are 10 t-shirt options."}]
        self.assertEqual(d.request("turn", unit="2", dir=ws, transcript=t, simulator=self.route, judge=self.route), {"done": True})
        r = d.request("score", unit="2", dir=ws, transcript=t, answer="", judge=self.route)
        judged = [b for b in Fake.asked if "expectedOutcomes" in json.dumps(b["messages"])]
        self.assertEqual(len(judged), 1)
        self.assertEqual(judged[0]["model"], "gpt-4.1-2025-04-14")                # the upstream's own model name
        self.assertIn("There are 10 t-shirt options", json.dumps(judged[0]["messages"]))
        self.assertEqual(r["detail"]["judge"], "gpt-4.1-2025-04-14")
        self.assertEqual(r["detail"]["reward_info"]["reward_breakdown"]["NL_ASSERTION"], 1.0)
        self.assertEqual(r["detail"]["reward_info"]["reward_breakdown"]["DB"], 0.0)          # no return was made
        self.assertEqual(r["reward"], 0.0)

    def test_without_a_judge_a_task_that_asks_one_is_an_error_before_anyone_speaks(self):
        d = self.driver("retail")
        ws = self.unit_dir("nojudge")
        d.request("setup", unit="2", dir=ws)
        with self.assertRaises(jsonl.Failed):
            d.request("turn", unit="2", dir=ws, transcript=[], simulator=self.route)
        self.assertEqual(Fake.asked, [])


if __name__ == "__main__":
    unittest.main()
