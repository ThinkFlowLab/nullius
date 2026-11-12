"""Settings' provider Test: GETs that cost nothing, pinned per provider, mapped to one line, kept without a key."""
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from nullius import providers

KEY = "sk-synthetic-provider-key-0123456789"
MODELS = json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode()


class Fake:
    """An opener that records every request and answers by URL: bytes, an HTTP status, or an exception."""

    def __init__(self, answers, wrong=200):
        self.answers, self.wrong, self.seen = answers, wrong, []

    def __call__(self, req, timeout):
        auth = req.get_header("Authorization") or ""
        self.seen.append((req.get_method(), req.full_url, auth or req.get_header("X-api-key"), req.data))
        got = self.answers[req.full_url]
        if "nullius-wrong-" in auth:
            got = self.wrong
        if isinstance(got, BaseException):
            raise got
        if isinstance(got, int) and got != 200:
            raise urllib.error.HTTPError(req.full_url, got, "no", {}, io.BytesIO(KEY.encode()))
        return io.BytesIO(MODELS if got == 200 else got)


class Requests(unittest.TestCase):
    def test_each_provider_sends_only_its_free_gets(self):
        env = {"OPENROUTER_API_KEY": KEY, "OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": KEY,
               "MODEL_GPU_API_URL": "http://gpu:8000/v1", "MODEL_GPU_API_KEY": KEY}
        expected = {
            "openrouter": [("GET", "https://openrouter.ai/api/v1/models", None, None),
                           ("GET", "https://openrouter.ai/api/v1/key", "Bearer " + KEY, None)],
            "openai": [("GET", "https://api.openai.com/v1/models", "Bearer " + KEY, None)],
            "anthropic": [("GET", "https://api.anthropic.com/v1/models?limit=1000", KEY, None)],
            "custom:GPU": [("GET", "http://gpu:8000/v1/models", "Bearer " + KEY, None), ("GET", "http://gpu:8000/v1/models", "wrong", None)],
        }
        for choice, sent in expected.items():
            fake = Fake({url: 200 if url.endswith(("models", "1000")) else b'{"data": {}}' for _, url, _, _ in sent}, wrong=401)
            got = providers.test(choice, env, opener=fake)
            seen = [(m, u, "wrong" if a and "nullius-wrong-" in a else a, d) for m, u, a, d in fake.seen]
            self.assertEqual(seen, sent, choice)
            self.assertEqual((got["ok"], got["reached"], got["key"], got["models"]), (True, True, "accepted", 2), choice)

    def test_anthropic_sends_its_key_and_version_headers(self):
        seen = []
        def fetch(req, timeout):
            seen.append(dict(req.header_items()))
            return io.BytesIO(MODELS)
        providers.test("anthropic", {"ANTHROPIC_API_KEY": KEY}, opener=fetch)
        self.assertEqual((seen[0]["X-api-key"], seen[0]["Anthropic-version"]), (KEY, "2023-06-01"))
        self.assertNotIn("Authorization", seen[0])

    def test_typesafe_and_incomplete_settings_send_nothing(self):
        def forbidden(*a, **kw):
            self.fail("must not fetch")
        for choice, env in [("typesafe", {"TYPESAFE_API_KEY": KEY}), ("openai", {}), ("anthropic", {}), ("openrouter", {}),
                            ("custom", {}), ("custom", {"MODEL_API_URL": "https://u:p@example.org/v1"})]:
            with self.assertRaises(ValueError, msg=choice):
                providers.test(choice, env, opener=forbidden)


class Results(unittest.TestCase):
    URL = "http://lab:8000/v1/models"

    def run_one(self, answer, env=None, wrong=401):
        return providers.test("custom", env or {"MODEL_API_URL": "http://lab:8000/v1", "MODEL_API_KEY": KEY}, opener=Fake({self.URL: answer}, wrong))

    def test_answers_map_to_one_state_each(self):
        cases = {
            200: (True, True, "accepted", ""),
            401: (False, True, "refused", ""),
            403: (False, True, "refused", ""),
            500: (False, True, "not tested", "HTTP 500"),
            b"not json": (False, True, "not tested", "no model list"),
            urllib.error.URLError(socket.timeout("timed out")): (False, False, "not tested", "timed out"),
            TimeoutError(): (False, False, "not tested", "timed out"),
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")): (False, False, "not tested", "name not resolved"),
            urllib.error.URLError(ConnectionRefusedError(111, "refused")): (False, False, "not tested", "connection refused"),
        }
        for answer, want in cases.items():
            got = self.run_one(answer)
            self.assertEqual((got["ok"], got["reached"], got["key"], got["error"]), want, answer)

    def test_a_wrong_key_also_accepted_means_the_server_checks_none(self):
        got = self.run_one(200, wrong=200)
        self.assertEqual((got["ok"], got["key"]), (True, "not checked"))
        self.assertEqual(providers.mark(got), "warn")
        self.assertIn("does not check keys", providers.said(got))
        # no key at all: one request, and the answer already says it
        fake = Fake({self.URL: 200})
        got = providers.test("custom", {"MODEL_API_URL": "http://lab:8000/v1"}, opener=fake)
        self.assertEqual((len(fake.seen), got["key"]), (1, "not checked"))

    def test_lines(self):
        self.assertTrue(providers.said(self.run_one(200)).startswith("Key accepted · 2 models · "))
        self.assertTrue(providers.said(self.run_one(401)).startswith("Key refused · "))
        self.assertTrue(providers.said(self.run_one(TimeoutError())).startswith("Not reached: timed out · "))
        self.assertEqual([providers.mark(self.run_one(a)) for a in (200, 401)], ["ok", "bad"])


class Kept(unittest.TestCase):
    def test_results_are_kept_by_choice_without_a_key_and_forgotten(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"NULLIUS_HOME": home}):
            boom = urllib.error.HTTPError(Results.URL, 401, KEY, {}, io.BytesIO(KEY.encode()))
            got = providers.test("custom", {"MODEL_API_URL": "http://lab:8000/v1", "MODEL_API_KEY": KEY}, opener=Fake({Results.URL: boom}))
            providers.remember({"custom": got, "openai": dict(got)})
            text = Path(home, "data", "provider-tests.json").read_text()
            self.assertNotIn(KEY, text)
            self.assertNotIn("synthetic", text)
            providers.remember({"openai": None})
            self.assertEqual(list(providers.tests()), ["custom"])


if __name__ == "__main__":
    unittest.main()
