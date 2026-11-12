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

    def test_the_result_has_every_check_and_proven_is_written_with_the_jobs(self):
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
        self.assertEqual(proven["oracle"]["job"], "2026-01-02__03-04-05__agent-workspace-v1__kit__prove-oracle")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "jobs", proven["nop"]["job"])))
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
        may be redistributed names them, as before."""
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
