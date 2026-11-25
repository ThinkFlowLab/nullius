"""Recorded identities and deferred metrics through the shared board service."""
import json
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.test_comparison import job, trial
from nullius.runs import parse
from nullius.standard.contract import definitions
from nullius.standard.runner import job as recorded_job
from nullius.leaderboard import board, configuration


class RecordedIdentity(unittest.TestCase):
    def test_new_standard_runs_record_the_resolved_source_pin(self):
        for key in ("gaia", "osworld1"):
            bench = definitions.get("benchmark", key)
            expected = bench.get("_version") or bench["upstream"]["tested"]
            recorded = recorded_job._version(bench)
            self.assertEqual(recorded["benchmark_version"], expected)
            identity = parse.identity_of({"standard": {"benchmark": key, **recorded}}, {}, "")
            self.assertEqual(identity["benchmark_version"], expected)

    def test_no_roots_all_scores_can_rank_a_saved_manual_catalogue_score(self):
        from nullius.leaderboard import service
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, NULLIUS_HOME=folder):
            agents, benches = service.catalogue()
            doc = configuration.defaults(benches)
            key = board.build([], agents, benches, doc)["rows"][0]["key"]
            doc["custom"] = [{"id": "calendar", "name": "Calendar", "scores": {key: 72}}]
            doc["axes"]["Personalization"]["entries"] = [{"custom": "calendar", "weight": 1}]
            configuration.save(configuration.location(), doc, benches)
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            for flags, expected in (([], None), (["--all-scores"], .72)):
                result = subprocess.run([sys.executable, "-m", "nullius", "leaderboard", "--json", *flags], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                row = next(r for r in json.loads(result.stdout)["rows"] if r["key"] == key)
                self.assertEqual(row["overall"], expected)
                self.assertEqual(row["rank"], 1 if flags else None)
                self.assertFalse(row["has_runs"])
                self.assertTrue(all(m["value"] is None for m in row["metrics"].values()))

    def test_real_cli_reads_gaia_and_banking_result_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "jobs"
            for name, benchmark, dataset, outcomes in (("gaia-run", "gaia", "gaia-validation", ("pass", "crash", "infra")),
                                                        ("bank-run", "tau3-bench", "banking_knowledge", ("pass",))):
                path = root / name
                path.mkdir(parents=True)
                (path / "config.json").write_text(json.dumps({"standard": {"benchmark": benchmark, "plugged": "codex", **recorded_job._version(definitions.get("benchmark", benchmark))},
                    "agents": [{"name": "codex", "model_name": "model"}], "datasets": [{"name": f"{benchmark}/{dataset}"}]}))
                (path / "result.json").write_text(json.dumps({"started_at": "2026-11-01T10:00:00Z", "finished_at": "2026-11-01T11:00:00Z"}))
                for i, outcome in enumerate(outcomes):
                    trial_dir = path / str(i)
                    trial_dir.mkdir()
                    value = {"task_name": str(i), "trial_name": str(i), "source": benchmark,
                             "agent_info": {"name": "codex", "version": "2.2.2"}}
                    if outcome == "pass":
                        value["verifier_result"] = {"rewards": {"reward": 1}}
                        (trial_dir / "verifier").mkdir()
                        (trial_dir / "verifier" / "reward.txt").write_text("1")
                    else:
                        value["exception_info"] = {"exception_type": "AgentTimeoutError" if outcome == "crash" else "ApiRateLimitError", "exception_message": "fixture"}
                    (trial_dir / "result.json").write_text(json.dumps(value))
            env = dict(os.environ, NULLIUS_HOME=str(Path(folder)/"profile"), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            result = subprocess.run([sys.executable, "-m", "nullius", "leaderboard", str(root), "--json"], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            row = next(r for r in document["rows"] if r["has_runs"])
            self.assertEqual(row["axes"]["Retrieval"]["score"], .75)
            self.assertEqual(row["axes"]["Money"]["score"], 1)
            self.assertEqual(row["company"], "OpenAI")

    def test_missing_version_is_unavailable_and_explicit_pin_matches(self):
        agents = definitions.load("agent")[0]
        benches = definitions.load("benchmark")[0]
        cfg = {"standard": {"benchmark": "gaia", "plugged": "codex"},
               "agents": [{"name": "codex", "model_name": "model"}]}
        run = job([trial("a"), trial("b", "agent_error")], dataset="gaia/gaia-validation", agent="codex")
        run.identity = parse.identity_of(cfg, {}, "2.2.2")
        self.assertNotIn("benchmark_version", run.identity)
        result = board.build([run], agents, benches, configuration.defaults(benches))
        row = next(r for r in result["rows"] if r["has_runs"])
        self.assertIsNone(row["axes"]["Retrieval"]["score"])
        self.assertEqual(row["axes"]["Retrieval"]["entries"][0]["reason"], "Benchmark version not recorded")
        run.identity["benchmark_version"] = next(b for b in benches if b["key"] == "gaia")["upstream"]["tested"]
        result = board.build([run], agents, benches, configuration.defaults(benches))
        row = next(r for r in result["rows"] if r["has_runs"])
        self.assertEqual(row["axes"]["Retrieval"]["score"], .5)
        self.assertEqual(row["company"], "OpenAI")
        run.identity["benchmark_version"] = "unknown-version"
        result = board.build([run], agents, benches, configuration.defaults(benches))
        self.assertIsNone(next(r for r in result["rows"] if r["has_runs"])["axes"]["Retrieval"]["score"])


def _write_run(root, name, benchmark, dataset, agent="codex", backend="local", outcomes=("pass",), cost=None):
    """A finished run whose config is the standard runner's own."""
    path = Path(root) / name
    path.mkdir(parents=True)
    recorded_job.write_config(str(path), job=name, bench=definitions.get("benchmark", benchmark), other=definitions.get("agent", agent),
                              model="model", provider="openrouter", dataset=dataset, units=[str(i) for i in range(len(outcomes))],
                              concurrency=1, granted=True, backend=backend)
    (path / "result.json").write_text(json.dumps({"started_at": "2026-11-01T10:00:00Z", "finished_at": "2026-11-01T11:00:00Z"}))
    for i, outcome in enumerate(outcomes):
        folder = path / str(i)
        folder.mkdir()
        value = {"task_name": str(i), "trial_name": str(i), "source": benchmark, "agent_info": {"name": agent, "version": "adapter 9.9"}}
        if cost is not None:
            value["agent_result"] = {"cost_usd": cost}
        if outcome == "pass":
            value["verifier_result"] = {"rewards": {"reward": 1}}
            (folder / "verifier").mkdir()
            (folder / "verifier" / "reward.txt").write_text("1")
        else:
            value["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "fixture"}
        (folder / "result.json").write_text(json.dumps(value))


class RecordedRuns(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def rows(self, *roots):
        env = dict(os.environ, NULLIUS_HOME=str(self.folder / "profile"), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        result = subprocess.run([sys.executable, "-m", "nullius", "leaderboard", "--json", *map(str, roots)], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [r for r in json.loads(result.stdout)["rows"] if r["has_runs"]]

    def test_a_run_naming_its_agent_s_version_counts_for_that_agent_and_version(self):
        codex = definitions.get("agent", "codex")
        self.assertIn("@", codex["_ref"])
        _write_run(self.folder / "jobs", "run", "gaia", "gaia-validation")
        row, = self.rows(self.folder / "jobs")
        self.assertEqual((row["agent"], row["version"], row["version_label"]), ("codex", codex["_version"], definitions.version_label(codex)))
        self.assertEqual(row["axes"]["Retrieval"]["score"], 1)

    def test_runs_on_two_backends_are_two_rows(self):
        _write_run(self.folder / "jobs", "a", "gaia", "gaia-validation", "goose", "local")
        _write_run(self.folder / "jobs", "b", "gaia", "gaia-validation", "goose", "docker", ("crash",))
        rows = self.rows(self.folder / "jobs")
        self.assertEqual({r["identity"]["backend"]: r["axes"]["Retrieval"]["score"] for r in rows}, {"local": 1, "docker": 0})

    def test_runs_of_one_name_under_two_roots_stay_apart(self):
        _write_run(self.folder / "one", "same", "gaia", "gaia-validation", cost=1)
        _write_run(self.folder / "two", "same", "tau3-bench", "banking_knowledge", cost=3)
        row, = self.rows(self.folder / "one", self.folder / "two")
        runs = [e["runs"] for e in row["axes"]["Retrieval"]["entries"] if e["runs"]]
        self.assertEqual([len(r) for r in runs], [1, 1])
        self.assertNotEqual(runs[0], runs[1])
        self.assertEqual(row["metrics"]["cost"], {"value": 2, "count": 2})

    def test_a_merged_run_still_replaces_its_own_shards_when_names_repeat(self):
        from dataclasses import replace
        from nullius.leaderboard import service
        here, there = (replace(job([trial("a")], job_id=n), dir=f"/{root}/{n}", merged_from=m)
                       for root, n, m in (("one", "shard", []), ("two", "shard", [])))
        whole = replace(job([trial("a")], job_id="whole"), dir="/one/whole", merged_from=["shard"])
        got = service.qualified([here, there, whole])
        self.assertEqual([j.job_id for j in got], ["/one/shard", "/two/shard", "whole"])
        self.assertEqual(got[2].merged_from, ["/one/shard"])


class DeferredMetrics(unittest.TestCase):
    def test_content_cache_reparses_only_changed_files_and_isolates_failures(self):
        from nullius.leaderboard import metrics
        from nullius.traces import trajectory
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.json"
            second = Path(folder) / "second.json"
            doc = {"steps": [{"step_id": i, "source": "agent", "message": "hello",
                               "tool_calls": [{"tool_call_id": str(i), "function_name": "test", "arguments": {}}] if i < 2 else []}
                              for i in range(3)]}
            first.write_text(json.dumps(doc))
            second.write_text("{")
            cache = metrics.Cache()
            with patch.object(trajectory, "load", wraps=trajectory.load) as load:
                self.assertEqual(cache.read(str(first)), {"steps": 3, "calls": 2})
                self.assertEqual(cache.read(str(first)), {"steps": 3, "calls": 2})
                self.assertEqual(load.call_count, 1)
                self.assertEqual(cache.read(str(second)), {"steps": None, "calls": None})
                first.write_text(json.dumps({"steps": doc["steps"][:2]}))
                self.assertEqual(cache.read(str(first)), {"steps": 2, "calls": 2})
                self.assertEqual(load.call_count, 3)
                self.assertEqual(cache.read(str(second)), {"steps": None, "calls": None})
                self.assertEqual(load.call_count, 3)


if __name__ == "__main__":
    unittest.main()
