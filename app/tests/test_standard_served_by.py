"""A pin of the serving providers that may answer a model (``--served-by``):
the relay puts OpenRouter's routing field on every request it sends as that
model and on no other, in place of the caller's own; each call records the
routing sent, the provider that served the reply, how the reply ended and an
error inside a success, as cleaned copies, read apart from the rest of the
record; the job and each trial say who served the calls of every route and
whether the pin held; ``merge`` refuses shards of different pins; a
container's relay is given the pin as the unit's own relay is. A run without
the option sends the same requests and ends its units as it did. Every
upstream here is a fake on this machine's loopback: no request leaves it."""
from __future__ import annotations

import contextlib
import io
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nullius import progress
from nullius.standard import cli, container, definitions, install, job, merge, relay, run

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = {"order": ["prov-a", "prov-b/fp8"], "allow_fallbacks": False}
KEY = "or-test-relay-key-0123456789"
NEW = ("served_by", "end", "error")           # what the record's reply gains, beside what it held


def sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


# Each reply names its serving provider and how it ended, as OpenRouter's do: Chat Completions, Messages, Responses.
CHAT = {"id": "c1", "provider": "Prov A", "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
CHAT_SSE = sse({"id": "c1", "provider": "Prov A", "choices": [{"index": 0, "delta": {"content": "h"}, "finish_reason": None}]},
               {"id": "c1", "provider": "Prov A", "choices": [{"index": 0, "delta": {"content": "i"}, "finish_reason": "length"}]},
               {"id": "c1", "provider": "Prov A", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
MESSAGES = {"type": "message", "provider": "Prov B", "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1}}
MESSAGES_SSE = sse({"type": "message_start", "message": {"type": "message", "provider": "Prov B", "usage": {"input_tokens": 3}}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                   {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 1}},
                   {"type": "message_stop"})
RESPONSES = {"object": "response", "provider": "Prov C", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
             "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
             "usage": {"input_tokens": 3, "output_tokens": 1}}
RESPONSES_SSE = sse({"type": "response.created", "response": {"provider": "Prov C", "status": "in_progress"}},
                    {"type": "response.output_text.delta", "delta": "hi"},
                    {"type": "response.completed", "response": {"provider": "Prov C", "status": "completed", "output": [],
                                                                "usage": {"input_tokens": 3, "output_tokens": 1}}})
SHAPES = {("/chat/completions", False): (CHAT, "Prov A", "stop"), ("/chat/completions", True): (CHAT_SSE, "Prov A", "length"),
          ("/messages", False): (MESSAGES, "Prov B", "end_turn"), ("/messages", True): (MESSAGES_SSE, "Prov B", "max_tokens"),
          ("/responses", False): (RESPONSES, "Prov C", "incomplete: max_output_tokens"),
          ("/responses", True): (RESPONSES_SSE, "Prov C", "completed")}


def body_for(path: str, stream: bool) -> dict:
    """A caller's request of that shape."""
    if path == "/responses":
        return {"model": "asked", "input": "hello", "stream": stream}
    return {"model": "asked", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 5, "stream": stream}


def without_new(reply: dict) -> dict:
    return {k: v for k, v in reply.items() if k not in NEW}


class Upstream(BaseHTTPRequestHandler):
    """A provider on the loopback: keeps every request as it came, bytes and headers, and answers it in its shape;
    ``Upstream.next`` replaces the next reply (status, content type, body), ``Upstream.always`` every one."""
    seen = []
    next = None
    always = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
        req = json.loads(raw)
        if Upstream.next is not None or Upstream.always is not None:
            (status, kind, body), Upstream.next = Upstream.next or Upstream.always, None
        else:
            path = "/" + self.path.split("/v1/", 1)[-1]
            reply = SHAPES.get((path, bool(req.get("stream"))), (CHAT,))[0]
            status, kind, body = 200, "text/event-stream" if req.get("stream") else "application/json", \
                reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post(url: str, body: dict, headers=None) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.PLACEHOLDER_KEY,
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


class WithUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Upstream.seen, Upstream.next, Upstream.always = [], None, None
        self.relays = []
        self.addCleanup(setattr, Upstream, "always", None)

    def tearDown(self):
        for r in self.relays:
            r.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def relay(self, **kw):
        r = relay.Relay(self.base, kw.pop("key", "or-test"), os.path.join(self.tmp, f"rec{len(self.relays)}"), **kw).start()
        self.relays.append(r)
        return r

    def sent(self, i=-1) -> dict:
        return json.loads(Upstream.seen[i][2])


class TheRelay(WithUpstream):
    def test_the_pin_goes_on_every_request_sent_as_the_pinned_model_and_on_no_other(self):
        r = self.relay(model="m-run", serving={"m-run": ["prov-a", "prov-b/fp8"]})
        post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False))
        post(r.url("u0-judge", model="m-judge") + "/chat/completions", body_for("/chat/completions", False))
        post(r.url("u0") + "/messages", body_for("/messages", True))
        self.assertEqual([(json.loads(raw)["model"], json.loads(raw).get("provider")) for _, _, raw in Upstream.seen],
                         [("m-run", PIN), ("m-judge", None), ("m-run", PIN)])
        self.assertEqual([c["request"].get("routing") for c in r.calls("u0")], [PIN, PIN])
        self.assertNotIn("routing", r.calls("u0-judge")[0]["request"])

    def test_a_callers_own_routing_is_replaced_and_both_are_recorded(self):
        r = self.relay(model="m-run", serving={"m-run": ["prov-a", "prov-b/fp8"]})
        post(r.url("u0") + "/chat/completions", dict(body_for("/chat/completions", False), provider={"sort": "price"}))
        self.assertEqual(self.sent()["provider"], PIN)
        (c,) = r.calls("u0")
        self.assertEqual((c["request"]["routing"], c["request"]["routing_asked"]), (PIN, {"sort": "price"}))

    def test_without_a_pin_a_callers_routing_goes_on_as_it_came_and_is_recorded(self):
        r = self.relay(model="m-run")
        post(r.url("u0") + "/chat/completions", dict(body_for("/chat/completions", False), provider={"only": ["prov-z"]}))
        self.assertEqual(self.sent()["provider"], {"only": ["prov-z"]})
        (c,) = r.calls("u0")
        self.assertEqual(c["request"]["routing"], {"only": ["prov-z"]})
        self.assertNotIn("routing_asked", c["request"])

    def test_the_recorded_copies_are_cleaned_and_what_is_sent_is_not(self):
        """An error that echoes the request's credentials, and a routing object holding a token: the record keeps
        neither the relay's key nor the token; the provider receives the caller's bytes as they were."""
        Upstream.always = (200, "application/json", json.dumps({"error": {"code": 401, "message": f"Authorization: Bearer {KEY}"}}).encode())
        for serving in (None, {"m-run": ["prov-a"]}):
            with self.subTest(serving=serving):
                r = self.relay(model="m-run", key=KEY, serving=serving)
                asked = dict(body_for("/chat/completions", False), provider={"order": ["prov-a"], "token": KEY, "note": f"key {KEY}", KEY: "as a key"})
                post(r.url("u0") + "/chat/completions", asked)
                c = r.calls("u0")[-1]
                self.assertNotIn(KEY, json.dumps(c))
                self.assertEqual(c["reply"]["error"]["code"], 401)
                if serving is None:                  # sent as the caller sent it: the record alone is cleaned
                    self.assertEqual(self.sent()["provider"], asked["provider"])
                    self.assertEqual(c["request"]["routing"]["token"], "[REDACTED]")
                else:
                    self.assertEqual(c["request"]["routing_asked"]["token"], "[REDACTED]")

    def test_each_shape_streamed_or_not_says_who_served_it_and_how_it_ended(self):
        r = self.relay(model="m-run", serving={"m-run": ["prov-a"]})
        for (path, stream), (_, who, end) in SHAPES.items():
            with self.subTest(path=path, stream=stream):
                post(r.url("u0") + path, body_for(path, stream))
                self.assertEqual(self.sent()["provider"], {"order": ["prov-a"], "allow_fallbacks": False})
                reply = r.calls("u0")[-1]["reply"]
                self.assertEqual((reply.get("served_by"), reply.get("end"), reply.get("error")), (who, end, None))
                # what was read before is read as before, beside the new facts
                self.assertEqual(without_new(reply), relay.read_reply(path, SHAPES[(path, stream)][0] if stream
                                                                      else json.dumps(SHAPES[(path, stream)][0]).encode(), stream))

    def test_several_choices_keep_their_endings_and_errors_by_index(self):
        body = {"choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"},
                            {"index": 1, "message": {}, "finish_reason": "error", "error": {"code": 429, "message": "limited"}}]}
        self.assertEqual(relay.reply_facts("/chat/completions", json.dumps(body).encode(), False),
                         {"end": {"0": "stop", "1": "error"}, "error": {"code": 429, "message": "limited", "choice": 1}})
        stream = sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}, {"index": 1, "delta": {}, "finish_reason": None}]},
                     {"choices": [{"index": 1, "delta": {}, "finish_reason": "stop"}]})
        self.assertEqual(relay.reply_facts("/chat/completions", stream, True)["end"], {"0": "length", "1": "stop"})

    def test_an_error_beside_a_whole_reply_is_read_in_every_shape(self):
        error = {"code": 502, "message": "failed"}
        for path, body in (("/messages", {"type": "message", "content": [], "error": error}),
                           ("/embeddings", {"object": "list", "data": [], "error": error}),
                           ("/chat/completions", {"choices": [{"message": {"content": "ok"}}], "error": error}),
                           ("/responses", {"object": "response", "status": "failed", "output": [], "error": error})):
            with self.subTest(path=path):
                self.assertEqual(relay.reply_facts(path, json.dumps(body).encode(), False)["error"], error)

    def test_a_completed_responses_events_routing_facts_name_who_served_it(self):
        body = sse({"type": "response.completed", "response": {"object": "response", "status": "completed", "output": [],
                    "openrouter_metadata": {"attempts": [{"provider": "Prov A", "status": 200}]}}})
        self.assertEqual(relay.reply_facts("/responses", body, True), {"served_by": "Prov A", "end": "completed"})

    def test_facts_of_the_wrong_type_are_left_out_and_never_stop_a_reply(self):
        odd = [{"choices": [{"message": {"content": "ok"}}], "openrouter_metadata": {"attempts": 1}},
               {"type": "message", "content": [], "provider": 7, "error": "a string", "stop_reason": ["x"]},
               {"choices": [{"index": "0", "finish_reason": 3, "error": []}], "openrouter_metadata": [1]}]
        r = self.relay(model="m-run", serving={"m-run": ["prov-a"]})
        for body in odd:
            with self.subTest(body=body):
                raw = json.dumps(body).encode()
                Upstream.next = (200, "application/json", raw)
                self.assertEqual(post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False)), raw)
                self.assertEqual({k: v for k, v in r.calls("u0")[-1]["reply"].items() if k in NEW}, {})
        self.assertEqual(relay.reply_facts("/chat/completions", b"[1, 2]", False), {})
        self.assertEqual(relay.reply_facts("/chat/completions", b"not json", False), {})
        self.assertEqual(relay.reply_facts("/chat/completions", b"data: [1]\n\ndata: {\"error\": 5}\n\n", True), {})

    def test_what_the_record_read_before_is_read_as_before(self):
        """The facts are read apart: a failed Responses stream's text, tool calls and usage stay as the relay read
        them before (it took nothing from a ``response.failed`` event), its error beside them."""
        body = sse({"type": "response.output_text.delta", "delta": "part"},
                   {"type": "response.failed", "response": {"object": "response", "status": "failed",
                                                            "error": {"code": "server_error", "message": "failed"},
                                                            "output": [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}],
                                                            "usage": {"input_tokens": 5, "output_tokens": 2}}})
        self.assertEqual(relay.read_reply("/responses", body, True), {"text": "", "tool_calls": [], "usage": None})
        self.assertEqual(relay.reply_facts("/responses", body, True),
                         {"end": "failed", "error": {"code": "server_error", "message": "failed"}})

    def test_an_error_inside_a_200_is_recorded_and_changes_nothing_else(self):
        """Recorded beside the reply as it was read; the call keeps its status, no ``error`` of the record's own
        is added, and a unit's failure reads it as before (a 200 is no failure)."""
        r = self.relay(model="m-run")
        errors = [(b'{"error":{"code":502,"message":"Provider returned error"}}', "application/json",
                   {"code": 502, "message": "Provider returned error"}),
                  (sse({"id": "c1", "provider": "Prov A", "choices": [{"index": 0, "delta": {"content": "part"}}]},
                       {"id": "c1", "error": {"code": "server_error", "message": "upstream ended"},
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}), "text/event-stream",
                   {"code": "server_error", "message": "upstream ended"}),
                  (sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}), "text/event-stream",
                   {"type": "overloaded_error", "message": "Overloaded"})]
        for body, kind, said in errors:
            with self.subTest(said=said):
                Upstream.next = (200, kind, body)
                streamed = kind == "text/event-stream"
                self.assertEqual(post(r.url("u0") + "/chat/completions", body_for("/chat/completions", streamed)), body)
                c = r.calls("u0")[-1]
                self.assertEqual((c["status"], c["reply"]["error"]), (200, said))
                self.assertNotIn("error", c)
                self.assertEqual(without_new(c["reply"]), relay.read_reply("/chat/completions", body, streamed))
                self.assertIsNone(relay.failure(r.calls("u0")))

    def test_the_routing_facts_are_asked_of_openrouter_alone_and_always_as_enabled(self):
        meta = json.dumps(dict({k: v for k, v in CHAT.items() if k != "provider"},
                               openrouter_metadata={"attempts": [{"provider": "Prov X", "status": 503},
                                                                 {"provider": "Prov A", "status": 200}]})).encode()
        r = self.relay(model="m-run", serving={"m-run": ["prov-a"]}, facts=True)
        Upstream.next = (200, "application/json", meta)
        post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False), {"X-OpenRouter-Metadata": "disabled"})
        got = [v for k, v in Upstream.seen[-1][1].items() if k == "x-openrouter-metadata"]
        self.assertEqual(got, ["enabled"])
        self.assertEqual(r.calls("u0")[-1]["reply"]["served_by"], "Prov A")
        post(r.url("u0") + "/messages", body_for("/messages", False))       # not a Chat request: nothing asked
        self.assertNotIn("x-openrouter-metadata", Upstream.seen[-1][1])
        for kw in ({"serving": {"m-run": ["prov-a"]}}, {"facts": True}):     # another provider's address; no pin
            with self.subTest(kw=kw):
                other = self.relay(model="m-run", **kw)
                post(other.url("u1") + "/chat/completions", body_for("/chat/completions", False))
                self.assertNotIn("x-openrouter-metadata", Upstream.seen[-1][1])
        self.assertTrue(relay.Relay("https://openrouter.ai/api/v1", "k", os.path.join(self.tmp, "or")).facts)
        self.assertFalse(relay.Relay("https://api.openai.com/v1", "k", os.path.join(self.tmp, "oa")).facts)

    def test_without_a_pin_every_request_goes_on_byte_for_byte_as_before(self):
        """What a relay sent before pins existed: the caller's body with the model set (and a streamed Chat
        Completions request's usage flag), serialised once, and the caller's headers but its credentials. The
        three shapes, streamed or not, with and without a routing field of the caller's, as the run's model, a
        side model and a judge's route's model; with no pin, or a pin on another model."""
        for serving in (None, {"m-other": ["prov-a"]}):
            r = self.relay(model="m-run", side_models=("m-side",), serving=serving)
            for (path, stream) in SHAPES:
                for extra in ({}, {"provider": {"order": ["prov-z"]}}):
                    for url, asked, model in ((r.url("u0"), "asked", "m-run"), (r.url("u0"), "m-side", "m-side"),
                                              (r.url("u0-judge", model="m-judge"), "asked", "m-judge")):
                        with self.subTest(serving=serving, path=path, stream=stream, extra=extra, model=model):
                            req = dict(body_for(path, stream), model=asked, **extra)
                            post(url + path, req, {"X-Caller": "kept"})
                            expected = dict(req, model=model)
                            if stream and path == "/chat/completions":
                                expected.setdefault("stream_options", {})["include_usage"] = True
                            sent_path, headers, raw = Upstream.seen[-1]
                            self.assertEqual(raw, json.dumps(expected).encode())
                            self.assertEqual(sent_path, "/v1" + path)
                            self.assertEqual({k: v for k, v in headers.items() if k not in ("host", "content-length")},
                                             {"content-type": "application/json", "x-caller": "kept", "accept": "*/*",
                                              "accept-encoding": "identity", "authorization": "Bearer or-test",
                                              "user-agent": headers["user-agent"]})


class TheSidecar(WithUpstream):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(container, "_security", ""))      # a default rootful engine, never asked

    def test_the_containers_relay_is_given_the_pin_on_its_command_line(self):
        argv = container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="http://x/v1",
                                    model="m-run", routes={"u0-judge": "m-judge"},
                                    serving={"m-run": ["prov-a", "prov-b/fp8"], "m-judge": ["prov-c"]})
        got = [argv[i + 1] for i, a in enumerate(argv) if a == "--served-by"]
        self.assertEqual(got, ["m-judge=prov-c", "m-run=prov-a,prov-b/fp8"])
        self.assertNotIn("--served-by", container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k",
                                                             base="http://x/v1", model="m-run", routes={}))

    def test_the_unit_hands_its_pin_to_the_sidecars_command(self):
        unit = container.Unit("job", "u0", os.path.join(self.tmp, "rec"), image="img", app=APP, base=self.base, key="or-test",
                              model="m-run", serving={"m-run": ["prov-a"]})
        ran = []

        def docker(args, **kw):
            ran.append(args)
            if "run" in args[:1]:
                raise container.Unavailable("no engine in this test")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(container, "_docker", docker), self.assertRaises(container.Unavailable):
            unit.start()
        (started,) = [a for a in ran if a[:1] == ["run"]]
        self.assertIn("m-run=prov-a", started[started.index("--served-by") + 1:])

    def test_the_relays_own_command_refuses_a_pin_it_cannot_take(self):
        key = os.path.join(self.tmp, "key")
        with open(key, "w") as f:
            f.write("or-test")
        for given in (["m=bad slug"], ["m=prov-a,prov-a"], ["m="], ["m=prov-a", "m=prov-b"], ["prov-a"]):
            with self.subTest(given=given):
                # a process of its own, bounded: a relay that took the pin would serve until stopped
                got = subprocess.run([sys.executable, "-m", "nullius.standard.relay", "serve", "--listen", "127.0.0.1:0",
                                      "--base", self.base, "--record", os.path.join(self.tmp, "rec"), "--key-file", key,
                                      *[w for g in given for w in ("--served-by", g)]],
                                     cwd=APP, env=dict(os.environ, PYTHONPATH=APP), capture_output=True, text=True, timeout=20)
                self.assertEqual(got.returncode, 2, got.stdout)
                self.assertIn("error", json.loads(got.stdout.splitlines()[0]))

    def test_the_relay_as_a_process_of_its_own_puts_the_pin_on_its_requests(self):
        key = os.path.join(self.tmp, "key")
        with open(key, "w") as f:
            f.write("or-test")
        p = subprocess.Popen([sys.executable, "-m", "nullius.standard.relay", "serve", "--listen", "127.0.0.1:0", "--base", self.base,
                              "--record", os.path.join(self.tmp, "rec"), "--key-file", key, "--model", "m-run",
                              "--served-by", "m-run=prov-a,prov-b/fp8", "--routing"],
                             cwd=APP, env=dict(os.environ, PYTHONPATH=APP), stdout=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([p.stdout], [], [], 20)              # its one line, or no more than 20 s
            self.assertTrue(ready, "the relay did not say it listens within 20 s")
            url = json.loads(p.stdout.readline())["listening"]
            post(url + "/u0/v1/chat/completions", body_for("/chat/completions", False))
            self.assertEqual(self.sent()["provider"], PIN)
        finally:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(10)
            p.stdout.close()


class TheRun(WithUpstream):
    def setUp(self):
        super().setUp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.b = definitions.get("benchmark", "fakebench", ROOT)
        self.a = definitions.get("agent", "fake", ROOT)

    def tearDown(self):
        install.HOME = self._home
        super().tearDown()

    def make(self, **kw):
        kw.setdefault("other", self.a)
        return run.Run(kw.pop("bench", self.b), kw.pop("other"), model=kw.pop("model", "m-1"), provider=kw.pop("provider", "openrouter"),
                       env=kw.pop("env", {"OPENROUTER_API_KEY": "or-test"}), unsandboxed=True,
                       jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, **kw)

    def test_the_option_reads_model_and_slugs_and_refuses_what_it_cannot_take(self):
        self.assertEqual(run.served_by(["prov-a", "m-judge=prov-b/fp8, prov-c"], "m-run"),
                         {"m-run": ["prov-a"], "m-judge": ["prov-b/fp8", "prov-c"]})
        for given, said in ((["m-run=prov-a,"], "not a serving provider's slug"), (["m-run=a b"], "not a serving provider's slug"),
                            (["prov-a,prov-a"], "named twice"), (["prov-a", "m-run=prov-b"], "m-run twice"),
                            (["m-run="], "not a serving provider's slug")):
            with self.subTest(given=given), self.assertRaisesRegex(run.Refused, said):
                run.served_by(given, "m-run")
        with self.assertRaisesRegex(run.Refused, "MODEL=SLUG"):
            run.served_by(["prov-a"], "")

    def test_a_run_refuses_a_pin_it_cannot_take_however_it_is_given(self):
        for pin in ({"m-1": []}, {"m-1": ["bad slug"]}, {"m-1": ["prov-a", "prov-a"]}, {"": ["prov-a"]}, {"m-1": "prov-a"}):
            with self.subTest(pin=pin), self.assertRaisesRegex(run.Refused, "--served-by"):
                self.make(served_by=pin)

    def test_a_provider_that_routes_no_model_to_several_is_refused_before_any_unit(self):
        for provider, env in (("openai", {"OPENAI_API_KEY": "sk-x"}), ("anthropic", {"ANTHROPIC_API_KEY": "x"}),
                              ("custom", {"MODEL_API_URL": self.base})):
            with self.subTest(provider=provider), self.assertRaisesRegex(run.Refused, "no serving providers to choose among"):
                self.make(provider=provider, env=env, served_by={"m-1": ["prov-a"]})

    def test_a_model_the_run_does_not_call_is_refused_before_any_unit(self):
        with self.assertRaisesRegex(run.Refused, "names m-other, which this run does not call: it calls m-1"):
            self.make(served_by={"m-1": ["prov-a"], "m-other": ["prov-b"]})
        nop = definitions.control(dict(self.b, controls={"nop": {"source": "nullius", "from": "nothing said"}}), "nop")
        with self.assertRaisesRegex(run.Refused, "names m-1, which this run does not call: it calls no model"):
            self.make(other=nop, bench=dict(self.b, controls={"nop": {"source": "nullius", "from": "nothing said"}}),
                      served_by={"m-1": ["prov-a"]})

    def test_the_command_line_refuses_each_with_exit_2(self):
        for patch in (mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_other", return_value=self.a),
                      mock.patch.object(cli, "_exit_on_term"), mock.patch.object(cli.settings, "load"),
                      mock.patch.object(progress, "ECHO", None)):
            patch.start()
            self.addCleanup(patch.stop)
        for extra, said in ((["--provider", "openai", "--served-by", "prov-a"], "no serving providers"),
                            (["--served-by", "m-2=prov-a"], "does not call"),
                            (["--served-by", "m-1=bad slug"], "not a serving provider's slug"),
                            (["--served-by", "m-1="], "not a serving provider's slug"),
                            (["--served-by", "prov-a,prov-a"], "named twice"),
                            (["--served-by", "prov-a", "--served-by", "m-1=prov-b"], "m-1 twice")):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENROUTER_API_KEY": "or-test"}):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["run", "fakebench", "fake", "--model", "m-1", "--unsandboxed", "--provider", "openrouter",
                                     "--jobs", os.path.join(self.tmp, "jobs"), *extra])
                self.assertEqual(code, 2)
                self.assertTrue(out.getvalue().strip().splitlines()[-1].startswith("refused:"), out.getvalue())
                self.assertIn(said, out.getvalue())

    def test_a_pinned_run_records_its_pin_and_says_who_served_each_unit(self):
        b = dict(self.b, datasets=[{"id": "responding", "units": 3}])
        with mock.patch.dict(run.PROVIDERS, {"openrouter": dict(run.PROVIDERS["openrouter"], base=self.base)}):
            job_dir = run.run(b, self.a, model="m-1", provider="openrouter", env={"OPENROUTER_API_KEY": "or-test"}, unsandboxed=True,
                              jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, dataset="responding",
                              served_by={"m-1": ["prov-a"]})
        self.assertTrue(Upstream.seen)
        self.assertTrue(all(json.loads(raw).get("provider") == {"order": ["prov-a"], "allow_fallbacks": False}
                            for _, _, raw in Upstream.seen))
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["served_by"], {"m-1": ["prov-a"]})
        trials = {}
        for d in os.listdir(job_dir):
            if "__u" in d:
                with open(os.path.join(job_dir, d, "result.json")) as f:
                    trials[d.split("__")[0]] = json.load(f)["standard"]
        n = len(Upstream.seen)
        self.assertEqual(trials["model"]["served"], {"calls": {"Prov A": n}, "unnamed": 0, "held": {"m-1": True}})
        self.assertEqual(trials["good"]["served"], {"calls": {}, "unnamed": 0, "held": {"m-1": None}})   # it called no model
        with open(os.path.join(job_dir, "model__u0002", "agent", "calls.jsonl")) as f:
            calls = [json.loads(line) for line in f]
        self.assertTrue(all(c["request"]["routing"]["order"] == ["prov-a"] and c["reply"]["served_by"] == "Prov A" for c in calls))
        with open(os.path.join(job_dir, "result.json")) as f:
            self.assertEqual(json.load(f)["served"], {"calls": {"Prov A": n}, "unnamed": 0, "held": {"m-1": True}})

    def test_a_run_without_the_option_sends_records_and_ends_as_before(self):
        """Every request as before, the record's earlier fields as they were read, and the same verdicts, even
        when the provider answers a 200 that holds an error: the relay reads that error beside the reply and the
        unit ends as it did."""
        b = dict(self.b, datasets=[{"id": "responding", "units": 3}])
        reply = dict({k: v for k, v in CHAT.items() if k != "provider"}, error={"code": 502, "message": "Provider returned error"})
        Upstream.always = (200, "application/json", json.dumps(reply).encode())
        job_dir = run.run(b, self.a, model="m-1", provider="custom", env={"MODEL_API_URL": self.base, "MODEL_API_KEY": "k"},
                          unsandboxed=True, jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, dataset="responding")
        self.assertTrue(Upstream.seen)
        for _, headers, raw in Upstream.seen:
            self.assertNotIn("provider", json.loads(raw))
            self.assertNotIn("x-openrouter-metadata", headers)
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertNotIn("served_by", json.load(f)["standard"])
        with open(os.path.join(job_dir, "model__u0002", "result.json")) as f:
            res = json.load(f)
        self.assertNotIn("exception_info", res)                  # a 200 is no failure: the unit's verdict stands
        self.assertEqual(res["verifier_result"]["rewards"]["reward"], 0.0)
        self.assertNotIn("served", res["standard"])               # no call answered, no pin: nothing to say
        with open(os.path.join(job_dir, "model__u0002", "agent", "calls.jsonl")) as f:
            calls = [json.loads(line) for line in f]
        for c in calls:
            self.assertEqual((c["status"], "error" in c), (200, False))
            self.assertEqual(without_new(c["reply"]), relay.read_reply("/chat/completions", json.dumps(reply).encode(), False))
            self.assertEqual(c["reply"]["error"], {"code": 502, "message": "Provider returned error"})
            self.assertNotIn("routing", c["request"])

    def test_the_summary_counts_every_route_of_the_unit(self):
        """A judge on the pinned model, served by another provider than the agent's: the pin does not hold."""
        r = self.make(judge_model="m-1", served_by={"m-1": ["prov-a"]})
        call = lambda who: {"model": "m-1", "status": 200, "reply": {"served_by": who}}
        routes = {"u0000": [call("Prov A")], "u0000-judge": [call("Prov Z")], "u0000-simulator": [call("Prov A")]}
        rr = mock.Mock(mcp_listeners=[], unit_image="")
        rr.calls.side_effect = lambda tag: routes.get(tag, [])
        driver = mock.Mock()
        driver.request.side_effect = lambda op, **kw: {} if op == "setup" else {"reward": 1}
        with contextlib.ExitStack() as stack:
            for name in ("phase", "said_unit", "note_refusal"):
                stack.enter_context(mock.patch.object(r, name))
            stack.enter_context(mock.patch.object(r, "driver", return_value=driver))
            stack.enter_context(mock.patch.object(r, "unit_relay", return_value=rr))
            stack.enter_context(mock.patch.object(r, "drive_agent", return_value=("answer", None, {})))
            stack.enter_context(mock.patch.object(run.subprocess, "run"))
            stack.enter_context(mock.patch.object(run.progress, "ECHO", None))
            res = r.unit(0, "good")
        self.assertEqual(res["standard"]["served"], {"calls": {"Prov A": 2, "Prov Z": 1}, "unnamed": 0, "held": {"m-1": "unknown"}})
        self.assertEqual(res["agent_result"]["n_model_calls"], 1)          # the tokens and cost: the agent's calls, as before

    def test_the_benchmarks_own_relay_counts_in_the_job(self):
        r = self.make(served_by={"m-1": ["prov-a"]})
        folder = os.path.join(r.job_dir, ".work", "relay-benchmark")
        os.makedirs(folder)
        with open(os.path.join(folder, "embed-1.jsonl"), "w") as f:
            f.write(json.dumps({"model": "m-1", "status": 200, "reply": {"served_by": "Prov Z"}}) + "\n")
        bench = job.served(r.benchmark_calls(), r.serving)
        job.write_result(r.job_dir, r.name, 0, 1, [{"standard": {"served": {"calls": {"Prov A": 2}, "unnamed": 0, "held": {"m-1": True}}}}],
                         1, served=bench)
        with open(os.path.join(r.job_dir, "result.json")) as f:
            self.assertEqual(json.load(f)["served"], {"calls": {"Prov A": 2, "Prov Z": 1}, "unnamed": 0, "held": {"m-1": "unknown"}})

    def test_the_containers_unit_is_given_the_runs_pin(self):
        with mock.patch.object(container, "unusable", return_value=None):
            r = run.Run(self.b, self.a, model="m-1", provider="openrouter", env={"OPENROUTER_API_KEY": "or-test"}, backend="container",
                        jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, served_by={"m-1": ["prov-a"]})
        r.image, r.setup_driver, r.licences = "img", None, run.Licences(self.b, given={})
        trial = os.path.join(self.tmp, "trial")
        with mock.patch.object(container, "Unit") as unit, mock.patch.object(run.Run, "unit_image", return_value="img"):
            r.unit_relay("u0000", trial, {})
        self.assertEqual(unit.call_args.kwargs["serving"], {"m-1": ["prov-a"]})


class TheRecord(unittest.TestCase):
    def call(self, model, who=None, status=200, error=None):
        reply = {**({"served_by": who} if who else {}), **({"error": error} if error else {})}
        return {"model": model, "status": status, "reply": reply}

    def test_who_served_a_units_calls_and_whether_the_pin_held(self):
        pin = {"m-1": ["google-ai-studio", "prov-b/fp8"], "m-judge": ["prov-c"]}
        self.assertEqual(job.served([self.call("m-1", "Google AI Studio"), self.call("m-1", "Prov B"), self.call("m-1", status=429)], pin),
                         {"calls": {"Google AI Studio": 1, "Prov B": 1}, "unnamed": 0, "held": {"m-1": True, "m-judge": None}})
        self.assertEqual(job.served([self.call("m-1", "Prov Z"), self.call("m-1")], pin)["held"]["m-1"], "unknown")
        self.assertEqual(job.served([self.call("m-1", "Prov B", error={"message": "x"})], pin)["calls"], {})   # failed: not answered
        self.assertEqual(job.served([self.call("m-1", "Prov A")], None), {"calls": {"Prov A": 1}, "unnamed": 0})
        self.assertIsNone(job.served([self.call("m-1", status=429)], None))

    def test_a_name_reads_as_a_slug_by_its_case_and_spaces_alone(self):
        pin = {"m-1": ["prov-a"]}
        for name, held in (("Prov A", True), ("prov-a", True), ("PROV  A", True), ("Prov/A", "unknown"), ("Prov_A", "unknown"),
                           ("Prov.A", "unknown"), ("Prov Ab", "unknown")):
            with self.subTest(name=name):
                self.assertEqual(job.served([self.call("m-1", name)], pin)["held"]["m-1"], held)

    def test_answered_calls_that_name_no_provider_are_counted(self):
        known, unknown = job.served([self.call("m-1", "Prov A")]), job.served([self.call("m-1")])
        self.assertEqual(unknown, {"calls": {}, "unnamed": 1})
        self.assertEqual(job.served_total([{"standard": {"served": known}}, {"standard": {"served": unknown}}, {"standard": {}}]),
                         {"calls": {"Prov A": 1}, "unnamed": 1})
        parts = [{"standard": {"served": {"calls": {"Prov A": 2}, "unnamed": 1, "held": {"m-1": True}}}},
                 {"standard": {"served": {"calls": {"Prov A": 1, "Prov B": 1}, "unnamed": 0, "held": {"m-1": "unknown"}}}}]
        self.assertEqual(job.served_total(parts), {"calls": {"Prov A": 3, "Prov B": 1}, "unnamed": 1, "held": {"m-1": "unknown"}})


class TheReview(unittest.TestCase):
    """What a review of the pin found, each a rule of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_provider_of_the_replys_end_is_recorded_never_an_earlier_attempts(self):
        events = [{"type": "response.created", "response": {"status": "in_progress", "openrouter_metadata": {
                      "attempts": [{"provider": "Prov A", "status": 503}]}}},
                  {"type": "response.completed", "response": {"status": "completed", "output": [], "provider": "Prov B"}}]
        facts = relay.reply_facts("/responses", sse(*events), True)
        held = job.served([{"status": 200, "model": "m", "reply": facts}], {"m": ["prov-a"]})["held"]["m"]
        self.assertEqual((facts["served_by"], held), ("Prov B", "unknown"))

    def test_a_last_attempt_that_names_no_provider_takes_no_earlier_name(self):
        body = {"choices": [{"message": {"content": "ok"}}], "openrouter_metadata": {"attempts": [
            {"provider": "Prov A", "status": 503}, {"provider": 7, "status": 200}]}}
        self.assertNotIn("served_by", relay.reply_facts("/chat/completions", json.dumps(body).encode(), False))
        stream = sse({"provider": "Prov A", "choices": [{"index": 0, "delta": {"content": "o"}}]},
                     {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                      "openrouter_metadata": {"attempts": [{"provider": "Prov A", "status": 503}, {"status": 200}]}})
        self.assertNotIn("served_by", relay.reply_facts("/chat/completions", stream, True))

    def test_an_error_keeps_its_stated_fields_alone(self):
        nested = relay.reply_facts("/chat/completions", json.dumps({"error": {"api_key": ["review-only-credential"]}}).encode(), False)
        self.assertNotIn("review-only-credential", json.dumps(nested))
        self.assertEqual(relay.reply_facts("/messages", b'{"type":"message","content":[],"error":{"code":[],"type":{},"message":17}}', False), {})
        self.assertEqual(relay.reply_facts("/chat/completions", b'{"error":{"code":502,"message":"m","detail":{"key":"x"}}}', False),
                         {"error": {"code": 502, "message": "m"}})

    def test_of_several_choices_errors_the_top_level_one_else_the_first_is_kept(self):
        two = [{"index": 0, "finish_reason": "error", "error": {"code": 429, "message": "rate limited"}},
               {"index": 1, "finish_reason": "error", "error": {"code": 503, "message": "unavailable"}}]
        self.assertEqual(relay.reply_facts("/chat/completions", json.dumps({"choices": two}).encode(), False)["error"],
                         {"code": 429, "message": "rate limited", "choice": 0})
        top = {"choices": two, "error": {"code": 500, "message": "whole"}}
        self.assertEqual(relay.reply_facts("/chat/completions", json.dumps(top).encode(), False)["error"], {"code": 500, "message": "whole"})

    def test_a_merged_job_keeps_each_shards_whole_summary(self):
        """A shard's summary holds its benchmark relay's calls too: a merge made of its trials alone would drop
        them, and an ``unknown`` would read true."""
        pin = {"m": ["prov-a"]}
        trial = {"standard": {"served": job.served([{"status": 200, "model": "m", "reply": {"served_by": "Prov A"}}], pin), "shard_job": "s1"}}
        bench = job.served([{"status": 200, "model": "m", "reply": {"served_by": "Prov B"}}], pin)
        shard = {"name": "s1", "config": {"standard": {"served_by": pin}}, "result": {"served": job.served_total([trial], bench)}}
        self.assertEqual(merge._served([shard], [trial]), shard["result"]["served"])
        old = {"name": "s2", "config": {"standard": {"served_by": pin}}, "result": {}}          # a shard written before: its trials
        self.assertEqual(merge._served([old], [dict(trial, standard=dict(trial["standard"], shard_job="s2"))])["held"], {"m": True})

    def test_a_control_is_refused_a_pin_on_its_simulated_users_model(self):
        b = dict(definitions.get("benchmark", "fakebench", ROOT), controls={"nop": {"source": "nullius", "from": "nothing"}},
                 simulator={"model": "sim"})
        with self.assertRaisesRegex(run.Refused, "names sim, which this run does not call"):
            run.Run(b, definitions.control(b, "nop"), model="sim", provider="openrouter", env={},
                    jobs_root=self.tmp, say=lambda _: None, served_by={"sim": ["prov-a"]})

    def test_the_relays_own_command_refuses_another_provider_and_a_model_it_does_not_send(self):
        key = os.path.join(self.tmp, "key")
        with open(key, "w") as f:
            f.write("review-test-key")
        for base, model, extra in (("https://api.openai.com/v1", "m", []), ("https://openrouter.ai/api/v1", "unused", []),
                                   ("http://127.0.0.1:9/v1", "m", [])):
            with self.subTest(base=base, model=model):
                out = io.StringIO()
                with mock.patch.object(relay, "Relay") as fake, redirect_stdout(out):
                    code = relay.serve(["--base", base, "--model", "m", "--key-file", key, "--record", self.tmp,
                                        "--served-by", model + "=prov-a", *extra])
                self.assertEqual(code, 2)
                self.assertIn("--served-by", json.loads(out.getvalue())["error"])
                fake.assert_not_called()


class TheMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls={"nop": {"source": "nullius", "from": "nothing said"}})

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shards(self, *pins):
        jobs = [run.run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=os.path.join(self.tmp, "jobs"),
                        env={}, say=lambda s: None, shard=(i, len(pins)), launch="L", name=f"run-shard-{i}of{len(pins)}")
                for i in range(1, len(pins) + 1)]
        for job_dir, pin in zip(jobs, pins):
            path = os.path.join(job_dir, "config.json")
            with open(path) as f:
                cfg = json.load(f)
            cfg["standard"]["served_by"] = pin
            with open(path, "w") as f:
                json.dump(cfg, f)
        return jobs

    def test_shards_of_different_pins_are_refused(self):
        jobs = self.shards({"m-1": ["prov-a"]}, {"m-1": ["prov-b"]})
        with self.assertRaisesRegex(merge.Refused, "serving providers"):
            merge.check(jobs)
        self.assertEqual(merge.check(jobs, force=True)["differ"][0].split(":")[0], "serving providers")
        jobs = self.shards({"m-1": ["prov-a", "prov-b"]}, {"m-1": ["prov-b", "prov-a"]})       # the order is the pin's
        with self.assertRaisesRegex(merge.Refused, "serving providers"):
            merge.check(jobs)

    def test_equal_pins_merge_whatever_the_order_of_their_models(self):
        jobs = self.shards({"a": ["prov-a"], "b": ["prov-b"]}, {"b": ["prov-b"], "a": ["prov-a"]})
        self.assertEqual(merge.check(jobs)["differ"], [])

    def test_a_forced_merge_of_different_pins_holds_no_pin(self):
        jobs = self.shards({"m-1": ["prov-a"]}, {"m-1": ["prov-b"]})
        for job_dir, who in zip(jobs, ("Prov A", "Prov B")):
            trial = sorted(d for d in os.listdir(job_dir) if "__u" in d)[0]
            path = os.path.join(job_dir, trial, "result.json")
            with open(path) as f:
                res = json.load(f)
            res["standard"]["served"] = {"calls": {who: 1}, "unnamed": 0, "held": {"m-1": True}}
            with open(path, "w") as f:
                json.dump(res, f)
        out = os.path.join(self.tmp, "merged")
        merge.merge(jobs, out, force=True)
        with open(os.path.join(out, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["served_by"], {"m-1": ["prov-a"]})
        with open(os.path.join(out, "result.json")) as f:
            self.assertEqual(json.load(f)["served"], {"calls": {"Prov A": 1, "Prov B": 1}, "unnamed": 0, "held": {"m-1": "unknown"}})


if __name__ == "__main__":
    unittest.main()
