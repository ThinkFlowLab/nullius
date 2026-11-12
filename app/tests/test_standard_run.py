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
from datetime import datetime
from unittest import mock

from nullius.runs import classify, live, parse
from nullius.standard import acp, container, definitions, install, ledger, pace as pacing, relay, run

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


# the same, one unit asking far more than the others: its prompt is three thousand characters
UNEVEN_DRIVER = CALLING_DRIVER.replace('{"prompt": "model hi"}', '{"prompt": "model " + ("x" * 3000 if unit == "m5" else "hi")}')
# the same, m5's agent taking its refused call in its stride and ending its turn; each score leaves a mark in the workspace
SWALLOWING_DRIVER = (UNEVEN_DRIVER.replace('"model " + ("x" * 3000', '("swallow " if unit == "m5" else "model ") + ("x" * 3000')
                     .replace("from nullius.standard.jsonl import serve", "import os\nfrom nullius.standard.jsonl import serve")
                     .replace('"score": lambda unit, dir, transcript, answer: {',
                              '"score": lambda unit, dir, transcript, answer: open(os.path.join(dir, "scored"), "w").close() or {'))


class Echo(BaseHTTPRequestHandler):
    """A model that says back the last message, and counts its tokens."""
    status = 200
    refused = None          # a model that gets the rate limit while every other is answered
    limited = 0             # how many calls, from now, get the rate limit before the model answers again

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        limited = Echo.limited > 0
        Echo.limited -= limited
        if Echo.status != 200 or body.get("model") == Echo.refused or limited:
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
        Echo.status, Echo.refused, Echo.limited = 200, None, 0
        self.addCleanup(setattr, pacing, "FIRST", pacing.FIRST)
        pacing.FIRST = 0.5                       # a rate-limited unit's pause, short enough for a test

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
        self.assertEqual(classify.from_verdict(info["exception_type"], None), classify.CANCELLED)   # charged to no one
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

    def relay_calls(self, job_dir):
        out = []
        for root, _, files in os.walk(job_dir):
            for n in files:
                if n.endswith(".jsonl") and os.path.basename(root) == "relay":
                    with open(os.path.join(root, n)) as f:
                        out += [json.loads(line) for line in f if line.strip()]
        return out

    def test_a_run_given_a_price_estimates_its_calls_and_records_the_price(self):
        price = {"input": 1.0, "output": 10.0}
        job_dir = self.go("fakebench", "fake", prices={"m-1": price, "another": {"input": 9.0, "output": 9.0}})
        calls = self.relay_calls(job_dir)
        self.assertTrue(calls)
        for c in calls:
            self.assertAlmostEqual(c["cost_usd"], (11 * 1.0 + 3 * 10.0) / 1e6)
            self.assertEqual(c["cost_from"], "estimate")
        with open(os.path.join(job_dir, "config.json")) as f:
            # the models it calls, no other; the row is this run's, so no list is credited with it
            self.assertEqual(json.load(f)["standard"]["prices"], {"models": {"m-1": {**price, "source": "run"}}})

    def test_the_record_says_whose_each_price_is_and_names_the_list_only_for_its_own(self):
        listed, own = {"input": 1.0, "output": 10.0}, {"input": 2.0, "output": 4.0}
        held = os.path.join(self.tmp, "prices.json")

        def recorded(doc):
            with open(held, "w") as f:
                json.dump({"from": "https://example.invalid/list.json", "on": "2026-11-10", **doc}, f)
            with mock.patch.dict(os.environ, {"NULLIUS_PRICES": held}):
                job_dir = self.go("fakebench", "fake")
            with open(os.path.join(job_dir, "config.json")) as f:
                return json.load(f)["standard"]["prices"]

        self.assertEqual(recorded({"models": {"m-1": listed}}),
                         {"from": "https://example.invalid/list.json", "on": "2026-11-10", "models": {"m-1": {**listed, "source": "list"}}})
        # the user's own row over the list's: the list did not say this price, so it is not named
        self.assertEqual(recorded({"models": {"m-1": listed}, "own": {"m-1": own}}), {"models": {"m-1": {**own, "source": "own"}}})

    def calling(self, driver=CALLING_DRIVER, **kw):
        """Eight units, each one model call (11 tokens in, 3 out), one at a
        time. Input is free here and the model's own output limit is 10
        tokens at 10 USD a million: the first call is booked 0.0001, and once
        its reply is seen each call is booked what a reply is, 0.00003, its cost."""
        here = os.path.join(self.tmp, "calling")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(driver)
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here, driver={"command": ["python3", "{here}/driver.py"]})
        said = []
        kw = {"prices": {"m-1": {"input": 0.0, "output": 10.0, "max_output": 10}}, **kw}
        job_dir, failed = run.run_and_report(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                                             concurrency=1, env=self.env, say=said.append, unsandboxed=True, proving=True, **kw)
        with open(os.path.join(job_dir, "result.json")) as f:
            return job_dir, failed, json.load(f), said

    def test_a_cap_per_unit_cuts_off_the_unit_that_reaches_it_and_the_run_goes_on(self):
        """Input at 1 USD a million here: seven units book about 0.0001 a call, one a thousand tokens of prompt,
        0.001 - over its cap of 0.0005. It alone is cut off, against its agent; the seven others run."""
        prices = {"m-1": {"input": 1.0, "output": 10.0, "max_output": 10}}
        job_dir, failed, result, said = self.calling(UNEVEN_DRIVER, prices=prices, max_usd_per_unit=0.0005)
        self.assertEqual(result["budget"], {"max_usd": None, "spent_usd": 0.000287, "stopped": False, "cut_off": [], "not_run": [],
                                            "unit_max_usd": 0.0005, "over_unit": ["m5"]})
        self.assertFalse(failed)
        got, _ = self.outcomes(job_dir)
        self.assertEqual(sorted(got), [f"m{i}" for i in range(8)])               # every unit was begun: the run went on
        self.assertEqual((got["m5"].exception_type, got["m5"].outcome), (ledger.UNIT_ERROR, classify.AGENT_ERROR))
        self.assertTrue(all(got[u].outcome == classify.RESOLVED for u in got if u != "m5"))
        calls = self.relay_calls(job_dir)
        self.assertEqual(([c.get("refused") for c in calls].count("unit"), len([c for c in calls if not c.get("refused")])), (1, 7))
        self.assertTrue(any(s.startswith("1 of the tasks reached the cap of 0.0005 USD each") for s in said), said)
        self.assertFalse(any(s.startswith("stopped:") for s in said), said)
        with open(os.path.join(job_dir, "events.jsonl")) as f:
            board = live.fold([live.parse_line(line) for line in f if line.strip()])
        self.assertEqual(board.budget, {"unit_max_usd": 0.0005})
        self.assertEqual({t.trial.split("__")[0]: t.outcome for t in board.trials if t.outcome == "agent_error"}, {"m5": "agent_error"})

    def test_a_unit_cut_off_at_a_cap_is_not_scored_whatever_its_agent_made_of_the_refusal(self):
        """Its verdict would be thrown away, and a judge would be paid for it."""
        prices = {"m-1": {"input": 1.0, "output": 10.0, "max_output": 10}}
        job_dir, failed, result, said = self.calling(SWALLOWING_DRIVER, prices=prices, max_usd_per_unit=0.0005)
        got, _ = self.outcomes(job_dir)
        self.assertEqual((got["m5"].exception_type, got["m5"].outcome), (ledger.UNIT_ERROR, classify.AGENT_ERROR))
        scored = sorted(n.split("__")[0] for n in os.listdir(job_dir) if os.path.exists(os.path.join(job_dir, n, "workspace", "scored")))
        self.assertEqual(scored, ["m0", "m1", "m2", "m3", "m4", "m6", "m7"])

    def test_a_run_its_cap_stopped_before_any_verdict_is_no_failure(self):
        """A launch pauses on a failed run (\"usually the dataset or the environment\"): a cap reached is the
        limit its user set, and the launch's other jobs have shares of their own."""
        prices = {"m-1": {"input": 1.0, "output": 10.0, "max_output": 10}}
        job_dir, failed, result, said = self.calling(prices=prices, max_usd=0.00001)       # under the first request itself
        self.assertEqual((result["budget"]["stopped"], result["budget"]["cut_off"], len(result["budget"]["not_run"])), (True, ["m0"], 7))
        self.assertEqual(result["budget"]["spent_usd"], 0.0)
        self.assertFalse(failed)

    def test_a_repeated_runs_budget_names_each_attempt(self):
        job_dir, failed, result, said = self.calling(max_usd=0.00011, repeat=2)
        self.assertEqual(result["budget"]["cut_off"], ["m3 (attempt 1)"])
        self.assertEqual(result["budget"]["not_run"], [f"m{i} (attempt 1)" for i in range(4, 8)] + [f"m{i} (attempt 2)" for i in range(8)])
        self.assertEqual((result["n_total_trials"], result["stats"]["n_cancelled_trials"]), (16, 12))     # as many as the trials not run

    def test_each_threads_driver_has_a_route_of_its_own_on_the_benchmarks_relay(self):
        """A cap's refusal of a driver's own call (an option's embeddings) is the unit's that driver served, never one beside it."""
        r = run.Run.__new__(run.Run)
        r.embed_model = "embed-1"
        r.rerank_model = None
        r.bench_relay = relay.Relay("http://127.0.0.1:9/v1", "sk-real", os.path.join(self.tmp, "relay-benchmark")).start()
        self.addCleanup(r.bench_relay.stop)
        route = lambda: (r.embeddings_tag(), r.embeddings_env()["OPENAI_BASE_URL"])
        urls = {"a": route()}
        t = threading.Thread(target=lambda: urls.update(b=route()))      # a second thread while this one lives, as two units side by side
        t.start()
        t.join()
        (tag_a, url_a), (tag_b, url_b) = urls["a"], urls["b"]
        self.assertNotEqual(tag_a, tag_b)
        self.assertTrue(url_a.endswith(f"/{tag_a}/v1") and url_b.endswith(f"/{tag_b}/v1"))
        self.assertEqual({r.bench_relay.models[tag_a], r.bench_relay.models[tag_b]}, {"embed-1"})

    def test_a_spending_cap_stops_the_run_and_its_result_says_where(self):
        # 0.00003 spent by each call and as much booked for the next: with 0.00009 spent the fourth no longer fits 0.00011
        job_dir, failed, result, said = self.calling(max_usd=0.00011)
        self.assertEqual(result["budget"], {"max_usd": 0.00011, "spent_usd": 0.00009, "stopped": True,
                                            "cut_off": ["m3"], "not_run": ["m4", "m5", "m6", "m7"]})
        self.assertEqual((result["n_total_trials"], result["stats"]["n_cancelled_trials"], result["stats"]["n_errored_trials"]), (8, 4, 1))
        self.assertFalse(failed)                                             # three units reached a verdict
        got, _ = self.outcomes(job_dir)
        self.assertEqual(sorted(got), ["m0", "m1", "m2", "m3"])              # the four never begun have no trial
        self.assertEqual(got["m3"].exception_type, ledger.ERROR)
        self.assertEqual(got["m3"].outcome, classify.SPENDING_CAP)           # its own outcome, outside the rate: never a miss
        self.assertTrue(all(got[u].outcome == classify.RESOLVED for u in ("m0", "m1", "m2")))
        calls = self.relay_calls(job_dir)
        self.assertEqual([c.get("refused") for c in calls].count("cap"), 1)
        sent = [c for c in calls if not c.get("refused")]
        self.assertEqual(len(sent), 3)                                       # no call went out past the cap
        self.assertEqual(sorted(round(c["booked_usd"], 8) for c in sent), [0.00003, 0.00003, 0.0001])   # the start once, then what a reply has been
        self.assertLessEqual(sum(c["cost_usd"] for c in sent), 0.00011)
        self.assertTrue(any(s.startswith("stopped: a call did not fit the spending cap of 0.00011 USD (0.00009 spent): 1 cut off, 4 not run")
                            for s in said), said)
        # the live board is told the cap as the units start, and the unit it cut off ends there as budget
        with open(os.path.join(job_dir, "events.jsonl")) as f:
            board = live.fold([live.parse_line(line) for line in f if line.strip()])
        self.assertEqual(board.budget, {"max_usd": 0.00011})
        self.assertEqual({t.trial.split("__")[0]: t.outcome for t in board.trials if t.outcome}, {"m0": "resolved", "m1": "resolved", "m2": "resolved", "m3": "budget"})
        self.assertEqual((board.counts().get("not_run"), board.counts()["pending"]), (len(result["budget"]["not_run"]), 0))   # as the result says
        self.assert_no_key(job_dir)

    def test_a_spending_cap_the_run_stays_under_changes_nothing_but_the_result(self):
        job_dir, failed, result, said = self.calling(max_usd=1.0)
        self.assertEqual(result["budget"], {"max_usd": 1.0, "spent_usd": 0.00024, "stopped": False, "cut_off": [], "not_run": []})
        self.assertEqual(len(self.outcomes(job_dir)[0]), 8)
        self.assertIn("spent 0.00024 of the 1.00 USD cap", said)
        job_dir, _, result, _ = self.calling(name="uncapped")
        self.assertNotIn("budget", result)
        self.assertFalse(os.path.exists(os.path.join(job_dir, ".work", "ledger")))
        self.assertFalse(any("booked_usd" in c for c in self.relay_calls(job_dir)))

    def test_a_spending_cap_is_refused_without_a_price_for_each_model_or_an_amount(self):
        with self.assertRaises(run.Refused) as e:
            self.calling(max_usd=1.0, prices={"another": {"input": 1.0, "output": 1.0}})
        self.assertIn("there is none for m-1", str(e.exception))
        for bad in (0, -1, float("inf"), float("nan"), "much"):
            with self.assertRaises(run.Refused, msg=bad):
                self.calling(max_usd=bad)
        self.assertEqual([n for n in os.listdir(self.tmp) if "fakebench" in n], [])       # nothing was begun

    def test_a_run_with_no_price_has_no_cost_and_says_nothing_of_prices(self):
        job_dir = self.go("fakebench", "fake")
        calls = self.relay_calls(job_dir)
        self.assertTrue(calls)
        self.assertFalse(any("cost_usd" in c for c in calls))
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertNotIn("prices", json.load(f)["standard"])

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
                self.assertEqual(failed_prompts, ["model hi", "model hi"])    # run once more, after the others
                self.assertEqual(got["model"].outcome, classify.RATE_LIMITED)
                self.assertEqual(got["good"].outcome, classify.RESOLVED)     # made no call: its verdict stands

    def test_a_rate_limited_unit_holds_the_next_back_and_is_run_again_once(self):
        Echo.limited = 1                         # the first call is refused, every later one answered
        b = dict(definitions.get("benchmark", "fakebench", ROOT), datasets=[{"id": "responding", "units": 3}])
        job_dir = run.run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                          concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, dataset="responding")
        got, j = self.outcomes(job_dir)
        self.assertEqual(len(j.trials), 3)       # one trial per unit: the first attempt is not one of them
        self.assertEqual(got["model"].outcome, classify.FAILED_VERIFICATION)   # its second attempt counts: a verdict, no error
        (name,) = os.listdir(os.path.join(job_dir, "retried"))
        self.assertTrue(name.startswith("model__"))
        with open(os.path.join(job_dir, "retried", name, "result.json")) as f:
            first = json.load(f)                 # the first attempt, kept whole beside the job's trials
        self.assertEqual(first["exception_info"]["exception_type"], "RateLimitError")
        with open(os.path.join(job_dir, name, "result.json")) as f:
            second = json.load(f)
        ended, began = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (first["finished_at"], second["started_at"]))
        self.assertGreaterEqual((began - ended).total_seconds(), 0.4)       # the pause, before it starts again
        with open(os.path.join(job_dir, "progress.jsonl")) as f:
            said = [json.loads(line) for line in f]
        self.assertTrue(any("rate limited: no unit starts for 0.5 s" in (e.get("detail") or "") for e in said))

    def test_a_stopped_run_runs_nothing_again(self):
        Echo.status = 429
        b = dict(definitions.get("benchmark", "fakebench", ROOT), datasets=[{"id": "responding", "units": 3}])
        r = run.Run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                    concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, dataset="responding")
        real = r.unit

        def unit(*a):
            res = real(*a)
            if res and res["trial_name"].startswith("model__"):
                r._stopped.set()                 # a Stop as the rate-limited unit ends
            return res
        r.unit = unit
        job_dir = r.start()
        self.assertFalse(os.path.exists(os.path.join(job_dir, "retried")))
        got, _ = self.outcomes(job_dir)
        self.assertEqual(got["model"].outcome, classify.RATE_LIMITED)

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

    def test_selected_auxiliary_files_are_downloaded_and_given_only_to_the_driver(self):
        import hashlib
        from unittest import mock
        from nullius.standard import install
        b = definitions.get("benchmark", "fakebench", ROOT)
        b["files"] = {"keys": {"url": "https://example.org/keys.json", "sha256": hashlib.sha256(b"{}").hexdigest()}}
        b["options"] = {
            "retrieve": {"summary": "retrieve", "values": ["off", "on"], "default": "off"},
            "expand": {"summary": "expand", "values": ["off", "on"], "default": "on", "when": {"retrieve": ["on"]},
                       "needs": {"on": {"files": ["keys"]}}}}
        a = definitions.get("agent", "fake", ROOT)
        for active in (False, True):
            r = run.Run(b, a, model="m-1", provider="custom", env=self.env, unsandboxed=True,
                        jobs_root=self.tmp, bench_options={"retrieve": "on" if active else "off"})
            with mock.patch.object(install, "packages", return_value=""), \
                    mock.patch.object(install, "download", return_value="/checked/keys.json") as downloaded:
                r.ready()
            self.assertEqual(downloaded.call_count, int(active))
            self.assertEqual(r.file_env, {"NULLIUS_FILE_KEYS": "/checked/keys.json"} if active else {})
            r.image = "check-image"
            engine = {run.container.CHECK_ENGINE: '["podman"]'}
            with mock.patch.object(run.jsonl, "Proc") as proc, mock.patch.object(run.container, "check_env", return_value=engine):
                r.driver()
                self.assertEqual(proc.call_args.args[2].get("NULLIUS_FILE_KEYS"), "/checked/keys.json" if active else None)
                self.assertEqual(proc.call_args.args[2][run.container.CHECK_ENGINE], engine[run.container.CHECK_ENGINE])
                self.assertEqual(proc.call_args.args[2][run.container.CHECK_IMAGE], "check-image")
            if active:
                with mock.patch.object(install, "download", side_effect=install.InstallFailed("sha256 differs")):
                    with self.assertRaisesRegex(run.Refused, "sha256"):
                        r.ready()

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

    def test_on_the_docker_backend_the_agent_is_installed_in_the_base_image_whatever_this_machine(self):
        from unittest import mock
        from nullius.standard import container, install
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}
        with mock.patch.object(container, "unusable", return_value=None), \
                mock.patch.object(container, "present", return_value=True), \
                mock.patch.object(container, "base_image", return_value="nullius/base:x"):
            for backend, unsandboxed in (("docker", False), ("local", True)):
                r = run.Run(b, a, model="m", provider="custom", env=env, backend=backend, unsandboxed=unsandboxed,
                            jobs_root=os.path.join(self.tmp, "jobs"), say=lambda s: None)
                with self.subTest(backend=backend), mock.patch.object(install, "packages", return_value=self.tmp) as pk:
                    r.ready()
                    kw = pk.call_args.kwargs
                    if backend == "docker":     # the Linux base image's tools and libc, never this machine's
                        self.assertEqual(kw["image"], "nullius/base:x")
                        self.assertEqual(kw["run_in"](["uv", "venv"], "/w")[-3:], ["nullius/base:x", "uv", "venv"])
                    else:
                        self.assertEqual((kw["image"], kw["run_in"]), (None, None))

    def test_a_model_door_that_fails_to_open_still_writes_the_jobs_result(self):
        from unittest import mock
        from nullius.standard import container
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        env = {"MODEL_API_URL": "http://127.0.0.1:9/v1"}
        with mock.patch.object(container, "unusable", return_value=None), mock.patch.object(container, "sweep"), \
                mock.patch.object(container, "present", return_value=True), \
                mock.patch.object(container, "base_image", return_value="img"), \
                mock.patch.object(container.Loopback, "start", side_effect=OSError("cannot bind")):
            with self.assertRaisesRegex(OSError, "cannot bind"):
                run.run(b, a, model="m", provider="custom", env=env, backend="docker", jobs_root=os.path.join(self.tmp, "jobs"),
                        say=lambda s: None, limit=1)
        (job,) = os.listdir(os.path.join(self.tmp, "jobs"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "jobs", job, "result.json")))

    def test_a_missing_key_is_refused(self):
        b = definitions.get("benchmark", "fakebench", ROOT)
        a = definitions.get("agent", "fake", ROOT)
        with self.assertRaisesRegex(run.Refused, "OPENAI_API_KEY is not set"):
            run.Run(b, a, model="m", provider="openai", env={}, unsandboxed=True)


if __name__ == "__main__":
    unittest.main()
