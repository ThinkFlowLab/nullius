"""Opt-in driver setup and unit cleanup, with agent-only options and setup bounds."""
from __future__ import annotations

import json
import os
from concurrent.futures import CancelledError
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import threading
import unittest

from unittest import mock

from nullius.standard import definitions, install, jsonl, prove, recipe, run

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
BENCH = {
    "schema": "nullius.benchmark/v1", "key": "fixture", "label": "Fixture", "summary": "A task.",
    "family": "agent", "needs": ["agent.task/v1"],
    "driver": {"command": ["python3", "{here}/driver.py"]},
    "datasets": [{"id": "all", "units": 1}],
    "units": {"noun": "task", "nouns": "tasks"},
    "score": {"kind": "pass", "says": "the answer is right"},
}
# A driver that records what setup was told, in the unit's trial folder.
DRIVER = '''import json, os
from nullius.standard.jsonl import serve
def setup(unit, dir, **told):
    with open(os.path.join(os.path.dirname(dir), "told.json"), "w") as f:
        json.dump(told, f)
    return {"prompt": "hi"}
serve({"units": lambda dataset: {"units": ["a"]}, "setup": setup,
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True, "detail": {}}})
'''
# The same, recording each unit's end too.
ENDING_DRIVER = DRIVER.replace('serve({"units"', '''def end(unit, dir):
    with open(os.path.join(os.path.dirname(dir), "ended.json"), "a") as f:
        f.write(json.dumps({"unit": unit, "dir": dir}) + "\\n")
    return {}
serve({"end": end, "units"''')
# Its setup failing: the unit ends before any agent or score.
FAILING_DRIVER = ENDING_DRIVER.replace('    return {"prompt": "hi"}', '    raise ValueError("no task")')
# The same with setup as most drivers write it: the unit and its folder, nothing else.
PLAIN_DRIVER = DRIVER.replace("def setup(unit, dir, **told):", "def setup(unit, dir):\n    told = {}")


class SetupFields(unittest.TestCase):
    def test_participant_and_setup_seconds_are_checked(self):
        self.assertEqual(definitions.problems(dict(BENCH, driver=dict(BENCH["driver"], participant=True),
                                                   limits={"setup_seconds": 1800}), "benchmark"), [])
        self.assertIn("driver.participant: true or false - whether setup is told the participant's kind",
                      definitions.problems(dict(BENCH, driver=dict(BENCH["driver"], participant="yes")), "benchmark"))
        self.assertIn("limits.setup_seconds: a whole number from 1",
                      definitions.problems(dict(BENCH, limits={"setup_seconds": 0}), "benchmark"))

    def test_setup_seconds_rejects_noninteger_and_out_of_range_values(self):
        for value in (True, -1, 1.5, "600", 100001):
            with self.subTest(value=value):
                self.assertIn("limits.setup_seconds: a whole number from 1",
                              definitions.problems(dict(BENCH, limits={"setup_seconds": value}), "benchmark"))

    def test_setup_preserves_attempts_and_sends_only_requested_fields(self):
        for kind in ("agent", "capability", "control"):
            with self.subTest(kind=kind):
                self.assertEqual(run.setup_request(BENCH, kind, 2), ({}, 600.0))
                bench = dict(BENCH, driver=dict(BENCH["driver"], participant=True),
                             limits={"attempts": "A seed per attempt.", "setup_seconds": 1800})
                self.assertEqual(run.setup_request(bench, kind, 2),
                                 ({"participant": kind, "attempt": 2}, 1800.0 if kind == "agent" else 600.0))
                bench["driver"]["participant"] = False
                self.assertEqual(run.setup_request(bench, kind, 2)[0], {"attempt": 2})

    def test_the_leak_check_sets_up_a_control_with_the_original_bound(self):
        bench = dict(BENCH, driver=dict(BENCH["driver"], participant=True), limits={"setup_seconds": 1800})
        with tempfile.TemporaryDirectory() as tmp:
            proc = mock.Mock()
            proc.request.return_value = {}
            driver = mock.Mock(work=tmp, bench=bench)
            driver.start.return_value = proc
            prove.leak_check(driver, ["a"], "agent")
        op, kw = proc.request.call_args_list[0].args[0], proc.request.call_args_list[0].kwargs
        self.assertEqual((op, kw["timeout"], kw["participant"]), ("setup", 600.0, "control"))
        proc.close.assert_called_once()


class AgentOptions(unittest.TestCase):
    def setUp(self):
        self.option = {"label": "Steps", "summary": "The number of steps.", "values": ["10", "20"],
                       "default": "10", "setups": ["agent"]}
        self.bench = dict(BENCH, options={"steps": self.option})

    def test_agent_scope_is_validated(self):
        self.assertEqual(definitions.problems(self.bench, "benchmark"), [])
        for setups in ([], "agent", ["capability"]):
            with self.subTest(setups=setups):
                self.assertTrue(any("options.steps.setups" in p for p in definitions.problems(
                    dict(BENCH, options={"steps": dict(self.option, setups=setups)}), "benchmark")))

    def test_agent_stands_alone_on_a_benchmark_agents_run(self):
        for setups in (["agent", "original"], ["agent", "yours"], ["model", "agent"]):
            with self.subTest(setups=setups):
                self.assertTrue(any("options.steps.setups" in p for p in definitions.problems(
                    dict(self.bench, options={"steps": dict(self.option, setups=setups)}), "benchmark")))
        capability = dict(self.bench, family="capability", needs=["cap.memory/v1"])
        self.assertTrue(any("options.steps.setups" in p for p in definitions.problems(capability, "benchmark")))
        self.assertFalse(any("options.steps" in p for p in definitions.problems(
            dict(capability, options={"steps": dict(self.option, setups=["yours"])}), "benchmark")))

    def test_only_an_agent_gets_and_validates_the_option(self):
        for kind in ("agent", "capability", "control", "upstream", None):
            for scope in ("yours", "original", "model"):
                with self.subTest(kind=kind, scope=scope):
                    active = kind == "agent" and scope == "yours"
                    vals, bad = definitions.bench_options_of(self.bench, "all", {"steps": "20"}, scope, kind=kind)
                    self.assertEqual((vals, bad), ({"steps": "20"} if active else {}, []))
                    self.assertEqual(bool(definitions.bench_options_of(
                        self.bench, "all", {"steps": "bad"}, scope, kind=kind)[1]), active)

    def test_a_recipe_carries_the_option_only_to_its_agent(self):
        defs = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
        bench = definitions.get("benchmark", "fakebench", ROOT)
        bench["options"] = self.bench["options"]
        bench["controls"] = {"nop": {"source": "nullius", "from": "nothing"}}
        defs["benchmark"] = [bench]
        rec = {"version": 1, "harness": "standard", "name": "fixture", "benchmark": "fakebench",
               "dataset": "all", "others": ["fake", "nop"], "provider": "openai", "model": "m",
               "limit": 1, "concurrency": 1, "unsandboxed": True, "where": "local", "bench_options": {"steps": "20"}}
        out = recipe.compile(rec, defs)
        self.assertEqual(out["errors"], [])
        self.assertIn("--bench-option steps=20", out["parts"][0]["script"])
        self.assertNotIn("--bench-option steps", out["parts"][1]["script"])
        rec["bench_options"]["steps"] = "bad"
        self.assertTrue(recipe.compile(rec, defs)["errors"])
        rec["others"] = ["nop"]
        self.assertEqual(recipe.compile(rec, defs)["errors"], [])

    @unittest.skipUnless(shutil.which("node"), "Node is required for builder options")
    def test_the_builder_places_the_option_with_selected_agents(self):
        source = (Path(__file__).resolve().parents[1] / "static/builder/command-builder.js").read_text()
        function = source[source.index("  function benchOptions("):source.index("\n  function ", source.index("  function benchOptions(") + 1)]
        program = """const C = {chosenOf: r => r.others, dsIds: () => ['all']};
const schema = {standardHere: {}}, esc = x => x, tile = x => x.title;
""" + function + """
const b = {options: {steps: {label: 'Steps', summary: 'Steps', values: ['10'], default: '10', datasets: [], setups: ['agent'], step: ''}}};
const r = {others: ['fake', 'original']};
process.stdout.write(JSON.stringify(['yours', 'setup', 'shared'].map(s => benchOptions(r, b, s))));
"""
        result = subprocess.run(["node", "-e", program], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        yours, setup, shared = json.loads(result.stdout)
        self.assertIn("Steps", yours)
        self.assertEqual((setup, shared), ("", ""))



class ThroughARun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

        self.env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}
        for name, value in (("unit_relay", None), ("drive_agent", ("answer", None, {}))):
            patch = mock.patch.object(run.Run, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)


    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


    def go(self, code: str, other: dict, **bench) -> tuple:
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(code)
        b = dict(BENCH, _here=here, _kind="benchmark", **bench)
        job = run.run(b, other, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env,
                      say=lambda s: None, unsandboxed=True)
        trial = next(os.path.join(job, d) for d in os.listdir(job) if "__u" in d)
        with open(os.path.join(job, "config.json")) as f:
            config = json.load(f)
        with open(os.path.join(trial, "told.json")) as f:
            told = json.load(f)
        with open(os.path.join(trial, "result.json")) as f:
            result = json.load(f)
        return config, told, result


    def fake_agent(self) -> dict:
        return definitions.get("agent", "fake", ROOT)


    def test_setup_is_told_the_participant_when_the_driver_asks(self):
        _, told, _ = self.go(DRIVER, self.fake_agent(), driver=dict(BENCH["driver"], participant=True))
        self.assertEqual(told, {"participant": "agent"})
        control = definitions.control(dict(BENCH, controls={"nop": {"source": "nullius", "from": "nothing"}}), "nop")
        _, told, _ = self.go(DRIVER, control, driver=dict(BENCH["driver"], participant=True),
                             controls={"nop": {"source": "nullius", "from": "nothing"}})
        self.assertEqual(told, {"participant": "control"})


    def test_a_driver_whose_setup_takes_nothing_more_runs_as_before(self):
        config, told, result = self.go(PLAIN_DRIVER, self.fake_agent())
        self.assertEqual(told, {})
        self.assertIsNone(result.get("exception_info"))


    def trial_of(self, code: str, other: dict = None, **bench) -> str:
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(code)
        jobs = tempfile.mkdtemp(dir=self.tmp)
        job = run.run(dict(BENCH, _here=here, _kind="benchmark", **bench), other or self.fake_agent(), model="m-1",
                      provider="custom", jobs_root=jobs, concurrency=1, env=self.env, say=lambda s: None,
                      unsandboxed=True)
        return next(os.path.join(job, d) for d in os.listdir(job) if "__u" in d)


    def test_the_null_controls_unit_gets_no_end(self):
        asks = dict(BENCH["driver"], unit_end=True)
        controls = {"nop": {"source": "nullius", "from": "nothing"}}
        control = definitions.control(dict(BENCH, controls=controls), "nop")
        trial = self.trial_of(ENDING_DRIVER, control, driver=asks, controls=controls)
        self.assertFalse(os.path.exists(os.path.join(trial, "ended.json")))


    def test_the_end_still_goes_once_the_run_is_stopped_and_its_failure_is_no_units(self):
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here, exist_ok=True)
        record = os.path.join(here, "ended.txt")
        answering = ("import sys, json\n"
                     "for line in sys.stdin:\n"
                     "    open(sys.argv[1], 'a').write(json.loads(line)['op'] + '\\n')\n"
                     "    print(sys.argv[2], flush=True)\n")
        stopped = threading.Event()
        for reply in ('{}', 'not json', '{"detail": ' + '[' * 3000 + '0' + ']' * 3000 + '}'):
            with self.subTest(reply=reply[:12]):
                r = run.Run.__new__(run.Run)
                r.bench, r.other, r.local = dict(BENCH, driver=dict(BENCH["driver"], unit_end=True)), {"_kind": "agent"}, \
                    threading.local()
                r.local.driver = jsonl.Proc([sys.executable, "-c", answering, record, reply], here, dict(os.environ),
                                            os.path.join(here, "driver.log"), stopped=stopped).start()
                stopped.set()                                       # the run is stopped once the unit has its driver
                try:
                    r.unit_ended("a", here)                         # returns, whatever the driver answered
                finally:
                    r.local.driver.close()
                    stopped.clear()
        with open(record) as f:
            self.assertEqual(f.read().split(), ["end", "end", "end"])


    def test_a_driver_that_asks_is_told_each_units_end(self):
        asks = dict(BENCH["driver"], unit_end=True)
        trial = self.trial_of(ENDING_DRIVER, driver=asks)
        with open(os.path.join(trial, "ended.json")) as f:
            self.assertEqual(json.load(f), {"unit": "a", "dir": os.path.join(trial, "workspace")})
        trial = self.trial_of(FAILING_DRIVER, driver=asks)
        with open(os.path.join(trial, "result.json")) as f:
            self.assertEqual(json.load(f)["exception_info"]["exception_type"], "EnvironmentSetupError")
        self.assertTrue(os.path.isfile(os.path.join(trial, "ended.json")))          # told although setup failed


    def test_a_driver_that_does_not_ask_is_sent_no_end(self):
        self.assertIn("driver.unit_end: true or false - whether the driver is told each unit's end",
                      definitions.problems(dict(BENCH, driver=dict(BENCH["driver"], unit_end="yes")), "benchmark"))
        trial = self.trial_of(ENDING_DRIVER)
        self.assertFalse(os.path.exists(os.path.join(trial, "ended.json")))

    def test_a_capability_is_named_but_gets_no_end(self):
        capability = definitions.get("capability", "fakemem", ROOT)
        with mock.patch.object(run.Run, "drive_feature", return_value=("answer", None)):
            trial = self.trial_of(ENDING_DRIVER, capability, family="capability", needs=["cap.memory/v1"],
                                  driver=dict(BENCH["driver"], participant=True, unit_end=True))
        with open(os.path.join(trial, "told.json")) as f:
            self.assertEqual(json.load(f), {"participant": "capability"})
        self.assertFalse(os.path.exists(os.path.join(trial, "ended.json")))

    def test_a_stopped_unit_gets_one_end_and_the_next_unit_never_starts(self):
        def stop(r, *args, **kwargs):
            r._stopped.set()
            return "answer", None, {}

        with mock.patch.object(run.Run, "drive_agent", stop):
            trial = self.trial_of(ENDING_DRIVER.replace('["a"]', '["a", "b"]'),
                                  driver=dict(BENCH["driver"], unit_end=True))
        with open(os.path.join(trial, "ended.json")) as f:
            self.assertEqual([json.loads(line) for line in f], [{"unit": "a", "dir": os.path.join(trial, "workspace")}])
        with open(os.path.join(trial, "result.json")) as f:
            self.assertEqual(json.load(f)["exception_info"]["exception_type"], "CancelledError")
        self.assertEqual([d for d in os.listdir(os.path.dirname(trial)) if "__u" in d], ["a__u0000"])

    def ending(self) -> "run.Run":
        r = run.Run.__new__(run.Run)
        r.bench, r.other, r.local = dict(BENCH, driver=dict(BENCH["driver"], unit_end=True)), {"_kind": "agent"}, threading.local()
        return r

    def driver_proc(self, code: str, *args, stopped=None) -> jsonl.Proc:
        here = tempfile.mkdtemp(dir=self.tmp)
        return jsonl.Proc([sys.executable, "-c", code, *args], here, dict(os.environ), os.path.join(here, "driver.log"),
                          stopped=stopped)

    def test_only_an_agents_failed_end_closes_the_computer_driver(self):
        for kind in ("agent", "control", "upstream"):
            for failed in (False, True):
                with self.subTest(kind=kind, failed=failed):
                    r = self.ending()
                    r.bench["needs"] = ["agent.computer/v1"]
                    r.other = {"_kind": kind}
                    d = r.local.driver = mock.Mock(unanswered=False)
                    d.p.poll.return_value = None
                    d.request.side_effect = run.jsonl.Failed("end failed") if failed else None

                    def unit(*args):
                        r.local.set_up = ("a", self.tmp)
                        return {"reward": 1}

                    with mock.patch.object(r, "_unit", side_effect=unit):
                        self.assertEqual(r.unit(0, "a"), {"reward": 1})
                    self.assertEqual(d.request.called, kind == "agent")
                    self.assertEqual(d.close.called, kind == "agent" and failed)
                    self.assertIs(r.local.driver, None if kind == "agent" and failed else d)

    def test_an_end_never_starts_a_driver(self):
        r = self.ending()
        r.local.driver = None
        r.unit_ended("a", self.tmp)                                 # no driver: nothing is started
        never = self.driver_proc("import sys; sys.stdin.read()")
        ended = self.driver_proc("pass").start()
        ended.p.wait(timeout=10)
        running = self.driver_proc("import sys\nfor line in sys.stdin: print('{}', flush=True)").start()
        try:
            for driver, asked in ((never, False), (ended, False), (running, True)):
                with self.subTest(started=driver.p is not None, asked=asked):
                    r.local.driver = driver
                    with mock.patch.object(driver, "request", wraps=driver.request) as request:
                        r.unit_ended("a", self.tmp)
                    self.assertEqual(request.called, asked)
            self.assertIsNone(never.p)                                  # a driver never started stays so
        finally:
            for driver in (ended, running):
                driver.close()

    def test_an_end_is_waited_for_once_the_run_is_stopped(self):
        here = tempfile.mkdtemp(dir=self.tmp)
        mark = os.path.join(here, "ended")
        answering = ("import sys, time\n"
                     "for line in sys.stdin:\n"
                     "    time.sleep(1.0)\n"
                     "    open(sys.argv[1], 'w').write('ended')\n"
                     "    print('{}', flush=True)\n")
        stopped = threading.Event()
        r = self.ending()
        r.local.driver = self.driver_proc(answering, mark, stopped=stopped).start()
        stopped.set()
        try:
            r.unit_ended("a", here)
            self.assertTrue(os.path.exists(mark), "returned before the driver answered its end")
        finally:
            r.local.driver.close()

    def test_a_driver_with_a_request_the_stop_left_unanswered_gets_no_end(self):
        here = tempfile.mkdtemp(dir=self.tmp)
        ops = os.path.join(here, "ops")
        late = ("import sys, json, time\n"
                "for line in sys.stdin:\n"
                "    op = json.loads(line)['op']\n"
                "    open(sys.argv[1], 'a').write(op + '\\n')\n"
                "    time.sleep(1.0)\n"
                "    print(json.dumps({'to': op}), flush=True)\n")
        stopped = threading.Event()
        r = self.ending()
        d = r.local.driver = self.driver_proc(late, ops, stopped=stopped).start()
        try:
            with self.assertRaises(CancelledError):                 # stopped once setup is written: its reply comes late
                d.request("setup", unit="a", dir=here, timeout=60, on_sent=stopped.set)
            r.unit_ended("a", here)
            d.p.stdin.close()                                       # the driver reads all it was sent, then ends
            d.p.wait(timeout=30)
        finally:
            d.close()
        with open(ops) as f:
            self.assertEqual(f.read().split(), ["setup"])

    def test_a_stop_before_setup_is_sent_sends_no_end(self):
        real_phase = run.Run.phase

        def phase(r, name, unit, ph, timeout=None):
            if unit == "b" and ph == "env_setup":
                r._stopped.set()                                    # b passed the stop check; its setup is not sent yet
            return real_phase(r, name, unit, ph, timeout)

        noting = ENDING_DRIVER.replace("def setup(unit, dir, **told):\n",
                                       "def setup(unit, dir, **told):\n"
                                       "    open(os.path.join(os.path.dirname(dir), 'setup-sent'), 'w').write(unit)\n")
        with mock.patch.object(run.Run, "phase", phase):
            job = os.path.dirname(self.trial_of(noting.replace('["a"]', '["a", "b"]'), driver=dict(BENCH["driver"], unit_end=True)))
        a, b = os.path.join(job, "a__u0000"), os.path.join(job, "b__u0001")
        self.assertTrue(os.path.isfile(os.path.join(a, "ended.json")))               # a: set up and ended
        self.assertFalse(os.path.exists(os.path.join(b, "setup-sent")))
        self.assertFalse(os.path.exists(os.path.join(b, "ended.json")))

    def test_a_setup_past_its_time_gets_no_end_from_its_closed_driver(self):
        slow = ENDING_DRIVER.replace('    return {"prompt": "hi"}', '    import time; time.sleep(3)\n    return {"prompt": "hi"}')
        trial = self.trial_of(slow, driver=dict(BENCH["driver"], unit_end=True), limits={"setup_seconds": 1})
        with open(os.path.join(trial, "result.json")) as f:
            self.assertEqual(json.load(f)["exception_info"]["exception_type"], "EnvironmentSetupError")
        self.assertFalse(os.path.exists(os.path.join(trial, "ended.json")))

    def test_the_run_selects_agent_options_by_kind(self):
        option = {"summary": "Steps.", "values": ["10"], "default": "10", "setups": ["agent"]}
        bench = dict(BENCH, options={"steps": option}, controls={"nop": {"source": "nullius", "from": "nothing"}})
        for other in (self.fake_agent(), definitions.control(bench, "nop")):
            with self.subTest(kind=other["_kind"]):
                r = run.Run(bench, other, model="m", provider="custom",
                            env=self.env, unsandboxed=True, jobs_root=self.tmp)
                self.assertEqual(r.bench_options, {"steps": "10"} if other["_kind"] == "agent" else {})


    def env_setup_timeouts(self, other: dict, **bench) -> list:
        """The ``timeout_sec`` of each unit's ``env_setup`` phase in the job's live feed, as written."""
        trial = self.trial_of(DRIVER, other, **bench)
        with open(os.path.join(os.path.dirname(trial), "events.jsonl")) as f:
            return [e["timeout_sec"] for e in map(json.loads, f) if e.get("phase") == "env_setup"]

    def test_the_setup_phase_takes_the_setup_limit(self):
        controls = {"nop": {"source": "nullius", "from": "nothing"}}
        control = definitions.control(dict(BENCH, controls=controls), "nop")
        for other, limits, expected in ((self.fake_agent(), None, 600), (control, None, 600),
                                        (self.fake_agent(), {"setup_seconds": 1800}, 1800),
                                        (control, {"setup_seconds": 1800}, 600)):
            with self.subTest(kind=other["_kind"], limits=limits):
                bench = dict(controls=controls, **({"limits": limits} if limits else {}))
                got = self.env_setup_timeouts(other, **bench)
                self.assertEqual(got, [expected])
                self.assertIs(type(got[0]), int)                    # recorded as a whole number, as given

    def test_setup_gets_the_benchmarks_own_limit(self):
        r = run.Run.__new__(run.Run)
        r.bench, r.other, r.local = dict(BENCH, limits={"setup_seconds": 1800}), {"_kind": "agent"}, threading.local()
        self.assertEqual(r.setup_seconds(), 1800.0)
        r.other = {"_kind": "control"}
        self.assertEqual(r.setup_seconds(), float(run.DRIVER_SECONDS))
        r.bench, r.other = dict(BENCH), {"_kind": "agent"}
        self.assertEqual(r.setup_seconds(), float(run.DRIVER_SECONDS))
        slow = DRIVER.replace('    return {"prompt": "hi"}', '    import time; time.sleep(3)\n    return {"prompt": "hi"}')
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(slow)
        b = dict(BENCH, _here=here, _kind="benchmark", limits={"setup_seconds": 1})
        job = run.run(b, self.fake_agent(), model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1,
                      env=self.env, say=lambda s: None, unsandboxed=True)
        trial = next(os.path.join(job, d) for d in os.listdir(job) if "__u" in d)
        with open(os.path.join(trial, "result.json")) as f:
            failed = json.load(f)["exception_info"]
        self.assertEqual(failed["exception_type"], "EnvironmentSetupError")
        self.assertIn("setup: no reply in 1 s", failed["exception_message"])


# A driver that is told when the agent is given its task: it marks the trial, and records each score's answer.
GIVEN_DRIVER = '''import json, os, time
from nullius.standard.jsonl import serve
def setup(unit, dir, **told):
    with open(os.path.join(os.path.dirname(dir), "told.json"), "w") as f:
        json.dump(told, f)
    return {"prompt": "hi"}
def task_given(unit, dir):
    with open(os.path.join(os.path.dirname(dir), "given"), "a") as f:
        f.write(json.dumps({"unit": unit, "dir": dir}) + "\\n")
    return {}
def score(unit, dir, transcript, answer):
    with open(os.path.join(os.path.dirname(dir), "answer.txt"), "w") as f:
        f.write(answer)
    return {"reward": 1.0, "passed": True, "detail": {}}
serve({"units": lambda dataset: {"units": ["a"]}, "setup": setup, "task_given": task_given, "score": score})
'''


class TaskGiven(unittest.TestCase):
    """The run tells a driver that asks (driver.task_given) that the agent is being given its task, and waits for its
    answer, before the first prompt of an ACP agent and before a command agent starts: a real run, the fake ACP agent
    and a command of the test's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", self._home)
        self.env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}

    def given(self):
        return sorted(str(p) for p in Path(self.tmp).rglob("given"))

    def trial(self, other, code=GIVEN_DRIVER, **bench):
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(code)
        jobs = tempfile.mkdtemp(dir=self.tmp)
        job = run.run(dict(BENCH, _here=here, _kind="benchmark", **bench), other, model="m-1", provider="custom",
                      jobs_root=jobs, concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True)
        return next(os.path.join(job, d) for d in os.listdir(job) if "__u" in d)

    def test_the_flag_is_true_or_false(self):
        self.assertEqual(definitions.problems(dict(BENCH, driver=dict(BENCH["driver"], task_given=True)), "benchmark"), [])
        self.assertIn("driver.task_given: true or false - whether the driver is told when the agent is given its task",
                      definitions.problems(dict(BENCH, driver=dict(BENCH["driver"], task_given="yes")), "benchmark"))

    def test_an_acp_agent_is_sent_its_task_once_the_driver_has_taken_it(self):
        seen, prompt = [], run.acp.Client.prompt

        def watched(client, *a, **kw):
            seen.append(self.given())
            return prompt(client, *a, **kw)
        with mock.patch.object(run.acp.Client, "prompt", watched):
            trial = self.trial(definitions.get("agent", "fake", ROOT), driver=dict(BENCH["driver"], task_given=True))
        mark = os.path.join(trial, "given")
        self.assertEqual(seen, [[mark]])
        with open(mark) as f:
            self.assertEqual(json.loads(f.read()), {"unit": "a", "dir": os.path.join(trial, "workspace")})
        with open(os.path.join(trial, "answer.txt")) as f:
            self.assertEqual(f.read(), "HI")

    def test_a_command_agent_starts_once_the_driver_has_taken_its_task(self):
        counting = [sys.executable, "-c", "import pathlib, sys; print(len(list(pathlib.Path(sys.argv[1]).rglob('given'))))",
                    self.tmp, "{prompt}"]
        command = dict(definitions.get("agent", "fake", ROOT), start={"command": counting})
        trial = self.trial(command, driver=dict(BENCH["driver"], task_given=True))
        with open(os.path.join(trial, "answer.txt")) as f:
            self.assertEqual(f.read(), "1")

    def test_a_driver_that_does_not_ask_and_a_control_are_told_nothing(self):
        trial = self.trial(definitions.get("agent", "fake", ROOT))
        with open(os.path.join(trial, "answer.txt")) as f:
            self.assertEqual(f.read(), "HI")
        controls = {"nop": {"source": "nullius", "from": "nothing"}}
        control = definitions.control(dict(BENCH, controls=controls), "nop")
        self.trial(control, driver=dict(BENCH["driver"], task_given=True), controls=controls)
        self.assertEqual(self.given(), [])

    def test_a_driver_that_fails_it_ends_the_unit_before_the_task_is_sent(self):
        seen, prompt = [], run.acp.Client.prompt

        def watched(client, *a, **kw):
            seen.append(True)
            return prompt(client, *a, **kw)
        failing = GIVEN_DRIVER.replace("    return {}\ndef score", "    raise ValueError('no window')\ndef score")
        with mock.patch.object(run.acp.Client, "prompt", watched):
            trial = self.trial(definitions.get("agent", "fake", ROOT), failing, driver=dict(BENCH["driver"], task_given=True))
        with open(os.path.join(trial, "result.json")) as f:
            failed = json.load(f)["exception_info"]
        self.assertEqual(failed["exception_type"], "EnvironmentSetupError")
        self.assertIn("the benchmark was not told that the task is given", failed["exception_message"])
        self.assertEqual(seen, [])
