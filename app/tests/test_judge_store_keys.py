"""Two different trials, or two different jobs, never share one judgement
file; a store written under the older names is still read; and a job's
judged trials are its own trials only."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from nullius.judge import questions as Q, store


def rec(choice):
    return {"questions": Q.VERSION, "answers": {"q": {"choice": choice}}, "judge": {"model": "m"}}


class StoreKeys(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nullius-judge-keys-")
        self.addCleanup(shutil.rmtree, self.ws, True)

    def test_two_task_names_keep_two_records(self):
        store.save(self.ws, "task:ds", "task:abc", rec("first"))
        store.save(self.ws, "task:ds", "task_abc", rec("second"))
        self.assertEqual(store.load(self.ws, "task:ds", "task:abc")["answers"]["q"]["choice"], "first")
        self.assertEqual(store.load(self.ws, "task:ds", "task_abc")["answers"]["q"]["choice"], "second")

    def test_a_split_agent_view_and_a_job_of_the_lookalike_name_keep_two_records(self):
        store.save(self.ws, "job~a-b", "t1", rec("view"))      # parse.py names an agent's view <job>~<label>
        store.save(self.ws, "job_a-b", "t1", rec("job"))
        self.assertEqual(store.load(self.ws, "job~a-b", "t1")["answers"]["q"]["choice"], "view")

    def test_a_safe_name_keeps_its_file_name(self):
        store.save(self.ws, "job-a", "task__x1", rec("x"))
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "judgements", "job-a", "task__x1.json")))

    def test_a_record_kept_under_the_older_name_is_read_and_merged_into(self):
        older = os.path.join(self.ws, "judgements", "job_a-b", "t_1.json")         # job~a-b, t:1 as the store spelled them before
        os.makedirs(os.path.dirname(older))
        with open(older, "w", encoding="utf-8") as fh:
            json.dump({Q.VERSION: rec("kept"), Q.EARLIER[0]: {**rec("v1"), "questions": Q.EARLIER[0]}}, fh)
        self.assertEqual(store.load(self.ws, "job~a-b", "t:1")["answers"]["q"]["choice"], "kept")
        self.assertEqual(list(store.for_job(self.ws, "job~a-b", {"t:1": None})), ["t:1"])
        self.assertEqual(store.job_dirs(self.ws, ["job~a-b"]), [os.path.dirname(older)])
        store.save(self.ws, "job~a-b", "t:1", {**rec("later"), "answers": {"r": {"choice": "y"}}})
        got = store.load(self.ws, "job~a-b", "t:1")
        self.assertEqual(sorted(got["answers"]), ["q", "r"])
        self.assertEqual(store.load(self.ws, "job~a-b", "t:1", Q.EARLIER[0])["answers"]["q"]["choice"], "v1")

    def test_a_v1_pair_record_is_not_a_judged_trial_of_job_a(self):
        store.save(self.ws, "job-a", "t1", rec("trial"))
        store.save(self.ws, "job-a", store.pair_key("t1", "t9"), {**rec("A"), "questions": Q.EARLIER[0]})
        self.assertEqual(sorted(store.for_job(self.ws, "job-a", {"t1": None, "t2": None})), ["t1"])
        self.assertEqual(store.load(self.ws, "job-a", store.pair_key("t1", "t9"))["answers"]["q"]["choice"], "A")


if __name__ == "__main__":
    unittest.main()
