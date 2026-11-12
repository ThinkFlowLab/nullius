"""The Runs page's first view: runs grouped by the launch that made them.

A launch is one block: its task checks (the nop and oracle that ran with it),
then its agents on one scale, best adjusted rate first, then the runs its record
named that Harbor has not written yet. The flat table is the second view,
`?view=all`. Jobs are Harbor-shaped, written by `tests.test_feasibility.write_job`
and given start times here; nothing is run.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nullius import execution, workspace
from nullius.runs import parse
from tests.test_feasibility import write_job
from .test_controls_apart import text_of
from .test_routes import web

T0 = datetime(2026, 9, 19, 11, 29, tzinfo=timezone.utc)


def blocks(html):
    return re.findall(r'<section class="launch".*?</section>', html, re.S)


class RunsByLaunch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-ui-launch-")
        root = cls.temp.name
        cls.nop = write_job(root, "sweep-0919-nop", "nop", {t: 0 for t in "abcd"}, n_tasks=4)
        cls.oracle = write_job(root, "sweep-0919-oracle", "oracle", {t: 1 for t in "abcd"}, n_tasks=4)
        cls.strong = write_job(root, "sweep-0919-strong", "my-agent", {"a": 1, "b": 1, "c": 1, "d": 0}, n_tasks=4)
        cls.weak = write_job(root, "sweep-0919-weak", "opencode", {"a": 1, "b": 0, "c": 0, "d": "ApiRateLimitError"}, n_tasks=4)
        cls.later = write_job(root, "later-run", "my-agent", {"a": 1}, n_tasks=1)
        for job, minutes in ((cls.nop, 0), (cls.oracle, 11), (cls.weak, 20), (cls.strong, 24), (cls.later, 60 * 24)):
            job.started_at = T0 + timedelta(minutes=minutes)
        cls.ws = workspace.Workspace("active", "Active", [root], "2026-01-01", "2026-01-01", str(Path(root) / "ws"), [root])

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
        self.jobs = [self.later, self.strong, self.nop, self.weak, self.oracle]
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: self.jobs))
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", side_effect=lambda: self.jobs))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, {}, clear=True))
        self.client = web.app.test_client()

    def get(self, path):
        response = self.client.get(path)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_a_merge_replaces_only_its_sources_in_counts_and_launches(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = []
            for name, trials in (("shard-a", {"a": 1}), ("shard-b", {"b": 0}),
                                 ("merged", {"a": 1, "b": 0}), ("unmerged", {"c": 1})):
                j = write_job(root, name, "my-agent", trials, n_tasks=len(trials))
                path = Path(j.dir) / "config.json"
                cfg = json.loads(path.read_text())
                cfg["standard"] = {"launch": "one-launch"}
                if name == "merged":
                    cfg["standard"]["merged_from"] = [{"job": "shard-a"}, {"job": "shard-b"}]
                path.write_text(json.dumps(cfg))
                jobs.append(parse.parse_job(j.dir))
            self.jobs = jobs
            self.assertEqual(jobs[2].merged_from, ["shard-a", "shard-b"])
            # A builder record must not resurrect replaced shards as pending rows.
            plan = execution.Plan(name="source-run", script="", jobs=[{"name": "shard-a"}, {"name": "shard-b"}])
            ex = execution.Execution(plan, root=root)
            execution.EXECUTIONS[ex.id] = ex
            for view in ("launch", "all"):
                html = self.get("/runs?view=" + view)
                self.assertIn('data-runs="2" data-trials="3"', html)
                self.assertNotIn("shard-a", html)
                self.assertNotIn("shard-b", html)
                self.assertIn("merged", html)
                self.assertIn("unmerged", html)
                self.assertEqual(len(blocks(html)), 1)
                self.assertIn('class="dataset-count">2 runs<span class="dot">·</span>3 trials', html)
                self.assertIn("Compare these 2", html)
            # Removing the merge makes both source jobs count again.
            self.jobs = [j for j in jobs if j.job_id != "merged"]
            self.assertIn('data-runs="3" data-trials="3"', self.get("/runs"))

    def test_a_launch_is_one_block_checks_first_then_its_agents_best_first(self):
        html = self.get("/runs")
        self.assertIn('id="view-launch" class="launches">', html)                 # the first view, open
        self.assertIn('id="view-all" hidden', html)
        sweep, later = None, None
        for b in blocks(html):
            if "sweep-0919-*" in b: sweep = b
            if "later-run" in b: later = b
        self.assertIsNotNone(sweep); self.assertIsNotNone(later)
        self.assertLess(html.index("later-run</span>"), html.index("sweep-0919-*"))     # newest launch first
        text = text_of(sweep)
        self.assertIn("nop task check sweep-0919-nop finished ✓ 4/4 rejected ✓", text); self.assertIn("oracle task check sweep-0919-oracle finished ✓ 4/4 passed ✓", text)
        self.assertIn("Every task is feasible", text)
        self.assertLess(sweep.index("sweep-0919-strong"), sweep.index("sweep-0919-weak"))  # 75% above 33%
        self.assertLess(sweep.index("sweep-0919-oracle</small>"), sweep.index("sweep-0919-strong</small>"))   # the checks' rows first, apart
        self.assertEqual(sweep.count('<tr class="la-control'), 2)
        self.assertIn("75%", text); self.assertIn("33%", text)                         # weak: 1 of the 3 that ran; the rate limit is infra
        self.assertIn("?job=sweep-0919-strong&amp;job=sweep-0919-weak", sweep)      # compare the two finished agents
        self.assertIn("Compare these 2", text)
        self.assertIn("No nop or oracle ran with this launch", text_of(later))
        self.assertIn("Compare needs two finished agents", text_of(later))

    def test_a_part_held_back_by_a_pause_or_an_ended_run_reads_not_run(self):
        plan = execution.Plan(name="x", script="", runner="standard", jobs=[{"name": "x-a"}, {"name": "x-b"}],
                              parts=[{"id": "a", "label": "A", "script": "true"}, {"id": "b", "label": "B", "script": "true"}])
        for state, says in (("paused", "continue or stop"), ("stopped", None)):
            with self.subTest(state), tempfile.TemporaryDirectory() as root:
                ex = execution.Execution(plan, root=root, record={"id": "r9", "state": state, "launched": "a", "part": 1, "started": 1.0, "finished": 2.0,
                                                                   "ends": {"a": {"rc": 3, "at": 2.0, "label": "A"}}})
                ex.jobs = self.ws.roots[0]
                with patch.dict(execution.EXECUTIONS, {"r9": ex}, clear=True):
                    html = self.get("/runs")
                block = next(b for b in blocks(html) if 'data-launch="run:r9"' in b)
                self.assertIn('<span class="la-state not-run">', block)
                self.assertNotIn(">queued<", block.replace("</i>", ">"))
                if says:
                    self.assertIn(says, block)

    def test_a_later_paused_shard_has_a_resume_link(self):
        from web.runs import _launch_view
        group = SimpleNamespace(job_ids=[], pending=["first", "second"], source="record", dataset="")
        first = SimpleNamespace(state="done", plan=SimpleNamespace(recipe={}), refused=lambda _: None, never_ran=lambda _: False)
        second = SimpleNamespace(state="paused", plan=SimpleNamespace(recipe={}), live_state=lambda _: "paused")
        with patch.object(execution, "for_job", side_effect=lambda key: {"first": first, "second": second}[key]), \
             patch("web.runs._control_planned", return_value=""):
            self.assertEqual(_launch_view(group, {}, {}, {})["paused"], "second")

    def test_a_run_record_groups_its_jobs_and_names_the_ones_not_written_yet(self):
        plan = SimpleNamespace(name="sweep-0919", recipe={"agents": ["my-agent", "opencode", "claude-code"]}, runner="harbor",
                               jobs=[{"name": "sweep-0919-strong"}, {"name": "sweep-0919-weak"}, {"name": "sweep-0919-claude-code"}])
        ex = SimpleNamespace(id="r1", plan=plan, state="paused", started=1.0, finished=None, job_state=lambda job_id: None, live_state=lambda job_id: "paused",
                             summary=lambda: {"job_ids": [j["name"] for j in plan.jobs], "state": "paused"})
        with patch.dict(execution.EXECUTIONS, {"r1": ex}, clear=True):
            html = self.get("/runs")
        rec = next(b for b in blocks(html) if 'data-launch="run:r1"' in b)
        self.assertIn("sweep-0919-claude-code", rec)                                   # named by the record, not written yet
        self.assertIn('<span class="la-state paused">', rec); self.assertIn("Open to resume", rec)
        self.assertIn(">claude-code</a>", rec)                                          # its name after the launch's prefix
        self.assertIn("Open this launch's recipe", rec)
        inferred = next(b for b in blocks(html) if "sweep-0919-nop" in b)
        self.assertNotIn("sweep-0919-strong", inferred)                                 # the record's jobs are not also inferred

    def test_a_run_refused_before_it_started_reads_refused_with_why(self):
        """Not "not in this workspace": its job was never written anywhere."""
        from nullius import progress
        plan = execution.Plan(name="std-0919", script="", runner="standard", jobs=[{"name": "std-0919"}],
                              parts=[{"id": "reference", "label": "Reference", "script": "true"}])
        with tempfile.TemporaryDirectory() as root:
            ex = execution.Execution(plan, root=root, record={"id": "r3", "state": "failed", "launched": "reference", "error": "Reference exited 2",
                                                               "ends": {"reference": {"rc": 2, "at": 1.0, "label": "Reference"}}})
            ex.jobs = self.ws.roots[0]                                          # its job root is the workspace's: a launch here
            progress.Reporter(f"{ex.jobs}/std-0919").emit("refused", detail="could not install what the run needs: npm is not on this machine")
            self.addCleanup(shutil.rmtree, f"{ex.jobs}/std-0919", True)
            with patch.dict(execution.EXECUTIONS, {"r3": ex}, clear=True):
                html = self.get("/runs")
        block = next(b for b in blocks(html) if 'data-launch="run:r3"' in b)
        self.assertIn('<span class="la-state refused" title="Refused before it started: could not install what the run needs: npm is not on this machine">', block)
        self.assertNotIn("not in this workspace", block)

    def test_a_run_of_several_is_running_one_part_and_the_rest_are_queued(self):
        # The parts run one after another: only the one launched is running. A
        # control the record names is a task check still to come, not an agent.
        agents = ("nop", "oracle", "codex", "copilot-cli")
        plan = execution.Plan(name="exp-1008", script="", jobs=[{"name": f"exp-1008-{a}", "agent": a} for a in agents],
                              parts=[{"id": a, "label": f"{a} run", "script": "true"} for a in agents])
        with tempfile.TemporaryDirectory() as root:
            ex = execution.Execution(plan, root=root, record={"id": "r2", "state": "running", "launched": "nop", "started": 1.0})
            ex.jobs = self.ws.roots[0]
            with patch.dict(execution.EXECUTIONS, {"r2": ex}, clear=True):
                html = self.get("/runs")
        block = next(b for b in blocks(html) if 'data-launch="run:r2"' in b)
        text = text_of(block)
        self.assertIn('data-control="nop" data-state="pending"', block)                  # a task check's row, not an agent's
        self.assertIn("nop task check exp-1008-nop running", text); self.assertIn("oracle task check exp-1008-oracle queued", text)
        self.assertIn("Not checked yet: a control has not finished.", text)
        self.assertNotIn("No nop or oracle ran", text)
        self.assertIn('<span class="la-state queued">', block)                           # codex and copilot-cli wait their turn
        self.assertEqual(block.count('<span class="la-state running">'), 1)                # the nop alone
        self.assertIn("1 running · 3 queued", text)
        with patch.dict(execution.EXECUTIONS, {"r2": ex}, clear=True):
            page = self.get("/job/exp-1008-codex")                                     # its own page, before Harbor writes it
        self.assertIn('id="execution-state">queued</span>', page)
        self.assertIn("· the run: running", page)

    def test_a_run_record_from_another_workspace_is_not_a_launch_here(self):
        def ex_of(key, name, jobs_dir, state="running"):
            plan = SimpleNamespace(name=name, recipe={}, runner="harbor", jobs=[{"name": f"{name}-agent"}, {"name": f"{name}-nop"}])
            return SimpleNamespace(id=key, plan=plan, state=state, started=1.0, finished=None, jobs=jobs_dir, job_state=lambda job_id: None, live_state=lambda job_id: state,
                                   summary=lambda: {"job_ids": [j["name"] for j in plan.jobs], "state": state})
        with tempfile.TemporaryDirectory() as elsewhere:
            records = {"away": ex_of("away", "elsewhere-0930", elsewhere),                 # launched into another workspace
                       "here": ex_of("here", "here-0930", self.ws.roots[0])}               # launched into this one, nothing written yet
            with patch.dict(execution.EXECUTIONS, records, clear=True):
                html = self.get("/runs")
        self.assertNotIn("elsewhere-0930", html)                                         # no block, no preparing line
        self.assertNotIn('data-launch="run:away"', html)
        self.assertIn('data-launch="run:here"', html)
        self.assertIn("here-0930-agent", html)

    def test_a_run_in_another_workspace_does_not_move_this_pulse(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            plan = SimpleNamespace(name="elsewhere-0930", recipe={}, jobs=[{"name": "elsewhere-0930-agent"}])
            away = SimpleNamespace(id="away", plan=plan, state="running", jobs=elsewhere)
            def pulse():
                r = self.client.get("/runs/pulse"); self.addCleanup(r.close)
                return r.get_json()["signature"]
            with patch.dict(execution.EXECUTIONS, {}, clear=True):
                before = pulse()
            with patch.dict(execution.EXECUTIONS, {"away": away}, clear=True):
                during = pulse(); away.state = "done"; after = pulse()
        self.assertEqual(before, during); self.assertEqual(during, after)

    def test_the_flat_table_is_the_second_view(self):
        html = self.get("/runs?view=all")
        self.assertIn('id="view-launch" class="launches" hidden', html)
        self.assertIn('<div id="view-all">', html)
        self.assertIn('aria-pressed="true">All runs</a>', html)
        for job in self.jobs:
            self.assertIn(f'<td data-col="run"><a href="/job/{job.job_id}">', html)   # every run, one row
        self.assertIn("Tick two or more runs to compare", html)


if __name__ == "__main__":
    unittest.main()
