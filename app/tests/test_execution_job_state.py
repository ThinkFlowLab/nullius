"""One standard process can write several jobs, each with its own end."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nullius import execution
from nullius.standard import job


class SharedStep(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = execution.Plan(name="kits", script="conform", runner="standard",
                                   jobs=[{"name": "kit-ok"}, {"name": "kit-next"}])
        self.ex = execution.Execution(self.plan, root=self.temp.name)
        self.ex.state = "running"
        self.ex.launched = "run"
        self.folder = Path(self.ex.jobs) / "kit-ok"
        self.folder.mkdir(parents=True)
        job.write_result(str(self.folder), "kit-ok", 1, 2, [{}, {}], 2)

    def test_a_finished_kit_keeps_its_own_end_while_its_sibling_runs_or_fails(self):
        for state in ("running", "failed", "stopped", "done"):
            with self.subTest(state=state):
                self.ex.state = state
                self.ex.finished = 3 if state != "running" else None
                self.ex.ends = {} if state == "running" else {"run": {"rc": 1, "at": 3}}
                self.assertEqual(self.ex.job_state("kit-ok"), ("done", datetime.fromtimestamp(2, timezone.utc)))
                self.assertEqual(self.ex.job_state("kit-next"), None if state == "running" else
                                 ("failed", datetime.fromtimestamp(3, timezone.utc)))
                self.assertEqual(self.ex.state, state)  # the launch's state is untouched

    def test_a_restored_run_reads_each_job_end_from_disk(self):
        ex = execution.Execution(self.plan, root=self.temp.name,
                                 record={"state": "failed", "finished": 3, "ends": {"run": {"rc": 1, "at": 3}}})
        self.assertEqual(ex.job_state("kit-ok")[0], "done")
        self.assertEqual(ex.job_state("kit-next")[0], "failed")

    def test_incomplete_errored_or_unreadable_results_keep_the_runner_state(self):
        path = self.folder / "result.json"
        original = path.read_text()
        variants = ["", "{", "null", "[]", "{}"]
        for field, value in (("n_completed_trials", 1), ("n_errored_trials", 1),
                             ("n_cancelled_trials", 1), ("n_running_trials", 1), ("n_pending_trials", 1)):
            result = json.loads(original)
            result["stats"][field] = value
            variants.append(json.dumps(result))
        for field, value in (("finished_at", None), ("finished_at", "bad"), ("id", "another")):
            result = json.loads(original)
            result[field] = value
            variants.append(json.dumps(result))
        for contents in variants:
            with self.subTest(contents=contents):
                path.write_text(contents)
                self.ex.state, self.ex.finished = "running", None
                self.assertIsNone(self.ex.job_state("kit-ok"))
                self.ex.state, self.ex.finished = "stopped", 3
                self.assertEqual(self.ex.job_state("kit-ok")[0], "stopped")

    def test_single_job_and_harbor_steps_keep_their_exit_status(self):
        self.ex.state, self.ex.finished = "failed", 3
        self.plan.runner = "harbor"
        self.assertEqual(self.ex.job_state("kit-ok")[0], "failed")
        self.plan.runner = "standard"
        self.plan.jobs = [{"name": "kit-ok"}]
        self.assertEqual(self.ex.job_state("kit-ok")[0], "failed")
