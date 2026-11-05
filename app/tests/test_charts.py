"""Distribution charts over a trace's spans.

The failure this guards is a chart that reads as an accounting of the run. Every
number here is derived by summing or counting spans, and spans nest and overlap —
so a total computed this way overshoots the wall clock, and a chart presenting it
next to the breakdown strip would look authoritative while disagreeing with the
only numbers on the page that partition the timeline. `SummedTimeIsNotWallClock`
pins that apart deliberately, so nobody later "fixes" the disagreement.

The rest is the arithmetic that quietly lies when it goes wrong: a log axis whose
last bin drops the longest span in the trace, a zero-width range on a trace where
every span took the same time, a span with one known instant binned at zero, and
a chart built off the render-capped row list while claiming to describe the view.
"""

import unittest
from dataclasses import replace

import pathlib

import nullius
from nullius.traces import trace
from nullius.traces import charts

from .tracedata import DEGRADED, RICH, doc, raw, span

MS = 1_000_000


def parse(*spans, filt=None, **top):
    return trace.parse(doc(*spans, **top), filt)


def built(*spans, filt=None, **top):
    return charts.charts_for(parse(*spans, filt=filt, **top).roots)


def chart(cs, key):
    return next(c for c in cs.items if c.key == key)


def samples(*durations, kind="tool"):
    return [charts.Sample(ns=ns, kind=kind) for ns in durations]


class Bins(unittest.TestCase):
    def test_the_longest_span_lands_in_the_last_bin_rather_than_past_it(self):
        # The classic off-by-one: the value that defined the axis falls off it.
        edges = charts.log_edges(1_000, 10 * MS)
        self.assertEqual(charts.bin_index(edges[-1], edges), len(edges) - 2)
        self.assertEqual(charts.bin_index(10 * MS, edges), len(edges) - 2)

    def test_the_shortest_span_lands_in_the_first_bin(self):
        edges = charts.log_edges(1_000, 10 * MS)
        self.assertEqual(charts.bin_index(1_000, edges), 0)
        self.assertEqual(charts.bin_index(edges[0], edges), 0)

    def test_identical_durations_still_get_a_range_instead_of_a_zero_width_one(self):
        # Same stance as extent_of: a zero-width range makes every position NaN.
        edges = charts.log_edges(5 * MS, 5 * MS)
        self.assertGreaterEqual(len(edges), 2)
        self.assertGreater(edges[-1], edges[0])

    def test_the_bin_count_stays_readable_across_one_decade_and_eight(self):
        for lo, hi in ((1_000, 9_000), (1_000, 10 ** 11)):
            edges = charts.log_edges(lo, hi)
            self.assertGreaterEqual(len(edges) - 1, 4, f"{lo}..{hi} too coarse")
            self.assertLessEqual(len(edges) - 1, 24, f"{lo}..{hi} too fine")

    def test_the_edges_increase(self):
        edges = charts.log_edges(1_200, 3 * MS)
        self.assertEqual(list(edges), sorted(set(edges)))

    def test_a_bin_is_a_round_range(self):
        edges = charts.log_edges(MS, 10_000 * MS)
        self.assertEqual([e / MS for e in edges[:4]], [1, 2, 5, 10])
        self.assertEqual(edges[-1], 10_000 * MS)

    def test_the_smallest_durations_do_not_repeat_an_edge(self):
        edges = charts.log_edges(1, 9)
        self.assertEqual(list(edges), sorted(set(edges)))
        self.assertEqual((edges[0], edges[-1]), (1, 10))


class Exclusions(unittest.TestCase):
    def test_a_span_with_no_end_is_counted_out_rather_than_binned_at_zero(self):
        cs = built(span("a", 0, MS), span("b", 0, None))
        self.assertEqual((cs.total, cs.plotted, cs.excluded), (2, 1, 1))
        for key in ("time-hist", "kind-hist"):
            self.assertEqual(sum(b.count for b in chart(cs, key).bars), 1)
        for key in ("time-cdf", "kind-cdf"):
            self.assertTrue(all(s.n == 1 for s in chart(cs, key).series))

    def test_the_note_says_how_many_were_left_out(self):
        cs = built(span("a", 0, MS), span("b", 0, None))
        self.assertIn("1 of 2 spans", cs.note)
        self.assertNotIn("0 of", built(span("a", 0, MS)).note)

    def test_an_unknown_clock_is_excluded_even_with_a_duration(self):
        # is_instant treats `unknown` as no measurement at all; a duration
        # alongside it is a number nobody stands behind.
        cs = built(span("a", 0, MS, timing_quality="unknown"))
        self.assertEqual((cs.plotted, cs.excluded), (0, 1))
        self.assertFalse(cs.has_spans)

    def test_inferred_timings_still_plot(self):
        # `inferred` is approximate, not absent — the page warns about it
        # elsewhere, and dropping it here would empty every transcript-derived
        # trace.
        cs = charts.charts_for(trace.load(DEGRADED).roots)
        self.assertTrue(cs.has_spans)
        self.assertEqual(cs.excluded, 0)


class Histograms(unittest.TestCase):
    def setUp(self):
        self.cs = charts.charts_for(trace.load(RICH).roots)

    def test_every_plotted_span_is_in_a_bar(self):
        self.assertEqual(sum(b.count for b in chart(self.cs, "time-hist").bars),
                         self.cs.plotted)
        self.assertEqual(sum(b.count for b in chart(self.cs, "kind-hist").bars),
                         self.cs.plotted)

    def test_the_bars_hold_all_of_the_summed_time(self):
        self.assertEqual(sum(b.ns for b in chart(self.cs, "time-hist").bars),
                         self.cs.summed_ns)

    def test_the_stacked_bars_agree_with_the_pooled_ones_bin_by_bin(self):
        pooled = {}
        for b in chart(self.cs, "time-hist").bars:
            pooled[b.lo_ns] = pooled.get(b.lo_ns, 0) + b.count
        stacked = {}
        for b in chart(self.cs, "kind-hist").bars:
            stacked[b.lo_ns] = stacked.get(b.lo_ns, 0) + b.count
        self.assertEqual(stacked, pooled)

    def test_an_empty_bin_draws_nothing_rather_than_a_minimum_height_bar(self):
        # Two spans three decades apart leave bins between them with no bar at
        # all; a floor height there would read as "a little happened here".
        cs = built(span("a", 0, 1_000), span("b", 0, 10 ** 7))
        bars = chart(cs, "time-hist").bars
        self.assertTrue(all(b.count for b in bars))
        self.assertLess(len(bars), len(cs.edges) - 1)

    def test_the_bars_stay_inside_the_plot_box(self):
        box = self.cs.box
        for key in ("time-hist", "kind-hist"):
            for b in chart(self.cs, key).bars:
                self.assertGreaterEqual(round(b.x, 2), box.x0)
                self.assertLessEqual(round(b.x + b.w, 2), box.x1 + 0.01)
                self.assertGreaterEqual(round(b.y, 2), box.y0 - 0.01)
                self.assertLessEqual(round(b.y + b.h, 2), box.y1 + 0.01)

    def test_the_stack_segments_sit_on_each_other_without_a_gap(self):
        cs = built(
            span("a", 0, MS, kind="tool"),
            span("b", 0, MS, kind="llm_request"),
            span("c", 0, 50 * MS, kind="tool"),
        )
        by_bin = {}
        for b in chart(cs, "kind-hist").bars:
            by_bin.setdefault(b.lo_ns, []).append(b)
        for stack in by_bin.values():
            stack.sort(key=lambda b: -b.y)
            for lower, upper in zip(stack, stack[1:]):
                self.assertAlmostEqual(lower.y, upper.y + upper.h, places=1)


class Cdfs(unittest.TestCase):
    def setUp(self):
        self.cs = charts.charts_for(trace.load(RICH).roots)

    @staticmethod
    def ys(series):
        return [float(point.split(",")[1]) for point in series.points.split()]

    def test_a_cdf_never_goes_backwards(self):
        # y grows downward in SVG, so a rising curve is a falling y.
        for key in ("time-cdf", "kind-cdf"):
            for series in chart(self.cs, key).series:
                ys = self.ys(series)
                self.assertEqual(ys, sorted(ys, reverse=True), f"{key} {series.label}")

    def test_every_cdf_starts_at_zero_and_reaches_one_hundred_percent(self):
        box = self.cs.box
        for key in ("time-cdf", "kind-cdf"):
            for series in chart(self.cs, key).series:
                ys = self.ys(series)
                self.assertAlmostEqual(ys[0], box.y1, places=1)
                self.assertAlmostEqual(ys[-1], box.y0, places=1)

    def test_each_kind_is_scaled_to_its_own_total_so_the_lines_are_comparable(self):
        # Otherwise a kind with two spans is a flat line at the bottom and the
        # chart says nothing about how long its calls ran.
        cs = built(
            span("a", 0, MS, kind="tool"),
            span("b", 0, 2 * MS, kind="tool"),
            span("c", 0, 900 * MS, kind="llm_request"),
        )
        series = chart(cs, "kind-cdf").series
        self.assertEqual({s.kind for s in series}, {"tool", "llm_request"})
        for s in series:
            self.assertAlmostEqual(self.ys(s)[-1], cs.box.y0, places=1)

    def test_the_series_share_the_histograms_x_axis(self):
        for key in ("time-cdf", "kind-cdf"):
            for s in chart(self.cs, key).series:
                xs = [float(p.split(",")[0]) for p in s.points.split()]
                self.assertEqual(xs, sorted(xs))
                self.assertGreaterEqual(xs[0], self.cs.box.x0)
                self.assertLessEqual(xs[-1], self.cs.box.x1)

    def test_a_line_only_turns_at_a_duration_a_span_really_took(self):
        # No slope between two durations: every move is along or straight up.
        cs = built(span("a", 0, MS, kind="tool"), span("b", 0, 3 * MS, kind="tool"), span("c", 0, 40 * MS, kind="tool"))
        s = chart(cs, "kind-cdf").series[0]
        points = [tuple(map(float, p.split(","))) for p in s.points.split()]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            self.assertTrue(x0 == x1 or y0 == y1, (x0, y0, x1, y1))
        self.assertEqual(len({x for x, _ in points}), 3)

    def test_a_line_runs_from_its_shortest_span_to_its_longest_and_stops(self):
        cs = built(span("a", 0, 2 * MS, kind="tool"), span("b", 0, 5 * MS, kind="tool"), span("c", 0, 8000 * MS, kind="llm_request"))
        tool = next(s for s in chart(cs, "kind-cdf").series if s.kind == "tool")
        xs = [float(p.split(",")[0]) for p in tool.points.split()]
        self.assertEqual((tool.lo_ns, tool.hi_ns), (2 * MS, 5 * MS))
        self.assertGreater(xs[0], cs.box.x0)
        self.assertLess(xs[-1], cs.box.x1)

    def test_a_readout_has_no_dot_where_a_line_is_not_drawn(self):
        cs = built(span("a", 0, 2 * MS, kind="tool"), span("b", 0, 5 * MS, kind="tool"), span("c", 0, 8000 * MS, kind="llm_request"))
        c = chart(cs, "kind-cdf")
        i = [s.kind for s in c.series].index("tool")
        last = c.readouts[-1].rows[i]
        self.assertEqual((last.share, last.on_line), ("100%", False))       # it still says 100%, off the line's end
        self.assertTrue(any(r.rows[i].on_line for r in c.readouts))

    def test_thousands_of_spans_stay_a_small_page(self):
        cs = built(*[span(f"s{i}", 0, MS + i * 1000, kind="tool") for i in range(3000)])
        s = chart(cs, "kind-cdf").series[0]
        self.assertLessEqual(len(s.points.split()), 2 * charts._MAX_STEPS + 3)
        self.assertEqual(float(s.points.split()[-1].split(",")[1]), cs.box.y0)     # and still reaches the top

    def test_the_series_count_every_plotted_span(self):
        self.assertEqual(chart(self.cs, "time-cdf").series[0].n, self.cs.plotted)
        self.assertEqual(sum(s.n for s in chart(self.cs, "kind-cdf").series),
                         self.cs.plotted)


class SummedTimeIsNotWallClock(unittest.TestCase):
    """These charts describe the calls; the breakdown strip accounts for the run.

    Calls overlap and leave gaps, so their durations add up past the run or short
    of it. The two numbers are supposed to disagree, and a later change that made
    them match would mean one of them had stopped measuring what it claims to.
    """

    def test_the_summed_span_time_is_not_the_wall_clock_on_the_real_capture(self):
        tr = trace.load(RICH)
        cs = charts.charts_for(tr.roots)
        self.assertNotEqual(cs.summed_ns, tr.totals.wall_ns)

    def test_calls_that_overlap_add_up_past_the_run(self):
        cs = built(span("a", 0, 10 * MS, kind="tool"), span("b", 0, 10 * MS, kind="tool"))
        self.assertEqual(cs.summed_ns, 20 * MS)

    def test_nothing_here_reports_a_wall_clock(self):
        cs = charts.charts_for(trace.load(RICH).roots)
        self.assertFalse(hasattr(cs, "wall_ns"))
        self.assertNotIn("wall", " ".join(c.subtitle for c in cs.items))


class TheFilterReachesTheCharts(unittest.TestCase):
    def test_hiding_a_label_shrinks_the_charts(self):
        data = raw()
        whole = charts.charts_for(trace.parse(data).roots)
        cut = charts.charts_for(trace.parse(data, trace.Filter.of(names=("Write",))).roots)
        self.assertLess(cut.total, whole.total)
        self.assertLess(cut.plotted, whole.plotted)

    def test_hiding_a_parent_takes_its_subtree_out_of_the_charts(self):
        parent = span("p", 0, 10 * MS, children=[span("c", 0, MS)])
        whole = built(parent)
        cut = built(parent, filt=trace.Filter.of(span_ids=("p",)))
        self.assertEqual(whole.total, 2)
        self.assertEqual(cut.total, 0)
        self.assertFalse(cut.has_spans)

    def test_a_view_with_every_span_hidden_plots_nothing_and_does_not_raise(self):
        cs = built(span("a", 0, MS), filt=trace.Filter.of(span_ids=("a",)))
        self.assertFalse(cs.has_spans)
        self.assertEqual(len(cs.items), len(charts._SPECS))
        self.assertTrue(all(not (c.bars or c.series) for c in cs.items))


class TheRenderCapDoesNotLeak(unittest.TestCase):
    def test_the_charts_see_spans_the_waterfall_stops_drawing(self):
        # `rows` is capped at MAX_ROWS; `roots` is not. A chart built off the
        # rows would describe a subset while the panel above says otherwise.
        many = [span(f"s{i}", 0, (i % 50 + 1) * MS) for i in range(trace.MAX_ROWS + 25)]
        tr = trace.parse(doc(*many))
        cs = charts.charts_for(tr.roots)
        self.assertEqual(len(tr.rows), trace.MAX_ROWS)
        self.assertEqual(cs.total, trace.MAX_ROWS + 25)
        self.assertEqual(sum(b.count for b in chart(cs, "kind-hist").bars), cs.total)


class Shape(unittest.TestCase):
    def test_the_four_charts_come_back_in_a_fixed_order(self):
        keys = [c.key for c in charts.charts_for(trace.load(RICH).roots).items]
        self.assertEqual(keys, ["time-hist", "time-cdf", "kind-hist", "kind-cdf"])

    def test_an_empty_trace_still_returns_the_four_frames(self):
        cs = charts.charts_for([])
        self.assertEqual(len(cs.items), 4)
        self.assertFalse(cs.has_spans)
        self.assertEqual(cs.note, "")

    def test_the_legend_lists_the_kinds_in_the_waterfalls_order(self):
        cs = built(
            span("a", 0, MS, kind="tool"),
            span("b", 0, MS, kind="llm_request"),
        )
        self.assertEqual([f.kind for f in chart(cs, "kind-hist").kinds],
                         ["llm_request", "tool"])

    def test_the_axis_labels_at_the_ends_are_anchored_inward(self):
        # Centred, they hang outside the viewBox and clip into a wrong number.
        ticks = chart(charts.charts_for(trace.load(RICH).roots), "time-hist").xticks
        self.assertEqual(ticks[0].anchor, "start")
        self.assertEqual(ticks[-1].anchor, "end")
        self.assertTrue(all(t.anchor == "middle" for t in ticks[1:-1]))

    def test_the_x_ticks_are_round_decades_inside_the_plot(self):
        cs = charts.charts_for(trace.load(RICH).roots)
        box = cs.box
        for t in chart(cs, "time-hist").xticks:
            self.assertRegex(str(t.ns), r"^10*$", "a tick that is not a round decade")
            self.assertGreaterEqual(t.at, box.x0 - 0.01)
            self.assertLessEqual(t.at, box.x1 + 0.01)


class TheDurationAxisSaysItsStep(unittest.TestCase):
    def test_each_mark_is_how_many_times_the_last(self):
        cs = built(span("a", 0, MS, kind="tool"), span("b", 0, 9000 * MS, kind="llm_request"))
        self.assertEqual(chart(cs, "time-hist").x_step, 10)

    def test_a_thinned_axis_says_its_own_step(self):
        cs = built(span("a", 0, 1, kind="tool"), span("b", 0, 5000 * 1000 * MS, kind="llm_request"))
        c = chart(cs, "time-hist")
        self.assertEqual(c.x_step, round(c.xticks[1].ns / c.xticks[0].ns))
        self.assertGreater(c.x_step, 10)


class AxisLabelsSayWhatTheyMean(unittest.TestCase):
    """A tick label has to mean the same thing as the bar it sits beside.

    The time histogram stands its bars at their real share on a round axis tall
    enough to read the shape, so its top gridline is a value on that scale and
    never "the tallest bar", and a "100%" there would mean what it says.
    """

    def setUp(self):
        self.cs = charts.charts_for(trace.load(RICH).roots)

    def test_a_time_bar_stands_at_its_real_share_of_the_axis(self):
        c = chart(self.cs, "time-hist")
        axis = float(c.yticks[-1].text.rstrip("%"))
        for column in c.columns:
            drawn = sum(b.h for b in column.segs) / self.cs.box.h * axis
            self.assertAlmostEqual(drawn, column.ns / self.cs.summed_ns * 100, places=0)
        self.assertIn(axis, charts._SHARE_TOPS)

    def test_the_axis_rounds_up_to_the_fullest_bin(self):
        self.assertEqual(charts._share_top(84, 100), 100)
        self.assertEqual(charts._share_top(14, 100), 20)
        self.assertEqual(charts._share_top(3, 100), 5)
        self.assertEqual([t.text for t in charts._scaled_yticks(20)], ["0%", "10%", "20%"])

    def test_the_count_histogram_labels_a_count_of_spans(self):
        ticks = chart(self.cs, "kind-hist").yticks
        pooled = {}
        for b in chart(self.cs, "kind-hist").bars:
            pooled[b.lo_ns] = pooled.get(b.lo_ns, 0) + b.count
        self.assertEqual(ticks[0].text, "0")
        self.assertEqual(int(ticks[-1].text), max(pooled.values()))

    def test_only_the_cumulative_charts_claim_a_hundred_percent(self):
        for key in ("time-cdf", "kind-cdf"):
            self.assertEqual([t.text for t in chart(self.cs, key).yticks],
                             ["0%", "50%", "100%"])

    def test_a_share_too_small_to_round_is_not_labelled_zero(self):
        # Same stance as the `share` filter: time that was really spent must not
        # render as "0%".
        self.assertEqual(charts._pct(0.0), "0%")
        self.assertEqual(charts._pct(0.04), "<0.1%")


class TheLegendIsAControl(unittest.TestCase):
    """Switching a kind off redraws one chart and nothing else.

    Two ways this goes wrong, and both look reasonable from inside the code. One
    is reaching for `trace.Filter` — the spans would leave the waterfall, the
    breakdown and the saved file too, when all the reader asked was that a line
    stop being drawn. The other is leaving the axis where it was, which is the
    right rule for the waterfall (a shared timeline must not move under a
    filter) and the wrong one here: an axis kept at a range nothing is drawn in
    squeezes what is left into a corner.
    """

    def cf(self, *values):
        return charts.ChartFilter.of(values)

    def test_a_kind_switched_off_leaves_the_chart_it_was_switched_off_on(self):
        cs = charts.charts_for(trace.load(RICH).roots, self.cf("kind-cdf:tool"))
        self.assertNotIn("tool", [s.kind for s in chart(cs, "kind-cdf").series])
        self.assertIn("tool", [s.kind for s in
                               charts.charts_for(trace.load(RICH).roots).items[3].series])

    def test_switching_a_kind_off_in_one_chart_leaves_the_other_alone(self):
        cs = charts.charts_for(trace.load(RICH).roots, self.cf("kind-cdf:tool"))
        self.assertIn("tool", [b.kind for b in chart(cs, "kind-hist").bars])
        self.assertEqual(chart(cs, "kind-hist").hidden_kinds, ())

    def test_the_stacked_histogram_drops_the_segments_of_a_kind_switched_off(self):
        cs = charts.charts_for(trace.load(RICH).roots, self.cf("kind-hist:tool"))
        self.assertNotIn("tool", [b.kind for b in chart(cs, "kind-hist").bars])
        self.assertEqual(chart(cs, "kind-hist").hidden_kinds, ("tool",))

    def test_the_axis_rescales_to_what_is_still_drawn(self):
        # The hidden kind holds the only long span, so the axis must come in.
        spans = [span("a", 0, 1 * MS, kind="tool"),
                 span("b", 0, 2 * MS, kind="tool"),
                 span("c", 0, 5_000 * MS, kind="llm_request")]
        before = built(*spans)
        after = charts.charts_for(parse(*spans).roots,
                                  charts.ChartFilter.of(["kind-hist:llm_request"]))
        self.assertGreater(chart(before, "kind-hist").xticks[-1].ns,
                           chart(after, "kind-hist").xticks[-1].ns)
        # ... and the untouched charts keep the axis they always had.
        self.assertEqual([t.ns for t in chart(after, "time-hist").xticks],
                         [t.ns for t in chart(before, "time-hist").xticks])

    def test_the_time_weighted_charts_are_not_redrawn_by_a_legend_they_do_not_have(self):
        roots = trace.load(RICH).roots
        plain, toggled = charts.charts_for(roots), charts.charts_for(
            roots, self.cf("kind-hist:tool", "kind-cdf:tool"))
        for key in ("time-hist", "time-cdf"):
            self.assertEqual(chart(plain, key), chart(toggled, key))
            self.assertEqual(chart(toggled, key).kinds, ())

    def test_the_chip_of_a_kind_switched_off_still_counts_its_spans(self):
        # Otherwise the control hides the reason to click it a second time.
        cs = charts.charts_for(trace.load(RICH).roots, self.cf("kind-hist:tool"))
        plain = charts.charts_for(trace.load(RICH).roots)
        off = {f.kind: f for f in chart(cs, "kind-hist").kinds}
        on = {f.kind: f for f in chart(plain, "kind-hist").kinds}
        self.assertTrue(off["tool"].off)
        self.assertEqual(off["tool"].n, on["tool"].n)
        self.assertEqual(sorted(off), sorted(on))

    def test_switching_every_kind_off_leaves_an_empty_plot_and_a_way_back(self):
        cs = charts.charts_for(trace.load(RICH).roots, charts.ChartFilter.of(
            f"kind-hist:{f.kind}" for f in
            charts.charts_for(trace.load(RICH).roots).items[2].kinds))
        c = chart(cs, "kind-hist")
        self.assertEqual(c.bars, ())
        self.assertEqual(c.yticks, ())      # no count axis over a plot with no counts
        self.assertTrue(c.kinds)            # every chip still there to click back on
        self.assertIn("switched off", c.note)

    def test_the_note_says_the_axis_moved(self):
        cs = charts.charts_for(trace.load(RICH).roots, self.cf("kind-hist:tool"))
        self.assertIn("rescaled", chart(cs, "kind-hist").note)

    def test_the_trace_underneath_is_untouched(self):
        # The count of spans in the view is a property of `hide`, not of a legend.
        roots = trace.load(RICH).roots
        plain = charts.charts_for(roots)
        toggled = charts.charts_for(roots, self.cf("kind-hist:tool", "kind-cdf:tool"))
        self.assertEqual((toggled.total, toggled.plotted, toggled.summed_ns),
                         (plain.total, plain.plotted, plain.summed_ns))
        self.assertEqual(toggled.edges, plain.edges)


class TheChartLegendHasOneUrl(unittest.TestCase):
    """Same contract as `trace.Filter`: one view, one query string."""

    def test_toggling_twice_returns_to_where_it_started(self):
        cf = charts.ChartFilter()
        self.assertFalse(cf.active)
        once = cf.toggled("kind-cdf", "tool")
        self.assertEqual(once.off, ("kind-cdf:tool",))
        self.assertEqual(once.toggled("kind-cdf", "tool"), cf)

    def test_the_order_they_were_clicked_in_does_not_make_a_second_url(self):
        a = charts.ChartFilter().toggled("kind-cdf", "tool").toggled("kind-hist", "llm_request")
        b = charts.ChartFilter().toggled("kind-hist", "llm_request").toggled("kind-cdf", "tool")
        self.assertEqual(a, b)
        self.assertEqual(charts.ChartFilter.of(["a:b", "a:b"]).off, ("a:b",))

    def test_it_carries_its_own_query_key_and_not_the_trace_filters(self):
        cf = charts.ChartFilter.of(["kind-cdf:tool"])
        self.assertEqual(cf.query, {"chart_off": ["kind-cdf:tool"]})
        self.assertNotIn("hide", cf.query)

    def test_a_hand_edited_url_is_ignored_rather_than_fatal(self):
        cf = charts.ChartFilter.of(["nonsense", "", "kind-cdf:tool", "  "])
        self.assertEqual(cf.off, ("kind-cdf:tool",))
        self.assertEqual(cf.hidden("no-such-chart"), frozenset())
        roots = trace.load(RICH).roots
        plain = charts.charts_for(roots)
        unknown = charts.charts_for(roots, charts.ChartFilter.of(["kind-hist:ghost"]))
        # The URL filter can leave a note, but must not change the plotted data or axes.
        for actual, expected in zip(unknown.items, plain.items, strict=True):
            self.assertEqual(replace(actual, note=expected.note), expected)

    def test_clearing_one_chart_leaves_the_others_switched_off(self):
        cf = charts.ChartFilter.of(["kind-hist:tool", "kind-cdf:tool"])
        self.assertEqual(cf.cleared("kind-hist").off, ("kind-cdf:tool",))
        self.assertEqual(cf.cleared().off, ())


class NoFlask(unittest.TestCase):
    def test_no_backend_module_imports_the_web_stack(self):
        # Check the entire backend stays Flask-free so it works without the web stack.
        backend = pathlib.Path(nullius.__file__).parent
        offenders = {}
        modules = [m for m in sorted(backend.rglob("*.py")) if "__pycache__" not in m.parts]
        self.assertGreater(len(modules), 40)
        for module in modules:
            bad = [ln for ln in module.read_text(encoding="utf-8").splitlines()
                   if ln.startswith(("import ", "from ")) and "flask" in ln.lower()]
            if bad:
                offenders[str(module.relative_to(backend))] = bad
        self.assertEqual(offenders, {})


class Wrappers(unittest.TestCase):
    """A span that only brackets others is not a call: left out, and counted."""

    def setUp(self):
        self.cs = built(span("run", 0, 900 * MS, kind="session"), span("turn 1", 0, 800 * MS, kind="user_message"),
                        span("a", 0, 10 * MS, kind="tool"), span("b", 0, 20 * MS, kind="llm_request"), span("mark", 5 * MS, None, kind="output"))

    def test_they_are_not_drawn_and_no_headline_names_them(self):
        self.assertEqual(self.cs.plotted, 2)
        self.assertEqual(self.cs.summed_ns, 30 * MS)
        self.assertEqual({k.kind for k in chart(self.cs, "kind-hist").kinds}, {"tool", "llm_request"})
        self.assertIn("llm_request", chart(self.cs, "kind-cdf").head.unit)

    def test_the_note_counts_them_apart_from_the_spans_with_no_duration(self):
        self.assertEqual((self.cs.total, self.cs.excluded, self.cs.wrapped), (5, 1, 2))
        self.assertIn("1 of 5 spans has no known duration", self.cs.note)
        self.assertIn("2 of 5 only wrap others", self.cs.note)

    def test_a_subagent_stays_a_call(self):
        self.assertEqual(built(span("s", 0, 10 * MS, kind="subagent")).plotted, 1)


class Anatomy(unittest.TestCase):
    """What the page draws beside the bars and lines: each number from the samples drawn."""

    def setUp(self):
        self.cs = built(span("a", 0, 10 * MS, kind="tool"), span("b", 0, 20 * MS, kind="tool"), span("c", 0, 30 * MS, kind="tool"),
                        span("d", 0, 4000 * MS, kind="llm_request"), span("e", 0, 8000 * MS, kind="llm_request"))

    def test_the_time_histograms_columns_share_the_summed_time_coloured_by_kind(self):
        c = chart(self.cs, "time-hist")
        self.assertEqual(sum(col.count for col in c.columns), 5)
        self.assertTrue(all(col.share.endswith("%") for col in c.columns))
        self.assertEqual({b.kind for b in c.bars}, {"tool", "llm_request"})
        self.assertEqual({k.kind for k in c.key_kinds}, {"tool", "llm_request"})
        self.assertEqual(c.head.text, max(c.columns, key=lambda col: col.ns).share)

    def test_a_stacked_column_holds_its_segments_from_the_baseline_up(self):
        for column in chart(self.cs, "kind-hist").columns:
            self.assertEqual(column.count, sum(b.count for b in column.segs))
            self.assertEqual(column.share, "")                      # it counts spans: no share of the time
            tops = [b.y for b in column.segs]
            self.assertEqual(tops, sorted(tops, reverse=True))

    def test_how_many_calls_leads_with_how_many_and_draws_no_median_across_kinds(self):
        c = chart(self.cs, "kind-hist")
        self.assertEqual((c.head.text, c.head.unit), ("5", "calls"))
        self.assertIsNone(c.marker)                                             # tools in ms, calls in s: their middle is neither

    def test_the_headlines_are_a_real_spans_duration(self):
        self.assertEqual(chart(self.cs, "time-cdf").head.ns, 8000 * MS)         # 8s is past half of the 8.06s summed
        self.assertEqual(chart(self.cs, "kind-cdf").head.ns, 4000 * MS)         # the llm calls' lower middle
        self.assertIn("llm_request", chart(self.cs, "kind-cdf").head.unit)
        fullest = chart(self.cs, "time-hist").head
        self.assertTrue(fullest.lo_ns <= 8000 * MS <= fullest.hi_ns)
        self.assertTrue(fullest.text.endswith("%"))

    def test_a_readout_gives_every_series_at_each_edge_and_the_plot_is_split_between_them(self):
        c = chart(self.cs, "kind-cdf")
        self.assertEqual(len(c.readouts), len(c.series[0].values))
        self.assertTrue(all(len(r.rows) == len(c.series) for r in c.readouts))
        self.assertEqual(c.readouts[0].left, self.cs.box.x0)
        self.assertEqual(c.readouts[-1].right, self.cs.box.x1)
        for a, b in zip(c.readouts, c.readouts[1:]):
            self.assertEqual(a.right, b.left)
            self.assertTrue(a.x <= a.right and b.left <= b.x)
        self.assertEqual([row.share for row in c.readouts[-1].rows], ["100%"] * len(c.series))

    def test_a_kind_switched_off_leaves_the_chart_its_headline_from_what_is_left(self):
        cs = built(span("a", 0, 10 * MS, kind="tool"), span("d", 0, 4000 * MS, kind="llm_request"), filt=None)
        off = charts.charts_for(parse(span("a", 0, 10 * MS, kind="tool"), span("d", 0, 4000 * MS, kind="llm_request")).roots,
                                charts.ChartFilter.of(["kind-hist:llm_request", "kind-cdf:llm_request"]))
        self.assertEqual(chart(off, "kind-hist").head.text, "1")
        self.assertEqual(chart(off, "kind-cdf").head.unit, "median tool")
        self.assertEqual(chart(cs, "kind-cdf").head.ns, 4000 * MS)
        everything = charts.ChartFilter.of(["kind-hist:llm_request", "kind-hist:tool"])
        none = chart(charts.charts_for(parse(span("a", 0, 10 * MS, kind="tool"), span("d", 0, 4000 * MS, kind="llm_request")).roots, everything), "kind-hist")
        self.assertIsNone(none.head)
        self.assertIsNone(none.marker)
        self.assertEqual(none.columns, ())

    def test_the_box_places_a_coordinate_by_percentage(self):
        box = self.cs.box
        self.assertEqual((box.left(box.x0), box.left(box.x1), box.top(box.y0), box.top(box.y1)), (0, 100, 0, 100))
        self.assertEqual((box.width(box.w), box.height(box.h)), (100, 100))


if __name__ == "__main__":
    unittest.main()
