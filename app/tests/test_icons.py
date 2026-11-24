"""Every icon a page names is a symbol of the sprite, app/templates/_icons.svg, drawn the sprite's way."""
from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SPRITE = APP / "templates" / "_icons.svg"
SOURCES = [p for p in [*(APP / "templates").rglob("*"), *(APP / "static").rglob("*")]
           if p.suffix in (".html", ".js", ".css") and p != SPRITE] + [*(APP / "web").glob("*.py"), *(APP / "nullius").rglob("*.py")]
# Symbols drawn their own way, each with its reason.
STYLE_EXCEPTIONS: dict[str, str] = {}

ICON_KWARG = ("templates/**/*.html", r"\bicon=(?!true\b|false\b)([^,)\s]+)")   # card fields, greyed()
# Each place that builds a symbol id from a value: (file, the expression after "#i-") -> where its values are
# written, as (files, pattern, inner pattern). A pattern captures an argument as written; it must be a quoted name.
SINKS = {
    ("templates/base.html", "icon"): [("templates/base.html", r"for kind, label, list_ep, entry_ep, icon in \[(.*?)\] %\}", r",\s*([^,()]+)\)")],
    ("templates/_cards.html", "f.icon"): [ICON_KWARG],
    ("templates/_tables.html", "key"): [("templates/_tables.html", r"for key, name in \((.*?)\) %\}", r"\(([^,()]+),")],
    ("templates/_tables.html", "c.filter_icon"): [("templates/**/*.html", r"\bfilter_icon=([^,)\s]+)")],
    ("templates/_tables.html", "icon"): [ICON_KWARG],
    ("templates/runs/launches.html", "icon[state]"): [("templates/runs/launches.html", r"set icon = \{(.*?)\} %\}", r":\s*([^,}]+)")],
    ("templates/settings/settings.html", "icon"): [
        ("templates/settings/settings.html", r"(?<!macro )\bsection_link\([^,]+,[^,]+,\s*([^,]+),"),
        ("templates/settings/settings.html", r"(?<!macro )\bact\([^,]+,\s*([^,]+),"),
        ("templates/settings/settings.html", r"(?<!macro )\bnever\(\s*([^,]+),")],
    ("static/builder/command-builder.js", "name"): [("static/builder/command-builder.js", r"\b(?:icon|iconTile)\(([^)]*)\)")],
}


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# A symbol drawn per size: its 16-grid drawing, its 24-grid one, then a 32-grid one, each shown at exactly its
# size.
SIZES = (("0 0 16 16", re.escape("display:var(--ic-d16,inline);stroke-width:var(--ic-w16,") + r"(\d)\)"),
         ("0 0 24 24", re.escape("display:var(--ic-d24,none);stroke-width:var(--ic-w24,") + r"(\d)\)"),
         ("0 0 32 32", re.escape("display:var(--ic-d32,none);stroke-width:var(--ic-w32,") + r"(\d)\)"))
DUO = "fill-opacity:var(--ic-duo"   # the duotone layer, its tone following the theme
ARGS = dict(zip("MLHVCSQTAZ", (2, 2, 1, 1, 6, 4, 4, 2, 7, 0)))


def sprite(path: Path = SPRITE) -> ET.Element:
    return ET.fromstring(re.sub(r"\{#.*?#\}", "", text(path), flags=re.S))


def symbols(path: Path = SPRITE) -> list[ET.Element]:
    return sprite(path).findall("./defs/symbol")


def alias_of(symbol: ET.Element) -> str | None:
    """The id an alias stands for: a symbol whose only child is a <use> of another symbol; None for a drawn symbol."""
    children = list(symbol)
    if [e.tag for e in children] == ["use"] and not (symbol.text or "").strip() and not (children[0].tail or "").strip():
        return children[0].get("href", "").removeprefix("#")
    return None


def straight_lines(path: str):
    """Each horizontal or vertical segment of a path's data, as ("x" or "y", the coordinate it lies on)."""
    tokens = re.findall(r"[A-Za-z]|-?(?:\d*\.\d+|\d+\.?)(?:e[-+]?\d+)?", path)
    x = y = sx = sy = 0.0
    cmd, i = "M", 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd, i = tokens[i], i + 1
            if cmd in "Zz":
                if (x == sx) != (y == sy):
                    yield ("x", x) if x == sx else ("y", y)
                x, y = sx, sy
                continue
        n = ARGS[cmd.upper()]
        a = [float(v) for v in tokens[i:i + n]]
        i += n
        rel = cmd.islower()
        if cmd in "Hh":
            nx, ny = a[0] + (x if rel else 0), y
        elif cmd in "Vv":
            nx, ny = x, a[0] + (y if rel else 0)
        else:
            nx, ny = a[-2] + (x if rel else 0), a[-1] + (y if rel else 0)
        if cmd in "LlHhVv" and (nx, ny) != (x, y):
            if nx == x:
                yield "x", x
            elif ny == y:
                yield "y", y
        x, y = nx, ny
        if cmd in "Mm":
            sx, sy = x, y
            cmd = "l" if rel else "L"


class Icons(unittest.TestCase):
    names = frozenset(s.get("id", "").removeprefix("i-") for s in symbols())

    def assertKnown(self, name: str, where: str):
        self.assertIn(name, self.names, f"{where}: no symbol i-{name} in {SPRITE.name}")

    def test_every_literal_reference_is_a_symbol(self):
        for path in SOURCES:
            for name in re.findall(r"#i-([a-z0-9]+(?:-[a-z0-9]+)*)", text(path)):
                self.assertKnown(name, str(path.relative_to(APP)))

    def test_every_name_passed_as_data_is_a_symbol(self):
        seen = set()
        for path in SOURCES:
            where = path.relative_to(APP).as_posix()
            for expr in re.findall(r"#i-(?:\{\{\s*(.*?)\s*\}\}|\$\{(.*?)\}|(['\"`]))", text(path)):
                sink = (where, next(e for e in expr if e))
                self.assertIn(sink, SINKS, f"{where}: a symbol id built from {sink[1]!r}: list where its values come from in SINKS")
                seen.add(sink)
        self.assertEqual(seen, set(SINKS), "a sink in SINKS is no longer in the code")
        for sink, sites in SINKS.items():
            for files, pattern, *inner in sites:
                written = [arg for path in APP.glob(files) for m in re.finditer(pattern, text(path), re.S)
                           for arg in (re.findall(inner[0], m.group(1)) if inner else [m.group(1)])]
                self.assertTrue(written, f"{sink}: {pattern!r} finds nothing")
                for arg in written:
                    literal = re.fullmatch(r"\s*(['\"])([a-z0-9-]+)\1\s*", arg)
                    self.assertTrue(literal, f"{sink}: cannot resolve {arg.strip()!r} to an icon name")
                    self.assertKnown(literal.group(2), f"{sink[0]} via {files}")
        for path in [*(APP / "web").glob("*.py"), *(APP / "nullius").rglob("*.py")]:   # bare names built in Python
            for name in re.findall(r"""['"](?:filter_)?icon['"]\s*:\s*['"]([a-z0-9-]+)['"]|\b(?:filter_)?icon=['"]([a-z0-9-]+)['"]""", text(path)):
                self.assertKnown(next(n for n in name if n), str(path.relative_to(APP)))

    def test_symbols_are_unique_and_take_their_style_from_the_page(self):
        ids = [s.get("id") for s in symbols()]
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])
        self.assertTrue(all(i and i.startswith("i-") for i in ids), ids)
        for symbol in symbols():
            name = symbol.get("id")
            if name in STYLE_EXCEPTIONS or alias_of(symbol):
                continue
            self.assertEqual(symbol.get("viewBox"), "0 0 24 24", name)
            for part in symbol.iter():
                self.assertFalse({"stroke", "stroke-width", "fill"} & set(part.attrib), f"{name}: {part.attrib}")

    def test_a_symbol_drawn_per_size_puts_its_straight_lines_on_whole_pixels(self):
        """Horizontal and vertical lines are whole pixels wide at the size shown: a 1 px line on .5, a 2 px line on 0."""
        for symbol in symbols():
            name = symbol.get("id")
            drawings = [e for e in symbol if e.tag == "svg"]
            if alias_of(symbol):
                continue
            self.assertIn([e.tag for e in symbol if e.tag != "title"], (["svg"] * 2, ["svg"] * 3), name)
            self.assertEqual(len(drawings), 3, f"{name}: no 32-grid drawing")
            for drawing, (box, style), weight in zip(drawings, SIZES, (1, 1, 2)):
                self.assertEqual(drawing.get("viewBox"), box, name)
                match = re.fullmatch(style, drawing.get("style", ""))
                self.assertTrue(match, f"{name}: the {box} drawing's style is {drawing.get('style')!r}")
                width = int(match.group(1))
                self.assertEqual(width, weight, f"{name}: one weight per size")
                centre = width % 2 / 2
                for e in drawing:
                    lines = list(straight_lines(e.get("d"))) if e.tag == "path" else [] if e.tag != "rect" else [
                        (axis, float(e.get(a)) + float(e.get(b, 0)) * k) for axis, a, b in (("x", "x", "width"), ("y", "y", "height")) for k in (0, 1)]
                    # the second tone, a filled area: an edge under a stroke, or on a pixel boundary
                    edges = (centre, 0.0) if DUO in e.get("style", "") else (centre,)
                    for axis, at in lines:
                        self.assertIn(at % 1, edges, f"{name}: a {width} px line on {axis}={at} in the {box} drawing")

    def test_straight_lines_are_read_from_path_data(self):
        self.assertEqual(list(straight_lines("M7.5 3.5v8M3.5 7.5h8")), [("x", 7.5), ("y", 7.5)])
        self.assertEqual(list(straight_lines("M1 1l2 2H6a1 1 0 0 1 1 1V9h-3z")), [("y", 3), ("x", 7), ("y", 9)])
        self.assertEqual(list(straight_lines("m2 2 3 0 0 4")), [("y", 2), ("x", 5)])
        self.assertEqual(list(straight_lines("M1 1A1 1 0 0 1 2 2A1 1 0 0 1 1 1Z")), [])

    def test_an_alias_names_a_drawn_symbol_of_its_own_sprite(self):
        by_id = {s.get("id"): s for s in symbols()}
        for sid, symbol in by_id.items():
            target = alias_of(symbol)
            if target is None:
                self.assertNotIn("use", [e.tag for e in symbol.iter()], f"{sid}: a <use> beside a drawing")
                continue
            self.assertEqual(symbol.get("viewBox"), "0 0 24 24", sid)
            self.assertEqual(set(symbol[0].attrib), {"href"}, f"{sid}: the <use> carries only its href")
            self.assertTrue(target in by_id, f"{sid}: an alias of no symbol #{target}")
            self.assertIsNone(alias_of(by_id[target]), f"{sid}: an alias of the alias #{target}")

    def test_an_alias_is_read_from_its_only_child(self):
        def symbol(body):
            return ET.fromstring(f'<symbol id="i-a" viewBox="0 0 24 24">{body}</symbol>')
        self.assertEqual(alias_of(symbol('<use href="#i-b"/>')), "i-b")
        self.assertIsNone(alias_of(symbol('<use href="#i-b"/><path d="M1 1h2"/>')))
        self.assertIsNone(alias_of(symbol('<svg viewBox="0 0 16 16"><path d="M1 1h2"/></svg>')))


if __name__ == "__main__":
    unittest.main()
