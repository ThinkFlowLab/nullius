"""GSM8K and HumanEval as published: the original setup of each, which asks
any model in the paper's place since the papers' own models were never
released, and says so (``comparable: false``); the temperature option of
each, the paper's decoding; and the two Harbor benchmarks' published agent.

The definition checks need nothing installed. The drivers' checks need each
upstream under ``STANDARD_DIR`` (a run or ``cli prove`` fetches it) and are
skipped without it; their model is a local fake endpoint, no key, no network.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nullius.catalogue import catalog
from nullius.standard import definitions, install, jsonl

APP = os.path.dirname(os.path.dirname(os.path.abspath(definitions.CATALOGUE)))


class Definitions(unittest.TestCase):
    def test_each_original_takes_any_model_and_is_not_the_papers(self):
        for key in ("gsm8k", "humaneval"):
            b = definitions.get("benchmark", key)
            with open(os.path.join(b["_here"], "standard.json")) as f:
                self.assertEqual(definitions.problems(json.load(f), "benchmark"), [], key)
            self.assertEqual(definitions.settings_problems([b]), [], key)
            o, why = definitions.setup(b, "original", "any-model", "openai")
            self.assertEqual(why, "")
            self.assertEqual((o["asked"], o["sent"], o["comparable"]), ("any-model", "any-model", False), key)
            self.assertIsNone(definitions.setup(b, "model", "x", "openai")[0], "no model setup: the original takes any already")
            self.assertEqual(b["options"]["temperature"]["setups"], ["original"], key)
            self.assertEqual(b["options"]["temperature"]["default"], "default", key)

    def test_a_setup_that_says_comparable_must_say_it_as_a_boolean(self):
        b = definitions.get("benchmark", "gsm8k")
        with open(os.path.join(b["_here"], "standard.json")) as f:
            d = json.load(f)
        d["setups"]["original"]["comparable"] = "no"
        self.assertTrue(any("setups.original.comparable" in p for p in definitions.problems(d, "benchmark")))

    def test_the_harbor_benchmarks_name_their_published_agent(self):
        cat = catalog.load()
        self.assertEqual(catalog.validate(cat), [])
        self.assertEqual(cat.benchmark("terminal-bench").published["agent"], "terminus-2")
        self.assertEqual(cat.benchmark("swe-bench-verified").published["agent"], "swe-agent")
        for b in cat.benchmarks:
            if b.published:
                self.assertIn("harbor", b.harness, b.key)
        b = cat.benchmark("terminal-bench")
        b.published = {"agent": "terminus-2"}
        self.assertTrue(any("published" in p for p in catalog.validate(cat)))


class Echo(BaseHTTPRequestHandler):
    """A model that answers every problem the same way, and keeps what it was asked."""
    asked = []
    reply = ""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Echo.asked.append(body)
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": Echo.reply}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(out.__len__()))
        self.end_headers()
        self.wfile.write(out)


def driver_env(bench: dict, extra: dict) -> dict:
    """The driver's environment as a run gives it, or None when its upstream is not fetched."""
    up = install.upstream_dir(bench)
    if not up or not os.path.isdir(up):
        return None
    data = os.path.join(up, bench["datasets"][0]["path"])
    if not os.path.isfile(data):
        return None
    return dict(os.environ, NULLIUS_UPSTREAM=up, NULLIUS_DATASET=data, NULLIUS_DATASET_ID=bench["datasets"][0]["id"],
                PYTHONPATH=APP, PYTHONDONTWRITEBYTECODE="1", **extra)


class Drivers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Echo.asked.clear()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def driver(self, key: str, temperature: str = "default"):
        bench = definitions.get("benchmark", key)
        env = driver_env(bench, {"NULLIUS_OPTION_TEMPERATURE": temperature})
        if env is None:
            self.skipTest(f"{key}'s upstream is not fetched")
        try:
            cmd, extra = install.driver_argv(bench)
        except install.InstallFailed as e:
            self.skipTest(f"{key}'s driver environment is not installed: {e}")
        env.update(extra)
        return jsonl.Proc(cmd, bench["_here"], env, os.path.join(self.tmp, "driver.log")).start(), bench

    def test_gsm8k_asks_the_problem_once_and_scores_the_reply_as_published(self):
        d, bench = self.driver("gsm8k")
        try:
            unit = d.request("units", dataset="test", setup="original", timeout=120)["units"][0]
            Echo.reply = "Half of 16 is 8, so she sells 16 - 3 - 4 = 9 eggs for $2 each: 9 * 2 = 18.\n#### 18"
            got = d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            self.assertEqual(got["answer"], Echo.reply)
            self.assertEqual(len(Echo.asked), 1)
            self.assertNotIn("temperature", Echo.asked[0])                    # default: the provider's own
            self.assertIn("#### <number>", Echo.asked[0]["messages"][0]["content"])
            s = d.request("score", unit=unit, dir=self.tmp, transcript=[], answer=got["answer"], setup="original", timeout=60)
            self.assertEqual((s["reward"], s["detail"]["extracted"]), (1.0, "18"))
            Echo.reply = "I think 17."
            got = d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            s = d.request("score", unit=unit, dir=self.tmp, transcript=[], answer=got["answer"], setup="original", timeout=60)
            self.assertEqual(s["reward"], 0.0)
            with self.assertRaises(jsonl.Failed):                             # no model setup: the original takes any model
                d.request("answer", unit=unit, dir=self.tmp, setup="model", model="m", base_url=self.base, key="k", timeout=60)
        finally:
            d.close()

    def test_gsm8k_sends_the_temperature_the_paper_decoded_at_when_asked(self):
        d, bench = self.driver("gsm8k", "0")
        try:
            unit = d.request("units", dataset="test", timeout=120)["units"][0]
            Echo.reply = "#### 18"
            d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            self.assertEqual(Echo.asked[0]["temperature"], 0.0)
        finally:
            d.close()

    def test_humaneval_completes_the_prompt_and_checks_it_as_published(self):
        d, bench = self.driver("humaneval", "0.2")
        try:
            unit = d.request("units", dataset="humaneval", setup="original", timeout=120)["units"][0]      # HumanEval/0
            # a whole definition in a fenced block: the file as written
            Echo.reply = ("Here you go:\n```python\nfrom typing import List\n\n\ndef has_close_elements(numbers: List[float], threshold: float) -> bool:\n"
                          "    for i, a in enumerate(numbers):\n        for b in numbers[i + 1:]:\n            if abs(a - b) < threshold:\n"
                          "                return True\n    return False\n```\n")
            got = d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            self.assertEqual(Echo.asked[0]["temperature"], 0.2)
            self.assertIn("def has_close_elements", Echo.asked[0]["messages"][0]["content"])   # the prompt, as published
            with open(os.path.join(self.tmp, "solution.py")) as f:
                self.assertTrue(f.read().startswith("from typing import List"))
            s = d.request("score", unit=unit, dir=self.tmp, transcript=[], answer=got["answer"], setup="original", timeout=60)
            self.assertEqual((s["reward"], s["passed"]), (1.0, True))
            # a body alone: appended to the prompt, as the paper's completion was
            Echo.reply = "    return any(abs(a - b) < threshold for i, a in enumerate(numbers) for b in numbers[i + 1:])\n"
            got = d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            s = d.request("score", unit=unit, dir=self.tmp, transcript=[], answer=got["answer"], setup="original", timeout=60)
            self.assertEqual(s["reward"], 1.0)
            self.assertEqual(s["detail"]["completion"], "after the prompt")
            Echo.reply = "```python\ndef has_close_elements(numbers, threshold):\n    return False\n```"
            got = d.request("answer", unit=unit, dir=self.tmp, setup="original", model="m", base_url=self.base, key="k", timeout=60)
            s = d.request("score", unit=unit, dir=self.tmp, transcript=[], answer=got["answer"], setup="original", timeout=60)
            self.assertEqual(s["reward"], 0.0)
        finally:
            d.close()


if __name__ == "__main__":
    unittest.main()
