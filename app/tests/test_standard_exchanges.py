"""The relay's store of each model exchange: the request as sent, by part, each
part and picture written once, and the reply as it arrived; what it keeps in
each mode and under its bound; that it never stands in a reply's way and never
holds the relay's key; and the request rebuilt to the bytes sent. The provider
is a fake endpoint on loopback; no key, no network."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nullius.runs import cli as runs_cli, parse
from nullius.standard import cli as standard_cli
from nullius.standard.backends import container
from nullius.standard.calls import exchanges, relay
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.requirements import install
from nullius.standard.runner import pace as pacing, rerun, run

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-relay-real-0123"                # a stand-in in the redaction's key shape, shorter than a real key


def picture(i: int, size: int = 3000, kind: bytes = b"\x89PNG\r\n\x1a\n") -> bytes:
    """A picture's bytes, made up: a header and ``size`` bytes of its own."""
    seed = hashlib.sha256(str(i).encode()).digest()
    return kind + (seed * (size // len(seed) + 1))[:size]


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def data_url(raw: bytes, kind: str = "image/png") -> str:
    return f"data:{kind};base64,{b64(raw)}"


PNG, JPEG = picture(1), picture(2, kind=b"\xff\xd8\xff\xe0")
TOOL = {"type": "object", "properties": {"x": {"type": "integer"}}}
# each API shape with inline pictures in its own form: (path, request, the pictures it holds as (bytes, extension))
SHAPES = {
    "chat": ("/chat/completions",
             {"model": "m", "max_tokens": 100, "messages": [
                 {"role": "system", "content": "be brief"},
                 {"role": "user", "content": [{"type": "text", "text": "look"},
                                              {"type": "image_url", "image_url": {"url": data_url(PNG)}}]},
                 {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                        "function": {"name": "click", "arguments": "{\"x\": 1}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": [{"type": "image_url", "image_url": {"url": data_url(JPEG, "image/jpeg")}}]}],
              "tools": [{"type": "function", "function": {"name": "click", "parameters": TOOL}}]},
             [(PNG, "png"), (JPEG, "jpg")]),
    "messages": ("/messages",
                 {"model": "m", "max_tokens": 100, "system": "be brief",
                  "tools": [{"name": "click", "input_schema": TOOL}],
                  "messages": [
                      {"role": "user", "content": [{"type": "text", "text": "look"},
                                                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(PNG)}}]},
                      {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "click", "input": {"x": 1}}]},
                      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
                          {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(JPEG)}}]}]}]},
                 [(PNG, "png"), (JPEG, "jpg")]),
    "responses": ("/responses",
                  {"model": "m", "instructions": "be brief", "tools": [{"type": "function", "name": "click", "parameters": TOOL}],
                   "input": [{"role": "user", "content": [{"type": "input_text", "text": "look"},
                                                          {"type": "input_image", "image_url": data_url(PNG), "detail": "auto"}]},
                             {"type": "function_call", "call_id": "c1", "name": "click", "arguments": "{\"x\": 1}"},
                             {"type": "function_call_output", "call_id": "c1", "output": "clicked"},
                             {"role": "user", "content": [{"type": "input_image", "image_url": data_url(JPEG, "image/jpeg")}]}]},
                  [(PNG, "png"), (JPEG, "jpg")]),
}

# the replies, each with a reasoning text, streamed and not
CHAT = {"choices": [{"message": {"role": "assistant", "content": "ok", "reasoning": "I looked"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
CHAT_SSE = (b'data: {"choices":[{"delta":{"reasoning":"I "}}]}\n\n'
            b'data: {"choices":[{"delta":{"reasoning":"looked"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
            b'data: [DONE]\n\n')
MESSAGE = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
           "content": [{"type": "thinking", "thinking": "I looked", "signature": "s"}, {"type": "text", "text": "ok"}],
           "usage": {"input_tokens": 5, "output_tokens": 2}}
MESSAGE_SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
               b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
               b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"I looked"}}\n\n'
               b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
               b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"ok"}}\n\n'
               b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
               b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
RESPONSE = {"object": "response", "status": "completed",
            "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "I looked"}]},
                       {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 9, "output_tokens": 2}}
RESPONSE_SSE = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                b'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": RESPONSE}).encode() + b'\n\n')


class Upstream(BaseHTTPRequestHandler):
    """A model: each body as it arrived is kept, and every reply holds a reasoning text."""
    seen = []                                    # (path, headers, the body's bytes)
    refuse = 0                                   # how many calls, from now, get a rate limit

    def log_message(self, *a):
        pass

    def do_GET(self):                            # no list of models, as a provider without one
        self.reply(404, b"", "application/json")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, dict(self.headers), raw))
        try:
            body = json.loads(raw)
        except ValueError:
            return self.reply(400, b'{"error":{"message":"not JSON"}}', "application/json")
        if Upstream.refuse > 0:
            Upstream.refuse -= 1
            return self.reply(429, b'{"error":{"message":"Rate limit reached for requests"}}', "application/json")
        stream = body.get("stream")
        if self.path.endswith("/messages"):
            return self.reply(200, MESSAGE_SSE, "text/event-stream") if stream else self.reply(200, json.dumps(MESSAGE).encode(), "application/json")
        if self.path.endswith("/responses"):
            return self.reply(200, RESPONSE_SSE, "text/event-stream") if stream else self.reply(200, json.dumps(RESPONSE).encode(), "application/json")
        if stream:
            return self.reply(200, CHAT_SSE, "text/event-stream")
        said = body["messages"][-1]["content"] if isinstance(body.get("messages"), list) and body["messages"] else ""
        got = dict(CHAT, choices=[{"message": dict(CHAT["choices"][0]["message"],
                                                   content="you said: " + (said if isinstance(said, str) else "pictures"))}])
        return self.reply(200, json.dumps(got).encode(), "application/json")

    def reply(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Upstream.refuse = 0

    def relay(self, mode="whole", **kw):
        r = relay.Relay(self.base, KEY, os.path.join(self.tmp, f"rec-{len(os.listdir(self.tmp))}"), exchanges=mode, **kw).start()
        self.addCleanup(r.stop)
        return r

    def post(self, r, tag, path, body, headers=None, raw=None):
        req = urllib.request.Request(r.url(tag) + path, data=raw if raw is not None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.PLACEHOLDER_KEY,
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req) as got:
                return got.status, got.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @staticmethod
    def store(r):
        return os.path.join(r.dir, exchanges.FOLDER)

    @staticmethod
    def files(folder):
        out = {}
        for root, _, names in os.walk(folder):
            for n in names:
                with open(os.path.join(root, n), "rb") as f:
                    out[os.path.relpath(os.path.join(root, n), folder)] = f.read()
        return out


class StoreFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def store(self, mode="whole"):
        return exchanges.Store(tempfile.mkdtemp(dir=self.root), mode=mode, keys=(KEY,))

    def keep(self, store, req, n=1, headers=None):
        store.write("u0001", n, 1.0, "/messages", req, json.dumps(req).encode(), headers or {}, 200, False, b"{}")

    def test_only_real_keys_are_cut_from_nested_values_and_names(self):
        _, placeholder = run.provider_endpoint("custom", {"MODEL_API_URL": "http://localhost:8000/v1"})
        value = {"localhost": ["local-model", {KEY: "localhost " + KEY}]}
        expected = {"localhost": ["local-model", {"[REDACTED]": "localhost [REDACTED]"}]}
        for clean in (exchanges._cut_key, relay._clean):
            with self.subTest(clean=clean.__name__):
                self.assertEqual(clean(value, ("", placeholder, KEY)), expected)

    def test_a_keyless_relay_keeps_localhost_in_records_and_messages(self):
        base, key = run.provider_endpoint("custom", {"MODEL_API_URL": "http://localhost:8000/v1"})
        req = {"model": "local-model", "messages": [{"role": "user", "content": "localhost"}],
               "provider": {"order": ["local-asked"]}}
        body = json.dumps(dict(CHAT, provider="localhost", error={"message": "localhost unavailable"})).encode()
        for mode in exchanges.MODES:
            with self.subTest(mode=mode):
                r = relay.Relay(base, key, str(self.root / mode), exchanges=mode, serving={"local-model": ["localhost"]})
                self.addCleanup(r.stop)
                response = mock.Mock(status=200, headers={"Content-Type": "application/json"})
                response.getheader.return_value = "application/json"
                response.read.return_value = body
                raw = json.dumps(req).encode()
                h = mock.Mock(path="/u0001/v1/chat/completions", command="POST",
                              headers={"Content-Length": str(len(raw))}, rfile=io.BytesIO(raw), wfile=io.BytesIO())
                with mock.patch.object(r.pool, "send", return_value=(mock.Mock(), response)), mock.patch.object(r.pool, "give"):
                    r._forward(h)
                self.assertEqual(h.wfile.getvalue(), body)
                call, = r.calls("u0001")
                self.assertEqual(call["request"]["routing"], relay.routing(["localhost"]))
                self.assertEqual(call["request"]["routing_asked"], req["provider"])
                self.assertEqual(call["reply"]["served_by"], "localhost")
                self.assertEqual(call["reply"]["error"]["message"], "localhost unavailable")
                self.assertEqual(r._said(OSError("localhost unavailable")), "OSError: localhost unavailable")
                for p in Path(r.dir).rglob("*"):
                    if p.is_file():
                        self.assertNotIn(b"[REDACTED]", p.read_bytes())

    def test_empty_and_placeholder_keys_leave_exchanges_unchanged(self):
        env = {"MODEL_API_URL": "http://localhost:8000/v1"}
        _, placeholder = run.provider_endpoint("custom", env)
        req = {"model": "local-model", "messages": [{"role": "user", "content": "localhost"}]}
        raw = json.dumps(req).encode()
        headers = {"X-local": "localhost"}
        body = b'{"text":"localhost local-model"}'
        for key in ("", placeholder):
            for mode in ("whole", "text"):
                with self.subTest(key=key, mode=mode):
                    job = self.root / f"{mode}-{key or 'empty'}"
                    store = exchanges.Store(str(job / "task__u0001" / ".work" / "relay"), mode=mode, keys=(key,))
                    for n, request in enumerate((req, None), 1):
                        store.write("u0001", n, 1.0, "/chat/completions", request, raw, headers, 200, False, body)
                        line = exchanges.lines(store.dir, "u0001")[-1]
                        self.assertNotIn("key_cut", line)
                        self.assertEqual(line["headers"], headers)
                        self.assertEqual(line["reply"]["body"], body.decode())
                        self.assertEqual(exchanges.rebuild(store.dir, line), raw)
                        self.assertTrue(exchanges.check(store.dir, "u0001", n)["same"])

    def test_a_damaged_part_leaves_other_calls_rebuildable(self):
        for damage in ("truncated", "digest", "picture", "missing"):
            with self.subTest(damage=damage):
                trial = mock.Mock(dir=str(self.root / damage / "task__u0001"), trial_name="task__u0001")
                store = exchanges.Store(os.path.join(trial.dir, ".work", "relay"))
                bad, good = {"messages": [data_url(PNG)]}, {"messages": ["still readable"]}
                for n, req in enumerate((bad, good, bad), 1):
                    self.keep(store, req, n=n)
                path = Path(store.dir) / exchanges.PARTS
                rows = path.read_text().splitlines()
                first = next(i for i, r in enumerate(rows) if '"part"' in r)       # the first part, after the mark
                row = json.loads(rows[first])
                if damage == "digest":
                    row["sha256"] = "invalid"
                elif damage == "picture":
                    row["part"]["blob"] = "../outside"
                elif damage == "missing":
                    del row["part"]
                rows[first] = rows[first][:40] if damage == "truncated" else json.dumps(row)
                path.write_text("\n".join(rows) + "\n")
                for n in (1, 2, 3):
                    out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.StringIO()
                    self.addCleanup(out.close)
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = runs_cli._request(trial, n)
                    out.flush()
                    result = exchanges.check(store.dir, "u0001", n)
                    self.assertEqual(result["same"], n == 2)
                    if n == 2:
                        self.assertEqual((code, out.buffer.getvalue()), (0, json.dumps(good).encode() + b"\n"))
                        self.assertIn("the recorded sent_sha256", err.getvalue())
                        line = exchanges.lines(store.dir, "u0001")[1]
                        self.assertEqual(exchanges.rebuild(store.dir, line, exchanges.stored_parts(store.dir)),
                                         json.dumps(good).encode())
                    else:
                        self.assertEqual((code, out.buffer.getvalue()), (runs_cli.EXIT_FAILED, b""))
                        self.assertIsNone(result["request"])

    def assert_picture_rejected(self, store, line, parts):
        self.assertEqual(exchanges.stored_parts(store.dir), {})
        for read in (lambda: exchanges.rebuild(store.dir, line),
                     lambda: exchanges.rebuild(store.dir, line, parts)):
            with self.assertRaises(exchanges.Missing):
                read()
        result = exchanges.check(store.dir, "u0001", 1)
        self.assertFalse(result["same"])
        self.assertIsNone(result["request"])

    def test_picture_names_are_only_sha256_digests_in_every_part_reader(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside picture")
        for name in (str(outside.with_suffix("")), "../../outside", "a" * 63, "a" * 65,
                     "g" * 64, "a" * 64 + "\n", "a" * 64 + ".png", None):
            with self.subTest(name=name):
                store = self.store()
                self.keep(store, {"messages": [data_url(PNG)]})
                line, = exchanges.lines(store.dir, "u0001")
                parts = exchanges.stored_parts(store.dir)
                part, = parts.values()
                part["part"]["blob"] = name
                (Path(store.dir) / exchanges.PARTS).write_text("".join(
                    json.dumps(dict(sha256=h, **p)) + "\n" for h, p in parts.items()))
                self.assert_picture_rejected(store, line, parts)

    def test_picture_links_must_resolve_inside_the_stores_own_blobs_folder(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside picture")
        for linked in ("file", "directory"):
            with self.subTest(linked=linked):
                store = self.store()
                req = {"messages": [data_url(PNG)]}
                self.keep(store, req)
                line, = exchanges.lines(store.dir, "u0001")
                parts = exchanges.stored_parts(store.dir)
                blobs = Path(store.dir) / exchanges.BLOBS
                blob, = blobs.iterdir()
                if linked == "file":
                    blob.unlink()
                    blob.symlink_to(outside)
                else:
                    moved = self.root / "moved"
                    blobs.rename(moved)
                    blobs.symlink_to(moved, target_is_directory=True)
                self.assert_picture_rejected(store, line, parts)

        store = self.store()
        self.keep(store, req)
        blobs = Path(store.dir) / exchanges.BLOBS
        blob, = blobs.iterdir()
        kept = blobs / ("0" * 64 + ".png")
        blob.rename(kept)
        blob.symlink_to(kept)
        self.assertTrue(exchanges.check(store.dir, "u0001", 1)["same"])

    def test_part_and_call_files_cannot_be_read_through_outside_links(self):
        for name, read in ((exchanges.PARTS, exchanges.stored_parts),
                           ("u0001.jsonl", lambda folder: exchanges.lines(folder, "u0001"))):
            with self.subTest(name=name):
                store = self.store()
                self.keep(store, {"messages": ["hello"]})
                path = Path(store.dir) / name
                outside = self.root / name
                path.rename(outside)
                path.symlink_to(outside)
                with self.assertRaises(exchanges.Missing):
                    read(store.dir)

    def test_header_names_and_values_both_cut_out_the_relay_key(self):
        store = self.store()
        self.keep(store, {"messages": ["hello"]}, headers={"X-Trace-" + KEY: "trace", "X-Value": KEY})
        for path in Path(store.dir).rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes())
        line, = exchanges.lines(store.dir, "u0001")
        self.assertEqual(line["headers"], {"X-Trace-[REDACTED]": "trace", "X-Value": "[REDACTED]"})
        self.assertTrue(exchanges.check(store.dir, "u0001", 1)["same"])

    @contextlib.contextmanager
    def failed_write(self, target, failure):
        original = open
        failed = False

        @contextlib.contextmanager
        def opened(path, mode="r", *args, **kwargs):
            nonlocal failed
            with original(path, mode, *args, **kwargs) as file:
                if Path(path) != target or mode not in ("a", "wb") or failed:
                    yield file
                    return
                failed = True

                def write(data):
                    count = file.write(data[:20])
                    file.flush()
                    if failure == "write":
                        raise OSError(28, "No space left on device")
                    return count

                proxy = mock.Mock(wraps=file)
                if failure != "close":
                    proxy.write.side_effect = write
                yield proxy
            if failure == "close":
                raise OSError(28, "No space left on device")

        with mock.patch("builtins.open", side_effect=opened):
            yield
        self.assertTrue(failed)

    def test_failed_appends_restore_the_earlier_length_and_retry_whole_parts(self):
        for name in (exchanges.PARTS, "u0001.jsonl"):
            for failure in ("write", "short", "close"):
                with self.subTest(name=name, failure=failure):
                    store = self.store()
                    self.keep(store, {"messages": ["earlier"]})
                    path = Path(store.dir) / name
                    before = path.read_bytes()
                    req = {"messages": ["a later part"]}
                    with self.failed_write(path, failure):
                        with self.assertRaises(OSError):
                            store._append(name, '{"unfinished":"a long value"}\n')
                    self.assertEqual(path.read_bytes(), before)
                    if name == exchanges.PARTS:
                        prior_parts = set(store.parts)
                        with self.failed_write(path, failure):
                            self.keep(store, req, 2)
                        self.assertEqual(path.read_bytes(), before)
                        self.assertEqual(store.parts, prior_parts)
                    else:
                        with self.failed_write(path, failure):
                            self.keep(store, req, 2)
                        self.assertEqual(path.read_bytes()[:len(before)], before)
                    rows = exchanges.lines(store.dir, "u0001")
                    self.assertEqual([row["n"] for row in rows], [1, 2])
                    self.assertIn("error", rows[1])
                    for n in (3, 4):
                        self.keep(store, req, n)
                        self.assertTrue(exchanges.check(store.dir, "u0001", n)["same"])
                    self.assertEqual(store.size, sum(p.stat().st_size for p in Path(store.dir).rglob("*") if p.is_file()))

    def test_incomplete_pictures_are_not_marked_stored_and_are_written_again(self):
        for failure in ("write", "short", "close"):
            with self.subTest(failure=failure):
                store = self.store()
                req = {"messages": [data_url(PNG)]}
                blob = Path(store.dir) / exchanges.BLOBS / (hashlib.sha256(PNG).hexdigest() + ".png")
                with self.failed_write(Path(str(blob) + ".part"), failure):
                    self.keep(store, req)
                self.assertFalse(blob.exists())
                self.assertEqual((store.parts, store.blobs), (set(), set()))
                line, = exchanges.lines(store.dir, "u0001")
                self.assertIn("error", line)
                self.keep(store, req, 2)
                self.assertEqual(blob.read_bytes(), PNG)
                self.assertTrue(exchanges.check(store.dir, "u0001", 2)["same"])

    def test_literal_picture_marks_do_not_disable_picture_extraction(self):
        literal = {"blob": "x", "type": "x", "bytes": 1, "as": "base64"}
        tool = {"type": "object", "properties": {
            "payload": {"default": literal}, "reference": {"default": data_url(PNG)},
            "nested": {"default": {"literal": {"literal": literal}}}}}
        req = {"tools": [tool]}
        for mode in ("whole", "text"):
            with self.subTest(mode=mode):
                store = self.store(mode)
                self.keep(store, req)
                self.assertNotIn(b64(PNG), (Path(store.dir) / exchanges.PARTS).read_text())
                result = exchanges.check(store.dir, "u0001", 1)
                if mode == "whole":
                    self.assertEqual(result["request"], json.dumps(req).encode())
                    self.assertTrue(result["same"])
                else:
                    self.assertFalse((Path(store.dir) / exchanges.BLOBS).exists())
                    self.assertIsNone(result["request"])
                plain = {"tools": [literal, {"literal": literal}]}
                self.keep(store, plain, 2)
                self.assertEqual(exchanges.check(store.dir, "u0001", 2)["request"], json.dumps(plain).encode())

    def test_text_omits_noncanonical_picture_encodings(self):
        encoded = b64(PNG)
        wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
        for kind, data in (("image/png", wrapped), ("image/png", encoded.rstrip("=")),
                           ("image/bmp", wrapped), ("image/png", "QR==")):
            for form in ("data_url", "base64"):
                with self.subTest(kind=kind, data=data[:10], form=form):
                    value = (f"data:{kind};base64,{data}" if form == "data_url" else
                             {"type": "base64", "media_type": kind, "data": data})
                    store = self.store("text")
                    self.keep(store, {"messages": [value]})
                    self.assertFalse((Path(store.dir) / exchanges.BLOBS).exists())
                    for path in Path(store.dir).rglob("*"):
                        if path.is_file():
                            self.assertNotIn(json.dumps(data)[1:-1].encode(), path.read_bytes())
                    part, = exchanges.stored_parts(store.dir).values()
                    mark = part["part"] if form == "data_url" else part["part"]["data"]
                    raw = b"A" if data == "QR==" else PNG
                    self.assertEqual(mark, {"blob": hashlib.sha256(raw).hexdigest(), "type": kind,
                                            "bytes": len(raw), "as": form})
                    self.assertFalse(exchanges.check(store.dir, "u0001", 1)["same"])

    def test_text_omits_pictures_from_the_call_line_too(self):
        value = {"type": "base64", "media_type": "image/png", "data": b64(PNG)}
        for streamed in (False, True):
            for req in ({"metadata": data_url(PNG), "messages": value}, None):
                with self.subTest(streamed=streamed, raw=req is None):
                    store = self.store("text")
                    reply = json.dumps({"image": value})
                    body = ("data: " + reply + "\n\ndata: [DONE]\n\n" if streamed else reply).encode()
                    raw = json.dumps(req if req is not None else [value]).encode()
                    store.write("u0001", 1, 1.0, "/messages", req, raw,
                                {"X-Image": data_url(PNG)}, 200, streamed, body)
                    for path in Path(store.dir).rglob("*"):
                        if path.is_file():
                            self.assertNotIn(b64(PNG).encode(), path.read_bytes())
                    self.assertFalse((Path(store.dir) / exchanges.BLOBS).exists())


class ByteForByte(WithUpstream):
    def test_each_shape_rebuilds_to_the_bytes_sent_streamed_or_not_and_a_changed_part_fails(self):
        for shape, (path, ask, pictures) in SHAPES.items():
            for stream in (False, True):
                with self.subTest(shape=shape, stream=stream):
                    r = self.relay()
                    status, reply = self.post(r, "u0001", path, dict(ask, **({"stream": True} if stream else {})))
                    self.assertEqual(status, 200)
                    sent = Upstream.seen[-1][2]
                    store = self.store(r)
                    (line,) = exchanges.lines(store, "u0001")
                    self.assertEqual((line["n"], r.calls("u0001")[-1]["n"]), (1, 1))     # the record and the line, one number
                    self.assertEqual(line["sent_sha256"], hashlib.sha256(sent).hexdigest())
                    self.assertEqual(exchanges.rebuild(store, line), sent)
                    self.assertTrue(exchanges.check(store, "u0001", 1)["same"])
                    # each picture a file of its own, its bytes as sent; no part holds its text
                    self.assertEqual(sorted(n for n in os.listdir(os.path.join(store, exchanges.BLOBS))),
                                     sorted(f"{hashlib.sha256(p).hexdigest()}.{ext}" for p, ext in pictures))
                    for p, ext in pictures:
                        with open(os.path.join(store, exchanges.BLOBS, f"{hashlib.sha256(p).hexdigest()}.{ext}"), "rb") as f:
                            self.assertEqual(f.read(), p)
                    texts = self.files(store)
                    self.assertFalse(any(b64(p).encode() in texts[n] for p, _ in pictures for n in texts if not n.startswith(exchanges.BLOBS)))
                    # the reply as it arrived, its reasoning read back
                    self.assertEqual((line["reply"]["status"], line["reply"]["streamed"], line["reply"]["body"].encode()), (200, stream, reply))
                    self.assertEqual(relay.read_reply(path, line["reply"]["body"].encode(), stream, reasoning=True)["reasoning"], "I looked")
                    # a changed part: its bytes no longer the ones sent
                    parts = os.path.join(store, exchanges.PARTS)
                    with open(parts) as f:
                        text = f.read()
                    with open(parts, "w") as f:
                        f.write(text.replace('"look"', '"LOOK"'))
                    got = exchanges.check(store, "u0001", 1)
                    self.assertFalse(got["same"])
                    self.assertIn("do not rebuild", got["why"])

    def test_a_picture_whose_text_is_not_its_bytes_own_encoding_stays_in_its_part(self):
        """Noncanonical pictures and literal marks rebuild as sent. Canonical pictures are files."""
        odd = ["data:image/png;base64,QR==", "data:image/png;base64,QQ", "data:image/png;base64,QQ==\n", "data:image/bmp;base64,QQ=="]
        ask = {"model": "m", "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": u}} for u in odd + ["data:image/png;base64,QQ=="]]},
            {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QR=="}}]},
            {"role": "user", "content": [{"blob": "x", "type": "t", "bytes": 1, "as": "data_url"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUI="}}]}]}
        r = self.relay()
        self.assertEqual(self.post(r, "u0001", "/chat/completions", ask)[0], 200)
        store = self.store(r)
        (line,) = exchanges.lines(store, "u0001")
        self.assertEqual(exchanges.rebuild(store, line), Upstream.seen[-1][2])
        self.assertEqual(sorted(os.listdir(os.path.join(store, exchanges.BLOBS))),
                         sorted(hashlib.sha256(raw).hexdigest() + ".png" for raw in (b"A", b"AB")))
        self.assertEqual(sum('"as_sent":true' in x for x in open(os.path.join(store, exchanges.PARTS))), 0)

    def test_the_relays_changes_are_what_is_kept(self):
        """``sent`` is the request after the relay's changes: the model it forces, a stream's usage option."""
        r = self.relay(model="m-run")
        self.post(r, "u0001", "/chat/completions", dict(SHAPES["chat"][1], model="asked", stream=True))
        (line,) = exchanges.lines(self.store(r), "u0001")
        self.assertEqual((line["sent"]["model"], line["sent"]["stream_options"]), ("m-run", {"include_usage": True}))
        self.assertEqual(exchanges.rebuild(self.store(r), line), Upstream.seen[-1][2])


class StoredOnce(WithUpstream):
    def test_a_unit_that_resends_its_context_writes_each_part_and_picture_once(self):
        r = self.relay()
        history = [{"role": "system", "content": "be brief"}]
        sizes = 0
        for i in range(4):
            history.append({"role": "user", "content": [{"type": "text", "text": f"step {i}"},
                                                        {"type": "image_url", "image_url": {"url": data_url(picture(10 + i, 20_000))}}]})
            body = {"model": "m", "messages": history, "tools": [{"type": "function", "function": {"name": "click", "parameters": TOOL}}]}
            self.assertEqual(self.post(r, "u0003", "/chat/completions", body)[0], 200)
            sizes += len(Upstream.seen[-1][2])
            history.append({"role": "assistant", "content": f"did {i}"})
        store = self.store(r)
        with open(os.path.join(store, exchanges.PARTS)) as f:
            parts = exchanges.unmarked([json.loads(x) for x in f], exchanges.PARTS)
        hashes = [p["sha256"] for p in parts]
        self.assertEqual(len(hashes), len(set(hashes)))
        self.assertEqual(len(hashes), 1 + 4 + 3 + 1)          # the system text, four asks, three answers, the tool
        self.assertEqual(len(os.listdir(os.path.join(store, exchanges.BLOBS))), 4)
        lines = exchanges.lines(store, "u0003")
        self.assertEqual([x["n"] for x in lines], [1, 2, 3, 4])
        for x in lines:                                       # a call's line lists hashes alone
            self.assertEqual(set(x["sent"]["messages"]), {"parts"})
            self.assertTrue(all(len(h) == 64 and h in hashes for h in x["sent"]["messages"]["parts"]))
            self.assertTrue(exchanges.check(store, "u0003", x["n"])["same"])
        self.assertEqual(len(exchanges.new_parts(lines[1], lines[0])), 2)    # the answer before it, and the new ask
        self.assertEqual(exchanges.new_parts(lines[0]), lines[0]["sent"]["messages"]["parts"] + lines[0]["sent"]["tools"]["parts"])
        kept = sum(len(v) for v in self.files(store).values())
        self.assertLess(kept, sizes / 2)


class TextOnly(WithUpstream):
    def test_text_keeps_each_pictures_hash_type_and_size_and_none_of_its_bytes(self):
        r = self.relay("text")
        path, ask, pictures = SHAPES["messages"]
        self.assertEqual(self.post(r, "u0001", path, ask)[0], 200)
        store = self.store(r)
        self.assertFalse(os.path.exists(os.path.join(store, exchanges.BLOBS)))
        texts = self.files(store)
        for p, _ in pictures:
            self.assertFalse(any(b64(p).encode() in t for t in texts.values()))
            self.assertIn(json.dumps({"blob": hashlib.sha256(p).hexdigest(), "type": "image/png" if p is PNG else "image/jpeg",
                                      "bytes": len(p), "as": "base64"}, separators=(",", ":")).encode(), texts[exchanges.PARTS])
        got = exchanges.check(store, "u0001", 1)
        self.assertFalse(got["same"])
        self.assertIn("kept as text holds no pictures", got["why"])


class Bound(WithUpstream):
    def test_past_its_bound_a_units_lines_say_cut_and_its_calls_go_on_unchanged(self):
        r = self.relay(exchanges_max_mb=1)
        replies = []
        for i in range(5):
            content = [{"type": "image_url", "image_url": {"url": data_url(picture(100 + i, 300_000))}}] if i < 4 else "a last word"
            status, reply = self.post(r, "u0002", "/chat/completions", {"model": "m", "messages": [{"role": "user", "content": content}]})
            replies.append((status, json.loads(reply)["choices"][0]["message"]["content"]))
        self.assertEqual(replies, [(200, "you said: pictures")] * 4 + [(200, "you said: a last word")])
        lines = exchanges.lines(self.store(r), "u0002")
        self.assertEqual([x["n"] for x in lines], [1, 2, 3, 4, 5])
        self.assertEqual([bool(x.get("cut")) for x in lines], [False, False, False, True, True])   # the small last one too
        self.assertIn("bound of 1 MB", lines[3]["cut"])
        self.assertEqual([(c["n"], c["status"]) for c in r.calls("u0002")], [(n, 200) for n in range(1, 6)])
        self.assertLessEqual(sum(len(v) for v in self.files(self.store(r)).values()), 10 ** 6 + 2000)
        self.assertIn("not kept", exchanges.check(self.store(r), "u0002", 4)["why"])


class Off(WithUpstream):
    def test_off_keeps_no_store_and_the_call_record_of_before(self):
        ask = SHAPES["chat"][1]
        off, whole, default = self.relay("off"), self.relay("whole"), relay.Relay(self.base, KEY, os.path.join(self.tmp, "default")).start()
        self.addCleanup(default.stop)
        for r in (off, whole, default):
            self.assertEqual(self.post(r, "u0001", "/chat/completions", ask)[0], 200)
        self.assertFalse(os.path.exists(self.store(off)))
        self.assertFalse(os.path.exists(self.store(default)))      # the relay's own default: off
        normalized = lambda c: {k: 0.0 if k in ("t0", "t1") else v for k, v in c.items()}
        (a,), (b,), (c,) = off.calls("u0001"), whole.calls("u0001"), default.calls("u0001")
        # every record holds the request's digest; keeping the exchange or not leaves it as it is
        digests = {x["request"].pop("sha256") for x in (a, b, c)}
        self.assertEqual(len(digests), 1)
        expected = {
            "t0": 0.0, "t1": 0.0, "path": "/chat/completions", "model": "m", "model_asked": "m", "status": 200,
            "request": {"n_messages": 4, "last": [{"role": "assistant", "content": ""}, {"role": "tool", "content": ""}],
                        "stream": False, "reasoning_effort": None},
            "headers": ["content-type", "user-agent"],
            "reply": {"text": "you said: pictures", "tool_calls": [],
                      "usage": {"input": 10, "output": 2, "cached": 0, "reasoning": 0}},
        }
        self.assertEqual(normalized(a), expected)
        self.assertEqual(normalized(c), expected)
        self.assertEqual(normalized(b), dict(expected, n=1))

    def test_the_judges_and_the_simulated_users_routes_keep_the_record_alone(self):
        r = self.relay()
        for tag in ("u0001-judge", "u0001-simulator"):
            self.assertEqual(self.post(r, tag, "/chat/completions", SHAPES["chat"][1])[0], 200)
            self.assertNotIn("n", r.calls(tag)[-1])
        self.assertEqual(exchanges.tags(self.store(r)), [])


class NeverInTheWay(WithUpstream):
    def test_a_full_disk_neither_changes_nor_stops_the_reply_and_the_line_says_what_failed(self):
        path, ask, _ = SHAPES["chat"]
        clean = self.relay("off")
        expected = self.post(clean, "u0001", path, ask)
        r = self.relay()
        full = OSError(28, "No space left on device")
        with mock.patch.object(exchanges.Store, "_put", side_effect=full):
            self.assertEqual(self.post(r, "u0001", path, ask), expected)
        (line,) = exchanges.lines(self.store(r), "u0001")
        self.assertIn("No space left on device", line["error"])
        with mock.patch.object(exchanges.Store, "_put", side_effect=full), mock.patch.object(exchanges.Store, "_append", side_effect=full):
            self.assertEqual(self.post(r, "u0001", path, ask), expected)      # nothing of the store written: the reply still whole
        self.assertEqual([c["n"] for c in r.calls("u0001")], [1, 2])          # and the call record goes on
        self.assertEqual(len(exchanges.lines(self.store(r), "u0001")), 1)
        self.assertEqual(self.post(r, "u0001", path, ask), expected)          # room again: the next call is kept whole
        self.assertTrue(exchanges.check(self.store(r), "u0001", 3)["same"])

    def test_a_part_that_cannot_be_split_and_a_body_that_is_not_json_are_said_in_the_line(self):
        r = self.relay()
        path, ask, _ = SHAPES["chat"]
        with mock.patch.object(exchanges, "split", side_effect=TypeError("a part that is not JSON")):
            status, reply = self.post(r, "u0001", path, ask)
        self.assertEqual((status, json.loads(reply)["choices"][0]["message"]["content"]), (200, "you said: pictures"))
        status, reply = self.post(r, "u0001", path, None, raw=b"not json at all")
        self.assertEqual((status, reply), (400, b'{"error":{"message":"not JSON"}}'))
        first, second = exchanges.lines(self.store(r), "u0001")
        self.assertEqual(first["error"], "TypeError: a part that is not JSON")
        self.assertIn("not a JSON object", second["error"])
        self.assertEqual(second["sent_body"], "not json at all")
        self.assertTrue(exchanges.check(self.store(r), "u0001", 2)["same"])        # kept whole, it still rebuilds


class NoKey(WithUpstream):
    def test_a_keyless_custom_endpoint_keeps_a_call_that_can_be_replayed(self):
        env = {"MODEL_API_URL": self.base}
        base, key = run.provider_endpoint("custom", env)
        job = Path(self.tmp) / "job"
        r = relay.Relay(base, key, str(job / "task__u0001" / ".work" / "relay"), exchanges="whole").start()
        self.addCleanup(r.stop)
        req = {"model": "local-model", "max_tokens": 100, "messages": [{"role": "user", "content": "localhost"}]}
        status, body = self.post(r, "u0001", "/chat/completions", req, headers={"X-Trace-Id": "localhost"})
        self.assertEqual(status, 200)
        raw = Upstream.seen[-1][2]
        line, = exchanges.lines(self.store(r), "u0001")
        self.assertNotIn("key_cut", line)
        self.assertEqual(line["reply"]["body"], body.decode())
        self.assertEqual(exchanges.rebuild(self.store(r), line), raw)

    def test_the_relays_key_is_in_no_file_of_the_store(self):
        r = self.relay()
        ask = dict(SHAPES["messages"][1], system=f"the key is {KEY}, keep it",
                   tools=[{"name": "click", "description": f"say {KEY}", "input_schema": TOOL}])
        status, _ = self.post(r, "u0001", "/messages?k=" + KEY, ask, headers={"X-Trace-Id": KEY, "Authorization": "Bearer " + KEY})
        self.assertEqual(status, 200)
        self.assertIn(KEY.encode(), Upstream.seen[-1][2])                 # the agent sent it, and the relay sent it on
        for name, data in self.files(self.store(r)).items():
            self.assertNotIn(KEY.encode(), data, name)
        (line,) = exchanges.lines(self.store(r), "u0001")
        self.assertTrue(line["key_cut"])
        self.assertEqual({k.lower(): v for k, v in line["headers"].items()}["x-trace-id"], "[REDACTED]")
        self.assertFalse(any(k.lower() in ("authorization", "x-api-key") for k in line["headers"]))
        got = exchanges.check(self.store(r), "u0001", 1)
        self.assertFalse(got["same"])
        self.assertIn("key was cut out", got["why"])


class TheSidecar(WithUpstream):
    def test_the_sidecars_arguments_hold_both_values(self):
        argv = container.relay_argv("img", name="r", job="j", app="/app", record="/rec", key_file="/k", base="b", model="m", routes={},
                                    exchanges="text", exchanges_max_mb=64)
        self.assertEqual(argv[argv.index("--exchanges") + 1:argv.index("--exchanges") + 4], ["text", "--exchanges-max-mb", "64"])
        unit = container.Unit("job", "u0", os.path.join(self.tmp, "rec"), image="img", app="/app", base="b", key="k", model="m",
                              exchanges="whole", exchanges_max_mb=32)
        ran = []

        def docker(args, **kw):
            ran.append(args)
            if args[:1] == ["run"]:
                raise container.Unavailable("no engine in this test")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(container, "_docker", docker), self.assertRaises(container.Unavailable):
            unit.start()
        (started,) = [a for a in ran if a[:1] == ["run"]]
        self.assertEqual(started[started.index("--exchanges") + 1:started.index("--exchanges") + 4], ["whole", "--exchanges-max-mb", "32"])

    def test_a_relay_container_beside_local_processes_is_given_them(self):
        r = run.Run.__new__(run.Run)
        r.base, r.key, r.model, r.name, r.image, r.key_header = "http://b", "k", "m", "job", "img", "authorization"
        r.other, r.judge_model, r.simulator_model, r.backend, r.relay_container = {"_kind": "agent"}, "", "", "local", True
        r.serving, r.max_calls, r.call_seconds, r.exchanges, r.exchanges_max_mb = {}, None, relay.TIMEOUT, "text", 64
        made = {}

        class Unit:
            def __init__(self, *args, **kw):
                made.update(kw)

            def start(self):
                return self

            def url(self, tag):
                return "http://relay/" + tag
        with mock.patch.object(container, "Unit", Unit), mock.patch.object(run.Run, "ledger_file", return_value=None), \
                mock.patch.object(run.Run, "models_called", return_value=[]):
            got = r.unit_relay("u0", os.path.join(self.tmp, "trial"), {})
        self.assertEqual(got.unit_url, "http://relay/u0")
        self.assertEqual((made["exchanges"], made["exchanges_max_mb"]), ("text", 64))

    def test_the_relays_own_command_takes_them(self):
        """The sidecar's process: given ``text``, it keeps the call and none of its pictures' bytes."""
        key, record = os.path.join(self.tmp, "key"), os.path.join(self.tmp, "rec")
        with open(key, "w") as f:
            f.write(KEY)
        command = [sys.executable, "-m", "nullius.standard.calls.relay", "serve", "--listen", "127.0.0.1:0", "--base", self.base,
                   "--record", record, "--key-file", key]
        env = dict(os.environ, PYTHONPATH=APP)
        bad = subprocess.run(command + ["--exchanges-max-mb", "0"], cwd=APP, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("at least 1", bad.stdout)
        p = subprocess.Popen(command + ["--exchanges", "text", "--exchanges-max-mb", "8"], cwd=APP, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            at = json.loads(p.stdout.readline())["listening"]
            path, ask, _ = SHAPES["messages"]
            req = urllib.request.Request(f"{at}/u0001/v1{path}", data=json.dumps(ask).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as got:
                self.assertEqual(got.status, 200)
        finally:
            p.terminate()
            p.wait(timeout=20)
            p.stdout.close()
        store = os.path.join(record, exchanges.FOLDER)
        self.assertEqual([x["n"] for x in exchanges.lines(store, "u0001")], [1])
        self.assertFalse(os.path.exists(os.path.join(store, exchanges.BLOBS)))


class TheRun(WithUpstream):
    """A whole run of the fake agent, its first call refused: a retried try's store goes aside with it."""

    def setUp(self):
        super().setUp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", self._home)
        self.addCleanup(setattr, pacing, "FIRST", pacing.FIRST)
        pacing.FIRST = 0.5
        self.env = {"MODEL_API_URL": self.base, "MODEL_API_KEY": KEY}

    def go(self, **kw):
        b = dict(definitions.get("benchmark", "fakebench", ROOT), datasets=[{"id": "responding", "units": 3}])
        return run.run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                       concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, dataset="responding", **kw)

    def test_a_retried_try_keeps_its_store_and_the_next_try_starts_an_empty_one(self):
        Upstream.refuse = 1
        job_dir = self.go(retries=1)
        (kept,) = os.listdir(os.path.join(job_dir, "retried"))
        name = kept.rpartition(".try-")[0]
        tag = name.rsplit("__", 1)[1]
        before = os.path.join(job_dir, "retried", kept, ".work", "relay", exchanges.FOLDER)
        now = os.path.join(job_dir, name, ".work", "relay", exchanges.FOLDER)
        self.assertEqual([(x["n"], x["reply"]["status"]) for x in exchanges.lines(before, tag)], [(1, 429)])
        self.assertEqual([(x["n"], x["reply"]["status"]) for x in exchanges.lines(now, tag)], [(1, 200)])
        self.assertTrue(exchanges.check(now, tag, 1)["same"])          # its own parts: nothing read from the try before
        with open(os.path.join(job_dir, "config.json")) as f:
            said = json.load(f)["standard"]
        self.assertEqual((said["exchanges"], said["exchanges_max_mb"]), ("whole", 256))
        for root, _, names in os.walk(job_dir):
            for n in names:
                with open(os.path.join(root, n), "rb") as f:
                    self.assertNotIn(KEY.encode(), f.read(), os.path.join(root, n))

        # the command: the request rebuilt, on stdout as the bytes sent, the verdict on stderr
        (j,) = parse.load_all([os.path.join(self.tmp, "jobs")])
        sent = [raw for _, _, raw in Upstream.seen if b"model hi" in raw][-1]
        code, out, err = self.show(j.job_id, name, "--call", "1", "--request")
        self.assertEqual((code, out), (0, sent + b"\n"))
        self.assertIn("the recorded sent_sha256", err)
        with open(os.path.join(now, exchanges.PARTS)) as f:
            text = f.read()
        with open(os.path.join(now, exchanges.PARTS), "w") as f:
            f.write(text.replace("model hi", "model HI"))
        code, out, err = self.show(j.job_id, name, "--call", "1", "--request")
        self.assertEqual(code, runs_cli.EXIT_FAILED)
        self.assertIn("not the recorded sent_sha256", err)
        self.assertEqual(self.show(j.job_id, name, "--call", "9", "--request")[0], runs_cli.EXIT_NOT_FOUND)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as usage:
            runs_cli.main(["show", os.path.join(self.tmp, "jobs"), j.job_id, name, "--call", "1"])
        self.assertEqual(usage.exception.code, 2)

    def test_an_earlier_try_is_read_by_its_name_call_by_call_as_for_one_request(self):
        # a try kept in retried/ goes by its folder's name (NAME.try-N): --calls reads it as --call N --request does
        Upstream.refuse = 1
        job_dir = self.go(retries=1)
        (kept,) = os.listdir(os.path.join(job_dir, "retried"))
        name = kept.rpartition(".try-")[0]
        (j,) = parse.load_all([os.path.join(self.tmp, "jobs")])
        statuses = {}
        for trial in (kept, name):
            code, out, err = self.show(j.job_id, trial, "--calls")
            self.assertEqual(code, 0, err)
            statuses[trial] = [c["status"] for c in json.loads(out)["calls"]]
        self.assertEqual(statuses, {kept: [429], name: [200]})          # each try's own calls
        self.assertEqual(self.show(j.job_id, kept, "--call", "1", "--request")[0], 0)
        self.assertEqual(self.show(j.job_id, name + ".try-9", "--calls")[0], runs_cli.EXIT_NOT_FOUND)

    def show(self, job_id, trial, *more):
        out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.StringIO()
        with mock.patch.object(sys, "stdout", out), contextlib.redirect_stderr(err):
            code = runs_cli.main(["show", os.path.join(self.tmp, "jobs"), job_id, trial, *more])
        out.flush()
        return code, out.buffer.getvalue(), err.getvalue()

    def test_off_and_a_bad_value_and_the_command_line(self):
        job_dir = self.go(exchanges="off", exchanges_max_mb=1, name="off")
        # a task that calls the model, run again in the job, keeps the job's choice
        b = dict(definitions.get("benchmark", "fakebench", ROOT), datasets=[{"id": "responding", "units": 3}])
        with mock.patch.object(rerun, "resolve", return_value=(b, definitions.get("agent", "fake", ROOT))):
            got = rerun.rerun(job_dir, ["model"], env=self.env, say=lambda s: None, root=ROOT)
        self.assertEqual([x["unit"] for x in got["replaced"]], ["model"])
        for root, dirs, _ in os.walk(job_dir):
            self.assertNotIn(exchanges.FOLDER, dirs, root)
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["exchanges"], "off")
        for bad in ({"exchanges": "all"}, {"exchanges_max_mb": 0}, {"exchanges_max_mb": True}, {"exchanges_max_mb": 1.5}):
            with self.subTest(bad=bad), self.assertRaises(run.Refused):
                self.go(name="bad", **bad)
        given = {}
        b, o = definitions.get("benchmark", "fakebench", ROOT), definitions.get("agent", "fake", ROOT)
        with mock.patch.object(standard_cli, "_bench", return_value=b), mock.patch.object(standard_cli, "_other", return_value=o), \
                mock.patch.object(standard_cli, "licences", return_value=(None, None)), mock.patch.object(standard_cli, "_exit_on_term"), \
                mock.patch.object(standard_cli.run, "run_and_report", side_effect=lambda *a, **kw: given.update(kw) or ("job", False)), \
                mock.patch.object(standard_cli.progress, "ECHO", None), contextlib.redirect_stdout(io.StringIO()):
            standard_cli.main(["run", "fakebench", "fake", "--model", "m-1", "--from-run"])
            self.assertEqual((given["exchanges"], given["exchanges_max_mb"]), ("whole", 256))
            standard_cli.main(["run", "fakebench", "fake", "--model", "m-1", "--from-run", "--exchanges", "text", "--exchanges-max-mb", "64"])
            self.assertEqual((given["exchanges"], given["exchanges_max_mb"]), ("text", 64))


class Recipe(unittest.TestCase):
    def test_a_recipe_takes_them_and_says_them_only_when_not_the_default(self):
        defs = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
        base = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
                "other": "fake", "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": True, "where": "local"}
        out = recipe.compile(base, defs)
        self.assertEqual(out["errors"], [])
        self.assertNotIn("--exchanges", out["script"])
        out = recipe.compile(dict(base, exchanges="text", exchanges_max_mb=64), defs)
        self.assertEqual(out["errors"], [])
        self.assertIn("--exchanges text --exchanges-max-mb 64 ", out["script"])
        self.assertIn(["Model exchanges", "text · at most 64 MB per task"], [row[1:] for row in out["review"]])
        for bad in ({"exchanges": "all"}, {"exchanges_max_mb": 0}, {"exchanges_max_mb": "x"}):
            errors = recipe.compile(dict(base, **bad), defs)["errors"]
            self.assertEqual([e["step"] for e in errors], [recipe.STEP["limits"]], bad)


class Reasoning(unittest.TestCase):
    def test_read_reply_gives_the_reasoning_only_when_asked(self):
        for path, body, streamed in (("/chat/completions", json.dumps(CHAT).encode(), False), ("/chat/completions", CHAT_SSE, True),
                                     ("/messages", json.dumps(MESSAGE).encode(), False), ("/messages", MESSAGE_SSE, True),
                                     ("/responses", json.dumps(RESPONSE).encode(), False), ("/responses", RESPONSE_SSE, True)):
            with self.subTest(path=path, streamed=streamed):
                got = relay.read_reply(path, body, streamed, reasoning=True)
                self.assertEqual((got["text"], got["reasoning"]), ("ok", "I looked"))
                self.assertNotIn("reasoning", relay.read_reply(path, body, streamed))     # the call record's reading, unchanged
        thought = {"choices": [{"message": {"content": "ok", "reasoning_content": "deep"}}]}
        self.assertEqual(relay.read_reply("/chat/completions", json.dumps(thought).encode(), False, reasoning=True)["reasoning"], "deep")


class Version(unittest.TestCase):
    """Each file of a store names the version of its format on its first line; a store without it is version 1."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    @staticmethod
    def keep(record_dir):
        store = exchanges.Store(record_dir)
        for n, req in enumerate(({"messages": [data_url(PNG), "hello"]}, {"messages": [data_url(PNG), "hello", "again"]}), 1):
            store.write("u0001", n, 1.0, "/messages", req, json.dumps(req).encode(), {}, 200, False, b"{}")
        return store

    def test_a_new_store_holds_the_mark_and_grows_by_it_alone_and_reads_as_one_without_it(self):
        marked = self.keep(str(self.root / "marked"))
        with mock.patch.object(exchanges, "HEADER", ""):           # the store as written before the mark
            plain = self.keep(str(self.root / "plain"))
        got, before = WithUpstream.files(marked.dir), WithUpstream.files(plain.dir)
        self.assertEqual(exchanges.HEADER, '{"schema": "nullius.exchanges/v1"}\n')
        self.assertEqual(sorted(got), sorted(before))
        for name in got:
            self.assertEqual(got[name], (exchanges.HEADER.encode() if name.endswith(".jsonl") else b"") + before[name], name)
        self.assertEqual(sum(map(len, got.values())) - sum(map(len, before.values())), 2 * len(exchanges.HEADER))
        self.assertEqual((marked.size, plain.size), (sum(map(len, got.values())), sum(map(len, before.values()))))
        self.assertEqual(exchanges.lines(marked.dir, "u0001"), exchanges.lines(plain.dir, "u0001"))
        self.assertEqual(exchanges.stored_parts(marked.dir), exchanges.stored_parts(plain.dir))
        for store in (marked, plain):
            for n in (1, 2):
                self.assertTrue(exchanges.check(store.dir, "u0001", n)["same"])

    def test_pending_marks_count_towards_the_first_calls_bound(self):
        probe = exchanges.Store(str(self.root / "probe"))
        req = {"messages": ["x"]}
        probe.write("u0001", 1, 1.0, "/messages", req, json.dumps(req).encode(), {}, 200, False, b"{}")
        size = sum(map(len, WithUpstream.files(probe.dir).values()))
        for extra in (0, 1, 2 * len(exchanges.HEADER.encode())):
            with self.subTest(extra=extra):
                store = exchanges.Store(str(self.root / str(extra)), max_mb=1)
                req = {"messages": ["x" * (store.bound - size + 1 + extra)]}
                store.write("u0001", 1, 1.0, "/messages", req, json.dumps(req).encode(), {}, 200, False, b"{}")
                line, = exchanges.lines(store.dir, "u0001")
                self.assertEqual("cut" in line, extra > 0)
                self.assertEqual(store.size, sum(map(len, WithUpstream.files(store.dir).values())))
                if extra == 0:
                    self.assertEqual(store.size, store.bound)
                    self.assertTrue(exchanges.check(store.dir, "u0001", 1)["same"])
                else:
                    self.assertFalse((Path(store.dir) / exchanges.PARTS).exists())
                    store.write("u0001", 2, 1.0, "/messages", {}, b"{}", {}, 200, False, b"{}")
                    self.assertIn("cut", exchanges.lines(store.dir, "u0001")[1])

    def test_later_parts_are_refused_before_raw_cut_or_missing_requests(self):
        trial = mock.Mock(dir=str(self.root / "task__u0001"), trial_name="task__u0001")
        store = self.keep(os.path.join(trial.dir, ".work", "relay"))
        store.write("u0001", 3, 1.0, "/messages", None, b"raw request", {}, 400, False, b"{}")
        with open(Path(store.dir) / "u0001.jsonl", "a") as f:
            f.write(json.dumps({"n": 4, "cut": "the store reached its bound"}) + "\n")
            f.write(json.dumps({"n": 5, "error": "the request was not kept"}) + "\n")
        path = Path(store.dir) / exchanges.PARTS
        path.write_text(json.dumps({"schema": "nullius.exchanges/v2"}) + "\n" + path.read_text().split("\n", 1)[1])
        for n in (1, 3, 4, 5):
            with self.subTest(n=n):
                out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.StringIO()
                self.addCleanup(out.close)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = runs_cli._request(trial, n)
                out.flush()
                self.assertEqual((code, out.buffer.getvalue()), (runs_cli.EXIT_NOT_FOUND, b""))
                self.assertIn("version 2", err.getvalue())
                self.assertIn("version 1", err.getvalue())

    def test_a_later_version_or_another_format_is_refused_by_each_reader_naming_both(self):
        for name in ("u0001.jsonl", exchanges.PARTS):
            for said, words in (("nullius.exchanges/v2", ("version 2", "version 1")),
                                ("another.format/v1", ("'another.format/v1'", "version 1"))):
                with self.subTest(name=name, said=said):
                    trial = mock.Mock(dir=str(self.root / name / said.replace("/", "-") / "task__u0001"), trial_name="task__u0001")
                    store = self.keep(os.path.join(trial.dir, ".work", "relay"))
                    path = Path(store.dir) / name
                    path.write_text(json.dumps({"schema": said}) + "\n" + path.read_text().split("\n", 1)[1])
                    reads = [lambda: exchanges.check(store.dir, "u0001", 1)]
                    reads.append((lambda: exchanges.lines(store.dir, "u0001")) if name != exchanges.PARTS
                                 else (lambda: exchanges.stored_parts(store.dir)))
                    for read in reads:
                        with self.assertRaises(exchanges.Later) as refused:
                            read()
                        for word in (name,) + words:
                            self.assertIn(word, str(refused.exception))
                    out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.StringIO()
                    self.addCleanup(out.close)
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = runs_cli._request(trial, 1)
                    out.flush()
                    self.assertEqual((code, out.buffer.getvalue()), (runs_cli.EXIT_NOT_FOUND, b""))
                    for word in words:
                        self.assertIn(word, err.getvalue())


class Unmarked:
    """The tests again on stores written without the mark of their format, as before it: they read the same."""

    def setUp(self):
        patch = mock.patch.object(exchanges, "HEADER", "")
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()


class UnmarkedStoreFiles(Unmarked, StoreFiles):
    pass


class UnmarkedByteForByte(Unmarked, ByteForByte):
    pass


class UnmarkedStoredOnce(Unmarked, StoredOnce):
    pass


class UnmarkedTextOnly(Unmarked, TextOnly):
    pass


class UnmarkedBound(Unmarked, Bound):
    pass


class UnmarkedNeverInTheWay(Unmarked, NeverInTheWay):
    pass


class UnmarkedNoKey(Unmarked, NoKey):
    pass


class UnmarkedTheRun(Unmarked, TheRun):
    pass


if __name__ == "__main__":
    unittest.main()
