"""The relay passes replies through unchanged, streamed or not, records each
call by its unit, and names a unit's failure the way the classifier reads it.
A driver or capability process answers one JSON line per request."""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
from unittest import mock
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.runs import classify
from nullius.standard.calls import ledger, relay
from nullius.standard.protocols import jsonl
from nullius.standard.runner import job

CHAT = {"choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}}
CHAT_SSE = (b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
            b'data: [DONE]\n\n')
RESP_SSE = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"output":['
            b'{"type":"function_call","name":"shell","arguments":"{\\"cmd\\":\\"ls\\"}"},'
            b'{"type":"message","content":[{"type":"output_text","text":"hi"}]}],'
            b'"usage":{"input_tokens":20,"output_tokens":5,"input_tokens_details":{"cached_tokens":10}}}}\n\n')

RESP_FAILED = json.dumps({"object": "response", "id": "gen-1", "status": "failed", "output": [], "usage": None,
                          "error": {"code": "server_error", "message": "the request said x"}}).encode()
RESP_FAILED_SSE = (b'event: response.failed\ndata: {"type":"response.failed","response":{"status":"failed","output":[],'
                   b'"error":{"code":"server_error","message":"the request said x"}}}\n\n')
RESP_INCOMPLETE = json.dumps({"object": "response", "status": "incomplete", "output": [],
                              "incomplete_details": {"reason": "max_output_tokens"},
                              "usage": {"input_tokens": 30, "output_tokens": 8}}).encode()

HOLLOW = {"hollow": json.dumps({"id": "gen-1", "provider": "Minimax", "model": "m"}).encode(),
          "hollow-error": json.dumps({"id": "gen-1", "error": {"code": 502, "message": "the request said x"}}).encode(),
          "hollow-429": json.dumps({"id": "gen-1", "error": {"code": 429, "message": "the request said x"}}).encode(),
          "hollow-402": json.dumps({"id": "gen-1", "error": {"code": 402, "message": "the request said x"}}).encode(),
          "hollow-404": json.dumps({"id": "gen-1", "error": {"code": 404, "message": "the request said x"}}).encode(),
          "count": json.dumps({"input_tokens": 12}).encode(), "no-choice": json.dumps({"choices": []}).encode(),
          "not-json": b"<html>the request said x</html>"}


class Upstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.seen.append((self.path, self.headers.get("Authorization"), body))
        mode = body.get("mode")
        if mode == "429":
            self.reply(429, b'{"error":{"message":"Rate limit reached"}}', "application/json")
        elif mode == "401":
            self.reply(401, b'{"error":{"message":"Incorrect API key"}}', "application/json")
        elif mode == "no-usage":
            self.reply(200, b'{"choices":[{"message":{"role":"assistant","content":"hello"}}]}', "application/json")
        elif mode in ("400", "413", "422"):
            self.reply(int(mode), b'{"error":{"message":"could not process image"}}', "application/json")
        elif mode in ("cut", "short"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header(*(("Transfer-Encoding", "chunked") if mode == "cut" else ("Content-Length", str(len(CHAT_SSE) + 9))))
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(CHAT_SSE), CHAT_SSE) if mode == "cut" else CHAT_SSE)
        elif mode in HOLLOW:
            self.reply(200, HOLLOW[mode], "application/json", *[("X-Generation-Id", "gen-1")] * (mode == "not-json"),
                       ("Retry-After", "30"))
        elif self.path.endswith("/responses") and mode == "failed":
            self.reply(200, RESP_FAILED, "application/json")
        elif self.path.endswith("/responses") and mode == "failed-sse":
            self.reply(200, RESP_FAILED_SSE, "text/event-stream")
        elif self.path.endswith("/responses") and mode == "incomplete":
            self.reply(200, RESP_INCOMPLETE, "application/json")
        elif self.path.endswith("/responses"):
            self.reply(200, RESP_SSE, "text/event-stream")
        elif body.get("stream"):
            self.reply(200, CHAT_SSE, "text/event-stream")
        else:
            self.reply(200, json.dumps(CHAT).encode(), "application/json")

    def reply(self, status, body, kind, *headers):
        try:
            self.send_response(status)
            for header in (("Content-Type", kind),) + headers:
                self.send_header(*header)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass                                 # the relay hung up first: a test of a reply that never came


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.relay = relay.Relay(f"http://127.0.0.1:{cls.up.server_address[1]}/v1", "sk-real", cls.tmp,
                                prices={"m": {"input": 1.0, "cached": 0.1, "output": 10.0}}).start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.up.shutdown()

    def post(self, tag, path, body):
        req = urllib.request.Request(self.relay.url(tag) + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.PLACEHOLDER_KEY})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            self.retry_after = e.headers.get("Retry-After")
            return e.code, e.read()

    def test_a_units_agent_is_refused_past_its_cap_on_model_calls(self):
        """``max_calls`` counts the answered calls of a unit's agent route alone: a refused
        call (a 429) is not one, its judge's and simulated user's never are, and past the cap
        the next call is the relay's own 402, which the unit ends with as the agent's."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = relay.Relay(f"http://127.0.0.1:{self.up.server_address[1]}/v1", "sk-real", tmp, max_calls=2).start()
        self.addCleanup(r.stop)

        def post(tag, body):
            req = urllib.request.Request(r.url(tag) + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as got:
                    return got.status, got.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        ask = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(post("u0007", dict(ask, mode="429"))[0], 429)          # refused upstream: not counted
        self.assertEqual([post("u0007", ask)[0] for _ in range(2)], [200, 200])
        for side in ("u0007-judge", "u0007-simulator"):                          # never the agent's
            self.assertEqual(post(side, ask)[0], 200)
        status, body = post("u0007", ask)
        self.assertEqual(status, 402)
        self.assertEqual(json.loads(body)["error"]["code"], "unit_call_cap")
        self.assertEqual(post("u0008", ask)[0], 200)                             # another unit counts its own
        kind, said = relay.failure(r.calls("u0007"))
        self.assertEqual(kind, relay.CALLS_ERROR)
        self.assertIn("cap of 2 model calls", said)
        self.assertEqual(classify.from_verdict(kind, None), classify.AGENT_ERROR)

    def test_plain_reply_passes_through_and_the_real_key_goes_out(self):
        status, body = self.post("u1", "/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((status, json.loads(body)), (200, CHAT))
        path, auth, _ = Upstream.seen[-1]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer sk-real"))
        rec = self.relay.calls("u1")[-1]
        self.assertEqual(rec["reply"]["text"], "hello")
        self.assertEqual(rec["reply"]["usage"], {"input": 10, "output": 2, "cached": 4, "reasoning": 0})
        self.assertAlmostEqual(rec["cost_usd"], (6 * 1.0 + 4 * 0.1 + 2 * 10.0) / 1e6)
        self.assertEqual(rec["cost_from"], "estimate")

    def test_a_model_is_priced_by_exactly_its_name(self):
        """``m`` has a price; a model whose name only starts with it is another model, and has no cost."""
        status, _ = self.post("u1x", "/chat/completions", {"model": "m-large", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        rec = self.relay.calls("u1x")[-1]
        self.assertEqual(rec["model"], "m-large")
        self.assertNotIn("cost_usd", rec)
        self.assertNotIn("cost_from", rec)

    def test_streamed_chat_is_passed_byte_for_byte_and_read_whole(self):
        status, body = self.post("u2", "/chat/completions", {"model": "m", "stream": True, "messages": []})
        self.assertEqual(body, CHAT_SSE)
        self.assertTrue(Upstream.seen[-1][2]["stream_options"]["include_usage"])   # asked so the stream says its tokens
        rec = self.relay.calls("u2")[-1]
        self.assertEqual(rec["reply"]["text"], "hello")
        self.assertEqual(rec["reply"]["usage"]["input"], 7)

    def test_a_stream_cut_off_after_its_headers_is_a_failed_call(self):
        for mode in ("cut", "short"):
            self.post("u" + mode, "/chat/completions", {"model": "m", "stream": True, "mode": mode, "messages": []})
            rec = self.relay.calls("u" + mode)[-1]
            self.assertEqual(rec["status"], 502)
            self.assertIn("cut off", rec["error"])
            self.assertEqual(rec["reply"]["usage"]["input"], 7)
            self.assertEqual(job._usage_total([rec])["cost"], rec["cost_usd"])

    def test_a_refused_call_keeps_each_inline_image_by_its_type_wholeness_and_size_alone(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20 + b"IEND\xaeB`\x82\n").decode()
        cut = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20).decode()
        jpeg = base64.b64encode(b"\xff\xd8\xff" + b"y" * 9 + b"\xff\xd9tail").decode()
        webp = base64.b64encode(b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"z" * 4 + b"\n").decode()
        image = lambda kind, data: {"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}}
        content = [image("image/png", png), image("image/png", cut), image("image/gif", "not base64!"),
                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg}"}},
                   {"type": "input_image", "image_url": f"data:image/webp;base64,{webp}"},
                   {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}, {"type": "text", "text": "grade"}]
        sent = [{"type": "image/png", "is": "image/png", "bytes": len(png)}, {"type": "image/png", "is": None, "bytes": len(cut)},
                {"type": "image/gif", "is": None, "bytes": 11}, {"type": "image/jpeg", "is": "image/jpeg", "bytes": len(jpeg)},
                {"type": "image/webp", "is": "image/webp", "bytes": len(webp)}]
        for mode in ("400", "413", "422", "429", None):
            self.post("img", "/messages", {"model": "m", "mode": mode, "messages": [{"role": "user", "content": content}]})
            rec = self.relay.calls("img")[-1]
            with self.subTest(mode):
                self.assertEqual(rec.get("images"), sent if mode in ("400", "413", "422") else None)
                self.assertFalse(any(data in json.dumps(rec) for data in (png, cut, jpeg, webp)))

    def test_a_200_to_a_model_call_with_no_reply_is_a_502_said_by_its_kind_alone(self):
        for mode, path, code, said, kind in (
                ("hollow", "/messages", 502, "no reply", "NetworkConnectionError"),
                ("hollow", "/chat/completions", 502, "no reply", "NetworkConnectionError"),
                ("hollow", "/responses", 502, "no reply", "NetworkConnectionError"),
                ("hollow-error", "/messages", 502, "an error 502", "NetworkConnectionError"),
                ("hollow-429", "/messages", 429, "an error 429", "RateLimitError"),
                ("hollow-404", "/completions", 502, "an error 404", "NetworkConnectionError"),
                ("hollow-402", "/messages", 402, "an error 402", "ApiUsageLimitError"),
                ("not-json", "/messages", 502, "no reply", "NetworkConnectionError")):
            tag = f"uh-{mode}{path.replace('/', '-')}"
            status, body = self.post(tag, path, {"model": "m", "mode": mode, "messages": []})
            rec = self.relay.calls(tag)[-1]
            self.assertEqual(self.retry_after, "30")
            with self.subTest(mode=mode, path=path):
                self.assertEqual((status, rec["status"]), (code, code))
                self.assertEqual(json.loads(body)["error"]["message"], f"the endpoint answered 200 with {said}")
                self.assertEqual(rec["error"], f"HTTP {code}: the endpoint answered 200 with {said}")
                self.assertEqual(rec["answered"], 200)
                self.assertFalse({"reply", "cost_usd"} & set(rec))
                self.assertNotIn("the request said", json.dumps(rec))
                self.assertEqual(relay.failure([rec], agent=True)[0], kind)
        self.assertEqual(self.post("uk-asked-stream", "/messages", {"model": "m", "mode": "hollow", "stream": True,
                                                                     "messages": []}), (200, HOLLOW["hollow"]))
        for mode, path in (("count", "/messages/count_tokens"), ("no-choice", "/chat/completions"), ("hollow", "/models"),
                           ("hollow", "/threads/t1/messages")):
            tag = f"uk-{mode}{path.replace('/', '-')}"
            status, body = self.post(tag, path, {"model": "m", "mode": mode, "messages": []})
            with self.subTest(path):
                self.assertEqual((status, body), (200, HOLLOW[mode]))
                self.assertEqual(self.relay.calls(tag)[-1]["status"], 200)
                self.assertNotIn("answered", self.relay.calls(tag)[-1])

    def test_streamed_responses_read_from_the_completed_event(self):
        status, body = self.post("u3", "/responses", {"model": "m", "stream": True, "input": "x"})
        self.assertEqual(body, RESP_SSE)
        rec = self.relay.calls("u3")[-1]
        self.assertEqual(rec["reply"]["text"], "hi")
        self.assertEqual(rec["reply"]["tool_calls"][0]["name"], "shell")
        self.assertEqual(rec["reply"]["usage"]["cached"], 10)

    def test_a_failed_response_with_no_usage_costs_nothing(self):
        for tag, mode in (("uf1", "failed"), ("uf2", "failed-sse")):
            status, _ = self.post(tag, "/responses", {"model": "m", "mode": mode, "input": "x", "stream": mode == "failed-sse"})
            self.assertEqual(status, 200)
            rec = self.relay.calls(tag)[-1]
            self.assertEqual((rec["reply"]["end"], rec["reply"]["error"]["code"]), ("failed", "server_error"))
            self.assertEqual((rec["cost_usd"], rec["cost_from"]), (0.0, "failed"))
        failed = {"status": 200, "cost_usd": 0.0, "cost_from": "failed"}
        self.assertEqual(job._usage_total([failed, {"status": 200}])["cost"], None)
        self.assertEqual({k: job._usage_total([failed, {"status": 200, "cost_usd": 0.5, "cost_from": "provider"}])[k]
                          for k in ("cost", "cost_from")}, {"cost": 0.5, "cost_from": "provider"})

    def test_an_incomplete_response_is_priced_from_its_usage(self):
        self.post("uf3", "/responses", {"model": "m", "mode": "incomplete", "input": "x"})
        rec = self.relay.calls("uf3")[-1]
        self.assertTrue(rec["reply"]["end"].startswith("incomplete"))
        self.assertEqual(rec["cost_from"], "estimate")
        self.assertGreater(rec["cost_usd"], 0)

    def test_calls_are_recorded_by_their_unit(self):
        self.post("ua", "/chat/completions", {"model": "m", "messages": []})
        self.post("ub", "/chat/completions", {"model": "m", "messages": []})
        self.assertEqual(len(self.relay.calls("ua")), 1)
        self.assertEqual(len(self.relay.calls("ub")), 1)

    def test_a_unit_that_ended_on_429_is_rate_limited(self):
        status, _ = self.post("u4", "/chat/completions", {"model": "m", "mode": "429"})
        self.assertEqual(status, 429)
        exc, said = relay.failure(self.relay.calls("u4"))
        self.assertEqual(classify.from_verdict(exc, None), classify.RATE_LIMITED)
        self.assertIn("Rate limit", said)

    def test_a_refused_key_is_infrastructure(self):
        self.post("u5", "/chat/completions", {"model": "m", "mode": "401"})
        exc, _ = relay.failure(self.relay.calls("u5"))
        self.assertEqual(classify.from_verdict(exc, None), classify.API_ERROR)

    def test_a_failure_the_agent_recovered_from_is_not_the_units(self):
        self.post("u6", "/chat/completions", {"model": "m", "mode": "429"})
        self.post("u6", "/chat/completions", {"model": "m", "messages": []})
        self.assertIsNone(relay.failure(self.relay.calls("u6")))

    def test_the_run_model_replaces_the_agents(self):
        r = relay.Relay(self.relay.base, "k", tempfile.mkdtemp(), model="the-recipes").start()
        try:
            req = urllib.request.Request(r.url("x") + "/chat/completions", data=b'{"model":"agents-own"}',
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            self.assertEqual(Upstream.seen[-1][2]["model"], "the-recipes")
            self.assertEqual(r.calls("x")[-1]["model_asked"], "agents-own")
        finally:
            r.stop()

    def test_a_judge_route_keeps_the_judges_model(self):
        r = relay.Relay(self.relay.base, "k", tempfile.mkdtemp(), model="the-recipes").start()
        try:
            for tag, model in (("u1-judge", "the-judge"), ("u1", None)):
                req = urllib.request.Request(r.url(tag, model=model) + "/chat/completions", data=b'{"model":"asked"}',
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req).read()
                self.assertEqual(Upstream.seen[-1][2]["model"], model or "the-recipes")
        finally:
            r.stop()

    def test_bad_tag_is_refused(self):
        with self.assertRaises(ValueError):
            self.relay.url("../x")

    def test_an_advertised_address_is_what_url_hands_out(self):
        r = relay.Relay(self.relay.base, "k", tempfile.mkdtemp())
        r.public = "http://relay:8080"
        r.start(host="127.0.0.1", port=0)
        try:
            self.assertEqual(r.url("u7"), "http://relay:8080/u7/v1")
        finally:
            r.stop()


class CapTest(unittest.TestCase):
    """A relay of a run with a spending cap: a call is booked before it goes
    out and is not sent when it does not fit - the one answer that is the
    relay's own, and marked so."""
    PRICES = {"m": {"input": 1.0, "cached": 0.1, "output": 10.0}}

    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def capped(self, cap):
        tmp = tempfile.mkdtemp()
        led = ledger.Ledger.create(os.path.join(tmp, "ledger", "ledger.json"), cap)
        r = relay.Relay(self.base, "sk-real", os.path.join(tmp, "relay"), prices=self.PRICES, ledger=led.path).start()
        self.addCleanup(r.stop)
        return r, led

    def post(self, r, tag, body):
        req = urllib.request.Request(r.url(tag) + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as got:
                return got.status, dict(got.headers.items()), got.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items()), e.read()

    def test_a_call_that_fits_is_sent_and_its_booking_becomes_what_it_cost(self):
        r, led = self.capped(1.0)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        status, _, _ = self.post(r, "u1", body)
        self.assertEqual(status, 200)
        rec = r.calls("u1")[-1]
        self.assertAlmostEqual(rec["booked_usd"], ledger.bound(body, json.dumps(body).encode(), self.PRICES["m"]))
        got = led.state()
        self.assertAlmostEqual(got["spent_usd"], rec["cost_usd"])
        self.assertEqual((got["held_usd"], got["unsettled_usd"], got["closed"]), (0, 0.0, False))

    def test_a_stream_cut_off_or_a_failed_response_with_no_usage_keeps_its_whole_booking(self):
        r, led = self.capped(1.0)
        for tag, path, body in (("uc", "/chat/completions", {"model": "m", "stream": True, "mode": "cut", "messages": []}),
                                ("uf", "/responses", {"model": "m", "mode": "failed", "input": "x"})):
            req = urllib.request.Request(r.url(tag) + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req).read()
            except (http.client.IncompleteRead, urllib.error.HTTPError):
                pass
        (cut,), (failed,) = r.calls("uc"), r.calls("uf")
        self.assertEqual((cut["status"], failed["cost_from"]), (502, "failed"))
        self.assertIsNotNone(cut.get("cost_usd"))
        got = led.state()
        self.assertEqual((got["spent_usd"], got["held_usd"]), (0.0, 0))
        self.assertAlmostEqual(got["unsettled_usd"], cut["booked_usd"] + failed["booked_usd"])
        self.assertNotIn("m", led._read()["seen"])

    def test_a_stream_cut_off_that_already_cost_more_than_its_booking_counts_what_it_cost(self):
        r, led = self.capped(1.0)
        body = {"model": "m", "stream": True, "mode": "cut", "messages": []}
        req = urllib.request.Request(r.url("uc") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with mock.patch.object(ledger, "bound", lambda *a, **k: 1e-9):
            try:
                urllib.request.urlopen(req).read()
            except (http.client.IncompleteRead, urllib.error.HTTPError):
                pass
        (cut,) = r.calls("uc")
        got = led.state()
        self.assertEqual((cut["status"], got["unsettled_usd"]), (502, 0.0))
        self.assertGreater(cut["cost_usd"], cut["booked_usd"])
        self.assertAlmostEqual(got["spent_usd"], cut["cost_usd"])
        self.assertNotIn("m", led._read()["seen"])

    def test_a_unit_asked_to_stop_books_nothing_and_a_200_with_no_reply_keeps_its_whole_booking(self):
        r, led = self.capped(1.0)
        with open(os.path.join(r.dir, relay.STOP), "w") as f:
            f.write("batch cap\n")
        self.assertEqual(self.post(r, "u1", {"model": "m", "messages": []})[0], 400)
        got = led.state()
        self.assertEqual((got["spent_usd"], got["held_usd"], got["unsettled_usd"]), (0.0, 0, 0.0))
        os.unlink(os.path.join(r.dir, relay.STOP))
        self.assertEqual(self.post(r, "u1", {"model": "m", "mode": "hollow", "messages": []})[0], 502)
        (call,) = r.calls("u1")
        got = led.state()
        self.assertEqual((call["answered"], got["spent_usd"], got["held_usd"]), (200, 0.0, 0))
        self.assertAlmostEqual(got["unsettled_usd"], call["booked_usd"])

    def test_a_call_is_booked_at_what_the_runs_replies_have_been_once_one_came_back(self):
        """Fifteen calls at the model's limit would fill a cap nothing was spent of: after the first reply
        (2 output tokens here) a call that names no limit is booked those, at the same price."""
        r, led = self.capped(1.0)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        raw = json.dumps(body).encode()
        self.assertEqual(self.post(r, "u1", body)[0], 200)
        first = r.calls("u1")[-1]
        self.assertAlmostEqual(first["booked_usd"], ledger.bound(body, raw, self.PRICES["m"]))            # no reply yet: the start
        self.assertEqual(self.post(r, "u2", body)[0], 200)
        second = r.calls("u2")[-1]
        self.assertAlmostEqual(second["booked_usd"], ledger.bound(body, raw, self.PRICES["m"], seen=first["reply"]["usage"]["output"]))
        self.assertLess(second["booked_usd"], first["booked_usd"])

    def test_a_units_own_cap_refuses_its_calls_and_its_simulated_users_not_its_judges_nor_another_units(self):
        tmp = tempfile.mkdtemp()
        led = ledger.Ledger.create(os.path.join(tmp, "ledger", "ledger.json"), None, 0.0005)
        r = relay.Relay(self.base, "sk-real", os.path.join(tmp, "relay"), prices=self.PRICES, ledger=led.path).start()
        self.addCleanup(r.stop)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        big = {"model": "m", "messages": [{"role": "user", "content": "x" * 3000}], "max_tokens": 100}    # a thousand tokens of request: 0.001, over 0.0005
        sent = len(Upstream.seen)
        status, headers, answer = self.post(r, "u1", big)
        self.assertEqual((status, headers.get("X-Nullius-Refused"), json.loads(answer)["error"]["code"]), (402, "unit", "unit_spending_cap"))
        self.assertEqual(len(Upstream.seen), sent)                                 # never sent
        self.assertEqual(r.calls("u1")[-1]["refused"], "unit")
        exc, why = relay.failure(r.calls("u1"))
        self.assertEqual(exc, ledger.UNIT_ERROR)
        self.assertIn("this unit's spending cap of 0.0005 USD", why)
        self.assertEqual(self.post(r, "u1-judge", body)[0], 200)                   # the scorer is no part of the unit's task
        self.assertEqual(self.post(r, "u1-simulator", body)[0], 402)               # its simulated user is
        self.assertEqual(self.post(r, "u2", body)[0], 200)                         # another unit: booked what a reply has been, within its own cap
        self.assertFalse(led.closed())
        # the benchmark's own relay books for no unit: only the run's cap holds its calls
        shared = relay.Relay(self.base, "sk-real", os.path.join(tmp, "shared"), prices=self.PRICES, ledger=led.path, unit_caps=False).start()
        self.addCleanup(shared.stop)
        self.assertEqual(self.post(shared, "u1", body)[0], 200)

    def test_only_a_call_a_model_answers_is_booked_and_a_missing_price_is_no_cap_reached(self):
        r, led = self.capped(1.0)
        # a token count, and a body that names no model, cost nothing and report no usage: never booked, so never
        # kept against the cap as a call whose cost was not known
        for path, body in (("/messages/count_tokens", {"model": "m", "messages": [{"role": "user", "content": "x" * 6000}]}),
                           ("/responses/abc/cancel", {})):
            req = urllib.request.Request(r.url("u1") + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req).read()
            except urllib.error.HTTPError as e:
                e.read()
        self.assertEqual(len(r.calls("u1")), 2)
        self.assertFalse(any("booked_usd" in c or c.get("refused") for c in r.calls("u1")))
        self.assertEqual((led.state()["held_usd"], led.state()["unsettled_usd"], led.state()["spent_usd"]), (0, 0.0, 0.0))
        # a model with no price is not sent, and that is no cap reached: the unit is not the cap's, and the run goes on
        sent = len(Upstream.seen)
        status, headers, answer = self.post(r, "u2", {"model": "another", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((status, headers.get("X-Nullius-Refused"), json.loads(answer)["error"]["code"]), (402, "price", "unpriced_model"))
        self.assertIn("no price for the model 'another'", json.loads(answer)["error"]["message"])
        self.assertEqual(len(Upstream.seen), sent)
        self.assertEqual(r.calls("u2")[-1]["refused"], "price")
        self.assertEqual(relay.failure(r.calls("u2"))[0], "ApiError")
        self.assertFalse(led.closed())

    def test_a_call_waits_for_the_calls_in_flight_and_is_refused_only_when_they_do_not_settle(self):
        r, led = self.capped(0.0015)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}     # booked a little over 0.001
        held, _ = led.reserve(0.001)                                               # another call, in flight
        got = {}
        waiting = threading.Thread(target=lambda: got.update(status=self.post(r, "u1", body)[0]))
        waiting.start()
        time.sleep(0.6)
        self.assertEqual((got, led.closed()), ({}, False))                         # not refused, not sent: waiting
        led.settle(held, 0.0001)                                                   # the call in flight comes back, far under its booking
        waiting.join(10)
        self.assertEqual((got.get("status"), led.closed()), (200, False))
        # one that waits in vain is refused, and the run stops there
        stuck, _ = led.reserve(led.state()["left_usd"] - 0.00001)                 # a call in flight that never comes back, leaving next to nothing
        with mock.patch.object(ledger, "WAIT", 0.5):
            status, headers, _ = self.post(r, "u2", body)
        self.assertEqual((status, headers.get("X-Nullius-Refused"), led.closed()), (402, "cap", True))
        # once the cap is reached, a call with nothing to price is not sent either
        req = urllib.request.Request(r.url("u3") + "/responses/abc/cancel", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req)
        self.assertEqual((refused.exception.code, refused.exception.headers.get("X-Nullius-Refused")), (402, "cap"))
        refused.exception.read()

    def test_a_call_that_names_a_model_is_booked_on_any_route_and_a_ledger_that_fails_is_no_cap_reached(self):
        r, led = self.capped(1.0)
        req = urllib.request.Request(r.url("u1") + "/images/generations", data=json.dumps({"model": "m", "prompt": "a cat"}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req).read()
        except urllib.error.HTTPError as e:
            e.read()
        self.assertIn("booked_usd", r.calls("u1")[-1])                             # a route the relay does not know is booked all the same
        os.unlink(led.path)                                                        # the ledger gone: the call is not sent, and that is no cap
        status, headers, answer = self.post(r, "u2", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((status, headers.get("X-Nullius-Refused"), json.loads(answer)["error"]["code"]), (402, "ledger", "ledger_unavailable"))
        self.assertEqual(relay.failure(r.calls("u2"))[0], "ApiError")
        # a call with nothing to price the same way: not sent, and not the cap's
        req = urllib.request.Request(r.url("u3") + "/responses/abc/cancel", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req)
        self.assertEqual((refused.exception.code, refused.exception.headers.get("X-Nullius-Refused")), (402, "ledger"))
        refused.exception.read()
        self.assertEqual(relay.failure(r.calls("u3"))[0], "ApiError")

    def test_a_call_that_does_not_fit_is_answered_by_the_relay_and_never_sent(self):
        r, led = self.capped(0.0005)             # a thousand tokens of request at 1 USD a million are 0.001 already
        sent = len(Upstream.seen)
        status, headers, body = self.post(r, "u1", {"model": "m", "messages": [{"role": "user", "content": "x" * 3000}], "max_tokens": 100})
        self.assertEqual((status, headers.get("X-Nullius-Refused")), (402, "cap"))
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "spending_cap")
        self.assertIn("nullius relay: the run's spending cap of 0.0005 USD", error["message"])
        rec = r.calls("u1")[-1]
        self.assertEqual((rec["status"], rec["refused"]), (402, "cap"))
        self.assertNotIn("reply", rec)
        self.assertTrue(led.closed())
        # a call small enough to fit is refused too: a run stops at one point
        status, _, _ = self.post(r, "u1", {"model": "m", "messages": [], "max_tokens": 1})
        self.assertEqual(status, 402)
        self.assertEqual(len(Upstream.seen), sent)                       # neither reached the provider
        self.assertEqual(led.state()["spent_usd"], 0.0)
        exc, said = relay.failure(r.calls("u1"))
        self.assertEqual(exc, ledger.ERROR)
        self.assertIn("spending cap", said)

    def test_a_cap_under_one_guessed_call_lets_the_first_out_and_books_the_next_from_its_reply(self):
        """100 output tokens at 10 USD a million are 0.001, over this cap of 0.0005: before any reply that is a
        guess, and refusing on it refused every call of the run with nothing spent."""
        r, led = self.capped(0.0005)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        self.assertEqual(self.post(r, "u1", body)[0], 200)
        first = r.calls("u1")[-1]
        self.assertAlmostEqual(first["booked_usd"], 0.0005)                        # the room there was
        self.assertEqual(self.post(r, "u2", body)[0], 200)
        self.assertLess(r.calls("u2")[-1]["booked_usd"], 0.0001)                   # what a reply has been
        self.assertFalse(led.closed())

    def test_a_call_that_never_left_gives_its_booking_back_and_one_cut_off_after_it_was_sent_keeps_it(self):
        r, led = self.capped(1.0)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        def a_connection(**failing):
            conn = http.client.HTTPConnection(*self.up.server_address[:2], timeout=5)
            for step, error in failing.items():
                setattr(conn, step, mock.Mock(side_effect=error))
            return conn, False

        for failing in (TimeoutError("timed out"), OSError(113, "No route to host"), ssl.SSLCertVerificationError("certificate verify failed")):
            with mock.patch.object(r.pool, "take", side_effect=lambda fresh=False: a_connection(connect=failing)):
                self.assertEqual(self.post(r, "u1", body)[0], 502)
            got = led.state()
            self.assertEqual((got["held_usd"], got["unsettled_usd"], got["spent_usd"]), (0, 0.0, 0.0), failing)   # never sent: nothing kept
        with mock.patch.object(r.pool, "take", side_effect=lambda fresh=False: a_connection(getresponse=TimeoutError("timed out"))):
            self.assertEqual(self.post(r, "u1", body)[0], 502)
        got = led.state()
        self.assertEqual(got["held_usd"], 0)
        self.assertAlmostEqual(got["unsettled_usd"], r.calls("u1")[-1]["booked_usd"])       # sent, then silence: it may have been billed

    def test_a_waiting_call_whose_caller_hung_up_is_dropped_and_closes_nothing(self):
        r, led = self.capped(0.0015)
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}).encode()
        self.assertEqual(self.post(r, "u0", json.loads(body))[0], 200)             # a reply, so the next is booked from it
        held, _ = led.reserve(led.state()["left_usd"] - 0.000001)                  # a call in flight, leaving next to nothing
        sent = len(Upstream.seen)
        host, port = r.server.server_address[:2]
        caller = socket.create_connection((host, port))
        caller.sendall(b"POST /u1/v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        time.sleep(0.5)
        self.assertEqual(len(Upstream.seen), sent)                                 # waiting
        caller.close()                                                             # its agent gave up
        time.sleep(0.6)
        led.settle(held, 0.0)                                                      # room now: an orphan would be booked and sent here
        time.sleep(0.6)
        got = led.state()
        self.assertEqual((len(Upstream.seen), got["held_usd"], got["closed"], r.calls("u1")), (sent, 0, False, []))
        # a relay that stops lets its waiting calls go the same way
        stuck, _ = led.reserve(led.state()["left_usd"] - 0.000001)
        answered = {}

        def call():
            try:
                answered.update(status=self.post(r, "u2", json.loads(body))[0])
            except OSError as e:                                                   # let go without an answer
                answered.update(dropped=type(e).__name__)

        waiting = threading.Thread(target=call, daemon=True)
        waiting.start()
        time.sleep(0.5)
        r.stopping.set()
        waiting.join(5)
        self.assertEqual((answered, len(Upstream.seen), led.closed()), ({"dropped": "RemoteDisconnected"}, sent, False))

    def test_a_refused_call_is_the_units_whatever_its_agent_did_next(self):
        calls = [{"status": 200}, {"status": 402, "refused": "cap", "error": "the run's spending cap"}, {"status": 200}]
        self.assertEqual(relay.failure(calls), (ledger.ERROR, "the run's spending cap"))

    def test_an_error_gives_the_booking_back_and_a_reply_without_tokens_keeps_it(self):
        r, led = self.capped(1.0)
        status, _, _ = self.post(r, "u1", {"model": "m", "mode": "429", "max_tokens": 100})
        self.assertEqual(status, 429)                                     # the provider's own, passed on as ever
        got = led.state()
        self.assertEqual((got["spent_usd"], got["unsettled_usd"], got["held_usd"]), (0.0, 0.0, 0))
        status, _, _ = self.post(r, "u1", {"model": "m", "mode": "no-usage", "max_tokens": 100})
        self.assertEqual(status, 200)
        rec = r.calls("u1")[-1]
        self.assertNotIn("cost_usd", rec)
        got = led.state()
        self.assertEqual((got["spent_usd"], got["held_usd"]), (0.0, 0))
        self.assertAlmostEqual(got["unsettled_usd"], rec["booked_usd"])     # it may have been billed


class EffortTest(unittest.TestCase):
    def test_the_record_says_the_reasoning_effort_a_request_asked_for(self):
        # chat's reasoning_effort, the Responses API's reasoning.effort, and none at all
        self.assertEqual(relay._request_tail({"messages": [], "reasoning_effort": "low"})["reasoning_effort"], "low")
        self.assertEqual(relay._request_tail({"input": [], "reasoning": {"effort": "high"}})["reasoning_effort"], "high")
        self.assertIsNone(relay._request_tail({"messages": []})["reasoning_effort"])

    def test_reasoning_is_read_as_reasoning_or_thinking_tokens(self):
        self.assertEqual(relay._usage({"completion_tokens_details": {"reasoning_tokens": 3}})["reasoning"], 3)
        self.assertEqual(relay._usage({"output_tokens_details": {"reasoning_tokens": 5}})["reasoning"], 5)
        self.assertEqual(relay._usage({"output_tokens": 9, "output_tokens_details": {"thinking_tokens": 7}})["reasoning"], 7)


class ServeTest(unittest.TestCase):
    """The relay as a process of its own: the key from its environment only,
    one line when it listens, a clean stop on SIGTERM."""

    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def serve(self, key, *extra):
        import subprocess
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "key"), "w") as f:
            f.write(key + "\n")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.app}
        return subprocess.Popen([sys.executable, "-m", "nullius.standard.calls.relay", "serve", "--listen", "127.0.0.1:0",
                                 "--base", f"http://127.0.0.1:{self.up.server_address[1]}/v1", "--record", d,
                                 "--key-file", os.path.join(d, "key"), "--model", "the-recipes", *extra],
                                env=env, stdout=subprocess.PIPE, text=True)

    def test_serve_relays_with_the_key_from_its_file_and_stops_on_sigterm(self):
        import signal
        p = self.serve("sk-from-file", "--route", "u1-judge=the-judge")
        try:
            where = json.loads(p.stdout.readline())["listening"]
            for tag, model in (("u1", "the-recipes"), ("u1-judge", "the-judge")):
                req = urllib.request.Request(f"{where}/{tag}/v1/chat/completions", data=b'{"model":"asked"}',
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req).read()
                self.assertEqual(Upstream.seen[-1][1], "Bearer sk-from-file")
                self.assertEqual(Upstream.seen[-1][2]["model"], model)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()

    def test_serve_books_its_calls_in_the_runs_ledger(self):
        """A docker unit's relay: the ledger is a file it is given, shared with every other relay of the job."""
        import signal
        led = ledger.Ledger.create(os.path.join(tempfile.mkdtemp(), "ledger", "ledger.json"), 1.0)
        p = self.serve("sk-from-env", "--ledger", led.path, "--prices", json.dumps({"the-recipes": {"input": 1.0, "output": 10.0}}))
        try:
            where = json.loads(p.stdout.readline())["listening"]
            req = urllib.request.Request(f"{where}/u1/v1/chat/completions", data=b'{"model":"asked","max_tokens":5}',
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            self.assertAlmostEqual(led.state()["spent_usd"], (10 * 1.0 + 2 * 10.0) / 1e6)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()

    def test_serve_without_its_key_file_refuses(self):
        import subprocess
        p = subprocess.Popen([sys.executable, "-m", "nullius.standard.calls.relay", "serve", "--base", "http://127.0.0.1:9/v1",
                              "--record", tempfile.mkdtemp(), "--key-file", "/nonexistent/key"],
                             env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.app}, stdout=subprocess.PIPE, text=True)
        self.assertIn("cannot read --key-file", p.stdout.readline())
        self.assertEqual(p.wait(timeout=10), 2)
        p.stdout.close()

    def test_serve_with_an_empty_key_file_refuses(self):
        p = self.serve("")
        self.assertIn("contains no key", p.stdout.readline())
        self.assertEqual(p.wait(timeout=10), 2)
        p.stdout.close()


MSG = {"type": "message", "role": "assistant",
       "content": [{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"},
                   {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}}],
       "usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}
MSG_SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5,'
           b'"cache_read_input_tokens":40,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n'
           b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
           b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t","name":"bash","input":{}}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":"}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"ls\\"}"}}\n\n'
           b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":9}}\n\n'
           b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


class KeptAlive(BaseHTTPRequestHandler):
    """An upstream that keeps its connections open, as a provider does, and
    says which of them each call came on."""
    protocol_version = "HTTP/1.1"
    seen, peers = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        KeptAlive.seen.append((self.path, dict(self.headers.items()), body))
        KeptAlive.peers.append(self.client_address)
        if body.get("mode") == "429":
            return self.reply(429, b'{"error":{"message":"Rate limit reached"}}', "application/json",
                              {"retry-after": "7", "x-ratelimit-remaining-requests": "0"})
        if self.path.endswith("/messages"):
            if body.get("stream"):
                return self.reply(200, MSG_SSE, "text/event-stream")
            return self.reply(200, json.dumps(MSG).encode(), "application/json")
        self.reply(200, json.dumps(CHAT).encode(), "application/json",
                   {"x-request-id": "req_1", "Set-Cookie": "track=1", "x-ratelimit-remaining-tokens": "99"})

    def reply(self, status, body, kind, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class PassThroughTest(unittest.TestCase):
    """What the relay keeps of a call: one connection to the provider for
    many calls, the caller's headers but its credentials, the provider's
    answer as it was - a 429 included - and Anthropic's Messages read."""

    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), KeptAlive)
        cls.up.daemon_threads = True
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        KeptAlive.seen.clear()
        KeptAlive.peers.clear()

    def relay(self, **kw):
        r = relay.Relay(self.base, "sk-real", tempfile.mkdtemp(), **kw).start()
        self.addCleanup(r.stop)
        return r

    def post(self, r, tag, path, body, headers=None):
        req = urllib.request.Request(r.url(tag) + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as got:
                return got.status, dict(got.headers.items()), got.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items()), e.read()

    def test_one_connection_carries_many_calls(self):
        r = self.relay(model="m")
        for _ in range(4):
            self.assertEqual(self.post(r, "u", "/chat/completions", {"model": "m", "messages": []})[0], 200)
        self.assertEqual(len(set(KeptAlive.peers)), 1)
        self.assertEqual(r.pool.opened, 1)

    def test_a_connection_the_provider_closed_is_replaced_once(self):
        r = self.relay(model="m")
        self.post(r, "u", "/chat/completions", {"model": "m", "messages": []})
        for _ in range(100):                      # given back just after the caller has its reply
            if r.pool.idle:
                break
            time.sleep(0.01)
        for c in r.pool.idle:                     # the provider dropped it while it sat idle
            c.sock.shutdown(2)
        status, _, body = self.post(r, "u", "/chat/completions", {"model": "m", "messages": []})
        self.assertEqual((status, json.loads(body)), (200, CHAT))
        self.assertEqual(len(KeptAlive.seen), 2)  # sent once, not twice
        self.assertEqual(r.pool.opened, 2)

    def test_headers_go_on_but_the_credentials_and_the_hops(self):
        r = self.relay(model="m")
        status, back, _ = self.post(r, "u", "/chat/completions", {"model": "m", "messages": []}, {
            "Authorization": "Bearer " + relay.PLACEHOLDER_KEY, "x-api-key": "sk-agent", "api-key": "sk-agent2",
            "OpenAI-Organization": "org-x", "Cookie": "c=1", "Keep-Alive": "timeout=5", "Upgrade": "h2c",
            "session-id": "s-1", "thread-id": "t-1", "x-codex-beta-capabilities": "a,b", "User-Agent": "codex/2"})
        sent = {k.lower(): v for k, v in KeptAlive.seen[-1][1].items()}
        self.assertEqual(sent["authorization"], "Bearer sk-real")       # the relay's key, and only it
        for gone in ("x-api-key", "api-key", "openai-organization", "cookie", "keep-alive", "upgrade"):
            self.assertNotIn(gone, sent)
        self.assertEqual((sent["session-id"], sent["thread-id"], sent["x-codex-beta-capabilities"], sent["user-agent"]),
                         ("s-1", "t-1", "a,b", "codex/2"))
        self.assertEqual(sent["accept-encoding"], "identity")           # the relay reads the reply
        self.assertEqual([k.lower() for k in KeptAlive.seen[-1][1]].count("content-type"), 1)
        rec = r.calls("u")[-1]
        self.assertIn("session-id", rec["headers"])
        self.assertNotIn("authorization", rec["headers"])
        self.assertNotIn("s-1", json.dumps(rec))                         # names recorded, never values
        back = {k.lower(): v for k, v in back.items()}
        self.assertEqual((back["x-request-id"], back["x-ratelimit-remaining-tokens"]), ("req_1", "99"))
        self.assertNotIn("set-cookie", back)

    def test_a_search_route_pins_embeddings_separately_and_keeps_other_routes(self):
        led = ledger.Ledger.create(os.path.join(tempfile.mkdtemp(), "ledger.json"), 1.0)
        rates = {"embedding": {"input": 1, "output": 0}, "reranker": {"input": 3, "output": 15},
                 "agent": {"input": 2, "output": 4}, "reviewer": {"input": 1, "output": 1}}
        r = self.relay(model="agent", side_models=["reviewer"], prices=rates, ledger=led.path)
        r.url("search", model="reranker", embeddings="embedding")
        schema = {"type": "json_schema", "json_schema": {"name": "RelevanceScore", "strict": True,
                  "schema": {"type": "object", "properties": {"relevance_score": {"type": "integer"}},
                             "required": ["relevance_score"], "additionalProperties": False}}}
        for tag, path, asked, sent in (("search", "/embeddings", "asked", "embedding"),
                                     ("search", "/chat/completions", "asked", "reranker"),
                                     ("search", "/responses", "asked", "reranker"),
                                     ("agent", "/embeddings", "asked", "agent"),
                                     ("agent", "/chat/completions", "reviewer", "reviewer")):
            status, _, _ = self.post(r, tag, path, {"model": asked, "input": "query", "max_tokens": 8,
                                                   "response_format": schema, "reasoning_effort": "low"})
            self.assertEqual(status, 200)
            request = KeptAlive.seen[-1][2]
            self.assertEqual((request["model"], request["response_format"], request["reasoning_effort"]),
                             (sent, schema, "low"))
            record = r.calls(tag)[-1]
            self.assertEqual((record["model"], record["model_asked"]), (sent, asked))
            self.assertIsNotNone(record["cost_usd"])
        self.assertAlmostEqual(led.state()["spent_usd"], sum(x["cost_usd"] for t in ("search", "agent") for x in r.calls(t)))

    def test_a_side_model_goes_as_asked_and_another_is_the_runs(self):
        r = self.relay(model="run-model", side_models=["reviewer"])
        self.post(r, "u", "/responses", {"model": "reviewer", "input": "x"})
        self.assertEqual(KeptAlive.seen[-1][2]["model"], "reviewer")
        self.post(r, "u", "/responses", {"model": "elsewhere", "input": "x"})
        self.assertEqual(KeptAlive.seen[-1][2]["model"], "run-model")
        side, other = r.calls("u")
        self.assertEqual((side["model"], side.get("side")), ("reviewer", True))
        self.assertEqual((other["model"], other["model_asked"], other.get("side")), ("run-model", "elsewhere", None))

    def test_a_side_model_may_be_sent_as_the_provider_names_it(self):
        r = self.relay(model="vendor/reader", side_models=["extractor=vendor/extractor"])
        self.post(r, "u", "/chat/completions", {"model": "extractor", "messages": []})
        self.assertEqual(KeptAlive.seen[-1][2]["model"], "vendor/extractor")
        rec = r.calls("u")[-1]
        self.assertEqual((rec["model"], rec["model_asked"], rec.get("side")), ("vendor/extractor", "extractor", True))

    def test_a_429_reaches_the_caller_as_sent_and_is_not_retried(self):
        r = self.relay(model="m")
        status, back, body = self.post(r, "u", "/chat/completions", {"model": "m", "mode": "429"})
        self.assertEqual((status, body), (429, b'{"error":{"message":"Rate limit reached"}}'))
        back = {k.lower(): v for k, v in back.items()}
        self.assertEqual((back["retry-after"], back["x-ratelimit-remaining-requests"]), ("7", "0"))
        self.assertEqual(len(KeptAlive.seen), 1)
        self.assertEqual(r.calls("u")[-1]["status"], 429)

    def test_a_benchmark_route_beside_an_anthropic_agent_takes_a_bearer_key(self):
        r = self.relay(model="claude-x", key_header="x-api-key")
        self.post(r, "u", "/messages", {"model": "claude-x", "max_tokens": 10, "messages": []}, {"x-api-key": relay.PLACEHOLDER_KEY})
        self.assertIn("x-api-key", {k.lower() for k in KeptAlive.seen[-1][1]})
        r.url("u-judge", model="judge-x")
        self.post(r, "u-judge", "/chat/completions", {"model": "any", "messages": []})
        sent = {k.lower(): v for k, v in KeptAlive.seen[-1][1].items()}
        self.assertEqual(sent["authorization"], "Bearer sk-real")
        self.assertFalse({"x-api-key", "anthropic-version"} & set(sent))

    def test_anthropic_messages_take_the_key_as_x_api_key_and_their_usage_is_read(self):
        r = self.relay(model="claude-x", key_header="x-api-key")
        status, _, body = self.post(r, "u", "/messages", {"model": "claude-x", "max_tokens": 10, "messages": []},
                                    {"x-api-key": relay.PLACEHOLDER_KEY})
        self.assertEqual(json.loads(body), MSG)
        sent = {k.lower(): v for k, v in KeptAlive.seen[-1][1].items()}
        self.assertEqual((sent["x-api-key"], sent["anthropic-version"]), ("sk-real", relay.ANTHROPIC_VERSION))
        self.assertNotIn("authorization", sent)
        rec = r.calls("u")[-1]["reply"]
        self.assertEqual(rec["text"], "hello")
        self.assertEqual(rec["tool_calls"], [{"name": "bash", "arguments": '{"cmd": "ls"}'}])
        self.assertEqual(rec["usage"], {"input": 132, "output": 3, "cached": 100, "reasoning": 0, "cache_write": 20})

    def test_tokens_written_to_a_cache_are_priced_apart_from_those_read_from_it(self):
        """The reply read 100 tokens and wrote 20: a write costs more than a fresh token, a read less."""
        row = {"input": 3.0, "cached": 0.3, "cache_write": 3.75, "output": 15.0}
        for prices, cost in ((row, 12 * 3.0 + 100 * 0.3 + 20 * 3.75 + 3 * 15.0),
                             ({k: v for k, v in row.items() if k != "cache_write"}, 12 * 3.0 + 100 * 0.3 + 20 * 3.0 + 3 * 15.0)):   # no price for a write: the input's
            r = self.relay(model="claude-x", key_header="x-api-key", prices={"claude-x": prices})
            self.post(r, "u", "/messages", {"model": "claude-x", "max_tokens": 10, "messages": []}, {"x-api-key": relay.PLACEHOLDER_KEY})
            rec = r.calls("u")[-1]
            self.assertEqual(rec["cost_from"], "estimate")
            self.assertAlmostEqual(rec["cost_usd"], cost / 1e6)

    def test_a_cache_write_said_in_the_chat_format_is_priced_as_one(self):
        """The usage of a real call that wrote 7,081 tokens, then of one that read them: charged 0.000889025 and 0.00007471 USD."""
        r = self.relay(prices={"m": {"input": 0.10, "output": 0.50, "cached": 0.01, "cache_write": 0.125}})
        wrote = relay._usage({"prompt_tokens": 7105, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 7081}})
        self.assertEqual(wrote, {"input": 7105, "output": 3, "cached": 0, "reasoning": 0, "cache_write": 7081})
        self.assertAlmostEqual(r._cost("m", wrote)[0], 0.000889025)                  # 0.000712 at the input rate
        read = relay._usage({"prompt_tokens": 7105, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 7081, "cache_write_tokens": 0}})
        self.assertEqual(read, {"input": 7105, "output": 3, "cached": 7081, "reasoning": 0})
        self.assertAlmostEqual(r._cost("m", read)[0], 0.00007471)

    def test_mixed_cache_lifetimes_reach_cost_ledger_totals_and_spans(self):
        usage = {"input_tokens": 2048, "cache_read_input_tokens": 1800, "cache_creation_input_tokens": 248,
                 "output_tokens": 503, "cache_creation": {"ephemeral_5m_input_tokens": 148, "ephemeral_1h_input_tokens": 100}}
        row = {"input": 3.0, "output": 15.0, "cached": 0.3, "cache_write": 3.75, "cache_write_1h": 6.0}
        expected = (2048 * 3 + 1800 * 0.3 + 148 * 3.75 + 100 * 6 + 503 * 15) / 1e6
        with tempfile.TemporaryDirectory() as folder:
            led = ledger.Ledger.create(os.path.join(folder, "ledger.json"), 1.0)
            r = self.relay(model="m", prices={"m": row}, ledger=led.path)
            # Two real local HTTP exchanges: the same provider usage as JSON and SSE.
            for stream in (False, True):
                sse = ('data: ' + json.dumps({"type": "message_start", "message": {"usage": usage}}) + '\n\n'
                       + 'data: {"type":"message_delta","usage":{"output_tokens":503}}\n\n').encode()
                with mock.patch.dict(MSG, usage=usage), mock.patch(__name__ + ".MSG_SSE", sse):
                    status, _, body = self.post(r, "u", "/messages", {"model": "m", "max_tokens": 600, "messages": [], "stream": stream})
                self.assertEqual(status, 200)
                if stream:
                    self.assertEqual(body, sse)
                call = r.calls("u")[-1]
                self.assertEqual(call["reply"]["usage"], {"input": 4096, "output": 503, "cached": 1800,
                                                         "reasoning": 0, "cache_write": 248, "cache_write_1h": 100})
                self.assertAlmostEqual(call["cost_usd"], expected)
                tokens = job._call_span(0, call)["tokens"]
                self.assertEqual((tokens["input_tokens"], tokens["cache_read_tokens"], tokens["cache_write_tokens"]), (4096, 1800, 248))
            self.assertAlmostEqual(led.state()["spent_usd"], 2 * expected)
            self.assertEqual(job._usage_total(r.calls("u"))["cached"], 3600)
            self.assertEqual(job._usage_total(r.calls("u"))["input"], 8192)
            r.prices["m"] = {k: v for k, v in row.items() if k != "cache_write_1h"}
            normalized = relay._usage(usage)
            self.assertAlmostEqual(r._cost("m", normalized)[0], (2048 * 3 + 1800 * 0.3 + 248 * 3.75 + 503 * 15) / 1e6)
            self.assertEqual(r._cost("m", dict(normalized, cost=0.02)), (0.02, "provider"))

    def test_anthropic_messages_streamed(self):
        r = self.relay(model="claude-x", key_header="x-api-key")
        _, _, body = self.post(r, "u", "/messages", {"model": "claude-x", "stream": True, "messages": []},
                               {"anthropic-version": "2099-01-01"})
        self.assertEqual(body, MSG_SSE)
        sent = KeptAlive.seen[-1]
        self.assertNotIn("stream_options", sent[2])                     # Chat Completions' only: Messages would refuse it
        self.assertEqual({k.lower(): v for k, v in sent[1].items()}["anthropic-version"], "2099-01-01")
        rec = r.calls("u")[-1]["reply"]
        self.assertEqual(rec["text"], "hi")
        self.assertEqual(rec["tool_calls"], [{"name": "bash", "arguments": '{"cmd":"ls"}'}])
        self.assertEqual(rec["usage"], {"input": 45, "output": 9, "cached": 40, "reasoning": 0})

    def test_an_endpoint_out_of_reach_is_a_502_recorded(self):
        r = relay.Relay("http://127.0.0.1:9/v1", "k", tempfile.mkdtemp()).start()
        self.addCleanup(r.stop)
        status, _, body = self.post(r, "u", "/chat/completions", {"model": "m"})
        self.assertEqual(status, 502)
        self.assertIn("could not reach", r.calls("u")[-1]["error"])

    def test_a_relay_stops_without_waiting_out_a_poll(self):
        import time
        took = []
        for _ in range(3):                                                  # the fastest: a loaded machine can delay any one stop
            r = relay.Relay(self.base, "k", tempfile.mkdtemp()).start()
            t0 = time.monotonic()
            r.stop()
            took.append(time.monotonic() - t0)
        self.assertLess(min(took), 0.2)

    def test_a_key_the_header_refuses_never_reaches_the_reply_or_the_record(self):
        """http.client quotes a header value it refuses in its error: a key
        read from a CRLF file, with a trailing CR, would have been sent to
        the agent and written into the run's records."""
        key = "sk-proj-SECRETSECRETSECRET\r"
        d = tempfile.mkdtemp()
        r = relay.Relay(self.base, key, d).start()
        self.addCleanup(r.stop)
        status, _, body = self.post(r, "u", "/chat/completions", {"model": "m"})
        self.assertEqual(status, 502)
        self.assertNotIn("SECRETSECRET", body.decode() if isinstance(body, bytes) else str(body))
        for name in os.listdir(d):
            with open(os.path.join(d, name)) as f:
                self.assertNotIn("SECRETSECRET", f.read())

    def test_a_run_refuses_such_a_key_before_it_starts(self):
        from nullius.standard.runner import run
        for key in ("sk-abc\r", " sk-abc", "\ufeffsk-abc", "sk-ab\u200bc"):
            with self.assertRaises(run.Refused):
                run.provider_endpoint("openai", {"OPENAI_API_KEY": key})
        self.assertEqual(run.provider_endpoint("openai", {"OPENAI_API_KEY": "sk-abc"})[1], "sk-abc")


ECHO = r"""
import sys
sys.path.insert(0, sys.argv[1])
from nullius.standard.protocols import jsonl
jsonl.serve({"echo": lambda **kw: {"got": kw}, "boom": lambda **kw: 1 / 0})
"""


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        script = os.path.join(self.tmp, "p.py")
        open(script, "w").write(ECHO)
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.p = jsonl.Proc([sys.executable, script, app], self.tmp, dict(os.environ), os.path.join(self.tmp, "log")).start()

    def tearDown(self):
        self.p.close()

    def test_one_request_one_reply(self):
        self.assertEqual(self.p.request("echo", a=1), {"got": {"a": 1}})

    def test_an_error_reply_is_raised_and_the_program_goes_on(self):
        with self.assertRaisesRegex(jsonl.Failed, "ZeroDivisionError"):
            self.p.request("boom")
        self.assertEqual(self.p.request("echo"), {"got": {}})

    def test_no_reply_in_time_is_raised(self):
        q = jsonl.Proc([sys.executable, "-c", "import time; time.sleep(30)"], self.tmp, dict(os.environ),
                       os.path.join(self.tmp, "log2")).start()
        with self.assertRaisesRegex(jsonl.Failed, "no reply in 1 s"):
            q.request("x", timeout=1)
        q.close()


class Failure(unittest.TestCase):
    """What a unit whose last call failed ends with: named so a retry is asked
    only of what a second call may mend (``retries``)."""

    def test_a_spent_credit_and_an_unknown_model_are_named_as_no_retry_mends(self):
        def named(status, said, **kw):
            return relay.failure([{"status": status, "error": said, **kw}])[0]
        self.assertEqual(named(429, "HTTP 429: Rate limit reached for requests"), "RateLimitError")
        # OpenAI's spent credit is a 429 too; Gemini says "exceeded your current quota" for its per-minute limit
        self.assertEqual(named(429, "HTTP 429: You exceeded your current quota", error_code="insufficient_quota"), "ApiUsageLimitError")
        self.assertEqual(named(429, "HTTP 429: You exceeded your current quota, please check your plan"), "RateLimitError")
        self.assertEqual(named(402, "HTTP 402: Insufficient credits"), "ApiUsageLimitError")
        self.assertEqual(named(402, "HTTP 402: no price for the model", refused="price"), "ApiError")
        for said in ("HTTP 400: xyz is not a valid model ID", "HTTP 400: Invalid model: foo", "HTTP 400: Model Not Exist",
                     "HTTP 404: models/x is not found for API version v1beta"):
            with self.subTest(said=said):
                self.assertEqual(named(int(said[5:8]), said), "ModelNotFoundError")
        for said in ("HTTP 400: This model's maximum context length is 8192 tokens", "HTTP 400: The model does not support tools"):
            with self.subTest(said=said):
                self.assertEqual(named(400, said), "ApiError")

    def test_the_providers_word_on_when_to_call_again_is_read(self):
        self.assertEqual(relay._retry_after([("Retry-After", "20")]), 20.0)
        self.assertEqual(relay._retry_after([("retry-after-ms", "1500")]), 1.5)
        self.assertEqual(relay._retry_after([("Retry-After", "Wed, 21 Oct 2015 07:28:00 GMT")]), 0.0)   # a date gone by
        self.assertIsNone(relay._retry_after([("Retry-After", "soon")]))
        self.assertIsNone(relay._retry_after([]))
        self.assertEqual(relay._code(b'{"error": {"message": "x", "type": "insufficient_quota", "code": "insufficient_quota"}}'),
                         "insufficient_quota")
        self.assertIsNone(relay._code(b'{"error": {"message": "x", "code": 429}}'))
        self.assertIsNone(relay._code(b"not json"))


if __name__ == "__main__":
    unittest.main()
