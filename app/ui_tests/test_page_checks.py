"""Capability sentences, asserted against the rendered pages.

Each test here is one statement a user could confirm by looking: it opens a
workspace on the recorded fixture jobs, requests the page through the app's
own test client, reduces it to the text of its main content, and asserts the
statement's own sentence against that text. Reader properties (folding,
digests, replay) keep their tests beside the reader; these are the checks of
what the pages say about them.

The workspace lives under a temporary workspace home and the fixture roots
are only read; no judge key is in the environment, so the pages show the
unconfigured judge.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import workspace

from .test_routes import web

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
ROOTS = [str(FIXTURES / "harbor-jobs")]

#: name -> (path, a marker the page must still carry). The path is formatted
#: with the fixture trial names and the workspace name a selection needs.
PAGES = {
    "runs-list": ("/runs", "Run Agent Version"),
    "trial-oracle-control": ("/trial/liveboard-oracle-x4/{oracle}", "Reward"),
    "job-oracle-board": ("/job/liveboard-oracle-x4", "4/4 passed"),
    "benchmarks-runner": ("/benchmarks/locomo", "Build a run"),
    "settings-judge": ("/settings", "TYPESAFE_API_KEY"),
    "feasibility-oracle": ("/job/liveboard-oracle-x4/feasibility", "Feasibility"),
}


def text_of(html: str) -> str:
    """The page's main content as text - the navigation is the same on every page."""
    at = html.find("<main")
    html = html[at:] if at >= 0 else html
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class Pages(unittest.TestCase):
    """The pages, read live."""

    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        tmp = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-page-checks-"))
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", os.path.join(tmp, "workspaces")))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "",
                                                              "job_roots": []}, clear=True))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(os.environ))
        os.environ.pop("TYPESAFE_API_KEY", None)
        ws = workspace.create(web.shared.WS_HOME, ROOTS, "page-checks")
        workspace.save(ws)
        web.shared.open_workspace(ws)
        cls.jobs = {j.job_id: j for j in web.shared._load_jobs()}
        cls.names = {"ws": ws.name,
                     "oracle": next(t.trial_name for t in cls.jobs["liveboard-oracle-x4"].trials)}
        cls.client = web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def page(self, name: str) -> str:
        """The text of a named page, whole."""
        path, marker = PAGES[name]
        resp = self.client.get(path.format(**self.names))
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200, path)
        text = text_of(resp.get_data(as_text=True))
        self.assertIn(marker, text, f"{name}: the marker is gone from the page")
        return text

    # --- views.md ---------------------------------------------------------


    def test_a_degenerate_clock_says_unattributable_and_keeps_the_totals(self):
        control = self.page("trial-oracle-control")
        self.assertIn("No per-step clock is available", control)
        self.assertIn("Agent 0.2s", control)                                  # the all-call total stays
        self.assertIn("Trajectory · absent", control)




    def test_a_benchmark_the_standard_runs_builds_a_run_on_its_runner(self):
        """the page half; validation itself is test_catalog"""
        page = self.page("benchmarks-runner")
        self.assertIn("Official", page)
        self.assertNotIn("No usable route", page)
        self.assertIn("Upstream limitations", page); self.assertIn("Nullius limitations", page)
        self.assertNotIn("Next steps", page)                                   # nothing queued

    def test_a_bracketed_task_shows_both_sides_and_one_missing_side_validates_nothing(self):
        page = self.page("feasibility-oracle")
        self.assertIn("Task nop oracle Feasibility Why", page)
        self.assertIn("0 /4 feasible Missing result 4", page)
        self.assertIn("hello-alpine not run Resolved Missing result nop not run", page)
        self.assertNotIn("Feasible 1", page)

    def test_the_harbor_that_ran_is_shown_where_the_job_recorded_it(self):
        self.assertIn("harbor 0.22.0 · editable checkout", self.page("job-oracle-board"))


    def test_the_board_of_a_recorded_run_renders_from_its_own_clock(self):
        """the page half; the replay of every pair is test_live"""
        board = self.page("job-oracle-board")
        self.assertIn("hello-alpine Resolved 0 23s 4.9s 0.2s 6.7s", board)
        self.assertIn("reward-kit-example Resolved 0 24s 2.9s 2.0s 7.9s", board)


if __name__ == "__main__":
    unittest.main()
