"""The builder's Served by: a recipe's ``served_by`` (the serving providers
each model field pins, on OpenRouter) is checked as ``--served-by`` is and
compiled into it for each job that calls that model, by the name the run
sends it as; a recipe with none compiles to the command it did. The model's
providers come from OpenRouter's endpoint list, asked as the model list is."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error

from nullius import providers
from nullius.standard import definitions, recipe

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
NOP = {"nop": {"source": "nullius", "from": "nothing said"}}


def defs(bench=None):
    d = {k: definitions.load(k, ROOT)[0] for k in ("benchmark", "agent", "capability")}
    if bench is not None:
        d["benchmark"] = [bench if x["key"] == bench["key"] else x for x in d["benchmark"]]
    return d


def fakebench(**kw):
    return dict(definitions.find(definitions.load("benchmark", ROOT)[0], "fakebench"), **kw)


def judged():
    b = fakebench(controls=NOP)
    return dict(b, score=dict(b["score"], judge={"model": "gpt-4o-mini", "says": "a judge"}))


def a_recipe(**kw):
    r = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
         "other": "fake", "provider": "openrouter", "model": "openai/gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": True,
         "where": "local"}
    r.update(kw)
    return r


class TheRecipe(unittest.TestCase):
    def test_a_pin_compiles_to_the_option_and_shows_in_the_review(self):
        out = recipe.compile(a_recipe(served_by={"model": ["deepinfra", "together/fp8"]}), defs())
        self.assertEqual(out["errors"], [])
        self.assertIn("--served-by openai/gpt-5-mini=deepinfra,together/fp8", out["script"])
        model_row = next(row[2] for row in out["review"] if row[1] == "Model")
        self.assertIn("model served by deepinfra, together/fp8", model_row)

    def test_each_job_is_pinned_where_it_calls_the_model(self):
        """A control beside the agent calls no model of its own: its job takes no pin for the run's model; a judge
        pin goes on every job of a judged benchmark, the control's included, by the name OpenRouter serves it as."""
        d = defs(judged())
        out = recipe.compile(a_recipe(others=["fake", "nop"], served_by={"model": ["deepinfra"], "judge": ["prov-j"]}), d)
        self.assertEqual(out["errors"], [])
        scripts = [p["script"] for p in out["parts"]]
        self.assertIn("--served-by openai/gpt-5-mini=deepinfra", scripts[0])
        self.assertIn("--served-by openai/gpt-4o-mini=prov-j", scripts[0])
        self.assertNotIn("openai/gpt-5-mini=deepinfra", scripts[1])
        self.assertIn("--served-by openai/gpt-4o-mini=prov-j", scripts[1])
        row = next(r for r in out["review"] if r[1] == "Model")[2]
        self.assertIn("judge served by prov-j", row)

    def test_a_pin_is_refused_as_the_option_is(self):
        for kw, said in (({"provider": "openai", "model": "gpt-5-mini", "served_by": {"model": ["deepinfra"]}}, "needs OpenRouter"),
                         ({"served_by": {"model": ["bad slug"]}}, "not a serving provider's slug"),
                         ({"served_by": {"model": ["deepinfra", "deepinfra"]}}, "named twice"),
                         ({"served_by": {"agent": ["deepinfra"]}}, "Served by names"),
                         ({"served_by": ["deepinfra"]}, "Served by names"),
                         ({"served_by": {"judge": ["prov-j"]}}, "no job of this recipe calls the judge's model"),
                         ({"served_by": {"model": "deepinfra"}}, "Served by")):
            with self.subTest(kw=kw):
                out = recipe.compile(a_recipe(**kw), defs())
                self.assertTrue(out["errors"], out["errors"])
                self.assertTrue(all(e["step"] == recipe.STEP["model"] for e in out["errors"]), out["errors"])
                self.assertTrue(any(said in e["message"] for e in out["errors"]), out["errors"])
                self.assertEqual(out["script"], "")

    def test_one_model_in_two_fields_takes_one_list(self):
        d = defs(judged())
        same = recipe.compile(a_recipe(model="openai/gpt-4o-mini", served_by={"model": ["a"], "judge": ["a"]}), d)
        self.assertEqual(same["errors"], [])
        lines = [line for line in same["script"].splitlines() if "--served-by" in line]
        self.assertTrue(lines and all(line.count("--served-by") == 1 for line in lines))      # each way of starting it: once
        other = recipe.compile(a_recipe(model="openai/gpt-4o-mini", served_by={"model": ["a"], "judge": ["b"]}), d)
        self.assertIn("pins openai/gpt-4o-mini twice", other["errors"][0]["message"])

    def test_a_pin_is_its_models_on_every_job_that_calls_it(self):
        """The model field's model is also the judge's: the control beside the agent calls it as its judge, and its job
        takes the pin too."""
        out = recipe.compile(a_recipe(others=["fake", "nop"], model="openai/gpt-4o-mini", served_by={"model": ["prov-a"]}), defs(judged()))
        self.assertEqual(out["errors"], [])
        for part in out["parts"]:
            self.assertIn("--served-by openai/gpt-4o-mini=prov-a", part["script"])

    def test_a_list_with_an_empty_or_a_bad_slug_is_refused_in_sight(self):
        for slugs in (["deepinfra", ""], ["deepinfra", "bad slug"]):
            with self.subTest(slugs=slugs):
                out = recipe.compile(a_recipe(served_by={"model": slugs}), defs())
                self.assertTrue(any("not a serving provider's slug" in e["message"] for e in out["errors"]), out["errors"])

    def test_each_fields_model_is_given_as_the_run_sends_it(self):
        """Like the paper, LongMemEval's gpt-4o is sent as openai/gpt-4o-2024-08-06 on OpenRouter: its providers are that
        model's, and the pin is put on that name."""
        sets = definitions.find(definitions.load("benchmark")[0], "longmemeval")["datasets"]
        r = a_recipe(benchmark="longmemeval", dataset=sets[0]["id"], other="", others=["original"], model_choice="paper", model="gpt-4o",
                     served_by={"model": ["openai"]})
        out = recipe.compile(r)
        sent = out["served_models"]["model"]
        self.assertEqual(sent, "openai/gpt-4o-2024-08-06")
        self.assertIn(f"--served-by {sent}=openai", out["script"])
        self.assertEqual(recipe.compile(a_recipe(), defs())["served_models"]["model"], "openai/gpt-5-mini")

    def test_a_recipe_with_no_pin_compiles_to_the_command_it_did(self):
        plain = recipe.compile(a_recipe(), defs())
        self.assertEqual(plain["errors"], [])
        self.assertNotIn("--served-by", plain["script"])
        for none in ({}, {"model": []}, None):
            with self.subTest(none=none):
                self.assertEqual(recipe.compile(a_recipe(served_by=none), defs())["script"], plain["script"])
        openai = recipe.compile(a_recipe(provider="openai", model="gpt-5-mini", served_by={}), defs())
        self.assertEqual(openai["errors"], [])


class TheProviders(unittest.TestCase):
    """``providers.list_endpoints``: OpenRouter's endpoint list, a fake opener in the place of the network."""

    def opener(self, body=None, error=None):
        seen = []

        def opener(req, timeout=None):
            seen.append(req)
            if error:
                raise error
            return io.BytesIO(json.dumps(body).encode())
        return opener, seen

    def test_a_models_providers_by_slug_and_name(self):
        body = {"data": {"id": "meta-llama/llama-3.3-70b-instruct", "endpoints": [
            {"provider_name": "DeepInfra", "tag": "deepinfra/fp8", "quantization": "fp8"},
            {"provider_name": "Together", "tag": "together"},
            {"provider_name": "Google AI Studio"},                         # no tag: its name as a slug reads
            {"provider_name": "Bad", "tag": "bad slug"}, "not a row", {"tag": 7}]}}
        opener, seen = self.opener(body)
        got = providers.list_endpoints("openrouter", "meta-llama/llama-3.3-70b-instruct", env={}, opener=opener)
        self.assertEqual(got, [{"slug": "deepinfra/fp8", "name": "DeepInfra", "quantization": "fp8"},
                               {"slug": "google-ai-studio", "name": "Google AI Studio", "quantization": ""},
                               {"slug": "together", "name": "Together", "quantization": ""}])
        (req,) = seen
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/models/meta-llama/llama-3.3-70b-instruct/endpoints")
        self.assertIsNone(req.get_header("Authorization"))                  # no key set: the list is asked without one
        self.assertEqual(req.get_method(), "GET")

    def test_the_key_goes_as_for_the_model_list_and_a_bare_name_gets_its_maker(self):
        opener, seen = self.opener({"data": {"endpoints": []}})
        providers.list_endpoints("openrouter", "gpt-4o-mini", env={"OPENROUTER_API_KEY": "or-test"}, opener=opener)
        self.assertEqual(seen[0].full_url, "https://openrouter.ai/api/v1/models/openai/gpt-4o-mini/endpoints")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer or-test")

    def test_what_cannot_be_asked_or_read_is_said_in_fixed_words(self):
        for kw in ({"error": urllib.error.URLError("refused at https://openrouter.ai?key=x")}, {"body": {"error": "no"}},
                   {"body": {"data": {"endpoints": "none"}}}):
            with self.subTest(kw=kw):
                opener, _ = self.opener(**kw)
                with self.assertRaisesRegex(ValueError, r"^Provider list unavailable\. Enter a provider's slug by hand\.$"):
                    providers.list_endpoints("openrouter", "meta-llama/llama-3.3-70b-instruct", env={}, opener=opener)
        opener, seen = self.opener({})
        with self.assertRaisesRegex(ValueError, "Only OpenRouter"):
            providers.list_endpoints("openai", "gpt-4o", env={"OPENAI_API_KEY": "x"}, opener=opener)
        for model in ("no-maker", "a/b/../../c", "maker/model?x=1", ""):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "maker/model"):
                providers.list_endpoints("openrouter", model, env={}, opener=opener)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
