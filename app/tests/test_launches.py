"""Runs grouped by the launch that made them: the run record first, then the
same dataset within half an hour. Every job lands in exactly one launch."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nullius.runs import launches as L

T0 = datetime(2026, 9, 19, 11, 29, tzinfo=timezone.utc)


def job(job_id, minutes=None, dataset="tau3-bench"):
    return SimpleNamespace(job_id=job_id, dataset=dataset, started_at=None if minutes is None else T0 + timedelta(minutes=minutes))


class MergedSources(unittest.TestCase):
    def test_only_finished_merges_replace_their_explicit_sources(self):
        merged = SimpleNamespace(job_id="merged", merged_from=["a", "b", "merged"],
                                 is_running=False, is_preparing=False)
        self.assertEqual(L.merged_sources([merged]), {"a", "b"})
        merged.is_running = True
        self.assertEqual(L.merged_sources([merged]), set())
        merged.is_running = False
        merged.is_preparing = True
        self.assertEqual(L.merged_sources([merged]), set())


class Group(unittest.TestCase):
    def test_runs_of_one_dataset_started_within_half_an_hour_are_one_launch(self):
        jobs = [job("x-agent", 24), job("x-nop", 0), job("x-oracle", 11), job("y-agent", 24 * 60 * 10)]
        out = L.group(jobs)
        self.assertEqual([l.job_ids for l in out], [["y-agent"], ["x-nop", "x-oracle", "x-agent"]])   # newest first, each in start order
        self.assertEqual(out[1].source, "inferred"); self.assertEqual(out[1].started, T0)
        self.assertEqual(out[1].prefix, "x-*")

    def test_the_window_counts_from_the_launchs_first_run_and_a_dataset_splits(self):
        jobs = [job("a", 0), job("b", 29), job("c", 31), job("d", 5, dataset="swe-bench")]
        out = L.group(jobs)
        self.assertEqual(sorted(sorted(l.job_ids) for l in out), [["a", "b"], ["c"], ["d"]])

    def test_the_run_record_wins_and_names_the_runs_not_written_yet(self):
        jobs = [job("tau-latest3-0919-132944-agent", 24), job("tau-latest3-0919-132944-nop", 0), job("other", 5)]
        records = {"r1": ("tau-latest3-0919-132944", ["tau-latest3-0919-132944-nop", "tau-latest3-0919-132944-agent",
                                                       "tau-latest3-0919-132944-claude-code"], None)}
        out = L.group(jobs, records)
        rec = next(l for l in out if l.source == "record")
        self.assertEqual((rec.key, rec.name), ("run:r1", "tau-latest3-0919-132944"))
        self.assertEqual(rec.job_ids, ["tau-latest3-0919-132944-nop", "tau-latest3-0919-132944-agent"])
        self.assertEqual(rec.pending, ["tau-latest3-0919-132944-claude-code"])
        self.assertEqual(rec.prefix, "tau-latest3-0919-132944-*")
        self.assertEqual([l.job_ids for l in out if l.source == "inferred"], [["other"]])   # the record's jobs never join an inferred launch

    def test_a_record_with_only_pending_runs_is_still_a_launch(self):
        out = L.group([], {"r2": ("sweep", ["sweep-a", "sweep-b"], None)})
        self.assertEqual([(l.pending, l.started, l.dataset) for l in out], [(["sweep-a", "sweep-b"], None, None)])

    def test_a_launch_with_no_job_yet_sorts_by_when_it_was_launched(self):
        later = (T0 + timedelta(hours=5)).timestamp()
        out = L.group([job("older", 0), job("newer", 60 * 10)], {"r": ("r", ["r-nop", "r-agent"], later)})
        self.assertEqual([l.key for l in out], ["inferred:newer", "run:r", "inferred:older"])
        self.assertEqual(out[1].started, T0 + timedelta(hours=5))

    def test_a_launchs_written_jobs_outrank_when_it_was_launched(self):
        out = L.group([job("r-nop", 3)], {"r": ("r", ["r-nop", "r-agent"], (T0 + timedelta(hours=5)).timestamp())})
        self.assertEqual(out[0].started, T0 + timedelta(minutes=3))

    def test_a_run_with_no_start_is_its_own_launch_and_sorts_last(self):
        out = L.group([job("started", 0), job("never-a"), job("never-b")])
        self.assertEqual([l.job_ids for l in out][0], ["started"])
        self.assertEqual(sorted(l.job_ids[0] for l in out[1:]), ["never-a", "never-b"])
        self.assertEqual(out[1].prefix, out[1].job_ids[0])                                     # one run: its own name

    def test_runs_that_share_no_prefix_are_named_not_wildcarded(self):
        out = L.group([job("demo-liveboard-tau3-x4", 0), job("tau3-ended", 3)])
        self.assertEqual(out[0].prefix, "demo-liveboard-tau3-x4 and 1 more")

    def test_every_job_lands_in_exactly_one_launch(self):
        jobs = [job(f"j{k}", k * 7, dataset="ds" + str(k % 2)) for k in range(12)]
        records = {"r": ("r", ["j3", "j4", "gone"], None)}
        placed = [i for l in L.group(jobs, records) for i in l.job_ids]
        self.assertEqual(sorted(placed), sorted(j.job_id for j in jobs))


if __name__ == "__main__":
    unittest.main()
