"""VitaBench in the standard runs its upstream: its runner, user simulator, tools and
evaluator. The simulated user and the judge are a local fake endpoint: no key, no
network. Skipped without the upstream and the driver's environment under
``STANDARD_DIR`` (``cli prove vitabench`` installs both)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.standard.contract import definitions
from nullius.standard.protocols import jsonl
from nullius.standard.requirements import install

APP = os.path.dirname(os.path.dirname(os.path.abspath(definitions.CATALOGUE)))      # what a driver imports
BENCH = definitions.get("benchmark", "vitabench")


def _ready():
    if not (install.ready(install.upstream_dir(BENCH)) and install.ready(install.driver_dir(BENCH))):
        return "VitaBench's upstream and driver environment are not installed (cli prove vitabench)"
    return ""


class Fake(BaseHTTPRequestHandler):
    """The simulated user, its lines from a script, and the judge, which finds every rubric met or cannot answer."""
    lines, asked, judge_ok, missed = [], [], True, None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.asked.append(body)
        said = json.dumps(body["messages"])
        if "<current_rubrics>" not in said:
            text = Fake.lines.pop(0)
        else:
            text = json.dumps([{"rubric_idx": k, "justification": "it did", "meetExpectation": k != Fake.missed}
                               for k in sorted(set(re.findall(r"rubric_\d+", said)))]) if Fake.judge_ok else None
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}] if text is not None else [],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
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
        cls.route = {"model": "fake", "base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1", "key": "placeholder"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Fake.lines, Fake.asked, Fake.judge_ok, Fake.missed = [], [], True, None

    def env(self, dataset):
        ds = next(d for d in BENCH["datasets"] if d["id"] == dataset)
        return dict(self.extra, PATH=os.environ.get("PATH", os.defpath), HOME=self.tmp, PYTHONPATH=APP,
                    PYTHONDONTWRITEBYTECODE="1", NULLIUS_UPSTREAM=self.up,
                    NULLIUS_DATASET=os.path.join(self.up, ds["path"]), NULLIUS_DATASET_ID=dataset)

    def opened(self, dataset, unit, lines):
        """A unit set up and its MCP server started: the setup, and how to list tools, call one, take a turn and score."""
        env = self.env(dataset)
        d = jsonl.Proc(self.argv, BENCH["_here"], env, os.path.join(self.tmp, "driver.log")).start()
        self.addCleanup(d.close)
        ws = os.path.join(self.tmp, "unit", "workspace")
        os.makedirs(ws)
        s = d.request("setup", unit=unit, dir=ws)
        cmd = install.words(BENCH["mcp_servers"]["vita"]["command"], {"here": BENCH["_here"], "install": install.driver_dir(BENCH)})
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             env=dict(env, **s["mcp"][0]["env"]))
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
        Fake.lines = list(lines)

        def call(name, arguments):
            got = ask("tools/call", {"name": name, "arguments": arguments})["result"]
            return got["content"][0]["text"], got["isError"]

        def at_once(*calls):
            """Several calls sent before any answer, as an agent's parallel calls are."""
            ids = []
            for name, arguments in calls:
                n[0] += 1
                ids.append(n[0])
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}}) + "\n")
            p.stdin.flush()
            while ids:
                m = json.loads(p.stdout.readline())
                if m.get("id") in ids:
                    ids.remove(m["id"])
        call.at_once = at_once

        def turn(said=None):
            t = [] if said is None else [{"role": "user", "text": "..."}, {"role": "agent", "text": said}]
            return d.request("turn", unit=unit, dir=ws, transcript=t, simulator=self.route, judge=self.route)

        def score():
            return d.request("score", unit=unit, dir=ws, transcript=[], answer="", judge=self.route)
        return s, ask, call, turn, score

    def test_the_orchestrator_executes_each_call_and_the_users_stop_ends_it(self):
        s, ask, call, turn, score = self.opened("cross-en", "A0812003", ["Lunch, please.", "Thanks. ###STOP###"])
        self.assertIn("Current time: ", s["prompt"])
        tools = ask("tools/list")["result"]["tools"]
        self.assertGreater(len(tools), 50)                               # the three domains' tools
        self.assertNotIn("$ref", json.dumps(tools))
        self.assertIn("not started", call("nope", {})[0])
        self.assertEqual(turn(), {"message": "Lunch, please."})
        self.assertEqual(Fake.asked[0]["messages"][1], {"role": "user", "content": "Hello, how can I help you?"})
        text, error = call("nope", {})
        self.assertTrue(error)
        self.assertNotIn("was not made", text)                           # the upstream's own answer
        text, error = call("longitude_latitude_to_distance", {"longitude1": 1, "latitude1": 1, "longitude2": 1, "latitude2": 1})
        self.assertFalse(error, text)
        self.assertEqual(turn("Ordered."), {"done": True})
        self.assertIn("conversation has ended (user_stop)", call("nope", {})[0])
        r = score()
        self.assertEqual((r["reward"], r["detail"]["termination"], r["detail"]["tool_calls"], r["detail"]["tool_errors"]),
                         (1.0, "user_stop", 2, 1), r["detail"])
        self.assertNotIn("window_evaluations", r["detail"]["reward_info"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "unit", "verifier", "simulation.json")))

    def test_calls_made_at_once_are_one_step_as_the_upstreams_parallel_calls(self):
        _, _, call, turn, score = self.opened("cross-en", "A0812003", ["Lunch, please.", "Thanks. ###STOP###"])
        turn()
        args = {"longitude1": 1, "latitude1": 1, "longitude2": 1, "latitude2": 1}
        call.at_once(("longitude_latitude_to_distance", args), ("longitude_latitude_to_distance", args))
        turn("Ordered.")
        score()
        with open(os.path.join(self.tmp, "unit", "verifier", "simulation.json")) as f:
            said = [m for m in json.load(f)["messages"] if m["role"] == "assistant" and m.get("tool_calls")]
        self.assertEqual([len(m["tool_calls"]) for m in said], [2])

    def test_ten_tool_errors_score_zero_unjudged(self):
        _, _, call, turn, score = self.opened("delivery", "10711001", ["你好"])
        turn()
        for _ in range(9):
            call("nope", {})
        self.assertIn("too_many_errors", call("nope", {})[0])
        self.assertEqual(turn("bye ###STOP###"), {"done": True})
        r = score()
        self.assertEqual((r["reward"], r["detail"]["termination"]), (0.0, "too_many_errors"))
        self.assertEqual(len(Fake.asked), 1)                             # the user's first line alone

    def test_the_agents_stop_is_judged(self):
        _, _, _, turn, score = self.opened("instore", "10826004", ["你好"])
        turn()
        self.assertEqual(turn("好的 ###STOP###"), {"done": True})
        r = score()
        self.assertEqual((r["reward"], r["detail"]["termination"]), (1.0, "agent_stop"))

    def test_one_missed_rubric_fails_the_task_and_is_named(self):
        _, _, _, turn, score = self.opened("instore", "10826004", ["你好"])
        Fake.missed = "rubric_0"
        turn()
        turn("好的 ###STOP###")
        r = score()
        self.assertEqual((r["reward"], r["passed"], list(r["detail"]["rubrics_missed"].values())), (0.0, False, ["it did"]))

    def test_an_empty_final_message_is_the_agents_invalid_message(self):
        _, _, call, turn, score = self.opened("ota", "D0812006", ["你好"])
        turn()
        call("nope", {})
        self.assertEqual(turn(""), {"done": True})
        r = score()
        self.assertEqual((r["reward"], r["detail"]["termination"]), (0.0, "invalid_agent_message"))

    def test_a_judge_that_fails_is_the_units_error_never_a_score(self):
        _, _, _, turn, score = self.opened("delivery-en", "10711001", ["Hello."])
        Fake.judge_ok = False
        turn()
        self.assertEqual(turn("ok ###STOP###"), {"done": True})
        with self.assertRaises(jsonl.Failed) as e:
            score()
        self.assertIn("evaluator raised", str(e.exception))

    def test_a_failed_user_call_starts_the_conversation_again(self):
        _, _, _, turn, score = self.opened("delivery-en", "10711001", ["Hello.", "", "Hello again.", "Bye ###STOP###"])
        turn()
        self.assertEqual(turn("What would you like?"), {"message": "Hello again.", "restart": True})
        self.assertEqual(turn("Done."), {"done": True})
        self.assertEqual(score()["detail"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
