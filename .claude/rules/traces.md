## Traces, as this application renders them

The reader itself — folding, grouping, normalisation — is
`app/nullius/traces/trace.py`.

Four things about a rendered trace are load-bearing:

- **`totals` is displayed verbatim, never recomputed.** Spans nest, so summing
  them double-counts. The producer already resolved that.
- **One global axis.** Bars are placed against the whole trace, not their
  parent. Log-derived children really do start before their OTel parents, and
  a per-parent layout would have to clamp that away.
- **The axis is the instrumented window, not the run.** It is derived from the
  spans, so it fills the width no matter how little was captured — which is why
  there is a coverage strip above it.
- **`totals.wall_ns` is the agent's own window, not the trial total.** The trial
  also carries image pull, setup and the verifier; the phase bar covers those.

### Filtering a trace, and saving the view

The trial page can hide a label's spans or one call, each with its subtree. The filter lives in the query string
(`?hide=<label>&hide_span=<span id>`), which is what keeps a single definition of
"which spans are in this view" — the page renders from it and the save writes
from it, so a saved file cannot drift from what was on screen.

Four rules:

- **The axis does not move.** It comes from the *unfiltered* spans, so hiding one
  leaves a gap instead of re-scaling the survivors — otherwise a filter looks
  like it changed the timings it was only asked to leave out. The coverage strip
  is unchanged too: it describes the capture, not the view.
- **`totals` is still displayed verbatim.** The recomputed numbers are a second
  strip that appears only while a filter is active — `Breakdown`, never
  `Totals`, and never written over it.
- **The two strips disagree for two reasons, so only one of them is shown as a
  Δ.** `Breakdown` partitions the timeline (every instant charged once, priority
  idle > tool > reasoning > llm) where Harbor sums its LLM spans. The
  producer may also know a gap was a wait for a prompt, which this partition cannot infer. Each segment
  therefore carries a Δ against the same partition over the whole tree — the Δ
  is the filter's doing, the absolute number is not.
- **Hidden time becomes `Unaccounted`, not a shorter run.** `wall_ns` is carried
  over: hiding a span does not make the run faster. So the deltas sum to zero,
  and time moves *between* categories as well — hide a tool and the call it ran
  inside reclaims the instants the two overlapped.

**Zooming is not filtering.** `?zoom=<span id>` re-bases the waterfall on one
subtree's own extent (`extent_of`, never clamped) and draws that subtree alone; spans outside it are left out, never
re-scaled in. It rides on `trace.Filter` so every filter link carries it, but it
hides nothing: the coverage strip, `totals`, the breakdown and the charts are
exactly what the un-zoomed view shows, and a saved view is the filtered tree
whole.
Offsets in a zoomed view count from the zoomed span's start; the chip says where
that is in the capture. A zoom on a span not in the view — unknown, or hidden by
the filter — is the whole trace with a note.

Saving writes a new `harbor-trace-v1` document into the trial's own `agent/`
directory, under a name typed into the form. It is the only write the app makes
into a *source* tree and it refuses to overwrite
anything at all — `trace.json` above all, since a capture cannot be taken a
second time. The file carries `totals` recomputed over
what is left, the producer's own numbers under `filtered.source_totals`, and
`producer` reset to this tree so nothing claims authorship of numbers it never
saw. Spans keep their content unredacted, so the file is exactly as sensitive as
its source. Read one back with `?trace=<filename>` on the trial page; the name
is checked against `parse.TRACE_NAME_RE` before it is read or written.
