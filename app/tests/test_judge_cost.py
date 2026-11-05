"""The cost of a judgement, stated honestly: an estimate is a point and a
band with its basis, on a price recorded with its date and never fetched;
the band tightens to what the workspace's own stored calls came back as;
truncation is counted per state kind so a state that hits the limit can be
named; and a call the judge's budget would refuse is refused here first,
naming the state, before anything is sent. Every number is arithmetic in
code - the judge is never asked for one - and nothing here touches the
network."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from nullius.judge import batch, client, questions, state, store

KEY = "sk-fixture-key-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def record(states: dict, truncated: bool = False, version: str = questions.VERSION) -> dict:
    """A stored record with per-state sizes, as client.merge writes them."""
    return {"questions": version, "judge": {"provider": "typesafe.ai", "model": "jev-latest"}, "asked_at": "2026-09-18T09:00:00Z",
            "state_chars": max((e["chars"] for e in states.values()), default=0), "truncated": truncated, "answers": {"task_completed": {"noul": 0.5}},
            "usage": {"input_tokens": sum(e["input_tokens"] or 0 for e in states.values()), "output_tokens": 1}, "states": states}


class Estimate(unittest.TestCase):
    def test_a_point_and_a_band_on_the_rule_of_thumb(self):
        est = client.estimate(18342, today="2026-09-18")
        self.assertEqual((est["calls"], est["input_tokens"], est["usd"]), (1, 6115, 0.000257))                     # the point: 3.0 characters per token, measured
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (int(18342 // 3.5) + 1, int(18342 // 2.5) + 1))   # the band: 2.5 to 3.5 characters per token
        self.assertLess(est["usd_low"], est["usd"]); self.assertLess(est["usd"], est["usd_high"])
        self.assertEqual(est["basis"], "reference captures: 3 characters per token; firms up once this workspace has judged a trial"); self.assertEqual(est["chars_per_token"], 3.0)
        self.assertEqual((est["price_per_mtok"], est["price_as_of"], est["price_stale"]), (0.042, "2026-09-17", False))
        self.assertEqual(client.estimate(400_000, calls=20)["input_tokens"], 133_353)
        self.assertEqual(client.estimate(0, calls=0)["tokens_high"], 0)

    def test_the_price_is_dated_never_fetched_and_says_when_it_is_old(self):
        self.assertFalse(client.price_stale("2026-09-17")); self.assertFalse(client.price_stale("2026-12-16"))
        self.assertTrue(client.price_stale("2026-12-17")); self.assertTrue(client.price_stale("2027-09-17"))
        self.assertTrue(client.estimate(100, today="2027-01-01")["price_stale"])
        self.assertIsInstance(client.price_stale(), bool)
        src = Path(client.__file__).read_text(encoding="utf-8")
        self.assertNotIn("urlopen(\"https://docs", src); self.assertIn('PRICE_AS_OF = "2026-09-17"', src)          # a constant with a date, not a fetch

    def test_calibration_from_stored_calls_tightens_the_band(self):
        recs = [record({"trial": {"chars": 10000, "truncated": False, "input_tokens": 2500}, "final": {"chars": 300, "truncated": False, "input_tokens": 100}}),
                record({"failure": {"chars": 20000, "truncated": True, "input_tokens": 5000}})]
        cal = client.calibrate(recs)
        self.assertEqual(cal, {"chars_per_token": round(30300 / 7600, 3), "n": 3, "low": 4.0, "high": 4.0, "n_large": 2})   # the band over the large samples; the 300-character call is pooled, not an edge
        est = client.estimate(30300, calls=1, calibration=cal, today="2026-09-18")
        self.assertEqual(est["input_tokens"], int(30300 // cal["chars_per_token"]) + 1)
        self.assertEqual((est["tokens_low"], est["tokens_high"]), (30300 // 4 + 1, 30300 // 4 + 1))                # the band is the large calls' own spread
        self.assertEqual(est["basis"], f"3 stored calls: {cal['chars_per_token']:g} characters per token")
        # a request's characters are the state and the questions: a record that kept them is read by them
        req = client.calibrate([record({"trial": {"chars": 800, "truncated": False, "input_tokens": 600, "request_chars": 5600}})])
        self.assertEqual((req["chars_per_token"], req["low"], req["n_large"]), (round(5600 / 600, 3), round(5600 / 600, 3), 1))
        # small samples only: the band is the pooled ratio widened to the default band, never a small call's own ratio
        small = client.calibrate([record({"final": {"chars": 800, "truncated": False, "input_tokens": 600}})])
        self.assertEqual((small["low"], small["high"], small["n_large"]), (round(800 / 600, 3), 3.5, 0))

    def test_one_small_call_in_the_store_does_not_make_a_full_state_fail_preflight(self):
        """The larger trial call sets the token estimate's lower bound;
        a short final answer must not inflate the next full trial's estimate."""
        recs = [record({"trial": {"chars": state.LIMIT, "truncated": True, "input_tokens": 21000, "request_chars": state.LIMIT + 4900},
                        "final": {"chars": 809, "truncated": False, "input_tokens": 560, "request_chars": 809 + 1500}})]
        cal = client.calibrate(recs)
        self.assertLess(cal["low"], 3.2); self.assertGreater(cal["low"], 2.9)                                       # the trial call sets the edge, not the final
        states = {"trial": (["task_completed", "transcript_quality"], "x" * state.LIMIT, True)}
        self.assertLess(batch.preflight(states, calibration=cal)["trial"], client.BUDGET_TOKENS)
        # a sample needs both sides; a v1 record's whole-call numbers are not one (a maximum beside a sum)
        self.assertIsNone(client.calibrate([]))
        self.assertIsNone(client.calibrate([record({}, version="judgement.questions.v1")]))
        self.assertIsNone(client.calibrate([record({"trial": {"chars": 100, "truncated": False, "input_tokens": None}})]))
        self.assertIsNone(client.calibrate([record({"trial": {"chars": 0, "truncated": False, "input_tokens": 3}})]))
        one = client.calibrate([record({"trial": {"chars": 90, "truncated": False, "input_tokens": 30}})])
        self.assertEqual(one["n"], 1); self.assertIn("1 stored call:", client.estimate(10, calibration=one)["basis"])


class Stored(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-cost-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = str(self.tmp)

    def test_the_store_walks_every_record_and_every_version_and_calibrates(self):
        self.assertEqual(list(store.all_records(self.d)), []); self.assertIsNone(store.calibration(self.d)); self.assertIsNone(store.calibration(None))
        store.save(self.d, "job-a", "t1", record({"trial": {"chars": 400, "truncated": False, "input_tokens": 100}}))
        store.save(self.d, "job-a", "t1", record({}, version="judgement.questions.v1"))
        store.save(self.d, "job-b", "t/2", record({"pair": {"chars": 900, "truncated": False, "input_tokens": 300}}))
        (self.tmp / "judgements" / "job-a" / "broken.json").write_text("{not json")
        (self.tmp / "judgements" / "job-a" / "notes.txt").write_text("x")
        seen = sorted((j, t, v) for j, t, v, _ in store.all_records(self.d))
        self.assertEqual(seen, [("job-a", "t1", "judgement.questions.v1"), ("job-a", "t1", "judgement.questions.v2"), ("job-b", "t_2", "judgement.questions.v2")])
        self.assertEqual([t for _, t, _, _ in store.all_records(self.d, "job-b")], ["t_2"])
        self.assertEqual(store.calibration(self.d), {"chars_per_token": 3.25, "n": 2, "low": 2.5, "high": 3.5, "n_large": 0})   # small samples only: the band widened to the default

    def test_truncation_is_counted_per_state_kind_so_the_cut_state_can_be_named(self):
        limit = state.LIMIT
        store.save(self.d, "job", "t1", record({"trial": {"chars": limit, "truncated": True, "input_tokens": 15000}, "final": {"chars": 50, "truncated": False, "input_tokens": 12}}, truncated=True))
        store.save(self.d, "job", "t2", record({"trial": {"chars": 3000, "truncated": False, "input_tokens": 700}, "failure": {"chars": limit, "truncated": True, "input_tokens": 15000}}, truncated=True))
        store.save(self.d, "job", "t3", record({"trial": {"chars": 200, "truncated": False, "input_tokens": 50}}))
        store.save(self.d, "job", "old", {"questions": "judgement.questions.v1", "judge": {"model": "m"}, "asked_at": "x", "state_chars": 9, "truncated": True, "answers": {}, "usage": {}})
        store.save(self.d, "other", "t9", record({"trial": {"chars": limit, "truncated": True, "input_tokens": 1}}, truncated=True))
        rep = store.truncation_report(self.d, "job")
        self.assertEqual((rep["limit"], rep["records"], rep["truncated"], rep["untyped"]), (limit, 4, 3, 1))
        self.assertEqual(list(rep["kinds"]), ["failure", "trial", "final"])                                       # most truncated first, then by name
        self.assertEqual(rep["kinds"]["trial"], {"calls": 3, "truncated": 1, "max_chars": limit})
        self.assertEqual(rep["kinds"]["failure"], {"calls": 1, "truncated": 1, "max_chars": limit})
        self.assertEqual(rep["kinds"]["final"], {"calls": 1, "truncated": 0, "max_chars": 50})
        self.assertEqual(store.truncation_report(self.d, "nowhere"), {"limit": limit, "records": 0, "truncated": 0, "untyped": 0, "kinds": {}})
        self.assertEqual(store.truncation_report(self.d, "job", limit=10)["limit"], 10)

    def test_every_call_through_the_seam_records_its_state_kind(self):
        def post(url, body, key):
            # the judge charges the whole request - the state and the questions' JSON - at 4 characters per token
            return 200, {"model": "jev-latest", "answers": {q: {"type": "noul", "noul": 0.5} for q in body["questions"]},
                         "usage": {"input_tokens": (len(body["state"]) + len(json.dumps(body["questions"]))) // 4, "output_tokens": 1}}
        states = {"trial": (["task_completed"], "t" * 400, False), "final": (["user_facing_answer"], "f" * 80, True)}
        rec = batch.ask_and_store(self.d, "job", "t", states, KEY, post=post)
        self.assertEqual({k: {kk: vv for kk, vv in v.items() if kk not in ("request_chars", "input_tokens")} for k, v in rec["states"].items()},
                         {"trial": {"chars": 400, "truncated": False}, "final": {"chars": 80, "truncated": True}})
        self.assertTrue(all(v["request_chars"] > v["chars"] and v["input_tokens"] == v["request_chars"] // 4 for v in rec["states"].values()))
        rep = store.truncation_report(self.d, "job")
        self.assertEqual((rep["kinds"]["final"]["truncated"], rep["kinds"]["trial"]["truncated"], rep["untyped"]), (1, 0, 0))
        cal = store.calibration(self.d)
        self.assertEqual((round(cal["chars_per_token"]), cal["n"], cal["n_large"]), (4, 2, 0))                     # read over the request, not the state alone
        self.assertEqual((cal["low"], cal["high"]), (2.5, round(cal["chars_per_token"], 3)))                        # small samples: the pooled ratio widened to the default band
        self.assertNotIn(KEY, json.dumps(rec))


class Preflight(unittest.TestCase):
    """A batch the judge's budget would refuse is refused here, naming the state, before any call."""
    def states(self, trial_chars: int, final_chars: int = 10):
        return {"trial": (["task_completed", "transcript_quality"], "t" * trial_chars, False), "final": (["user_facing_answer"], "f" * final_chars, False),
                "spare": ([], "s" * 10 ** 6, False)}

    def test_it_counts_the_questions_with_the_state_at_the_bands_upper_edge(self):
        out = batch.preflight(self.states(3000))
        qs = len(json.dumps(questions.request_questions(batch.with_hygiene(["task_completed", "transcript_quality"]))))   # every call carries the hygiene pair
        self.assertEqual(out["trial"], int((3000 + qs) // 2.5) + 1); self.assertNotIn("spare", out)              # no questions, no call, no check
        self.assertEqual(batch.preflight({}), {})

    def test_a_state_over_budget_is_refused_locally_and_named(self):
        calls = []
        def post(url, body, key):
            calls.append(body); return 200, {"answers": {}, "usage": {}}
        with self.assertRaises(client.JudgeError) as cm:
            batch.ask_states(self.states(3000), KEY, post=post, budget=1000)
        self.assertEqual(cm.exception.status, 413)
        msg = str(cm.exception)
        self.assertIn("the `trial` state is too big", msg); self.assertIn("budget of 1,000", msg); self.assertIn("Nothing was sent", msg); self.assertIn("reference captures", msg)
        self.assertEqual(calls, [])                                                                                  # refused before the first call
        # the boundary: exactly the budget passes, one token more does not
        qs = len(json.dumps(questions.request_questions(batch.with_hygiene(["task_completed", "transcript_quality"]))))   # every call carries the hygiene pair
        fits = {"trial": (["task_completed", "transcript_quality"], "t" * (int(2.5 * 1000) - qs), False)}       # 2,500 characters at the band's edge of 2.5 = 1,000 tokens, plus the call
        self.assertEqual(batch.preflight(fits, budget=1001), {"trial": 1001})
        with self.assertRaises(client.JudgeError):
            batch.preflight(fits, budget=1000)

    def test_a_calibration_below_the_rule_of_thumb_is_what_makes_a_built_in_state_fail(self):
        text = "x" * state.LIMIT
        states = {"trial": (["task_completed"], text, True)}
        self.assertLess(batch.preflight(states)["trial"], client.BUDGET_TOKENS)                                    # 60,000 characters fit on the measured ratio, even at the band's edge of 2.5 per token
        tight = {"chars_per_token": 1.6, "n": 4, "low": 1.5, "high": 1.8}
        with self.assertRaises(client.JudgeError) as cm:
            batch.preflight(states, calibration=tight)
        self.assertIn("4 stored calls: 1.6 characters per token", str(cm.exception)); self.assertIn("characters - over", str(cm.exception))
        # ask_and_store reads the workspace's own calibration
        tmp = Path(tempfile.mkdtemp(prefix="nullius-judge-preflight-")); self.addCleanup(shutil.rmtree, tmp, True)
        store.save(str(tmp), "job", "seen", record({"trial": {"chars": 150, "truncated": False, "input_tokens": 100}}))
        calls = []
        def post(url, body, key):
            calls.append(1); return 200, {"answers": {}, "usage": {}}
        with self.assertRaises(client.JudgeError):
            batch.ask_and_store(str(tmp), "job", "t", states, KEY, post=post)
        self.assertEqual(calls, []); self.assertIsNone(store.load(str(tmp), "job", "t"))
        batch.ask_and_store(str(tmp), "job", "t", states, KEY, post=post, calibration={"chars_per_token": 4, "n": 1, "low": 4, "high": 4})
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
