"""A kit's basic checks and its skills: the port is proven when every basic
check passes, each skill is recorded shown or not and never takes the port
away; a benchmark may rely on a skill (``needs_skills``) and refuses an agent
that has not shown it; ``--repeat`` holds a check by the majority of its
runs; and a proof from before skills were recorded reads as all basic."""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest

from nullius.standard import cli, conform, definitions, install, match, recipe

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard")
TOOLS = "agent.tools/v1"
KIT = {"basic": ["one-tool", "arguments"], "skills": {"decoys": "picks the right tool", "chain": "chains", "retry": "retries"}}


def agent(proven, offers=(TOOLS,)):
    return {"_kind": "agent", "key": "bot", "label": "Bot", "offers": list(offers), "proven": proven}


def bench(**kw):
    return dict({"key": "b", "label": "B", "family": "agent", "needs": [TOOLS]}, **kw)


class Outcome(unittest.TestCase):
    def test_the_port_is_its_basic_checks_and_each_skill_is_recorded(self):
        got = conform.outcome(KIT, {"one-tool": [True], "arguments": [True], "decoys": [False], "chain": [True], "retry": [False]})
        self.assertEqual(got["passed"], "2/2")
        self.assertTrue(got["ok"])                                       # skills missed: the port stays
        self.assertEqual(got["skills"], {"decoys": {"passed": "0/1", "ok": False}, "chain": {"passed": "1/1", "ok": True},
                                         "retry": {"passed": "0/1", "ok": False}})
        self.assertNotIn("checks", got)                                  # one run each: nothing more to count

    def test_a_failed_basic_check_fails_the_port_whatever_the_skills(self):
        got = conform.outcome(KIT, {"one-tool": [True], "arguments": [False], "decoys": [True], "chain": [True], "retry": [True]})
        self.assertEqual((got["passed"], got["ok"]), ("1/2", False))

    def test_a_basic_check_the_job_never_ran_is_not_held(self):
        got = conform.outcome(KIT, {"one-tool": [True]})
        self.assertEqual((got["passed"], got["ok"]), ("1/2", False))
        self.assertEqual(got["skills"]["decoys"], {"passed": "0/0", "ok": False})

    def test_a_kit_without_skills_is_every_check_basic_as_before(self):
        got = conform.outcome({}, {"a": [True], "b": [True], "c": [False]})
        self.assertEqual((got["passed"], got["ok"]), ("2/3", False))
        self.assertNotIn("skills", got)
        self.assertEqual(conform.outcome({}, {"a": [True], "b": [True]}), {"passed": "2/2", "ok": True})

    def test_repeat_holds_a_check_by_its_majority_and_counts_the_runs(self):
        runs = {"one-tool": [True, True, True], "arguments": [True, False, True], "decoys": [False, True, False],
                "chain": [True, True, False], "retry": [False, False, False]}
        got = conform.outcome(KIT, runs, repeat=3)
        self.assertTrue(got["ok"])
        self.assertEqual(got["repeat"], 3)
        self.assertEqual(got["checks"], {"one-tool": "3/3", "arguments": "2/3"})
        self.assertEqual({s: (r["passed"], r["ok"]) for s, r in got["skills"].items()},
                         {"decoys": ("1/3", False), "chain": ("2/3", True), "retry": ("0/3", False)})
        self.assertFalse(conform.outcome(KIT, dict(runs, arguments=[True, False, False]), 3)["ok"])

    def test_majority_is_more_than_half(self):
        self.assertEqual([conform.majority(p, n) for p, n in ((2, 3), (1, 2), (2, 2), (0, 0), (1, 1))],
                         [True, False, True, False, True])

    def test_said_names_each_skill_shown_or_not(self):
        rec = conform.outcome(definitions.kit(TOOLS), {"one-tool": [True], "arguments": [True], "decoys": [False],
                                                        "chain": [True], "retry": [False]})
        lines = conform.said(TOOLS, rec)
        self.assertEqual(lines[0], "agent.tools/v1: passed 2/2")
        self.assertIn("  skill decoys: not shown 0/1 - chooses the right tool among similar ones", lines)
        self.assertIn("  skill chain: shown 1/1 - feeds one tool's answer into the next, three times over", lines)


class Shown(unittest.TestCase):
    def test_the_tools_kit_has_two_basic_checks_and_three_skills_of_its_five(self):
        k = definitions.kit(TOOLS)
        self.assertEqual(k["basic"], ["one-tool", "arguments"])
        self.assertEqual(list(definitions.skills_of(k)), ["decoys", "chain", "retry"])
        spec = importlib.util.spec_from_file_location("kit_driver_for_skills", os.path.join(k["_here"], "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        self.assertEqual(set(k["basic"]) | set(definitions.skills_of(k)), set(driver.UNITS))       # every check is one or the other
        self.assertEqual(definitions.port_skills("agent.task/v1"), {})              # a kit without skills

    def test_a_proof_says_which_skills_it_showed(self):
        rec = {"passed": "2/2", "kit_version": 2, "skills": {"decoys": {"passed": "0/3", "ok": False}, "chain": {"passed": "3/3", "ok": True}}}
        self.assertEqual(definitions.skills_shown(TOOLS, rec), ["chain"])

    def test_a_proof_from_before_skills_reads_as_all_basic(self):
        old = {"passed": "5/5", "kit_version": 2}                         # every check passed, all of them basic then
        self.assertEqual(definitions.skills_shown(TOOLS, old), ["decoys", "chain", "retry"])
        self.assertEqual([s["shown"] for s in recipe.skills_of(TOOLS, old)], [True, True, True])
        older = {"passed": "2/2"}                                          # kit v1: it says nothing of v2's skills
        self.assertEqual(definitions.skills_shown(TOOLS, older), [])
        self.assertEqual(recipe.skills_of(TOOLS, older), [])
        self.assertEqual(definitions.skills_shown(TOOLS, None), [])

    def test_cli_shows_the_skills_under_the_port(self):
        d = agent({TOOLS: {"passed": "2/2", "kit_version": 2, "skills": {"decoys": {"passed": "0/1", "ok": False},
                                                                          "chain": {"passed": "1/1", "ok": True},
                                                                          "retry": {"passed": "0/1", "ok": False}}}})
        self.assertEqual(cli.skills(d), ["agent.tools/v1 skills: decoys ✗ · chain ✓ · retry ✗"])
        self.assertEqual(cli.skills(d, ["agent.task/v1"]), [])
        self.assertEqual(cli.skills(agent({TOOLS: {"passed": "2/2"}})), [])           # an older kit: older() speaks


class NeedsSkills(unittest.TestCase):
    MISSED = {TOOLS: {"passed": "2/2", "kit_version": 2, "skills": {"decoys": {"passed": "0/3", "ok": False},
                                                                    "chain": {"passed": "3/3", "ok": True},
                                                                    "retry": {"passed": "1/3", "ok": False}}}}

    def test_a_benchmark_needing_a_skill_refuses_an_agent_that_has_not_shown_it(self):
        why = match.fit(bench(needs_skills={TOOLS: ["decoys", "chain"]}), agent(self.MISSED))
        self.assertEqual(why, ["Bot has not shown the decoys skill of agent.tools/v1 (chooses the right tool among similar ones)"])
        self.assertEqual(match.fit(bench(needs_skills={TOOLS: ["chain"]}), agent(self.MISSED)), [])
        self.assertEqual(match.fit(bench(), agent(self.MISSED)), [])                     # needing none: the port is enough

    def test_a_proof_from_before_skills_on_the_current_kit_fits(self):
        self.assertEqual(match.fit(bench(needs_skills={TOOLS: ["retry"]}), agent({TOOLS: {"passed": "5/5", "kit_version": 2}})), [])
        why = match.fit(bench(needs_skills={TOOLS: ["retry"]}), agent({TOOLS: {"passed": "2/2"}}))
        self.assertEqual(len(why), 1)
        self.assertIn("has not shown the retry skill of agent.tools/v1", why[0])

    def test_problems_checks_needs_skills_against_the_kit(self):
        base = {k: v for k, v in definitions.kit(TOOLS).items() if not k.startswith("_")}
        base = {k: v for k, v in base.items() if k not in ("basic", "skills", "proven")}
        ok = dict(base, key="b", needs=[TOOLS], needs_skills={TOOLS: ["decoys"]})
        self.assertEqual(definitions.problems(ok, "benchmark"), [])
        self.assertIn("needs_skills.agent.tools/v1: 'guess' is not a skill of its kit (decoys, chain, retry)",
                      definitions.problems(dict(ok, needs_skills={TOOLS: ["guess"]}), "benchmark"))
        self.assertIn("needs_skills.agent.task/v1: a port the benchmark needs",
                      definitions.problems(dict(ok, needs_skills={"agent.task/v1": ["x"]}), "benchmark"))
        self.assertIn("needs_skills.agent.tools/v1: a list of skill ids",
                      definitions.problems(dict(ok, needs_skills={TOOLS: []}), "benchmark"))
        self.assertIn("needs_skills: {port: [skill ids of its kit]}", definitions.problems(dict(ok, needs_skills=["decoys"]), "benchmark"))

    def test_problems_checks_a_kits_basic_and_skills(self):
        k = {x: v for x, v in definitions.kit(TOOLS).items() if not x.startswith("_")}
        self.assertEqual(definitions.problems(k, "benchmark"), [])
        self.assertIn("skills: one-tool cannot be a basic check and a skill",
                      definitions.problems(dict(k, skills={"one-tool": "x"}), "benchmark"))
        self.assertIn("skills: {check id: what the skill shows, in plain words}",
                      definitions.problems(dict(k, skills={"decoys": ""}), "benchmark"))
        self.assertIn("basic: a list of the kit's check ids that are the port itself",
                      definitions.problems(dict(k, basic="one-tool"), "benchmark"))


class Repeat(unittest.TestCase):
    """``conform --repeat``: each check a trial of its own per run, the record counting them."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "catalogue")
        shutil.copytree(os.path.join(FIX, "catalogue"), self.root)
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_check_runs_k_times_and_the_proof_counts_them(self):
        d = definitions.get("capability", "fakemem", self.root)
        got = conform.check(d, model="m", provider="custom", which=["cap.memory/v1"], jobs_root=os.path.join(self.tmp, "jobs"),
                            say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, repeat=2)
        rec = got["cap.memory/v1"]
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["passed"], rec["repeat"]), ("2/2", 2))
        self.assertEqual(sorted(rec["checks"].values()), ["2/2", "2/2"])
        trials = sorted(n for n in os.listdir(os.path.join(self.tmp, "jobs", rec["job"])) if "__u" in n)
        self.assertEqual(len(trials), 4)
        self.assertEqual(sorted(t.rsplit("__", 1)[1] for t in trials), ["u0000r1", "u0000r2", "u0001r1", "u0001r2"])
        saved = definitions.get("capability", "fakemem", self.root)["proven"]["cap.memory/v1"]
        self.assertEqual(saved["checks"], rec["checks"])
        self.assertNotIn("ok", saved)


if __name__ == "__main__":
    unittest.main()
