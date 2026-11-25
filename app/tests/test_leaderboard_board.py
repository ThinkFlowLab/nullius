"""The composite view uses verified coverage and stable participant identities."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from tests.test_leaderboard_scoring import run
from nullius.leaderboard import board, configuration


class BoardExamples(unittest.TestCase):
    def setUp(self):
        self.agents = [{"key": key, "label": key, "_version": "1", "_default": "1", "company": "Maker"}
                       for key in ("A", "B", "C")]
        self.benchmarks = [{"key": "gaia", "label": "GAIA", "_version": "v1", "_default": "v1",
                            "score": {"kind": "pass"}, "datasets": [{"id": "first", "tasks": {str(i): str(i) for i in range(100)}}]}]
        self.config = configuration.defaults(self.benchmarks)
        for axis in self.config["axes"].values():
            axis["weight"] = 0
        self.config["axes"]["Retrieval"] = {"weight": 1, "entries": [
            {"benchmark": "gaia", "dataset": "", "version": "v1", "metric": "Score", "weight": 1}]}

    def make_run(self, name, outcomes, **identity):
        result = run(name, outcomes, agent_name=name)
        result.identity.update(agent=name, agent_version="1", model="m", provider="p", **identity)
        return result

    def build(self, jobs, all_scores=False):
        return board.build(jobs, self.agents, self.benchmarks, self.config, all_scores=all_scores)

    def test_c6_partial_perfection_follows_complete_sixty_percent(self):
        rows = self.build([self.make_run("A", ["resolved"]), self.make_run("B", ["resolved"] * 60 + ["agent_error"] * 40)])["rows"]
        self.assertEqual([(r["name"], r["overall"], r["rank"]) for r in rows], [("B", .6, 1), ("A", 1, None), ("C", None, None)])
        self.assertIn("GAIA 1 of 100 tasks", rows[1]["reasons"])

    def test_c21_verified_all_custom_exemption_and_average(self):
        jobs = [self.make_run("A", ["resolved"] * 50 + ["agent_error"] * 50), self.make_run("B", ["resolved"] * 60 + ["agent_error"] * 40)]
        a = next(r for r in self.build(jobs)["rows"] if r["name"] == "A")
        self.config["custom"] = [{"id": "calendar", "name": "Family calendar test", "scores": {a["key"]: 100}}]
        self.config["axes"]["Personalization"] = {"weight": 1, "entries": [{"custom": "calendar", "weight": 1}]}
        verified = self.build(jobs)
        self.assertEqual([(r["name"], r["overall"]) for r in verified["rows"][:2]], [("B", .6), ("A", .5)])
        all_scores = self.build(jobs, True)
        self.assertEqual([(r["name"], r["overall"], r["rank"]) for r in all_scores["rows"][:2]], [("A", .75, 1), ("B", .6, 2)])
        self.assertEqual(all_scores["averages"]["Personalization"], {"score": 1, "count": 1})
        self.assertTrue(all_scores["rows"][0]["unverified"])
        self.assertEqual(all_scores["rows"][0]["custom_names"], ["Family calendar test"])

    def test_c4_setup_and_version_isolation_sampling_not_setup(self):
        base = self.make_run("A", ["resolved"])
        changed = replace(base, job_id="different", identity=base.identity | {"limits": {"max_turns": 20}})
        other = replace(base, job_id="other", identity=base.identity | {"limits": {"max_turns": 50}})
        version = replace(base, job_id="version", identity=base.identity | {"agent_version": "2"})
        self.assertEqual(len([r for r in self.build([base, changed, other, version])["rows"] if r["has_runs"]]), 4)
        sample = replace(base, job_id="sample", n_tasks=30, identity=base.identity | {"limits": {"repeat": 3}})
        self.assertEqual(len([r for r in self.build([base, sample])["rows"] if r["has_runs"]]), 1)

    def test_c4_benchmark_options_do_not_split_the_participant(self):
        base = self.make_run("A", ["resolved"])
        other = replace(base, job_id="other-benchmark", identity=base.identity | {"benchmark_options": {"temperature": "default"}})
        self.assertEqual(len([r for r in self.build([base, other])["rows"] if r["has_runs"]]), 1)

    def test_c17_efficiency_deduplicates_mappings_and_never_loads_trajectories(self):
        base = self.make_run("A", ["resolved"])
        self.config["axes"]["Money"]["entries"] = self.config["axes"]["Retrieval"]["entries"]
        with patch("nullius.traces.trajectory.load") as load:
            row = next(r for r in self.build([base])["rows"] if r["has_runs"])
            load.assert_not_called()
        self.assertEqual(row["metrics"]["time"], {"value": 40, "count": 1})
        self.assertEqual(row["metrics"]["cost"], {"value": 1, "count": 1})
        self.assertIsNone(row["metrics"]["steps"]["value"])

    def test_c6_positive_domain_weights_gate_and_zero_weights_do_not(self):
        self.config["axes"]["Travel"]["entries"] = [{"benchmark": "gaia", "version": "v1", "dataset": "second", "metric": "Score", "weight": 1}]
        self.config["axes"]["Retrieval"]["entries"][0]["dataset"] = "first"
        self.benchmarks[0]["datasets"].append({"id": "second", "tasks": {"0": "travel-0", "1": "travel-1"}})
        complete = self.make_run("A", ["resolved"] * 100)
        partial = replace(self.make_run("A", ["resolved"]), job_id="travel", dataset="second")
        self.assertEqual(self.build([complete, partial])["rows"][0]["rank"], 1)
        self.config["axes"]["Travel"]["weight"] = 1
        row = self.build([complete, partial])["rows"][0]
        self.assertIsNone(row["rank"])
        self.assertIn("GAIA 1 of 2 tasks", row["reasons"])

    def test_c6_whole_entry_requires_full_union_not_a_full_small_dataset(self):
        self.benchmarks[0]["datasets"].append({"id": "second", "tasks": {"0": "extra"}})
        row = self.build([self.make_run("A", ["resolved"] * 100)])["rows"][0]
        self.assertIsNone(row["rank"])
        self.assertIn("GAIA 100 of 101 tasks", row["reasons"])

    def test_c21_custom_only_rows_rank_without_verified_requirements(self):
        catalogue = self.build([])["rows"]
        self.config["custom"] = [{"id": "calendar", "name": "Family calendar test", "scores": {catalogue[0]["key"]: 72}}]
        self.config["axes"]["Retrieval"]["entries"] = [{"custom": "calendar", "weight": 1}]
        row = self.build([], True)["rows"][0]
        self.assertEqual((row["overall"], row["rank"], row["has_runs"]), (.72, 1, False))
        self.assertEqual(row["metrics"]["cost"], {"value": None, "count": 0})

    def test_c6_attempted_unavailable_entry_still_requires_verified_evidence(self):
        jobs = [self.make_run("A", ["resolved"] * 100)]
        self.benchmarks.append({"key": "tau3-bench", "label": "Tau", "_version": "v1", "score": {"kind": "pass"},
                                "datasets": [{"id": "first", "tasks": {"0": "0"}}]})
        self.config["axes"]["Retrieval"]["entries"].append({"benchmark": "tau3-bench", "version": "v1", "dataset": "", "metric": "pass^3", "weight": 1})
        short = self.make_run("B", ["resolved"], benchmark="tau3-bench")
        rows = self.build(jobs + [short])["rows"]
        a = next(r for r in rows if r["name"] == "A")
        self.assertIsNone(a["rank"])
        self.assertIn("Tau not run", a["reasons"])

    def test_task_name_part_uses_its_distinct_union_and_excludes_other_tasks(self):
        from tests.test_comparison import trial
        bench = self.benchmarks[0]
        bench["task_parts"] = [{"id": "os", "label": "OS tasks", "prefixes": ["os__"]}]
        bench["datasets"] = [{"id": "first", "tasks": {"os__shared": "shared", "office__other": "other"}},
                             {"id": "second", "tasks": {"os__shared": "shared", "os__unique": "unique"}}]
        self.config["axes"]["Retrieval"]["entries"][0]["dataset"] = "os"
        first = self.make_run("A", [])
        first.trials = [trial("os__shared", "resolved"), trial("office__other", "resolved")]
        second = replace(first, job_id="second", dataset="second", trials=[trial("os__shared", "agent_error"), trial("os__unique", "resolved")])
        row = self.build([first, second])["rows"][0]
        entry = row["axes"]["Retrieval"]["entries"][0]
        self.assertEqual((entry["score"], entry["covered"], entry["tasks"]), (.5, 2, 2))
        self.assertEqual(row["rank"], 1)

    def test_c13_missing_version_evidence_never_renormalizes_a_difference(self):
        self.benchmarks.append({"key": "other", "label": "Other", "_version": "v1", "score": {"kind": "pass"},
                                "datasets": [{"id": "first", "tasks": {"0": "0", "1": "1"}}]})
        other = {"benchmark": "other", "version": "v1", "dataset": "", "metric": "Score", "weight": 1}
        self.config["axes"]["Retrieval"]["entries"].append(other)
        self.config["axes"]["Execution"] = {"weight": 1, "entries": [other]}
        jobs = [self.make_run("A", ["resolved"]),
                replace(self.make_run("A", ["resolved", "agent_error"], benchmark="other"), job_id="other-run")]
        original = self.build(jobs)
        self.assertEqual(original["rows"][0]["axes"]["Retrieval"]["score"], .75)
        projected = board.project(jobs, self.agents, self.benchmarks, self.config, "gaia", "unrun")
        row = next(r for r in projected["rows"] if r["has_runs"])
        self.assertIsNone(row["axes"]["Retrieval"]["score"])
        self.assertIsNone(row["overall"])
        self.assertEqual(row["axes"]["Execution"]["score"], .5)
        self.assertEqual(self.build(jobs), original)


if __name__ == "__main__":
    unittest.main()
