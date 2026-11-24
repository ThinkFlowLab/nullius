"""A run with a spending cap, as its pages show it: the job page says the cap,
that it stopped the run, and the units it cut off or never began, read from
the job as a standard run writes it; the builder keeps a recipe's cap."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import classify
from nullius.standard.calls import ledger
from nullius.standard.runner import job
from ui_tests.builder_support import AGENT as CUSTOM_AGENT, APP, PIN, CompilerCase
from .test_routes import web

BENCH = {"key": "fakebench", "_ref": "fakebench", "family": "agent", "needs": ["agent.workspace/v1"],
         "units": {"noun": "task", "nouns": "tasks"}, "score": {"kind": "pass", "says": "the tests pass"}}
AGENT = {"key": "fake", "_kind": "agent", "_ref": "fake"}


def capped_job(root: Path, name: str, ran: int, planned: int, budget=None, cut=(), cut_by=None):
    """A standard job of ``planned`` units of which the first ``ran`` have a trial; those in ``cut`` ended at the cap."""
    d = root / name
    d.mkdir(parents=True)
    units = [f"t-{i}" for i in range(planned)]
    job.write_config(str(d), job=name, bench=BENCH, other=AGENT, model="m-1", provider="openai", dataset="all", units=units,
                     concurrency=1, granted=True)
    results = []
    for k, u in enumerate(units[:ran]):
        trial = d / f"{u}__u{k:04d}"
        trial.mkdir()
        exc = (ledger.ERROR, "the run's spending cap") if u in cut else ((cut_by or {})[u], "its own spending cap") if u in (cut_by or {}) else None
        results.append(job.write_trial(str(trial), job=name, bench=BENCH, other=AGENT, model="m-1", unit=u, t0=1_760_000_000.0 + k,
                                       t1=1_760_000_005.0 + k, produce=(1_760_000_000.0 + k, 1_760_000_004.0 + k),
                                       score=None if exc else {"reward": 1.0}, exception=exc, calls=[], trajectory=None))
    job.write_result(str(d), name, 1_760_000_000.0, 1_760_000_060.0, results, planned, budget=budget)


class CappedJobPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-capped-job-")
        tmp = Path(cls.temp.name)
        capped_job(tmp / "jobs", "stopped", 6, 10, cut=("t-5",),
                   budget={"max_usd": 0.5, "spent_usd": 0.41, "unsettled_usd": 0.03, "stopped": True, "cut_off": ["t-5"],
                           "not_run": ["t-6", "t-7", "t-8", "t-9"]})
        capped_job(tmp / "jobs", "within", 4, 4, budget={"max_usd": 0.395, "spent_usd": 0.004, "stopped": False, "cut_off": [], "not_run": []})
        capped_job(tmp / "jobs", "uncapped", 4, 4)
        capped_job(tmp / "jobs", "merged", 3, 4, budget={"max_usd": 0.154, "spent_usd": 0.014991, "stopped": True, "cut_off": [], "not_run": ["t-3"],
                                                         "shards": 4, "shards_stopped": 1})
        capped_job(tmp / "jobs", "unit-only", 4, 4, budget={"max_usd": None, "spent_usd": None, "stopped": True, "cut_off": [], "not_run": [],
                                                            "unit_max_usd": 0.002, "over_unit": []})
        capped_job(tmp / "jobs", "per-unit", 4, 4, cut_by={"t-2": ledger.UNIT_ERROR},
                   budget={"max_usd": None, "spent_usd": 0.004, "stopped": False, "cut_off": [], "not_run": [], "unit_max_usd": 0.002, "over_unit": ["t-2"]})
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        cls.ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs")], label="capped")
        web.shared.open_workspace(cls.ws)
        cls.client = web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        cls.temp.cleanup()

    def page(self, name):
        r = self.client.get(f"/job/{name}?view=results"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, name)
        return r.get_data(as_text=True)

    def text(self, html, pattern):
        got = re.search(pattern, html, re.S)
        self.assertIsNotNone(got, pattern)
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", got[0]))).strip()

    def test_a_run_the_cap_stopped_says_so_with_what_was_spent_and_the_units_left_out(self):
        html = self.page("stopped")
        self.assertEqual(self.text(html, r'<div class="alert" id="cap-stopped">.*?</div>'),
                         "Stopped at its spending cap of $0.50, with $0.41 spent: 1 cut off, 4 not run.")   # a neutral notice: nothing failed
        self.assertEqual(self.text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.50")
        self.assertIn("$0.03", unescape(html))                                            # what stays booked for calls whose cost was never reported
        self.assertEqual(self.text(html, r'<div class="popm cap-units".*?</div>'), "t-6 t-7 t-8 t-9")
        self.assertIn("4 not run</summary>", html)
        # the unit it cut off is a trial with its own outcome, outside the rate
        self.assertRegex(html, r'class="[^"]*oc-spending_cap[^"]*"[^>]*data-o="spending_cap"')
        self.assertIn(classify.LABELS[classify.SPENDING_CAP] + '<span class="infra">infra</span>', html)
        self.assertIn("6 of 10 planned", self.text(html, r'<div class="run-meta[^\"]*".*?</header>'))

    def test_a_run_within_its_cap_says_the_cap_and_no_more_and_one_without_says_nothing(self):
        html = self.page("within")
        self.assertEqual(self.text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.395")       # as it was given, not rounded to $0.40
        for absent in ('id="cap-stopped"', "cap-units", "not run</summary>", "oc-spending_cap"):
            self.assertNotIn(absent, html)
        plain = self.page("uncapped")
        for absent in ('id="run-cap"', 'id="cap-stopped"', "Spending cap"):
            self.assertNotIn(absent, plain)


    def test_a_run_with_a_cap_per_unit_says_it_and_the_units_that_reached_it_count_against_the_agent(self):
        html = self.page("per-unit")
        self.assertEqual(self.text(html, r'<div class="kv" id="unit-cap".*?</div>'), "Cap per task $0.002")
        self.assertIn("1 reached it</summary>", html)
        self.assertEqual(self.text(html, r'<div class="popm cap-units".*?</div>'), "t-2")
        for absent in ('id="run-cap"', 'id="cap-stopped"', "oc-spending_cap"):          # the run had no cap of its own, and was not stopped
            self.assertNotIn(absent, html)
        self.assertRegex(html, r'class="[^"]*oc-agent_error[^"]*"[^>]*data-o="agent_error"')


    def test_a_merged_job_says_how_many_shards_stopped_and_a_run_without_its_own_cap_says_no_stop(self):
        html = self.page("merged")
        self.assertEqual(self.text(html, r'<div class="alert" id="cap-stopped">.*?</div>'),
                         "1 of 4 shards stopped at their share of the spending cap of $0.154, with $0.015 spent: 0 cut off, 1 not run.")   # not "$0.01"
        alone = self.page("unit-only")                                                   # recorded stopped with no cap of the run's: nothing to say, no error
        self.assertNotIn('id="cap-stopped"', alone)
        self.assertEqual(self.text(alone, r'<div class="kv" id="unit-cap".*?</div>'), "Cap per task $0.002")

    def test_a_trial_with_no_record_of_a_judge_has_no_judge_panel(self):
        """Scored by its tests, never by a judge: nothing to say about one."""
        r = self.client.get("/trial/uncapped/t-0__u0000"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn('id="runner-verdict"', html)
        self.assertNotIn("No record of its call", html)


class RunFactsOnTheJobPage(unittest.TestCase):
    """What identifies a run, as the job page shows it: in the header's cards, each fact a standard job records
    beneath the card it belongs to, read from the job as a run writes it, the names its definitions give; a card
    with nothing more does not open; and beside the mean score over the trials scored, the mean over every trial
    planned."""

    @classmethod
    def setUpClass(cls):
        from tests.test_parse import BENCH as VERSIONED, NOP, SCORED, standard_job
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-run-facts-")
        tmp = Path(cls.temp.name)
        jobs = tmp / "jobs"
        jobs.mkdir()
        standard_job(str(jobs), "facts", [1.0, 0.0, None], budget={"max_usd": 0.5, "spent_usd": 0.1, "stopped": False, "cut_off": [], "not_run": []})
        standard_job(str(jobs), "named", [1.0], bench={k: v for k, v in VERSIONED.items() if k != "_version"} | {"key": "locomo", "_ref": "locomo"},
                     bench_options={"rag_mode": "dialog"}, options={}, served_by=None, timeouts=None, max_turns=None, max_calls=None, retries=0)
        result = Path(standard_job(str(jobs), "unserved", [1.0], served_by=None)) / "result.json"      # a run that kept no serving record
        result.write_text(json.dumps({k: v for k, v in json.loads(result.read_text()).items() if k != "served"}))
        standard_job(str(jobs), "scored", [1.0, 0.5, 0.0, None], bench=SCORED)
        standard_job(str(jobs), "every", [1], concurrency=2, repeat=3, max_turns=30, max_calls=100, retries=2,
                     timeouts={"score": 30, "verify": 90, "agent": 7200, "image": 3600, "setup": 600},
                     constraints={"cpus": 2, "memory_mb": 512, "processes": 128, "cpu_set": "0-1", "gpu_ids": [], "concurrency": 2})
        standard_job(str(jobs), "nop", [0.0], other=NOP, model="", options={}, served_by=None)
        harbor = jobs / "harbor"
        (harbor / "t__1").mkdir(parents=True)
        (harbor / "config.json").write_text('{"agents": [{"name": "claude-code", "model_name": "m-1"}], "datasets": [{"path": "../swe"}]}')
        (harbor / "result.json").write_text('{"stats": {}}')
        (harbor / "t__1" / "result.json").write_text('{"task_name": "swe/t", "trial_name": "t__1", "verifier_result": {"rewards": {"reward": 1}}}')
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        cls.ws = workspace.create(str(tmp / "ws"), [str(jobs)], label="facts")
        web.shared.open_workspace(cls.ws)
        cls.client = web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        cls.temp.cleanup()

    page = CappedJobPage.page
    text = CappedJobPage.text

    def header(self, html):
        return re.search(r'<div class="run-meta run-identity".*?(?=<div class="page-head-actions">|</header>)', html, re.S)[0]

    def cards(self, html):
        """The header's cards that open, by the first word each card says (a fact's name, or the agent's): the
        (name, value) rows beneath it. They are one group of disclosures, so the browser keeps one open at a time."""
        found = re.findall(r'<details class="pop run-identity-more" name="run-identity">\s*<summary class="run-identity-card">(.*?)'
                           r'<svg class="ic dd-chev"[^>]*>.*?</summary>\s*<dl class="run-identity-details">(.*?)</dl>', self.header(html), re.S)
        self.assertEqual(len(found), self.header(html).count("<details"))
        return {re.search(r'<span class="[kv]">([^<]*)', card)[1].strip():
                [(self.text(dt, r".*"), self.text(dd, r".*")) for dt, dd in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", rows, re.S)]
                for card, rows in found}

    def test_a_standard_job_shows_what_identifies_it_beneath_its_cards_and_no_credential(self):
        from tests.test_parse import SECRET
        html = self.page("facts")
        self.assertEqual(self.cards(html), {
            "Benchmark": [("Temperature", "0.2")],
            "fake": [("Reasoning effort", "high"), ("Max tokens", "4096"), ("Api key", "[REDACTED]"), ("Note", "from [REDACTED]"),
                     ("Provider", "OpenRouter"), ("Requested", "deepinfra/fp8, together"), ("Answered by", "DeepInfra · 3 calls")],
            "Trials": [("Concurrent", "3"), ("Retries", "2"), ("Turns", "30"), ("Model calls", "100"), ("Agent limit", "10 min")]})
        self.assertNotIn(SECRET, html)
        self.assertEqual(self.text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.50")    # the cap stays under Run totals
        self.assertIn("Benchmark fakebench 2 Dataset all", self.text(self.header(html), r".*"))             # the names on the cards
        self.assertIn("fake v1.2 m-1", self.text(self.header(html), r".*"))
        self.assertRegex(self.header(html), r'^<div class="run-meta run-identity"[^>]*>\s*<div class="run-identity-card">\s*'
                                            r'<div><span class="k">Runner</span>')                          # the runner has nothing more

    def test_names_come_from_the_definitions_and_a_fact_not_recorded_has_no_row(self):
        html = self.page("named")
        self.assertIn("Benchmark LoCoMo", self.text(self.header(html), r".*"))
        self.assertEqual(self.cards(html), {"Benchmark": [("RAG mode", "dialog")], "fake": [("Provider", "OpenRouter"), ("Answered by", "DeepInfra · 1 call")],
                                            "Trials": [("Concurrent", "3"), ("Retries", "0")]})
        self.assertEqual(list(self.cards(self.page("nop"))), ["Benchmark", "Trials"])                     # a control asks no model
        self.assertNotIn("dd-chev", self.header(self.page("harbor")))                                    # nothing recorded: no card opens
        self.assertEqual(self.cards(self.page("every"))["Trials"], [
            ("Concurrent", "2"), ("Attempts", "3"), ("Retries", "2"), ("Turns", "30"), ("Model calls", "100"),
            ("Setup limit", "10 min"), ("Image limit", "60 min"), ("Agent limit", "120 min"), ("Verifier limit", "1.5 min"),
            ("Score limit", "0.5 min")])

    def test_with_no_serving_record_the_provider_stays_and_requested_and_answered_by_go(self):
        self.assertEqual([name for name, _ in self.cards(self.page("unserved"))["fake"]],
                         ["Reasoning effort", "Max tokens", "Api key", "Note", "Provider"])

    def test_the_mean_over_every_trial_planned_stands_beside_the_mean_over_the_trials_scored(self):
        html = self.page("scored")
        self.assertEqual(self.text(html, r'<div class="kv"[^>]*><span class="k">Mean score</span>.*?</div>'), "Mean score 0.50")
        self.assertEqual(self.text(html, r'<div class="kv" id="planned-mean-total".*?</div>'), "Mean over planned 0.38")
        self.assertEqual(self.text(html, r'<dd id="planned-mean".*?</dd>'), "0.38")
        self.assertEqual(self.text(html, r'<div class="hero[^"]*" id="adjusted-rate">.*?</div>'), "0.50")
        for passed in ("facts", "harbor"):                                                               # a pass or fail benchmark: unchanged
            self.assertNotIn("planned-mean", self.page(passed))

    def test_both_runs_list_views_show_the_planned_mean(self):
        response = self.client.get("/runs")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(re.findall(r'<small class="mean-planned faint">(.*?)</small>', html), ["0.38 planned", "0.38 planned"])
        self.assertRegex(html, r'<div class="la-rate la-score"[^>]*><b>0\.50</b>')
        self.assertRegex(html, r'<td data-col="adjusted" data-sort="0\.5"[^>]*>0\.50')


class BuilderKeepsTheCap(CompilerCase):
    def test_a_saved_recipe_keeps_an_amount_above_zero_and_nothing_else(self):
        out = self.js('const back=v=>C.fromJSON(JSON.stringify({...C.createStandard("gsm8k"),max_usd:v})).max_usd;'
                      'process.stdout.write(JSON.stringify([back(2),back(0.25),back(0),back(-1),back("2"),back(true),back(null),'
                      '"max_usd" in C.createStandard("gsm8k")].map(v=>v===undefined?"none":v)));')
        self.assertEqual(out, [2, 0.25, "none", "none", "none", "none", "none", False])
        each = self.js('const back=v=>C.fromJSON(JSON.stringify({...C.createStandard("gsm8k"),max_usd_per_unit:v})).max_usd_per_unit;'
                       'process.stdout.write(JSON.stringify([back(0.01),back(0),back("1")].map(v=>v===undefined?"none":v)));')
        self.assertEqual(each, [0.01, "none", "none"])

    def test_a_harbor_recipe_takes_no_cap(self):
        """Harbor's calls do not pass the relay, so nothing could hold them to one: the field is the standard's alone."""
        out = self.js('const r=C.fromJSON(JSON.stringify({...C.create("harbor"),max_usd:2}));' + CUSTOM_AGENT + PIN
                      + 'process.stdout.write(JSON.stringify(["max_usd" in r,/max.usd/i.test(JSON.stringify(C.compile(r)))]));')
        self.assertEqual(out, [False, False])
        page = (APP / "static" / "builder" / "command-builder.js").read_text(encoding="utf-8")
        self.assertEqual((page.count("'max_usd'"), page.count("'max_usd_per_unit'")), (1, 1))   # offered once each, on the standard's Limits step
        self.assertRegex(page, r"C\.standardKeyless\(rec\) \? '' : field\('[^']*', 'max_usd'")   # a run that calls no model is offered none


if __name__ == "__main__":
    unittest.main()
