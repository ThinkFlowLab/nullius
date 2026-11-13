"""A benchmark's own options (a search, a mode) and the machine setup some of
their values need: checked in its definition, scoped to the datasets they
apply to, compiled into each command that runs such a dataset, refused by a
run on a machine without the programs, and told to a person as the commands
that install them."""
from __future__ import annotations

import copy
import subprocess
import unittest
from unittest.mock import patch

from nullius import hosts, prerequisites
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
        self.assertIn("qwen_embeddings", o["values"])
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
        self.assertFalse(definitions.bench_options_of(TAU, "banking_knowledge", {"retrieval": "qwen_embeddings"})[1])
        self.assertTrue(definitions.bench_options_of(TAU, "airline", {"nope": "x"})[1])

    def test_what_a_value_needs(self):
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "alltools"}), {"embeddings": ["text-embedding-3-large"], "reranker": [], "host": ["sandbox"], "files": []})
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "terminal_use"}), {"embeddings": [], "reranker": [], "host": ["sandbox"], "files": []})
        self.assertEqual(definitions.option_needs(TAU, {"retrieval": "bm25"}), {"embeddings": [], "reranker": [], "host": [], "files": []})
        self.assertEqual(run.option_env({"retrieval": "bm25"}), {"NULLIUS_OPTION_RETRIEVAL": "bm25"})

    def test_only_active_options_need_files_and_saved_values_reactivate(self):
        b = tau(files={"expansions": {"url": "https://example.org/cache.zip", "sha256": "a" * 64}})
        b["options"]["expansion"] = {"summary": "extra retrieval keys", "values": ["none", "facts"], "default": "none",
                                     "when": {"retrieval": ["alltools"]}, "needs": {"facts": {"files": ["expansions"], "reranker": "gpt-5.2"}}}
        self.assertEqual(definitions.problems(b, "benchmark"), [])
        saved = {"retrieval": "bm25", "expansion": "facts"}
        self.assertEqual(definitions.option_needs(b, saved)["files"], [])
        self.assertEqual(definitions.option_needs(b, saved)["reranker"], [])
        saved["retrieval"] = "alltools"
        self.assertEqual(definitions.option_needs(b, saved),
                         {"embeddings": ["text-embedding-3-large"], "reranker": ["gpt-5.2"], "host": ["sandbox"], "files": ["expansions"]})
        self.assertEqual(saved["expansion"], "facts")
        for files in ([], {"BAD-NAME": {}}, {"expansions": {"url": "http://example.org/x", "sha256": "short"}}):
            self.assertTrue(any(p.startswith("files") for p in definitions.problems(dict(b, files=files), "benchmark")))
        b["options"]["expansion"]["needs"]["facts"]["when"] = {"missing": ["yes"]}
        self.assertTrue(any("needs.facts.when" in p for p in definitions.problems(b, "benchmark")))
        b["options"]["expansion"]["needs"]["facts"]["files"] = ["absent"]
        self.assertTrue(any("needs.facts.files" in p for p in definitions.problems(b, "benchmark")))

    def test_rerankers_need_their_chat_model_and_only_embedding_variants_need_embeddings(self):
        from unittest.mock import Mock
        for method in ("bm25_reranker", "bm25_reranker_grep", "openai_embeddings_reranker", "openai_embeddings_reranker_grep"):
            with self.subTest(method=method):
                vals, bad = definitions.bench_options_of(TAU, "banking_knowledge", {"retrieval": method}, "original")
                self.assertEqual(bad, [])
                embedded = method.startswith("openai")
                self.assertEqual(definitions.option_needs(TAU, vals),
                                 {"embeddings": ["text-embedding-3-large"] if embedded else [], "reranker": ["gpt-5.2"], "host": [], "files": []})
                bench = copy.deepcopy(TAU)
                bench["score"].pop("judge")
                bench.pop("simulator")
                other = definitions.control(bench, "nop")
                r = run.Run(bench, other, model="", provider="openai", dataset="banking_knowledge",
                            bench_options={"retrieval": method}, env={"OPENAI_API_KEY": "fixture"})
                r.bench_relay = Mock()
                r.embeddings_env()
                self.assertEqual(r.bench_relay.url.call_args.kwargs,
                                 {"model": "gpt-5.2", "embeddings": "text-embedding-3-large" if embedded else None})
                self.assertIn("gpt-5.2", r.models_called())
                self.assertEqual("text-embedding-3-large" in r.models_called(), embedded)
                with self.assertRaisesRegex(run.Refused, "OPENAI_API_KEY"):
                    run.Run(bench, other, model="", provider="openai", dataset="banking_knowledge",
                            bench_options={"retrieval": method}, env={})
                if not embedded:
                    routed = run.Run(bench, other, model="", provider="openrouter", dataset="banking_knowledge",
                                     bench_options={"retrieval": method}, env={"OPENROUTER_API_KEY": "fixture"})
                    self.assertEqual(routed.rerank_model, "openai/gpt-5.2")


class Host(unittest.TestCase):
    def test_missing_programs_and_the_commands_that_install_them(self):
        have = {"rg"}
        self.assertEqual(hostreq.missing(TAU, ["sandbox"], which=lambda p: p in have), {"sandbox": ["srt", "bwrap", "socat"]})
        self.assertEqual(hostreq.missing(TAU, ["sandbox"], which=lambda p: True), {})
        self.assertEqual(hostreq.install_lines(TAU, ["sandbox"]),
                         ["sudo apt-get install -y ripgrep bubblewrap socat", "npm install -g @anthropic-ai/sandbox-runtime@0.0.23"])
        self.assertIn("sudo -n apt-get", hostreq.install_script(TAU, ["sandbox"]))   # never a password prompt
        self.assertIsNone(hostreq.install_script(TAU, []))
        self.assertEqual(hostreq.all_of(TAU)["sandbox"]["needed_by"], ["retrieval=alltools", "retrieval=terminal_use", "retrieval=terminal_use_write", "retrieval=alltools-qwen"])

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
        other = recipe.compile(self.a_recipe(bench_options={"retrieval": "openai_embeddings"}, provider="custom"))
        self.assertIn("OpenAI or OpenRouter provider", " ".join(e["message"] for e in other["errors"]))
        bad = recipe.compile(self.a_recipe(bench_options={"retrieval": "nope"}))
        self.assertEqual(bad["errors"][0]["step"], recipe.STEP["dataset"])

    def test_reranker_price_is_required_by_review_without_charging_for_unused_embeddings(self):
        for method in ("bm25_reranker", "bm25_reranker_grep", "openai_embeddings_reranker", "openai_embeddings_reranker_grep"):
            called = []
            def prices(models):
                called.extend(models)
                return [m for m in models if m == "gpt-5.2"]
            out = recipe.compile(self.a_recipe(bench_options={"retrieval": method}, max_usd=1), unpriced=prices)
            self.assertIn("gpt-5.2", called)
            self.assertEqual("text-embedding-3-large" in called, method.startswith("openai"))
            self.assertTrue(any("price for gpt-5.2" in e["message"] for e in out["errors"]))
            routed = recipe.compile(self.a_recipe(bench_options={"retrieval": method}, provider="openrouter"))
            self.assertEqual(routed["errors"], [])

    @patch.object(hostreq, "missing", return_value={})
    def test_qwen_requires_openrouter_and_all_search_prices_use_sent_names(self, _missing):
        for method in ("qwen_embeddings", "qwen_embeddings_grep", "qwen_embeddings_reranker",
                       "qwen_embeddings_reranker_grep", "alltools-qwen", "openai_embeddings_reranker"):
            embed = "text-embedding-3-large" if method.startswith("openai") else "qwen/qwen3-embedding-8b"
            sent = "openai/" + embed if method.startswith("openai") else embed
            called = []
            out = recipe.compile(self.a_recipe(bench_options={"retrieval": method}, provider="openrouter", max_usd=1),
                                 unpriced=lambda models: called.extend(models) or [])
            self.assertEqual(out["errors"], [])
            self.assertIn(sent, called)
            self.assertEqual("openai/gpt-5.2" in called, "reranker" in method)
            self.assertEqual(bool(out["host"]["needs"]), method == "alltools-qwen")
            other = definitions.control(TAU, "nop")
            r = run.Run(TAU, other, model="", provider="openrouter", dataset="banking_knowledge",
                        bench_options={"retrieval": method}, env={"OPENROUTER_API_KEY": "fixture"})
            self.assertEqual(r.embed_model, sent)
            self.assertIn(sent, r.models_called())
            with self.assertRaisesRegex(run.Refused, "OPENROUTER_API_KEY"):
                run.Run(TAU, other, model="", provider="openrouter", dataset="banking_knowledge",
                        bench_options={"retrieval": method}, env={})
            for provider in (("custom",) if method.startswith("openai") else ("openai", "custom")):
                bad = recipe.compile(self.a_recipe(bench_options={"retrieval": method}, provider=provider))
                self.assertTrue(any("provider for this search" in e["message"] for e in bad["errors"]))
                with self.assertRaisesRegex(run.Refused, "provider for this search"):
                    run.Run(TAU, other, model="", provider=provider, dataset="banking_knowledge",
                            bench_options={"retrieval": method}, env={})


class OtherBenchmarks(unittest.TestCase):
    """The options the other benchmarks' upstreams offer, each on the version that brought it."""

    def test_longmemeval_keeps_all_options_in_one_entry(self):
        b = definitions.get("benchmark", "longmemeval")
        self.assertNotIn("@", b["_ref"])
        self.assertEqual(definitions.bench_options_of(b, "longmemeval_s")[0], {"question_type": "all", "reading": "con"})
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


class MachineDocker(unittest.TestCase):
    """OSWorld1's VMs run in Docker on the machine its driver runs on: whoever runs its tasks, a setup or the
    null control, that machine is asked for Docker before Run and again as the run starts."""

    def a_recipe(self, others, **kw):
        r = {"version": 1, "harness": "standard", "name": "osw", "title": "t", "benchmark": "osworld1", "datasets": ["test_small"],
             "others": others, "model_choice": "paper", "provider": "openrouter", "model": "MiniMax-M3", "limit": 1,
             "concurrency": 1, "where": "local"}
        r.update(kw)
        return r

    def test_review_asks_for_docker_for_every_setup_and_the_null_control(self):
        for others, kw in ((["nop"], {}), (["original"], {}), (["original"], {"model_choice": "any", "model": "vendor/their-model"}),
                           (["original", "nop"], {})):
            with self.subTest(others=others, **kw):
                out = recipe.compile(self.a_recipe(others, **kw))
                self.assertEqual(out["errors"], [])
                self.assertEqual([n["name"] for n in out["host"]["needs"]], ["docker"])
                setups = {n["name"]: n for n in out["host"]["needs"]}
                rows = prerequisites.check_here([], setups, which=lambda p: None)
                self.assertEqual([(x["name"], x["state"]) for x in rows], [("setup:docker", "missing")])
                self.assertFalse(prerequisites.ready([{"rows": rows}]))
                self.assertTrue(prerequisites.ready([{"rows": prerequisites.check_here([], setups, which=lambda p: "/usr/bin/" + p)}]))

    def test_the_null_control_is_refused_as_it_starts_on_a_machine_without_docker(self):
        b = definitions.get("benchmark", "osworld1")
        with self.assertRaises(run.Refused) as e:
            run.Run(b, definitions.control(b, "nop"), model="", provider="openrouter", dataset="test_small", env={},
                    which=lambda p: None, jobs_root="/nonexistent-never-written")
        self.assertIn("vm_provider=docker needs on this machine docker (docker not on PATH)", str(e.exception))


class TurnLimit(unittest.TestCase):
    def test_a_units_own_agent_limit_wins_within_bounds(self):
        r = run.Run.__new__(run.Run)
        r.turn_seconds = 900
        self.assertEqual(r.turn_limit({"agent_seconds": 3600}), 3600)
        self.assertEqual(r.turn_limit({}), 900)
        for bad in (0, -5, True, "3600", run.MAX_AGENT_SECONDS + 1):
            self.assertEqual(r.turn_limit({"agent_seconds": bad}), 900, bad)


GPU = {"summary": "A CUDA GPU with enough memory.", "check": ["nvidia-smi"], "gpu_memory_mb": 12000}
BENCH = {"host": {"gpu": GPU}}
TOO_SMALL = "needs at least 12000 MiB of GPU memory; largest GPU has 11264 MiB"


class GPURequirements(unittest.TestCase):
    def test_only_positive_integer_memory_is_valid(self):
        for value in (None, True, False, 0, -1, 12000.0, "12000", [], {}):
            with self.subTest(value=value):
                self.assertIn("host.gpu.gpu_memory_mb: a positive whole number of MiB",
                              definitions._host({"host": {"gpu": dict(GPU, gpu_memory_mb=value)}}))
        for value in (1, 12000):
            self.assertEqual(definitions._host({"host": {"gpu": dict(GPU, gpu_memory_mb=value)}}), [])

    def test_largest_card_is_used_not_the_sum_and_boundary_is_inclusive(self):
        for output, expected in (("8192\n11264\n", TOO_SMALL), ("8192\n12000\n", None),
                                 ("24576\n8192\n", None)):
            with self.subTest(output=output), patch.object(hostreq.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output, "")) as query:
                got = hostreq.problems(BENCH, ["gpu"], which=lambda p: True)
                self.assertEqual(got, {"gpu": expected} if expected else {})
                query.assert_called_once_with(
                    ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5)

    def test_unreadable_memory_is_not_reported_as_too_small(self):
        for output, code in (("", 0), ("[N/A]\n", 0), ("8192\nN/A\n", 0), ("0\n", 0),
                             ("-1\n", 0), ("12000 MiB\n", 0), ("24576\n", 1)):
            with self.subTest(output=output, code=code), patch.object(hostreq.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], code, output, "driver unavailable")):
                self.assertEqual(hostreq.problems(BENCH, ["gpu"], which=lambda p: True),
                                 {"gpu": "nvidia-smi could not report GPU memory"})
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("nvidia-smi", 5)):
            with self.subTest(error=error), patch.object(hostreq.subprocess, "run", side_effect=error):
                self.assertIsNone(hostreq.gpu_memory_mb())

    def test_absent_program_is_reported_without_querying_memory(self):
        with patch.object(hostreq, "gpu_memory_mb") as query:
            self.assertEqual(hostreq.problems(BENCH, ["gpu"], which=lambda p: False),
                             {"gpu": "nvidia-smi not on PATH"})
            query.assert_not_called()

    def test_only_selected_memory_requirements_query_the_gpu_once(self):
        bench = {"host": {**BENCH["host"], "larger": dict(GPU, gpu_memory_mb=24000),
                          "program_only": {"check": ["nvidia-smi"]}}}
        with patch.object(hostreq, "gpu_memory_mb", return_value=12000) as query:
            self.assertEqual(hostreq.problems(bench, ["program_only"], which=lambda p: True), {})
            self.assertEqual(hostreq.problems(bench, [], which=lambda p: True), {})
            query.assert_not_called()
            self.assertEqual(hostreq.problems(bench, ["gpu", "larger"], which=lambda p: True),
                             {"larger": "needs at least 24000 MiB of GPU memory; largest GPU has 12000 MiB"})
            query.assert_called_once_with()

    def test_builder_and_run_refuse_stella_with_the_same_explanation(self):
        bench = definitions.get("benchmark", "longmemeval")
        other, why = definitions.setup(bench, "original", "gpt-4o", "openai")
        self.assertFalse(why)
        with patch.object(hostreq, "gpu_memory_mb", return_value=11264):
            for retriever in ("flat-stella-session", "flat-stella-turn"):
                with self.subTest(retriever=retriever), self.assertRaises(run.Refused) as raised:
                    run.Run(bench, other, model="gpt-4o", provider="openai", dataset="longmemeval_s",
                            bench_options={"retriever": retriever}, env={}, which=lambda p: True,
                            jobs_root="/nonexistent-never-written")
                rows = prerequisites.check_here([], {"stella_gpu": bench["host"]["stella_gpu"]}, which=lambda p: True)
                self.assertEqual(rows[0]["detail"], TOO_SMALL)
                self.assertEqual(rows[0]["state"], "missing")
                self.assertIn(rows[0]["detail"], str(raised.exception))
                self.assertFalse(prerequisites.ready([{"rows": rows}]))

    def test_stella_can_start_at_the_minimum_and_contriever_does_not_probe_memory(self):
        bench = definitions.get("benchmark", "longmemeval")
        other, _ = definitions.setup(bench, "original", "gpt-4o", "openai")
        with patch.object(hostreq, "gpu_memory_mb", return_value=12000) as query:
            for retriever in ("flat-contriever-session", "flat-contriever-turn", "flat-stella-session"):
                run.Run(bench, other, model="gpt-4o", provider="openai", dataset="longmemeval_s",
                        bench_options={"retriever": retriever}, env={"OPENAI_API_KEY": "fixture"}, which=lambda p: True,
                        jobs_root="/nonexistent-never-written")
            query.assert_called_once_with()
            rows = prerequisites.check_here([], {"gpu": GPU}, which=lambda p: True)
            self.assertTrue(prerequisites.ready([{"rows": rows}]))


if __name__ == "__main__":
    unittest.main()
