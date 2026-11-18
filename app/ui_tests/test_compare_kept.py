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
WHOLE = ["Workspace", "Run", "Agent", "Model", "Dataset", "Runner", "Outcome mix", "Trials", "Mean score", "Category 1", "Category 2",
         "Solved", "Raw rate", "Adjusted rate", "Infra errors", "Broke tests", "Prompt size", "Fresh prompt", "Cached",
         "Cached ratio", "Output tokens", "Cost (estimated)", "Run wall time", "Rerun"]
# matched tasks: no run wall time, which no subset of a run has
MATCHED = [label for label in WHOLE if label != "Run wall time"]
# a task's detail row
DETAIL = ["Outcome", "Score", "Fail → Pass", "Pass → Pass", "Agent time", "Env / Verify", "Steps", "Tool calls", "Failed calls", "Tool mix", "First edit",
          "Edit → test cycles", "Redundant reads", "Retried failed calls", "Exploration per edit", "Prompt size",
          "Fresh prompt", "Cached", "Cached ratio", "Output tokens", "Cost (estimated)", "Files edited", "Broke"]
SECTIONS = ["Side by side", "Task by task"]


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
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda w, roots: jobs[w.name]))
        self.client = web.app.test_client()

    def get(self, path="/compare", **query):
        response = self.client.get(path, query_string={"pick": ["active:run", "foreign:run"], **query})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_every_row_and_section_is_kept(self):
        whole, matched = self.get(cohort="whole"), self.get(cohort="matched")
        self.assertEqual(labels(whole), WHOLE)
        self.assertEqual(labels(matched), MATCHED)
        for body in (whole, matched):
            for section in SECTIONS:
                self.assertIn(f"<h2>{section}</h2>", body)
            # the cohort switch: its two links, their labels and cohort= values
            switch = re.search(r'<nav class="cohort-switch" aria-label="Comparison cohort">(.*?)</nav>', body, re.S).group(1)
            self.assertEqual(re.findall(r'cohort=(\w+)"[^>]*>([^<]+)</a>', switch), [("whole", "Whole runs"), ("matched", "Matched tasks")])
            self.assertIn('class="cohort-note"', body)
            self.assertIn("Open trials side by side", body)
            self.assertIn('data-detail-url="/compare/detail/0?', body)
        self.assertIn('id="judgement"', matched)
        self.assertIn("Better of two", matched)
        self.assertEqual(labels(self.get("/compare/detail/0", cohort="matched")), DETAIL)


if __name__ == "__main__":
    unittest.main()
