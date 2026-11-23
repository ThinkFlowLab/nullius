"""A unit cut at a limit, scored where its benchmark says so (``limits.scored_cuts``).

Whole runs against the fakes: the scripted agent, a fake benchmark that keeps a progress file as a desktop port
does, and a local model endpoint. Each cut is scored when the benchmark declares it and an error, as before, when it
does not; a time run out under refused calls and a unit where nothing moved stay errors; the record says what ended
the agent's work, and a unit no limit cut is written as before."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from nullius.runs import classify, parse
from nullius.standard.calls import relay
from nullius.standard.contract import definitions
from nullius.standard.requirements import install
from nullius.standard.runner import pace as pacing, run
from tests.test_standard_run import CALLING_CHAT_DRIVER, ROOT, Echo

# One unit per case. Each keeps a progress file in its trial as a desktop port does: its steps, and what ended the
# benchmark's own loop (empty while it runs). The score reads it back as the benchmark's detail; ``turn``: the
# agent's turn ended first.
CUT_DRIVER = '''import json, os
from nullius.standard.protocols.jsonl import serve
CASES = {"stuck": (2, "", "hang"), "busy": (0, "", "loop"), "idle": (0, "", "hang"), "refused": (2, "", "loop"),
         "over": (1, "", "loop"), "ended": (3, "done", "hang"), "quick": (1, "", "model hi")}
def progress(dir):
    return os.path.join(os.path.dirname(dir), ".work", "count", "progress.json")
def setup(unit, dir):
    steps, ended, prompt = CASES[unit]
    os.makedirs(os.path.dirname(progress(dir)), exist_ok=True)
    with open(progress(dir), "w") as f:
        json.dump({"steps": steps, "steps_limit": 9, "ended": ended, "open": 0}, f)
    return {"prompt": prompt, "progress": os.path.join(".work", "count", "progress.json")}
def score(unit, dir, transcript, answer):
    with open(progress(dir)) as f:
        p = json.load(f)
    return {"reward": 1.0, "passed": True, "detail": {"steps": p["steps"], "steps_limit": 9, "ended": p["ended"] or "turn"}}
serve({"units": lambda dataset: {"units": UNITS}, "setup": setup, "score": score})
'''


class ScoredCutsTest(unittest.TestCase):
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
        Echo.status, Echo.refused, Echo.limited, Echo.retry_after, Echo.listed = 200, None, 0, None, None
        self.addCleanup(setattr, pacing, "FIRST", pacing.FIRST)
        pacing.FIRST = 0.5

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bench(self, units, cuts=None, driver=None, needs=None):
        here = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write((driver or CUT_DRIVER).replace("UNITS", json.dumps(units)))
        b = dict(definitions.get("benchmark", "fakebench", ROOT), _here=here, driver={"command": ["python3", "{here}/driver.py"]})
        if needs:
            b["needs"] = needs
        if cuts is not None:
            b["limits"] = dict(b["limits"], scored_cuts=cuts)
            self.assertFalse([p for p in definitions.problems(b, "benchmark") if "scored_cuts" in p])
        return b

    def go(self, b, **kw):
        # a name of its own: two runs begun in the same second would otherwise share one job folder
        self.runs = getattr(self, "runs", 0) + 1
        kw = dict({"timeouts": {"agent": 3}, "retries": 0, "name": f"cuts-{self.runs}"}, **kw)
        job_dir = run.run(b, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom", jobs_root=self.tmp,
                          concurrency=1, env=self.env, say=lambda s: None, unsandboxed=True, proving=True, **kw)
        trials = {t.trial_name.split("__")[0]: t for t in parse.parse_job(job_dir).trials}
        records = {}
        for name in os.listdir(job_dir):
            path = os.path.join(job_dir, name, "result.json")
            if os.path.isfile(path):
                with open(path) as f:
                    records[name.split("__")[0]] = json.load(f)
        return trials, records

    def assertScored(self, t, rec, by, ended):
        self.assertEqual((t.exception_type, t.reward, t.outcome), ("", 1.0, classify.RESOLVED), t.trial_name)
        self.assertEqual(rec["standard"].get("cut"), by)
        self.assertEqual(t.ended_by, ended)

    def test_a_unit_past_the_agents_time_is_scored_when_its_benchmark_says_so_and_an_error_when_not(self):
        """Moved by its steps (``stuck``) or by its model calls (``busy``): cut at the time and scored as it stands.
        Without the declaration, the agent's timeout, as before."""
        b = self.bench(["stuck", "busy"], cuts=["time"])
        trials, records = self.go(b)
        for u in ("stuck", "busy"):
            self.assertScored(trials[u], records[u], "time", "time")
        self.assertGreater(records["busy"]["agent_result"]["n_model_calls"], 0)
        trials, records = self.go(self.bench(["stuck", "busy"]))
        for u in ("stuck", "busy"):
            self.assertEqual((trials[u].exception_type, trials[u].reward), ("AgentTimeoutError", None), u)
            self.assertNotIn("cut", records[u]["standard"])
            self.assertEqual(trials[u].ended_by, "")

    def test_a_time_run_out_under_refused_calls_and_a_unit_where_nothing_moved_stay_errors(self):
        """Every call refused by the provider: its rate limit, tried again as before. No call answered and no
        step: the agent's timeout, as before."""
        Echo.status = 429
        trials, records = self.go(self.bench(["refused", "idle"], cuts=["time", "calls", "end"]))
        self.assertEqual((trials["refused"].exception_type, trials["refused"].reward), ("RateLimitError", None))
        self.assertIn(trials["refused"].outcome, classify.INFRA_OUTCOMES)
        self.assertEqual((trials["idle"].exception_type, trials["idle"].reward), ("AgentTimeoutError", None))
        for u in ("refused", "idle"):
            self.assertNotIn("cut", records[u]["standard"])
            self.assertNotIn("retryable", records[u]["exception_info"])

    def test_a_unit_whose_agent_reached_the_cap_on_model_calls_is_scored_once_the_next_call_is_refused(self):
        """An agent that goes on calling: its turn is cut once the cap refused its next call, long before its time,
        and the unit scored; without the declaration, the cap's error, as before."""
        b = self.bench(["over"], cuts=["calls"])
        t0 = time.monotonic()
        trials, records = self.go(b, max_calls=2, timeouts={"agent": 60})
        self.assertLess(time.monotonic() - t0, 40)
        self.assertScored(trials["over"], records["over"], "calls", "calls")
        self.assertEqual(records["over"]["agent_result"]["n_model_calls"], 2)
        trials, records = self.go(self.bench(["over"]), max_calls=2, timeouts={"agent": 3})
        self.assertEqual((trials["over"].exception_type, trials["over"].reward), (relay.CALLS_ERROR, None))
        self.assertNotIn("cut", records["over"]["standard"])

    def test_an_agent_that_ends_its_turn_after_the_caps_refusal_is_scored_too(self):
        """A conversation of four turns, one call each, under a cap of three: the agent takes the refused fourth
        call in its stride and ends its turn; the unit is scored, its last answer the cap's refusal."""
        b = self.bench(["a"], cuts=["calls"], driver=CALLING_CHAT_DRIVER.replace('["a", "b"]', "UNITS"),
                       needs=["agent.task/v1", "agent.conversation/v1"])
        trials, records = self.go(b, max_calls=3, timeouts={"agent": 60})
        self.assertScored(trials["a"], records["a"], "calls", "calls")
        trials, records = self.go(self.bench(["a"], driver=CALLING_CHAT_DRIVER.replace('["a", "b"]', "UNITS"),
                                             needs=["agent.task/v1", "agent.conversation/v1"]), max_calls=3, timeouts={"agent": 60})
        self.assertEqual((trials["a"].exception_type, trials["a"].reward), (relay.CALLS_ERROR, None))

    def test_an_agent_that_goes_on_after_its_benchmarks_end_is_cut_after_the_grace_and_keeps_the_score(self):
        """The benchmark's loop has ended (done) while the agent's turn goes on: after the grace (one second here,
        a minute in a run) its turn is cut and the unit keeps the benchmark's score; without the declaration it
        runs on to its time and ends as the agent's timeout."""
        with mock.patch.object(run, "END_GRACE_SECONDS", 1.0):
            t0 = time.monotonic()
            trials, records = self.go(self.bench(["ended"], cuts=["end"]), timeouts={"agent": 60})
            self.assertLess(time.monotonic() - t0, 40)
        self.assertScored(trials["ended"], records["ended"], "end", "done")
        trials, records = self.go(self.bench(["ended"]))
        self.assertEqual((trials["ended"].exception_type, trials["ended"].reward), ("AgentTimeoutError", None))

    def test_a_unit_no_limit_cut_is_written_as_without_the_declaration(self):
        """The agent ends its turn by itself. With or without the declaration, its record matches the base
        except for the times and the job's name. The reader says the agent ended its work."""
        def plain(rec):
            out = json.loads(json.dumps(rec))
            for k in ("started_at", "finished_at"):
                out.pop(k, None)
            for block in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
                if isinstance(out.get(block), dict):
                    out[block] = sorted(out[block])
            out["standard"].pop("job")
            return out
        trials, cut_records = self.go(self.bench(["quick"], cuts=["time", "calls", "end"]), max_calls=5)
        _, base_records = self.go(self.bench(["quick"]), max_calls=5)
        # The quick unit's record as the writer wrote it before a benchmark could declare scored cuts.
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "standard", "uncut-record.json")) as f:
            expected = json.load(f)
        self.assertEqual(plain(cut_records["quick"]), expected)
        self.assertEqual(plain(base_records["quick"]), expected)
        self.assertNotIn("cut", cut_records["quick"]["standard"])
        self.assertEqual((trials["quick"].reward, trials["quick"].ended_by), (1.0, "agent"))


class CutWatchTest(unittest.TestCase):
    def test_cancellation_waits_for_the_end_grace(self):
        with tempfile.TemporaryDirectory() as trial:
            with open(os.path.join(trial, "progress.json"), "w") as f:
                json.dump({"ended": "done"}, f)
            watch = run.CutWatch(["end"], trial, None, "progress.json")
            times = iter((100.0, 159.999, 160.001))
            with mock.patch.object(run.time, "monotonic") as now:
                def tick(seconds):
                    self.assertFalse(watch.event.is_set())
                    self.assertIsNone(watch.by)
                    now.return_value = next(times)
                    return False

                with mock.patch.object(watch.done, "wait", side_effect=tick) as wait:
                    watch._run()
                self.assertEqual(wait.call_count, 3)
            self.assertTrue(watch.event.is_set())
            self.assertEqual(watch.by, "end")


class EndedByTest(unittest.TestCase):
    """What the reader says ended a trial's work, for each way one ends: from the record's cut first, then the
    benchmark's own detail."""

    def trial(self, cut=None, ended=None, standard=True, scored=True):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        res = {"task_name": "b/u", "trial_name": "u__0001", "source": "b"}
        if standard:
            res["standard"] = dict({"benchmark": "b"}, **({"cut": cut} if cut else {}))
        if scored:
            res["verifier_result"] = {"rewards": {"reward": 0.0}}
            os.makedirs(os.path.join(d, "verifier"))
            with open(os.path.join(d, "verifier", "score.json"), "w") as f:
                json.dump({"reward": 0.0, "detail": {} if ended is None else {"ended": ended}}, f)
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump(res, f)
        return parse.parse_trial("j", d)

    def test_each_way_a_unit_can_end(self):
        """Its known ends by their labels. An end the pages have no label for is said as the benchmark's detail wrote
        it, before a cut after the benchmark's end, kept whole with a short form past ENDED_CHARS; a value that is
        not text is none."""
        long = "x" * (parse.ENDED_CHARS + 1)
        cases = [((None, "turn"), "agent", "Agent"), ((None, "done"), "done", "Done"), ((None, "fail"), "fail", "Fail"),
                 ((None, "steps"), "steps", "Step limit"), (("time", "turn"), "time", "Time limit"),
                 (("calls", "turn"), "calls", "Call cap"), (("end", "steps"), "steps", "Step limit"),
                 (("end", None), "end", "Task end"), ((None, None), "", ""),
                 ((None, "guard"), "guard", "guard"), (("end", "guard"), "guard", "guard"), ((None, " its own "), "its own", "its own"),
                 (("time", "guard"), "time", "Time limit"), ((None, long), long, long), (("end", long), long, long),
                 ((None, "   "), "", ""), ((None, 7), "", ""), ((None, ["guard"]), "", "")]
        for (cut, ended), by, label in cases:
            with self.subTest(cut=cut, ended=ended):
                t = self.trial(cut, ended)
                self.assertEqual((t.ended_by, t.ended_label), (by, label))
                self.assertEqual(t.ended_preview, long[:parse.ENDED_CHARS] + "…" if label == long else label)
        self.assertEqual(self.trial(ended="done", standard=False).ended_by, "")     # not a run of the standard
        self.assertEqual(self.trial(scored=False).ended_by, "")                     # no score to read

    def test_an_unknown_end_is_redacted_before_it_is_cached(self):
        from nullius import workspace

        secret = "sk-" + "example123456789012345"
        trial = self.trial(ended=secret)
        self.assertEqual(trial.ended_by, "[REDACTED]")
        job = parse.Job("j", trial.dir, "model", "example", 1, None, None, [trial])
        ws = workspace.Workspace("test", "Test", [], "", "", os.path.join(trial.dir, "workspace"))
        path = os.path.join(ws.jobs_dir, "cached.json")
        workspace._write_cache(ws, path, "sig", "head", job)
        with open(path) as handle:
            self.assertEqual(json.load(handle)["job"]["trials"][0]["ended_by"], "[REDACTED]")

    def test_score_links_are_not_followed(self):
        for part in ("score", "verifier", "trial"):
            with self.subTest(part=part):
                t = self.trial(ended="done")
                path = {"score": os.path.join(t.dir, "verifier", "score.json"),
                        "verifier": os.path.join(t.dir, "verifier"), "trial": t.dir}[part]
                target = path + "-outside"
                os.rename(path, target)
                if part == "trial":
                    self.addCleanup(shutil.rmtree, target, ignore_errors=True)
                    self.addCleanup(os.unlink, path)
                os.symlink(target, path)
                self.assertEqual(parse.parse_trial("j", t.dir).ended_by, "")

    def test_a_score_pipe_does_not_wait_for_a_writer(self):
        t = self.trial(ended="done")
        path = os.path.join(t.dir, "verifier", "score.json")
        os.unlink(path)
        os.mkfifo(path)
        result = subprocess.run([sys.executable, "-c", "from nullius.runs import parse; import sys; "
                                 "assert parse.parse_trial('j', sys.argv[1]).ended_by == ''", t.dir],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_score_size_is_bounded(self):
        t = self.trial(ended="done")
        path = os.path.join(t.dir, "verifier", "score.json")
        raw = b'{"detail": {"ended": "done"}}'
        for size, expected in ((1024 * 1024, "done"), (1024 * 1024 + 1, "")):
            with self.subTest(size=size):
                with open(path, "wb") as f:
                    f.write(raw + b" " * (size - len(raw)))
                self.assertEqual(parse.parse_trial("j", t.dir).ended_by, expected)


if __name__ == "__main__":
    unittest.main()
