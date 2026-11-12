"""The providers a run can start on, and the runners the builder offers:
Harbor and the Nullius standard. A recipe of a runner no longer offered (a
benchmark's own scripts, now its Original setup) is refused by one sentence,
the same in the page and the routes."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nullius import providers  # noqa: E402
from nullius.catalogue import catalog  # noqa: E402
from nullius.recipes import launch  # noqa: E402


class Providers(unittest.TestCase):
    def test_a_provider_is_available_once_its_setting_is_set_in_the_builders_order(self):
        self.assertEqual(providers.available({}), [])
        env = {"OPENAI_API_KEY": "k", "MODEL_API_URL": "http://127.0.0.1:8000/v1", "OPENROUTER_API_KEY": "k"}
        self.assertEqual(providers.available(env), ["openrouter", "openai", "custom"])
        self.assertEqual(providers.available({"MODEL_API_KEY": "k"}), [])          # a local endpoint needs its URL, not its key

    def test_each_named_endpoint_is_a_choice_by_its_id_listed_by_its_name(self):
        env = {"OPENAI_API_KEY": "k", "MODEL_API_URL": "http://127.0.0.1:8000/v1",
               "MODEL_GPU_NAME": "GPU box", "MODEL_GPU_API_URL": "http://gpu:8000/v1",
               "MODEL_A_LAPTOP_NAME": "Laptop", "MODEL_A_LAPTOP_API_URL": "http://laptop:11434/v1",
               "MODEL_KEYONLY_API_KEY": "k"}                                       # no URL: not one a run can be sent to
        self.assertEqual(providers.endpoints(env), [{"id": "", "name": "OpenAI-compatible"}, {"id": "GPU", "name": "GPU box"},
                                                    {"id": "A_LAPTOP", "name": "Laptop"}])
        self.assertEqual(providers.available(env), ["openai", "custom", "custom:GPU", "custom:A_LAPTOP"])
        self.assertEqual([providers.label(c, env) for c in providers.available(env)], ["OpenAI", "OpenAI-compatible", "GPU box", "Laptop"])
        self.assertEqual(providers.needs("custom:GPU"), "MODEL_GPU_API_URL"); self.assertEqual(providers.needs("custom"), "MODEL_API_URL")
        self.assertEqual(providers.endpoint_vars("GPU"), ("MODEL_GPU_API_URL", "MODEL_GPU_API_KEY"))
        self.assertEqual(providers.endpoint_vars(""), ("MODEL_API_URL", "MODEL_API_KEY"))
        self.assertEqual((providers.choice("custom", "GPU"), providers.choice("openai", "GPU")), ("custom:GPU", "openai"))
        self.assertEqual(providers.split("custom:GPU"), ("custom", "GPU"))
        for odd in ("custom:gpu", "custom:../x", "openai:GPU", "custom:"):
            self.assertNotIn(providers.split(odd)[0], providers.PROVIDERS, odd)   # no provider at all: refused, never another endpoint
        self.assertEqual(providers.label("custom:GONE", env), "GONE")             # a removed endpoint: named by its id
        self.assertEqual(providers.endpoints({"MODEL_GPU_API_URL": "http://gpu/v1"}), [{"id": "GPU", "name": "GPU"}])   # unnamed: its id


    def test_an_official_model_is_named_as_openrouter_names_it(self):
        """LongMemEval's judge and tau3-bench's simulator are written as OpenAI
        spells them; OpenRouter refuses a name without its maker."""
        self.assertEqual(providers.served_as("gpt-4o-2024-08-06", "openrouter"), "openai/gpt-4o-2024-08-06")
        self.assertEqual(providers.served_as("claude-sonnet-4", "openrouter"), "anthropic/claude-sonnet-4")
        for model, provider in (("gpt-4o-2024-08-06", "openai"), ("gpt-4o", "custom"),
                                ("openai/gpt-4o", "openrouter"), ("some-local-model", "openrouter")):
            self.assertEqual(providers.served_as(model, provider), model)


class Runners(unittest.TestCase):
    def test_the_builder_offers_harbor_and_nullius_and_says_why_another_is_gone(self):
        data = launch.payload(catalog.load(), [])
        self.assertEqual([r["key"] for r in data["runners"]], ["standard", "harbor"])
        self.assertEqual(data["runnerGone"], providers.GONE)
        self.assertIn("Original setup", providers.GONE)


if __name__ == "__main__":
    unittest.main()
