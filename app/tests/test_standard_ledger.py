"""A run's spending cap: a call is booked before it is sent, sent only while
it fits, and the first that does not closes the ledger for every later one."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from nullius.standard import ledger

ROW = {"input": 1.0, "output": 10.0}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ledger", "ledger.json")

    def test_the_ledger_keeps_what_each_model_has_answered_with_on_average(self):
        """The average, not the largest: one long answer does not raise what every later call is booked."""
        led = ledger.Ledger.create(self.path, 1.0)
        self.assertIsNone(led.usual("m"))                                          # before any reply
        for cost, model, output in ((0.01, "m", 300), (0.01, "m", 120), (0.01, "other", 9000), (None, "m", None), (0.01, "m", True)):
            booking, _ = led.reserve(0.02)
            led.settle(booking, cost, model, output)
        self.assertEqual((led.usual("m"), led.usual("other"), led.usual("never")), (210, 9000, None))
        for output in (100,) * 8 + (4000,):                                        # eight short answers and a long one
            booking, _ = led.reserve(0.02)
            led.settle(booking, 0.01, "steady", output)
        self.assertEqual(led.usual("steady"), 534)
        with open(self.path) as f:                                                 # a file that lacks a part is no ledger: no call it guards is sent
            doc = json.load(f)
        del doc["seen"]
        with open(self.path, "w") as f:
            json.dump(doc, f)
        self.assertRaises(ledger.Unavailable, led.usual, "m")
        self.assertRaises(ledger.Unavailable, led.reserve, 0.02)
        self.assertTrue(led.closed())

    def test_a_booking_is_made_from_what_the_models_replies_have_been_under_the_same_lock(self):
        led = ledger.Ledger.create(self.path, 1.0, 0.5)
        told = []
        a, state = led.reserve(lambda usual: told.append(usual) or 0.1, "u1", "m")
        self.assertEqual((told, state["amount"]), ([None], 0.1))                      # no reply yet
        led.settle(a, 0.05, "m", 200)
        b, state = led.reserve(lambda usual: usual / 1000, "u1", "m")               # the average so far: 200 tokens
        self.assertEqual(state["amount"], 0.2)
        led.settle(b, 0.01, "m", 100)
        led.forget("u1")                                                           # the unit ended: its own count goes, the run's stays
        with open(self.path) as f:
            doc = json.load(f)
        self.assertEqual((doc["units"], round(doc["spent_usd"], 6)), ({}, 0.06))

    def test_a_unit_that_reaches_its_own_cap_is_refused_from_then_on_and_the_run_goes_on(self):
        led = ledger.Ledger.create(self.path, None, 0.10)                          # a cap per unit alone: none on the run
        a, _ = led.reserve(0.06, "u1")
        self.assertTrue(a)
        refused, state = led.reserve(0.06, "u1")                                   # 0.12 in flight would pass its 0.10
        self.assertEqual((refused, state["by"], state["unit"]["closed"]), (None, "unit", False))
        self.assertIn("this unit's spending cap of 0.10 USD", ledger.refusal(state, 0.06))
        again, state = led.reserve(0.001, "u1")                                    # a small one after it: refused too
        self.assertEqual((again, state["by"]), (None, "unit"))
        self.assertIn("an earlier call of this unit did not fit its spending cap of 0.10 USD", ledger.refusal(state, 0.001))
        self.assertFalse(led.closed())                                             # the run is not stopped
        self.assertTrue(led.reserve(0.06, "u2")[0])                                # another unit has its own
        self.assertTrue(led.reserve(5.0)[0])                                       # a call that is no unit's (its judge's) has none
        led.settle(a, 0.02)
        with open(self.path) as f:
            doc = json.load(f)
        self.assertEqual((doc["units"]["u1"]["spent_usd"], doc["units"]["u1"]["closed"], doc["of"].get(a)), (0.02, True, None))
        self.assertAlmostEqual(led.state()["spent_usd"], 0.02)

    def test_the_runs_cap_and_a_units_are_held_together_and_only_the_runs_stops_the_run(self):
        led = ledger.Ledger.create(self.path, 0.20, 0.15)
        self.assertEqual((led.reserve(0.16, "u1")[1]["by"], led.closed()), ("unit", False))
        a, _ = led.reserve(0.12, "u2")
        refused, state = led.reserve(0.12, "u3")                                   # fits its unit's 0.15, not the run's 0.20
        self.assertEqual((bool(a), refused, state["by"], led.closed()), (True, None, "cap", True))
        self.assertIn("the run's spending cap of 0.20 USD", ledger.refusal(state, 0.12))
        unsettled, _ = None, led.settle(a, None)                                   # never known: kept on the unit's count too
        with open(self.path) as f:
            self.assertEqual(json.load(f)["units"]["u2"]["unsettled_usd"], 0.12)

    def test_a_units_calls_are_its_own_and_its_simulated_users_never_its_judges(self):
        self.assertEqual([ledger.unit_of(t) for t in ("t-3", "t-3-simulator", "t-3-judge", "judge")], ["t-3", "t-3", None, "judge"])

    def test_a_call_only_the_calls_in_flight_keep_out_is_told_to_wait_and_nothing_closes(self):
        """Fifteen first calls booked before any reply must not end a run nothing was spent of: their bookings
        are estimates, and settle."""
        led = ledger.Ledger.create(self.path, 1.0, 0.6)
        a, _ = led.reserve(0.7)
        again, state = led.reserve(0.5, wait=True)                                 # 0.5 fits a cap of 1.0 once the 0.7 in flight settles
        self.assertEqual((again, state["by"], led.closed()), (None, "wait", False))
        led.settle(a, 0.1)
        self.assertTrue(led.reserve(0.5, wait=True)[0])                            # and it does
        never, state = led.reserve(0.5, wait=True)                                 # 0.1 spent and 0.5 in flight: 0.5 more fits alone, so it waits
        self.assertEqual((never, state["by"]), (None, "wait"))
        big, state = led.reserve(0.95, wait=True)                                  # with nothing in flight it would still not fit: refused, for good
        self.assertEqual((big, state["by"], led.closed()), (None, "cap", True))
        # a unit's own calls in flight are waited for the same way, and what it could never fit closes it
        units = ledger.Ledger.create(self.path, None, 0.6)
        b, _ = units.reserve(0.4, "u1")
        self.assertEqual(units.reserve(0.4, "u1", wait=True)[1]["by"], "wait")
        self.assertEqual(units.reserve(0.7, "u1", wait=True)[1]["by"], "unit")
        # without waiting (the wait is over), what does not fit now is refused
        self.assertEqual(units.reserve(0.4, "u2")[0] is not None, True)
        self.assertEqual(units.reserve(0.4, "u2")[1]["by"], "unit")

    def test_the_models_a_run_may_call_are_listed_once_for_the_run_and_the_builder(self):
        agent = {"model": {"side_models": ["text-embedding-3-small", "fast=gpt-5-nano"]}}
        self.assertEqual(ledger.models(agent, "m", "judge-1", "user-1", "embed-1"),
                         ["m", "judge-1", "user-1", "embed-1", "text-embedding-3-small", "gpt-5-nano"])
        self.assertEqual(ledger.models({}, None, "judge-1", "user-1", None, control=True), ["judge-1"])   # a control holds no conversation
        self.assertEqual(ledger.models({}, "m", "m", None, None), ["m"])

    def test_a_call_is_sent_while_spent_and_in_flight_and_its_booking_fit(self):
        led = ledger.Ledger.create(self.path, 1.0)
        a, _ = led.reserve(0.4)
        b, _ = led.reserve(0.4)
        self.assertTrue(a and b and a != b)
        self.assertAlmostEqual(led.state()["held_usd"], 0.8)
        led.settle(a, 0.05)                      # the reply: what it cost, in place of what was booked
        self.assertAlmostEqual(led.state()["spent_usd"], 0.05)
        self.assertAlmostEqual(led.state()["left_usd"], 1.0 - 0.05 - 0.4)
        c, _ = led.reserve(0.5)
        self.assertTrue(c)
        self.assertFalse(led.closed())

    def test_the_first_call_that_does_not_fit_closes_it_for_every_later_one(self):
        led = ledger.Ledger.create(self.path, 1.0)
        a, _ = led.reserve(0.7)
        refused, seen = led.reserve(0.4)         # 0.7 in flight: 0.4 more would pass the cap
        self.assertIsNone(refused)
        self.assertFalse(seen["closed"])         # the state it was refused in, for its message
        self.assertTrue(led.closed())
        led.settle(a, 0.01)
        self.assertIsNone(led.reserve(0.0001)[0])        # room again, and still closed: a run stops at one point
        said = ledger.refusal(led.state(), 0.0001)
        self.assertIn("1.00 USD", said)
        self.assertIn("no further call", said)

    def test_a_call_whose_cost_was_never_known_keeps_its_booking_and_an_error_gives_it_back(self):
        led = ledger.Ledger.create(self.path, 1.0)
        a, _ = led.reserve(0.3)
        b, _ = led.reserve(0.3)
        led.settle(a, None)                      # no token count, or cut off: it may have been billed
        led.release(b)                           # the provider's error: not billed
        led.release(b)                           # twice is once
        led.settle("unknown", 9.0)               # a booking it does not hold changes nothing
        got = led.state()
        self.assertEqual((got["spent_usd"], got["unsettled_usd"], got["held_usd"]), (0.0, 0.3, 0))
        self.assertAlmostEqual(got["left_usd"], 0.7)

    def test_a_ledger_that_cannot_be_read_lets_no_call_out(self):
        led = ledger.Ledger.create(self.path, 1.0)
        with open(self.path, "w") as f:
            f.write("{not json")
        with self.assertRaises(ledger.Unavailable):
            led.reserve(0.1)
        self.assertTrue(led.closed())
        self.assertTrue(ledger.Ledger(os.path.join(self.tmp, "none.json")).closed())

    def test_many_calls_at_once_never_book_past_the_cap(self):
        led = ledger.Ledger.create(self.path, 1.0)
        got = []

        def ask():
            got.append(led.reserve(0.03)[0])

        threads = [threading.Thread(target=ask) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for b in got if b), 33)           # 33 x 0.03 fit in 1.00, the 34th does not
        self.assertLessEqual(led.state()["held_usd"], 1.0)

    def test_relays_in_other_processes_book_in_the_same_ledger(self):
        """What a docker unit's relay does from its container: each process
        books and settles; none of it is lost between them."""
        led = ledger.Ledger.create(self.path, 100.0)
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys\nfrom nullius.standard import ledger\nled = ledger.Ledger(sys.argv[1])\n"
                "for _ in range(40):\n    b, _ = led.reserve(0.5)\n    led.settle(b, 0.01)\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, self.path], env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": app})
                 for _ in range(4)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0, 0, 0])
        got = led.state()
        self.assertAlmostEqual(got["spent_usd"], 4 * 40 * 0.01)
        self.assertEqual((got["held_usd"], got["closed"]), (0, False))
        with open(self.path) as f:
            self.assertEqual(json.load(f)["held"], {})


class BoundTest(unittest.TestCase):
    def bound(self, req, row=ROW, path="/chat/completions"):
        return ledger.bound(req, json.dumps(req).encode(), row, path)

    def test_the_limit_the_call_names_is_its_output(self):
        req = {"model": "m", "messages": [{"role": "user", "content": "x" * 300}], "max_tokens": 100}
        tokens = -(-len(json.dumps(req)) // 3)   # its characters over three, rounded up
        self.assertAlmostEqual(self.bound(req), (tokens * 1.0 + 100 * 10.0) / 1e6)
        for name in ("max_completion_tokens", "max_output_tokens"):
            other = {"model": "m", "input": "hi", name: 50}
            self.assertAlmostEqual(self.bound(other), (-(-len(json.dumps(other)) // 3) * 1.0 + 50 * 10.0) / 1e6)

    def test_a_call_that_names_none_is_booked_the_models_own_limit_else_the_default(self):
        req = {"model": "m", "messages": []}
        tokens = -(-len(json.dumps(req)) // 3)
        self.assertAlmostEqual(self.bound(req, dict(ROW, max_output=2000)), (tokens + 2000 * 10.0) / 1e6)
        self.assertAlmostEqual(self.bound(req), (tokens + ledger.OUTPUT * 10.0) / 1e6)

    def test_a_call_in_flight_is_booked_what_the_runs_replies_have_been_never_past_a_limit(self):
        """Its cost is known only when its reply comes back: until then it counts as the average reply the run
        has had from that model (``seen``), not as the model's limit, which a short answer is a hundredth of."""
        req = {"model": "m", "messages": []}
        tokens = -(-len(json.dumps(req)) // 3)
        out = lambda row=ROW, seen=None, **named: round((ledger.bound({**req, **named}, json.dumps({**req, **named}).encode(), row, "/chat/completions", seen) * 1e6
                                                         - -(-len(json.dumps({**req, **named})) // 3)) / 10.0)
        self.assertEqual(out(seen=300), 300)                                       # what its replies have been
        self.assertEqual(out(), ledger.OUTPUT)                                     # before any reply
        self.assertEqual(out(dict(ROW, max_output=131072)), ledger.OUTPUT)         # not the model's whole limit
        self.assertEqual(out(dict(ROW, max_output=131072), seen=900), 900)
        self.assertEqual(out(seen=300, max_tokens=100), 100)                       # never more than the call asks for
        self.assertEqual(out(dict(ROW, max_output=2000), seen=5000), 2000)         # nor than the model can answer
        self.assertEqual(out(max_tokens=64000), ledger.OUTPUT)                     # a large limit named is no reply seen
        self.assertEqual(out(seen=7000, max_tokens=64000), 7000)
        self.assertAlmostEqual(ledger.bound(req, json.dumps(req).encode(), ROW, "/chat/completions", 300), (tokens + 300 * 10.0) / 1e6)

    def test_input_is_booked_at_the_dearer_of_input_and_cache_write(self):
        req = {"model": "m", "messages": [], "max_tokens": 1}
        tokens = -(-len(json.dumps(req)) // 3)
        self.assertAlmostEqual(self.bound(req, dict(ROW, cache_write=1.25)), (tokens * 1.25 + 10.0) / 1e6)

    def test_an_embeddings_call_has_no_output_and_an_encoded_file_is_not_a_prompt(self):
        req = {"model": "e", "input": ["some text"]}
        self.assertAlmostEqual(self.bound(req, {"input": 0.02, "output": 5.0}, "/embeddings"), -(-len(json.dumps(req)) // 3) * 0.02 / 1e6)
        picture = "A" * 30000
        plain = {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": ""}}, {"type": "image", "source": {"type": "base64", "data": ""}}]}]}
        with_files = json.loads(json.dumps(plain))
        with_files["messages"][0]["content"][0]["image_url"]["url"] = "data:image/png;base64," + picture
        with_files["messages"][0]["content"][1]["source"]["data"] = picture
        self.assertAlmostEqual(self.bound(with_files), self.bound(plain))


if __name__ == "__main__":
    unittest.main()
