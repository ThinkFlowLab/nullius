"""Folding the agent log's request spans into the call the exporter measured.

Harbor joins these two views of one API call itself, on the request id the
exporter reports — `harbor.merged_from` in `fixtures/trace_rich.json`. Against a
provider that returns no request id it cannot, and the capture this was written
for (a 6-minute SWE-bench trial, 47 calls) carries the same 47 calls twice: once
as measured `claude_code.llm_request` spans under the interaction, and once as
106 inferred `llm_request` spans under the parallel `session` subtree, which are
the only spans holding the reasoning and output text.

The shape below is that capture's, hand-built rather than trimmed from it so no
job text enters the repo: one reply arrives as several records sharing a
`message.id`, each record repeating the call's usage, some carrying a single
typed block and some (a `tool_use`) carrying none.

Three failures are worth more than the rest, and all three would still look
plausible on screen:

**A fold that moves a number.** The point is to re-parent rows. If the recomputed
breakdown shifts — time or tokens — the fold has started editing the measurement
rather than arranging it. The token case is real and was caught here: the
producer's own `totals.tokens` sums every span it wrote, duplicated usage and
all, so a record that gave up its tokens on the way in made the breakdown
disagree with the reported totals threefold.

**A fold that guesses.** Matching on overlap alone would put one call's blocks
under another whenever two calls overlap. Usage is a hard constraint wherever
both sides report it.

**A view and a saved file that disagree.** `filtered_document` folds too, or
hiding a call would take its blocks with it on screen and leave them behind on
disk.
"""

import unittest

from nullius.traces import trace

from .test_trace import doc, span

# One reply, as the two producers describe it. Times are ns and the log side
# starts a few before the exporter's, exactly as the real clocks do.
CALL = "otel-call-1"
RECORD_THINK = "request:r1"
RECORD_TOOL = "request:r2"
RECORD_TEXT = "request:r3"
BLOCK_THINK = "block:r1:0"
BLOCK_TEXT = "block:r3:0"

USAGE = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 900}


def record(sid, start, end, message="msg-1", block=None, **fields):
    """One `llm_request` span as the log-derived producer writes it.

    Every record of a reply repeats the reply's usage; the ones that carry a
    block carry exactly one, spanning the record exactly.
    """
    children = []
    if block is not None:
        kind, block_id, text = block
        children = [
            span(
                block_id, start, end,
                name=kind, kind=kind, source="log", timing_quality="inferred",
                parent_span_id=sid,
                attributes={"block.index": 0},
                content={kind: text},
            )
        ]
    return span(
        sid, start, end,
        name="llm_request", kind="llm_request", source="log",
        timing_quality="inferred", parent_span_id="session:s",
        attributes={"request_id": sid.split(":")[-1], "message.id": message},
        tokens=dict(USAGE),
        children=children,
        **fields,
    )


def call(sid=CALL, start=1_000, end=9_000, **fields):
    """The exporter's span for the call — measured, and with no request id."""
    return span(
        sid, start, end,
        name="claude_code.llm_request", kind="llm_request",
        attributes={"gen_ai.request.model": "a-model", "stop_reason": "tool_use"},
        tokens=dict(USAGE),
        **fields,
    )


def capture(*records, calls=(), interaction=(1_000, 9_000)):
    """The two subtrees, side by side under one interaction."""
    start, end = interaction
    session = span(
        "session:s", start, end,
        name="session", kind="session", source="log", timing_quality="inferred",
        children=list(records),
    )
    return doc(
        span(
            "interaction", start, end,
            name="claude_code.interaction", kind="session",
            children=[session, *(calls or [call()])],
        ),
        totals={"wall_ns": end - start},
    )


def reply(think=True, text=False, tool=True, message="msg-1"):
    """The records of one reply, in arrival order."""
    out = []
    if think:
        out.append(record(RECORD_THINK, 990, 4_000, message, ("reasoning", BLOCK_THINK, "hm")))
    if tool:
        out.append(record(RECORD_TOOL, 4_000, 5_000, message))
    if text:
        out.append(record(RECORD_TEXT, 5_000, 6_000, message, ("output", BLOCK_TEXT, "done")))
    return out


def folded(*records, **kwargs):
    return trace.fold_llm_requests(capture(*records, **kwargs)["spans"])


def find(spans, span_id):
    return next(s for s in trace.raw_spans_of(spans) if s.get("span_id") == span_id)


def children_of(spans, span_id):
    return [str(c.get("span_id")) for c in find(spans, span_id).get("children") or []]


def labels(tr):
    return [(r.span.span_id, r.span.label) for r in tr.rows]


class TheCallAdoptsItsReply(unittest.TestCase):
    def test_the_records_move_under_the_call_that_produced_them(self):
        out = folded(*reply())
        self.assertEqual(children_of(out, CALL), [BLOCK_THINK, RECORD_TOOL])

    def test_the_log_session_no_longer_holds_them(self):
        self.assertEqual(children_of(folded(*reply()), "session:s"), [])

    def test_children_are_ordered_by_when_they_started(self):
        out = folded(*reply(text=True))
        self.assertEqual(children_of(out, CALL), [BLOCK_THINK, RECORD_TOOL, BLOCK_TEXT])

    def test_a_moved_record_points_at_its_new_parent(self):
        self.assertEqual(find(folded(*reply()), RECORD_TOOL)["parent_span_id"], CALL)

    def test_the_call_says_what_it_was_assembled_from(self):
        out = folded(*reply())
        self.assertEqual(
            find(out, CALL)["attributes"]["app.merged_from"], f"{RECORD_THINK}, {RECORD_TOOL}"
        )
        row = next(r for r in trace.parse({"spans": out}).rows if r.span.span_id == CALL)
        self.assertTrue(row.span.merged)

    def test_records_with_no_call_to_join_are_left_where_they_are(self):
        # A second reply the exporter never reported: the `session` subtree is
        # the only record of it, and dropping it would lose the call entirely.
        out = folded(*reply(), *reply(tool=False, message="msg-2"))
        self.assertEqual(children_of(out, "session:s"), [RECORD_THINK])


class ARecordIsItsBlock(unittest.TestCase):
    """A record with one typed block spanning it exactly is that block."""

    def test_the_block_keeps_its_identity_and_its_text(self):
        block = find(folded(*reply()), BLOCK_THINK)
        self.assertEqual((block["name"], block["kind"]), ("reasoning", "reasoning"))
        self.assertEqual(block["content"], {"reasoning": "hm"})

    def test_the_record_hands_over_its_attributes(self):
        block = find(folded(*reply()), BLOCK_THINK)
        self.assertEqual(block["attributes"]["message.id"], "msg-1")
        self.assertEqual(block["attributes"]["block.index"], 0)
        self.assertEqual(block["attributes"]["app.merged_from"], RECORD_THINK)

    def test_the_record_hands_over_its_tokens(self):
        # Not dropped as a duplicate: the producer's totals counted this copy.
        self.assertEqual(find(folded(*reply()), BLOCK_THINK)["tokens"], USAGE)

    def test_a_record_with_no_block_stays_a_request(self):
        # The `tool_use` block is untyped; what it went on to run is a tool span
        # of its own, already beside the call.
        tool = find(folded(*reply()), RECORD_TOOL)
        self.assertEqual((tool["name"], tool["kind"]), ("llm_request", "llm_request"))

    def test_a_block_shorter_than_its_record_stays_nested(self):
        # Collapsing would either lose the record's clock or stretch the block's.
        short = record(RECORD_THINK, 990, 4_000, block=("reasoning", BLOCK_THINK, "hm"))
        short["children"][0].update(end_ns=2_000, duration_ns=1_010)
        out = folded(short, *reply(think=False))
        self.assertEqual(children_of(out, RECORD_THINK), [BLOCK_THINK])


class TheCallReadsAsWhatItProduced(unittest.TestCase):
    def test_a_call_that_only_reasoned_reads_as_reasoning(self):
        tr = trace.parse({"spans": folded(*reply())})
        self.assertIn((CALL, "reasoning"), labels(tr))

    def test_a_call_that_reasoned_and_answered_stays_a_request(self):
        # Two kinds of block is not one type of call.
        tr = trace.parse({"spans": folded(*reply(text=True))})
        self.assertIn((CALL, "llm_request"), labels(tr))

    def test_the_exporter_name_is_never_shown(self):
        tr = trace.parse(capture(*reply()))
        self.assertNotIn("claude_code.llm_request", [label for _, label in labels(tr)])

    def test_a_call_with_nothing_to_fold_still_reads_as_a_request(self):
        tr = trace.parse(doc(call()))
        self.assertEqual(labels(tr), [(CALL, "llm_request")])

    def test_text_on_the_call_itself_names_it_too(self):
        # `app/session_to_trace.py` keeps the blocks as content rather than as
        # child spans; the label rule has to read both.
        tr = trace.parse(doc(call(content={"output": "done"})))
        self.assertEqual(labels(tr), [(CALL, "output")])

    def test_both_kinds_of_text_leave_it_a_request(self):
        tr = trace.parse(doc(call(content={"reasoning": "hm", "output": "done"})))
        self.assertEqual(labels(tr), [(CALL, "llm_request")])

    def test_the_chip_counts_the_call_with_its_block(self):
        # Same as a merged tool span and its execution span sharing a name.
        facets = {f.name: f.count for f in trace.inventory(folded(*reply()))}
        self.assertEqual(facets["reasoning"], 2)
        self.assertEqual(facets["llm_request"], 1)


class MatchingRefusesToGuess(unittest.TestCase):
    def test_usage_that_disagrees_is_not_a_match(self):
        odd = record(RECORD_THINK, 990, 4_000, block=("reasoning", BLOCK_THINK, "hm"))
        odd["tokens"] = {"input_tokens": 7}
        self.assertEqual(children_of(folded(odd), CALL), [])

    def test_a_record_outside_the_call_is_not_a_match(self):
        away = record(RECORD_THINK, 20_000, 21_000, block=("reasoning", BLOCK_THINK, "hm"))
        self.assertEqual(children_of(folded(away, interaction=(1_000, 21_000)), CALL), [])

    def test_overlapping_calls_keep_their_own_replies(self):
        # Identical usage and overlapping windows: only the arrival order tells
        # them apart, and the nearer start wins.
        second = call("otel-call-2", 4_500, 9_000)
        out = folded(
            *reply(tool=False),
            record(RECORD_TEXT, 4_600, 6_000, "msg-2", ("output", BLOCK_TEXT, "done")),
            calls=(call(end=5_000), second),
        )
        self.assertEqual(children_of(out, CALL), [BLOCK_THINK])
        self.assertEqual(children_of(out, "otel-call-2"), [BLOCK_TEXT])

    def test_one_reply_is_never_folded_into_two_calls(self):
        out = folded(*reply(tool=False), calls=(call(end=5_000), call("otel-call-2", 1_100, 9_000)))
        self.assertEqual(children_of(out, "otel-call-2"), [])

    def test_records_with_no_message_id_fold_one_by_one(self):
        loose = [record(RECORD_THINK, 990, 4_000, block=("reasoning", BLOCK_THINK, "hm")),
                 record(RECORD_TOOL, 4_000, 5_000)]
        for rec in loose:
            rec["attributes"].pop("message.id")
        self.assertEqual(children_of(folded(*loose), CALL), [BLOCK_THINK])


class TheFoldMovesNoNumber(unittest.TestCase):
    """Rows are re-parented. Nothing about the measurement may change."""

    def setUp(self):
        self.source = capture(*reply(text=True))
        self.before = self.source["spans"]
        self.after = trace.fold_llm_requests(self.before)

    def test_the_axis_is_where_it_was(self):
        self.assertEqual(trace.raw_extent(self.after), trace.raw_extent(self.before))

    def test_the_breakdown_charges_the_same_time(self):
        before, after = trace.breakdown(self.before), trace.breakdown(self.after)
        self.assertEqual(
            (before.llm_ns, before.tool_ns, before.reasoning_ns, before.idle_ns),
            (after.llm_ns, after.tool_ns, after.reasoning_ns, after.idle_ns),
        )

    def test_the_breakdown_counts_the_same_tokens(self):
        self.assertEqual(trace.breakdown(self.after).tokens, trace.breakdown(self.before).tokens)

    def test_the_producers_totals_are_untouched(self):
        self.assertEqual(trace.parse(self.source).totals.wall_ns, 8_000)

    def test_every_span_survives_but_the_records_that_became_blocks(self):
        gone = {RECORD_THINK, RECORD_TEXT}
        self.assertEqual(
            {str(s.get("span_id")) for s in trace.raw_spans_of(self.after)},
            {str(s.get("span_id")) for s in trace.raw_spans_of(self.before)} - gone,
        )

    def test_the_source_document_is_not_mutated(self):
        trace.fold_llm_requests(self.before)
        self.assertEqual(children_of(self.before, "session:s"), [RECORD_THINK, RECORD_TOOL, RECORD_TEXT])

    def test_nothing_to_fold_is_passed_back_by_reference(self):
        spans = doc(call())["spans"]
        self.assertIs(trace.fold_llm_requests(spans), spans)

    def test_folding_twice_changes_nothing(self):
        self.assertEqual(trace.fold_llm_requests(self.after), self.after)


class SavingSeesTheSameTree(unittest.TestCase):
    """`filtered_document` folds too — see the module docstring."""

    def test_hiding_a_call_takes_the_blocks_it_adopted(self):
        out = trace.filtered_document(capture(*reply()), trace.Filter.of(span_ids=[CALL]))
        self.assertEqual([str(s.get("span_id")) for s in trace.raw_spans_of(out["spans"])],
                         ["interaction", "session:s"])
        self.assertEqual(out["filtered"]["spans_removed"], 3)

    def test_what_is_left_matches_what_the_page_shows(self):
        source = capture(*reply())
        filt = trace.Filter.of(["reasoning"])
        out = trace.filtered_document(source, filt)
        self.assertEqual(
            [str(s.get("span_id")) for s in trace.raw_spans_of(out["spans"])],
            [r.span.span_id for r in trace.parse(source, filt).rows],
        )


if __name__ == "__main__":
    unittest.main()
