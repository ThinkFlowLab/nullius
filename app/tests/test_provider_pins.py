"""Serving pins alongside the protocol registry and the relay's route authentication."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from nullius import providers
from nullius.standard import cli
from nullius.standard.calls import relay
from nullius.standard.pages import recipe
from nullius.standard.runner import run
from tests import test_standard_recipe_served_by as recipes
from tests import test_standard_served_by as calls


class RegistryPins(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.defs = recipes.defs()
        self.bench = recipes.fakebench()
        self.agent = next(a for a in self.defs["agent"] if a["key"] == "fake")
        self.agent = dict(self.agent, model={**self.agent.get("model", {}), "apis": ["messages"]})
        self.defs["agent"] = [self.agent if a["key"] == "fake" else a for a in self.defs["agent"]]

    def test_openrouter_protocol_checks_and_serving_pins_both_apply(self):
        env = {"OPENROUTER_API_KEY": "fixture-key"}
        options = dict(model="m", provider="openrouter", served_by={"m": ["prov-a"]},
                       env=env, unsandboxed=True, jobs_root=self.tmp)
        with self.assertRaisesRegex(run.Refused, "does not serve"):
            run.Run(self.bench, self.agent, **options)
        env["OPENROUTER"] = json.dumps({"endpoints": {"messages": "/messages"}})
        started = run.Run(self.bench, self.agent, **options)
        self.assertEqual((started.base, started.key_header, started.serving),
                         (providers.PRESETS["openrouter"]["base"], "x-api-key", {"m": ["prov-a"]}))
        with patch.dict(os.environ, env):
            compiled = recipe.compile(recipes.a_recipe(model="m", served_by={"model": ["prov-a"]}), self.defs)
        self.assertEqual(compiled["errors"], [])
        self.assertEqual(compiled["served_models"]["model"], "m")
        self.assertIn("--served-by m=prov-a", compiled["script"])

    def test_a_named_custom_protocol_provider_still_cannot_take_a_pin(self):
        for base in ("https://gateway.example/v1", "https://openrouter.ai/api/v1"):
            env = {"MODEL_GATEWAY": json.dumps({"base": base, "endpoints": {"messages": "/messages"}}),
                   "MODEL_GATEWAY_API_KEY": "fixture-key"}
            self.assertEqual(providers.protocol_for("custom:GATEWAY", ["chat", "messages"], env), "messages")
            handed = cli.run_env("custom", {"OPENAI_BASE_URL": providers.base_of("MODEL_GATEWAY_API_URL", env),
                                            "OPENAI_API_KEY": env["MODEL_GATEWAY_API_KEY"]})
            with self.subTest(base=base), self.assertRaisesRegex(run.Refused, "no serving providers"):
                run.Run(self.bench, self.agent, model="m", provider="custom", env=handed,
                        served_by={"m": ["prov-a"]}, unsandboxed=True, jobs_root=self.tmp)
            with patch.dict(os.environ, env):
                compiled = recipe.compile(recipes.a_recipe(provider="custom", endpoint="GATEWAY", model="m",
                                                          served_by={"model": ["prov-a"]}), self.defs)
            self.assertTrue(any("Served by needs OpenRouter" in e["message"] for e in compiled["errors"]))
            out = io.StringIO()
            with patch.object(cli, "_bench", return_value=self.bench), patch.object(cli, "_other", return_value=self.agent), \
                    patch.object(cli, "_exit_on_term"), patch.object(cli.settings, "load"), \
                    patch.dict(os.environ, {"OPENAI_BASE_URL": base, "OPENAI_API_KEY": "fixture-key"}), redirect_stdout(out):
                code = cli.main(["run", "fakebench", "fake", "--provider", "custom", "--from-run", "--model", "m",
                                 "--served-by", "prov-a", "--unsandboxed", "--jobs", self.tmp])
            self.assertEqual(code, 2)
            self.assertIn("no serving providers", out.getvalue())


class ProtocolRouting(unittest.TestCase):
    def test_pinned_agent_and_benchmark_routes_keep_their_own_authentication(self):
        pins = {m: ["prov-a"] for m in ("m", "judge", "simulator", "embedding")}
        folder = self.enterContext(tempfile.TemporaryDirectory())
        r = relay.Relay("https://openrouter.ai/api/v1", "fixture-key", folder,
                        model="m", key_header="x-api-key", serving=pins)
        self.addCleanup(r.stop)
        r.public = "http://relay"
        pool = self.enterContext(patch.object(r, "pool"))
        routes = [("unit", None, None, "/messages"), ("unit-judge", "judge", None, "/chat/completions"),
                  ("unit-simulator", "simulator", None, "/responses"), ("search", None, "embedding", "/embeddings")]
        for tag, model, embedding, path in routes:
            with self.subTest(path=path):
                r.url(tag, model=model, embeddings=embedding)
                raw = json.dumps(dict(calls.body_for(path, False), provider={"sort": "price"})).encode()
                handler = Mock(path=f"/{tag}/v1{path}", command="POST", headers={"Content-Length": str(len(raw))},
                               rfile=io.BytesIO(raw), wfile=io.BytesIO())
                body = json.dumps(calls.SHAPES.get((path, False), (calls.CHAT,))[0]).encode()
                response = Mock(status=200, headers={"Content-Type": "application/json"})
                response.getheader.return_value = "application/json"
                response.read.return_value = body
                pool.send.return_value = (Mock(), response)
                r._forward(handler)
                headers = {k.lower(): v for k, v in pool.send.call_args.args[3].items()}
                if tag == "unit":
                    self.assertEqual(headers["x-api-key"], "fixture-key")
                    self.assertEqual(headers["anthropic-version"], relay.ANTHROPIC_VERSION)
                    self.assertNotIn("authorization", headers)
                else:
                    self.assertEqual(headers["authorization"], "Bearer fixture-key")
                    self.assertNotIn("x-api-key", headers)
                    self.assertNotIn("anthropic-version", headers)
                self.assertEqual(headers.get("x-openrouter-metadata"),
                                 "enabled" if path in ("/chat/completions", "/responses") else None)
                record = r.calls(tag)[-1]
                self.assertEqual(record["request"]["routing"], relay.routing(["prov-a"]))
                self.assertEqual(record["request"]["routing_asked"], {"sort": "price"})
                if path != "/embeddings":
                    self.assertTrue(record["reply"].get("served_by"))


if __name__ == "__main__":
    unittest.main()
