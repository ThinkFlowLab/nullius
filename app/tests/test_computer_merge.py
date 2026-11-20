"""Computer-port boundaries with limits, retries, setup clocks and trial records."""
from datetime import datetime
import json
import importlib.util
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from nullius.runs import artifacts, parse
from nullius.standard import boat, computer_pipe, definitions, install, recipe, run

FIX = Path(__file__).parent / "fixtures/standard"


class MeetingPoints(unittest.TestCase):
    def test_one_step_option_applies_to_scripts_and_the_port(self):
        bench = definitions.get("benchmark", "osworld1")
        self.assertEqual(definitions.problems(bench, "benchmark"), [])
        for scope, kind in (("original", "upstream"), ("model", "upstream"), ("yours", "agent")):
            values, bad = definitions.bench_options_of(bench, "test_small", setup=scope, kind=kind)
            self.assertEqual((values["max_steps"], bad), ("default", []))
            for value in ("default", "1", "15", "100", "1500", "9999"):
                values, bad = definitions.bench_options_of(bench, "test_small", {"max_steps": value}, scope, kind=kind)
                self.assertEqual((values["max_steps"], bad), (value, []))
            for value in ("0", "10000", "1.5"):
                self.assertTrue(definitions.bench_options_of(bench, "test_small", {"max_steps": value}, scope, kind=kind)[1])
        option = recipe._bench_page(bench)["options"]["max_steps"]
        self.assertEqual((option["limit"], option["by"]["agent"]["m3"]), (True, "100"))
        self.assertEqual(option["setups"], ["original", "model", "yours"])

    def test_step_option_also_applies_to_controls_and_capabilities_that_do_not_read_it(self):
        """The shared yours scope includes the null control and capability runs."""
        bench = definitions.get("benchmark", "osworld1")
        for kind in ("control", "capability"):
            with self.subTest(kind=kind):
                values, bad = definitions.bench_options_of(bench, "test_small", setup="yours", kind=kind)
                self.assertEqual((values["max_steps"], bad), ("default", []))
                values, bad = definitions.bench_options_of(bench, "test_small", {"max_steps": "1500"},
                                                           "yours", kind=kind)
                self.assertEqual((values["max_steps"], bad), ("1500", []))

    @unittest.skipUnless(shutil.which("node"), "Node is required for builder options")
    def test_the_builder_shows_one_steps_field_for_scripts_agents_and_both(self):
        source = (Path(__file__).resolve().parents[1] / "static/builder/command-builder.js").read_text()
        functions = []
        for name in ("benchOptions", "benchLimits"):
            start = source.index("  function " + name + "(")
            end = source.index("\n  function ", start + 1)
            functions.append(source[start:end])
        bench = recipe._bench_page(definitions.get("benchmark", "osworld1"))
        # OSWorld's PromptAgent, one of its own agents: run as its Original setup (command-builder-core.js, runsOf)
        program = ("const own = [{key: 'osworld1:prompt', agent: 'prompt'}], C = {chosenOf: r => r.others, datasetsOf: () => ['test_small'],"
                   "ownAgentsOf: () => own, ownChosen: r => own.filter(a => r.others.includes(a.key)),"
                   "runsOf: r => r.others.map(k => own.some(a => a.key === k) ? 'original' : k)};"
                   "const schema = {standardHere: {}}, esc = x => x, tile = x => x.title;"
                   + "\n".join(functions) + "\nconst b = " + json.dumps(bench) + ";"
                   "process.stdout.write(JSON.stringify([['osworld1:prompt'], ['fake'], ['osworld1:prompt', 'fake']].map(others => {"
                   "const r = {others, bench_options: {}};"
                   "return ['yours', 'setup', 'shared'].map(s => benchOptions(r, b, s)).join('') + benchLimits(r, b);"
                   "})));")
        result = subprocess.run(["node", "-e", program], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        for page, default in zip(json.loads(result.stdout), ("15", "default", "default")):
            self.assertEqual(page.count('data-bench-opt-select="max_steps"'), 1)
            field = page.split('data-bench-opt-select="max_steps"', 1)[1].split('>', 1)[0]
            self.assertIn('value=""', field)
            self.assertIn(f'placeholder="{default}"', field)

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
                    with self.assertRaises(ProcessLookupError):
                        os.kill(before["pid"], 0)
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
            self.assertEqual(len(keys), 6)
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
