"""`importers/atif.py`: standalone ATIF documents, rendered as a job directory.

Each case writes the documents it reads. What is pinned is what the spec asks for — the supported versions
are recorded, a document normalises the same whichever directory it sits in
and whatever its `agent` block says, a standalone trial reads as `unknown`
with no reward, a broken or foreign-version document is refused by name
with a redacted message, the source is never written, and the Harbor jobs
the same trajectories came from are not claimed (their accounting is the
reader's, exercised by `test_parse` and `test_trajectory`).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

from nullius.importers import atif
from nullius.runs import classify, parse


FAKE_KEY = "sk-test-0123456789abcdefghij"


def minimal(version="ATIF-v1.7", **overrides) -> dict:
    """The smallest document the reader accepts, with a clock and usage."""
    doc = {
        "schema_version": version,
        "session_id": "s-1",
        "agent": {"name": "someagent", "version": "0.1", "model_name": "m"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "do it",
             "timestamp": "2026-08-12T13:55:06Z"},
            {"step_id": 2, "source": "agent", "message": "done",
             "timestamp": "2026-08-12T13:55:16Z", "model_name": "m",
             "metrics": {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0}},
        ],
        "final_metrics": {"total_prompt_tokens": 10, "total_completion_tokens": 2,
                          "total_cached_tokens": 0, "total_cost_usd": None},
    }
    doc.update(overrides)
    return doc


def write_doc(root, name, doc) -> str:
    path = Path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return str(path)


class SupportedVersions(unittest.TestCase):
    def test_the_supported_versions_are_pinned(self):
        # The issue's first supported input format: ATIF-v1.0 through v1.7,
        # the versions trajectory.py already parses inside a Harbor job.
        self.assertEqual(atif.SUPPORTED_VERSIONS,
                         tuple(f"ATIF-v1.{i}" for i in range(8)))


class Detection(unittest.TestCase):


    def test_it_does_not_claim_an_unrelated_directory(self):
        self.assertFalse(atif.detect(tempfile.mkdtemp()))
        self.assertFalse(atif.detect("/nonexistent/tree"))


    def test_well_formed_json_that_is_not_atif_is_passed_over(self):
        src = tempfile.mkdtemp()
        write_doc(src, "trace.json", {"harbor-trace-v1": True, "roots": []})
        write_doc(src, "spans.meta.json", {"n": 1})
        self.assertFalse(atif.detect(src))
        write_doc(src, "t.json", minimal())
        self.assertTrue(atif.detect(src))
        self.assertEqual([os.path.basename(p) for p in atif.find_documents(src)], ["t.json"])

    def test_a_broken_file_that_says_it_is_atif_is_claimed_so_it_can_be_refused(self):
        src = tempfile.mkdtemp()
        write_doc(src, "t.json", '{"schema_version": "ATIF-v1.7", "steps": [')
        self.assertTrue(atif.detect(src))
        # ...whereas a broken file that never claimed to be one is not ours.
        other = tempfile.mkdtemp()
        write_doc(other, "t.json", '{"version": 2, "eval": {')
        self.assertFalse(atif.detect(other))


class Conversion(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)

    def convert(self, root, **kw):
        job_dirs = atif.convert(str(root), self.out, **kw)
        self.assertEqual(len(job_dirs), 1)
        return job_dirs[0], parse.parse_job(job_dirs[0])


    def test_trial_names_follow_the_tree_and_a_bare_trajectory_takes_its_directory(self):
        src = tempfile.mkdtemp()
        write_doc(src, "a.json", minimal())
        write_doc(src, "runs/b/trajectory.json", minimal())
        write_doc(src, "runs/c.json", minimal())
        write_doc(src, "trajectory.json", minimal())
        _, job = self.convert(src)
        self.assertEqual(sorted(t.trial_name for t in job.trials),
                         ["a", "runs__b", "runs__c", "trajectory"])

    def test_re_converting_updates_in_place_and_drops_a_stale_trial(self):
        src = tempfile.mkdtemp()
        write_doc(src, "a.json", minimal())
        write_doc(src, "b.json", minimal())
        job_dir, job = self.convert(src)
        self.assertEqual(len(job.trials), 2)
        os.remove(os.path.join(src, "b.json"))
        again, job = self.convert(src)
        self.assertEqual(again, job_dir)
        self.assertEqual([t.trial_name for t in job.trials], ["a"])

    def test_missing_clock_and_usage_stay_unknown_not_zero(self):
        src = tempfile.mkdtemp()
        doc = minimal()
        for step in doc["steps"]:
            step.pop("timestamp")
            step.pop("metrics", None)
        doc.pop("final_metrics")
        write_doc(src, "bare.json", doc)
        _, job = self.convert(src)
        t = job.trials[0]
        record = json.loads(Path(t.dir, "result.json").read_text())
        self.assertIsNone(record["started_at"])
        self.assertIsNone(record["agent_execution"])
        self.assertEqual(record["agent_result"],
                         {"n_input_tokens": None, "n_output_tokens": None,
                          "n_cache_tokens": None, "cost_usd": None})
        self.assertIsNone(t.dur_total)
        self.assertIsNone(t.cost_usd)
        self.assertEqual(t.outcome, classify.UNKNOWN)

    def test_provenance_falls_back_to_the_steps_model_when_the_agent_block_is_silent(self):
        src = tempfile.mkdtemp()
        write_doc(src, "t.json", minimal(agent=None))
        _, job = self.convert(src)
        t = job.trials[0]
        self.assertEqual((t.agent_info_name, t.agent_version, t.model_name), ("", "", "m"))


class Refusal(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp()
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.src, True)
        self.addCleanup(shutil.rmtree, self.out, True)

    def refused(self, root=None):
        root = root or self.src
        with self.assertRaises(SystemExit) as caught:
            atif.convert(root, self.out)
        with self.assertRaises(SystemExit) as checked:
            atif.check(root)
        self.assertEqual(str(caught.exception), str(checked.exception))
        self.assertEqual(os.listdir(self.out), [], "a refused tree writes nothing")
        return str(caught.exception)

    def test_truncated_json_is_refused_by_name_and_redacted(self):
        good = write_doc(self.src, "good.json", minimal())
        bad = write_doc(self.src, f"{FAKE_KEY}.json",
                        '{"schema_version": "ATIF-v1.7", "api_key": "' + FAKE_KEY + '", "steps": [')
        message = self.refused()
        self.assertIn("not JSON", message)
        self.assertIn(".json", message)
        self.assertNotIn(FAKE_KEY, message)
        self.assertNotIn(FAKE_KEY[8:], message)
        self.assertIn("[REDACTED]", message)
        # nothing was rewritten while refusing
        self.assertTrue(os.path.isfile(good) and os.path.isfile(bad))

    def test_a_document_without_steps_is_refused_as_partial(self):
        doc = minimal()
        doc.pop("steps")
        doc["extra"] = {"token": FAKE_KEY}
        write_doc(self.src, "partial.json", doc)
        message = self.refused()
        self.assertIn("partial.json", message)
        self.assertIn("no steps", message)
        self.assertNotIn(FAKE_KEY, message)

    def test_steps_of_the_wrong_shape_are_refused(self):
        write_doc(self.src, "odd.json", minimal(steps={"not": "a list"}))
        self.assertIn("steps is not a list", self.refused())

    def test_an_unsupported_version_is_refused_by_version(self):
        write_doc(self.src, "future.json", minimal(version="ATIF-v9.0"))
        message = self.refused()
        self.assertIn("future.json", message)
        self.assertIn("ATIF-v9.0", message)
        self.assertIn("ATIF-v1.0", message)
        self.assertIn("ATIF-v1.7", message)

    def test_a_supported_version_is_not_only_the_latest(self):
        write_doc(self.src, "old.json", minimal(version="ATIF-v1.0"))
        job = parse.parse_job(atif.convert(self.src, self.out)[0])
        self.assertEqual(job.trials[0].outcome, classify.UNKNOWN)

    def test_a_directory_with_nothing_of_ours_is_refused(self):
        message = self.refused()
        self.assertIn("no ATIF trajectories", message)
        self.assertIn("ATIF-v1.0", message)

    def test_the_file_is_named_relative_to_the_tree_given(self):
        write_doc(self.src, "deep/er/bad.json", '{"schema_version": "ATIF-v1.7"')
        self.assertTrue(self.refused().startswith("deep/er/bad.json: "))

    def test_a_single_bad_file_is_refused_by_its_own_name(self):
        path = write_doc(self.src, "lone.json", '{"schema_version": "ATIF-v1.7", "steps": ')
        self.assertTrue(self.refused(path).startswith("lone.json: not JSON"))

    def test_a_good_tree_passes_check_without_writing(self):
        write_doc(self.src, "good.json", minimal())
        self.assertIsNone(atif.check(self.src))
        self.assertEqual(os.listdir(self.out), [])


class Trees(unittest.TestCase):
    """What one job is under a root the picker opened: the root, as the CLI
    reads it — a nested directory of trajectories is one job of several
    trials, not a job per child."""


    def test_a_directory_with_nothing_of_ours_is_no_tree(self):
        self.assertEqual(atif.trees(tempfile.mkdtemp()), [])


if __name__ == "__main__":
    unittest.main()
