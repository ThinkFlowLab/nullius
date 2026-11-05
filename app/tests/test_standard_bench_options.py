"""A benchmark's own options (a search, a mode) and the machine setup some of
their values need: checked in its definition, scoped to the datasets they
apply to, compiled into each command that runs such a dataset, refused by a
run on a machine without the programs, and told to a person as the commands
that install them."""
from __future__ import annotations

import copy
import unittest

from nullius import hosts
from nullius.standard import definitions, hostreq, recipe, run

TAU = definitions.get("benchmark", "tau3-bench")


def tau(**changes):
    b = copy.deepcopy(TAU)
    b.update(changes)
    return b


class Definition(unittest.TestCase):
    def test_tau3s_search_option_and_its_machine_setup_are_valid(self):
        self.assertEqual(definitions.problems({k: v for k, v in TAU.items() if not k.startswith("_")}, "benchmark"), [])
        o = TAU["options"]["retrieval"]
        self.assertEqual(o["datasets"], ["banking_knowledge"])
        self.assertEqual(o["default"], "bm25")                       # needs nothing of the machine, no key
        self.assertNotIn("qwen_embeddings", o["values"])              # its address and key are the upstream's own
        self.assertEqual(TAU["host"]["sandbox"]["check"], ["srt", "rg", "bwrap", "socat"])

    def test_a_value_may_only_need_a_declared_requirement_and_a_known_dataset(self):
        bad = tau(options={"retrieval": dict(TAU["options"]["retrieval"], datasets=["nowhere"],
                                             needs={"alltools": {"host": ["gpu"], "embeddings": "a b"}})})
        problems = " ".join(definitions.problems({k: v for k, v in bad.items() if not k.startswith("_")}, "benchmark"))
        for said in ("options.retrieval.datasets", "needs.alltools.host", "needs.alltools.embeddings"):
            self.assertIn(said, problems)
        worse = tau(host={"sandbox": {"summary": "x", "check": ["srt; rm"], "install": {"brew": ["x"]}}})
        problems = " ".join(definitions.problems({k: v for k, v in worse.items() if not k.startswith("_")}, "benchmark"))
        self.assertIn("host.sandbox.check", problems)
        self.assertIn("host.sandbox.install", problems)


class Scope(unittest.TestCase):
    def test_an_option_applies_only_to_its_datasets(self):
        self.assertEqual(definitions.bench_options_of(TAU, "airline", {"retrieval": "alltools"}),
                         ({"task_split": "base", "user_reasoning_effort": "default"}, []))
        vals, bad = definitions.bench_options_of(TAU, "banking_knowledge", {"retrieval": "alltools"})
        self.assertEqual((vals["retrieval"], "task_split" in vals, bad), ("alltools", False, []))
        self.assertEqual(definitions.bench_options_of(TAU, "banking_knowledge")[0]["retrieval"], "bm25")
        self.assertTrue(definitions.bench_options_of(TAU, "banking_knowledge", {"retrieval": "qwen_embeddings"})[1])
        self.assertTrue(definitions.bench_options_of(TAU, "airline", {"nope": "x"})[1])

    def test_what_a_value_needs(self):
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "alltools"}), {"embeddings": ["text-embedding-3-large"], "host": ["sandbox"]})
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "terminal_use"}), {"embeddings": [], "host": ["sandbox"]})
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "bm25"}), {"embeddings": [], "host": []})
        self.assertEqual(run.option_env({"retrieval": "bm25"}), {"NULLIUS_OPTION_RETRIEVAL": "bm25"})


class Host(unittest.TestCase):
    def test_missing_programs_and_the_commands_that_install_them(self):
        have = {"rg"}
        self.assertEqual(hostreq.missing(TAU, ["sandbox"], which=lambda p: p in have), {"sandbox": ["srt", "bwrap", "socat"]})
        self.assertEqual(hostreq.missing(TAU, ["sandbox"], which=lambda p: True), {})
        self.assertEqual(hostreq.install_lines(TAU, ["sandbox"]),
                         ["sudo apt-get install -y ripgrep bubblewrap socat", "npm install -g @anthropic-ai/sandbox-runtime@0.0.23"])
        self.assertIn("sudo -n apt-get", hostreq.install_script(TAU, ["sandbox"]))   # never a password prompt
        self.assertIsNone(hostreq.install_script(TAU, []))
        self.assertEqual(hostreq.all_of(TAU)["sandbox"]["needed_by"], ["retrieval=alltools", "retrieval=terminal_use", "retrieval=terminal_use_write"])

    def test_hardware_has_no_install_and_its_refusal_says_where_to_run(self):
        gpu = {"summary": "A CUDA GPU.", "check": ["nvidia-smi"]}
        self.assertEqual(definitions._host({"host": {"gpu": gpu}}), [])
        self.assertTrue(definitions._host({"host": {"gpu": dict(gpu, install={})}}))     # said, it installs something
        b = {"host": {"gpu": gpu}}
        self.assertEqual(hostreq.missing(b, ["gpu"], which=lambda p: False), {"gpu": ["nvidia-smi"]})
        self.assertEqual((hostreq.install_lines(b, ["gpu"]), hostreq.install_script(b, ["gpu"])), ([], None))

    def test_a_host_test_looks_for_the_programs_named_and_nothing_else(self):
        sent = {}

        def fake(argv, timeout):
            sent["cmd"] = argv[-1]
            return 0, "ok\nuv=yes\nmissing=bwrap\nmissing=rm\nhostname=h\n", ""
        got = hosts.test(hosts.parse("me@example.invalid"), run=fake, programs=["srt", "bwrap", "x; rm -rf ~"])
        self.assertEqual(got["missing"], ["bwrap"])
        self.assertIn("for p in srt bwrap;", sent["cmd"])
        self.assertNotIn("rm -rf", sent["cmd"])
        self.assertNotIn("missing", hosts.test(hosts.parse("me@example.invalid"), run=fake))


class Relay(unittest.TestCase):
    def test_an_embeddings_reply_is_recorded_by_its_usage_never_its_vectors(self):
        import json
        from nullius.standard import relay
        body = json.dumps({"object": "list", "data": [{"embedding": [0.1, 0.2]}] * 3, "usage": {"prompt_tokens": 1000, "total_tokens": 1000}}).encode()
        got = relay.read_reply("/embeddings", body, False)
        self.assertEqual((got["embeddings"], got["usage"]["input"]), (3, 1000))
        self.assertNotIn("0.1", json.dumps(got))
        self.assertIn("text-embedding-3-large", run.PRICES)


class Refused(unittest.TestCase):
    def test_a_run_whose_search_needs_what_the_machine_lacks_is_refused_with_the_commands(self):
        nop = definitions.control(TAU, "nop")
        with self.assertRaises(run.Refused) as e:
            run.Run(TAU, nop, model="", provider="openai", dataset="banking_knowledge", bench_options={"retrieval": "terminal_use"},
                    env={"OPENAI_API_KEY": "k"}, which=lambda p: None, jobs_root="/nonexistent-never-written")
        self.assertIn("sandbox (srt, rg, bwrap, socat not on PATH)", str(e.exception))
        self.assertIn("npm install -g @anthropic-ai/sandbox-runtime@0.0.23", str(e.exception))


class Recipe(unittest.TestCase):
    def a_recipe(self, **kw):
        r = {"version": 1, "harness": "standard", "name": "tau-x", "title": "t", "benchmark": "tau3-bench", "datasets": ["airline", "banking_knowledge"],
             "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": "local"}
        r.update(kw)
        return r

    def test_the_option_is_given_to_the_datasets_it_applies_to_only(self):
        out = recipe.compile(self.a_recipe(bench_options={"retrieval": "openai_embeddings"}))
        self.assertEqual(out["errors"], [])
        airline, banking = (p["script"] for p in out["parts"])
        self.assertNotIn("--bench-option retrieval", airline)
        self.assertIn("--bench-option retrieval=openai_embeddings", banking)
        self.assertFalse(out["keyless"])                                  # the embeddings call a model, with the key
        self.assertIn("openai_embeddings · banking_knowledge", [r[2] for r in out["review"]])
        self.assertEqual(out["host"]["needs"], [])

    def test_a_shell_search_says_its_machine_setup_and_embeddings_want_openai(self):
        out = recipe.compile(self.a_recipe(bench_options={"retrieval": "alltools"}))
        self.assertEqual([n["name"] for n in out["host"]["needs"]], ["sandbox"])
        self.assertEqual(out["host"]["check"], ["srt", "rg", "bwrap", "socat"])
        self.assertTrue(out["host"]["install"])
        other = recipe.compile(self.a_recipe(bench_options={"retrieval": "openai_embeddings"}, provider="openrouter"))
        self.assertIn("OpenAI provider", " ".join(e["message"] for e in other["errors"]))
        bad = recipe.compile(self.a_recipe(bench_options={"retrieval": "nope"}))
        self.assertEqual(bad["errors"][0]["step"], recipe.STEP["dataset"])


class OtherBenchmarks(unittest.TestCase):
    """The options the other benchmarks' upstreams offer, each on the version that brought it."""

    def test_longmemeval_keeps_all_options_in_one_entry(self):
        b = definitions.get("benchmark", "longmemeval")
        self.assertNotIn("@", b["_ref"])
        self.assertEqual(definitions.bench_options_of(b, "longmemeval_s")[0], {"reading": "con"})
        self.assertEqual(set(b["options"]["retriever"]["values"]),
                         {"full-history-session", "full-history-turn", "no-retrieval",
                          "flat-bm25-session", "flat-bm25-turn", "flat-contriever-session",
                          "flat-contriever-turn", "flat-stella-session", "flat-stella-turn"})
        self.assertNotIn("indexes", b["driver"]["install"])

    def test_locomo_offers_the_authors_rag_modes_dialog_first(self):
        b = definitions.get("benchmark", "locomo")
        self.assertEqual(definitions.bench_options_of(b, "locomo10")[0], {"rag_mode": "dialog"})
        self.assertEqual(b["options"]["rag_mode"]["values"], ["dialog", "observation", "summary"])

    def test_terminal_bench_offers_pull_and_build_in_one_entry(self):
        b = definitions.get("benchmark", "terminal-bench")
        self.assertNotIn("@", b["_ref"])
        self.assertEqual(definitions.bench_options_of(b, "offline")[0], {"image": "pull"})
        self.assertEqual(definitions.bench_options_of(b, "offline", {"image": "build"})[0], {"image": "build"})

    def test_bundled_benchmarks_do_not_expose_integration_revisions(self):
        from pathlib import Path
        import json
        root = Path(definitions.get("benchmark", "longmemeval")["_here"]).parent
        for path in root.glob("*/standard.json"):
            with self.subTest(benchmark=path.parent.name):
                data = json.loads(path.read_text())
                self.assertNotIn("versions", data)



class TurnLimit(unittest.TestCase):
    def test_a_units_own_agent_limit_wins_within_bounds(self):
        r = run.Run.__new__(run.Run)
        r.turn_seconds = 900
        self.assertEqual(r.turn_limit({"agent_seconds": 3600}), 3600)
        self.assertEqual(r.turn_limit({}), 900)
        for bad in (0, -5, True, "3600", run.MAX_AGENT_SECONDS + 1):
            self.assertEqual(r.turn_limit({"agent_seconds": bad}), 900, bad)


if __name__ == "__main__":
    unittest.main()
