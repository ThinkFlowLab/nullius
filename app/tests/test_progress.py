"""What a run is doing, one line per stage (``nullius/progress.py``): the
line read back as it was said, the job's ``progress.jsonl``, a stage's start
and end, a download said every few seconds and not at every chunk, the
stages folded for the page; and a standard run saying its own, and why it
was refused, in its job. No network, no docker."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius import progress
from nullius.standard import definitions, install, run


class Line(unittest.TestCase):
    def test_a_line_reads_back_as_it_was_said(self):
        text = progress.line("image", "start", detail='building nullius/base:229ac6f71272 (first time "here")')
        self.assertTrue(text.startswith('[nullius] stage=image state=start detail="building nullius/base:229ac6f71272'))
        ev = progress.parse(text)
        self.assertEqual((ev["stage"], ev["state"], ev["detail"]), ("image", "start", 'building nullius/base:229ac6f71272 (first time "here")'))
        self.assertIsInstance(ev["t"], float)

    def test_numbers_stay_numbers_and_a_bare_word_stays_bare(self):
        text = progress.line("unit", "done", i=3, of=10, unit="HumanEval/2", outcome="passed", reward=1.0, seconds=41.0)
        self.assertIn(" unit=HumanEval/2 ", text)
        self.assertIn(" seconds=41 ", text)                               # whole seconds read as such
        ev = progress.parse(text)
        self.assertEqual((ev["i"], ev["of"], ev["unit"], ev["reward"], ev["seconds"]), (3, 10, "HumanEval/2", 1.0, 41))

    def test_a_field_left_out_is_not_said_and_any_other_line_is_none(self):
        self.assertNotIn("total", progress.line("dataset", "progress", bytes=5, total=None))
        for other in ("job /x/y", "  HumanEval/2: reward 1.0", "[nullius]", "[nullius] state=done", "@progress f 1 2"):
            self.assertIsNone(progress.parse(other), other)
        self.assertEqual(progress.parse("  [nullius] stage=units n=3")["n"], 3)   # read from a log, indented or not


class Reporter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-progress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_each_event_goes_to_the_stream_and_the_job(self):
        out = io.StringIO()
        r = progress.Reporter(os.path.join(self.tmp, "job"), out)
        r.emit("units", "start", n=2, noun="tasks")
        with r.stage("install", agent="codex@2.2.2"):
            pass
        with r.stage("upstream", cached=True):
            pass
        lines = out.getvalue().splitlines()
        self.assertEqual([progress.parse(l)["state"] for l in lines], ["start", "start", "done", "cached"])
        self.assertIn("agent=codex@2.2.2", lines[2])
        self.assertIn("seconds=", lines[2])
        self.assertEqual([e["state"] for e in progress.read(os.path.join(self.tmp, "job"))], ["start", "start", "done", "cached"])

    def test_a_stage_that_raises_is_said_failed_and_the_error_goes_on(self):
        r = progress.Reporter(self.tmp)
        with self.assertRaises(install.InstallFailed):
            with r.stage("install", agent="x"):
                raise install.InstallFailed("npm is not on this machine")
        last = progress.read(self.tmp)[-1]
        self.assertEqual((last["state"], last["detail"]), ("failed", "npm is not on this machine"))

    def test_without_a_stream_or_a_job_nothing_is_written(self):
        with patch.object(progress, "ECHO", None):
            progress.Reporter().emit("units", "start", n=1)            # nowhere to go: no error either
        out = io.StringIO()
        with patch.object(progress, "ECHO", out):
            progress.Reporter().emit("units", "start", n=1)
        self.assertIn("stage=units", out.getvalue())

    def test_a_download_is_said_every_few_seconds_and_at_its_end(self):
        r = progress.Reporter(self.tmp)
        clock = iter([100.0, 101.0, 102.0, 106.0, 107.0, 108.0])
        with patch.object(progress.time, "monotonic", lambda: next(clock)):
            say = r.download("dataset")
            for done in (1, 2, 3, 4, 5):
                say(done * 10, 50)
            say(50, 50, final=True)
        got = [(e["bytes"], e["total"]) for e in progress.read(self.tmp)]
        self.assertEqual(got, [(10, 50), (40, 50), (50, 50)])


def ev(stage, state=None, t=0.0, **kw):
    return dict({"stage": stage, "t": t}, **({"state": state} if state else {}), **kw)


class Fold(unittest.TestCase):
    def test_planned_stages_wait_then_run_then_end(self):
        got = progress.fold([ev("plan", stages="connect,uv,app"), ev("plan", stages="image,install,units"),
                             ev("connect", "start", 1.0), ev("connect", "done", seconds=2.5), ev("uv", "cached"),
                             ev("app", "start", 9.0)])
        self.assertEqual([(s["key"], s["state"]) for s in got["stages"]],
                         [("connect", "done"), ("uv", "cached"), ("app", "running"), ("image", "waiting"), ("install", "waiting"), ("units", "waiting")])
        self.assertEqual(got["current"], "app")
        self.assertEqual(got["stages"][0]["seconds"], 2.5)
        self.assertEqual(got["stages"][2]["t"], 9.0)

    def test_a_planned_stage_said_with_what_it_is_of_takes_its_place(self):
        got = progress.fold([ev("plan", stages="install,units"), ev("install", "start", agent="codex@2.2.2")])
        self.assertEqual([(s["key"], s["label"]) for s in got["stages"]], [("install:codex@2.2.2", "Install codex@2.2.2"), ("units", "Units")])

    def test_a_planned_stage_a_later_one_overtook_was_not_needed(self):
        got = progress.fold([ev("plan", stages="image,dataset,install"), ev("image", "cached"), ev("install", "start", agent="a")])
        self.assertEqual([s["stage"] for s in got["stages"]], ["image", "install"])

    def test_units_are_counted_as_they_end(self):
        got = progress.fold([ev("units", "start", n=10, noun="tasks"), ev("unit", "start", unit="a"), ev("unit", "start", unit="b"),
                             ev("unit", "start", unit="c"), ev("unit", "done", outcome="passed"), ev("unit", "done", outcome="failed"),
                             ev("unit", "done", outcome="error", error="AgentTimeoutError")])
        u = got["units"]
        self.assertEqual((u["n"], u["noun"], u["done"], u["passed"], u["failed"], u["errors"], u["running"]), (10, "tasks", 3, 1, 1, 1, 0))

    def test_a_run_of_several_parts_counts_every_parts_units_and_goes_on_after_one_ends(self):
        """nop then oracle, three units each: 6 of 6 at the end, never 6 of 3,
        and the first part's end is not the run's."""
        part = lambda: [ev("plan", stages="upstream,units"), ev("units", "start", n=3, noun="problems"),
                        *[e for u in "abc" for e in (ev("unit", "start", unit=u), ev("unit", "done", outcome="passed"))],
                        ev("units", "done", seconds=1.0)]
        first = part() + [ev("done", n=3, passed=3)]
        between = progress.fold(first + part()[:2])
        self.assertIsNone(between["ended"])
        self.assertEqual((between["units"]["n"], between["units"]["done"], between["current"]), (6, 3, "units"))
        got = progress.fold(first + part() + [ev("done", n=3, passed=3)])
        self.assertEqual((got["units"]["n"], got["units"]["done"], got["units"]["passed"], got["ended"]), (6, 6, 6, "done"))

    def test_a_download_shows_its_bytes(self):
        got = progress.fold([ev("dataset", "start", 1.0), ev("dataset", "progress", bytes=5_000_000, total=9_000_000)])
        self.assertEqual((got["stages"][0]["bytes"], got["stages"][0]["total"]), (5_000_000, 9_000_000))

    def test_a_refusal_ends_the_list_and_says_why(self):
        got = progress.fold([ev("plan", stages="image,install,units"), ev("image", "start"), ev("refused", detail="the container backend: no")])
        self.assertEqual((got["ended"], got["detail"]), ("refused", "the container backend: no"))
        self.assertEqual([(s["stage"], s["state"]) for s in got["stages"]], [("image", "failed")])
        self.assertEqual(progress.refusal([ev("refused", detail="why")]), "why")
        self.assertIsNone(progress.refusal([ev("done")]))


class StandardRun(unittest.TestCase):
    """A control run on a kit says its stages and its units in its job."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-progress-run-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        here = os.path.join(self.tmp, "kits", "agent-workspace-v1")
        shutil.copytree(os.path.join(definitions.KITS, "agent-workspace-v1"), here)
        self.b, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", "agent-workspace-v1")
        assert not bad, bad

    def go(self, **kw):
        return run.Run(self.b, definitions.control(self.b, "oracle"), model="", provider="openai",
                       jobs_root=os.path.join(self.tmp, "jobs"), env={}, say=lambda s: None, **kw)

    def test_a_run_says_its_units_one_by_one_and_its_end(self):
        out = io.StringIO()
        with patch.object(progress, "ECHO", out):
            job = self.go().start()
        events = progress.read(job)
        self.assertEqual(events[0]["stage"], "plan")
        self.assertEqual([e["stage"] for e in events if e["stage"] == "units"], ["units", "units"])
        done = [e for e in events if e["stage"] == "unit" and e["state"] == "done"]
        self.assertEqual(sorted(e["i"] for e in done), [1, 2])
        self.assertTrue(all(e["of"] == 2 and e["outcome"] == "passed" and e["reward"] == 1.0 for e in done))
        self.assertEqual((events[-1]["stage"], events[-1]["n"], events[-1]["passed"]), ("done", 2, 2))
        self.assertEqual(len(out.getvalue().splitlines()), len(events))   # the same events, as lines, on the stream
        folded = progress.fold(events)
        self.assertEqual((folded["units"]["done"], folded["units"]["passed"], folded["ended"]), (2, 2, "done"))

    def test_a_run_writes_the_live_feed_its_job_page_folds(self):
        from nullius.runs import live
        job = self.go().start()
        self.assertTrue(live.has_feed(job))                # the page's board, not a 404 until the first unit ends
        snap = live.snapshot(job)
        self.assertEqual(snap["state"], "finished")
        self.assertEqual((snap["counts"]["resolved"], snap["counts"]["total"]), (2, 2))
        names = sorted(d for d in os.listdir(job) if "__u" in d)
        self.assertEqual(sorted(t["trial"] for t in snap["trials"]), names)   # the trial folders' own names

    def test_a_refused_run_says_why_in_its_job_and_writes_nothing_else(self):
        r = self.go()
        with patch.object(install, "upstream_dir", lambda b, home=None: os.path.join(self.tmp, "up")), \
                patch.object(install, "upstream", side_effect=install.InstallFailed("git fetch: exit 128")):
            self.b["upstream"] = {"repository": "/nowhere", "tested": "a" * 40}
            with self.assertRaises(run.Refused):
                r.start()
        events = progress.read(r.job_dir)
        self.assertEqual([(e["stage"], e.get("state")) for e in events],
                         [("plan", None), ("upstream", "start"), ("upstream", "failed"), ("refused", None)])
        self.assertIn("git fetch: exit 128", progress.refusal(events))
        self.assertFalse(os.path.exists(os.path.join(r.job_dir, "config.json")))   # no job: nothing a runs page would list


if __name__ == "__main__":
    unittest.main()
