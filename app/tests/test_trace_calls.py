"""The transcript by model call: what each call sent that the call before had
not, the answer whole, the steps under each call, and the pictures by hash.
Fixture trials are written with the relay's own store (``exchanges.Store``);
no relay runs, no network."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from nullius.runs import cli as runs_cli, parse
from nullius.standard.calls import exchanges, relay
from nullius.traces import calls, trajectory

T0 = 1_790_000_000.0


def picture(i: int, size: int = 3000) -> bytes:
    seed = hashlib.sha256(str(i).encode()).digest()
    return b"\x89PNG\r\n\x1a\n" + (seed * (size // len(seed) + 1))[:size]


def data_url(raw: bytes, kind: str = "image/png") -> str:
    return f"data:{kind};base64,{base64.b64encode(raw).decode()}"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def chat_reply(text="ok", tool=None, reasoning="", provider="Prov"):
    m = {"role": "assistant", "content": text}
    if reasoning:
        m["reasoning"] = reasoning
    if tool:
        m["tool_calls"] = [{"id": "c", "type": "function", "function": {"name": tool[0], "arguments": tool[1]}}]
    return json.dumps({"choices": [{"message": m, "finish_reason": "tool_calls" if tool else "stop"}], "provider": provider,
                       "usage": {"prompt_tokens": 100, "completion_tokens": 10}}).encode()


def write_trial(folder: str, exchanges_kept, *, mode="whole", tag="u0001", steps=(), record_n=True):
    """A trial at ``folder``: ``exchanges_kept`` is a list of calls, each a dict of ``path``, ``req``, ``status``,
    ``body``, ``streamed`` and the call record's own fields (``refused``, ``error``, ``side``); a call with
    ``req`` None was refused before it was sent. ``mode`` None keeps the call record alone."""
    record_dir = os.path.join(folder, ".work", "relay")
    os.makedirs(os.path.join(folder, "agent"), exist_ok=True)
    store = exchanges.Store(record_dir, mode) if mode else None
    rows = []
    for n, c in enumerate(exchanges_kept, 1):
        t0 = T0 + 10 * n
        path, req, status = c.get("path", "/chat/completions"), c.get("req"), c.get("status", 200)
        body, streamed = c.get("body", b""), c.get("streamed", False)
        rec = {"t0": t0, "t1": t0 + 2, "path": path, "model": "m", "model_asked": "m", "status": status,
               "request": relay._request_tail(req) if req else {}, "headers": []}
        if record_n and store is not None:
            rec = {"n": n, **rec}
        for k in ("refused", "error", "side"):
            if k in c:
                rec[k] = c[k]
        if status == 200:
            rec["reply"] = relay.read_reply(path, body, streamed)
            rec["reply"].update({k: v for k, v in relay.reply_facts(path, body, streamed).items() if k not in rec["reply"]})
            rec["cost_usd"], rec["cost_from"] = 0.001 * n, "provider"
        elif "error" not in rec:
            rec["error"] = relay._said(status, body)
        rows.append(rec)
        if store is not None and req is not None:
            store.write(tag, n, t0, path, req, json.dumps(req).encode(), {"Content-Type": "application/json"},
                        status, streamed, body)
    with open(os.path.join(folder, "agent", "calls.jsonl"), "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    with open(os.path.join(folder, "agent", "trajectory.json"), "w") as f:
        json.dump({"steps": [{"step_id": i + 1, "source": src, "timestamp": iso(at), "message": msg}
                             for i, (src, at, msg) in enumerate(steps)]}, f)
    return folder


def load(folder: str) -> dict:
    return calls.view(folder, trajectory.load(os.path.join(folder, "agent", "trajectory.json")))


SYSTEM = {"role": "system", "content": "be brief"}
TOOLS = [{"type": "function", "function": {"name": "click", "parameters": {}}},
         {"type": "function", "function": {"name": "type_text", "parameters": {}}}]
P1, P2, P3 = picture(1), picture(2), picture(3)


def history():
    """Four calls of a chat agent that resends its context; the fourth replaces an earlier picture."""
    user = {"role": "user", "content": [{"type": "text", "text": "open the file"}, {"type": "image_url", "image_url": {"url": data_url(P1)}}]}
    a1 = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "click", "arguments": "{\"x\": 1}"}}]}
    r1 = {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "clicked"}, {"type": "image_url", "image_url": {"url": data_url(P2)}}]}
    a2 = {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "type_text", "arguments": "{}"}}]}
    r2 = {"role": "tool", "tool_call_id": "c2", "content": "typed"}
    more = {"role": "user", "content": "and save it"}
    r1_cut = {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "clicked"}, {"type": "text", "text": "[picture left out]"}]}
    a3 = {"role": "assistant", "content": "saving"}
    r3 = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url(P3)}}]}
    reqs = [[SYSTEM, user], [SYSTEM, user, a1, r1], [SYSTEM, user, a1, r1, a2, r2, more], [SYSTEM, user, a1, r1_cut, a2, r2, more, a3, r3]]
    replies = [chat_reply(tool=("click", "{\"x\": 1}")), chat_reply(tool=("type_text", "{}")), chat_reply("saving"), chat_reply("done")]
    return [{"req": {"model": "m", "messages": msgs, "tools": TOOLS}, "body": body} for msgs, body in zip(reqs, replies)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-calls-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def trial(self, kept, name="task__u0001", **kw):
        return write_trial(os.path.join(self.tmp, "job", name), kept, **kw)


class SentIsWhatIsNew(Base):
    def test_search_finds_rewritten_text_in_the_folded_section(self):
        kept = history()
        kept[3]["req"]["messages"][3]["content"][1]["text"] = "unique-rewritten-marker"
        v = load(self.trial(kept))
        page = calls.page(v, query="UNIQUE-REWRITTEN-MARKER", section="folded")
        self.assertEqual(page["matches"], 1)
        self.assertEqual(page["selected"]["number"], 4)
        self.assertIn("unique-rewritten-marker", page["selected"]["content"]["text"])

    def test_each_call_sends_only_what_the_call_before_lacked_pictures_by_hash(self):
        v = load(self.trial(history()))
        self.assertEqual(v["record"], "exchanges")
        sent = [[(i["kind"], i.get("text"), [p["sha256"] for p in i["pictures"]]) for i in c["sent"]["items"]] for c in v["calls"]]
        self.assertEqual([k for k, _, _ in sent[0]], ["system", "tools", "user"])        # the first: system text and tools, then the prompt
        self.assertEqual(sent[0][0][1], "be brief")
        self.assertEqual(v["calls"][0]["sent"]["items"][1]["names"], ["click", "type_text"])
        self.assertEqual(sent[0][2][2], [sha(P1)])
        self.assertEqual(sent[1], [("tool result", "clicked\n[picture " + sha(P2)[:12] + "]", [sha(P2)])])   # the answer sent back is left out
        self.assertEqual([(k, t) for k, t, _ in sent[2]], [("tool result", "typed"), ("user", "and save it")])
        self.assertEqual(sent[3], [("user", "[picture " + sha(P3)[:12] + "]", [sha(P3)])])
        fourth = v["calls"][3]["sent"]
        self.assertEqual((len(fourth["rewritten"]), fourth["dropped"]), (1, 1))         # the picture replaced: one folded line
        self.assertEqual(fourth["rewritten"][0]["text"], "clicked\n[picture left out]")
        self.assertEqual(v["calls"][1]["sent"]["items"][0]["tool_call_id"], "c1")
        text = json.dumps(calls.document(v))
        for raw in (P1, P2, P3):
            self.assertNotIn(base64.b64encode(raw).decode()[:40], text)                # named by hash, never inlined
        self.assertEqual(set(v["pictures"]), {sha(P1), sha(P2), sha(P3)})
        page = calls.page(v, position=3)["selected"]
        self.assertEqual([s["key"] for s in page["sections"]][-2:], ["folded", "text"])
        self.assertEqual(page["sent"]["folded"], {"key": "folded", "rewritten": 1, "dropped": 1})

    def test_the_three_shapes_name_their_tool_results_and_new_prompts(self):
        tool = {"type": "object"}
        messages = [
            {"path": "/messages", "req": {"model": "m", "system": "sys", "tools": [{"name": "click", "input_schema": tool}],
                                          "messages": [{"role": "user", "content": "go"}]},
             "body": json.dumps({"type": "message", "content": [{"type": "tool_use", "id": "t1", "name": "click", "input": {}}],
                                 "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()},
            {"path": "/messages", "req": {"model": "m", "system": "sys", "tools": [{"name": "click", "input_schema": tool}],
                                          "messages": [{"role": "user", "content": "go"},
                                                       {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "click", "input": {}}]},
                                                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
                                                           {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                                        "data": base64.b64encode(P1).decode()}}]}]}]},
             "body": json.dumps({"type": "message", "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()}]
        v = load(self.trial(messages))
        self.assertEqual([i["kind"] for i in v["calls"][0]["sent"]["items"]], ["system", "tools", "user"])
        (result,) = v["calls"][1]["sent"]["items"]
        self.assertEqual((result["kind"], result["tool_call_id"], [p["sha256"] for p in result["pictures"]]), ("tool result", "t1", [sha(P1)]))
        responses = [
            {"path": "/responses", "req": {"model": "m", "instructions": "sys", "input": [{"role": "user", "content": "go"}]},
             "body": json.dumps({"object": "response", "status": "completed",
                                 "output": [{"type": "function_call", "call_id": "c1", "name": "click", "arguments": "{}"}]}).encode()},
            {"path": "/responses", "req": {"model": "m", "instructions": "sys", "input": [
                {"role": "user", "content": "go"}, {"type": "function_call", "call_id": "c1", "name": "click", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "clicked"}]},
             "body": json.dumps({"object": "response", "status": "completed", "output": []}).encode()}]
        v = load(self.trial(responses, name="task__u0002", tag="u0002"))
        self.assertEqual([i["kind"] for i in v["calls"][0]["sent"]["items"]], ["system", "user"])
        self.assertEqual([(i["kind"], i["text"], i["tool_call_id"]) for i in v["calls"][1]["sent"]["items"]], [("tool result", "clicked", "c1")])

    def test_a_side_call_is_compared_with_the_side_call_before_it(self):
        kept = history()[:2]
        kept.insert(1, {"req": {"model": "m", "messages": [{"role": "user", "content": "summarise"}]}, "body": chat_reply("short"), "side": True})
        v = load(self.trial(kept))
        self.assertEqual([i["kind"] for i in v["calls"][1]["sent"]["items"]], ["user"])
        self.assertEqual([i["kind"] for i in v["calls"][2]["sent"]["items"]], ["tool result"])


LONG = json.dumps({"text": "x" * 3000})


def answers():
    """(path, body, streamed) of a reply with reasoning, text and a tool call of more than 2,000 characters, by shape."""
    chat = json.dumps({"choices": [{"message": {"content": "ok", "reasoning": "I looked", "tool_calls": [
        {"function": {"name": "type_text", "arguments": LONG}}]}, "finish_reason": "tool_calls"}], "provider": "Prov",
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
    chat_sse = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in [
        {"provider": "Prov", "choices": [{"delta": {"reasoning": "I looked"}}]},
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "type_text", "arguments": LONG[:1500]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": LONG[1500:]}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}]) + b"data: [DONE]\n\n"
    msg = json.dumps({"type": "message", "provider": "Prov", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "I looked", "signature": "s"}, {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "t", "name": "type_text", "input": {"text": "x" * 3000}}],
        "usage": {"input_tokens": 5, "output_tokens": 2}}).encode()
    events = [{"type": "message_start", "message": {"provider": "Prov", "usage": {"input_tokens": 5}}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "I looked"}},
              {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "ok"}},
              {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t", "name": "type_text", "input": {}}},
              {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": LONG[:1500]}},
              {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": LONG[1500:]}},
              {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}]
    msg_sse = b"".join(f"event: {e['type']}\ndata: ".encode() + json.dumps(e).encode() + b"\n\n" for e in events)
    response = {"object": "response", "status": "completed", "provider": "Prov", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "I looked"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
        {"type": "function_call", "call_id": "c", "name": "type_text", "arguments": LONG}],
        "usage": {"input_tokens": 5, "output_tokens": 2}}
    resp_sse = b"event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": response}).encode() + b"\n\n"
    return [("/chat/completions", chat, False, "tool_calls"), ("/chat/completions", chat_sse, True, "tool_calls"),
            ("/messages", msg, False, "tool_use"), ("/messages", msg_sse, True, "tool_use"),
            ("/responses", json.dumps(response).encode(), False, "completed"), ("/responses", resp_sse, True, "completed")]


class AnswerIsWhole(Base):
    def test_reasoning_text_whole_arguments_end_and_provider_for_each_shape_streamed_or_not(self):
        for i, (path, body, streamed, end) in enumerate(answers()):
            with self.subTest(path=path, streamed=streamed):
                req = {"model": "m", "stream": streamed, **({"input": "go"} if path == "/responses" else {"messages": [{"role": "user", "content": "go"}]})}
                v = load(self.trial([{"path": path, "req": req, "body": body, "streamed": streamed}], name=f"t__u{i:04d}", tag=f"u{i:04d}"))
                a = v["calls"][0]["answer"]
                self.assertEqual((a["text"], a["reasoning"], a["end"], a["served_by"], a["abridged"]), ("ok", "I looked", end, "Prov", False))
                (tool,) = a["tool_calls"]
                self.assertEqual(json.loads(tool["arguments"]), {"text": "x" * 3000})      # whole: the call record cut it to 2,000
                page = calls.page(v)["selected"]
                self.assertEqual([x["label"] for x in page["answer"]["items"]], ["Reasoning", "Text", "type_text"])
                tail = calls.page(v, section="tool-0", chunk=9)["selected"]["content"]
                self.assertTrue(tail["text"].endswith("x" * 50 + "\"}"))


class Numbering(Base):
    def test_answered_calls_count_up_and_a_refused_or_failed_call_keeps_its_place_unnumbered(self):
        h = history()
        kept = [h[0], {"req": h[1]["req"], "status": 429, "body": b'{"error": {"message": "slow down"}}'},
                {"req": None, "status": 402, "refused": "calls", "error": "the unit's cap of 1 model calls was reached"}, h[1]]
        v = load(self.trial(kept))
        self.assertEqual([(c["number"], c["label"]) for c in v["calls"]], [(1, "Call 1"), (None, "Failed"), (None, "Refused"), (2, "Call 2")])
        self.assertEqual(v["calls"][2]["sent"], {"recorded": False, "note": "Not sent: refused before it went out"})
        self.assertIsNone(v["calls"][1]["answer"])
        self.assertEqual(calls.page(v, position=1)["selected"]["sections"][-1]["label"], "Error")
        # the failed call's request is Sent against the call before; the next answered one against the failed one
        self.assertEqual([i["kind"] for i in v["calls"][3]["sent"]["items"]], [])


class StepsUnderCalls(Base):
    def test_a_prompt_with_several_calls_has_each_step_under_the_call_before_it(self):
        steps = [("user", T0 + 1, "open the file"), ("agent", T0 + 11, "looking"), ("agent", T0 + 15, "(tool use)"),
                 ("agent", T0 + 21, "typing"), ("agent", T0 + 35, "saved"), ("agent", T0 + 45, "done")]
        v = load(self.trial(history(), steps=steps))
        self.assertEqual([s["step_id"] for s in v["before"]], [1])
        self.assertEqual([[s["step_id"] for s in c["steps"]] for c in v["calls"]], [[2, 3], [4], [5], [6]])
        page = calls.page(v, position=0)["selected"]
        self.assertEqual([s["step_id"] for s in page["before"]], [1])
        self.assertEqual(calls.page(v, step="5")["selected"]["number"], 3)


class ThreeKinds(Base):
    def test_a_trial_with_the_call_record_alone_or_with_none(self):
        alone = load(self.trial(history(), mode=None))
        self.assertEqual(alone["record"], "calls")
        first = alone["calls"][0]
        self.assertEqual(first["sent"], {"recorded": False, "note": "Not recorded"})
        self.assertEqual((first["number"], first["answer"]["abridged"], first["answer"]["tool_calls"][0]["name"]), (1, True, "click"))
        self.assertEqual(first["tokens"], {"input": 100, "output": 10, "cached": 0, "reasoning": 0})
        self.assertEqual((first["cost_usd"], first["seconds"]), (0.001, 2.0))
        self.assertEqual(alone["calls"][2]["answer"]["text"], "saving")
        selected = calls.page(alone, position=2, section="text")["selected"]
        self.assertTrue(selected["answer"]["abridged"])
        self.assertEqual(selected["content"]["text"], "saving")
        none = os.path.join(self.tmp, "harbor", "task__x")
        os.makedirs(os.path.join(none, "agent"))
        self.assertEqual((calls.recorded(none), load(none)["record"], load(none)["calls"]), (0, None, []))
        with open(os.path.join(none, "agent", "calls.jsonl"), "w"):
            pass                                  # a control: its record is empty
        self.assertEqual(calls.view(none)["record"], None)

    def test_text_keeps_each_pictures_type_and_size_and_no_bytes(self):
        v = load(self.trial(history(), mode="text"))
        (pic,) = v["calls"][0]["sent"]["items"][2]["pictures"]
        self.assertEqual(pic, {"sha256": sha(P1), "type": "image/png", "bytes": len(P1), "stored": False})
        with self.assertRaises(LookupError):
            calls.picture(v, sha(P1))


class LaterVersion(Base):
    def test_a_later_store_without_call_records_keeps_its_refusal_on_the_page(self):
        for name in ("u0001.jsonl", exchanges.PARTS):
            with self.subTest(name=name):
                folder = self.trial(history(), name=name.split(".")[0] + "__u0001")
                os.unlink(os.path.join(folder, calls.CALLS))
                self.assertFalse(os.path.exists(os.path.join(folder, calls.RELAY, "u0001.jsonl")))
                self.assertEqual(len(load(folder)["calls"]), 4)
                path = os.path.join(calls.store_folder(folder), name)
                with open(path) as f:
                    rows = f.read().split("\n", 1)
                with open(path, "w") as f:
                    f.write(json.dumps({"schema": "nullius.exchanges/v2"}) + "\n" + rows[1])
                v = load(folder)
                self.assertEqual((v["calls"], v["pictures"]), ([], {}))
                for page in (calls.page(v), calls.page(v, query="missing", step=1)):
                    self.assertEqual((page["total"], page["rows"], page["selected"]), (0, [], None))
                    self.assertIn("version 2", page["notice"])
                    self.assertIn("version 1", page["notice"])
                    self.assertEqual(page["notice"], v["notice"])

    def test_a_store_in_a_later_format_is_not_read_and_each_call_says_both_versions(self):
        for name in ("u0001.jsonl", exchanges.PARTS):
            with self.subTest(name=name):
                folder = self.trial(history(), name=name.split(".")[0] + "__u0001")
                path = os.path.join(calls.store_folder(folder), name)
                with open(path) as f:
                    rows = f.read().split("\n", 1)
                self.assertEqual(rows[0] + "\n", exchanges.HEADER)
                with open(path, "w") as f:
                    f.write(json.dumps({"schema": "nullius.exchanges/v2"}) + "\n" + rows[1])
                v = load(folder)
                self.assertEqual((v["record"], v["pictures"], len(v["calls"])), ("calls", {}, 4))
                for c in v["calls"]:
                    self.assertEqual(c["sent"], {"recorded": False, "unreadable": True, "note":
                                                 f"Unreadable: {name} is in version 2 of the exchange store's format; "
                                                 "this Nullius reads version 1: read it with a later Nullius"})
                self.assertEqual((v["calls"][3]["answer"]["text"], v["calls"][3]["answer"]["abridged"]), ("done", True))
                self.assertIn("version 2", calls.page(v)["selected"]["sent"]["note"])
                # each call says why: the page's own notices stay as they were
                self.assertEqual([calls.page(v, **ask)["notice"] for ask in ({}, {"query": "zzzz"}, {"step": "none"})],
                                 ["", "No calls match this search.", "No call in this search holds that source step."])
                with self.assertRaises(LookupError):
                    calls.picture(v, sha(P1))


class Unreadable(Base):
    def test_every_call_naming_a_damaged_part_is_unreadable(self):
        for damage in ("truncated", "picture"):
            with self.subTest(damage=damage):
                kept = history()[:2] + [{"req": {"messages": [{"role": "user", "content": "fresh"}]},
                                        "body": chat_reply("done")}]
                folder = self.trial(kept, name=damage + "__u0001")
                parts = os.path.join(calls.store_folder(folder), exchanges.PARTS)
                with open(parts) as f:
                    rows = f.read().splitlines()
                rows = [(row[:40] if damage == "truncated" else row.replace(sha(P1), "../outside"))
                        if sha(P1) in row else row for row in rows]
                with open(parts, "w") as f:
                    f.write("\n".join(rows) + "\n")
                v = load(folder)
                self.assertEqual([c["sent"].get("unreadable", False) for c in v["calls"]], [True, True, False])
                self.assertEqual([r["unreadable"] for r in calls.page(v)["rows"]], [True, True, False])
                self.assertEqual(v["calls"][0]["sent"]["note"], v["calls"][1]["sent"]["note"])
                self.assertEqual(v["calls"][2]["sent"]["items"][0]["text"], "fresh")

    def test_a_damaged_part_or_a_bad_picture_name_makes_that_call_unreadable_and_the_others_are_read(self):
        kept = history()
        kept[3]["req"]["messages"] = [m for m in kept[3]["req"]["messages"] if m.get("content") != "typed"]
        folder = self.trial(kept)
        parts = os.path.join(calls.store_folder(folder), exchanges.PARTS)
        with open(parts) as f:
            rows = f.read().splitlines()
        r2 = next(i for i, r in enumerate(rows) if '"typed"' in r)
        p3 = next(i for i, r in enumerate(rows) if sha(P3) in r)
        rows[r2] = rows[r2][:40]                                       # damaged
        rows[p3] = rows[p3].replace(sha(P3), "../../../outside")       # a picture name that is not a digest
        with open(parts, "w") as f:
            f.write("\n".join(rows) + "\n")
        v = load(folder)
        said = [(c["sent"].get("unreadable", False), c["sent"].get("note", "")) for c in v["calls"]]
        self.assertEqual([u for u, _ in said], [False, False, True, True])
        self.assertIn("is not in the store", said[2][1])
        self.assertIn("64 hexadecimal digits", said[3][1])
        self.assertEqual(v["calls"][3]["answer"]["text"], "done")     # its answer still reads
        self.assertTrue(calls.page(v)["rows"][2]["unreadable"])


class Pictures(Base):
    def test_inline_encodings_are_picture_metadata_in_every_display(self):
        encoded = base64.b64encode(P1).decode()
        variants = [encoded + "\n", encoded[:40] + " \n" + encoded[40:], encoded.rstrip("="),
                    base64.urlsafe_b64encode(P1).decode()]
        for i, value in enumerate(variants):
            for source in (False, True):
                with self.subTest(variant=i, source=source):
                    image = ({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": value}}
                             if source else {"type": "image_url", "image_url": {"url": "data:image/png;base64," + value}})
                    req = {"messages": [{"role": "user", "content": [image]}],
                           "tools": [{"name": "look", "input_schema": {"default": image}}]}
                    folder = self.trial([{"req": req, "body": chat_reply(json.dumps(image))}], name=f"inline-{i}-{source}__u0001")
                    v = load(folder)
                    for item in v["calls"][0]["sent"]["items"]:
                        self.assertEqual(item["pictures"], [{"sha256": sha(P1), "type": "image/png",
                                                            "bytes": len(P1), "stored": False}])
                    for shown in (v, calls.document(v), calls.page(v, section="sent-0"),
                                  calls.page(v, section="sent-1"), calls.page(v, section="text")):
                        self.assertNotIn(value[:40], json.dumps(shown))
                    self.assertTrue(exchanges.check(calls.store_folder(folder), "u0001", 1)["same"])

    def test_a_listed_picture_is_given_with_its_type_and_any_other_is_refused(self):
        v = load(self.trial(history()))
        self.assertEqual(calls.picture(v, sha(P2)), (P2, "image/png"))
        for bad in (sha(b"nothing"), "A" * 64, "../" + sha(P2)[3:], sha(P2)[:63]):
            with self.subTest(bad=bad), self.assertRaises(LookupError):
                calls.picture(v, bad)
        folder = self.trial(history()[:1], name="other__u0001")
        with self.assertRaises(LookupError):
            calls.picture(load(folder), sha(P2))                       # another trial's
        # a blob whose type is not an image: listed in a part, its bytes there, never given
        store = calls.store_folder(folder)
        page = b"<html><script>alert(1)</script></html>"
        h = sha(page)
        with open(os.path.join(store, exchanges.BLOBS, h + ".bin"), "wb") as f:
            f.write(page)
        with open(os.path.join(store, exchanges.PARTS), "a") as f:
            f.write(json.dumps({"sha256": "f" * 64, "part": {"x": {"blob": h, "type": "text/html", "bytes": len(page), "as": "base64"}}}) + "\n")
        again = load(folder)
        self.assertEqual(again["pictures"][h], "text/html")
        with self.assertRaises(LookupError):
            calls.picture(again, h)
        self.assertEqual(calls.picture(again, sha(P1)), (P1, "image/png"))
        with open(os.path.join(store, exchanges.BLOBS, sha(P1) + ".png"), "r+b") as f:
            f.write(b"damaged")
        with self.assertRaises(LookupError):
            calls.picture(again, sha(P1))                              # bytes that are not their hash


class Weight(Base):
    def test_sixty_calls_with_sixty_pictures_page_twelve_calls_and_no_bytes(self):
        msgs, kept = [SYSTEM, {"role": "user", "content": "start"}], []
        for i in range(60):
            kept.append({"req": {"model": "m", "messages": list(msgs), "tools": TOOLS}, "body": chat_reply(tool=("click", "{}"))})
            msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "click", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": [{"type": "image_url", "image_url": {"url": data_url(picture(100 + i, 200_000))}}]}]
        folder = self.trial(kept, steps=[("agent", T0 + 10 * n + 5, f"step {n}") for n in range(1, 61)])
        started = time.perf_counter()
        v = load(folder)
        took = time.perf_counter() - started
        print(f"\nthe reader on 60 calls, 60 pictures of 200 kB: {took:.3f} s", file=sys.stderr)
        self.assertEqual(len(v["calls"]), 60)
        self.assertEqual(sum(len(i["pictures"]) for c in v["calls"] for i in c["sent"]["items"]), 59)
        data = json.dumps(calls.page(v, position=59))
        self.assertEqual(len(calls.page(v)["rows"]), 12)
        self.assertLess(len(data), 20_000)
        self.assertNotIn(base64.b64encode(picture(158, 200_000)).decode()[:40], data)


class NothingSecret(Base):
    def test_texts_pass_through_the_pages_redaction(self):
        kept = [{"req": {"model": "m", "messages": [{"role": "user", "content": "use sk-live-01234567 please"}]},
                 "body": chat_reply("key is sk-live-01234567")}]
        v = load(self.trial(kept))
        text = json.dumps(calls.document(v)) + json.dumps(calls.page(v, section="sent-0"))
        self.assertNotIn("sk-live-01234567", text)
        self.assertIn("[REDACTED]", text)


class Tries(Base):
    def test_each_try_reads_its_own_calls(self):
        job = os.path.join(self.tmp, "job")
        self.trial(history()[:1], name=os.path.join("retried", "task__u0001.try-1"))
        self.trial(history(), name="task__u0001")
        first = load(os.path.join(job, "retried", "task__u0001.try-1"))
        last = load(os.path.join(job, "task__u0001"))
        self.assertEqual((len(first["calls"]), len(last["calls"])), (1, 4))


class TheCommand(Base):
    def test_request_reads_the_route_of_each_try(self):
        root = os.path.join(self.tmp, "jobs")
        job = os.path.join(root, "job-a")
        kept = history()
        names = ("task__u0001.try-1", "task__u0001")
        for i, name in enumerate(names):
            folder = os.path.join(job, "retried", name) if i == 0 else os.path.join(job, name)
            write_trial(folder, [kept[i]])
            with open(os.path.join(folder, "result.json"), "w") as f:
                json.dump({"task_name": "d/task", "trial_name": "task__u0001", "source": "d",
                           "standard": {"retries": i}}, f)
            other = {"messages": ["another route"]}
            exchanges.Store(os.path.join(folder, calls.RELAY)).write(
                "other", 1, T0, "/messages", other, json.dumps(other).encode(), {}, 200, False, b"{}")
        with open(os.path.join(job, "config.json"), "w") as f:
            json.dump({"agents": [{"name": "a", "model_name": "m"}], "datasets": [{"name": "d", "n_tasks": 1}]}, f)
        with open(os.path.join(job, "result.json"), "w") as f:
            json.dump({"stats": {"n_trials": 1}}, f)
        (j,) = parse.load_all([root])
        tries = parse.tries(j.trials[0])
        self.assertEqual([t.trial_name for _, t in tries], list(names))
        for i, (_, trial) in enumerate(tries):
            for entry in ("request", "command"):
                with self.subTest(trial=i, entry=entry):
                    out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.StringIO()
                    self.addCleanup(out.close)
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = (runs_cli._request(trial, 1) if entry == "request" else
                                runs_cli.main(["show", root, j.job_id, trial.trial_name, "--call", "1", "--request"]))
                    out.flush()
                    self.assertEqual((code, out.buffer.getvalue()), (0, json.dumps(kept[i]["req"]).encode() + b"\n"))
                    self.assertIn("the recorded sent_sha256", err.getvalue())
                    self.assertEqual(load(trial.dir)["calls"][0]["answer"]["tool_calls"][0]["name"],
                                     "click" if i == 0 else "type_text")

    def test_runs_show_calls_prints_the_view_as_json(self):
        root = os.path.join(self.tmp, "jobs")
        job = os.path.join(root, "job-a")
        os.makedirs(job)
        with open(os.path.join(job, "config.json"), "w") as f:
            json.dump({"agents": [{"name": "a", "model_name": "m"}], "datasets": [{"name": "d", "n_tasks": 1}]}, f)
        with open(os.path.join(job, "result.json"), "w") as f:
            json.dump({"stats": {"n_trials": 1}}, f)
        kept = history()
        kept[0]["req"]["messages"][1]["content"][1]["image_url"]["url"] += "\n"
        trial = write_trial(os.path.join(job, "task__u0001"), kept, steps=[("agent", T0 + 11, "looking")])
        with open(os.path.join(trial, "result.json"), "w") as f:
            json.dump({"task_name": "d/task", "trial_name": "task__u0001", "source": "d"}, f)
        (j,) = parse.load_all([root])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runs_cli.main(["show", root, j.job_id, "task__u0001", "--calls"])
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads(out.getvalue())
        self.assertNotIn(base64.b64encode(P1).decode()[:40], out.getvalue())
        self.assertEqual((doc["record"], [c["number"] for c in doc["calls"]], doc["calls"][0]["steps"]), ("exchanges", [1, 2, 3, 4], [1]))
        self.assertEqual(doc["calls"][1]["sent"]["items"][0]["pictures"][0]["sha256"], sha(P2))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as usage:
            runs_cli.main(["show", root, j.job_id, "--calls"])
        self.assertEqual(usage.exception.code, 2)


class PicturesKept(Base):
    """The pictures a trial's store keeps, for the row the Artifacts tab gives them: each counted once, with its bytes."""

    def test_each_picture_sent_is_counted_once_with_its_bytes_and_nothing_else(self):
        folder = self.trial(history())
        self.assertEqual(calls.pictures_kept(folder), {"count": 3, "bytes": len(P1) + len(P2) + len(P3)})
        with open(os.path.join(calls.store_folder(folder), exchanges.BLOBS, "a" * 64 + ".bin"), "wb") as f:
            f.write(b"not a picture")
        self.assertEqual(calls.pictures_kept(folder)["count"], 3)
        for name, mode in (("text__u0001", "text"), ("none__u0001", None)):      # the text alone, or no store: none
            with self.subTest(mode=mode):
                self.assertIsNone(calls.pictures_kept(self.trial(history(), name=name, mode=mode)))
        linked = self.trial([], name="linked__u0001", mode=None)                 # a store reached through a link is not read
        os.makedirs(os.path.join(linked, ".work", "relay"), exist_ok=True)
        os.symlink(calls.store_folder(folder), calls.store_folder(linked))
        self.assertIsNone(calls.pictures_kept(linked))


class Unmarked:
    """The tests again on stores written without the mark of their format, as before it: they read the same."""

    def setUp(self):
        patch = mock.patch.object(exchanges, "HEADER", "")
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()


class UnmarkedSentIsWhatIsNew(Unmarked, SentIsWhatIsNew):
    pass


class UnmarkedAnswerIsWhole(Unmarked, AnswerIsWhole):
    pass


class UnmarkedNumbering(Unmarked, Numbering):
    pass


class UnmarkedStepsUnderCalls(Unmarked, StepsUnderCalls):
    pass


class UnmarkedThreeKinds(Unmarked, ThreeKinds):
    pass


class UnmarkedUnreadable(Unmarked, Unreadable):
    pass


class UnmarkedPictures(Unmarked, Pictures):
    pass


class UnmarkedWeight(Unmarked, Weight):
    pass


class UnmarkedNothingSecret(Unmarked, NothingSecret):
    pass


class UnmarkedTries(Unmarked, Tries):
    pass


class UnmarkedTheCommand(Unmarked, TheCommand):
    pass


if __name__ == "__main__":
    unittest.main()
