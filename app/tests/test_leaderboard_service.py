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
from nullius.leaderboard import board, configuration


class RecordedIdentity(unittest.TestCase):
    def test_real_cli_reads_gaia_and_banking_result_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "jobs"
            for name, benchmark, dataset, outcomes in (("gaia-run", "gaia", "gaia-validation", ("pass", "crash", "infra")),
                                                        ("bank-run", "tau3-bench", "banking_knowledge", ("pass",))):
                path = root / name
                path.mkdir(parents=True)
                (path / "config.json").write_text(json.dumps({"standard": {"benchmark": benchmark, "plugged": "codex"},
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

    def test_unversioned_catalogue_and_real_parser_agree_without_a_wildcard(self):
        agents = definitions.load("agent")[0]
        benches = definitions.load("benchmark")[0]
        cfg = {"standard": {"benchmark": "gaia", "plugged": "codex"},
               "agents": [{"name": "codex", "model_name": "model"}]}
        run = job([trial("a"), trial("b", "agent_error")], dataset="gaia/gaia-validation", agent="codex")
        run.identity = parse.identity_of(cfg, {}, "2.2.2")
        self.assertNotIn("benchmark_version", run.identity)
        result = board.build([run], agents, benches, configuration.defaults(benches))
        row = next(r for r in result["rows"] if r["has_runs"])
        self.assertEqual(row["axes"]["Retrieval"]["score"], .5)
        self.assertEqual(row["company"], "OpenAI")
        run.identity["benchmark_version"] = "unknown-version"
        result = board.build([run], agents, benches, configuration.defaults(benches))
        self.assertIsNone(next(r for r in result["rows"] if r["has_runs"])["axes"]["Retrieval"]["score"])


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
