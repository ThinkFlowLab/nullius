"""The provider registry: one record per provider, merged in three layers,
read by every consumer for the protocol it speaks. No request is sent."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nullius import migrate, onboarding, paths, providers, settings
from nullius.judge import client
from nullius.recipes.launch import payload
from nullius.catalogue import catalog
from nullius.standard.runner import run

KEYS = {"OPENROUTER_API_KEY": "sk-or-fixture-0001", "OPENAI_API_KEY": "sk-oa-fixture-0002",
        "ANTHROPIC_API_KEY": "sk-ant-fixture-0003", "MODEL_GW_API_KEY": "sk-gw-fixture-0004"}


class Records(unittest.TestCase):
    def test_a_preset_is_its_fixed_fields_and_the_user_adds_only_other_protocols(self):
        env = {"OPENROUTER": json.dumps({"endpoints": {"messages": "/messages"}})}
        rec = providers.record("openrouter", env)
        self.assertEqual((rec["name"], rec["base"], rec["key"]), ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
        self.assertEqual(providers.url(rec, "messages"), "https://openrouter.ai/api/v1/messages")
        self.assertEqual(providers.url(rec, "decisions"), "https://openrouter.ai/api/alpha/decisions")     # outside its base: a full URL
        for fixed in ({"base": "https://proxy/v1"}, {"name": "X"}, {"key": "OTHER_API_KEY"}, {"endpoints": {"chat": "/x"}}):
            self.assertIn("fixed", providers.record_problem("OPENROUTER", json.dumps(fixed)), fixed)
            self.assertEqual(providers.record("openrouter", {"OPENROUTER": json.dumps(fixed)})["base"], "https://openrouter.ai/api/v1")

    def test_a_preset_adds_a_full_url_only_on_its_own_host(self):
        for name, proto, bad in (("TYPESAFE", "decisions", "https://evil.example/d"), ("OPENROUTER", "messages", "https://evil.example/m"),
                                 ("OPENAI", "messages", "https://api.openai.com.evil.example/m")):
            why = providers.record_problem(name, json.dumps({"endpoints": {proto: bad}}))
            self.assertIn("the preset's key is sent there", why or "", name)
            self.assertNotIn("evil", providers.url(providers.record(name.lower(), {name: json.dumps({"endpoints": {proto: bad}})}), proto))
        self.assertIsNone(providers.record_problem("OPENROUTER", json.dumps({"endpoints": {"messages": "https://openrouter.ai/api/v1/messages"}})))
        self.assertIsNone(providers.record_problem("TYPESAFE", json.dumps({"endpoints": {"decisions": "/decisions"}})))

    def test_a_custom_record_merges_per_protocol_and_null_removes(self):
        file = {"name": "Gateway", "base": "https://gw.example/v1", "endpoints": {"chat": "/chat/completions", "decisions": "/alpha/decisions"}}
        env = {"endpoints": {"decisions": None, "responses": "https://eu.gw.example/v1/responses"}}
        merged = settings.merge_patch(file, env)
        self.assertEqual(merged["endpoints"], {"chat": "/chat/completions", "responses": "https://eu.gw.example/v1/responses"})
        rec = providers.record("custom:GW", {"MODEL_GW": json.dumps(settings.compose(file, env))})
        self.assertEqual(rec["endpoints"], merged["endpoints"]); self.assertEqual(rec["key"], "MODEL_GW_API_KEY")
        self.assertEqual(providers.url(rec, "decisions"), "https://gw.example/v1/decisions")              # removed: the default path
        self.assertEqual(settings.merge_patch({"a": {"b": 1}}, {"a": None}), {})

    def test_a_path_is_joined_with_one_slash_and_a_url_stands_alone(self):
        self.assertEqual(providers.join("https://gw.example/v1/", "/chat/completions"), "https://gw.example/v1/chat/completions")
        self.assertEqual(providers.join("https://gw.example/v1", "/chat"), "https://gw.example/v1/chat")      # /v1 kept: not URL resolution
        self.assertEqual(providers.join("https://gw.example/v1", "https://other.example/d"), "https://other.example/d")
        good = lambda e: providers.record_problem("MODEL_GW", json.dumps({"endpoints": {"chat": e}}))
        self.assertIsNone(good("/v2/chat")); self.assertIsNone(good("https://other.example/chat"))
        for bad in ("chat/completions", "//evil.example/x", "/a b", "ftp://x/y", "https://u:p@x/y", "https://x/y?api_key=1", 7, "/v1/../../x", "/.."):
            self.assertIsNotNone(good(bad), bad)
        self.assertIn("some of chat", providers.record_problem("MODEL_GW", json.dumps({"endpoints": {"carrier-pigeon": "/x"}})))
        self.assertIn("MODEL_GW_API_KEY", providers.record_problem("MODEL_GW", json.dumps({"key": "OPENAI_API_KEY"})))
        self.assertIsNone(providers.record_problem("MODEL_GW", json.dumps({"key": "MODEL_GW_API_KEY", "base": "https://gw/v1"})))
        self.assertIn("JSON object", providers.record_problem("MODEL_GW", "https://gw/v1"))

    def test_a_base_on_a_presets_host_suggests_its_endpoints(self):
        self.assertEqual(set(providers.KNOWN_HOSTS), {"openrouter.ai", "api.openai.com", "api.anthropic.com", "api.typesafe.ai"})
        got = providers.suggestions("https://user:pw@openrouter.ai/api/v1")
        self.assertEqual(got["decisions"], "https://openrouter.ai/api/alpha/decisions")                 # never the user or password
        self.assertEqual(providers.suggestions("https://api.anthropic.com/v1"), {"messages": "https://api.anthropic.com/v1/messages"})
        self.assertEqual(providers.suggestions("https://gw.example/v1"), {})

    def test_old_settings_are_read_and_the_record_wins(self):
        env = {"MODEL_GPU_NAME": "GPU box", "MODEL_GPU_API_URL": "http://gpu:8000/v1"}
        rec = providers.record("custom:GPU", env)
        self.assertEqual((rec["name"], rec["base"], rec["ignored"]), ("GPU box", "http://gpu:8000/v1", []))
        rec = providers.record("custom:GPU", dict(env, MODEL_GPU=json.dumps({"base": "http://new:8000/v1"})))
        self.assertEqual((rec["name"], rec["base"], rec["ignored"]), ("GPU box", "http://new:8000/v1", ["MODEL_GPU_API_URL"]))
        self.assertEqual(providers.record("custom", {"MODEL_API_URL": "http://box/v1"})["base"], "http://box/v1")
        self.assertEqual(settings.endpoint_ids(dict(env, MODEL_GW="{}")), ["GPU", "GW"])

    def test_a_reserved_name_in_agent_keys_leaves_the_others_in_use(self):
        env = {"AGENT_KEYS": "CURSOR_API_KEY, OPENAI,GH_TOKEN,MODEL_GW", "CURSOR_API_KEY": "c", "GH_TOKEN": "g"}
        self.assertEqual(settings.agent_key_names(env), ["CURSOR_API_KEY", "GH_TOKEN"])
        self.assertEqual(settings.agent_key_values(env), {"CURSOR_API_KEY": "c", "GH_TOKEN": "g"})
        self.assertEqual(settings.reserved_agent_keys(env), ["OPENAI", "MODEL_GW"])

    def test_a_shell_variable_named_like_a_record_is_no_provider(self):
        env = {"MODEL_NAME": "gpt-4", "MODEL_PATH": "/models", "MODEL_API_URL": "http://box/v1", "MODEL_X_API_KEY": "k"}
        self.assertEqual(settings.endpoint_ids({k: v for k, v in env.items() if k != "MODEL_X_API_KEY"}), [])
        self.assertFalse(settings.is_record("MODEL_NAME")); self.assertFalse(settings.is_record("MODEL_API_URL"))
        self.assertFalse(settings.is_record("MODEL_GPU_API_KEY")); self.assertTrue(settings.is_record("MODEL_GPU"))
        self.assertNotEqual(settings.new_endpoint_id("My name", set()), "MY_NAME")                     # its record would read as an old setting

    def test_no_key_is_ever_in_a_record_the_builder_or_its_payload(self):
        env = dict(KEYS, MODEL_GW=json.dumps({"name": "GW", "base": "https://gw.example/v1"}))
        with patch.dict(os.environ, env):
            shown = json.dumps([providers.records(env), providers.run_choices(env), payload(catalog.load(), [])["providers"]])
        for key in KEYS.values():
            self.assertNotIn(key, shown)
        self.assertIn("MODEL_GW_API_KEY", shown)                                                       # the setting's name, never its value


class Consumers(unittest.TestCase):
    def test_a_consumer_gets_the_providers_that_may_serve_its_protocol(self):
        self.assertEqual(providers.offered(["chat"], {}), ["openrouter", "openai", "custom"])          # Anthropic: not for a chat-only agent
        self.assertEqual(providers.offered(["messages"], {}), ["anthropic", "custom"])
        self.assertEqual(providers.offered(["responses"], {}), ["openrouter", "openai", "custom"])
        env = dict(KEYS, MODEL_GW=json.dumps({"base": "https://gw/v1", "endpoints": {"decisions": "/d"}}), MODEL_LAB=json.dumps({"base": "http://lab/v1"}))
        self.assertEqual(list(client._table(["decisions"], env, known=False)), ["openrouter", "custom:GW", "custom:LAB"])
        self.assertEqual(list(client.decision_judge_providers(env)), ["openrouter", "openai", "typesafe", "custom:GW"])
        px = dict(env, MODEL_PX=json.dumps({"name": "PX", "base": "https://px/v1", "endpoints": {"messages": "/messages"}}))
        self.assertEqual(client.llm_providers(px)["custom:PX"]["protocol"], "messages")              # the one it lists, before chat
        self.assertEqual(providers.protocol_for("anthropic", ["chat", "messages"], env), "messages")
        self.assertEqual(run.key_header("anthropic", {"model": {"apis": ["messages"]}}, env), "x-api-key")
        self.assertEqual(run.key_header("openrouter", {"model": {"apis": ["chat"]}}, env), "authorization")
        with self.assertRaisesRegex(run.Refused, "Anthropic does not serve what reference speaks"):
            run.key_header("anthropic", {"key": "reference", "model": {"apis": ["chat"]}}, env)     # as recipe.compile refuses it

    def test_each_judge_setting_takes_only_a_provider_that_judge_can_use(self):
        s1, llm = settings.BY_NAME["DECISION_PROVIDER"].validate, settings.BY_NAME["LLM_JUDGE_PROVIDER"].validate
        for ok in ("typesafe", "openrouter", "openai", "custom:GW", "local:GW"):
            self.assertIsNone(s1(ok), ok)
        for ok in ("openrouter", "openai", "anthropic", "custom", "custom:GW", "local"):
            self.assertIsNone(llm(ok), ok)
        self.assertIsNotNone(s1("anthropic")); self.assertIsNotNone(s1("custom")); self.assertIsNotNone(llm("typesafe"))

    def test_the_llm_judge_falls_back_to_anthropic_last(self):
        env = {"ANTHROPIC_API_KEY": "k", "MODEL_API_URL": "http://localhost:1/v1"}
        self.assertEqual(client.llm_judge(env).provider, "custom")
        self.assertEqual(client.llm_judge({"ANTHROPIC_API_KEY": "k"}).provider, "anthropic")
        self.assertEqual(client.llm_judge(dict(env, OPENAI_API_KEY="k")).provider, "openai")

    def test_a_run_reads_its_base_and_key_from_the_record(self):
        self.assertEqual(run.provider_endpoint("anthropic", KEYS), ("https://api.anthropic.com/v1", KEYS["ANTHROPIC_API_KEY"]))
        with self.assertRaises(run.Refused):
            run.provider_endpoint("typesafe", KEYS)                                                     # no run provider
        env = {"MODEL_GW": json.dumps({"base": "https://gw.example/v1"})}
        self.assertEqual(providers.base_of("MODEL_GW_API_URL", env), "https://gw.example/v1")
        self.assertEqual(providers.available(KEYS), ["openrouter", "openai", "anthropic"])

    def test_an_anthropic_only_user_is_set_up(self):
        self.assertTrue(onboarding.facts({"ANTHROPIC_API_KEY": "k"}, docker=lambda: False)["provider"])
        self.assertTrue(onboarding.facts({"MODEL_GW": json.dumps({"base": "http://gw/v1"})}, docker=lambda: False)["provider"])
        self.assertFalse(onboarding.facts({}, docker=lambda: False)["provider"])
        self.assertFalse(onboarding.facts({"TYPESAFE_API_KEY": "k"}, docker=lambda: False)["provider"])     # the judge's, not a run's
        self.assertTrue(onboarding.facts({"MODEL_API_URL": "http://box/v1"}, docker=lambda: False)["provider"])


class Layers(unittest.TestCase):
    """settings.load: the environment's partial record is merged over the file's; only its fields lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "settings.json")
        env = patch.dict(os.environ, {"MODEL_GW": json.dumps({"base": "https://gw-eu.example/v1", "endpoints": {"decisions": None}})})
        env.start(); self.addCleanup(env.stop)
        for kept in (settings.LOADED_KEYS, settings.ENV_PART, settings._FROM):
            saved = kept.copy()
            self.addCleanup(lambda k=kept, s=saved: (k.clear(), k.update(s)))

    def test_the_environment_sets_only_its_fields(self):
        Path(self.file).write_text(json.dumps({"MODEL_GW": json.dumps({"name": "Gateway", "base": "https://gw.example/v1",
                                                                      "endpoints": {"chat": "/c", "decisions": "/d"}})}))
        settings.load(self.file)
        rec = providers.record("custom:GW")
        self.assertEqual((rec["name"], rec["base"], rec["endpoints"]), ("Gateway", "https://gw-eu.example/v1", {"chat": "/c"}))
        self.assertEqual(settings.locked("MODEL_GW"), ["base", "endpoints.decisions"])
        state = settings.state(settings.endpoint_variable("MODEL_GW"), settings.in_file(self.file))
        self.assertEqual((state["source"], state["locked"], state["shadowed"]), ("environment", ["base", "endpoints.decisions"], False))
        settings.save(self.file, "MODEL_GW", json.dumps({"name": "Renamed", "base": "https://gw.example/v1"}))
        self.assertEqual(providers.record("custom:GW")["name"], "Renamed")                              # the file's field changes now
        self.assertEqual(providers.record("custom:GW")["base"], "https://gw-eu.example/v1")              # the environment's still wins
        Path(self.file).write_text(json.dumps({"MODEL_GW": json.dumps({"name": "By hand"})}))
        settings.refresh(self.file)
        self.assertEqual(providers.record("custom:GW")["name"], "By hand")


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"NULLIUS_HOME": self.tmp.name}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.target = Path(paths.settings_file())
        self.target.parent.mkdir(parents=True)

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = migrate.main(["--providers", *args])
        self.assertNotIn("sk-gpu-fixture", out.getvalue())
        return code, out.getvalue()

    def test_an_old_name_as_long_as_it_could_be_moves_whole(self):
        self.target.write_text(json.dumps({"MODEL_GPU_NAME": "N" * 400, "MODEL_GPU_API_URL": "http://gpu:8000/v1"}))
        self.assertEqual(self.cli("--apply")[0], 0)
        self.assertEqual(json.loads(json.loads(self.target.read_text())["MODEL_GPU"])["name"], "N" * 400)    # never cut

    def test_old_names_move_into_records_on_apply_only(self):
        before = {"MODEL_GPU_NAME": "GPU box", "MODEL_GPU_API_URL": "http://gpu:8000/v1", "MODEL_GPU_API_KEY": "sk-gpu-fixture",
                  "MODEL_NEW": json.dumps({"base": "http://new/v1"}), "MODEL_NEW_NAME": "ignored name", "MODEL_API_URL": "http://box/v1"}
        self.target.write_text(json.dumps(before))
        code, said = self.cli()
        self.assertEqual(code, 0); self.assertIn("Preview only", said); self.assertEqual(json.loads(self.target.read_text()), before)
        code, said = self.cli("--apply")
        self.assertEqual(code, 0)
        after = json.loads(self.target.read_text())
        self.assertEqual(json.loads(after["MODEL_GPU"]), {"name": "GPU box", "base": "http://gpu:8000/v1"})
        self.assertEqual(json.loads(after["MODEL_NEW"]), {"base": "http://new/v1", "name": "ignored name"})
        self.assertEqual(after["MODEL_GPU_API_KEY"], "sk-gpu-fixture"); self.assertEqual(after["MODEL_API_URL"], "http://box/v1")   # keys keep their names
        self.assertFalse({"MODEL_GPU_NAME", "MODEL_GPU_API_URL", "MODEL_NEW_NAME"} & set(after))
        self.assertEqual(self.cli()[1].splitlines()[1], "No old provider settings.")


if __name__ == "__main__":
    unittest.main()
