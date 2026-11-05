"""An agent's options: what a run may set of the agent's own behaviour, each
defaulting to the vendor's own. Checked in the definition, filled into its
templates as ``{opt_<name>}``, taken by the cli and the builder, and recorded
in the job's config so two runs say what differed."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from nullius.standard import cli, conform, definitions, install, recipe, run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard")
ROOT = os.path.join(FIX, "catalogue")
EFFORT = {"summary": "How hard the model reasons; the vendor's default.", "values": ["low", "medium", "high"], "default": "medium"}


def agent(**kw):
    d = {"schema": "nullius.agent/v1", "key": "bot", "label": "Bot", "summary": "A bot.",
         "upstream": {"package": "bot-acp", "version": "1.0.0", "license": "MIT"},
         "install": {"npm": ["bot-acp@1.0.0"]},
         "start": {"acp": ["{install}/node_modules/.bin/bot-acp"]},
         "model": {"apis": ["chat"], "env": {"OPENAI_BASE_URL": "{base_url}", "BOT_EFFORT": "{opt_effort}"}},
         "options": {"effort": dict(EFFORT)}}
    d.update(kw)
    return d


class Problems(unittest.TestCase):
    def test_an_option_with_its_vendor_default_is_fine(self):
        self.assertEqual(definitions.problems(agent(), "agent"), [])

    def test_a_default_it_does_not_allow_is_refused(self):
        bad = definitions.problems(agent(options={"effort": dict(EFFORT, default="max")}), "agent")
        self.assertEqual(bad, ["options.effort.default: the vendor's own default, a value it allows"])

    def test_values_or_a_pattern_and_plain_words_only(self):
        both = definitions.problems(agent(options={"effort": dict(EFFORT, pattern="^[a-z]+$")}), "agent")
        self.assertIn("options.effort: values or pattern, one of them", both)
        quote = definitions.problems(agent(options={"effort": dict(EFFORT, values=["low", 'x"; rm', "medium"])}), "agent")
        self.assertIn("options.effort.values: distinct plain words", quote)
        pattern = {"summary": "s", "pattern": "^[0-9]{1,3}$", "default": "40"}
        self.assertEqual(definitions.problems(agent(options={"effort": pattern}), "agent"), [])
        self.assertTrue(definitions.problems(agent(options={"effort": dict(pattern, default="x")}), "agent"))

    def test_a_name_and_a_summary(self):
        bad = definitions.problems(agent(options={"Effort": dict(EFFORT), "effort": dict(EFFORT, summary="")}), "agent")
        self.assertIn("options: 'Effort' is not a name (lower case letters and _)", bad)
        self.assertIn("options.effort.summary: say what it changes, and where its default comes from", bad)

    def test_every_slot_is_an_option(self):
        d = agent(options={}, env={"BOT_MODE": "{opt_mode}"})
        bad = definitions.problems(d, "agent")
        self.assertIn("model.env.BOT_EFFORT: {opt_effort} is not an option (options.effort)", bad)
        self.assertIn("env.BOT_MODE: {opt_mode} is not an option (options.mode)", bad)

    def test_a_slot_in_a_config_file_is_checked_too(self):
        here = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, here)
        with open(os.path.join(here, "conf.toml"), "w") as f:
            f.write('effort = "{opt_effort}"\nspeed = "{opt_speed}"\n')
        d = agent(_here=here, model={"apis": ["chat"], "files": {"{home}/.bot/conf.toml": "conf.toml"}})
        self.assertEqual(definitions.problems(d, "agent"), ["model.files: conf.toml: {opt_speed} is not an option (options.speed)"])

    def test_a_version_adds_its_own_options_to_the_shared_ones(self):
        d = agent(default="2.0.0", versions={"2.0.0": {"install": {"npm": ["bot-acp@2.0.0"]},
                                                       "options": {"speed": {"summary": "s", "values": ["a", "b"], "default": "a"}}},
                                             "1.0.0": {"install": {"npm": ["bot-acp@1.0.0"]}}})
        del d["install"]
        d["upstream"] = {"license": "MIT"}
        self.assertEqual(definitions.problems(d, "agent"), [])
        self.assertEqual(sorted(definitions.resolve(d, "2.0.0")["options"]), ["effort", "speed"])
        self.assertEqual(sorted(definitions.resolve(d, "1.0.0")["options"]), ["effort"])

    def test_a_fidelity_command_carries_the_prompt(self):
        ok = agent(fidelity={"command": ["{install}/node_modules/.bin/bot", "exec", "--", "{prompt}"]})
        self.assertEqual(definitions.problems(ok, "agent"), [])
        bad = definitions.problems(agent(fidelity={"command": ["bot", "exec"]}), "agent")
        self.assertIn("fidelity.command: carries {prompt}", bad)


class Values(unittest.TestCase):
    def test_the_rest_keep_their_defaults(self):
        self.assertEqual(definitions.options_of(agent()), ({"effort": "medium"}, []))
        self.assertEqual(definitions.options_of(agent(), {"effort": "high"}), ({"effort": "high"}, []))

    def test_an_unknown_name_or_value_is_said(self):
        _, bad = definitions.options_of(agent(), {"effort": "max", "speed": "x"})
        self.assertEqual(bad, ["Bot has no option 'speed' (its options: effort)", "option effort: 'max' is not one of low, medium, high"])

    def test_the_slots_are_filled_in_variables_files_and_its_own_env(self):
        here, home = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, here)
        self.addCleanup(shutil.rmtree, home)
        with open(os.path.join(here, "conf.toml"), "w") as f:
            f.write('model = "{model}"\neffort = "{opt_effort}"\n')
        d = agent(_here=here, env={"BOT_OWN": "e-{opt_effort}"},
                  model={"apis": ["chat"], "env": {"BOT_EFFORT": "{opt_effort}"}, "files": {"{home}/.bot/conf.toml": "conf.toml"}})
        env = run.agent_env(d, here, home, "http://relay/v1", "m-1", {"effort": "high"})
        self.assertEqual((env["BOT_EFFORT"], env["BOT_OWN"]), ("high", "e-high"))
        with open(os.path.join(home, ".bot", "conf.toml")) as f:
            self.assertEqual(f.read(), 'model = "m-1"\neffort = "high"\n')
        env = run.agent_env(d, here, home, "http://relay/v1", "m-1")
        self.assertEqual(env["BOT_EFFORT"], "medium")                  # nothing set: the vendor's default, written out


class InARun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self):
        return dict(definitions.get("agent", "fake", ROOT), options={"effort": dict(EFFORT)}, env={"FAKE_EFFORT": "{opt_effort}"})

    def test_the_jobs_config_records_every_option_defaults_included(self):
        bench = definitions.get("benchmark", "fakebench", ROOT)
        job = run.run(bench, self.fake(), model="m-1", provider="custom", jobs_root=self.tmp, limit=1, concurrency=1,
                      env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, say=lambda s: None, unsandboxed=True)
        with open(os.path.join(job, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["options"], {"effort": "medium"})
        job = run.run(bench, self.fake(), model="m-1", provider="custom", jobs_root=self.tmp, limit=1, concurrency=1, name="hi",
                      env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, say=lambda s: None, unsandboxed=True, options={"effort": "high"})
        with open(os.path.join(job, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["options"], {"effort": "high"})

    def test_a_value_it_does_not_allow_is_refused_before_anything_starts(self):
        bench = definitions.get("benchmark", "fakebench", ROOT)
        with self.assertRaises(run.Refused) as e:
            run.Run(bench, self.fake(), model="m-1", provider="custom", jobs_root=self.tmp, env={"MODEL_API_URL": "http://x/v1"},
                    unsandboxed=True, options={"effort": "max"})
        self.assertIn("'max' is not one of low, medium, high", str(e.exception))

    def test_the_cli_reads_name_equals_value(self):
        self.assertEqual(cli.options(["effort=high", "speed = a"]), {"effort": "high", "speed": "a"})
        with self.assertRaises(SystemExit):
            cli.options(["effort"])


class Conform(unittest.TestCase):
    def test_a_conformance_run_keeps_the_fidelity_proof(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = os.path.join(tmp, "catalogue")
        shutil.copytree(ROOT, root)
        path = os.path.join(root, "capabilities", "fakemem", "capability.json")
        with open(path) as f:
            d = json.load(f)
        d["proven"]["fidelity"] = {"verdict": "same", "on": "2026-10-12"}
        with open(path, "w") as f:
            json.dump(d, f)
        home, install.HOME = install.HOME, os.path.join(tmp, "home")
        try:
            conform.check(definitions.get("capability", "fakemem", root), model="m", provider="custom", which=["cap.memory/v1"],
                          jobs_root=os.path.join(tmp, "jobs"), say=lambda s: None, env={"MODEL_API_URL": "http://127.0.0.1:9/v1"})
        finally:
            install.HOME = home
        self.assertEqual(definitions.get("capability", "fakemem", root)["proven"]["fidelity"]["verdict"], "same")


class Builder(unittest.TestCase):
    def defs(self):
        d = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
        d["agent"] = [dict(a, options={"effort": dict(EFFORT)}) for a in d["agent"]]
        return d

    def recipe(self, **kw):
        return dict({"version": 1, "harness": "standard", "name": "n", "benchmark": "fakebench", "dataset": "all", "other": "fake",
                     "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": True,
                     "where": "local"}, **kw)

    def test_the_page_is_offered_each_option(self):
        page = recipe._other_page(self.defs()["agent"][0])
        self.assertEqual(page["options"], {"effort": {"summary": EFFORT["summary"], "default": "medium", "values": ["low", "medium", "high"]}})

    def test_a_chosen_option_reaches_the_command_and_the_review(self):
        out = recipe.compile(self.recipe(options={"effort": "high"}), self.defs())
        self.assertEqual(out["errors"], [])
        self.assertIn("--option effort=high", out["script"])
        self.assertIn(["Options", "effort high"], [r[1:] for r in out["review"]])
        out = recipe.compile(self.recipe(), self.defs())
        self.assertNotIn("--option", out["script"])
        self.assertIn(["Options", "effort medium (its default)"], [r[1:] for r in out["review"]])

    def test_a_value_it_does_not_allow_is_the_agent_steps_error(self):
        out = recipe.compile(self.recipe(options={"effort": "x; rm -rf"}), self.defs())
        self.assertEqual([e["step"] for e in out["errors"]], [recipe.STEP["agent"]])
        self.assertEqual(out["script"], "")


if __name__ == "__main__":
    unittest.main()
