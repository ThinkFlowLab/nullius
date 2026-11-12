"""The Benchmarks pages as a user discovers routes on them: the list, a card
per benchmark, and one page per benchmark folder that names its route - the
builder, or why there is none - as text a reader can quote.

Of the four discovery cases the spec names, the shipped catalogue renders
three - Harbor only, a harness of its own (a runner, the standard), and
Harbor beside one of those - and this suite pins them.

A benchmark's page carries its datasets: `DatasetsOnTheirPage` pins what
each one shows.

No workspace is opened; the pages read the shipped catalogue only."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius.catalogue import catalog

from .test_routes import web

SCRIPT = Path(__file__).resolve().parents[1] / "static" / "catalogue" / "catalogue.js"


def visible(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class PageCase(unittest.TestCase):
    """The rendered list and a benchmark's page; the tests are in the classes below."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()
        self.html = self.get("/benchmarks")

    def get(self, url, status=200):
        response = self.client.get(url)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, status, url)
        return response.get_data(as_text=True)

    def card(self, key):
        """One benchmark's card on the list, as markup."""
        match = re.search(r'<a class="bench-card" href="/benchmarks/%s".*?</a>\s*(?=<a class="bench-card"|</div>\s*</section>)' % re.escape(key), self.html, re.S)
        self.assertIsNotNone(match, key)
        return match.group(0)

    def page_html(self, key):
        """A benchmark's own page, the article only: the rail links to the builder on every page."""
        html = self.get("/benchmarks/%s" % key)
        return html[html.index('<article class="bench-page"'):html.index("</article>")]

    def page(self, key):
        return visible(self.page_html(key))

    def route_case(self, key):
        return re.search(r'<article class="bench-page" data-route-case="([^"]+)"', self.get("/benchmarks/%s" % key)).group(1)


class OriginTags(PageCase):
    """Every benchmark card and page says whether it is official - the
    publisher's, run as it certifies - or an experiment adapted here: a tag
    beside the category."""

    def test_a_card_and_its_page_carry_the_origin(self):
        key = "terminal-bench"
        self.assertIn('<span class="origin-tag official">Official</span>', self.card(key))
        self.assertIn('<span class="origin-tag official">Official</span>', self.page_html(key))
        self.assertIn('<div class="bench-card-tags"><span class="cat-tag">', self.card(key))       # beside the category, on the left

    def test_the_list_filters_by_what_runs_it(self):
        html = self.get("/benchmarks")
        self.assertIn('data-filter="runs"', html)
        self.assertIn('data-runs="standard harbor"', self.card("swe-bench-verified")); self.assertIn('data-runs="standard"', self.card("longmemeval"))

    def test_the_list_filters_by_origin(self):
        html = self.get("/benchmarks")
        self.assertIn('data-filter="origin"', html)
        for o in catalog.ORIGINS:
            self.assertIn(f'data-value="{o}"', html)
        self.assertIn('data-origin="official"', self.card("terminal-bench"))
        self.assertIn('data-origin="official"', self.card("longmemeval"))                 # every shipped entry is official


class CitationSeal(PageCase):
    """A benchmark's citation is its paper, named as a reader names it, beside a seal of its venue and year."""

    def test_the_page_shows_the_paper_its_seal_and_copies_its_entry(self):
        html = self.page_html("terminal-bench")
        self.assertIn('class="cite-seal"', html); self.assertIn(">2026</text>", html)
        self.assertIn("ICLR · 2026 · CONFERENCE PAPER · </textPath>", html)
        self.assertIn('class="cite-title">Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces</h3>', html)
        self.assertIn('data-copy="@inproceedings{merrill2026terminalbench', html)
        self.assertNotIn('class="bench-bibtex"', html)                                     # the raw BibTeX is copied, not shown
        tau = self.page_html("tau3-bench")
        self.assertIn("Copy all 3 BibTeX", tau); self.assertEqual(tau.count('<li><span><b>'), 2)   # one seal, the other two papers listed


class BenchmarksList(PageCase):
    def test_overlapping_datasets_use_the_distinct_headline_count(self):
        datasets = [catalog.Dataset(key=f"overlap-{n}", title=f"Subset {n}", benchmark="overlap",
                                    tasks=n, source="Hugging Face") for n in (165, 53, 86, 26)]
        benchmark = catalog.Benchmark(key="overlap", title="Overlap", category="QA", harness=["harbor"],
                                      task_count=165, datasets=[d.key for d in datasets])
        cat = catalog.Catalog(benchmarks=[benchmark], datasets=datasets)
        with patch.object(web.shared, "_catalog_cached", return_value=cat):
            self.html = self.get("/benchmarks")
            self.assertIn("165 tasks", visible(self.card("overlap")))
            page = self.page("overlap")
            self.assertIn("Tasks 165", page)
            self.assertNotIn("26–165", page)
            for n in (53, 86, 26):
                self.assertIn(f"Tasks {n}", page)

    def test_every_listed_benchmark_is_a_card_opening_its_page(self):
        cat = catalog.load()
        keys = re.findall(r'<a class="bench-card" href="/benchmarks/([^"]+)"', self.html)
        self.assertEqual(sorted(keys), sorted(b.key for b in cat.listed_benchmarks))
        self.assertEqual(len(keys), 8)
        # two shelves, the official benchmarks then the experiments; a shelf with nothing on it is not shown
        official = self.html[self.html.index('data-shelf="official"'):].split('</section>', 1)[0]
        for b in cat.listed_benchmarks:
            with self.subTest(b.key):
                self.assertEqual('/benchmarks/%s"' % b.key in official, b.origin == "official")
        self.assertEqual('data-shelf="experiment"' in self.html, any(b.origin == "experiment" for b in cat.listed_benchmarks))

    def test_a_card_says_what_it_holds(self):
        swe = visible(self.card("swe-bench-verified"))
        for fact in ("Coding", "Harbor", "SWE-bench Verified", "500 tasks", "1 dataset", "Tests scored by"):
            self.assertIn(fact, swe)
        memory = visible(self.card("longmemeval"))
        for fact in ("Nullius", "500 tasks each", "3 datasets", "GPT-4o judge scored by"):
            self.assertIn(fact, memory)
        self.assertNotIn("benchmark.json", memory)                                  # the folder is on the benchmark's page, not its card
        self.assertIn('data-keys="longmemeval longmemeval-s longmemeval-m longmemeval-oracle"', self.card("longmemeval"))

    def test_an_anchor_from_the_one_page_list_goes_on_to_its_page(self):
        # /benchmarks#longmemeval-m was a dataset inside a card; now it is that benchmark's page, at that tab
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("location.replace(owner.href", script)
        self.assertIn("dataset.keys.split(' ')", script)

    def test_every_listed_benchmark_has_its_mark(self):
        # one mark per benchmark, by key - its maker's logo or a line icon - in the sprite the builder's cards and these pages use
        marks = (Path(__file__).resolve().parents[1] / "static" / "catalogue" / "marks.svg").read_text(encoding="utf-8")
        builder = (Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder.js").read_text(encoding="utf-8")
        self.assertIn("/static/catalogue/marks.svg#bm-${esc(key)}", builder)
        for b in catalog.load().listed_benchmarks:
            with self.subTest(b.key):
                self.assertIn('<symbol id="bm-%s"' % b.key, marks)
                self.assertIn('marks.svg#bm-%s"' % b.key, self.card(b.key))

    def test_a_page_exists_only_for_a_listed_benchmark(self):
        self.get("/benchmarks/nope", status=404)


class BenchmarkPage(PageCase):
    def test_a_benchmark_on_both_runners_opens_the_builder(self):
        self.assertEqual(self.route_case("swe-bench-verified"), "multi-route")
        html, text = self.page_html("swe-bench-verified"), self.page("swe-bench-verified")
        self.assertIn('href="/new-run?standard=swe-bench-verified"', html)
        self.assertNotIn("No usable route", text)
        self.assertNotIn("nothing to switch to", text)
        self.assertNotIn("harness=", html)
        self.assertIn("Runs with Nullius Harbor", text)                             # the glance, Nullius first, not a runner section
        self.assertNotIn('id="runs-with"', html)

    def test_a_benchmark_only_the_standard_runs_builds_a_run_on_it(self):
        for key in ("longmemeval", "locomo"):
            self.assertEqual(self.route_case(key), "non-harbor")                     # the standard's, not Harbor's
            self.assertIn(f'href="/new-run?standard={key}"', self.page_html(key))
            self.assertNotIn("No usable route", self.page(key))
            self.assertIn('<span class="origin-tag official">Official</span>', self.page_html(key))

    def test_every_listed_benchmark_has_a_page_in_one_of_the_three_cases(self):
        cases = {self.route_case(b.key) for b in catalog.load().listed_benchmarks}
        self.assertEqual(cases, {"multi-route", "non-harbor"})                         # the standard's, or both

    def test_a_benchmark_whose_own_scripts_run_says_so_with_no_native_runner(self):
        for key, repo in (("longmemeval", "xiaowu0162/LongMemEval @ 9e0b455"), ("locomo", "snap-research/locomo @ 3eb6f2c")):
            text = self.page(key)
            self.assertIn(repo, text)                                                    # the upstream the standard pins
            self.assertIn("Original", text)                                              # its own scripts: the Original setup
            self.assertNotIn("Native", text); self.assertNotIn("runner.json", text)

    def test_the_page_shows_its_folder(self):
        self.assertIn("benchmarks/longmemeval/ ├─ benchmark.json ├─ driver.py ├─ standard.json └─ upstream-settings.json", self.page("longmemeval"))
        self.assertIn("benchmarks/swe-bench-verified/ ├─ benchmark.json ├─ driver.py ├─ images.json ├─ standard.json └─ upstream-settings.json", self.page("swe-bench-verified"))
        self.assertIn("benchmarks/terminal-bench/ ├─ benchmark.json ├─ driver.py ├─ standard.json └─ upstream-settings.json", self.page("terminal-bench"))


class DatasetsOnTheirPage(PageCase):
    """A benchmark's page carries its datasets: the task count, the official
    link, the verifier, the notes, and where its data comes from - one row per
    way it runs, each fetched by its harness, with no command for a person to run."""

    def test_a_single_dataset_shows_every_fact_and_where_each_way_reads_it(self):
        html, text = self.page_html("terminal-bench"), self.page("terminal-bench")
        for fact in ("Tasks 89", "github.com/laude-institute/terminal-bench",
                     "Data Nullius GitHub laude-institute/terminal-bench-2 2fd12b8 Nullius fetches the repository at this commit",
                     "Harbor Harbor registry terminal-bench@2.0 Harbor downloads it when a run needs it"):
            self.assertIn(fact, text)
        self.assertNotIn("How to get it", text)
        self.assertNotIn('class="copy-mini"', html)
        d = catalog.load().dataset("terminal-bench")
        self.assertIn("Verifier " + d.verifier, text)
        if d.notes:
            self.assertIn(d.notes, text)
        self.assertEqual(html.count('id="terminal-bench"'), 1)
        self.assertIn('<section class="cat-dataset" id="terminal-bench" aria-label="Dataset: Terminal-Bench 2.0">', html)
        self.assertNotIn('class="bench-tabs"', html)                                   # one dataset: no tabs

    def test_several_datasets_are_tabs_each_with_its_anchor(self):
        html, text = self.page_html("longmemeval"), self.page("longmemeval")
        keys = re.findall(r'<section class="cat-dataset" id="([^"]+)"', html)
        self.assertEqual(keys, ["longmemeval-s", "longmemeval-m", "longmemeval-oracle"])
        self.assertEqual(re.findall(r'role="tab" id="tab-[^"]+" aria-controls="([^"]+)"', html), keys)
        for title in ("LongMemEval-S (cleaned)", "LongMemEval-M (cleaned)", "LongMemEval oracle"):
            self.assertIn(title, text)
        self.assertEqual(text.count("Nullius Hugging Face xiaowu0162/longmemeval-cleaned 98d7416"), 3)
        self.assertNotIn("Native", text)
        self.assertIn("Tasks 500 each", text)                                               # the glance
        self.assertIn("longmemeval_s_cleaned.json 277.4 MB, checked by sha256", text)
        self.assertEqual(text.count("Verifier LongMemEval's own judge"), 3)
        self.assertNotIn("Adapter", text)
        # the script selects the tab a #dataset names
        self.assertIn("const fromHash", SCRIPT.read_text(encoding="utf-8"))

    def test_a_harbor_benchmark_names_the_agent_it_runs_as_published_with(self):
        for key, agent in (("terminal-bench", "terminus-2"), ("swe-bench-verified", "swe-agent")):
            html = self.page_html(key)
            self.assertIn('id="published-title">As published</h2>', html, key)
            self.assertIn(f'<span class="mono">{agent}</span>', html, key)
        self.assertNotIn('id="published-title"', self.page_html("gsm8k"))              # the standard's setups say it instead

    def test_a_benchmarks_open_work_is_not_on_its_page(self):
        for b in catalog.load().listed_benchmarks:
            with self.subTest(b.key):
                self.assertNotIn("Next steps", self.page(b.key))

    def test_a_page_ends_with_its_citation(self):
        html = self.page_html("longmemeval")
        cite = html[html.index('<section class="bench-section cat-citation"'):]
        self.assertIn("@article{wu2024longmemeval,", cite)
        self.assertIn('data-copy="@article{wu2024longmemeval,', cite)                     # copied whole
        self.assertNotIn("From ", visible(cite))                                          # the dataset names the repository already
        self.assertLess(html.index('class="cat-dataset"'), html.index('cat-citation"'))   # after the datasets

    def test_each_way_names_its_own_source_once(self):
        html, text = self.page_html("tau3-bench"), self.page("tau3-bench")
        self.assertIn('<h3 class="bench-ds-title">τ³-bench</h3>', html)               # its title, though it is the benchmark's
        self.assertEqual(text.count("sierra-research/tau3-bench Harbor downloads it"), 1)   # Harbor's hub name, said once
        self.assertIn("Nullius GitHub sierra-research/tau2-bench v1.0.1 · fc0055d", text)   # the standard reads the upstream's release
        self.assertIn('href="https://github.com/sierra-research/tau2-bench/tree/fc0055dc4e0a316c3f83133267fbd6faaa770992"', html)
        self.assertNotIn("harbor datasets download", html)                                   # nothing for a person to run

    def test_a_dataset_the_driver_downloads_names_it_not_the_scorer(self):
        text = self.page("gaia")
        self.assertEqual(text.count("Nullius Hugging Face gaia-benchmark/GAIA 682dd72"), 4)   # data-source.json, not the leaderboard Space
        self.assertNotIn("Repository https://huggingface.co/spaces/", text)

    def test_environments_add_no_section_to_a_benchmark_page(self):
        for key in ("gaia", "terminal-bench"):
            with self.subTest(key):
                self.assertNotIn("Environment requirements", self.page(key))


class SetupsOnTheirPage(PageCase):
    def test_substitute_models_and_normal_scoring_rules_are_named_clearly(self):
        for key in ("gsm8k", "humaneval"):
            text = self.page(key)
            self.assertIn("Setup Original Model Choose in the run builder", text)
            self.assertIn("Good to know", text)
        html = self.get("/benchmarks/gsm8k")
        notes = html.split('aria-labelledby="good-to-know"', 1)[1].split('</section>', 1)[0]
        limits = html.split('aria-labelledby="limitations"', 1)[1].split('</section>', 1)[0]
        self.assertIn("18.00", notes)
        self.assertNotIn("18.00", limits)


    def test_one_original_setup_with_model_selection_in_the_builder(self):
        for key in ("locomo", "longmemeval", "gsm8k", "humaneval", "tau3-bench"):
            html = self.get("/benchmarks/" + key)
            section = html.split('id="setups"', 1)[1].split('</section>', 1)[0]
            self.assertEqual(section.count('class="std-setup-row"'), 1)
            self.assertIn('<h3>Original</h3>', section)
            self.assertNotIn('Original, your model', section)
            self.assertNotIn('<h3>Your ', section)


class CategoryFilters(unittest.TestCase):
    def test_multiword_categories_and_multiple_runners(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is needed to exercise catalogue filters")
        script = Path(__file__).resolve().parents[1] / "static/catalogue/catalogue.js"
        program = r"""
const assert = require('node:assert/strict');
const cards = [
 {dataset: {category:'Tool use', runs:'standard harbor', find:'appworld', keys:'appworld'}},
 {dataset: {category:'Customer service', runs:'standard', find:'tau', keys:'tau'}},
 {dataset: {category:'Coding', runs:'harbor', find:'swe', keys:'swe'}}
];
const chips = [['category','Tool use'], ['category','Customer service'], ['category',''],
 ['runs','harbor'], ['runs','standard'], ['runs','']].map(([filter,value])=>({
 dataset:{value}, closest:()=>({dataset:{filter}}),
 classList:{toggle(){}}, setAttribute(){}, addEventListener(_,fn){this.click=fn;}
}));
const none={};
global.location={hash:''};
global.document={
 querySelectorAll:s=>s==='.bench-card'?cards:s==='[data-filter] .bench-cat'?chips:[],
 getElementById:id=>id==='bench-none'?none:null
};
require(process.argv[1]);
const shown=()=>cards.filter(c=>!c.hidden).map(c=>c.dataset.keys);
chips[0].click(); assert.deepEqual(shown(),['appworld']);
chips[3].click(); assert.deepEqual(shown(),['appworld']);
chips[1].click(); assert.deepEqual(shown(),[]); assert.equal(none.hidden,false);
chips[4].click(); assert.deepEqual(shown(),['tau']);
chips[2].click(); assert.deepEqual(shown(),['appworld','tau']);
chips[5].click(); assert.equal(shown().length,3);
"""
        subprocess.run([node, "-e", program, str(script)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
