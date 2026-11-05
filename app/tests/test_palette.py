"""The span palette meets the contrast and separation rules in style.css.

Contrast is measured against the rendered surfaces, categories must remain
distinct under simulated colour-vision deficiencies, and the blue ramp must
have readable lightness steps. Calibration checks the measurement against
the category palette before using it to judge the span palette."""

import re
import unittest
from itertools import combinations

from . import palette

SPAN_TOKENS = ("span-llm", "span-reasoning", "span-output",
               "span-tool", "span-subagent", "span-neutral")
RAMP = ("span-llm", "span-reasoning", "span-output")
FAMILY = {"span-llm": "blue", "span-reasoning": "blue", "span-output": "blue",
          "span-tool": "tool", "span-subagent": "subagent", "span-neutral": "neutral"}
SURFACES = ("bg", "surface", "surface-2")

# WCAG 2.1 non-text contrast. The bar IS the content, not decoration.
MIN_CONTRAST = 3.0
# style.css's own threshold for separating categories.
MIN_DELTA_E76 = 25.0
# Dichromatic floor. style.css states 17 for tool categories and this script
# measures that palette at 16.6/15.6, so it reads a little stricter than
# whatever produced those numbers. 16 leaves the span palette (17.1 dark,
# 17.8 light) real headroom without blessing a regression.
MIN_CVD = 16.0
# An ordinal ramp needs a lightness step big enough to read as ordered.
MIN_RAMP_STEP = 12.0


def scopes():
    themes = palette.parse_scopes()
    css = palette.STYLESHEET.read_text()
    for name in ("charcoal", "graphite", "midnight"):
        block = re.search(r':root\[data-theme="dark"\]\[data-palette="' + name + r'"\]\s*\{([^}]+)\}', css)
        if not block:
            raise AssertionError(f"Missing dark palette: {name}")
        overrides = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]+);", block[1]))
        themes[name] = {**themes["dark"], **overrides}
    return themes


class Tokens(unittest.TestCase):
    def test_every_span_token_is_defined_in_both_themes(self):
        for scope, tokens in scopes().items():
            for token in SPAN_TOKENS:
                self.assertIn(token, tokens, f"--{token} missing from {scope}")

    def test_the_two_themes_use_different_values(self):
        dark, light = scopes()["dark"], scopes()["light"]
        for token in SPAN_TOKENS:
            self.assertNotEqual(dark[token], light[token],
                                f"--{token} is identical in both themes")


class Contrast(unittest.TestCase):
    """Every bar must be visible against every surface it can sit on.

    --surface-2 is the binding one: it is the waterfall track, so it sits
    directly behind every bar.
    """

    def test_span_colours_clear_the_non_text_contrast_floor(self):
        for scope, tokens in scopes().items():
            for token in SPAN_TOKENS:
                colour = palette.to_rgb(tokens[token])
                for surface in SURFACES:
                    ratio = palette.contrast(colour, palette.to_rgb(tokens[surface]))
                    self.assertGreaterEqual(
                        ratio, MIN_CONTRAST,
                        f"{scope} --{token} is {ratio:.2f}:1 on --{surface}")


class InterfaceContrast(unittest.TestCase):
    """Hints, menu entries and selected items stay readable in both themes.

    A muted label can move from the page to a panel, navigation rail or selected
    workspace row. Measuring only the page background misses those regressions.
    """

    def test_text_clears_aa_on_every_interface_surface(self):
        surfaces = (*SURFACES, "rail", "accent-soft")
        for scope, tokens in scopes().items():
            for token in ("text", "text-dim", "text-faint", "accent", "ok"):
                for surface in surfaces:
                    ratio = palette.contrast(palette.to_rgb(tokens[token]),
                                             palette.to_rgb(tokens[surface]))
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        f"{scope} --{token} is {ratio:.2f}:1 on --{surface}")

    def test_navigation_text_clears_aa_at_rest_and_on_hover(self):
        for scope, tokens in scopes().items():
            for surface in ("rail", "nav-hover", "accent-soft"):
                ratio = palette.contrast(palette.to_rgb(tokens["nav-text"]), palette.to_rgb(tokens[surface]))
                self.assertGreaterEqual(ratio, 4.5, f"{scope} navigation on {surface}: {ratio:.2f}")

    def test_primary_button_text_stays_readable_on_hover(self):
        for scope, tokens in scopes().items():
            for background in ("accent", "accent-strong"):
                ratio = palette.contrast(palette.to_rgb(tokens["btn-fg"]),
                                         palette.to_rgb(tokens[background]))
                self.assertGreaterEqual(
                    ratio, 4.5,
                    f"{scope} button text is {ratio:.2f}:1 on --{background}")


class Separation(unittest.TestCase):
    """All-pairs, not adjacent-pairs: any two kinds can end up stacked."""

    def _cross_family_pairs(self, tokens):
        for a, b in combinations(SPAN_TOKENS, 2):
            if FAMILY[a] != FAMILY[b]:
                yield a, b, palette.to_rgb(tokens[a]), palette.to_rgb(tokens[b])

    def test_families_are_separated_in_normal_vision(self):
        for scope, tokens in scopes().items():
            for a, b, ca, cb in self._cross_family_pairs(tokens):
                value = palette.delta_e_76(ca, cb)
                self.assertGreaterEqual(value, MIN_DELTA_E76,
                                        f"{scope} {a} vs {b} is ΔE76 {value:.1f}")

    def test_families_stay_separated_under_dichromacy(self):
        for scope, tokens in scopes().items():
            for a, b, ca, cb in self._cross_family_pairs(tokens):
                for kind in ("deuteranopia", "protanopia"):
                    value = palette.delta_e_2000(palette.simulate(ca, kind),
                                                 palette.simulate(cb, kind))
                    self.assertGreaterEqual(
                        value, MIN_CVD,
                        f"{scope} {a} vs {b} is ΔE2000 {value:.1f} under {kind}")

    def test_tool_and_subagent_are_separated_by_lightness(self):
        # The binding pair. Dichromacy collapses orange and green toward the
        # same hue, so hue alone cannot carry them and equal lightness makes
        # them identical — which is exactly how an earlier tuning failed.
        for scope, tokens in scopes().items():
            gap = abs(palette.lightness(palette.to_rgb(tokens["span-tool"]))
                      - palette.lightness(palette.to_rgb(tokens["span-subagent"])))
            self.assertGreater(gap, 8.0, f"{scope} tool/subagent differ by only {gap:.1f} L*")


class BlueRamp(unittest.TestCase):
    """llm/reasoning/output are one hue in three ordered steps.

    They are deliberately exempt from the cross-family ΔE floor — an ordinal
    family is supposed to look related — so lightness is what has to carry them.
    """

    def _ramp(self, tokens):
        return [palette.lightness(palette.to_rgb(tokens[t])) for t in RAMP]

    def test_the_ramp_is_monotone(self):
        for scope, tokens in scopes().items():
            steps = self._ramp(tokens)
            ordered = steps == sorted(steps) or steps == sorted(steps, reverse=True)
            self.assertTrue(ordered, f"{scope} ramp is not monotone: {steps}")

    def test_each_step_is_big_enough_to_read_as_ordered(self):
        for scope, tokens in scopes().items():
            values = self._ramp(tokens)
            for a, b in zip(values, values[1:]):
                self.assertGreaterEqual(abs(b - a), MIN_RAMP_STEP,
                                        f"{scope} ramp step {a:.0f}->{b:.0f} is too small")

    def test_the_ramp_steps_away_from_its_surface(self):
        # The whole point: stepping toward the surface buries the far end in the
        # track. On dark the ramp lightens, on light it darkens.
        all_scopes = scopes()
        dark = self._ramp(all_scopes["dark"])
        light = self._ramp(all_scopes["light"])
        self.assertLess(dark[0], dark[-1], "dark ramp must lighten away from the surface")
        self.assertGreater(light[0], light[-1], "light ramp must darken away from the surface")


class NoHardcodedColours(unittest.TestCase):
    """Every waterfall rule goes through a token, so both themes stay in sync.

    Bounded to the waterfall's own sections rather than "everything after this
    marker": a colour further down the file is somebody else's rule, and
    failing their edit for our reason wastes their time.
    """

    SECTIONS = ("agent trace waterfall", "breakdown + coverage strips",
                "selected-span detail", "distribution charts")

    def test_waterfall_rules_reference_tokens_only(self):
        css = palette.STYLESHEET.read_text(encoding="utf-8")
        marker = re.compile(r"/\* ---- (.+?) ---- \*/")
        offending = []
        current = None
        for line in css.splitlines():
            hit = marker.search(line)
            if hit:
                current = hit.group(1)
                continue
            if current not in self.SECTIONS:
                continue
            stripped = line.lstrip()
            if stripped.startswith(("/*", "*")):
                continue
            if re.search(r"#[0-9a-fA-F]{3,8}\b", line):
                offending.append(line.strip())
        self.assertEqual(offending, [], "hardcoded colours in waterfall rules")

    def test_the_sections_it_guards_actually_exist(self):
        # Otherwise a rename silently turns the check above into a no-op.
        css = palette.STYLESHEET.read_text(encoding="utf-8")
        for section in self.SECTIONS:
            self.assertIn(f"/* ---- {section} ---- */", css)


class Calibration(unittest.TestCase):
    """Does this script still agree with the numbers style.css states?

    If these drift, every other assertion in this module is measuring something
    other than what the stylesheet's comments claim, and the palette should be
    re-derived rather than the thresholds nudged.
    """

    CATEGORIES = ("cat-search", "cat-read", "cat-edit", "cat-execute", "cat-plan")

    def test_the_shipped_category_palette_still_measures_near_its_documented_worst_pair(self):
        # style.css says 17.0 (dark) and 16.7 (light); this script reads
        # 16.6/15.6, i.e. slightly stricter. Assert the neighbourhood, not the
        # exact figure — the point is that the model has not drifted.
        for scope, tokens in scopes().items():
            worst = min(
                min([palette.delta_e_2000(palette.to_rgb(tokens[a]), palette.to_rgb(tokens[b]))]
                    + [palette.delta_e_2000(palette.simulate(palette.to_rgb(tokens[a]), k),
                                            palette.simulate(palette.to_rgb(tokens[b]), k))
                       for k in ("deuteranopia", "protanopia")])
                for a, b in combinations(self.CATEGORIES, 2)
            )
            self.assertTrue(14.0 <= worst <= 19.0,
                            f"{scope} category worst pair measured {worst:.1f}, "
                            "outside the range this script was calibrated in")

    def test_known_reference_values(self):
        white, black = palette.to_rgb("#ffffff"), palette.to_rgb("#000000")
        self.assertAlmostEqual(palette.contrast(white, black), 21.0, places=2)
        self.assertAlmostEqual(palette.lightness(white), 100.0, places=2)
        self.assertAlmostEqual(palette.lightness(black), 0.0, places=2)
        self.assertAlmostEqual(palette.delta_e_2000(white, white), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
