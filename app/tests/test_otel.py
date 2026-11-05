"""`traces/otel.py`: OTLP/JSON spans read as a ``harbor-trace-v1`` trace.

Every case is pinned to what the trial page will show — the kind, the
duration or its absence, the orphan and error markers — because the way this
reader goes wrong is by producing a plausible waterfall: a span given the
duration it never finished, an orphan quietly dropped, a container's
aggregated token count added on top of the calls it aggregates. Each case
builds the spans it reads.
"""

import json
import os
import tempfile
import unittest

from nullius.traces import otel, trace



class LangfuseAttributes(unittest.TestCase):
    """The Langfuse observation attributes a Langfuse-instrumented
    application's spans carry, read like the OpenInference ones."""

    @staticmethod
    def span(span_id, parent, name, attributes):
        return {"span": {"traceId": "t", "spanId": span_id, "parentSpanId": parent, "name": name,
                         "startTimeUnixNano": "1", "endTimeUnixNano": "9",
                         "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attributes.items()]},
                "resource": {}, "scope": {"name": "", "version": ""}}

    def test_langfuse_types_map_like_openinference_kinds(self):
        doc = otel.build("t", [
            self.span("a", "", "agent", {"langfuse.observation.type": "agent"}),
            self.span("b", "a", "llm-call-1", {"langfuse.observation.type": "generation"}),
            self.span("c", "b", "Bash", {"langfuse.observation.type": "tool"}),
            self.span("d", "a", "verify", {"langfuse.observation.type": "evaluator"}),
            self.span("e", "a", "setup", {"langfuse.observation.type": "span"}),
        ])
        kinds = {raw["name"]: raw["kind"] for raw in trace.raw_spans_of(doc["spans"])}
        self.assertEqual(kinds, {"agent": "session", "llm-call-1": "llm_request", "Bash": "tool",
                                 "verify": "evaluator", "setup": "span"})

    def test_langfuse_usage_details_are_read_on_a_generation(self):
        doc = otel.build("t", [
            {"span": {"traceId": "t", "spanId": "a", "parentSpanId": "", "name": "llm-call-1",
                      "startTimeUnixNano": "1", "endTimeUnixNano": "9",
                      "attributes": [
                          {"key": "langfuse.observation.type", "value": {"stringValue": "generation"}},
                          {"key": "langfuse.observation.usage_details", "value": {"stringValue":
                              '{"input": 100, "output": 7, "input_cached_tokens": 60, "total": 107}'}}]},
             "resource": {}, "scope": {"name": "", "version": ""}},
        ])
        self.assertEqual(doc["spans"][0]["tokens"], {"input_tokens": 100, "output_tokens": 7,
                                                     "cache_read_tokens": 60, "total_tokens": 107})
        self.assertEqual(doc["totals"]["tokens"]["input_tokens"], 100)

    def test_usage_details_on_a_container_are_kept_and_not_read(self):
        # usage on an agent span is the lossy case the docstring names: kept
        # as an attribute, not read, so nothing is counted twice
        doc = otel.build("t", [
            self.span("a", "", "agent", {"langfuse.observation.type": "agent",
                                          "langfuse.observation.usage_details": '{"input": 100, "output": 7}'}),
        ])
        root = doc["spans"][0]
        self.assertIn("langfuse.observation.usage_details", root["attributes"])
        self.assertNotIn("tokens", root)
        self.assertEqual(doc["totals"]["tokens"], {})


class Refusals(unittest.TestCase):
    """What the reader will not read, and how it says so."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_bad_json(self):
        path = self.write("bad.json", '{"resourceSpans": [')
        with self.assertRaises(otel.ReadError) as ctx:
            otel.read_file(path)
        self.assertIn("bad.json: not JSON", str(ctx.exception))

    def test_no_resource_spans(self):
        path = self.write("other.json", json.dumps({"schema_version": "harbor-trace-v1", "spans": []}))
        with self.assertRaises(otel.ReadError) as ctx:
            otel.read_file(path)
        self.assertIn("no resourceSpans", str(ctx.exception))
        self.assertIn("schema_version", str(ctx.exception))

    def test_an_unsupported_schema_is_refused_with_a_redacted_message(self):
        path = self.write("future.json", json.dumps({"resourceSpans": [{
            "schemaUrl": "https://example.test/schemas/2.0?token=abcdefghijklmnop",
            "scopeSpans": []}]}))
        with self.assertRaises(otel.ReadError) as ctx:
            otel.read_file(path)
        message = str(ctx.exception)
        self.assertIn("unsupported schema", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("abcdefghijklmnop", message)

    def test_the_1x_schema_line_is_accepted(self):
        path = self.write("ok.json", json.dumps({"resourceSpans": [{
            "schemaUrl": "https://opentelemetry.io/schemas/1.26.0", "scopeSpans": []}]}))
        self.assertEqual(len(otel.read_file(path)), 1)

    def test_json_lines_are_one_request_per_line(self):
        path = self.write("lines.json", '{"resourceSpans": []}\n\n{"resourceSpans": []}\n')
        self.assertEqual(len(otel.read_file(path)), 2)

    def test_a_missing_file_is_a_read_error_not_a_crash(self):
        with self.assertRaises(otel.ReadError):
            otel.read_file(os.path.join(self.dir, "absent.json"))


class Values(unittest.TestCase):
    def test_every_any_value_shape(self):
        self.assertEqual(otel.any_value({"stringValue": "s"}), "s")
        self.assertEqual(otel.any_value({"intValue": "42"}), 42)
        self.assertEqual(otel.any_value({"intValue": 42}), 42)
        self.assertEqual(otel.any_value({"doubleValue": 0.5}), 0.5)
        self.assertIs(otel.any_value({"boolValue": True}), True)
        self.assertEqual(otel.any_value({"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}}),
                         ["a", 1])
        self.assertEqual(otel.any_value({"kvlistValue": {"values": [{"key": "k", "value": {"stringValue": "v"}}]}}),
                         {"k": "v"})
        self.assertEqual(otel.any_value({"bytesValue": "AAEC"}), "<3 bytes>")
        self.assertIsNone(otel.any_value({}))
        self.assertIsNone(otel.any_value("not a value"))

    def test_enums_by_number_and_by_name(self):
        self.assertEqual(otel._enum(2, otel._SPAN_KINDS), "SERVER")
        self.assertEqual(otel._enum("SPAN_KIND_CLIENT", otel._SPAN_KINDS), "CLIENT")
        self.assertEqual(otel._enum("STATUS_CODE_ERROR", otel._STATUS_CODES), "ERROR")
        self.assertEqual(otel._enum(None, otel._STATUS_CODES), "UNSET")

    def test_a_duplicate_span_id_keeps_the_first_and_says_so(self):
        rec = lambda sid, name: {"span": {"traceId": "t", "spanId": sid, "parentSpanId": "", "name": name,
                                          "startTimeUnixNano": "1", "endTimeUnixNano": "2", "attributes": []},
                                 "resource": {}, "scope": {"name": "", "version": ""}}
        doc = otel.build("t", [rec("a", "first"), rec("a", "second")])
        self.assertEqual([r["name"] for r in doc["spans"]], ["first"])
        self.assertIn("span id a appears twice; the first occurrence is kept", doc["import"]["notes"])

    def test_a_parent_cycle_is_broken_into_an_orphan_root_not_lost(self):
        rec = lambda sid, parent, name: {
            "span": {"traceId": "t", "spanId": sid, "parentSpanId": parent, "name": name,
                     "startTimeUnixNano": "1", "endTimeUnixNano": "2", "attributes": []},
            "resource": {}, "scope": {"name": "", "version": ""}}
        doc = otel.build("t", [rec("a", "b", "a"), rec("b", "a", "b"), rec("c", "", "c")])
        names = sorted(r["name"] for r in trace.raw_spans_of(doc["spans"]))
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(doc["totals"]["span_count"], 3)
        self.assertTrue(any("parent cycle" in note for note in doc["import"]["notes"]))


if __name__ == "__main__":
    unittest.main()
