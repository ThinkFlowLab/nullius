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
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda ws, roots, runs=None: self.jobs[ws.name]))
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
    def row(body, label):
        found = re.search(r'<th class="rowhead[^"]*"[^>]*>' + re.escape(label) + r'</th>(.*?)</tr>', body, re.S)
        return found.group(1) if found else ""

    @classmethod
    def cells(cls, body, label):
        """A row's cells, each without its difference from the baseline."""
        return [re.sub(r'<span class="delta[^"]*">.*?</span>', "", c)
                for c in re.findall(r'<td[^>]*>(.*?)</td>', cls.row(body, label), re.S)]

    @classmethod
    def ranks(cls, body, label):
        """A row's cells' marks: 'best', 'tie' or ''."""
        return [next((m for m in ("best", "tie") if m in c.split()), "")
                for c in re.findall(r'<td(?: class="([^"]*)")?>', cls.row(body, label))]

    @classmethod
    def deltas(cls, body, label):
        return [(d.group(2), d.group(1)) if d else None for d in
                (re.search(r'<span class="delta ?([^"]*)">(.*?)</span>', c) for c in re.findall(r'<td[^>]*>(.*?)</td>', cls.row(body, label), re.S))]

    def test_no_selection_is_two_empty_columns_of_the_open_workspace(self):
        response = self.client.get("/compare")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertEqual(re.findall(r'<th class="compare-col( empty)?" data-column="(\d)" data-run="([^"]*)"', body),
                         [(" empty", "0", ""), (" empty", "1", "")])
        self.assertIn('<option value="active" selected>', body)
        self.assertIn('<option value="foreign">', body)
        self.assertNotIn(">Agent</th>", body)                             # no numbers for a column with no run
        self.assertNotIn('aria-label="Remove column', body)               # a column removed would come straight back
        self.assertNotIn("<h2>Task by task</h2>", body)
        self.assertIn('values: ["active:", "active:"]', body)
        self.assertIn('"value": "active:run"', body)                       # its run list, embedded


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
        urls = [urlsplit(attrs["href"]) for body in (self.get(cohort="matched"), self.get(cohort="matched", view="tasks"))
                for tag, attrs in Elements(body).elements if tag == "a" and "href" in attrs]
        run_links = [parse_qs(url.query).get("workspace", []) for url in urls if url.path == "/job/run"]
        self.assertIn(["active"], run_links)
        self.assertIn(["foreign"], run_links)
        task_links = [url for url in urls if url.path == "/compare/task/shared"]
        self.assertTrue(task_links)
        for url in task_links:
            query = parse_qs(url.query)
            self.assertEqual(query["pick"], ["active:run", "foreign:run"])
            self.assertEqual(query["cohort"], ["matched"])
            self.assertEqual(query["view"], ["tasks"])
            self.assertEqual(query["dataset"], [self.left.dataset])

    def test_last_indexed_attempt_is_the_same_in_the_table_and_detail(self):
        first = trial("shared", "agent_error", trial_name="attempt-first")
        last = trial("shared", trial_name="attempt-last")
        self.left.trials = [first, last]
        table = self.get(cohort="matched")
        detail = self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset)
        for body in (self.get(cohort="matched", view="tasks"), detail):
            self.assertIn("/trial/run/attempt-last?workspace=active", unescape(body))
            self.assertNotIn("/trial/run/attempt-first?workspace=active", unescape(body))
        self.assertEqual(self.cells(table, "Trials"), ["2", "1"])
        tabs = re.findall(r'<a class="attempt-tab[^"]*" href="([^"]*)"[^>]*?( aria-current="page")?>', detail)
        self.assertEqual([bool(current) for _, current in tabs], [False, True])
        self.assertIn("<b>1/2</b> passed", detail)

    def test_a_compared_task_counts_its_infra_attempts_apart(self):
        limited = trial("shared", "rate_limited", trial_name="limited", exception_type="ApiRateLimitError")
        self.left.trials = [trial("shared", trial_name="one"), trial("shared", trial_name="two"), limited]
        expected = '<b>2/2</b> passed<span class="faint"> · 1 infra</span>'
        self.assertIn(expected, self.get(cohort="matched", view="tasks"))
        self.assertIn(expected, self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset))

    def test_a_scored_task_side_by_side_shows_its_mean_not_a_pass_count(self):
        self.left.trials = [trial("shared", "scored", trial_name=f"s-{i}", reward=None, score=v) for i, v in enumerate((.4, .8))]
        detail = self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset)
        self.assertIn("mean <b>0.60</b> over 2", detail)
        self.assertNotIn("0/2", detail)

    def test_each_side_of_a_compared_task_chooses_its_attempt(self):
        first = trial("shared", "agent_error", trial_name="attempt-first")
        last = trial("shared", trial_name="attempt-last")
        self.left.trials = [first, last]
        detail = unescape(self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset,
                                   attempt="active:run/attempt-first"))
        self.assertIn("/trial/run/attempt-first?workspace=active", detail)
        self.assertNotIn("/trial/run/attempt-last?workspace=active", detail)
        # the other attempt's tab keeps the right side as it is and names the left one
        href = re.search(r'<a class="attempt-tab oc-resolved" href="([^"]*)"', detail).group(1)
        self.assertEqual(parse_qs(urlsplit(href).query)["attempt"], ["active:run/attempt-last"])
        # a name that is not an attempt at this task falls back to the last indexed
        fallback = unescape(self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset,
                                     attempt="active:run/right-only"))
        self.assertIn("/trial/run/attempt-last?workspace=active", fallback)

    def test_dataset_mismatch_is_an_empty_matched_cohort_not_a_false_join(self):
        self.right.dataset = "different-dataset@1.0"
        body = self.get(cohort="matched")
        self.assertIn('<p class="alert warn">No shared cohort is available.', body)
        self.assertFalse(self.cells(body, "Raw rate"))
        ambiguous = self.client.get("/compare/task/shared", query_string={"pick": ["active:run", "foreign:run"]})
        self.assertEqual(ambiguous.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(ambiguous.location).query)["pick"], ["active:run", "foreign:run"])

    def test_legacy_multiselect_becomes_qualified_and_preserves_order(self):
        other = replace(self.right, job_id="other", trials=[replace(t, job_id="other") for t in self.right.trials])
        self.jobs["active"] = [self.left, other]
        response = self.client.get("/compare", query_string=[("job", "other"), ("job", "run"), ("view", "tasks")])
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertLess(body.index('data-job="active:other"'), body.index('data-job="active:run"'))
        self.assertIn('"active:other", "active:run"', body)

    def test_task_quick_metrics_use_recorded_total_and_count_cached_input_once(self):
        self.left.trials = [trial("shared", dur_total=125, n_input_tokens=100,
                                  n_output_tokens=30, n_cache_tokens=80)]
        self.right.trials = [trial("shared", dur_total=None, n_input_tokens=0,
                                   n_output_tokens=0, n_cache_tokens=0)]
        body = self.get(cohort="matched", view="tasks")
        totals = re.findall(r'<span>Total time</span><strong>(.*?)</strong>', body)
        tokens = re.findall(r'<span>Tokens</span><strong>(.*?)</strong>', body)
        self.assertEqual(totals, ["2.08m", "—"])
        self.assertEqual(tokens, ["130", "—"])
        self.assertIn("Token usage not recorded", body)
        self.right.agent_name = "nop"
        control = self.get(cohort="matched", view="tasks")
        self.assertEqual(re.findall(r'<span>Tokens</span><strong>(.*?)</strong>', control), ["130", "0"])

    def test_controls_are_never_ranked_in_matched_metrics(self):
        self.right.agent_name = "nop"
        self.right.trials = [trial("shared", "no_patch", cost_usd=0, n_input_tokens=0, n_output_tokens=0, n_cache_tokens=0)]
        body = self.get(cohort="matched")
        for label in ("Cost (estimated)", "Raw rate", "Adjusted rate", "Prompt size"):
            self.assertEqual(self.ranks(body, label)[1], "", label)
            self.assertNotIn(" ok", self.row(body, label)); self.assertNotIn(" bad", self.row(body, label))   # nor coloured
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
        self.assertIn('data-detail-url="/compare/detail/0?', self.get(cohort="matched", view="tasks"))
        detail = self.get("/compare/detail/0", cohort="matched")
        for body in (table, detail):
            self.assertEqual(body.count('title="An estimate: priced by the agent from its price table'), 1)
        self.assertEqual(self.cells(detail, "Cost (estimated)"), ["$2.00", "—"])
        # A missing measurement is never a loss: no cell in the row is ranked
        # against the dash except the one figure that exists.
        self.assertEqual(self.ranks(table, "Cost (estimated)"), ["best", ""])
        self.assertEqual(self.deltas(table, "Cost (estimated)"), [None, None])   # nothing against a missing value

        side = self.get("/compare/task/shared", cohort="matched", dataset=self.left.dataset)
        self.assertIn("Cost (estimated)</dt>", side)
        self.assertEqual(side.count("Cost (estimated)</dt>"), 2)
        costs = re.findall(r'Cost \(estimated\)</dt>\s*<dd[^>]*>(.*?)</dd>', side, re.S)
        self.assertEqual(costs, ["$2.00", "—"])
        self.assertNotIn("$0.00", side)

    # ---- columns: N of them, in the URL's order, the first the baseline ----

    def another(self, job_id, workspace="foreign", **kw):
        """A copy of the right run under ``job_id``, added to ``workspace``."""
        kw.setdefault("trials", [replace(t, job_id=job_id) for t in self.right.trials])
        j = replace(self.right, job_id=job_id, **kw)
        self.jobs[workspace].append(j)
        return j

    @staticmethod
    def columns(body):
        return re.findall(r'<th class="compare-col( empty)?" data-column="\d+" data-run="([^"]*)"', body)

    def links(self, body, label):
        a = re.search(r'<a [^>]*href="([^"]+)"[^>]*aria-label="' + re.escape(label) + '"', body)
        return parse_qs(urlsplit(unescape(a.group(1))).query) if a else None

    def test_one_run_opens_beside_an_empty_column_with_nothing_ranked(self):
        body = self.get(pick="active:run")
        self.assertEqual(self.columns(body), [("", "run"), (" empty", "")])
        self.assertIn('<span class="compare-baseline">Baseline</span>', body)
        self.assertEqual(self.cells(body, "Cost (estimated)"), ["$10.00", ""])
        self.assertEqual(self.ranks(body, "Cost (estimated)"), ["", ""])
        self.assertEqual(self.deltas(body, "Cost (estimated)"), [None, None])
        self.assertEqual(self.links(body, "Remove column 1"), {"pick": ["active:"], "cohort": ["whole"]})
        self.assertIsNone(self.links(body, "Remove column 2"))
        # the same run twice is one column, and an empty one beside it
        self.assertEqual(self.columns(self.get(pick=["active:run", "active:run"])), [("", "run"), (" empty", "")])

    def test_four_columns_one_empty_follow_the_url_in_every_table(self):
        self.another("third")
        picks = ["foreign:third", "active:run", "foreign:", "foreign:run"]
        body = self.get(pick=picks, cohort="matched")
        self.assertEqual(self.columns(body), [("", "third"), ("", "run"), (" empty", ""), ("", "run")])
        self.assertEqual(self.cells(body, "Cost (estimated)"), ["$3.00", "$2.00", "", "$3.00"])
        self.assertIn('style="--cols: 4"', body)
        # the task table: a cell a column, the empty one empty, in the same order
        body = self.get(pick=picks, cohort="matched", view="tasks")
        task = re.search(r'<tr class="compare-task-row[^"]*">(.*?)</tr>', body, re.S).group(1)
        cells = re.findall(r'<td( class="[^"]*")?>', task)[1:]
        self.assertEqual([c for c in cells], ["", "", ' class="compare-empty"', ""])
        heads = re.findall(r'<th class="draggable-col[^"]*" draggable="true" data-job="([^"]*)"', body)
        self.assertEqual(heads, picks)
        detail = self.get("/compare/detail/0", pick=picks, cohort="matched")
        self.assertEqual(self.cells(detail, "Cost (estimated)"), ["$3.00", "$2.00", "", "$3.00"])
        # the per-task page shows every run picked
        side = self.get("/compare/task/shared", pick=picks, cohort="matched", dataset=self.left.dataset)
        self.assertEqual(side.count('class="cmp-head draggable-col"'), 3)
        self.assertEqual(re.findall(r'Cost \(estimated\)</dt>\s*<dd[^>]*>(.*?)</dd>', side, re.S), ["$3.00", "$2.00", "$3.00"])

    def test_workspace_and_run_are_rows_of_the_table_a_cell_a_column(self):
        body = self.get(pick=["active:run", "foreign:"])
        workspaces, runs = self.row(body, "Workspace"), self.row(body, "Run")
        self.assertEqual(re.findall(r'<option value="(\w+)" selected>', workspaces), ["active", "foreign"])
        self.assertEqual(runs.count("data-run-btn"), 2)
        self.assertLess(body.index(">Run</th>"), body.index(">Agent</th>"))

    def test_a_benchmark_row_above_the_dataset_names_each_run_s_benchmark(self):
        self.right.dataset = "not-in-the-catalogue@1.0"
        body = self.get()
        self.assertEqual(self.cells(body, "Benchmark"), ["SWE-bench Verified", "—"])
        self.assertLess(body.index(">Benchmark</th>"), body.index(">Dataset</th>"))

    def test_remove_reorder_and_add_are_links_that_keep_the_cohort(self):
        self.another("third")
        picks = ["active:run", "foreign:run", "foreign:third"]
        body = self.get(pick=picks, cohort="matched")
        self.assertIsNone(self.links(body, "Move column 1 left"))
        self.assertIsNone(self.links(body, "Move column 3 right"))
        self.assertEqual(self.links(body, "Move column 1 right"), {"pick": ["foreign:run", "active:run", "foreign:third"], "cohort": ["matched"]})
        self.assertEqual(self.links(body, "Move column 3 left"), {"pick": ["active:run", "foreign:third", "foreign:run"], "cohort": ["matched"]})
        self.assertEqual(self.links(body, "Remove column 2"), {"pick": ["active:run", "foreign:third"], "cohort": ["matched"]})
        add = re.search(r'href="([^"]+)" id="compare-add"', body).group(1)
        self.assertEqual(parse_qs(urlsplit(unescape(add)).query), {"pick": picks + ["foreign:"], "cohort": ["matched"]})
        # a reorder is its URL: the columns and their numbers follow it
        swapped = self.get(pick=["foreign:run", "active:run"])
        self.assertEqual(self.cells(swapped, "Cost (estimated)"), ["$12.00", "$10.00"])
        self.assertEqual(self.columns(swapped), [("", "run"), ("", "run")])

    def test_the_controls_row_holds_the_cohort_the_order_and_add_a_run(self):
        self.another("third")
        body = self.get(pick=["active:run", "foreign:run", "foreign:third"], cohort="matched")
        row = body[body.index('<div class="table-tools table-controls" id="side-tools">'):body.index('<div class="table-wrap compare-table-wrap"')]
        self.assertEqual(re.findall(r'data-slot="(\w+)"|aria-label="([^"]+)"|href="#(i-\w+)"|>([A-Z][\w ]+)</(?:a|button)>', row),
                         [("views", "", "", ""), ("", "Comparison cohort", "", ""), ("", "", "", "Whole runs"), ("", "", "", "Matched tasks"),
                          ("", "Comparison view", "", ""), ("", "", "", "Side by side"), ("", "", "", "Task by task"), ("", "", "", "Chart"),
                          ("", "", "i-reorder", ""), ("", "", "", "Order"), ("", "", "", "Group by agent"), ("", "", "", "Alternate agents"),
                          ("actions", "", "", ""), ("", "", "i-plus", ""), ("", "", "", "Add a run")])
        # Order opens a menu of the two one-off reorders, links that keep the cohort
        menu = re.search(r'data-order-btn.*?<div class="menu tools-menu" hidden>(.*?)</div>', row, re.S).group(1)
        self.assertEqual([parse_qs(urlsplit(unescape(h)).query)["cohort"] for h in re.findall(r'<a href="([^"]+)">', menu)], [["matched"], ["matched"]])
        self.assertIn('id="compare-add"', row)
        self.assertNotIn("data-search", row)
        self.assertNotIn("Sort by", row)
        self.assertLess(body.index('id="side-tools"'), body.index('aria-label="Run comparison"'))
        # one agent: nothing to order
        self.assertNotIn("data-order-btn", self.get(pick=["active:run"]))

    def test_the_side_table_pins_its_head_and_identity_rows_the_task_table_its_head(self):
        body = self.get(pick=["active:run", "foreign:run"])
        side = body[body.index('<table class="grid compare" id="side"'):]
        pinned = re.findall(r'<tr( data-sticky(?:="wide")?)?>\s*<th[^>]*>(?:<span class="sr">)?([^<]+)<', side)
        self.assertEqual(pinned[:7], [(" data-sticky", "Metric"), (' data-sticky="wide"', "Workspace"), (' data-sticky="wide"', "Run"),
                                      (" data-sticky", "Agent"), (' data-sticky="wide"', "Model"), (' data-sticky="wide"', "Benchmark"),
                                      ("", "Dataset")])
        self.assertEqual([p for p, _ in pinned[7:]], [""] * len(pinned[7:]))
        self.assertIn('id="side-tools"', body[:body.index('<table class="grid compare" id="side"')])   # the controls row whose height the box leaves
        # the task table: the same column controls pinned the same way, then its own header row, nothing else
        tasks = self.get(pick=["active:run", "foreign:run"], view="tasks")
        table = tasks[tasks.index('<table class="grid compare tasks" id="tasks"'):]
        pinned = re.findall(r'<tr( class="[^"]*")?( data-sticky(?:="wide")?)?>\s*<th[^>]*>(?:<span class="sr">)?([^<]+)<', table)
        self.assertEqual(pinned[:4], [("", " data-sticky", "Task"), ("", ' data-sticky="wide"', "Workspace"), ("", ' data-sticky="wide"', "Run"),
                                      (' class="compare-task-head"', " data-sticky", "Task")])
        self.assertEqual(table.count("data-sticky"), 4)
        self.assertIn('id="tasks-tools"', tasks[:tasks.index('<table class="grid compare tasks"')])
        self.assertNotIn('data-sticky', tasks[:tasks.index('<table class="grid compare tasks"')])

    def test_each_column_s_difference_from_the_baseline_in_the_row_s_unit(self):
        self.another("cheap", trials=[trial("shared", cost_usd=1, n_input_tokens=1500, job_id="cheap"),
                                      trial("other", cost_usd=1, n_input_tokens=1500, job_id="cheap")])
        self.another("check", agent_name="nop", trials=[trial("shared", "no_patch", cost_usd=0, job_id="check")])
        body = self.get(pick=["active:run", "foreign:run", "foreign:cheap", "foreign:check"])
        # cost: lower is better; the check is never coloured
        self.assertEqual(self.deltas(body, "Cost (estimated)"), [None, ("+$2.00", "bad"), ("−$8.00", "ok"), ("−$10.00", "")])
        # rates in percentage points, higher is better
        self.assertEqual(self.deltas(body, "Raw rate"), [None, ("±0", ""), ("+50 pp", "ok"), ("−50 pp", "")])
        self.assertEqual(self.deltas(body, "Trials"), [None, ("±0", ""), ("±0", ""), ("−1", "")])
        self.assertEqual(self.deltas(body, "Prompt size"), [None, ("±0", ""), ("+2.8k", "bad"), ("−100", "")])
        # the best is still marked, the check never
        self.assertEqual(self.ranks(body, "Cost (estimated)"), ["", "", "best", ""])
        # no baseline run, no difference
        empty_first = self.get(pick=["foreign:", "active:run", "foreign:run"])
        self.assertEqual(self.deltas(empty_first, "Cost (estimated)"), [None, None, None])

    def test_the_datasets_differ_in_one_warning_line(self):
        self.assertNotIn('class="compare-warn"', self.get())
        self.right.dataset = "different-dataset@1.0"
        body = self.get()
        self.assertEqual(len(re.findall(r'<p class="compare-warn">[^<\n]+</p>', body)), 1)

    def test_a_workspace_s_runs_as_json_newest_launch_first_the_baseline_s_dataset_first(self):
        from datetime import datetime
        for k, (dataset, day) in enumerate((("d1", 1), ("d2", 3), ("d1", 5))):
            self.another(f"r{k}", "active", dataset=dataset, started_at=datetime(2026, 9, day))
        got = self.client.get("/compare/runs", query_string={"workspace": "active"}).get_json()
        self.assertEqual([r["id"] for r in got["runs"]], ["r2", "r1", "r0", "run"])
        self.assertEqual(got["same"], 0)
        got = self.client.get("/compare/runs", query_string={"workspace": "active", "dataset": "d1"}).get_json()
        self.assertEqual(([r["id"] for r in got["runs"]], got["same"]), (["r2", "r0", "r1", "run"], 2))
        first = got["runs"][0]
        self.assertEqual({k: first[k] for k in ("value", "agent", "model", "where", "date", "check", "shards")},
                         {"value": "active:r2", "agent": "B", "model": "model", "where": "d1", "date": "Sep 05  00:00",
                          "check": False, "shards": 1})
        self.assertNotEqual(first["launch"], got["runs"][1]["launch"])
        self.assertEqual(self.client.get("/compare/runs", query_string={"workspace": "gone"}).status_code, 404)
        # the page embeds the same lists, ordered by its baseline's dataset
        body = self.get(pick=["active:r0", "foreign:run"])

        self.assertIn('"dataset": "d1"', body)
        lists = re.search(r'lists: (\{.*?\}), dataset:', body, re.S).group(1)
        self.assertLess(lists.index('"active:r2"'), lists.index('"active:r1"'))

    def test_the_old_picker_opens_its_picks_as_columns(self):
        response = self.client.get("/compare/pick", query_string={"pick": ["active:run", "foreign:run"], "cohort": "matched"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.location).path, "/compare")
        self.assertEqual(parse_qs(urlsplit(response.location).query), {"pick": ["active:run", "foreign:run"], "cohort": ["matched"]})

    def test_the_view_is_side_by_side_or_task_by_task_and_every_link_keeps_it(self):
        self.another("third", agent_name="agent:C")
        picks = ["active:run", "foreign:run", "foreign:third"]
        query = lambda h: parse_qs(urlsplit(unescape(h)).query)
        side, tasks = self.get(pick=picks, cohort="matched"), self.get(pick=picks, cohort="matched", view="tasks")
        self.assertIn('id="side" style', side); self.assertNotIn('id="tasks"', side)
        self.assertIn('id="tasks" style', tasks); self.assertNotIn('id="side" style', tasks)
        self.assertIn('"value": "active:run"', tasks)                                     # its run lists, embedded as in the side view
        for body, view in ((side, None), (tasks, ["tasks"])):
            row = body[body.index('<div class="table-tools table-controls"'):body.index('<div class="table-wrap compare-table-wrap"')]
            switch = re.search(r'aria-label="Comparison view">(.*?)</nav>', row, re.S).group(1)
            # the view switch keeps the cohort and the picks
            self.assertEqual([query(h) for h in re.findall(r'href="([^"]+)"', switch)],
                             [{"pick": picks, "cohort": ["matched"]}, {"pick": picks, "cohort": ["matched"], "view": ["tasks"]},
                              {"pick": picks, "cohort": ["matched"], "view": ["chart"]}])
            # the cohort switch, Order and Add a run keep the view
            links = [query(h) for h in re.findall(r'href="([^"]+)"', row.replace(switch, "")) if not h.startswith("#")]
            self.assertEqual(len(links), 5)
            self.assertEqual([q.get("view") for q in links], [view] * 5)
        # the column links keep the cohort, and the view
        self.assertEqual(self.links(side, "Remove column 2"), {"pick": ["active:run", "foreign:third"], "cohort": ["matched"]})
        self.assertEqual(self.links(tasks, "Remove column 2"), {"pick": ["active:run", "foreign:third"], "cohort": ["matched"], "view": ["tasks"]})
        self.assertEqual(self.links(tasks, "Move column 1 right"), {"pick": ["foreign:run", "active:run", "foreign:third"], "cohort": ["matched"], "view": ["tasks"]})
        # the task table's links keep the view, and the task page's links back
        task_links = [query(h) for h in re.findall(r'href="(/compare/task/[^"]+)"', tasks)]
        self.assertTrue(task_links)
        self.assertEqual({tuple(q["view"]) for q in task_links}, {("tasks",)})
        page = self.get("/compare/task/shared", pick=picks, cohort="matched", view="tasks", dataset=self.left.dataset)
        back = [query(h) for h in re.findall(r'href="(/compare\?[^"]+)"', page)]
        self.assertEqual(len(back), 2)
        self.assertEqual([q["view"] for q in back], [["tasks"]] * 2)
        self.assertIn("url.searchParams.set('view', \"tasks\")", page)
        self.assertNotIn("searchParams.set('view'", self.get("/compare/task/shared", pick=picks, cohort="matched", dataset=self.left.dataset))
        # anything but view=tasks or chart is side by side
        self.assertIn('id="side" style', self.get(pick=picks, view="other"))
        # the old picker keeps it
        response = self.client.get("/compare/pick", query_string={"pick": picks, "view": "tasks"})
        self.addCleanup(response.close)
        self.assertEqual(query(response.location)["view"], ["tasks"])

    # ---- the chart: the side table's summary rows, drawn, a card a chart ----

    @staticmethod
    def chart_of(body):
        return body[body.index('<div class="compare-chart">'):body.index("<script>", body.index('<div class="compare-chart">'))]

    @classmethod
    def cards(cls, body):
        """Each chart card: its style, its buttons (their accessible names), its menus as {link text: chart=}, its SVG
        title and its values' rows as (name, readings...)."""
        query = lambda h: parse_qs(urlsplit(unescape(h)).query)["chart"]
        out = []
        for card in re.findall(r'<section class="fig"[^>]*>(.*?)</section>', cls.chart_of(body), re.S):
            menus = [{unescape(t): query(h) for h, t in re.findall(r'<a href="([^"]+)"(?: aria-current="true")?>([^<]+)</a>', m)}
                     for m in re.findall(r'<div class="menu tools-menu" hidden>(.*?)</div>', card, re.S)]
            current = [unescape(t) for m in re.findall(r'<div class="menu tools-menu" hidden>(.*?)</div>', card, re.S)
                       for t in re.findall(r'aria-current="true">([^<]+)</a>', m)]
            style = current[0]
            chosen = current[1:]
            title = re.search(r"<title id=[^>]*>(.*?)</title>", card)
            rows = [tuple(unescape(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r)) for r in re.findall(r"<tr>(.*?)</tr>", card.split("<tbody>")[1], re.S)] if title else []
            out.append({"style": style, "metrics": chosen, "menus": menus,
                        "buttons": [unescape(b) for b in re.findall(r'data-chart-btn aria-haspopup="true" aria-expanded="false" aria-label="([^"]+)"', card)],
                        "title": unescape(title.group(1)) if title else "", "rows": rows, "html": card})
        return out

    def test_the_default_charts_are_the_headline_rate_the_cost_and_the_one_against_the_other(self):
        body = self.get(view="chart")
        self.assertNotIn('id="side" style', body); self.assertNotIn('id="tasks"', body)
        cards = self.cards(body)
        self.assertEqual([(c["style"], c["metrics"]) for c in cards],
                         [("Bars", ["Adjusted rate"]), ("Bars", ["Cost (estimated)"]), ("Scatter", ["Cost (estimated)", "Adjusted rate"])])
        self.assertEqual([c["title"] for c in cards], ["Adjusted rate", "Cost (estimated)", "Adjusted rate by Cost (estimated)"])
        # the values are the side table's own cells, the baseline named, its difference beside the other
        self.assertEqual(self.cells(self.get(), "Cost (estimated)"), ["$10.00", "$12.00"])
        self.assertEqual(cards[1]["rows"], [("A · model (baseline)", "$10.00"), ("B · model", "$12.00 +$2.00")])
        self.assertEqual(self.chart_of(body).count('role="img"'), 3)
        self.assertEqual(re.findall(r'<rect class="(bar[^"]*)"', cards[0]["html"]), ["bar s1", "bar s2"])
        # a card's buttons: Style, then Data, or X and Y, each named for the metric it shows
        self.assertEqual([c["buttons"] for c in cards], [["Style of chart 1", "Data of chart 1: Adjusted rate"], ["Style of chart 2", "Data of chart 2: Cost (estimated)"],
                                                         ["Style of chart 3", "X of chart 3: Cost (estimated)", "Y of chart 3: Adjusted rate"]])
        self.assertEqual(re.findall(r'<span class="ic ic-letter" aria-hidden="true">(\w)</span>([^<]+)<svg class="ic dd-chev"', cards[2]["html"]),
                         [("X", "Cost (estimated)"), ("Y", "Adjusted rate")])
        self.assertIn('<use href="#i-sliders"/></svg>Adjusted rate<svg class="ic dd-chev"', cards[0]["html"])
        # the controls row: Add chart beside Add a run, and no chart-wide Data or Style
        row = body[body.index('id="chart-tools"'):body.index('<div class="compare-chart">')]
        self.assertEqual(re.findall(r'id="(chart-add|compare-add)"', row), ["chart-add", "compare-add"])
        self.assertNotIn("Data</button>", row); self.assertNotIn("Style</button>", row)

    def test_each_card_s_style_metrics_and_remove_change_its_own_chart_and_keep_the_rest(self):
        self.another("third", agent_name="agent:C")
        picks = ["active:run", "foreign:run", "foreign:third"]
        query = lambda h: parse_qs(urlsplit(unescape(h)).query)
        charts = ["bars:solved", "scatter:prompt_size,cost"]
        body = self.get(pick=picks, view="chart", chart=charts)
        bars, scatter = self.cards(body)
        self.assertEqual((bars["title"], scatter["title"]), ("Solved", "Cost (estimated) by Prompt size"))
        self.assertEqual(scatter["html"].count("<circle"), 3)
        # a style change keeps the first metric, a scatter's Y the headline rate; the other card stays
        self.assertEqual(bars["menus"][0], {"Bars": charts, "Dots": ["dots:solved", charts[1]], "Scatter": ["scatter:solved,adjusted_rate", charts[1]]})
        self.assertEqual(scatter["menus"][0]["Dots"], [charts[0], "dots:prompt_size"])
        # each axis's menu: a link a summary row, the chosen one marked, changing that axis of that card alone
        self.assertEqual(bars["metrics"], ["Solved"])
        self.assertEqual(bars["menus"][1]["Prompt size"], ["bars:prompt_size", charts[1]])
        self.assertEqual(len(bars["menus"][1]), len(scatter["menus"][1]))
        self.assertEqual(scatter["menus"][1]["Trials"], [charts[0], "scatter:trials,cost"])
        self.assertEqual(scatter["menus"][2]["Solved"], [charts[0], "scatter:prompt_size,solved"])
        self.assertEqual(re.findall(r"<legend>(.*?)</legend>", scatter["html"])[:3], ["Outcomes", "Tokens", "Cost and time"])
        remove = re.search(r'href="([^"]+)" aria-label="Remove this chart"', scatter["html"]).group(1)
        self.assertEqual(parse_qs(urlsplit(unescape(remove)).query), {"pick": picks, "cohort": ["whole"], "view": ["chart"], "chart": [charts[0]]})
        self.assertIn('<use href="#i-x"/>', scatter["html"])
        # Add chart appends a bar chart of the headline rate
        add = re.search(r'href="([^"]+)" id="chart-add"', body).group(1)
        self.assertEqual(query(add)["chart"], charts + ["bars:adjusted_rate"])
        # every link in the cards keeps the picks, the cohort and the view
        self.assertEqual({(tuple(q["pick"]), tuple(q["cohort"]), tuple(q["view"])) for q in
                          (query(h) for h in re.findall(r'href="(/compare\?[^"]+)"', self.chart_of(body)))}, {(tuple(picks), ("whole",), ("chart",))})
        # every other link keeps the charts: the cohort, the views, Order, Add a run, the columns
        links = [query(h) for h in re.findall(r'href="(/compare\?[^"]+)"(?! id="chart-add")', body.split('<div class="compare-chart">')[0])]
        self.assertGreater(len(links), 8)
        self.assertEqual({tuple(q["chart"]) for q in links}, {tuple(charts)})
        self.assertEqual(self.links(body, "Remove column 2")["view"], ["chart"])

    def test_no_chart_is_one_add_chart_prompt_and_a_metric_not_in_the_cohort_says_so(self):
        none = self.get(view="chart", chart="")
        self.assertEqual(self.cards(none), [])
        prompt = self.chart_of(none)
        self.assertEqual(prompt.count("Add chart</a>"), 1)
        self.assertEqual(parse_qs(urlsplit(unescape(re.search(r'href="([^"]+)"', prompt).group(1))).query)["chart"], ["bars:adjusted_rate"])
        # wall time is no interval of matched tasks: the card keeps its controls and says why it draws nothing
        card, = self.cards(self.get(view="chart", cohort="matched", chart="bars:wall_time"))
        self.assertIn("Not in this comparison.", card["html"])
        self.assertEqual(card["metrics"], [])
        self.assertEqual(card["buttons"][1], "Data of chart 1: —")
        # an unknown style is no card
        self.assertEqual(self.cards(self.get(view="chart", chart=["pie:cost", "dots:cost"]))[0]["style"], "Dots")

    def test_each_run_keeps_its_column_s_colour_in_every_chart_from_tokens_of_every_theme(self):
        for k in range(9):
            self.another(f"run-{k}", agent_name=f"agent:{k}")
        picks = ["active:run", "foreign:run"] + [f"foreign:run-{k}" for k in range(9)]
        cards = self.cards(self.get(pick=picks, view="chart", chart=["bars:cost", "dots:cost", "scatter:cost,trials"]))
        for card in cards:
            mark = r'<(?:rect|circle) class="(?:bar|dot)( s\d)?'
            self.assertEqual([m.strip() for m in re.findall(mark, card["html"])], [f"s{(n - 1) % 8 + 1}" for n in range(1, 12)])
        response = self.client.get("/static/style.css")
        self.addCleanup(response.close)
        css = response.get_data(as_text=True)
        for block in (css[css.index(":root {"):], css[css.index(':root[data-theme="light"] {'):]):
            block = block[:block.index("}")]
            self.assertEqual(sorted(set(re.findall(r"--series-(\d):", block))), [str(n) for n in range(1, 9)])
        self.assertEqual(re.findall(r"\.fig-svg \.s(\d) \{ --c: var\(--series-\1\); \}", css), [str(n) for n in range(1, 9)])

    def test_a_check_is_drawn_apart_and_never_ranked(self):
        self.right.agent_name = "nop"
        self.right.trials = [trial("shared", "no_patch", cost_usd=None, n_input_tokens=0, n_output_tokens=0, n_cache_tokens=0)]
        cost, infra = (c["html"] for c in self.cards(self.get(view="chart", chart=["bars:cost", "bars:infra_errors"])))
        self.assertIn("(check)", cost)
        # the check's cost is the table's real $0.00, drawn hollow; its dash is a gap
        self.assertEqual(self.cells(self.get(), "Cost (estimated)")[1], "$0.00")
        self.assertEqual(re.findall(r'<rect class="(bar[^"]*)"', cost), ["bar s1", "bar s2 check"])
        self.assertEqual(re.findall(r'<rect class="(bar[^"]*)"', infra)[1], "bar s2 check")   # its 0 infra errors, a real count
        self.assertNotIn("best", self.chart_of(self.get(view="chart")))

    def test_one_run_and_an_empty_column_chart_the_one_run(self):
        body = self.get(pick=["active:run", "foreign:"], view="chart")
        cards = self.cards(body)
        self.assertEqual([len(c["rows"]) for c in cards], [1, 1, 1])
        self.assertNotIn('class="ref"', body)                                    # nothing to set against the baseline
        empty = self.get(pick=["active:"], view="chart")
        self.assertNotIn('<div class="compare-chart">', empty)
        self.assertIn('id="chart-tools"', empty)

    def test_the_tasks_view_has_the_column_controls_and_adds_a_run_in_place(self):
        """Each column's header (Baseline, move, remove), workspace and run, as in the side view; Add a run keeps
        the view, and its empty column is picked right there."""
        picks = ["active:run", "foreign:run"]
        tasks = self.get(pick=picks, view="tasks")
        self.assertEqual(self.columns(tasks), [("", "run"), ("", "run")])
        self.assertIn('<span class="compare-baseline">Baseline</span>', tasks)
        self.assertEqual(re.findall(r'<option value="(\w+)" selected>', self.row(tasks, "Workspace")), ["active", "foreign"])
        self.assertEqual(self.row(tasks, "Run").count("data-run-btn"), 2)
        add = unescape(re.search(r'href="([^"]+)" id="compare-add"', tasks).group(1))
        self.assertEqual(parse_qs(urlsplit(add).query), {"pick": picks + ["foreign:"], "cohort": ["whole"], "view": ["tasks"]})
        added = self.client.get(add)
        self.addCleanup(added.close)
        body = added.get_data(as_text=True)
        self.assertIn('id="tasks" style="--cols: 3"', body)
        self.assertEqual(self.columns(body), [("", "run"), ("", "run"), (" empty", "")])
        self.assertEqual(self.row(body, "Run").count("data-run-btn"), 3)
        self.assertEqual(self.links(body, "Remove column 3"), {"pick": picks, "cohort": ["whole"], "view": ["tasks"]})
        # the picker's lists are the side view's, with every pick to grey or check
        self.assertIn('values: ["active:run", "foreign:run", "foreign:"]', body)
        self.assertIn('"value": "foreign:run"', body)
        # a judged column after the runs' gets an empty cell in each control row
        with patch.object(web.compare, "_judgement", lambda view: {"rows": {}, "pairs": 0, "judged": 0, "earlier": 0, "unjudged": 0,
                                                                     "keyed": False, "question": "q", "margin": 0.5}):
            judged = self.get(pick=picks, view="tasks")
        for label in ("Workspace", "Run"):
            self.assertEqual(len(re.findall(r"<td[ >]", self.row(judged, label))), 3)

    def test_the_warnings_show_in_both_views(self):
        self.right.dataset = "different-dataset@1.0"
        for view in (None, "tasks"):
            body = self.get(cohort="matched", **({"view": view} if view else {}))
            self.assertIn('<p class="alert warn">No shared cohort is available.', body)
            self.assertEqual(len(re.findall(r'<p class="compare-warn">[^<\n]+</p>', body)), 1)

    def test_the_judge_form_and_its_redirect_keep_the_view(self):
        import os
        judge = web.judge
        with patch.dict(os.environ, TYPESAFE_API_KEY="ts-fake"), \
             patch.object(judge, "judge_side", lambda *a, **k: {}), \
             patch.object(judge, "pairs_of", lambda view: [(row.task, *row.trials) for row in view.tasks]), \
             patch.object(judge, "_sides_todo", lambda view: []):
            tasks = self.get(cohort="matched", view="tasks")
            action = unescape(re.search(r'<form method="post" action="([^"]+)" class="header-form" id="judge-all">', tasks).group(1))
            self.assertEqual(parse_qs(urlsplit(action).query)["view"], ["tasks"])
            with self.client.session_transaction() as sess:
                sess["csrf"] = "route-token"
            response = self.client.post(action, data={"csrf": "route-token"})
            self.addCleanup(response.close)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response.location).query)["view"], ["tasks"])
        self.assertTrue(response.location.endswith("#judgement"))


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
