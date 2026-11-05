"""Parsing a trace into waterfall rows.

The failure this guards is a chart that lies. A trace exists to expose timing
that other formats hide, so the ways this module can go wrong are all ways of
quietly producing a plausible picture: clamping a child that starts before its
parent, recomputing totals by summing leaves that nest, treating a span with one
known instant as a zero-length measurement, or dividing by a zero extent and
rendering NaN as 0%.

Several cases are pinned against the real capture in fixtures/trace_rich.json
rather than a hand-built tree, because the awkward shapes here — a child
starting before its parent, a parent and child sharing a label, span ids that
are not OTel ids — were all found in real output and none of them would have
been invented.
"""

import json
import unittest

from nullius.traces import trace

from . import FIXTURES

RICH = str(FIXTURES / "trace_rich.json")
DEGRADED = str(FIXTURES / "trace_degraded.json")
BAD = str(FIXTURES / "trace_bad.json")

# Ids from the real capture. `block:...:0` is a synthetic log-derived id and is
# deliberately kept in the assertions: span ids are opaque strings, not hex.
ROOT = "7b77d45d7608c5ee"
LOG_SESSION = "session:06f69643-92ef-4b70-9b7e-8895ec3e4095"
LLM_1 = "aa6520d8a3413bac"
TOOL_PARENT = "fbe9aa3b5d05bfec"
TOOL_BLOCKED = "80ab6e382cf0adc7"
TOOL_WRITE = "b8485ddca72125ef"
LLM_2 = "bc000785dbfa7ad7"
OUTPUT = "block:28bda920-a4fa-4918-9e96-4036d44013fe:0"


def raw(path=RICH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def span(sid, start=1_000, end=2_000, **fields):
    """A minimal span. Only what the format requires, so tests add what they mean."""
    out = {
        "span_id": sid,
        "trace_id": "t",
        "name": sid,
        "kind": "tool",
        "start_ns": start,
        "timing_quality": "measured",
        "source": "otel",
        "attributes": {},
        "children": [],
    }
    if end is not None:
        out["end_ns"] = end
        out["duration_ns"] = end - start
    out.update(fields)
    return out


def doc(*spans, **top):
    out = {"schema_version": "harbor-trace-v1", "spans": list(spans)}
    out.update(top)
    return out


def by_id(tr, span_id):
    return next(r for r in tr.rows if r.span.span_id == span_id)


class Loading(unittest.TestCase):
    def test_missing_file_reports_an_error_instead_of_raising(self):
        tr = trace.load(str(FIXTURES / "does_not_exist.json"))
        self.assertTrue(tr.error)
        self.assertFalse(tr.has_spans)

    def test_malformed_json_reports_an_error_instead_of_raising(self):
        tr = trace.load(BAD)
        self.assertTrue(tr.error)
        self.assertFalse(tr.has_spans)

    def test_a_valid_trace_reports_no_error(self):
        self.assertEqual(trace.load(RICH).error, "")

    def test_trial_name_is_taken_as_the_run_id(self):
        # Real files emit `trial_name`; the format doc calls the field `run_id`.
        self.assertEqual(trace.load(RICH).run_id, "hello-world__k6TfusL")

    def test_run_id_is_accepted_when_trial_name_is_absent(self):
        self.assertEqual(trace.parse(doc(run_id="r-1")).run_id, "r-1")

    def test_trial_name_wins_over_run_id(self):
        self.assertEqual(trace.parse(doc(trial_name="t", run_id="r")).run_id, "t")

    def test_absent_optional_fields_parse(self):
        # The producer writes with exclude_none, so optionals are absent keys.
        tr = trace.parse(doc(span("a")))
        self.assertEqual(tr.rows[0].span.parent_span_id, "")
        self.assertEqual(tr.rows[0].span.tokens, {})
        self.assertEqual(tr.rows[0].span.content, [])

    def test_explicit_nulls_parse_exactly_like_absent_fields(self):
        # The trap is `.get(k, {})`, which returns None when the key exists as
        # null and hands a None straight to the template.
        absent = trace.parse(doc(span("a")))
        nulls = trace.parse(
            doc(span("a", parent_span_id=None, tokens=None, content=None, attributes=None)),
            )
        self.assertEqual(nulls.rows[0].span.parent_span_id, absent.rows[0].span.parent_span_id)
        self.assertEqual(nulls.rows[0].span.tokens, {})
        self.assertEqual(nulls.rows[0].span.content, [])
        self.assertEqual(nulls.rows[0].span.attributes, [])

    def test_a_null_totals_block_yields_no_segments(self):
        self.assertEqual(trace.parse(doc(span("a"), totals=None)).totals.segments, [])

    def test_a_non_object_document_reports_an_error(self):
        self.assertTrue(trace.parse([]).error)


class LargeIntegers(unittest.TestCase):
    """Epoch nanoseconds exceed 2**53. Python ints are exact; keep them ints."""

    def test_start_ns_survives_the_round_trip_exactly(self):
        source = raw()["spans"][0]["start_ns"]
        self.assertGreater(source, 2 ** 53)
        parsed = by_id(trace.load(RICH), ROOT).span.start_ns
        self.assertEqual(parsed, source)
        self.assertIsInstance(parsed, int)

    def test_offsets_stay_integers(self):
        for row in trace.load(RICH).rows:
            self.assertIsInstance(row.offset_ns, int)

    def test_durations_stay_integers(self):
        for row in trace.load(RICH).rows:
            self.assertIsInstance(row.span.duration_ns, int)


class TotalsAreVerbatim(unittest.TestCase):
    """The producer resolved the double-counting; the consumer must not redo it."""

    def test_every_total_matches_the_file(self):
        source = raw()["totals"]
        totals = trace.load(RICH).totals
        for key in ("wall_ns", "llm_ns", "tool_ns", "reasoning_ns", "idle_ns",
                    "span_count", "measured_span_count"):
            self.assertEqual(getattr(totals, key), source[key], key)

    def test_reported_tool_time_is_not_the_sum_of_tool_spans(self):
        # This is the whole point. Everything the instrumentation called a tool
        # here sums to 46.1ms against a true 23.3ms, because the `claude_code.tool`
        # wrapper contains both of the spans it is being added to.
        tr = trace.load(RICH)
        toolish = [r.span for r in tr.rows if r.span.kind in ("tool", trace.GROUP_KIND)]
        self.assertEqual(sum(s.duration_ns for s in toolish), 46_146_687)
        # Re-kinding the wrapper `group` takes the duplicate out of that sum, and
        # what is left still is not the reported figure — a consumer that adds up
        # spans gets a different number whichever spans it picks.
        leaf_sum = sum(s.duration_ns for s in toolish if s.kind == "tool")
        self.assertEqual(leaf_sum, 22_865_196)
        self.assertEqual(tr.totals.tool_ns, 23_281_491)
        self.assertNotEqual(leaf_sum, tr.totals.tool_ns)


class ExtentIsNeverDegenerate(unittest.TestCase):
    def test_an_empty_trace_does_not_divide_by_zero(self):
        tr = trace.parse(doc())
        self.assertEqual(tr.extent.span_ns, 1)
        self.assertFalse(tr.has_spans)
        self.assertEqual(tr.coverage_pct, 0.0)

    def test_a_single_instant_span_does_not_divide_by_zero(self):
        tr = trace.parse(doc(span("a", start=5, end=5)))
        self.assertEqual(tr.extent.span_ns, 1)
        self.assertEqual(tr.rows[0].left_pct, 0.0)
        self.assertEqual(tr.rows[0].width_pct, 0.0)

    def test_spans_all_at_one_instant_produce_finite_percentages(self):
        tr = trace.parse(doc(span("a", start=7, end=7, children=[span("b", start=7, end=7)])))
        for row in tr.rows:
            self.assertTrue(0.0 <= row.left_pct <= 100.0)
            self.assertTrue(0.0 <= row.width_pct <= 100.0)

    def test_ticks_survive_a_degenerate_extent(self):
        self.assertEqual(trace.ticks_for(trace.Extent()), [trace.Tick(pct=0.0, ns=0)])


class Layout(unittest.TestCase):
    def test_a_child_that_starts_before_its_parent_renders_early(self):
        # Real: the log-derived `output` span starts 180us before the OTel
        # llm_request it hangs under, because they came from different clocks.
        # Laying out relative to the parent would need a clamp, and the clamp
        # would hide the very discrepancy worth seeing.
        tr = trace.load(RICH)
        parent, child = by_id(tr, LLM_2), by_id(tr, OUTPUT)
        self.assertLess(child.span.start_ns, parent.span.start_ns)
        self.assertLess(child.left_pct, parent.left_pct)

    def test_percentages_stay_within_the_track(self):
        for row in trace.load(RICH).rows:
            self.assertGreaterEqual(row.left_pct, 0.0)
            self.assertLessEqual(row.left_pct + row.width_pct, 100.0 + 1e-9)

    def test_depth_first_order_matches_the_tree(self):
        self.assertEqual([r.depth for r in trace.load(RICH).rows], [0, 1, 1, 1, 2, 2, 1, 2])

    def test_multiple_roots_are_all_at_depth_zero(self):
        tr = trace.parse(doc(span("a"), span("b")))
        self.assertEqual([r.depth for r in tr.rows], [0, 0])

    def test_collapsing_a_span_drops_its_subtree(self):
        tr = trace.load(RICH)
        rows, _ = trace.flatten(tr.roots, collapsed={TOOL_PARENT})
        ids = [r.span.span_id for r in rows]
        self.assertIn(TOOL_PARENT, ids)
        self.assertNotIn(TOOL_BLOCKED, ids)
        self.assertNotIn(TOOL_WRITE, ids)
        self.assertIn(LLM_2, ids)          # a later sibling still renders

    def test_has_children_marks_only_parents(self):
        tr = trace.load(RICH)
        self.assertTrue(by_id(tr, TOOL_PARENT).has_children)
        self.assertFalse(by_id(tr, TOOL_WRITE).has_children)

    def test_a_parent_cycle_terminates(self):
        # JSON cannot express a cycle, so `load` never sees one — but `parse`
        # takes a dict, and a runaway tree must bottom out rather than exhaust
        # the stack. The row count is bounded by the nesting cap; the point of
        # the test is that it returns at all.
        a = span("a")
        a["children"] = [a]
        rows = trace.parse(doc(a)).rows
        self.assertLessEqual(len(rows), trace.MAX_DEPTH + 1)

    def test_pathologically_deep_nesting_bottoms_out(self):
        node = span("leaf")
        for i in range(trace.MAX_DEPTH + 50):
            node = span(f"n{i}", children=[node])
        tr = trace.parse(doc(node))
        self.assertEqual(max(r.depth for r in tr.rows), trace.MAX_DEPTH)


class SpansWithoutAnEnd(unittest.TestCase):
    """One known instant is not a zero-length measurement."""

    def test_a_missing_end_leaves_the_duration_unknown(self):
        row = trace.parse(doc(span("a", start=100, end=None))).rows[0]
        self.assertIsNone(row.span.duration_ns)
        self.assertTrue(row.is_instant)

    def test_an_unknown_quality_is_an_instant_even_with_a_duration(self):
        row = trace.parse(doc(span("a", timing_quality="unknown"))).rows[0]
        self.assertTrue(row.is_instant)

    def test_a_span_without_an_end_still_widens_the_extent(self):
        tr = trace.parse(doc(span("a", start=0, end=10), span("b", start=90, end=None)))
        self.assertEqual(tr.extent.end_ns, 90)

    def test_duration_is_derived_when_only_an_end_is_given(self):
        s = span("a", start=10, end=None)
        s["end_ns"] = 60
        self.assertEqual(trace.parse(doc(s)).rows[0].span.duration_ns, 50)


class Labels(unittest.TestCase):
    def test_the_tool_name_attribute_wins_over_the_instrumentation_name(self):
        # `claude_code.tool` is a namespace; the row has to read as the call.
        tr = trace.load(RICH)
        parent = by_id(tr, TOOL_PARENT).span
        self.assertEqual(parent.name, "claude_code.tool")
        self.assertTrue(parent.label.startswith("Write"))

    def test_a_parent_and_child_have_distinct_labels(self):
        # The group and execution have separate chips; the detail pane keeps
        # each span's instrumentation name.
        tr = trace.load(RICH)
        parent, child = by_id(tr, TOOL_PARENT).span, by_id(tr, TOOL_WRITE).span
        self.assertEqual((parent.label, child.label), ("Write [group]", "Write"))
        self.assertNotEqual(parent.name, child.name)

    def test_synthetic_span_ids_survive_verbatim(self):
        # Log-derived ids are not hex and not fixed-length. They are opaque.
        ids = [r.span.span_id for r in trace.load(RICH).rows]
        self.assertIn(LOG_SESSION, ids)
        self.assertIn(OUTPUT, ids)

    def test_merged_spans_are_flagged(self):
        tr = trace.load(RICH)
        self.assertTrue(by_id(tr, LLM_1).span.merged)
        self.assertFalse(by_id(tr, LOG_SESSION).span.merged)


# Milliseconds, because the slack a group is allowed is measured in them. A
# hand-built tree in bare integers would be inside it whatever it contained.
MS = trace.NS_PER_MS


def wrapper(self_ns=0, label="Bash", inner_kind="tool"):
    """A namespace span holding a wait and the call it is named after.

    The shape Harbor's Claude Code instrumentation emits, reduced to what decides
    it: the outer span carries the tool name, and the two inside it cover the
    outer one from end to end unless *self_ns* is asked for.
    """
    return span(
        "outer", 0, 100 * MS + self_ns, kind="tool",
        attributes={"tool_name": label},
        children=[
            span("wait", 0, 10 * MS, kind="tool",
                 name="claude_code.tool.blocked_on_user"),
            span("inner", 10 * MS, 100 * MS, kind=inner_kind,
                 attributes={"tool_name": label}),
        ],
        name="claude_code.tool",
    )


class Groups(unittest.TestCase):
    """A span that is a namespace for its children, not an event of its own.

    Two shapes, both of which drew one measurement as two rows. Harbor opens
    `claude_code.tool` carrying `tool_name: "Bash"` and puts the permission wait
    and the actual Bash execution inside it; and an `llm_request` can hold a
    single content block of byte-identical extent, because a one-block response
    is the request again.

    The failure to guard against is the fix being too eager. "Covered by its
    children" describes plenty of spans that are genuinely events — a call that
    wraps its own transport but leads it by seconds, a launch whose background
    subagent overruns it by minutes, an interaction whose calls tile it — and
    re-kinding those would quietly empty the categories the breakdown is built
    from.
    """

    def test_the_instrumentation_name_survives_for_the_detail_pane(self):
        # The suffix is display only. Nothing is written over the span.
        tr = trace.parse(doc(wrapper()))
        self.assertEqual(by_id(tr, "outer").span.name, "claude_code.tool")
        self.assertIn(("tool_name", "Bash"), by_id(tr, "outer").span.attributes)

    def test_a_wrapper_with_time_of_its_own_is_still_a_tool(self):
        # It is doing something, whatever it is named.
        tr = trace.parse(doc(wrapper(self_ns=trace.GROUP_SLACK_NS + 1)))
        self.assertEqual(by_id(tr, "outer").span.kind, "tool")

    def test_the_producers_own_rounding_does_not_decide_it(self):
        # Harbor stamps the wrapper to the millisecond and its children to the
        # nanosecond, so a shortfall of a millisecond or so is the clock, not work.
        tr = trace.parse(doc(wrapper(self_ns=trace.GROUP_SLACK_NS)))
        self.assertEqual(by_id(tr, "outer").span.kind, "group")

    def test_a_single_child_of_the_same_extent_is_the_same_bar_twice(self):
        # 55 llm_request spans in the reference capture hold one reasoning or
        # output block whose start and end are byte-identical to their own: the
        # producer splits a response into blocks, and a one-block response is the
        # request again. Two rows, one measurement.
        call = span("call", 0, 100 * MS, kind="llm_request", children=[
            span("think", 0, 100 * MS, kind="reasoning"),
        ])
        tr = trace.parse(doc(call))
        outer = by_id(tr, "call").span
        self.assertEqual((outer.label, outer.kind), ("call [group]", "group"))
        self.assertEqual(by_id(tr, "think").span.kind, "reasoning")

    def test_a_call_that_merely_wraps_its_own_transport_is_still_a_call(self):
        # OpenCode's `ai.streamText` brackets the stream call it is named for,
        # but leads it by seconds. Only an exact bracket is a bracket.
        call = span("call", 0, 100 * MS, kind="llm_request", children=[
            span("stream", 20 * MS, 100 * MS, kind="llm_request"),
        ])
        self.assertEqual(by_id(trace.parse(doc(call)), "call").span.kind, "llm_request")

    def test_a_child_that_overruns_its_parent_does_not_make_it_a_bracket(self):
        # A background subagent is hung under the launch that returned in 47ms
        # and runs for minutes. Clipped to the parent it covers all of it, and
        # the launch is still a real event — which is why both ends are checked.
        launch = span("launch", 0, 47, kind="tool", children=[
            span("agent", 0, 600 * MS, kind="subagent"),
        ])
        self.assertEqual(by_id(trace.parse(doc(launch)), "launch").span.kind, "tool")

    def test_a_kind_that_already_brackets_its_children_is_left_alone(self):
        # `session` and `subagent` never claimed to be work, and the breakdown
        # already charges them nothing. Renaming them adds noise, not honesty.
        for kind in ("session", "subagent"):
            outer = span("outer", 0, 100 * MS, kind=kind, children=[
                span("inner", 0, 100 * MS, kind="llm_request"),
            ])
            self.assertEqual(by_id(trace.parse(doc(outer)), "outer").span.kind, kind)

    def test_a_leaf_is_never_a_group(self):
        self.assertEqual(trace.parse(doc(span("a"))).rows[0].span.kind, "tool")

    def test_a_span_with_no_end_cannot_be_shown_to_contribute_nothing(self):
        raw_span = wrapper()
        raw_span.pop("end_ns"), raw_span.pop("duration_ns")
        self.assertEqual(trace.parse(doc(raw_span)).rows[0].span.kind, "tool")

    def test_children_that_overlap_are_not_counted_twice(self):
        # Two nested children covering half of it each would otherwise add up to
        # the whole and make a span with real self-time look like a bracket.
        outer = span("outer", 0, 100 * MS, kind="tool", name="claude_code.tool",
                     attributes={"tool_name": "Bash"}, children=[
                         span("a", 0, 50 * MS, kind="tool",
                              attributes={"tool_name": "Bash"}),
                         span("b", 0, 50 * MS, kind="tool"),
                     ])
        self.assertEqual(by_id(trace.parse(doc(outer)), "outer").span.kind, "tool")

    def test_the_group_and_the_call_are_separate_chips(self):
        facets = {f.name: f for f in trace.parse(doc(wrapper())).facets}
        self.assertEqual(facets["Bash [group]"].count, 1)
        self.assertEqual(facets["Bash"].count, 1)
        self.assertEqual(facets["Bash [group]"].kind, "group")
        self.assertEqual(facets["Bash"].kind, "tool")                 # the call inside it is untouched

    def test_hiding_the_call_leaves_the_group_a_group(self):
        # Groupness is decided once, over the whole document, before anything is
        # filtered. Deciding it from the survivors would rename a row the filter
        # was never asked about — hide "Bash" and the bracket becomes "Bash".
        tr = trace.parse(doc(wrapper()), trace.Filter.of(["Bash"]))
        self.assertEqual([r.span.span_id for r in tr.rows], ["outer", "wait"])
        self.assertEqual(by_id(tr, "outer").span.label, "Bash [group]")

    def test_hiding_the_group_takes_the_call_with_it(self):
        tr = trace.parse(doc(wrapper()), trace.Filter.of(["Bash [group]"]))
        self.assertEqual(tr.rows, [])

    def test_the_callers_document_is_not_mutated(self):
        source = doc(wrapper())
        before = json.dumps(source)
        trace.parse(source)
        self.assertEqual(json.dumps(source), before)

    def test_a_bracket_around_a_bracket_is_a_group_all_the_way_down(self):
        # `mark_groups` works children first, so the inner span is already a
        # group when the outer one is asked. Matching on the displayed label
        # would miss it and leave the outer one claiming to be the tool.
        inner = wrapper()
        outer = span("outermost", 0, 100 * MS, kind="tool", name="claude_code.tool",
                     attributes={"tool_name": "Bash"}, children=[inner])
        tr = trace.parse(doc(outer))
        self.assertEqual(by_id(tr, "outermost").span.label, "Bash [group]")
        self.assertEqual(by_id(tr, "outer").span.label, "Bash [group]")
        self.assertEqual(by_id(tr, "inner").span.label, "Bash")

    def test_a_document_with_nothing_to_normalise_is_passed_through(self):
        spans = [span("a")]
        self.assertIs(trace.mark_groups(spans), spans)


class DegradedTrace(unittest.TestCase):
    """The exporter captured nothing and the producer fell back to log parsing.

    Two thirds of early real captures looked like this. Building only against a
    rich capture ships an empty-looking view for most runs.
    """

    def test_zero_measured_spans_is_surfaced(self):
        tr = trace.load(DEGRADED)
        self.assertTrue(tr.zero_measured)
        self.assertEqual(tr.totals.measured_span_count, 0)

    def test_every_row_is_inferred(self):
        self.assertEqual(
            {r.span.timing_quality for r in trace.load(DEGRADED).rows}, {"inferred"}
        )

    def test_bars_are_still_visible(self):
        # All three spans share one window, so each fills the axis rather than
        # collapsing to an invisible zero-width bar.
        for row in trace.load(DEGRADED).rows:
            self.assertFalse(row.is_instant)
            self.assertGreater(row.width_pct, 0)

    def test_no_tool_segment_when_no_tool_time(self):
        keys = [s["key"] for s in trace.load(DEGRADED).totals.segments]
        self.assertNotIn("tool", keys)

    def test_a_rich_trace_is_not_flagged_as_unmeasured(self):
        self.assertFalse(trace.load(RICH).zero_measured)

    def test_zero_measured_needs_spans_to_mean_anything(self):
        self.assertFalse(trace.parse(doc()).zero_measured)


class Breakdown(unittest.TestCase):
    def test_the_categories_do_not_close_and_the_gap_is_named(self):
        # The session span brackets more than its instrumented children, so
        # llm + tool + reasoning + idle falls short of the wall clock. Leaving
        # the gap unlabelled reads as a rendering bug.
        totals = trace.load(RICH).totals
        self.assertEqual(totals.unaccounted_ns, 49_693_493)
        self.assertIn("unaccounted", [s["key"] for s in totals.segments])

    def test_categories_that_do_close_produce_no_phantom_segment(self):
        totals = trace.load(DEGRADED).totals
        self.assertEqual(totals.unaccounted_ns, 0)
        self.assertNotIn("unaccounted", [s["key"] for s in totals.segments])

    def test_sub_millisecond_float_noise_is_clamped_away(self):
        # Producers computing in floats leave residuals like 4.5e-13, which are
        # > 0 and would render a zero-width slice legended "0ms".
        totals = trace.parse(
            doc(totals={"wall_ns": 1_000_000_000, "llm_ns": 400_000_000,
                        "tool_ns": 100_000_000, "reasoning_ns": 0,
                        "idle_ns": 499_999_999})
        ).totals
        self.assertEqual(totals.unaccounted_ns, 0)

    def test_over_closing_categories_never_go_negative(self):
        totals = trace.parse(
            doc(totals={"wall_ns": 100, "llm_ns": 200, "tool_ns": 0,
                        "reasoning_ns": 0, "idle_ns": 0})
        ).totals
        self.assertEqual(totals.unaccounted_ns, 0)

    def test_a_missing_totals_block_yields_no_segments(self):
        tr = trace.parse(doc(span("a")))
        self.assertEqual(tr.totals.segments, [])

    def test_segment_order_is_stable(self):
        keys = [s["key"] for s in trace.load(RICH).totals.segments]
        self.assertEqual(keys, ["llm", "tool", "idle", "unaccounted"])


class Coverage(unittest.TestCase):
    """How much of the run the waterfall can actually see.

    The axis comes from the spans, so without this a run where 1% was
    instrumented renders bars across the full width and reads as complete.
    """

    def test_coverage_is_the_span_extent_over_the_wall_clock(self):
        tr = trace.load(RICH)
        self.assertAlmostEqual(tr.coverage_pct, 50.1, places=1)
        self.assertEqual(tr.instrumented_ns, tr.extent.span_ns)

    def test_coverage_is_zero_when_the_wall_clock_is_unknown(self):
        self.assertEqual(trace.parse(doc(span("a"))).coverage_pct, 0.0)

    def test_coverage_never_exceeds_one_hundred(self):
        tr = trace.parse(doc(span("a", start=0, end=1000),
                             totals={"wall_ns": 10}))
        self.assertEqual(tr.coverage_pct, 100.0)


class Redaction(unittest.TestCase):
    """Nothing from a job directory reaches a template unscrubbed."""

    def test_no_credential_survives_anywhere_in_the_parsed_trace(self):
        secret = "sk-abcdefghij0123456789"
        tr = trace.parse(
            doc(span("a", name=f"tool {secret}",
                     attributes={"k": secret},
                     content={"input": secret, "arguments": {"key": secret}}))
        )
        self.assertNotIn(secret, repr(tr))

    def test_arguments_are_serialised_before_being_scrubbed(self):
        tr = trace.parse(doc(span("a", content={"arguments": {"file_path": "/app/hello.txt"}})))
        label, text = tr.rows[0].span.content[0]
        self.assertEqual(label, "Arguments")
        self.assertIn("/app/hello.txt", text)

    def test_attribute_keys_are_left_alone(self):
        # Keys are instrumentation enums, not job text.
        tr = trace.parse(doc(span("a", attributes={"gen_ai.request.model": "x"})))
        self.assertEqual(tr.rows[0].span.attributes[0][0], "gen_ai.request.model")

    def test_opaque_ids_containing_sk_survive(self):
        # An embedded `sk-` in an opaque identifier is not a credential.
        opaque = "req_011Cdxbpsk-Ab3Xz9Qw1Lm4Np7R"
        tr = trace.parse(doc(span("a", attributes={"request_id": opaque})))
        self.assertEqual(tr.rows[0].span.attributes[0][1], opaque)


class LargeTraces(unittest.TestCase):
    def test_row_count_is_bounded_and_the_remainder_is_counted(self):
        # An exporter with an unpruned `other` bucket emitted 1037 spans for a
        # trivial task. Every row carries a detail pane, so the page is capped.
        spans = [span(f"s{i}", start=i, end=i + 1) for i in range(trace.MAX_ROWS + 250)]
        tr = trace.parse(doc(*spans))
        self.assertEqual(len(tr.rows), trace.MAX_ROWS)
        self.assertEqual(tr.truncated, 250)

    def test_truncation_counts_unrendered_descendants(self):
        deep = span("parent", children=[span("kid1"), span("kid2")])
        spans = [span(f"s{i}", start=i, end=i + 1) for i in range(trace.MAX_ROWS)] + [deep]
        tr = trace.parse(doc(*spans))
        self.assertEqual(tr.truncated, 3)


class Legend(unittest.TestCase):
    def test_kinds_present_are_ordered_and_deduplicated(self):
        self.assertEqual(
            trace.load(RICH).kinds_present,
            ["llm_request", "output", "tool", "session", "group"],
        )

    def test_unknown_kinds_still_appear(self):
        tr = trace.parse(doc(span("a", kind="wat")))
        self.assertEqual(tr.kinds_present, ["wat"])

    def test_qualities_present_are_in_confidence_order(self):
        self.assertEqual(trace.load(RICH).qualities_present, ["measured", "inferred"])


if __name__ == "__main__":
    unittest.main()
