"""Replay: one recorded model call of a trial sent again K times under a spending cap, as it was or changed, its
answers read and counted, everything kept in one folder outside the run's tree. The source trial is recorded
here by a relay against a fake endpoint on loopback, as a run records it; the replays go to the same endpoint.
No key, no network."""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nullius.standard import cli as standard_cli
from nullius.standard.calls import exchanges, relay
from nullius.standard.runner import replay

APP = Path(__file__).resolve().parents[1]
KEY = "sk-or-replay-012"                 # a stand-in in the redaction's key shape, shorter than a real key
PNG = b"\x89PNG\r\n\x1a\n" + hashlib.sha256(b"screen").digest() * 40
TOOLS = [{"name": "click", "description": "Click a point.", "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}}},
         {"name": "type", "description": "Type a text.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}]
SDK_HEADERS = {"User-Agent": "sdk/1.0", "anthropic-version": "2023-06-01", "X-Stainless-Lang": "python",
               "x-api-key": relay.PLACEHOLDER_KEY}


def messages_request(n: int) -> dict:
    """An agent's n-th call in the Messages form: a system text, two tools, a screenshot, the history so far."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Open the settings."},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                      "data": base64.b64encode(PNG).decode()}}]}]
    for i in range(1, n):
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "click", "input": {"x": i}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"clicked {i}"}]}]
    return {"model": "agent-asked", "max_tokens": 1000, "system": "You operate a desktop. Act with one tool call.",
            "tools": TOOLS, "messages": msgs}


def reply_of(x) -> dict:
    return {"type": "message", "role": "assistant", "stop_reason": "tool_use", "provider": "prov-a",
            "content": [{"type": "thinking", "thinking": "look", "signature": "s"},
                        {"type": "tool_use", "id": "t", "name": "click", "input": {"x": x}}] if x is not None else
                       [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 50, "output_tokens": Upstream.output, "cost": Upstream.cost}}


class Upstream(BaseHTTPRequestHandler):
    """A model: each request kept as it arrived; the answers are ``plan``'s x in turn (None: no tool call), or
    ``body`` as it is."""
    seen = []                                    # (path, headers, body)
    plan = [3]
    cost = 0.001                                 # the cost the reply reports; None, none: the relay prices its tokens
    output = 10
    status = 200
    body = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, dict(self.headers), raw))
        if Upstream.status != 200:
            return self.reply(Upstream.status, b'{"error":{"message":"no"}}')
        if Upstream.body is not None:
            return self.reply(200, json.dumps(Upstream.body).encode())
        body = json.loads(raw)
        if self.path.endswith("/responses"):
            return self.reply(200, json.dumps({"object": "response", "status": "completed", "output": [],
                                               "usage": {"input_tokens": 5, "output_tokens": 1}}).encode())
        x = Upstream.plan[(len(Upstream.seen) - 1) % len(Upstream.plan)]
        assert body["model"]
        self.reply(200, json.dumps(reply_of(x)).encode())

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def tree(root) -> dict:
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class ReplayCase(unittest.TestCase):
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
        self.tmp = tempfile.mkdtemp(prefix="nullius-replay-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Upstream.seen, Upstream.plan, Upstream.cost, Upstream.output, Upstream.status, Upstream.body = [], [3], 0.001, 10, 200, None
        self.job = os.path.join(self.tmp, "jobs", "2026-11-18__05-58-04__bench__agent")
        self.env = {"OPENROUTER_API_KEY": KEY, "PATH": os.environ.get("PATH", "")}
        self.record_trial("task-a__u0001", calls=3)
        with open(os.path.join(self.job, "config.json"), "w") as f:
            json.dump({"job_name": "j", "standard": {"provider": "openrouter", "served_by": {"m": ["prov-a"]},
                                                      "prices": {"models": {"m": {"input": 1.0, "output": 2.0, "source": "run"}}},
                                                      "exchanges": "whole"},
                       "agents": [{"name": "agent", "model_name": "m"}]}, f)
        self.original = list(Upstream.seen)
        Upstream.seen = []

    def record_trial(self, name: str, calls: int, mode: str = "whole", path: str = "/messages", req=None) -> str:
        """A trial as a run leaves it: its unit's relay, saving its exchanges, sent ``calls`` calls."""
        tdir = os.path.join(self.job, name)
        r = relay.Relay(self.base, KEY, os.path.join(tdir, ".work", "relay"), prices={"m": {"input": 1.0, "output": 2.0}},
                        model="m", serving={"m": ["prov-a"]}, exchanges=mode).start()
        try:
            tag = name.rsplit("__", 1)[1]
            for n in range(1, calls + 1):
                body = json.dumps(req or messages_request(n)).encode()
                c = http.client.HTTPConnection("127.0.0.1", r.server.server_address[1], timeout=10)
                c.request("POST", f"/{tag}/v1{path}", body=body, headers={**SDK_HEADERS, "Content-Type": "application/json"})
                c.getresponse().read()
                c.close()
        finally:
            r.stop()
        return tdir

    def prepared(self, *changes, call=2, trial="task-a__u0001", prices=None):
        return replay.prepare(replay.source(self.job, trial, call), list(changes), prices=prices, endpoint=self.base, env=self.env)

    def send(self, prep, times=2, max_usd=1.0, **kw):
        return replay.send(prep, times, max_usd, os.path.join(self.tmp, "out", str(len(Upstream.seen))), env=self.env, **kw)


class KeylessCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.job = self.root / "local-job"
        self.env = {"MODEL_API_URL": "http://localhost:8000/v1"}
        self.trial = "local-task__u0001"
        self.request = dict(messages_request(1), model="local-model", system="localhost")
        self.body = json.dumps(dict(reply_of("localhost"), provider="localhost")).encode()
        store = exchanges.Store(str(self.job / self.trial / ".work" / "relay"))
        store.write("u0001", 1, 1.0, "/messages", self.request, json.dumps(self.request).encode(), {}, 200, False, self.body)
        (self.job / "config.json").write_text(json.dumps({"standard": {"provider": "custom",
            "prices": {"models": {"local-model": {"input": 1.0, "output": 2.0}}}}}))
        self.prep = replay.prepare(replay.source(str(self.job), self.trial, 1), [], env=self.env)
        self.enterContext(mock.patch.object(relay.Relay, "start", lambda r: r))
        self.post = self.enterContext(mock.patch.object(replay, "post", side_effect=lambda r, p, tag: (
            dict(replay.answer(p.source.path, 200, self.body, False), route=tag, refused=None,
                 sent_sha256=p.sha256, cost_usd=0.001), {})))

    def assert_no_redaction(self, out, doc, log):
        self.assertNotIn("[REDACTED]", json.dumps(doc) + "".join(log))
        self.assertTrue(log[-1].startswith(str(out) + ": "))
        for p in out.rglob("*"):
            if p.is_file():
                self.assertNotIn(b"[REDACTED]", p.read_bytes())


class Keyless(KeylessCase):
    def test_a_keyless_replay_keeps_localhost_in_files_labels_and_messages(self):
        out, log = self.root / "local-replay", []
        doc = replay.send(self.prep, 1, 1.0, str(out), env=self.env, say=log.append,
                          rule=lambda a: json.loads(a["tool_calls"][0]["arguments"])["x"])
        self.assert_no_redaction(out, doc, log)
        self.assertEqual(doc["request"]["endpoint"], self.env["MODEL_API_URL"])
        self.assertEqual(doc["request"]["model"], "local-model")
        self.assertEqual(doc["recorded_answer"], "localhost")
        self.assertEqual(doc["counts"], [{"label": "localhost", "count": 1, "recorded": True}])
        row = json.loads((out / "answers.jsonl").read_text())
        self.assertEqual((row["body"], row["served_by"], row["label"]), (self.body.decode(), "localhost", "localhost"))
        self.assertEqual((out / "request.json").read_bytes(), self.prep.raw)
        self.assertEqual(self.post.call_args.args[0].base, self.env["MODEL_API_URL"])


class Rebuilt(ReplayCase):
    def test_source_links_cannot_escape_the_job_or_trial(self):
        trial = Path(self.job, "task-a__u0001")
        outside = Path(self.tmp, "outside")
        (trial / "unused-link").symlink_to(outside)
        self.assertEqual(replay.source(self.job, trial.name, 1).model, "m")
        (trial / "unused-link").unlink()
        trial.rename(outside)
        trial.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(replay.Refused, "outside its folder"):
            replay.source(self.job, trial.name, 1)
        trial.unlink()
        outside.rename(trial)
        store = trial / ".work" / "relay" / "exchanges"
        store.rename(outside)
        store.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(replay.Refused, "outside its folder"):
            replay.calls(self.job, trial.name)

    def test_a_request_rebuilt_and_sent_unchanged_is_the_recorded_one_byte_for_byte(self):
        """A request rebuilt and sent equals the recorded one, byte for byte, when nothing is changed."""
        src = replay.source(self.job, "task-a__u0001", 2)
        recorded = self.original[1][2]
        self.assertEqual(src.sent_sha256, hashlib.sha256(recorded).hexdigest())
        prep = replay.prepare(src, [], endpoint=self.base, env=self.env)
        self.assertEqual(prep.raw, recorded)
        self.assertEqual((prep.model, prep.serving, prep.key_header), ("m", {"m": ["prov-a"]}, "x-api-key"))
        doc = self.send(prep, times=2)
        self.assertEqual([raw for _, _, raw in Upstream.seen], [recorded, recorded])
        self.assertEqual([p for p, _, _ in Upstream.seen], ["/v1/messages"] * 2)
        for _, headers, _ in Upstream.seen:          # the relay's key, the agent's own headers as recorded
            self.assertEqual(headers["x-api-key"], KEY)
            self.assertEqual((headers["User-Agent"], headers["X-Stainless-Lang"]), ("sdk/1.0", "python"))
        self.assertTrue(doc["sent_as_prepared"])
        self.assertTrue(doc["request"]["unchanged"])
        self.assertEqual(doc["changes"], [])

    def test_a_call_not_whole_in_the_record_is_refused_with_a_plain_message(self):
        """A call whose request is cut or missing is refused with a plain message."""
        with self.assertRaisesRegex(replay.NotFound, "no call 9"):
            replay.source(self.job, "task-a__u0001", 9)
        self.record_trial("task-b__u0002", calls=1, mode="text")
        with self.assertRaisesRegex(replay.NotWhole, "a store kept as text holds no pictures"):
            replay.source(self.job, "task-b__u0002", 1)
        blobs = os.path.join(self.job, "task-a__u0001", ".work", "relay", "exchanges", "blobs")
        shutil.rmtree(blobs)
        with self.assertRaisesRegex(replay.NotWhole, r"call 1 of task-a__u0001 cannot be sent again as it was: .*picture"):
            replay.source(self.job, "task-a__u0001", 1)
        lines = os.path.join(self.job, "task-a__u0001", ".work", "relay", "exchanges", "u0001.jsonl")
        rows = [json.loads(x) for x in Path(lines).read_text().splitlines()]
        Path(lines).write_text("".join(json.dumps(dict(x, sent=None, cut="the store reached its bound of 1 MB") if x.get("n") == 3 else x) + "\n"
                                       for x in rows))
        with self.assertRaisesRegex(replay.NotWhole, "call 3 was not kept: the store reached its bound"):
            replay.source(self.job, "task-a__u0001", 3)
        os.makedirs(os.path.join(self.job, "task-c__u0003"))
        with self.assertRaisesRegex(replay.NotFound, "saved no exchanges"):
            replay.source(self.job, "task-c__u0003", 1)
        with self.assertRaisesRegex(replay.NotFound, "no trial named"):
            replay.source(self.job, "task-z__u0009", 1)
        with self.assertRaisesRegex(replay.NotFound, "not a trial's name"):
            replay.source(self.job, "../jobs", 1)

    def test_the_list_says_each_call_its_size_and_whether_its_request_is_whole(self):
        """What a trial holds that can be replayed: the calls, their numbers, sizes and whether whole."""
        got = replay.calls(self.job, "task-a__u0001")
        self.assertEqual([c["n"] for c in got], [1, 2, 3])
        self.assertTrue(all(c["whole"] and c["status"] == 200 and c["model"] == "m" for c in got))
        self.assertEqual([c["bytes"] for c in got], [len(raw) for _, _, raw in self.original])
        os.remove(next(Path(self.job, "task-a__u0001", ".work", "relay", "exchanges", "blobs").iterdir()))
        self.assertFalse(any(c["whole"] for c in replay.calls(self.job, "task-a__u0001")))


class Changes(ReplayCase):
    def test_responses_text_and_system_inputs_accept_prompt_changes(self):
        self.record_trial("text__u0002", 1, path="/responses", req={"model": "m", "input": "Open settings."})
        prep = self.prepared(replay.replace_text("settings", "browser"), trial="text__u0002", call=1)
        self.assertEqual(prep.request["input"], "Open browser.")
        self.assertChain(prep, ["replace_text"])
        for role in ("system", "developer"):
            with self.subTest(role=role):
                req = {"input": [{"role": role, "content": [{"type": "input_text", "text": "Be brief."}]},
                                 {"role": "user", "content": "Open settings."}]}
                changed = replay.replace_text("brief", "careful").apply(req)
                self.assertEqual(changed["input"][0]["content"][0]["text"], "Be careful.")
                changed = replay.system_text("Be concise.").apply(changed)
                self.assertEqual(changed["input"][0]["content"], "Be concise.")
                self.assertEqual(changed["input"][1]["content"], "Open settings.")

    def test_stream_usage_is_in_the_prepared_bytes_and_change_hash(self):
        self.record_trial("chat__u0002", 1, path="/chat/completions", req={
            "model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "Open settings."}]})
        prep = self.prepared(replay.set_field("stream", True), trial="chat__u0002", call=1)
        self.assertEqual(prep.request["stream_options"], {"include_usage": True})
        self.assertChain(prep, ["set_field"])
        Upstream.seen = []
        doc = self.send(prep, times=1)
        self.assertTrue(doc["sent_as_prepared"])
        self.assertEqual(Upstream.seen[0][2], Path(doc["out"], "request.json").read_bytes())
        with self.assertRaisesRegex(replay.Refused, "stream_options must be a JSON object"):
            self.prepared(replay.set_field("stream", True), replay.set_field("stream_options", "invalid"),
                          trial="chat__u0002", call=1)

    def test_endpoints_with_credentials_are_refused_without_showing_them(self):
        src = replay.source(self.job, "task-a__u0001", 2)
        for endpoint in (self.base + "?api_key=" + KEY, "http://user:" + KEY + "@localhost/v1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(replay.Refused) as caught:
                replay.prepare(src, endpoint=endpoint, env=self.env)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertIn("endpoint", str(caught.exception))
        self.assertEqual(Upstream.seen, [])
        self.assertFalse(Path(self.tmp, "out").exists())

    def test_commands_receive_neither_key_names_nor_value_aliases(self):
        env = dict(self.env, API_TOKEN=KEY, API_KEY_TYPESAFE=KEY, PUBLIC_VALUE="kept")
        script = Path(self.tmp, "environment.py")
        script.write_text("import json, os, sys\njson.load(sys.stdin)\njson.dump(dict(os.environ), sys.stdout)\n")
        command = f"{sys.executable} {script}"
        for supplied in (env, None):
            with self.subTest(supplied=supplied is not None), mock.patch.dict(os.environ, env, clear=True):
                changed = replay.through(command, env=supplied).apply({})
                read = json.loads(replay.command_rule(command, env=supplied)({"status": 200}))
                for got in (changed, read):
                    self.assertNotIn(KEY, got.values())
                    self.assertNotIn("OPENROUTER_API_KEY", got)
                    self.assertNotIn("API_KEY_TYPESAFE", got)
                    self.assertEqual(got["PUBLIC_VALUE"], "kept")

    def assertChain(self, prep, names):
        self.assertEqual([c["name"] for c in prep.changes], names)
        self.assertEqual(prep.changes[0]["sha256_before"], prep.source.sent_sha256)
        for a, b in zip(prep.changes, prep.changes[1:]):
            self.assertEqual(a["sha256_after"], b["sha256_before"])
        self.assertEqual(prep.changes[-1]["sha256_after"], hashlib.sha256(prep.raw).hexdigest())
        for c in prep.changes:
            self.assertNotEqual(c["sha256_before"], c["sha256_after"], c["name"])

    def test_each_change_does_what_it_names_and_is_recorded_with_both_hashes(self):
        """Each change does what it names and is recorded with both hashes."""
        prep = self.prepared(replay.replace_text("one tool call", "one or two tool calls"),
                             replay.replace_text("clicked 1", "clicked once"),
                             replay.tool_description("type", "Type a text, then press Enter."),
                             replay.set_field("temperature", 0), replay.set_field("max_tokens", None), call=3)
        req = prep.request
        self.assertEqual(req["system"], "You operate a desktop. Act with one or two tool calls.")
        self.assertEqual(req["messages"][2]["content"][0]["content"], "clicked once")
        self.assertEqual([t["description"] for t in req["tools"]], ["Click a point.", "Type a text, then press Enter."])
        self.assertEqual(req["temperature"], 0)
        self.assertNotIn("max_tokens", req)
        self.assertEqual(req["messages"][0]["content"][1], prep.source.request["messages"][0]["content"][1], "the picture as recorded")
        self.assertChain(prep, ["replace_text", "replace_text", "tool_description", "set_field", "set_field"])
        self.assertEqual(prep.changes[0]["args"], {"old": "one tool call", "new": "one or two tool calls"})

        prep = self.prepared(replay.system_text("Be brief."), replay.tools(TOOLS[:1]), replay.drop_last(2), call=3)
        self.assertEqual((prep.request["system"], [t["name"] for t in prep.request["tools"]], len(prep.request["messages"])),
                         ("Be brief.", ["click"], 3))
        self.assertChain(prep, ["system_text", "tools", "drop_last"])

    def test_another_model_pin_or_provider_changes_where_it_goes(self):
        """Another model, another serving pin, another provider: recorded, and the pin follows."""
        other = {"other/model": {"input": 3.0, "output": 4.0}}
        prep = self.prepared(replay.model("other/model"), replay.served_by(["prov-b", "prov-c"]), prices=other)
        self.assertEqual((prep.model, prep.request["provider"], prep.serving, prep.price),
                         ("other/model", {"order": ["prov-b", "prov-c"], "allow_fallbacks": False}, {"other/model": ["prov-b", "prov-c"]},
                          other["other/model"]))
        self.assertChain(prep, ["model", "served_by"])
        prep = self.prepared(replay.model("other/model"), prices=other)
        self.assertEqual(prep.serving, {})
        self.assertNotIn("provider", prep.request)
        prep = self.prepared(replay.provider("anthropic"))
        self.assertEqual((prep.provider, prep.serving, prep.key_setting), ("anthropic", {}, "ANTHROPIC_API_KEY"))
        self.assertNotIn("provider", prep.request)
        self.assertEqual(self.prepared(replay.served_by([])).serving, {})
        with mock.patch.object(replay.price_list, "table", return_value={}), \
                self.assertRaisesRegex(replay.Refused, "no price for the model 'other/model'"):
            self.prepared(replay.model("other/model"))

    def test_a_change_that_does_not_apply_is_refused(self):
        for change, said in ((replay.replace_text("absent words", "x"), "appears 0 times"),
                             (replay.replace_text("clicked", "x"), "appears 2 times"),
                             (replay.tool_description("scroll", "x"), "0 tools named 'scroll'"),
                             (replay.drop_last(5), "dropping 5 leaves none")):
            with self.subTest(change=change.name, said=said), self.assertRaisesRegex(replay.Refused, said):
                self.prepared(change, call=3)
        for make, said in ((lambda: replay.set_field("model", "x"), "changes of their own"),
                           (lambda: replay.drop_last(0), "at least 1"), (lambda: replay.served_by(["a b"]), "slug"),
                           (lambda: replay.provider("nowhere"), "not one of")):
            with self.subTest(said=said), self.assertRaisesRegex(replay.Refused, said):
                make()

    def test_a_command_rebuilds_the_request_without_the_key(self):
        """``through``: the request passed through the user's command, recorded by its hashes; the command has no key."""
        script = os.path.join(self.tmp, "builder.py")
        Path(script).write_text("import json, os, sys\nr = json.load(sys.stdin)\n"
                                "r['metadata'] = {'key': os.environ.get('OPENROUTER_API_KEY', 'none')}\n"
                                "json.dump(r, sys.stdout)\n")
        prep = self.prepared(replay.through(f"{sys.executable} {script}", env=self.env))
        self.assertEqual(prep.request["metadata"], {"key": "none"})
        self.assertChain(prep, ["through"])
        with self.assertRaisesRegex(replay.Refused, "printed no JSON object"):
            self.prepared(replay.through("echo nothing"))

    def test_a_stored_conversation_is_refused(self):
        """A request that names a conversation stored at the provider is refused."""
        self.record_trial("task-r__u0004", calls=1, path="/responses",
                          req={"model": "m", "previous_response_id": "resp_1", "input": [{"role": "user", "content": "go on"}]})
        src = replay.source(self.job, "task-r__u0004", 1)
        with self.assertRaisesRegex(replay.Refused, r"conversation stored at the provider \(previous_response_id\)"):
            replay.prepare(src, [], endpoint=self.base, env=self.env)
        with self.assertRaisesRegex(replay.Refused, r"\(conversation\)"):
            self.prepared(replay.set_field("conversation", "conv_1"))


class Sends(ReplayCase):
    def test_whole_replies_survive_the_exchange_store_limit(self):
        from nullius.standard.runner import study
        original = exchanges.Store.__init__
        body = dict(reply_of(3), extra="whole-provider-field" + "x" * 600000)

        def small_store(store, record_dir, mode=exchanges.DEFAULT, max_mb=exchanges.MAX_MB, keys=()):
            original(store, record_dir, mode, 1, keys)

        def large_reply(handler):
            raw = handler.rfile.read(int(handler.headers["Content-Length"]))
            Upstream.seen.append((handler.path, dict(handler.headers), raw))
            handler.reply(200, json.dumps(body).encode())

        with mock.patch.object(exchanges.Store, "__init__", small_store), mock.patch.object(Upstream, "do_POST", large_reply):
            doc = self.send(self.prepared(), times=2)
        rows = [json.loads(x) for x in Path(doc["out"], "answers.jsonl").read_text().splitlines()]
        self.assertEqual([json.loads(a["body"]) for a in rows], [body, body])
        self.assertIn("cut", exchanges.lines(str(Path(doc["out"], "relay", "exchanges")), "r002")[0])
        again = study.recount(doc["out"], rule=lambda a: json.loads(a["body"]).get("extra", "recorded"))
        self.assertEqual((again["read"], again["not_saved"]), (2, 0))
        self.assertEqual(len(Upstream.seen), 2)

    def test_echoed_credentials_are_removed_from_answers_and_labels(self):
        credential = "active-credential-without-a-provider-prefix"
        self.env["OPENROUTER_API_KEY"] = credential
        Upstream.plan = [credential]
        log = []
        doc = self.send(self.prepared(), times=1, rule=lambda a: credential + a["body"], say=log.append)
        self.assertNotIn(credential, json.dumps(doc) + "".join(log))
        for p in Path(doc["out"]).rglob("*"):
            if p.is_file():
                self.assertNotIn(credential.encode(), p.read_bytes())
        with self.assertRaises(replay.Refused) as caught:
            replay._read_rule(lambda a: (_ for _ in ()).throw(ValueError(credential)), {}, (credential,))
        self.assertNotIn(credential, str(caught.exception))

    def test_k_sends_their_counts_and_the_spend_recorded(self):
        """K sends, the counts of the distinct answers, and the spend recorded."""
        Upstream.plan = [3, 4, 3, None, 3]
        prep = self.prepared()
        doc = self.send(prep, times=5)
        self.assertEqual(doc["sends"], {"sent": 5, "answered": 5, "failed": 0, "refused": 0, "not_sent": 0})
        self.assertEqual(doc["recorded_answer"], 'click {"x":3}')
        self.assertEqual([(r["label"], r["count"], r["recorded"]) for r in doc["counts"]],
                         [('click {"x":3}', 3, True), ('click {"x":4}', 1, False), ("no call", 1, False)])
        self.assertEqual(doc["same_as_recorded"], 3)
        self.assertAlmostEqual(doc["spend"]["spent_usd"], 0.005)
        out = Path(doc["out"])
        kept = json.loads((out / "replay.json").read_text())
        self.assertEqual({k: kept[k] for k in ("times", "max_usd", "state", "counts", "spend", "changes")},
                         {"times": 5, "max_usd": 1.0, "state": "done", "counts": doc["counts"], "spend": doc["spend"], "changes": []})
        self.assertEqual(kept["rule"]["name"], "first tool call")
        answers = [json.loads(x) for x in (out / "answers.jsonl").read_text().splitlines()]
        self.assertEqual([a["send"] for a in answers], [1, 2, 3, 4, 5])
        self.assertEqual(answers[0]["tool_calls"], [{"name": "click", "arguments": '{"x": 3}'}])
        self.assertEqual((answers[0]["reasoning"], answers[0]["served_by"], answers[0]["cost_usd"]), ("look", "prov-a", 0.001))
        self.assertEqual((out / "request.json").read_bytes(), prep.raw)
        whole = exchanges.lines(str(out / "relay" / "exchanges"), "r001")
        self.assertEqual(json.loads(whole[0]["reply"]["body"]), reply_of(3), "each answer whole, as it arrived")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["answers.jsonl", "ledger", "relay", "replay.json", "request.json"])

    def test_the_cap_stops_the_sends(self):
        """Each send is booked at the most it can cost, three in flight at once: the first that does not fit is
        refused, nothing after it is sent, and the spend stays under the cap."""
        Upstream.cost = 0.1                      # 1,000 output tokens at most, at 100 USD a million: 0.1 and the prompt
        prep = self.prepared(prices={"m": {"input": 1.0, "output": 100.0}})
        self.assertGreater(prep.most_usd, 0.1)
        doc = self.send(prep, times=5, max_usd=0.25, concurrency=3)
        self.assertEqual((doc["sends"]["sent"], doc["sends"]["answered"], len(Upstream.seen)), (2, 2, 2))
        self.assertEqual(doc["state"], "stopped")
        self.assertIn("refused (cap)", doc["stopped"])
        self.assertAlmostEqual(doc["spend"]["spent_usd"], 0.2)
        self.assertEqual(doc["spend"]["max_usd"], 0.25)

    def test_a_refused_key_stops_the_sends_and_a_failure_is_its_error(self):
        Upstream.status = 401
        doc = self.send(self.prepared(), times=4)
        self.assertEqual((doc["sends"]["failed"], doc["sends"]["not_sent"], len(Upstream.seen)), (1, 3, 1))
        Upstream.status = 500
        doc = self.send(self.prepared(), times=2)
        self.assertEqual([(r["label"], r["count"]) for r in doc["counts"]], [("error: HTTP 500", 2), ('click {"x":3}', 0)])
        Upstream.status = 200                    # a 200 that ended on an error, or holds no choice, fails as well
        for body, label in (({"choices": [{"index": 0, "message": {"content": "Natalia sold 48"}, "finish_reason": "error"}]},
                             "error: the reply ended on an error"), ({"choices": []}, "error: the reply holds no choice")):
            with self.subTest(label=label):
                Upstream.body = body
                doc = self.send(self.prepared(), times=1, rule=replay.command_rule("echo read"))
                self.assertEqual((doc["sends"]["answered"], doc["sends"]["failed"]), (0, 1))
                self.assertEqual([r["label"] for r in doc["counts"] if r["count"]], [label])

    def test_a_rule_given_as_a_command_reads_each_answer_without_the_key(self):
        script = os.path.join(self.tmp, "read.py")
        Path(script).write_text("import json, os, sys\na = json.load(sys.stdin)\n"     # the answer read, and its body as it arrived
                                "print(a['reasoning'], a['tool_calls'][0]['name'], len(json.loads(a['body'])['content']),"
                                " os.environ.get('OPENROUTER_API_KEY', 'no key'))\n")
        doc = self.send(self.prepared(), times=2, rule=replay.command_rule(f"{sys.executable} {script}", env=self.env))
        self.assertEqual(doc["counts"], [{"label": "look click 2 no key", "count": 2, "recorded": True}], "by the whole label")
        self.assertEqual(doc["rule"], {"name": "command", "command": f"{sys.executable} {script}"})

    def test_the_key_is_in_no_file_and_nothing_is_written_in_the_runs_tree(self):
        """The key in no file and no log; nothing written in the run's tree."""
        before = tree(self.job)
        doc = self.send(self.prepared(), times=2)
        self.assertEqual(tree(self.job), before)
        for p in Path(doc["out"]).rglob("*"):
            if p.is_file():
                self.assertNotIn(KEY.encode(), p.read_bytes(), str(p))
        with self.assertRaisesRegex(replay.Refused, "inside the job's folder"):
            replay.send(self.prepared(), 1, 1.0, os.path.join(self.job, "replays"), env=self.env)
        with self.assertRaisesRegex(replay.Refused, "already holds files"):
            replay.send(self.prepared(), 1, 1.0, doc["out"], env=self.env)
        with self.assertRaisesRegex(replay.Refused, "OPENROUTER_API_KEY is not set"):
            replay.send(self.prepared(), 1, 1.0, os.path.join(self.tmp, "nokey"), env={})
        self.assertEqual(tree(self.job), before)


class CommandLine(ReplayCase):
    def main(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                code = standard_cli.main(["replay", *map(str, args)])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_the_dry_run_sends_nothing_and_writes_nothing(self):
        """The dry run prints what would be sent and its estimated cost, and sends nothing."""
        before, cwd = tree(self.tmp), os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        code, out, err = self.main(self.job, "task-a__u0001", "--call", 2, "--times", 8, "--max-usd", 0.5, "--dry-run", "--json")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertTrue(doc["dry_run"])
        self.assertEqual((doc["times"], doc["request"]["sha256"], doc["request"]["model"], doc["request"]["served_by"]),
                         (8, doc["source"]["sent_sha256"], "m", ["prov-a"]))
        self.assertAlmostEqual(doc["estimate_usd"]["all"], 8 * doc["estimate_usd"]["per_call"])
        self.assertGreater(doc["estimate_usd"]["per_call"], 0)
        self.assertEqual(doc["booked_usd"]["output_tokens"], 1000)             # the request's max_tokens, at 2 USD a million
        self.assertGreater(doc["booked_usd"]["per_call"], 1000 * 2.0 / 1e6)
        self.assertEqual(Upstream.seen, [])
        self.assertEqual(tree(self.tmp), before)
        code, out, _ = self.main(self.job, "task-a__u0001", "--call", 2, "--times", 8, "--max-usd", 0.5, "--dry-run")
        self.assertIn("dry run, nothing sent: call 2 of task-a__u0001", out)
        self.assertRegex(out, r"booked [0-9.]+ USD a send until its answer, the most it can cost \(1000 output tokens\)")
        self.assertIn("(the recorded one)", out)

    def test_a_send_from_the_command_line_and_the_list(self):
        out_dir = os.path.join(self.tmp, "r1")
        env = {"MODEL_API_URL": self.base, "MODEL_API_KEY": KEY}
        code, out, err = self.main(self.job, "task-a__u0001", "--call", 2, "--times", 3, "--max-usd", 0.5, "--provider", "custom",
                                   "--out", out_dir, "--json", env=env)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["sends"]["answered"], doc["out"], doc["changes"][0]["name"]), (3, out_dir, "provider"))
        self.assertNotIn(KEY, out + err)
        code, out, err = self.main(self.job, "task-a__u0001", "--list", "--json")
        self.assertEqual((code, [c["n"] for c in json.loads(out)]), (0, [1, 2, 3]))
        code, out, _ = self.main(self.job, "task-a__u0001", "--list")
        self.assertRegex(out, r"(?m)^   2     200 +\d+  yes    m$")

    def test_a_send_whose_most_cost_passes_the_cap_is_not_made(self):
        """``--times 1 --max-usd 0.001`` with an answer of 5,000 tokens: refused before any send, with the cap one
        send needs; an answer with no bound is refused with what bounds it."""
        Upstream.cost, Upstream.output = None, 5000
        self.record_trial("chat__u0002", 1, path="/chat/completions", req={
            "model": "m", "max_tokens": 8192, "messages": [{"role": "user", "content": "Natalia sold 48 clips."}]})
        self.record_trial("open__u0003", 1, path="/chat/completions", req={
            "model": "m", "messages": [{"role": "user", "content": "Natalia sold 48 clips."}]})
        Upstream.seen = []
        out = os.path.join(self.tmp, "r1")
        env = {"MODEL_API_URL": self.base, "MODEL_API_KEY": KEY}
        for trial, said in (("chat__u0002", r"refused: one send may cost up to (0\.0165) USD \(its prompt and 8192 output "
                                             r"tokens\), past the cap of 0\.001 USD: --max-usd \1 or more lets one go out"),
                            ("open__u0003", r"refused: nothing bounds the length of an answer to m, .*--set max_tokens=N bounds it")):
            with self.subTest(trial=trial):
                code, got, err = self.main(self.job, trial, "--call", 1, "--times", 1, "--max-usd", 0.001,
                                                  "--provider", "custom", "--out", out, env=env)
                self.assertEqual((code, got, Upstream.seen), (2, "", []))
                self.assertRegex(err, "^" + said)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertFalse(os.path.exists(out))
        doc = self.send(self.prepared(trial="chat__u0002", call=1), times=1, max_usd=0.0165)
        self.assertEqual((doc["sends"]["answered"], doc["spend"]["spent_usd"]), (1, (50 + 5000 * 2.0) / 1e6))

    def test_help_and_refusals_follow_the_command_line_contract(self):
        """Help and refusals: usage, examples and exit codes; a refusal is one line on stderr, stdout left empty."""
        site = os.path.join(self.tmp, "site")
        os.makedirs(site)
        Path(site, "sitecustomize.py").write_text(
            "import socket\nclass _R(socket.socket):\n    def __init__(self, *a, **k):\n        raise AssertionError('socket opened')\n"
            "socket.socket = _R\n")
        env = dict(os.environ, PYTHONPATH=str(APP) + os.pathsep + site)
        env.pop("OPENROUTER_API_KEY", None)

        def sh(*args):
            return subprocess.run([sys.executable, "-m", "nullius.standard.cli", "replay", *map(str, args)], cwd=self.tmp,
                                  env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        got = sh("--help")
        self.assertEqual((got.returncode, got.stderr), (0, ""))
        for line in ("usage:", "examples:", "exit codes:", "--dry-run", "--max-usd"):
            self.assertIn(line, got.stdout)
        blobs = os.path.join(self.job, "task-a__u0001", ".work", "relay", "exchanges", "blobs")
        shutil.rmtree(blobs)
        need = ("--call", 2, "--times", 2, "--max-usd", 0.1, "--json")
        for args, code, said in (((self.job, "task-a__u0001"), 2, "usage:"),
                                 ((os.path.join(self.tmp, "nowhere"), "t__u1", *need), 3, "no such directory"),
                                 ((self.job, "task-z__u0009", *need), 3, "no trial named"),
                                 ((self.job, "task-a__u0001", "--call", 9, *need[2:]), 3, "no call 9"),
                                 ((self.job, "task-a__u0001", *need), 4, "cannot be sent again as it was"),
                                 ((self.job, "task-a__u0001", *need, "--replace", "absent", "x"), 4, "cannot be sent again")):
            with self.subTest(args=args):
                out = sh(*args)
                self.assertEqual(out.returncode, code, out.stderr)
                self.assertIn(said, out.stderr)
                self.assertEqual(out.stdout, "")
                if code != 2:
                    self.assertEqual(len(out.stderr.strip().splitlines()), 1, out.stderr)
        self.record_trial("task-d__u0005", calls=1)
        out = sh(self.job, "task-d__u0005", "--call", 1, "--times", 2, "--max-usd", 0.1, "--replace", "absent", "x", "--json")
        self.assertEqual((out.returncode, out.stdout), (2, ""))
        self.assertIn("refused: the text to replace ('absent') appears 0 times", out.stderr)
        out = sh(self.job, "task-d__u0005", "--call", 1, "--times", 2, "--max-usd", 0.1, "--json")
        self.assertEqual((out.returncode, out.stdout), (2, ""))
        self.assertIn("refused: OPENROUTER_API_KEY is not set", out.stderr)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["jobs", "site"], "a refusal writes nothing")


class ReadingPage(unittest.TestCase):
    def test_the_documented_reader_consumes_the_parsed_answer(self):
        page = (APP.parent / "docs" / "cli" / "replay.md").read_text()
        script = re.search(r'```python title="read_action.py"\n(.*?)```', page, re.S).group(1)
        body = json.dumps({"choices": [{"message": {"content": '{"action":"click"}'}}]}).encode()
        answer = replay.answer("/chat/completions", 200, body, False)
        got = subprocess.run([sys.executable, "-c", script], input=json.dumps(answer),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual((got.returncode, got.stdout, got.stderr), (0, "click\n", ""))


if __name__ == "__main__":
    unittest.main()
