"""A single job's page reads that job's folder alone, and the whole workspace
on any doubt (``shared._job_for_request``), finding what ``_find_job`` over
every job finds."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace

from .test_routes import web

JOB = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"


class JobLookup(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-job-lookup-")))
        self.roots = [str(tmp / "a"), str(tmp / "b")]
        shutil.copytree(JOB, tmp / "a" / "job-x")
        shutil.copytree(JOB, tmp / "b" / "job-y")
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        self.ws = workspace.create(web.shared.WS_HOME, self.roots, "lookup")
        web.shared.open_workspace(self.ws)
        self.whole = patch.object(workspace, "load_jobs", wraps=workspace.load_jobs)

    def lookup(self, job_id, query=""):
        with web.app.test_request_context(f"/job/{job_id}{query}"), self.whole as whole:
            return web.shared._job_for_request(job_id), whole.called

    def test_one_folder_of_that_name_is_read_alone(self):
        job, whole = self.lookup("job-x")
        self.assertFalse(whole)
        self.assertEqual(job.dir, os.path.join(self.roots[0], "job-x"))
        self.assertEqual(web.app.test_client().get("/job/job-x").status_code, 200)

    def test_another_workspace_asked_for_reads_the_whole_of_it(self):
        job, whole = self.lookup("job-x", f"?workspace={self.ws.name}")
        self.assertTrue(whole); self.assertEqual(job.dir, os.path.join(self.roots[0], "job-x"))

    def test_two_folders_of_that_name_read_the_whole_workspace_and_find_the_first(self):
        shutil.copytree(JOB, os.path.join(self.roots[1], "job-x"))
        job, whole = self.lookup("job-x")
        self.assertTrue(whole); self.assertEqual(job.dir, os.path.join(self.roots[0], "job-x"))

    def test_no_such_folder_reads_the_whole_workspace(self):
        job, whole = self.lookup("job-z")
        self.assertTrue(whole); self.assertIsNone(job)


if __name__ == "__main__":
    unittest.main()
