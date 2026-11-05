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
