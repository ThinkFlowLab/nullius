"""The user's price list: nothing shipped, a public list fetched on request,
the user's own rows over it, a model priced by exactly its name."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from nullius import prices
from nullius.standard import run

# LiteLLM's shape: costs per token, a sample entry and an image model without an input price.
LISTING = {
    "sample_spec": {"input_cost_per_token": "a number", "max_output_tokens": "a number"},
    "model-a": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "cache_read_input_token_cost": 1.25e-06,
                "max_output_tokens": 16384, "litellm_provider": "one"},
    "model-b": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "cache_read_input_token_cost": 3e-07,
                "cache_creation_input_token_cost": 3.75e-06, "max_output_tokens": 128000},
    "embedder": {"input_cost_per_token": 2e-08, "output_cost_per_token": 0.0},
    "picture-model": {"output_cost_per_image": 0.04},
}


class Profile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-prices-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"NULLIUS_HOME": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NULLIUS_PRICES", None)

    def read(self):
        with open(prices.path()) as f:
            return f.read()

    def fetch(self, listing=LISTING):
        return lambda source: json.dumps(listing).encode()


class Nothing(Profile):
    def test_no_price_is_written_in_the_code(self):
        self.assertFalse(hasattr(run, "PRICES"))
        self.assertEqual(prices.table(), {})
        self.assertEqual(prices.state(), {"n": 0, "from": "", "on": "", "own": {}})

    def test_an_unreadable_file_is_no_prices_rather_than_a_run_that_cannot_start(self):
        os.makedirs(os.path.dirname(prices.path()))
        with open(prices.path(), "w") as f:
            f.write("{not json")
        self.assertEqual(prices.table(), {})

    def test_reading_the_list_never_fetches(self):
        prices.update(fetch=self.fetch())
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("the network was asked")):
            self.assertEqual(len(prices.table()), 3)
            prices.state()
            prices.given(["model-a"], prices.table())


class Update(Profile):
    def test_the_list_is_kept_per_million_tokens_with_its_source_and_day(self):
        got = prices.update(fetch=self.fetch(), today="2026-11-10")
        self.assertEqual(got, {"n": 3, "from": prices.SOURCE, "on": "2026-11-10"})
        self.assertEqual(prices.table(), {
            "model-a": {"input": 2.5, "output": 10.0, "cached": 1.25, "max_output": 16384},
            "model-b": {"input": 3.0, "output": 15.0, "cached": 0.3, "cache_write": 3.75, "max_output": 128000},
            "embedder": {"input": 0.02, "output": 0.0}})
        self.assertEqual(prices.path(), os.path.join(self.tmp, "config", "prices.json"))

    def test_a_list_that_is_not_one_changes_nothing(self):
        prices.update(fetch=self.fetch(), today="2026-11-10")
        before = self.read()
        for bad in (b"<html>", b"[]", json.dumps({"x": {"mode": "chat"}}).encode()):
            with self.assertRaises(ValueError):
                prices.update(fetch=lambda source, bad=bad: bad)
        with self.assertRaises(ValueError):
            prices.update(fetch=mock.Mock(side_effect=OSError("no route")))
        self.assertEqual(self.read(), before)

    def test_a_file_is_a_source_and_only_https_is_fetched(self):
        listing = os.path.join(self.tmp, "listing.json")
        with open(listing, "w") as f:
            json.dump(LISTING, f)
        self.assertEqual(prices.update(listing)["n"], 3)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("fetched")):
            for source in ("http://example.invalid/prices.json", "ftp://example.invalid/prices.json"):
                with self.assertRaises(ValueError) as e:
                    prices.update(source)
                self.assertEqual(str(e.exception).count(source), 1)          # the address once, not twice
        with self.assertRaises(ValueError) as e:
            prices.update(os.path.join(self.tmp, "none.json"))
        self.assertEqual(str(e.exception).count("none.json"), 1)

    def test_the_fetch_says_nothing_of_the_user(self):
        seen = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            seen.append(req)
            return Reply(json.dumps(LISTING).encode())
        with mock.patch("urllib.request.urlopen", urlopen):
            prices.update()
        self.assertEqual(seen[0].full_url, prices.SOURCE)
        self.assertEqual(dict(seen[0].header_items()), {"User-agent": "nullius"})
        self.assertIsNone(seen[0].data)


class Own(Profile):
    def test_the_users_own_price_wins_and_survives_an_update(self):
        prices.update(fetch=self.fetch())
        prices.set_own("model-a", prices.row(1, 2))
        prices.set_own("my-gateway/model", prices.row("0.5", "1.5", cached="0.05", max_output="4096"))
        prices.update(fetch=self.fetch())
        t = prices.table()
        self.assertEqual(t["model-a"], {"input": 1.0, "output": 2.0})
        self.assertEqual(t["my-gateway/model"], {"input": 0.5, "output": 1.5, "cached": 0.05, "max_output": 4096})
        self.assertTrue(prices.remove_own("model-a"))
        self.assertFalse(prices.remove_own("model-a"))
        self.assertEqual(prices.table()["model-a"]["input"], 2.5)             # the list's again

    def test_a_row_is_checked(self):
        for bad in ((-1, 2), ("x", 2), (1, None), (float("inf"), 1), (float("nan"), 1)):
            with self.assertRaises((ValueError, TypeError), msg=bad):
                prices.set_own("m", prices.row(*bad))
        with self.assertRaises(ValueError):
            prices.row(1, 2, max_output=0)
        with self.assertRaises(ValueError):
            prices.set_own(" ", prices.row(1, 2))

    def test_a_runs_price_is_model_input_output_and_optionally_cached(self):
        self.assertEqual(prices.parse("m=1,2"), ("m", {"input": 1.0, "output": 2.0}))
        self.assertEqual(prices.parse("org/m:free = 0.5, 1.5, 0.05"), ("org/m:free", {"input": 0.5, "output": 1.5, "cached": 0.05}))
        for bad in ("m", "m=1", "=1,2", "m=1,2,3,4", "m=1,x", "m=1,", "m=-1,2"):
            with self.assertRaises(ValueError, msg=bad):
                prices.parse(bad)

    def test_another_file_is_named_by_the_environment(self):
        other = os.path.join(self.tmp, "sent", "prices.json")
        with mock.patch.dict(os.environ, {"NULLIUS_PRICES": other}):
            prices.set_own("m", prices.row(1, 2))
            self.assertEqual(prices.table(), {"m": {"input": 1.0, "output": 2.0}})
        self.assertTrue(os.path.isfile(other))
        self.assertEqual(prices.table(), {})


class Exactly(Profile):
    def test_only_the_models_named_are_given_and_only_by_their_exact_name(self):
        t = {"model-a": {"input": 1.0, "output": 2.0}, "model-a-mini": {"input": 0.1, "output": 0.2}}
        self.assertEqual(prices.given(["model-a", None, "", "model-a", "model-a-2026"], t), {"model-a": t["model-a"]})
        self.assertEqual(prices.given(["model-a"], None), {})

    def test_a_run_is_priced_for_every_model_its_relays_send(self):
        r = run.Run.__new__(run.Run)
        r.model, r.judge_model, r.simulator_model, r.embed_model = "m", "judge", None, "embedder"
        r.other = {"model": {"side_models": ["side", "asked=sent", "m"]}}
        self.assertEqual(r.models_called(), ["m", "judge", "embedder", "side", "sent"])


class Cli(Profile):
    def say(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = prices.main(list(argv))
        return code, out.getvalue()

    def test_set_list_and_remove(self):
        self.assertEqual(self.say("set", "m", "1", "2", "--cached", "0.1")[0], 0)
        code, out = self.say("list", "m", "unknown")
        self.assertEqual(code, 0)
        self.assertIn("list: none yet - nullius prices update", out)
        self.assertIn("own  m: input 1, output 2, cached 0.1", out)
        self.assertIn("unknown: no price", out)
        got = json.loads(self.say("list", "m", "--json")[1])
        self.assertEqual((got["n"], got["models"]), (0, {"m": {"input": 1.0, "output": 2.0, "cached": 0.1}}))
        self.assertEqual(self.say("remove", "m")[0], 0)
        self.assertEqual(prices.table(), {})

    def test_an_update_from_a_file_says_how_many(self):
        listing = os.path.join(self.tmp, "listing.json")
        with open(listing, "w") as f:
            json.dump(LISTING, f)
        code, out = self.say("update", "--from", listing)
        self.assertEqual((code, out.strip()), (0, f"3 models priced, from {listing}"))
        self.assertIn(f"list: 3 models, from {listing} on ", self.say()[1])

    def test_a_bad_price_is_refused_with_its_reason(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as e:
            prices.main(["set", "m", "free", "2"])
        self.assertEqual(e.exception.code, 1)
        self.assertIn("input: a price in USD per million tokens", err.getvalue())


if __name__ == "__main__":
    unittest.main()
