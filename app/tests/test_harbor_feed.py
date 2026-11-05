"""The live feed any Harbor writes through ``nullius.harbor.feed``.

A stand-in ``Job`` with Harbor's hook methods runs two trials; the lines it
writes are folded by the board's own reader (``nullius.runs.live``), so the
feed is proven against what the page reads, not against a copy of its schema.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nullius.harbor import feed
from nullius.runs import live

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def timing(start, seconds):
    return SimpleNamespace(started_at=T0 + timedelta(seconds=start), finished_at=T0 + timedelta(seconds=start + seconds))


def trial_config(name, task):
    return SimpleNamespace(trial_name=name, agent=SimpleNamespace(name="oracle", model_name=None),
                           task=SimpleNamespace(get_task_id=lambda: SimpleNamespace(get_name=lambda: task)))


def result(reward=None, exception=None):
    return SimpleNamespace(verifier_result=SimpleNamespace(rewards={"reward": reward}) if reward is not None else None,
                           exception_info=SimpleNamespace(exception_type=exception) if exception else None,
                           agent_result=SimpleNamespace(n_input_tokens=120, n_cache_tokens=None, n_output_tokens=30, cost_usd=None),
                           environment_setup=timing(0, 3), agent_setup=timing(3, 0), agent_execution=timing(3, 5), verifier=timing(8, 2))


def stand_in(job_dir):
    """A Job as Harbor's: hooks registered by ``on_*``, trial configs, ``run``
    calling the hooks for each trial in turn."""
    class Job:
        def __init__(self):
            self.job_dir = job_dir
            self.config = SimpleNamespace(job_name="feedtest", n_concurrent_trials=2)
            self._trial_configs = [trial_config("hello__a", "hello"), trial_config("broken__b", "broken")]
            self._remaining_trial_configs = list(self._trial_configs)
            self.hooks = {}

        def __getattr__(self, name):
            if name.startswith("on_"):
                return lambda cb: self.hooks.setdefault(name, []).append(cb)
            raise AttributeError(name)

        async def run(self):
            outcomes = {"hello__a": result(reward=1.0), "broken__b": result(exception="AgentTimeoutError")}
            for c in self._trial_configs:
                event = SimpleNamespace(trial_name=c.trial_name, task_name=c.task.get_task_id().get_name(), config=c, result=outcomes[c.trial_name])
                for hook in ("on_environment_started", "on_agent_started", "on_verification_started", "on_trial_ended"):
                    for cb in self.hooks.get(hook, []):
                        await cb(event)
            return SimpleNamespace(stats=SimpleNamespace(n_completed_trials=2, n_errored_trials=1, n_cancelled_trials=0))
    return Job


class Feed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nullius-feed-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def test_a_harbor_without_a_feed_writes_one_the_board_folds(self):
        Job = stand_in(self.dir)
        self.assertTrue(feed.install(Job))
        asyncio.run(Job().run())
        with open(os.path.join(self.dir, feed.FEED)) as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual([x["seq"] for x in lines], list(range(1, len(lines) + 1)))       # strictly increasing
        self.assertEqual(lines[0]["event"], "job.start"); self.assertEqual(lines[-1]["event"], "job.end")
        snap = live.snapshot(self.dir)
        self.assertEqual(snap["state"], "finished")
        self.assertEqual((snap["counts"]["total"], snap["counts"]["resolved"], snap["counts"]["errored"]), (2, 1, 1))
        a, b = snap["trials"]
        self.assertEqual((a["outcome"], a["reward"], a["phases_done"]), ("resolved", 1.0, ["env_setup", "agent_run", "verify"]))
        self.assertEqual(b["outcome"], "timeout")
        self.assertIsNone(a["cost_usd"])                                                    # not reported is absent, never 0
        self.assertNotIn("agent_setup", [p for t in snap["trials"] for p in t["phases_done"]])   # no hook for it: left out, not made up

    def test_a_phase_carries_the_trial_s_own_limit(self):
        class Trial:
            def __init__(self, config):
                self.config = config
                self.environment_build_timeout_sec, self.agent_timeout_sec, self.verifier_timeout_sec = 1800.0, 3000.0, None
        Job = stand_in(self.dir)
        feed.install(Job, Trial)
        job = Job()
        for c in job._trial_configs:
            Trial(c)                                                                           # as Harbor makes them
        asyncio.run(job.run())
        snap = live.snapshot(self.dir)
        with open(os.path.join(self.dir, feed.FEED)) as fh:
            phases = [json.loads(x) for x in fh if '"trial.phase"' in x]
        self.assertEqual({(p["phase"], p.get("timeout_sec")) for p in phases},
                         {("env_setup", 1800.0), ("agent_run", 3000.0), ("verify", None)})       # a limit it does not have is left out
        self.assertEqual(snap["state"], "finished")

    def test_a_harbor_that_writes_its_own_feed_is_left_alone(self):
        Job = stand_in(self.dir)
        Job._init_live_feed = lambda self: None
        run = Job.run
        self.assertFalse(feed.install(Job))
        self.assertIs(Job.run, run)

    def test_a_feed_that_fails_never_stops_the_run(self):
        Job = stand_in(os.path.join(self.dir, "missing", "\0bad"))                          # the feed cannot be written
        feed.install(Job)
        stats = asyncio.run(Job().run()).stats
        self.assertEqual(stats.n_completed_trials, 2)

    def test_a_hook_that_fails_never_reaches_the_trial(self):
        Job = stand_in(self.dir)
        feed.install(Job)
        job = Job()
        with unittest.mock.patch.object(feed.Writer, "emit", side_effect=[None, None, None, OSError("disk full")] + [OSError("disk full")] * 20):
            stats = asyncio.run(job.run()).stats                                         # the job.start lines, then every write fails
        self.assertEqual(stats.n_completed_trials, 2)

    def test_outcomes_follow_the_reward_then_the_exception(self):
        self.assertEqual(feed.outcome_of(result(reward=0.0))[0], "unresolved")
        self.assertEqual(feed.outcome_of(result(exception="ApiRateLimitError"))[0], "infra_error")
        self.assertEqual(feed.outcome_of(result(exception="ValueError"))[0], "agent_error")
        self.assertEqual(feed.outcome_of(result(reward=1.0, exception="CancelledError"))[0], "cancelled")


if __name__ == "__main__":
    unittest.main()
