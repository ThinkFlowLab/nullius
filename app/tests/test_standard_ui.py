"""What the standard's pages are given: a backend refused where docker cannot
run, the proof a benchmark's page starts, a failed proof kept with the value
it reached, an agent's proofs with their kit, ports by name, and a run of
controls handed no key at all. No network, no docker."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius import execution
from nullius.standard import definitions, ports, prove, recipe

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
CONTROLS = {"nop": {"source": "nullius", "from": "Nothing runs; the answer is empty."},
            "oracle": {"source": "official", "from": "The authors' answer field."}}
STAMP = "2026-01-02__03-04-05"


def defs():
    d = {k: definitions.load(k, FIX)[0] for k in ("benchmark", "agent", "capability")}
    d["benchmark"][0]["controls"] = CONTROLS
    return d


def a_recipe(**kw):
    r = {"version": 1, "harness": "standard", "name": "fake-run", "title": "t", "benchmark": "fakebench", "dataset": "all",
         "other": "fake", "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": False, "where": "local"}
    r.update(kw)
    return r


class Backend(unittest.TestCase):
    def test_docker_needs_no_yes_and_says_so_in_the_command(self):
        out = recipe.compile(a_recipe(backend="docker"), defs(), docker_unusable=lambda: None)
        self.assertEqual(out["errors"], [])
        self.assertIn("--backend docker", out["script"])
        self.assertNotIn("--unsandboxed", out["script"])
        self.assertTrue(out["docker"])

    def test_local_still_asks_for_the_yes_on_the_where_step(self):
        out = recipe.compile(a_recipe(), defs())
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"]])
        self.assertIn("--unsandboxed", recipe.compile(a_recipe(unsandboxed=True), defs())["script"])

    def test_docker_this_machine_cannot_run_is_refused_on_the_where_step_with_its_reason(self):
        why = "docker is installed but not usable by this user: permission denied"
        out = recipe.compile(a_recipe(backend="docker"), defs(), docker_unusable=lambda: why)
        self.assertEqual(out["errors"], [{"step": recipe.STEP["where"],
                                          "message": f"The docker backend cannot run on this machine: {why}. Choose local processes, or a host with Docker or Podman."}])

    def test_this_machines_docker_is_not_asked_for_a_host_or_for_local_processes(self):
        asked = []
        def unusable():
            asked.append(1)
            return "no docker"
        self.assertEqual(recipe.compile(a_recipe(backend="docker", where="me@bench"), defs(), docker_unusable=unusable)["errors"], [])
        self.assertEqual(recipe.compile(a_recipe(unsandboxed=True), defs(), docker_unusable=unusable)["errors"], [])
        self.assertEqual(asked, [])


class Proving(unittest.TestCase):
    def setUp(self):
        self.stack = patch.object(recipe, "_all", defs)
        self.stack.start(); self.addCleanup(self.stack.stop)

    def test_an_unjudged_benchmarks_proof_is_keyless_and_names_its_control_jobs(self):
        got = recipe.proving("fakebench", stamp=STAMP, limit=2)
        self.assertEqual(got["errors"], [])
        self.assertTrue(got["keyless"])
        self.assertIn('runpy.run_module("nullius.standard.cli"', got["script"])
        self.assertIn("exec nullius standard prove fakebench --limit 2 -j 4 --stamp " + STAMP + " --from-run", got["script"])
        self.assertNotIn("--provider", got["script"])
        self.assertEqual([j["name"] for j in got["jobs"]], [prove.job_name(STAMP, "fakebench", "all", c) for c in ("nop", "oracle")])

    def test_a_judged_benchmark_needs_its_judges_provider(self):
        d = defs()
        d["benchmark"][0]["score"] = dict(d["benchmark"][0]["score"], judge={"model": "gpt-4o", "says": "yes or no"})
        with patch.object(recipe, "_all", lambda: d):
            got = recipe.proving("fakebench", stamp=STAMP, provider="openai", judge_model="gpt-5-mini")
        self.assertFalse(got["keyless"])
        self.assertIn("--provider openai --judge-model gpt-5-mini", got["script"])

    def test_bad_choices_are_refused(self):
        self.assertTrue(recipe.proving("nope", stamp=STAMP)["errors"])
        got = recipe.proving("fakebench", stamp=STAMP, dataset="other", limit=0)
        self.assertEqual(len(got["errors"]), 2)
        self.assertTrue(recipe.proving("fakebench", stamp="today")["errors"])


class FailedProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-failed-proof-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        here = os.path.join(self.tmp, "fakebench")
        shutil.copytree(os.path.join(FIX, "benchmarks", "fakebench"), here)
        self.b, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", "fakebench")
        self.assertEqual(bad, [])
        self.b["controls"] = CONTROLS

    def test_a_failure_is_kept_with_the_value_it_reached_and_a_pass_clears_it(self):
        prove.record(self.b, {"all": {"oracle": {"ok": False, "passed": "3/4", "value": 0.75, "expect": {"at_least": 1.0}, "units": 4,
                                                 "job": "j", "on": "2026-01-02", "problems": ["pass rate 0.7500, expected at least 1.0"]}}})
        failed = prove.status(self.b)["all"]["oracle"]["failed"]
        self.assertEqual((failed["value"], failed["expect"], failed["problems"]), (0.75, {"at_least": 1.0}, ["pass rate 0.7500, expected at least 1.0"]))
        self.assertIsNone(prove.status(self.b)["all"]["oracle"]["proven"])
        self.assertEqual(definitions.problems(dict(self.b), "benchmark"), [])
        with patch.object(recipe, "_all", lambda: {"benchmark": [self.b], "agent": [], "capability": []}):
            page = recipe.choices()["benchmarks"][0]["datasets"][0]
        self.assertEqual(page["controls"][1]["failed"]["value"], 0.75)
        prove.record(self.b, {"all": {"oracle": {"ok": True, "passed": "4/4", "units": 4}}})
        self.assertNotIn("failed", self.b["proven"]["all"])
        self.assertEqual(prove.status(self.b)["all"]["oracle"]["proven"]["passed"], "4/4")


class AgentPage(unittest.TestCase):
    def test_each_proof_carries_its_kit_and_an_older_kit_says_so(self):
        a = dict(definitions.load("agent", FIX)[0][0])
        a["proven"] = dict(a.get("proven") or {}, **{"agent.tools/v1": {"passed": "2/2", "on": "2026-01-02", "model": "m"}})
        page = recipe._other_page(a)                                   # the tools kit is at v2
        self.assertEqual(page["proven"]["agent.tools/v1"]["kit"], 1)
        self.assertEqual(page["proven"]["agent.tools/v1"]["older"], "proven on kit v1; kit v2 is current")

    def test_every_port_has_a_name_a_person_reads(self):
        self.assertEqual(ports.PORTS["agent.tools/v1"]["label"], "MCP tools")
        self.assertTrue(all(s.get("label") for s in ports.PORTS.values()))


class Keyless(unittest.TestCase):
    def test_a_keyless_plan_is_handed_no_key_and_no_endpoint(self):
        env = {"OPENAI_API_KEY": "sk-secret-0123456789", "MODEL_API_URL": "http://x"}
        plan = execution.Plan(name="prove-x", script="true\n", runner="standard", key_var="OPENAI_API_KEY", url_var="", keyless=True)
        ex = execution.Execution(plan, root=tempfile.mkdtemp(prefix="nullius-keyless-"), env=env)
        self.addCleanup(shutil.rmtree, ex.root, True)
        self.assertNotIn("sk-secret", ex._stdin())
        self.assertEqual(ex._stdin().splitlines()[:2], ["", ""])
        self.assertTrue(execution.Plan.from_dict(plan.to_dict()).keyless)                # a restart keeps it keyless

    def test_a_plan_with_a_model_still_gets_its_key(self):
        plan = execution.Plan(name="run-x", script="true\n", runner="standard", key_var="OPENAI_API_KEY", url_var="")
        ex = execution.Execution(plan, root=tempfile.mkdtemp(prefix="nullius-keyed-"), env={"OPENAI_API_KEY": "sk-secret-0123456789"})
        self.addCleanup(shutil.rmtree, ex.root, True)
        self.assertIn("sk-secret-0123456789", ex._stdin())


if __name__ == "__main__":
    unittest.main()
