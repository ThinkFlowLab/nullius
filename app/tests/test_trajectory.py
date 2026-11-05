"""Tests for trajectory parsing: per-step durations and the token series.

Step timestamps exist in both writers, but some older trajectories
were stamped at write time — every step within the same few milliseconds.
Durations from such a clock would be noise, so the parser must refuse them
(same policy as the agent-side metrics matcher).
"""

import json
import os
import tempfile
import unittest
from collections import Counter

from nullius.traces import trajectory


def _load(steps, **top):
    data = {"schema_version": "ATIF-v1.7", "session_id": "s", "steps": steps}
    data.update(top)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "trajectory.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return trajectory.load(path)


class MultimodalMessages(unittest.TestCase):
    """ATIF v1.6 allows a step's message to be a list of content parts; the
    trial, conversation, compare and judge pages raised TypeError on it."""

    def test_the_text_parts_read_and_another_part_is_named(self):
        t = _load([{"step_id": 1, "source": "user", "message": [
                        {"type": "text", "text": "What is in this picture? key sk-abcdefghijklmnop"},
                        {"type": "image", "source": {"media_type": "image/png", "path": "a.png"}}]},
                   {"step_id": 2, "source": "agent", "message": [{"type": "text", "text": "A cat."}]}])
        self.assertFalse(t.error)
        self.assertEqual(t.steps[0].message, "What is in this picture? key [REDACTED]\n[image]")
        self.assertEqual(t.last_agent_message, "A cat.")


class StepDurations(unittest.TestCase):
    def test_durations_from_real_timestamps(self):
        tr = _load(
            [
                {"step_id": 1, "source": "user", "timestamp": "2026-08-09T22:07:00+00:00",
                 "message": "fix it"},
                {"step_id": 2, "source": "agent", "timestamp": "2026-08-09T22:07:06+00:00",
                 "message": "(tool use)"},
                {"step_id": 3, "source": "agent", "timestamp": "2026-08-09T22:07:11+00:00",
                 "message": "done"},
            ]
        )
        self.assertTrue(tr.has_step_clock)
        self.assertEqual(tr.steps[0].dur, 6.0)
        self.assertEqual(tr.steps[1].dur, 5.0)
        self.assertIsNone(tr.steps[2].dur)  # nothing after the last step

    def test_degenerate_clock_yields_no_durations(self):
        tr = _load(
            [
                {"step_id": 1, "source": "user", "timestamp": "2026-08-09T20:55:14.959403+00:00"},
                {"step_id": 2, "source": "agent", "timestamp": "2026-08-09T20:55:14.956392+00:00"},
                {"step_id": 3, "source": "agent", "timestamp": "2026-08-09T20:55:14.956548+00:00"},
            ]
        )
        self.assertFalse(tr.has_step_clock)
        self.assertTrue(all(s.dur is None for s in tr.steps))

    def test_missing_timestamps_tolerated(self):
        tr = _load(
            [
                {"step_id": 1, "source": "user"},
                {"step_id": 2, "source": "agent", "timestamp": "2026-08-09T22:07:06+00:00"},
                {"step_id": 3, "source": "agent", "timestamp": "2026-08-09T22:07:11+00:00"},
            ]
        )
        self.assertIsNone(tr.steps[0].dur)
        self.assertEqual(tr.steps[1].dur, 5.0)


class TokenSeries(unittest.TestCase):
    def test_series_from_step_metrics(self):
        tr = _load(
            [
                {"step_id": 1, "source": "user"},
                {"step_id": 2, "source": "agent",
                 "metrics": {"prompt_tokens": 100, "completion_tokens": 10,
                             "cached_tokens": 0, "cost_usd": 0.001}},
                {"step_id": 3, "source": "agent",
                 "metrics": {"prompt_tokens": 250, "completion_tokens": 20,
                             "cached_tokens": 90,
                             "extra": {"reasoning_tokens": 64}}},
            ]
        )
        self.assertEqual(
            tr.token_series,
            [
                # new/sent are the cache fold's annotations: under this
                # (OpenAI-style) shape, new = prompt − cached and sent = prompt.
                {"step_id": 2, "prompt": 100, "cached": 0, "completion": 10,
                 "new": 100, "sent": 100},
                {"step_id": 3, "prompt": 250, "cached": 90, "completion": 20,
                 "new": 160, "sent": 250},
            ],
        )
        self.assertEqual(tr.total_reasoning_tokens, 64)

    def test_no_metrics_no_series(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "hi"}])
        self.assertEqual(tr.token_series, [])
        self.assertIsNone(tr.total_reasoning_tokens)


class WorkspacePaths(unittest.TestCase):
    """Absolute and relative forms of one path must not read as two files.

    Whether a path arrives as /testbed/x.py or x.py is the model's choice, not
    the harness's: one agent passed relative paths on two runs of a task and
    absolute on a third. Before normalising, "which files did both agents
    touch" answered 0 when both had edited the same two files.
    """

    def test_known_roots_are_stripped(self):
        for raw, want in (("/testbed/sklearn/base.py", "sklearn/base.py"),
                          ("/app/main.go", "main.go")):
            with self.subTest(raw):
                self.assertEqual(trajectory.workspace_relative(raw), want)

    def test_unknown_roots_are_left_alone(self):
        """Better a path we don't recognise than a path we mangle."""
        self.assertEqual(trajectory.workspace_relative("/srv/work/x.py"),
                         "/srv/work/x.py")

    def test_a_root_lookalike_is_not_stripped(self):
        """/testbed-2/ is a different directory to /testbed/."""
        self.assertEqual(trajectory.workspace_relative("/testbed-2/x.py"),
                         "/testbed-2/x.py")

    def test_both_forms_converge(self):
        self.assertEqual(trajectory.workspace_relative("/testbed/a/b.py"),
                         trajectory.workspace_relative("a/b.py"))


class ReasoningTotalFallback(unittest.TestCase):
    """Where the reasoning count lives depends on which agent wrote the file.

    Agents whose LLM calls can be matched to steps carry it per step, under
    `metrics.extra.reasoning_tokens`. Some agents often cannot match them — the
    log says "could not match LLM calls to steps; per-step metrics stay unset"
    — and reports the total in `final_metrics` instead. Reading only the
    per-step path renders nothing for those runs while the number sits in the
    file.
    """

    def test_per_step_totals_still_win(self):
        tr = _load([
            {"step_id": 1, "source": "agent", "message": "a",
             "metrics": {"extra": {"reasoning_tokens": 40}}},
            {"step_id": 2, "source": "agent", "message": "b",
             "metrics": {"extra": {"reasoning_tokens": 26}}},
        ], final_metrics={"total_reasoning_tokens": 999})
        self.assertEqual(tr.total_reasoning_tokens, 66)

    def test_falls_back_to_final_metrics(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_reasoning_tokens": 86})
        self.assertEqual(tr.total_reasoning_tokens, 86)

    def test_absent_stays_none(self):
        # None, not 0: a zero asserts the model did not reason.
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}])
        self.assertIsNone(tr.total_reasoning_tokens)

    # Newer writers move every field ATIF does not name into the `extra`
    # slot of its level; earlier artifacts keep the old position.
    def test_read_from_the_final_metrics_extra_slot(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_prompt_tokens": 10, "extra": {"total_reasoning_tokens": 7}})
        self.assertEqual(tr.total_reasoning_tokens, 7)

    def test_the_slot_wins_over_the_old_position_and_is_not_summed(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_reasoning_tokens": 86, "extra": {"total_reasoning_tokens": 7}})
        self.assertEqual(tr.total_reasoning_tokens, 7)

    def test_a_null_in_the_slot_stays_none(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_reasoning_tokens": 86, "extra": {"total_reasoning_tokens": None}})
        self.assertIsNone(tr.total_reasoning_tokens)

    def test_a_non_dict_extra_falls_back_to_the_old_position(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_reasoning_tokens": 86, "extra": "not a slot"})
        self.assertEqual(tr.total_reasoning_tokens, 86)


def _metered(*calls):
    """One agent step per (prompt, cached, completion) call, as per-step metrics."""
    return [{"step_id": i, "source": "agent", "message": "a",
             "metrics": {"prompt_tokens": p, "cached_tokens": c, "completion_tokens": o}}
            for i, (p, c, o) in enumerate(calls, start=1)]


class CacheAccountingFold(unittest.TestCase):
    """Reads ÷ every input token, whichever convention wrote the file.

    OpenAI-style already counts new tokens inside prompt_tokens; Anthropic-
    style banks them in a cache-write field outside it, so its own ratio is
    ~100% by construction and the writes must be added back in.
    """

    def test_openai_convention_at_face_value(self):
        tr = _load(_metered((18943, 0, 322), (19973, 18940, 474)))
        acct = tr.cache
        self.assertEqual(acct.convention, "openai")
        self.assertEqual(acct.reads, 18940)
        self.assertEqual(acct.new, 18943 + 1033)
        self.assertAlmostEqual(acct.hit, 18940 / 38916)

    def test_anthropic_convention_recovers_writes(self):
        # OpenCode's shape: prompt is reads + a 3-token remainder, every call.
        # The write is the next call's prompt growth; the last one never
        # surfaces and counts as zero rather than a guess.
        tr = _load([
            {"step_id": 1, "source": "agent", "message": "a",
             "metrics": {"prompt_tokens": 3, "completion_tokens": 134}},
            {"step_id": 2, "source": "agent", "message": "b",
             "metrics": {"prompt_tokens": 5936, "cached_tokens": 5933,
                         "completion_tokens": 171}},
            {"step_id": 3, "source": "agent", "message": "c",
             "metrics": {"prompt_tokens": 6245, "cached_tokens": 6242,
                         "completion_tokens": 257}},
        ])
        acct = tr.cache
        self.assertEqual(acct.convention, "anthropic")
        self.assertEqual(acct.reads, 5933 + 6242)
        self.assertEqual(acct.new, 9 + 5933 + 309)  # remainders + two writes
        # The strip draws each call's whole input: prompt plus its write.
        self.assertEqual(tr.token_series[0]["sent"], 3 + 5933)
        self.assertEqual(tr.token_series[2]["sent"], 6245)
        # The file's own ratio reads ~100%; the fold does not.
        self.assertGreater(acct.reported_hit, 0.99)
        self.assertLess(acct.hit, 0.70)
        self.assertTrue(acct.reported_diverges)

    def test_large_remainder_is_not_anthropic(self):
        # One call paying real fresh tokens breaks the every-call signature.
        tr = _load(_metered((100, 97, 5), (300, 100, 5)))
        self.assertEqual(tr.cache.convention, "openai")

    def test_doubled_final_metrics_flagged_as_divergence(self):
        # Totals that double-count the per-call sum.
        tr = _load(_metered((100, 50, 7), (200, 100, 9)), final_metrics={"total_prompt_tokens": 600, "total_cached_tokens": 150})
        self.assertAlmostEqual(tr.cache.hit, 0.5)
        self.assertAlmostEqual(tr.cache.reported_hit, 0.25)
        self.assertTrue(tr.cache.reported_diverges)

    def test_totals_only_file_still_accounts(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}],
                   final_metrics={"total_prompt_tokens": 1000,
                                  "total_cached_tokens": 800})
        self.assertEqual(tr.token_series, [])
        self.assertEqual(tr.cache.convention, "openai")
        self.assertEqual(tr.cache.reads, 800)
        self.assertEqual(tr.cache.new, 200)

    def test_no_usage_anywhere_is_none(self):
        tr = _load([{"step_id": 1, "source": "agent", "message": "a"}])
        self.assertIsNone(tr.cache)


# ---- the tool-call name is not a comparison key across agents ---------------
#
# Two agents, one task, the same sequence of what they did — searched, read,
# edited, ran, planned — under names of their own: OpenCode's short verbs on
# one side, the sentences another agent writes on the other. Every summary the
# pages read must come out equal for the two; only the names may differ.

SAME_WORK = [
    # (category, short name, verbose name, arguments)
    ("search", "grep", "Search the codebase for the failing symbol", {"pattern": "solve"}),
    ("read", "read", "Read the file where the symbol is defined", {"file_path": "/testbed/a.py"}),
    ("read", "read", "Read the file where the symbol is defined", {"file_path": "/testbed/b.py"}),
    ("edit", "edit", "Write the corrected implementation into the file", {"file_path": "/testbed/a.py"}),
    ("execute", "bash", "Run the test suite to confirm the fix", {"command": "pytest -q"}),
    ("plan", "todowrite", "Note the remaining work in the plan", {"todos": ["done"]}),
    ("execute", "bash", "Execute the reproduction script once more", {"command": "python repro.py"}),
]


def two_agent_steps() -> dict:
    """``{"short": steps, "verbose": steps}`` — the same work, differently named."""
    out = {}
    for agent, column in (("short", 1), ("verbose", 2)):
        steps = []
        for i, row in enumerate(SAME_WORK, start=1):
            call_id = f"c{i}"
            steps.append({
                "step_id": i, "source": "agent", "message": "(tool use)",
                "timestamp": f"2026-09-17T10:00:{i:02d}+00:00",
                "tool_calls": [{"tool_call_id": call_id, "function_name": row[column],
                                "arguments": row[3]}],
                "observation": {"results": [{"source_call_id": call_id,
                                             "content": "success=True data={}"}]},
            })
        out[agent] = steps
    return out


class CategoryIsTheKey(unittest.TestCase):
    """The reader keys its summaries by category, never by function_name."""

    def setUp(self):
        steps = two_agent_steps()
        self.short = _load(steps["short"])
        self.verbose = _load(steps["verbose"])

    def test_category_counts_are_equal_where_name_counts_are_not(self):
        self.assertEqual(self.short.category_freq, self.verbose.category_freq)
        self.assertEqual(self.short.category_freq,
                         Counter({"execute": 2, "read": 2, "search": 1, "edit": 1, "plan": 1}))
        self.assertEqual(self.short.n_tool_calls, self.verbose.n_tool_calls)
        # The names are kept as evidence beside the key, and they differ.
        self.assertNotEqual(self.short.name_freq, self.verbose.name_freq)
        self.assertEqual(self.short.names_of("read"), [("read", 2)])
        self.assertEqual(self.verbose.names_of("read"),
                         [("Read the file where the symbol is defined", 2)])
        self.assertEqual(self.short.names_of("nothing"), [])

    def test_names_and_categories_count_the_same_calls(self):
        # Every category's names sum to its chip; every chip sums to the total.
        for tr in (self.short, self.verbose):
            for cat, n in tr.category_freq.items():
                self.assertEqual(sum(k for _, k in tr.names_of(cat)), n)
            self.assertEqual(sum(tr.category_freq.values()), tr.n_tool_calls)
            self.assertEqual(tr.n_tool_calls, len(tr.calls))

    def test_other_is_a_category_with_its_names_too(self):
        tr = _load([{
            "step_id": 1, "source": "agent", "message": "(tool use)",
            "tool_calls": [{"tool_call_id": "w", "function_name": "webfetch",
                            "arguments": {"url": "https://example.test"}}],
        }])
        self.assertEqual(tr.category_freq, Counter({"other": 1}))
        self.assertEqual(tr.names_of("other"), [("webfetch", 1)])

    def test_placeholder_entries_count_nowhere(self):
        # An agent's "Using tool" placeholders are dropped from the call
        # list; the chips and the total must not count what the list dropped.
        tr = _load([{
            "step_id": 1, "source": "agent", "message": "(tool use)",
            "tool_calls": [
                {"tool_call_id": "p", "function_name": "Using tool"},
                {"tool_call_id": "r", "function_name": "Read the file",
                 "arguments": {"file_path": "x.py"}},
            ],
            "observation": {"results": [{"source_call_id": "r", "content": "success=True data={}"}]},
        }])
        self.assertEqual(len(tr.calls), 1)
        self.assertEqual(tr.n_tool_calls, 1)
        self.assertEqual(tr.category_freq, Counter({"read": 1}))
        self.assertEqual(tr.names_of("read"), [("Read the file", 1)])
        self.assertEqual(tr.names_of("other"), [])


class McpTools(unittest.TestCase):
    """A benchmark's tools, in any agent's spelling: one tool, categorised by what its name says."""

    def test_every_spelling_reads_back_the_same_tool(self):
        for name, servers in (("mcp.kit.get_code", ()), ("mcp__kit__get_code", ()), ("Tool: kit/get_code", ()), ("kit__get_code", ()),
                              ("kit: get code", ("kit",)), ("kit_get_code", ("kit",))):
            self.assertEqual(trajectory.mcp_tool(name, servers), ("kit", "get_code"), name)
            self.assertEqual(trajectory.categorize(name, servers), "lookup", name)

    def test_a_spelling_that_could_be_anything_needs_the_runs_servers(self):
        for name in ("Edit: calc.py", "read_file", "bash"):
            self.assertIsNone(trajectory.mcp_tool(name, ("kit",)), name)
        self.assertEqual(trajectory.categorize("kit_get_code"), "other")

    def test_the_first_word_says_what_it_does_and_an_unclear_one_stays_other(self):
        for tool, category in (("get_flight_status", "lookup"), ("list_all_airports", "lookup"), ("book_reservation", "change"),
                               ("cancel_reservation", "change"), ("transfer_to_human_agents", "other"), ("calculate", "other")):
            self.assertEqual(trajectory.categorize(f"mcp.x.{tool}"), category, tool)

    def test_an_mcp_tool_over_files_or_a_shell_keeps_its_coding_category(self):
        self.assertEqual(trajectory.categorize("mcp.fs.write_file"), "edit")
        self.assertEqual(trajectory.categorize("mcp.fs.read_file"), "read")
        self.assertEqual(trajectory.categorize("mcp.sh.run_shell"), "execute")

    def test_an_agents_own_tools_read_as_before(self):
        for name, category in (("Bash", "execute"), ("todowrite", "plan"), ("Grep", "search"), ("Edit", "edit"), ("Read", "read")):
            self.assertEqual(trajectory.categorize(name), category)

    def test_tools_folds_each_tool_once_with_its_failures_and_time(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=os.environ.get("TMPDIR"), delete=False) as f:
            self.addCleanup(os.unlink, f.name)
            json.dump({"extra": {"mcp_servers": ["kit"]}, "steps": [
                {"step_id": 1, "timestamp": "2026-01-01T00:00:00+00:00", "tool_calls": [{"tool_call_id": "a", "function_name": "kit_get_code", "arguments": {"x": 1}}],
                 "extra": {"running_at": "2026-01-01T00:00:01+00:00", "ended_at": "2026-01-01T00:00:03+00:00"}},
                {"step_id": 2, "timestamp": "2026-01-01T00:00:04+00:00", "tool_calls": [{"tool_call_id": "b", "function_name": "kit: get code", "arguments": {"x": 2}}],
                 "observation": {"results": [{"source_call_id": "b", "content": "Error: no"}]}, "extra": {"ended_at": "2026-01-01T00:00:05+00:00"}},
                {"step_id": 3, "timestamp": "2026-01-01T00:00:06+00:00", "tool_calls": [{"tool_call_id": "c", "function_name": "Bash", "arguments": {"c": 1}}]},
            ]}, f)
        tools = trajectory.load(f.name).tools
        self.assertEqual([(t["server"], t["name"], t["category"], t["calls"], t["ns"]) for t in tools],
                         [("kit", "get_code", "lookup", 2, 3 * 10**9), ("", "Bash", "execute", 1, None)])
        self.assertEqual(tools[0]["spellings"], ["kit_get_code", "kit: get code"])
        self.assertEqual(tools[0]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
