"""Control runs (nop, oracle) stay apart from the agents' results.

A nop rejected on every task is the verifier working, not five failed
verifications; an oracle passing every task is a check, not a score. The runs
list counts the agents' trials only — header, chips, mix bar — and names each
control on its own line with its verdict; a control row's Solved/Raw/Adjusted
cells carry that verdict and sort below every agent row; the job page of a
control says the same in its header. Jobs are Harbor-shaped, written into a
temporary root by `tests.test_feasibility.write_job`; nothing is run.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import feasibility
from tests.test_feasibility import write_job
from .test_routes import web


def text_of(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def checks_of(html):
    """Every launch's task checks - its control rows at the top of its table and the line that sums
    them up - as one fragment."""
    return " ".join(re.findall(r'<tr class="la-control[^"]*".*?</tr>|<div class="launch-checks[^"]*">.*?</div>', html, re.S))


def cell(row, col):
    match = re.search(r'<td data-col="%s"[^>]*>.*?</td>' % col, row, re.S)
    assert match, col
    return match.group(0)


def sort_key(row, col):
    return float(re.search(r'data-sort="([^"]*)"', cell(row, col)).group(1))


class ControlsApart(unittest.TestCase):
    """One agent run (2 resolved, 1 failed verification, 1 rate limited),
    a nop that behaved (5/5 rejected) and an oracle that behaved (3/3 passed);
    then a nop that resolved a task and an oracle the verifier rejected."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-ui-controls-")
        root = cls.temp.name
        cls.agent = write_job(root, "tau-x-agent", "my-agent",
                              {"a": 1, "b": 1, "c": 0, "d": "ApiRateLimitError"}, n_tasks=4)
        cls.nop = write_job(root, "tau-x-nop", "nop", {t: 0 for t in "abcde"}, n_tasks=5)
        cls.oracle = write_job(root, "tau-x-oracle", "oracle", {"a": 1, "b": 1, "c": 1}, n_tasks=5)
        cls.leaky_nop = write_job(root, "tau-y-nop", "nop", {"a": 0, "leaky": 1}, n_tasks=2)
        cls.broken_oracle = write_job(root, "tau-y-oracle", "oracle", {"a": 1, "bad": 0}, n_tasks=2)
        cls.ws = workspace.Workspace("active", "Active", [root], "2026-01-01", "2026-01-01",
                                     str(Path(root) / "ws"), [root])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws, "roots": list(self.ws.roots),
                                                              "job_roots": list(self.ws.job_roots), "label": self.ws.label}, clear=True))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.ws]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name: self.ws))
        self.jobs = [self.agent, self.nop, self.oracle]
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: self.jobs))
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", side_effect=lambda: self.jobs))
        self.client = web.app.test_client()

    def get(self, path):
        response = self.client.get(path)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def row(self, html, job_id):
        rows = [r for r in re.findall(r'<tr [^>]*>.*?</tr>', html, re.S) if '<td data-col="run"><a href="/job/%s">' % job_id in r]
        self.assertEqual(len(rows), 1, job_id)
        return rows[0]

    def test_a_control_is_read_as_behaved_or_not_never_as_a_score(self):
        self.assertIsNone(feasibility.summary_of(self.agent))
        nop = feasibility.summary_of(self.nop)
        self.assertEqual((nop.control, nop.n, nop.n_behaved, nop.state, nop.label), ("nop", 5, 5, "ok", "5/5 rejected ✓"))
        oracle = feasibility.summary_of(self.oracle)
        self.assertEqual((oracle.n_behaved, oracle.state, oracle.label), (3, "ok", "3/3 passed ✓"))
        leaky = feasibility.summary_of(self.leaky_nop)
        self.assertEqual((leaky.n_unexpected, leaky.state, leaky.label), (1, "warn", "1 unexpected reward ⚠"))
        broken = feasibility.summary_of(self.broken_oracle)
        self.assertEqual((broken.n_unexpected, broken.state, broken.label), (1, "warn", "1 reference rejected ⚠"))

    def test_a_control_that_failed_before_the_verifier_is_a_setup_failure(self):
        with tempfile.TemporaryDirectory() as root:
            nop = write_job(root, "z-nop", "nop", {"a": 0, "env": "EnvironmentSetupError"})
            summary = feasibility.summary_of(nop)
            self.assertEqual((summary.n_failed, summary.state, summary.label), (1, "warn", "1 setup failure ⚠"))
            empty = write_job(root, "e-nop", "nop", {})
            self.assertEqual((feasibility.summary_of(empty).state, feasibility.summary_of(empty).label), ("pending", "no trials"))

    def test_the_header_the_chips_and_the_mix_bar_count_the_agent_only(self):
        html = self.get("/runs")
        self.assertIn('data-runs="1" data-trials="4"', html)
        self.assertIn("1 runs", text_of(re.search(r'<p class="subtitle" id="counts".*?</p>', html, re.S).group(0)))
        legend = re.search(r'<div class="legend">.*?</div>', html, re.S).group(0)
        chips = dict(re.findall(r'data-outcome="([a-z_]+)"[^>]*>[^<]*<b>(\d+)</b>', legend))
        self.assertEqual(chips, {"resolved": "2", "failed_verification": "1", "rate_limited": "1"})
        bar = re.search(r'<div class="bar">.*?</div>', html, re.S).group(0)
        self.assertEqual(sorted(re.findall(r'style="flex:(\d+)" data-outcome="([a-z_]+)"', bar)),
                         [("1", "failed_verification"), ("1", "rate_limited"), ("2", "resolved")])

    def test_a_controls_line_names_each_control_with_its_verdict(self):
        # the controls are their launch's task checks: a row each at the top of its table, with its verdict
        html = self.get("/runs")
        line = checks_of(html)
        self.assertIn('href="/job/tau-x-nop"', line)
        self.assertIn('data-control="nop" data-state="ok">', line); self.assertIn('data-control="oracle" data-state="ok">', line)
        self.assertIn("nop task check tau-x-nop finished ✓ 5/5 rejected ✓", text_of(line))
        self.assertIn("oracle task check tau-x-oracle finished ✓ 3/3 passed ✓", text_of(line))
        # no start time recorded: each control is a launch of its own, and says what it checked alone
        self.assertIn("An empty answer fails every task", text_of(line)); self.assertIn("The reference passes every task", text_of(line))
        self.assertNotIn("Failed verification", text_of(line))

    def test_a_nop_that_resolved_and_an_oracle_that_failed_are_flagged_not_scored(self):
        self.jobs = [self.agent, self.leaky_nop, self.broken_oracle]
        html = self.get("/runs")
        line = text_of(checks_of(html))
        self.assertIn("⚠ 1 unexpected reward ⚠", line)
        self.assertIn("⚠ 1 reference rejected ⚠", line)
        self.assertIn("A control did not behave", line)
        self.assertIn('data-state="warn"', self.row(html, "tau-y-nop"))
        self.assertIn("1 unexpected reward ⚠", text_of(cell(self.row(html, "tau-y-nop"), "solved")))
        self.assertIn("1 reference rejected ⚠", text_of(cell(self.row(html, "tau-y-oracle"), "solved")))
        # The agent's one failed verification is still the only one counted.
        chips = dict(re.findall(r'data-outcome="([a-z_]+)"[^>]*>[^<]*<b>(\d+)</b>',
                                re.search(r'<div class="legend">.*?</div>', html, re.S).group(0)))
        self.assertEqual(chips["failed_verification"], "1")
        self.assertEqual(chips["resolved"], "2")

    def test_a_control_row_shows_its_verdict_and_sorts_apart(self):
        html = self.get("/runs")
        agent, nop, oracle = (self.row(html, j) for j in ("tau-x-agent", "tau-x-nop", "tau-x-oracle"))
        self.assertEqual(text_of(cell(nop, "solved")), "5/5 rejected ✓")
        self.assertEqual(text_of(cell(oracle, "solved")), "3/3 passed ✓")
        for row in (nop, oracle):
            self.assertIn('class="control-row"', row)
            for col in ("raw", "adjusted"):
                self.assertEqual(text_of(cell(row, col)), "—")
                self.assertNotIn("%", cell(row, col))
                self.assertIn("Not a score", cell(row, col))
            self.assertIn("ctl-ok", cell(row, "mix"))
            self.assertNotIn("oc-failed_verification", cell(row, "mix"))
            self.assertNotIn("oc-resolved", cell(row, "mix"))
            self.assertEqual(re.search(r'data-outcomes="([^"]*)"', row).group(1).strip(), "")
        # Sort keys: the agent's measured values, the controls below any agent
        # row and below a run nothing scored (-1).
        self.assertEqual(sort_key(agent, "solved"), 2)
        self.assertEqual(sort_key(agent, "raw"), 0.5)
        for row in (nop, oracle):
            for col in ("solved", "raw", "adjusted"):
                self.assertEqual(sort_key(row, col), -2)
            self.assertEqual(sort_key(row, "mix"), -1)
        self.assertEqual(text_of(cell(agent, "solved")), "2")
        self.assertIn("50%", text_of(cell(agent, "raw")))

    # -- the job page -----------------------------------------------------------
    def test_the_job_page_of_a_control_reads_its_header_the_same_way(self):
        html = self.get("/job/tau-x-nop")
        header = re.search(r'<header class="run-header">.*?</header>', html, re.S).group(0)
        self.assertIn('id="control-verdict" data-state="ok"', header)
        self.assertIn("5/5 rejected ✓", text_of(header))
        self.assertIn("Feasibility · nop + oracle", text_of(header))
        cards = text_of(re.search(r'id="run-hero".*?</dl>', html, re.S).group(0))
        self.assertIn("nop · rejected 5 /5", cards)
        self.assertNotIn("Solved", cards)
        self.assertNotIn("0%", cards)
        self.jobs = [self.agent, self.leaky_nop, self.broken_oracle]
        html = self.get("/job/tau-y-oracle")
        self.assertIn('id="control-verdict" data-state="warn"', html)
        self.assertIn("1 reference rejected ⚠", text_of(html))

    def test_an_agent_job_page_is_unchanged(self):
        html = self.get("/job/tau-x-agent")
        self.assertNotIn('id="control-verdict"', html)
        self.assertIn("Solved", text_of(html))
        self.assertIn("50%", text_of(html))


if __name__ == "__main__":
    unittest.main()
