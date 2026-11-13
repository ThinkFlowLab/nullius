"""A pin of the serving providers that may answer a model (``--served-by``):
the relay puts OpenRouter's routing field on every request it sends as that
model and on no other, in place of the caller's own; each call records the
routing sent, the provider that served the reply, how the reply ended and an
error inside a success, which makes the call a failed one; the job and each
trial say who served their calls and whether the pin held; ``merge`` refuses
shards of different pins; a container's relay is given the pin as the unit's
own relay is. Without a pin a request goes on byte for byte as it did. Every
upstream here is a fake on this machine's loopback: no request leaves it."""
from __future__ import annotations

import io
import json
import os
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


def sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


# Each reply names its serving provider and how it ended, as OpenRouter's do: Chat Completions, Messages, Responses.
CHAT = {"id": "c1", "provider": "Prov A", "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
CHAT_SSE = sse({"id": "c1", "provider": "Prov A", "choices": [{"delta": {"content": "h"}, "finish_reason": None}]},
               {"id": "c1", "provider": "Prov A", "choices": [{"delta": {"content": "i"}, "finish_reason": "length"}]},
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


class Upstream(BaseHTTPRequestHandler):
    """A provider on the loopback: keeps every request as it came, bytes and headers, and answers it in its shape;
    ``Upstream.next`` replaces the next reply (status, content type, body)."""
    seen = []
    next = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
        req = json.loads(raw)
        if Upstream.next is not None:
            (status, kind, body), Upstream.next = Upstream.next, None
        else:
            path = "/" + self.path.split("/v1/", 1)[-1]
            reply = SHAPES[(path, bool(req.get("stream")))][0]
            status, kind, body = 200, "text/event-stream" if req.get("stream") else "application/json", \
                reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post(url: str, body: dict) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                                                                             "Authorization": "Bearer " + relay.PLACEHOLDER_KEY})
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
        Upstream.seen, Upstream.next = [], None
        self.relays = []

    def tearDown(self):
        for r in self.relays:
            r.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def relay(self, **kw):
        r = relay.Relay(self.base, "or-test", os.path.join(self.tmp, "rec"), **kw).start()
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

    def test_each_shape_streamed_or_not_says_who_served_it_and_how_it_ended(self):
        r = self.relay(model="m-run", serving={"m-run": ["prov-a"]})
        for (path, stream), (_, who, end) in SHAPES.items():
            with self.subTest(path=path, stream=stream):
                post(r.url("u0") + path, body_for(path, stream))
                self.assertEqual(self.sent()["provider"], {"order": ["prov-a"], "allow_fallbacks": False})
                reply = r.calls("u0")[-1]["reply"]
                self.assertEqual((reply.get("served_by"), reply.get("end"), reply.get("error")), (who, end, None))
                self.assertEqual(reply["text"], "hi" if path != "/responses" or not stream else "")

    def test_an_error_inside_a_200_is_recorded_and_makes_the_call_a_failed_one(self):
        r = self.relay(model="m-run")
        errors = [(b'{"error":{"code":502,"message":"Provider returned error"}}', "application/json",
                   {"code": 502, "message": "Provider returned error"}),
                  (sse({"id": "c1", "provider": "Prov A", "error": {"code": "server_error", "message": "upstream ended"},
                        "choices": [{"delta": {}, "finish_reason": "error"}]}), "text/event-stream",
                   {"code": "server_error", "message": "upstream ended"}),
                  (sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}), "text/event-stream",
                   {"type": "overloaded_error", "message": "Overloaded"})]
        for body, kind, said in errors:
            with self.subTest(said=said):
                Upstream.next = (200, kind, body)
                post(r.url("u0") + "/chat/completions", body_for("/chat/completions", kind == "text/event-stream"))
                c = r.calls("u0")[-1]
                self.assertEqual((c["status"], c["reply"]["error"]), (200, said))
                self.assertIn(said["message"], c["error"])
                self.assertEqual(relay.failure(r.calls("u0")), ("ApiError", c["error"]))
        Upstream.next = (200, "application/json", b'{"error":{"code":429,"message":"Rate limit exceeded"}}')
        post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False))
        self.assertEqual(relay.failure(r.calls("u0"))[0], "RateLimitError")
        post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False))     # answered: the unit recovered
        self.assertIsNone(relay.failure(r.calls("u0")))

    def test_a_pinned_chat_request_asks_for_the_routing_facts_and_reads_its_provider_there(self):
        r = self.relay(model="m-run", serving={"m-run": ["prov-a"]})
        Upstream.next = (200, "application/json", json.dumps(dict({k: v for k, v in CHAT.items() if k != "provider"},
                         openrouter_metadata={"attempts": [{"provider": "Prov X", "status": 503},
                                                           {"provider": "Prov A", "status": 200}]})).encode())
        post(r.url("u0") + "/chat/completions", body_for("/chat/completions", False))
        self.assertEqual(Upstream.seen[-1][1].get("x-openrouter-metadata"), "enabled")
        self.assertEqual(r.calls("u0")[-1]["reply"]["served_by"], "Prov A")
        post(r.url("u0") + "/messages", body_for("/messages", False))       # its reply names its provider: nothing asked
        self.assertNotIn("x-openrouter-metadata", Upstream.seen[-1][1])
        plain = self.relay(model="m-run")
        post(plain.url("u1") + "/chat/completions", body_for("/chat/completions", False))
        self.assertNotIn("x-openrouter-metadata", Upstream.seen[-1][1])

    def test_without_a_pin_every_request_goes_on_byte_for_byte_as_before(self):
        """What a relay sent before pins existed: the caller's body with the model set (and a streamed Chat
        Completions request's usage flag), serialised once; a pin on another model changes none of it."""
        for serving in (None, {"m-other": ["prov-a"]}):
            r = self.relay(model="m-run", serving=serving)
            for (path, stream) in SHAPES:
                for extra in ({}, {"provider": {"order": ["prov-z"]}}):
                    with self.subTest(serving=serving, path=path, stream=stream, extra=extra):
                        asked = dict(body_for(path, stream), **extra)
                        post(r.url("u0") + path, asked)
                        expected = dict(asked, model="m-run")
                        if stream and path == "/chat/completions":
                            expected.setdefault("stream_options", {})["include_usage"] = True
                        self.assertEqual(Upstream.seen[-1][2], json.dumps(expected).encode())
                        self.assertNotIn("x-openrouter-metadata", Upstream.seen[-1][1])


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

    def test_the_relay_as_a_process_of_its_own_puts_the_pin_on_its_requests(self):
        key = os.path.join(self.tmp, "key")
        with open(key, "w") as f:
            f.write("or-test")
        p = subprocess.Popen([sys.executable, "-m", "nullius.standard.relay", "serve", "--listen", "127.0.0.1:0", "--base", self.base,
                              "--record", os.path.join(self.tmp, "rec"), "--key-file", key, "--model", "m-run",
                              "--served-by", "m-run=prov-a,prov-b/fp8"],
                             cwd=APP, env=dict(os.environ, PYTHONPATH=APP), stdout=subprocess.PIPE, text=True)
        try:
            url = json.loads(p.stdout.readline())["listening"]
            post(url + "/u0/v1/chat/completions", body_for("/chat/completions", False))
            self.assertEqual(self.sent()["provider"], PIN)
        finally:
            p.terminate()
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

    def test_the_option_reads_model_and_slugs_and_refuses_what_it_cannot_take(self):
        self.assertEqual(run.served_by(["prov-a", "m-judge=prov-b/fp8, prov-c"], "m-run"),
                         {"m-run": ["prov-a"], "m-judge": ["prov-b/fp8", "prov-c"]})
        for given, said in ((["m-run=prov-a,"], "not a serving provider's slug"), (["m-run=a b"], "not a serving provider's slug"),
                            (["prov-a,prov-a"], "twice"), (["prov-a", "m-run=prov-b"], "m-run twice"),
                            (["prov-a"], "MODEL=SLUG")):
            with self.subTest(given=given), self.assertRaisesRegex(run.Refused, said):
                run.served_by(given, "m-run" if given != ["prov-a"] else "")

    def test_a_provider_that_routes_no_model_to_several_is_refused_before_any_unit(self):
        for provider, env in (("openai", {"OPENAI_API_KEY": "sk-x"}), ("anthropic", {"ANTHROPIC_API_KEY": "x"}),
                              ("custom", {"MODEL_API_URL": self.base})):
            with self.subTest(provider=provider), self.assertRaisesRegex(run.Refused, "no serving providers to choose among"):
                run.Run(self.b, self.a, model="m-1", provider=provider, env=env, unsandboxed=True,
                        jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, served_by={"m-1": ["prov-a"]})

    def test_a_model_the_run_does_not_call_is_refused_before_any_unit(self):
        with self.assertRaisesRegex(run.Refused, "names m-other, which this run does not call: it calls m-1"):
            run.Run(self.b, self.a, model="m-1", provider="openrouter", env={"OPENROUTER_API_KEY": "or-test"}, unsandboxed=True,
                    jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None, served_by={"m-1": ["prov-a"], "m-other": ["prov-b"]})

    def test_the_command_line_refuses_with_exit_2(self):
        for patch in (mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_other", return_value=self.a),
                      mock.patch.object(cli, "_exit_on_term"),
                      mock.patch.object(cli.settings, "load"), mock.patch.object(progress, "ECHO", None)):
            patch.start()
            self.addCleanup(patch.stop)
        for extra, said in ((["--provider", "openai", "--served-by", "prov-a"], "no serving providers"),
                            (["--provider", "openrouter", "--served-by", "m-2=prov-a"], "does not call"),
                            (["--provider", "openrouter", "--served-by", "m-1=bad slug"], "not a serving provider's slug")):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENROUTER_API_KEY": "or-test"}):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["run", "fakebench", "fake", "--model", "m-1", "--unsandboxed", "--jobs",
                                     os.path.join(self.tmp, "jobs"), *extra])
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
            cfg = json.load(f)
        self.assertEqual(cfg["standard"]["served_by"], {"m-1": ["prov-a"]})
        trials = {d.split("__")[0]: json.load(open(os.path.join(job_dir, d, "result.json")))["standard"]
                  for d in os.listdir(job_dir) if "__u" in d}
        n = len(Upstream.seen)
        self.assertEqual(trials["model"]["served"], {"calls": {"Prov A": n}, "unnamed": 0, "held": {"m-1": True}})
        self.assertEqual(trials["good"]["served"], {"calls": {}, "unnamed": 0, "held": {"m-1": None}})   # it called no model
        with open(os.path.join(job_dir, "model__u0002", "agent", "calls.jsonl")) as f:
            calls = [json.loads(line) for line in f]
        self.assertTrue(all(c["request"]["routing"]["order"] == ["prov-a"] and c["reply"]["served_by"] == "Prov A" for c in calls))
        with open(os.path.join(job_dir, "result.json")) as f:
            self.assertEqual(json.load(f)["served"], {"calls": {"Prov A": n}, "unnamed": 0, "held": {"m-1": True}})

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
    def test_who_served_a_units_calls_and_whether_the_pin_held(self):
        def call(model, who=None, status=200, error=None):
            reply = {**({"served_by": who} if who else {}), **({"error": error} if error else {})}
            return {"model": model, "status": status, "reply": reply}
        pin = {"m-1": ["google-ai-studio", "prov-b/fp8"], "m-judge": ["prov-c"]}
        self.assertEqual(job.served([call("m-1", "Google AI Studio"), call("m-1", "Prov B"), call("m-1", status=429)], pin),
                         {"calls": {"Google AI Studio": 1, "Prov B": 1}, "unnamed": 0, "held": {"m-1": True, "m-judge": None}})
        self.assertEqual(job.served([call("m-1", "Prov Z"), call("m-1")], pin)["held"]["m-1"], "unknown")
        self.assertEqual(job.served([call("m-1", "Prov B", error={"message": "x"})], pin)["calls"], {})   # failed: not answered
        self.assertEqual(job.served([call("m-1", "Prov A")], None), {"calls": {"Prov A": 1}, "unnamed": 0})
        self.assertIsNone(job.served([call("m-1")], None))
        parts = [{"standard": {"served": {"calls": {"Prov A": 2}, "unnamed": 1, "held": {"m-1": True}}}},
                 {"standard": {"served": {"calls": {"Prov A": 1, "Prov B": 1}, "unnamed": 0, "held": {"m-1": "unknown"}}}},
                 {"standard": {}}]
        self.assertEqual(job.served_total(parts), {"calls": {"Prov A": 3, "Prov B": 1}, "unnamed": 1, "held": {"m-1": "unknown"}})


class TheMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls={"nop": {"source": "nullius", "from": "nothing said"}})

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shards_of_different_pins_are_refused(self):
        jobs = [run.run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=os.path.join(self.tmp, "jobs"),
                        env={}, say=lambda s: None, shard=(i, 2), launch="L", name=f"run-shard-{i}of2") for i in (1, 2)]
        for job_dir, pin in zip(jobs, ({"m-1": ["prov-a"]}, {"m-1": ["prov-b"]})):
            path = os.path.join(job_dir, "config.json")
            with open(path) as f:
                cfg = json.load(f)
            cfg["standard"]["served_by"] = pin
            with open(path, "w") as f:
                json.dump(cfg, f)
        with self.assertRaisesRegex(merge.Refused, "serving providers"):
            merge.check(jobs)
        self.assertEqual(merge.check(jobs, force=True)["differ"][0].split(":")[0], "serving providers")


if __name__ == "__main__":
    unittest.main()
