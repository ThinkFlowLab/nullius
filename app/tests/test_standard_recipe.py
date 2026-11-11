"""The standard in the builder: a recipe compiles to one command of this
repository's, never carrying a key; a pair that does not fit, an agent
without the explicit yes to running unsandboxed, a machine named twice, a missing model are
refused with the step that edits them; the conformance check names its jobs
ahead; and a run started by Nullius reads the key it was handed, not
app/.env."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard import cli, conform, definitions, recipe

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")


def defs(**extra):
    d = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
    for k, v in extra.items():
        d[k] = d[k] + v
    return d


def a_recipe(**kw):
    r = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
         "other": "fake", "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": True, "where": "local"}
    r.update(kw)
    return r


class Compile(unittest.TestCase):
    def test_a_fitting_pair_is_one_command_and_one_job(self):
        out = recipe.compile(a_recipe(), defs())
        self.assertEqual(out["errors"], [])
        self.assertIn("run fakebench fake --model gpt-5-mini --provider openai --dataset all --limit 2 -j 2 "
                      "--name fake-0101-120000 --unsandboxed --from-run", out["script"])
        self.assertIn('export PYTHONPATH="$nullius_app/app"', out["script"])
        self.assertIn("exec nullius standard run ", out["script"])
        self.assertEqual(out["jobs"], [{"name": "fake-0101-120000", "agent": "fake", "dataset": "fakebench"}])
        self.assertEqual([p["id"] for p in out["parts"]], ["fake"])
        for secret in ("sk-", "OPENAI_API_KEY=", "app/.env"):
            self.assertNotIn(secret, out["script"])
        self.assertEqual([row[1] for row in out["review"]], ["Benchmark", "Runner", "Dataset", "Agent", "Model", "Scope", "Where"])
        self.assertNotIn("--shard", out["script"])                                          # one machine: no shard
        self.assertEqual([(sh["where"], sh["name"]) for sh in out["shards"]], [("local", "fake-0101-120000")])

    def test_several_agents_are_one_run_a_job_each(self):
        twin = dict(definitions.load("agent", ROOT)[0][0], key="twin", label="Twin agent")
        out = recipe.compile(a_recipe(others=["fake", "twin"], options={"twin": {}}), defs(agent=[twin]))
        self.assertEqual(out["errors"], [])
        self.assertEqual([j["name"] for j in out["jobs"]], ["fake-0101-120000-fake", "fake-0101-120000-twin"])
        self.assertEqual([p["id"] for p in out["parts"]], ["fake", "twin"])                 # one part each, one after the other
        self.assertEqual([r[1] for r in out["review"]].count("Agent"), 2)
        self.assertFalse(out["keyless"])                                                    # an agent calls a model
        self.assertEqual(recipe.compile(a_recipe(others=["fake", "fake"]), defs())["jobs"][0]["name"], "fake-0101-120000")   # once each
        self.assertEqual(recipe.compile(a_recipe(others=[], other=""), defs())["errors"][0]["step"], recipe.STEP["agent"])   # nothing chosen: who runs it

    def test_several_over_several_machines_keep_a_launch_per_agent(self):
        twin = dict(definitions.load("agent", ROOT)[0][0], key="twin", label="Twin agent")
        out = recipe.compile(a_recipe(others=["fake", "twin"], where=["local", "me@bench"]), defs(agent=[twin]))
        self.assertEqual(out["errors"], [])
        self.assertEqual([[j["name"] for j in sh["jobs"]] for sh in out["shards"]],
                         [["fake-0101-120000-fake-shard-1of2", "fake-0101-120000-twin-shard-1of2"],
                          ["fake-0101-120000-fake-shard-2of2", "fake-0101-120000-twin-shard-2of2"]])
        self.assertIn("--launch fake-0101-120000-twin", out["shards"][1]["parts"][1]["script"])   # cli merge puts each agent's shards together

    def test_several_datasets_with_several_agents_are_a_job_each_pair(self):
        twin = dict(definitions.load("agent", ROOT)[0][0], key="twin", label="Twin agent")
        two = dict(definitions.find(definitions.load("benchmark", ROOT)[0], "fakebench"))
        two["datasets"] = two["datasets"] + [dict(two["datasets"][0], id="more", label="More")]
        d = defs(agent=[twin]); d["benchmark"] = [two if x["key"] == "fakebench" else x for x in d["benchmark"]]
        out = recipe.compile(a_recipe(datasets=["all", "more"], others=["fake", "twin"]), d)
        self.assertEqual(out["errors"], [])
        self.assertEqual([j["name"] for j in out["jobs"]], ["fake-0101-120000-all-fake", "fake-0101-120000-all-twin",
                                                            "fake-0101-120000-more-fake", "fake-0101-120000-more-twin"])
        self.assertIn("--dataset more", out["parts"][2]["script"])
        self.assertEqual(out["review"][2][1], "Datasets")
        one = recipe.compile(a_recipe(datasets=["more"]), d)
        self.assertEqual([j["name"] for j in one["jobs"]], ["fake-0101-120000"])            # one pair: the recipe's name alone
        bad = recipe.compile(a_recipe(datasets=["all", "nope"]), d)
        self.assertEqual(bad["errors"][0]["step"], recipe.STEP["dataset"])

    def test_a_recipe_from_before_with_one_other_and_flat_options_still_reads(self):
        self.assertEqual(recipe.chosen_of({"other": "fake"}), ["fake"])
        self.assertEqual(recipe.options_for({"options": {"effort": "high"}}, "fake", False), {"effort": "high"})
        self.assertEqual(recipe.options_for({"options": {"effort": "high"}}, "fake", True), {})
        self.assertEqual(recipe.options_for({"options": {"fake": {"effort": "low"}}}, "fake", True), {"effort": "low"})

    def test_a_pair_that_does_not_fit_is_refused_with_the_reasons(self):
        half = dict(definitions.load("agent", ROOT)[0][0], key="half", label="Half agent", offers=["agent.task/v1"],
                    proven={"agent.task/v1": {"passed": "3/3"}})
        out = recipe.compile(a_recipe(other="half"), defs(agent=[half]))
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["agent"]])
        self.assertIn("needs agent.workspace/v1", out["errors"][0]["message"])
        self.assertEqual(out["script"], "")

    def test_an_agent_waits_for_the_yes_and_a_feature_does_not_ask(self):
        out = recipe.compile(a_recipe(unsandboxed=False), defs())
        self.assertIn("not sandboxed", out["errors"][0]["message"])
        mem = recipe.compile(a_recipe(benchmark="fakememo", other="fakemem", unsandboxed=False), defs())
        self.assertEqual(mem["errors"], [])
        self.assertNotIn("--unsandboxed", mem["script"])
        self.assertEqual(mem["review"][3][1], "Capability")

    def test_an_agent_benchmark_does_not_take_a_feature(self):
        out = recipe.compile(a_recipe(other="fakemem"), defs())
        self.assertIn("Choose an agent", out["errors"][0]["message"])

    def test_each_problem_names_its_step(self):
        out = recipe.compile(a_recipe(model="", provider="nowhere", where=["local", "local"], limit=0, concurrency=99, name="no spaces"), defs())
        steps = {e["step"] for e in out["errors"]}
        self.assertEqual(steps, {recipe.STEP["model"], recipe.STEP["where"], recipe.STEP["limits"]})
        self.assertEqual(recipe.compile(a_recipe(benchmark="nope"), defs())["errors"][0]["step"], recipe.STEP["benchmark"])

    def test_no_limit_is_every_unit_and_asked_first(self):
        out = recipe.compile(a_recipe(limit=""), defs())
        self.assertNotIn("--limit", out["script"])
        self.assertEqual(out["confirm"], "Run all 4 tasks of Fake bench with Fake agent?")

    def test_no_concurrency_is_one_at_a_time(self):
        out = recipe.compile(a_recipe(concurrency=""), defs())
        self.assertEqual(out["errors"], [])
        self.assertIn("-j 1", out["script"])
        self.assertIn("1 at a time", [row[2] for row in out["review"] if row[1] == "Scope"][0])

    def test_a_value_cannot_become_a_shell_word(self):
        out = recipe.compile(a_recipe(model="gpt;rm -rf ~"), defs())
        self.assertEqual(out["script"], "")
        self.assertEqual(out["errors"][0]["step"], recipe.STEP["model"])


class SpendingCap(unittest.TestCase):
    """*Limits*: one cap for the whole run (``max_usd``), shared in equal parts by its jobs that call a model."""

    def twin(self):
        return dict(definitions.load("agent", ROOT)[0][0], key="twin", label="Twin agent")

    def caps(self, out):
        return [c.split("--max-usd ")[1].split()[0] if "--max-usd " in c else None for sh in out["shards"] for c in sh["command"].split("\n")]

    def test_one_job_takes_the_whole_cap_and_no_cap_adds_nothing(self):
        out = recipe.compile(a_recipe(max_usd=2), defs())
        self.assertEqual(out["errors"], [])
        self.assertIn("-j 2 --max-usd 2 --name fake-0101-120000", out["script"])
        self.assertIn([recipe.STEP["limits"], "Spending cap", "$2.00"], out["review"])
        for none in ({}, {"max_usd": ""}, {"max_usd": None}):
            plain = recipe.compile(a_recipe(**none), defs())
            self.assertEqual((plain["errors"], self.caps(plain)), ([], [None]))
            self.assertNotIn("Spending cap", [row[1] for row in plain["review"]])

    def test_the_jobs_of_a_run_share_the_cap_in_equal_parts_and_never_pass_it(self):
        out = recipe.compile(a_recipe(others=["fake", "twin"], where=["local", "me@bench"], max_usd=1), defs(agent=[self.twin()]))
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.caps(out), ["0.25"] * 4)                                      # two agents on two machines: four jobs
        self.assertIn([recipe.STEP["limits"], "Spending cap", "$1.00 in all · $0.25 for each of 4 jobs"], out["review"])
        three = recipe.compile(a_recipe(where=["local", "me@bench", "gpu-box"], max_usd=2), defs())
        self.assertEqual(self.caps(three), ["0.666666"] * 3)                                # rounded down: the shares never add up to more

    def test_a_job_that_calls_no_model_takes_no_share(self):
        bench = dict(definitions.find(definitions.load("benchmark", ROOT)[0], "fakebench"), key="checked",
                     controls={"nop": {"source": "nullius", "from": "an empty answer"}})
        out = recipe.compile(a_recipe(benchmark="checked", others=["fake", "nop"], max_usd=2), defs(benchmark=[bench]))
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.caps(out), ["2", None])                                       # the control spends nothing
        self.assertIn([recipe.STEP["limits"], "Spending cap", "$2.00"], out["review"])
        alone = recipe.compile(a_recipe(benchmark="checked", others=["nop"], max_usd=2), defs(benchmark=[bench]))
        self.assertEqual((alone["errors"], self.caps(alone)), ([], [None]))
        self.assertNotIn("Spending cap", [row[1] for row in alone["review"]])

    def test_a_cap_per_unit_goes_whole_to_every_job_that_calls_a_model(self):
        out = recipe.compile(a_recipe(others=["fake", "twin"], where=["local", "me@bench"], max_usd=1, max_usd_per_unit=0.05), defs(agent=[self.twin()]))
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.caps(out), ["0.25"] * 4)                                      # the run's is shared
        each = [c.split("--max-usd-per-unit ")[1].split()[0] for sh in out["shards"] for c in sh["command"].split("\n")]
        self.assertEqual(each, ["0.05"] * 4)                                                # a unit's is not: a unit is a unit wherever it runs
        self.assertIn([recipe.STEP["limits"], "Spending cap per task", "$0.05"], out["review"])
        alone = recipe.compile(a_recipe(max_usd_per_unit=0.05), defs(), unpriced=lambda models: list(models))
        self.assertEqual([e["step"] for e in alone["errors"]], [recipe.STEP["limits"]])    # alone it needs prices too
        self.assertIn("gpt-5-mini", alone["errors"][0]["message"])
        plain = recipe.compile(a_recipe(max_usd_per_unit=0.05), defs())
        self.assertEqual((plain["errors"], self.caps(plain)), ([], [None]))
        self.assertIn("-j 2 --max-usd-per-unit 0.05 --name", plain["script"])
        self.assertNotIn("Spending cap", [row[1] for row in plain["review"]])
        tiny = recipe.compile(a_recipe(max_usd_per_unit=1e-7), defs())["errors"]          # less than the command line can say
        self.assertEqual([e["step"] for e in tiny], [recipe.STEP["limits"]])
        self.assertIn("per task is too small", tiny[0]["message"])
        bad = recipe.compile(a_recipe(max_usd_per_unit=0), defs())["errors"]
        self.assertEqual([e["step"] for e in bad], [recipe.STEP["limits"]])
        self.assertIn("per task", bad[0]["message"])                                        # which cap it is about

    def test_the_price_question_names_every_model_the_run_will_call(self):
        """As ``run.Run.models_called`` lists them: an agent's side models too, each as it is sent."""
        base = self.twin()
        memo = dict(base, model={**(base.get("model") or {}), "side_models": ["text-embedding-3-small", "fast=gpt-5-nano"]})
        asked = []
        out = recipe.compile(a_recipe(others=["twin"], max_usd=1), defs(agent=[memo]), unpriced=lambda models: asked.append(list(models)) or [])
        self.assertEqual((out["errors"], asked), ([], [["gpt-5-mini", "text-embedding-3-small", "gpt-5-nano"]]))

    def test_a_cap_that_is_no_amount_names_limits(self):
        for bad in (0, -1, "2", True, float("inf"), float("nan"), [2]):
            with self.subTest(bad):
                errors = recipe.compile(a_recipe(max_usd=bad), defs())["errors"]
                self.assertEqual([e["step"] for e in errors], [recipe.STEP["limits"]])
        small = recipe.compile(a_recipe(where=["local", "me@bench"], max_usd=1e-6), defs())["errors"]
        self.assertEqual([e["step"] for e in small], [recipe.STEP["limits"]])
        self.assertIn("2 jobs", small[0]["message"])

    def test_a_cap_without_a_price_is_said_at_limits_and_only_a_cap_asks(self):
        asked = []
        out = recipe.compile(a_recipe(max_usd=2), defs(), unpriced=lambda models: asked.append(list(models)) or list(models))
        self.assertEqual(asked, [["gpt-5-mini"]])                                           # the model as the run sends it
        self.assertEqual([(e["step"], e["message"]) for e in out["errors"]],
                         [(recipe.STEP["limits"], "A spending cap needs a price for gpt-5-mini: update the price list in Settings, or set a price there.")])
        self.assertEqual(recipe.compile(a_recipe(max_usd=2), defs(), unpriced=lambda models: [])["errors"], [])
        never = lambda models: self.fail("a recipe without a cap asks for no price")
        self.assertEqual(recipe.compile(a_recipe(), defs(), unpriced=never)["errors"], [])


class ExecutionScripts(unittest.TestCase):
    """Execution scripts dispatch only to stubs, with no installed package
    visible to the shell's python3 and no developer settings or credentials."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nullius-standard-recipe-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = self.root / "working directory 'quote'"
        self.cwd.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                    "HOME": str(self.root / "home"), "NULLIUS_HOME": str(self.root / "storage"),
                    "NULLIUS_TEST_LOG": str(self.log), "OPENAI_API_KEY": "fixture-token",
                    "OPENAI_BASE_URL": "https://fixture.invalid/v1"}
        # An absolute interpreter prevents the python3 stub recursing. Neither
        # stub imports Nullius or starts a benchmark, viewer or network call.
        stub = f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
with open(os.environ["NULLIUS_TEST_LOG"], "a") as handle:
    handle.write(json.dumps({"command": pathlib.Path(__file__).name,
        "args": sys.argv[1:], "cwd": os.getcwd(),
        "env": {k: os.environ[k] for k in ("PYTHONPATH", "NULLIUS_CATALOGUE",
                "OPENAI_API_KEY", "OPENAI_BASE_URL") if k in os.environ}}) + "\\n")
sys.exit(int(os.environ.get("NULLIUS_TEST_EXIT", "0")))
'''
        for name in ("nullius", "python3"):
            path = self.bin / name
            path.write_text(stub)
            path.chmod(0o700)

    def scripts(self):
        fake = definitions.load("agent", ROOT)[0][0]
        with patch.object(recipe, "_all", defs), patch.object(definitions, "get", return_value=fake):
            return {"run": recipe.compile(a_recipe(), defs())["script"],
                    "conform": recipe.conformance("agent", "fake", model="m", provider="openai",
                                                  unsandboxed=True, stamp="2026-10-10__12-00-00")["script"],
                    "prove": recipe.proving("fakebench", stamp="2026-10-10__12-00-00")["script"]}

    def run_script(self, text, *args, env=None):
        path = self.root / "execution.sh"
        path.write_text(text)
        return subprocess.run(["bash", str(path), *args], cwd=self.cwd, env=env or self.env,
                              capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def source_tree(self):
        root = self.cwd / "source 'quote'\n$(touch injected)"
        path = root / "app/nullius/standard/cli.py"
        path.parent.mkdir(parents=True)
        path.write_text("# source/payload fixture; never executed\n")
        return root

    def test_installed_run_conform_and_prove_need_no_checkout_or_system_python_import(self):
        for kind, script in self.scripts().items():
            with self.subTest(kind=kind):
                result = self.run_script(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.calls()[-1]
                self.assertEqual(call["command"], "nullius")
                self.assertEqual(call["args"][:2], ["standard", kind])
                self.assertIn("--from-run", call["args"])
                self.assertEqual(call["args"][-2:], ["--jobs", "jobs_standard"])
                self.assertEqual(call["cwd"], str(self.cwd))
                self.assertNotIn("PYTHONPATH", call["env"])
                self.assertEqual(call["env"]["OPENAI_API_KEY"], "fixture-token")
                self.assertEqual(call["env"]["OPENAI_BASE_URL"], self.env["OPENAI_BASE_URL"])
                self.assertNotIn("fixture-token", script + result.stdout + result.stderr)
                self.assertNotIn(str(Path(__file__).resolve().parents[2]), script)
        self.assertEqual(len(self.calls()), 3)

    def test_explicit_source_and_execution_payload_override_the_installed_command(self):
        source = self.source_tree()
        for kind, script in self.scripts().items():
            for mode in ("app", "payload"):
                with self.subTest(kind=kind, mode=mode):
                    env = dict(self.env, NULLIUS_CATALOGUE=str(source / "app/nullius/catalogue"),
                               NULLIUS_JOBS="relative jobs 'quote'\n$(touch injected)")
                    args = ["--app", source.name] if mode == "app" else []
                    if mode == "payload":
                        env["NULLIUS_STANDARD"] = str(source)
                    result = self.run_script(script, *args, env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    call = self.calls()[-1]
                    self.assertEqual(call["command"], "python3")
                    self.assertEqual(call["args"][:4], ["-I", "-S", "-B", "-c"])
                    self.assertIn('runpy.run_module("nullius.standard.cli"', call["args"][4])
                    self.assertEqual(call["args"][5:7], [str(source / "app"), kind])
                    self.assertEqual(call["args"][-2:], ["--jobs", env["NULLIUS_JOBS"]])
                    self.assertEqual(call["env"]["PYTHONPATH"], str(source / "app"))
                    self.assertEqual(call["env"]["NULLIUS_CATALOGUE"], env["NULLIUS_CATALOGUE"])
                    self.assertEqual(call["cwd"], str(self.cwd))
                    self.assertFalse((self.cwd / "injected").exists())

    def test_installed_absolute_jobs_and_failure_status_are_preserved(self):
        jobs = str(self.root / "absolute jobs 'quote'")
        result = self.run_script(self.scripts()["run"],
                                 env=dict(self.env, NULLIUS_JOBS=jobs, NULLIUS_TEST_EXIT="23"))
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.calls()[0]["args"][-2:], ["--jobs", jobs])

    def test_execution_can_select_its_python_without_changing_installed_dispatch(self):
        source = self.source_tree()
        python = self.bin / "runtime python 'quote'"
        python.write_text((self.bin / "python3").read_text())
        python.chmod(0o700)
        env = dict(self.env, NULLIUS_STANDARD=str(source), NULLIUS_PYTHON=str(python))
        result = self.run_script(self.scripts()["run"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["command"], python.name)
        del env["NULLIUS_STANDARD"]
        result = self.run_script(self.scripts()["run"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["command"], "nullius")

    def test_invalid_explicit_source_never_falls_back_to_an_installed_command(self):
        for args in (("--app", "missing"), ("--app", ""), ("--app", "."), ("--app",)):
            with self.subTest(args=args):
                result = self.run_script(self.scripts()["run"], *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Nullius:", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_help_needs_no_application_or_credentials(self):
        result = self.run_script(self.scripts()["run"], "--help",
                                 env={"PATH": os.defpath, "HOME": self.env["HOME"],
                                      "NULLIUS_HOME": self.env["NULLIUS_HOME"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--app", result.stdout)
        self.assertEqual(self.calls(), [])


class Choices(unittest.TestCase):
    def test_the_page_sees_choices_and_reasons_never_a_command(self):
        with patch.object(recipe, "_all", lambda: defs()):
            c = recipe.choices()
        self.assertEqual([b["key"] for b in c["benchmarks"]], ["fakebench", "fakememo"])
        self.assertEqual(c["fits"], {"fakebench": {"fake": []}, "fakememo": {"fakemem": []}})
        self.assertEqual(c["benchmarks"][1]["kind"], "memory")
        self.assertNotIn("command", repr(c))
        self.assertNotIn("acp", repr(c["agents"]).replace("ACP", ""))

    def test_an_example_is_never_offered(self):
        # the example memory fits, listed first, and is still neither listed nor picked
        mem = definitions.load("capability", ROOT)[0][0]
        example = dict(mem, key="example-mem", _ref="example-mem", example=True)
        d = defs()
        d["capability"] = [example] + d["capability"]
        with patch.object(recipe, "_all", lambda: d):
            c = recipe.choices()
        self.assertNotIn("example-mem", [o["key"] for o in c["capabilities"]])


class Conformance(unittest.TestCase):
    def test_the_check_names_its_jobs_before_they_exist(self):
        fake = definitions.load("agent", ROOT)[0][0]
        with patch.object(definitions, "get", lambda kind, key, root=None: fake if key == "fake" else None):
            got = recipe.conformance("agent", "fake", model="gpt-5-mini", provider="openai", unsandboxed=True, stamp="2026-10-10__12-00-00")
        self.assertEqual(got["errors"], [])
        self.assertEqual([j["name"] for j in got["jobs"]],
                         [conform.job_name("2026-10-10__12-00-00", k, "fake") for k in ("agent-task-v1", "agent-workspace-v1", "agent-tools-v1", "agent-conversation-v1")])   # an ACP agent takes every kit
        self.assertIn("conform fake --model gpt-5-mini --provider openai --stamp 2026-10-10__12-00-00 --unsandboxed --from-run", got["script"])

    def test_docker_conformance_does_not_grant_local_permissions(self):
        for kind in ("agent", "capability"):
            definition = definitions.load(kind, ROOT)[0][0]
            with patch.object(definitions, "get", return_value=definition):
                got = recipe.conformance(kind, definition["key"], model="m", provider="openai", unsandboxed=False,
                                         stamp="2026-10-10__12-00-00", backend="docker")
                self.assertEqual(got["errors"], [])
                self.assertIn("--backend docker", got["script"])
                self.assertNotIn("--unsandboxed", got["script"])
                bad = recipe.conformance(kind, definition["key"], model="m", provider="openai", unsandboxed=True,
                                         stamp="2026-10-10__12-00-00", backend="unknown")
                self.assertTrue(bad["errors"])
                self.assertEqual(bad["script"], "")

    def test_an_agent_check_waits_for_the_yes(self):
        fake = definitions.load("agent", ROOT)[0][0]
        with patch.object(definitions, "get", lambda kind, key, root=None: fake):
            got = recipe.conformance("agent", "fake", model="m", provider="openai", unsandboxed=False, stamp="2026-10-10__12-00-00")
        self.assertIn("not a sandbox", got["errors"][0])


class FromRun(unittest.TestCase):
    def test_the_handed_key_is_named_as_the_providers_own_and_nothing_else_is_kept(self):
        env = {"OPENAI_API_KEY": "sk-handed", "OPENAI_BASE_URL": "http://127.0.0.1:9/v1", "PATH": "/bin", "OPENROUTER_API_KEY": "sk-other"}
        self.assertEqual(cli.run_env("openrouter", env), {"OPENROUTER_API_KEY": "sk-handed", "PATH": "/bin"})
        self.assertEqual(cli.run_env("custom", env), {"MODEL_API_KEY": "sk-handed", "MODEL_API_URL": "http://127.0.0.1:9/v1", "PATH": "/bin"})
        self.assertEqual(cli.run_env("openai", env), {"OPENAI_API_KEY": "sk-handed", "PATH": "/bin"})

    def test_a_run_started_by_nullius_does_not_read_app_env(self):
        loaded = []
        with patch.object(cli.settings, "load", lambda path: loaded.append(path)), patch.object(cli, "cmd_run", lambda a: 0):
            cli.main(["run", "fakebench", "fake", "--model", "m", "--from-run"])
            self.assertEqual(loaded, [])
            cli.main(["run", "fakebench", "fake", "--model", "m"])
            self.assertEqual(loaded, [cli.ENV_FILE])


class Where(unittest.TestCase):
    """*Where*: one machine is one run; several split the units, one shard
    per machine, each its own run and job under one launch - the recipe's
    name."""

    def test_a_host_is_one_run_there(self):
        out = recipe.compile(a_recipe(where="me@bench"), defs())
        self.assertEqual(out["errors"], [])
        (sh,) = out["shards"]
        self.assertEqual((sh["where"], sh["index"], sh["of"], sh["name"]), ("me@bench", 1, 1, "fake-0101-120000"))
        self.assertIn('nullius_app=${NULLIUS_STANDARD:-}', sh["script"])                 # execution's explicit source or payload
        self.assertNotIn("--shard", sh["script"])
        self.assertIn("as you on me@bench", " ".join(out["notes"]))

    def test_several_machines_are_one_shard_each_under_one_launch(self):
        out = recipe.compile(a_recipe(where=["local", "me@bench", "gpu-box"]), defs())
        self.assertEqual(out["errors"], [])
        self.assertEqual([(sh["where"], sh["name"]) for sh in out["shards"]],
                         [("local", "fake-0101-120000-shard-1of3"), ("me@bench", "fake-0101-120000-shard-2of3"),
                          ("gpu-box", "fake-0101-120000-shard-3of3")])
        for i, sh in enumerate(out["shards"], 1):
            self.assertIn(f"--name fake-0101-120000-shard-{i}of3", sh["script"])
            self.assertIn(f"--shard {i}/3 --launch fake-0101-120000 --from-run", sh["script"])
            self.assertEqual(sh["jobs"], [{"name": sh["name"], "agent": "fake", "dataset": "fakebench"}])
        self.assertEqual([j["name"] for j in out["jobs"]], [sh["name"] for sh in out["shards"]])
        self.assertEqual(len(out["parts"]), 3)
        self.assertIn("3 shards, one per machine", out["review"][-1][2])
        self.assertIn("of each shard", out["review"][-2][2])                                 # a shard's limit is its own

    def test_the_docker_backend_needs_no_yes_and_says_it_needs_docker(self):
        out = recipe.compile(a_recipe(where="me@bench", backend="docker", unsandboxed=False), defs())
        self.assertEqual(out["errors"], [])
        self.assertTrue(out["docker"])
        self.assertIn("--backend docker", out["script"]); self.assertNotIn("--unsandboxed", out["script"])
        self.assertFalse(recipe.compile(a_recipe(), defs())["docker"])
        self.assertEqual(recipe.compile(a_recipe(backend="vm"), defs())["errors"][0]["step"], recipe.STEP["where"])

    def test_a_machine_that_is_not_one_is_refused(self):
        for where in ([], ["local", "local"], "not a host!"):
            out = recipe.compile(a_recipe(where=where), defs())
            self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["where"]], where)


if __name__ == "__main__":
    unittest.main()
