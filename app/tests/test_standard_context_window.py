"""An agent's last call over the model's window ends the unit as ContextWindowExceededError, whatever followed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius.runs import classify, parse
from nullius.standard import definitions, install, job, relay, retries, run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
WORDS = "the gated task's own sentence"
OPENROUTER = f"HTTP 400: This endpoint's maximum context length is 1000 tokens. However, you requested 1200: {WORDS}"
ANTHROPIC = f"HTTP 400: prompt is too long: 1200 tokens > 1000 maximum: {WORDS}"
EMPTY = "HTTP 400: messages: at least one message is required"
AGENT_OK = {"status": 200}
OVERFLOW = {"status": 400, "error": OPENROUTER}
SIDE_EMPTY = {"status": 400, "error": EMPTY, "side": True}
ANSWERING = '''from nullius.standard.jsonl import serve
def answer(unit, dir, setup, model, base_url, key, **_):
    raise RuntimeError("the upstream's runner ended on its simulated user's error")
serve({"units": lambda dataset, setup=None: {"units": ["a"]}, "setup": lambda unit, dir: {"prompt": "hi"},
       "answer": answer, "score": lambda unit, dir, transcript, answer, **_: {"reward": 0.0, "detail": {}}})
'''
STOPPED = ANSWERING.replace('raise RuntimeError("the upstream\'s runner ended on its simulated user\'s error")',
                            'return {"answer": "DONE"}').replace('{"reward": 0.0, "detail": {}}',
                                                                '{"reward": 0.5, "detail": {}, "stopped": "context window"}')
UNREAD = '''from nullius.standard.jsonl import serve
class VerifierError(RuntimeError):
    pass
def score(unit, dir, **_):
    raise VerifierError("the evaluation did not read the VM")
serve({"units": lambda dataset, setup=None: {"units": ["a"]}, "setup": lambda unit, dir: {"prompt": "hi"},
       "answer": lambda unit, dir, setup, model, base_url, key, **_: {"answer": "DONE"}, "score": score})
'''
SETUPS = {"original": {"summary": "The paper's own pipeline.", "models": ["paper-1"], "default": "paper-1",
                       "providers": {"custom": ""}}}


class Failure(unittest.TestCase):
    def test_the_agents_overflow_outranks_the_side_call_after_it(self):
        calls = [AGENT_OK, OVERFLOW, OVERFLOW, OVERFLOW, SIDE_EMPTY]
        self.assertEqual(relay.failure(calls, agent=True), (relay.CONTEXT_ERROR, OPENROUTER))
        self.assertFalse(retries.retryable({"exception_info": {"exception_type": relay.CONTEXT_ERROR}}))
        exc, said = relay.failure(calls, words=False, agent=True)
        self.assertEqual(exc, "ContextWindowExceededError")
        self.assertIn("answered 400", said)
        self.assertIn("context window", said)
        self.assertNotIn(WORDS, said)
        self.assertNotIn("maximum context length", said)

    def test_anthropics_words_and_a_later_rate_limit(self):
        calls = [AGENT_OK, {"status": 400, "error": ANTHROPIC}, {"status": 429, "error": "slow down", "side": True}]
        self.assertEqual(relay.failure(calls, words=False, agent=True)[0], relay.CONTEXT_ERROR)

    def test_the_agents_overflow_as_the_last_call(self):
        self.assertEqual(relay.failure([AGENT_OK, OVERFLOW], words=False, agent=True)[0], relay.CONTEXT_ERROR)

    def test_an_overflow_the_agent_recovered_from_is_not_the_units(self):
        self.assertIsNone(relay.failure([OVERFLOW, AGENT_OK], agent=True))

    def test_a_side_call_that_went_through_after_it_does_not_recover_the_agent(self):
        self.assertEqual(relay.failure([AGENT_OK, OVERFLOW, {"status": 200, "side": True}], agent=True)[0], relay.CONTEXT_ERROR)
        limited = [AGENT_OK, {"status": 429, "error": "slow down"}, {"status": 502, "error": "x"},
                   {"status": 429, "error": "slow down"}, {"status": 200, "side": True}]
        self.assertEqual(relay.failure(limited, words=False, agent=True)[0], "RateLimitError")
        self.assertIsNone(relay.failure(limited[1:3] + [AGENT_OK, {"status": 200, "side": True}], agent=True))
        self.assertIsNone(relay.failure(limited, words=False))
        calls = [AGENT_OK, OVERFLOW, {"status": 200, "side": True}, {"status": 429, "error": "slow down", "side": True}]
        self.assertEqual(relay.failure(calls, words=False, agent=True)[0], relay.CONTEXT_ERROR)
        self.assertEqual(relay.failure(calls, words=False)[0], "RateLimitError")
        calls = [{"status": 429, "error": "x"}, {"status": 502, "error": "x", "side": True}, {"status": 200, "side": True}]
        self.assertEqual(relay.failure(calls, words=False, agent=True)[0], "RateLimitError")

    def test_the_calls_are_read_in_the_order_they_began(self):
        abandoned = {"status": 502, "error": "x", "t0": 1}
        self.assertIsNone(relay.failure([dict(AGENT_OK, t0=2), abandoned], agent=True))
        self.assertEqual(relay.failure([{"status": 429, "error": "x", "t0": 2}, dict(AGENT_OK, t0=1)])[0], "RateLimitError")
        self.assertEqual(relay.failure([dict(AGENT_OK, t0=1), {"status": 429, "error": "x"}], agent=True)[0], "RateLimitError")

    def test_another_400_of_the_agent_stays_the_infrastructures(self):
        calls = [AGENT_OK, {"status": 400, "error": "HTTP 400: invalid image"}, SIDE_EMPTY]
        self.assertEqual(relay.failure(calls, words=False, agent=True)[0], "ApiError")

    def test_a_side_calls_overflow_is_not_the_agents(self):
        calls = [AGENT_OK, dict(OVERFLOW, side=True)]
        self.assertEqual(relay.failure(calls, words=False, agent=True)[0], "ApiError")

    def test_a_judges_or_simulators_route_is_never_the_agents_context(self):
        calls = [AGENT_OK, OVERFLOW]
        self.assertEqual(relay.failure(calls, words=False)[0], "ApiError")
        self.assertEqual(relay.failure(calls)[0], "ApiError")

    def test_only_a_400_says_so(self):
        self.assertTrue(relay.overflowed(OVERFLOW))
        self.assertTrue(relay.overflowed({"status": 400, "error": ANTHROPIC}))
        self.assertTrue(relay.overflowed({"status": 400, "error": "HTTP 400: input length and `max_tokens` exceed context "
                                                                  "limit: 196000 + 8192 > 200000"}))
        self.assertTrue(relay.overflowed({"status": 400, "error": "HTTP 400: The input token count (1200) exceeds the "
                                                                  "maximum number of tokens allowed (1000)."}))
        self.assertFalse(relay.overflowed(dict(OVERFLOW, status=500)))
        self.assertFalse(relay.overflowed({"status": 400, "error": EMPTY}))
        self.assertFalse(relay.overflowed({"status": 400}))

    def test_it_is_the_agents_result_not_the_infrastructure(self):
        self.assertEqual(classify.from_verdict(relay.CONTEXT_ERROR, None), classify.AGENT_ERROR)
        self.assertNotIn(classify.from_verdict(relay.CONTEXT_ERROR, None), classify.INFRA_OUTCOMES)
        fed = job.feed_outcome("score", None, (relay.CONTEXT_ERROR, "said"))
        self.assertEqual((fed["outcome"], fed["exc"]), ("agent_error", relay.CONTEXT_ERROR))


class Calls:
    """The agent's calls ending on an overflow, then a refused side call."""
    def __init__(self, own, dir=os.devnull):
        self.own, self.dir = own, dir

    def calls(self, tag):
        return [] if tag.endswith(("-judge", "-simulator")) else list(self.own)

    def url(self, tag, model=None):
        return "http://127.0.0.1:9/" + tag + "/v1"

    def stop(self):
        pass


class Unit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        here = os.path.join(self.tmp, "paper")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(ANSWERING)
        self.b = dict(definitions.get("benchmark", "fakebench", FIX), _here=here, setups=SETUPS,
                      driver={"command": ["python3", "{here}/driver.py"]})

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ran(self, own, gated=True, record=os.devnull):
        b = dict(self.b, **({"gated": True} if gated else {}))
        o, why = definitions.setup(b, "original", "paper-1", "custom")
        self.assertIsNotNone(o, why)
        jobs = os.path.join(self.tmp, f"jobs-{len(os.listdir(self.tmp))}")
        with patch.object(run.Run, "unit_relay", lambda r, *a, **k: Calls(own, record)):
            job_dir = run.run(b, o, model="paper-1", provider="custom", jobs_root=jobs, say=lambda s: None,
                              env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        name = next(t for t in os.listdir(job_dir) if "__u" in t)
        with open(os.path.join(job_dir, name, "result.json")) as f:
            return json.load(f).get("exception_info"), parse.parse_job(job_dir)

    def test_the_unit_is_the_agents_context_said_by_status_alone(self):
        said, ran = self.ran([AGENT_OK, OVERFLOW, OVERFLOW, OVERFLOW, SIDE_EMPTY])
        self.assertEqual(said["exception_type"], "ContextWindowExceededError")
        self.assertIn("answered 400", said["exception_message"])
        self.assertNotIn(WORDS, said["exception_message"])
        (trial,) = ran.trials
        self.assertEqual(trial.outcome, classify.AGENT_ERROR)
        self.assertFalse(trial.is_infra)
        self.assertEqual(ran.n_infra, 0)

    def test_a_benchmark_not_gated_keeps_the_providers_words(self):
        said, _ = self.ran([AGENT_OK, OVERFLOW, SIDE_EMPTY], gated=False)
        self.assertEqual(said["exception_type"], "ContextWindowExceededError")
        self.assertIn(WORDS, said["exception_message"])

    def test_an_episode_the_driver_stopped_and_scored_keeps_its_score(self):
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(STOPPED)
        said, ran = self.ran([AGENT_OK, OVERFLOW, OVERFLOW, OVERFLOW])
        self.assertIsNone(said)
        self.assertEqual((ran.trials[0].outcome, ran.trials[0].reward), (classify.FAILED_VERIFICATION, 0.5))
        said, _ = self.ran([AGENT_OK, OVERFLOW, SIDE_EMPTY, {"status": 429, "error": "slow down"}])
        self.assertEqual(said["exception_type"], "RateLimitError")

    def test_an_episode_a_guard_stopped_and_the_driver_scored_keeps_its_score_after_a_429(self):
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(STOPPED.replace('"stopped": "context window"', '"stopped": "batch cap"'))
        record = os.path.join(self.tmp, "record")
        os.makedirs(record)
        with open(os.path.join(record, relay.STOP), "w") as f:
            f.write("batch cap\n")
        limited = {"status": 429, "error": "slow down"}
        for calls in ([AGENT_OK, limited], [AGENT_OK, limited, {"status": 502, "error": "down", "side": True}]):
            said, ran = self.ran(calls, record=record)
            self.assertIsNone(said, calls)
            self.assertEqual((ran.trials[0].outcome, ran.trials[0].reward), (classify.FAILED_VERIFICATION, 0.5))
            with open(os.path.join(ran.trials[0].dir, "result.json")) as f:
                self.assertEqual(json.load(f)["verifier_result"]["stopped"], "batch cap")
        said, _ = self.ran([AGENT_OK, limited])
        self.assertEqual(said["exception_type"], "RateLimitError")
        said, _ = self.ran([AGENT_OK, dict(limited, error_code="insufficient_quota")], record=record)
        self.assertEqual(said["exception_type"], "ApiUsageLimitError")
        for own in (AGENT_OK, {"status": 401, "error": "no"}, {"status": 502, "error": "down"}):
            said, _ = self.ran([own, dict(limited, side=True)], record=record)
            self.assertEqual(said["exception_type"], "RateLimitError", own)
        with open(os.path.join(record, relay.STOP), "w") as f:
            f.write("context window\n")
        self.assertEqual(self.ran([AGENT_OK, limited], record=record)[0]["exception_type"], "RateLimitError")
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(STOPPED)
        with open(os.path.join(record, relay.STOP), "a") as f:
            f.write("batch cap\n")
        self.assertEqual(self.ran([AGENT_OK, limited], record=record)[0]["exception_type"], "RateLimitError")
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(STOPPED.replace(', "stopped": "context window"', ""))
        self.assertEqual(self.ran([AGENT_OK, limited], record=record)[0]["exception_type"], "RateLimitError")

    def test_an_episode_the_overflow_ended_whose_evaluation_failed_is_the_checks(self):
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(UNREAD)
        said, ran = self.ran([AGENT_OK, OVERFLOW])
        self.assertEqual(said["exception_type"], "VerifierError")
        self.assertIn("the evaluation did not read the VM", said["exception_message"])
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write(UNREAD.replace("raise VerifierError", "raise RuntimeError"))
        said, _ = self.ran([AGENT_OK, OVERFLOW])
        self.assertEqual(said["exception_type"], "ContextWindowExceededError")

    def test_a_unit_run_again_under_its_jobs_name_starts_with_no_earlier_record_and_keeps_a_new_stop(self):
        o, _ = definitions.setup(self.b, "original", "paper-1", "custom")
        jobs = os.path.join(self.tmp, "jobs")
        record = os.path.join(jobs, "again", "a__u0000", ".work", "relay")
        os.makedirs(record)
        os.makedirs(os.path.join(jobs, "again", "a__u0000", "verifier"))
        os.makedirs(os.path.join(jobs, "again", "a__u0000", "agent"))
        for name, text in (("u0000.jsonl", json.dumps({"status": 429, "error": "slow down"}) + "\n"), (relay.STOP, "batch cap\n"),
                           ("../../verifier/score.json", "{}"), ("../../result.json", "{}"), ("../../agent/old.txt", "")):
            with open(os.path.join(record, name), "w") as f:
                f.write(text)
        seen = []

        def phase(r, name, unit, phase, timeout=None):
            if phase == "env_setup":
                os.makedirs(record, exist_ok=True)
                with open(os.path.join(record, relay.STOP), "w") as f:
                    f.write("key cap\n")

        def unit_relay(r, tag, trial, *a, **k):
            with open(os.path.join(trial, ".work", "relay", relay.STOP)) as f:
                seen.append((sorted(os.listdir(os.path.join(trial, ".work", "relay"))), f.read()))
            return Calls([AGENT_OK])

        with patch.object(run.Run, "phase", phase), patch.object(run.Run, "unit_relay", unit_relay):
            run.run(self.b, o, model="paper-1", provider="custom", jobs_root=jobs, name="again", say=lambda s: None,
                    env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        self.assertEqual(seen, [([relay.STOP], "key cap\n")])
        (earlier,) = [d for d in os.listdir(os.path.dirname(record)) if d.startswith("earlier.")]
        earlier = os.path.join(os.path.dirname(record), earlier)
        with open(os.path.join(earlier, "relay", relay.STOP)) as f:
            self.assertEqual(f.read(), "batch cap\n")
        for part in (("verifier", "score.json"), ("result.json",), ("agent", "old.txt")):
            self.assertTrue(os.path.isfile(os.path.join(earlier, *part)))
        self.assertFalse(os.path.exists(os.path.join(jobs, "again", "a__u0000", "verifier", "score.json")))

    def test_a_run_under_a_job_name_another_run_holds_is_refused_before_it_writes(self):
        o, _ = definitions.setup(self.b, "original", "paper-1", "custom")
        jobs = os.path.join(self.tmp, "jobs")
        with patch.object(run.Run, "unit_relay", lambda r, *a, **k: Calls([AGENT_OK])):
            run.run(self.b, o, model="paper-1", provider="custom", jobs_root=jobs, name="again", say=lambda s: None,
                    env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        job_dir = os.path.join(jobs, "again")
        lck = os.path.join(job_dir, ".work", "job.lck")
        tried = subprocess.run([sys.executable, "-c", "import fcntl, sys\nfcntl.flock(open(sys.argv[1], 'a'), fcntl.LOCK_EX | fcntl.LOCK_NB)", lck],
                               capture_output=True)
        self.assertNotEqual(tried.returncode, 0)
        run.HELD.pop(lck).close()
        before = {os.path.join(d, f): open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(job_dir) for f in fs} | {d: None for d, _, _ in os.walk(job_dir)}
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a')\n"
                                   "fcntl.flock(f, fcntl.LOCK_EX)\nprint(flush=True)\ntime.sleep(30)",
                                   lck], stdout=subprocess.PIPE)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        with patch.object(run.Run, "unit_relay", lambda r, *a, **k: Calls([AGENT_OK])), \
                self.assertRaisesRegex(run.Refused, "another run under the job name again is running"):
            run.run(self.b, o, model="paper-1", provider="custom", jobs_root=jobs, name="again", say=lambda s: None,
                    env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        after = {os.path.join(d, f): open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(job_dir) for f in fs} | {d: None for d, _, _ in os.walk(job_dir)}
        self.assertEqual(after, before)

    def test_a_second_run_in_the_same_process_under_a_live_job_name_is_refused(self):
        o, _ = definitions.setup(self.b, "original", "paper-1", "custom")
        go = lambda: run.run(self.b, o, model="paper-1", provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                             name="live", say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        tried = []

        def relay(r, *a, **k):
            if not tried:
                tried.append(None)
                for again in (go, r.start):
                    try:
                        tried.append(again())
                    except run.Refused as e:
                        tried.append(e)
            return Calls([AGENT_OK])

        with patch.object(run.Run, "unit_relay", relay):
            go()
            self.assertEqual(len(tried), 3)
            for refused in tried[1:]:
                self.assertIsInstance(refused, run.Refused)
                self.assertIn("another run under the job name live is running", str(refused))
            go()

    def test_a_side_call_failing_after_the_agents_success_stays_the_infrastructures(self):
        said, ran = self.ran([AGENT_OK, SIDE_EMPTY])
        self.assertEqual(said["exception_type"], "ApiError")
        self.assertEqual(ran.trials[0].outcome, classify.API_ERROR)


if __name__ == "__main__":
    unittest.main()
