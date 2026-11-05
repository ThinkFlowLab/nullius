"""A trial: its trace, trajectory, conversation, artifacts and saved views."""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from nullius.traces import behavior, charts, conversation, trace, trajectory
from nullius.runs import artifacts, parse

from .core import app
from . import shared


# What a saved trace view may be called. Defined in the data layer, because two
# things depend on agreeing about it: this route writes one, and ?trace= reads one.
SAVED_TRACE_RE = parse.TRACE_NAME_RE
SAVED_TRACE_DEFAULT = "trace-filtered.json"


def trace_file_path(trial, name: str) -> str | None:
    """Absolute path to a trace file inside *trial*'s ``agent/`` directory.

    *name* arrives from a query string or a form field, so it is validated as a
    filename rather than trusted as a path: the regex admits no separator and
    `shared._safe_dest` refuses anything that still manages to leave the directory.
    Returns None when the name is not one this app will read or write.
    """
    if not SAVED_TRACE_RE.match((name or "").strip()):
        return None
    return shared._safe_dest(os.path.join(trial.dir, "agent"), name.strip())


def _dataset_dirs(job):
    """Candidate dataset checkouts for a job's tasks, best lead first.

    The job config's dataset path (absolute, or relative to wherever harbor
    ran), then DATASET_ROOTS. Registry-fetched runs carry no path, so
    the env var is the only way to point those at a local checkout.
    """
    candidates = [job.dataset_path] + os.environ.get(
        "DATASET_ROOTS", ""
    ).split(os.pathsep)
    dirs = []
    for c in candidates:
        if not c:
            continue
        p = os.path.abspath(os.path.expanduser(c))
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    return dirs


def _gold_localization(job, trial, traj):
    """Agent-edited files vs the task's gold patch, or None when no local
    dataset checkout (or no parsable gold diff) can be found."""
    if traj is None:
        return None
    for root in _dataset_dirs(job):
        solve = os.path.join(root, trial.task_base, "solution", "solve.sh")
        if not os.path.isfile(solve):
            continue
        try:
            with open(solve, encoding="utf-8") as fh:
                gold = behavior.gold_files(fh.read())
        except OSError:
            return None
        if gold:
            return behavior.localization(traj.files_touched, gold)
    return None


def _find_trial(job, trial_name):
    return next((x for x in job.trials if x.trial_name == trial_name), None)


def _trial_evidence(job_id, trial_name):
    """Resolve URL identifiers through parsed runs, never as filesystem paths."""
    job = shared._find_job(shared._jobs_for_request(), job_id)
    if job is None:
        abort(404)
    trial = _find_trial(job, trial_name)
    if trial is None:
        abort(404)
    return job, trial


def _requested_trace(trial, *, notify=False):
    """One trace/filter resolution for the page and its read-only detail API.

    `zoom` is parsed beside `hide`/`hide_span` because it is part of the same
    URL-defined view — but it re-bases the waterfall's axis on one subtree and
    hides nothing (`trace.Filter`).
    """
    filt = trace.Filter.of(request.args.getlist("hide"), request.args.getlist("hide_span"),
                           request.args.get("zoom"))
    wanted = (request.args.get("trace") or "").strip()
    path, viewing = (trial.trace_path if trial.has_trace else None), ""
    if wanted:
        alt = trace_file_path(trial, wanted)
        if alt and os.path.isfile(alt):
            path, viewing = alt, os.path.basename(alt)
        elif notify:
            flash(Markup(f"No saved trace called <span class=\"mono\">{escape(wanted)}</span> "
                         "in this trial — showing the producer's own."), "error")
        else:
            abort(404)
    return (_parsed_trace(path, filt) if path else None), viewing


# Traces parsed for a view: every span-detail click, filter and zoom link asked
# for the whole fold, prune, redaction and breakdowns again (about a second at
# 6k spans). Kept by file (path, size, mtime) and view, a few at a time; a Trace
# is not changed once parsed.
_PARSED: "OrderedDict" = OrderedDict()
_PARSED_MAX = 8
_PARSED_LOCK = threading.Lock()


def _parsed_trace(path, filt):
    try:
        st = os.stat(path)
    except OSError:
        return trace.load(path, filt)
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns, filt)
    with _PARSED_LOCK:
        if key in _PARSED:
            _PARSED.move_to_end(key)
            return _PARSED[key]
    tr = trace.load(path, filt)
    with _PARSED_LOCK:
        _PARSED[key] = tr
        while len(_PARSED) > _PARSED_MAX:
            _PARSED.popitem(last=False)
    return tr


@app.route("/trial/<job_id>/<trial_name>/artifact/<key>")
def artifact_page(job_id, trial_name, key):
    job, trial = _trial_evidence(job_id, trial_name)
    try:
        record = artifacts.read(trial.dir, key, request.args.get("page", 1))
    except (LookupError, OSError):
        abort(404)
    response = app.make_response(render_template("traces/artifact.html", job=job, trial=trial,
                                                 artifact=record))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/trial/<job_id>/<trial_name>/conversation")
def conversation_data(job_id, trial_name):
    _, t = _trial_evidence(job_id, trial_name)
    traj = trajectory.load(t.trajectory_path) if t.has_trajectory else None
    payload = conversation.page(
        traj, query=request.args.get("q", ""), kind=request.args.get("kind", "all"),
        page=request.args.get("page", 1), position=request.args.get("position"),
        step=request.args.get("step"), section=request.args.get("section"),
        chunk=request.args.get("chunk", 0),
    )
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/trial/<job_id>/<trial_name>/trace-span")
def trace_span_data(job_id, trial_name):
    _, t = _trial_evidence(job_id, trial_name)
    tr, _ = _requested_trace(t)
    try:
        payload = conversation.span_detail(
            tr, request.args.get("span", ""), section=request.args.get("section"),
            chunk=request.args.get("chunk", 0),
        )
    except LookupError:
        abort(404)
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/trial/<job_id>/<trial_name>")
def trial(job_id, trial_name):
    j, t = _trial_evidence(job_id, trial_name)
    traj = trajectory.load(t.trajectory_path) if t.has_trajectory else None
    ok = traj is not None and not traj.error
    bench_judge = bench_parts = None
    if j.runner:                                   # the benchmark's own judge: what it was asked, what it said
        try:
            with open(os.path.join(t.dir, "verifier", "judge.json"), encoding="utf-8") as fh:
                bench_judge = json.load(fh)
        except (OSError, ValueError):
            pass
        try:                                       # an item scored part by part: each part's text, answer and score
            with open(os.path.join(t.dir, "verifier", "parts.json"), encoding="utf-8") as fh:
                got = json.load(fh)
            if isinstance(got, dict) and isinstance(got.get("parts"), list):
                bench_parts = {"says": str(got.get("says") or ""), "nouns": str(got.get("nouns") or "parts"),
                               "parts": [x for x in got["parts"] if isinstance(x, dict)]}
                # what the definition chose to show of a part, under its own words, in its order
                bench_parts["shows"] = list(dict.fromkeys(k for x in bench_parts["parts"] if isinstance(x.get("shows"), dict) for k in x["shows"]))
        except (OSError, ValueError):
            pass
    loc = _gold_localization(j, t, traj) if ok else None

    # `hide`/`hide_span` make a filtered view a plain link — shareable,
    # bookmarkable, and the same thing `save_trace` writes out. `trace` opens a
    # view saved earlier instead of the producer's own file.
    # A second, narrower query key: which kinds each chart is drawing. Separate
    # from `hide` because it changes one chart and not the trace — the waterfall,
    # the breakdown and what `save_trace` writes are all blind to it.
    cfilt = charts.ChartFilter.of(request.args.getlist("chart_off"))
    tr, viewing = _requested_trace(t, notify=True)
    # Over `tr.roots`, which `parse` has already pruned — the charts describe the
    # filtered view by construction, so there is one definition of "which spans
    # are in this view" and it is still the query string.
    ch = charts.charts_for(tr.roots, cfilt) if tr and tr.visible else None
    records = artifacts.listing(t.dir)
    for record in records:
        record["url"] = url_for("artifact_page", job_id=job_id, trial_name=trial_name,
                                key=record["key"], workspace=request.args.get("workspace"))
    return render_template(
        "traces/trial.html", job=j, trial=t, traj=traj, loc=loc, bench_judge=bench_judge, bench_parts=bench_parts,
        trace=tr, charts=ch, viewing=viewing,
        chart_filter=cfilt, default_save_name=SAVED_TRACE_DEFAULT,
        artifacts=records,
        judgement=_judgement_panel(j, t), llm_judgement=_judgement_panel(j, t, "llm"), csrf=shared._csrf_token(),
    )


def _judgement_panel(job, trial, lane="system-one"):
    from . import judge
    return judge.panel(job, trial, lane)


@app.route("/trial/<job_id>/<trial_name>/save-trace", methods=["POST"])
def save_trace(job_id, trial_name):
    """Write the filtered view to a new trace file beside the trial's own.

    The only route in this app that writes into a job directory. Three refusals
    keep that safe: the name must be a plain `*.json` filename, the destination
    must not exist, and it must not be the file being read — a producer's capture
    is not reproducible, so nothing here may overwrite one.
    """
    shared._require_csrf()
    _, t = _trial_evidence(job_id, trial_name)

    # The zoom rides along only so the page comes back as it was; the file written
    # is the filtered tree whole — a zoom is a way of looking, not a change.
    filt = trace.Filter.of(request.form.getlist("hide"), request.form.getlist("hide_span"),
                           request.form.get("zoom"))
    # Filtering a saved view again is allowed, so the source is whichever file the
    # page was rendering — falling back to the producer's if that one has gone.
    source_name = (request.form.get("trace") or "").strip()
    source = trace_file_path(t, source_name) if source_name else t.trace_path
    if not source or not os.path.isfile(source):
        source, source_name = t.trace_path, ""
    back = url_for(
        "trial", job_id=job_id, trial_name=trial_name,
        trace=source_name or None, **filt.query,
    )

    if not os.path.isfile(source):
        flash("The trace this view was filtered from is no longer there.", "error")
        return redirect(back)
    if not filt.active:
        flash("Nothing is filtered out — a saved view would just copy the original.", "error")
        return redirect(back)

    name = (request.form.get("filename") or "").strip() or SAVED_TRACE_DEFAULT
    if not name.lower().endswith(".json"):
        name += ".json"
    dest = trace_file_path(t, name)
    if not dest:
        flash("Name it as a plain file ending in .json — letters, digits, dot, dash "
              "and underscore, no directories.", "error")
        return redirect(back)
    shown = Markup(f'<span class="mono">{escape(os.path.basename(dest))}</span>')
    if os.path.realpath(dest) == os.path.realpath(source) or os.path.basename(dest) == "trace.json":
        flash(Markup(f"{shown} is the trace being read — save the view under another name."), "error")
        return redirect(back)
    if os.path.exists(dest):
        flash(Markup(f"{shown} already exists; nothing was overwritten."), "error")
        return redirect(back)

    try:
        with open(source, encoding="utf-8") as fh:
            data = json.load(fh)
        # Written from the raw document, so the saved file keeps every field the
        # producer wrote — including the content this app redacts at render. It is
        # exactly as sensitive as its source.
        out = trace.filtered_document(data, filt)
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        flash(Markup(f"Could not write {shown}: {escape(str(exc))}"), "error")
        return redirect(back)

    view = url_for("trial", job_id=job_id, trial_name=trial_name, trace=os.path.basename(dest))
    flash(Markup(
        f"Saved {shown} beside the trial's own trace — {out['filtered']['spans_removed']} "
        f'span(s) removed, totals recomputed. <a href="{view}">Open it</a>.'
    ), "")
    return redirect(back)
