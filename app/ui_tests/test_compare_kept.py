"""Every row and section the Compare page shows, pinned by name: a redesign of its columns keeps them all."""

from contextlib import ExitStack
from dataclasses import replace
import re
import unittest
from unittest.mock import patch

from tests.test_comparison import job, trial
from .test_routes import web
from nullius import workspace

# whole runs: every summary row a scored run with categories, a runner and tokens shows
WHOLE = ["Workspace", "Run", "Agent", "Model", "Benchmark", "Dataset", "Runner", "Outcome mix", "Trials", "Mean score", "Category 1", "Category 2",
         "Solved", "Raw rate", "Adjusted rate", "Infra errors", "Broke tests", "Prompt size", "Fresh prompt", "Cached",
         "Cached ratio", "Output tokens", "Cost (estimated)", "Run wall time", "Rerun"]
# matched tasks: no run wall time, which no subset of a run has
MATCHED = [label for label in WHOLE if label != "Run wall time"]
# a task's detail row
DETAIL = ["Outcome", "Score", "Fail → Pass", "Pass → Pass", "Agent time", "Env / Verify", "Steps", "Tool calls", "Failed calls", "Tool mix", "First edit",
          "Edit → test cycles", "Redundant reads", "Retried failed calls", "Exploration per edit", "Prompt size",
          "Fresh prompt", "Cached", "Cached ratio", "Output tokens", "Cost (estimated)", "Files edited", "Broke"]


def labels(body):
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'<th class="rowhead[^"]*"[^>]*>(.*?)</th>', body, re.S)]


class KeptRows(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        ws = {name: workspace.Workspace(name, name.title(), [], "", "", "/unused", []) for name in ("active", "foreign")}

        def scored(task, groups, **kw):
            n = sum(c for _, c in groups.values())
            return trial(task, outcome="scored", reward=None, score=sum(s for s, _ in groups.values()) / n, n_parts=n,
                         part_groups={g: list(v) for g, v in groups.items()}, parts_by="category", p2p_fail=1,
                         p2p_failures=["test_kept"], **kw)
        left = replace(job([scored("conv-1", {"1": (3.0, 4), "2": (1.0, 4)}), scored("conv-2", {"1": (1.0, 4)})],
                           dataset="locomo10"), runner="upstream", harbor_command="harbor run")
        right = replace(job([scored("conv-1", {"1": (2.0, 4), "2": (3.0, 4)}, trial_name="b")], dataset="locomo10"),
                        runner="upstream", agent_name="agent:B")
        jobs = {"active": [left], "foreign": [right]}
        self.stack.enter_context(patch.dict(web.shared.STATE, workspace=ws["active"], roots=[], job_roots=[], label="Active"))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=list(ws.values())))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name: ws.get(name)))
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda w, roots, runs=None: jobs[w.name]))
        self.client = web.app.test_client()

    def get(self, path="/compare", **query):
        response = self.client.get(path, query_string={"pick": ["active:run", "foreign:run"], **query})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_every_row_and_section_is_kept(self):
        """Side by side holds the summary rows, Task by task the task table, its detail rows and its judgement."""
        for cohort, rows in (("whole", WHOLE), ("matched", MATCHED)):
            side, tasks = self.get(cohort=cohort), self.get(cohort=cohort, view="tasks")
            self.assertEqual(labels(side), rows)
            self.assertEqual(labels(tasks), rows[:2])                                   # Workspace, Run
            for body, view, current in ((side, "side", "Side by side"), (tasks, "tasks", "Task by task")):
                # the controls row: the cohort switch, the view switch, Order, then Add a run
                tools = re.search(rf'<div class="table-tools table-controls" id="{view}-tools">(.*?)<div class="table-wrap compare-table-wrap"', body, re.S).group(1)
                switch = re.search(r'<nav class="seg-toggle" aria-label="Comparison cohort">(.*?)</nav>', tools, re.S).group(1)
                views = re.search(r'<nav class="seg-toggle" aria-label="Comparison view">(.*?)</nav>', tools, re.S).group(1)
                self.assertIn('id="compare-add"', tools)
                self.assertIn('<use href="#i-reorder"/></svg>Order</button>', tools)
                self.assertEqual(re.findall(r'<a href="[^"]+">([^<]+)</a>', tools.split("data-order-btn")[1]), ["Group by agent", "Alternate agents"])
                self.assertEqual(re.findall(r'cohort=(\w+)[^"]*"[^>]*>([^<]+)</a>', switch), [("whole", "Whole runs"), ("matched", "Matched tasks")])
                self.assertEqual(re.findall(r'aria-current="true">([^<]+)</a>', views), [current])
                self.assertNotIn('class="cohort-note"', body)
                self.assertNotIn("<h2>Task by task</h2>", body)
                # each column's controls: Baseline, move and remove, its workspace and its run
                self.assertIn('<span class="compare-baseline">Baseline</span>', body)
                self.assertEqual(re.findall(r'aria-label="((?:Move|Remove) column \d[^"]*)"', body), ["Move column 1 right", "Remove column 1", "Move column 2 left", "Remove column 2"])
                self.assertEqual(body.count("data-ws "), 2)
                self.assertEqual(body.count("data-run-btn "), 2)
            self.assertIn('aria-label="Run comparison"', side)
            self.assertNotIn('aria-label="Task comparison"', side)
            self.assertNotIn('aria-label="Run comparison"', tasks)
            self.assertIn("Open trials side by side", tasks)
            self.assertIn('data-detail-url="/compare/detail/0?', tasks)
        tasks = self.get(cohort="matched", view="tasks")
        self.assertIn('id="judgement"', tasks)
        self.assertIn("Better of two", tasks)
        self.assertNotIn('id="judgement"', self.get(cohort="matched"))
        self.assertEqual(labels(self.get("/compare/detail/0", cohort="matched")), DETAIL)

if __name__ == "__main__":
    unittest.main()
