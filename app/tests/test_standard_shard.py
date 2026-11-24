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
from unittest.mock import patch

from nullius import workspace
from nullius.runs import launches, parse
from nullius.standard.proofs import prove
from nullius.standard.runner import job, merge, shard

BENCH = {"key": "fakebench", "_ref": "fakebench", "family": "agent", "needs": ["agent.workspace/v1"],
         "units": {"noun": "task", "nouns": "tasks"}, "score": {"kind": "pass", "says": "the tests pass"}}
AGENT = {"key": "fake", "_kind": "agent", "_ref": "fake"}


def read(path):
    with open(path) as f:
        return json.load(f)


class Partition(unittest.TestCase):
    UNITS = [f"set/{i}" for i in range(500)]

    def test_by_position_shard_i_of_n_holds_the_units_at_places_i_minus_1_mod_n(self):
        units = ["a", "b", "c", "d", "e"]
        self.assertEqual([shard.select(units, i, 5, by="position") for i in (1, 5)], [[(0, "a")], [(4, "e")]])
        self.assertEqual(shard.select(units, 2, 2, by="position"), [(1, "b"), (3, "d")])
        self.assertEqual(shard.record(1, 5, 5, ["a"], by="position")["partition"], shard.BY["position"])

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


def write_shard(root, name, units, index, of, *, total=10, launch="L1", model="gpt-5-mini", bench=BENCH, other=AGENT,
                planned=None, t0=1_760_000_000.0, failed=(), budget=None, backend="local", bench_options=None,
                reward=lambda place: float(place % 2)):
    """A shard job as a run writes it: its config, a trial per unit (tagged by
    the unit's place in the whole dataset), its result."""
    d = os.path.join(root, name)
    os.makedirs(d)
    planned = list(units) if planned is None else planned
    job.write_config(d, job=name, bench=bench, other=other, model=model, provider="openai", dataset="all", units=planned,
                     concurrency=2, granted=True, launch=launch, shard=shard.record(index, of, total, planned), backend=backend,
                     bench_options=bench_options)
    results = []
    for k, u in enumerate(units):
        place = int(u.split("-")[1])
        trial = os.path.join(d, f"{u}__u{place:04d}")
        os.makedirs(os.path.join(trial, "workspace"))
        with open(os.path.join(trial, "workspace", "solution.py"), "w") as f:
            f.write("print(1)\n")
        exc = ("AgentTimeoutError", "slow") if u in failed else None
        results.append(job.write_trial(trial, job=name, bench=bench, other=other, model=model, unit=u, t0=t0 + k, t1=t0 + k + 5,
                                       produce=(t0 + k, t0 + k + 4), score=None if exc else {"reward": reward(place)},
                                       exception=exc, calls=[], trajectory=None))
    job.write_result(d, name, t0, t0 + 10 * (min(index, 9) + 1), results, len(planned), budget=budget)
    return d


class JobFiles(unittest.TestCase):
    """A job's config.json and result.json are read while a run goes on: a write that fails leaves the last whole one."""

    def test_a_write_that_fails_leaves_the_file_as_it_was(self):
        tmp = tempfile.mkdtemp(prefix="nullius-jobfiles-"); self.addCleanup(shutil.rmtree, tmp, True)
        d = write_shard(tmp, "j", ["u-1"], 1, 1)
        files = [os.path.join(d, n) for n in ("config.json", "result.json")]
        before = [open(p).read() for p in files]
        with self.assertRaises(TypeError):
            job.write_result(d, "j", 0.0, 1.0, [], 1, budget={"cap": object()})
        with self.assertRaises(TypeError):
            job.write_config(d, job="j", bench=BENCH, other=AGENT, model="m", provider="openai", dataset="all", units=["u-1"],
                             concurrency=1, granted=True, options={"bad": object()})
        self.assertEqual([open(p).read() for p in files], before)
        self.assertEqual(sorted(n for n in os.listdir(d) if not os.path.isdir(os.path.join(d, n))), ["config.json", "result.json"])


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
        # a merge is a job of its own: a new identity, none of its shards'
        self.assertEqual((j.uid_source, len({parse.parse_job(d).uid for d in dirs} | {j.uid})), ("declared", 4))
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

    def test_the_merged_jobs_config_is_whole_before_its_result_is_written(self):
        seen = []
        real = merge.ondisk.write_whole

        def write_whole(path, data, mode=None):
            if os.path.basename(path) == "result.json" and os.path.dirname(path) == out:
                seen.append(read(os.path.join(out, "config.json"))["standard"].get("uuid"))
            return real(path, data, mode)
        out = os.path.join(self.tmp, "out", "run")
        with patch.object(merge.ondisk, "write_whole", write_whole):
            merge.merge(self.shards(), out)
        self.assertEqual(seen, [read(os.path.join(out, "config.json"))["standard"]["uuid"]])

    def retried(self, dirs, retries=1):
        """The launch allowed each attempt one retry, and the first shard's first attempt took it: its try
        before kept under retried/, as a run leaves it. The attempt's name."""
        for d in dirs:
            cfg = read(os.path.join(d, "config.json"))
            cfg["standard"]["retries"] = retries
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump(cfg, f)
        t = sorted(n for n in os.listdir(dirs[0]) if "__u" in n)[0]
        res = read(os.path.join(dirs[0], t, "result.json"))
        first = dict(res, exception_info={"exception_type": "RateLimitError", "exception_message": "HTTP 429"})
        os.makedirs(os.path.join(dirs[0], "retried", t + ".try-1"))
        with open(os.path.join(dirs[0], "retried", t + ".try-1", "result.json"), "w") as f:
            json.dump(first, f)
        res["standard"]["retries"] = 1
        with open(os.path.join(dirs[0], t, "result.json"), "w") as f:
            json.dump(res, f)
        return t

    def test_the_merged_job_keeps_each_shards_earlier_tries(self):
        dirs = self.shards()
        t = self.retried(dirs)
        got = merge.merge(dirs, os.path.join(self.tmp, "out", "run"))
        self.assertEqual(read(os.path.join(got["out"], "retried", t + ".try-1", "result.json"))["standard"]["job"], "run")
        j = parse.parse_job(got["out"])
        self.assertEqual((len(j.trials), j.retries, j.n_retries), (10, 1, 1))     # a kept try is no trial; the retry is counted
        attempt = next(x for x in j.trials if x.trial_name == t)
        self.assertEqual([(n, x.outcome) for n, x in parse.tries(attempt)], [(1, "rate_limited"), (2, attempt.outcome)])

    def test_shards_that_allowed_different_retries_are_not_one_launch(self):
        dirs = self.shards()
        self.retried(dirs[:1], retries=2)
        with self.assertRaises(merge.Refused) as e:
            merge.merge(dirs, os.path.join(self.tmp, "never"))
        self.assertIn("retries per attempt", str(e.exception))

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
        back = parse.parse_job(got["out"]).budget
        self.assertEqual({k: back[k] for k in want}, want)
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
        got = parse._budget({"max_usd": 2, "stopped": "yes", "cut_off": "t-1"})
        self.assertEqual((got["max_usd"], got["spent_usd"], got["stopped"], got["cut_off"]), (2, None, False, []))

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

    def test_a_shard_recorded_with_docker_is_of_one_launch_with_a_container_shard(self):
        a = write_shard(self.tmp, "a", self.split[1], 1, 3, backend="docker")      # the container backend's earlier name
        b = write_shard(self.tmp, "b", self.split[2], 2, 3, backend="container")
        got = merge.merge([a, b], os.path.join(self.tmp, "out", "run"))
        self.assertEqual(read(os.path.join(got["out"], "config.json"))["standard"]["backend"], "container")
        c = write_shard(self.tmp, "c", self.split[3], 3, 3)
        with self.assertRaisesRegex(merge.Refused, "backend"):
            merge.merge([a, c], os.path.join(self.tmp, "never"))

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

    def test_a_launch_of_one_unit_per_shard_merges_and_says_only_the_units_it_lacks(self):
        n = 2 ** 64
        units = [f"t-{i}" for i in range(10)]
        dirs = [write_shard(self.tmp, f"one-{u}", [u], shard.of(u, n), n) for u in units[:-1]]
        got = merge.merge(dirs, os.path.join(self.tmp, "out"))
        self.assertEqual((got["trials"], got["notes"]), (9, ["1 of the dataset's 10 units are not in the merge"]))

    def test_shards_run_with_different_benchmark_options_keep_each_its_own(self):
        same = {"websites": "none", "gitlab": "none"}
        dirs = [write_shard(self.tmp, "s1", self.split[1], 1, 3, bench_options=same),
                write_shard(self.tmp, "s2", self.split[2], 2, 3, bench_options=dict(same, gitlab="gitlab.example")),
                write_shard(self.tmp, "s3", self.split[3], 3, 3, bench_options=same)]
        std = read(os.path.join(merge.merge(dirs, os.path.join(self.tmp, "out"))["out"], "config.json"))["standard"]
        self.assertNotIn("benchmark_options", std)
        self.assertEqual([m["benchmark_options"]["gitlab"] for m in std["merged_from"]], ["none", "gitlab.example", "none"])
        alike = [write_shard(self.tmp, f"a{i}", self.split[i], i, 3, bench_options=same) for i in (1, 2, 3)]
        std = read(os.path.join(merge.merge(alike, os.path.join(self.tmp, "alike"))["out"], "config.json"))["standard"]
        self.assertEqual(std["benchmark_options"], same)
        self.assertNotIn("benchmark_options", std["merged_from"][0])

    def test_a_merge_of_merges_keeps_the_options_of_its_inner_shards(self):
        same = {"websites": "none", "gitlab": "none"}
        inner = merge.merge([write_shard(self.tmp, "s1", self.split[1], 1, 3),
                             write_shard(self.tmp, "s2", self.split[2], 2, 3, bench_options=dict(same, gitlab="gitlab.example"))],
                            os.path.join(self.tmp, "inner"))["out"]
        outer = merge.merge([inner, write_shard(self.tmp, "s3", self.split[3], 3, 3, bench_options=same)],
                            os.path.join(self.tmp, "outer"), force=True)["out"]
        self.assertEqual(prove._options_of(read(os.path.join(outer, "config.json"))),
                         [dict(same, gitlab="gitlab.example"), same, {}])

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


class Fold(unittest.TestCase):
    """The runs page folds one participant's shards into one row: what it shows is the job ``cli merge`` makes."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-fold-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        units = [f"t-{i}" for i in range(10)]
        self.split = {i: [u for _, u in shard.select(units, i, 3)] for i in (1, 2, 3)}

    def test_a_fold_reads_as_the_merged_job(self):
        planned = self.split[2]
        dirs = [write_shard(self.tmp, "s1", self.split[1], 1, 3, failed=(self.split[1][0],), reward=lambda place: 1.0),
                write_shard(self.tmp, "s2", planned[:-1], 2, 3, planned=planned)]       # shard 3 never ran; nor did a unit of shard 2
        folded, check = launches.fold([parse.parse_job(d) for d in dirs])
        made = merge.merge(dirs, os.path.join(self.tmp, "out", "run"))
        merged = parse.parse_job(made["out"])
        for name in ("n_resolved", "adjusted_solve_rate", "raw_solve_rate", "counts", "n_planned", "n_errored_trials", "started_at",
                     "finished_at", "duration_min", "total_input_tokens", "total_output_tokens", "total_cost_usd", "mean_score", "budget"):
            self.assertEqual(getattr(folded, name), getattr(merged, name), name)
        self.assertEqual(sorted(t.trial_name for t in folded.trials), sorted(t.trial_name for t in merged.trials))
        # the rate is over every trial with a verdict across the shards, never the mean of the shards' rates
        rates = [parse.parse_job(d).adjusted_solve_rate for d in dirs]
        self.assertNotAlmostEqual(folded.adjusted_solve_rate, sum(rates) / len(rates))
        # the units no shard ran are merge's own: counted apart, never as misses
        self.assertEqual(check["notes"], made["notes"])
        self.assertEqual(check["units_total"] - len(check["by_unit"]), 10 - len(folded.trials))
        self.assertEqual(sum(folded.counts.values()), len(check["by_unit"]))

    def test_a_fold_is_read_again_only_when_the_workspace_parses_a_shard_again(self):
        dirs = [write_shard(self.tmp, f"c{i}", self.split[i], i, 3) for i in (1, 2)]
        ws = workspace.Workspace("w", "W", [self.tmp], "2026-01-01", "2026-01-01", os.path.join(self.tmp, "ws"), [self.tmp])
        with patch.object(merge, "check", wraps=merge.check) as check:
            first = launches.fold([workspace.load_job(ws, d)[0] for d in dirs])
            self.assertEqual(launches.fold([workspace.load_job(ws, d)[0] for d in dirs]), first)
            self.assertEqual(check.call_count, 1)
            with open(os.path.join(dirs[1], "result.json"), "a") as f:     # an ended shard is read again by its own record
                f.write("\n")
            launches.fold([workspace.load_job(ws, d)[0] for d in dirs])
            self.assertEqual(check.call_count, 2)
        # a shard more replaces the fold without it: one entry per dir, however long the app runs
        dirs.append(write_shard(self.tmp, "c3", self.split[3], 3, 3))
        launches.fold([workspace.load_job(ws, d)[0] for d in dirs])
        self.assertEqual([k for k in launches._FOLDED if dirs[0] in k], [tuple(dirs)])

    def test_shards_merge_refuses_are_not_folded(self):
        dirs = [write_shard(self.tmp, "a", self.split[1], 1, 3, launch="L1"), write_shard(self.tmp, "b", self.split[2], 2, 3, launch="L2")]
        self.assertIsNone(launches.fold([parse.parse_job(d) for d in dirs]))


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
        records = {"e1": ("r-shard-1of2", ["r-shard-1of2"], None, set()), "e2": ("r-shard-2of2", ["r-shard-2of2", "r-shard-2of2-pending"], None, set())}
        out = launches.group(jobs, records)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].job_ids, out[0].pending), (["r-shard-1of2", "r-shard-2of2"], ["r-shard-2of2-pending"]))
        self.assertEqual(out[0].prefix, "r-shard-*")
        self.assertEqual(out[0].title, "r")                                  # named by its launch id, not the jobs' prefix

    def test_shards_whose_jobs_are_not_written_join_their_launch_by_the_plans_launch_id(self):
        records = {f"e{i}": (f"r-shard-{i}of3", [f"r-shard-{i}of3"], None, {"r"}) for i in (1, 2, 3)}
        for written in ([], [a_job("r-shard-2of3", 0, "r")]):
            out = launches.group(written, records)
            self.assertEqual(len(out), 1, written)
            self.assertEqual(sorted(out[0].job_ids + out[0].pending), ["r-shard-1of3", "r-shard-2of3", "r-shard-3of3"])
            self.assertEqual(out[0].title, "r")

    def test_a_launch_of_no_shards_or_of_several_launches_is_named_by_its_prefix(self):
        out = launches.group([a_job("x-1", 0), a_job("x-2", 0.2)])
        self.assertEqual(out[0].title, out[0].prefix)
        mixed = launches.group([a_job("m-a", 0, "L1"), a_job("m-b", 0.1, "L2")], {"e": ("m", ["m-a", "m-b"], None, set())})
        self.assertEqual((len(mixed), mixed[0].title), (1, mixed[0].prefix))


if __name__ == "__main__":
    unittest.main()
