"""A run of the units a file lists (``--units FILE``): those units of the
dataset and no other, in the dataset's order, each keeping its place in the
whole dataset; an id the dataset does not hold refuses the run before any
unit starts, naming it; a file that lists an id twice, or none, is refused as
it is read; the job's config names the file, its sha256 and its units. A run without a list is unchanged. No key, no network: the runs are a
control's, but for one agent's whose model answers on this machine's
loopback."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from nullius import progress
from nullius.harbor import feed
from nullius.standard import cli
from nullius.standard.backends import container
from nullius.standard.contract import definitions
from nullius.standard.requirements import install
from nullius.standard.runner import merge, run, shard

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
NOP = {"nop": {"source": "nullius", "from": "nothing said"}}
# what the fake benchmark's driver lists, in its order
DATASET = ["good", "wrong", "slow", "model"]


def read(path):
    with open(path) as f:
        return json.load(f)


class Listed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls=NOP)
        self.jobs = os.path.join(self.tmp, "jobs")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, text, name="units.txt"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def go(self, text=None, **kw):
        """A run of the nop control, of the units ``text`` lists when it is given."""
        if text is not None:
            kw["units_file"] = run.units_file(self.file(text))
        return run.run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs,
                       env={}, say=lambda s: None, concurrency=2, **kw)

    def trials(self, job_dir):
        return sorted(d for d in os.listdir(job_dir) if "__u" in d)

    def started(self, job_dir):
        """The units the live feed says the job will run, in its order."""
        with open(os.path.join(job_dir, feed.FEED)) as f:
            return next(e for e in map(json.loads, f) if e["event"] == "job.start")["tasks"]

    def test_the_listed_units_run_and_no_other_in_the_datasets_order(self):
        job = self.go("model\nwrong\n")
        # each keeps its place in the whole dataset: model is the fourth, wrong the second
        self.assertEqual(self.trials(job), ["model__u0003", "wrong__u0001"])
        self.assertEqual(self.started(job), ["wrong", "model"])
        self.assertEqual(read(os.path.join(job, "result.json"))["n_total_trials"], 2)
        self.assertEqual(read(os.path.join(job, "config.json"))["datasets"], [{"name": "fakebench/all", "n_tasks": 2}])

    def test_an_id_the_dataset_does_not_hold_is_refused_before_any_unit_starts(self):
        r = run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                    say=lambda s: None, units_file=run.units_file(self.file("good\nnope\nmodel\nnot-either\n")))
        with self.assertRaises(run.Refused) as e:
            r.start()
        self.assertIn("nope, not-either", str(e.exception))
        self.assertIn("units.txt", str(e.exception))
        self.assertNotIn("good", str(e.exception))
        self.assertEqual(self.trials(r.job_dir), [])                                  # no unit began
        for name in ("config.json", "result.json", feed.FEED):
            self.assertFalse(os.path.exists(os.path.join(r.job_dir, name)), name)
        events = progress.read(r.job_dir)
        self.assertEqual(events[-1]["stage"], "refused")
        self.assertIn("nope, not-either", progress.refusal(events))
        self.assertTrue(r.drivers and all(d.p.poll() is not None for d in r.drivers))    # its driver is not left running

    def test_on_the_container_backend_a_refused_list_removes_the_jobs_containers(self):
        """On the container backend a driver, or a door it was given, may run in a container of the job's:
        a run whose list its dataset does not hold removes them, as a run whose driver cannot list its
        units does."""
        with mock.patch.object(container, "unusable", return_value=None), \
                mock.patch.object(container, "present", return_value=True), \
                mock.patch.object(container, "base_image", return_value="nullius/base:x"), \
                mock.patch.object(container, "check_env", return_value={}), \
                mock.patch.object(container, "sweep") as sweep:
            r = run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                        say=lambda s: None, backend="container", units_file=run.units_file(self.file("good\nnope\n")))
            with self.assertRaisesRegex(run.Refused, "nope"):
                r.start()
        sweep.assert_called_once_with(r.name)
        self.assertTrue(r.drivers and all(d.p.poll() is not None for d in r.drivers))
        self.assertEqual(self.trials(r.job_dir), [])

    def test_an_id_given_twice_is_refused_and_named(self):
        with self.assertRaises(run.Refused) as e:
            run.units_file(self.file("good\nwrong\n good\nmodel\nwrong\n"))
        self.assertIn("good, wrong", str(e.exception))
        self.assertIn("more than once", str(e.exception))

    def test_a_file_that_lists_no_unit_is_refused(self):
        for text in ("", "\n\n", "# nothing yet\n\n  # still nothing\n"):
            with self.subTest(text=text), self.assertRaises(run.Refused) as e:
                run.units_file(self.file(text))
            self.assertIn("lists no unit", str(e.exception))

    def test_a_file_that_cannot_be_read_as_text_is_refused(self):
        with self.assertRaisesRegex(run.Refused, "cannot read it"):
            run.units_file(os.path.join(self.tmp, "absent.txt"))
        path = os.path.join(self.tmp, "binary.txt")
        with open(path, "wb") as f:
            f.write(b"good\n\xff\xfe\x00wrong\n")
        with self.assertRaisesRegex(run.Refused, "not UTF-8 text"):
            run.units_file(path)

    def test_the_list_with_a_limit_runs_its_first_units_in_the_datasets_order(self):
        job = self.go("model\nwrong\ngood\n", limit=2)
        self.assertEqual(self.trials(job), ["good__u0000", "wrong__u0001"])
        self.assertEqual(self.started(job), ["good", "wrong"])

    def test_the_list_with_a_repeat_runs_each_listed_unit_that_many_times(self):
        job = self.go("model\ngood\n", repeat=2)
        # each run of a unit a trial of its own, tagged with the unit's place in the whole dataset and the run's number
        self.assertEqual(self.trials(job), ["good__u0000r1", "good__u0000r2", "model__u0003r1", "model__u0003r2"])
        self.assertEqual(self.started(job), ["good", "model"])
        self.assertEqual(read(os.path.join(job, "result.json"))["n_total_trials"], 4)
        cfg = read(os.path.join(job, "config.json"))["standard"]
        self.assertEqual((cfg["repeat"], cfg["units_file"]["units"]), (2, ["model", "good"]))

    def test_the_list_with_a_shard_and_a_limit_takes_the_listed_units_then_the_shard_then_the_limit(self):
        """The driver's units, then the listed ones, then the shard, then the limit. Shard 1 of 2 holds good,
        wrong and model. The list leaves good out, and the shard's first listed unit is wrong. The other
        shard holds slow alone."""
        jobs = [self.go("model\nwrong\nslow\n", shard=(i, 2), limit=1, name=f"run-shard-{i}of2", launch="L") for i in (1, 2)]
        self.assertEqual([shard.of(u, 2) for u in DATASET], [1, 1, 2, 1])
        self.assertEqual([self.trials(j) for j in jobs], [["wrong__u0001"], ["slow__u0002"]])
        for job, planned in zip(jobs, (["wrong"], ["slow"])):
            rec = read(os.path.join(job, "config.json"))["standard"]["shard"]
            self.assertEqual((rec["units_total"], rec["planned"], rec["limit"]), (3, planned, 1))   # the listed units are its whole
        got = merge.merge(jobs, os.path.join(self.jobs, "run"))
        self.assertEqual(got["trials"], 2)
        self.assertIn("1 of the 3 listed units are not in the merge", got["notes"])

    def test_the_config_records_the_files_name_its_sha256_and_its_units(self):
        text = "# two of them\n\n  model  \n   # good\nwrong\r\n\n"      # comments, blank lines and spaces say nothing
        job = self.go(text)
        self.assertEqual(read(os.path.join(job, "config.json"))["standard"]["units_file"],
                         {"name": "units.txt", "sha256": hashlib.sha256(text.encode()).hexdigest(), "units": ["model", "wrong"]})

    def test_force_never_merges_shards_whose_lists_hold_different_units(self):
        """``--force`` merges shards of different launches, models or agents; a merged job records one
        list, so shards of different lists, or of a list and none, are refused whatever it says."""
        left = self.go("good\n", shard=(1, 2), name="left-shard-1of2", launch="L")
        right = self.go("slow\n", shard=(2, 2), name="right-shard-2of2", launch="L")
        whole = self.go(shard=(2, 2), name="whole-shard-2of2", launch="L")
        for pair in ((left, right), (left, whole)):
            with self.subTest(pair=[os.path.basename(p) for p in pair]):
                for force in (False, True):
                    with self.assertRaisesRegex(merge.Refused, "units file"):
                        merge.merge(list(pair), os.path.join(self.jobs, "merged"), force=force)
                self.assertFalse(os.path.exists(os.path.join(self.jobs, "merged")))

    def test_lists_of_the_same_units_merge_whatever_their_files_names_comments_or_order(self):
        """``merge`` compares the units each list holds, after the file's reading rules, never the file's
        bytes: a name, a comment, a line ending or an order of its own changes the sha256, not the units."""
        with open(os.path.join(self.tmp, "right.txt"), "wb") as f:
            f.write(b"# the same two\r\nmodel\r\ngood\r\n")
        left = self.go("good\nmodel\n", shard=(1, 2), name="left-shard-1of2", launch="L")
        right = run.run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                        say=lambda s: None, shard=(2, 2), name="right-shard-2of2", launch="L",
                        units_file=run.units_file(os.path.join(self.tmp, "right.txt")))
        records = [read(os.path.join(j, "config.json"))["standard"]["units_file"] for j in (left, right)]
        self.assertNotEqual(records[0]["sha256"], records[1]["sha256"])
        self.assertEqual((records[0]["name"], records[1]["name"]), ("units.txt", "right.txt"))
        got = merge.merge([left, right], os.path.join(self.jobs, "merged"))
        self.assertEqual(got["trials"], 2)

    def test_a_list_and_a_task_set_together_are_refused_before_any_unit(self):
        listed = run.units_file(self.file("good\n"))
        record = {"id": "0" * 32, "name": "dev", "revision": 1, "hash": "0" * 64, "units": ["model"]}
        with self.assertRaisesRegex(run.Refused, "from a file or from a task set, not both"):
            run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                    say=lambda s: None, units_file=listed, task_set=record)
        self.assertFalse(os.path.exists(self.jobs))

    def test_a_task_sets_units_run_as_a_lists_do_and_its_record_is_kept(self):
        record = {"id": "0" * 32, "name": "dev", "revision": 2, "hash": "0" * 64, "units": ["model", "wrong"]}
        job = self.go(task_set=record, limit=1)
        self.assertEqual(self.trials(job), ["wrong__u0001"])
        cfg = read(os.path.join(job, "config.json"))["standard"]
        self.assertEqual(cfg["task_set"], record)
        self.assertNotIn("units_file", cfg)


class FromTheCommandLine(unittest.TestCase):
    """``nullius standard run ... --units FILE``: the file read where the command runs, with or
    without ``--from-run``; a list it refuses ends the command with ``refused:`` and exit 2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls=NOP)
        self.jobs = os.path.join(self.tmp, "jobs")
        for patch in (mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_exit_on_term"),
                      mock.patch.object(cli.settings, "load"), mock.patch.object(progress, "ECHO", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["run", "fakebench", "nop", "--jobs", self.jobs, "-j", "2", *args])
        return code, out.getvalue()

    def file(self, text):
        path = os.path.join(self.tmp, "dev.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_capped_run_fetches_the_price_list_first(self):
        offline = "could not fetch the price list from the list's address: offline"
        with mock.patch.object(cli.price_list, "update", side_effect=ValueError(offline)) as update, \
                mock.patch.object(cli.run, "run_and_report", return_value=("job", False)) as ran:
            self.cli("--max-usd", "1")
        update.assert_called_once_with()
        self.assertEqual(ran.call_args.kwargs["prices_unreachable"], offline)          # the run goes on uncapped, saying why
        with mock.patch.object(cli.price_list, "update") as update, \
                mock.patch.object(cli.run, "run_and_report", return_value=("job", False)) as ran:
            self.cli()
        update.assert_not_called()                                                      # no cap: nothing to price
        self.assertIsNone(ran.call_args.kwargs["prices_unreachable"])

    def test_the_command_runs_the_listed_units_with_and_without_from_run(self):
        path = self.file("# a pair\nslow\ngood\n")
        for extra in ([], ["--from-run"]):
            with self.subTest(extra=extra):
                code, out = self.cli("--units", path, "--name", "listed" + "".join(extra), *extra)
                self.assertEqual(code, 0, out)
                job = out.strip().splitlines()[-1]
                self.assertEqual(sorted(d for d in os.listdir(job) if "__u" in d), ["good__u0000", "slow__u0002"])
                self.assertEqual(read(os.path.join(job, "config.json"))["standard"]["units_file"]["units"], ["slow", "good"])

    def test_a_list_the_run_cannot_take_is_refused_with_exit_2(self):
        for text, said in (("good\ngood\n", "more than once"), ("# none\n", "lists no unit"), ("good\nnope\n", "nope")):
            with self.subTest(text=text):
                code, out = self.cli("--units", self.file(text))
                self.assertEqual(code, 2)
                last = out.strip().splitlines()[-1]
                self.assertTrue(last.startswith("refused:"), out)
                self.assertIn(said, last)


if __name__ == "__main__":
    unittest.main()
