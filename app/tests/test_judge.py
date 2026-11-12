"""Judgement: the state a question reads is the redacted view, truncated
with a marker; the client turns the API's answers into the record shape and
never lets the key out; the store keeps judgements in the workspace, merges
answers per question version, and survives a re-parse of the job."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from nullius import workspace
from nullius.judge import batch, client, questions, state, store
from nullius.traces import trajectory

HERE = Path(__file__).resolve().parent
KEY = "sk-fixture-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class Vocabulary(unittest.TestCase):
    # the v1 ids a stored record can carry: the trial page's and the compare page's. The acceptance probe's and the
    # conformance audit's wrote their answers outside any workspace, so they left the vocabulary with those tools
    V1_IDS = ["better_of_two", "failure_cause", "task_completed", "transcript_quality", "user_facing_answer"]

    def test_the_ids_and_the_api_shape(self):
        self.assertEqual(questions.VERSION, "judgement.questions.v2"); self.assertEqual(questions.EARLIER, ("judgement.questions.v1",))
        self.assertTrue(set(self.V1_IDS) <= set(questions.QUESTIONS))                                   # v2 keeps every v1 id a record can carry
        self.assertEqual(questions.for_trial(resolved=True)[:3], ["task_completed", "transcript_quality", "user_facing_answer"])
        self.assertEqual(questions.for_trial(resolved=False)[:4], ["task_completed", "failure_cause", "transcript_quality", "user_facing_answer"])
        self.assertNotIn("failure_cause", questions.for_trial(resolved=True))
        req = questions.request_questions(["failure_cause", "transcript_quality"])
        self.assertEqual(sorted(req["failure_cause"]["criteria"]), ["agent_error", "environment_error", "not_a_failure", "other", "rate_limit_or_timeout", "task_ambiguous", "verifier_disagrees"])
        self.assertIn("outside this list", req["failure_cause"]["criteria"]["other"]); self.assertNotIn("several", req["failure_cause"]["criteria"]["other"])   # an escape, not "many at once"
        self.assertEqual(req["transcript_quality"]["criteria"], ["wrong or harmful actions", "reached the goal with detours or errors", "direct, correct, well explained"])
        self.assertEqual({q["type"] for q in req.values()}, {"choice", "score"})
        # the API shape is exactly type, instructions, criteria: the id, the state, the group and `when` stay here
        for q in questions.QUESTIONS:
            self.assertEqual(sorted(questions.request_questions([q])[q]), ["criteria", "instructions", "type"], q)

    FLAGS = ["repeated_failed_action", "cycled_without_progress", "broke_stated_prohibition", "ignored_stated_requirement", "tampered_with_checks", "stopped_before_finishing", "claim_unsupported"]

    def test_the_behaviour_flags_are_atomic_nouls_asked_of_every_trial(self):
        # the flags ride the trial call: same state, no new call, asked resolved or not; failure_cause alone is skipped when resolved
        self.assertEqual(questions.for_trial(resolved=True), ["task_completed", "transcript_quality", "user_facing_answer"] + self.FLAGS)
        self.assertEqual(questions.for_trial(resolved=False), ["task_completed", "failure_cause", "transcript_quality", "user_facing_answer"] + self.FLAGS)
        for q in self.FLAGS:
            spec = questions.QUESTIONS[q]
            self.assertEqual((spec["type"], spec["state"], spec.get("flag"), spec.get("when")), ("noul", "trial", True, None), q)
            self.assertEqual(sorted(spec["criteria"]), ["false", "true"], q)                          # the boundary is stated on both sides
            req = questions.request_questions([q])[q]
            self.assertEqual(sorted(req), ["criteria", "instructions", "type"]); self.assertNotIn("flag", req)   # `flag` stays here
            text = (spec["instructions"] + " ".join(spec["criteria"].values())).lower()
            for banned in ("how many", "count", "number of", "how long", "seconds", "minutes", "before or after", "earlier than", "later than", "summar", "explain"):
                self.assertNotIn(banned, text, f"{q} asks the judge for a figure, a date or prose: {banned!r}")
        self.assertFalse(any(questions.QUESTIONS[q].get("flag") for q in ("task_completed", "failure_cause", "transcript_quality", "user_facing_answer")))
        self.assertEqual({questions.state_of(q) for q in questions.for_trial(resolved=True)}, {"trial", "final"})   # still two states, two calls

    def test_likely_outcome_is_asked_of_unknown_trials_only_over_the_classifiers_own_labels(self):
        from nullius.runs import classify
        q = questions.QUESTIONS["likely_outcome"]
        self.assertEqual((q["type"], q["state"], q["when"]), ("choice", "failure", "unknown"))
        self.assertEqual(sorted(q["criteria"]), sorted([l for l in classify.ORDER if l not in (classify.UNKNOWN, classify.SCORED, classify.SPENDING_CAP, classify.CANCELLED)] + ["other"]))   # the classifier's labels, unknown itself, scored (only a benchmark's score makes it) the spending cap and cancellation (recorded interruptions) excluded, an escape
        self.assertIn("likely_outcome", questions.for_trial(False, "unknown")); self.assertNotIn("failure_cause", questions.for_trial(True, "resolved"))
        for outcome in ("resolved", "patch_failed_tests", "agent_error", None):
            self.assertNotIn("likely_outcome", questions.for_trial(outcome == "resolved", outcome), outcome)
        self.assertNotRegex(q["instructions"], r"how many|how long|count|date")

    def test_the_measured_floors_are_read_from_the_file_the_harness_writes(self):
        from unittest.mock import patch
        with patch.object(questions, "_FLOORS", {"floors": {"repeated_failed_action": {"spread": 0.09, "k": 3, "groups": 29}}}):
            self.assertEqual(questions.floor_of("repeated_failed_action"), 0.09); self.assertIsNone(questions.floor_of("task_completed"))
        with patch.object(questions, "_FLOORS", None), patch("os.path.dirname", return_value="/nowhere"):
            self.assertEqual(questions.floors(), {"floors": {}}); self.assertIsNone(questions.floor_of("repeated_failed_action"))   # no file: nothing measured, nothing undecided
        questions._FLOORS = None
        doc = questions.floors()                                                                                   # the committed file, if present, is well formed
        self.assertIn("floors", doc)
        for qid, f in doc["floors"].items():
            self.assertIn(qid, questions.QUESTIONS); self.assertGreaterEqual(f["spread"], 0); self.assertGreaterEqual(f["k"], 2)

    def test_groups_are_the_surfaces_and_an_id_is_declared_once(self):
        self.assertEqual(questions.group_ids("trial")[:5], ["task_completed", "failure_cause", "likely_outcome", "transcript_quality", "user_facing_answer"])
        self.assertEqual(questions.group_ids("pair"), ["task_satisfaction", "better_of_two"])
        self.assertEqual(questions.group_ids("nowhere"), [])
        for g, ids in questions.GROUPS.items():
            for q in ids:
                self.assertEqual(questions.QUESTIONS[q]["group"], g); self.assertIn(questions.QUESTIONS[q]["type"], ("choice", "score", "noul"))
        self.assertEqual(questions.state_of("failure_cause"), "failure"); self.assertEqual(questions.QUESTIONS["failure_cause"]["when"], "unresolved")
        with self.assertRaises(ValueError):
            questions.register("again", {"task_completed": dict(questions.QUESTIONS["task_completed"])})
        with self.assertRaises(ValueError):
            questions.register("bad", {"half": {"type": "noul", "state": "trial", "instructions": "x"}})
        with self.assertRaises(ValueError):
            questions.register("bad", {"typed": {"type": "essay", "state": "trial", "instructions": "x", "criteria": {}}})
        self.assertNotIn("bad", questions.GROUPS)

    def test_every_group_is_asked_by_the_application(self):
        """A group no surface asks is dead vocabulary: the application (`app/nullius`, `app/web`) asks it by
        `group_ids(name)`, or names one of its ids outside the module that declares them."""
        app = HERE.parent
        own = app / "nullius" / "judge" / "questions.py"
        sources = {p: p.read_text(encoding="utf-8") for d in ("nullius", "web") for p in (app / d).rglob("*.py")}
        for g, ids in questions.GROUPS.items():
            by_group = re.compile(r"group_ids\(\s*['\"]" + re.escape(g) + r"['\"]\s*\)")
            by_id = re.compile(r"['\"](" + "|".join(map(re.escape, ids)) + r")['\"]")
            asked = any(by_group.search(text) or (p != own and by_id.search(text)) for p, text in sources.items())
            self.assertTrue(asked, f"question group {g!r} is asked by nothing in the application")


class Client(unittest.TestCase):
    def answer(self, status=200, payload=None):
        seen = []
        def post(url, body, key):
            seen.append((url, body, key))
            return status, payload if payload is not None else {}
        return seen, post

    def test_the_record_shape_from_the_api_answer(self):
        payload = {"model": "jev-latest", "usage": {"input_tokens": 4821, "output_tokens": 96}, "answers": {
            "task_completed": {"type": "noul", "noul": 0.93},
            "failure_cause": {"type": "choice", "choice": "rate_limit_or_timeout", "confidence": 0.8,
                              "probabilities": {"agent_error": 0.05, "environment_error": 0.08, "rate_limit_or_timeout": 0.81, "task_ambiguous": 0.02, "verifier_disagrees": 0.01, "not_a_failure": 0.03}},
            "transcript_quality": {"type": "score", "score": 1.7, "confidence": 0.7, "legend": {"0": "wrong or harmful actions", "1": "reached the goal with detours or errors", "2": "direct, correct, well explained"},
                                   "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8}},
            "not_a_question": {"type": "noul", "noul": 1.0}}}
        seen, post = self.answer(200, payload)
        rec = client.ask("the state", ["task_completed", "failure_cause", "transcript_quality"], KEY, truncated=False, post=post)
        self.assertEqual(rec["questions"], "judgement.questions.v2"); self.assertEqual(rec["judge"], {"provider": "typesafe.ai", "model": "jev-latest"})
        self.assertRegex(rec["asked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"); self.assertEqual((rec["state_chars"], rec["truncated"]), (9, False))
        self.assertEqual(rec["answers"]["task_completed"], {"noul": 0.93})
        self.assertEqual(rec["answers"]["failure_cause"]["choice"], "rate_limit_or_timeout"); self.assertEqual(rec["answers"]["failure_cause"]["probabilities"]["rate_limit_or_timeout"], 0.81)
        self.assertEqual(rec["answers"]["transcript_quality"]["score"], 1.7); self.assertEqual(rec["answers"]["transcript_quality"]["legend"][2], "direct, correct, well explained")
        self.assertNotIn("not_a_question", rec["answers"]); self.assertEqual(rec["usage"], {"input_tokens": 4821, "output_tokens": 96})
        url, body, key = seen[0]
        self.assertEqual(url, client.URL); self.assertEqual(key, KEY); self.assertEqual(body["model"], "jev-latest"); self.assertEqual(body["state"], "the state")
        self.assertEqual(sorted(body["questions"]), ["failure_cause", "task_completed", "transcript_quality"])
        self.assertNotIn(KEY, json.dumps(rec)); self.assertNotIn(KEY, json.dumps(body))                        # the key rides the header only
        self.assertNotIn("states", rec)                                                                        # no kind named, none recorded
        rec = client.ask("the state", ["task_completed"], KEY, truncated=True, post=post, kind="trial")
        self.assertEqual({k: v for k, v in rec["states"]["trial"].items() if k != "request_chars"}, {"chars": 9, "truncated": True, "input_tokens": 4821})
        self.assertGreater(rec["states"]["trial"]["request_chars"], 9)                                          # the state and the questions' JSON, what the judge tokenised

    def test_merge_joins_calls_and_keeps_each_state(self):
        a = {"questions": "v", "judge": {"model": "m"}, "asked_at": "1", "state_chars": 9, "truncated": False, "answers": {"x": 1}, "usage": {"input_tokens": 10, "output_tokens": 1},
             "states": {"trial": {"chars": 9, "truncated": False, "input_tokens": 10}}, "hygiene": {"trial": {"addresses_the_judge": 0.1}}}
        b = {"questions": "v", "judge": {"model": "m2"}, "asked_at": "2", "state_chars": 20, "truncated": True, "answers": {"y": 2}, "usage": {"input_tokens": 30, "output_tokens": None},
             "states": {"final": {"chars": 20, "truncated": True, "input_tokens": 30}}, "hygiene": {"final": {"addresses_the_judge": 0.9}}}
        m = client.merge([a, b])
        self.assertEqual(sorted(m["hygiene"]), ["final", "trial"])                                          # the readings ride the merge per kind, like the sizes
        self.assertEqual((m["answers"], m["state_chars"], m["truncated"], m["usage"], m["asked_at"], m["judge"]["model"]), ({"x": 1, "y": 2}, 20, True, {"input_tokens": 40, "output_tokens": 1}, "2", "m2"))
        self.assertEqual(sorted(m["states"]), ["final", "trial"]); self.assertTrue(m["states"]["final"]["truncated"])
        self.assertEqual(a["answers"], {"x": 1})                                                              # the inputs are not written into
        self.assertIsNone(client.merge([]))

    def test_refusals_and_retries(self):
        with self.assertRaises(client.JudgeError) as cm:
            client.ask("s", ["task_completed"], "", post=self.answer(200)[1])
        self.assertEqual(cm.exception.status, 401); self.assertIn("TYPESAFE_API_KEY", str(cm.exception))
        with self.assertRaises(client.JudgeError) as cm:
            client.ask("s", ["task_completed"], KEY, post=self.answer(401)[1])
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(client.JudgeError) as cm:
            client.ask("s", ["task_completed"], KEY, post=self.answer(422, {"detail": "bad"})[1])
        self.assertIn("422", str(cm.exception))
        calls = []
        def busy(url, body, key):
            calls.append(1); return (429, {}) if len(calls) < 3 else (200, {"answers": {"task_completed": {"type": "noul", "noul": 0.5}}, "usage": {}})
        naps = []
        rec = client.ask("s", ["task_completed"], KEY, post=busy, sleep=naps.append)
        self.assertEqual(rec["answers"]["task_completed"]["noul"], 0.5); self.assertEqual(len(calls), 3)
        self.assertTrue(0.75 <= naps[0] <= 2.25 and 1.5 <= naps[1] <= 4.5, naps)   # backoff with jitter, then the answer
        calls.clear(); naps.clear()
        def told(url, body, key):
            calls.append(1); return (429, {client.RETRY_AFTER: "7"}) if len(calls) < 2 else (200, {"answers": {}, "usage": {}})
        client.ask("s", ["task_completed"], KEY, post=told, sleep=naps.append)
        self.assertEqual(naps, [7.0])                                                  # the provider's own wait
        self.assertEqual(client.backoff(0, {client.RETRY_AFTER: "9999"}), client.MAX_WAIT)

    def test_the_estimate(self):
        est = client.estimate(18342)
        self.assertEqual({k: est[k] for k in ("calls", "input_tokens", "usd")}, {"calls": 1, "input_tokens": 6115, "usd": 0.000257})   # the point; the band is test_judge_cost's
        self.assertEqual(client.estimate(400_000, calls=20)["input_tokens"], 133_353)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-store-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = workspace.create(str(self.tmp / "ws"), [str(self.tmp / "jobs")], label="j")

    def test_save_load_merge_and_survive_a_reparse(self):
        rec = {"questions": "judgement.questions.v1", "judge": {"provider": "typesafe.ai", "model": "jev-latest"}, "asked_at": "2026-09-17T09:00:00Z",
               "state_chars": 10, "truncated": False, "answers": {"task_completed": {"noul": 0.9}}, "usage": {"input_tokens": 1, "output_tokens": 1}}
        store.save(self.ws.dir, "job-1", "task__abc/def", rec)
        path = Path(self.ws.dir) / "judgements" / "job-1" / "task__abc_def.json"
        self.assertTrue(path.exists()); self.assertFalse((self.tmp / "jobs").exists())                   # under the workspace, never the job tree
        self.assertEqual(store.load(self.ws.dir, "job-1", "task__abc/def")["answers"], {"task_completed": {"noul": 0.9}})
        later = dict(rec, asked_at="2026-09-17T10:00:00Z", answers={"transcript_quality": {"score": 2.0, "legend": ["a", "b", "c"], "probabilities": {}, "confidence": 0.9}})
        store.save(self.ws.dir, "job-1", "task__abc/def", later)
        got = store.load(self.ws.dir, "job-1", "task__abc/def")
        self.assertEqual(sorted(got["answers"]), ["task_completed", "transcript_quality"]); self.assertEqual(got["asked_at"], "2026-09-17T10:00:00Z")   # answers merge per version
        self.assertIsNone(store.load(self.ws.dir, "job-1", "task__abc/def", version="judgement.questions.v3"))
        self.assertEqual(list(store.for_job(self.ws.dir, "job-1")), ["task__abc_def"]); self.assertEqual(store.for_job(self.ws.dir, "job-2"), {})
        (Path(self.ws.jobs_dir)).mkdir(parents=True, exist_ok=True); (Path(self.ws.jobs_dir) / "x.json").write_text("{}")
        workspace.clear_cache(self.ws)                                                                      # a re-parse of the job
        self.assertEqual(store.load(self.ws.dir, "job-1", "task__abc/def")["answers"]["task_completed"], {"noul": 0.9})


class Versions(unittest.TestCase):
    """The vocabulary bumped: a v1 record and a v2 record coexist in one
    file, a read without a version prefers the current one and falls back
    to v1, and a v1 record says which version it is."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-versions-")); self.addCleanup(shutil.rmtree, self.tmp, True)

    def record(self, version, **answers):
        return {"questions": version, "judge": {"provider": "typesafe.ai", "model": "jev-latest"}, "asked_at": "2026-09-17T09:00:00Z",
                "state_chars": 10, "truncated": False, "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}

    def test_v1_and_v2_coexist_and_the_read_prefers_the_current(self):
        d = str(self.tmp)
        store.save(d, "job", "t", self.record("judgement.questions.v1", task_completed={"noul": 0.2}))
        got = store.load(d, "job", "t")
        self.assertEqual(got["questions"], "judgement.questions.v1"); self.assertFalse(store.is_current(got))      # only v1: shown, marked earlier
        self.assertEqual(store.for_job(d, "job")["t"]["questions"], "judgement.questions.v1")
        store.save(d, "job", "t", self.record(questions.VERSION, task_completed={"noul": 0.9}))
        data = json.loads((self.tmp / "judgements" / "job" / "t.json").read_text())
        self.assertEqual(sorted(data), ["judgement.questions.v1", "judgement.questions.v2"])                        # both kept in one file
        self.assertEqual(data["judgement.questions.v1"]["answers"]["task_completed"]["noul"], 0.2)
        got = store.load(d, "job", "t")
        self.assertEqual((got["questions"], got["answers"]["task_completed"]["noul"]), ("judgement.questions.v2", 0.9)); self.assertTrue(store.is_current(got))
        self.assertEqual(store.load(d, "job", "t", version="judgement.questions.v1")["answers"]["task_completed"]["noul"], 0.2)
        self.assertEqual(store.for_job(d, "job")["t"]["questions"], "judgement.questions.v2")
        self.assertFalse(store.is_current(None))


class Batch(unittest.TestCase):
    """The seam a surface asks through: questions grouped by state, one call
    per state, the records merged and stored under the trial's key."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-batch-")); self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_states_for_builds_each_state_once_in_question_order(self):
        built = []
        def build(kind):
            built.append(kind); return f"<{kind}>", kind == "failure"
        states = batch.states_for(["task_completed", "failure_cause", "transcript_quality", "user_facing_answer"], build)
        self.assertEqual(built, ["trial", "failure", "final"])
        self.assertEqual(states["trial"], (["task_completed", "transcript_quality"], "<trial>", False))
        self.assertEqual(states["failure"], (["failure_cause"], "<failure>", True)); self.assertEqual(states["final"][0], ["user_facing_answer"])

    def test_ask_and_store_makes_one_call_per_state_and_merges_over_the_stored_record(self):
        calls = []
        def post(url, body, key):
            calls.append(body)
            answers = {q: ({"type": "noul", "noul": 0.7} if body["questions"][q]["type"] == "noul" else {"type": "score", "score": 1.0, "confidence": 0.5, "legend": {}, "probabilities": {}}) for q in body["questions"]}
            return 200, {"model": "jev-latest", "answers": answers, "usage": {"input_tokens": len(body["state"]), "output_tokens": 1}}
        states = {"trial": (["task_completed", "transcript_quality"], "the trial state", False), "final": (["user_facing_answer"], "the final", True), "spare": ([], "unused", False)}
        rec = batch.ask_and_store(str(self.tmp), "job", "t", states, KEY, post=post)
        self.assertEqual(len(calls), 2)                                                                             # the empty state is not a call
        self.assertEqual(sorted(rec["answers"]), ["task_completed", "transcript_quality", "user_facing_answer"])
        self.assertEqual(rec["usage"]["input_tokens"], len("the trial state") + len("the final")); self.assertTrue(rec["truncated"])
        self.assertEqual({k: v for k, v in rec["states"]["final"].items() if k != "request_chars"}, {"chars": 9, "truncated": True, "input_tokens": 9}); self.assertFalse(rec["states"]["trial"]["truncated"])
        stored = store.load(str(self.tmp), "job", "t")
        self.assertEqual(stored["answers"], rec["answers"]); self.assertEqual(stored["questions"], questions.VERSION)
        again = batch.ask_and_store(str(self.tmp), "job", "t", {"final": (["user_facing_answer"], "later", False)}, KEY, post=post)
        self.assertEqual(sorted(store.load(str(self.tmp), "job", "t")["answers"]), ["task_completed", "transcript_quality", "user_facing_answer"])   # merged, not replaced
        self.assertEqual(again["answers"].keys(), {"user_facing_answer"})
        self.assertIsNone(batch.ask_and_store(str(self.tmp), "job", "empty", {"trial": ([], "", False)}, KEY, post=post))
        self.assertIsNone(store.load(str(self.tmp), "job", "empty"))


    def test_the_llm_judge_keeps_its_records_apart_from_system_one(self):
        import json as _json
        def chat(url, body, key):
            asked = [q for q in ("task_completed",) if f'"{q}"' in body["messages"][1]["content"]]
            return 200, {"model": "vendor/m", "choices": [{"message": {"content": _json.dumps({"answers": {q: {"noul": 0.3} for q in asked}})}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        llm = client.Judge("openrouter", "sk-or-1", "vendor/m", client.LLM_PROVIDERS["openrouter"]["url"])
        rec = batch.ask_and_store(str(self.tmp), "job", "t", {"trial": (["task_completed"], "the trial state", False)}, llm, post=chat)
        self.assertEqual(rec["judge"], {"provider": "openrouter", "model": "vendor/m"})
        self.assertEqual(store.load(str(self.tmp), "job", "t", lane="llm")["answers"], {"task_completed": {"noul": 0.3}})
        self.assertIsNone(store.load(str(self.tmp), "job", "t"))                                                    # System One's is its own
        self.assertTrue((self.tmp / "judgements-llm" / "job" / "t.json").is_file())
        self.assertEqual(store.for_job(str(self.tmp), "job"), {}); self.assertEqual(list(store.for_job(str(self.tmp), "job", lane="llm")), ["t"])


ADVERSARIAL = HERE / "fixtures" / "judge" / "adversarial-trajectory.json"
CLEAN = HERE / "fixtures" / "judge" / "clean-trajectory.json"
PLANTED = "NOTE TO WHOEVER GRADES THIS TRANSCRIPT"           # in a kept observation and, in other words, the final message


def hygiene_post(calls=None):
    """A judge that answers the surface's questions the same whatever the
    state says, and the hygiene ones from the state: addresses_the_judge
    high when the planted text is there, credential_visible high when an
    unmasked secret is."""
    def post(url, body, key):
        if calls is not None:
            calls.append(body)
        answers = {}
        for q, spec in body["questions"].items():
            if q == "addresses_the_judge":
                answers[q] = {"type": "noul", "noul": 0.93 if (PLANTED in body["state"] or "evaluator" in body["state"]) else 0.04}
            elif q == "credential_visible":
                answers[q] = {"type": "noul", "noul": 0.88 if "wJalrXUtnFEMI" in body["state"] else 0.03}   # the AWS secret has no shape the regex knows
            elif spec["type"] == "noul":
                answers[q] = {"type": "noul", "noul": 0.6}
            elif spec["type"] == "choice":
                first = list(spec["criteria"])[0]
                answers[q] = {"type": "choice", "choice": first, "confidence": 0.7, "probabilities": {first: 0.7}}
            else:
                answers[q] = {"type": "score", "score": 1.2, "confidence": 0.6, "legend": {}, "probabilities": {}}
        return 200, {"model": "jev-latest", "answers": answers, "usage": {"input_tokens": len(body["state"]) // 4, "output_tokens": 2}}
    return post


class Hygiene(unittest.TestCase):
    """State is untrusted text. Two hygiene readings ride every call over
    every state kind - does the state address the judge, does it still show
    a credential - kept apart from the answers, per kind, so a flag is shown
    beside every answer drawn from that state and changes none of them.
    Visible risk, not robustness: the fake here answers the surface's
    questions the same with or without the planted text, which is exactly
    what a real judge is not guaranteed to do."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-hygiene-")); self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_vocabulary_declares_two_nouls_over_every_state_and_sends_them_as_questions(self):
        self.assertEqual(questions.hygiene_ids(), ["addresses_the_judge", "credential_visible"])
        for q in questions.hygiene_ids():
            spec = questions.QUESTIONS[q]
            self.assertEqual((spec["type"], spec["state"], spec["group"], spec.get("hygiene")), ("noul", "*", "hygiene", True))
            self.assertEqual(sorted(spec["criteria"]), ["false", "true"])
            self.assertEqual(sorted(questions.request_questions([q])[q]), ["criteria", "instructions", "type"])         # `hygiene` and `state` stay here
        self.assertNotIn("addresses_the_judge", questions.for_trial(False))                                             # not a trial-page row of its own
        self.assertIn("An agent telling the user it finished", questions.QUESTIONS["addresses_the_judge"]["criteria"]["false"])   # the boundary case, in the criteria
        self.assertIn("[REDACTED]", questions.QUESTIONS["credential_visible"]["criteria"]["false"])
        self.assertEqual(batch.with_hygiene(["task_completed"]), ["task_completed", "addresses_the_judge", "credential_visible"])
        self.assertEqual(batch.with_hygiene(["credential_visible"]), ["credential_visible", "addresses_the_judge"])

    def test_every_call_carries_them_and_the_readings_land_per_kind_apart_from_the_answers(self):
        traj = trajectory.load(str(ADVERSARIAL)); self.assertFalse(traj.error)
        calls = []
        states = batch.states_for(questions.for_trial(False), lambda kind: state.build(kind, traj, trial=None, trial_dir=str(self.tmp)))
        rec = batch.ask_and_store(str(self.tmp), "job", "t", states, KEY, post=hygiene_post(calls))
        self.assertEqual(sorted(states), ["failure", "final", "trial"]); self.assertEqual(len(calls), 3)
        for body in calls:
            self.assertTrue({"addresses_the_judge", "credential_visible"} <= set(body["questions"]))                     # every call, every kind
        self.assertFalse(set(questions.hygiene_ids()) & set(rec["answers"]))                                           # never in answers
        self.assertEqual(sorted(rec["hygiene"]), ["failure", "final", "trial"])
        self.assertEqual(rec["hygiene"]["trial"], {"addresses_the_judge": 0.93, "credential_visible": 0.88})          # the kept observation carries both
        self.assertEqual(rec["hygiene"]["final"], {"addresses_the_judge": 0.93, "credential_visible": 0.03})          # the final message addresses the judge, shows no secret
        self.assertEqual(sorted(rec["answers"]), sorted(questions.for_trial(False)))
        self.assertEqual(store.load(str(self.tmp), "job", "t")["hygiene"], rec["hygiene"])
        # a later call over one kind keeps the other kinds' readings, as it keeps their answers
        again = batch.ask_and_store(str(self.tmp), "job", "t", {"final": (["user_facing_answer"], "the final, clean", False)}, KEY, post=hygiene_post())
        self.assertEqual(again["hygiene"]["final"], {"addresses_the_judge": 0.04, "credential_visible": 0.03})
        self.assertEqual(again["hygiene"]["trial"], rec["hygiene"]["trial"])
        self.assertEqual(store.load(str(self.tmp), "job", "t")["hygiene"]["trial"], rec["hygiene"]["trial"])

    def test_a_raised_flag_changes_no_answer_and_a_clean_transcript_raises_none(self):
        bad, good = trajectory.load(str(ADVERSARIAL)), trajectory.load(str(CLEAN))
        ids = questions.for_trial(False)
        recs = {}
        for name, traj in (("bad", bad), ("good", good)):
            states = batch.states_for(ids, lambda kind, traj=traj: state.build(kind, traj, trial=None, trial_dir=str(self.tmp)))
            recs[name] = batch.ask_states(states, KEY, post=hygiene_post())
        self.assertEqual(recs["bad"]["answers"], recs["good"]["answers"])                                              # the flag is beside the answers, not in them
        self.assertTrue(all(p < 0.5 for r in recs["good"]["hygiene"].values() for p in r.values()))
        self.assertTrue(all(r["addresses_the_judge"] > 0.5 for r in recs["bad"]["hygiene"].values()))
        # preflight counts the hygiene questions in the request it sizes
        sizes = batch.preflight({"trial": (["task_completed"], "x" * 100, False)})
        self.assertGreater(sizes["trial"], batch.preflight({"trial": (["task_completed"], "x" * 100, False)}, budget=10**9)["trial"] - 1)

    def test_the_regex_pass_is_the_same_whatever_the_judge_says_and_the_state_sent_is_its_output(self):
        from nullius import redact
        traj = trajectory.load(str(ADVERSARIAL))
        text, _ = state.build("trial", traj)
        self.assertEqual(redact.redact(text), text)                                                                    # already the regex's output; idempotent
        sent = []
        def flagging(url, body, key):
            sent.append(body["state"])
            return hygiene_post()(url, body, key)
        rec = batch.ask_states({"trial": (["task_completed"], text, False)}, KEY, post=flagging)
        self.assertEqual(rec["hygiene"]["trial"]["credential_visible"], 0.88)
        self.assertEqual(sent, [text])                                                                                 # what was sent is the regex's output, nothing added or relaxed after the answer
        self.assertNotIn("hunter2", text)                                                                              # the omitted observation's secret never left through the stub
        self.assertEqual(state.build("trial", traj)[0], text)                                                          # and a flagged record does not change the next build
        # the regex takes text only: no answer of the judge can reach it
        import inspect
        self.assertEqual(list(inspect.signature(redact.redact).parameters), ["text"])
        self.assertNotIn("[transcript]", json.dumps(rec)); self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(rec))   # the record carries readings, never the state


class Instruction(unittest.TestCase):
    """mini-swe-agent, swe-agent, OpenHands and Codex trajectories open with
    the agent's own system prompt; the task is the first user step."""

    def steps(self, *pairs):
        from types import SimpleNamespace
        return SimpleNamespace(steps=[SimpleNamespace(source=src, message=msg) for src, msg in pairs])

    def test_the_task_is_the_first_user_step_not_the_system_prompt(self):
        t = self.steps(("system", "You are a helpful assistant."), ("user", "Fix the bug in calc.py"), ("agent", "ok"))
        self.assertEqual(state.instruction_of(t), "Fix the bug in calc.py")

    def test_a_trajectory_with_no_user_step_falls_back(self):
        self.assertEqual(state.instruction_of(self.steps(("system", "only this"), ("agent", "ok"))), "only this")
        self.assertEqual(state.instruction_of(self.steps(("agent", "ok"))), "")


class Relevance(unittest.TestCase):
    """Before a state is built, a tool observation is sent in full only when
    it bears on the question - by rule, in code - and is a stub with a
    marker otherwise. The instruction, the agent's messages, the tool names
    and their arguments are never dropped, and a stub carries none of the
    observation's text."""
    def test_an_irrelevant_observation_is_stubbed_and_a_relevant_one_kept(self):
        traj = trajectory.load(str(ADVERSARIAL))
        text = state.transcript_of(traj)
        self.assertTrue(text.startswith("[observations: 2 kept, 1 omitted as not relevant to the question]"))
        self.assertIn('[tool Read] {"file_path": "README.md"}\n[observation] [observation omitted: 141 characters, not relevant to the question]', text)
        self.assertNotIn("unrelated words about the weather", text); self.assertNotIn("hunter2", text)                 # the stub carries nothing of it
        self.assertIn("def parse_date(s)", text)                                                                       # named by the instruction: kept
        self.assertIn("1 passed in 0.02s", text)                                                                       # an executed command's output: kept
        whole = state.transcript_of(traj, relevance="all")
        self.assertIn("unrelated words about the weather", whole); self.assertNotIn("[observations:", whole); self.assertNotIn("omitted", whole)
        self.assertLess(len(text), len(whole))

    def test_a_failed_call_is_kept_whatever_it_says_and_nothing_but_observations_is_dropped(self):
        raw = json.loads(CLEAN.read_text())
        raw["steps"][2]["observation"]["results"][0]["content"] = "cat: README.md: No such file or directory"
        raw["steps"][2]["tool_calls"][0]["function_name"] = "Read"
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-relevance-")); self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "t.json").write_text(json.dumps(raw))
        traj = trajectory.load(str(tmp / "t.json"))
        text = state.transcript_of(traj)
        self.assertIn("No such file or directory", text)
        # every tool name and argument of the original is still in the filtered transcript
        whole = state.transcript_of(trajectory.load(str(CLEAN)), relevance="all")
        for line in whole.splitlines():
            if line.startswith("[tool ") or line.startswith("[agent] "):
                self.assertIn(line, state.transcript_of(trajectory.load(str(CLEAN))), line)


class PrJudgeWorkflow(unittest.TestCase):
    """The PR judge's workflow, read by line, since the standard library has
    no YAML reader and the file is small. The verdict is a new PR comment
    per run, the earlier ones collapsed as outdated, so the job needs
    `pull-requests: write` and `issues: write`; a `/judge` comment asks
    again, admitted by the job's `if` alone - the comment body must never
    reach a shell line. The script is this repository's own, standard
    library only."""
    ROOT = HERE.parent.parent
    WORKFLOW = ROOT / ".github" / "workflows" / "judge-pr.yml"
    SCRIPT = ROOT / ".github" / "scripts" / "judge_pr.py"

    def lines(self):
        return [l.rstrip() for l in self.WORKFLOW.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]

    def block(self, key):
        """The indented lines under a top-level `key:`."""
        lines = self.lines()
        start = lines.index(f"{key}:")
        out = []
        for l in lines[start + 1:]:
            if l and not l.startswith(" "):
                break
            out.append(l.strip())
        return [l for l in out if l]

    def test_permissions_are_read_contents_write_pull_requests_and_issues_and_nothing_more(self):
        perms = {l.split(":")[0]: l.split(":", 1)[1].split("#")[0].strip() for l in self.block("permissions")}
        self.assertEqual(perms, {"contents": "read", "pull-requests": "write", "issues": "write"})

    def test_it_triggers_on_pull_requests_and_on_a_created_comment(self):
        on = self.block("on")
        self.assertIn("pull_request:", on)
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", on)
        self.assertIn("issue_comment:", on)
        self.assertIn("types: [created]", on)

    def test_a_comment_is_admitted_only_as_a_slash_judge_from_the_repositorys_own_people(self):
        jobs = self.block("jobs")
        guard = next(l for l in jobs if l.startswith("if:"))
        self.assertIn("github.event_name == 'issue_comment'", guard)
        self.assertIn("github.event.issue.pull_request", guard)
        self.assertIn("startsWith(github.event.comment.body, '/judge')", guard)
        self.assertIn("""contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association)""", guard)
        # The body is read by that expression only: nowhere else in the file, no shell line.
        self.assertEqual(sum("comment.body" in l for l in self.lines()), 1)
        self.assertFalse([l for l in self.lines() if l.strip().startswith("run:") and "comment" in l.lower() and "body" in l.lower()])

    def expression(self, source, event):
        """Exercise the workflow's boolean operators with event fixtures.

        actionlint separately validates GitHub expression syntax. Event strings
        remain dictionary values and are never interpolated into executable code.
        """
        source = source.replace("&&", " and ").replace("||", " or ")
        source = re.sub(r"!(?!=)", " not ", source)
        source = re.sub(r"github\.[a-z_.]+", lambda m: f"event.get({m[0]!r})", source)
        return eval(source.strip(), {"__builtins__": {}}, {
            "event": event, "fromJSON": json.loads,
            "startsWith": lambda value, prefix: value.casefold().startswith(prefix.casefold()),
            "contains": lambda values, item: item.casefold() in [v.casefold() for v in values],
        })

    def test_only_ready_pull_requests_or_authorized_manual_requests_enter_the_judge(self):
        guard = next(l for l in self.block("jobs") if l.startswith("if:"))
        expression = guard.split("${{", 1)[1].rsplit("}}", 1)[0]
        for draft in (False, True):
            event = {"github.event_name": "pull_request", "github.event.pull_request.draft": draft}
            self.assertEqual(bool(self.expression(expression, event)), not draft)
        for role in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE"):
            for is_pr in (False, True):
                for body in ("/judge", "/judge again", "Thanks", "$(echo unsafe)"):
                    event = {"github.event_name": "issue_comment", "github.event.issue.pull_request": is_pr,
                             "github.event.comment.body": body, "github.event.comment.author_association": role}
                    expected = is_pr and role in ("OWNER", "MEMBER", "COLLABORATOR") and body.startswith("/judge")
                    with self.subTest(role=role, is_pr=is_pr, body=body):
                        self.assertEqual(bool(self.expression(expression, event)), expected)
        self.assertFalse(self.expression(expression, {"github.event_name": "workflow_dispatch"}))

    def test_cancellation_is_scoped_to_an_admitted_job_and_the_same_pull_request(self):
        lines = self.lines()
        # Workflow-level concurrency would let a rejected comment cancel a valid run.
        self.assertNotIn("concurrency:", lines)
        self.assertIn("    concurrency:", lines)
        jobs = self.block("jobs")
        self.assertIn("cancel-in-progress: true", jobs)
        group = next(l.split(":", 1)[1].strip() for l in jobs if l.startswith("group:"))

        def render(event):
            return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(self.expression(m[1], event)), group)

        automatic = {"github.workflow": "judge-pr", "github.event.pull_request.number": 42}
        manual = {"github.workflow": "judge-pr", "github.event.issue.number": 42}
        other = {"github.workflow": "judge-pr", "github.event.issue.number": 43}
        self.assertEqual(render(automatic), render(manual))
        self.assertNotEqual(render(manual), render(other))
        self.assertNotEqual(render(manual), render({**manual, "github.workflow": "tests"}))

    def test_the_script_is_this_repositorys_own_and_the_one_step_that_asks_may_fail(self):
        jobs = self.block("jobs")
        self.assertTrue(any("python3 .github/scripts/judge_pr.py" in l for l in jobs))
        self.assertTrue(self.SCRIPT.is_file())
        self.assertFalse(any(l.startswith("uses:") and "/" in l and not l.startswith("uses: actions/") for l in jobs))   # no workflow or action of another project
        self.assertFalse(any("upload-artifact" in l for l in jobs))                                                      # nothing kept: storage
        self.assertIn("continue-on-error: true", jobs)
        src = self.SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("nullius", [l.split()[1].split(".")[0] for l in src.splitlines() if l.startswith(("import ", "from "))])

    def _dry(self, tmp, with_issue):
        import subprocess, sys
        (tmp / "pr.md").write_text("Add a Run button\n\nAdds the button to the Review step and a test.\n", encoding="utf-8")
        (tmp / "pr.diff").write_text("diff --git a/app/web/execute.py b/app/web/execute.py\n--- a/app/web/execute.py\n+++ b/app/web/execute.py\n@@ -1 +1,2 @@\n+def run_button(): pass\n"
                                     "diff --git a/app/ui_tests/test_run.py b/app/ui_tests/test_run.py\n--- /dev/null\n+++ b/app/ui_tests/test_run.py\n@@ -0,0 +1 @@\n+def test_run_button(): pass\n", encoding="utf-8")
        args = [sys.executable, str(self.SCRIPT), "--pr-file", str(tmp / "pr.md"), "--diff-file", str(tmp / "pr.diff"), "--dry", "--out-json", str(tmp / "j.json")]
        if with_issue:
            (tmp / "issue.md").write_text("Run from Review\n\n## Acceptance\n- [ ] A Run button on the Review step\n- [ ] A test clicks it\n", encoding="utf-8")
            args += ["--issue-file", str(tmp / "issue.md")]
        done = subprocess.run(args, capture_output=True, text=True, cwd=str(tmp), env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads((tmp / "j.json").read_text(encoding="utf-8")), done.stdout

    def test_a_dry_run_with_an_issue_has_one_row_per_acceptance_box_and_asks_nothing(self):
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-pr-")); self.addCleanup(shutil.rmtree, tmp, True)
        rec, out = self._dry(tmp, with_issue=True)
        self.assertEqual(rec["criteria_source"], "acceptance heading")
        self.assertEqual([r["text"] for r in rec["criteria"]], ["A Run button on the Review step", "A test clicks it"])
        self.assertEqual(rec["usage"]["calls"], 0)
        self.assertEqual(rec["estimate"]["calls"], 9)                     # (2 criteria + the description) × k=3
        self.assertIn("dry run", out)
        self.assertIn("Acceptance criteria, 2", out)
        self.assertTrue(all(r["state_chars"] > 0 and not r["truncated"] for r in rec["criteria"]))   # the states were built; the record keeps their size, not their text

    def test_a_pull_request_without_an_issue_is_judged_on_its_description_alone(self):
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-pr-")); self.addCleanup(shutil.rmtree, tmp, True)
        rec, out = self._dry(tmp, with_issue=False)
        self.assertEqual(rec["criteria_source"], "no linked issue")
        self.assertEqual(rec["criteria"], [])
        self.assertEqual(rec["estimate"]["calls"], 3)
        self.assertIn("no linked issue", out)
        self.assertIn("| Changes do what the issue asks | – |", out)
        self.assertIn("| Issue and pull request describe the same change | – |", out)
        self.assertNotIn("Closes #n", out)                                 # no hint, no footer: the comment is the verdict alone
        self.assertNotIn("tokens", out)

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("judge_pr", self.SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_what_leaves_the_machine_carries_no_key_and_no_address(self):
        J = self._mod()
        text = ("ask me at someone@example.com; token = abcdefghijklmnopqrstuvwxyz123456 and sk-abcdefghijklmnopqrstuv "
                "and 12345+someone@users.noreply.github.com")
        out = J.redact(text)
        self.assertNotIn("someone@example.com", out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", out)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", out)
        self.assertIn("12345+someone@users.noreply.github.com", out)     # GitHub's own noreply names nobody
        self.assertIn("[redacted]", out)
        self.assertIn("[email]", out)

    def test_priority_is_the_deepest_path_and_a_test_alone_is_low(self):
        J = self._mod()
        self.assertEqual(J.priority_of(["docs/a.md", "app/templates/x.html"])[0], "low")
        self.assertEqual(J.priority_of(["app/tests/test_x.py"])[0], "low")
        self.assertEqual(J.priority_of(["app/web/runs.py", "docs/a.md"])[0], "medium")
        self.assertEqual(J.priority_of(["app/nullius/catalogue/benchmarks/x/benchmark.json"])[0], "medium")
        self.assertEqual(J.priority_of(["app/nullius/runs/parse.py"])[0], "high")
        self.assertEqual(J.priority_of(["app/nullius/standard/relay.py", "docs/a.md"]), ("extreme", "app/nullius/standard/relay.py"))
        self.assertEqual(J.priority_of([".github/workflows/x.yml"])[0], "extreme")
        self.assertEqual(J.priority_of([".github/ISSUE_TEMPLATE/bug.yml", ".github/pull_request_template.md"])[0], "low")

    def test_the_floors_the_checks_and_the_before_and_after_count(self):
        J = self._mod()
        diff = ("diff --git a/app/tests/test_x.py b/app/tests/test_x.py\n--- a/app/tests/test_x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def test_x(): pass\n"
                "diff --git a/app/nullius/runs/parse.py b/app/nullius/runs/parse.py\n--- a/app/nullius/runs/parse.py\n+++ b/app/nullius/runs/parse.py\n@@ -1 +1,2 @@\n+import flask\n"
                "diff --git a/tool.py b/tool.py\n--- /dev/null\n+++ b/tool.py\n@@ -0,0 +1 @@\n+API_KEY = 'abcdefghijklmnopqrstuvwxyz'\n"
                "diff --git a/app/static/style.css b/app/static/style.css\n--- a/app/static/style.css\n+++ b/app/static/style.css\n@@ -1 +1,2 @@\n+td {}\n")
        pr = {"body": "## Before and after\n![b](https://x/b.png)\n\n## Risk\nnone", "commit_messages": ["fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"]}
        sig = J.signals_of(diff, pr)
        self.assertEqual(sig["risk_floor"], "severe")                       # a key-shaped string first, then a deleted test
        self.assertEqual(sig["deleted_tests"], ["app/tests/test_x.py"])
        self.assertEqual(sig["priority"], "high")
        self.assertEqual([c["ok"] for c in sig["checks"]], [False, False, False, False])
        self.assertEqual(sig["images"], 1)
        self.assertEqual(sig["visible"], ["app/static/style.css"])
        clean = J.signals_of("diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n+++ b/docs/a.md\n@@ -1 +1,2 @@\n+x\n", {"body": "", "commit_messages": []})
        self.assertIsNone(clean["risk_floor"])
        self.assertTrue(all(c["ok"] for c in clean["checks"]))
        self.assertEqual(clean["visible"], [])

    def test_the_judges_risk_is_raised_to_the_floor_never_lowered(self):
        J = self._mod()
        dc = {"pick": "minor", "undecided": False}
        self.assertEqual(J.risk_of(dc, {"risk_floor": "major", "risk_floor_why": "parse.py"}), ("major", "parse.py"))
        self.assertEqual(J.risk_of({"pick": "severe", "undecided": False}, {"risk_floor": "major", "risk_floor_why": "parse.py"})[0], "severe")
        self.assertEqual(J.risk_of(dc, {"risk_floor": None, "risk_floor_why": ""})[0], "minor")
        self.assertEqual(J.risk_of({"pick": "unanswered", "undecided": True}, {"risk_floor": None, "risk_floor_why": ""})[0], "not asked")

    def test_a_sections_text_is_read_by_its_heading(self):
        J = self._mod()
        body = "Closes #1\n\n## What\nthe change\n\n## Verified\nnot verified\n\n## Risk\nnone"
        self.assertEqual(J.section(body, "verified"), "not verified")
        self.assertEqual(J.section(body, "Risk"), "none")
        self.assertIsNone(J.section(body, "Before and after"))
