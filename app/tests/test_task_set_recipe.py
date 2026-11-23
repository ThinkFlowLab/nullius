"""A task set in the builder's recipe and on a host: ``task_sets`` compiles to ``--set NAME`` beside the
dataset, its label and count in the Review step; a set of another benchmark or dataset is refused; the runner snapshot a
host receives carries the recipe's sets, and a run started from it there reads the set and records it. No key,
no network: the runs are the fake benchmark's control."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nullius import progress
from nullius.standard import cli
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.requirements import install
from nullius.standard.runner import payload, sets

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
NOP = {"nop": {"source": "nullius", "from": "nothing said"}}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-set-recipe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat = self.tmp / "catalogue"
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls=NOP,
                      datasets=[{"id": "all", "units": 4}, {"id": "more", "units": 4}])
        self.memo = definitions.get("benchmark", "fakememo", ROOT)
        for patch in (mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": str(self.cat)}),
                      mock.patch.object(install, "HOME", str(self.tmp / "home")), mock.patch.object(progress, "ECHO", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.defs = {"benchmark": [self.b, self.memo], "agent": [], "capability": []}

    def recipe(self, **kw):
        r = {"version": 1, "harness": "standard", "name": "fake-sets", "title": "t", "benchmark": "fakebench", "datasets": ["all"],
             "others": ["nop"], "provider": "openai", "model": "", "concurrency": 1, "where": "local"}
        r.update(kw)
        return r

    def compile(self, **kw):
        return recipe.compile(self.recipe(**kw), self.defs)


class OnAHost(Scratch):
    """What the app sends a host for a recipe with a set: the snapshot holds the set's entry; the run there, reading
    the copy of the snapshot's catalogue a host run reads, runs the set and records it."""

    def test_the_snapshot_carries_the_recipes_sets_and_the_run_there_records_the_set(self):
        entry = sets.create(self.b, "settings", dataset="all", units=["good", "wrong"])
        r = self.recipe(task_sets={"all": "settings"})
        self.assertEqual(payload.recipe_sets(r), [("fakebench", "settings")])
        self.assertEqual(payload.recipe_sets(self.recipe()), [])
        empty = self.tmp / "empty-package"
        shutil.copytree(Path(payload.PACKAGE), empty, ignore=shutil.ignore_patterns("__pycache__", "catalogue", "kits"))
        key0, _ = payload.build(str(self.tmp / "cache"), package=empty, catalogue=str(self.cat))
        key, here = payload.build(str(self.tmp / "cache"), package=empty, catalogue=str(self.cat), sets=payload.recipe_sets(r))
        self.assertNotEqual(key, key0)
        self.assertEqual(payload.build(str(self.tmp / "cache"), package=empty, catalogue=str(self.cat),
                                       sets=payload.recipe_sets(r)), (key, here))
        carried = Path(here) / "app/nullius/catalogue/sets/fakebench/settings.json"
        self.assertEqual(json.loads(carried.read_text())["id"], entry["id"])
        for name in ("gone", "../settings"):
            with self.assertRaises(ValueError):
                payload.build(str(self.tmp / "cache"), package=empty, catalogue=str(self.cat), sets=[("fakebench", name)])
        # on the host: the snapshot's catalogue copied for the run (execution: cp -R .../app/nullius/catalogue), read there
        run_catalogue = self.tmp / "host" / "runs" / "fake-sets" / "catalogue"
        shutil.copytree(Path(here) / "app/nullius/catalogue", run_catalogue)
        shutil.rmtree(self.cat)                               # nothing of this machine's catalogue is left to read
        jobs = self.tmp / "host" / "runs" / "fake-sets" / "jobs"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": str(run_catalogue)}), \
                mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_exit_on_term"), \
                mock.patch.object(cli.settings, "load"), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", "fakebench", "nop", "--dataset", "all", "--set", "settings", "-j", "1", "--name", "fake-sets",
                             "--jobs", str(jobs)])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        cfg = json.loads((jobs / "fake-sets" / "config.json").read_text())
        self.assertEqual(cfg["standard"]["task_set"], sets.record(entry))
        self.assertEqual(sorted(d for d in os.listdir(jobs / "fake-sets") if "__u" in d), ["good__u0000", "wrong__u0001"])


if __name__ == "__main__":
    unittest.main()
