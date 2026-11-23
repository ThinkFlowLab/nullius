"""A run planned on a whole and begun on a part of it (``run --start-with``), the rest run later in the same job
by ``rerun``, against a fake benchmark of six tasks and the fake agent. No key, no network: the provider is a
local fake endpoint."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from nullius.runs import classify, cli as runs_cli, live, parse
from nullius.standard import cli as std_cli
from nullius.standard.contract import definitions
from nullius.standard.runner import rerun, run, sets
from tests import test_standard_rerun as base

ROOT = base.ROOT
UNITS = [f"t{i}" for i in range(6)]
SIX_DRIVER = '''from nullius.standard.protocols.jsonl import serve
serve({"units": lambda dataset: {"units": [f"t{i}" for i in range(6)]}, "setup": lambda unit, dir: {"prompt": "model hi"},
       "score": lambda unit, dir, transcript, answer: {"reward": 1.0, "passed": True, "detail": {}}})
'''
# what differs between two runs of the same plan by their clocks and their names alone
VOLATILE = {"started_at", "finished_at", "at", "found_at", "id", "job_name", "job", "date", "seconds", "ts"}


def steady(doc):
    """A record with what a clock or a job's name puts in it left out."""
    if isinstance(doc, dict):
        return {k: steady(v) for k, v in doc.items() if k not in VOLATILE and not k.endswith("_sec") and not k.endswith("_ms")}
    if isinstance(doc, list):
        return [steady(v) for v in doc]
    return doc


class RecordComparison(unittest.TestCase):
    def test_cost_changes_are_not_discarded(self):
        self.assertNotEqual(steady({"agent_result": {"cost_usd": base.CALL}}),
                            steady({"agent_result": {"cost_usd": 42}}))


class PartTest(unittest.TestCase):
    setUpClass, tearDownClass = base.RerunTest.__dict__["setUpClass"], base.RerunTest.__dict__["tearDownClass"]
    setUp, read = base.RerunTest.setUp, base.RerunTest.read

    def catalogue(self) -> str:
        root = os.path.join(self.tmp, "catalogue")
        if not os.path.isdir(root):
            shutil.copytree(ROOT, root)
            bench = Path(root, "benchmarks", "fakebench")
            (bench / "driver.py").write_text(SIX_DRIVER)
            spec = json.loads((bench / "standard.json").read_text())
            spec["datasets"] = [{"id": "all", "units": 6}, {"id": "other", "units": 6}]
            (bench / "standard.json").write_text(json.dumps(spec))
            agent = Path(root, "agents", "fake", "agent.json")
            agent.write_text(agent.read_text().replace("{here}/../../..", os.path.dirname(ROOT)))
        return root

    def plan(self, name="job", lines=None, **kw) -> str:
        root = self.catalogue()
        b, o = definitions.get("benchmark", "fakebench", root), definitions.get("agent", "fake", root)
        return run.run(b, o, model="m-1", provider="custom", jobs_root=self.tmp, env=self.env,
                       say=(lines.append if lines is not None else lambda s: None), unsandboxed=True, name=name,
                       concurrency=2, dataset="all", prices=base.PRICES, **kw)

    def again(self, job_dir, ids, **kw) -> dict:
        return rerun.rerun(job_dir, ids, env=self.env, say=lambda s: None, root=self.catalogue(), **kw)

    def a_set(self, name, units, dataset="all") -> dict:
        b = definitions.get("benchmark", "fakebench", self.catalogue())
        entry = sets.create(b, name, dataset=dataset, units=units, root=os.path.join(self.tmp, "sets"))
        return {"task_set": sets.record(entry, sets.revision(entry)), "dataset": entry["dataset"]}

    def units_file(self, units) -> dict:
        path = os.path.join(self.tmp, "first.txt")
        Path(path).write_text("\n".join(units) + "\n")
        return {"units_file": run.units_file(path)}

    def show(self, job_dir) -> tuple:
        out = io.StringIO()
        with redirect_stdout(out):
            runs_cli.main(["show", os.path.dirname(job_dir), os.path.basename(job_dir)])
        return out.getvalue(), runs_cli.job_row(parse.parse_job(job_dir))

    def test_a_plan_of_six_begun_on_two_by_a_count_a_file_and_a_set(self):
        for how, given in (("count", {"count": 2}), ("file", self.units_file(["t3", "t1"])),
                           ("set", self.a_set("first-two", ["t1", "t3"])), ("ids", {"ids": ["t3", "t1"]})):
            with self.subTest(how=how):
                lines = []
                job_dir = self.plan(name=f"job-{how}", start_with=given, lines=lines)
                cfg, res = self.read(job_dir, "config.json"), self.read(job_dir, "result.json")
                part = ["t0", "t1"] if how == "count" else ["t1", "t3"]                 # in the plan's order
                self.assertEqual(cfg["datasets"][0]["n_tasks"], 6)                     # the plan is the whole
                self.assertEqual(cfg["standard"]["start_with"]["units"], part)
                self.assertEqual(set(cfg["standard"]["start_with"]) - {"units"}, {"count": {"count"}, "file": {"units_file"},
                                                                                   "set": {"task_set"}, "ids": set()}[how])
                self.assertEqual(res["n_total_trials"], 6)
                self.assertNotIn("budget", res)
                self.assertEqual([n for n, _ in rerun.planned(job_dir)], [f"t{i}__u{i:04d}" for i in range(6)])
                self.assertEqual(sorted(x["unit"] for x in rerun.unfinished(job_dir).values()), sorted(set(UNITS) - set(part)))
                j = parse.parse_job(job_dir)
                self.assertEqual((len(j.trials), j.n_planned, j.n_tasks, j.n_tasks_run, j.state), (2, 6, 6, 2, "finished"))
                self.assertEqual(j.start_with, {"by": how, "units": 2,
                                                "name": {"count": "", "file": "first.txt", "set": "first-two", "ids": "the tasks chosen"}[how]})
                self.assertEqual(j.raw_solve_rate, 1.0)                                  # over the tasks run
                text, row = self.show(job_dir)
                self.assertIn("2 trial(s) of 6 planned", text)
                self.assertIn("begun on a part: 2 of 6 planned tasks run; the rates are over those run", text)
                self.assertEqual((row["state"], row["n_trials"], row["n_planned"], row["n_tasks"], row["n_tasks_run"]),
                                 ("finished", 2, 6, 6, 2))
                self.assertEqual(row["start_with"], j.start_with)
                self.assertEqual(row["mean_score"], j.mean_score)
                self.assertIn("2 of 6 planned tasks run; the rest in this job: nullius standard rerun", lines[-1])
                board = live.load(live.feed_path(job_dir)).snapshot()
                self.assertEqual((board["state"], board["counts"]["total"], board["counts"]["completed"],
                                  board["counts"].get("not_run")), ("finished", 6, 2, 4))

    def test_two_more_by_name_then_the_rest_read_as_a_job_run_whole(self):
        part = self.plan(name="part", start_with={"count": 2})
        got = self.again(part, ["t3", "t2"], reason="two more")
        self.assertEqual([x["unit"] for x in got["begun"]], ["t3", "t2"])
        self.assertEqual(parse.parse_job(part).n_tasks_run, 4)
        got = self.again(part, [], every_unfinished=True)
        self.assertEqual([x["unit"] for x in got["begun"]], ["t4", "t5"])
        whole = self.plan(name="whole")
        j, w = parse.parse_job(part), parse.parse_job(whole)
        self.assertEqual((len(j.trials), j.n_planned, j.n_tasks_run, j.n_resolved, j.state), (6, 6, 6, 6, "finished"))
        self.assertEqual({t.trial_name: t.outcome for t in j.trials}, {t.trial_name: t.outcome for t in w.trials})
        self.assertEqual(self.read(part, "result.json")["stats"]["n_cancelled_trials"], 0)
        # the records, but for the parts' own (start_with, the re-runs and their summary, each later trial's mark)
        cfg = self.read(part, "config.json")
        del cfg["standard"]["start_with"]
        self.assertEqual(steady(cfg), steady(self.read(whole, "config.json")))
        res = self.read(part, "result.json")
        self.assertEqual([len(r["begun"]) for r in res.pop("reruns")], [2, 2])
        res.pop("standard")
        self.assertEqual(steady(res), steady(self.read(whole, "result.json")))
        for t in w.trials:
            mine = self.read(part, t.trial_name, "result.json")
            self.assertEqual(mine["standard"].pop("rerun", {}).get("begun", True), True)
            self.assertEqual(steady(mine), steady(self.read(whole, t.trial_name, "result.json")), t.trial_name)
        self.assertEqual(sorted(os.listdir(part)), sorted(os.listdir(whole)))

    def test_two_attempts_each_a_task_begun_gets_both(self):
        job_dir = self.plan(start_with={"count": 2}, repeat=2)
        j = parse.parse_job(job_dir)
        self.assertEqual((len(j.trials), j.n_planned, j.n_tasks_run), (4, 12, 2))
        self.assertEqual(sorted(t.trial_name for t in j.trials),
                         ["t0__u0000r1", "t0__u0000r2", "t1__u0001r1", "t1__u0001r2"])
        got = self.again(job_dir, [], count=1)
        self.assertEqual(sorted(x["trial"] for x in got["begun"]), ["t2__u0002r1", "t2__u0002r2"])
        got = self.again(job_dir, [], task_set=self.a_set("late", ["t5", "t0"]))
        self.assertEqual(sorted(x["trial"] for x in got["begun"]), ["t5__u0005r1", "t5__u0005r2"])   # t0 had run
        self.assertEqual(sorted({x["unit"] for x in rerun.unfinished(job_dir).values()}), ["t3", "t4"])
        lines = []
        got = rerun.rerun(job_dir, [], task_set=self.a_set("done", ["t0"]), env=self.env, say=lines.append,
                          root=self.catalogue())
        self.assertEqual((got["trials"], lines), ([], ["nothing to run: every task of the set done job planned has a result"]))

    def test_the_refusals_start_nothing(self):
        root = self.catalogue()
        cases = [({"count": 7}, "--start-with 7: more tasks than the plan holds, 6 tasks of all"),
                 (self.units_file(["t1", "t9"]), "--start-with first.txt names 1 task outside the plan of 6 tasks: t9"),
                 (self.a_set("elsewhere", ["t1"], dataset="other"),
                  "--start-with: the task set elsewhere is cut from the dataset other, and the run plans all")]
        for n, (given, why) in enumerate(cases):
            with self.subTest(why=why):
                with self.assertRaises(run.Refused) as e:
                    self.plan(name=f"no-{n}", start_with=given)
                self.assertIn(why, str(e.exception))
                folder = os.path.join(self.tmp, f"no-{n}")
                self.assertFalse(os.path.exists(folder) and any(x.startswith("t") for x in os.listdir(folder)))
        with self.assertRaises(run.Refused):
            self.plan(name="no-shard", start_with={"count": 1}, shard=(1, 2))
        job_dir = self.plan(start_with={"count": 2})
        whole = base.digest(job_dir)
        for kw, why in (({"count": 5}, "--next 5: 4 tasks of the plan are left to run"),
                        ({"count": 0}, "--next 0: a whole number of tasks, at least 1"),
                        ({"task_set": self.a_set("wide", ["t1"], dataset="other")},
                         "the task set wide is cut from the dataset other, and job planned all")):
            with self.subTest(why=why):
                with self.assertRaises(run.Refused) as e:
                    self.again(job_dir, [], **kw)
                self.assertIn(why, str(e.exception))
                self.assertEqual(base.digest(job_dir), whole)
        # a set naming a task the job did not plan: a plan cut by --limit
        limited = self.plan(name="limited", start_with={"count": 1}, limit=3)
        with self.assertRaises(run.Refused) as e:
            self.again(limited, [], task_set=self.a_set("Beyond the plan", ["t2", "t4"]))
        self.assertIn("the task set Beyond the plan names 1 task limited did not plan: t4", str(e.exception))

    def test_the_command_line_names_the_part_four_ways(self):
        b = definitions.get("benchmark", "fakebench", self.catalogue())
        self.assertEqual(std_cli.start_with(b, "3"), {"count": 3})
        Path(self.tmp, "ids.txt").write_text("t2\n# a comment\nt0\n")
        self.assertEqual(std_cli.start_with(b, os.path.join(self.tmp, "ids.txt"))["units_file"]["units"], ["t2", "t0"])
        with self.assertRaises(run.Refused) as e:
            std_cli.start_with(b, os.path.join(self.tmp, "none.txt"))
        self.assertIn("--start-with", str(e.exception))
        with self.assertRaises(run.Refused):
            std_cli.start_with(b, "0")
        entry = sets.create(b, "quick", dataset="all", units=["t4"])          # the profile's own catalogue
        got = std_cli.start_with(b, "set:quick@1")
        self.assertEqual((got["dataset"], got["task_set"]["units"], got["task_set"]["id"]), ("all", ["t4"], entry["id"]))
        with self.assertRaises(sets.NotFound):
            std_cli.start_with(b, "set:nothing")
        # ids inline, as the run builder writes them
        self.assertEqual(std_cli.start_with(b, "ids:t2,t0"), {"ids": ["t2", "t0"]})
        for bad, said in (("ids:", "ids separated by commas"), ("ids:t1,,t2", "ids separated by commas"),
                          ("ids:t1, t2", "with a space around it"), ("ids:t1,t1", "t1 given more than once")):
            with self.subTest(bad=bad), self.assertRaises(run.Refused) as e:
                std_cli.start_with(b, bad)
            self.assertIn(said, str(e.exception))
        # an id outside the plan refuses the run before any unit, naming it
        with self.assertRaises(run.Refused) as e:
            self.plan(name="outside", start_with={"ids": ["t1", "nope"]})
        self.assertIn("--start-with ids:t1,nope names 1 task outside the plan of 6 tasks: nope", str(e.exception))


if __name__ == "__main__":
    unittest.main()
