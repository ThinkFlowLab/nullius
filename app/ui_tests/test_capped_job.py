"""A run with a spending cap, as its pages show it: the job page says the cap,
that it stopped the run, and the units it cut off or never began, read from
the job as a standard run writes it; the builder keeps a recipe's cap."""
from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import classify
from nullius.standard import job, ledger
from ui_tests.builder_support import AGENT as CUSTOM_AGENT, APP, PIN, CompilerCase
from .test_routes import web

BENCH = {"key": "fakebench", "_ref": "fakebench", "family": "agent", "needs": ["agent.workspace/v1"],
         "units": {"noun": "task", "nouns": "tasks"}, "score": {"kind": "pass", "says": "the tests pass"}}
AGENT = {"key": "fake", "_kind": "agent", "_ref": "fake"}


def capped_job(root: Path, name: str, ran: int, planned: int, budget=None, cut=()):
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
        exc = (ledger.ERROR, "the run's spending cap") if u in cut else None
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
                         "Stopped at its spending cap of $0.50, with $0.41 spent: 1 cut off, 4 not run.")
        self.assertNotIn('class="alert error" id="cap-stopped"', html)                    # nothing failed: a neutral notice
        self.assertEqual(self.text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.50")
        self.assertIn("$0.03 stays booked for calls whose cost was never reported", unescape(html))
        self.assertEqual(self.text(html, r'<div class="popm cap-units".*?</div>'), "t-6 t-7 t-8 t-9")
        self.assertIn("4 not run</summary>", html)
        # the unit it cut off is a trial with its own outcome, outside the rate
        self.assertRegex(html, r'class="[^"]*oc-spending_cap[^"]*"[^>]*data-o="spending_cap"')
        self.assertIn(classify.LABELS[classify.SPENDING_CAP] + '<span class="infra">infra</span>', html)
        self.assertIn("6 of 10 planned", self.text(html, r'<div class="run-meta".*?</div>'))

    def test_a_run_within_its_cap_says_the_cap_and_no_more_and_one_without_says_nothing(self):
        html = self.page("within")
        self.assertEqual(self.text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.395")       # as it was given, not rounded to $0.40
        for absent in ('id="cap-stopped"', "cap-units", "not run</summary>", "oc-spending_cap"):
            self.assertNotIn(absent, html)
        plain = self.page("uncapped")
        for absent in ('id="run-cap"', 'id="cap-stopped"', "Spending cap"):
            self.assertNotIn(absent, plain)


class BuilderKeepsTheCap(CompilerCase):
    def test_a_saved_recipe_keeps_an_amount_above_zero_and_nothing_else(self):
        out = self.js('const back=v=>C.fromJSON(JSON.stringify({...C.createStandard("gsm8k"),max_usd:v})).max_usd;'
                      'process.stdout.write(JSON.stringify([back(2),back(0.25),back(0),back(-1),back("2"),back(true),back(null),'
                      '"max_usd" in C.createStandard("gsm8k")].map(v=>v===undefined?"none":v)));')
        self.assertEqual(out, [2, 0.25, "none", "none", "none", "none", "none", False])

    def test_a_harbor_recipe_takes_no_cap(self):
        """Harbor's calls do not pass the relay, so nothing could hold them to one: the field is the standard's alone."""
        out = self.js('const r=C.fromJSON(JSON.stringify({...C.create("harbor"),max_usd:2}));' + CUSTOM_AGENT + PIN
                      + 'process.stdout.write(JSON.stringify(["max_usd" in r,/max.usd/i.test(JSON.stringify(C.compile(r)))]));')
        self.assertEqual(out, [False, False])
        page = (APP / "static" / "builder" / "command-builder.js").read_text(encoding="utf-8")
        self.assertEqual(page.count("'max_usd'"), 1)                                        # offered once, on the standard's Limits step
        self.assertIn("C.standardKeyless(rec) ? '' : field('Spending cap (USD)', 'max_usd'", page)


if __name__ == "__main__":
    unittest.main()
