"""One run over many machines: the partition is the unit id's own (every unit
in exactly one shard, stable as a dataset grows), a run keeps each unit's
place in the whole dataset, and the shard jobs merge into one job the
pages read as one run - refused when they are not one launch or a unit ran
twice, with the units no shard ran said. The runs page groups a launch's
shards before they are merged."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from nullius.runs import launches, parse
from nullius.standard import job, merge, shard

BENCH = {"key": "fakebench", "_ref": "fakebench", "family": "agent", "needs": ["agent.workspace/v1"],
         "units": {"noun": "task", "nouns": "tasks"}, "score": {"kind": "pass", "says": "the tests pass"}}
AGENT = {"key": "fake", "_kind": "agent", "_ref": "fake"}


def read(path):
    with open(path) as f:
        return json.load(f)


class Partition(unittest.TestCase):
    UNITS = [f"set/{i}" for i in range(500)]

    def test_every_unit_is_in_exactly_one_shard(self):
        for n in (1, 2, 3, 7):
            got = [u for i in range(1, n + 1) for _, u in shard.select(self.UNITS, i, n)]
            self.assertEqual(sorted(got), sorted(self.UNITS), n)
            self.assertEqual(len(got), len(set(got)))

    def test_the_shard_is_the_unit_id_s_and_the_place_is_the_whole_dataset_s(self):
        # the same id, the same shard, whatever else the list holds or in what order
        self.assertEqual([shard.of(u, 4) for u in self.UNITS], [shard.of(u, 4) for u in self.UNITS])
        grown = self.UNITS + [f"new/{i}" for i in range(100)]
        before = {u for _, u in shard.select(self.UNITS, 2, 4)}
        after = {u for _, u in shard.select(grown, 2, 4)}
        self.assertEqual(before, after & set(self.UNITS))                           # growing moves no unit it had
        places = dict((u, i) for i, u in shard.select(grown, 3, 4))
        self.assertTrue(all(grown[i] == u for u, i in places.items()))                # each keeps its place in the whole
        sizes = [len(shard.select(self.UNITS, i, 4)) for i in range(1, 5)]
        self.assertTrue(all(90 < s < 160 for s in sizes), sizes)                      # and the shards are about even

    def test_a_shard_is_i_of_n(self):
        self.assertEqual(shard.parse("2/5"), (2, 5))
        for bad in ("0/2", "3/2", "2", "a/b", "1/0"):
            with self.assertRaises(ValueError):
                shard.parse(bad)

    def test_shards_started_apart_agree_on_their_launch(self):
        self.assertEqual(shard.launch_id("fakebench", "fake", "gpt-5-mini", day="2026-10-12"),
                         shard.launch_id("fakebench", "fake", "gpt-5-mini", day="2026-10-12"))
        self.assertEqual(shard.launch_id("b", "o", "openai/gpt 5", day="2026-10-12"), "2026-10-12__b__o__openai_gpt_5")


def write_shard(root, name, units, index, of, *, total=10, launch="L1", model="gpt-5-mini", bench=BENCH, other=AGENT,
                planned=None, t0=1_760_000_000.0, failed=(), budget=None):
    """A shard job as a run writes it: its config, a trial per unit (tagged by
    the unit's place in the whole dataset), its result."""
    d = os.path.join(root, name)
    os.makedirs(d)
    planned = list(units) if planned is None else planned
    job.write_config(d, job=name, bench=bench, other=other, model=model, provider="openai", dataset="all", units=planned,
                     concurrency=2, granted=True, launch=launch, shard=shard.record(index, of, total, planned))
    results = []
    for k, u in enumerate(units):
        place = int(u.split("-")[1])
        trial = os.path.join(d, f"{u}__u{place:04d}")
        os.makedirs(os.path.join(trial, "workspace"))
        with open(os.path.join(trial, "workspace", "solution.py"), "w") as f:
            f.write("print(1)\n")
        exc = ("AgentTimeoutError", "slow") if u in failed else None
        results.append(job.write_trial(trial, job=name, bench=bench, other=other, model=model, unit=u, t0=t0 + k, t1=t0 + k + 5,
                                       produce=(t0 + k, t0 + k + 4), score=None if exc else {"reward": 1.0 if place % 2 else 0.0},
                                       exception=exc, calls=[], trajectory=None))
    job.write_result(d, name, t0, t0 + 10 * (index + 1), results, len(planned), budget=budget)
    return d


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-merge-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        units = [f"t-{i}" for i in range(10)]
        self.split = {i: [u for _, u in shard.select(units, i, 3)] for i in (1, 2, 3)}

    def shards(self, which=(1, 2, 3), **kw):
        return [write_shard(self.tmp, f"run-shard-{i}of3", self.split[i], i, 3, **kw) for i in which]

    def test_the_merged_job_reads_as_one_run_of_every_trial(self):
        dirs = self.shards(failed=("t-4",))
        got = merge.merge(dirs, os.path.join(self.tmp, "out", "run"))
        self.assertEqual((got["trials"], got["missing"]), (10, []))
        self.assertEqual(got["notes"], [])
        j = parse.parse_job(got["out"])
        self.assertEqual((j.job_id, j.n_tasks, j.n_total_trials, len(j.trials)), ("run", 10, 10, 10))
        self.assertEqual(sorted(t.trial_name for t in j.trials), sorted(f"t-{i}__u{i:04d}" for i in range(10)))
        self.assertEqual(sum(1 for t in j.trials if t.reward == 1.0), 5)                  # the odd ones pass; t-4 ended in an error
        self.assertEqual(j.n_errored_trials, 1)
        self.assertEqual(j.started_at, datetime.fromtimestamp(1_760_000_000.0, timezone.utc))
        self.assertEqual(j.finished_at, datetime.fromtimestamp(1_760_000_040.0, timezone.utc))   # the latest end of the three
        cfg = read((os.path.join(got["out"], "config.json")))
        self.assertNotIn("shard", cfg["standard"])
        self.assertEqual([m["shard"] for m in cfg["standard"]["merged_from"]], ["1/3", "2/3", "3/3"])
        self.assertEqual((cfg["job_name"], cfg["standard"]["launch"], cfg["standard"]["units_total"]), ("run", "L1", 10))
        res = read((os.path.join(got["out"], "t-1__u0001", "result.json")))
        self.assertEqual((res["standard"]["job"], res["standard"]["shard_job"]), ("run", f"run-shard-{shard.of('t-1', 3)}of3"))
        # the shard's own record is untouched, and the workspace is the same file, not a second copy
        src = os.path.join(self.tmp, f"run-shard-{shard.of('t-1', 3)}of3", "t-1__u0001")
        self.assertEqual(read((os.path.join(src, "result.json")))["standard"]["job"], f"run-shard-{shard.of('t-1', 3)}of3")
        self.assertTrue(os.path.samefile(os.path.join(src, "workspace", "solution.py"),
                                         os.path.join(got["out"], "t-1__u0001", "workspace", "solution.py")))

    def test_the_merged_job_carries_the_shards_spending_caps_as_one(self):
        """Each shard ran under its share of the run's cap: the merged job says the whole cap, what all of them
        spent, and every unit a cap cut off or left out."""
        caps = {1: {"max_usd": 0.5, "spent_usd": 0.2, "stopped": False, "cut_off": [], "not_run": []},
                2: {"max_usd": 0.5, "spent_usd": 0.41, "unsettled_usd": 0.03, "stopped": True, "cut_off": ["t-9"], "not_run": ["t-11", "t-12"]},
                3: {"max_usd": 0.5, "spent_usd": 0.1, "stopped": False, "cut_off": [], "not_run": []}}
        dirs = [write_shard(self.tmp, f"run-shard-{i}of3", self.split[i], i, 3, budget=caps[i]) for i in (1, 2, 3)]
        got = merge.merge(dirs, os.path.join(self.tmp, "capped", "run"))
        want = {"max_usd": 1.5, "spent_usd": 0.71, "unsettled_usd": 0.03, "stopped": True, "cut_off": ["t-9"], "not_run": ["t-11", "t-12"],
                "shards": 3, "shards_stopped": 1}                                # one shard stopped at its share: not the launch at 1.5
        self.assertEqual(read(os.path.join(got["out"], "result.json"))["budget"], want)
        self.assertEqual(parse.parse_job(got["out"]).budget, dict(want, unit_max_usd=None, over_unit=[]))
        self.assertGreaterEqual(int(parse.SCHEMA), 15)                                  # a job cached before the field is read again
        # a cap over part of a launch is not the launch's: nothing is claimed
        shutil.rmtree(dirs[2])
        dirs[2] = write_shard(self.tmp, "run-shard-3of3", self.split[3], 3, 3)
        part = merge.merge(dirs, os.path.join(self.tmp, "partly", "run"))
        self.assertNotIn("budget", read(os.path.join(part["out"], "result.json")))
        self.assertEqual(parse.parse_job(part["out"]).budget, {})

    def test_a_recorded_cap_is_read_leniently_and_an_amount_missing_is_not_zero(self):
        self.assertEqual(parse._budget(None), {})
        self.assertEqual(parse._budget({"max_usd": "2", "spent_usd": 1}), {})                # no cap named: no cap
        self.assertEqual(parse._budget({"max_usd": 2, "stopped": "yes", "cut_off": "t-1"}),
                         {"max_usd": 2, "spent_usd": None, "unsettled_usd": None, "stopped": False, "cut_off": [], "not_run": [],
                          "unit_max_usd": None, "over_unit": [], "shards": 0, "shards_stopped": 0})
        self.assertFalse(parse._budget({"unit_max_usd": 0.5, "stopped": True})["stopped"])   # only the run's cap stops a run
        self.assertEqual(parse._budget({"unit_max_usd": 0.5, "over_unit": ["t-2"]})["over_unit"], ["t-2"])   # a cap per unit alone is a cap

    def test_a_launch_whose_shards_did_not_all_have_the_runs_cap_claims_no_stop(self):
        some = lambda i: {"max_usd": 0.5 if i < 3 else None, "spent_usd": 0.1, "stopped": i == 1, "cut_off": [], "not_run": [], "unit_max_usd": 0.05, "over_unit": []}
        dirs = [write_shard(self.tmp, f"run-shard-{i}of3", self.split[i], i, 3, budget=some(i)) for i in (1, 2, 3)]
        got = read(os.path.join(merge.merge(dirs, os.path.join(self.tmp, "mixed", "run"))["out"], "result.json"))["budget"]
        self.assertEqual((got["max_usd"], got["stopped"], got["unit_max_usd"], "shards" in got), (None, False, 0.05, False))

    def test_the_merged_job_keeps_a_cap_per_unit_and_the_units_that_reached_it(self):
        each = lambda over: {"max_usd": None, "spent_usd": 0.1, "stopped": False, "cut_off": [], "not_run": [], "unit_max_usd": 0.05, "over_unit": over}
        dirs = [write_shard(self.tmp, f"run-shard-{i}of3", self.split[i], i, 3, budget=each(over)) for i, over in ((1, ["t-0"]), (2, []), (3, ["t-7"]))]
        got = merge.merge(dirs, os.path.join(self.tmp, "units", "run"))
        self.assertEqual(read(os.path.join(got["out"], "result.json"))["budget"],
                         {"max_usd": None, "spent_usd": 0.3, "stopped": False, "cut_off": [], "not_run": [], "unit_max_usd": 0.05, "over_unit": ["t-0", "t-7"]})

    def test_shards_of_different_launches_models_or_versions_are_refused_unless_forced(self):
        a = write_shard(self.tmp, "a", self.split[1], 1, 3)
        for kw, word in (({"launch": "L2"}, "launch"), ({"model": "gpt-4o"}, "model"),
                         ({"bench": dict(BENCH, _ref="fakebench@2", _version="2")}, "benchmark version"),
                         ({"other": dict(AGENT, key="other", _ref="other")}, "agent or capability")):
            b = write_shard(self.tmp, "b-" + word.replace(" ", "-"), self.split[2], 2, 3, **kw)
            with self.assertRaises(merge.Refused) as e:
                merge.merge([a, b], os.path.join(self.tmp, "never"))
            self.assertIn(word, str(e.exception))
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "never")))
        got = merge.merge([a, b], os.path.join(self.tmp, "forced"), force=True)
        self.assertIn("merged_despite", read((os.path.join(got["out"], "config.json")))["standard"])

    def test_a_unit_run_twice_is_refused_and_named(self):
        a = write_shard(self.tmp, "a", self.split[1], 1, 3)
        b = write_shard(self.tmp, "b", self.split[1][:1] + self.split[2], 2, 3)
        with self.assertRaises(merge.Refused) as e:
            merge.merge([a, b], os.path.join(self.tmp, "out"), force=True)
        self.assertIn(self.split[1][0], str(e.exception))

    def test_units_no_shard_ran_are_said(self):
        planned = self.split[2]
        dirs = [write_shard(self.tmp, "s1", self.split[1], 1, 3),
                write_shard(self.tmp, "s2", planned[:-1], 2, 3, planned=planned)]           # one planned unit never ran
        got = merge.merge(dirs, os.path.join(self.tmp, "out"))
        self.assertEqual(got["missing"], [planned[-1]])
        notes = " | ".join(got["notes"])
        self.assertIn("shard 3/3 not in the merge", notes)
        self.assertIn(f"s2: 1 planned unit(s) with no trial: {planned[-1]}", notes)
        self.assertIn(f"{10 - len(self.split[1]) - len(planned) + 1} of the dataset's 10 units are not in the merge", notes)
        j = parse.parse_job(got["out"])
        self.assertEqual((j.n_total_trials, len(j.trials)), (len(self.split[1]) + len(planned), len(self.split[1]) + len(planned) - 1))

    def test_a_job_that_is_no_shard_or_an_out_that_is_taken_is_refused(self):
        dirs = self.shards()
        os.makedirs(os.path.join(self.tmp, "taken", "x"))
        with self.assertRaises(merge.Refused):
            merge.merge(dirs, os.path.join(self.tmp, "taken"))
        plain = os.path.join(self.tmp, "plain"); os.makedirs(plain)
        with open(os.path.join(plain, "config.json"), "w") as f:
            json.dump({"runner": "standard", "standard": {}}, f)
        with self.assertRaises(merge.Refused) as e:
            merge.merge(dirs + [plain], os.path.join(self.tmp, "out"))
        self.assertIn("not a shard", str(e.exception))


T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


def a_job(job_id, hours, launch="", dataset="fakebench"):
    from datetime import timedelta
    return SimpleNamespace(job_id=job_id, dataset=dataset, started_at=T0 + timedelta(hours=hours), launch_id=launch)


class Launches(unittest.TestCase):
    def test_shards_of_one_launch_are_one_launch_hours_apart(self):
        jobs = [a_job("s1", 0, "L"), a_job("s2", 5, "L"), a_job("other", 5.1), a_job("s3", 9, "L", dataset="mixed")]
        out = launches.group(jobs)
        self.assertEqual(sorted(sorted(l.job_ids) for l in out), [["other"], ["s1", "s2", "s3"]])

    def test_shards_started_by_separate_run_records_join_one_launch(self):
        jobs = [a_job("r-shard-1of2", 0, "r"), a_job("r-shard-2of2", 0.1, "r")]
        records = {"e1": ("r-shard-1of2", ["r-shard-1of2"], None), "e2": ("r-shard-2of2", ["r-shard-2of2", "r-shard-2of2-pending"], None)}
        out = launches.group(jobs, records)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].job_ids, out[0].pending), (["r-shard-1of2", "r-shard-2of2"], ["r-shard-2of2-pending"]))
        self.assertEqual(out[0].prefix, "r-shard-*")
        self.assertEqual(out[0].title, "r")                                  # named by its launch id, not the jobs' prefix

    def test_a_launch_of_no_shards_or_of_several_launches_is_named_by_its_prefix(self):
        out = launches.group([a_job("x-1", 0), a_job("x-2", 0.2)])
        self.assertEqual(out[0].title, out[0].prefix)
        mixed = launches.group([a_job("m-a", 0, "L1"), a_job("m-b", 0.1, "L2")], {"e": ("m", ["m-a", "m-b"], None)})
        self.assertEqual((len(mixed), mixed[0].title), (1, mixed[0].prefix))

    def test_a_job_with_no_launch_groups_as_before(self):
        out = launches.group([a_job("x", 0), a_job("y", 0.2)])
        self.assertEqual([sorted(l.job_ids) for l in out], [["x", "y"]])


if __name__ == "__main__":
    unittest.main()
