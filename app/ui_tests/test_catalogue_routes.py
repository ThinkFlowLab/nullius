"""The Benchmarks pages as a user discovers routes on them: the list, a card
per benchmark, and one page per benchmark folder that names its route - the
builder, or why there is none - as text a reader can quote.

No workspace is opened; the pages read the shipped catalogue only."""
from __future__ import annotations

import json
import re
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
        match = re.search(r'<article class="bench-card"(?:(?!</article>).)*href="/benchmarks/%s".*?</article>' % re.escape(key), self.html, re.S)
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
    """Every benchmark page says whether it is official - the publisher's
    benchmark - or an experiment, as a tag; the cards are filtered by it."""

    def test_a_page_carries_the_origin_and_a_card_is_filtered_by_it(self):
        key = "terminal-bench"
        self.assertIn('data-f-origin="official&#10;"', self.card(key))                          # filtered by, not tagged
        self.assertNotIn('origin-tag', self.card(key))
        self.assertIn('<span class="origin-tag official">Official</span>', self.page_html(key))
        self.assertIn('<span class="origin-tag official">Official</span>', self.page_html("gaia"))

    def test_the_list_is_cards_by_default_and_a_table_on_request(self):
        self.assertIn('href="/benchmarks?view=table" aria-label="Table" title="Table" aria-current="false"', self.html)
        self.assertIn('href="/benchmarks" aria-label="Cards" title="Cards" aria-current="true"', self.html)
        self.assertNotIn("data-table", self.html)
        html = self.get("/benchmarks?view=table&q=1")
        self.assertIn('href="/benchmarks?view=table&amp;q=1" aria-label="Table" title="Table" aria-current="true"', html)
        self.assertIn('href="/benchmarks?q=1" aria-label="Cards" title="Cards" aria-current="false"', html)   # the default has no view
        self.assertNotIn("data-cards", html); self.assertNotIn('class="bench-card"', html)
        heads = re.findall(r'<th data-col="(\w+)"', html)
        self.assertEqual(heads[:3], ["title", "category", "origin"])
        self.assertNotIn("icon", heads)                                                     # the mark is in the name cell
        row = re.search(r'<tr data-search="[^"]*"[^>]*data-f-origin="official&#10;"[^>]*>(?:(?!</tr>).)*href="/benchmarks/swe-bench-verified".*?</tr>', html, re.S).group(0)
        self.assertIn('data-f-runs="standard&#10;harbor&#10;"', row)
        self.assertIn('<td data-col="tasks" data-sort="500" class="align-right">', row)
        self.assertIn('data-pills="category"', html); self.assertIn('data-filter-menu="origin"', html)
        self.assertEqual(re.findall(r'data-sort-col value="(\w*)"', html[html.index('data-tools="sort"'):]), ["", "title", "tasks", "datasets"])
        self.assertIn('<input type="checkbox" data-col="needs" data-off> Needs', html)       # secondary: off until chosen
        origins = re.findall(r'data-f-origin="(\w+)&#10;"', html)
        self.assertEqual(origins, sorted(origins, key=list(catalog.ORIGINS).index))         # the shelves' order

        self.assertIn('<input type="search" data-search placeholder="Name, category, dataset"', self.html)
        self.assertIn('data-search="LongMemEval Memory longmemeval', self.card("longmemeval"))
        self.assertIn('<p class="empty-note" data-empty="benchmarks" hidden>No benchmark matches.</p>', self.html)


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
            self.assertIn("Tasks 165", visible(self.card("overlap")))
            page = self.page("overlap")
            self.assertIn("Tasks 165", page)
            self.assertNotIn("26–165", page)
            for n in (53, 86, 26):
                self.assertIn(f"Tasks {n}", page)

    def test_every_listed_benchmark_is_a_card_opening_its_page(self):
        cat = catalog.load()
        keys = re.findall(r'<a class="card-link" href="/benchmarks/([^"]+)"', self.html)
        self.assertEqual(sorted(keys), sorted(b.key for b in cat.listed_benchmarks))
        self.assertEqual(len(keys), 11)
        # one catalogue: no shelf per origin; the origin filters the cards, never a tag on them
        self.assertNotIn("data-shelf", self.html)
        self.assertIn('data-f-origin="official&#10;experiment&#10;"', self.card("locomo"))
        self.assertNotIn("Experiment", visible(self.card("locomo")))

    def test_a_card_says_what_it_holds(self):
        swe = visible(self.card("swe-bench-verified"))
        for fact in ("Coding", "Harbor", "SWE-bench Verified", "Tasks 500", "Dataset 1", "Scored by Tests"):
            self.assertIn(fact, swe)
        memory = visible(self.card("longmemeval"))
        for fact in ("Nullius", "Tasks each 500", "Datasets 3", "Scored by GPT-4o judge"):
            self.assertIn(fact, memory)
        self.assertNotIn("benchmark.json", memory)                                  # the folder is on the benchmark's page, not its card
        self.assertIn('data-keys="longmemeval longmemeval-s longmemeval-m longmemeval-oracle"', self.card("longmemeval"))

    def test_every_listed_benchmark_has_its_mark(self):
        # one mark per benchmark, by key - its maker's logo or a line icon - in the sprite the builder's cards and these pages use
        marks = (Path(__file__).resolve().parents[1] / "static" / "catalogue" / "marks.svg").read_text(encoding="utf-8")
        builder = self.client.get("/new-run/benchmarks").get_data(as_text=True)                  # the builder's cards are these
        for b in catalog.load().listed_benchmarks:
            with self.subTest(b.key):
                self.assertIn('<symbol id="bm-%s"' % b.key, marks)
                self.assertIn('marks.svg#bm-%s"' % b.key, self.card(b.key))
                self.assertIn('marks.svg#bm-%s"' % b.key, builder)

    def test_a_page_exists_only_for_a_listed_benchmark(self):
        self.get("/benchmarks/nope", status=404)


class BenchmarkPage(PageCase):
    def test_a_benchmark_with_versions_is_the_whole_page_once_per_version_one_shown(self):
        from html.parser import HTMLParser

        def shown(html):
            """The page as a reader sees it: every hidden version block left out, its whole subtree."""
            out, depth = [], [0]
            void = {"input", "br", "img", "meta", "link", "hr", "source", "wbr"}

            class Strip(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    a = dict(attrs)
                    if depth[0]:
                        depth[0] += tag not in void
                    elif "data-version" in a and "hidden" in a and tag not in void:
                        depth[0] = 1
                    else:
                        out.append(self.get_starttag_text())

                def handle_endtag(self, tag):
                    if depth[0]:
                        depth[0] -= 1
                    else:
                        out.append(f"</{tag}>")

                def handle_data(self, data):
                    if not depth[0]:
                        out.append(data)
            Strip(convert_charrefs=False).feed(html)
            return "".join(out)
        html = self.get("/benchmarks/osworld1")
        nav = re.search(r'<nav class="std-versions".*?</nav>', html, re.S).group(0)
        self.assertIn('aria-current="page" data-ref="osworld1"><b>desktop-env-v1.0.3</b>', nav)   # the release is the default
        self.assertIn('data-ref="osworld1@044bc33"', nav)
        self.assertIn('data-build-run href="/new-run?standard=osworld1"', html)                # Build a run opens the version shown
        self.assertIn("<b>main (044bc33)</b>", nav)                                          # an untagged pin: its branch
        page = shown(html)
        self.assertIn("xlang-ai/OSWorld @ desktop-env-v1.0.3 · 9d6f711", page)
        self.assertNotIn("xlang-ai/OSWorld @ main · 044bc33", page)
        self.assertIn("<small>360 tasks</small>", page); self.assertNotIn("<small>361 tasks</small>", page)
        self.assertIn("368 tasks in 10 domains", page); self.assertNotIn("369 tasks in 10 domains", page)
        self.assertIn("comparable only with this version", page)                             # its own limitations
        self.assertIn('name="version" value="desktop-env-v1.0.3"', page)                    # its checks run on it
        self.assertIn("<dd><span class=\"std-version\" data-version=\"desktop-env-v1.0.3\">368</span>", page)
        old = shown(self.get("/benchmarks/osworld1?v=044bc33"))
        self.assertIn("xlang-ai/OSWorld @ main · 044bc33", old)
        self.assertIn("<small>361 tasks</small>", old); self.assertIn("369 tasks in 10 domains", old)
        self.assertIn("Only M3, PromptAgent and Qwen-CUA have completed tasks", old)
        self.assertNotIn("comparable only with this version", old)
        self.assertIn('name="version" value="044bc33"', old)
        self.assertIn('data-build-run href="/new-run?standard=osworld1@044bc33"', old)
        self.assertIn("/tree/044bc33901cb893808e3f91ebc9a17ed8e54b44c", old)                  # its data at its own commit
        self.assertNotIn("/tree/9d6f711", old)
        self.get("/benchmarks/osworld1?v=nope", status=404)
        self.assertNotIn("data-version", self.get("/benchmarks/gsm8k"))                       # one version: nothing to swap

    def test_a_proof_short_of_the_full_mark_has_no_green_tick_and_says_its_bound(self):
        # Open Telco's oracle must score at least 93%: 3GPP TSG's scorer cannot read RAN_AH1, so its rows stop short
        html = self.page_html("open-telco")
        row = lambda label: re.search(r'<th scope="row">%s<.*?</tr>' % re.escape(label), html, re.S).group(0)
        self.assertIn('<span class="std-proof std-proof-part" title="93.55%, at least 93% needed">', row("3GPP TSG · Full"))
        self.assertIn("✓ 1871/2000</span>", row("3GPP TSG · Full"))
        self.assertIn('<span class="std-proof std-proof-part" title="94%, at least 93% needed">', row("3GPP TSG · Lite"))
        self.assertIn("the reference answers: must score at least 93% - made by Nullius", row("3GPP TSG · Lite"))
        self.assertIn('<span class="std-proof std-proof-ok">✓', row("TeleQnA · Lite"))      # every unit: green
        self.assertNotIn("std-proof-part", row("TeleQnA · Lite"))
        self.assertIn("no answer at all: must score nothing", row("TeleQnA · Lite"))

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
            self.assertRegex(self.page_html(key), r'<span class="origin-tag official[^"]*"[^>]*>Official</span>')

    def test_an_experiment_says_what_it_changes_and_the_official_version_does_not(self):
        html = self.page_html("locomo")
        self.assertIn('<h2 id="changes-full">Changes from official</h2>', html)
        self.assertEqual(html.count("Changes from official"), 1)
        self.assertIn('<a href="https://github.com/snap-research/locomo/pull/41">open pull request #41</a>.', html)

    def test_an_option_with_a_pattern_shows_its_example_not_the_pattern(self):
        for key in ("osworld2", "gsm8k", "humaneval", "gaia", "osworld1", "tau3-bench"):
            html, opts = self.page_html(key), json.loads((Path(catalog.__file__).parent / "benchmarks" / key / "standard.json").read_text())["options"]
            for o in opts.values():
                if "pattern" in o:
                    self.assertIn("e.g. <code>%s</code>" % o["example"], html)
                    self.assertNotIn(o["pattern"], unescape(html))
        self.assertNotIn("Provider default", self.page("osworld1"))

    def test_a_machine_setup_shows_its_label_and_its_key_without_one(self):
        osworld2 = self.page("osworld2")
        for label in ("Docker", "Scoring tools"):
            self.assertIn("Setup: " + label, osworld2)
            self.assertIn("needs " + label, osworld2)
        self.assertNotIn("Setup: scoring_programs", osworld2)
        self.assertIn("Setup: Docker for the tasks' VMs", self.page("osworld1"))

    def test_a_card_names_what_its_options_need_by_label(self):
        for key, said in (("locomo", "NVIDIA GPU"), ("longmemeval", "NVIDIA GPU, NVIDIA GPU with 12000 MiB"),
                          ("osworld2", "Docker, Scoring tools"), ("tau3-bench", "Sandbox runtime")):
            with self.subTest(key):
                card = self.card(key)
                self.assertIn(f'title="Some of its options need {said} installed on the machine it runs on: see its page"', card)
                self.assertIn(f"</svg>needs {said}</div>", card)
        self.assertNotIn("_gpu", self.html)


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

    def test_a_dataset_the_driver_downloads_names_it_not_the_scorer(self):
        text = self.page("gaia")
        self.assertEqual(text.count("Nullius Hugging Face gaia-benchmark/GAIA 682dd72"), 4)   # data-source.json, not the leaderboard Space
        self.assertNotIn("Repository https://huggingface.co/spaces/", text)

    def test_an_empty_limitations_list_shows_no_section(self):
        self.assertNotIn("Nullius limitations", self.page("gaia"))
        self.assertNotIn("none recorded", self.page("gaia"))


if __name__ == "__main__":
    unittest.main()
