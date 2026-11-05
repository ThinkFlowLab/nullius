"""The Supported pages say what the catalogue holds: each section's README
block and each entry's page are what `app/tests/catalogue_docs.py` renders
from the definitions now.

A definition added, a version pinned, a port proven or an option declared
without the pages following fails here; `python3 app/tests/catalogue_docs.py
--write` refreshes them, to be committed with the change.
"""
from __future__ import annotations

import os
import unittest

from nullius.catalogue import catalog
from tests import catalogue_docs


class SupportedPages(unittest.TestCase):
    def test_the_generated_files_are_current(self):
        self.assertEqual(catalogue_docs.stale(), [],
                         "run: python3 app/tests/catalogue_docs.py --write")

    def test_every_section_has_its_index_with_a_block(self):
        for kind, folder in catalogue_docs.SECTIONS.items():
            path = os.path.join(folder, catalogue_docs.INDEX)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn(catalogue_docs.START, text, path)
            self.assertIn(catalogue_docs.END, text, path)
            self.assertLess(text.find(catalogue_docs.START), text.find(catalogue_docs.END), path)

    def test_the_pages_name_every_definition(self):
        def named(key, text):
            # a key is a code chip: markdown's in a table, HTML's on a card
            self.assertTrue(f"`{key}`" in text or f"<code>{key}</code>" in text, key)
        agents = catalogue_docs.render_agents_index()
        for key in ("codex@2.2.2", "codex@0.16.0", "goose", "jiuwenswarm", "mini-swe-agent", "opencode", "terminus-2"):
            named(key, agents)
        for port in ("agent.task/v1", "agent.workspace/v1", "agent.tools/v1", "agent.conversation/v1"):
            self.assertIn(f"`{port}`", agents)
        pages = catalogue_docs.render_pages("agents")
        self.assertEqual(sorted(pages), ["codex.md", "goose.md", "jiuwenswarm.md", "mini-swe-agent.md", "opencode.md", "terminus-2.md"])
        named("codex@0.16.0", pages["codex.md"])
        for text in (agents, *pages.values()):
            self.assertNotIn("2026-", text, "no proof dates on a page")
        self.assertNotIn("gpt-5-mini", agents, "no proof models on a page")
        capabilities = catalogue_docs.render_features_index()
        named("mem0", capabilities)
        self.assertIn("`cap.memory/v1`", capabilities)
        self.assertEqual(sorted(catalogue_docs.render_pages("capabilities")), ["mem0.md"])
        benchmarks = catalogue_docs.render_benchmarks_index()
        pages = catalogue_docs.render_pages("benchmarks")
        for key in ("gsm8k", "humaneval", "locomo", "longmemeval", "swe-bench-verified", "tau3-bench", "terminal-bench"):
            named(key, benchmarks)
            self.assertIn(f'<a href="{key}.md">', benchmarks)
            self.assertIn("## Datasets", pages[f"{key}.md"])
        self.assertIn("https://github.com/openai/grade-school-math", pages["gsm8k.md"])   # the origin, linked
        # a benchmark on the standard says its definition's controls; one Harbor alone runs would say Harbor's two
        how = benchmarks[benchmarks.index("### How each runs"):]
        row = how[how.index('<a href="swe-bench-verified.md">'):].split("</tr>")[0]
        self.assertIn("<td>Nullius</td><td>the authors</td>", row)
        harbor_only = catalog.load().benchmark("swe-bench-verified")
        harbor_only.harness = ["harbor"]
        self.assertEqual(catalogue_docs._controls({}, harbor_only), {"nop": "Harbor", "oracle": "the authors"})
        self.assertEqual(catalogue_docs._controls({}, None), {})
        self.assertIn("## Cite", pages["gsm8k.md"])

    def test_apply_replaces_only_the_block(self):
        text = "# Page\n\nhand-written\n\n" + catalogue_docs.START + "\n\nold\n\n" + catalogue_docs.END + "\n\nmore\n"
        new = catalogue_docs.apply(text, "new block")
        self.assertTrue(new.startswith("# Page\n\nhand-written\n\n"))
        self.assertTrue(new.endswith(catalogue_docs.END + "\n\nmore\n"))
        self.assertIn("\n\nnew block\n\n", new)
        self.assertNotIn("old", new)
        with self.assertRaises(ValueError):
            catalogue_docs.apply("no markers", "x")


if __name__ == "__main__":
    unittest.main()
