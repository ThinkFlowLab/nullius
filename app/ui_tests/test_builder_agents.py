"""The builder's Agent step grid (/new-run/agents): the card toolkit's pick cards for one benchmark, from the
entries the Agents page lists, greyed as the builder's payload says."""
import json
import os
import re
import shutil
import tempfile
import unittest
from html import unescape
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

    def test_osworld1_offers_its_own_agents_and_codex_and_says_why_not_the_rest(self):
        html, cards = self.grid("osworld1")
        on = [k for k, c in cards.items() if " disabled" not in c[:c.index("</button>")]]
        self.assertEqual(len([k for k in on if k.startswith("osworld1:")]), 12)
        self.assertEqual([k for k in on if not k.startswith("osworld1:")], ["codex@2.2.2"])
        self.assertTrue(list(cards)[0].startswith("osworld1:"))                                  # its own first
        self.assertNotIn("original", cards)                                                         # they are its Original
        self.assertIn('<button type="button" class="card-link" data-pick="osworld1:openai-cua" aria-pressed="false"', cards["osworld1:openai-cua"])
        self.assertIn('<span class="bench-harness">OSWorld1&#39;s own</span>', cards["osworld1:openai-cua"])
        self.assertIn('<div class="bench-card-blocker" title="MiniMax M3 belongs to OSWorld2">Belongs to OSWorld2</div>', cards["osworld2:m3"])
        self.assertIn('data-f-from="elsewhere&#10;"', cards["osworld2:m3"])
        self.assertIn(">Lacks computer</div>", cards["goose"])
        self.assertIn('<span class="std-tag" title="', cards["codex@2.2.2"])                     # the ports it proved that it needs
        self.assertIn(">Computer use</span>", cards["codex@2.2.2"])
        pills = lambda col: re.findall(r'class="filter-pill"[^>]*>([^<]*)<', html[html.index(f'data-pills="{col}"'):].split("</div>")[0])
        self.assertEqual(pills("protocol"), ["All", "ACP", "A command", "Its own script"])
        self.assertEqual(pills("fit"), ["Any", "Can run it", "Cannot"])
        menu = unescape(html[html.index('data-tools="filter"'):])
        self.assertIn("> Bundled</label><label><input type=\"checkbox\" data-value=\"benchmark\" checked> OSWorld1's own</label>"
                      "<label><input type=\"checkbox\" data-value=\"elsewhere\" checked> Other benchmarks' agents", menu)
        self.assertIn('data-filter-menu="proves"', menu)
        self.assertNotIn("data-tools-btn=\"sort\"", html)

    def test_a_benchmark_without_own_agents_keeps_its_original_and_greys_osworlds(self):
        _, cards = self.grid("gsm8k")
        self.assertEqual(list(cards)[0], "original")
        self.assertIn(">No agent · Original</button>", cards["original"])
        self.assertIn('<span class="std-tag">as published</span>', cards["original"])            # how it runs, not a protocol
        self.assertIn('data-f-protocol=""', cards["original"])
        html, _ = self.grid("gsm8k")
        self.assertNotIn('data-value="reference"', html)
        for k, c in cards.items():
            if k.startswith("osworld"):
                self.assertIn(" disabled", c)
                self.assertRegex(c, r">Belongs to OSWorld[12]</div>")

    def test_a_capability_benchmark_lists_capabilities(self):
        _, cards = self.grid("locomo")
        self.assertIn("original", cards)
        self.assertIn("mem0", cards)
        self.assertFalse([k for k in cards if k.startswith("osworld") or k.startswith("codex")])

    def test_an_unknown_benchmark_is_not_found_and_the_page_loads_the_toolkit(self):
        self.assertEqual(self.client.get("/new-run/agents?benchmark=nope").status_code, 404)
        page = self.client.get("/new-run").get_data(as_text=True)
        self.assertIn("/static/tables.js", page)
        self.assertIn("/static/catalogue/catalogue.css", page)


if __name__ == "__main__":
    unittest.main()
