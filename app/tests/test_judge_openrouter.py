"""The judge Settings chooses, and an OpenRouter model as the judge.

OpenRouter is a fake ``post``: it records what it was sent and answers with
a chat completion whose content is the JSON a model would give."""
from __future__ import annotations

import json
import unittest

from nullius.judge import client, questions as Q

IDS = ["task_completed", "failure_cause", "transcript_quality"]


def completion(answers, model="vendor/judge-1", usage=None, content=None):
    return {"model": model, "choices": [{"message": {"content": content if content is not None else json.dumps({"answers": answers})}}],
            "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.0012}}


def fake(status, data):
    sent = []
    def post(url, body, key, timeout=120.0):
        sent.append((url, body, key))
        return status, data
    return post, sent


class Configured(unittest.TestCase):
    def test_system_one_and_the_llm_judge_are_set_apart(self):
        self.assertFalse(client.system_one({})); self.assertTrue(client.system_one({"TYPESAFE_API_KEY": "ts-1"}))
        self.assertEqual(client.system_one({}).lane, "system-one")
        j = client.llm_judge({"OPENROUTER_API_KEY": "sk-or-1"})
        self.assertEqual((j.provider, j.lane, j.url), ("openrouter", "llm", client.LLM_PROVIDERS["openrouter"]["url"]))
        self.assertFalse(j); self.assertEqual(j.missing, "a model")                                         # no model: not ready
        j = client.llm_judge({"OPENROUTER_API_KEY": "sk-or-1", "OPENAI_API_KEY": "sk-oa-1", "LLM_JUDGE_PROVIDER": "openai", "LLM_JUDGE_MODEL": "gpt-x"})
        self.assertTrue(j); self.assertEqual((j.key, j.url), ("sk-oa-1", "https://api.openai.com/v1/chat/completions"))
        local = client.llm_judge({"MODEL_API_URL": "http://box:8000/v1/", "LLM_JUDGE_MODEL": "qwen"})          # the first provider set
        self.assertEqual((local.provider, local.url, local.key), ("local", "http://box:8000/v1/chat/completions", "local"))
        self.assertTrue(local)
        self.assertNotIn("sk-or-1", repr(client.llm_judge({"OPENROUTER_API_KEY": "sk-or-1"})))


class SystemOneOnOpenRouter(unittest.TestCase):
    def test_system_one_is_asked_where_settings_say_with_the_model_named(self):
        self.assertEqual(client.system_one({"OPENROUTER_API_KEY": "sk-or-1"}).provider, "openrouter")          # the only key: OpenRouter
        self.assertEqual(client.system_one({"OPENROUTER_API_KEY": "sk-or-1", "TYPESAFE_API_KEY": "ts-1"}).provider, "typesafe")
        j = client.system_one({"OPENROUTER_API_KEY": "sk-or-1", "SYSTEM_ONE_PROVIDER": "openrouter", "SYSTEM_ONE_MODEL": "jaredpalmer/kev-4b"})
        self.assertEqual((j.lane, j.url, j.model), ("system-one", "https://openrouter.ai/api/v1/systemone", "jaredpalmer/kev-4b"))
        sent = []
        def post(url, body, key):
            sent.append((url, body, key))
            return 200, {"model": body["model"], "answers": {"task_completed": {"type": "noul", "noul": 0.81}}, "usage": {"input_tokens": 50}}
        rec = client.ask("the state", ["task_completed"], j, post=post)
        url, body, key = sent[0]
        self.assertEqual((url, key, body["model"]), ("https://openrouter.ai/api/v1/systemone", "sk-or-1", "jaredpalmer/kev-4b"))
        self.assertIn("questions", body); self.assertEqual(body["state"], "the state")                         # the System One contract, not a chat
        self.assertEqual(rec["judge"], {"provider": "openrouter", "model": "jaredpalmer/kev-4b"})
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.81}})
        self.assertEqual(client.system_one({"OPENROUTER_API_KEY": "k", "SYSTEM_ONE_PROVIDER": "openrouter"}).model, "~typesafe/jev-latest")


class OpenRouterJudge(unittest.TestCase):
    judge = client.Judge("openrouter", "sk-or-1", "vendor/judge-1", client.LLM_PROVIDERS["openrouter"]["url"])

    def test_one_call_asks_every_question_and_reads_each_answer_by_its_type(self):
        post, sent = fake(200, completion({
            "task_completed": {"noul": 0.2},
            "failure_cause": {"choice": "agent_error", "probabilities": {"agent_error": 3, "environment_error": 1}},
            "transcript_quality": {"score": 1}}))
        rec = client.ask("the state", IDS, self.judge, post=post, kind="trial")
        url, body, key = sent[0]
        self.assertEqual((url, key, body["model"]), (client.LLM_PROVIDERS["openrouter"]["url"], "sk-or-1", "vendor/judge-1"))
        self.assertEqual(body["response_format"], {"type": "json_object"})
        prompt = body["messages"][1]["content"]
        for qid in IDS:
            self.assertIn(f'"{qid}"', prompt)
        self.assertIn("the state", prompt)
        self.assertEqual(rec["judge"], {"provider": "openrouter", "model": "vendor/judge-1"})
        self.assertEqual(rec["answers"]["task_completed"], {"noul": 0.2})
        self.assertEqual(rec["answers"]["failure_cause"]["probabilities"], {"agent_error": 0.75, "environment_error": 0.25})   # normalised
        self.assertIsNone(rec["answers"]["failure_cause"]["confidence"])                                   # stated, not calibrated
        self.assertEqual(rec["answers"]["transcript_quality"]["legend"], Q.QUESTIONS["transcript_quality"]["criteria"])
        self.assertEqual(rec["usage"], {"input_tokens": 1200, "output_tokens": 80, "cost_usd": 0.0012})
        self.assertEqual(rec["states"]["trial"]["input_tokens"], 1200)
        self.assertNotIn("sk-or-1", json.dumps(rec))

    def test_an_answer_outside_its_question_is_left_out(self):
        post, _ = fake(200, completion({"task_completed": {"noul": 1.7}, "failure_cause": {"choice": "not_an_option"},
                                        "transcript_quality": {"score": 9}}))
        self.assertEqual(client.ask("s", IDS, self.judge, post=post)["answers"], {})

    def test_a_reply_in_prose_around_the_json_is_read_and_one_without_json_is_said(self):
        post, _ = fake(200, completion(None, content='Here you go:\n```json\n{"answers": {"task_completed": {"noul": 0.9}}}\n```'))
        self.assertEqual(client.ask("s", IDS, self.judge, post=post)["answers"], {"task_completed": {"noul": 0.9}})
        post, _ = fake(200, completion(None, content="I cannot judge this."))
        with self.assertRaisesRegex(client.JudgeError, "did not answer in the JSON asked for"):
            client.ask("s", IDS, self.judge, post=post)

    def test_refusals_are_said_for_what_they_are(self):
        for status, said in ((401, "refused the key"), (402, "credit is spent"), (404, "refused the request"), (500, "answered 500")):
            post, _ = fake(status, {"error": {"message": "nope"}})
            with self.subTest(status), self.assertRaisesRegex(client.JudgeError, said):
                client.ask("s", IDS, self.judge, post=post, sleep=lambda s: None)
        with self.assertRaisesRegex(client.JudgeError, "needs a model"):
            client.ask("s", IDS, client.Judge("openrouter", "sk-or-1", "", client.LLM_PROVIDERS["openrouter"]["url"]))

    def test_a_busy_openrouter_is_asked_again(self):
        answers = iter([(429, {}), (200, completion({"task_completed": {"noul": 0.4}}))])
        rec = client.ask("s", IDS, self.judge, post=lambda *a, **k: next(answers), sleep=lambda s: None)
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.4}})

    def test_its_calls_never_calibrate_typesafe_estimates(self):
        rec = {"judge": {"provider": "openrouter"}, "states": {"trial": {"request_chars": 9000, "input_tokens": 1000}}}
        self.assertIsNone(client.calibrate([rec]))


if __name__ == "__main__":
    unittest.main()
