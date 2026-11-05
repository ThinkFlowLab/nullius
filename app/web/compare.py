"""Runs against runs: whole runs, matched tasks, one task side by side."""
from __future__ import annotations

from functools import partial
from itertools import zip_longest
from flask import abort, flash, redirect, render_template, request, url_for
from nullius.traces import behavior, charts, trace, trajectory
from nullius.runs import classify
from nullius.runs import verifier
from nullius import workspace

from .core import app
from . import shared
from . import filters


def _dash(value, suffix=""):
    return "—" if value in (None, "") else f"{value}{suffix}"


# Every cost this application shows is priced by the agent from its price
# table over the usage it recorded — the ATIF RFC formula, never a provider's
# invoice (the field guide: derived, not published). The row says so in its
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
    reported value anyway (used for cost, which not every agent records yet).

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
    if not direction or len(values) < (1 if allow_single else 2):
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
        "label": label, "hint": hint, "cells": cells,
        "classes": rank_cells(raw, direction, allow_single, cells, exclude),
    }


def _ratio(part, whole):
    return (part / whole) if (part and whole) else None


def summary_rows(jobs):
    """The numeric half of the side-by-side table, with winners marked.

    Control runs (oracle/nop) are excluded from every ranking, and their token
    and cost cells render a real 0 rather than the "—" used for a missing
    measurement.
    """
    controls = [j.is_control for j in jobs]
    kinds = [j.kind for j in jobs]

    def row(label, cells, raw=None, direction=None, hint="", allow_single=False):
        return _row(label, cells, raw, direction, hint, allow_single, exclude=controls)

    def tok(values):
        return [filters._tokens(v, k) for v, k in zip(values, kinds)]

    # A benchmark that scores (an F1, partial credit) is compared on its mean
    # score - over every part when a trial is scored on its parts - and a run
    # with a score and no pass or fail has nothing solved to count.
    scored = [
        row("Mean score", ["—" if j.mean_score is None else f"{j.mean_score:.2f}" for j in jobs],
            [j.mean_score for j in jobs], "higher",
            "The mean of the benchmark's own score, over every part scored when a trial is scored on its parts"),
    ] if any(j.mean_score is not None for j in jobs) else []
    # and by the field the benchmark breaks it down by (a question's category), a row a value
    by = next((j.parts_by for j in jobs if j.parts_by), "")
    groups = {}
    for j in jobs:
        for g in j.score_by_group:
            groups.setdefault(g["group"], None)
    for g in groups:
        means = [next((x["mean"] for x in j.score_by_group if x["group"] == g), None) for j in jobs]
        scored.append(row(f"{by.capitalize()} {g}", ["—" if m is None else f"{m:.2f}" for m in means], means, "higher",
                          f"The mean score of the parts with {by} {g}"))
    return [
        row("Trials", [len(j.trials) for j in jobs]),
        *scored,
        row("Solved", ["—" if j.scored_only else f"{j.n_resolved}/{len(j.trials)}" for j in jobs],
            [None if j.scored_only else j.n_resolved for j in jobs], "higher"),
        row("Raw rate", [filters._pct(j.raw_solve_rate) for j in jobs],
            [j.raw_solve_rate for j in jobs], "higher", "Solved ÷ all trials"),
        row("Adjusted rate", [filters._pct(j.adjusted_solve_rate) for j in jobs],
            [j.adjusted_solve_rate for j in jobs], "higher",
            "Solved ÷ trials that actually ran"),
        row("Infra errors", [j.n_infra or "—" for j in jobs],
            [j.n_infra for j in jobs], "lower",
            "Trials that never got a fair attempt"),
        row("Broke tests", [j.n_regressions or "—" for j in jobs],
            [j.n_regressions for j in jobs], "lower",
            "Patches that broke previously-passing tests"),
        # Token block, ordered so the arithmetic reads down the column:
        # prompt size = fresh + cached, then the ratio those two imply.
        row("Prompt size", tok([j.total_input_tokens for j in jobs]),
            [j.total_input_tokens or None for j in jobs], "lower",
            "Total prompt tokens sent, cached ones included"),
        row("Fresh prompt",
            tok([j.total_input_tokens - j.total_cache_tokens for j in jobs]),
            [(j.total_input_tokens - j.total_cache_tokens) or None for j in jobs], "lower",
            "Prompt tokens NOT served from cache — the expensive part"),
        # Ranked on the ratio, not the absolute: a bigger cached count only
        # means a bigger prompt. The share of the prompt reused is the claim.
        row("Cached", tok([j.total_cache_tokens for j in jobs]),
            [_ratio(j.total_cache_tokens, j.total_input_tokens) for j in jobs], "higher",
            "Prompt tokens served from cache — ranked by share of the prompt, not the raw count"),
        row("Cached ratio",
            [filters._pct(_ratio(j.total_cache_tokens, j.total_input_tokens)) for j in jobs],
            [_ratio(j.total_cache_tokens, j.total_input_tokens) for j in jobs], "higher",
            "Cached ÷ prompt size — higher means less of the prompt was re-sent"),
        row("Output tokens", tok([j.total_output_tokens for j in jobs]),
            [j.total_output_tokens or None for j in jobs], "lower"),
        row(COST_LABEL, [filters._usd(j.total_cost_usd, k) for j, k in zip(jobs, kinds)],
            [j.total_cost_usd for j in jobs], "lower", COST_HINT, allow_single=True),
        row("Wall time",
            [f"{j.duration_min:.0f}m" if j.duration_min else "—" for j in jobs],
            [j.duration_min for j in jobs], "lower"),
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

    # Controls never compete, so every row in this block excludes them.
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


@app.route("/compare")
def compare():
    """Qualified, ordered run columns over whole runs or exact shared tasks."""
    view = _comparison_requested()
    if len(view.selected) < 2:
        if view.selected:
            flash("Choose two different runs to compare.", "error")
        return compare_picker()
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
    summary = summary_rows(chosen) if view.cohort == "whole" or view.shared_tasks else []
    # There is no truthful wall-clock interval for a subset of parallel trials.
    # Whole-run intervals keep their existing ranking, explicitly labelled.
    if view.cohort == "matched":
        summary = [row for row in summary if row["label"] != "Wall time"]
    else:
        for row in summary:
            if row["label"] == "Wall time":
                row.update(label="Run wall time", hint="The recorded wall-clock interval of the full run")
    return render_template(
        "compare/compare.html", comparison=view, selections=view.selected, jobs=chosen,
        tasks=view.tasks, summary=summary,
        n_differing=view.n_differing, n_shared=view.shared_tasks,
        picks=view.references, cohort=view.cohort,
        order_grouped=grouped, order_alternating=alternating,
        n_agents=len(by_agent),
        judgement=_judgement(view), csrf=shared._csrf_token(),
    )


@app.route("/compare/detail/<int:index>")
def compare_detail_row(index):
    """One task's detail row, built when it is opened: building every row of
    the page loaded and analysed every trajectory of every run compared
    (2 x 500 trials took 10 to 12 s and 4.8 MB of HTML)."""
    view = _comparison_requested()
    if len(view.selected) < 2 or not 0 <= index < len(view.tasks):
        abort(404)
    return render_template("compare/_detail.html", jobs=view.jobs, d=compare_detail(view.tasks[index].trials, view.jobs))


def _judgement(view):
    from . import judge
    return judge.compare_summary(view) if view.cohort == "matched" else None


def _comparison_requested():
    """Resolve only the workspaces named by this URL, without switching one."""
    from nullius.compare import comparison
    active = shared.STATE["workspace"]
    active_name = active.name if active else ""
    values = request.args.getlist("pick")
    if not values:
        # Qualify Runs-page selections with the active workspace for generated links.
        values = [comparison.RunRef(active_name, job_id).value
                  for job_id in request.args.getlist("job")]
    try:
        refs = [comparison.RunRef.parse(value) for value in values]
        names = list(dict.fromkeys(ref.workspace for ref in refs))
        jobs = {name: shared._jobs_for_workspace(name) for name in names}
        labels = {}
        for name in names:
            ws = active if name == active_name else workspace.load(shared.WS_HOME, name)
            labels[name] = ws.label if ws else "Active source"
        selected = comparison.resolve(values, jobs, labels)
        return comparison.build(selected, request.args.get("cohort", "whole"))
    except ValueError as exc:
        abort(400, description=str(exc))


@app.route("/compare/pick")
def compare_picker():
    """Choose real runs from two saved workspaces; reads never open or refresh."""
    from nullius.compare import comparison
    saved = workspace.list_all(shared.WS_HOME)
    active = shared.STATE["workspace"]
    options = [{"name": ws.name, "label": ws.label} for ws in saved]
    if active and active.name not in {option["name"] for option in options}:
        options.insert(0, {"name": active.name, "label": active.label})
    if not options or (not active and shared.STATE["roots"]):
        options.insert(0, {"name": "", "label": "Active source"})
    values = request.args.getlist("pick")
    try:
        refs = [comparison.RunRef.parse(value) for value in values[:2]]
    except ValueError as exc:
        abort(400, description=str(exc))
    default_left = refs[0].workspace if refs else (active.name if active else options[0]["name"])
    left = request.args.get("left", default_left)
    default_right = refs[1].workspace if len(refs) > 1 else next(
        (option["name"] for option in options if option["name"] != left), left)
    right = request.args.get("right", default_right)
    choices, loaded = [], {}
    for index, name in enumerate((left, right)):
        if name not in {option["name"] for option in options}:
            abort(404, description="That workspace is unavailable.")
        if name not in loaded:
            loaded[name] = shared._jobs_for_workspace(name)
        jobs = loaded[name]
        selected = refs[index].value if len(refs) > index and refs[index].workspace == name else ""
        if not selected and jobs:
            run_index = 1 if index == 1 and left == right and len(jobs) > 1 else 0
            selected = comparison.RunRef(name, jobs[run_index].job_id).value
        choices.append({"name": name, "label": next(option["label"] for option in options if option["name"] == name),
                        "jobs": jobs, "selected": selected})
    cohort = request.args.get("cohort", "whole")
    if cohort not in {"whole", "matched"}:
        abort(400, description="Choose whole runs or matched tasks as the comparison cohort.")
    return render_template("compare/compare_picker.html", workspace_options=options,
                           choices=choices, cohort=cohort)


def trace_panel(t, chart_filter=None):
    """One side's trace on the task page: its charts and strips, or why not.

    Reads the producer's own ``trace.json`` unfiltered — the compare page has no
    ``hide`` control, so there is nothing for a saved view to differ by. The
    charts go through :func:`charts.charts_for`, which is :func:`charts.charts_of`
    over that trace's own samples, so a side here shows the numbers the trial's
    own page shows for it (`test_comparison_routes` pins the two as one
    drawing). Each side is loaded on its own and never pooled: two trials never
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
    view = _comparison_requested()
    if len(view.selected) < 2:
        return redirect(url_for("compare_picker", cohort=view.cohort))
    dataset = request.args.get("dataset")
    matches = [row for row in view.tasks if row.task == task and (dataset is None or row.dataset == dataset)]
    if len(matches) != 1:
        if len(matches) > 1:
            flash("That task name occurs in different datasets. Choose its dataset row first.", "error")
            return redirect(url_for("compare", pick=view.references, cohort=view.cohort))
        abort(404, description="This task is not in the selected comparison cohort.")
    row = matches[0]
    # One legend state for every side: a kind switched off in a chart is off in
    # that chart on both sides, so the two stay drawn over the same kinds. The
    # trace filter (`hide`) is deliberately not offered here — a filtered view
    # belongs to one trial, and is saved from its own page.
    cfilt = charts.ChartFilter.of(request.args.getlist("chart_off"))
    panels = []
    for selection, job, t, attempts in zip(view.selected, view.jobs, row.trials, row.attempts):
        traj = trajectory.load(t.trajectory_path) if (t and t.has_trajectory) else None
        panels.append({"job": job, "selection": selection, "trial": t,
                       "attempts": attempts,
                       "traj": traj if traj and not traj.error else None,
                       **trace_panel(t, cfilt)})
    return render_template(
        "compare/compare_task.html", task=task, dataset=row.dataset, panels=panels,
        comparison=view, picks=view.references, cohort=view.cohort,
        chart_filter=cfilt, no_filter=trace.Filter(),
    )
