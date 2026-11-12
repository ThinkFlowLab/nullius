"""The ACP client drives an agent through sessions and turns, serves its
requests only inside the workspace, and survives an agent that hangs or dies."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from nullius.standard import acp

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "fake_acp_agent.py")


def ev(kind, text=None, **fields):
    u = {"sessionUpdate": kind, **fields}
    if text is not None:
        u["content"] = {"type": "text", "text": text}
    return {"t": 0, "update": u}


class LastMessage(unittest.TestCase):
    def test_message_ids_separate_streamed_and_final_answers(self):
        answer = '{"answer":"lantern"}'
        evs = [ev("agent_message_chunk", answer[:10], messageId="stream"),
               ev("agent_message_chunk", answer[10:], messageId="stream"),
               ev("agent_message_chunk", answer, messageId="final"), ev("session_info_update")]
        self.assertEqual(acp.last_message(evs), answer)

    def test_same_message_id_joins_chunks_across_metadata_and_thoughts(self):
        evs = [ev("agent_message_chunk", "lan", messageId="answer"), ev("session_info_update"),
               ev("agent_thought_chunk", "thinking", messageId="thought"), ev("usage_update"),
               ev("agent_message_chunk", "tern", messageId="answer")]
        self.assertEqual(acp.last_message(evs), "lantern")

    def test_missing_ids_keep_the_existing_message_boundaries(self):
        evs = [ev("agent_message_chunk", "lan", messageId="answer"), ev("agent_message_chunk", "tern"),
               ev("agent_message_chunk", "!", messageId="answer")]
        self.assertEqual(acp.last_message(evs), "lantern!")
        evs += [ev("available_commands_update"), ev("agent_message_chunk", "final")]
        self.assertEqual(acp.last_message(evs), "final")
        evs += [ev("tool_call"), ev("agent_message_chunk", "new", messageId="answer")]
        self.assertEqual(acp.last_message(evs), "new")

    def test_the_answer_is_the_message_after_the_last_other_event(self):
        evs = [ev("user_message_chunk", "q"), ev("agent_message_chunk", "Model metadata not found."),
               ev("available_commands_update"), ev("agent_message_chunk", "I'll look."), ev("tool_call"),
               ev("tool_call_update"), ev("agent_message_chunk", "lan"), ev("usage_update"), ev("agent_message_chunk", "tern")]
        self.assertEqual(acp.last_message(evs), "lantern")

    def test_metadata_after_the_answer_does_not_erase_it(self):
        # Goose: the answer, its usage, then the session's title
        evs = [ev("user_message_chunk", "q"), ev("tool_call"), ev("agent_message_chunk", "lan"), ev("agent_message_chunk", "tern"),
               ev("usage_update"), ev("session_info_update"), ev("current_mode_update")]
        self.assertEqual(acp.last_message(evs), "lantern")

    def test_metadata_between_two_messages_parts_them(self):
        # Codex: a notice sent as text, its commands, then the answer
        evs = [ev("user_message_chunk", "q"), ev("agent_message_chunk", "Model metadata for `m` not found."),
               ev("available_commands_update"), ev("agent_message_chunk", "lan"), ev("agent_message_chunk", "tern"), ev("usage_update")]
        self.assertEqual(acp.last_message(evs), "lantern")

    def test_no_message_is_an_empty_answer(self):
        self.assertEqual(acp.last_message([ev("user_message_chunk", "q"), ev("tool_call")]), "")


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.c = acp.Client([sys.executable, FAKE], dict(os.environ), os.path.join(self.tmp, "agent.log"), grant=True).start()

    def tearDown(self):
        self.c.close()

    def test_a_turn_returns_the_agents_message_whole(self):
        sid = self.c.new_session(self.ws)
        t = self.c.prompt(sid, "hello there", timeout=20)
        self.assertEqual((t.text, t.stop), ("HELLO THERE", "end_turn"))
        self.assertEqual(t.events[0]["update"]["sessionUpdate"], "user_message_chunk")

    def test_the_agent_writes_inside_its_workspace(self):
        sid = self.c.new_session(self.ws)
        t = self.c.prompt(sid, "write out.txt forty two", timeout=20)
        with open(os.path.join(self.ws, "out.txt")) as f:
            self.assertEqual(f.read(), "forty two")
        kinds = [e["update"]["sessionUpdate"] for e in t.events]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_call_update", kinds)

    def test_a_write_outside_the_workspace_is_refused(self):
        sid = self.c.new_session(self.ws)
        t = self.c.prompt(sid, "write ../escape.txt nope", timeout=20)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))
        upd = [e["update"] for e in t.events if e["update"].get("sessionUpdate") == "tool_call_update"][0]
        self.assertEqual(upd["status"], "failed")

    def test_permission_is_granted(self):
        sid = self.c.new_session(self.ws)
        self.assertEqual(self.c.prompt(sid, "ask", timeout=20).text, "granted yes")

    def test_an_adapters_warning_comes_as_a_notice_not_in_the_answer(self):
        # the client takes notices, so an adapter's warning is never text in front of the answer
        from nullius.standard import job
        sid = self.c.new_session(self.ws)
        t = self.c.prompt(sid, "warn", timeout=20)
        self.assertEqual(t.text, "ok")
        self.assertIn("notice", [e["update"]["sessionUpdate"] for e in t.events])
        steps = job.trajectory_from_events(t.events, [], {"key": "fake"}, "m", "s")["steps"]
        self.assertIn("warning: Model metadata not found", [s.get("message") for s in steps if s["source"] == "system"])

    def test_permission_is_refused_unless_granting_was_asked_for(self):
        c = acp.Client([sys.executable, FAKE], dict(os.environ), os.path.join(self.tmp, "b.log")).start()
        try:
            sid = c.new_session(self.ws)
            self.assertEqual(c.prompt(sid, "ask", timeout=20).text, "granted none")
        finally:
            c.close()

    def test_turns_of_one_session_remember_each_other(self):
        sid = self.c.new_session(self.ws)
        self.c.prompt(sid, "the code is 7", timeout=20)
        self.assertEqual(self.c.prompt(sid, "recall", timeout=20).text, "the code is 7")

    def test_mcp_servers_reach_the_session(self):
        sid = self.c.new_session(self.ws, [{"name": "bench", "command": "python3", "args": ["x.py"], "env": {"A": "1"}}])
        self.assertEqual(self.c.prompt(sid, "call secret", timeout=20).text, "servers bench")

    def test_a_turn_that_never_ends_times_out_and_the_agent_is_stopped(self):
        sid = self.c.new_session(self.ws)
        with self.assertRaises(acp.Timeout):
            self.c.prompt(sid, "hang", timeout=2)
        self.assertIsNotNone(self.c.p.poll())

    def test_an_agent_that_dies_mid_turn_is_reported(self):
        sid = self.c.new_session(self.ws)
        with self.assertRaisesRegex(acp.AgentFailed, "the agent ended"):
            self.c.prompt(sid, "die", timeout=20)


if __name__ == "__main__":
    unittest.main()
