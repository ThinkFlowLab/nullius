"""Fidelity's comparison, on canned relay records - no agent, no network:
what the relay keeps of a request, which fields are noise, which differ."""
from __future__ import annotations

import unittest

from nullius.standard import fidelity, relay

CONTEXT = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "permissions"}]},
           {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "write result.txt"}]}]
TOOLS = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
         {"type": "web_search"}]


def request(effort=None, cache="k-1", tools=TOOLS, context=CONTEXT, turn=0):
    r = {"model": "gpt-5-mini", "instructions": "You are Codex.", "input": context + [{"type": "reasoning", "turn": turn}] * turn,
         "tools": tools, "tool_choice": "auto", "stream": True, "store": False, "reasoning": {"summary": "auto"},
         "prompt_cache_key": cache, "client_metadata": {"turn_id": cache}}
    if effort:
        r["reasoning"]["effort"] = effort
    return r


def records(*reqs):
    return [{"path": "/responses", "status": 200, "fields": relay.request_fields(r)} for r in reqs]


def a_pass(effort=None, cache="k", n=2, **kw):
    return {f"u{u}": records(*[request(effort, cache=f"{cache}-{u}-{t}", turn=t, **kw) for t in range(n)]) for u in range(2)}


class Fields(unittest.TestCase):
    def test_the_prompt_is_kept_as_digests_never_as_text(self):
        f = relay.request_fields(request("medium"))
        self.assertEqual([(i["type"], i["role"]) for i in f["input"]], [("message", "developer"), ("message", "user")])
        self.assertEqual(set(f["instructions"]), {"sha256", "chars"})
        self.assertEqual([t["name"] for t in f["tools"]], ["exec_command", "web_search"])
        self.assertEqual(f["reasoning"], {"summary": "auto", "effort": "medium"})
        self.assertNotIn("write result.txt", repr(f))
        self.assertNotIn("You are Codex", repr(f))

    def test_an_items_own_id_is_no_difference(self):
        a = relay.request_fields(request(context=[dict(CONTEXT[1], id="msg_1")]))
        b = relay.request_fields(request(context=[dict(CONTEXT[1], id="msg_2")]))
        self.assertEqual(a["input"], b["input"])


class Compare(unittest.TestCase):
    def test_the_same_requests_are_the_same_whatever_their_ids(self):
        got = fidelity.compare({"acp": [a_pass("medium", "a1"), a_pass("medium", "a2", n=3)],
                                "cli": [a_pass("medium", "c1"), a_pass("medium", "c2")]})
        self.assertEqual(got["verdict"], "same")
        self.assertEqual(got["differing"], [])
        self.assertIn("reasoning.effort", got["equal"])
        self.assertIn("tools[exec_command]", got["equal"])
        self.assertIn("input[1]", got["equal"])                       # the unit's opening context
        self.assertEqual(got["ignored"], ["client_metadata", "prompt_cache_key"])
        self.assertEqual(got["requests"], {"acp": [4, 6], "cli": [4, 4]})

    def test_an_effort_one_way_sends_and_the_other_leaves_out_differs(self):
        got = fidelity.compare({"acp": [a_pass("medium"), a_pass("medium")], "cli": [a_pass(), a_pass()]})
        self.assertEqual(got["verdict"], "differs")
        self.assertEqual(got["differing"], [{"field": "reasoning.effort", "acp": ['"medium"'], "cli": [fidelity.ABSENT]}])

    def test_a_tool_or_an_opening_that_differs_is_named(self):
        other = [dict(TOOLS[0], parameters={"type": "object", "x": 1}), TOOLS[1]]
        got = fidelity.compare({"acp": [a_pass(), a_pass()], "cli": [a_pass(tools=other), a_pass(tools=other)]})
        self.assertEqual([d["field"] for d in got["differing"]], ["tools[exec_command]"])
        ctx = [CONTEXT[0], {"type": "message", "role": "user", "content": "another prompt"}]
        got = fidelity.compare({"acp": [a_pass(), a_pass()], "cli": [a_pass(context=ctx), a_pass(context=ctx)]})
        self.assertEqual([d["field"] for d in got["differing"]], ["input[1]"])

    def test_a_field_that_varies_between_two_runs_of_one_way_is_noise_not_a_difference(self):
        got = fidelity.compare({"acp": [a_pass("low"), a_pass("high")], "cli": [a_pass("medium"), a_pass("medium")]})
        self.assertEqual(got["noise"], [{"field": "reasoning.effort", "varies": ["acp"]}])
        self.assertEqual(got["verdict"], "same")

    def test_a_way_that_reached_no_model_has_no_verdict(self):
        got = fidelity.compare({"acp": [a_pass(), a_pass()], "cli": [{"u0": [], "u1": []}, {"u0": [], "u1": []}]})
        self.assertEqual(got["verdict"], "no requests")

    def test_only_model_calls_count(self):
        p = a_pass()
        p["u0"].insert(0, {"path": "/models", "status": 200, "fields": {"x": 1}})
        got = fidelity.compare({"acp": [p, p], "cli": [a_pass(), a_pass()]})
        self.assertEqual(got["verdict"], "same")

    def test_the_table_says_the_verdict_and_both_values(self):
        got = fidelity.compare({"acp": [a_pass("medium"), a_pass("medium")], "cli": [a_pass(), a_pass()]})
        text = fidelity.table(got)
        self.assertIn('DIFFERS reasoning.effort:\n    acp: "medium"\n    cli: —', text)
        self.assertTrue(text.endswith("verdict: differs"))


if __name__ == "__main__":
    unittest.main()
