"""The builder's Agent step grid (/new-run/agents): the card toolkit's pick cards for one benchmark, from the
entries the Agents page lists, greyed as the builder's payload says."""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from .test_routes import web


class AgentGrid(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="nullius-grid-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = patch.dict(os.environ, {"NULLIUS_HOME": tmp, "HOSTS": "", "RUNS_DIR": os.path.join(tmp, "runs")})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("NULLIUS_CATALOGUE", None)
        self.client = web.app.test_client()

    def grid(self, ref):
        r = self.client.get("/new-run/agents?benchmark=" + ref)
        self.assertEqual(r.status_code, 200, ref)
        html = r.get_data(as_text=True)
        cards = {m.group(1): m.group(0) for m in re.finditer(r'<article class="bench-card".*?data-pick="([^"]+)".*?</article>', html, re.S)}
        return html, cards

    def schema(self):
        html = self.client.get("/new-run").get_data(as_text=True)
        return json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))["standard"]

    def test_its_greying_is_the_builders_for_every_benchmark(self):
        st = self.schema()
        offered = {o["key"] for o in st["agents"] + st["capabilities"]}
        for b in st["benchmarks"]:
            with self.subTest(b["key"]):
                _, cards = self.grid(b["key"])
                fits = {k: v for k, v in st["fits"][b["key"]].items() if k in offered}
                self.assertEqual({k for k in cards if k != "original"}, set(fits))
                self.assertEqual({k for k, c in cards.items() if " disabled" in c[:c.index("</button>")]}, {k for k, v in fits.items() if v})

    def test_the_version_procedure_is_the_first_card_and_other_benchmarks_agents_are_disabled(self):
        _, cards = self.grid("gsm8k")
        self.assertEqual(list(cards)[0], "original")
        self.assertIn(">GSM8K</button>", cards["original"])                                    # named as its settings tab
        _, cards = self.grid("locomo@full")
        self.assertIn(">LoCoMo · full</button>", cards["original"])                            # the version's, no commit
        html, _ = self.grid("gsm8k")
        self.assertNotIn('data-value="reference"', html)
        for k, c in cards.items():
            if k.startswith("osworld"):
                self.assertIn(" disabled", c)
                self.assertRegex(c, r">Belongs to OSWorld[12]</div>")

    def test_its_table_view_picks_as_its_cards_do(self):
        cards, _ = self.grid("osworld1")
        self.assertIn('href="/new-run/agents?benchmark=osworld1&amp;view=table" aria-label="Table"', cards)    # the toggle, cards by default
        r = self.client.get("/new-run/agents?benchmark=osworld1&view=table")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('<table class="grid sortable tools snug" id="nb-agents" data-table>', html)
        self.assertNotIn("data-cards", html)
        self.assertIn('<td data-col="_pick" class="pick-col sticky-col"><input type="checkbox" data-pick="osworld1:openai-cua" aria-label="OpenAI CUA"></td>', html)
        self.assertIn('<input type="checkbox" data-pick="goose" aria-label="Goose 1.53.0" disabled>', html)
        self.assertNotIn("data-check-all", html)                                                        # one at a time: no box ticks them all
        self.assertIn('<th data-col="_pick" scope="col" class="pick-col sticky-col"><span class="sr">Chosen</span></th>', html)   # as narrow as a check column
        self.assertIn('#nb-agents > * > tr > :nth-child(2) { width: auto;', html)                        # Name takes the spare width, not the box
        self.assertNotIn("pick-check", html)                                                            # the box is the mark
        self.assertNotIn("/static/catalogue/agents/goose.svg</td>", html)                              # its logo beside its name, not a column
        heads = re.findall(r'<th data-col="(\w+)"', html)
        self.assertEqual(heads[:4], ["_pick", "title", "alert", "protocol"])                            # the box first, why not beside the name

    def test_an_unknown_benchmark_is_not_found_and_the_page_loads_the_toolkit(self):
        self.assertEqual(self.client.get("/new-run/agents?benchmark=nope").status_code, 404)
        page = self.client.get("/new-run").get_data(as_text=True)
        self.assertIn("/static/tables.js", page)
        self.assertIn("/static/catalogue/catalogue.css", page)


if __name__ == "__main__":
    unittest.main()
