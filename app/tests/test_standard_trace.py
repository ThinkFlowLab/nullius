"""A standard trial's ``agent/trace.json``: the trial page's waterfall, from what
Nullius saw: the relay's model calls and the ACP tool calls, under the agent's turns."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from nullius.standard import job
from nullius.traces import trace

T = 1_800_000_000.0
AGENT = {"key": "codex", "_kind": "agent"}
BENCH = {"key": "b", "family": "f", "needs": [], "score": {"kind": "pass", "says": "x"}}


def ev(at, update, **fields):
    return {"t": T + at, "update": {"sessionUpdate": update, **fields}}


def call(t0, t1, **more):
    return {"t0": T + t0, "t1": T + t1, "model": "m", "status": 200, "path": "chat/completions",
            "request": {"last": [{"role": "user", "content": "go"}]},
            "reply": {"text": "ok", "tool_calls": [{"name": "bash"}],
                      "usage": {"input": 10, "output": 4, "cached": 2, "reasoning": 1}}, **more}


EVENTS = [
    ev(0, "user_message_chunk", content={"type": "text", "text": "first"}),
    ev(1, "agent_thought_chunk", content={"type": "text", "text": "hm"}),
    ev(2, "tool_call", toolCallId="a", title="bash", status="pending", kind="execute", rawInput={"command": "ls"}),
    ev(3, "tool_call_update", toolCallId="a", status="in_progress"),
    ev(8, "tool_call_update", toolCallId="a", status="completed", content=[{"type": "content", "content": {"type": "text", "text": "out"}}]),
    ev(10, "tool_call", toolCallId="b", title="never ends", status="in_progress", kind="execute"),
    ev(20, "user_message_chunk", content={"type": "text", "text": "second"}),
    ev(21, "agent_message_chunk", content={"type": "text", "text": "done"}),
]


def timeline(events=EVENTS, calls=(), other=AGENT, produce=(T, T + 30)):
    return job.trace_from_run(job.trajectory_from_events(list(events), list(calls), other, "m", "j/u"), list(calls), other, produce)


class AgentTrace(unittest.TestCase):
    def setUp(self):
        self.doc = timeline(calls=[call(0.5, 2.5), call(22, 25)])
        self.session = self.doc["spans"][0]
        self.turns = [c for c in self.session["children"] if c["kind"] == "user_message"]

    def test_the_session_is_the_agents_window_and_its_turns_hold_what_began_in_them(self):
        self.assertEqual(self.doc["schema_version"], "harbor-trace-v1")
        self.assertEqual(self.doc["totals"]["wall_ns"], 30 * 10**9)
        self.assertEqual([t["name"] for t in self.turns], ["turn 1", "turn 2"])
        self.assertEqual([c["kind"] for c in self.turns[0]["children"]], ["llm_request", "reasoning", "tool", "tool"])
        self.assertEqual([c["kind"] for c in self.turns[1]["children"]], ["output", "llm_request"])

    def test_a_tool_runs_from_when_it_began_not_when_it_was_announced(self):
        tool = self.turns[0]["children"][2]
        self.assertEqual(tool["attributes"]["tool_name"], "bash")
        self.assertEqual(tool["duration_ns"], 5 * 10**9)
        self.assertEqual(tool["timing_quality"], "measured")
        self.assertEqual(tool["content"], {"arguments": {"command": "ls"}, "result": "out"})

    def test_a_tool_never_seen_to_end_has_no_duration(self):
        tool = self.turns[0]["children"][3]
        self.assertNotIn("end_ns", tool)
        self.assertEqual(tool["timing_quality"], "unknown")

    def test_a_model_call_keeps_the_relays_clock_and_tokens(self):
        llm = self.turns[0]["children"][0]
        self.assertEqual((llm["source"], trace.parse(self.doc).rows[2].span.label), ("relay", "m"))
        self.assertEqual(llm["duration_ns"], 2 * 10**9)
        self.assertEqual(llm["tokens"], {"input_tokens": 10, "output_tokens": 4, "cache_read_tokens": 2, "reasoning_tokens": 1})
        self.assertEqual(llm["attributes"]["tool_calls"], "bash")

    def test_the_reader_draws_it_and_the_totals_add_up(self):
        parsed = trace.parse(self.doc)
        self.assertTrue(parsed.has_spans)
        self.assertEqual(parsed.totals.wall_ns, 30 * 10**9)
        self.assertEqual(parsed.totals.tool_ns, 5 * 10**9)
        self.assertEqual(parsed.totals.llm_ns, 5 * 10**9)

    def test_a_failed_call_says_so_and_a_run_that_never_began_has_no_window(self):
        doc = timeline(events=[], calls=[{"t0": T, "t1": T + 1, "model": "m", "status": 429, "error": "rate limited"}], produce=(None, None))
        llm = doc["spans"][0]["children"][0]
        self.assertEqual((llm["attributes"]["status"], llm["attributes"]["error"]), (429, "rate limited"))
        self.assertEqual(doc["totals"]["wall_ns"], 10**9)


class OtherTrace(unittest.TestCase):
    def test_a_capabilitys_requests_are_tools_and_its_model_calls_are_not_turns(self):
        other = {"key": "mem0", "_kind": "capability"}
        calls = [call(2, 3)]
        traj = job.trajectory_from_exchanges([{"op": "recall", "request": {"q": 1}, "reply": {"memory": "x"}, "t0": T, "t1": T + 4}],
                                             calls, other, "m", "j/u")
        spans = job.trace_from_run(traj, calls, other, (T, T + 5))["spans"][0]["children"]
        self.assertEqual([s["kind"] for s in spans], ["tool", "llm_request"])
        self.assertEqual(spans[0]["attributes"]["tool_name"], "mem0.recall")
        self.assertEqual(spans[0]["duration_ns"], 4 * 10**9)

    def test_nothing_recorded_is_no_trace(self):
        self.assertIsNone(timeline(events=[]))


class Written(unittest.TestCase):
    def write(self, other, trajectory):
        d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        job.write_trial(d, job="j", bench=BENCH, other=other, model="m", unit="u", t0=T, t1=T + 40, produce=(T, T + 30),
                        score={"reward": 1.0}, exception=None, calls=[call(1, 2)] if trajectory else [], trajectory=trajectory)
        return os.path.join(d, "agent", "trace.json")

    def test_a_trial_writes_its_trace_beside_its_trajectory(self):
        path = self.write(AGENT, job.trajectory_from_events(list(EVENTS), [call(1, 2)], AGENT, "m", "j/u"))
        with open(path) as f:
            self.assertEqual(json.load(f)["spans"][0]["name"], "codex")
        self.assertTrue(trace.load(path).has_spans)

    def test_a_control_writes_none(self):
        self.assertFalse(os.path.exists(self.write({"key": "nop", "_kind": "control"}, None)))


class Backfill(unittest.TestCase):
    """A trial written before traces gets one from the files it kept, and nothing else changes."""

    def job(self, kind="agent"):
        d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump({"standard": {"kind": kind}}, f)
        return d

    def trial(self, job_dir, name, other=AGENT, events=True, score=None):
        trial = os.path.join(job_dir, name)
        calls = [call(1, 2)] if other["_kind"] != "control" else []
        traj = None if other["_kind"] == "control" else job.trajectory_from_events(list(EVENTS), calls, other, "m", "j/u")
        job.write_trial(trial, job="j", bench=BENCH, other=other, model="m", unit="u", t0=T, t1=T + 40, produce=(T, T + 30),
                        score=score or {"reward": 1.0}, exception=None, calls=calls, trajectory=traj,
                        extra_logs={"acp.jsonl": "".join(json.dumps(e) + "\n" for e in EVENTS)} if events else None)
        path = os.path.join(trial, "agent", "trace.json")
        if os.path.exists(path):
            os.unlink(path)                  # as a trial from before traces has it
        return trial, path

    def test_a_trial_that_kept_its_events_gets_tool_times_and_a_dry_run_writes_nothing(self):
        trial, path = self.trial(self.job(), "a")
        self.assertEqual(job.backfill_trace(trial), "full")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(job.backfill_trace(trial, write=True), "full")
        with open(path) as f:
            session = json.load(f)["spans"][0]
        tool = next(s for s in session["children"][0]["children"] if s["kind"] == "tool")
        self.assertEqual(tool["duration_ns"], 5 * 10**9)
        self.assertEqual(session["duration_ns"], 30 * 10**9)

    def test_a_trial_that_kept_none_gets_its_model_calls_and_tools_with_no_duration(self):
        trial, path = self.trial(self.job(), "a", events=False)
        with open(os.path.join(trial, "agent", "trajectory.json")) as f:
            kept = json.load(f)
        for step in kept["steps"]:
            (step.get("extra") or {}).pop("ended_at", None)        # written before tool clocks
            (step.get("extra") or {}).pop("running_at", None)
        with open(os.path.join(trial, "agent", "trajectory.json"), "w") as f:
            json.dump(kept, f)
        self.assertEqual(job.backfill_trace(trial, write=True), "partial")
        with open(path) as f:
            spans = list(trace.raw_spans_of(json.load(f)["spans"]))
        self.assertEqual(next(s for s in spans if s["kind"] == "llm_request")["duration_ns"], 10**9)
        self.assertTrue(all("duration_ns" not in s for s in spans if s["kind"] == "tool"))

    def test_an_existing_trace_is_never_replaced_a_link_included(self):
        trial, path = self.trial(self.job(), "a")
        with open(path, "w") as f:
            f.write("captured")
        self.assertEqual(job.backfill_trace(trial, write=True), "has_trace")
        with open(path) as f:
            self.assertEqual(f.read(), "captured")
        os.unlink(path)
        os.symlink("/nonexistent/elsewhere", path)
        self.assertEqual(job.backfill_trace(trial, write=True), "has_trace")
        self.assertTrue(os.path.islink(path))

    def test_controls_and_other_runners_are_left_alone(self):
        d = self.job("control")
        trial, path = self.trial(d, "a", other={"key": "nop", "_kind": "control"}, events=False)
        self.assertEqual(job.backfill_trace(trial, write=True), "control")
        self.assertFalse(os.path.exists(path))
        harbor = os.path.join(d, "h")
        os.makedirs(os.path.join(harbor, "agent"))
        with open(os.path.join(harbor, "result.json"), "w") as f:
            json.dump({"task_name": "t"}, f)
        self.assertEqual(job.backfill_trace(harbor, write=True), "not_standard")
        self.assertEqual(job.trial_dirs(d), [os.path.join(d, "a"), harbor])

    def test_a_folder_of_jobs_is_walked_two_levels(self):
        parent = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        d = os.path.join(parent, "one")
        os.makedirs(d)
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump({"standard": {"kind": "agent"}}, f)
        self.trial(d, "a")
        self.assertEqual(job.trial_dirs(parent), [os.path.join(d, "a")])


class ShortTitle(unittest.TestCase):
    def test_a_path_reads_as_its_last_part_and_the_whole_title_is_kept(self):
        for title, short in (("Fetching file:///var/tmp/run/x__u1/workspace/calc.py", "Fetching calc.py"),
                             ("Reading x__u1/workspace/test_calc.py", "Reading test_calc.py"),
                             ("pytest -q", "pytest -q"), ("Tool Call", "Tool Call")):
            self.assertEqual(job._short_title(title), short)
        doc = timeline([ev(0, "tool_call", toolCallId="a", title="Reading /a/b/c.py", status="completed")])
        tool = doc["spans"][0]["children"][0]
        self.assertEqual(tool["attributes"]["tool_name"], "Reading c.py")
        self.assertEqual(tool["attributes"]["title"], "Reading /a/b/c.py")


if __name__ == "__main__":
    unittest.main()
