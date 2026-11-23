"""Chosen tasks of an ended job run again in that job (``standard/runner/rerun.py``),
against the fake benchmark and agent: the new try takes the trial's place,
the earlier tries stay readable, the job's result is recomputed and each
re-run recorded. No key, no network: the provider is a local fake endpoint."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nullius.harbor import feed
from nullius.runs import classify, live, parse
from nullius.standard.calls import ledger
from nullius.standard.contract import definitions
from nullius.standard.protocols import acp
from nullius.standard.requirements import install
from nullius.standard.runner import job, merge, pace as pacing, rerun, run
from tests import test_standard_run as base

ROOT = base.ROOT
UNITS = {"name": "units.txt", "sha256": "0" * 64, "units": ["good", "wrong", "model"]}
PRICES = {"m-1": {"input": 1.0, "output": 10.0, "max_output": 10}}     # a call: 11 in, 3 out, 0.000041 USD
CALL = 0.000041
usd = rerun.ledgers.usd


def digest(root) -> dict:
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def said(text):
    """The fake agent's prompt, turned into another for the re-run's tries (``acp.Client.prompt``)."""
    real = acp.Client.prompt

    def prompt(client, sid, given, timeout=600):
        for want in text.get(given, [given]):
            out = real(client, sid, want, timeout=timeout)
        return out
    return mock.patch.object(acp.Client, "prompt", prompt)


class RerunRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "catalogue"
        dest = self.root / "benchmarks/fakebench"
        shutil.copytree(Path(ROOT) / "benchmarks/fakebench", dest)
        spec = json.loads((dest / "standard.json").read_text())
        spec["controls"] = {"nop": {"source": "nullius", "from": "Empty answer."}}
        (dest / "standard.json").write_text(json.dumps(spec))
        self.bench = definitions.get("benchmark", "fakebench", str(self.root))
        self.other = definitions.control(self.bench, "nop")
        self.enterContext(mock.patch.object(install, "HOME", str(self.tmp / "installs")))

    def job(self):
        return Path(run.run(self.bench, self.other, model="", provider="custom", jobs_root=str(self.tmp),
                            name="job", concurrency=1, units_file=dict(UNITS, units=["good", "wrong"]),
                            env={}, say=lambda s: None))

    def test_a_job_never_run_again_keeps_the_result_a_run_writes(self):
        # Freeze the clock and transport. The runner still schedules and writes every trial.
        with mock.patch.object(run.time, "time", return_value=1767225600.0), \
             mock.patch.object(run.Run, "known_models"), \
             mock.patch.object(run.Run, "unit_relay", return_value=None), \
             mock.patch.object(run.Run, "drive_agent", return_value=("", None, {})), \
             mock.patch.object(run.Run, "benchmark_calls", return_value=[{"status": 200, "reply": {}}]):
            path = Path(run.run(self.bench, definitions.get("agent", "fake", ROOT), model="m-1", provider="custom",
                                jobs_root=str(self.tmp), name="job", units_file=UNITS, prices=PRICES, concurrency=2,
                                env={"MODEL_API_URL": "http://127.0.0.1:1/v1", "MODEL_API_KEY": "fake"},
                                unsandboxed=True, say=lambda s: None))
        expected = Path(__file__).parent / "fixtures/standard/ordinary-result.json"
        self.assertEqual(json.loads((path / "result.json").read_text()), json.loads(expected.read_text()))
        self.assertFalse((path / "retried").exists())

    def test_a_linked_archive_is_refused_before_any_trial_moves(self):
        path = self.job()
        outside = self.tmp / "outside"
        outside.mkdir()
        (path / "retried").symlink_to(outside, target_is_directory=True)
        before = digest(path)
        for exists in (True, False):
            with self.subTest(target_exists=exists):
                if not exists:
                    outside.rmdir()
                with self.assertRaisesRegex(run.Refused, "retried.*link"):
                    rerun.rerun(str(path), ["good", "wrong"], root=str(self.root), env={}, say=lambda s: None)
                self.assertEqual(digest(path), before)
                self.assertEqual(list(outside.iterdir()) if outside.exists() else [], [])
                self.assertEqual(outside.exists(), exists)
                self.assertTrue((path / "retried").is_symlink())

    def test_a_no_result_marker_replaces_links_without_writing_their_target(self):
        path = self.job()
        outside = self.tmp / "evidence.bin"
        outside.write_bytes(b"\x00original evidence\xff")
        for unit, link in (("good", os.link), ("wrong", os.symlink)):
            name = next(n for n, u in rerun.planned(str(path)) if u == unit)
            (path / name / "result.json").unlink()
            link(outside, path / name / rerun.NO_RESULT)
            with self.subTest(link=link.__name__):
                rerun.rerun(str(path), [unit], root=str(self.root), env={}, say=lambda s: None)
                kept = path / "retried" / f"{name}.try-1" / rerun.NO_RESULT
                self.assertEqual(outside.read_bytes(), b"\x00original evidence\xff")
                self.assertFalse(kept.is_symlink())
                self.assertEqual(kept.stat().st_mode & 0o777, 0o644)
                self.assertNotEqual(kept.stat().st_ino, outside.stat().st_ino)
                self.assertIsNone(json.loads(kept.read_text())["result"])

    def test_unfinished_counts_interruptions_before_starting_and_keeps_verdicts(self):
        path = Path(run.run(self.bench, self.other, model="", provider="custom", jobs_root=str(self.tmp),
                            name="job", concurrency=1, repeat=3, units_file=dict(UNITS, units=["good", "wrong"]),
                            env={}, say=lambda s: None))
        names = [name for name, _ in rerun.planned(str(path))]
        for name, error in zip(names[:4], (None, "NonZeroAgentExitCodeError", "SpendingCapError", run.CANCELLED)):
            result = path / name / "result.json"
            res = json.loads(result.read_text())
            res["exception_info"] = {"exception_type": error} if error else None
            res["verifier_result"] = None if error else {"rewards": {"score": 0.5}}
            result.write_text(json.dumps(res))
        shutil.rmtree(path / names[4])
        (path / names[5] / "result.json").unlink()
        verdicts = {name: digest(path / name) for name in names[:2]}
        lines = []
        ready = run.Run.ready

        def check_before_start(r):
            self.assertEqual(lines, ["unfinished: 2 no result, 1 spending cap, 1 stopped"])
            return ready(r)

        with mock.patch.object(run.Run, "ready", check_before_start):
            got = rerun.rerun(str(path), [], every_unfinished=True, root=str(self.root), env={}, say=lines.append)
        self.assertEqual(got["trials"], names[2:])
        self.assertEqual([entry["trial"] for entry in got["begun"]], [names[4]])
        self.assertEqual(got["no_result"], [names[5]])
        self.assertEqual({name: digest(path / name) for name in names[:2]}, verdicts)

    def test_stop_emits_cancellation_before_restoring_the_previous_try(self):
        path = self.job()
        name = "good__u0000"
        result = path / name / "result.json"
        original = json.loads(result.read_text())
        original["agent_result"]["cost_usd"] = 0.25
        result.write_text(json.dumps(original))
        writer = feed.Writer(str(path / "events.jsonl"), "job")
        writer.seq = rerun._last_seq(str(path / "events.jsonl"))
        writer.emit("trial.end", name, outcome="unresolved", reward=0.0, cost_usd=0.25)

        def stop(r, *args):
            r._stopped.set()
            raise OSError("the control was stopped")

        with mock.patch.object(run.Run, "control_answer", stop):
            rerun.rerun(str(path), ["good"], root=str(self.root), env={}, say=lambda s: None)
        events = live.read_events(path / "events.jsonl").events
        ends = [e.data for e in events if e.event == "trial.end" and e.trial == name]
        self.assertEqual([e["outcome"] for e in ends[-2:]], ["cancelled", "unresolved"])
        self.assertTrue(ends[-1]["back"])
        self.assertEqual(json.loads(result.read_text()), original)
        self.assertFalse((path / "retried").exists())
        board = live.fold(events).snapshot()
        card = next(t for t in board["trials"] if t["trial"] == name)
        self.assertEqual((board["state"], card["state"], card["outcome"]), ("finished", "done", "unresolved"))
        self.assertEqual((card.get("attempt", 1), board["totals"]["cost_usd"]), (1, 0.25))


class RerunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), base.Echo)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.env = {"MODEL_API_URL": f"http://127.0.0.1:{cls.up.server_address[1]}/v1", "MODEL_API_KEY": "sk-real"}

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", self._home)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        base.Echo.status, base.Echo.refused, base.Echo.limited, base.Echo.retry_after, base.Echo.listed = 200, None, 0, None, None
        self.addCleanup(setattr, pacing, "FIRST", pacing.FIRST)
        pacing.FIRST = 0.5

    def job(self, name="job", **kw) -> str:
        b, o = definitions.get("benchmark", "fakebench", ROOT), definitions.get("agent", "fake", ROOT)
        kw = dict(dict(units_file=UNITS, prices=PRICES, concurrency=2), **kw)
        return run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, env=self.env, say=lambda s: None,
                       unsandboxed=True, name=name, **kw)

    def again(self, job_dir, ids, **kw) -> dict:
        return rerun.rerun(job_dir, ids, env=self.env, say=lambda s: None, root=ROOT, **kw)

    def read(self, *path) -> dict:
        with open(os.path.join(*path)) as f:
            return json.load(f)

    def trials(self, job_dir):
        j = parse.parse_job(job_dir)
        return {t.trial_name.split("__")[0]: t for t in j.trials}, j

    def test_a_task_that_failed_then_passes(self):
        job_dir = self.job()
        before, j0 = self.trials(job_dir)
        first, config = self.read(job_dir, "result.json"), Path(job_dir, "config.json").read_bytes()
        self.assertEqual(before["model"].outcome, classify.FAILED_VERIFICATION)
        with said({"model hi": ["model hi", "write answer.txt 42"]}):     # a paid call, then the answer
            got = self.again(job_dir, ["model"], reason="the provider failed")
        name = before["model"].trial_name
        after, j1 = self.trials(job_dir)
        self.assertEqual(after["model"].outcome, classify.RESOLVED)
        self.assertEqual((j0.n_resolved, j1.n_resolved), (1, 2))
        self.assertEqual(len(j1.trials), 3)                                  # the try before is not a trial of its own
        self.assertEqual([(n, t.trial_name, t.outcome) for n, t in parse.tries(after["model"])],
                         [(1, f"{name}.try-1", classify.FAILED_VERIFICATION), (2, name, classify.RESOLVED)])
        self.assertAlmostEqual(j0.total_cost_usd, CALL)
        self.assertAlmostEqual(j1.total_cost_usd, 2 * CALL)                 # the job's cost: every try's
        res = self.read(job_dir, "result.json")
        self.assertEqual((res["started_at"], res["finished_at"], res["n_total_trials"]),
                         (first["started_at"], first["finished_at"], first["n_total_trials"]))
        self.assertEqual(res["standard"]["passed"], 2)
        self.assertEqual(res["stats"]["n_retries"], 1)
        self.assertEqual(Path(job_dir, "config.json").read_bytes(), config)      # the settings are the job's
        (entry,) = res["reruns"]
        self.assertEqual(entry["reason"], "the provider failed")
        self.assertEqual(entry["trials"], [name])
        self.assertEqual(entry["replaced"], [{"trial": name, "unit": "model", "try": 1, "was": {"reward": 0.0}, "now": {"reward": 1.0}}])
        self.assertIsNone(entry["max_usd"])
        self.assertAlmostEqual(entry["spent_usd"], CALL)
        self.assertNotIn("stopped", entry)
        self.assertEqual(got["replaced"], entry["replaced"])
        mark = self.read(job_dir, name, "result.json")["standard"]
        self.assertEqual((mark["retries"], mark["rerun"]["by"], mark["rerun"]["reason"], mark["rerun"]["replaced"]),
                         (1, "hand", "the provider failed", {"reward": 0.0}))
        self.assertEqual(mark["rerun"]["at"], entry["at"])
        self.assertNotIn("rerun", self.read(job_dir, "retried", f"{name}.try-1", "result.json")["standard"])
        # the feed goes on numbered, and its board reads the job ended, the trial at its second attempt
        with open(os.path.join(job_dir, "events.jsonl")) as f:
            seqs = [json.loads(line)["seq"] for line in f]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        board = live.load(live.feed_path(job_dir)).snapshot()
        self.assertEqual(board["state"], "finished")
        card = next(t for t in board["trials"] if t["trial"] == name)
        self.assertEqual((card["outcome"], card["attempt"], card["retried"]), ("resolved", 2, ["unresolved"]))
        self.assertFalse(rerun.running(job_dir))

    def test_a_task_that_passed_then_fails(self):
        job_dir = self.job()
        before, j0 = self.trials(job_dir)
        with said({"write answer.txt 42": ["write answer.txt 41"]}):
            self.again(job_dir, ["good"])
        after, j1 = self.trials(job_dir)
        self.assertEqual((before["good"].outcome, after["good"].outcome), (classify.RESOLVED, classify.FAILED_VERIFICATION))
        self.assertEqual((j0.n_resolved, j1.n_resolved), (1, 0))
        res = self.read(job_dir, "result.json")
        self.assertEqual(res["standard"]["passed"], 0)
        self.assertEqual(res["reruns"][0]["replaced"][0]["was"], {"reward": 1.0})
        self.assertNotIn("reason", res["reruns"][0])
        self.assertEqual([t.outcome for _, t in parse.tries(after["good"])], [classify.RESOLVED, classify.FAILED_VERIFICATION])

    def test_two_tasks_one_of_them_twice_in_a_row(self):
        job_dir = self.job()
        before, _ = self.trials(job_dir)
        with said({"write answer.txt 41": ["write answer.txt 42"]}):
            self.again(job_dir, ["good", "wrong", "good"])                  # a task named twice runs once
        self.again(job_dir, ["wrong"], reason="once more")
        after, j = self.trials(job_dir)
        wrong, good = before["wrong"].trial_name, before["good"].trial_name
        self.assertEqual(sorted(os.listdir(os.path.join(job_dir, "retried"))),
                         sorted([f"{good}.try-1", f"{wrong}.try-1", f"{wrong}.try-2"]))
        self.assertEqual([(n, t.trial_name, t.outcome) for n, t in parse.tries(after["wrong"])],
                         [(1, f"{wrong}.try-1", classify.FAILED_VERIFICATION), (2, f"{wrong}.try-2", classify.RESOLVED),
                          (3, wrong, classify.FAILED_VERIFICATION)])
        self.assertEqual([t.n_retries for _, t in parse.tries(after["wrong"])], [0, 1, 2])
        reruns = self.read(job_dir, "result.json")["reruns"]
        self.assertEqual([(e["trials"], [x["try"] for x in e["replaced"]]) for e in reruns], [([good, wrong], [1, 1]), ([wrong], [2])])
        self.assertEqual(reruns[1]["replaced"][0]["was"], {"reward": 1.0})
        self.assertEqual(self.read(job_dir, "result.json")["stats"]["n_retries"], 3)
        self.assertEqual(j.n_retries, 3)

    def test_a_stop_during_a_rerun_puts_the_try_before_back(self):
        """Stop is a SystemExit in the CLI, landing on the re-run's own thread: whether the try was waiting or
        running, the trial is the one it was, and the job reads as before."""
        job_dir = self.job()
        before, j0 = self.trials(job_dir)
        trials = {t.trial_name: digest(os.path.join(job_dir, t.trial_name)) for t in j0.trials}
        real = run.Run.unit
        for where in ("waiting", "running"):
            with self.subTest(where=where):
                def unit(r, i, u, rr=1, retry=0, where=where):
                    if retry:
                        if where == "running":       # broken off as it ran: a folder, no result
                            os.makedirs(os.path.join(r.job_dir, r.trial_name(i, u, rr), "workspace"))
                        raise SystemExit(143)
                    return real(r, i, u, rr, retry)
                with mock.patch.object(run.Run, "unit", unit), self.assertRaises(SystemExit):
                    self.again(job_dir, ["model"])
                after, j1 = self.trials(job_dir)
                self.assertEqual({t.trial_name: digest(os.path.join(job_dir, t.trial_name)) for t in j1.trials}, trials)
                self.assertFalse(os.path.exists(os.path.join(job_dir, "retried")))
                self.assertEqual([(t.trial_name, t.outcome, t.n_retries) for t in j1.trials],
                                 [(t.trial_name, t.outcome, t.n_retries) for t in j0.trials])
                self.assertAlmostEqual(j1.total_cost_usd, j0.total_cost_usd)
                self.assertEqual(self.read(job_dir, "result.json")["reruns"][-1]["replaced"], [])
                self.assertTrue(self.read(job_dir, "result.json")["reruns"][-1]["stopped"])
                self.assertFalse(rerun.running(job_dir))

    def test_a_stop_after_the_try_began_leaves_the_board_on_the_try_before(self):
        """The try had said its phase on the feed when the Stop came: the try before is back on disk, and the feed
        says so, so the board reads the card's earlier outcome at its first attempt, and the job ended."""
        job_dir = self.job()
        before, _ = self.trials(job_dir)
        name = before["model"].trial_name
        real = run.Run.unit

        def unit(r, i, u, rr=1, retry=0):
            if retry:
                r.feed.emit("trial.phase", r.trial_name(i, u, rr), phase="agent_run")
                raise SystemExit(143)
            return real(r, i, u, rr, retry)
        with mock.patch.object(run.Run, "unit", unit), self.assertRaises(SystemExit):
            self.again(job_dir, ["model"])
        board = live.load(live.feed_path(job_dir)).snapshot()
        self.assertEqual(board["state"], "finished")
        card = next(t for t in board["trials"] if t["trial"] == name)
        self.assertEqual((card["state"], card["outcome"], card["reward"]), ("done", "unresolved", 0.0))
        self.assertNotIn("attempt", card)
        self.assertAlmostEqual(board["totals"]["cost_usd"], CALL)            # the try before, counted once

    def test_each_refusal_moves_nothing(self):
        job_dir = self.job()
        real = definitions.get

        def newer(kind, ref, root=None):         # the agent's definition moved on to another version since the run
            got = real(kind, ref, root)
            return dict(got, upstream={"version": "0.2"}) if got and kind == "agent" else got
        whole = digest(job_dir)
        cases = [
            ("no task", [], {}, "no task named"),
            ("an id it does not hold", ["model", "nosuch"], {}, "holds no trial or unit nosuch"),
            ("a changed agent", ["model"], {"get": newer}, "the job ran fake with no version, and fake resolves to 0.2 now"),
        ]
        for what, ids, kw, words in cases:
            with self.subTest(what), mock.patch.object(definitions, "get", kw.get("get", real)):
                with self.assertRaises(run.Refused) as e:
                    self.again(job_dir, ids)
                self.assertIn(words, str(e.exception))
                self.assertEqual(digest(job_dir), whole)
        with self.subTest("a re-run under way"):
            held = rerun._hold(job_dir)
            try:
                self.assertTrue(rerun.running(job_dir))
                whole = digest(job_dir)
                with self.assertRaises(run.Refused) as e:
                    self.again(job_dir, ["model"])
                self.assertIn("is still running", str(e.exception))
                self.assertEqual(digest(job_dir), whole)
            finally:
                held.close()
        with self.subTest("a job that has not ended"):
            os.rename(os.path.join(job_dir, "result.json"), os.path.join(self.tmp, "result.json"))
            whole = digest(job_dir)
            with self.assertRaises(run.Refused) as e:
                self.again(job_dir, ["model"])
            self.assertIn("is still running", str(e.exception))
            self.assertEqual(digest(job_dir), whole)

    def test_a_merged_job_reruns_in_place_and_leaves_its_shards_as_they_were(self):
        """``merge`` hard-links the shards' files into the merged job: a re-run renames a trial's folder and writes
        its new try into a new one, so no shard's file changes. A merge of shards that differed is refused."""
        shards = [self.job(f"shard-{i}", shard=(i, 2), launch="one") for i in (1, 2)]
        before = {s: digest(s) for s in shards}
        out = os.path.join(self.tmp, "merged")
        merge.merge(shards, out)
        name = next(t for t in merge._trials(out) if t.startswith("wrong__"))
        with said({"write answer.txt 41": ["write answer.txt 42"]}):
            self.again(out, ["wrong"])
        self.assertEqual({s: digest(s) for s in shards}, before)
        self.assertEqual(self.read(out, name, "result.json")["standard"]["retries"], 1)
        self.assertEqual(self.read(out, "retried", f"{name}.try-1", "result.json")["standard"]["shard_job"],
                         next(s for s in ("shard-1", "shard-2") if os.path.isdir(os.path.join(self.tmp, s, name))))
        self.assertEqual(self.read(out, "result.json")["standard"]["passed"], 2)
        forced = os.path.join(self.tmp, "forced")
        merge.merge([shards[0], self.job("other", shard=(2, 2), launch="another")], forced, force=True)
        whole = digest(forced)
        with self.assertRaises(run.Refused) as e:
            self.again(forced, ["good"])
        self.assertIn("merges shards that differed", str(e.exception))
        self.assertEqual(digest(forced), whole)

    def test_a_rerun_stops_at_its_cap_and_records_its_spend(self):
        job_dir = self.job(concurrency=1)
        before, _ = self.trials(job_dir)
        self.assertIsNone(rerun.budget(job_dir))                             # no cap, none given: none
        got = self.again(job_dir, ["model", "good"], max_usd=CALL + 0.00001, concurrency=1)
        after, _ = self.trials(job_dir)
        self.assertEqual(after["model"].outcome, classify.SPENDING_CAP)          # cut off at what the cap had left
        self.assertEqual(after["good"].n_retries, 0)                         # never begun: it stands as it was
        self.assertEqual(got["max_usd"], CALL + 0.00001)
        self.assertAlmostEqual(got["left_usd"], 0.00001)                     # the first run's call counted against it
        self.assertEqual(got["spent_usd"], 0.0)
        self.assertTrue(got["stopped"])
        self.assertEqual([x["trial"] for x in got["replaced"]], [before["model"].trial_name])
        b = self.read(job_dir, "result.json")["budget"]                      # the job had none: it has this one from now on
        self.assertEqual((b["max_usd"], b["spent_total_usd"], b["closed"]), (CALL + 0.00001, CALL, True))
        self.assertEqual([(c["max_usd"], c["was"]) for c in b["changes"]], [(CALL + 0.00001, None)])
        self.assertEqual(self.read(job_dir, ".work", "rerun-1", "ledger", "ledger.json")["max_usd"], CALL + 0.00001 - CALL)


class OneBudget(unittest.TestCase):
    """A job has one spending cap: each later part has what the parts before it left, a new cap holds from then on,
    and a part is refused when nothing is left."""
    setUpClass, tearDownClass = RerunTest.__dict__["setUpClass"], RerunTest.__dict__["tearDownClass"]
    setUp, job, again, read, trials = RerunTest.setUp, RerunTest.job, RerunTest.again, RerunTest.read, RerunTest.trials

    def ledger(self, job_dir, n):
        return self.read(job_dir, ".work", f"rerun-{n}", "ledger", "ledger.json")

    def test_a_later_part_has_what_the_parts_before_it_left(self):
        job_dir = self.job(concurrency=1, max_usd=0.0001)
        self.assertEqual(self.read(job_dir, "result.json")["budget"]["spent_usd"], CALL)
        self.assertEqual(rerun.budget(job_dir), {"max_usd": 0.0001, "spent_usd": CALL, "left_usd": 0.0001 - CALL,
                                                 "closed": False, "changed": False, "was": 0.0001})
        lines = []
        got = rerun.rerun(job_dir, ["model"], env=self.env, say=lines.append, root=ROOT)    # no cap given: the job's
        self.assertEqual(self.ledger(job_dir, 1)["max_usd"], 0.0001 - CALL)
        self.assertEqual((got["max_usd"], got["left_usd"], got["spent_usd"]), (0.0001, 0.0001 - CALL, CALL))
        b = self.read(job_dir, "result.json")["budget"]
        self.assertEqual((b["max_usd"], b["spent_usd"], b["spent_total_usd"], b["closed"]), (0.0001, CALL, 2 * CALL, False))
        self.assertNotIn("changes", b)
        self.assertIn(f"the job's spending cap: 0.0001 USD, {usd(CALL)} spent, {usd(0.0001 - CALL)} left for this part", lines)
        self.assertIn(f"spent {usd(CALL)} USD; the job has spent {usd(2 * CALL)} of its 0.0001 USD cap", lines)
        # the third call does not fit what is left: that part stops at the cap, and the next is refused until it changes
        got = self.again(job_dir, ["model"])
        self.assertEqual(self.ledger(job_dir, 2)["seen"], self.ledger(job_dir, 1)["seen"])
        self.assertTrue(got["stopped"])
        self.assertTrue(self.read(job_dir, "result.json")["budget"]["closed"])
        whole = digest(job_dir)
        with self.assertRaises(run.Refused) as e:
            self.again(job_dir, ["good"])
        self.assertEqual(str(e.exception), f"the job's spending cap of 0.0001 USD is spent ({usd(2 * CALL)} spent, and a call did not "
                                           "fit what was left): raise it with --max-usd USD")
        self.assertEqual(digest(job_dir), whole)
        got = self.again(job_dir, ["model"], max_usd=0.001)                  # a new cap holds from then on
        self.assertEqual(self.ledger(job_dir, 3)["max_usd"], 0.001 - 2 * CALL)
        b = self.read(job_dir, "result.json")["budget"]
        self.assertEqual((b["max_usd"], b["spent_total_usd"], b["closed"]), (0.001, 3 * CALL, False))
        self.assertEqual([(c["max_usd"], c["was"]) for c in b["changes"]], [(0.001, 0.0001)])
        self.again(job_dir, ["model"])
        self.assertEqual(self.ledger(job_dir, 4)["max_usd"], 0.001 - 3 * CALL)
        self.assertEqual(len(self.read(job_dir, "result.json")["budget"]["changes"]), 1)     # the same cap is no change

    def test_a_part_is_refused_when_the_cap_is_at_or_under_what_was_spent(self):
        job_dir = self.job(concurrency=1, max_usd=0.0001)
        whole = digest(job_dir)
        for cap in (CALL, CALL / 2):
            with self.subTest(cap=cap), self.assertRaises(run.Refused) as e:
                self.again(job_dir, ["model"], max_usd=cap)
            self.assertEqual(str(e.exception), f"the job's spending cap of {usd(cap)} USD is spent ({usd(CALL)} spent): "
                                               "raise it with --max-usd USD")
            self.assertEqual(digest(job_dir), whole)
        for bad in (0, -1.0, float("inf")):
            with self.subTest(bad=bad), self.assertRaises(run.Refused):
                self.again(job_dir, ["model"], max_usd=bad)

    def test_a_job_with_no_cap_has_none_unless_one_is_given(self):
        job_dir = self.job(concurrency=1)
        self.again(job_dir, ["model"])
        res = self.read(job_dir, "result.json")
        self.assertNotIn("budget", res)
        self.assertFalse(os.path.exists(os.path.join(job_dir, ".work", "rerun-1", "ledger")))
        self.assertIsNone(res["reruns"][0]["max_usd"])
        # given one: what its uncapped parts' trials cost, every try, counts against it
        self.assertEqual(rerun.budget(job_dir, cap=0.001)["spent_usd"], 2 * CALL)

    def test_a_job_recorded_before_one_budget_counts_each_part_s_spend(self):
        job_dir = self.job(concurrency=1, max_usd=0.0001)
        res = self.read(job_dir, "result.json")
        res["reruns"] = [{"at": "2026-11-18T10:00:00+00:00", "trials": [], "replaced": [], "max_usd": 5.0, "spent_usd": 0.00002},
                         {"at": "2026-11-18T11:00:00+00:00", "trials": [], "replaced": [], "max_usd": None, "spent_usd": None}]
        Path(job_dir, "result.json").write_text(json.dumps(res))
        self.assertEqual(rerun.budget(job_dir), {"max_usd": 0.0001, "spent_usd": CALL + 0.00002,
                                                 "left_usd": 0.0001 - (CALL + 0.00002), "closed": False,
                                                 "changed": False, "was": 0.0001})
        j = parse.parse_job(job_dir)                                         # and it reads as it did
        self.assertEqual((j.budget["max_usd"], [r["max_usd"] for r in j.reruns]), (0.0001, [5.0, None]))


class BudgetAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bench = definitions.get("benchmark", "fakebench", ROOT)
        self.agent = definitions.get("agent", "fake", ROOT)
        for method in ("ready", "known_models", "let_go"):
            self.enterContext(mock.patch.object(run.Run, method))
        self.enterContext(mock.patch.object(run.Run, "benchmark_calls", return_value=[]))

    def job(self, cap, cost, output):
        path = self.tmp / "job"
        path.mkdir()
        job.write_config(str(path), job="job", bench=self.bench, other=self.agent, model="m-1", provider="custom",
                         dataset="all", units=["model"], concurrency=1, granted=True,
                         prices={"models": {"m-1": {"input": 0.0, "output": cost * 1e6 / output}}})
        trial = self.trial(path / "model__u0000", cost, output)
        first = ledger.Ledger.create(str(path / ".work/ledger/ledger.json"), cap)
        booking, _ = first.reserve(cost)
        first.settle(booking, cost, model="m-1", output=output)
        runner = mock.Mock(ledger=first, max_usd=cap, unit_max_usd=None, _cut=[])
        job.write_result(str(path), "job", 1, 2, [trial], 1, budget=run.Run.budget(runner, []))
        writer = feed.Writer(str(path / "events.jsonl"), "job")
        writer.emit("job.start", n_total=1, tasks=["model"])
        writer.emit("trial.queued", trial["trial_name"], position=0)
        writer.emit("trial.end", trial["trial_name"], outcome="resolved", reward=1)
        writer.emit("job.end")
        return path

    def trial(self, path, cost, output, retry=0, refused=False):
        return job.write_trial(str(path), job="job", bench=self.bench, other=self.agent, model="m-1", unit="model",
                               t0=1, t1=2, produce=(1, 2), score=None if refused else {"reward": 1},
                               exception=(ledger.ERROR, "call does not fit") if refused else None,
                               calls=[] if refused else [{"status": 200, "reply": {"usage": {"output": output}}, "cost_usd": cost}],
                               trajectory=None, retries=retry)

    def part(self, path, cost, output, amount=None):
        admitted = []

        def simulate(r, index, task, attempt, retry=0):
            booking, state = r.ledger.reserve(cost if amount is None else amount, model="m-1")
            admitted.append((bool(booking), state["amount"]))
            if booking:
                r.ledger.settle(booking, cost, model="m-1", output=output)
            return self.trial(Path(r.job_dir) / r.trial_name(index, task, attempt), cost, output,
                              retry, refused=booking is None)

        with mock.patch.object(run.Run, "unit", simulate):
            record = rerun.rerun(str(path), ["model"], root=ROOT, say=lambda s: None,
                                 env={"MODEL_API_URL": "http://127.0.0.1:1/v1", "MODEL_API_KEY": "fake"})
        return record, admitted

    def test_small_parts_keep_unrounded_spending_under_one_cap(self):
        cost, cap = 0.0000004, 0.000001
        path = self.job(cap, cost, 1)
        calls = 1
        for _ in range(5):
            try:
                _, admitted = self.part(path, cost, 1)
            except run.Refused:
                break
            calls += sum(sent for sent, _ in admitted)
        self.assertLessEqual(calls * cost, cap + cost)
        self.assertEqual(calls, 2)
        result = json.loads((path / "result.json").read_text())
        self.assertEqual(result["budget"]["spent_total_usd"], calls * cost)
        self.assertEqual(rerun.budget(str(path))["left_usd"], cap - calls * cost)
        self.assertTrue(rerun.budget(str(path))["closed"])
        parsed = parse.parse_job(str(path))
        self.assertEqual(parsed.budget["spent_usd"], 0)
        self.assertTrue(all(r["spent_usd"] == 0 for r in parsed.reruns))

    def test_a_spend_equal_to_the_cap_but_for_float_noise_closes_the_job(self):
        # eight calls of 0.1 under a cap of 0.8 add up to 0.7999999999999999: nothing is left, and no part opens on it
        path = self.job(0.8, 0.1, 1)
        noisy = 0.0
        for _ in range(8):
            noisy += 0.1                                                         # as a ledger adds its calls
        result = json.loads((path / "result.json").read_text())
        result["budget"].update(spent_total_usd=noisy, closed=False)
        (path / "result.json").write_text(json.dumps(result))
        self.assertGreater(0.8 - noisy, 0)
        got = rerun.budget(str(path))
        self.assertEqual((got["closed"], got["left_usd"]), (True, 0.0))
        with self.assertRaises(run.Refused) as e:
            self.part(path, 0.1, 1)
        self.assertEqual(str(e.exception), "the job's spending cap of 0.80 USD is spent (0.80 spent): raise it with --max-usd USD")

    def test_next_part_inherits_cumulative_replies_once(self):
        path = self.job(1.61, 1, 100)
        row = {"input": 0.0, "output": 10000.0}
        self.part(path, 0.01, 1, amount=lambda usual: ledger.bound({"max_tokens": 1}, b"{}", row, seen=usual))
        result = json.loads((path / "result.json").read_text())
        result["reruns"][0].pop("cumulative_replies", None)
        (path / "result.json").write_text(json.dumps(result))
        record, admitted = self.part(path, 0.01, 1, amount=lambda usual: ledger.bound({}, b"{}", row, seen=usual))
        self.assertEqual(admitted, [(True, 0.51)])
        self.assertFalse(record.get("stopped", False))
        state = json.loads((path / ".work/rerun-2/ledger/ledger.json").read_text())
        self.assertEqual(state["seen"]["m-1"], [3, 102])

    def test_older_per_part_replies_are_combined_before_inheritance(self):
        path = self.job(1.61, 1, 100)
        self.part(path, 0.01, 1)
        result = json.loads((path / "result.json").read_text())
        result["budget"].pop("spent_total_usd")
        result["reruns"][0].pop("cumulative_replies", None)
        result["reruns"][0].pop("left_usd")
        (path / "result.json").write_text(json.dumps(result))
        older = path / ".work/rerun-1/ledger/ledger.json"
        state = json.loads(older.read_text())
        state["seen"]["m-1"] = [1, 1]
        older.write_text(json.dumps(state))
        before = older.read_bytes()
        row = {"input": 0.0, "output": 10000.0}
        for part, amount, seen in ((2, 0.51, [3, 102]), (3, 0.34, [4, 103])):
            _, admitted = self.part(path, 0.01, 1, amount=lambda usual: ledger.bound({}, b"{}", row, seen=usual))
            self.assertEqual(admitted, [(True, amount)])
            state = json.loads((path / f".work/rerun-{part}/ledger/ledger.json").read_text())
            self.assertEqual(state["seen"]["m-1"], seen)
        self.assertEqual(older.read_bytes(), before)


class NoVersion(unittest.TestCase):
    """A job that records no version of a benchmark that has versions now is older than them: it ran the pin the
    definition held then, the one version that runs the shared commit."""

    def test_the_version_that_runs_the_shared_pin_is_the_earlier_one(self):
        up = {"repository": "https://example.org/b", "tested": "a" * 40}
        for versions, want in [({"v2": {"upstream": {"tag": "v2", "tested": "b" * 40}}, "old": {"upstream": {"branch": "main"}}}, "old"),
                               ({"v2": {"upstream": {"tag": "v2", "tested": "b" * 40}}, "v1": {"upstream": {"tag": "v1"}}}, None),
                               ({"a": {"upstream": {"branch": "main"}}, "b": {"upstream": {"branch": "dev"}}}, None),
                               ({"fork": {"upstream": {"branch": "main", "repository": "https://example.org/fork"}}}, None),
                               ({}, None)]:
            with self.subTest(versions=list(versions)):
                self.assertEqual(rerun.earlier_version({"upstream": up, "default": "v2", "versions": versions}), want)
        self.assertIsNone(rerun.earlier_version({"upstream": {}, "versions": {"old": {"upstream": {"branch": "main"}}}}))

    def test_a_job_of_osworld1_from_before_its_versions_is_taken_as_the_earlier_one(self):
        bench = definitions.get("benchmark", "osworld1")
        self.assertEqual(bench["_version"], "desktop-env-v1.0.3")                # what a bare key resolves to now
        lines = []
        std = {"benchmark": "osworld1", "plugged": "nop", "kind": "control"}     # recorded before versions: none
        got, other = rerun.resolve({"job_name": "old-job", "standard": std}, [], say=lines.append)
        self.assertEqual((got["_ref"], other["key"]), ("osworld1@044bc33", "nop"))
        self.assertEqual(lines, ["old-job records no version of osworld1: taken as osworld1@044bc33, main (044bc33), "
                                 "the one osworld1 had before it had versions"])
        lines.clear()
        std = dict(std, **rerun.job._version(definitions.get("benchmark", "osworld1@044bc33")))
        self.assertEqual(rerun.resolve({"standard": std}, [], say=lines.append)[0]["_ref"], "osworld1@044bc33")
        self.assertEqual(lines, [])                                              # a recorded version is the one run

    def test_without_one_earlier_version_the_job_is_refused_with_what_to_do(self):
        bench = dict(definitions.get("benchmark", "osworld1"))
        with mock.patch.object(rerun, "_ran_before_versions", lambda b, root: None), \
                mock.patch.object(rerun.definitions, "get", lambda kind, ref, root=None: bench), self.assertRaises(run.Refused) as e:
            rerun.resolve({"standard": {"benchmark": "osworld1", "plugged": "nop", "kind": "control"}}, [])
        self.assertEqual(str(e.exception), "the job records no version of osworld1, and its definition does not say which of "
                                           "its versions (desktop-env-v1.0.3, 044bc33) the job ran: start a new run to compare")


class OwnAgent(unittest.TestCase):
    """A later part of a job of one of a benchmark's own agents runs that agent again, named as its job names it."""

    def test_the_agent_the_job_ran_is_the_one_resolved(self):
        bench = definitions.get("benchmark", "osworld1")
        for upstream, key, model in (("openai_cua", "openai-cua", "computer-use-preview"), ("", "m3", "MiniMax-M3")):
            std = {"benchmark": "osworld1", **rerun.job._version(bench), "plugged": "original", "kind": "upstream",
                   "provider": "openrouter", "setup": {"name": "original", "agent": key, "asked": model},
                   **({"benchmark_options": {"agent": upstream}} if upstream else {})}
            with self.subTest(agent=key):
                _, other = rerun.resolve({"standard": std}, [])
                self.assertEqual((other["agent"], rerun.job.agent_name(other), other["asked"]), (key, key, model))

    def test_a_trial_begun_in_a_later_part_is_queued_under_the_jobs_agent(self):
        # the board names a queued trial by the job's agent, as the first part did, not by the setup it runs under
        got, tmp = [], tempfile.mkdtemp(prefix="nullius-own-agent-")
        self.addCleanup(shutil.rmtree, tmp, True)

        class Feed:
            def emit(self, event, trial=None, **kw):
                got.append((event, trial, kw.get("agent")))

        r = mock.Mock(job_dir=tmp, other={"key": "original", "agent": "openai-cua"}, feed=Feed())
        r._stopped.is_set.return_value = False
        self.assertIsNone(rerun._begin(r, "t__1", (0, "t", 1), 0, lambda iur: None))
        self.assertEqual(got, [("trial.queued", "t__1", "openai-cua")])


# five units, each one model call of 0.00003 USD (3 tokens out at 10 USD a million), each scored a pass
FIVE_DRIVER = '''from nullius.standard.protocols.jsonl import serve
serve({"units": lambda dataset: {"units": [f"t{i}" for i in range(5)]}, "setup": lambda unit, dir: {"prompt": "model hi"},
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True, "detail": {}}})
'''


class FinishTest(unittest.TestCase):
    """The tasks a job planned and has no result for - never begun, or begun and killed - run in that job."""
    setUpClass, tearDownClass = RerunTest.__dict__["setUpClass"], RerunTest.__dict__["tearDownClass"]
    setUp, job, again, read, trials = RerunTest.setUp, RerunTest.job, RerunTest.again, RerunTest.read, RerunTest.trials

    def capped(self) -> tuple:
        """Five tasks, one at a time, under a cap of 0.00008 USD: two pass, the third's call does not fit and it is
        cut off, and the last two are never begun. (the job, the catalogue it ran from)"""
        root = os.path.join(self.tmp, "catalogue")
        shutil.copytree(ROOT, root)
        with open(os.path.join(root, "benchmarks", "fakebench", "driver.py"), "w") as f:
            f.write(FIVE_DRIVER)
        agent = Path(root, "agents", "fake", "agent.json")          # the fake agent's script, where the fixtures keep it
        agent.write_text(agent.read_text().replace("{here}/../../..", os.path.dirname(ROOT)))
        b, o = definitions.get("benchmark", "fakebench", root), definitions.get("agent", "fake", root)
        job_dir = run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, env=self.env, say=lambda s: None,
                          unsandboxed=True, name="five", concurrency=1, max_usd=0.00008,
                          prices={"m-1": {"input": 0.0, "output": 10.0, "max_output": 3}})
        return job_dir, root

    def test_a_job_its_cap_stopped_is_finished_in_that_job(self):
        job_dir, root = self.capped()
        first, config = self.read(job_dir, "result.json"), Path(job_dir, "config.json").read_bytes()
        self.assertEqual((first["budget"]["stopped"], first["budget"]["cut_off"], first["budget"]["not_run"]), (True, ["t2"], ["t3", "t4"]))
        names = [n for n, _ in rerun.planned(job_dir)]
        self.assertEqual(names, [f"t{i}__u{i:04d}" for i in range(5)])                       # what the feed says it planned
        self.assertEqual(rerun.unfinished(job_dir), {n: {"unit": n[:2], "folder": False} for n in names[3:]})
        j0 = parse.parse_job(job_dir)
        self.assertEqual((j0.n_planned, len(j0.trials), j0.n_resolved), (5, 3, 2))
        whole = digest(job_dir)
        with self.assertRaises(run.Refused) as e:                                            # one cap for the job: spent
            rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=lambda s: None, root=root)
        self.assertEqual(str(e.exception), "the job's spending cap of 0.00008 USD is spent (0.00006 spent, and a call did not "
                                           "fit what was left): raise it with --max-usd USD")
        self.assertEqual(digest(job_dir), whole)
        said_lines = []
        got = rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=said_lines.append, root=root, reason="finish",
                          max_usd=0.001)
        j1 = parse.parse_job(job_dir)
        self.assertEqual((j1.n_planned, len(j1.trials), j1.n_resolved), (5, 5, 5))
        self.assertEqual({t.trial_name: t.outcome for t in j1.trials}, {n: classify.RESOLVED for n in names})
        res = self.read(job_dir, "result.json")
        self.assertEqual((res["n_total_trials"], res["stats"]["n_completed_trials"], res["stats"]["n_cancelled_trials"]), (5, 5, 0))
        self.assertEqual(res["standard"]["passed"], 5)                                       # the rate, over the five
        self.assertEqual((res["started_at"], res["finished_at"]), (first["started_at"], first["finished_at"]))
        changes = res["budget"].pop("changes")
        self.assertEqual([(c["max_usd"], c["was"]) for c in changes], [(0.001, 0.00008)])
        self.assertEqual(res["budget"], dict(first["budget"], max_usd=0.001, spent_total_usd=2 * 0.00003 + 3 * 0.00003, closed=False))
        self.assertEqual(Path(job_dir, "config.json").read_bytes(), config)
        (entry,) = res["reruns"]
        self.assertEqual(entry["trials"], names[2:])
        self.assertEqual(entry["replaced"], [{"trial": names[2], "unit": "t2", "try": 1, "was": {"error": "SpendingCapError"},
                                              "now": {"reward": 1.0}}])
        self.assertEqual(entry["begun"], [{"trial": n, "unit": n[:2], "now": {"reward": 1.0}} for n in names[3:]])
        self.assertNotIn("no_result", entry)
        self.assertEqual((entry["max_usd"], entry["left_usd"]), (0.001, 0.00094))           # the job's new cap, less what was spent
        self.assertAlmostEqual(entry["spent_usd"], 3 * 0.00003)
        self.assertEqual(got["begun"], entry["begun"])
        self.assertEqual((parse.parse_job(job_dir).reruns[0]["begun"], j0.reruns), (entry["begun"], []))
        for n in names[3:]:                                                                  # each its first try
            std = self.read(job_dir, n, "result.json")["standard"]
            self.assertEqual((std.get("retries", 0), std["rerun"]["begun"], std["rerun"]["reason"]), (0, True, "finish"))
            self.assertNotIn("replaced", std["rerun"])
        self.assertEqual([x for x in os.listdir(os.path.join(job_dir, "retried"))], [f"{names[2]}.try-1"])
        self.assertTrue(any(s.startswith("run: 2 of 2 begun") for s in said_lines), said_lines)
        self.assertTrue(any(s.startswith("ran again: 1 of 1 replaced") for s in said_lines), said_lines)
        # the board reads the job ended, every card done
        board = live.load(live.feed_path(job_dir)).snapshot()
        self.assertEqual(board["state"], "finished")
        self.assertEqual(sorted((t["trial"], t["outcome"]) for t in board["trials"]), [(n, "resolved") for n in names])
        # finished whole: nothing more to take, said, and nothing written
        whole, said_lines = digest(job_dir), []
        got = rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=said_lines.append, root=root)
        self.assertEqual((got["trials"], said_lines), ([], ["nothing to run: every task five planned has a result"]))
        self.assertEqual(digest(job_dir), whole)

    def test_a_task_never_begun_is_named_by_its_unit(self):
        job_dir, root = self.capped()
        got = rerun.rerun(job_dir, ["t4"], env=self.env, say=lambda s: None, root=root, max_usd=0.001)
        self.assertEqual(([x["trial"] for x in got["begun"]], got["replaced"]), (["t4__u0004"], []))
        self.assertEqual(sorted(rerun.unfinished(job_dir)), ["t3__u0003"])

    def test_a_task_killed_mid_run_is_kept_marked_and_run(self):
        """Killed in the job's first run (a folder, no result), and killed in a re-run, after the try before it was
        moved into retried/: each folder is kept as the try it was, marked, and the task runs again."""
        job_dir = self.job()
        before, _ = self.trials(job_dir)
        model, good = before["model"].trial_name, before["good"].trial_name
        os.remove(os.path.join(job_dir, model, "result.json"))                    # the first run's try, killed
        os.makedirs(os.path.join(job_dir, "retried"))
        os.replace(os.path.join(job_dir, good), os.path.join(job_dir, "retried", f"{good}.try-1"))
        os.makedirs(os.path.join(job_dir, good, "workspace"))                     # a re-run's try, killed

        def inventory(folder):
            return {str(p.relative_to(folder)): p.read_bytes() if p.is_file() else None
                    for p in folder.rglob("*") if p.relative_to(folder) != Path(rerun.NO_RESULT)}

        for name in (model, good):
            workspace = Path(job_dir, name, "workspace")
            (workspace / "empty").mkdir()
            (workspace / "nested").mkdir()
            (workspace / "nested" / "evidence.bin").write_bytes(b"\x00kept evidence\xff")
        inventories = {name: inventory(Path(job_dir, name)) for name in (model, good)}
        self.assertEqual(rerun.unfinished(job_dir), {model: {"unit": "model", "folder": True}, good: {"unit": "good", "folder": True}})
        self.assertEqual(len(parse.parse_job(job_dir).trials), 1)
        got = self.again(job_dir, [], every_unfinished=True)
        after, j = self.trials(job_dir)
        self.assertEqual(len(j.trials), 3)
        self.assertEqual(sorted(got["no_result"]), sorted([model, good]))
        self.assertEqual((j.reruns[0]["no_result"], "begun" in j.reruns[0]), (got["no_result"], False))
        self.assertEqual({x["trial"]: (x["try"], x["was"]) for x in got["replaced"]}, {model: (1, {}), good: (2, {})})
        for name, n in ((model, 1), (good, 2)):
            kept = os.path.join(job_dir, "retried", f"{name}.try-{n}")
            self.assertEqual(inventory(Path(kept)), inventories[name])
            self.assertFalse(os.path.exists(os.path.join(kept, "result.json")))
            self.assertIsNone(self.read(kept, rerun.NO_RESULT)["result"])
            self.assertEqual(self.read(job_dir, name, "result.json")["standard"]["retries"], n)
            self.assertTrue(self.read(job_dir, name, "result.json")["standard"]["rerun"]["no_result"])
        self.assertEqual(after["good"].outcome, classify.RESOLVED)
        # the tries a page lists: the one kept with a result, and the last; the one with none is not a try to open
        self.assertEqual([(n, t.trial_name) for n, t in parse.tries(after["good"])], [(1, f"{good}.try-1"), (3, good)])
        self.assertEqual(rerun.unfinished(job_dir), {})

    def test_a_trial_a_stop_ended_is_taken_with_the_tasks_never_begun(self):
        """A Stop ends the unit under way with a result that says so, and the units after it are never begun: both
        are what the stop left, and --unfinished takes them; a judged trial is not taken."""
        job_dir, root = self.capped()
        name = "t1__u0001"
        res = self.read(job_dir, name, "result.json")
        res["exception_info"], res["verifier_result"] = {"exception_type": run.CANCELLED, "exception_message": "stopped"}, None
        Path(job_dir, name, "result.json").write_text(json.dumps(res))
        got = rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=lambda s: None, root=root, max_usd=0.001)
        self.assertEqual(got["trials"], ["t1__u0001", "t2__u0002", "t3__u0003", "t4__u0004"])
        self.assertEqual([(x["trial"], x["was"]) for x in got["replaced"]],
                         [(name, {"error": run.CANCELLED}), ("t2__u0002", {"error": "SpendingCapError"})])

    def test_a_stop_leaves_a_task_never_begun_as_it_was(self):
        job_dir, root = self.capped()
        whole = digest(job_dir)

        def unit(r, i, u, rr=1, retry=0):
            os.makedirs(os.path.join(r.job_dir, r.trial_name(i, u, rr), "workspace"))
            raise SystemExit(143)
        with mock.patch.object(run.Run, "unit", unit), self.assertRaises(SystemExit):
            rerun.rerun(job_dir, ["t3"], env=self.env, say=lambda s: None, root=root, max_usd=0.001)
        self.assertEqual(sorted(rerun.unfinished(job_dir)), ["t3__u0003", "t4__u0004"])
        self.assertFalse(any(n.startswith("t3__") for n in os.listdir(job_dir)))
        entry = self.read(job_dir, "result.json")["reruns"][-1]
        self.assertEqual((entry["begun"], entry["stopped"]), ([], True))
        self.assertEqual({k for k, v in digest(job_dir).items() if whole.get(k) != v}, {"result.json", "events.jsonl", "progress.jsonl", ".work/rerun.lock",
                                                                                         ".work/rerun-1/ledger/ledger.json"})

    def test_each_refusal_moves_nothing(self):
        job_dir, root = self.capped()
        whole = digest(job_dir)
        for what, ids, words in (("an id it never planned", ["t9"], "holds no trial or unit t9"),
                                 ("a trial name it never planned", ["t3__u0009"], "holds no trial or unit t3__u0009")):
            with self.subTest(what), self.assertRaises(run.Refused) as e:
                rerun.rerun(job_dir, ids, env=self.env, say=lambda s: None, root=root)
            self.assertIn(words, str(e.exception))
            self.assertEqual(digest(job_dir), whole)
        with self.subTest("a job still running"):
            os.rename(os.path.join(job_dir, "result.json"), os.path.join(self.tmp, "result.json"))
            whole = digest(job_dir)
            with self.assertRaises(run.Refused) as e:
                rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=lambda s: None, root=root)
            self.assertIn("is still running", str(e.exception))
            self.assertEqual(digest(job_dir), whole)
            os.rename(os.path.join(self.tmp, "result.json"), os.path.join(job_dir, "result.json"))
        with self.subTest("a job whose feed does not say what it planned"):
            os.rename(os.path.join(job_dir, "events.jsonl"), os.path.join(self.tmp, "events.jsonl"))
            whole = digest(job_dir)
            with self.assertRaises(run.Refused) as e:
                rerun.rerun(job_dir, [], every_unfinished=True, env=self.env, say=lambda s: None, root=root)
            self.assertIn("does not record the tasks it planned", str(e.exception))
            with self.assertRaises(run.Refused) as e:
                rerun.rerun(job_dir, ["t3"], env=self.env, say=lambda s: None, root=root)
            self.assertIn("holds no trial or unit t3", str(e.exception))
            self.assertEqual(digest(job_dir), whole)


if __name__ == "__main__":
    unittest.main()
