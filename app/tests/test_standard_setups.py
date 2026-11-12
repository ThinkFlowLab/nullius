"""A benchmark's setups: the schema of ``setups``, the original that takes only
the paper's models and the model setup that puts another in their place, a
run of each through the relay against a local fake endpoint, the job's record
of which setup ran, and the builder's recipe. No key, no network."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.standard import definitions, hostreq, install, match, recipe, run

# A paper's benchmark in miniature: two conversations, each answered by the upstream's own code (``answer``) in one
# call to the model it is asked as, and scored by whether the reply came back.
DRIVER = '''import json, os, sys, urllib.request
from nullius.standard.jsonl import serve
def units(dataset, setup=None):
    return {"units": ["c1", "c2"] if setup else ["c1:0", "c1:1", "c2:0"]}
def answer(unit, dir, setup, model, base_url, key):
    req = urllib.request.Request(base_url + "/chat/completions", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                                 data=json.dumps({"model": model, "messages": [{"role": "user", "content": unit}]}).encode())
    text = json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"]
    if not text:
        raise RuntimeError("the reply had no text")
    return {"answer": text}
def score(unit, dir, transcript, answer, setup=None):
    return {"reward": 1.0 if answer and unit in answer else 0.0, "detail": {"setup": setup, "asked": [t.get("asked") for t in transcript]}}
serve({"units": units, "setup": lambda unit, dir: {"prompt": unit}, "answer": answer, "score": score})
'''
SETUPS = {"original": {"summary": "The paper's own reader on a whole conversation.", "models": ["paper-1", "paper-1-16k"],
                       "default": "paper-1", "served_as": {"paper-1-16k": "paper-1"}, "providers": {"custom": "", "openrouter": "vendor/"},
                       "units": {"noun": "conversation", "nouns": "conversations"}},
          "model": {"summary": "The same reader, another model.", "runs_as": "paper-1"}}


class Echo(BaseHTTPRequestHandler):
    """A model that says back the last message; silent when told to be."""
    silent = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = "" if Echo.silent else "you said: " + body["messages"][-1]["content"]
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                          "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                    "completion_tokens_details": {"reasoning_tokens": 3 if Echo.silent else 0}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def bench(root: str, setups=SETUPS) -> dict:
    here = os.path.join(root, "paperbench")
    os.makedirs(here, exist_ok=True)
    with open(os.path.join(here, "driver.py"), "w") as f:
        f.write(DRIVER)
    d = {"schema": "nullius.benchmark/v1", "key": "paperbench", "label": "Paper bench",
         "summary": "Each question is a unit for a memory; the paper answers a conversation at a time.",
         "family": "capability", "needs": ["cap.memory/v1"], "driver": {"command": ["python3", "{here}/driver.py"]},
         "datasets": [{"id": "all", "units": 3}], "units": {"noun": "question", "nouns": "questions"},
         "score": {"kind": "score", "says": "the reply names the unit"}, "limits": {"turn_seconds": 20}}
    if setups is not None:
        d["setups"] = setups
    with open(os.path.join(here, "standard.json"), "w") as f:
        json.dump(d, f)
    b, bad = definitions._read(os.path.join(here, "standard.json"), "benchmark", "paperbench")
    assert not bad, bad
    return b


class Schema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.b = bench(self.tmp, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def problems(self, setups):
        return definitions.problems(dict(self.b, setups=setups), "benchmark")

    def test_each_wrong_field_is_named(self):
        o = SETUPS["original"]
        self.assertIn("setups.gold: a setup is one of ('original', 'model')", self.problems({"gold": o}))
        self.assertTrue(any("setups.original.default" in p for p in self.problems({"original": dict(o, default="other")})))
        self.assertTrue(any("setups.original.providers" in p for p in self.problems({"original": dict(o, providers={"azure": ""})})))
        self.assertTrue(any("setups.original.served_as" in p for p in self.problems({"original": dict(o, served_as={"x": "y"})})))
        self.assertIn("setups.model: needs original, whose pipeline it runs", self.problems({"model": SETUPS["model"]}))
        self.assertTrue(any("setups.model.runs_as" in p for p in self.problems(dict(SETUPS, model={"summary": "x", "runs_as": "nope"}))))
        self.assertTrue(any("is not a field" in p for p in self.problems({"original": dict(o, temperature=0)})))

    def test_an_agent_cannot_take_a_setups_name(self):
        a = {"schema": "nullius.agent/v1", "key": "original"}
        self.assertIn("key: original is the word for a benchmark's setup", definitions.problems(a, "agent"))

    def test_every_catalogued_setup_is_valid(self):
        for b in definitions.load("benchmark", every=True)[0]:
            self.assertEqual([p for p in definitions.problems(b, "benchmark") if p.startswith("setups")], [], b["_ref"])


class Choosing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.b = bench(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_original_takes_only_the_papers_models_each_as_the_provider_spells_it(self):
        o, _ = definitions.setup(self.b, "original", "", "openrouter")
        self.assertEqual((o["asked"], o["sent"], o["comparable"]), ("paper-1", "vendor/paper-1", True))
        o, _ = definitions.setup(self.b, "original", "paper-1-16k", "custom")
        self.assertEqual((o["asked"], o["sent"]), ("paper-1-16k", "paper-1"))
        o, why = definitions.setup(self.b, "original", "their-model", "custom")
        self.assertIsNone(o)
        self.assertIn("only the paper's models", why)
        o, why = definitions.setup(self.b, "original", "paper-1", "openai")
        self.assertIsNone(o)
        self.assertIn("not served by openai", why)

    def test_the_originals_side_models_go_as_the_provider_serves_them_and_never_as_the_reader(self):
        b = dict(self.b, setups=dict(SETUPS, original=dict(SETUPS["original"], side_models=["extractor"])))
        o, _ = definitions.setup(b, "original", "paper-1", "openrouter")
        self.assertEqual(o["model"]["side_models"], ["extractor=vendor/extractor"])
        b["setups"]["original"]["side_models"] = ["paper-1"]                # the reader's own name: its calls are the reader's
        self.assertEqual(definitions.setup(b, "original", "paper-1", "openrouter")[0]["model"]["side_models"], [])
        self.assertEqual(definitions.setup(b, "model", "their-model", "openai")[0]["model"]["side_models"], [])  # all yours
        self.assertTrue(any("side_models" in p for p in definitions.problems(
            dict(self.b, setups={"original": dict(SETUPS["original"], side_models=["x", "x"])}), "benchmark")))

    def test_the_model_setup_asks_as_the_papers_model_and_sends_yours(self):
        o, _ = definitions.setup(self.b, "model", "their-model", "openai")
        self.assertEqual((o["asked"], o["sent"], o["comparable"]), ("paper-1", "their-model", False))
        self.assertIsNone(definitions.setup(self.b, "model", "", "openai")[0])

    def test_a_setup_fits_the_benchmark_that_declares_it(self):
        o, _ = definitions.setup(self.b, "original")
        self.assertEqual(match.fit(self.b, o), [])
        self.assertTrue(match.fit(dict(self.b, setups={"original": SETUPS["original"]}), dict(o, key="model")))


class SetupRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.env = {"MODEL_API_URL": f"http://127.0.0.1:{cls.up.server_address[1]}/v1", "MODEL_API_KEY": "sk-real"}

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        self.enterContext(patch.dict(os.environ, {"NULLIUS_HOME": os.path.join(self.tmp, "profile")}))   # a unit's relay writes its key file there
        Echo.silent = False
        self.b = bench(self.tmp)

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self, which, model, **kwargs):
        o, why = definitions.setup(self.b, which, model, "custom")
        assert o, why
        return run.run(self.b, o, model=model, provider="custom", jobs_root=os.path.join(self.tmp, "jobs"),
                       concurrency=2, env=self.env, say=lambda s: None, **kwargs)

    def trials(self, job_dir):
        out = {}
        for t in sorted(os.listdir(job_dir)):
            p = os.path.join(job_dir, t, "result.json")
            if os.path.isfile(p):
                with open(p) as f:
                    out[t.split("__")[0]] = json.load(f)
        return out

    def calls(self, job_dir):
        got = []
        for t in os.listdir(job_dir):
            d = os.path.join(job_dir, t, ".work", "relay")
            for n in os.listdir(d) if os.path.isdir(d) else []:
                if not n.endswith(".jsonl"):
                    continue
                with open(os.path.join(d, n)) as f:
                    got += [json.loads(x) for x in f if x.strip()]
        return got

    def test_the_original_runs_the_papers_units_asked_as_its_model(self):
        job_dir = self.go("original", "paper-1-16k")
        got = self.trials(job_dir)
        self.assertEqual(set(got), {"c1", "c2"})                          # its own units: conversations, not questions
        self.assertTrue(all(r["verifier_result"]["rewards"]["score"] == 1.0 for r in got.values()))
        self.assertEqual(got["c1"]["standard"]["setup"], {"name": "original", "asked": "paper-1-16k", "sent": "paper-1",
                                                          "comparable_with_paper": True})
        calls = self.calls(job_dir)
        self.assertEqual({(c["model_asked"], c["model"]) for c in calls}, {("paper-1-16k", "paper-1")})
        with open(os.path.join(job_dir, "config.json")) as f:
            c = json.load(f)
        self.assertEqual(c["runner_items"], ["conversation", "conversations"])
        self.assertEqual(c["standard"]["kind"], "upstream")
        self.assertNotIn("benchmark_options", c["standard"])

    def test_the_model_setup_sends_yours_in_the_papers_place(self):
        job_dir = self.go("model", "their-model")
        self.assertEqual({(c["model_asked"], c["model"]) for c in self.calls(job_dir)}, {("paper-1", "their-model")})
        r = self.trials(job_dir)["c1"]
        self.assertFalse(r["standard"]["setup"]["comparable_with_paper"])

    def test_a_reply_with_no_text_is_said_as_the_models(self):
        Echo.silent = True
        r = self.trials(self.go("model", "thinker"))["c1"]
        self.assertEqual(r["exception_info"]["exception_type"], "OutputLengthExceededError")
        self.assertIn("thinker returned no text (3 of its 3 tokens were reasoning)", r["exception_info"]["exception_message"])

    def test_a_docker_setup_still_requires_a_working_daemon(self):
        o, _ = definitions.setup(self.b, "original")
        with patch("nullius.standard.container.unusable", return_value="daemon unavailable"):
            with self.assertRaisesRegex(run.Refused, "daemon unavailable"):
                run.Run(self.b, o, model="paper-1", provider="custom", jobs_root=self.tmp, env=self.env, backend="container")

    def test_docker_setup_runs_driver_and_linux_dependencies_with_scoped_credentials(self):
        from tests import docker_tier
        from nullius.standard import container
        why = docker_tier.why_not()
        if why:
            self.skipTest(why)
        here = self.b["_here"]
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write("import os, packaging\nassert os.path.isfile('/.dockerenv')\n"
                    "assert packaging.__version__ == '24.2'\n"
                    "assert os.environ['HF_TOKEN'] == 'fixture-gated-token'\n"
                    "assert open(os.environ['TOOL_FILE']).read() == 'fixture licence'\n"
                    "assert open(os.environ['NULLIUS_DATASET']).read() == 'fixture data'\n"
                    "assert 'MODEL_API_KEY' not in os.environ\n" + DRIVER)
        self.b["driver"] = {"command": ["{install}/.venv/bin/python", "{here}/driver.py"],
                            "install": {"python": "3.12", "pip": ["packaging==24.2"]}}
        data = os.path.join(here, "data.txt")
        with open(data, "w") as f:
            f.write("fixture data")
        self.b["datasets"][0].update(path="data.txt", sha256=install.sha256_of(data), environment="data")
        credential = os.path.join(self.tmp, "tool.lic")
        with open(credential, "w") as f:
            f.write("fixture licence")
        self.b["environments"] = {"data": {"local_ok": True, "needs_secrets": [
            {"name": "HF_TOKEN", "kind": "value", "to": "driver"},
            {"name": "TOOL_LICENSE", "kind": "file", "to": "driver", "env": "TOOL_FILE"}]}}
        held = run.Licences(self.b, {"HF_TOKEN": {"kind": "value", "value": "fixture-gated-token"},
                                    "TOOL_LICENSE": {"kind": "file", "path": credential}})
        job_dir = self.go("original", "paper-1", backend="container", licences=held)
        trials = self.trials(job_dir)
        self.assertEqual(set(trials), {"c1", "c2"})
        self.assertTrue(all(t.get("verifier_result", {}).get("rewards", {}).get("score") == 1 for t in trials.values()), trials)
        self.assertEqual({c["model"] for c in self.calls(job_dir)}, {"paper-1"})
        with open(os.path.join(job_dir, "config.json")) as f:
            self.assertEqual(json.load(f)["standard"]["backend"], "container")
        left = container._docker(["ps", "-aq", "--filter", "label=nullius.job=" + os.path.basename(job_dir)])
        self.assertFalse(left.stdout.strip())
        Echo.silent = True
        failed = self.go("model", "thinker", backend="container", licences=held)
        self.assertTrue(all(t["exception_info"]["exception_type"] == "OutputLengthExceededError"
                            for t in self.trials(failed).values()))
        left = container._docker(["ps", "-aq", "--filter", "label=nullius.job=" + os.path.basename(failed)])
        self.assertFalse(left.stdout.strip())
        # A driver that exits before listing units must also leave no container.
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write("raise RuntimeError('fixture startup failure')\n")
        other, _ = definitions.setup(self.b, "original", "paper-1", "custom")
        broken = run.Run(self.b, other, model="paper-1", provider="custom", jobs_root=self.tmp,
                         env=self.env, backend="container", licences=held, say=lambda s: None)
        with self.assertRaisesRegex(Exception, "fixture startup failure"):
            broken.start()
        left = container._docker(["ps", "-aq", "--filter", "label=nullius.job=" + broken.name])
        self.assertFalse(left.stdout.strip())


class Recipe(unittest.TestCase):
    """The builder's recipe, on LoCoMo's catalogued setups."""

    def compiled(self, **kw):
        r = {"version": 1, "harness": recipe.HARNESS, "name": "locomo-experiment", "title": "LoCoMo experiment", "benchmark": "locomo",
             "datasets": ["locomo10"], "others": [], "model_choice": "any", "provider": "openrouter", "model": "", "limit": 1,
             "concurrency": 2, "unsandboxed": False, "where": "local", "options": {}}
        r.update(**kw)
        return recipe.compile(r)

    def test_one_original_participant_uses_the_selected_model_scope(self):
        paper = self.compiled(others=["original"], model_choice="paper", model="gpt-3.5-turbo-16k")
        custom = self.compiled(others=["original"], model_choice="any", model="vendor/their-model")
        for out in (paper, custom):
            self.assertEqual(out["errors"], [])
            self.assertIn("--bench-option retrieval=none", out["script"])
            self.assertNotIn("--bench-option rag_mode", out["script"])
        self.assertIn("run locomo original --model gpt-3.5-turbo-16k --provider openrouter", paper["script"])
        self.assertIn("run locomo model --model vendor/their-model", custom["script"])

    def test_paper_filter_also_applies_to_a_memory_capability(self):
        out = self.compiled(others=["mem0"], model_choice="paper", model="gpt-3.5-turbo-16k")
        self.assertEqual(out["errors"], [])
        self.assertIn("--model openai/gpt-3.5-turbo", out["script"])
        self.assertTrue(self.compiled(others=["mem0"], model_choice="paper", model="vendor/custom")["errors"])

    def test_like_the_paper_without_paper_models_leaves_the_model_free_and_keeps_the_official_judge_and_user(self):
        tau = dict(benchmark="tau3-bench", datasets=["airline"], others=["original"], provider="openai", model="their-model")
        out = self.compiled(**tau, model_choice="paper")
        self.assertEqual(out["errors"], [])
        self.assertIn("--model their-model", out["script"])
        self.assertNotIn("--judge-model", out["script"])
        self.assertNotIn("--simulator-model", out["script"])
        for side in ("judge_model", "simulator_model"):
            errors = self.compiled(**tau, model_choice="paper", **{side: "other-model"})["errors"]
            self.assertIn("Like the paper uses", " ".join(e["message"] for e in errors))
            self.assertEqual(self.compiled(**tau, model_choice="paper", **{side: "gpt-4.1-2025-04-14"})["errors"], [])
        gsm = self.compiled(benchmark="gsm8k", datasets=["test"], others=["original"], provider="openai", model="m", model_choice="paper")
        self.assertTrue(gsm["errors"])

    def test_like_the_leaderboard_runs_its_user_simulator_options_and_attempts(self):
        tau = dict(benchmark="tau3-bench", datasets=["airline"], others=["original"], provider="openai", model="their-model",
                   model_choice="leaderboard")
        out = self.compiled(**tau)
        self.assertEqual(out["errors"], [])
        for words in ("--model their-model", "--simulator-model gpt-5.2", "--bench-option user_reasoning_effort=low", "--repeat 4"):
            self.assertIn(words, out["script"])
        self.assertNotIn("--judge-model", out["script"])
        self.assertIn("--simulator-model openai/gpt-5.2", self.compiled(**{**tau, "provider": "openrouter"})["script"])
        for bad, step in (({"repeat": 2}, "Limits"), ({"bench_options": {"user_reasoning_effort": "high"}}, "Dataset"),
                          ({"simulator_model": "gpt-4.1-2025-04-14"}, "Model"), ({"judge_model": "other-model"}, "Model")):
            errors = self.compiled(**tau, **bad)["errors"]
            self.assertTrue(errors, bad)
            self.assertIn("Like the leaderboard", errors[0]["message"])
        self.assertEqual(self.compiled(**tau, repeat=8)["errors"], [])
        self.assertTrue(self.compiled(benchmark="gsm8k", datasets=["test"], others=["original"], provider="openai", model="m",
                                      model_choice="leaderboard")["errors"])

    def test_a_leaderboard_block_is_checked_against_the_benchmark_it_sits_in(self):
        b = {"options": {"effort": {"values": ["default", "low"]}}}               # no simulator: a leaderboard cannot set one
        b["leaderboard"] = {"says": "a board", "simulator": "m", "bench_options": {"nope": "x"}, "repeat": 0, "extra": 1}
        bad = definitions._leaderboard(b)
        for word in ("leaderboard.extra", "leaderboard.simulator", "leaderboard.bench_options.nope", "leaderboard.repeat"):
            self.assertTrue(any(x.startswith(word) for x in bad), (word, bad))
        b["leaderboard"] = {"says": "a board", "bench_options": {"effort": "low"}, "repeat": 4}
        self.assertEqual(definitions._leaderboard(b), [])

    def test_the_original_plays_its_user_with_the_simulator_chosen(self):
        out = self.compiled(benchmark="tau3-bench", datasets=["airline"], others=["original"], provider="openai", model="m",
                            model_choice="any", simulator_model="their-user")
        self.assertEqual(out["errors"], [])
        self.assertIn("--simulator-model their-user", out["script"])

    def test_schema_exposes_one_original_and_the_paper_model_mapping(self):
        benches = recipe.choices()["benchmarks"]
        locomo = next(b for b in benches if b["key"] == "locomo")
        self.assertEqual([s["key"] for s in locomo["setups"]], ["original"])
        self.assertEqual(locomo["setups"][0]["sent"]["openrouter"]["gpt-3.5-turbo-16k"], "openai/gpt-3.5-turbo")

    def test_original_requires_an_explicit_model(self):
        self.assertTrue(self.compiled(others=["original"], model="")["errors"])

    def test_old_model_participant_is_not_a_recipe_choice(self):
        self.assertTrue(self.compiled(others=["model"], model="vendor/model")["errors"])

    def test_a_gpu_option_is_refused_for_a_container_setup_and_asked_of_the_host_locally(self):
        boxed = self.compiled(others=["original"], model="vendor/model", backend="container", bench_options={"retrieval": "dialog"})
        self.assertIn("cuda_gpu cannot be provided in a container: choose local processes.", [e["message"] for e in boxed["errors"]])
        self.assertEqual(boxed["host"]["needs"], [])
        here = self.compiled(others=["original"], model="vendor/model", bench_options={"retrieval": "dialog"})
        self.assertEqual([n["name"] for n in here["host"]["needs"]], ["cuda_gpu"])

    def test_a_gpu_option_is_refused_for_a_container_setup_beside_another_participant(self):
        mixed = self.compiled(others=["original", "keyword-memory"], model="vendor/model", backend="container", bench_options={"retrieval": "dialog"})
        self.assertIn("cuda_gpu cannot be provided in a container: choose local processes.", [e["message"] for e in mixed["errors"]])

    def test_a_container_setups_image_installs_what_has_an_install(self):
        tau = definitions.get("benchmark", "tau3-bench")
        self.assertEqual(hostreq.in_image(tau, ["sandbox"]), ["sandbox"])
        self.assertEqual(hostreq.in_image(definitions.get("benchmark", "locomo"), ["cuda_gpu"]), [])

    def test_a_setup_compiles_for_a_container_without_local_agent_consent(self):
        out = self.compiled(others=["original"], model="vendor/model", backend="container")
        self.assertEqual(out["errors"], [])
        self.assertIn("--backend container", out["script"])
        self.assertNotIn("--unsandboxed", out["script"])



class Attempts(unittest.TestCase):
    """A unit tried several times: pass^k, and an option where it applies."""

    def trial(self, unit, reward=None, error=False):
        r = {"standard": {"unit": unit}}
        if error:
            r["exception_info"] = {"exception_type": "AgentTimeoutError"}
        else:
            r["verifier_result"] = {"rewards": {"reward": reward}}
        return r

    def test_pass_hat_k_as_tau_bench_counts_it(self):
        from nullius.standard import job
        rs = [self.trial("a", 1.0), self.trial("a", 1.0), self.trial("b", 1.0), self.trial("b", 0.0)]
        self.assertEqual(job.pass_hat(rs), {"units": 2, "attempts": 2, "1": 0.75, "2": 0.5, "at": {"1": 0.75, "2": 1.0}})
        self.assertIsNone(job.pass_hat(rs[:1] + rs[2:3]))                         # one attempt each: no pass^k
        self.assertEqual(job.pass_hat(rs + [self.trial("c", error=True), self.trial("c", 1.0)])["units"], 2)   # an error leaves its unit out

    def test_an_option_applies_where_its_setups_say(self):
        tmp = tempfile.mkdtemp()
        try:
            b = bench(tmp)
            b = dict(b, options={"top_k": {"summary": "how many", "values": ["5", "10"], "default": "5", "setups": ["original"]},
                                 "feed": {"summary": "what a memory is fed", "values": ["a", "b"], "default": "a"}})
            self.assertEqual([p for p in definitions.problems(b, "benchmark") if "options" in p], [])
            self.assertEqual(definitions.bench_options_of(b, "all", {"top_k": "10"}, "original")[0], {"top_k": "10"})
            self.assertEqual(definitions.bench_options_of(b, "all", {"top_k": "10"})[0], {"feed": "a"})      # yours: the port's
            bad = dict(b, options={"x": {"summary": "s", "values": ["a"], "default": "a", "setups": ["elsewhere"]}})
            self.assertTrue(any("options.x.setups" in p for p in definitions.problems(bad, "benchmark")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_attempts_reach_a_driver_only_when_it_says_its_upstream_tells_them_apart(self):
        b = {"limits": {"attempts": "a seed per trial"}}
        r = run.Run.__new__(run.Run)
        r.local, r.bench = threading.local(), b
        r.local.attempt = 2
        self.assertEqual(r.attempt_kw(), {"attempt": 2})
        r.bench = {}
        self.assertEqual(r.attempt_kw(), {})

if __name__ == "__main__":
    unittest.main()
