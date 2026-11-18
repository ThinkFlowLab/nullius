"""A pinned route's failure on top of the serving pin (``serving``): a pinned call no endpoint took, or one its reply
says another provider served, fails the unit as its route's. Before a fake OpenRouter."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.runs import classify
from nullius.providers import STEP
from nullius.standard import definitions, install, job, prove, recipe, relay, run

M3, SONNET = "minimax/minimax-m3", "anthropic/claude-sonnet-4.6"
PINS = {M3: ["minimax"], SONNET: ["anthropic"]}
SERVES = {M3: ["Parasail", "MiniMax"], SONNET: ["Google", "Anthropic"]}
SECRET = "the words of a gated task"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")


class OpenRouter(BaseHTTPRequestHandler):
    """Routes as OpenRouter documents them: ``down`` a 503, no endpoint of the pin a 404."""
    ignore_pin = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model, pref = body.get("model"), body.get("provider") or {}
        allowed = [e for e in SERVES.get(model, ["Someone"])
                   if OpenRouter.ignore_pin or not pref.get("order") or e.lower() in pref["order"]]
        if body.get("down"):
            allowed = []
        if not allowed:
            status = 503 if body.get("down") else 404
            return self.reply(status, {"error": {"code": status, "message": f"No allowed providers are available for the selected model: {SECRET}"}})
        if self.path.endswith("/messages"):
            return self.reply(200, {"id": "gen-555", "type": "message", "role": "assistant", "content": [{"type": "text", "text": SECRET}],
                                    "usage": {"input_tokens": 3, "output_tokens": 1}})
        self.reply(200, {"id": "gen-123", "provider": allowed[0], "choices": [{"message": {"role": "assistant", "content": SECRET}}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.0001}})

    def reply(self, status, out):
        body = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.PLACEHOLDER_KEY})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


class Pinned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), OpenRouter)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.relay = self.pinned(PINS)

    def pinned(self, serving, model=M3):
        r = relay.Relay(self.base, "sk-real", tempfile.mkdtemp(dir=self.dir), model=model,
                        side_models=["claude-sonnet-4-6=" + SONNET], serving=serving).start()
        self.addCleanup(r.stop)
        return r

    def test_no_endpoint_of_the_pin_is_the_routes_error_not_an_empty_answer(self):
        r = self.pinned({M3: ["fireworks"]})
        status, _ = post(r.url("u3") + "/messages", {"model": "MiniMax-M3", "messages": []})
        self.assertEqual(status, 404)
        kind, said = relay.failure(r.calls("u3"), serving={M3: ["fireworks"]})
        self.assertEqual(kind, "ApiProviderRouteError")
        self.assertIn("pinned to fireworks", said)
        self.assertEqual(classify.from_verdict(kind, None), classify.API_ERROR)

    def test_a_pinned_provider_down_is_the_routes_error(self):
        post(self.relay.url("u1") + "/messages", {"model": "MiniMax-M3", "messages": [], "down": True})
        (call,) = self.relay.calls("u1")
        self.assertEqual(call["status"], 503)
        self.assertEqual(relay.failure(self.relay.calls("u1"), serving=PINS)[0], "ApiProviderRouteError")

    def test_a_gated_benchmark_keeps_the_endpoints_words_out_of_a_route_error(self):
        post(self.relay.url("u1") + "/messages", {"model": "MiniMax-M3", "messages": [], "down": True})
        kind, said = relay.failure(self.relay.calls("u1"), words=False, serving=PINS)
        self.assertEqual(kind, "ApiProviderRouteError")
        self.assertIn("pinned to minimax", said)
        self.assertIn("answered 503", said)
        self.assertNotIn(SECRET, said)
        self.assertNotIn("No allowed providers", said)

    def test_an_unpinned_404_is_still_what_it_was(self):
        calls = [{"status": 404, "model": "other/model", "error": "HTTP 404: model not found"}]
        self.assertEqual(relay.failure(calls, serving=PINS)[0], "ModelNotFoundError")
        self.assertIsNone(relay.route_failure(calls, PINS))

    def test_a_side_models_refusal_fails_the_unit_even_when_the_agent_answered_last(self):
        unit = self.relay.url("u1")
        post(unit + "/messages", {"model": "claude-sonnet-4-6", "messages": [], "down": True})
        post(unit + "/messages", {"model": "MiniMax-M3", "messages": []})
        calls = self.relay.calls("u1")
        self.assertIsNone(relay.failure(calls, serving=PINS))
        kind, said = relay.route_failure(calls, PINS, words=False)
        self.assertEqual(kind, "ApiProviderRouteError")
        self.assertIn(SONNET, said)
        post(unit + "/messages", {"model": "claude-sonnet-4-6", "messages": []})
        self.assertIsNone(relay.route_failure(self.relay.calls("u1"), PINS))

    def test_a_routes_calls_are_read_in_the_order_they_began(self):
        pin, refused = {M3: ["fireworks"]}, {"model": M3, "status": 404}
        self.assertIsNone(relay.route_failure([{"model": M3, "status": 200, "t0": 2}, dict(refused, t0=1)], pin))
        self.assertEqual(relay.route_failure([dict(refused, t0=2), {"model": M3, "status": 200, "t0": 1}], pin)[0],
                         "ApiProviderRouteError")
        self.assertEqual(relay.route_failure([{"model": M3, "status": 200, "t0": 2}, refused], pin)[0], "ApiProviderRouteError")

    def test_a_judges_answer_does_not_cover_the_simulated_users_refusal_of_the_same_model(self):
        unit, judge = self.relay.url("u1"), self.relay.url("u1-judge", model=SONNET)
        post(unit + "/messages", {"model": "claude-sonnet-4-6", "messages": [], "down": True})
        post(unit + "/messages", {"model": "MiniMax-M3", "messages": []})
        post(judge + "/messages", {"model": SONNET, "messages": []})
        routes = [self.relay.calls("u1"), self.relay.calls("u1-judge"), []]
        self.assertEqual(relay.routes_failure(routes, PINS, words=False)[0], "ApiProviderRouteError")
        self.assertIsNone(relay.routes_failure([self.relay.calls("u1-judge")], PINS))

    def test_only_a_route_with_no_endpoint_stops_the_run(self):
        post(self.relay.url("u1") + "/messages", {"model": "MiniMax-M3", "messages": [], "down": True})
        self.assertFalse(relay.route_dead(relay.failure(self.relay.calls("u1"), serving=PINS)))
        r = self.pinned({M3: ["fireworks"]})
        post(r.url("u5") + "/messages", {"model": "MiniMax-M3", "messages": []})
        self.assertTrue(relay.route_dead(relay.failure(r.calls("u5"), words=False, serving={M3: ["fireworks"]})))
        self.assertFalse(relay.route_dead(("ApiError", "404: x")))

    def test_an_answer_from_outside_the_pin_fails_the_unit_as_its_route(self):
        pin = {"openai/gpt-5-mini": ["minimax"]}
        r = self.pinned(pin, model="openai/gpt-5-mini")
        SERVES["openai/gpt-5-mini"] = ["Parasail"]
        self.addCleanup(SERVES.pop, "openai/gpt-5-mini")
        OpenRouter.ignore_pin = True
        self.addCleanup(setattr, OpenRouter, "ignore_pin", False)
        post(r.url("u4") + "/chat/completions", {"model": "x", "messages": []})
        (call,) = r.calls("u4")
        self.assertEqual((call["status"], call["reply"]["served_by"]), (200, "Parasail"))
        self.assertIsNone(relay.failure(r.calls("u4"), serving=pin))
        kind, said = relay.route_failure(r.calls("u4"), pin, words=False)
        self.assertEqual(kind, "ApiProviderRouteError")
        self.assertIn("served by Parasail", said)
        self.assertNotIn(SECRET, said)
        self.assertIsNone(relay.route_failure(r.calls("u4"), {}))

    def test_a_provider_is_off_its_pin_only_when_its_name_shares_nothing_with_the_pin(self):
        self.assertIs(relay.held("MiniMax", ["minimax"]), True)
        self.assertIs(relay.held("Anthropic", ["anthropic"]), True)
        self.assertIs(relay.held("Google AI Studio", ["google-ai-studio"]), True)
        self.assertIs(relay.held("Parasail", ["minimax"]), False)
        self.assertIsNone(relay.held("Google", ["google-vertex"]))
        self.assertIsNone(relay.route_failure([{"status": 200, "model": "m", "reply": {"served_by": "Google"}}], {"m": ["google-vertex"]}))

    def test_a_name_that_is_not_a_providers_name_fails_nothing_and_is_never_said(self):
        odd = [{"status": 200, "model": "m", "reply": {"served_by": "Parasail\n" + SECRET}}]
        self.assertIsNone(relay.route_failure(odd, {"m": ["minimax"]}, words=False))


class Definition(unittest.TestCase):
    def test_a_benchmark_without_pins_runs_as_before(self):
        benches, _ = definitions.load("benchmark")
        for b in benches:
            with self.subTest(b["key"]):
                self.assertEqual(run.pins_of(b, {"_kind": "agent"}, "openrouter", "m"), {})
                for which in (b.get("setups") or {}):
                    if which == "original":
                        other, _ = definitions.setup(b, which, "", "openrouter")
                        self.assertNotIn("pins", other or {})

    def test_served_by_replaces_the_definitions_pin_and_says_so(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(setattr, install, "HOME", install.HOME)
        install.HOME = os.path.join(tmp, "home")
        b = definitions.get("benchmark", "fakebench", ROOT)
        b = dict(b, score=dict(b["score"], judge={"model": SONNET, "says": "a verdict", "pin": {"openrouter": ["anthropic"]}}))
        said = []
        make = lambda **kw: run.Run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="openrouter",
                                    env={"OPENROUTER_API_KEY": "k"}, unsandboxed=True, jobs_root=tmp, say=said.append, **kw)
        r = make()
        self.assertEqual((r.serving, r.route_pins, r.pins_replaced), ({SONNET: ["anthropic"]}, {SONNET: ["anthropic"]}, {}))
        r = make(served_by={SONNET: ["anthropic"]})
        self.assertEqual((r.route_pins, r.pins_replaced, said), ({SONNET: ["anthropic"]}, {}, []))
        r = make(served_by={"m-1": ["prov-a"], SONNET: ["google-vertex"]})
        self.assertEqual((r.serving, r.route_pins, r.pins_replaced), ({"m-1": ["prov-a"], SONNET: ["google-vertex"]}, {}, {SONNET: ["anthropic"]}))
        self.assertEqual(said, [f"--served-by {SONNET}=google-vertex replaces the benchmark's pin anthropic: not as in the paper"])
        self.assertIs(r.other["comparable"], False)
        self.assertNotIn("comparable", make().other)
        job.write_config(tmp, job="j", bench=b, other=r.other, model="m-1", provider="openrouter", dataset="d", units=[],
                         concurrency=1, granted=True, served_by=r.serving, pins_replaced=r.pins_replaced)
        with open(os.path.join(tmp, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["pins_replaced"], {SONNET: ["anthropic"]})
        self.assertIn(f"{os.path.basename(tmp)}: --served-by replaced the benchmark's pin of {SONNET}",
                      prove.job_problems(b, {"id": "all"}, "nop", tmp, []))

    def test_the_recipe_gives_each_fields_pin_and_review_says_when_served_by_replaces_it(self):
        from tests.test_standard_recipe_served_by import a_recipe, defs, judged
        b = judged()
        d = defs(dict(b, score=dict(b["score"], judge=dict(b["score"]["judge"], pin={"openrouter": ["anthropic"]}))))
        paper = [STEP["model"], "Served by", "not as in the paper"]
        for served, words, row in ((None, "--served-by openai/gpt-4o-mini=anthropic", False), ({"judge": ["anthropic"]}, "--served-by openai/gpt-4o-mini=anthropic", False),
                                   ({"judge": ["google-vertex"]}, "--served-by openai/gpt-4o-mini=google-vertex", True)):
            with self.subTest(served=served):
                out = recipe.compile(a_recipe(**({"served_by": served} if served else {})), d)
                self.assertEqual((out["errors"], out["served_pins"]), ([], {"judge": ["anthropic"]}))
                self.assertIn(words, out["script"])
                self.assertEqual(paper in out["review"], row)
                self.assertIn(f"judge served by {(served or {}).get('judge', ['anthropic'])[0]}", next(r[2] for r in out["review"] if r[1] == "Model"))
        self.assertEqual(recipe.compile(a_recipe(), defs(judged()))["served_pins"], {})
        both = recipe.compile(a_recipe(others=["fake", "nop"]), d)
        self.assertTrue(all("--served-by openai/gpt-4o-mini=anthropic" in p["script"] for p in both["parts"]))
        out = recipe.compile(a_recipe(model="openai/gpt-4o-mini", served_by={"model": ["google-vertex"]}), d)
        self.assertEqual((out["errors"], out["served_pins"]), ([], {"model": ["anthropic"], "judge": ["anthropic"]}))
        self.assertIn([STEP["model"], "Served by", "not as in the paper"], out["review"])
        self.assertIn("model served by google-vertex · judge served by google-vertex", next(r[2] for r in out["review"] if r[1] == "Model"))


class Wired(unittest.TestCase):
    """A unit of a run whose judge the definition pins and whose own model the user pins (``--served-by``)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, install, "HOME", install.HOME)
        install.HOME = os.path.join(self.tmp, "home")
        b = definitions.get("benchmark", "fakebench", ROOT)
        b = dict(b, score=dict(b["score"], judge={"model": SONNET, "says": "a verdict", "pin": {"openrouter": ["anthropic"]}}))
        self.r = run.Run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="openrouter", env={"OPENROUTER_API_KEY": "k"},
                         unsandboxed=True, jobs_root=self.tmp, say=lambda s: None, served_by={"m-1": ["prov-a"]})

    def ended(self, routes):
        rr = mock.Mock(mcp_listeners=[], unit_image="", dir=self.tmp)
        rr.calls.side_effect = lambda tag: routes.get(tag, [])
        driver = mock.Mock()
        driver.request.side_effect = lambda op, **kw: {} if op == "setup" else {"reward": 0}
        with contextlib.ExitStack() as stack:
            for name in ("phase", "said_unit"):
                stack.enter_context(mock.patch.object(self.r, name))
            self.noted = stack.enter_context(mock.patch.object(self.r, "note_refusal"))
            stack.enter_context(mock.patch.object(self.r, "driver", return_value=driver))
            stack.enter_context(mock.patch.object(self.r, "unit_relay", return_value=rr))
            stack.enter_context(mock.patch.object(self.r, "drive_agent", return_value=("answer", None, {})))
            stack.enter_context(mock.patch.object(run.subprocess, "run"))
            stack.enter_context(mock.patch.object(run.progress, "ECHO", None))
            self.res = self.r.unit(0, "good")
        return (self.res.get("exception_info") or {}).get("exception_type")

    def test_only_a_route_the_definition_pins_fails_the_unit_as_its_route(self):
        ok, no = {"model": "m-1", "status": 200}, {"status": 404, "error": "HTTP 404: no endpoint"}
        self.assertNotIn(self.ended({"u0000": [dict(no, model="m-1")]}), (None, "ApiProviderRouteError"))
        self.assertIsNone(self.ended({"u0000": [dict(ok, reply={"served_by": "Parasail"})]}))
        self.assertEqual(self.ended({"u0000": [ok], "u0000-judge": [dict(no, model=SONNET)]}), "ApiProviderRouteError")
        self.assertTrue(self.noted.call_args[0][0][1].startswith("404: "))
        self.assertIs(self.res["exception_info"]["retryable"], False)
        self.assertEqual(self.ended({"u0000": [dict(no, model=SONNET)]}), "ApiProviderRouteError")
        self.assertEqual(self.ended({"u0000": [ok], "u0000-judge": [{"model": SONNET, "status": 200, "reply": {"served_by": "Parasail"}}]}),
                         "ApiProviderRouteError")

    def test_three_units_in_a_row_with_no_endpoint_on_a_pinned_route_stop_the_run(self):
        self.r._halt, self.r._refused, self.r.halted = threading.Event(), [], None
        for _ in range(3):
            self.r.note_refusal(("ApiProviderRouteError", "404: no endpoint"))
        self.assertTrue(self.r._halt.is_set())


if __name__ == "__main__":
    unittest.main()
