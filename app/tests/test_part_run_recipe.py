"""A recipe's plan begun on a part (``start_with``): all of it, or the tasks ticked (``[ID, ...]``, the builder's
form), or from a recipe before, a number of its tasks or a task set of its dataset; compiled to ``--start-with``
(``ids:A,B`` for the ticks), said in Review as what runs first and what stays for later, refused where the run
would refuse it; the set it begins with carried to a host, and a run there begun on it. No key, no network: the
runs are the fake benchmark's control."""
from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nullius.standard import cli
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.runner import payload, rerun, run, sets, units
from tests.test_task_set_recipe import NOP, ROOT, Scratch


class StartWith(Scratch):
    def test_a_start_on_a_part_asks_nothing_at_run_and_review_counts_what_it_plans_and_begins(self):
        for datasets, limit, planned, begun in ((["all", "more"], None, 8, 5),
                                               (["all", "more"], 2, 4, 3),
                                               (["all"], 2, 2, 1)):
            with self.subTest(datasets=datasets, limit=limit):
                out = self.compile(datasets=datasets, limit=limit, start_with={"all": "1"})
                self.assertEqual(out["errors"], [])
                self.assertEqual(out["confirm"], "")
                self.assertTrue(next(r[2] for r in out["review"] if r[1] == "Scope").startswith(f"{begun} first, {planned - begun} later"))
        # every task of the datasets, with none chosen: Run asks first
        self.assertTrue(self.compile(datasets=["all", "more"])["confirm"].startswith("Run all 8 tasks of Fake bench (all, more) with "))

    def test_the_tasks_ticked_a_number_or_a_set_compile_to_the_option_and_review_says_what_runs_first(self):
        sets.create(self.b, "first", dataset="all", units=["good", "slow"])
        later = "; the other 2 stay planned, to run later from the job's page"
        for given, word, said in ((["slow", "good"], "ids:slow,good", "all · 2 tasks run first (the tasks ticked)" + later),
                                  ("2", "2", "all · 2 tasks run first (the first 2)" + later),
                                  ("set:first", "set:first", "all · 2 tasks run first (task set first)" + later)):
            with self.subTest(given=given):
                out = self.compile(start_with={"all": given})
                self.assertEqual(out["errors"], [])
                self.assertIn(f"--dataset all --start-with {word} -j 1", out["shards"][0]["command"])
                self.assertEqual(next(r for r in out["review"] if r[1] == "Dataset")[2], said)
                self.assertTrue(next(r[2] for r in out["review"] if r[1] == "Scope").startswith("2 first, 2 later"))
                self.assertEqual(out["confirm"], "")
        # the plan a set or a limit cuts is what is planned
        sets.create(self.b, "dev", dataset="all", units=["good", "wrong", "slow"])
        out = self.compile(task_sets={"all": "dev"}, start_with={"all": ["wrong"]})
        self.assertEqual(out["errors"], [])
        self.assertIn("--set dev --start-with ids:wrong", out["shards"][0]["command"])
        self.assertIn("· 1 task runs first (the tasks ticked); the other 2 stay planned", next(r for r in out["review"] if r[1] == "Dataset")[2])
        out = self.compile(task_sets={"all": "dev"}, start_with={"all": ["wrong", "good", "slow"]})
        self.assertIn("· 3 tasks run first (the tasks ticked), and nothing is left for later", next(r for r in out["review"] if r[1] == "Dataset")[2])
        out = self.compile(limit=3, start_with={"all": "1"})
        self.assertIn("--limit 3 --start-with 1", out["shards"][0]["command"])
        self.assertIn("· 1 task runs first (the first 1); the other 2 stay planned", next(r for r in out["review"] if r[1] == "Dataset")[2])

    def test_what_the_run_would_refuse_is_refused_on_its_step(self):
        sets.create(self.b, "other-ds", dataset="more", units=["good"])
        sets.create(self.b, "dev", dataset="all", units=["good"])
        sets.create(self.b, "wide", dataset="all", units=["good", "slow"])
        for kw, step, said in (({"start_with": {"all": "5"}}, "dataset", "Start with 5 tasks: all plans 4."),
                               ({"start_with": {"all": "0"}}, "dataset", "Start with a number of tasks of at least 1."),
                               ({"start_with": {"all": "set:other-ds"}}, "dataset",
                                "Start with: the task set other-ds is cut from the dataset more, not all."),
                               ({"start_with": {"all": "set:nosuch"}}, "dataset", "Fake bench has no task set nosuch to start with"),
                               ({"start_with": {"all": "first.txt"}}, "dataset", "Start with all of the tasks, a number of them, or a task set."),
                               ({"start_with": {"all": []}}, "dataset", "Start with: tick at least one task, or start with all of it."),
                               ({"start_with": {"all": ["a,b"]}}, "dataset", "Start with: an id holding a comma or a space cannot be ticked here"),
                               ({"start_with": {"all": [3]}}, "dataset", "Start with is a dataset with its tasks each."),
                               ({"task_sets": {"all": "dev"}, "start_with": {"all": ["good", "slow"]}}, "dataset",
                                "Start with: 1 of the tasks ticked are not in the task set dev."),
                               ({"start_with": {"more": "1"}}, "dataset", "Start with 1 is for more, a dataset not chosen."),
                               ({"task_sets": {"all": "dev"}, "start_with": {"all": "set:wide"}}, "dataset",
                                "Start with: the task set wide names 1 of all's tasks outside the task set dev."),
                               ({"start_with": {"all": "1"}, "where": ["local", "bench"]}, "where",
                                "A run begun on a part of its plan runs on one machine")):
            with self.subTest(kw=kw):
                out = self.compile(**kw)
                self.assertIn(recipe.STEP[step], [e["step"] for e in out["errors"]], out["errors"])
                self.assertTrue(any(e["message"].startswith(said) for e in out["errors"]), out["errors"])

    def test_the_tasks_ticked_run_first_and_the_rest_stay_planned(self):
        out = self.compile(start_with={"all": ["slow", "good"]})
        jobs = self.tmp / "jobs"
        command = out["shards"][0]["command"].replace('--from-run --jobs "${NULLIUS_JOBS:-jobs_standard}"', f"--jobs {jobs}")
        words = command.split()[3:]
        out_, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_exit_on_term"), \
                mock.patch.object(cli.settings, "load"), redirect_stdout(out_), redirect_stderr(err):
            code = cli.main(["run", *words])
        self.assertEqual(code, 0, out_.getvalue() + err.getvalue())
        cfg = json.loads((jobs / "fake-sets" / "config.json").read_text())
        self.assertEqual(cfg["standard"]["start_with"], {"units": ["good", "slow"]})          # in the plan's order
        self.assertEqual(sorted(d for d in os.listdir(jobs / "fake-sets") if "__u" in d), ["good__u0000", "slow__u0002"])
        self.assertEqual(sorted(x["unit"] for x in rerun.unfinished(str(jobs / "fake-sets")).values()), ["model", "wrong"])

    def test_a_recipe_without_it_compiles_as_before(self):
        self.assertEqual(self.compile(), self.compile(start_with={}))
        self.assertNotIn("--start-with", self.compile()["shards"][0]["command"])


class TicksAgainstThePlan(Scratch):
    """The tasks ticked, checked again at each compile against the plan the listing in the cache gives: a tick the
    plan no longer holds is refused on the Dataset step with the ids it drops. The stand-in's file has versions, so
    its title (``Fake bench 1.0``) is not its label, as at a real benchmark with versions."""
    def setUp(self):
        super().setUp()
        profile = mock.patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp / "profile")})   # a cache of its own
        profile.start()
        self.addCleanup(profile.stop)
        root = self.tmp / "versioned"
        shutil.copytree(os.path.join(ROOT, "benchmarks", "fakebench"), root / "benchmarks" / "fakebench")
        spec = json.loads((root / "benchmarks" / "fakebench" / "standard.json").read_text())
        spec.update(upstream={"license": "unknown"}, default="1.0", versions={"1.0": {}, "2.0": {}})
        (root / "benchmarks" / "fakebench" / "standard.json").write_text(json.dumps(spec))
        self.b = dict(definitions.get("benchmark", "fakebench", str(root)), controls=NOP,
                      datasets=[{"id": "all", "units": 4}, {"id": "more", "units": 4}])
        self.defs = {"benchmark": [self.b], "agent": [], "capability": []}
        self.assertNotEqual(definitions.title(self.b), self.b["label"])

    def test_a_tick_the_lowered_number_leaves_out_is_refused_and_a_tick_inside_passes(self):
        units.listing(self.b, "all")                     # as the task picker lists it
        out = self.compile(limit=1, start_with={"all": ["wrong"]})
        self.assertEqual([(e["step"], e.get("dropped")) for e in out["errors"]], [(recipe.STEP["dataset"], ["wrong"])])
        self.assertEqual(out["errors"][0]["message"], "Start with: tasks outside the current plan: wrong.")
        runner = run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai",
                         jobs_root=str(self.tmp / "jobs"), name="limited", env={}, say=lambda s: None,
                         limit=1, start_with={"ids": ["wrong"]})
        with self.assertRaisesRegex(run.Refused, "outside the plan of 1 task: wrong"):
            runner.start()                               # what Run would say at its start, said at Review now
        out = self.compile(limit=1, start_with={"all": ["good"]})
        self.assertEqual(out["errors"], [])
        self.assertIn("--limit 1 --start-with ids:good", out["shards"][0]["command"])
        self.assertEqual(self.compile(start_with={"all": ["wrong"]})["errors"], [])

    def test_review_checks_the_listing_under_the_changed_benchmark_options(self):
        self.b["options"] = {"mode": {"summary": "Tasks", "values": ["long", "short"], "default": "long"}}
        rows = {"long": ["good", "wrong"], "short": ["good"]}

        def made(bench, ds, values):
            return {"tasks": [{"id": i, "title": "", "text": "", "categories": {}} for i in rows[values["mode"]]],
                    "described": False}
        with mock.patch.object(units, "_list", made):
            for mode in rows:
                units.listing(self.b, "all", {"mode": mode})
        with mock.patch.object(units, "_list", side_effect=AssertionError("listed again")):
            self.assertEqual(self.compile(start_with={"all": ["wrong"]})["errors"], [])
            out = self.compile(bench_options={"mode": "short"}, start_with={"all": ["wrong"]})
            self.assertEqual(out["errors"][0]["dropped"], ["wrong"])
            out = self.compile(bench_options={"mode": "short"}, start_with={"all": ["good"]})
            self.assertEqual(out["errors"], [])
            self.assertIn("nothing is left for later", next(r[2] for r in out["review"] if r[1] == "Dataset"))

    def test_a_listing_not_in_the_cache_asks_nothing(self):
        self.assertNotEqual(units.state(self.b, "all")["state"], "cached")
        with mock.patch.object(units, "listing", side_effect=AssertionError("listed")):
            out = self.compile(limit=1, start_with={"all": ["pasted-id"]})
        self.assertEqual(out["errors"], [])              # Run checks them at its start
        self.assertIn("--start-with ids:pasted-id", out["shards"][0]["command"])

    def test_ids_pasted_for_a_listing_that_needs_the_full_environment_are_accepted(self):
        with mock.patch.object(units, "state", return_value={"state": "full", "fetch": [], "reason": ""}), \
                mock.patch.object(units, "listing", side_effect=AssertionError("listed")):
            out = self.compile(start_with={"all": ["pasted-a", "pasted-b"]})
        self.assertEqual(out["errors"], [])
        self.assertIn("--start-with ids:pasted-a,pasted-b", out["shards"][0]["command"])


class OnAHost(Scratch):
    def test_the_snapshot_carries_the_set_begun_with_and_the_run_there_plans_the_whole(self):
        entry = sets.create(self.b, "first", dataset="all", units=["wrong"])
        r = self.recipe(start_with={"all": "set:first"})
        self.assertEqual(payload.recipe_sets(r), [("fakebench", "first")])
        self.assertEqual(payload.recipe_sets(self.recipe(start_with={"all": "2"})), [])
        empty = self.tmp / "empty-package"
        shutil.copytree(Path(payload.PACKAGE), empty, ignore=shutil.ignore_patterns("__pycache__", "catalogue", "kits"))
        _, here = payload.build(str(self.tmp / "cache"), package=empty, catalogue=str(self.cat), sets=payload.recipe_sets(r))
        run_catalogue = self.tmp / "host" / "catalogue"
        shutil.copytree(Path(here) / "app/nullius/catalogue", run_catalogue)
        shutil.rmtree(self.cat)
        jobs = self.tmp / "host" / "jobs"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": str(run_catalogue)}), \
                mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_exit_on_term"), \
                mock.patch.object(cli.settings, "load"), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", "fakebench", "nop", "--dataset", "all", "--start-with", "set:first", "-j", "1",
                             "--name", "begun", "--jobs", str(jobs)])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        cfg = json.loads((jobs / "begun" / "config.json").read_text())
        self.assertEqual(cfg["standard"]["start_with"], {"task_set": sets.record(entry), "units": ["wrong"]})
        self.assertEqual(cfg["datasets"][0]["n_tasks"], 4)
        self.assertEqual(sorted(d for d in os.listdir(jobs / "begun") if "__u" in d), ["wrong__u0001"])
        self.assertEqual(len(rerun.unfinished(str(jobs / "begun"))), 3)
        self.assertIn("1 of 4 planned tasks run; the rest in this job: nullius standard rerun", out.getvalue())
