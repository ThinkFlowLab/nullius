"""A whole run, against the fakes: units set up, driven and scored, and the
job read back by the same parser every page uses - outcomes, tokens,
trajectories. No key, no network: the provider is a local fake endpoint."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nullius.runs import classify, parse
from nullius.standard import acp, container, definitions, install, run

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
# A conversation whose user a model plays: it opens, speaks once more, then is done.
CHAT_DRIVER = '''import json, sys, urllib.request
from nullius.standard.jsonl import serve
ROUTES = []
def ask(sim, text):
    ROUTES.append(sim["model"])
    req = urllib.request.Request(sim["base_url"] + "/chat/completions", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": sim["model"], "messages": [{"role": "user", "content": text}]}).encode())
    return json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"]
def turn(unit, dir, transcript, simulator=None):
    if not transcript:
        return {"message": ask(simulator, "open")}
    if sum(1 for t in transcript if t["role"] == "user") >= 2:
        return {"done": True}
    return {"message": ask(simulator, "more")}
serve({"units": lambda dataset: {"units": ["a", "b"]}, "setup": lambda unit, dir: {"prompt": "intro"}, "turn": turn,
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True,
                 "detail": {"said": [t["text"] for t in transcript if t["role"] == "agent"], "routes": ROUTES}}})
'''

JUDGED_DRIVER = '''import json, urllib.request
from nullius.standard.jsonl import serve
def score(unit, dir, transcript, answer, judge=None):
    req = urllib.request.Request(judge["base_url"] + "/chat/completions", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": judge["model"], "messages": [{"role": "user", "content": answer or "x"}]}).encode())
    urllib.request.urlopen(req)
    return {"reward": 1.0, "passed": True, "detail": {}}
serve({"units": lambda dataset: {"units": ["a"]}, "setup": lambda unit, dir: {"prompt": "hi"}, "score": score})
'''

CWD_DRIVER = '''import os
from nullius.standard.jsonl import serve
def setup(unit, dir):
    os.makedirs("data", exist_ok=True)
    open(os.path.join("data", ".cache"), "w").write("x")
    return {"prompt": "hi"}
serve({"units": lambda dataset: {"units": ["a"]}, "setup": setup,
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True, "detail": {}}})
'''

CALLING_DRIVER = '''from nullius.standard.jsonl import serve
serve({"units": lambda dataset: {"units": [f"m{i}" for i in range(8)]}, "setup": lambda unit, dir: {"prompt": "model hi"},
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True, "detail": {}}})
'''


class Echo(BaseHTTPRequestHandler):
    """A model that says back the last message, and counts its tokens."""
    status = 200
    refused = None          # a model that gets the rate limit while every other is answered

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if Echo.status != 200 or body.get("model") == Echo.refused:
            out = json.dumps({"error": {"message": "Rate limit reached for requests"}}).encode()
            self.send_response(Echo.status if Echo.status != 200 else 429)
        else:
            said = body["messages"][-1]["content"]
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "you said: " + said}}],
                              "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class RunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.env = {"MODEL_API_URL": f"http://127.0.0.1:{cls.up.server_address[1]}/v1", "MODEL_API_KEY": "sk-real"}

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        Echo.status, Echo.refused = 200, None

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self, bench, other, **kw):
        b = definitions.get("benchmark", bench, ROOT)
        if kw.get("dataset") == "responding":
            b = dict(b, datasets=[{"id": "responding", "units": 3}])
        o = definitions.get("agent", other, ROOT) or definitions.get("capability", other, ROOT)
        return run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=2, env=self.env,
                       say=lambda s: None, unsandboxed=True, **kw)

    def outcomes(self, job_dir):
        j = parse.parse_job(job_dir)
        return {t.trial_name.split("__")[0]: t for t in j.trials}, j

    def assert_no_key(self, job_dir):
        for root, _, files in os.walk(job_dir):
            for n in files:
                with open(os.path.join(root, n), errors="replace") as f:
                    self.assertNotIn("sk-real", f.read(), os.path.join(root, n))

    def assert_unit_relays(self, job_dir):
        trials = [d for d in os.listdir(job_dir) if "__u" in d]
        for t in trials:
            tag = t.rsplit("__", 1)[1]
            self.assertEqual([n for n in os.listdir(os.path.join(job_dir, t, ".work", "relay")) if n.endswith(".jsonl")],
                             [tag + ".jsonl"] if t.startswith("model__") else [], t)
        self.assertFalse(os.path.exists(os.path.join(job_dir, ".work", "relay")))      # no relay shared by the run

    def test_a_stopped_run_starts_no_queued_unit_and_still_writes_its_result(self):
        """Stop is a SystemExit in the CLI (cli._exit_on_term): the units not
        begun stay unbegun and Run.start's cleanup writes the job's result."""
        seen = []
        def unit(self_, i, u, r):
            seen.append(u)
            raise SystemExit(143)
        b, o = definitions.get("benchmark", "fakebench", ROOT), definitions.get("agent", "fake", ROOT)
        with mock.patch.object(run.Run, "unit", unit), self.assertRaises(SystemExit):
            run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env,
                    say=lambda s: None, unsandboxed=True)
        self.assertEqual(len(seen), 1)
        jobs = [d for d in os.listdir(self.tmp) if os.path.isfile(os.path.join(self.tmp, d, "result.json"))]
        self.assertEqual(len(jobs), 1)

    def test_an_endpoint_that_refuses_the_key_stops_the_run_and_fails_it(self):
        """A wrong key fails every unit the same way: after three in a row
        nothing more is begun, and the run reports a failure."""
        here = os.path.join(self.tmp, "calling")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(CALLING_DRIVER)
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here, driver={"command": ["python3", "{here}/driver.py"]})
        o = definitions.get("agent", "fake", ROOT)
        Echo.status = 401
        said = []
        job_dir, failed = run.run_and_report(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1,
                                             env=self.env, say=said.append, unsandboxed=True, proving=True)
        trials = parse.parse_job(job_dir).trials
        self.assertTrue(failed)
        self.assertEqual(len(trials), run.REFUSED_IN_A_ROW)                  # of eight units
        self.assertTrue(all(t.exception_type == "AgentAuthenticationError" for t in trials))
        self.assertTrue(any(s.startswith("stopped: the endpoint refused 3 units in a row") for s in said), said)
        Echo.status = 200
        job_dir, failed = run.run_and_report(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env,
                                             say=lambda s: None, unsandboxed=True, proving=True, name="answered")
        self.assertFalse(failed)

    def test_an_unexpected_error_in_a_unit_is_that_units_alone(self):
        """A missing agent binary (OSError) or a driver reply without its
        prompt (KeyError) ended the job with a traceback, the unit unwritten
        and the rest miscounted as cancelled."""
        real = run.Run.drive_agent

        def drive(self_, unit, *a, **kw):
            if unit == "good":
                raise FileNotFoundError(2, "No such file or directory", "agent-acp")
            return real(self_, unit, *a, **kw)
        with mock.patch.object(run.Run, "drive_agent", drive):
            job_dir = self.go("fakebench", "fake", dataset="responding")
        by_unit, j = self.outcomes(job_dir)
        self.assertEqual(sorted(by_unit), ["good", "model", "wrong"])           # every unit written
        self.assertEqual(by_unit["good"].exception_type, "FileNotFoundError")
        self.assertIn("agent-acp", by_unit["good"].exception_message)
        self.assertEqual(by_unit["wrong"].exception_type, "")

    def test_a_unit_still_running_when_the_run_is_stopped_ends_cancelled(self):
        """Stop ends a unit's agent and relay under it, and it then failed
        with whatever that broke - an EnvironmentSetupError for a relay
        removed as it started - charged to the environment."""
        import signal
        real = run.Run.drive_agent

        def drive(self_, unit, *a, **kw):
            if unit == "good":
                os.kill(os.getpid(), signal.SIGINT)          # the Stop, while this unit runs
                self_._stopped.wait(10)                      # taken by the main thread, however loaded the machine
                raise container.Unavailable("the unit's relay: no public port published")
            return real(self_, unit, *a, **kw)
        with mock.patch.object(run.Run, "drive_agent", drive), self.assertRaises(KeyboardInterrupt):
            self.go("fakebench", "fake", dataset="responding", name="stopped")
        with open(os.path.join(self.tmp, "stopped", [d for d in os.listdir(os.path.join(self.tmp, "stopped")) if d.startswith("good__")][0], "result.json")) as f:
            info = json.load(f)["exception_info"]
        self.assertEqual(info["exception_type"], "CancelledError")
        self.assertIn("stopped before this unit ended", info["exception_message"])
        self.assertEqual(classify.from_verdict(info["exception_type"], None), classify.UNKNOWN)   # charged to no one
        with open(os.path.join(self.tmp, "stopped", run.live_feed.FEED)) as f:
            ends = [e for e in map(json.loads, f) if e.get("event") == "trial.end" and e.get("task") == "good"]
        self.assertEqual([e.get("outcome") for e in ends], ["cancelled"])         # the live board's word for it

    def test_a_completed_score_is_not_relabelled_cancelled_during_cleanup(self):
        real = run.Run.judge
        def judge(self_, *args):
            # Set Stop after the scorer replied, while the unit closes its relay.
            rl = args[0]
            stop = rl.stop
            def finish():
                stop()
                self_._stopped.set()
            rl.stop = finish
            return real(self_, *args)
        with mock.patch.object(run.Run, "judge", judge):
            directory = self.go("fakebench", "fake", dataset="responding")
        trials = parse.parse_job(directory).trials
        self.assertTrue(trials)
        self.assertTrue(all(t.exception_type != "CancelledError" for t in trials if t.reward is not None))
        self.assertTrue(any(t.reward is not None for t in trials))

    def test_a_term_runs_the_cleanup_and_a_second_term_cannot_cut_it(self):
        import subprocess
        import sys
        from tests import APP
        done = os.path.join(self.tmp, "cleaned")
        script = ("import os, signal, time\nfrom nullius.standard import cli\ncli._exit_on_term()\n"
                  "try:\n    os.kill(os.getpid(), signal.SIGTERM); time.sleep(5)\n"
                  f"finally:\n    os.kill(os.getpid(), signal.SIGTERM); time.sleep(0.2); open({done!r}, 'w').write('ok')\n")
        p = subprocess.run([sys.executable, "-c", script], env=dict(os.environ, PYTHONPATH=str(APP)), timeout=30)
        self.assertEqual(p.returncode, 143)
        self.assertTrue(os.path.exists(done))

    def test_an_agent_run_reads_back_with_its_outcomes(self):
        job_dir = self.go("fakebench", "fake")
        got, j = self.outcomes(job_dir)
        self.assertEqual(got["good"].outcome, classify.RESOLVED)
        self.assertEqual(got["wrong"].outcome, classify.FAILED_VERIFICATION)
        self.assertEqual(got["slow"].outcome, classify.AGENT_ERROR)          # the turn timed out: the agent's
        self.assertEqual(got["model"].outcome, classify.FAILED_VERIFICATION)
        self.assertEqual(j.n_resolved, 1)
        self.assert_no_key(job_dir)
        self.assert_unit_relays(job_dir)

    def test_the_trajectory_carries_the_prompt_the_tool_call_and_the_answer(self):
        job_dir = self.go("fakebench", "fake", limit=1)
        trial = [d for d in os.listdir(job_dir) if d.startswith("good__")][0]
        with open(os.path.join(job_dir, trial, "agent", "trajectory.json")) as f:
            t = json.load(f)
        self.assertEqual(t["steps"][0]["source"], "user")
        self.assertEqual(t["steps"][0]["message"], "write answer.txt 42")
        calls = [s for s in t["steps"] if s.get("tool_calls")]
        self.assertEqual(calls[0]["tool_calls"][0]["function_name"], "write answer.txt")
        self.assertEqual(t["steps"][-1]["message"], "done")

    def test_an_agents_trial_writes_a_trace_with_its_turn_and_tool_call(self):
        job_dir = self.go("fakebench", "fake", limit=1)
        trial = [d for d in os.listdir(job_dir) if d.startswith("good__")][0]
        with open(os.path.join(job_dir, trial, "agent", "trace.json")) as f:
            session = json.load(f)["spans"][0]
        self.assertEqual(session["kind"], "session")
        turn = next(c for c in session["children"] if c["kind"] == "user_message")
        tool = next(c for c in turn["children"] if c["kind"] == "tool")
        self.assertEqual(tool["attributes"]["tool_name"], "write answer.txt")
        self.assertEqual(tool["timing_quality"], "measured")

    def test_each_workspace_is_a_repository_of_its_own(self):
        # an agent looking up for its instructions (AGENTS.md) stops there, not at the checkout holding the jobs
        job_dir = self.go("fakebench", "fake", limit=1)
        trial = [d for d in os.listdir(job_dir) if d.startswith("good__")][0]
        self.assertTrue(os.path.isdir(os.path.join(job_dir, trial, "workspace", ".git")))

    def test_the_agent_gets_the_relay_and_a_placeholder_never_the_key(self):
        job_dir = self.go("fakebench", "fake", dataset="responding")
        trial = [d for d in os.listdir(job_dir) if d.startswith("model__")][0]
        with open(os.path.join(job_dir, trial, "agent", "calls.jsonl")) as f:
            calls = [json.loads(l) for l in f]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "m-1")                 # the recipe's model, whatever the agent asked
        self.assertEqual(calls[0]["model_asked"], "fake")
        with open(os.path.join(job_dir, trial, "result.json")) as f:
            res = json.load(f)
        self.assertEqual(res["agent_result"]["n_input_tokens"], 11)
        self.assert_no_key(job_dir)
        with open(os.path.join(job_dir, "config.json")) as f:
            std = json.load(f)["standard"]
        self.assertEqual((std["backend"], std["unsandboxed"]), ("local", True))

    def test_an_agent_that_never_answers_its_start_fails_its_units_not_the_run(self):
        real = acp.Client.request

        def slow_start(c, method, params, timeout=600):
            if method == "initialize":
                raise TimeoutError(method)       # as a machine too loaded to start the agent in time
            return real(c, method, params, timeout)
        with mock.patch.object(acp.Client, "request", slow_start):
            got, j = self.outcomes(self.go("fakebench", "fake"))
        self.assertEqual(len(got), 4)            # every unit written, the run ended
        self.assertTrue(all(t.outcome == classify.SETUP_ERROR for t in got.values()), {k: t.outcome for k, t in got.items()})

    def test_a_rate_limit_is_infrastructure_not_a_miss(self):
        Echo.status = 429
        real_prompt = acp.Client.prompt

        def prompt(client, sid, text, timeout=600):
            try:
                return real_prompt(client, sid, text, timeout=timeout)
            except acp.AgentFailed as e:
                failed_prompts.append(text)
                if agent_timeout:
                    # The full run tests real timeout cleanup; here the 429 must outrank either agent failure.
                    raise acp.Timeout(str(e), client.events) from e
                raise

        for agent_timeout in (False, True):
            failed_prompts = []
            with self.subTest(agent_timeout=agent_timeout), mock.patch.object(acp.Client, "prompt", prompt):
                got, _ = self.outcomes(self.go("fakebench", "fake", dataset="responding",
                                               name=f"rate-limit-{agent_timeout}"))
                self.assertEqual(failed_prompts, ["model hi"])
                self.assertEqual(got["model"].outcome, classify.RATE_LIMITED)
                self.assertEqual(got["good"].outcome, classify.RESOLVED)     # made no call: its verdict stands

    def test_a_feature_run_drives_the_feature_through_its_port(self):
        job_dir = self.go("fakememo", "fakemem")
        got, j = self.outcomes(job_dir)
        self.assertEqual(set(got), {"q1", "q2"})
        self.assertTrue(all(t.outcome == classify.SCORED and t.score == 1.0 for t in got.values()))   # a score, no pass
        trial = [d for d in os.listdir(job_dir) if d.startswith("q1__")][0]
        with open(os.path.join(job_dir, trial, "agent", "trajectory.json")) as f:
            t = json.load(f)
        names = [s["tool_calls"][0]["function_name"] for s in t["steps"] if s.get("tool_calls")]
        self.assertEqual(names, ["fakemem.add"] * 3 + ["fakemem.recall"])
        self.assertIn("green", t["steps"][-1]["message"])

    def test_a_pair_that_does_not_fit_is_refused_before_anything_starts(self):
        with self.assertRaisesRegex(run.Refused, "takes a capability"):
            self.go("fakememo", "fake")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_an_agent_run_needs_an_explicit_yes_to_running_unsandboxed(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        with self.assertRaisesRegex(run.Refused, "not a sandbox"):
            run.Run(b, a, model="m", provider="custom", env={"MODEL_API_URL": "http://127.0.0.1:9/v1"})

    def test_a_driver_that_ended_is_started_again_for_the_next_unit(self):
        """A request with no reply in its time closes the driver (a reader
        that retries without end): the worker's next unit gets a new one."""
        b = definitions.get("benchmark", "fakebench", ROOT)
        o = definitions.get("agent", "fake", ROOT)
        r = run.Run(b, o, model="m-1", provider="custom", env=self.env, unsandboxed=True, jobs_root=self.tmp)
        r.up, r.data = None, None                    # what ready() finds before the first unit
        try:
            first = r.driver()
            self.assertIs(r.driver(), first)
            first.request("units", dataset=b["datasets"][0]["id"])
            first.close()
            again = r.driver()
            self.assertIsNot(again, first)
            self.assertTrue(again.request("units", dataset=b["datasets"][0]["id"])["units"])
        finally:
            for d in r.drivers:
                d.close()

    def test_a_judge_route_is_on_the_units_relay(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        o = definitions.get("agent", "fake", ROOT)
        r = run.Run(b, o, model="m-1", provider="custom", env=self.env, unsandboxed=True, jobs_root=self.tmp)
        r.judge_model = "the-judge"
        rl = r.unit_relay("u0003", os.path.join(self.tmp, "t"), {})
        try:
            self.assertEqual(rl.models, {})                      # set when the score asks for it, on this relay
            rl.url("u0003-judge", model="the-judge")
            self.assertEqual(rl.models, {"u0003-judge": "the-judge"})
            self.assertTrue(os.path.isdir(os.path.join(self.tmp, "t", ".work", "relay")))
        finally:
            rl.stop()

    def test_the_official_judge_and_simulator_are_named_as_the_provider_serves_them(self):
        b = dict(definitions.get("benchmark", "fakebench", ROOT), simulator={"model": "gpt-4.1-2025-04-14", "says": "a customer"})
        b["score"] = dict(b["score"], judge={"model": "gpt-4o-2024-08-06", "says": "a judge"})
        o = definitions.get("agent", "fake", ROOT)
        env = dict(self.env, OPENROUTER_API_KEY="k")
        r = run.Run(b, o, model="openai/gpt-4o-mini", provider="openrouter", env=env, unsandboxed=True, jobs_root=self.tmp)
        self.assertEqual((r.judge_model, r.simulator_model), ("openai/gpt-4o-2024-08-06", "openai/gpt-4.1-2025-04-14"))
        typed = run.Run(b, o, model="m", provider="openrouter", env=env, unsandboxed=True, jobs_root=self.tmp,
                        judge_model="anthropic/claude-x", simulator_model="openai/gpt-5")
        self.assertEqual((typed.judge_model, typed.simulator_model), ("anthropic/claude-x", "openai/gpt-5"))   # what was typed goes as typed

    def test_a_judge_refused_by_its_provider_is_the_units_infrastructure_not_a_miss(self):
        here = os.path.join(self.tmp, "judged")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(JUDGED_DRIVER)
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here, driver={"command": ["python3", "{here}/driver.py"]})
        b["score"] = dict(b["score"], judge={"model": "the-judge", "says": "a judge"})
        Echo.refused = "the-judge"
        job_dir = run.run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                          concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, proving=True, limit=1)
        (t,) = parse.parse_job(job_dir).trials
        self.assertEqual(t.exception_type, "RateLimitError")
        self.assertIn(t.outcome, classify.INFRA_OUTCOMES)

    def test_a_driver_writes_beside_the_job_never_into_its_catalogue_folder(self):
        """tau2 writes its embeddings cache relative to its working
        directory: from the catalogue folder that wrote into the package, or
        into the runner snapshot, which then refused every later run."""
        here = os.path.join(self.tmp, "bench")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(CWD_DRIVER)
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here, driver={"command": ["python3", "{here}/driver.py"]})
        job_dir = run.run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                          concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, proving=True, limit=1)
        self.assertEqual(os.listdir(here), ["driver.py"])
        self.assertTrue(os.path.isfile(os.path.join(job_dir, ".work", "driver", "data", ".cache")))

    def test_a_simulated_user_opens_the_conversation_and_speaks_through_its_route(self):
        """A benchmark whose user a model plays (``simulator``): its first
        turn, asked with nothing said, follows setup's prompt; every turn is
        given the simulator's route, recorded beside the unit's calls."""
        here = os.path.join(self.tmp, "chat")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(CHAT_DRIVER)
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here,
                 needs=["agent.task/v1", "agent.conversation/v1"], driver={"command": ["python3", "{here}/driver.py"]},
                 simulator={"model": "the-user", "says": "a customer"})
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        o = definitions.get("agent", "fake", ROOT)
        job_dir = run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env, say=lambda s: None,
                          unsandboxed=True, proving=True, limit=1, simulator_model="another-user")
        trial = os.path.join(job_dir, [t for t in os.listdir(job_dir) if "__u" in t][0])
        with open(os.path.join(trial, "verifier", "score.json")) as f:
            detail = json.load(f)["detail"]
        # the fake agent says each prompt back upper-cased: the first is setup's, then the user's opening
        self.assertEqual(detail["said"][0], "INTRO\n\nYOU SAID: OPEN")
        self.assertEqual(detail["routes"], ["another-user"] * 2)
        with open(os.path.join(job_dir, "events.jsonl")) as f:
            clocks = [e for e in map(json.loads, f) if e["event"] == "trial.phase" and e["phase"] == "agent_run"]
        self.assertEqual(len(clocks), len(detail["said"]))                   # the limit is each turn's: its clock starts with it
        self.assertTrue(all(e["timeout_sec"] for e in clocks))
        with open(os.path.join(trial, ".work", "relay", "u0000-simulator.jsonl")) as f:
            self.assertEqual([json.loads(l)["model"] for l in f], ["another-user"] * 2)
        self.assertFalse(os.path.exists(os.path.join(trial, ".work", "relay", "u0000.jsonl")))   # not the agent's calls
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["simulator"], "another-user")
        # a control holds no conversation: no route is asked, no key is needed
        b["controls"] = {"nop": {"source": "nullius", "from": "nothing said"}}
        job_dir = run.run(b, definitions.control(b, "nop"), model="", provider="custom", jobs_root=self.tmp, env={},
                          say=lambda s: None, limit=1, name="nop")
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertNotIn("simulator", json.load(f)["standard"])

    def test_a_conversation_the_upstream_starts_again_is_a_new_session(self):
        """A driver's ``turn`` may answer ``restart`` (its upstream retried the
        conversation): the agent gets a new session, told setup's prompt again
        with the user's first message."""
        here = os.path.join(self.tmp, "again")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(CHAT_DRIVER.replace('''    return {"message": ask(simulator, "more")}''',
                                        '''    return {"message": ask(simulator, "again"), "restart": True}'''))
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here,
                 needs=["agent.task/v1", "agent.conversation/v1"], driver={"command": ["python3", "{here}/driver.py"]},
                 simulator={"model": "the-user", "says": "a customer"})
        o = definitions.get("agent", "fake", ROOT)
        with mock.patch.object(acp.Client, "new_session", autospec=True, side_effect=acp.Client.new_session) as opened:
            job_dir = run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, concurrency=1, env=self.env,
                              say=lambda s: None, unsandboxed=True, proving=True, limit=1)
        trial = os.path.join(job_dir, [t for t in os.listdir(job_dir) if "__u" in t][0])
        with open(os.path.join(trial, "verifier", "score.json")) as f:
            detail = json.load(f)["detail"]
        self.assertEqual(detail["said"], ["INTRO\n\nYOU SAID: OPEN", "INTRO\n\nYOU SAID: AGAIN"])
        self.assertEqual(opened.call_count, 2)

    def test_a_simulator_is_only_for_a_conversation(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        self.assertIn("simulator: only a benchmark that needs agent.conversation/v1",
                      " ".join(definitions.problems(dict(b, simulator={"model": "u", "says": "x"}), "benchmark")))
        self.assertIn("simulator: {model",
                      " ".join(definitions.problems(dict(b, simulator={"model": "a user"}), "benchmark")))

    def test_the_docker_backend_needs_no_yes_but_a_usable_docker(self):
        from unittest import mock
        from nullius.standard import container
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}
        with mock.patch.object(container, "unusable", return_value="docker is not installed on this machine"):
            with self.assertRaisesRegex(run.Refused, "docker backend cannot run here: docker is not installed"):
                run.Run(b, a, model="m", provider="custom", env=env, backend="docker")
        with mock.patch.object(container, "unusable", return_value=None):
            self.assertEqual(run.Run(b, a, model="m", provider="custom", env=env, backend="docker").backend, "docker")
        with self.assertRaisesRegex(run.Refused, "backend 'vm'"):
            run.Run(b, a, model="m", provider="custom", env=env, unsandboxed=True, backend="vm")

    def test_a_missing_key_is_refused(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        with self.assertRaisesRegex(run.Refused, "OPENAI_API_KEY is not set"):
            run.Run(b, a, model="m", provider="openai", env={}, unsandboxed=True)


if __name__ == "__main__":
    unittest.main()
