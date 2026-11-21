"""A gated benchmark's failing endpoint is said by its status alone, down to the trial's result.json."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard.calls import relay
from nullius.standard.contract import definitions
from nullius.standard.requirements import install
from nullius.standard.runner import run

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
CONTROLS = {"nop": {"source": "nullius", "from": "Nothing runs; the answer is empty."}}
WORDS = "the gated task's own sentence"
FAILING = '''from nullius.standard.protocols.jsonl import serve
def score(unit, dir, transcript, answer, **_):
    raise RuntimeError("the LLM grader's endpoint answered 500, and the task was scored without it")
serve({"units": lambda dataset: {"units": ["a"]}, "setup": lambda unit, dir: {"prompt": "hi"}, "score": score})
'''


ANSWERING = '''from nullius.standard.protocols.jsonl import serve
def answer(unit, dir, setup, model, base_url, key, **_):
    raise RuntimeError("the upstream's grader said: status 500: " + %r)
serve({"units": lambda dataset, setup=None: {"units": ["a"]}, "setup": lambda unit, dir: {"prompt": "hi"},
       "answer": answer, "score": lambda unit, dir, transcript, answer, **_: {"reward": 0.0, "detail": {}}})
''' % WORDS
SETUPS = {"original": {"summary": "The paper's own pipeline.", "models": ["paper-1"], "default": "paper-1",
                       "providers": {"custom": ""}}}


class Calls:
    """The unit's relay as Run.unit reads it: each route's calls, the judge's ending on a refusal with words."""
    def __init__(self, side):
        self.side, self.dir = side, os.devnull

    def calls(self, tag):
        if tag.endswith("-" + self.side):
            return [{"status": 200}, {"status": 500, "error": f"status 500: {WORDS}"}]
        return []

    def url(self, tag, model=None):
        return "http://127.0.0.1:9/" + tag + "/v1"

    def stop(self):
        pass


class Gated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = install.HOME
        install.HOME = os.path.join(self.tmp, "home")
        here = os.path.join(self.tmp, "failing")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(FAILING)
        self.b = dict(definitions.get("benchmark", "fakebench", FIX), _here=here, controls=CONTROLS,
                      driver={"command": ["python3", "{here}/driver.py"]})

    def tearDown(self):
        install.HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def saved(self, side, **bench):
        b = dict(self.b, **bench)
        jobs = os.path.join(self.tmp, "jobs-" + side + str(bench))
        with patch.object(run.Run, "unit_relay", lambda r, *a, **k: Calls(side)):
            job_dir = run.run(b, definitions.control(b, "nop"), model="", provider="openai", jobs_root=jobs, env={},
                              say=lambda s: None)
        name = next(t for t in os.listdir(job_dir) if "__u" in t)
        with open(os.path.join(job_dir, name, "result.json")) as f:
            return json.load(f)["exception_info"]

    def test_a_gated_benchmarks_refused_judge_or_simulator_is_said_by_status_alone(self):
        for side in ("judge", "simulator"):
            with self.subTest(side):
                said = self.saved(side, gated=True)
                self.assertEqual(said["exception_type"], "ApiError")
                self.assertIn("answered 500", said["exception_message"])
                self.assertNotIn(WORDS, said["exception_message"])

    def test_a_benchmark_not_gated_keeps_the_providers_words(self):
        said = self.saved("judge")
        self.assertEqual(said["exception_type"], "ApiError")
        self.assertIn(WORDS, said["exception_message"])

    def answered(self, side, gated):
        """drive_upstream's answer failing on its judge's or simulated user's refusal."""
        here = os.path.join(self.tmp, "paper")
        os.makedirs(here, exist_ok=True)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(ANSWERING)
        b = dict(self.b, _here=here, setups=SETUPS, **({"gated": True} if gated else {}))
        o, why = definitions.setup(b, "original", "paper-1", "custom")
        self.assertIsNotNone(o, why)
        jobs = os.path.join(self.tmp, f"answer-{side}-{gated}")
        with patch.object(run.Run, "unit_relay", lambda r, *a, **k: Calls(side)):
            job_dir = run.run(b, o, model="paper-1", provider="custom", jobs_root=jobs, say=lambda s: None,
                              env={"MODEL_API_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "k"})
        name = next(t for t in os.listdir(job_dir) if "__u" in t)
        with open(os.path.join(job_dir, name, "result.json")) as f:
            return json.load(f)["exception_info"]

    def test_a_gated_benchmarks_answer_failing_on_its_judge_or_simulator_is_said_by_status_alone(self):
        for side in ("judge", "simulator"):
            with self.subTest(side):
                said = self.answered(side, gated=True)
                self.assertEqual(said["exception_type"], "ApiError")
                self.assertIn("answered 500", said["exception_message"])
                self.assertNotIn(WORDS, said["exception_message"])

    def test_an_answer_failing_on_a_benchmark_not_gated_keeps_its_words(self):
        said = self.answered("judge", gated=False)
        self.assertEqual(said["exception_type"], "NonZeroAgentExitCodeError")
        self.assertIn(WORDS, said["exception_message"])

    def test_the_failure_alone(self):
        calls = [{"status": 429, "error": WORDS}]
        self.assertEqual(relay.failure(calls), ("RateLimitError", WORDS))
        exc, said = relay.failure(calls, words=False)
        self.assertEqual(exc, "RateLimitError")
        self.assertNotIn(WORDS, said)
        self.assertEqual(relay.failure([{"status": 404, "error": "no such model"}], words=False)[0], "ModelNotFoundError")

    def test_gated_is_true_or_false(self):
        self.assertEqual(definitions.problems(dict(self.b, gated=True), "benchmark"), [])
        self.assertIn("gated: true for a benchmark whose tasks are gated, so no provider's words about them are kept",
                      definitions.problems(dict(self.b, gated="yes"), "benchmark"))
        self.assertIs(definitions.get("benchmark", "osworld2").get("gated"), True)


if __name__ == "__main__":
    unittest.main()
