"""The relay passes replies through unchanged, streamed or not, records each
call by its unit, and names a unit's failure the way the classifier reads it.
A driver or capability process answers one JSON line per request."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.runs import classify
from nullius.standard import jsonl, relay

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
        elif self.path.endswith("/responses"):
            self.reply(200, RESP_SSE, "text/event-stream")
        elif body.get("stream"):
            self.reply(200, CHAT_SSE, "text/event-stream")
        else:
            self.reply(200, json.dumps(CHAT).encode(), "application/json")

    def reply(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
            return e.code, e.read()

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

    def test_streamed_chat_is_passed_byte_for_byte_and_read_whole(self):
        status, body = self.post("u2", "/chat/completions", {"model": "m", "stream": True, "messages": []})
        self.assertEqual(body, CHAT_SSE)
        self.assertTrue(Upstream.seen[-1][2]["stream_options"]["include_usage"])   # asked so the stream says its tokens
        rec = self.relay.calls("u2")[-1]
        self.assertEqual(rec["reply"]["text"], "hello")
        self.assertEqual(rec["reply"]["usage"]["input"], 7)

    def test_streamed_responses_read_from_the_completed_event(self):
        status, body = self.post("u3", "/responses", {"model": "m", "stream": True, "input": "x"})
        self.assertEqual(body, RESP_SSE)
        rec = self.relay.calls("u3")[-1]
        self.assertEqual(rec["reply"]["text"], "hi")
        self.assertEqual(rec["reply"]["tool_calls"][0]["name"], "shell")
        self.assertEqual(rec["reply"]["usage"]["cached"], 10)

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


class EffortTest(unittest.TestCase):
    def test_the_record_says_the_reasoning_effort_a_request_asked_for(self):
        # chat's reasoning_effort, the Responses API's reasoning.effort, and none at all
        self.assertEqual(relay._request_tail({"messages": [], "reasoning_effort": "low"})["reasoning_effort"], "low")
        self.assertEqual(relay._request_tail({"input": [], "reasoning": {"effort": "high"}})["reasoning_effort"], "high")
        self.assertIsNone(relay._request_tail({"messages": []})["reasoning_effort"])


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
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.app}
        if key:
            env[relay.KEY_VAR] = key
        return subprocess.Popen([sys.executable, "-m", "nullius.standard.relay", "serve", "--listen", "127.0.0.1:0",
                                 "--base", f"http://127.0.0.1:{self.up.server_address[1]}/v1", "--record", tempfile.mkdtemp(),
                                 "--model", "the-recipes", *extra], env=env, stdout=subprocess.PIPE, text=True)

    def test_serve_relays_with_the_key_from_its_environment_and_stops_on_sigterm(self):
        import signal
        p = self.serve("sk-from-env", "--route", "u1-judge=the-judge")
        try:
            where = json.loads(p.stdout.readline())["listening"]
            for tag, model in (("u1", "the-recipes"), ("u1-judge", "the-judge")):
                req = urllib.request.Request(f"{where}/{tag}/v1/chat/completions", data=b'{"model":"asked"}',
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req).read()
                self.assertEqual(Upstream.seen[-1][1], "Bearer sk-from-env")
                self.assertEqual(Upstream.seen[-1][2]["model"], model)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()

    def test_serve_without_the_key_in_its_environment_refuses(self):
        p = self.serve("")
        self.assertIn(relay.KEY_VAR, p.stdout.readline())
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
        self.assertEqual(rec["usage"], {"input": 132, "output": 3, "cached": 120, "reasoning": 0})

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
        r = relay.Relay(self.base, "k", tempfile.mkdtemp()).start()
        t0 = time.monotonic()
        r.stop()
        self.assertLess(time.monotonic() - t0, 0.2)

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
        from nullius.standard import run
        for key in ("sk-abc\r", " sk-abc", "\ufeffsk-abc", "sk-ab\u200bc"):
            with self.assertRaises(run.Refused):
                run.provider_endpoint("openai", {"OPENAI_API_KEY": key})
        self.assertEqual(run.provider_endpoint("openai", {"OPENAI_API_KEY": "sk-abc"})[1], "sk-abc")


ECHO = r"""
import sys
sys.path.insert(0, sys.argv[1])
from nullius.standard import jsonl
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


if __name__ == "__main__":
    unittest.main()
