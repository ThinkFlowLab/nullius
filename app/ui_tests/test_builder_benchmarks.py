"""The builder's Benchmark step grid (/new-run/benchmarks): the Benchmarks page's cards as single-choice pick cards,
with what each benchmark evaluates, agents or capabilities, as a menu of its own."""
import json
import os
import re
import shutil
import tempfile
import unittest
from html import unescape
from unittest.mock import patch

from .test_routes import web


class BenchmarkGrid(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="nullius-bench-grid-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = patch.dict(os.environ, {"NULLIUS_HOME": tmp, "HOSTS": "", "RUNS_DIR": os.path.join(tmp, "runs")})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("NULLIUS_CATALOGUE", None)
        self.client = web.app.test_client()

    def get(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)

    def test_it_lists_the_benchmarks_pages_benchmarks_each_a_single_choice(self):
        html = self.get("/new-run/benchmarks")
        cards = {m.group(1): m.group(0) for m in re.finditer(r'<article class="bench-card".*?data-pick="([^"]+)".*?</article>', html, re.S)}
        listed = re.findall(r'<a class="card-link" href="/benchmarks/([^"]+)"', self.get("/benchmarks"))
        self.assertEqual(sorted(cards), sorted(listed))                                              # one listing
        for k, c in cards.items():
            self.assertIn(f'data-pick="{k}" data-pick-one aria-pressed="false"', c)
        self.assertIn('data-f-evaluates="capability&#10;"', cards["locomo"])
        self.assertIn('<span class="std-tag" title="Memory capabilities">Capabilities</span>', cards["locomo"])
        self.assertIn('<span class="std-tag">Agents</span>', cards["swe-bench-verified"])         # Harbor runs agents
        self.assertIn('<dt>Tasks</dt><dd>1,319</dd>', cards["gsm8k"])
        self.assertIn('<span class="cat-tag">Math</span>', cards["gsm8k"])
        self.assertNotIn("Official", html)                                                          # only a community one says so
        self.assertIn('title="Real-world questions', cards["gaia"])                                 # its domain, as its tooltip

        html = self.get("/new-run/benchmarks?view=table")
        self.assertIn('<table class="grid sortable tools snug" id="nb-benchmarks" data-table>', html)
        self.assertIn('<td data-col="_pick" class="pick-col sticky-col"><input type="radio" name="nb-benchmarks-pick" data-pick="gsm8k" aria-label="GSM8K"></td>', html)
        self.assertNotIn('type="checkbox" data-pick', html)
        self.assertIn('#nb-benchmarks > * > tr > :nth-child(2) { width: auto;', html)                 # Name takes the spare width
        self.assertEqual(re.findall(r'<th data-col="(\w+)"', html)[:3], ["_pick", "title", "evaluates"])

    def test_a_benchmark_with_versions_carries_each_ones_tasks(self):
        html = self.get("/new-run/benchmarks")
        card = re.search(r'<article class="bench-card"[^>]*id="pick-bench-osworld1"[^>]*>', html).group(0)
        tasks = json.loads(unescape(re.search(r'data-tasks="([^"]*)"', card).group(1)))
        self.assertEqual(tasks[""]["text"], "368")                                                # the default version's
        self.assertEqual(tasks["044bc33"], {"text": "369", "label": "Tasks"})
        self.assertNotIn("data-tasks", re.search(r'<article class="bench-card"[^>]*id="pick-bench-gsm8k"[^>]*>', html).group(0))
        self.assertRegex(self.get("/new-run/benchmarks?view=table"), r'<tr[^>]*id="pick-bench-osworld1"[^>]*data-tasks=')       # its row too


if __name__ == "__main__":
    unittest.main()
