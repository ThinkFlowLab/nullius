"""The feasibility page: worded as feasibility checking, naming the task
scope and the controls that actually executed, and showing both control
outcomes for every bracketed task. The job page links to it only when a
control was recorded with the job.

Jobs are synthetic control runs from tests.test_feasibility, plus Harbor's
real oracle-only capture; nothing is run."""
from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius.runs import parse
from tests.test_feasibility import write_job, swe_report
from .test_routes import web, APP_DIR


def text_of(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class FeasibilityPage(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = [
            write_job(self.tmp.name, "tb-x4-nop", "nop", {"good": 0, "leaky": 1, "env": "EnvironmentSetupError", "half": 0}, n_tasks=4),
            write_job(self.tmp.name, "tb-x4-oracle", "oracle", {"good": 1, "leaky": 1, "env": 1}, n_tasks=4),
            write_job(self.tmp.name, "tb-x4-agent", "my-agent", {"good": 1, "leaky": 1, "env": 0, "half": 0}, n_tasks=4),
            write_job(self.tmp.name, "unrelated", "my-agent", {"good": 1}),
        ]
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=self.jobs))
        self.client = web.app.test_client()

    def get(self, path, status=200):
        response = self.client.get(path)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, status, path)
        return response.get_data(as_text=True)

    def row(self, html, task):
        match = re.search(r'<tr data-task="%s"[^>]*>.*?</tr>' % re.escape(task), html, re.S)
        self.assertIsNotNone(match, task)
        return match.group(0)

    def test_the_page_is_worded_as_feasibility_checking_not_a_search(self):
        html = self.get("/job/tb-x4-agent/feasibility")
        meaning = " ".join(unescape(re.sub(r"<[^>]+>", "", re.search(r'<p class="subtitle" id="feasibility-meaning">(.*?)</p>', html, re.S).group(1))).split())
        self.assertIn("Is each task feasible: can an empty solution fail, and can a reference solution pass?", meaning)
        self.assertIn("says nothing about whether an agent will succeed", meaning)
        self.assertNotIn("hyperparameter", text_of(html)); self.assertNotIn("search", meaning)
        self.assertLess(len(meaning), 260)                                                          # one breath, not an essay

    def test_the_scope_and_the_executed_controls_are_stated(self):
        html = self.get("/job/tb-x4-agent/feasibility")
        self.assertIn('<span id="feasibility-scope">4 tasks of tb-small (--n-tasks 4)</span>', html)
        executed = text_of(re.search(r'<div class="feas-controls" id="feasibility-executed".*?</div>\s*</div>', html, re.S).group(0))
        self.assertIn("nop · 4 trials", executed); self.assertIn("oracle · 3 trials of 4 planned", executed)
        self.assertNotIn("tb-x4-nop", executed)                                                     # the job id is the hover, not the line
        self.assertIn('title="nop · tb-x4-nop · 4 trials"', html)
        self.assertNotIn("Agent under evaluation", executed)                                        # the breadcrumb and the run page say so
        self.assertNotIn("not run", executed)
        self.assertNotIn('id="feasibility-caveat"', html)

    def test_both_control_outcomes_per_task_and_nothing_validated_on_one_side(self):
        html = self.get("/job/tb-x4-agent/feasibility")
        good = self.row(html, "good")
        self.assertIn('data-validated="yes"', good)
        self.assertIn('<td data-side="nop">', good); self.assertIn('<td data-side="oracle">', good)
        self.assertIn("Failed verification", text_of(good)); self.assertIn("Resolved", text_of(good)); self.assertIn("Feasible", text_of(good))
        self.assertIn('href="/trial/tb-x4-nop/good__x"', good); self.assertIn('href="/trial/tb-x4-oracle/good__x"', good)
        leaky = self.row(html, "leaky")
        self.assertIn('data-verdict="unexpected_reward"', leaky); self.assertIn('data-validated="no"', leaky)
        self.assertIn("nop accepted", text_of(leaky))
        env = self.row(html, "env")
        self.assertIn('data-verdict="setup_failure"', env); self.assertIn('data-validated="no"', env)
        self.assertIn("Setup error EnvironmentSetupError", text_of(env)); self.assertIn("Resolved", text_of(env))   # oracle passed and it still does not count
        half = self.row(html, "half")
        self.assertIn('data-verdict="missing"', half); self.assertIn('data-validated="no"', half)
        self.assertIn("not run", text_of(half)); self.assertIn("oracle not run", text_of(half))
        self.assertEqual(html.count('data-validated="yes"'), 1)
        self.assertIn('<span class="feas-headline" id="feasibility-count"><strong>1</strong><span class="faint">/4</span> feasible</span>', html)
        self.assertNotIn('<div class="cards">', html)                                               # the classes are chips, non-zero only

    def test_a_control_job_reads_the_same_report_and_a_stranger_has_no_controls(self):
        html = self.get("/job/tb-x4-nop/feasibility")
        self.assertIn("nop · 4 trials", text_of(html))
        self.assertIn("oracle · 3 trials of 4 planned", text_of(html))
        html = self.get("/job/unrelated/feasibility")
        self.assertIn("nop · not run", text_of(html)); self.assertIn("oracle · not run", text_of(html))
        self.assertIn("Neither control ran: nothing here is feasibility evidence.", text_of(html))
        self.assertIn('data-verdict="missing"', html)
        self.get("/job/nowhere/feasibility", status=404)

    def test_the_job_page_links_the_report_only_when_a_control_exists(self):
        self.assertIn('href="/job/tb-x4-agent/feasibility"', self.get("/job/tb-x4-agent"))
        self.assertIn("Feasibility · nop + oracle", text_of(self.get("/job/tb-x4-agent")))
        self.assertIn('href="/job/tb-x4-nop/feasibility"', self.get("/job/tb-x4-nop"))
        self.assertNotIn("/feasibility", self.get("/job/unrelated"))


class TaskBrokenRow(unittest.TestCase):
    """A task whose nop control failed previously-passing tests is
    classed *Task broken* — never feasible, whatever the oracle did — with
    the failing tests named on the row, a card counting it, and its meaning
    in the Reading-this-table box."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        held_ok, fix = ["tests/test_a.py::test_ok"], "tests/test_fix.py::test_it"
        self.jobs = [
            write_job(self.tmp.name, "swe-x2-nop", "nop", {
                "already-broken": swe_report(p2p=(held_ok, ["tests/test_b.py::test_flaky", "tests/test_c.py::test_gone"]), f2p=([], [fix]), patch=False),
                "sound": swe_report(p2p=(held_ok, []), f2p=([], [fix]), patch=False)}, dataset="../datasets/swebench-verified"),
            write_job(self.tmp.name, "swe-x2-oracle", "oracle", {
                "already-broken": swe_report(resolved=True, p2p=(held_ok, []), f2p=([fix], [])),
                "sound": swe_report(resolved=True, p2p=(held_ok, []), f2p=([fix], []))}, dataset="../datasets/swebench-verified"),
        ]
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=self.jobs))
        self.client = web.app.test_client()

    def test_the_row_is_task_broken_and_names_the_failures(self):
        response = self.client.get("/job/swe-x2-nop/feasibility")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        row = re.search(r'<tr data-task="already-broken"[^>]*>.*?</tr>', html, re.S).group(0)
        self.assertIn('data-verdict="task_broken"', row); self.assertIn('data-validated="no"', row)
        self.assertIn("Task broken", text_of(row))
        self.assertIn("Resolved", text_of(row))                                       # the oracle's answer is shown, and does not rescue the task
        self.assertIn('data-broke="2"', row); self.assertIn("⚠ broke 2", text_of(row))
        self.assertIn("nop broke 2 passing tests: tests/test_b.py::test_flaky tests/test_c.py::test_gone", text_of(row))
        self.assertIn('<li class="mono bad">tests/test_b.py::test_flaky</li>', row)
        self.assertIn('href="/trial/swe-x2-nop/already-broken__x"', row)
        sound = re.search(r'<tr data-task="sound"[^>]*>.*?</tr>', html, re.S).group(0)
        self.assertIn('data-verdict="feasible"', sound); self.assertNotIn("broke", text_of(sound))
        self.assertEqual(html.count('data-validated="yes"'), 1)
        self.assertIn('<span class="feas-headline" id="feasibility-count"><strong>1</strong><span class="faint">/2</span> feasible</span>', html)
        self.assertIn('<span class="chip fv-task_broken">Task broken <b>1</b></span>', html)
        glossary = text_of(re.search(r'<div class="glossary">.*?</dl>', html, re.S).group(0))
        self.assertIn("Task broken The empty solution failed tests that were passing before it.", glossary)
        self.assertIn("Not feasible, whatever the oracle did.", glossary)


class RealOracleSweep(unittest.TestCase):
    """Harbor's real liveboard-oracle-x4 capture: oracle only. The page says
    what that evidence cannot establish and validates nothing."""

    def test_oracle_only_is_read_as_oracle_only(self):
        job = parse.parse_job(str(Path(APP_DIR) / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"))
        with patch.dict(web.app.config, TESTING=True), patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            response = web.app.test_client().get("/job/liveboard-oracle-x4/feasibility")
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            page = web.app.test_client().get("/job/liveboard-oracle-x4")
            self.addCleanup(page.close)
            self.assertIn("Feasibility · oracle", text_of(page.get_data(as_text=True)))
        text = text_of(html)
        self.assertIn("every task of oracle-x4 (no task limit recorded)", text)
        self.assertIn("oracle · 4 trials", text); self.assertIn('title="oracle · liveboard-oracle-x4 · 4 trials"', html)
        self.assertIn("nop · not run", text)
        self.assertIn("Only oracle ran: this cannot establish that the verifier rejects an empty solution. No task is validated.", text)
        self.assertEqual(html.count('data-validated="no"'), 4)
        self.assertNotIn('data-validated="yes"', html)
        self.assertEqual(html.count(">Resolved<"), 4)     # the four oracle cells, one per task


if __name__ == "__main__":
    unittest.main()
