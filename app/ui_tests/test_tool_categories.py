"""Category, not name, at the page: the trial page's tool summary is grouped by category.

Two agents doing the same work under names of their own - OpenCode's short
verbs, another agent's sentences - must show the same category chips with the
same counts; their names appear only as the evidence behind each chip (the
hover and the "Tool names behind the categories" table), never as a chip or
a count of their own. The fixture is test_trajectory's, written here into two
temporary Harbor jobs; no real workspace is opened.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import parse
from tests.test_trajectory import SAME_WORK, two_agent_steps
from .test_routes import Elements, web, write_json


def _job(root: Path, job_id: str, agent: str, steps: list):
    job_dir = root / job_id
    trial_dir = job_dir / f"A={agent}_D=swe-bench-verified_TASK=one_E=docker_M=fixture"
    write_json(job_dir / "result.json", {"stats": {"n_trials": 1}})
    write_json(job_dir / "config.json", {
        "agents": [{"name": agent, "model_name": "fixture-model"}],
        "datasets": [{"name": "swe-bench-verified", "n_tasks": 1}],
    })
    write_json(trial_dir / "result.json", {
        "task_name": "swe-bench-verified/one", "trial_name": trial_dir.name,
        "source": "swe-bench-verified", "verifier_result": {"rewards": {"reward": 1}},
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 50},
        "agent_info": {"name": agent, "model_info": {"name": "fixture-model"}},
    })
    write_json(trial_dir / "agent" / "trajectory.json",
               {"schema_version": "ATIF-v1.7", "session_id": job_id, "steps": steps})
    return parse.parse_job(str(job_dir))


class ToolSummaryByCategory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-tool-categories-")
        root = Path(cls.temp.name)
        steps = two_agent_steps()
        cls.jobs = [_job(root, "short-names", "opencode-like", steps["short"]),
                    _job(root, "verbose-names", "sentence-names", steps["verbose"])]
        cls.ws = workspace.Workspace("active", "Active workspace", [str(root)],
            "2026-01-01", "2026-01-01", str(root / "workspace"), [str(root)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {
            "workspace": self.ws, "roots": list(self.ws.roots),
            "job_roots": list(self.ws.job_roots), "label": self.ws.label}, clear=True))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.ws]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", return_value=self.ws))
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=list(self.jobs)))
        for obj, name in [(web.shared, "open_workspace"), (web.shared, "_import_roots"),
                          (workspace, "save"), (workspace, "touch")]:
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(
                f"Read-only UI route unexpectedly called {name}")))
        self.client = web.app.test_client()

    def trial_page(self, job):
        trial = job.trials[0]
        response = self.client.get(f"/trial/{job.job_id}/{trial.trial_name}")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, job.job_id)
        return response.get_data(as_text=True)

    @staticmethod
    def chips(html):
        """[(category, count)] of the tool summary's chips, in page order."""
        block = re.search(r'<div class="legend tool-summary".*?</div>', html, re.S)
        assert block, "no tool summary on the page"
        return [(a["data-category"], int(a["data-count"]))
                for tag, a in Elements(block.group(0)).elements
                if tag == "span" and "data-category" in a]

    @staticmethod
    def names(html):
        """{(category, name)} of the names table behind the chips."""
        return {(a["data-category"], a["data-name"])
                for tag, a in Elements(html).elements if tag == "tr" and "data-name" in a}

    def test_two_agents_same_work_read_the_same_chips(self):
        short, verbose = (self.trial_page(j) for j in self.jobs)
        self.assertEqual(self.chips(short), self.chips(verbose))
        self.assertEqual(dict(self.chips(short)),
                         {"execute": 2, "read": 2, "search": 1, "edit": 1, "plan": 1})

    def test_names_are_evidence_behind_the_chips_never_a_chip(self):
        short, verbose = (self.trial_page(j) for j in self.jobs)
        short_names, verbose_names = self.names(short), self.names(verbose)
        self.assertEqual({c for c, _ in short_names}, {c for c, _ in verbose_names})
        self.assertNotEqual(short_names, verbose_names)
        self.assertIn(("read", "read"), short_names)
        self.assertIn(("read", "Read the file where the symbol is defined"), verbose_names)
        # The verbose names reach the page only as hover text and table rows -
        # not as a chip category, so the legend has as many chips as categories.
        for html in (short, verbose):
            categories = [c for c, _ in self.chips(html)]
            self.assertEqual(len(categories), len(set(categories)))
            self.assertTrue(set(categories) <= {"search", "read", "edit", "execute", "plan", "other"})
        for _, _, sentence, _ in SAME_WORK:
            self.assertNotIn(f'data-category="{sentence}"', verbose)

    def test_the_hover_lists_the_names_the_chip_folded(self):
        verbose = self.trial_page(self.jobs[1])
        block = re.search(r'<div class="legend tool-summary".*?</div>', verbose, re.S).group(0)
        titles = {a["data-category"]: a["title"] for tag, a in Elements(block).elements
                  if tag == "span" and "data-category" in a}
        self.assertEqual(titles["read"], "read: Read the file where the symbol is defined ×2")
        self.assertIn("Run the test suite to confirm the fix ×1", titles["execute"])
        self.assertIn("Execute the reproduction script once more ×1", titles["execute"])

    def test_a_trial_without_a_trajectory_shows_no_summary(self):
        with tempfile.TemporaryDirectory(prefix="nullius-no-traj-") as tmp:
            bare = _job(Path(tmp), "bare", "none", [])
            shutil.rmtree(Path(tmp) / "bare" / bare.trials[0].trial_name / "agent")
            bare = parse.parse_job(str(Path(tmp) / "bare"))
            with patch.object(workspace, "load_jobs", return_value=[bare]):
                html = self.trial_page(bare)
        self.assertNotIn('class="legend tool-summary"', html)
        self.assertNotIn("Tool names behind the categories", html)


if __name__ == "__main__":
    unittest.main()
