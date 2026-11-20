"""A benchmark's own agents (its agents.json): agents of the catalogue that start with start.driver, bound to
their benchmark by where they sit, and the benchmark's agent option rebuilt from them."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest

from nullius.runs import parse
from nullius.standard import definitions, job, match, recipe

BENCHES = os.path.join(definitions.CATALOGUE, "benchmarks")


def bench(key="osworld1"):
    return definitions.get("benchmark", key, root=definitions.CATALOGUE)


class Loaded(unittest.TestCase):
    def test_each_entry_is_an_agent_named_by_its_benchmark_and_bound_to_it(self):
        b = bench()
        own = definitions.own_agents(b)
        self.assertEqual(len(own), 12)
        cua = definitions.own_agent(b, "osworld1:openai-cua")
        self.assertEqual((cua["key"], cua["from"], cua["only_for"], cua["benchmark"]), ("openai-cua", "benchmark", ["osworld1"], "osworld1"))
        self.assertEqual(cua["start"]["driver"]["agent"], "openai_cua")
        self.assertEqual(definitions.problems(cua, "agent"), [])
        with open(os.path.join(BENCHES, "osworld1", "agents.json")) as f:
            self.assertTrue(all("from" not in e and "only_for" not in e for e in json.load(f)["agents"].values()))

    def test_the_agent_option_is_rebuilt_from_the_file(self):
        with open(os.path.join(BENCHES, "osworld1", "standard.json")) as f:
            written = json.load(f)["options"]["agent"]
        self.assertEqual(set(written), {"label", "summary", "setups"})
        opt = bench()["options"]["agent"]
        own = definitions.own_agents(bench())
        self.assertEqual(opt["values"], [a["start"]["driver"]["agent"] for a in own])
        self.assertEqual(opt["default"], "m3")
        self.assertEqual(opt["names"]["openai_cua"], "OpenAI CUA")
        self.assertEqual(definitions.bench_options_of(bench(), "test_small", {"agent": "openai_cua"}, "original")[0]["agent"], "openai_cua")

    def test_every_published_model_is_the_original_setups(self):
        for key in ("osworld1", "osworld2"):
            b = bench(key)
            self.assertEqual(definitions.problems(b, "benchmark"), [])
            self.assertEqual({m for a in definitions.own_agents(b) for m in a["models"]}, set(b["setups"]["original"]["models"]))

    def test_the_catalogues_agents_say_where_they_come_from(self):
        tmp = tempfile.mkdtemp(prefix="nullius-from-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(os.path.join(definitions.CATALOGUE, "agents", "codex"), os.path.join(tmp, "agents", "codex"))
        old = os.environ.get("NULLIUS_CATALOGUE")
        os.environ["NULLIUS_CATALOGUE"] = tmp
        self.addCleanup(lambda: os.environ.pop("NULLIUS_CATALOGUE") if old is None else os.environ.update(NULLIUS_CATALOGUE=old))
        got = {a["key"]: a["from"] for a in definitions.load("agent")[0]}
        self.assertEqual((got["codex"], got["goose"]), ("yours", "bundled"))


class Refused(unittest.TestCase):
    def entry(self, change):
        b = copy.deepcopy(bench())
        change(b["_agents"]["openai-cua"], b)
        return definitions.problems(b, "benchmark")

    def test_a_driver_agent_speaks_no_port(self):
        bad = self.entry(lambda e, b: e.update(offers=["agent.computer/v1"], proven={"agent.computer/v1": {}}))
        self.assertIn("agents.json: agents.openai-cua: offers, proven: none - a benchmark's own agent speaks no port, its driver runs it", bad)

    def test_an_entry_says_what_it_is_and_what_it_is_published_with(self):
        for field, said in (("label", "label: say it"), ("summary", "summary: say it"),
                            ("models", "models: the models it is published with, each once, as its code names them")):
            with self.subTest(field):
                self.assertIn(f"agents.json: agents.openai-cua: {said}", self.entry(lambda e, b: e.pop(field)))
        self.assertIn("agents.json: agents.openai-cua: start.driver.script: its script, a path in the upstream",
                      self.entry(lambda e, b: e["start"]["driver"].pop("script")))

    def test_a_model_the_original_setup_does_not_take(self):
        self.assertIn("agents.json: agents.openai-cua.models: gpt-9 is not one of setups.original.models",
                      self.entry(lambda e, b: e["models"].append("gpt-9")))

    def test_only_for_names_its_own_benchmark_or_is_left_out(self):
        self.assertEqual(self.entry(lambda e, b: e.update(only_for=["osworld1"])), [])
        self.assertIn("agents.json: agents.openai-cua.only_for: its own benchmark (osworld1) alone, or left out",
                      self.entry(lambda e, b: e.update(only_for=["osworld2"])))

    def test_the_option_values_are_not_written_beside_the_file(self):
        with open(os.path.join(BENCHES, "osworld1", "standard.json")) as f:
            d = json.load(f)
        d["options"]["agent"]["values"] = ["m3"]
        d = definitions._with_agents(d, os.path.join(BENCHES, "osworld1"))
        self.assertIn("options.agent.values: from agents.json, not written here (label, summary and setups only)",
                      definitions.problems(d, "benchmark"))

    def test_a_driver_start_outside_a_benchmark_folder(self):
        tmp = tempfile.mkdtemp(prefix="nullius-driver-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "agents", "cua"))
        e = dict(bench()["_agents"]["openai-cua"], key="cua", schema="nullius.agent/v1", only_for=["osworld1"])
        with open(os.path.join(tmp, "agents", "cua", "agent.json"), "w") as f:
            json.dump(e, f)
        got, bad = definitions.load("agent", tmp)
        self.assertEqual(got, [])
        self.assertIn("agent cua: start.driver: only an entry of a benchmark's agents.json", bad)


class Fits(unittest.TestCase):
    def test_its_own_benchmark_alone_with_no_port_reason(self):
        benches = definitions.load("benchmark", definitions.CATALOGUE)[0]
        t = match.table(benches, [], [])
        self.assertEqual(t["osworld1"]["osworld1:openai-cua"], [])
        self.assertEqual(t["osworld2"]["osworld1:openai-cua"], ["OpenAI CUA belongs to OSWorld1"])
        self.assertEqual(t["gsm8k"]["osworld2:muse-spark"], ["Muse Spark belongs to OSWorld2"])
        self.assertNotIn("osworld1:openai-cua", t.get("locomo", {}))       # a capability benchmark takes none

    def test_one_entry_list_whose_fits_are_the_builders(self):
        # recipe.entries: the Agents page's list and the builder's Agent step grid; its fits are the builder payload's
        every = definitions.load("benchmark", every=True)[0]
        c = recipe.choices()
        fits, offered = c["fits"], {o["key"] for o in c["agents"] + c["capabilities"]}     # the builder never offers an example
        for kind in ("agent", "capability"):
            for b in (x for x in every if x["family"] == kind):
                ref = definitions.ref_of(b) if "_version" in b else b["key"]
                with self.subTest(ref):
                    got = {e["d"]["_ref"]: e["fits"][0] for e in recipe.entries(kind, benches=[b], every=True, own=True)["entries"]
                           if not e["d"].get("example")}
                    self.assertEqual({k: f["why"] for k, f in got.items()}, {k: v for k, v in fits[ref].items() if k in offered})
                    self.assertEqual({f["ref"] for f in got.values()}, {ref})
        page = {e["d"]["_ref"]: e for e in recipe.entries("agent", own=True)["entries"]}
        self.assertEqual([f["why"] for f in page["osworld1:openai-cua"]["fits"] if f["key"] == "osworld1"], [[]])
        self.assertEqual(page["osworld1:openai-cua"]["protocol"], "Its own script")



RECIPE = {"harness": "standard", "name": "os", "benchmark": "osworld1", "datasets": ["test_small"], "provider": "openrouter",
          "model": "computer-use-preview", "model_choice": "paper", "where": "local", "backend": "local"}


class Recipes(unittest.TestCase):
    def compiled(self, **change):
        out = recipe.compile(dict(RECIPE, **change))
        return out

    def test_the_builder_offers_them_as_agents(self):
        got = {a["key"]: a for a in recipe.choices()["agents"]}
        cua = got["osworld1:openai-cua"]
        self.assertEqual((cua["benchmark"], cua["agent"], cua["models"], cua["protocol"]),
                         ("osworld1", "openai_cua", ["computer-use-preview"], "Its own script"))

    def test_an_own_agent_compiles_to_the_command_of_the_agent_option(self):
        old = self.compiled(others=["original"], bench_options={"agent": "openai_cua", "max_steps": "20"})
        new = self.compiled(others=["osworld1:openai-cua"], bench_options={"max_steps": "20"})
        self.assertEqual((old["errors"], new["errors"]), ([], []))
        self.assertEqual(new["shards"][0]["command"], old["shards"][0]["command"])
        self.assertEqual(new["script"], old["script"])
        self.assertIn("run osworld1 original --model computer-use-preview --provider openrouter --dataset test_small -j 1 "
                      "--bench-option agent=openai_cua --bench-option thinking=adaptive --bench-option max_steps=20", new["script"])

    def test_a_recipe_from_before_is_read_as_its_agent(self):
        self.assertEqual(recipe.with_own_agents(dict(RECIPE, others=["original", "nop"], bench_options={"agent": "openai_cua", "pause": "1"}),
                                                bench())["others"], ["osworld1:openai-cua", "nop"])
        first = recipe.with_own_agents(dict(RECIPE, others=["original"]), bench())
        self.assertEqual((first["others"], first["bench_options"]), (["osworld1:m3"], {}))
        said = [e["message"] for e in self.compiled(others=["original"], bench_options={"agent": "nope"})["errors"]]
        self.assertEqual(said, ["Choose an agent for OSWorld1: osworld1:nope is not one of its own."])

    def test_two_of_its_agents_are_two_jobs_named_by_their_keys(self):
        out = self.compiled(others=["osworld1:m3", "osworld1:openai-cua"], model="MiniMax-M3", model_choice="any")
        self.assertEqual(out["errors"], [])
        self.assertEqual([j["name"] for j in out["jobs"]], ["os-m3", "os-openai-cua"])
        self.assertIn("--bench-option agent=m3 ", out["shards"][0]["command"])
        self.assertIn("--bench-option agent=openai_cua ", out["shards"][0]["command"])

    def test_like_the_paper_names_the_agent_a_model_is_not_published_with(self):
        said = [e for e in self.compiled(others=["osworld1:openai-cua"], model="MiniMax-M3")["errors"]]
        self.assertEqual(said, [{"step": recipe.STEP["model"],
                                 "message": "OpenAI CUA is published with computer-use-preview: choose one of them, or Any of your choice for another."}])

    def test_two_of_its_agents_without_a_model_say_so_once(self):
        said = [e["message"] for e in self.compiled(others=["osworld1:m3", "osworld1:claude"], model="")["errors"]]
        self.assertEqual(said.count("Choose a model."), 1)

    def test_another_benchmarks_agent_is_refused_with_its_reason(self):
        said = [e["message"] for e in self.compiled(others=["osworld2:m3"])["errors"]]
        self.assertEqual(said, ["MiniMax M3 cannot run OSWorld1: MiniMax M3 belongs to OSWorld2."])



class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-own-job-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def written(self, other, **opts):
        d = os.path.join(self.tmp, "job")
        os.makedirs(d, exist_ok=True)
        job.write_config(d, job="job", bench=bench(), other=other, model="computer-use-preview", provider="openrouter",
                         dataset="test_small", units=["u1"], concurrency=1, granted=False, bench_options=opts)
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump({}, f)
        return d

    def test_a_job_names_the_agent_that_ran_its_setup_still_original(self):
        other, why = definitions.setup(bench(), "original", "computer-use-preview", "openrouter", "openai_cua")
        self.assertEqual((why, other["key"], other["agent"]), ("", "original", "openai-cua"))
        d = self.written(other, agent="openai_cua")
        with open(os.path.join(d, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual((cfg["agents"][0]["name"], cfg["standard"]["setup"]["agent"], cfg["standard"]["plugged"]), ("openai-cua", "openai-cua", "original"))
        self.assertEqual(parse.parse_job(d).agent_name, "openai-cua")

    def test_a_job_from_before_is_named_by_its_agent_option(self):
        other, _ = definitions.setup(bench(), "original", "computer-use-preview", "openrouter", "openai_cua")
        other.pop("agent")                                   # as a job written before its agent was recorded
        d = self.written(other, agent="openai_cua")
        with open(os.path.join(d, "config.json")) as f:
            self.assertEqual(json.load(f)["agents"][0]["name"], "original")
        self.assertEqual(parse.parse_job(d).agent_name, "openai-cua")
        other, _ = definitions.setup(bench("gsm8k"), "original", "gpt-4o-mini", "openrouter")
        self.assertNotIn("agent", other)

    def test_under_the_model_method_its_agent_is_asked_as_its_own_first_model(self):
        other, why = definitions.setup(bench(), "model", "vendor/any", "openrouter", "claude")
        self.assertEqual((why, other["asked"], other["sent"], other["agent"]), ("", "claude-sonnet-4-5-20250929", "vendor/any", "claude"))
        self.assertEqual(definitions.setup(bench(), "model", "vendor/any", "openrouter")[0]["asked"], "MiniMax-M3")
        other, _ = definitions.setup(bench("gsm8k"), "original", "gpt-4o-mini", "openrouter")
        self.assertNotIn("agent", other)                    # a benchmark without agents of its own: nothing changes


if __name__ == "__main__":
    unittest.main()
