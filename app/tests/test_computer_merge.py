"""Computer-port boundaries with limits, retries, setup clocks and trial records."""
import copy
from datetime import datetime
import json
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

from nullius.runs import artifacts, parse
from nullius.standard import boat, computer_pipe, definitions, install, recipe, run

FIX = Path(__file__).parent / "fixtures/standard"


class MeetingPoints(unittest.TestCase):
    def test_both_step_options_keep_their_scopes_defaults_and_validation(self):
        bench = definitions.get("benchmark", "osworld1")
        self.assertEqual(definitions.problems(bench, "benchmark"), [])
        for scope, kind, default, large in (("original", "upstream", "default", True),
                                            ("model", "upstream", "default", True),
                                            ("yours", "agent", "100", False)):
            values, bad = definitions.bench_options_of(bench, "test_small", setup=scope, kind=kind)
            self.assertEqual((values["max_steps"], bad), (default, []))
            self.assertEqual(not definitions.bench_options_of(bench, "test_small", {"max_steps": "1000"},
                                                              scope, kind=kind)[1], large)
        self.assertNotIn("max_steps", definitions.bench_options_of(bench, "test_small", kind="control")[0])
        option = recipe._bench_page(bench)["options"]["max_steps"]
        self.assertEqual((option["limit"], option["by"]["agent"]["m3"]), (True, "100"))
        self.assertEqual((option["variants"][0]["setups"], option["variants"][0]["default"]), (["agent"], "100"))

    def test_scoped_definitions_are_checked_without_masking_the_primary(self):
        bench = copy.deepcopy(definitions.get("benchmark", "osworld1"))
        option = bench["options"]["max_steps"]
        option["variants"][0]["default"] = "default"
        self.assertTrue(any("max_steps.default" in e for e in definitions.problems(bench, "benchmark")))
        option["variants"][0]["default"] = "100"
        option["variants"][0]["setups"] = ["original"]
        self.assertTrue(any("scopes must not overlap" in e for e in definitions.problems(bench, "benchmark")))

    def test_kit_opts_into_cleanup_before_a_retry(self):
        kit = definitions.kit("agent.computer/v1")
        self.assertTrue(kit["driver"]["unit_end"])
        spec = importlib.util.spec_from_file_location("merge_kit", Path(kit["_here"]) / "driver.py")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        with mock.patch.object(driver, "close_all") as close:
            self.assertEqual(driver.end("see", "unused"), {})
            close.assert_called_once_with(shutdown=False)

    def test_boat_refuses_the_computer_port_before_probes_or_units(self):
        other = {"_kind": "agent", "key": "fixture"}
        for bench in (definitions.get("benchmark", "osworld1"), definitions.kit("agent.computer/v1")):
            with self.subTest(bench=bench["key"]), mock.patch.object(boat, "unusable") as probe, \
                    mock.patch.object(run.Run, "driver") as driver:
                with self.assertRaisesRegex(run.BenchmarkRefused, "computer port.*cannot reach"):
                    run.Run(bench, other, model="m", provider="custom", backend="boat")
                probe.assert_not_called()
                driver.assert_not_called()

    def test_retry_closes_processes_before_archive_and_keeps_hooks_records_and_clocks_apart(self):
        self.retry_run(inert=True)

    def test_retry_with_a_real_computer_server_and_acp_session(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
            with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.bind(os.path.join(tmp, "s"))
        except OSError as e:
            self.skipTest(f"local sockets unavailable: {e}")
        self.retry_run(inert=False)

    def retry_run(self, inert):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, "HOME", os.path.join(tmp, "install")):
            bench = {"key": "fixture", "label": "Fixture", "_here": str(FIX), "family": "agent",
                     "needs": ["agent.computer/v1"], "datasets": [{"id": "all"}],
                     "units": {"noun": "unit", "nouns": "units"}, "score": {"kind": "pass", "says": "The fixture answered."},
                     "driver": {"command": [sys.executable, "{here}/computer_merge_driver.py", *(["inert"] if inert else [])],
                                "participant": True, "unit_end": True},
                     "limits": {"setup_seconds": 50, "attempts": "A seed per attempt."},
                     "mcp_servers": {"computer": {"command": [sys.executable, computer_pipe.__file__]}}}
            other = definitions.get("agent", "fake", str(FIX / "catalogue"))
            other = dict(other, _here=str(FIX), start={"acp": [sys.executable, "{here}/fake_computer_agent.py"]})
            real, seen = run.Run.drive_agent, []

            def drive(r, *args, **kwargs):
                trial = Path(args[1]).parent
                if seen:
                    before = seen[0]
                    self.assertFalse(Path(before["socket"]).exists())
                    for pid in (before["pid"], before["driver"]):
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                seen.append(json.loads((trial / "setup.json").read_text()))
                if inert:
                    kwargs["clock"]["ready"] = time.time()
                    r.phase(trial.name, args[0], "agent_run", r.agent_limit(args[4]))
                    self.assertEqual(r.max_calls, 1)
                    self.assertEqual(r.driver().request("act")["steps"], 2)
                    answer = "done", None, {}
                else:
                    answer = real(r, *args, **kwargs)
                if len(seen) == 1:
                    raise run._Ended("NonZeroAgentExitCodeError", "fixture failure", answer[1])
                return answer

            with mock.patch.object(run.Run, "drive_agent", drive), \
                    mock.patch.object(run.Run, "unit_relay", return_value=None) if inert else mock.patch.dict(os.environ, {}):
                path = Path(run.run(bench, other, model="m", provider="custom", jobs_root=tmp, concurrency=1,
                                    proving=True, unsandboxed=True, retries=1, timeouts={"setup": 30, "agent": 10},
                                    max_calls=1, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, say=lambda _: None))
            self.assertEqual(len(seen), 2)
            current, previous = path / "one__u0000", path / "retried/one__u0000.try-1"
            for trial, retry in ((previous, 0), (current, 1)):
                result = json.loads((trial / "result.json").read_text())
                start = json.loads((trial / "setup.json").read_text())
                end = json.loads((trial / "ended.json").read_text())
                self.assertEqual((start["participant"], start["attempt"]), ("agent", 1))
                self.assertFalse(Path(start["socket"]).exists())
                self.assertGreater(end["time"], start["time"])
                setup_clock, agent_clock = result["agent_setup"], result["agent_execution"]
                self.assertEqual(setup_clock["finished_at"], agent_clock["started_at"])
                self.assertGreaterEqual(datetime.fromisoformat(setup_clock["started_at"]).timestamp(), start["time"])
                calls = [json.loads(line) for line in (trial / "verifier/computer-calls.jsonl").read_text().splitlines()]
                self.assertEqual([c["tool"] for c in calls], ["screenshot", "click"])
                self.assertIn("click", artifacts.read(trial, "computer-calls")["text"])
            self.assertLess(json.loads((previous / "ended.json").read_text())["time"], seen[1]["time"])
            job = parse.parse_job(str(path))
            self.assertEqual((len(job.trials), job.trials[0].n_retries), (1, 1))
            phases = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["timeout_sec"] for e in phases if e.get("phase") == "env_setup"], [30, 30])
            self.assertEqual([e["timeout_sec"] for e in phases if e.get("phase") == "agent_run"], [10, 10])

    def test_computer_records_use_the_existing_redacted_bounded_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            keys = [k for k in artifacts.FILES if k.startswith(("computer-", "osworld-"))]
            self.assertEqual(len(keys), 7)
            for key in keys:
                path = trial / artifacts.FILES[key][1]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("MODEL_API_KEY=sk-abcdefgh1234567890\n" + "x" * 9000)
                self.assertIn("[REDACTED]", artifacts.read(trial, key)["text"])
                self.assertLessEqual(len(artifacts.read(trial, key)["text"]), artifacts.CHUNK_SIZE)
                path.unlink()
                path.symlink_to(trial.parent / "outside")
                with self.assertRaises(LookupError):
                    artifacts.read(trial, key)
