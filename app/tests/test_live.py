"""The live feed and its fold, pinned.

`nullius.live` is the live board's backend: the feed schema, the fold from
lines to a snapshot, and the four rules the snapshot has to obey — last phase
event wins, counters are the latest values and absent rather than zero, a torn
last line is a notice, and free text is redacted on read. Beside the cases, the
cross-check that matters most: the fixture folds to its `expected.json`, byte
for byte as JSON.

The fixture (`fixtures/live/events.jsonl`, `expected.json`) is a hand-written
three-trial job with a torn last line; Harbor's first recorded run replaces it
additively, and these cases stay valid as a second case.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nullius.runs import live

from . import FIXTURES as _FIXTURES

FIXTURES = Path(_FIXTURES) / "live"
NOW = datetime(2026, 9, 12, 18, 29, 20, tzinfo=timezone.utc)


def ev(seq, event, trial="t1", ts=None, **data):
    if ts is None:
        at = datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seq)
        ts = at.isoformat().replace("+00:00", "Z")
    base = {"ts": ts, "seq": seq, "job": "j", "trial": trial, "event": event}
    return json.dumps({**base, **data})


def board_of(*lines, now=NOW):
    return live.fold([live.parse_line(l) for l in lines], now=now)


class Schema(unittest.TestCase):
    def test_every_field_the_contract_names_is_required(self):
        for missing in ("ts", "seq", "job", "event"):
            raw = json.loads(ev(1, "trial.phase", phase="env_setup"))
            del raw[missing]
            with self.assertRaisesRegex(live.FeedError, missing):
                live.parse_line(json.dumps(raw))

    def test_vocabularies_are_closed(self):
        with self.assertRaisesRegex(live.FeedError, "unknown event"):
            live.parse_line(ev(1, "trial.started"))
        with self.assertRaisesRegex(live.FeedError, "unknown phase"):
            live.parse_line(ev(1, "trial.phase", phase="setup"))
        with self.assertRaisesRegex(live.FeedError, "unknown outcome"):
            live.parse_line(ev(1, "trial.end", outcome="failed"))
        for outcome in live.OUTCOMES:
            live.parse_line(ev(1, "trial.end", outcome=outcome))  # budget included

    def test_trial_is_null_on_job_events_and_a_string_otherwise(self):
        live.parse_line(ev(1, "job.start", trial=None))
        with self.assertRaisesRegex(live.FeedError, "carries no trial"):
            live.parse_line(ev(1, "job.start", trial="t1"))
        with self.assertRaisesRegex(live.FeedError, "needs a trial"):
            live.parse_line(ev(1, "trial.phase", trial=None, phase="env_setup"))

    def test_ts_is_utc_iso8601(self):
        e = live.parse_line(ev(1, "trial.log", ts="2026-09-12T18:00:01Z", line="x"))
        self.assertEqual(e.ts.tzinfo, timezone.utc)
        with self.assertRaisesRegex(live.FeedError, "offset"):
            live.parse_line(ev(1, "trial.log", ts="2026-09-12T18:00:01", line="x"))

    def test_seq_is_an_int(self):
        with self.assertRaisesRegex(live.FeedError, "seq"):
            live.parse_line(ev(1, "trial.log").replace('"seq": 1', '"seq": "1"'))


class Redaction(unittest.TestCase):
    def test_the_tail_is_scrubbed_too_not_only_the_cards(self):
        # Redaction happens at parse, so the event tail the snapshot carries
        # cannot print what the card hid. This application's first draft did
        # exactly that; this is the shape that catches it.
        key = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
        snap = board_of(ev(1, "trial.log", line=f"got {key}"),
                        ev(2, "trial.progress", activity=f"call {key}")).snapshot()
        self.assertNotIn(key, json.dumps(snap["tail"]))
        self.assertNotIn(key, json.dumps(snap["trials"]))

    def test_activity_is_capped_at_120_chars(self):
        e = live.parse_line(ev(1, "trial.progress", activity="x" * 500))
        self.assertEqual(len(e.data["activity"]), live.ACTIVITY_MAX)


class Reading(unittest.TestCase):
    def write(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "events.jsonl"
        p.write_text(text)
        return p

    def test_a_torn_last_line_is_a_notice_not_an_error(self):
        p = self.write(ev(1, "job.start", trial=None) + "\n" + ev(2, "trial.phase", phase="env_setup") + "\n"
                       + '{"ts": "2026-09-12T18:00:03Z", "seq": 3, "job": "j", "tri')
        feed = live.read_events(p)
        self.assertEqual([e.seq for e in feed.events], [1, 2])
        self.assertTrue(any("torn" in n for n in feed.notices), feed.notices)

    def test_a_malformed_line_in_the_middle_raises(self):
        p = self.write(ev(1, "job.start", trial=None) + "\nnot json\n" + ev(3, "job.end", trial=None) + "\n")
        with self.assertRaisesRegex(live.FeedError, "line 2"):
            live.read_events(p)

    def test_out_of_order_seq_is_folded_in_order_and_noted(self):
        p = self.write(ev(2, "trial.phase", phase="agent_run") + "\n" + ev(1, "trial.phase", phase="env_setup") + "\n")
        feed = live.read_events(p)
        self.assertEqual([e.seq for e in feed.events], [1, 2])
        self.assertTrue(any("seq" in n for n in feed.notices))
        self.assertEqual(live.fold(feed.events).trials[0].phase, "agent_run")

    def test_blank_lines_are_skipped(self):
        p = self.write(ev(1, "job.start", trial=None) + "\n\n" + ev(2, "job.end", trial=None) + "\n")
        self.assertEqual(len(live.read_events(p).events), 2)


class Fold(unittest.TestCase):
    def test_the_last_phase_event_wins_and_time_in_phase_is_now_minus_its_ts(self):
        b = board_of(ev(1, "trial.phase", phase="env_setup", timeout_sec=300),
                     ev(2, "trial.phase", phase="agent_run", timeout_sec=540),
                     now=datetime(2026, 9, 12, 18, 0, 12, tzinfo=timezone.utc))
        t = b.trials[0]
        self.assertEqual(t.phase, "agent_run")
        self.assertEqual(t.elapsed_in_phase(b.now), 10.0)
        self.assertEqual(t.timeout_sec, 540.0)
        self.assertEqual(t.phases_done, ["env_setup"])
        self.assertEqual(t.state, "running")

    def test_counters_are_the_latest_values_and_absent_is_none_not_zero(self):
        b = board_of(ev(1, "trial.phase", phase="agent_run"),
                     ev(2, "trial.progress", step=3, n_input_tokens=100, n_cache_tokens=50, n_output_tokens=5),
                     ev(3, "trial.progress", step=4, n_input_tokens=200))
        t = b.trials[0]
        self.assertEqual((t.step, t.n_input_tokens, t.n_cache_tokens, t.n_output_tokens), (4, 200, 50, 5))
        self.assertIsNone(t.cost_usd)
        quiet = board_of(ev(1, "trial.phase", phase="agent_run"))
        self.assertIsNone(quiet.trials[0].step)
        self.assertIsNone(quiet.snapshot()["totals"]["n_input_tokens"])

    def test_cost_is_absent_until_something_reports_it(self):
        b = board_of(ev(1, "trial.phase", phase="agent_run"), ev(2, "trial.progress", step=1, n_input_tokens=10))
        self.assertIsNone(b.snapshot()["totals"]["cost_usd"])
        b = board_of(ev(1, "trial.phase", phase="agent_run"), ev(2, "trial.progress", cost_usd=0.0012))
        self.assertEqual(b.snapshot()["totals"]["cost_usd"], 0.0012)

    def test_trial_end_records_outcome_reward_durations_and_totals(self):
        b = board_of(ev(1, "trial.phase", phase="agent_run"),
                     ev(2, "trial.end", outcome="resolved", reward=1.0,
                        durations={"agent_run": 30.0}, n_input_tokens=1000, n_cache_tokens=900, n_output_tokens=10))
        t = b.trials[0]
        self.assertEqual((t.state, t.outcome, t.reward), ("done", "resolved", 1.0))
        self.assertIsNone(t.phase)
        self.assertEqual(t.phases_done, ["agent_run"])
        self.assertEqual(t.durations, {"agent_run": 30.0})
        self.assertEqual(t.total_duration(), 1.0)

    def test_counts_by_state_and_outcome(self):
        b = board_of(ev(1, "job.start", trial=None, n_total=4, n_concurrent=2),
                     ev(2, "trial.phase", trial="a", phase="agent_run"),
                     ev(3, "trial.phase", trial="b", phase="agent_run"),
                     ev(4, "trial.end", trial="a", outcome="resolved", reward=1.0),
                     ev(5, "trial.end", trial="b", outcome="infra_error", reward=0.0),
                     ev(6, "trial.queued", trial="c", task="x"),
                     ev(7, "trial.phase", trial="c", phase="env_setup"))
        c = b.counts()
        self.assertEqual(c, {"total": 4, "running": 1, "pending": 1, "completed": 2, "resolved": 1,
                             "unresolved": 0, "errored": 1, "cancelled": 0})
        self.assertEqual(b.mean_reward(), 0.5)

    def test_a_trial_scored_without_a_pass_or_fail_is_neither(self):
        """This application's own outcome, for a runner whose benchmark gives
        a score and no verdict: counted as done and as scored, never as
        resolved or unresolved, and absent from a feed that has none - which
        folds to the expected snapshot, key for key."""
        plain = board_of(ev(1, "job.start", trial=None, n_total=2, n_concurrent=1),
                         ev(2, "trial.end", trial="a", outcome="resolved", reward=1.0))
        self.assertNotIn("scored", plain.counts())
        self.assertNotIn("score", plain.snapshot()["trials"][0]); self.assertNotIn("parts", plain.snapshot()["trials"][0])
        b = board_of(ev(1, "job.start", trial=None, n_total=2, n_concurrent=1),
                     ev(2, "trial.phase", trial="a", phase="agent_run"),
                     ev(3, "trial.end", trial="a", outcome="scored", score=0.52, parts=152),
                     ev(4, "trial.end", trial="b", outcome="scored", score=0.4))
        self.assertEqual(b.counts(), {"total": 2, "running": 0, "pending": 0, "completed": 2, "resolved": 0,
                                      "unresolved": 0, "scored": 2, "errored": 0, "cancelled": 0})
        self.assertIsNone(b.mean_reward())                                                  # a score is not a reward
        got = {t["trial"]: (t["outcome"], t["reward"], t["score"], t["parts"]) for t in b.snapshot()["trials"]}
        self.assertEqual(got, {"a": ("scored", None, 0.52, 152), "b": ("scored", None, 0.4, None)})

    def test_eta_needs_two_finished_trials(self):
        start = datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        lines = [ev(1, "job.start", trial=None, ts=at(0), n_total=4, n_concurrent=2),
                 ev(2, "trial.phase", trial="a", ts=at(0), phase="agent_run"),
                 ev(3, "trial.phase", trial="b", ts=at(0), phase="agent_run"),
                 ev(4, "trial.end", trial="a", ts=at(100), outcome="resolved", reward=1.0)]
        self.assertIsNone(board_of(*lines, now=start + timedelta(seconds=100)).eta_sec())
        lines.append(ev(5, "trial.end", trial="b", ts=at(140), outcome="resolved", reward=1.0))
        b = board_of(*lines, now=start + timedelta(seconds=140))
        # median 120 s × 2 pending ÷ concurrency 2
        self.assertEqual(b.eta_sec(), 120.0)
        lines.append(ev(6, "job.end", trial=None, ts=at(150)))
        self.assertIsNone(board_of(*lines).eta_sec())

    def test_a_trial_harbor_retries_runs_again_under_its_name(self):
        # The first attempt ends in an agent error; Harbor (max_retries 1) starts it again
        # under the same trial name a second later; the board counts it running again, not done
        start = datetime(2026, 10, 8, 14, 32, 12, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        lines = [ev(1, "job.start", trial=None, ts=at(0), n_total=2, n_concurrent=2),
                 ev(2, "trial.phase", trial="flipt", ts=at(0), phase="env_setup"),
                 ev(3, "trial.phase", trial="element", ts=at(0), phase="env_setup"),
                 ev(4, "trial.end", trial="flipt", ts=at(4), outcome="agent_error", exception_type="RuntimeError"),
                 ev(5, "trial.phase", trial="flipt", ts=at(5), phase="env_setup"),
                 ev(6, "trial.phase", trial="flipt", ts=at(79), phase="agent_run")]
        b = board_of(*lines, now=start + timedelta(seconds=90))
        c = b.counts()
        self.assertEqual((c["running"], c["completed"], c["errored"]), (2, 0, 0))
        flipt = next(t for t in b.snapshot()["trials"] if t["trial"] == "flipt")
        self.assertEqual((flipt["state"], flipt["attempt"], flipt["retried"], flipt["phase"]), ("running", 2, ["agent_error"], "agent_run"))
        lines.append(ev(7, "trial.end", trial="flipt", ts=at(200), outcome="resolved", reward=1.0))
        flipt = next(t for t in board_of(*lines).snapshot()["trials"] if t["trial"] == "flipt")
        self.assertEqual((flipt["outcome"], flipt["attempt"]), ("resolved", 2))                     # the last attempt is its result

    def test_eta_counts_down_while_a_trial_runs(self):
        # the same median, read later: a running trial's time so far comes off its share, so the ETA moves
        start = datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        lines = [ev(1, "job.start", trial=None, ts=at(0), n_total=4, n_concurrent=2)]
        lines += [ev(2 + i, "trial.phase", trial=t, ts=at(s), phase="agent_run") for i, (t, s) in enumerate((("a", 0), ("b", 0), ("c", 100)))]
        lines += [ev(5, "trial.end", trial="a", ts=at(100), outcome="resolved", reward=1.0),
                  ev(6, "trial.end", trial="b", ts=at(140), outcome="resolved", reward=1.0),
                  ev(7, "trial.phase", trial="d", ts=at(140), phase="agent_run")]
        # median 120 s; at 150 s c has run 50 s and d 10 s: (70 + 110) ÷ 2
        self.assertEqual(board_of(*lines, now=start + timedelta(seconds=150)).eta_sec(), 90.0)
        self.assertEqual(board_of(*lines, now=start + timedelta(seconds=170)).eta_sec(), 70.0)
        self.assertEqual(board_of(*lines, now=start + timedelta(seconds=400)).eta_sec(), 0.0)   # over the median: nothing more is guessed

    def test_tokens_per_second_is_a_thirty_second_window(self):
        start = datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        b = board_of(ev(1, "trial.phase", ts=at(0), phase="agent_run"),
                     ev(2, "trial.progress", ts=at(10), n_input_tokens=1000),
                     ev(3, "trial.progress", ts=at(50), n_input_tokens=4000),
                     ev(4, "trial.progress", ts=at(60), n_input_tokens=7000),
                     now=start + timedelta(seconds=60))
        # window (30, 60]: baseline is the last report at or before 30 s → 1000
        self.assertEqual(b.tokens_per_sec(), (7000 - 1000) / 30)
        nothing = board_of(ev(1, "trial.phase", ts=at(0), phase="agent_run"), now=start + timedelta(seconds=60))
        self.assertIsNone(nothing.tokens_per_sec())
        # One point in the window and none before it is not a rate — null,
        # never 0.0: a zero where nothing was measured is a lie (the cost rule).
        one_point = board_of(ev(1, "trial.phase", ts=at(0), phase="agent_run"),
                             ev(2, "trial.end", ts=at(55), outcome="resolved", reward=1.0, n_input_tokens=5000),
                             now=start + timedelta(seconds=60))
        self.assertIsNone(one_point.tokens_per_sec())

    def test_a_key_that_appears_only_on_a_later_report_is_not_a_rate(self):
        # Harbor's writer reports input/output on progress and adds the cache
        # count only on trial.end; this application saw 32k "tokens/s" on a
        # real run from that key's first appearance. Deltas are per key, over
        # reports carrying that key — the cache figure has one point and no
        # baseline.
        start = datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        b = board_of(ev(1, "trial.phase", ts=at(0), phase="agent_run"),
                     ev(2, "trial.progress", ts=at(40), n_input_tokens=1000, n_output_tokens=100),
                     ev(3, "trial.end", ts=at(55), outcome="resolved", reward=1.0,
                        n_input_tokens=1600, n_output_tokens=160, n_cache_tokens=900000),
                     now=start + timedelta(seconds=60))
        self.assertEqual(b.tokens_per_sec(), (600 + 60) / 30)
        only_end = board_of(ev(1, "trial.phase", ts=at(0), phase="agent_run"),
                            ev(2, "trial.progress", ts=at(40), n_input_tokens=1000),
                            ev(3, "trial.end", ts=at(55), outcome="resolved", reward=1.0, n_cache_tokens=900000),
                            now=start + timedelta(seconds=60))
        self.assertIsNone(only_end.tokens_per_sec())   # no key has two points

    def test_the_tail_is_the_last_fifty_events(self):
        lines = [ev(i, "trial.log", line=str(i)) for i in range(1, 80)]
        self.assertEqual([e["seq"] for e in board_of(*lines).snapshot()["tail"]], list(range(30, 80)))

    def test_now_defaults_to_the_last_event_so_replay_is_exact(self):
        b = board_of(ev(1, "trial.phase", phase="agent_run"), ev(5, "trial.log", line="x"), now=None)
        self.assertEqual(b.trials[0].elapsed_in_phase(b.now), 4.0)


class Preparing(unittest.TestCase):
    """job.prepare: the window between Run and job.start is on the board."""

    def test_prepare_is_a_job_event_and_the_last_stage_wins(self):
        b = board_of(ev(1, "job.prepare", trial=None, stage="dataset", n_total=2, n_concurrent=2, agents=["my-agent"]),
                     ev(12, "job.prepare", trial=None, stage="image"), now=None)
        self.assertEqual(b.state, "preparing")
        self.assertEqual(b.preparing()["stage"], "image")
        self.assertEqual(b.preparing()["elapsed_sec"], 0.0)           # now = last event
        self.assertEqual((b.n_total, b.n_concurrent, b.agents), (2, 2, ["my-agent"]))
        self.assertEqual(b.elapsed_sec(), 11.0)                          # since the launch
        with self.assertRaisesRegex(live.FeedError, "carries no trial"):
            live.parse_line(ev(1, "job.prepare", trial="t1", stage="x"))

    def test_states_walk_preparing_starting_running_finished(self):
        start = datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        lines = [ev(1, "job.prepare", trial=None, ts=at(0), stage="dataset")]
        self.assertEqual(board_of(*lines, now=start + timedelta(seconds=20)).state, "preparing")
        lines.append(ev(2, "job.start", trial=None, ts=at(41), n_total=1, n_concurrent=1))
        lines.append(ev(3, "trial.queued", trial="a", ts=at(41), task="t"))
        b = board_of(*lines, now=start + timedelta(seconds=42))
        self.assertEqual(b.state, "starting")
        self.assertIsNone(b.preparing())
        self.assertEqual(b.elapsed_sec(), 42.0)                          # still since Run
        lines.append(ev(4, "trial.phase", trial="a", ts=at(44), phase="env_setup"))
        self.assertEqual(board_of(*lines, now=start + timedelta(seconds=45)).state, "running")
        lines.append(ev(5, "trial.end", trial="a", ts=at(90), outcome="resolved", reward=1.0))
        lines.append(ev(6, "job.end", trial=None, ts=at(91)))
        b = board_of(*lines, now=start + timedelta(seconds=100))
        self.assertEqual(b.state, "finished")
        self.assertEqual(b.elapsed_sec(), 91.0)                          # launch → job.end

    def test_without_a_prepare_event_nothing_changes(self):
        b = board_of(ev(1, "job.start", trial=None, n_total=1), now=None)
        self.assertEqual((b.state, b.launched_at, b.preparing(), b.elapsed_sec()), ("starting", None, None, 0.0))
        self.assertEqual(board_of(ev(1, "trial.log", line="x")).state, "running")
        self.assertEqual(live.fold([]).state, "unknown")

    def test_the_prepare_window_fixture_at_two_moments(self):
        root = FIXTURES / "prepare-window"
        during = live.load(root / "events.jsonl", now=datetime(2026, 9, 14, 8, 0, 20, tzinfo=timezone.utc)).snapshot()
        # Only the first two lines have happened by 08:00:20 — the loader reads
        # the whole file, so fold the prefix the way a poller would have seen it.
        events = [e for e in live.read_events(root / "events.jsonl").events if e.ts <= datetime(2026, 9, 14, 8, 0, 20, tzinfo=timezone.utc)]
        during = live.fold(events, now=datetime(2026, 9, 14, 8, 0, 20, tzinfo=timezone.utc)).snapshot()
        self.assertEqual(during, json.loads((root / "expected-preparing.json").read_text()))
        self.assertEqual(during["state"], "preparing")
        self.assertEqual(during["preparing"], {"stage": "image", "since": "2026-09-14T08:00:12Z", "elapsed_sec": 8.0})
        self.assertEqual((during["elapsed_sec"], during["counts"]["total"], during["trials"]), (20.0, 2, []))
        after = live.load(root / "events.jsonl").snapshot()
        self.assertEqual(after, json.loads((root / "expected.json").read_text()))
        self.assertEqual(after["state"], "running")
        self.assertIsNone(after["preparing"])
        self.assertEqual(after["elapsed_sec"], 45.0)                     # 08:00:00 → 08:00:45


class Lanes(unittest.TestCase):
    """Several agents in one job: one lane per agent, and a winner per task
    that more than one agent ran. Both are folded from the same lines, so a
    head-to-head reads the same on every destination."""

    def duel(self, *extra, now=NOW):
        # Two declared agents on the same task; a third agent nobody declared;
        # a trial whose agent is never named.
        return board_of(
            ev(1, "job.start", trial=None, n_total=4, n_concurrent=2, agents=["my-agent", "opencode"]),
            ev(2, "trial.queued", "a1", task="t-a", agent="my-agent", position=1),
            ev(3, "trial.queued", "a2", task="t-a", agent="opencode", position=2),
            ev(4, "trial.queued", "a3", task="t-a", agent="oracle", position=3),
            ev(5, "trial.phase", "a4", phase="env_setup"),
            *extra, now=now)

    def test_one_lane_per_agent_declared_first_then_first_seen_then_unnamed(self):
        lanes = self.duel().lanes()
        self.assertEqual([l["agent"] for l in lanes], ["my-agent", "opencode", "oracle", None])
        self.assertEqual([l["trials"] for l in lanes], [["a1"], ["a2"], ["a3"], ["a4"]])
        undeclared = board_of(ev(1, "trial.queued", "x", agent="opencode"), ev(2, "trial.queued", "y", agent="aider"))
        self.assertEqual([l["agent"] for l in undeclared.lanes()], ["opencode", "aider"])
        named = board_of(ev(1, "trial.queued", "x", agent="opencode"))
        self.assertEqual([l["agent"] for l in named.lanes()], ["opencode"])   # no unnamed lane without unnamed trials

    def test_a_lane_carries_its_own_counts_reward_totals_and_wins(self):
        b = self.duel(
            ev(6, "trial.end", "a1", outcome="resolved", reward=1.0, n_input_tokens=1000, n_output_tokens=10),
            ev(7, "trial.phase", "a2", phase="agent_run"),
            ev(8, "trial.end", "a3", outcome="agent_error"))
        js, oc, oracle, unnamed = b.lanes()
        self.assertEqual(js["counts"], {"total": 1, "running": 0, "pending": 0, "completed": 1,
                                        "resolved": 1, "unresolved": 0, "errored": 0, "cancelled": 0})
        self.assertEqual(oc["counts"]["running"], 1)
        self.assertEqual(oracle["counts"]["errored"], 1)
        self.assertEqual(unnamed["counts"]["running"], 1)
        self.assertEqual(js["mean_reward"], 1.0)
        self.assertIsNone(oc["mean_reward"])                       # nothing finished
        self.assertEqual(js["totals"], {"n_input_tokens": 1000, "n_cache_tokens": None,
                                        "n_output_tokens": 10, "cost_usd": None})
        self.assertEqual(oc["totals"]["cost_usd"], None)           # absent, never 0
        self.assertEqual([l["wins"] for l in (js, oc, oracle, unnamed)], [0, 0, 0, 0])   # a2 still running

    def test_winner_is_the_strictly_best_reward_once_every_trial_of_the_task_has_ended(self):
        b = self.duel(
            ev(6, "trial.end", "a1", outcome="resolved", reward=1.0),
            ev(7, "trial.end", "a2", outcome="unresolved", reward=0.0),
            ev(8, "trial.end", "a3", outcome="agent_error"))          # no reward at all
        [w] = b.winners()
        self.assertEqual(w, {"task": "t-a", "agents": ["my-agent", "opencode", "oracle"],
                             "rewards": {"my-agent": 1.0, "opencode": 0.0, "oracle": None},
                             "decided": True, "winner": "my-agent"})
        self.assertEqual([l["wins"] for l in b.lanes()], [1, 0, 0, 0])

    def test_a_tie_or_an_undecided_task_names_no_winner(self):
        undecided = self.duel(ev(6, "trial.end", "a1", outcome="resolved", reward=1.0),
                              ev(7, "trial.phase", "a2", phase="agent_run"))
        [w] = undecided.winners()
        self.assertEqual((w["decided"], w["winner"]), (False, None))
        self.assertEqual(w["rewards"], {"my-agent": 1.0, "opencode": None, "oracle": None})   # best so far, still shown
        tie = self.duel(ev(6, "trial.end", "a1", outcome="resolved", reward=1.0),
                        ev(7, "trial.end", "a2", outcome="resolved", reward=1.0),
                        ev(8, "trial.end", "a3", outcome="unresolved", reward=0.0))
        [w] = tie.winners()
        self.assertEqual((w["decided"], w["winner"]), (True, None))
        nobody = self.duel(ev(6, "trial.end", "a1", outcome="agent_error"),
                           ev(7, "trial.end", "a2", outcome="infra_error"),
                           ev(8, "trial.end", "a3", outcome="cancelled"))
        self.assertEqual((nobody.winners()[0]["decided"], nobody.winners()[0]["winner"]), (True, None))

    def test_an_agent_with_several_attempts_is_scored_by_its_best(self):
        b = board_of(ev(1, "trial.queued", "x1", task="t", agent="a"),
                     ev(2, "trial.queued", "x2", task="t", agent="a"),
                     ev(3, "trial.queued", "y1", task="t", agent="b"),
                     ev(4, "trial.end", "x1", outcome="unresolved", reward=0.0),
                     ev(5, "trial.end", "x2", outcome="resolved", reward=1.0),
                     ev(6, "trial.end", "y1", outcome="resolved", reward=0.5))
        [w] = b.winners()
        self.assertEqual((w["rewards"], w["winner"]), ({"a": 1.0, "b": 0.5}, "a"))

    def test_a_single_agent_job_has_one_lane_and_nothing_to_win(self):
        b = board_of(ev(1, "trial.queued", "x", task="t1", agent="a"),
                     ev(2, "trial.queued", "y", task="t2", agent="a"),
                     ev(3, "trial.end", "x", outcome="resolved", reward=1.0))
        self.assertEqual(len(b.lanes()), 1)
        self.assertEqual(b.winners(), [])
        self.assertEqual(b.lanes()[0]["wins"], 0)

    def test_a_task_run_by_one_agent_is_not_a_contest(self):
        b = self.duel(ev(6, "trial.queued", "solo", task="t-solo", agent="my-agent"),
                      ev(7, "trial.end", "solo", outcome="resolved", reward=1.0))
        self.assertEqual([w["task"] for w in b.winners()], ["t-a"])

    def test_harbors_recorded_prepare_feed_walks_preparing_starting_running_finished(self):
        # The prepare feed reports stages before the job starts:
        # job.prepare {stage: dataset}, job.prepare {stage: tasks},
        # then job.start - written at launch, so a board polling the file sees
        # the window. A local dataset here, so the whole window is 14 ms; on a
        # registry dataset it is the download. Folded prefix by prefix, the way
        # a poller sees the file grow.
        events = live.read_events(FIXTURES / "harbor-prepare-x4" / "events.jsonl").events
        self.assertEqual([e.event for e in events[:3]], ["job.prepare", "job.prepare", "job.start"])
        one = live.fold(events[:1]); two = live.fold(events[:2]); three = live.fold(events[:3])
        self.assertEqual((one.state, one.preparing()["stage"], one.n_concurrent, one.agents), ("preparing", "dataset", 4, ["oracle"]))
        self.assertEqual((two.state, two.preparing()["stage"]), ("preparing", "tasks"))
        self.assertEqual((three.state, three.preparing(), three.n_total), ("starting", None, 4))
        self.assertEqual(three.launched_at, events[0].ts)
        self.assertAlmostEqual(three.elapsed_sec(), 0.014, places=3)          # launch -> job.start, a local dataset
        whole = live.fold(events)
        self.assertEqual((whole.state, whole.snapshot()["counts"]["completed"]), ("finished", 4))
        self.assertAlmostEqual(whole.elapsed_sec(), 24.62, places=2)           # launch -> job.end
        self.assertNotIn("REDACTED", json.dumps(whole.snapshot()))


class Fixture(unittest.TestCase):
    """The committed sample inputs and their expected snapshots — the pairs a
    destination folds and compares against. Two, on purpose: the hand-written
    job is the only one that exercises progress, log sampling and redaction;
    the recorded one is what Harbor's writer really emits."""

    def test_task_and_agent_are_read_from_whichever_event_carries_them(self):
        b = board_of(ev(1, "trial.queued", agent="oracle", position=1),
                     ev(2, "trial.phase", phase="env_setup", task="harbor/hello-user", agent="oracle"),
                     ev(3, "trial.phase", phase="agent_run", task="something-else"))
        t = b.trials[0]
        self.assertEqual((t.task, t.agent, t.position), ("harbor/hello-user", "oracle", 1))

    def test_harbors_recorded_oracle_run_folds_to_its_snapshot(self):
        # The oracle feed covers five event
        # types (oracle reports no progress and no log), four resolved trials,
        # extra keys the schema does not name (evidence, n_* on job.end) read
        # leniently. Replay: now defaults to the last event's ts.
        root = FIXTURES / "harbor-oracle-x4"
        got = live.load(root / "events.jsonl").snapshot()
        want = json.loads((root / "expected.json").read_text())
        self.assertEqual(got, want)
        self.assertEqual(got["counts"]["completed"], 4)
        self.assertFalse(got["running"])
        self.assertEqual(got["totals"], {"n_input_tokens": None, "n_cache_tokens": None,
                                         "n_output_tokens": None, "cost_usd": None})
        self.assertTrue(all(t["task"] and t["agent"] == "oracle" for t in got["trials"]))

    def test_it_folds_to_the_expected_snapshot(self):
        got = live.load(FIXTURES / "events.jsonl", now=NOW).snapshot()
        want = json.loads((FIXTURES / "expected.json").read_text())
        self.assertEqual(got, want)

    def test_what_the_fixture_exercises(self):
        b = live.load(FIXTURES / "events.jsonl", now=NOW)
        self.assertTrue(any("torn" in n for n in b.notices))
        states = {t.trial: t.state for t in b.trials}
        self.assertEqual(states, {"airline-0__a1": "done", "retail-0__r1": "running", "banking-001__b1": "running"})
        retail = next(t for t in b.trials if t.trial == "retail-0__r1")
        self.assertNotIn("sk-", retail.last_line)          # redacted on read
        self.assertIsNone(b.snapshot()["totals"]["cost_usd"])  # nobody reported cost
        self.assertIsNone(b.eta_sec())                      # one finished, not two

    def test_harbors_recorded_progress_run_folds_to_its_snapshot(self):
        # Eight trials, four of which report
        # progress (step, n_input_tokens, n_output_tokens, activity — no cache,
        # no cost) at ≤1 per 2 s, four report nothing. The fold yields a rate:
        # the last progress reports fall inside the 30 s
        # window with earlier reports as baselines. The writer redacted the
        # fake key in `activity` to [REDACTED] before writing; redact() here
        # alters nothing further.
        root = FIXTURES / "harbor-progress-x8"
        got = live.load(root / "events.jsonl").snapshot()
        want = json.loads((root / "expected.json").read_text())
        self.assertEqual(got, want)
        self.assertEqual(got["counts"]["completed"], 8)
        self.assertIsNotNone(got["tokens_per_sec"])
        self.assertGreater(got["tokens_per_sec"], 0)
        reporting = [t for t in got["trials"] if t["step"] is not None]
        silent = [t for t in got["trials"] if t["step"] is None]
        self.assertEqual((len(reporting), len(silent)), (4, 4))
        self.assertTrue(all(t["n_cache_tokens"] is None and t["cost_usd"] is None for t in got["trials"]))
        self.assertTrue(all("[REDACTED]" in t["activity"] for t in reporting))
        self.assertNotRegex(json.dumps(got), r"sk-[A-Za-z0-9_-]{16,}")
        self.assertIsNone(got["totals"]["cost_usd"])

    def test_harbors_recorded_prepare_run_folds_at_both_moments(self):
        # The prepare feed contains:
        # job.prepare {stage: dataset, n_concurrent, agents} the
        # moment the job directory is claimed, job.prepare {stage: tasks}
        # before caching, then job.start — 14 ms later on a local dataset.
        # Harbor's stage vocabulary is dataset/tasks (images build per trial,
        # after job.start). n_total is not on its prepare lines, so the
        # header's total is null, not 0, until job.start.
        root = FIXTURES / "harbor-prepare-x4"
        feed = live.read_events(root / "events.jsonl")
        first = feed.events[0].ts
        start = next(e.ts for e in feed.events if e.event == "job.start")
        mid = first + (start - first) / 2
        during = live.fold([e for e in feed.events if e.ts <= mid], now=mid).snapshot()
        self.assertEqual(during, json.loads((root / "expected-preparing.json").read_text()))
        self.assertEqual(during["state"], "preparing")
        self.assertEqual(during["preparing"]["stage"], "tasks")
        self.assertIsNone(during["counts"]["total"])
        self.assertEqual((during["n_concurrent"], during["agents"]), (4, ["oracle"]))
        after = live.fold(feed.events).snapshot()
        self.assertEqual(after, json.loads((root / "expected.json").read_text()))
        self.assertEqual((after["state"], after["counts"]["completed"], after["counts"]["total"]), ("finished", 4, 4))
        self.assertEqual(after["launched_at"], "2026-09-14T08:35:10.101636Z")
        self.assertLess((start - first).total_seconds(), 0.1)

    def test_the_two_agent_job_folds_to_its_snapshot(self):
        # two-agents: hand-written, two declared agents on the same three tau3
        # tasks — the head-to-head the board's lanes are for.
        # One task decided with a winner, one tied, one still running; the
        # running trial reports progress, so the rate is measured.
        root = FIXTURES / "two-agents"
        now = datetime(2026, 9, 13, 20, 7, 20, tzinfo=timezone.utc)
        got = live.load(root / "events.jsonl", now=now).snapshot()
        want = json.loads((root / "expected.json").read_text())
        self.assertEqual(got, want)
        self.assertEqual([l["agent"] for l in got["lanes"]], ["my-agent", "opencode"])
        js, oc = got["lanes"]
        self.assertEqual((js["counts"]["completed"], js["mean_reward"], js["wins"]), (3, 1.0, 1))
        self.assertEqual((oc["counts"]["completed"], oc["counts"]["running"], oc["mean_reward"], oc["wins"]), (2, 1, 0.5, 0))
        self.assertIsNone(oc["totals"]["n_cache_tokens"])          # opencode never reported a cache count
        self.assertEqual([(w["task"].removeprefix("tau3-bench__tau3-"), w["decided"], w["winner"]) for w in got["winners"]],
                         [("airline-0", True, "my-agent"), ("retail-0", True, None), ("telecom-0", False, None)])
        self.assertEqual(got["winners"][2]["rewards"], {"my-agent": 1.0, "opencode": None})
        self.assertEqual(got["tokens_per_sec"], (50000 + 700) / 30)
        self.assertEqual(got["eta_sec"], 35.0)                    # what is left of a median for the running trial
        self.assertIsNone(got["totals"]["cost_usd"])

    def test_the_rate_eta_job_folds_to_its_snapshot(self):
        # rate-eta: hand-written, one agent on five tau3 tasks, read at
        # 10:06:00 — two finished (so the ETA exists), two in agent_run and
        # reporting progress inside the 30 s window with a baseline each (so
        # the rate is measured), one still queued; the running card's latest
        # activity and its last log line both carry a fake key. Every number
        # the renderer test pins as text is checked here by hand first.
        root = FIXTURES / "rate-eta"
        now = datetime(2026, 9, 14, 10, 6, 0, tzinfo=timezone.utc)
        got = live.load(root / "events.jsonl", now=now).snapshot()
        want = json.loads((root / "expected.json").read_text())
        self.assertEqual(got, want)
        self.assertEqual({t["trial"]: t["state"] for t in got["trials"]},
                         {"airline-0__p1": "done", "retail-0__p2": "done", "telecom-0__p3": "running",
                          "banking-001__p4": "running", "banking-002__p5": "queued"})
        # window 10:05:30 – 10:06:00: telecom 10:05:00 → 10:05:40 on all three
        # keys, banking 10:05:45 → 10:05:55 on two (no cache key, no delta for it)
        self.assertEqual(got["tokens_per_sec"], ((180000 - 120000) + (140000 - 90000) + (2400 - 1500)
                                                 + (45000 - 30000) + (600 - 400)) / 30)
        # median(155 s, 189 s) = 172 s a trial: the pending one a whole median, telecom (started 10:02:45,
        # 195 s ago) nothing more, banking (started 10:03:20, 160 s ago) the 12 s left of it; ÷ 2 concurrent
        self.assertEqual(got["eta_sec"], (172 + 0 + 12) / 2)
        self.assertEqual(got["elapsed_sec"], 360.0)
        self.assertEqual(got["totals"], {"n_input_tokens": 785000, "n_cache_tokens": 590000,
                                         "n_output_tokens": 10500, "cost_usd": None})
        telecom = next(t for t in got["trials"] if t["trial"] == "telecom-0__p3")
        self.assertEqual((telecom["phase"], telecom["elapsed_sec"], telecom["step"]), ("agent_run", 160.0, 8))
        self.assertEqual(telecom["activity"], "tool call tau3-runtime_send_message_to_user token=[REDACTED]")
        self.assertEqual(telecom["last_line"], "[gateway] session ready, key [REDACTED] loaded")
        self.assertNotIn("sk-test-", json.dumps(got))
        self.assertEqual(got["winners"], []); self.assertEqual(len(got["lanes"]), 1)

    def test_the_tied_and_the_undecided_jobs_fold_to_their_snapshots(self):
        # two-agents-tie: the same two agents on two tasks, every trial ended,
        # both agents level on each — airline both 1.0, retail both 0.0 — so
        # both contests are decided and neither names a winner; a third task
        # is not queued yet. two-agents-undecided: the same duel read three
        # minutes in — one trial ended, one reporting, one in setup, one
        # queued — so neither contest is decided and nothing names a winner.
        root = FIXTURES / "two-agents-tie"
        got = live.load(root / "events.jsonl", now=datetime(2026, 9, 14, 11, 6, 0, tzinfo=timezone.utc)).snapshot()
        self.assertEqual(got, json.loads((root / "expected.json").read_text()))
        self.assertEqual([(w["task"].removeprefix("tau3-bench__tau3-"), w["decided"], w["winner"], w["rewards"]) for w in got["winners"]],
                         [("airline-0", True, None, {"my-agent": 1.0, "opencode": 1.0}),
                          ("retail-0", True, None, {"my-agent": 0.0, "opencode": 0.0})])
        self.assertEqual([(l["agent"], l["wins"], l["counts"]["completed"]) for l in got["lanes"]],
                         [("my-agent", 0, 2), ("opencode", 0, 2)])
        self.assertEqual((got["state"], got["counts"]["pending"], got["eta_sec"], got["tokens_per_sec"]),
                         ("running", 2, 155.0, None))            # totals came on trial.end only: no rate
        root = FIXTURES / "two-agents-undecided"
        got = live.load(root / "events.jsonl", now=datetime(2026, 9, 14, 12, 3, 0, tzinfo=timezone.utc)).snapshot()
        self.assertEqual(got, json.loads((root / "expected.json").read_text()))
        self.assertEqual([(w["task"].removeprefix("tau3-bench__tau3-"), w["decided"], w["winner"], w["rewards"]) for w in got["winners"]],
                         [("airline-0", False, None, {"my-agent": 1.0, "opencode": None}),
                          ("retail-0", False, None, {"my-agent": None, "opencode": None})])
        self.assertEqual([l["wins"] for l in got["lanes"]], [0, 0])
        self.assertEqual({t["trial"]: t["state"] for t in got["trials"]},
                         {"airline-0__js": "done", "airline-0__oc": "running", "retail-0__js": "running", "retail-0__oc": "queued"})
        self.assertIsNone(got["eta_sec"])                          # one finished, not two


class TheRoute(unittest.TestCase):
    """What the board adds on top of the fold: reading a job directory."""

    def test_snapshot_reads_a_job_directory_and_is_clock_driven(self):
        s = live.snapshot(str(FIXTURES), now=NOW)
        self.assertEqual(s["now"], "2026-09-12T18:29:20Z")
        self.assertTrue(live.has_feed(str(FIXTURES)))
        json.dumps(s)   # the route returns it as JSON

    def test_no_feed_means_no_board(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(live.has_feed(d))

    def test_the_timeline_rides_beside_the_snapshot_never_inside_it(self):
        # Harbor's recorded oracle run: four trials, each phase started once and
        # each trial ended once. The snapshot stays the same document.
        root = FIXTURES / "harbor-oracle-x4"
        plain = live.snapshot(str(root), now=NOW)
        withit = live.snapshot(str(root), now=NOW, with_timeline=True)
        self.assertNotIn("timeline", plain)
        self.assertEqual({k: v for k, v in withit.items() if k != "timeline"}, plain)
        tl = withit["timeline"]
        self.assertEqual(set(tl), {t["trial"] for t in plain["trials"]})
        for trial, t in tl.items():
            self.assertEqual(list(t["starts"])[0], "env_setup", trial)
            self.assertIsNotNone(t["ended"], trial)
            self.assertTrue(all(t["starts"][a] <= t["starts"][b] for a, b in zip(list(t["starts"]), list(t["starts"])[1:])), trial)

    def test_the_timeline_keeps_the_first_start_and_leaves_an_open_trial_unended(self):
        events = [live.parse_line(e) for e in (
            ev(1, "trial.phase", phase="env_setup", ts="2026-09-12T18:20:00Z"),
            ev(2, "trial.phase", phase="agent_run", ts="2026-09-12T18:20:30Z"),
            ev(3, "trial.phase", phase="agent_run", ts="2026-09-12T18:21:00Z"),      # a repeat does not move the start
            ev(4, "trial.phase", trial="t2", phase="env_setup", ts="2026-09-12T18:20:05Z"),
            ev(5, "trial.end", outcome="resolved", ts="2026-09-12T18:22:00Z"))]
        self.assertEqual(live.timeline(events), {
            "t1": {"starts": {"env_setup": "2026-09-12T18:20:00Z", "agent_run": "2026-09-12T18:20:30Z"}, "ended": "2026-09-12T18:22:00Z"},
            "t2": {"starts": {"env_setup": "2026-09-12T18:20:05Z"}, "ended": None}})


class EndedByRunner(unittest.TestCase):
    """A feed Harbor never closed - the run was stopped, or died - reads as
    a job still going on its own; the runner's record ends it."""
    def lines(self):
        start = datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc)
        def at(sec): return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")
        return start, [ev(1, "job.start", trial=None, ts=at(0), n_total=4, n_concurrent=2),
                       ev(2, "trial.queued", trial="a", ts=at(0), task="t1"), ev(3, "trial.queued", trial="b", ts=at(0), task="t2"),
                       ev(4, "trial.phase", trial="a", ts=at(2), phase="env_setup"),
                       ev(5, "trial.phase", trial="b", ts=at(3), phase="env_setup"),
                       ev(6, "trial.end", trial="b", ts=at(30), outcome="resolved", reward=1.0)]

    def test_the_board_settles_at_the_stop_and_says_what_never_finished(self):
        start, lines = self.lines()
        now = start + timedelta(seconds=120)
        alive = board_of(*lines, now=now)
        self.assertEqual((alive.state, alive.counts()["running"], alive.counts()["pending"]), ("running", 1, 2))
        b = board_of(*lines, now=now)
        stopped_at = start + timedelta(seconds=58)
        b.end_by_runner(stopped_at, "stopped")
        self.assertEqual((b.state, b.finished_at, b.ended_by), ("finished", stopped_at, "stopped"))
        self.assertEqual(b.elapsed_sec(), 58.0)                                                # the clock stops there
        a = next(t for t in b.trials if t.trial == "a")
        self.assertEqual((a.state, a.outcome, a.finished_at), ("done", "cancelled", stopped_at))
        self.assertEqual((b.counts()["running"], b.counts()["completed"], b.counts()["pending"], b.counts()["not_run"]), (0, 2, 0, 2))
        self.assertEqual(b.notices[-1], "The run was stopped at 08:00:58Z before Harbor wrote job.end: 1 trial cancelled, 2 never started.")
        snap = b.snapshot()
        self.assertEqual((snap["state"], snap["running"], snap["ended_by"]), ("finished", False, "stopped"))
        self.assertNotIn("ended_by", board_of(*lines, now=now).snapshot())                     # absent unless the runner ended it
        self.assertEqual(snap["finished_at"], "2026-09-14T08:00:58Z")

    def test_a_run_its_feed_ended_early_counts_what_never_began_as_not_run(self):
        """A run its spending cap stopped, or its endpoint: job.end, with units never begun. They are not queued."""
        start, lines = self.lines()
        lines += [ev(7, "trial.end", trial="a", ts=at_(start, 40), outcome="budget"),
                  ev(8, "job.end", trial=None, ts=at_(start, 41), n_completed=1, n_errored=1, n_cancelled=2)]
        c = board_of(*lines, now=start + timedelta(seconds=100)).counts()
        self.assertEqual((c["total"], c["completed"], c["pending"], c["not_run"]), (4, 2, 0, 2))     # job.end's n_cancelled
        self.assertNotIn("not_run", board_of(*lines[:-1], now=start + timedelta(seconds=100)).counts())   # still going: queued

    def test_a_feed_with_its_own_end_is_left_alone(self):
        start, lines = self.lines()
        lines.append(ev(7, "trial.end", trial="a", ts=at_(start, 40), outcome="unresolved", reward=0.0))
        lines.append(ev(8, "job.end", trial=None, ts=at_(start, 41)))
        b = board_of(*lines, now=start + timedelta(seconds=100))
        b.end_by_runner(start + timedelta(seconds=90), "stopped")
        self.assertEqual((b.elapsed_sec(), b.ended_by, b.notices), (41.0, None, []))

    def test_snapshot_takes_the_runner_record(self):
        start, lines = self.lines()
        with tempfile.TemporaryDirectory() as d:
            Path(d, "events.jsonl").write_text("\n".join(lines) + "\n")
            snap = live.snapshot(d, now=start + timedelta(seconds=100), ended=("failed", start + timedelta(seconds=70)))
            self.assertEqual((snap["state"], snap["ended_by"], snap["counts"]["running"]), ("finished", "failed", 0))
            self.assertIn("The run was failed at 08:01:10Z", snap["notices"][-1])
            self.assertEqual(live.snapshot(d, now=start + timedelta(seconds=100))["state"], "running")   # without it, as before


def at_(start, sec):
    return (start + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
