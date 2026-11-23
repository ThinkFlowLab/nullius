"""A benchmark's controls and its proofs: the schema of ``controls``, a
control run on a kit with no agent, no model and no key, the jobs read back
as controls, ``cli prove``'s result and what it writes into ``proven``, and
the leak check, on drivers made to leak. No network."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius.runs import classify, feasibility, parse
from nullius.standard import definitions, install, match, prove, recipe, run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
CONTROLS = {"nop": {"source": "nullius", "from": "Nothing runs; the answer is empty."},
            "oracle": {"source": "official", "from": "The authors' answer field."}}


def kit(key: str, root: str) -> dict:
    """A copy of a kit under ``root``, loaded as a benchmark: its proofs are written there."""
    here = os.path.join(root, key)
    shutil.copytree(os.path.join(definitions.KITS, key), here)
    d, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", key)
    assert not bad, bad
    return d


class Schema(unittest.TestCase):
    def setUp(self):
        self.b = definitions.get("benchmark", "fakebench", FIX)

    def problems(self, controls):
        return definitions.problems(dict(self.b, controls=controls), "benchmark")

    def test_the_two_controls_with_their_sources_are_valid(self):
        self.assertEqual(self.problems(CONTROLS), [])
        self.assertEqual(self.problems({"oracle": dict(CONTROLS["oracle"], expect={"at_least": 0.9}, in_prompt="a choice")}), [])

    def test_each_wrong_field_is_named(self):
        self.assertIn("controls.gold: a control is one of ('nop', 'oracle')", self.problems({"gold": CONTROLS["nop"]}))
        self.assertTrue(any("controls.nop.source" in p for p in self.problems({"nop": {"source": "us", "from": "x"}})))
        self.assertTrue(any("controls.oracle.from" in p for p in self.problems({"oracle": {"source": "official"}})))
        self.assertTrue(any("controls.nop.expect" in p for p in self.problems({"nop": dict(CONTROLS["nop"], expect={"at_least": 1})})))
        self.assertTrue(any("controls.oracle.expect" in p for p in self.problems({"oracle": dict(CONTROLS["oracle"], expect={"at_least": 2})})))
        self.assertTrue(any("controls.nop.in_prompt" in p for p in self.problems({"nop": dict(CONTROLS["nop"], in_prompt="x")})))
        self.assertTrue(any("is not a field" in p for p in self.problems({"nop": dict(CONTROLS["nop"], answer="x")})))
        self.assertIn("controls: an object, nop and/or oracle", self.problems({}))

    def test_units_that_may_not_be_redistributed_say_so_with_a_boolean(self):
        units = dict(self.b["units"], redistribute=False)
        self.assertEqual(definitions.problems(dict(self.b, units=units), "benchmark"), [])
        self.assertIn("units.redistribute: false when the data may not be redistributed",
                      definitions.problems(dict(self.b, units=dict(units, redistribute="no")), "benchmark"))

    def test_an_agent_cannot_take_a_controls_name(self):
        a = definitions.get("agent", "fake", FIX)
        self.assertIn("key: nop is the word for a benchmark's control", definitions.problems(dict(a, key="nop"), "agent"))

    def test_a_control_is_offered_only_where_declared(self):
        self.assertIsNone(definitions.control(self.b, "oracle"))
        b = dict(self.b, controls=CONTROLS)
        nop, oracle = definitions.control(b, "nop"), definitions.control(b, "oracle")
        self.assertEqual((nop["expect"], oracle["expect"]), ({"at_most": 0.0}, {"at_least": 1.0}))
        self.assertEqual(match.fit(b, oracle), [])
        self.assertTrue(match.fit(self.b, oracle))
        self.assertEqual(match.table([b], [], [])["fakebench"], {"nop": [], "oracle": []})

    def test_every_catalogued_control_is_valid_and_honest(self):
        for b in definitions.load("benchmark")[0] + definitions.kits():
            for c in (b.get("controls") or {}).values():
                self.assertIn(c["source"], definitions.SOURCES, b["key"])

    def test_a_catalogued_proof_of_units_that_may_not_be_redistributed_names_none(self):
        held = [b for b in definitions.load("benchmark")[0] if b["units"].get("redistribute") is False]
        self.assertIn("gaia", [b["key"] for b in held])
        for b in held:
            for ds, checks in (b.get("proven") or {}).items():
                with self.subTest(benchmark=b["key"], dataset=ds):
                    self.assertNotIn("first", (checks.get("leak") or {}).get("in_prompt") or {})
                    self.assertFalse([p for r in (checks.get("failed") or {}).values() for p in r.get("problems") or []])


class ControlRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self, key, which, **kw):
        b = kit(key, os.path.join(self.tmp, "kits"))
        return run.run(b, definitions.control(b, which), model="", provider="openai", jobs_root=os.path.join(self.tmp, "jobs"),
                       env={}, say=lambda s: None, **kw)

    def test_the_oracle_writes_the_workspace_and_the_scorer_passes_it_with_no_key(self):
        j = parse.parse_job(self.go("agent-workspace-v1", "oracle"))
        self.assertEqual([t.outcome for t in j.trials], [classify.RESOLVED, classify.RESOLVED])
        self.assertTrue(j.is_control)
        self.assertEqual(j.control_source, "nullius")
        self.assertEqual(feasibility.summary_of(j).n_behaved, 2)
        with open(os.path.join(j.dir, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["standard"]["control"]["name"], "oracle")
        self.assertFalse(cfg["standard"]["unsandboxed"])
        with open(os.path.join(j.dir, j.trials[0].trial_name, "result.json")) as f:
            self.assertEqual(json.load(f)["standard"]["control"]["source"], "nullius")

    def test_two_shards_run_every_unit_once_tagged_by_its_place_and_merge_into_one_job(self):
        """The shard before the limit, each unit's tag its place in the whole
        dataset, the config saying which shard of which launch; merged, the
        two read as the one run they are."""
        from nullius.standard import merge, shard
        b = kit("agent-workspace-v1", os.path.join(self.tmp, "kits"))
        go = lambda which, **kw: run.run(b, definitions.control(b, which), model="", provider="openai",
                                         jobs_root=os.path.join(self.tmp, "jobs"), env={}, say=lambda s: None, **kw)
        dirs = [go("oracle", shard=(i, 2), launch="L") for i in (1, 2)]
        tags, units = [], []
        for i, d in enumerate(dirs, 1):
            self.assertTrue(d.endswith(f"__shard-{i}of2"))
            with open(os.path.join(d, "config.json")) as f:
                std = json.load(f)["standard"]
            self.assertEqual((std["launch"], std["shard"]["index"], std["shard"]["of"], std["shard"]["partition"]),
                             ("L", i, 2, shard.PARTITION))
            self.assertTrue(all(shard.of(u, 2) == i for u in std["shard"]["planned"]))
            j = parse.parse_job(d)
            units += std["shard"]["planned"]
            tags += [t.trial_name.rsplit("__", 1)[1] for t in j.trials]
        self.assertEqual(sorted(tags), ["u0000", "u0001"])                                 # every unit once, by its place in the whole
        self.assertEqual(len(units), 2)
        got = merge.merge(dirs, os.path.join(self.tmp, "merged"))
        j = parse.parse_job(got["out"])
        self.assertEqual(([t.outcome for t in j.trials], j.is_control, j.n_total_trials), ([classify.RESOLVED] * 2, True, 2))
        limited = parse.parse_job(go("nop", shard=(1, 2), limit=1))
        self.assertEqual(len(limited.trials), 1)                                             # the limit is the shard's own

    def test_the_null_control_passes_nothing(self):
        j = parse.parse_job(self.go("agent-workspace-v1", "nop"))
        self.assertEqual(j.n_resolved, 0)
        self.assertEqual(feasibility.control_of(j), "nop")
        self.assertEqual(feasibility.summary_of(j).n_unexpected, 0)

    def test_a_feature_benchmarks_oracle_goes_straight_to_the_score(self):
        j = parse.parse_job(self.go("cap-memory-v1", "oracle"))
        self.assertEqual(j.n_resolved, 2)
        self.assertFalse(any(os.path.exists(os.path.join(j.dir, n, "agent", "capability.log")) for n in os.listdir(j.dir)))

    def test_a_judged_benchmarks_control_still_needs_the_judges_key(self):
        b = kit("agent-task-v1", os.path.join(self.tmp, "kits"))
        b["score"] = dict(b["score"], judge={"model": "judge-1", "says": "x"})
        with self.assertRaises(run.Refused):
            run.Run(b, definitions.control(b, "nop"), model="", provider="openai", env={})

    def test_a_standard_job_named_oracle_without_the_record_is_no_control(self):
        j = parse.parse_job(self.go("agent-task-v1", "oracle"))
        with open(os.path.join(j.dir, "config.json")) as f:
            cfg = json.load(f)
        del cfg["standard"]["control"]
        with open(os.path.join(j.dir, "config.json"), "w") as f:
            json.dump(cfg, f)
        self.assertFalse(parse.parse_job(j.dir).is_control)          # a runner's arm called oracle stays an arm


SLOW_SCORE = '''import os, time
from nullius.standard.jsonl import serve
CHECKED = os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "checked"))
def setup(unit, dir):
    return dict({"prompt": "hi"}, **({"verify": {"run": "true"}} if CHECKED else {}))
def score(unit, dir, transcript, answer, verified=None):
    time.sleep(2)
    return {"reward": 0.0, "passed": False, "detail": {}}
serve({"units": lambda dataset: {"units": ["a"]}, "setup": setup, "score": score})
'''


class Box:
    image = ""

    def give_back(self, paths):
        pass

    def stop(self):
        pass


class ScoreLimit(unittest.TestCase):
    """A null control whose driver does its work in ``score``, waited for ``limits.score_seconds``."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        here = os.path.join(self.tmp, "slow")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(SLOW_SCORE)
        self.b = dict(definitions.get("benchmark", "fakebench", FIX), _here=here, controls=CONTROLS,
                      driver={"command": ["python3", "{here}/driver.py"]})

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def problems(self, limits):
        return definitions.problems(dict(self.b, limits=limits), "benchmark")

    def trial(self, limits):
        b = dict(self.b, limits=limits)
        job_dir = run.run(b, definitions.control(b, "nop"), model="", provider="openai",
                          jobs_root=os.path.join(self.tmp, "jobs"), env={}, say=lambda s: None)
        name = next(t for t in os.listdir(job_dir) if "__u" in t)
        with open(os.path.join(job_dir, name, "result.json")) as f:
            return json.load(f)

    def verify_phase(self):
        jobs = os.path.join(self.tmp, "jobs")
        with open(os.path.join(jobs, os.listdir(jobs)[0], "events.jsonl")) as f:
            return next(e for e in map(json.loads, f) if e["event"] == "trial.phase" and e["phase"] == "verify")

    def test_the_limit_is_a_whole_number_of_seconds_from_1(self):
        self.assertEqual(self.problems({"score_seconds": 1800}), [])
        for v in (0, -1, 1.5, "600", True, 100001):
            self.assertIn("limits.score_seconds: a whole number from 1", self.problems({"score_seconds": v}), v)

    def test_score_is_waited_for_600_seconds_or_the_benchmarks_own_limit(self):
        r = run.Run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", env={})
        self.assertEqual(r.score_seconds, 600)
        r = run.Run(dict(self.b, limits={"score_seconds": 1800}), definitions.control(self.b, "nop"), model="",
                    provider="openai", env={})
        self.assertEqual((r.score_seconds, r.phase_limit("score", r.score_seconds)), (1800, 1800))
        r = run.Run(dict(self.b, limits={"score_seconds": 1800}), definitions.control(self.b, "nop"), model="",
                    provider="openai", env={}, timeouts={"score": 60})
        self.assertEqual(r.phase_limit("score", r.score_seconds), 60)
        page = recipe._bench_page(dict(self.b, limits={"score_seconds": 1800}))["timeouts"]
        self.assertEqual((page["score"], "score" in page["catalogue"]), (1800, True))
        self.assertEqual(recipe._bench_page(self.b)["timeouts"]["score"], run.DRIVER_SECONDS)

    def test_a_score_past_the_limit_is_the_verifiers_error_and_one_within_it_scores(self):
        late = self.trial({"score_seconds": 1})
        self.assertEqual(late["exception_info"]["exception_type"], "VerifierError")
        self.assertIn("score: no reply in 1 s", late["exception_info"]["exception_message"])
        self.assertIsNone(late.get("verifier_result"))
        on_time = self.trial({"score_seconds": 10})
        self.assertIsNone(on_time.get("exception_info"))
        self.assertEqual(on_time["verifier_result"]["rewards"]["reward"], 0.0)

    def test_the_verify_phase_carries_the_score_limit_and_an_own_images_check(self):
        self.trial({"score_seconds": 10})
        self.assertEqual(self.verify_phase()["timeout_sec"], 10)
        shutil.rmtree(os.path.join(self.tmp, "jobs"))
        open(os.path.join(self.b["_here"], "checked"), "w").close()
        checked = []
        with patch.object(run.Run, "unit_environment", lambda r, setup: ("task", {"image": "own"})), \
                patch.object(run.Run, "not_run", lambda r, e, d: None), \
                patch.object(run.Run, "unit_relay", lambda r, *a, **k: None), \
                patch.object(run.Run, "own_box", lambda r, *a: Box()), \
                patch.object(run.Run, "box_changes", lambda r, box: ""), \
                patch.object(run.Run, "release_image", lambda r, image: None), \
                patch.object(run.Run, "verify", lambda r, step, box, trial: checked.append(step) or {"exit": 0}):
            got = self.trial({"score_seconds": 10})
        self.assertIsNone(got.get("exception_info"))
        self.assertEqual(checked, [{"run": "true"}])
        self.assertEqual(self.verify_phase()["timeout_sec"], 10 + run.TURN_SECONDS)


class Prove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prove(self, b, **kw):
        return prove.prove(b, jobs_root=os.path.join(self.tmp, "jobs"), env={}, say=lambda s: None, stamp="2026-01-02__03-04-05", **kw)

    def test_the_result_has_every_check_and_its_jobs_and_proven_is_written_without_them(self):
        b = kit("agent-workspace-v1", os.path.join(self.tmp, "kits"))
        got = self.prove(b)
        self.assertEqual(set(got), {"kit"})
        self.assertEqual(list(got["kit"]), ["units", "leak", "nop", "oracle"])
        self.assertTrue(all(r["ok"] for r in got["kit"].values()), got)
        self.assertEqual(got["kit"]["oracle"]["passed"], "2/2")
        self.assertEqual(got["kit"]["nop"]["passed"], "0/2")
        with open(os.path.join(b["_here"], "standard.json")) as f:
            written = json.load(f)
        proven = written["proven"]["kit"]
        self.assertEqual(list(proven), ["units", "leak", "nop", "oracle"])
        self.assertEqual(got["kit"]["oracle"]["job"], "2026-01-02__03-04-05__agent-workspace-v1__kit__prove-oracle")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "jobs", got["kit"]["nop"]["job"])))
        self.assertFalse({"job", "on", "source"} & {k for r in proven.values() for k in r})
        self.assertEqual(proven["oracle"]["expect"], {"at_least": 1.0})
        self.assertNotIn("ok", proven["oracle"])
        self.assertNotIn("limit", proven["oracle"])
        self.assertEqual(definitions.problems(written, "benchmark"), [])

    def test_a_shard_s_proof_runs_its_controls_on_its_units_and_says_which(self):
        from nullius.standard import shard
        b = kit("agent-workspace-v1", os.path.join(self.tmp, "kits"))
        got = self.prove(b, shard=(2, 2), launch="P", write=False)["kit"]
        self.assertEqual(got["oracle"]["shard"], "2/2"); self.assertNotIn("shard", got["units"])
        self.assertTrue(got["oracle"]["job"].endswith("__prove-oracle__shard-2of2"))
        with open(os.path.join(self.tmp, "jobs", got["oracle"]["job"], "config.json")) as f:
            std = json.load(f)["standard"]
        self.assertEqual((std["launch"], std["shard"]["index"]), ("P", 2))
        self.assertEqual(got["oracle"]["units"], len(std["shard"]["planned"]))
        self.assertTrue(all(shard.of(u, 2) == 2 for u in std["shard"]["planned"]))

    def test_removing_images_reaches_each_controls_run(self):
        b = kit("agent-workspace-v1", os.path.join(self.tmp, "kits"))
        seen, real = [], run.run
        with patch.object(run, "run", lambda *a, **kw: seen.append(kw.get("remove_images")) or real(*a, **kw)):
            self.prove(b, remove_images=True, write=False)
        self.assertEqual(seen, [True, True])

    def test_a_partial_proof_says_so_and_does_not_replace_a_fuller_one(self):
        b = kit("agent-task-v1", os.path.join(self.tmp, "kits"))
        got = self.prove(b, limit=1)
        self.assertEqual((got["kit"]["oracle"]["limit"], got["kit"]["oracle"]["units"]), (1, 1))
        prove.record(b, {"kit": {"oracle": {"ok": True, "units": 3, "passed": "3/3"}}})
        prove.record(b, {"kit": {"oracle": {"ok": True, "units": 1, "passed": "1/1", "limit": 1}}})
        self.assertEqual(b["proven"]["kit"]["oracle"]["units"], 3)
        prove.record(b, {"kit": {"oracle": {"ok": False, "units": 3, "problems": ["x"]}}})
        self.assertNotIn("oracle", b["proven"]["kit"])                 # a failed check is taken back

    def test_a_reference_below_its_bound_fails_and_names_why(self):
        b = kit("agent-task-v1", os.path.join(self.tmp, "kits"))
        with open(os.path.join(b["_here"], "driver.py")) as f:
            src = f.read()
        with open(os.path.join(b["_here"], "driver.py"), "w") as f:
            f.write(src.replace('return {"answer": WORDS[unit]}', 'return {"answer": WORDS[unit] if unit != "t2" else "no"}'))
        got = self.prove(b, write=False)
        self.assertFalse(got["kit"]["oracle"]["ok"])
        self.assertIn("pass rate 0.6667, expected at least 1.0", got["kit"]["oracle"]["problems"])

    def test_unstable_or_repeated_units_fail(self):
        b = kit("agent-task-v1", os.path.join(self.tmp, "kits"))
        with open(os.path.join(b["_here"], "driver.py")) as f:
            src = f.read()
        with open(os.path.join(b["_here"], "driver.py"), "w") as f:
            f.write(src.replace('return {"units": list(WORDS)}', 'import random\n    return {"units": random.sample(list(WORDS), 3) + ["t1"]}'))
        r = prove.units_check(prove._Driver(b, b["datasets"][0], self.tmp), b["datasets"][0])
        self.assertFalse(r["ok"])
        self.assertTrue(any("repeat" in p for p in r["problems"]))
        self.assertTrue(any("says 3 units" in p for p in r["problems"]))

    def test_units_that_may_not_be_redistributed_are_counted_never_named(self):
        """A prompt naming its answer as declared, and a reference left in a
        file: the definition keeps the counts, not the units; one whose units
        may be redistributed names them."""
        here = os.path.join(self.tmp, "held")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(HELD)
        d = {"schema": "nullius.benchmark/v1", "key": "held", "label": "Held", "summary": "Synthetic units.", "family": "agent",
             "needs": ["agent.task/v1"], "driver": {"command": ["python3", "{here}/driver.py"]},
             "datasets": [{"id": "choice", "units": 1}, {"id": "leaky", "units": 1}],
             "units": {"noun": "question", "nouns": "questions", "redistribute": False},
             "controls": dict(CONTROLS, oracle=dict(CONTROLS["oracle"], in_prompt="A choice question names its answer.")),
             "score": {"kind": "pass", "says": "The reference."}}
        with open(os.path.join(here, "standard.json"), "w") as f:
            json.dump(d, f)
        b, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", "held")
        self.assertEqual(bad, [])
        got = self.prove(b)
        self.assertEqual((got["choice"]["leak"]["in_prompt"]["first"], got["leaky"]["leak"]["ok"]), (["held-unit-1"], False))
        with open(os.path.join(here, "standard.json")) as f:
            text = f.read()
        self.assertNotIn("held-unit", text)
        proven = json.loads(text)["proven"]
        self.assertEqual(proven["choice"]["leak"]["in_prompt"], {"units": 1, "why": "A choice question names its answer."})
        self.assertEqual((proven["leaky"]["failed"]["leak"]["passed"], proven["leaky"]["failed"]["leak"]["problems"]), ("0/1", []))
        self.assertEqual(proven["leaky"]["oracle"]["passed"], "1/1")
        b["units"] = {"noun": "question", "nouns": "questions"}
        prove.record(b, got)
        self.assertEqual(b["proven"]["choice"]["leak"]["in_prompt"]["first"], ["held-unit-1"])
        self.assertIn("held-unit-2", b["proven"]["leaky"]["failed"]["leak"]["problems"][0])


class ProveOptions(unittest.TestCase):
    """The benchmark's options reach the checks' driver and each control's run; a merged control job is held as one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        here = os.path.join(self.tmp, "kits", "agent-task-v1")
        shutil.copytree(os.path.join(definitions.KITS, "agent-task-v1"), here)
        with open(os.path.join(here, "driver.py")) as f:
            src = f.read()
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(src.replace('return {"units": list(WORDS)}',
                                'import os\n    return {"units": list(WORDS) if os.environ.get("NULLIUS_OPTION_SITES") == "on" else []}'))
        with open(os.path.join(here, "standard.json")) as f:
            d = json.load(f)
        d["options"] = {"sites": {"summary": "The sites.", "values": ["off", "on"], "default": "off"},
                        "lane": {"summary": "A lane.", "values": ["a", "b"], "default": "a"},
                        "tool": {"summary": "A tool.", "values": ["none", "x"], "default": "none",
                                 "needs": {"x": {"host": ["tool"]}}}}
        d["host"] = {"tool": {"summary": "A program this machine lacks.", "check": ["nullius-no-such-program-k5"]}}
        with open(os.path.join(here, "standard.json"), "w") as f:
            json.dump(d, f)
        self.b, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", "agent-task-v1")
        assert not bad, bad
        self.jobs = os.path.join(self.tmp, "jobs")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prove(self, **kw):
        kw.setdefault("stamp", "2026-01-02__03-04-05")
        return prove.prove(self.b, jobs_root=self.jobs, env={}, say=lambda s: None, **kw)

    def config(self, job_dir):
        with open(os.path.join(job_dir, "config.json")) as f:
            return json.load(f)["standard"]

    def nop_shard(self, i, n, lane):
        return run.run(self.b, definitions.control(self.b, "nop"), model="", provider="openai", jobs_root=self.jobs, env={},
                       say=lambda s: None, shard=(i, n), launch="L", bench_options={"sites": "on", "lane": lane})

    def test_the_options_reach_the_units_driver_and_each_controls_run(self):
        got = self.prove(bench_options={"sites": "on"}, write=False)["kit"]
        self.assertTrue(all(r["ok"] for r in got.values()), got)
        self.assertEqual((got["units"]["units"], got["nop"]["passed"], got["oracle"]["passed"]), (3, "0/3", "3/3"))
        self.assertEqual(self.config(os.path.join(self.jobs, got["nop"]["job"]))["benchmark_options"],
                         {"sites": "on", "lane": "a", "tool": "none"})
        self.assertEqual(got["nop"]["options"], {"sites": "on", "lane": "a", "tool": "none"})
        dflt = self.prove(write=False, stamp="2026-01-02__03-04-06")["kit"]
        self.assertEqual(dflt["units"]["problems"], ["units: no ids"])
        self.assertEqual(dflt["nop"]["problems"], ["the units check failed: the control was not run"])

    def test_a_name_or_a_value_the_benchmark_does_not_allow_is_refused_before_anything_runs(self):
        was = json.dumps(self.b.get("proven"))
        for given, why in (({"sites": "maybe"}, "'maybe' is not one of off, on"), ({"nope": "x"}, "has no option 'nope'")):
            with self.assertRaises(run.Refused) as e:
                self.prove(bench_options=given)
            self.assertIn(why, str(e.exception))
        self.assertFalse(os.path.exists(self.jobs))
        self.assertEqual(json.dumps(self.b.get("proven")), was)

    def test_an_option_whose_value_needs_a_program_this_machine_lacks_is_refused_before_any_install(self):
        with patch.object(install, "upstream", side_effect=AssertionError("installed")) as up:
            with self.assertRaises(run.Refused) as e:
                self.prove(bench_options={"sites": "on", "tool": "x"})
        self.assertIn("nullius-no-such-program-k5 not on PATH", str(e.exception))
        up.assert_not_called()
        self.assertFalse(os.path.exists(self.jobs))

    def test_a_job_is_not_recorded_when_the_units_check_fails(self):
        from nullius.standard import merge
        whole = merge.merge([self.nop_shard(i, 4, "a") for i in (1, 3)], os.path.join(self.jobs, "nop-merged"))["out"]
        self.b["datasets"][0]["units"] = 4
        got = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": whole})["kit"]
        self.assertFalse(got["units"]["ok"])
        self.assertFalse(got["nop"]["ok"])
        self.assertIn("the units check failed, so this job cannot be checked against the dataset", got["nop"]["problems"])
        kept = self.b["proven"]["kit"]
        self.assertNotIn("nop", kept)
        self.assertNotIn("units", kept)
        self.assertNotIn("job", kept["failed"]["nop"])
        self.assertIn("the units check failed, so this job cannot be checked against the dataset",
                      kept["failed"]["nop"]["problems"])

    def test_a_control_is_not_run_nor_kept_when_the_units_check_fails(self):
        self.assertIn("nop", self.b["proven"]["kit"])
        self.b["datasets"][0]["units"] = 4
        got = self.prove(bench_options={"sites": "on"})["kit"]
        self.assertFalse(got["units"]["ok"])
        for c in ("nop", "oracle"):
            self.assertEqual(got[c]["problems"], ["the units check failed: the control was not run"])
            self.assertNotIn(c, self.b["proven"]["kit"])
            self.assertIn(c, self.b["proven"]["kit"]["failed"])
        self.assertFalse(os.path.exists(self.jobs) and any("prove-" in x for x in os.listdir(self.jobs)))

    def test_a_trial_of_another_job_cannot_be_merged_into_a_proof(self):
        from nullius.standard import merge
        shards = [self.nop_shard(i, 4, "a") for i in (1, 3)]
        trial = next(t for t in sorted(os.listdir(shards[0])) if os.path.isfile(os.path.join(shards[0], t, "result.json")))
        path = os.path.join(shards[0], trial, "result.json")
        with open(path) as f:
            res = json.load(f)
        res["standard"]["job"] = "old-unrelated-job"
        with open(path, "w") as f:
            json.dump(res, f)
        with self.assertRaises(merge.Refused) as e:
            merge.merge(shards, os.path.join(self.jobs, "never"))
        self.assertIn("names another job than its job", str(e.exception))
        forced = merge.merge(shards, os.path.join(self.jobs, "forced"), force=True)["out"]
        r = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": forced}, write=False)["kit"]["nop"]
        self.assertFalse(r["ok"])
        self.assertTrue(any("merged despite differences" in p and "not of the job they are in" in p for p in r["problems"]),
                        r["problems"])

    def test_a_merge_of_merges_keeps_what_any_of_them_was_merged_despite(self):
        from nullius.standard import merge
        good_shard, bad_shard = (self.nop_shard(i, 4, "a") for i in (1, 3))
        trial = next(t for t in sorted(os.listdir(bad_shard)) if os.path.isfile(os.path.join(bad_shard, t, "result.json")))
        path = os.path.join(bad_shard, trial, "result.json")
        with open(path) as f:
            res = json.load(f)
        res["standard"]["job"] = "old-unrelated-job"
        with open(path, "w") as f:
            json.dump(res, f)
        good = merge.merge([good_shard], os.path.join(self.jobs, "good"))["out"]
        bad = merge.merge([bad_shard], os.path.join(self.jobs, "bad"), force=True)["out"]
        with self.assertRaises(merge.Refused):
            merge.merge([good, bad], os.path.join(self.jobs, "never"))
        both = merge.merge([good, bad], os.path.join(self.jobs, "both"), force=True)["out"]
        std = self.config(both)
        self.assertTrue(any(w.startswith("bad: ") and "not of the job they are in" in w for w in std["merged_despite"]))
        self.assertEqual([m.get("merged_from", [{}])[0].get("job") for m in std["merged_from"]], [os.path.basename(good_shard), os.path.basename(bad_shard)])
        r = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": both}, write=False)["kit"]["nop"]
        self.assertFalse(r["ok"])
        self.assertTrue(any("merged despite differences" in p and "not of the job they are in" in p for p in r["problems"]),
                        r["problems"])

    def test_the_cli_hands_its_options_and_jobs_to_the_proof(self):
        from nullius import progress
        from nullius.standard import cli
        seen = {}
        with patch.object(cli, "_bench", return_value=self.b), patch.object(cli, "licences", return_value=(None, None)), \
                patch.object(cli, "_exit_on_term"), patch.object(progress, "ECHO", None), \
                patch.object(prove, "prove", side_effect=lambda b, **kw: seen.update(kw) or {}):
            self.assertEqual(cli.main(["prove", "agent-task-v1", "--from-run", "--dataset", "kit", "--bench-option", "sites=on",
                                       "--bench-option", "lane=b", "--job", "nop=/x/merged"]), 0)
            self.assertEqual((seen["bench_options"], seen["jobs"]), ({"sites": "on", "lane": "b"}, {"nop": "/x/merged"}))
            with self.assertRaises(SystemExit):
                cli.main(["prove", "agent-task-v1", "--from-run", "--job", "reference=/x"])

    def test_a_merged_launch_is_held_to_the_bound_as_one_job_and_must_cover_every_unit(self):
        from nullius.standard import merge
        shards = [self.nop_shard(i, 4, lane) for i, lane in ((1, "a"), (3, "b"))]
        whole = merge.merge(shards, os.path.join(self.jobs, "nop-merged"))["out"]
        before = sorted(os.listdir(self.jobs))
        got = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": whole})["kit"]
        self.assertTrue(got["nop"]["ok"], got["nop"])
        self.assertEqual((got["nop"]["job"], got["nop"]["units"], got["nop"]["passed"]), ("nop-merged", 3, "0/3"))
        self.assertNotIn("job", self.b["proven"]["kit"]["nop"])
        self.assertEqual(self.b["proven"]["kit"]["nop"]["options"],
                         [{"lane": "a", "sites": "on", "tool": "none"}, {"lane": "b", "sites": "on", "tool": "none"}])
        self.assertEqual(sorted(set(os.listdir(self.jobs)) - set(before)), ["2026-01-02__03-04-05__agent-task-v1__kit__prove-oracle"])
        self.assertEqual([m["benchmark_options"]["lane"] for m in self.config(whole)["merged_from"]], ["a", "b"])
        half = merge.merge(shards[:1], os.path.join(self.jobs, "nop-half"))["out"]
        part = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": half}, write=False, stamp="2026-01-02__03-04-06")["kit"]["nop"]
        self.assertFalse(part["ok"])
        self.assertTrue(any("units have no trial" in p for p in part["problems"]), part["problems"])
        wrong = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"oracle": whole}, write=False, stamp="2026-01-02__03-04-07")["kit"]["oracle"]
        self.assertIn("nop-merged: its control is 'nop', not 'oracle'", wrong["problems"])
        trial = next(t for t in sorted(os.listdir(whole)) if os.path.isfile(os.path.join(whole, t, "result.json")))
        with open(os.path.join(whole, trial, "result.json")) as f:
            res = json.load(f)
        res.pop("verifier_result", None)
        res.pop("exception_info", None)
        with open(os.path.join(whole, trial, "result.json"), "w") as f:
            json.dump(res, f)
        blank = self.prove(dataset="kit", bench_options={"sites": "on"}, jobs={"nop": whole}, write=False,
                           stamp="2026-01-02__03-04-08")["kit"]["nop"]
        self.assertFalse(blank["ok"])
        self.assertIn("1 unit(s) ended with no reward from 0 to 1 and no error", blank["problems"])
        for kw in ({"limit": 1}, {"shard": (1, 2)}):
            with self.assertRaises(run.Refused):
                self.prove(dataset="kit", jobs={"nop": whole}, **kw)
        with self.assertRaises(run.Refused):
            self.prove(jobs={"nop": os.path.join(self.tmp, "absent")})


NUMERIC = {"key": "numeric", "_ref": "numeric", "label": "Numeric", "family": "agent", "needs": ["agent.task/v1"],
           "units": {"noun": "task", "nouns": "tasks"}, "score": {"kind": "score", "says": "a number from 0 to 1"},
           "controls": {"nop": {"source": "nullius", "from": "Nothing runs."}}, "datasets": [{"id": "all", "units": 3}]}


class HeldJob(unittest.TestCase):
    """A control's job held to its bound, read from each trial's own record."""

    def setUp(self):
        from nullius.standard import job
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "nop-job")
        os.makedirs(self.dir)
        nop = definitions.control(NUMERIC, "nop")
        self.units = ["u1", "u2", "u3"]
        job.write_config(self.dir, job="nop-job", bench=NUMERIC, other=nop, model="", provider="openai", dataset="all",
                         units=self.units, concurrency=1, granted=False)
        results = [job.write_trial(os.path.join(self.dir, f"{u}__u{i:04d}"), job="nop-job", bench=NUMERIC, other=nop, model="",
                                   unit=u, t0=1_760_000_000.0, t1=1_760_000_001.0, produce=(None, None), score={"reward": 0.0},
                                   exception=None, calls=[], trajectory=None) for i, u in enumerate(self.units)]
        job.write_result(self.dir, "nop-job", 1_760_000_000.0, 1_760_000_002.0, results, 3)

    def held(self):
        return prove.control_check(NUMERIC, NUMERIC["datasets"][0], "nop", limit=None, jobs_root=self.tmp, stamp="s",
                                   provider="openai", model="", judge_model=None, env={}, concurrency=1,
                                   say=lambda s: None, job_dir=self.dir, ids=self.units)

    def edit(self, unit, change):
        trial = next(t for t in os.listdir(self.dir) if t.startswith(unit + "__"))
        path = os.path.join(self.dir, trial, "result.json")
        with open(path) as f:
            res = json.load(f)
        change(res)
        with open(path, "w") as f:
            json.dump(res, f)

    def test_a_clean_job_passes(self):
        r = self.held()
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual((r["passed"], r["value"], r["units"]), ("0/3", 0.0, 3))

    def test_a_reward_without_a_score_a_score_below_0_and_nan_are_no_score(self):
        for value in ({"reward": 1.0}, {"score": -100}, {"score": float("nan")}, {"score": True}, {"score": "0"}):
            self.edit("u2", lambda res: res["verifier_result"].update(rewards=value))
            r = self.held()
            self.assertFalse(r["ok"], value)
            self.assertIn("1 unit(s) ended with no score from 0 to 1 and no error", r["problems"], value)
            self.assertEqual(r["value"], 0.0)

    def test_a_trial_another_run_wrote_is_refused(self):
        self.edit("u1", lambda res: res["standard"].update(benchmark="other", control={"name": "oracle"}, plugged="oracle"))
        self.edit("u3", lambda res: res["standard"].update(job="another-job"))
        r = self.held()
        self.assertFalse(r["ok"])
        said = " | ".join(r["problems"])
        for k in ("benchmark", "control", "who ran it", "job"):
            self.assertIn(f"whose own record names another {k} than the job", said)


LEAKY = '''from nullius.standard.jsonl import serve
import os
ANSWER = "the reference answer is forty-two"
def units(dataset):
    return {"units": ["prompt", "file", "clean", "short"]}
def setup(unit, dir):
    if unit == "file":
        open(os.path.join(dir, "notes.txt"), "w").write("draft\\n" + ANSWER + "\\n")
    return {"prompt": "Answer: " + ANSWER if unit == "prompt" else "Answer the question."}
def oracle(unit, dir):
    if unit == "short":
        return {"answer": "42"}
    if unit == "file":
        open(os.path.join(dir, "answer.txt"), "w").write(ANSWER + "\\n")
        return {"answer": ""}
    return {"answer": ANSWER}
def score(unit, dir, transcript, answer):
    return {"reward": 0.0}
serve({"units": units, "setup": setup, "oracle": oracle, "score": score})
'''

HELD = '''from nullius.standard.jsonl import serve
import os
ANSWER = "the reference answer is forty-two"
UNITS = {"choice": ["held-unit-1"], "leaky": ["held-unit-2"]}
def units(dataset):
    return {"units": UNITS[dataset]}
def setup(unit, dir):
    if unit == "held-unit-2":
        open(os.path.join(dir, "notes.txt"), "w").write(ANSWER + "\\n")
    return {"prompt": "Is it " + ANSWER + ", or not?"}
def oracle(unit, dir):
    return {"answer": ANSWER}
def score(unit, dir, transcript, answer):
    return {"reward": float(answer == ANSWER), "passed": answer == ANSWER}
serve({"units": units, "setup": setup, "oracle": oracle, "score": score})
'''


class Leak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        here = os.path.join(self.tmp, "leaky")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(LEAKY)
        self.b = {"key": "leaky", "label": "Leaky", "family": "agent", "_here": here,
                  "driver": {"command": ["python3", "{here}/driver.py"]}, "datasets": [{"id": "all", "units": 4}]}
        self.d = prove._Driver(self.b, self.b["datasets"][0], self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_answer_in_the_prompt_or_in_a_file_is_a_leak_and_a_short_one_is_not_looked_for(self):
        r = prove.leak_check(self.d, ["prompt", "file", "clean", "short"], "agent")
        self.assertFalse(r["ok"])
        self.assertEqual(r["passed"], "2/4")
        self.assertEqual(r["problems"], ["prompt: the answer is already there after setup, in the prompt",
                                         "file: answer.txt is already there after setup, in a file"])
        self.assertEqual(r["short"], 1)
        self.assertIn("under 12 characters", r["note"])

    def test_a_declared_choice_question_is_counted_in_the_prompt_never_in_a_file(self):
        r = prove.leak_check(self.d, ["prompt", "file", "clean"], "agent", in_prompt="A choice question names its answer.")
        self.assertEqual(r["problems"], ["file: answer.txt is already there after setup, in a file"])
        self.assertEqual(r["in_prompt"]["first"], ["prompt"])


class Builder(unittest.TestCase):
    def defs(self):
        d = {k: definitions.load(k, FIX)[0] for k in ("benchmark", "agent", "capability")}
        d["benchmark"][0]["controls"] = CONTROLS
        return d

    def recipe(self, **kw):
        r = {"version": 1, "harness": "standard", "name": "fake-nop", "title": "t", "benchmark": "fakebench", "dataset": "all",
             "other": "nop", "provider": "openai", "model": "", "limit": 2, "concurrency": 2, "unsandboxed": False, "where": "local"}
        r.update(kw)
        return r

    def test_a_control_needs_no_model_and_no_yes_and_runs_keyless(self):
        out = recipe.compile(self.recipe(), self.defs())
        self.assertEqual(out["errors"], [])
        self.assertTrue(out["keyless"])
        self.assertIn(" run fakebench nop ", out["script"])
        self.assertNotIn("--model", out["script"])
        self.assertNotIn("--unsandboxed", out["script"])
        self.assertEqual(out["jobs"][0]["agent"], "nop")

    def test_an_undeclared_control_is_refused_on_the_agent_step(self):
        out = recipe.compile(self.recipe(benchmark="fakememo", other="oracle"), self.defs())
        self.assertEqual(out["errors"][0]["step"], recipe.STEP["agent"])

    def test_the_page_carries_each_datasets_controls_and_whether_proven(self):
        d = self.defs()
        d["benchmark"][0]["proven"] = {"all": {"oracle": {"passed": "4/4", "units": 4, "on": "2026-01-02"}}}
        with patch.object(recipe, "_all", lambda: d):
            c = recipe.choices()
        page = c["benchmarks"][0]
        self.assertEqual([x["key"] for x in page["controls"]], ["nop", "oracle"])
        ds = page["datasets"][0]
        self.assertEqual({x["key"]: (x["source"], bool(x["proven"])) for x in ds["controls"]},
                         {"nop": ("nullius", False), "oracle": ("official", True)})
        self.assertEqual(ds["proofs"], {"units": None, "leak": None})
        self.assertEqual(c["fits"]["fakebench"], {"fake": [], "nop": [], "oracle": []})


class ProofDriverCredentials(unittest.TestCase):
    def test_units_receive_saved_or_handed_driver_credentials_only(self):
        from nullius import secrets
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SECRETS_DIR": os.path.join(tmp, "secrets")}):
            secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", "synthetic-proof-token")
            here = Path(tmp) / "benchmark"
            here.mkdir()
            (here / "driver.py").write_text('''import os
from nullius.standard.jsonl import serve
def units(dataset):
    assert os.environ.get("HF_TOKEN") == "synthetic-proof-token"
    assert "AGENT_ONLY" not in os.environ
    return {"units": ["one"]}
serve({"units": units})
''')
            bench = {"key": "credential-fixture", "label": "Credential fixture", "family": "agent", "_here": str(here),
                     "driver": {"command": ["python3", "{here}/driver.py"]}, "datasets": [{"id": "all", "units": 1}],
                     "environments": {"data": {"needs_secrets": [
                         {"name": "HF_TOKEN", "kind": "value", "to": "driver", "provider": "huggingface"},
                         {"name": "AGENT_ONLY", "kind": "value", "to": "agent"}]}}}
            secrets.set(bench["key"], "AGENT_ONLY", "synthetic-agent-token")
            secrets.set(bench["key"], "HF_TOKEN", "synthetic-stale-token")          # never read: its provider's alone
            handed = run.Licences.handed(bench, {"HF_TOKEN": b"synthetic-proof-token", "AGENT_ONLY": b"synthetic-agent-token"}, tmp)
            for name, held in (("saved", None), ("handed", handed)):
                with self.subTest(name):
                    if held is not None:
                        secrets.delete(secrets.HUGGINGFACE, "HF_TOKEN")
                    result = prove.prove(bench, write=False, licences=held, say=lambda _: None)
                    self.assertTrue(result["all"]["units"]["ok"], result)


if __name__ == "__main__":
    unittest.main()
