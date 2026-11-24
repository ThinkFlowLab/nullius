"""Runs against runs: whole runs, matched tasks, one task side by side."""
from __future__ import annotations

import re
from dataclasses import replace
from functools import partial
from itertools import zip_longest
from flask import abort, flash, g, has_request_context, jsonify, redirect, render_template, request, url_for
from nullius.traces import behavior, charts, trace, trajectory
from nullius.runs import classify, failure_analysis
from nullius.runs import verifier
from nullius import charts as figures
from nullius import workspace

from .core import app
from . import shared
from . import filters


def _dash(value, suffix=""):
    return "—" if value in (None, "") else f"{value}{suffix}"


# Every cost this application shows is priced by the agent from its price
# table over the usage it recorded — the ATIF RFC formula, never a provider's
# invoice. The row says so in its
# name, and a run whose agent priced nothing shows a dash, not a free run.
COST_LABEL = "Cost (estimated)"
COST_HINT = ("An estimate: priced by the agent from its price table over the recorded usage, "
             "not a figure from the provider. — where the agent recorded none")


def _trial_stats(t):
    """Trajectory-derived numbers for one trial (empty dict when unavailable)."""
    if t is None or not t.has_trajectory:
        return {}
    tr = trajectory.load(t.trajectory_path)
    if tr.error:
        return {}
    return {
        "steps": tr.n_steps,
        "calls": len(tr.calls),
        "failed": tr.n_failed_calls,
        "cats": dict(tr.category_freq),
        "files": tr.files_touched,
        "beh": behavior.analyze(tr),
    }


def rank_cells(raw, direction, allow_single=False, cells=None, exclude=None):
    """Per-cell classes: 'best' for the winner(s), 'tie' when all sides match.

    *direction* is 'lower', 'higher' or None (no meaningful winner). Cells with
    no value are never marked — a missing measurement isn't a loss. Normally a
    metric needs two values to have a winner; *allow_single* marks the only
    reported value anyway (used for cost).
    A single column has nothing to win against.

    Ranking uses the raw numbers, but *cells* (the rendered text) decides
    equality: two runs showing "$0.01" or "100%" must both win, even though
    their underlying floats differ in a digit the reader can't see.

    *exclude* marks columns that don't compete — Harbor's oracle/nop controls.
    They are never marked, and they don't count toward "needs two values to
    have a winner": a control at $0.00 and ~0s would take every cost and speed
    row, which says nothing about the agents being compared.
    """
    n = len(raw)
    exclude = list(exclude) if exclude else [False] * n
    entrants = [i for i in range(n) if not exclude[i]]
    reported = [i for i in entrants if raw[i] is not None]
    values = [raw[i] for i in reported]
    if not direction or n < 2 or len(values) < (1 if allow_single else 2):
        return [""] * n

    shown = list(cells) if cells else [None] * n

    # Everyone displaying the same thing is a draw — but only if every entrant
    # reported. If some are missing the measurement, those that have it are
    # joint winners rather than a no-contest draw.
    all_reported = len(reported) == len(entrants)
    if cells and all_reported and len(values) > 1 and len({shown[i] for i in reported}) == 1:
        return ["tie" if not exclude[i] else "" for i in range(n)]

    best = min(values) if direction == "lower" else max(values)
    if not cells and len(values) > 1 and best == (max(values) if direction == "lower" else min(values)):
        return ["tie" if i in reported else "" for i in range(n)]

    winners = {i for i in reported if raw[i] == best}
    # Anything rendering identically to a winner wins too.
    if cells:
        texts = {shown[i] for i in winners}
        winners |= {i for i in reported if shown[i] in texts}
    return ["best" if i in winners else "" for i in range(n)]


def _row(label, cells, raw=None, direction=None, hint="", allow_single=False, exclude=None):
    raw = raw if raw is not None else [None] * len(cells)
    return {
        "label": label, "hint": hint, "cells": cells, "raw": raw,
        "classes": rank_cells(raw, direction, allow_single, cells, exclude),
    }


def _ratio(part, whole):
    return (part / whole) if (part and whole) else None


# A difference's unit, as each row reads it.
COUNT, SCORE, MINUTES = str, "{:.2f}".format, "{:.0f}m".format
POINTS = lambda d: f"{d * 100:.0f} pp"


def deltas(values, unit, direction, controls):
    """Each column's difference from the first, in the row's ``unit``: (text, 'ok', 'bad' or ''), coloured by the
    row's ``direction``; nothing for the first column or where either value is missing, uncoloured for a check."""
    base = values[0] if values else None
    out = [("", "")]
    for v, control in zip(values[1:], controls[1:]):
        if v is None or base is None:
            out.append(("", ""))
            continue
        d = round(v - base, 9)          # a float's last digits are no difference ($0.03 - $0.02)
        tone = "" if not d or not direction or control or controls[0] else "ok" if (d > 0) == (direction == "higher") else "bad"
        out.append(("±0" if not d else ("+" if d > 0 else "−") + unit(abs(d)), tone))
    return out


def summary_rows(jobs, baseline=True):
    """The numeric half of the side-by-side table, with winners marked and,
    under ``baseline``, each column's difference from the first.

    Control runs (oracle/nop) are excluded from every ranking, and their token
    and cost cells render a real 0 rather than the "—" used for a missing
    measurement.
    """
    controls = [j.is_control for j in jobs]
    kinds = [j.kind for j in jobs]

    def row(key, label, cells, raw=None, direction=None, hint="", allow_single=False, unit=None, values=None):
        r = _row(label, cells, raw, direction, hint, allow_single, exclude=controls)
        given = values if values is not None else r["raw"]
        r["deltas"] = deltas(given, unit, direction, controls) if unit and baseline else [("", "")] * len(cells)
        r.update(key=key, values=given, unit=unit)
        return r

    def group(name, rows):
        for r in rows:
            r["group"] = name
        return rows

    def tok(values):
        return [filters._tokens(v, k) for v, k in zip(values, kinds)]

    # A benchmark that scores (an F1, partial credit) is compared on its mean
    # score - over every part when a trial is scored on its parts - and a run
    # with a score and no pass or fail has nothing solved to count.
    scored = [
        row("mean_score", "Mean score", ["—" if j.mean_score is None else f"{j.mean_score:.2f}" for j in jobs],
            [j.mean_score for j in jobs], "higher",
            "The mean of the benchmark's own score, over every part scored when a trial is scored on its parts", unit=SCORE),
    ] if any(j.mean_score is not None for j in jobs) else []
    # and by the field the benchmark breaks it down by (a question's category), a row a value
    by = next((j.parts_by for j in jobs if j.parts_by), "")
    groups = {}
    for j in jobs:
        for g in j.score_by_group:
            groups.setdefault(g["group"], None)
    for g in groups:
        means = [next((x["mean"] for x in j.score_by_group if x["group"] == g), None) for j in jobs]
        scored.append(row("score_" + re.sub(r"\W+", "_", str(g).lower()), f"{by.capitalize()} {g}", ["—" if m is None else f"{m:.2f}" for m in means], means, "higher",
                          f"The mean score of the parts with {by} {g}", unit=SCORE))
    cached = [_ratio(j.total_cache_tokens, j.total_input_tokens) for j in jobs]
    return [
        *group("Outcomes", [row("trials", "Trials", [len(j.trials) for j in jobs], unit=COUNT, values=[len(j.trials) for j in jobs])]),
        *group("Score", scored),
        *group("Outcomes", [
            row("solved", "Solved", ["—" if j.scored_only else f"{j.n_resolved}/{len(j.trials)}" for j in jobs],
                [None if j.scored_only else j.n_resolved for j in jobs], "higher", unit=COUNT),
            row("raw_rate", "Raw rate", [filters._pct(j.raw_solve_rate) for j in jobs],
                [j.raw_solve_rate for j in jobs], "higher", "Solved ÷ all trials", unit=POINTS),
            row("adjusted_rate", "Adjusted rate", [filters._pct(j.adjusted_solve_rate) for j in jobs],
                [j.adjusted_solve_rate for j in jobs], "higher",
                "Solved ÷ trials that actually ran", unit=POINTS),
            row("infra_errors", "Infra errors", [j.n_infra or "—" for j in jobs],
                [j.n_infra for j in jobs], "lower",
                "Trials that never got a fair attempt", unit=COUNT),
            row("broke_tests", "Broke tests", [j.n_regressions or "—" for j in jobs],
                [j.n_regressions for j in jobs], "lower",
                "Patches that broke previously-passing tests", unit=COUNT),
        ]),
        *group("Tokens", [
            # Token block, ordered so the arithmetic reads down the column:
            # prompt size = fresh + cached, then the ratio those two imply.
            row("prompt_size", "Prompt size", tok([j.total_input_tokens for j in jobs]),
                [j.total_input_tokens or None for j in jobs], "lower",
                "Total prompt tokens sent, cached ones included", unit=filters._tokens),
            row("fresh_prompt", "Fresh prompt",
                tok([j.total_input_tokens - j.total_cache_tokens for j in jobs]),
                [(j.total_input_tokens - j.total_cache_tokens) or None for j in jobs], "lower",
                "Prompt tokens NOT served from cache — the expensive part", unit=filters._tokens),
            # Ranked on the ratio, not the absolute: a bigger cached count only
            # means a bigger prompt. The share of the prompt reused is the claim.
            row("cached", "Cached", tok([j.total_cache_tokens for j in jobs]),
                cached, "higher",
                "Prompt tokens served from cache — ranked by share of the prompt, not the raw count",
                unit=filters._tokens, values=[j.total_cache_tokens or None for j in jobs]),
            row("cached_ratio", "Cached ratio", [filters._pct(c) for c in cached], cached, "higher",
                "Cached ÷ prompt size — higher means less of the prompt was re-sent", unit=POINTS),
            row("output_tokens", "Output tokens", tok([j.total_output_tokens for j in jobs]),
                [j.total_output_tokens or None for j in jobs], "lower", unit=filters._tokens),
        ]),
        *group("Cost and time", [
            row("cost", COST_LABEL, [filters._usd(j.total_cost_usd, k) for j, k in zip(jobs, kinds)],
                [j.total_cost_usd for j in jobs], "lower", COST_HINT, allow_single=True, unit=filters._usd),
            row("wall_time", "Wall time",
                [f"{j.duration_min:.0f}m" if j.duration_min else "—" for j in jobs],
                [j.duration_min for j in jobs], "lower", unit=MINUTES),
        ]),
    ]


def compare_detail(row, jobs):
    """Line up the same measurements across each run's trial for one task.

    Returns {"rows": [{label, hint, cells, classes}], "files": [...],
    "broke": [...]}, with "—" wherever a run didn't cover the task. *jobs* is
    the matching run per column, needed to tell a control (real zeros, never
    ranked) from an agent that simply failed to report.
    """
    stats = [_trial_stats(t) for t in row]
    controls = [j.is_control for j in jobs]
    kinds = [j.kind for j in jobs]

    field = partial(_row, exclude=controls)

    def fmt_time(t):
        return "—" if t is None or t.dur_agent is None else filters._secs(t.dur_agent)

    def fmt_phase(t):
        return "—" if t is None else f"{filters._secs(t.dur_env)} / {filters._secs(t.dur_verifier)}"

    def fmt_tests(t, attr_pass, attr_fail):
        if t is None:
            return "—"
        good, bad = getattr(t, attr_pass), getattr(t, attr_fail)
        return "—" if not (good or bad) else f"{good} passed, {bad} failed"

    def fmt_cats(s):
        if not s.get("cats"):
            return "—"
        order = ("lookup", "change", "search", "read", "edit", "execute", "other")
        parts = [f"{k} {s['cats'][k]}" for k in order if s["cats"].get(k)]
        return " · ".join(parts)

    def fmt_first_edit(s):
        b = s.get("beh")
        if not b or b.first_edit_call is None:
            return "—"
        out = f"call {b.first_edit_call}"
        if b.time_to_first_edit is not None:
            out += f" · {filters._secs(b.time_to_first_edit)}"
        return out

    def phase_total(t):
        return None if t is None else (t.dur_env or 0) + (t.dur_verifier or 0)

    def order_rank(t):
        """Position in classify.ORDER — lower is a better outcome."""
        return None if t is None else classify.ORDER.index(t.outcome)

    # One task, so one dataset: the first present trial's vocabulary names the
    # check rows, and a category the dataset doesn't have emits no row at all.
    vocab = next((t.vocab for t in row if t), verifier.GENERIC_VOCABULARY)

    check_rows = []
    if vocab.gained:
        check_rows.append(
            field(vocab.gained, [fmt_tests(t, "f2p_pass", "f2p_fail") for t in row],
                  [t.f2p_fail if t else None for t in row], "lower",
                  "Checks the attempt had to make pass — fewer failures is better"))
    if vocab.held:
        check_rows.append(
            field(vocab.held, [fmt_tests(t, "p2p_pass", "p2p_fail") for t in row],
                  [t.p2p_fail if t else None for t in row], "lower",
                  "Tests that had to keep passing — failures here are regressions"))

    rows = [
        field("Outcome", [t.outcome_label if t else "—" for t in row],
             [order_rank(t) for t in row], "lower"),
        *([field("Score", ["—" if t is None or t.score is None else f"{t.score:.2f}" for t in row],
                 [None if t is None else t.score for t in row], "higher",
                 "The benchmark's own score for this task" + (", the mean of its parts" if any(t and t.n_parts for t in row) else ""))]
          if any(t and t.score is not None for t in row) else []),
        *check_rows,
        field("Agent time", [fmt_time(t) for t in row],
             [t.dur_agent if t else None for t in row], "lower",
             "Time the agent spent working"),
        field("Env / Verify", [fmt_phase(t) for t in row],
             [phase_total(t) for t in row], "lower",
             "Container build / verifier run"),
        field("Steps", [_dash(s.get("steps")) for s in stats],
             hint="Trajectory steps — not ranked, fewer isn't inherently better"),
        field("Tool calls", [_dash(s.get("calls")) for s in stats],
             hint="Real calls; writer placeholders excluded — not ranked"),
        field("Failed calls", [_dash(s.get("failed")) for s in stats],
             [s.get("failed") for s in stats], "lower",
             "Tool calls that returned an error"),
        field("Tool mix", [fmt_cats(s) for s in stats],
             hint="lookup / change / search / read / edit / execute"),
        field("First edit", [fmt_first_edit(s) for s in stats],
             hint="Which tool call first changed a file, and how far into the run"),
        field("Edit → test cycles", [_dash(s["beh"].edit_test_cycles) if s.get("beh") else "—" for s in stats],
             hint="Times an edit was followed by running something — the iterate loop"),
        field("Redundant reads", [_dash(s["beh"].n_redundant_reads) if s.get("beh") else "—" for s in stats],
             [s["beh"].n_redundant_reads if s.get("beh") else None for s in stats], "lower",
             "Re-fetches of a target already read or searched — paid for twice"),
        field("Retried failed calls", [_dash(s["beh"].n_repeated_failed_calls) if s.get("beh") else "—" for s in stats],
             [s["beh"].n_repeated_failed_calls if s.get("beh") else None for s in stats], "lower",
             "Calls repeating something that had already failed — flailing"),
        field("Exploration per edit", [_dash(s["beh"].exploration_per_edit) if s.get("beh") else "—" for s in stats],
             hint="(search + read) ÷ edits — not ranked, more isn't inherently worse"),
        field("Prompt size", [filters._tokens(t.n_input_tokens, k) if t else "—" for t, k in zip(row, kinds)],
             [(t.n_input_tokens or None) if t else None for t in row], "lower",
             "Total prompt tokens sent, cached ones included"),
        field("Fresh prompt",
             [filters._tokens(t.n_input_tokens - t.n_cache_tokens, k) if t else "—" for t, k in zip(row, kinds)],
             [((t.n_input_tokens - t.n_cache_tokens) or None) if t else None for t in row],
             "lower", "Prompt tokens NOT served from cache — the expensive part"),
        field("Cached", [filters._tokens(t.n_cache_tokens, k) if t else "—" for t, k in zip(row, kinds)],
             [_ratio(t.n_cache_tokens, t.n_input_tokens) if t else None for t in row], "higher",
             "Prompt tokens served from cache — ranked by share of the prompt, not the raw count"),
        field("Cached ratio",
             [filters._pct(_ratio(t.n_cache_tokens, t.n_input_tokens)) if t else "—" for t in row],
             [_ratio(t.n_cache_tokens, t.n_input_tokens) if t else None for t in row], "higher",
             "Cached ÷ prompt size — higher means less of the prompt was re-sent"),
        field("Output tokens", [filters._tokens(t.n_output_tokens, k) if t else "—" for t, k in zip(row, kinds)],
             [(t.n_output_tokens or None) if t else None for t in row], "lower",
             "Completion tokens"),
        field(COST_LABEL, [filters._usd(t.cost_usd, k) if t else "—" for t, k in zip(row, kinds)],
             [t.cost_usd if t else None for t in row], "lower", COST_HINT, allow_single=True),
    ]
    return {
        "rows": rows,
        "files": [s.get("files") or [] for s in stats],
        "broke": [(t.p2p_failures if t else []) for t in row],
    }


def _axis(*units, fewest=False):
    """A row's tick labels and, with ``fewest``, its unit (``Metric.size``), each unit ``(size, form, least)``: the first
    unit whose size the axis's largest tick reaches writes every tick, at the decimals its step needs in that unit,
    ``least`` at fewest, and the origin with none."""
    unit = lambda top: next((u for u in units if top >= u[0]), units[-1])

    def fmt(v, step, top):
        size, form, least = unit(top)
        return form.format(f"{v / size:.{0 if v == 0 else figures.places(step / size, least)}f}")
    return fmt, (lambda top: unit(top)[0]) if fewest else None


# A row's axis, by its difference's unit, as its cells read: a rate's ticks are rates, a short run's in seconds.
AXIS = {COUNT: _axis((1, "{}", 0)), SCORE: _axis((1, "{}", 2)), POINTS: _axis((0.01, "{}%", 0), fewest=True),
        MINUTES: _axis((1, "{}m", 0), (1 / 60, "{}s", 0)), filters._usd: _axis((1, "${}", 2)),
        filters._tokens: _axis((1e6, "{}M", 0), (1e3, "{}k", 0), (1, "{}", 0), fewest=True)}
BOUNDS = {POINTS: (0, 1)}


def chart(rows, columns, jobs, style, keys):
    """One chart: the summary rows ``keys`` drawn in ``style``, a run per column that has one, as the table shows it,
    its colour its column's place; None while a key names no row of this comparison."""
    if not all(k in rows for k in keys):
        return None
    names = [" · ".join(filter(None, (j.agent_label or j.job_id, j.model_name))) for j in jobs]

    def value(row, n, i):
        v, check = row["values"][i], jobs[i].is_control
        # a check's figure with no number is the table's real zero: the tokens and cost a control never spent
        if v is None and check and row["cells"][i] != "—":
            v = 0
        return figures.Value(label=f"{n}. {names[i]}" if names.count(names[i]) > 1 else names[i], value=v,
                             text=str(row["cells"][i]), note=row["deltas"][i][0], check=check, baseline=n == 1 and i == 0,
                             tag=str(n), series=n)

    def metric(row):
        fmt, size = AXIS.get(row["unit"], (figures.number, None))
        return figures.Metric(row["label"], tuple(value(row, n, c["i"]) for n, c in enumerate(columns, 1) if c["i"] is not None),
                              fmt, BOUNDS.get(row["unit"], (None, None)), size)

    return figures.STYLES[style].draw(*(metric(rows[k]) for k in keys))


def _charts_requested():
    """This URL's charts (``chart=<style>:<metric key>[,<metric key>]``, in order) as (style, keys), each with as many
    keys as its style's axes; None without ``chart=``, and none for ``chart=`` alone."""
    if "chart" not in request.args:
        return None
    out = []
    for entry in request.args.getlist("chart"):
        style, _, keys = entry.partition(":")
        if style in figures.STYLES:
            n = len(figures.STYLES[style].axes)
            out.append((style, tuple((keys.split(",") + [""] * n)[:n])))
    return out


def chart_query(charts):
    """``charts`` as ``chart=`` values; one empty value for none, so no chart stays none."""
    return [f"{style}:{','.join(keys)}" for style, keys in charts] or [""]


def _restyled(chart, style, headline):
    """``chart`` drawn in ``style``: its first metric kept, the next from the headline rate or the cost."""
    first = chart[1][0]
    keys = (first, headline if first != headline else "cost")
    return style, keys[:len(figures.STYLES[style].axes)]


@app.route("/compare")
def compare():
    """Ordered run columns over whole runs or exact shared tasks, the first the baseline; an empty column
    (``pick=<workspace>:``) waits for its run."""
    view, columns = _columns_requested()
    chosen = view.jobs
    by_agent: dict = {}
    for selection in view.selected:
        by_agent.setdefault(selection.job.agent_label or "—", []).append(selection.value)
    grouped = [ref for group in by_agent.values() for ref in group]
    alternating = [
        ref
        for tier in zip_longest(*by_agent.values())
        for ref in tier
        if ref is not None
    ]
    tasks_view = _view()
    summary = summary_rows(chosen, baseline=columns[0]["i"] == 0) if tasks_view != "tasks" and (view.cohort == "whole" or view.shared_tasks) else []
    # There is no truthful wall-clock interval for a subset of parallel trials.
    # Whole-run intervals are ranked, labelled as such.
    if view.cohort == "matched":
        summary = [row for row in summary if row["label"] != "Wall time"]
    else:
        for row in summary:
            if row["label"] == "Wall time":
                row.update(label="Run wall time", hint="The recorded wall-clock interval of the full run")
    # the charts, kept by every link once chosen; until then the headline rate, the cost, and the one against the other
    rows = {r["key"]: r for r in summary}
    scored = "mean_score" in rows and all(v is None for v in rows.get("adjusted_rate", {}).get("values", [None]))
    headline = "mean_score" if scored else "adjusted_rate"
    charts = _charts_requested()
    keep = {} if charts is None else {"chart": chart_query(charts)}
    if charts is None:
        charts = [("bars", (headline,)), ("bars", ("cost",)), ("scatter", ("cost", headline))]
    values = [c["ref"].value for c in columns]
    at = lambda cs: url_for("compare", pick=values, cohort=view.cohort, view="chart", chart=chart_query(cs))
    def card(k, style, keys):
        """A chart's card: its panel, and its links, each changing this chart alone."""
        put = lambda chart: at(charts[:k] + [chart] + charts[k + 1:])

        def axis(a, name):
            """The metric menu of the axis ``a``: a link a summary row, by group."""
            pick = lambda key: put((style, keys[:a] + (key,) + keys[a + 1:]))
            return {"name": name, "key": keys[a], "label": rows[keys[a]]["label"] if keys[a] in rows else "—",
                    "groups": {g: [(key, label, pick(key)) for key, label in rs] for g, rs in groups.items()}}

        return {"style": style, "axes": [axis(a, name) for a, name in enumerate(figures.STYLES[style].axes)],
                "panel": chart(rows, columns, chosen, style, keys) if tasks_view == "chart" else None,
                "styles": [(s, st.label, put(_restyled((style, keys), s, headline))) for s, st in figures.STYLES.items()],
                "remove": at(charts[:k] + charts[k + 1:])}

    groups = _metric_groups(summary)
    cards = [card(k, style, keys) for k, (style, keys) in enumerate(charts)]
    to = lambda vs: url_for("compare", pick=vs, cohort=view.cohort, view=tasks_view, **keep)

    def swap(a, b):
        vs = list(values)
        vs[a], vs[b] = vs[b], vs[a]
        return to(vs)

    for k, c in enumerate(columns):
        # an empty column of two would come straight back
        c.update(remove=(len(values) > 2 or c["ref"].job_id) and to(values[:k] + values[k + 1:]), left=k and swap(k, k - 1), right=k + 1 < len(values) and swap(k, k + 1))
    baseline = chosen[0].dataset if columns[0]["i"] == 0 else ""
    return render_template(
        "compare/compare.html", comparison=view, selections=view.selected, jobs=chosen,
        tasks=view.tasks, summary=summary, columns=columns, workspace_options=_workspace_options(),
        n_differing=view.n_differing, n_shared=view.shared_tasks,
        picks=values, cohort=view.cohort, view=tasks_view, add=to(values + [columns[-1]["ref"].workspace + ":"]),
        lists={n: _options(n, baseline) for n in dict.fromkeys(c["ref"].workspace for c in columns)},
        baseline=baseline, benchmarks=[b.title if b else "—" for b in (_benchmark(j.dataset)[0] for j in chosen)],
        order_grouped=grouped, order_alternating=alternating,
        n_agents=len(by_agent), keep=keep,
        judgement=tasks_view == "tasks" and _judgement(view), csrf=shared._csrf_token(),
        cards=cards, add_chart=at(charts + [("bars", (headline,))]), flag_filter=g.flag_filter,
    )


def _metric_groups(summary):
    """A chart's metric menu: the summary rows by their group, in the table's order, as (key, label)."""
    groups = {}
    for r in summary:
        groups.setdefault(r["group"], []).append((r["key"], r["label"]))
    return groups


@app.route("/compare/detail/<int:index>")
def compare_detail_row(index):
    """One task's detail row, built when it is opened: eager rendering analyzes every compared trajectory."""
    view, columns = _columns_requested()
    if not view.selected or not 0 <= index < len(view.tasks):
        abort(404)
    return render_template("compare/_detail.html", jobs=view.jobs, columns=columns,
                           d=compare_detail(view.tasks[index].trials, view.jobs))


def _judgement(view):
    from . import judge
    return judge.compare_summary(view) if view.cohort == "matched" else None


def _runs(name):
    """A workspace's runs as Compare offers them (``runs.offered``), once a request."""
    from . import runs
    memo = g.setdefault("_compare_runs", {})
    if name not in memo:
        active = shared.STATE["workspace"]
        ws = None
        if name and not (active and name == active.name):
            ws = workspace.load(shared.WS_HOME, name)
            if ws is None:
                abort(404, description="That workspace is unavailable.")
        memo[name] = runs.offered(ws)
    return memo[name]


def _benchmark(dataset):
    """A recorded dataset's catalogue benchmark and dataset entry, None for either the catalogue lacks."""
    cat = shared._catalog_cached()
    d = cat and next((d for d in cat.datasets if dataset and dataset in (d.key, d.registry, d.hub)), None)
    return d and cat.benchmark(d.benchmark), d


def _where(dataset):
    """A recorded dataset as the catalogue names it, "Benchmark / Dataset", else its own name."""
    b, d = _benchmark(dataset)
    return (b.title if b.title == d.title else f"{b.title} / {d.title}") if b else shared.dataset_title(dataset) or "—"


def _options(name, dataset=""):
    """A column's run list from the workspace ``name``: newest launch first, ``dataset``'s runs before the rest
    (``same`` of them), a run sharded over several machines once."""
    from nullius.compare import comparison
    runs = [{"value": comparison.RunRef(name, job.job_id).value, "id": job.job_id, "launch": launch.key,
             "agent": job.agent_label or "—", "model": job.model_name or "", "where": _where(job.dataset),
             "dataset": job.dataset, "date": filters._dt(job.started_at), "check": job.is_control,
             "shards": len(job.merged_from) or len(ids)}
            for launch, job, ids, _ in _runs(name)]
    same = [r for r in runs if dataset and r["dataset"] == dataset]
    return {"runs": same + [r for r in runs if not (dataset and r["dataset"] == dataset)], "same": len(same)}


@app.route("/compare/runs")
def compare_runs():
    """A workspace's runs (the open one by default) for a column's run list, as JSON; ``dataset``: the
    baseline's, listed first."""
    active = shared.STATE["workspace"]
    name = request.args.get("workspace", active.name if active else "")
    if name not in {o["name"] for o in _workspace_options()}:
        abort(404, description="That workspace is unavailable.")
    return jsonify(_options(name, request.args.get("dataset", "")))


def _workspace_options():
    """The workspaces a column can pick from: every saved one, the open one, and the active source when it has no workspace."""
    saved = workspace.list_all(shared.WS_HOME)
    active = shared.STATE["workspace"]
    options = [{"name": ws.name, "label": ws.label} for ws in saved]
    if active and active.name not in {option["name"] for option in options}:
        options.insert(0, {"name": active.name, "label": active.label})
    if not options or (not active and shared.STATE["roots"]):
        options.insert(0, {"name": "", "label": "Active source"})
    return options


def _view():
    """This URL's view: "tasks" for the task table, "chart" for the chart, None (no parameter) for the side-by-side
    table."""
    view = request.args.get("view")
    return view if view in ("tasks", "chart") else None


def _comparison_requested():
    return _columns_requested()[0]


def _columns_requested():
    """This URL's comparison and its columns, in order: {"ref", "i" its run's place in the comparison, None
    when empty}. Resolves only the workspaces it names, without switching one. A shard's id stands for its
    whole run, and a run picked twice is one column."""
    from nullius.compare import comparison
    active = shared.STATE["workspace"]
    active_name = active.name if active else ""
    values = request.args.getlist("pick")
    if not values:
        # Qualify Runs-page selections with the active workspace for generated links.
        values = [comparison.RunRef(active_name, job_id).value
                  for job_id in request.args.getlist("job")]
    try:
        refs = [comparison.RunRef.parse(value, empty=True) for value in values]
        if not refs:
            refs = [comparison.RunRef(active_name or _workspace_options()[0]["name"], "")]
        names = list(dict.fromkeys(ref.workspace for ref in refs))
        runs = {name: _runs(name) for name in names}
        whole = {name: {i: job.job_id for _, job, ids, _ in rs for i in ids} for name, rs in runs.items()}
        refs = [replace(ref, job_id=whole[ref.workspace].get(ref.job_id, ref.job_id)) for ref in refs]
        refs = [ref for k, ref in enumerate(refs) if not ref.job_id or ref not in refs[:k]]
        # never one column alone: an empty one waits beside it
        refs += [comparison.RunRef(refs[-1].workspace, "")] * (2 - len(refs))
        labels = {}
        for name in names:
            ws = active if name == active_name else workspace.load(shared.WS_HOME, name)
            labels[name] = ws.label if ws else "Active source"
        selected = comparison.resolve([ref.value for ref in refs if ref.job_id],
                                      {name: [job for _, job, _, _ in rs] for name, rs in runs.items()}, labels)
        g.flag_filter = _flag_filter(selected)
        view = comparison.build(selected, request.args.get("cohort", "whole"), g.flag_filter["set_aside"])
    except ValueError as exc:
        abort(400, description=str(exc))
    place = {s.ref: k for k, s in enumerate(view.selected)}
    shards = {(name, job.job_id): rows for name, rs in runs.items() for _, job, _, rows in rs}
    return view, [{"ref": ref, "i": place.get(ref), "label": labels[ref.workspace],
                   "shards": shards.get((ref.workspace, ref.job_id), [])} for ref in refs]


@app.route("/compare/pick")
def compare_picker():
    """The old picker's links: its picks as columns."""
    return redirect(url_for("compare", pick=request.args.getlist("pick"), cohort=request.args.get("cohort", "whole"), view=_view()))


def trace_panel(t, chart_filter=None):
    """One side's trace on the task page: its charts and strips, or why not.

    Reads the producer's own ``trace.json`` unfiltered — the compare page has no
    ``hide`` control, so there is nothing for a saved view to differ by. The
    charts go through :func:`charts.charts_for`, which is :func:`charts.charts_of`
    over that trace's own samples, so a side here shows the numbers the trial's
    own page shows for it. Each side is loaded on its own and never pooled: two trials never
    shared a clock, so nothing here carries a wall clock across them.

    Returns ``{"trace": Trace | None, "charts": Charts | None, "absent": str}``
    — ``absent`` says, in words, why a side has nothing to draw. Exactly one of
    ``charts`` and ``absent`` is set: a side is never silently empty.
    """
    if t is None:
        return {"trace": None, "charts": None, "absent": "Didn't include this task."}
    if not t.has_trace:
        return {"trace": None, "charts": None,
                "absent": "No trace was captured for this trial — the agent emitted no spans."}
    tr = trace.load(t.trace_path)
    if tr.error:
        return {"trace": tr, "charts": None, "absent": f"Trace unreadable: {tr.error}"}
    if not tr.has_spans:
        return {"trace": tr, "charts": None, "absent": "The trace has no spans to plot."}
    return {"trace": tr, "charts": charts.charts_for(tr.roots, chart_filter), "absent": ""}


@app.route("/compare/task/<path:task>")
def compare_task(task):
    """Full detail of the same last indexed attempt shown in the task table."""
    view, columns = _columns_requested()
    if not view.selected:
        return redirect(url_for("compare", pick=[c["ref"].value for c in columns], cohort=view.cohort, view=_view()))
    dataset = request.args.get("dataset")
    matches = [row for row in view.tasks if row.task == task and (dataset is None or row.dataset == dataset)]
    if len(matches) != 1:
        if len(matches) > 1:
            flash("That task name occurs in different datasets. Choose its dataset row first.", "error")
            return redirect(url_for("compare", pick=view.references, cohort=view.cohort, view=_view()))
        abort(404, description="This task is not in the selected comparison cohort.")
    row = matches[0]
    # One legend state for every side: a kind switched off in a chart is off in
    # that chart on both sides, so the two stay drawn over the same kinds.
    cfilt = charts.ChartFilter.of(request.args.getlist("chart_off"))
    # Each side shows one attempt: the one `attempt=<run>/<trial>` names, else
    # the last indexed, as the task table does.
    asked = dict(value.rsplit("/", 1) for value in request.args.getlist("attempt") if "/" in value)
    sides = []
    for selection, job, t, group in zip(view.selected, view.jobs, row.trials, row.tried):
        # the row's own attempts, so this page and the table count the same ones
        tried = list(group.trials) if group else []
        chosen = next((x for x in tried if x.trial_name == asked.get(selection.value)), t)
        sides.append((selection, job, chosen, group))
    picks = [c["ref"].value for c in columns]
    chosen_query = [f"{sel.value}/{c.trial_name}" for sel, _, c, group in sides if c and group and group.n > 1]
    panels = []
    for selection, job, t, group in sides:
        traj = trajectory.load(t.trajectory_path) if (t and t.has_trajectory) else None
        links = []
        if group and group.n > 1:
            others = [q for q in chosen_query if q.rsplit("/", 1)[0] != selection.value]
            links = [{"trial": x, "current": x.trial_name == t.trial_name,
                      "href": url_for("compare_task", task=task, pick=picks, cohort=view.cohort, view=_view(),
                                      dataset=row.dataset, attempt=others + [f"{selection.value}/{x.trial_name}"],
                                      **cfilt.query)}
                     for x in group.trials]
        panels.append({"job": job, "selection": selection, "trial": t,
                       "tried": group, "attempt_links": links,
                       "traj": traj if traj and not traj.error else None,
                       **trace_panel(t, cfilt)})
    return render_template(
        "compare/compare_task.html", task=task, dataset=row.dataset, panels=panels,
        comparison=view, picks=picks, cohort=view.cohort, view=_view(), chosen=chosen_query,
        chart_filter=cfilt, no_filter=trace.Filter(),
    )


# The pages that read a comparison: their links keep the page's flag filters, as they keep the cohort.
COMPARISON_ENDPOINTS = {"compare", "compare_task", "compare_detail_row", "judge_compare_route", "compare_picker"}


@app.url_defaults
def _keep_flag_filters(endpoint, values):
    # only from a comparison page to a link that names its runs: the sidebar's bare Compare starts afresh
    if (has_request_context() and request.endpoint in COMPARISON_ENDPOINTS and endpoint in COMPARISON_ENDPOINTS
            and "pick" in values):
        for kind in failure_analysis.KINDS:
            if kind not in values and request.args.get(kind) == "1":
                values[kind] = "1"


def _flag_filter(selected) -> dict:
    """The flag filters of a comparison: one list of tasks set aside in every column, the union of the runs'
    accepted flags of the kinds switched on, keyed as the comparison keys a task (`comparison.task_key`). A flag
    counts only on the task version it names, and a task is set aside only when every column holding it ran the
    same version; otherwise it stays, and is named in `differ` with the tasks the runs flag differently.
    `flagged` counts each kind over all runs, for the switches; `unreadable` names runs whose file is broken."""
    from nullius.compare import comparison
    on = [k for k in failure_analysis.KINDS if request.args.get(k) == "1"]
    runs, unreadable = [], []                 # per run: {task key: version}, {task key: kind}
    for selection in selected:
        job = selection.job
        try:
            analysis = failure_analysis.load(job.dir)
        except failure_analysis.FailureAnalysisError:
            analysis = failure_analysis.Analysis()
            unreadable.append(job.job_id)
        versions = failure_analysis.task_versions(job, analysis.flags) if analysis.flags else {}
        tasks = {t.task_base for t in job.trials}
        kinds = {comparison.task_key(selection, task): kind for kind in failure_analysis.KINDS
                 for task in analysis.set_aside([kind], versions) & tasks}
        runs.append((selection, tasks, kinds))
    flagged_keys = set().union(*(kinds for _, _, kinds in runs))
    # the versions every column holding a flagged task ran, read once per run for those tasks
    ran = {}
    for selection, tasks, _ in runs:
        names = {key[1] for key in flagged_keys if key[1] in tasks and comparison.task_key(selection, key[1]) == key}
        for task, version in failure_analysis.task_versions(selection.job, names).items():
            ran.setdefault(comparison.task_key(selection, task), set()).add(version)
    holders = {key: [r for r in runs if key[1] in r[1] and comparison.task_key(r[0], key[1]) == key]
               for key in flagged_keys}
    differ = sorted(key[1] for key in flagged_keys
                    if len(ran.get(key, set())) > 1 or len({kinds.get(key) for _, _, kinds in holders[key]}) > 1)
    set_aside = frozenset(key for key in flagged_keys if len(ran.get(key, set())) <= 1
                          and any(kinds.get(key) in on for _, _, kinds in holders[key]))
    return {"on": on, "flagged": {k: sum(1 for key in flagged_keys if any(kinds.get(key) == k for _, _, kinds in runs))
                                  for k in failure_analysis.KINDS},
            "set_aside": set_aside, "differ": differ, "unreadable": unreadable}
