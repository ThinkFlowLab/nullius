"""The UI budget must hold for long captures without changing source semantics."""
import json
import os
import tempfile
import unittest
from collections import Counter

from nullius.traces import trace, trajectory
from nullius.traces import conversation


def capture(steps):
    return trajectory.Trajectory(len(steps), steps, Counter(), 0, "", {})


class ConversationPages(unittest.TestCase):
    def test_hundred_steps_return_one_index_page_and_one_content_window(self):
        steps = [trajectory.Step(i * 3, "agent", "", f"message-{i} " + "x" * 50000) for i in range(100)]
        data = conversation.page(capture(steps))
        self.assertEqual((data["total"], data["pages"], len(data["rows"])), (100, 9, 12))
        self.assertEqual(len(data["selected"]["content"]["text"]), conversation.CHUNK_SIZE)
        self.assertLess(len(json.dumps(data)), 11000)
        self.assertNotIn("message-99", json.dumps(data))

    def test_search_reads_beyond_index_preview_and_first_content_chunk(self):
        tr = capture([trajectory.Step(4, "agent", "", "head " + "x" * 20000 + "needle")])
        data = conversation.page(tr, query="NEEDLE")
        self.assertEqual(data["matches"], 1)
        self.assertNotIn("needle", data["selected"]["content"]["text"])
        end = conversation.page(tr, query="needle", chunk=999)["selected"]["content"]
        self.assertTrue(end["text"].endswith("needle"))
        self.assertEqual(end["chunk"], end["chunks"] - 1)

    def test_gappy_and_duplicate_step_ids_keep_source_order_and_identity(self):
        tr = capture([trajectory.Step(sid, "agent", "", str(i)) for i, sid in enumerate([1, 42, 42, 900])])
        data = conversation.page(tr, step="42")
        self.assertEqual(data["selected"]["position"], 1)
        self.assertEqual(data["selected"]["next"], 2)
        next_step = conversation.page(tr, position=2)["selected"]
        self.assertEqual(next_step["step_id"], 42)
        self.assertEqual(next_step["content"]["text"], "2")
        self.assertEqual(next_step["previous"], 1)

    def test_page_navigation_and_filter_navigation_use_matching_positions(self):
        tr = capture([trajectory.Step(i * 4, "agent", "", "match" if i % 2 else "else") for i in range(60)])
        data = conversation.page(tr, query="match", page=2)
        self.assertEqual(data["selected"]["position"], 25)
        self.assertEqual((data["selected"]["previous"], data["selected"]["next"]), (23, 27))
        self.assertEqual(data["selected"]["match_number"], 13)
        self.assertEqual(conversation.page(tr, query="match", position=59)["page"], 3)

    def test_filters_do_not_turn_an_error_looking_message_into_a_failure(self):
        tr = capture([
            trajectory.Step(1, "agent", "", "Discuss Error: text without tool failure"),
            trajectory.Step(8, "agent", "", "(tool use)", [{"name": "Bash", "arguments": "{}", "observation": "ok", "failure": ""}]),
            trajectory.Step(10, "agent", "", "A tool failed", [{"name": "Read", "arguments": "{}", "observation": "No such file or directory", "failure": "No such file or directory"}]),
        ])
        self.assertEqual(conversation.page(tr, kind="messages")["matches"], 2)
        self.assertEqual(conversation.page(tr, kind="tools")["matches"], 2)
        errors = conversation.page(tr, kind="errors")
        self.assertEqual(errors["matches"], 1)
        self.assertEqual(errors["selected"]["step_id"], 10)
        self.assertEqual(conversation.page(tr, query="missing")["matches"], 0)

    def test_parallel_tools_keep_ids_and_results_attached_by_source_call_id(self):
        raw = {"steps": [{"step_id": 81, "source": "agent", "message": "(tool use)",
            "tool_calls": [{"tool_call_id": "call:b", "function_name": "Read", "arguments": {"path": "b"}},
                           {"tool_call_id": "call:a", "function_name": "Read", "arguments": {"path": "a"}}],
            "observation": {"results": [{"source_call_id": "call:a", "content": "result-A"},
                                          {"source_call_id": "call:b", "content": "result-B"}]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(raw, handle)
            tr = trajectory.load(path)
        b = conversation.page(tr, section="call-0-observation")["selected"]["content"]
        a = conversation.page(tr, section="call-1-observation")["selected"]["content"]
        self.assertEqual((b["tool_call_id"], b["text"]), ("call:b", "result-B"))
        self.assertEqual((a["tool_call_id"], a["text"]), ("call:a", "result-A"))
        self.assertEqual(conversation.page(tr, query="call:a")["selected"]["step_id"], 81)

    def test_section_index_keeps_a_long_command_whole_and_redacted(self):
        command = "cd /testbed && python -m pytest " + "tests/queries/test_query.py " * 8 + "-x  -q sk-secret123456789"
        tr = capture([trajectory.Step(1, "agent", "", "(tool use)", [{"name": command, "arguments": "{}", "observation": "ok"}])])
        tools = {s["tool"] for s in conversation.page(tr)["selected"]["sections"]}
        self.assertEqual(len(tools), 1)
        tool = tools.pop()
        self.assertGreater(len(tool), 100)
        self.assertIn("-x  -q", tool)
        self.assertNotIn("secret123456789", tool)
        self.assertFalse(tool.endswith("…"))

    def test_redaction_happens_before_chunking_in_all_content(self):
        text = "x" * (conversation.CHUNK_SIZE - 4) + " sk-secret123456789 tail"
        tr = capture([trajectory.Step(1, "agent", "", text, [{"name": "Bash", "arguments": text, "observation": text}])])
        for section in ("message", "call-0-arguments", "call-0-observation"):
            joined = "".join(conversation.page(tr, section=section, chunk=c)["selected"]["content"]["text"] for c in (0, 1))
            self.assertNotIn("secret123456789", joined)
            self.assertIn("[REDACTED]", joined)

    def test_missing_empty_unreadable_and_invalid_boundaries(self):
        self.assertIn("No trajectory", conversation.page(None)["notice"])
        self.assertIn("no recorded steps", conversation.page(capture([]))["notice"])
        tr = capture([]); tr.error = "bad JSON"
        self.assertIn("unreadable", conversation.page(tr)["notice"])
        tr = capture([trajectory.Step(92, "user", "", "hello")])
        data = conversation.page(tr, page=-100, position="invalid", kind="made-up", chunk=-5)
        self.assertEqual(data["kind"], "all")
        self.assertEqual((data["page"], data["selected"]["content"]["chunk"]), (1, 0))
        self.assertIn("not in", conversation.page(tr, step="91")["notice"])


class SpanWindows(unittest.TestCase):
    def setUp(self):
        self.span = trace.Span(span_id="block:opaque:0", name="Bash", label="Bash", kind="tool",
            timing_quality="inferred", source="log", start_ns=20, duration_ns=30,
            content=[("Output", "x" * 40000 + " sk-secret123456789")],
            attributes=[("original", "z" * 30000)], tokens={"input": 50})
        self.trace = trace.Trace(rows=[trace.Row(self.span, offset_ns=-10)])

    def test_on_demand_selected_content_is_bounded_and_metadata_is_faithful(self):
        data = conversation.span_detail(self.trace, "block:opaque:0")
        self.assertEqual(data["timing_quality"], "inferred")
        self.assertEqual((data["offset_ns"], data["duration_ns"]), (-10, 30))
        self.assertEqual(len(data["content"]["text"]), conversation.CHUNK_SIZE)
        self.assertLess(len(json.dumps(data)), 7500)
        self.assertEqual([s["key"] for s in data["sections"]], ["content-0", "tokens", "attributes"])
        tail = conversation.span_detail(self.trace, "block:opaque:0", chunk=999)["content"]["text"]
        self.assertIn("[REDACTED]", tail)
        self.assertNotIn("secret123456789", tail)

    def test_attribute_window_and_missing_or_hidden_spans(self):
        data = conversation.span_detail(self.trace, "block:opaque:0", section="attributes", chunk=2)
        self.assertEqual(data["content"]["chunk"], 2)
        self.assertEqual(len(data["content"]["text"]), conversation.CHUNK_SIZE)
        for source in (self.trace, None, trace.Trace(rows=[])):
            with self.assertRaises(LookupError):
                conversation.span_detail(source, "not-visible")


if __name__ == "__main__":
    unittest.main()
