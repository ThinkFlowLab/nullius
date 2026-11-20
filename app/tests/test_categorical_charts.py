"""The categorical charts (nullius/charts.py): scales, gaps, checks and the style registry."""

import unittest

from nullius import charts as c


def metric(*values, label="Metric", fmt=str):
    return c.Metric(label, tuple(c.Value(label=f"run {i}", value=v, text="—" if v is None else str(v))
                                      for i, v in enumerate(values)), fmt)


def shapes(panel, cls):
    return [m for m in panel.marks if cls in m.cls.split()]


class Ticks(unittest.TestCase):
    def test_round_steps_enclose_the_range(self):
        self.assertEqual(c.nice_ticks(0, 1), (0, 0.25, 0.5, 0.75, 1))
        self.assertEqual(c.nice_ticks(-3, 5), (-4, -2, 0, 2, 4, 6))
        self.assertEqual(c.nice_ticks(0.0012, 0.0031)[0], 0.001)

    def test_whole_counts_never_step_below_one(self):
        self.assertEqual(c.nice_ticks(0, 3, step_min=1), (0, 1, 2, 3))

    def test_long_tick_labels_that_would_touch_leave_one_out_never_the_last(self):
        p = c.bars(metric(0, 32, 85, 4, fmt=lambda v: f"${v:.2f}"))
        labels = [t for t in p.texts if t.cls == "tick"]
        self.assertEqual([t.lines[0] for t in labels], ["0", "$25.00", "$50.00", "$100.00"])
        self.assertEqual(len([m for m in p.marks if m.cls == "grid"]) // 4, 5)     # every tick keeps its gridline

    def test_one_value_still_has_a_range(self):
        ticks = c.nice_ticks(5, 5)
        self.assertLess(ticks[0], 5); self.assertGreater(ticks[-1], 5)


class Bars(unittest.TestCase):
    def test_a_bar_per_present_value_from_zero_on_the_metric_s_own_axis(self):
        p = c.bars(metric(2, 4))
        bars = shapes(p, "bar")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].x, bars[1].x)                       # both grow from zero
        self.assertAlmostEqual(bars[1].w, 2 * bars[0].w, delta=0.1)
        ticks = [t.lines[0] for t in p.texts if t.cls == "tick"]
        self.assertEqual(ticks[0], "0")

    def test_a_missing_value_is_a_gap_with_no_mark_never_zero(self):
        p = c.bars(metric(3, None))
        self.assertEqual(len(shapes(p, "bar")), 1)
        self.assertIn(("run 1", "—"), p.table[1])
        self.assertIn(("—",), [t.lines for t in p.texts if "none" in t.cls.split()])

    def test_zero_and_negative_values_show_a_mark_on_each_side_of_zero(self):
        p = c.bars(metric(-2, 0, 3))
        neg, zero, pos = shapes(p, "bar")
        self.assertGreater(zero.w, 0)                                # a real zero is still drawn
        self.assertAlmostEqual(neg.x + neg.w, zero.x, delta=0.01)    # the negative bar ends at zero
        self.assertEqual(pos.x, zero.x)
        self.assertIn("0", [t.lines[0] for t in p.texts if t.cls == "tick"])

    def test_a_check_is_drawn_hollow_and_named(self):
        m = c.Metric("M", (c.Value("agent", 1, "1"), c.Value("oracle", 2, "2", check=True)))
        p = c.bars(m)
        self.assertEqual([b.cls for b in shapes(p, "bar")], ["bar", "bar check"])
        self.assertEqual(p.table[1][1][0], "oracle (check)")

    def test_the_baseline_is_a_reference_mark_at_its_value_in_each_row(self):
        m = c.Metric("M", (c.Value("a", 2, "2", baseline=True), c.Value("b", 4, "4", "+2")))
        p = c.bars(m)
        refs = shapes(p, "ref")
        a, b = shapes(p, "bar")
        self.assertEqual(len(refs), 2)                               # across each row's band, never its name
        self.assertEqual({r.x for r in refs}, {round(a.x + a.w, 2)})
        self.assertEqual(p.table[1], (("a (baseline)", "2"), ("b", "4 +2")))

    def test_a_single_run_has_no_reference_line(self):
        p = c.bars(c.Metric("M", (c.Value("a", 2, "2", baseline=True),)))
        self.assertEqual((len(shapes(p, "bar")), shapes(p, "ref")), (1, []))

    def test_a_long_name_wraps_never_clips(self):
        name = "agent " * 30
        p = c.bars(c.Metric("M", (c.Value(name, 1, "1"),)))
        label = next(t for t in p.texts if t.cls == "label")
        self.assertGreater(len(label.lines), 1)
        self.assertEqual(" ".join(label.lines), name.strip())

    def test_very_different_magnitudes_get_their_own_panels_and_axes(self):
        panels = [c.bars(metric(0.002, 0.003)), c.bars(metric(4000, 9000))]
        small, large = ([t.lines[0] for t in p.texts if t.cls == "tick"] for p in panels)
        self.assertNotEqual(small[-1], large[-1])
        self.assertEqual(large[-1], "10000")


class Dots(unittest.TestCase):
    def test_the_axis_is_around_the_values_so_close_values_stay_apart(self):
        p = c.dots(metric(0.81, 0.83))
        a, b = shapes(p, "dot")
        self.assertGreater(b.x - a.x, 50)
        self.assertNotEqual([t.lines[0] for t in p.texts if t.cls == "tick"][0], "0")

    def test_a_gap_has_no_dot_and_no_track(self):
        p = c.dots(metric(1, None))
        self.assertEqual((len(shapes(p, "dot")), len(shapes(p, "track"))), (1, 1))


class Scatter(unittest.TestCase):
    def test_every_run_keeps_its_own_dot_and_tag_and_ties_sit_around_one_place(self):
        x, y = metric(40, 40, 40, 40, label="Trials"), metric(0, 0, 0, 3, label="Broke")
        p = c.scatter(x, y)
        dots = shapes(p, "dot")
        self.assertEqual(len(dots), 4)
        self.assertEqual(len({(d.x, d.y) for d in dots}), 4)                    # three at one place, set apart
        self.assertEqual(sorted(t.lines[0] for t in p.texts if t.cls == "tag"), ["1", "2", "3", "4"])
        self.assertEqual([t.lines[0] for t in p.texts if t.cls.startswith("label")], ["1. run 0", "2. run 1", "3. run 2", "4. run 3"])
        self.assertEqual(p.table[0], ("Name", "Trials", "Broke"))
        self.assertTrue(dots[0].title.startswith("1 run 0: Trials 40, Broke 0"))

    def test_tags_stay_inside_the_plot_and_clear_of_each_other(self):
        values = [(0.40 + k * 0.002, 1 + k * 0.01) for k in range(7)]           # a tight cluster
        p = c.scatter(metric(*[a for a, _ in values]), metric(*[b for _, b in values]))
        boxes = []
        for t in (t for t in p.texts if t.cls == "tag"):
            w = c._TAG_W * len(t.lines[0])
            x0 = {"start": t.x, "end": t.x - w, "middle": t.x - w / 2}[t.anchor]
            boxes.append((x0, t.y - c._TAG_H + 3, x0 + w, t.y + 3))
        self.assertEqual(len(boxes), 7)
        for box in boxes:
            self.assertGreaterEqual(box[0], c._S_LEFT); self.assertLessEqual(box[2], c.WIDTH - 8)
        clear = sum(not any(c._overlaps(a, b) for b in boxes if b is not a) for a in boxes)
        self.assertGreaterEqual(clear, 5)

    def test_a_tag_is_the_caller_s_and_a_run_missing_either_value_has_no_dot(self):
        x = c.Metric("X", (c.Value("a", 1, "1", tag="3"), c.Value("b", None, "—", tag="5"), c.Value("d", 2, "2", tag="6")))
        p = c.scatter(x, metric(1, 2, 3))
        self.assertEqual(len(shapes(p, "dot")), 2)
        self.assertEqual(sorted(t.lines[0] for t in p.texts if t.cls == "tag"), ["3", "6"])
        self.assertIn("label none", [t.cls for t in p.texts if t.lines[0].startswith("5")])
        numbered = c.Metric("X", (c.Value("3. a", 1, "1", tag="3"), c.Value("b", 2, "2", tag="4")))
        self.assertEqual([t.lines[0] for t in c.scatter(numbered, numbered).texts if t.cls == "label"], ["3. a", "4. b"])


class Series(unittest.TestCase):
    def test_marks_carry_their_series_cycling_after_eight_and_a_check_stays_hollow(self):
        values = tuple(c.Value(f"run {n}", n, str(n), series=n, check=n == 3) for n in range(1, 11))
        bars = shapes(c.bars(c.Metric("M", values)), "bar")
        self.assertEqual([b.cls for b in bars][:3], ["bar s1", "bar s2", "bar s3 check"])
        self.assertEqual([b.cls.split()[1] for b in bars][7:], ["s8", "s1", "s2"])
        dots = shapes(c.dots(c.Metric("M", values)), "dot")
        self.assertEqual([d.cls.split()[1] for d in dots], [b.cls.split()[1] for b in bars])
        self.assertEqual(c.bars(metric(1, 2)).marks[-1].cls, "bar")                # no series: no colour class

    def test_a_scatter_s_dots_and_key_share_each_run_s_colour(self):
        x = c.Metric("X", tuple(c.Value(f"run {n}", n, str(n), series=n, tag=str(n)) for n in (1, 2, 9)))
        p = c.scatter(x, x)
        self.assertEqual([m.cls for m in shapes(p, "dot")], ["dot s1", "dot s2", "dot s1"])
        self.assertEqual([m.cls for m in shapes(p, "swatch")], ["swatch s1", "swatch s2", "swatch s1"])
        self.assertEqual([t.lines[0] for t in p.texts if t.cls == "label"], ["1. run 1", "2. run 2", "9. run 9"])   # the name too


class Registry(unittest.TestCase):
    def test_each_style_names_the_metrics_it_draws_one_panel_from(self):
        self.assertEqual({k: s.axes for k, s in c.STYLES.items()}, {"bars": ("Metric",), "dots": ("Metric",), "scatter": ("X", "Y")})
        for key, s in c.STYLES.items():
            p = s.draw(*(metric(1, 2, label=a) for a in s.axes))
            self.assertIsInstance(p, c.Panel, key)
            self.assertTrue(s.label)
        self.assertEqual(c.STYLES["scatter"].draw(metric(1, label="A"), metric(2, label="B")).title, "B by A")


if __name__ == "__main__":
    unittest.main()
