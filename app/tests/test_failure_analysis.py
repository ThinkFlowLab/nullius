"""A run's failure analysis: labels per trial, flags per task, saved beside the job."""

import json
import os
import tempfile
import threading
import unittest

from nullius.runs import failure_analysis as fa
from tests.test_comparison import job, trial


class Labels(unittest.TestCase):
    def test_a_label_is_one_of_the_four(self):
        a = fa.Analysis()
        lab = a.set_label("t__1", "model", by="ann", note="rule in context, not applied", judge_cause="agent_error")
        self.assertEqual((lab.label, lab.by, lab.judge_cause), ("model", "ann", "agent_error"))
        with self.assertRaises(fa.FailureAnalysisError):
            a.set_label("t__2", "luck", by="ann")

    def test_the_judges_cause_suggests_labels(self):
        self.assertEqual(fa.suggestion("agent_error"), ("agent", "model"))
        self.assertEqual(fa.suggestion("rate_limit_or_timeout"), ("environment",))
        self.assertEqual(fa.suggestion("verifier_disagrees"), ("evaluator",))
        self.assertEqual(fa.suggestion("other"), ())
        self.assertEqual(fa.suggestion("not_a_failure"), ())
        self.assertEqual(fa.suggestion(None), ())

    def test_a_label_needs_a_trial_and_can_be_cleared(self):
        a = fa.Analysis()
        with self.assertRaises(fa.FailureAnalysisError):
            a.set_label("", "model", by="ann")
        a.set_label("t__1", "model", by="ann")
        a.clear_label("t__1")
        self.assertEqual(a.labels, {})


class Flags(unittest.TestCase):
    def test_a_persons_flag_counts_at_once_and_an_imported_one_waits(self):
        a = fa.Analysis()
        self.assertEqual(a.set_flag("b/t1", "flawed", "unreachable", by="ann").status, "accepted")
        self.assertEqual(a.set_flag("b/t2", "flawed", "task_broken", by="ann", source="import:feasibility").status,
                         "pending")
        self.assertEqual(a.set_aside(["flawed"]), {"b/t1"})
        flag = a.accept("b/t2", by="bob")
        self.assertEqual((flag.by, flag.accepted_by), ("ann", "bob"))
        self.assertEqual(a.set_aside(["flawed"]), {"b/t1", "b/t2"})

    def test_a_flag_from_elsewhere_never_replaces_an_accepted_one(self):
        a = fa.Analysis()
        a.set_flag("t1", "ambiguous", "two_readings", by="ann")
        self.assertIsNone(a.set_flag("t1", "flawed", "x", by="bot", source="copy:job-2"))
        self.assertEqual((a.flags["t1"].kind, a.flags["t1"].status), ("ambiguous", "accepted"))
        a.remove_flag("t1")
        self.assertEqual(a.set_flag("t1", "flawed", "x", by="bot", source="copy:job-2").status, "pending")

    def test_a_flag_for_the_current_version_replaces_an_accepted_flag_on_another(self):
        a = fa.Analysis()
        a.set_flag("t1", "ambiguous", "two readings", by="ann", task_version="v1")
        self.assertIsNone(a.set_flag("t1", "flawed", "x", by="bot", source="import:f", task_version="v1"))
        got = a.set_flag("t1", "flawed", "x", by="bot", source="import:f", task_version="v2")
        self.assertEqual((got.kind, got.status, got.task_version), ("flawed", "pending", "v2"))

    def test_categories_already_used_come_first_most_used_first(self):
        a, b = fa.Analysis(), fa.Analysis()
        a.set_flag("t1", "flawed", "flaky test", by="ann")
        b.set_flag("t1", "flawed", "flaky test", by="ann")
        b.set_flag("t2", "flawed", "network needed", by="ann")
        b.set_flag("t3", "flawed", "self promoted", by="bot", source="proposed:bot")      # pending: not counted
        self.assertEqual(fa.categories([a, b]), ["flaky test", "network needed"])

    def test_a_flags_source_is_person_import_or_copy(self):
        a = fa.Analysis()
        self.assertEqual(a.set_flag("t9", "flawed", "x", by="bot", source="proposed:bot").status, "pending")
        for bad in ("People", "import:", "copy", "proposed:"):
            with self.assertRaises(fa.FailureAnalysisError):
                a.set_flag("t1", "flawed", "x", by="ann", source=bad)

    def test_each_filter_sets_aside_its_own_kind(self):
        a = fa.Analysis()
        a.set_flag("b/t1", "flawed", "unreachable", by="ann")
        a.set_flag("b/t2", "ambiguous", "two_readings", by="ann")
        self.assertEqual(a.set_aside([]), set())
        self.assertEqual(a.set_aside(["flawed"]), {"b/t1"})
        self.assertEqual(a.set_aside(["ambiguous"]), {"b/t2"})
        self.assertEqual(a.set_aside(["flawed", "ambiguous"]), {"b/t1", "b/t2"})

    def test_a_flag_on_another_version_of_the_task_does_not_count(self):
        a = fa.Analysis()
        a.set_flag("b/t1", "flawed", "unreachable", by="ann", task_version="v1")
        self.assertEqual(a.set_aside(["flawed"], versions={"b/t1": "v1"}), {"b/t1"})
        self.assertEqual(a.set_aside(["flawed"], versions={"b/t1": "v2"}), set())

    def test_a_flag_with_no_version_applies_to_any_version(self):
        a = fa.Analysis()
        a.set_flag("b/t1", "flawed", "unreachable", by="ann")
        self.assertEqual(a.set_aside(["flawed"], versions={"b/t1": "v2"}), {"b/t1"})

    def test_a_flag_needs_a_known_kind_and_a_category(self):
        a = fa.Analysis()
        with self.assertRaises(fa.FailureAnalysisError):
            a.set_flag("b/t1", "broken", "x", by="ann")
        with self.assertRaises(fa.FailureAnalysisError):
            a.set_flag("b/t1", "flawed", "", by="ann")
        with self.assertRaises(fa.FailureAnalysisError):
            a.accept("b/none", by="ann")


class File(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_job_with_no_file_has_an_empty_analysis(self):
        a = fa.load(self.dir)
        self.assertEqual((a.labels, a.flags), ({}, {}))

    def test_save_then_load_gives_the_same_analysis_and_leaves_no_temporary_file(self):
        a = fa.Analysis()
        a.set_label("t__1", "environment", by="ann")
        a.set_flag("b/t1", "ambiguous", "two_readings", by="Zoë", evidence="politique §015", task_version="v1")
        fa.update(self.dir, lambda x: (x.labels.update(a.labels), x.flags.update(a.flags)))
        self.assertEqual(sorted(os.listdir(self.dir)), [fa.FILE, fa.FILE + ".lock"])
        b = fa.load(self.dir)
        self.assertEqual(b.labels, a.labels)
        self.assertEqual(b.flags, a.flags)
        self.assertIn("Zoë", open(fa.path(self.dir), encoding="utf-8").read())

    def test_two_writers_at_once_keep_both_changes(self):
        def label(i):
            fa.update(self.dir, lambda x: x.set_label(f"t__{i}", "model", by="ann"))
        threads = [threading.Thread(target=label, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(fa.load(self.dir).labels), 20)

    def test_a_file_of_another_kind_or_with_a_bad_value_is_refused(self):
        for body in ({"schema": "other"}, {"schema": fa.SCHEMA, "labels": {"t": {"label": "luck"}}},
                     {"schema": fa.SCHEMA, "flags": {"t": {"kind": "flawed", "category": "x", "status": "maybe"}}},
                     {"schema": fa.SCHEMA, "flags": {"t": {"kind": "flawed", "category": "x", "colour": "red"}}},
                     {"schema": fa.SCHEMA, "flags": {"t": {"kind": "flawed", "category": ""}}},
                     {"schema": fa.SCHEMA, "labels": ["not", "an", "object"]},
                     {"schema": fa.SCHEMA, "labels": {"t": "model"}}):
            with open(fa.path(self.dir), "w") as fh:
                json.dump(body, fh)
            with self.assertRaises(fa.FailureAnalysisError):
                fa.load(self.dir)
        with open(fa.path(self.dir), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(fa.FailureAnalysisError):
            fa.load(self.dir)


class Filtered(unittest.TestCase):
    def setUp(self):
        self.run = job([trial("t1"), trial("t1", "failed"), trial("t2", "failed"), trial("t3", "failed"),
                        trial("t4")])
        self.a = fa.Analysis()
        self.a.set_flag("t2", "flawed", "unreachable", by="ann")
        self.a.set_flag("t3", "ambiguous", "two_readings", by="ann")
        self.a.set_flag("t9", "flawed", "unreachable", by="ann")                 # not a task of this job
        self.a.set_flag("t4", "flawed", "x", by="bot", source="import:feasibility")  # pending

    def test_with_no_switch_on_nothing_is_set_aside(self):
        f = fa.filtered(self.run, self.a, [])
        self.assertEqual((f.flagged, f.other_version, f.kinds, f.set_aside, f.filtered_score),
                         ({"flawed": 1, "ambiguous": 1}, 0, (), {}, None))

    def test_each_switch_drops_the_trials_of_its_tasks_only(self):
        f = fa.filtered(self.run, self.a, ["flawed"])
        self.assertEqual((f.set_aside, f.solved, f.ran), ({"flawed": 1}, 2, 4))
        both = fa.filtered(self.run, self.a, ["ambiguous", "flawed"])
        self.assertEqual((both.kinds, both.set_aside), (("flawed", "ambiguous"), {"flawed": 1, "ambiguous": 1}))
        self.assertEqual((both.filtered_score, both.solved, both.ran), (2 / 3, 2, 3))
        self.assertEqual(self.run.adjusted_solve_rate, 2 / 5)       # the official job is unchanged


if __name__ == "__main__":
    unittest.main()
