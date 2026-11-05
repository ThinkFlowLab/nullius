"""Filtering a trace, and the one breakdown this tree is allowed to recompute.

Two failures are worth more than the rest here.

**A filter that changes the timings it was only asked to leave out.** Hiding a
span must not move the axis, must not touch the producer's `totals`, and must not
mutate the document on disk. Each has its own test, because each would produce a
picture that still looks plausible.

**A recomputed breakdown that double-counts.** A tool call runs inside the LLM
call that asked for it and a parent tool span contains its own execution span, so
summing durations attributes more than the wall clock. `breakdown` partitions
instead, and the numbers pinned below come from the real capture in
`fixtures/trace_rich.json` — including the 8.3ms where it disagrees with Harbor's
own arithmetic on purpose.

The wrapper has a distinct label: `claude_code.tool`
brackets the call it is named after, so the reader re-kinds it `group` and it reads
"Write [group]"; hiding *that* is what takes the whole call out of the picture,
and hiding "Write" leaves the bracket behind.
"""

import json
import unittest

from nullius.traces import trace

from .test_trace import RICH, doc, raw, span

# From the real capture. See test_trace for the rest of the tree.
INTERACTION = "7b77d45d7608c5ee"
LLM_1 = "aa6520d8a3413bac"
TOOL_PARENT = "fbe9aa3b5d05bfec"
TOOL_BLOCKED = "80ab6e382cf0adc7"
TOOL_WRITE = "b8485ddca72125ef"

# The producer's numbers, and the two the partition reaches instead.
REPORTED_LLM_NS = 3172317850
REPORTED_TOOL_NS = 23281491
PARTITIONED_LLM_NS = 3164064758     # less the tool call nested inside it
PARTITIONED_TOOL_NS = 18131419      # the execution; the bracket around it is a group
BLOCKED_NS = 4733777

# The wrapper's label, and what a filter has to name to take the call away whole.
GROUP = "Write [group]"


def ids(tr):
    return [r.span.span_id for r in tr.rows]


def raw_ids(spans):
    return [str(s.get("span_id")) for s in trace.raw_spans_of(spans)]


class Filters(unittest.TestCase):
    def test_values_are_deduplicated_and_sorted(self):
        f = trace.Filter.of(["Bash", "Write", "Bash"], ["b", "a"])
        self.assertEqual(f.names, ("Bash", "Write"))
        self.assertEqual(f.span_ids, ("a", "b"))

    def test_blanks_are_dropped_rather_than_hiding_unnamed_spans(self):
        # An empty `hide=` in the query string must not match every span whose
        # label happens to be missing.
        self.assertFalse(trace.Filter.of(["", "  "], [""]).active)

    def test_toggling_the_same_name_twice_returns_to_the_start(self):
        f = trace.Filter.of(["Read"])
        self.assertEqual(f.toggled_name("Write").toggled_name("Write"), f)

    def test_toggling_removes_what_is_already_there(self):
        f = trace.Filter.of(["Read", "Write"])
        self.assertEqual(f.toggled_name("Read").names, ("Write",))

    def test_the_two_axes_are_independent(self):
        f = trace.Filter.of(["Read"], ["x"])
        self.assertEqual(f.toggled_name("Write").span_ids, ("x",))
        self.assertEqual(f.toggled_span("y").names, ("Read",))

    def test_clearing_drops_both_axes(self):
        self.assertFalse(trace.Filter.of(["Read"], ["x"]).cleared().active)

    def test_query_carries_both_axes_for_url_for(self):
        self.assertEqual(
            trace.Filter.of(["Read"], ["x"]).query,
            {"hide": ["Read"], "hide_span": ["x"]},
        )

    def test_an_inactive_filter_adds_no_query_parameters(self):
        # url_for drops empty lists, so an unfiltered view keeps a clean URL.
        self.assertEqual(trace.Filter().query, {"hide": [], "hide_span": []})


class Pruning(unittest.TestCase):
    def test_hiding_a_label_takes_the_subtree_with_it(self):
        kept, removed = trace.prune(raw()["spans"], trace.Filter.of(["Write"]))
        # The parent tool span is labelled from its `tool_name` attribute, so it
        # reads "Write" too — and its blocked_on_user child goes with it.
        self.assertEqual(removed, 3)
        self.assertNotIn(TOOL_PARENT, raw_ids(kept))
        self.assertNotIn(TOOL_BLOCKED, raw_ids(kept))

    def test_hiding_one_span_leaves_its_siblings(self):
        kept, removed = trace.prune(raw()["spans"], trace.Filter.of(span_ids=[TOOL_WRITE]))
        self.assertEqual(removed, 1)
        self.assertIn(TOOL_PARENT, raw_ids(kept))
        self.assertIn(TOOL_BLOCKED, raw_ids(kept))

    def test_hiding_a_root_empties_the_document(self):
        kept, removed = trace.prune(raw()["spans"], trace.Filter.of(span_ids=[INTERACTION]))
        self.assertEqual(kept, [])
        self.assertEqual(removed, 8)

    def test_a_filter_that_matches_nothing_passes_the_tree_through_by_reference(self):
        source = raw()["spans"]
        kept, removed = trace.prune(source, trace.Filter.of(["NoSuchTool"]))
        self.assertEqual(removed, 0)
        self.assertIs(kept[0], source[0])

    def test_the_source_document_is_never_mutated(self):
        source = raw()
        trace.prune(source["spans"], trace.Filter.of(["Write"]))
        self.assertEqual(raw_ids(source["spans"]), raw_ids(raw()["spans"]))

    def test_a_non_dict_in_the_span_list_is_skipped_rather_than_raising(self):
        kept, _ = trace.prune(["not a span", span("a")], trace.Filter())
        self.assertEqual(raw_ids(kept), ["a"])


class Inventory(unittest.TestCase):
    def test_labels_are_counted_across_the_whole_tree(self):
        facets = {f.name: f.count for f in trace.inventory(raw()["spans"])}
        # A merged parent and its execution span share the tool name.
        self.assertEqual(facets["Write"], 2)
        # And a call shares a name with the one kind of block it produced: of the
        # two calls here, one answered and one only issued a tool call.
        self.assertEqual(facets["output"], 2)
        self.assertEqual(facets["llm_request"], 1)

    def test_the_commonest_label_comes_first(self):
        counts = [f.count for f in trace.inventory(raw()["spans"])]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_ties_are_broken_alphabetically_so_the_order_is_stable(self):
        facets = trace.inventory(doc(span("a", name="zebra"), span("b", name="apple"))["spans"])
        self.assertEqual([f.name for f in facets], ["apple", "zebra"])

    def test_a_hidden_label_still_gets_a_facet(self):
        # Otherwise there would be no control left to unhide it with.
        facets = trace.inventory(raw()["spans"], trace.Filter.of(["Write"]))
        self.assertTrue(next(f for f in facets if f.name == "Write").hidden)

    def test_the_facet_carries_the_commonest_kind_for_its_swatch(self):
        facets = {f.name: f.kind for f in trace.inventory(raw()["spans"])}
        self.assertEqual(facets["Write"], "tool")
        self.assertEqual(facets["llm_request"], "llm_request")

    def test_a_span_hidden_by_id_can_still_be_named(self):
        self.assertEqual(trace.labels_by_id(raw()["spans"])[TOOL_PARENT], "Write")


class TheAxisDoesNotMove(unittest.TestCase):
    def test_hiding_a_span_leaves_the_extent_alone(self):
        full = trace.load(RICH)
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        self.assertEqual(filtered.extent, full.extent)

    def test_surviving_bars_stay_where_they_were(self):
        full = trace.load(RICH)
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        before = {r.span.span_id: (r.left_pct, r.width_pct) for r in full.rows}
        for row in filtered.rows:
            self.assertEqual((row.left_pct, row.width_pct), before[row.span.span_id])

    def test_coverage_describes_the_capture_not_the_view(self):
        self.assertEqual(
            trace.load(RICH, trace.Filter.of(["Write"])).coverage_pct,
            trace.load(RICH).coverage_pct,
        )


class TheReportedTotalsSurvive(unittest.TestCase):
    def test_the_producers_totals_are_untouched_by_a_filter(self):
        filtered = trace.load(RICH, trace.Filter.of(["Write", "claude_code.llm_request"]))
        self.assertEqual(filtered.totals.llm_ns, REPORTED_LLM_NS)
        self.assertEqual(filtered.totals.tool_ns, REPORTED_TOOL_NS)

    def test_a_filter_that_hides_everything_keeps_the_panel_alive(self):
        filtered = trace.load(RICH, trace.Filter.of(span_ids=[INTERACTION]))
        self.assertFalse(filtered.has_spans)
        self.assertTrue(filtered.visible)
        self.assertEqual(filtered.hidden_count, 8)

    def test_an_unfiltered_trace_is_not_marked_as_a_view(self):
        tr = trace.load(RICH)
        self.assertFalse(tr.filter.active)
        self.assertEqual(tr.hidden_count, 0)
        self.assertTrue(tr.visible)


class Recomputing(unittest.TestCase):
    def test_a_bracket_around_a_call_is_charged_no_time_of_its_own(self):
        # Otherwise the wrapper's own 23.3ms is added to the 18.1ms execution it
        # contains, and one Write reads as two.
        view = trace.load(RICH).view
        self.assertEqual(view.tool_ns + view.idle_ns, BLOCKED_NS + 18131419)

    def test_a_tool_that_waits_for_a_human_is_idle_not_tool_time(self):
        self.assertEqual(trace.load(RICH).view.idle_ns, BLOCKED_NS)

    def test_the_blocked_marker_is_read_from_an_attribute_too(self):
        # Harbor names the span; session_to_trace.py sets an attribute instead.
        spans = [span("t", 0, 100, kind="tool", attributes={"blocked_on_user": True})]
        view = trace.breakdown(spans, wall_ns=100)
        self.assertEqual((view.idle_ns, view.tool_ns), (100, 0))

    def test_it_disagrees_with_the_producer_by_exactly_the_nested_tool_call(self):
        # The reason the recomputed strip is shown beside the reported one and
        # never instead of it: Harbor sums its LLM spans, this charges the
        # instants the tool call covered to the tool.
        view = trace.load(RICH).view
        self.assertEqual(view.llm_ns, PARTITIONED_LLM_NS)
        self.assertEqual(REPORTED_LLM_NS - view.llm_ns, 8253092)

    def test_structural_spans_contribute_no_time_of_their_own(self):
        # A session or subagent span brackets its children; charging it would
        # claim their work twice.
        for kind in ("session", "subagent", "output", "user_message", "compaction"):
            view = trace.breakdown([span("s", 0, 100, kind=kind)], wall_ns=100)
            self.assertEqual(view.attributed_ns, 0, kind)

    def test_a_subagent_running_in_parallel_cannot_exceed_the_wall_clock(self):
        spans = [
            span("a", 0, 100, kind="llm_request"),
            span("b", 0, 100, kind="llm_request"),
            span("c", 50, 150, kind="tool"),
        ]
        view = trace.breakdown(spans, wall_ns=150)
        self.assertEqual(view.attributed_ns, 150)
        self.assertEqual((view.llm_ns, view.tool_ns), (50, 100))

    def test_a_span_with_no_end_contributes_no_time_but_is_still_counted(self):
        view = trace.breakdown([span("a", 1000, None, kind="tool")], wall_ns=5000)
        self.assertEqual(view.attributed_ns, 0)
        self.assertEqual(view.span_count, 1)

    def test_counts_and_tokens_are_recomputed_over_what_is_left(self):
        full = trace.load(RICH).view
        self.assertEqual((full.span_count, full.measured_span_count), (8, 6))
        self.assertEqual(full.tokens["output_tokens"], 104)
        # Hiding one of the two calls drops its tokens with it.
        hidden = trace.load(RICH, trace.Filter.of(span_ids=[LLM_1])).view
        self.assertEqual(hidden.tokens["output_tokens"], 23)
        self.assertEqual(hidden.span_count, 7)

    def test_hidden_time_becomes_unaccounted_rather_than_disappearing(self):
        view = trace.load(RICH, trace.Filter.of(["Write"])).view
        self.assertEqual(view.tool_ns, 0)
        self.assertEqual(view.attributed_ns + view.unaccounted_ns, view.wall_ns)

    def test_the_wall_clock_is_the_runs_not_the_views(self):
        self.assertEqual(
            trace.load(RICH, trace.Filter.of(["Write"])).view.wall_ns,
            trace.load(RICH).totals.wall_ns,
        )

    def test_with_no_reported_wall_clock_the_instrumented_window_is_used(self):
        tr = trace.parse(doc(span("a", 1_000, 3_000, kind="tool")))
        self.assertEqual(tr.view.wall_ns, 2_000)
        self.assertEqual(tr.view.tool_ns, 2_000)


class ComparedToTheUnfilteredRun(unittest.TestCase):
    """The delta a filtered strip shows, and why it is not against the producer."""

    def test_the_baseline_is_the_same_arithmetic_over_the_whole_trace(self):
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        self.assertEqual(filtered.baseline.tool_ns, PARTITIONED_TOOL_NS)
        self.assertEqual(filtered.baseline.tool_ns, trace.load(RICH).view.tool_ns)

    def test_each_delta_is_what_the_filter_took_out(self):
        # The group, so the wait for the human inside it goes too — that is what
        # "hiding a call takes its whole cost" means, and hiding the execution
        # alone would leave the wait behind.
        filtered = trace.load(RICH, trace.Filter.of([GROUP]))
        changed = {s["key"]: s["delta_ns"] for s in filtered.view.compared_to(filtered.baseline)}
        self.assertEqual(changed["tool"], -PARTITIONED_TOOL_NS)
        self.assertEqual(changed["idle"], -BLOCKED_NS)

    def test_hiding_a_tool_gives_its_overlap_back_to_the_call_that_ran_it(self):
        # Not a rounding artefact: those 3.5ms were covered by both the tool span
        # and the LLM span, and went to the tool. With the tool gone they are the
        # call's, and pretending otherwise would leave them nowhere.
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        changed = {s["key"]: s["delta_ns"] for s in filtered.view.compared_to(filtered.baseline)}
        self.assertEqual(changed["llm"], 3519315)

    def test_an_emptied_category_keeps_its_entry_so_the_change_is_visible(self):
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        changed = {s["key"]: s["ns"] for s in filtered.view.compared_to(filtered.baseline)}
        self.assertEqual(changed["tool"], 0)

    def test_the_removed_time_shows_up_as_unaccounted_not_as_a_shorter_run(self):
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        changed = {s["key"]: s["delta_ns"] for s in filtered.view.compared_to(filtered.baseline)}
        self.assertEqual(sum(changed.values()), 0)

    def test_nothing_filtered_means_no_deltas(self):
        tr = trace.load(RICH)
        self.assertEqual(
            [s["delta_ns"] for s in tr.view.compared_to(tr.baseline)], [0, 0, 0, 0]
        )

    def test_segments_stay_in_the_canonical_order(self):
        filtered = trace.load(RICH, trace.Filter.of(["Write"]))
        keys = [s["key"] for s in filtered.view.compared_to(filtered.baseline)]
        self.assertEqual(keys, ["llm", "tool", "idle", "unaccounted"])


class Percentages(unittest.TestCase):
    def test_shares_are_of_the_wall_clock_so_a_gap_reads_as_short(self):
        # Categories that do not close must not be rescaled to fill the bar.
        # Real magnitudes: a residual under a millisecond reads as float noise
        # from the producer's arithmetic and is clamped away instead.
        totals = trace.Totals(wall_ns=1_000_000_000, llm_ns=500_000_000)
        segments = {s["key"]: s["pct"] for s in totals.segments}
        self.assertEqual(segments["llm"], 50.0)
        self.assertEqual(segments["unaccounted"], 50.0)

    def test_a_recomputed_strip_is_a_share_of_the_same_wall_clock(self):
        segments = trace.load(RICH, trace.Filter.of(["Write"])).view.segments
        self.assertAlmostEqual(sum(s["pct"] for s in segments), 100.0, places=6)

    def test_with_no_wall_clock_the_segments_own_sum_is_the_denominator(self):
        totals = trace.Totals(llm_ns=300_000_000, tool_ns=100_000_000)
        segments = {s["key"]: s["pct"] for s in totals.segments}
        self.assertEqual(segments, {"llm": 75.0, "tool": 25.0})

    def test_nothing_measured_divides_by_nothing(self):
        self.assertEqual(trace.Totals().segments, [])
        self.assertEqual(trace.Breakdown().segments, [])


class Saving(unittest.TestCase):
    def out(self, *names, span_ids=()):
        return trace.filtered_document(raw(), trace.Filter.of(names, span_ids))

    def test_the_hidden_spans_are_gone(self):
        out = self.out(GROUP)
        self.assertNotIn(TOOL_PARENT, raw_ids(out["spans"]))
        self.assertIn(LLM_1, raw_ids(out["spans"]))

    def test_it_is_still_a_readable_trace_document(self):
        out = self.out("Write")
        self.assertEqual(out["schema_version"], "harbor-trace-v1")
        self.assertEqual(out["trial_name"], raw()["trial_name"])
        self.assertFalse(trace.parse(out).error)

    def test_reopening_it_shows_the_same_spans_as_the_filtered_view(self):
        filt = trace.Filter.of(["Write"])
        saved = trace.parse(trace.filtered_document(raw(), filt))
        self.assertEqual(ids(saved), ids(trace.load(RICH, filt)))

    def test_totals_are_recomputed_over_what_is_left(self):
        out = self.out(GROUP)
        self.assertEqual(out["totals"]["tool_ns"], 0)
        self.assertEqual(out["totals"]["span_count"], 5)

    def test_the_wall_clock_is_carried_over_unchanged(self):
        self.assertEqual(self.out("Write")["totals"]["wall_ns"], raw()["totals"]["wall_ns"])

    def test_the_producers_own_totals_survive_verbatim(self):
        # A derived number that has overwritten the measured one is unfalsifiable.
        self.assertEqual(self.out("Write")["filtered"]["source_totals"], raw()["totals"])

    def test_it_says_who_wrote_it_without_losing_who_made_the_spans(self):
        out = trace.filtered_document(
            dict(raw(), producer="app/session_to_trace.py"), trace.Filter.of(["Write"])
        )
        self.assertEqual(out["producer"], trace.FILTER_PRODUCER)
        self.assertEqual(out["filtered"]["source_producer"], "app/session_to_trace.py")

    def test_a_source_with_no_producer_leaves_the_field_empty_not_invented(self):
        self.assertEqual(self.out("Write")["filtered"]["source_producer"], "")

    def test_the_filter_is_recorded_so_the_view_can_be_explained(self):
        note = self.out(GROUP, span_ids=[LLM_1])["filtered"]
        self.assertEqual(note["hide"], [GROUP])
        self.assertEqual(note["hide_span"], [LLM_1])
        self.assertEqual(note["spans_removed"], 4)
        self.assertIn("app/nullius/trace.py", note["by"])

    def test_the_content_is_written_unredacted(self):
        # The file on disk is exactly as sensitive as its source; the app redacts
        # at render, and a saved view must not be a lossy copy.
        out = trace.filtered_document(raw(), trace.Filter.of(["output"]))
        write = next(s for s in trace.raw_spans_of(out["spans"]) if s["span_id"] == TOOL_WRITE)
        self.assertEqual(write["content"]["arguments"]["content"], "Hello, world!")

    def test_filtering_a_saved_view_keeps_the_first_filters_provenance(self):
        once = trace.filtered_document(raw(), trace.Filter.of(["Write"]))
        twice = trace.filtered_document(once, trace.Filter.of(["output"]))
        self.assertEqual(twice["filtered"]["hide"], ["output"])
        self.assertEqual(twice["filtered"]["previous"]["hide"], ["Write"])
        self.assertEqual(
            twice["filtered"]["previous"]["source_totals"], raw()["totals"]
        )

    def test_it_round_trips_through_json(self):
        out = self.out("Write")
        self.assertEqual(json.loads(json.dumps(out)), out)


class Zooming(unittest.TestCase):
    """`?zoom=<span id>` re-bases the waterfall on one subtree.

    A second, explicit window — not the filter and not a re-scaling of it. The
    rules the filter is held to above apply here too: the capture's extent, the
    coverage and the producer's totals do not move, and the breakdown is still
    over the whole filtered view. What changes is the axis the rows are placed
    on, and which rows there are.
    """

    def test_the_axis_is_the_subtrees_own_extent(self):
        full = trace.load(RICH)
        zoomed = trace.load(RICH, trace.Filter.of(zoom=TOOL_PARENT))
        group = trace.find_span(full.roots, TOOL_PARENT)
        self.assertEqual(zoomed.axis, trace.extent_of([group]))
        # 850µs wider than the bracket itself: the Write inside it ends after it.
        self.assertEqual(zoomed.axis.span_ns, 24131419)
        # The subtree's extent is the same walk the global axis uses, so a child
        # that outruns its parent widens it rather than being clamped.
        self.assertEqual(zoomed.axis.start_ns, group.start_ns)
        self.assertEqual(zoomed.axis.end_ns, max(c.end_ns for c in group.children))
        self.assertEqual(zoomed.zoomed.span_id, TOOL_PARENT)
        self.assertEqual(zoomed.zoom_lost, "")

    def test_spans_outside_the_subtree_are_left_out_not_rescaled(self):
        zoomed = trace.load(RICH, trace.Filter.of(zoom=TOOL_PARENT))
        self.assertEqual(ids(zoomed), [TOOL_PARENT, TOOL_BLOCKED, TOOL_WRITE])
        self.assertEqual([r.depth for r in zoomed.rows], [0, 1, 1])
        # The bracket nearly fills the window, its 18ms execution is most of it —
        # where on the whole trace it was 0.56% of the width.
        by_id = {r.span.span_id: r for r in zoomed.rows}
        self.assertEqual(by_id[TOOL_PARENT].left_pct, 0.0)
        self.assertAlmostEqual(by_id[TOOL_PARENT].width_pct, 96.48, places=2)
        self.assertGreater(by_id[TOOL_WRITE].width_pct, 70)
        self.assertLess(trace.load(RICH).rows[5].width_pct, 1)
        # Offsets count from the zoomed span's start, the origin the ticks use.
        self.assertEqual(by_id[TOOL_PARENT].offset_ns, 0)
        self.assertEqual(by_id[TOOL_WRITE].offset_ns, 6000000)
        self.assertEqual(zoomed.ticks[0].ns, 0)
        self.assertLessEqual(zoomed.ticks[-1].ns, zoomed.axis.span_ns)

    def test_the_capture_does_not_move(self):
        full = trace.load(RICH)
        zoomed = trace.load(RICH, trace.Filter.of(zoom=TOOL_PARENT))
        self.assertEqual(zoomed.extent, full.extent)
        self.assertEqual(zoomed.coverage_pct, full.coverage_pct)
        self.assertEqual(zoomed.instrumented_ns, full.instrumented_ns)
        self.assertEqual(zoomed.totals, full.totals)
        self.assertEqual(zoomed.view, full.view)
        self.assertEqual(zoomed.baseline, full.baseline)
        self.assertEqual(zoomed.roots, full.roots)
        self.assertEqual(zoomed.shown, len(full.rows))
        self.assertEqual(zoomed.zoom_offset_ns, 2146000000)
        self.assertFalse(zoomed.filter.active)
        self.assertEqual(zoomed.hidden_count, 0)

    def test_a_zoom_on_no_span_shows_the_whole_trace_with_a_note(self):
        full = trace.load(RICH)
        lost = trace.load(RICH, trace.Filter.of(zoom="nope"))
        self.assertEqual(lost.zoom_lost, "nope")
        self.assertIsNone(lost.zoomed)
        self.assertEqual(ids(lost), ids(full))
        self.assertEqual(lost.axis, full.axis)
        self.assertEqual(lost.filter.zoom, "nope")

    def test_hide_and_zoom_compose(self):
        # Hidden first, then zoomed: the filter prunes the subtree, the zoom
        # re-bases on what is left of it, and the axis is still the subtree's own.
        both = trace.load(RICH, trace.Filter.of(span_ids=[TOOL_BLOCKED], zoom=TOOL_PARENT))
        self.assertEqual(ids(both), [TOOL_PARENT, TOOL_WRITE])
        self.assertEqual(both.hidden_count, 1)
        self.assertTrue(both.filter.active)
        self.assertEqual(both.axis.span_ns, 24131419)
        self.assertEqual(both.shown, 7)
        # The breakdown is the filter's doing, not the zoom's: the same numbers
        # as the filter alone.
        alone = trace.load(RICH, trace.Filter.of(span_ids=[TOOL_BLOCKED]))
        self.assertEqual(both.view, alone.view)
        self.assertEqual(both.extent, alone.extent)
        # A zoom on a span the filter hid falls back with the note — the
        # filter wins, the zoom does not resurrect anything.
        gone = trace.load(RICH, trace.Filter.of(["Write [group]"], zoom=TOOL_PARENT))
        self.assertEqual(gone.zoom_lost, TOOL_PARENT)
        self.assertEqual(ids(gone), ids(trace.load(RICH, trace.Filter.of(["Write [group]"]))))

    def test_the_zoom_rides_on_every_filter_link_but_hides_nothing(self):
        f = trace.Filter.of(["Read"], ["x"], zoom="z")
        self.assertEqual(f.query, {"hide": ["Read"], "hide_span": ["x"], "zoom": "z"})
        self.assertEqual(f.toggled_name("Write").zoom, "z")
        self.assertEqual(f.toggled_span("y").zoom, "z")
        self.assertEqual(f.cleared(), trace.Filter(zoom="z"))
        self.assertFalse(trace.Filter(zoom="z").active)
        self.assertEqual(f.unzoomed(), trace.Filter.of(["Read"], ["x"]))
        self.assertEqual(f.zoomed_to("w").zoom, "w")
        self.assertNotIn("zoom", trace.Filter.of(["Read"]).query)
        # What is written to disk knows nothing of it.
        out = trace.filtered_document(raw(), trace.Filter.of(["output"], zoom=TOOL_PARENT))
        self.assertNotIn("zoom", out["filtered"])
        self.assertEqual(out["filtered"]["hide"], ["output"])


if __name__ == "__main__":
    unittest.main()
