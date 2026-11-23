"""Task sets (``standard/runner/sets.py``): a named list of a benchmark's units in the
user catalogue, with an id, a label and the name made from it, the version it
was made at, and no option. The content hash says the same for the same ids in
any order and changes with an id or the dataset; a set is frozen: nothing adds
to its list, and a file of several lists from before reads as its latest,
unchanged; a label makes a name in lower case with hyphens, a second equal one
``-2``; a name that would leave the benchmark's folder is refused; a set is
made from a file or from a job's trials by outcome or by name; an entry comes
in from its file or from a job that ran it. ``run --set`` takes the set's
dataset, refuses what contradicts it, runs under any options it is given,
refuses ids its driver does not list under them (naming both versions for a set
made at another), and records the set in the job's config. No key, no network:
the runs are a control's."""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from nullius import progress
from nullius.harbor import feed
from nullius.standard import cli
from nullius.standard.contract import definitions
from nullius.standard.requirements import install
from nullius.standard.runner import job, merge, run, sets, shard

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
NOP = {"nop": {"source": "nullius", "from": "nothing said"}}
MODE = {"mode": {"summary": "a mode the listing follows", "values": ["plain", "hard"], "default": "plain"}}
DATASET = ["good", "wrong", "slow", "model"]


def read(path):
    with open(path) as f:
        return json.load(f)


class Scratch(unittest.TestCase):
    """A catalogue of its own (``NULLIUS_CATALOGUE``), the fake benchmark with a control and an option."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-sets-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat = os.path.join(self.tmp, "catalogue")
        self.jobs = os.path.join(self.tmp, "jobs")
        self.b = dict(definitions.get("benchmark", "fakebench", ROOT), controls=NOP, options=MODE)
        for patch in (mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": self.cat}), mock.patch.object(install, "HOME", os.path.join(self.tmp, "home")),
                      mock.patch.object(cli, "_bench", return_value=self.b), mock.patch.object(cli, "_find_bench", return_value=self.b),
                      mock.patch.object(cli, "_exit_on_term"), mock.patch.object(cli.settings, "load"),
                      mock.patch.object(progress, "ECHO", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def file(self, text, name="units.txt"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def make(self, label, text, *extra):
        name = sets.name_for("fakebench", label)
        code, out, err = self.cli("set", "create", "fakebench", label, "--dataset", "all", "--units", self.file(text), *extra)
        self.assertEqual(code, 0, err)
        return sets.load("fakebench", name)

    def trials(self, job_dir):
        return sorted(d for d in os.listdir(job_dir) if "__u" in d)


class Hash(Scratch):
    def test_the_same_ids_in_another_order_or_from_a_file_with_comments_hash_the_same(self):
        a = self.make("a", "good\nmodel\n")
        b = self.make("b", "# the same two\n\n  model  \r\ngood\n")
        self.assertEqual(sets.revision(a)["hash"], sets.revision(b)["hash"])
        self.assertNotEqual(a["id"], b["id"])                        # two entries of equal lists stay two sets
        self.assertEqual(sets.content_hash("fakebench", "all", ["good", "model"]),
                         sets.content_hash("fakebench", "all", ["model", "good"]))

    def test_it_changes_with_an_id_or_the_dataset(self):
        base = sets.content_hash("fakebench", "all", ["good", "model"])
        for other in (sets.content_hash("fakebench", "all", ["good", "slow"]),
                      sets.content_hash("fakebench", "responding", ["good", "model"])):
            self.assertNotEqual(base, other)
        self.assertEqual(sets.revision(self.make("plain", "good\nmodel\n"))["hash"], base)

    def test_a_file_changed_by_hand_is_refused_where_it_is_read(self):
        self.make("edited", "good\nmodel\n")
        path = sets.path_of("fakebench", "edited")
        entry = read(path)
        entry["revisions"][0]["units"].append("slow")
        with open(path, "w") as f:
            json.dump(entry, f)
        with self.assertRaisesRegex(sets.Refused, "changed by hand"):
            sets.load("fakebench", "edited")


class NoOption(Scratch):
    def test_set_create_takes_no_benchmark_option(self):
        for source in (["--units", self.file("good\n")], ["--search", "good"]):
            with self.subTest(source=source[0]):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                    cli.main(["set", "create", "fakebench", "dev", "--dataset", "all", *source, "--bench-option", "mode=hard"])
                self.assertEqual((stop.exception.code, out.getvalue()), (2, ""))
                self.assertIn("usage:", err.getvalue())
                self.assertIn("unrecognized arguments: --bench-option", err.getvalue())
        self.assertFalse(os.path.exists(self.cat))

    def test_the_entry_holds_no_option_and_an_earlier_entry_that_does_is_refused_where_it_is_read(self):
        entry = self.make("dev", "good\nmodel\n")
        self.assertEqual(sorted(read(sets.path_of("fakebench", "dev"))),
                         ["benchmark", "dataset", "description", "id", "label", "name", "revisions", "schema"])
        self.assertEqual(sorted(sets.record(entry, sets.revision(entry))), ["hash", "id", "label", "name", "revision", "units"])
        for field, value in (("benchmark_options", {"mode": "hard"}), ("setup", "yours"), ("benchmark_version", "1")):
            with self.subTest(field=field):
                path = sets.path_of("fakebench", "dev")
                with open(path, "w") as f:
                    json.dump(dict(entry, schema=sets.SCHEMA, **{field: value}), f)
                with self.assertRaisesRegex(sets.Refused, f"holds {field}, which a task set does not hold"):
                    sets.load("fakebench", "dev")
                code, out, err = self.cli("set", "show", "fakebench", "dev")
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"holds {field}", err)


class Frozen(Scratch):
    def earlier(self, name="dev"):
        """A set made before sets were frozen, as its file was written then: two lists, no label, no version."""
        folder = os.path.join(self.cat, "sets", "fakebench")
        os.makedirs(folder, exist_ok=True)
        lists = [["good", "model"], ["good", "model", "slow"]]
        entry = {"schema": sets.SCHEMA, "id": "a" * 32, "name": name, "description": "from before", "benchmark": "fakebench",
                 "dataset": "all", "revisions": [{"revision": i, "date": f"2026-11-1{i}T10:00:00Z", "note": "", "units": u,
                                                  "hash": sets.content_hash("fakebench", "all", u)} for i, u in enumerate(lists, 1)]}
        path = os.path.join(folder, name + ".json")
        with open(path, "w") as f:
            json.dump(entry, f, indent=2)
        return path

    def test_a_set_of_several_lists_from_before_reads_as_its_latest_and_is_not_rewritten(self):
        path = self.earlier()
        with open(path, "rb") as f:
            before = f.read()
        entry = sets.load("fakebench", "dev")
        self.assertEqual((sets.label_of(entry), sets.latest(entry)["units"]), ("dev", ["good", "model", "slow"]))
        self.assertEqual(sets.rows("fakebench"), [{"name": "dev", "label": "dev", "description": "from before", "dataset": "all",
                                                   "version": "", "units": 3, "date": "2026-11-12T10:00:00Z"}])
        code, out, err = self.cli("set", "show", "fakebench", "dev")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("dev: 3 units of fakebench all\n"), out)
        self.assertNotIn("revision", out)
        # a recipe or a command from before names its latest list as NAME@2: the same set; NAME@1 is refused
        self.assertEqual(sets.for_run(self.b, "dev@2")[0]["units"], ["good", "model", "slow"])
        code, out, err = self.cli("set", "show", "fakebench", "dev@1")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("dev@1 names an earlier list of the task set dev: a set is read as its latest list now; name it dev", err)
        code, out, err = self.run_nop("--set", "dev", "--name", "from-before")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.trials(os.path.join(self.jobs, "from-before")), ["good__u0000", "model__u0003", "slow__u0002"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def run_nop(self, *args):
        return self.cli("run", "fakebench", "nop", "--jobs", self.jobs, "-j", "2", *args)


class Names(Scratch):
    def test_a_label_makes_a_name_in_lower_case_with_hyphens_and_a_second_one_takes_two(self):
        for label, name in (("Custom task set", "custom-task-set"), ("custom task set", "custom-task-set-2"),
                            ("  Custom  task set!  ", "custom-task-set-3"), ("Café au lait", "cafe-au-lait"),
                            ("a/b", "a-b"), ("../up", "up"), ("x..y", "x-y"), ("ÉTÉ 2026", "ete-2026"), ("日本", "task-set"),
                            ("dev", "dev")):
            with self.subTest(label=label):
                code, out, err = self.cli("set", "create", "fakebench", label, "--dataset", "all", "--units", self.file("good\n"))
                self.assertEqual(code, 0, err)
                entry = sets.load("fakebench", name)
                self.assertEqual(entry["label"], label.strip())
                self.assertTrue(out.startswith(f"wrote {sets.path_of('fakebench', name)}: "), out)
        self.assertEqual(sets.slug("b" * 70), "b" * 64)
        self.assertEqual(sets.name_for("fakebench", "b" * 70), "b" * 64)
        sets.create(self.b, "b" * 70, dataset="all", units=["good"])
        self.assertEqual(sets.name_for("fakebench", "b" * 70), "b" * 62 + "-2")

    def test_a_label_with_no_letter_or_digit_on_two_lines_or_too_long_is_refused(self):
        for label, said in (("", "needs a name"), ("   ", "needs a name"), ("..", "needs a letter or a digit"),
                            ("a\nb", "one line"), ("x" * 81, "at most 80 characters")):
            with self.subTest(label=label):
                code, out, err = self.cli("set", "create", "fakebench", label, "--dataset", "all", "--units", self.file("good\n"))
                self.assertEqual((code, out), (2, ""))
                self.assertIn(said, err)
        self.assertFalse(os.path.exists(os.path.join(self.cat, "sets", "fakebench")))

    def test_a_name_that_would_leave_the_benchmarks_folder_is_refused(self):
        for name in ("a/b", "..", "", ".hidden", "../up", "a\\b", "x..y"):
            with self.subTest(name=name):
                code, out, err = self.cli("set", "show", "fakebench", name)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("not a task set's name", err)

    def test_links_cannot_escape_through_parent_folders_or_an_entry(self):
        entry = self.make("dev", "good\n")
        exported = self.file(json.dumps(read(sets.path_of("fakebench", "dev"))), "export.json")
        for relative in ("sets", "sets/fakebench", "sets/fakebench/dev.json"):
            with self.subTest(path=relative):
                path = os.path.join(self.cat, relative)
                outside = os.path.join(self.tmp, "outside")
                os.rename(path, outside)
                os.symlink(outside, path)
                try:
                    for action in (lambda: sets.load("fakebench", "dev"),
                                   lambda: sets.write(entry),
                                   lambda: sets.rename("fakebench", "dev", "other"),
                                   lambda: sets.import_path(exported)):
                        with self.assertRaisesRegex(sets.Refused, "outside"):
                            action()
                    if relative != "sets/fakebench/dev.json":
                        with self.assertRaisesRegex(sets.Refused, "outside"):
                            sets.create(self.b, "new", dataset="all", units=["good"])
                    with mock.patch.object(sets.os, "listdir", wraps=os.listdir) as listed:
                        try:
                            found, bad = sets.entries()
                        except sets.Refused as e:
                            self.assertIn("outside", str(e))
                        else:
                            self.assertEqual(found, [])
                            self.assertTrue(any("outside" in problem for problem in bad))
                        self.assertNotIn(mock.call(path), listed.call_args_list)
                finally:
                    os.unlink(path)
                    os.rename(outside, path)
                self.assertEqual(sets.load("fakebench", "dev"), entry)
                self.assertFalse(os.path.exists(sets.path_of("fakebench", "new")))

    def test_a_label_that_exists_makes_another_set(self):
        first = self.make("dev", "good\n")
        code, out, err = self.cli("set", "create", "fakebench", "dev", "--dataset", "all", "--units", self.file("model\n"))
        self.assertEqual(code, 0, err)
        self.assertIn(": dev-2 (dev), 1 task of all", out)
        self.assertEqual(sets.load("fakebench", "dev"), first)
        self.assertEqual(sets.revision(sets.load("fakebench", "dev-2"))["units"], ["model"])


class RenameAndDelete(Scratch):
    def test_a_rename_keeps_the_id_and_the_list_makes_the_name_again_and_the_id_finds_it(self):
        first = self.make("dev", "good\nmodel\n", "--description", "two of them")
        code, out, err = self.cli("set", "rename", "fakebench", "dev", "Misses of June")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("renamed dev to misses-of-june (Misses of June) ("), out)
        self.assertIn("its id and its tasks kept", out)
        got = sets.load("fakebench", "misses-of-june")
        self.assertEqual((got["id"], got["label"], got["description"], got["revisions"]),
                         (first["id"], "Misses of June", "two of them", first["revisions"]))
        self.assertEqual(sorted(os.listdir(os.path.join(self.cat, "sets", "fakebench"))), ["misses-of-june.json"])
        self.assertEqual(sets.by_id("fakebench", first["id"], "dev")["name"], "misses-of-june")
        # another set made under the old name is another set: the id still finds the renamed one
        self.make("dev", "good\n")
        self.assertEqual(sets.by_id("fakebench", first["id"], "dev")["name"], "misses-of-june")
        self.assertIsNone(sets.by_id("fakebench", "0" * 32, "dev"))
        # a label that makes the same name changes the label alone; one another set holds makes the next name
        self.assertEqual(sets.rename("fakebench", "misses-of-june", "misses of june")["name"], "misses-of-june")
        self.assertEqual(sets.rename("fakebench", "misses-of-june", "Dev")["name"], "dev-2")

    def test_its_own_label_an_empty_one_and_a_set_not_there_are_refused(self):
        dev = self.make("dev", "good\n")
        other = self.make("other", "model\n")
        for new, code, said in (("dev", 2, "is named 'dev' already"), ("", 2, "needs a name"), ("..", 2, "letter or a digit")):
            with self.subTest(new=new):
                got = self.cli("set", "rename", "fakebench", "dev", new)
                self.assertEqual(got[:2], (code, ""))
                self.assertIn(said, got[2])
        got = self.cli("set", "rename", "fakebench", "nowhere", "x")
        self.assertEqual(got[:2], (3, ""))
        self.assertIn("no task set 'nowhere'", got[2])
        self.assertEqual((sets.load("fakebench", "dev"), sets.load("fakebench", "other")), (dev, other))
        self.assertEqual(sorted(os.listdir(os.path.join(self.cat, "sets", "fakebench"))), ["dev.json", "other.json"])
        self.assertEqual(sets.rename("fakebench", "dev", "other")["name"], "other-2")

    def test_a_delete_removes_the_file_and_says_what_it_held(self):
        self.make("Dev set", "good\nmodel\n")
        path = sets.path_of("fakebench", "dev-set")
        code, out, err = self.cli("set", "delete", "fakebench", "dev-set")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), f"deleted {path}: dev-set (Dev set), 2 units; jobs that ran it keep their record")
        self.assertFalse(os.path.exists(path))
        code, out, err = self.cli("set", "delete", "fakebench", "dev-set")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("no task set 'dev-set'", err)

    def test_a_delete_of_a_link_removes_the_link_and_not_the_set_it_leads_to(self):
        self.make("dev", "good\n")
        folder = os.path.join(self.cat, "sets", "fakebench")
        path = os.path.join(folder, "alias.json")
        os.symlink(os.path.join(folder, "dev.json"), path)
        code, out, err = self.cli("set", "delete", "fakebench", "alias")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), f"deleted {path}: an entry this version could not read")
        self.assertEqual(sorted(os.listdir(folder)), ["dev.json"])
        self.assertEqual(sets.revision(sets.load("fakebench", "dev"))["units"], ["good"])

    def test_an_entry_of_the_earlier_format_is_refused_plainly_and_can_be_deleted(self):
        entry = self.make("old", "good\n")
        path = sets.path_of("fakebench", "old")
        with open(path, "w") as f:
            json.dump(dict(entry, schema=sets.SCHEMA, benchmark_options={"mode": "hard"}, setup="yours"), f)
        with self.assertRaises(sets.Earlier) as refused:
            sets.load("fakebench", "old")
        self.assertIn(path, str(refused.exception))
        said = sets.plain(refused.exception, "fakebench", "old")
        self.assertEqual(said, "The task set old is of an earlier format, which kept a run's options: delete it and make it again.")
        self.assertNotIn(self.tmp, said)
        code, out, err = self.cli("set", "rename", "fakebench", "old", "new")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("earlier format", err)
        code, out, err = self.cli("set", "delete", "fakebench", "old")
        self.assertEqual((code, out.strip(), err), (0, f"deleted {path}: an entry this version could not read", ""))
        self.assertFalse(os.path.exists(path))


class FromAJob(Scratch):
    def job(self, name="graded"):
        """A job as a run writes it: good passed, wrong failed, slow rate limited, a unit whose id has a slash passed."""
        d = os.path.join(self.jobs, name)
        os.makedirs(d)
        other = dict(definitions.control(self.b, "nop"), _kind="control")
        units = ["good", "wrong", "slow", "group/model"]
        job.write_config(d, job=name, bench=self.b, other=other, model="", provider="openai", dataset="all", units=units,
                         concurrency=1, granted=False, bench_options={"mode": "hard"})
        ends = {"good": ({"reward": 1.0}, None), "wrong": ({"reward": 0.0}, None), "slow": (None, ("RateLimitError", "429")),
                "group/model": ({"reward": 1.0}, None)}
        results = []
        for place, u in enumerate(units):
            score, exc = ends[u]
            trial = os.path.join(d, f"{u.rpartition('/')[2]}__u{place:04d}")
            os.makedirs(trial)
            results.append(job.write_trial(trial, job=name, bench=self.b, other=other, model="", unit=u, t0=1.0, t1=2.0,
                                           produce=(1.0, 2.0), score=score, exception=exc, calls=[], trajectory=None))
        job.write_result(d, name, 1.0, 3.0, results, len(units))
        return d

    def test_outcome_takes_the_trials_of_those_outcomes_by_their_full_unit_ids(self):
        d = self.job()
        for name, outcome, want in (("passed", "passed", ["good", "group/model"]), ("misses", "failed,infra", ["wrong", "slow"]),
                                    ("limited", "rate_limited", ["slow"])):
            with self.subTest(outcome=outcome):
                code, out, err = self.cli("set", "create", "fakebench", name, "--from-job", d, "--outcome", outcome)
                self.assertEqual(code, 0, err)
                entry = sets.load("fakebench", name)
                self.assertEqual(sets.revision(entry)["units"], want)
                self.assertEqual(entry["dataset"], "all")
                self.assertNotIn("benchmark_options", entry)       # the job ran under mode=hard; the set holds none
                self.assertEqual(sets.revision(entry)["hash"], sets.content_hash("fakebench", "all", want))
                self.assertIn(f"outcomes {outcome}", sets.revision(entry)["note"])

    def test_unit_takes_the_ones_named(self):
        d = self.job()
        code, out, err = self.cli("set", "create", "fakebench", "two", "--from-job", d, "--unit", "group/model", "--unit", "wrong")
        self.assertEqual(code, 0, err)
        self.assertEqual(sets.revision(sets.load("fakebench", "two"))["units"], ["wrong", "group/model"])
        code, out, err = self.cli("set", "create", "fakebench", "none", "--from-job", d, "--unit", "model")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no trial of model", err)                       # the trial's name is not its unit's id
        code, out, err = self.cli("set", "create", "fakebench", "bad", "--from-job", d, "--outcome", "sideways")
        self.assertEqual(code, 2)
        self.assertIn("--outcome sideways", err)


class Import(Scratch):
    def test_an_entry_comes_in_from_its_file_and_another_list_under_its_id_is_refused(self):
        entry = self.make("dev", "good\nmodel\n")
        exported = os.path.join(self.tmp, "dev.json")
        shutil.copy(sets.path_of("fakebench", "dev"), exported)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": elsewhere}):
            code, out, err = self.cli("set", "import", exported)
            self.assertEqual(code, 0, err)
            self.assertEqual(read(sets.path_of("fakebench", "dev")), read(exported))
            code, out, err = self.cli("set", "import", exported)
            self.assertEqual(code, 0, err)
            self.assertIn("here already", out)
        # here, the same id with another list
        other = read(exported)
        other["revisions"][0]["units"] = ["good", "wrong"]
        other["revisions"][0]["hash"] = sets.content_hash("fakebench", "all", ["good", "wrong"])
        with open(exported, "w") as f:
            json.dump(other, f)
        code, out, err = self.cli("set", "import", exported)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the same id with another list", err)
        self.assertEqual(sets.load("fakebench", "dev")["id"], entry["id"])
        self.assertEqual(sets.revision(sets.load("fakebench", "dev"))["units"], ["good", "model"])

    def test_import_checks_every_consumed_field_before_storing_an_entry_or_job_record(self):
        entry = dict(self.make("dev", "good\n"), schema=sets.SCHEMA)
        cases = [("entry", k, None, True) for k in ("id", "name", "description", "benchmark", "dataset")]
        cases += [("entry", k, v, False) for k, v in (("label", ""), ("label", 1), ("version", []), ("version", ""))]
        cases += [("revision", k, None, True) for k in sets.REVISION]
        cases += [("entry", k, v, False) for k, v in (
            ("id", 1), ("name", []), ("description", {}), ("benchmark", False), ("dataset", {}),
            ("benchmark_options", {}), ("setup", "yours"))]
        cases += [("revision", k, v, False) for k, v in (
            ("revision", True), ("date", None), ("date", []), ("date", ""), ("note", None),
            ("note", {}), ("units", [1]), ("units", [{}]), ("units", "good"), ("hash", []))]
        rec = sets.record(entry, sets.revision(entry))
        jobs = [(k, None, True) for k in sets.RECORD]
        jobs += [(k, v, False) for k, v in (("id", 1), ("name", []), ("revision", True), ("revision", 0),
                                            ("units", [1]), ("units", "good"), ("units", []), ("hash", []),
                                            ("hash", "0" * 64))]
        source_job = os.path.join(self.tmp, "source-job")
        os.makedirs(source_job)
        for index, (section, field, value, missing) in enumerate(cases + [("job",) + c for c in jobs]):
            elsewhere = os.path.join(self.tmp, f"elsewhere-{index}")
            with self.subTest(section=section, field=field, value=value, missing=missing):
                if section == "job":
                    got = dict(rec)
                    target = got
                else:
                    got = copy.deepcopy(entry)
                    target = got if section == "entry" else got["revisions"][0]
                if missing:
                    del target[field]
                else:
                    target[field] = value
                if section == "job":
                    self.file(json.dumps({"runner": "standard", "datasets": [{"name": "fakebench/all"}],
                                          "standard": {"benchmark": "fakebench", "task_set": got}}),
                              "source-job/config.json")
                    path = source_job
                else:
                    path = self.file(json.dumps(got), "incoming.json")
                with mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": elsewhere}):
                    code, out, err = self.cli("set", "import", path)
                self.assertEqual((code, out), (2, ""), err)
                self.assertTrue(err.startswith("refused: "), err)
                self.assertFalse(os.path.exists(elsewhere))

    def test_an_entry_comes_in_from_a_job_that_ran_it(self):
        entry = self.make("dev", "model\ngood\n", "--description", "two of them")
        code, out, err = self.cli("run", "fakebench", "nop", "--jobs", self.jobs, "--set", "dev", "--bench-option", "mode=hard",
                                  "--name", "ran")
        self.assertEqual(code, 0, out + err)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": elsewhere}):
            code, out, err = self.cli("set", "import", os.path.join(self.jobs, "ran"))
            self.assertEqual(code, 0, err)
            got = sets.load("fakebench", "dev")
            # the record holds the set's id, name, label, hash and units: the rest is the job's or the import's
            self.assertEqual({k: got[k] for k in ("id", "name", "label", "benchmark", "dataset", "description")},
                             {**{k: entry[k] for k in ("id", "name", "label", "benchmark", "dataset")}, "description": ""})
            self.assertEqual([{k: r[k] for k in ("revision", "units", "hash")} for r in got["revisions"]],
                             [{k: r[k] for k in ("revision", "units", "hash")} for r in entry["revisions"]])
            self.assertEqual(got["revisions"][0]["note"], "from the job ran")
            # the entry exported from here then adds nothing there: the same id, revision and hash
            code, out, err = self.cli("set", "import", sets.path_of("fakebench", "dev", self.cat))
            self.assertEqual(code, 0, err)
            self.assertIn("here already", out)
        code, out, err = self.cli("set", "import", self.jobs)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("no job", err)
        # another set under the name: refused, the entry here unchanged
        shutil.rmtree(os.path.join(self.cat, "sets"))
        self.make("dev", "model\ngood\n")
        code, out, err = self.cli("set", "import", os.path.join(self.jobs, "ran"))
        self.assertEqual(code, 2)
        self.assertIn("another id", err)


class Running(Scratch):
    def run_cli(self, *args):
        return self.cli("run", "fakebench", "nop", "--jobs", self.jobs, "-j", "2", *args)

    def test_an_empty_set_is_refused_before_any_unit(self):
        code, out, err = self.run_cli("--set", "", "--name", "empty")
        self.assertEqual(code, 2, out + err)
        self.assertIn("not a task set's name", out)
        self.assertFalse(os.path.exists(self.jobs))

    def test_each_contradiction_is_refused_before_any_unit_with_a_plain_message(self):
        self.make("dev", "good\nmodel\n")
        for args, said in ((["--dataset", "responding"], "--dataset responding: the task set dev is cut from the dataset all"),
                           (["--units", self.file("good\n")], "the set lists the units, give one or the other"),
                           (["--set", "nowhere"], "no task set 'nowhere' of fakebench")):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*(["--set", "dev"] if "--set" not in args else []), *args)
                self.assertEqual(code, 2, out + err)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                self.assertTrue(out.startswith("refused: "), out)
                self.assertIn(said, out)
                self.assertFalse(os.path.exists(self.jobs))           # no job was begun
        code, out, err = self.run_cli("--set", "dev", "--dataset", "all", "--bench-option", "mode=hard", "--name", "agrees")
        self.assertEqual(code, 0, out + err)

    def test_two_runs_under_two_values_of_an_option_both_start_and_record_the_same_set(self):
        entry = self.make("dev", "good\nwrong\n")
        rev = sets.revision(entry)
        records = []
        for value in ("plain", "hard"):
            with self.subTest(value=value):
                code, out, err = self.run_cli("--set", "dev", "--bench-option", f"mode={value}", "--name", value)
                self.assertEqual(code, 0, out + err)
                self.assertEqual(self.trials(os.path.join(self.jobs, value)), ["good__u0000", "wrong__u0001"])
                cfg = read(os.path.join(self.jobs, value, "config.json"))
                self.assertEqual(cfg["standard"]["benchmark_options"], {"mode": value})
                records.append(cfg["standard"]["task_set"])
        self.assertEqual(records[0], records[1])
        self.assertEqual({k: records[0][k] for k in ("id", "revision", "hash")},
                         {"id": entry["id"], "revision": 1, "hash": rev["hash"]})
        self.assertEqual(rev["hash"], sets.content_hash("fakebench", "all", ["good", "wrong"]))   # no option in it

    def listing_by_mode(self):
        """The fake benchmark whose driver leaves slow and model out of its listing under mode=hard."""
        here = os.path.join(self.tmp, "fakebench")
        shutil.copytree(self.b["_here"], here)
        path = os.path.join(here, "driver.py")
        with open(path) as f:
            text = f.read()
        text = text.replace('            units.remove("slow")\n',
                            '            units.remove("slow")\n'
                            '        if os.environ.get("NULLIUS_OPTION_MODE") == "hard":\n'
                            '            units = [u for u in units if u not in ("slow", "model")]\n')
        with open(path, "w") as f:
            f.write(text)
        self.b["_here"] = here

    def test_ids_the_driver_does_not_list_under_the_runs_options_refuse_it_before_any_unit(self):
        self.listing_by_mode()
        self.make("dev", "good\nslow\nmodel\nwrong\n")
        code, out, err = self.run_cli("--set", "dev", "--bench-option", "mode=hard", "--name", "hard")
        self.assertEqual(code, 2, out + err)
        self.assertIn("2 of the 4 ids of the task set dev are not listed by Fake bench's dataset all under the run's "
                      "options (mode=hard): slow, model", out)
        job_dir = os.path.join(self.jobs, "hard")
        self.assertEqual(self.trials(job_dir), [])
        self.assertFalse(os.path.exists(os.path.join(job_dir, "config.json")))
        code, out, err = self.run_cli("--set", "dev", "--bench-option", "mode=plain", "--name", "plain")
        self.assertEqual(code, 0, out + err)

    def test_the_message_names_the_first_five_ids_not_listed(self):
        self.make("dev", "good\n" + "".join(f"gone{i}\n" for i in range(7)))
        record = sets.record(sets.load("fakebench", "dev"), sets.revision(sets.load("fakebench", "dev")))
        r = run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                    say=lambda s: None, task_set=record)
        with self.assertRaises(run.Refused) as e:
            r.start()
        self.assertIn("7 of the 8 ids of the task set dev are not listed by Fake bench's dataset all under the run's "
                      "options (mode=plain): gone0, gone1, gone2, gone3, gone4, ...", str(e.exception))
        self.assertEqual(self.trials(r.job_dir), [])
        self.assertFalse(os.path.exists(os.path.join(r.job_dir, "config.json")))

    def test_the_units_run_are_the_sets_and_limit_and_shard_apply_after(self):
        self.make("dev", "model\nwrong\nslow\n")
        code, out, err = self.run_cli("--set", "dev", "--name", "whole")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.trials(out.strip().splitlines()[-1]), ["model__u0003", "slow__u0002", "wrong__u0001"])
        # the set, then the shard, then the limit: shard 1 of 2 holds wrong and model of the three, and the limit keeps wrong
        self.assertEqual([shard.of(u, 2) for u in DATASET], [1, 1, 2, 1])
        code, out, err = self.run_cli("--set", "dev", "--shard", "1/2", "--limit", "1", "--name", "part-shard-1of2")
        self.assertEqual(code, 0, out + err)
        part = out.strip().splitlines()[-1]
        self.assertEqual(self.trials(part), ["wrong__u0001"])
        rec = read(os.path.join(part, "config.json"))["standard"]["shard"]
        self.assertEqual((rec["units_total"], rec["planned"], rec["limit"]), (3, ["wrong"], 1))
        with open(os.path.join(part, feed.FEED)) as f:
            self.assertEqual(next(e for e in map(json.loads, f) if e["event"] == "job.start")["tasks"], ["wrong"])

    def test_the_config_records_the_set_and_the_runs_options(self):
        self.b["datasets"] = [*self.b["datasets"], {"id": "responding", "units": 3}]
        self.make("Dev set", "good\nmodel\n", "--dataset", "responding")
        code, out, err = self.run_cli("--set", "dev-set", "--bench-option", "mode=hard", "--name", "first")
        self.assertEqual(code, 0, out + err)
        cfg = read(os.path.join(self.jobs, "first", "config.json"))
        entry = sets.load("fakebench", "dev-set")
        self.assertEqual(cfg["standard"]["task_set"], sets.record(entry))
        self.assertEqual(cfg["standard"]["task_set"],
                         {"id": entry["id"], "name": "dev-set", "label": "Dev set", "revision": 1, "hash": entry["revisions"][0]["hash"],
                          "units": ["good", "model"]})
        self.assertEqual(cfg["standard"]["benchmark_options"], {"mode": "hard"})
        self.assertNotIn("units_file", cfg["standard"])
        self.assertEqual(cfg["datasets"], [{"name": "fakebench/responding", "n_tasks": 2}])

    def versions(self):
        """The fake benchmark at two versions, each its own copy of the entry: at "2", its driver no longer lists slow."""
        here = os.path.join(self.tmp, "fakebench-2")
        shutil.copytree(self.b["_here"], here)
        path = os.path.join(here, "driver.py")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace('            units.remove("slow")\n',
                                 '            units.remove("slow")\n'
                                 '        units = [u for u in units if u != "slow"]\n'))
        one = dict(self.b, _version="1", _versions=["1", "2"], _default="2", _ref="fakebench@1", _labels={"1": "v1 (one)", "2": "v2"})
        return one, dict(one, _version="2", _ref="fakebench@2", _here=here)

    def test_a_set_made_at_a_version_records_it_and_a_run_at_another_that_lacks_a_task_is_refused(self):
        one, two = self.versions()
        entry = sets.create(one, "From one", dataset="all", units=["good", "slow"])
        self.assertEqual((entry["version"], sets.record(entry)["version"]), ("1", "1"))
        self.assertEqual(sets.rows("fakebench")[0]["version"], "1")
        r = run.Run(two, definitions.control(two, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                    say=lambda s: None, task_set=sets.record(entry))
        with self.assertRaises(run.Refused) as e:
            r.start()
        self.assertIn("the task set From one was made at Fake bench v1 (one), and slow is not a task of all at v2, the "
                      "version this run is at: run it at v1 (one), or make a set at v2", str(e.exception))
        self.assertEqual(self.trials(r.job_dir), [])
        # at the version it was made at, or at another that holds all its tasks, it runs
        for b, label in ((one, ""), (two, "Only good")):
            if label:
                entry = sets.create(one, label, dataset="all", units=["good"])
            r = run.Run(b, definitions.control(b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                        say=lambda s: None, task_set=sets.record(entry), name=f"at-{b['_version']}-{entry['name']}")
            r.start()
            self.assertEqual(len(self.trials(r.job_dir)), len(sets.latest(entry)["units"]))

    def test_a_run_without_a_set_records_none(self):
        code, out, err = self.run_cli("--name", "plain")
        self.assertEqual(code, 0, out + err)
        cfg = read(os.path.join(self.jobs, "plain", "config.json"))
        self.assertEqual(sorted(cfg["standard"]), ["backend", "benchmark", "benchmark_options", "control", "kind", "needs",
                                                   "permissions_granted", "plugged", "provider", "retries", "unsandboxed"])


class Listing(Scratch):
    def test_list_and_show_say_each_set_with_its_name_and_label(self):
        self.make("dev", "good\nmodel\n", "--description", "two of them")
        self.make("All four", "\n".join(DATASET))
        code, out, err = self.cli("set", "list", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual([(r["name"], r["label"], r["units"]) for r in rows], [("all-four", "All four", 4), ("dev", "dev", 2)])
        self.assertTrue(all("revision" not in r for r in rows))
        code, out, err = self.cli("set", "list", "fakebench")
        self.assertEqual(code, 0, err)
        self.assertIn("two of them", out)
        self.assertIn("All four", out)
        code, out, err = self.cli("set", "show", "fakebench", "dev")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("dev: 2 units of fakebench all\n"), out)
        self.assertNotIn("revision", out)
        self.assertEqual(out.strip().splitlines()[-2:], ["  good", "  model"])
        code, out, err = self.cli("set", "show", "fakebench", "all-four", "--json")
        self.assertEqual((code, json.loads(out)["label"], len(json.loads(out)["units"])), (0, "All four", 4))
        code, out, err = self.cli("set", "list", "otherbench", "--json")
        self.assertEqual((code, json.loads(out)), (0, []))


if __name__ == "__main__":
    unittest.main()
