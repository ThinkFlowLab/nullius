"""Judge all reads the judge's calibration once for the batch, not the
whole store again for every trial it judges."""
from __future__ import annotations

import builtins
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from nullius import workspace
from nullius.judge import questions as Q

from .test_routes import web


def rec(*a, **k):
    return {"questions": Q.VERSION, "answers": {"q": {"choice": "x"}}, "judge": {"model": "m"},
            "usage": {"input_tokens": 100}, "states": {"trial": {"chars": 300}}}


class CalibrationReads(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-judge-calib-")))
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        stack.enter_context(patch.dict(os.environ, TYPESAFE_API_KEY="ts-fake"))
        stack.enter_context(patch.object(web.judge.batch, "ask_states", side_effect=rec))
        stack.enter_context(patch.object(web.judge.trajectory, "load", lambda path: NS(error=None)))
        stack.enter_context(patch.object(web.judge, "_trial_ids_and_states", lambda trial, traj: {}))
        self.client = web.app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf"] = "t"

    def reads_to_judge_all(self, n):
        web.shared.open_workspace(workspace.create(web.shared.WS_HOME, [], f"w{n}"))
        job_id = "job"
        trials = [NS(trial_name=f"t{i}", has_trajectory=True, trajectory_path="-", started_at=None, outcome="resolved") for i in range(n)]
        job = NS(job_id=job_id, trials=trials)
        real_open, reads = builtins.open, [0]

        def counting_open(path, mode="r", *a, **k):
            if "judgements" in str(path) and "r" in mode:
                reads[0] += 1
            return real_open(path, mode, *a, **k)

        with patch.object(web.shared, "_job_for_request", lambda j: job), patch("builtins.open", counting_open):
            self.assertEqual(self.client.post(f"/judge/job/{job_id}", data={"csrf": "t"}).status_code, 302)
        self.assertEqual(len(web.judge.store.for_job(web.judge.ws_dir(), job_id, {t.trial_name: None for t in trials})), n)
        return reads[0]

    def test_store_reads_grow_linearly_with_the_trials_judged(self):
        counts = {n: self.reads_to_judge_all(n) for n in (50, 100, 200)}
        self.assertLess(counts[200], 4 * 200, counts)


if __name__ == "__main__":
    unittest.main()
