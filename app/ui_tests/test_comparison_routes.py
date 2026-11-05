"""The UI keeps cohort arithmetic and repeat semantics intact.

This Flask-dependent suite is separate from the pure data-layer tests. It uses
synthetic parsed records and forbids workspace opens, conversion and writes.
"""

from contextlib import ExitStack
from dataclasses import replace
from html import unescape
import re
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tests.test_comparison import job, trial
from .test_routes import Elements, web
from nullius import workspace


class ComparisonRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.active = workspace.Workspace("active", "Active workspace", [], "", "", "/unused", [])
        self.foreign = workspace.Workspace("foreign", "Foreign workspace", [], "", "", "/unused", [])
        self.left = job([trial("shared", cost_usd=2), trial("left-only", "agent_error", cost_usd=8)], agent="agent:A")
        self.right = job([trial("shared", "unknown", cost_usd=3), trial("right-only", cost_usd=9)], agent="agent:B")
        self.jobs = {"active": [self.left], "foreign": [self.right]}
        self.stack.enter_context(patch.dict(web.shared.STATE, workspace=self.active, roots=[], job_roots=[], label="Active workspace"))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.active, self.foreign]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name:
            {"active": self.active, "foreign": self.foreign}.get(name)))
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: self.jobs[ws.name]))
        for obj, name in ((web.shared, "open_workspace"), (web.shared, "_import_roots"),
                          (workspace, "touch"), (workspace, "save")):
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(f"Unexpected workspace mutation: {name}")))
        self.client = web.app.test_client()

    def get(self, path="/compare", cohort="whole", **query):
        response = self.client.get(path, query_string={"pick": ["active:run", "foreign:run"], "cohort": cohort, **query})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    @staticmethod
    def cells(body, label):
        found = re.search(r'<th class="rowhead"[^>]*>' + re.escape(label) + r'</th>(.*?)</tr>', body, re.S)
        if not found:
            return []
        return re.findall(r'<td[^>]*>(.*?)</td>', found.group(1), re.S)

    def test_no_selection_opens_a_real_two_workspace_picker(self):
        response = self.client.get("/compare")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('name="left"', body)
        self.assertIn('name="right"', body)
        self.assertIn('value="active:run"', body)
        self.assertIn('value="foreign:run"', body)

    def test_matched_metrics_and_wall_clock_change_as_one_cohort(self):
        whole = self.get()
        matched = self.get(cohort="matched")
        self.assertEqual(self.cells(whole, "Cost (estimated)"), ["$10.00", "$12.00"])
        self.assertEqual(self.cells(matched, "Cost (estimated)"), ["$2.00", "$3.00"])
        self.assertEqual(self.cells(matched, "Trials"), ["1", "1"])
        self.assertTrue(self.cells(whole, "Run wall time"))
        self.assertFalse(self.cells(matched, "Run wall time"))
        self.assertNotIn("left-only", matched)
        self.assertNotIn("right-only", matched)

    def test_record_links_keep_duplicate_run_ids_in_their_own_workspace(self):
        body = self.get(cohort="matched")
        urls = [urlsplit(attrs["href"]) for tag, attrs in Elements(body).elements
                if tag == "a" and "href" in attrs]
        run_links = [parse_qs(url.query).get("workspace", []) for url in urls if url.path == "/job/run"]
        self.assertIn(["active"], run_links)
        self.assertIn(["foreign"], run_links)
        task_links = [url for url in urls if url.path == "/compare/task/shared"]
        self.assertTrue(task_links)
        for url in task_links:
            query = parse_qs(url.query)
            self.assertEqual(query["pick"], ["active:run", "foreign:run"])
            self.assertEqual(query["cohort"], ["matched"])
            self.assertEqual(query["dataset"], [self.left.dataset])

    def test_last_indexed_attempt_is_the_same_in_the_table_and_detail(self):
        first = trial("shared", "agent_error", trial_name="attempt-first")
        last = trial("shared", trial_name="attempt-last")
        self.left.trials = [first, last]
        table = self.get(cohort="matched")
        detail = self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset)
        for body in (table, detail):
            self.assertIn("/trial/run/attempt-last?workspace=active", unescape(body))
            self.assertNotIn("/trial/run/attempt-first?workspace=active", unescape(body))
        self.assertEqual(self.cells(table, "Trials"), ["2", "1"])
        self.assertIn("last indexed", detail)

    def test_dataset_mismatch_is_an_empty_matched_cohort_not_a_false_join(self):
        self.right.dataset = "different-dataset@1.0"
        body = self.get(cohort="matched")
        self.assertIn("No shared cohort is available", body)
        self.assertFalse(self.cells(body, "Raw rate"))
        ambiguous = self.client.get("/compare/task/shared", query_string={"pick": ["active:run", "foreign:run"]})
        self.assertEqual(ambiguous.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(ambiguous.location).query)["pick"], ["active:run", "foreign:run"])

    def test_legacy_multiselect_becomes_qualified_and_preserves_order(self):
        other = replace(self.right, job_id="other", trials=[replace(t, job_id="other") for t in self.right.trials])
        self.jobs["active"] = [self.left, other]
        response = self.client.get("/compare", query_string=[("job", "other"), ("job", "run")])
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertLess(body.index('data-job="active:other"'), body.index('data-job="active:run"'))
        self.assertIn('"active:other", "active:run"', body)

    def test_task_quick_metrics_use_recorded_total_and_count_cached_input_once(self):
        self.left.trials = [trial("shared", dur_total=125, n_input_tokens=100,
                                  n_output_tokens=30, n_cache_tokens=80)]
        self.right.trials = [trial("shared", dur_total=None, n_input_tokens=0,
                                   n_output_tokens=0, n_cache_tokens=0)]
        body = self.get(cohort="matched")
        totals = re.findall(r'<span>Total time</span><strong>(.*?)</strong>', body)
        tokens = re.findall(r'<span>Tokens</span><strong>(.*?)</strong>', body)
        self.assertEqual(totals, ["2.1m", "—"])
        self.assertEqual(tokens, ["130", "—"])
        self.assertIn("Token usage not recorded", body)
        self.right.agent_name = "nop"
        control = self.get(cohort="matched")
        self.assertEqual(re.findall(r'<span>Tokens</span><strong>(.*?)</strong>', control), ["130", "0"])

    def test_controls_are_never_ranked_in_matched_metrics(self):
        self.right.agent_name = "nop"
        self.right.trials = [trial("shared", "no_patch", cost_usd=0, n_input_tokens=0, n_output_tokens=0, n_cache_tokens=0)]
        body = self.get(cohort="matched")
        for label in ("Cost (estimated)", "Raw rate", "Adjusted rate", "Prompt size"):
            row = re.search(r'<th class="rowhead"[^>]*>' + re.escape(label) + r'</th>(.*?)</tr>', body, re.S).group(1)
            cells = re.findall(r'<td class="([^"]*)">', row)
            self.assertEqual(cells[1], "", label)
        self.assertEqual(float(self.cells(body, "Cost (estimated)")[1].removeprefix("$")), 0)

    def test_missing_metrics_read_unavailable_not_zero_and_cost_is_labelled_estimated(self):
        """An agent that priced nothing and recorded no usage shows a dash in
        every cost and token cell — summary, task detail and the side-by-side
        page — never $0.00 or 0; and every cost cell sits under a label that
        says the figure is an estimate."""
        self.right.trials = [trial("shared", cost_usd=None, n_input_tokens=0, n_output_tokens=0, n_cache_tokens=0)]
        table = self.get(cohort="matched")
        self.assertEqual(self.cells(table, "Cost (estimated)"), ["$2.00", "—"])
        self.assertEqual(self.cells(table, "Prompt size"), ["100", "—"])
        self.assertEqual(self.cells(table, "Output tokens"), ["10", "—"])
        self.assertNotIn("$0.00", table)
        # The summary row and the task's detail row - asked for when its row
        # opens - both carry the label with its explanation, and the detail's
        # cost cell is the same dash.
        self.assertIn('data-detail-url="/compare/detail/0?', table)
        detail = self.get("/compare/detail/0", cohort="matched")
        for body in (table, detail):
            self.assertEqual(body.count('title="An estimate: priced by the agent from its price table'), 1)
        self.assertEqual(self.cells(detail, "Cost (estimated)"), ["$2.00", "—"])
        # A missing measurement is never a loss: no cell in the row is ranked
        # against the dash except the one figure that exists.
        summary_cost = re.findall(r'<th class="rowhead"[^>]*>Cost \(estimated\)</th>(.*?)</tr>', table, re.S)[0]
        classes = re.findall(r'<td class="([^"]*)">', summary_cost)
        self.assertEqual(classes, ["best", ""])
        side = self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset)
        self.assertIn("Cost (estimated)</dt>", side)
        self.assertEqual(side.count("Cost (estimated)</dt>"), 2)
        costs = re.findall(r'Cost \(estimated\)</dt>\s*<dd[^>]*>(.*?)</dd>', side, re.S)
        self.assertEqual(costs, ["$2.00", "—"])
        self.assertNotIn("$0.00", side)


class ScoredRunsCompared(unittest.TestCase):
    """Two runs of a benchmark that scores and gives no pass or fail: the
    comparison is on the mean score and each task's score, the solve rates
    absent rather than 0%."""

    def test_the_mean_score_and_each_task_s_score_are_what_is_compared(self):
        from web import compare as web_compare
        scored = lambda task, score, parts: trial(task, outcome="scored", reward=None, score=score, n_parts=parts)
        a = job([scored("conv-26", 0.6, 150), scored("conv-30", 0.4, 50)], dataset="locomo10", job_id="gpt-4-turbo")
        b = job([scored("conv-26", 0.6, 150), scored("conv-30", 0.7, 50)], dataset="locomo10", job_id="gpt-3.5")
        rows = {r["label"]: r for r in web_compare.summary_rows([a, b])}
        self.assertEqual(rows["Mean score"]["cells"], ["0.55", "0.62"])
        self.assertIn("best", rows["Mean score"]["classes"][1])
        self.assertEqual((rows["Solved"]["cells"], rows["Raw rate"]["cells"], rows["Adjusted rate"]["cells"]), (["—", "—"], ["—", "—"], ["—", "—"]))
        detail = {r["label"]: r for r in web_compare.compare_detail([a.trials[1], b.trials[1]], [a, b])["rows"]}
        self.assertEqual(detail["Score"]["cells"], ["0.40", "0.70"])
        self.assertIn("best", detail["Score"]["classes"][1])
        self.assertEqual(detail["Outcome"]["cells"], ["Scored", "Scored"])

    def test_a_run_with_no_score_has_no_score_row(self):
        from web import compare as web_compare
        rows = {r["label"] for r in web_compare.summary_rows([job([trial("a")]), job([trial("a", "failed_verification")])])}
        self.assertNotIn("Mean score", rows)

    def test_the_score_by_category_is_compared_category_by_category(self):
        from web import compare as web_compare
        def scored(task, groups):
            n = sum(c for _, c in groups.values())
            return trial(task, outcome="scored", reward=None, score=sum(s for s, _ in groups.values()) / n, n_parts=n,
                         part_groups={g: list(v) for g, v in groups.items()}, parts_by="category")
        a = job([scored("conv-26", {"1": (3.0, 4), "2": (1.0, 4)})], dataset="locomo10", job_id="gpt-4-turbo")
        b = job([scored("conv-26", {"1": (2.0, 4), "2": (3.0, 4)})], dataset="locomo10", job_id="gpt-3.5")
        rows = {r["label"]: r for r in web_compare.summary_rows([a, b])}
        self.assertEqual((rows["Category 1"]["cells"], rows["Category 2"]["cells"]), (["0.75", "0.50"], ["0.25", "0.75"]))
        self.assertIn("best", rows["Category 1"]["classes"][0]); self.assertIn("best", rows["Category 2"]["classes"][1])
        self.assertEqual([g["group"] for g in a.score_by_group], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
