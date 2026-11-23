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


def sprite() -> ET.Element:
    return ET.fromstring(re.sub(r"\{#.*?#\}", "", text(SPRITE), flags=re.S))


def symbols() -> list[ET.Element]:
    return sprite().findall("./defs/symbol")


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

    def test_symbols_take_their_style_from_the_page(self):
        for symbol in symbols():
            name = symbol.get("id")
            if name in STYLE_EXCEPTIONS:
                continue
            self.assertEqual(symbol.get("viewBox"), "0 0 24 24", name)
            for part in symbol.iter():
                self.assertFalse({"stroke", "stroke-width", "fill"} & set(part.attrib), f"{name}: {part.attrib}")

    def test_symbol_ids_are_unique(self):
        ids = [s.get("id") for s in symbols()]
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])
        self.assertTrue(all(i and i.startswith("i-") for i in ids), ids)


if __name__ == "__main__":
    unittest.main()
