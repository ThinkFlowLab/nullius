"""A benchmark's own agents (its agents.json): agents of the catalogue that start with start.driver, bound to
their benchmark by where they sit, and the benchmark's agent option rebuilt from them."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest

from nullius.standard import definitions, match

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


if __name__ == "__main__":
    unittest.main()
