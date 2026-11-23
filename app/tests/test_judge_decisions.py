"""Decision in each decision protocol, and the LLM judge on Anthropic's
Messages. Every provider is a fake ``post``: it records what it was sent and
answers with a fixture reply."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from nullius import providers
from nullius.judge import client, questions as Q

IDS = ["task_completed", "failure_cause", "transcript_quality"]


def fake(status, data):
    sent = []

    def post(url, body, key, timeout=120.0, auth=None):
        sent.append({"url": url, "body": body, "key": key, "auth": auth})
        return status, data
    return post, sent


def judge(env):
    return client.decision_judge(env)


class Protocols(unittest.TestCase):
    def test_each_provider_and_model_gets_its_decision_protocol_and_is_asked_there(self):
        got = lambda env: (judge(env).protocol, judge(env).url)
        self.assertEqual(got({"TYPESAFE_API_KEY": "k"}), ("systemone", "https://api.typesafe.ai/v1/systemone"))
        self.assertEqual(got({"OPENROUTER_API_KEY": "k"}), ("systemone", "https://openrouter.ai/api/v1/systemone"))
        self.assertEqual(got({"OPENROUTER_API_KEY": "k", "DECISION_MODEL": "microsoft/microsoft-decision-1"}),
                         ("decisions", "https://openrouter.ai/api/alpha/decisions"))                   # another maker's model
        self.assertEqual(got({"OPENAI_API_KEY": "k", "DECISION_PROVIDER": "openai"}), ("openai-decisions", "https://api.openai.com/v1/decisions"))
        self.assertEqual(judge({"OPENAI_API_KEY": "k", "DECISION_PROVIDER": "openai"}).model, "gpt-6-luna")
        custom = {"MODEL_GW": '{"base": "https://gw/v1", "endpoints": {"decisions": "/alpha/decisions"}}', "MODEL_GW_API_KEY": "k",
                  "DECISION_PROVIDER": "custom:GW", "DECISION_MODEL": "m"}
        self.assertEqual(got(custom), ("decisions", "https://gw/v1/alpha/decisions"))
        self.assertNotIn("custom:LAB", client.decision_judge_providers({"MODEL_LAB": '{"base": "http://lab/v1"}'}))   # lists none: not offered
        self.assertEqual(judge({"OPENROUTER_API_KEY": "sk-or-1"}).provider, "openrouter")                          # the only key: OpenRouter
        self.assertEqual(judge({"OPENROUTER_API_KEY": "sk-or-1", "TYPESAFE_API_KEY": "ts-1"}).provider, "typesafe")
        self.assertEqual(judge({"OPENROUTER_API_KEY": "k", "DECISION_PROVIDER": "openrouter"}).model, "~typesafe/jev-latest")
        j = judge({"OPENROUTER_API_KEY": "sk-or-1", "DECISION_PROVIDER": "openrouter", "DECISION_MODEL": "jaredpalmer/kev-4b"})
        self.assertEqual((j.lane, j.url, j.model), ("decision", "https://openrouter.ai/api/v1/systemone", "jaredpalmer/kev-4b"))
        post, sent = fake(200, {"model": "jaredpalmer/kev-4b", "answers": {"task_completed": {"type": "noul", "noul": 0.81}}, "usage": {"input_tokens": 50}})
        rec = client.ask("the state", ["task_completed"], j, post=post)
        url, body, key = sent[0]["url"], sent[0]["body"], sent[0]["key"]
        self.assertEqual((url, key, body["model"]), ("https://openrouter.ai/api/v1/systemone", "sk-or-1", "jaredpalmer/kev-4b"))
        self.assertIn("questions", body); self.assertEqual(body["state"], "the state")                         # the Decision contract, not a chat
        self.assertEqual(rec["judge"], {"provider": "openrouter", "model": "jaredpalmer/kev-4b"})
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.81}})

    def test_a_setting_saved_before_the_rename_is_still_read_and_the_new_name_wins(self):
        old = {"OPENROUTER_API_KEY": "k", "SYSTEM_ONE_PROVIDER": "openrouter", "SYSTEM_ONE_MODEL": "typesafe/jev-1.14",
               "SYSTEM_ONE_PROTOCOL": "decisions"}
        j = judge(old)
        self.assertEqual((j.lane, j.provider, j.model, j.protocol), ("decision", "openrouter", "typesafe/jev-1.14", "decisions"))
        self.assertEqual(judge(dict(old, DECISION_MODEL="typesafe/jev-2")).model, "typesafe/jev-2")

    def test_without_a_provider_for_it_the_decision_judge_needs_a_model_on_any_of_them(self):
        """Not "the TypeSafe key": OpenRouter and OpenAI serve decision models too."""
        self.assertEqual(judge({}).missing, "a model")
        self.assertFalse(judge({}))
        self.assertEqual(judge({"OPENROUTER_API_KEY": "k"}).missing, "")

    def test_a_chosen_protocol_wins_where_the_provider_serves_it_else_the_automatic_one(self):
        from nullius import settings
        got = lambda env: (judge(env).protocol, judge(env).url)
        router = {"OPENROUTER_API_KEY": "k"}
        self.assertEqual(got(dict(router, DECISION_PROTOCOL="decisions")), ("decisions", "https://openrouter.ai/api/alpha/decisions"))   # over TypeSafe's model
        self.assertEqual(got(dict(router, DECISION_PROTOCOL="systemone", DECISION_MODEL="microsoft/microsoft-decision-1")),
                         ("systemone", "https://openrouter.ai/api/v1/systemone"))
        self.assertEqual(got(dict(router, DECISION_PROTOCOL="openai-decisions")), ("systemone", "https://openrouter.ai/api/v1/systemone"))  # not served: automatic
        self.assertEqual(client.decision_judge_providers(dict(router, DECISION_PROTOCOL="decisions"))["openrouter"]["protocol"], "decisions")
        self.assertEqual(client.decision_judge_providers(router)["openrouter"]["listed"], ["systemone", "decisions"])
        both = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC": '{"endpoints": {"chat": "/chat/completions"}}', "LLM_JUDGE_PROVIDER": "anthropic", "LLM_JUDGE_MODEL": "m"}
        llm = lambda env: (client.llm_judge(env).protocol, client.llm_judge(env).url)
        self.assertEqual(llm(both), ("chat", "https://api.anthropic.com/v1/chat/completions"))                 # unset: chat before messages
        self.assertEqual(llm(dict(both, LLM_JUDGE_PROTOCOL="messages")), ("messages", "https://api.anthropic.com/v1/messages"))
        self.assertEqual(client.llm_providers(both)["anthropic"]["listed"], ["chat", "messages"])
        self.assertEqual(llm({"OPENROUTER_API_KEY": "k", "LLM_JUDGE_MODEL": "m", "LLM_JUDGE_PROTOCOL": "messages"}),
                         ("chat", "https://openrouter.ai/api/v1/chat/completions"))                                 # not served: automatic
        for name, spoken in (("LLM_JUDGE_PROTOCOL", client.LLM_PROTOCOLS), ("DECISION_PROTOCOL", client.DECISION_PROTOCOLS)):
            var = settings.BY_NAME[name]
            self.assertEqual([p for p in client.providers.PROTOCOLS if var.validate(p) is None], list(spoken))   # what that judge speaks, no more
            self.assertIsNotNone(var.validate("Chat"))


class Decision(unittest.TestCase):
    def test_systemone_keeps_the_providers_cost_and_records_a_refusal(self):
        post, _ = fake(200, {"model": "~typesafe/jev-latest", "answers": {
            "task_completed": {"noul": 0.8}, "failure_cause": {"type": "refusal", "refusal": "not enough evidence"}},
            "usage": {"input_tokens": 900, "output_tokens": 0, "cost": 0.00004}})
        rec = client.ask("state", IDS, judge({"OPENROUTER_API_KEY": "sk-or-1"}), post=post)
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.8}})
        self.assertEqual(rec["refusals"], {"failure_cause": "not enough evidence"})                    # said, not dropped
        self.assertEqual(rec["usage"]["cost_usd"], 0.00004)
        merged = client.merge([rec, dict(rec, refusals={"transcript_quality": "refused"})])
        self.assertEqual(set(merged["refusals"]), {"failure_cause", "transcript_quality"})
        post, _ = fake(200, {"answers": {"task_completed": {"noul": 0.7, "refusal": None}}})                # a null refusal: the answer
        rec = client.ask("state", IDS, judge({"OPENROUTER_API_KEY": "sk-or-1"}), post=post)
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.7}}); self.assertNotIn("refusals", rec)

    def test_a_402_says_what_it_means(self):
        post, _ = fake(402, {"error": {"code": 402, "message": "Insufficient credits"}})
        with self.assertRaises(client.JudgeError) as e:
            client.ask("state", IDS, judge({"OPENROUTER_API_KEY": "sk-or-1"}), post=post, sleep=lambda s: None)
        self.assertEqual(e.exception.status, 402)
        self.assertIn("credit is spent", str(e.exception)); self.assertIn("Insufficient credits", str(e.exception))

    def test_decisions_reads_confidence_and_its_error_shape(self):
        env = {"OPENROUTER_API_KEY": "sk-or-1", "DECISION_MODEL": "inception/mercury-decide"}
        post, sent = fake(200, {"model": "inception/mercury-decide", "answers": {"task_completed": {"noul": 0.3, "confidence": 0.9}},
                                "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.001}})
        rec = client.ask("state", IDS, judge(env), post=post)
        self.assertEqual(sent[0]["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(set(sent[0]["body"]), {"model", "state", "questions"})                          # the same request as systemone
        self.assertEqual(rec["answers"]["task_completed"], {"noul": 0.3, "confidence": 0.9})
        self.assertEqual(rec["judge"], {"provider": "openrouter", "model": "inception/mercury-decide"})
        post, _ = fake(400, {"error": {"code": "invalid_model", "message": "Box questions are Jev-only"}})
        with self.assertRaises(client.JudgeError) as e:
            client.ask("state", IDS, judge(env), post=post)
        self.assertIn("Box questions are Jev-only", str(e.exception))

    def test_a_reply_of_another_shape_is_a_judge_error(self):
        for env, data in (({"OPENROUTER_API_KEY": "k"}, {"answers": [1, 2]}),
                          ({"OPENROUTER_API_KEY": "k"}, {"answers": {"failure_cause": {"choice": "a", "probabilities": {"a": "x"}}}}),
                          ({"OPENROUTER_API_KEY": "k"}, ["not", "an", "object"]),
                          ({"OPENAI_API_KEY": "k", "DECISION_PROVIDER": "openai"}, {"answers": [{"name": ["task_completed"]}]}),
                          ({"OPENAI_API_KEY": "k", "DECISION_PROVIDER": "openai"},
                           {"answers": [{"name": "failure_cause", "choice": next(iter(Q.QUESTIONS["failure_cause"]["criteria"])), "probabilities": 5}]})):
            post, _ = fake(200, data)
            with self.assertRaises(client.JudgeError, msg=data) as e:
                client.ask("state", IDS, judge(env), post=post)
            self.assertEqual(e.exception.status, 502); self.assertIn("could not be read", str(e.exception))


class Redirects(unittest.TestCase):
    def test_a_redirect_is_an_error_and_the_key_goes_no_further(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/judge":
                    self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/stolen")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

            do_GET = do_POST

        first, other = ThreadingHTTPServer(("127.0.0.1", 0), Handler), ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        for srv in (first, other):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        with self.assertRaises(client.JudgeError) as e:
            client.http_post(f"http://127.0.0.1:{first.server_address[1]}/judge", {}, "sk-fixture-redirect")
        self.assertEqual(e.exception.status, 302); self.assertIn("redirects", str(e.exception))
        self.assertEqual([s[1] for s in seen], ["/judge"])                                              # never the other host


LISTED = {"data": [{"id": "typesafe/jev-1.14", "architecture": {"output_modalities": ["decisions"]}},
                   {"id": "perplexity/pplx-decider-v1.3:free", "architecture": {"output_modalities": ["decisions"]}},
                   {"id": "~liquid/d1-latest", "architecture": {"output_modalities": ["text", "decisions"]}},
                   {"id": "openai/gpt-6", "architecture": {"output_modalities": ["text"]}},
                   {"id": "odd/string", "architecture": {"output_modalities": "decisions"}},
                   {"id": "odd/none"}]}


class DecisionModels(unittest.TestCase):
    """OpenRouter's decision models: its public list, filtered, cached, and the short list without either."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="nullius-decisions-"); self.addCleanup(shutil.rmtree, self.home, True)
        self.env, self.now, self.sent = {"NULLIUS_HOME": self.home}, 1000.0, []

    def opener(self, body=LISTED):
        def fetch(req, timeout):
            self.sent.append((req.full_url, dict(req.header_items()), timeout))
            if isinstance(body, Exception):
                raise body
            return io.BytesIO(json.dumps(body).encode())
        return fetch

    def models(self, body=LISTED, **kw):
        return providers.decision_models(self.env, opener=self.opener(body), clock=lambda: self.now, **kw)

    def test_only_decision_models_are_kept_and_no_key_is_sent(self):
        env = dict(self.env, OPENROUTER_API_KEY="sk-or-fixture")
        got = providers.decision_models(env, opener=self.opener(), clock=lambda: self.now)
        self.assertEqual(sorted(got), ["perplexity/pplx-decider-v1.3:free", "typesafe/jev-1.14", "~liquid/d1-latest"])
        (url, headers, timeout), = self.sent
        self.assertEqual(url, "https://openrouter.ai/api/v1/models?output_modalities=decisions")
        self.assertNotIn("Authorization", headers); self.assertNotIn("sk-or-fixture", json.dumps(headers))
        self.assertEqual(timeout, 5)

    def test_the_cache_is_used_while_fresh_and_kept_when_the_network_fails(self):
        first = self.models()
        self.assertEqual(self.models(OSError("unreached")), first); self.assertEqual(len(self.sent), 1)    # a hit: not asked
        self.now += providers.DECISION_TTL + 1
        self.assertEqual(self.models(OSError("unreached")), first); self.assertEqual(len(self.sent), 2)    # stale: asked, kept
        self.assertEqual(self.models({"data": [{"id": "new/one", "architecture": {"output_modalities": ["decisions"]}}]}), ["new/one"])
        self.assertEqual(self.models(OSError("unreached")), ["new/one"])                                  # fresh again
        self.now += providers.DECISION_TTL + 1
        self.assertEqual(self.models(fetch=False), ["new/one"]); self.assertEqual(len(self.sent), 3)        # never asked without fetch

    def test_no_network_and_no_cache_is_no_list(self):
        for body in (OSError("unreached"), {"data": {}}, {"data": []}, ValueError("x")):
            self.assertEqual(self.models(body), [])
        self.assertEqual(self.models(fetch=False), [])

    def test_the_suggestions_are_the_list_with_the_alias_first_or_the_short_list(self):
        with mock.patch.object(providers, "decision_models", return_value=["z/one", "typesafe/jev-1.14", "a/two:free", "~typesafe/jev-latest"]) as live:
            self.assertEqual(client.decision_judge_models("openrouter"), ["~typesafe/jev-latest", "typesafe/jev-1.14", "a/two:free", "z/one"])
            live.assert_called_with(fetch=False)
            client.decision_judge_models("openrouter", fetch=True); live.assert_called_with(fetch=True)
            self.assertEqual(client.decision_judge_models("openai"), ["gpt-6-luna"])
            self.assertEqual(client.decision_judge_providers({"OPENROUTER_API_KEY": "k"})["openrouter"]["model"], "~typesafe/jev-latest")
        with mock.patch.object(providers, "decision_models", return_value=["b/x"]):
            self.assertEqual(client.decision_judge_models("openrouter"), ["~typesafe/jev-latest", "b/x"])
        with mock.patch.object(providers, "decision_models", return_value=[]):
            self.assertEqual(client.decision_judge_models("openrouter"), client.DECISION_MODELS["openrouter"])

    def test_the_protocol_follows_the_model_not_the_list(self):
        on = lambda model, **env: client.decision_judge({"OPENROUTER_API_KEY": "k", "DECISION_MODEL": model, **env}).protocol
        with mock.patch.object(providers, "decision_models", return_value=["google/gemini-decision-1", "typesafe/jev-1.14"]):
            self.assertEqual(on("typesafe/jev-1.14"), "systemone")
            self.assertEqual(on("google/gemini-decision-1"), "decisions")
            self.assertEqual(on("someone/typed-by-hand"), "decisions")
            self.assertEqual(on("google/gemini-decision-1", DECISION_PROTOCOL="systemone"), "systemone")


class OpenAIDecisions(unittest.TestCase):
    ENV = {"OPENAI_API_KEY": "sk-oa-1", "DECISION_PROVIDER": "openai"}

    def test_the_request_is_openais_shape(self):
        post, sent = fake(200, {"model": "gpt-6-luna", "answers": [], "usage": {}})
        client.ask("the state", IDS, judge(self.ENV), post=post)
        body = sent[0]["body"]
        self.assertEqual((body["model"], body["input"]), ("gpt-6-luna", "the state"))
        self.assertNotIn("state", body)
        asked = {q["name"]: q for q in body["questions"]}
        self.assertEqual(list(asked), IDS)
        yes_no = asked["task_completed"]
        self.assertEqual(yes_no["type"], "predicate"); self.assertNotIn("criteria", yes_no)
        self.assertIn("True means: " + Q.QUESTIONS["task_completed"]["criteria"]["true"], yes_no["instructions"])   # criteria fold into it
        self.assertEqual([c["value"] for c in asked["failure_cause"]["choices"]], list(Q.QUESTIONS["failure_cause"]["criteria"]))
        self.assertEqual([lv["label"] for lv in asked["transcript_quality"]["levels"]], ["0", "1", "2"])

    def test_the_reply_is_read_into_the_record_and_a_refusal_is_said(self):
        option = next(iter(Q.QUESTIONS["failure_cause"]["criteria"]))
        post, _ = fake(200, {"model": "gpt-6-luna", "usage": {"input_tokens": 50, "output_tokens": 3},
                             "answers": [{"name": "task_completed", "type": "predicate", "probability": 0.7},
                                         {"name": "failure_cause", "type": "choice", "choice": option,
                                          "probabilities": [{"value": option, "probability": 0.6}]},
                                         {"name": "transcript_quality", "type": "refusal", "refusal": "cannot judge"},
                                         {"name": "not_asked", "type": "predicate", "probability": 1}]})
        rec = client.ask("s", IDS, judge(self.ENV), post=post)
        self.assertEqual(rec["answers"]["task_completed"], {"noul": 0.7})
        self.assertEqual(rec["answers"]["failure_cause"]["probabilities"], {option: 0.6})
        self.assertEqual(rec["refusals"], {"transcript_quality": "cannot judge"})
        self.assertNotIn("not_asked", rec["answers"]); self.assertEqual(rec["usage"]["input_tokens"], 50)
        post, _ = fake(200, {"answers": [{"name": "transcript_quality", "type": "score", "level": "7"}]})
        self.assertEqual(client.ask("s", IDS, judge(self.ENV), post=post)["answers"], {})
        post, _ = fake(200, {"answers": [{"name": "transcript_quality", "type": "score", "level": "2"}]})
        self.assertEqual(client.ask("s", IDS, judge(self.ENV), post=post)["answers"]["transcript_quality"]["score"], 2.0)


class LLMJudgeOnMessages(unittest.TestCase):
    def test_anthropic_is_asked_in_its_own_format_with_its_own_headers(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-1", "LLM_JUDGE_PROVIDER": "anthropic", "LLM_JUDGE_MODEL": "claude-x"}
        j = client.llm_judge(env)
        self.assertEqual((j.provider, j.protocol, j.url), ("anthropic", "messages", "https://api.anthropic.com/v1/messages"))
        post, sent = fake(200, {"model": "claude-x", "content": [{"type": "text", "text": '{"answers": {"task_completed": {"noul": 0.9}}}'}],
                                "usage": {"input_tokens": 120, "output_tokens": 9}})
        rec = client.ask("state", IDS, j, post=post)
        self.assertEqual(sent[0]["auth"]["x-api-key"], "sk-ant-1"); self.assertIn("anthropic-version", sent[0]["auth"])
        body = sent[0]["body"]
        self.assertEqual([m["role"] for m in body["messages"]], ["user"]); self.assertIn("system", body); self.assertNotIn("response_format", body)
        self.assertEqual(rec["answers"], {"task_completed": {"noul": 0.9}})
        self.assertEqual((rec["usage"]["input_tokens"], rec["usage"]["output_tokens"]), (120, 9))
        self.assertNotIn("sk-ant-1", repr(j))


if __name__ == "__main__":
    unittest.main()
