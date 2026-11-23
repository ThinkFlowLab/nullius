"""A trial: its trace, trajectory, conversation, artifacts and saved views."""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections import OrderedDict
from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from nullius.traces import behavior, charts, conversation, trace, trajectory
from nullius.runs import artifacts, attempts as task_attempts, parse
from nullius import importers as harnesses

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


# An attempt's try before its last, as a page addresses it (parse.tries)
_TRY_RE = re.compile(r"^(?P<trial>.+)\.try-(?P<n>[1-9][0-9]?)$")


def _find_trial(job, trial_name):
    return next((x for x in job.trials if x.trial_name == trial_name), None)


def _tries(attempt):
    """``parse.tries`` of an attempt, read once a request: a try's page finds
    its try in the same list its tabs show."""
    seen = g.setdefault("tries", {})
    key = (attempt.job_id, attempt.trial_name)
    if key not in seen:
        seen[key] = parse.tries(attempt)
    return seen[key]


def _find_try(job, name):
    m = _TRY_RE.match(name)
    kept = _find_trial(job, m["trial"]) if m else None
    return next((t for _, t in _tries(kept) if t.trial_name == name), None) if kept else None


def _attempt_of(job, trial):
    """The trial a try is one of: the attempt's last, the one the job counts.
    A trial of the job is its own."""
    if any(t is trial for t in job.trials):
        return trial
    m = _TRY_RE.match(trial.trial_name)
    return (_find_trial(job, m["trial"]) if m else None) or trial


def _trial_evidence(job_id, trial_name, tries=False):
    """Resolve URL identifiers through parsed runs, never as filesystem paths.
    ``tries``: an attempt's earlier try too, ``<trial>.try-N``: evidence to
    read, never a trial to judge."""
    job = shared._job_for_request(job_id)
    if job is None:
        abort(404)
    trial = _find_trial(job, trial_name) or (_find_try(job, trial_name) if tries else None)
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

# Traces parsed for a view: the fold, prune, redaction and breakdowns take about a second at 6k spans. Kept by file (path, size, mtime) and view, a few at a time; a Trace
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
    job, trial = _trial_evidence(job_id, trial_name, tries=True)
    try:
        record = artifacts.read(trial.dir, key, request.args.get("page", 1))
    except (LookupError, OSError):
        abort(404)
    response = app.make_response(render_template("traces/artifact.html", job=job, trial=trial,
                                                 artifact=record))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/trial/<job_id>/<trial_name>/artifact/<key>/download")
def artifact_download(job_id, trial_name, key):
    """The record whole, redacted as the reader shows it: what Download saves
    and Copy puts on the clipboard. One past the reader's limit is cut there,
    as its page says, and saved under a name that says so."""
    _, trial = _trial_evidence(job_id, trial_name, tries=True)
    try:
        text, truncated = artifacts.whole(trial.dir, key)
    except (LookupError, OSError):
        abort(404)
    name = os.path.basename(artifacts.FILES[key][1])
    if truncated:
        stem, ext = os.path.splitext(name)
        name = f"{stem}.partial{ext}"
    response = app.response_class(text, mimetype="application/json" if name.endswith(".json") else "text/plain")
    if not request.args.get("inline"):
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"      # a run's own text, shown as text whatever it holds
    return response


@app.route("/trial/<job_id>/<trial_name>/conversation")
def conversation_data(job_id, trial_name):
    _, t = _trial_evidence(job_id, trial_name, tries=True)
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
    _, t = _trial_evidence(job_id, trial_name, tries=True)
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


def _imported_from(job) -> str:
    """The importer's name for a job an importer wrote (``harness`` in its ``config.json``: ATIF,
    OpenTelemetry), else ""."""
    try:
        with open(os.path.join(job.dir, "config.json"), encoding="utf-8") as fh:
            name = json.load(fh).get("harness")
    except (OSError, ValueError, AttributeError):
        return ""
    module = harnesses.available().get(name) if isinstance(name, str) else None
    return getattr(module, "LABEL", name.upper()) if module else ""


@app.route("/trial/<job_id>/<trial_name>")
def trial(job_id, trial_name):
    j, t = _trial_evidence(job_id, trial_name, tries=True)
    attempt = _attempt_of(j, t)            # t itself, or the attempt's last try when t is an earlier one
    traj = trajectory.load(t.trajectory_path) if t.has_trajectory else None
    ok = traj is not None and not traj.error
    bench_parts = None
    if j.runner:
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

    # `trace` opens a
    # view saved earlier instead of the producer's own file.
    # A second, narrower query key: which kinds each chart is drawing. Chart selections affect only chart drawing and are excluded from saved traces.
    cfilt = charts.ChartFilter.of(request.args.getlist("chart_off"))
    tr, viewing = _requested_trace(t, notify=True)
    # Over `tr.roots`, which `parse` has already pruned — the charts describe the
    # filtered view by construction, so there is one definition of "which spans
    # are in this view" and it is still the query string.
    ch = charts.charts_for(tr.roots, cfilt) if tr and tr.visible else None
    records = [r for r in artifacts.listing(t.dir) if r["exists"]]   # a record never written has no row
    for record in records:
        record["url"] = url_for("artifact_page", job_id=job_id, trial_name=trial_name,
                                key=record["key"], workspace=request.args.get("workspace"))
        record["download"] = url_for("artifact_download", job_id=job_id, trial_name=trial_name,
                                     key=record["key"], workspace=request.args.get("workspace"))
    return render_template(
        "traces/trial.html", job=j, trial=t, traj=traj, loc=loc, bench_parts=bench_parts,
        trace=tr, charts=ch, viewing=viewing, imported_from=_imported_from(j),
        chart_filter=cfilt, default_save_name=SAVED_TRACE_DEFAULT,
        artifacts=records, attempt=attempt, attempts=task_attempts.of(j, attempt), tries=_tries(attempt),
        # an earlier try is evidence only: the job counts its last, and only that is judged
        judgement=_judgement_panel(j, t) if attempt is t else None,
        llm_judgement=_judgement_panel(j, t, "llm") if attempt is t else None, csrf=shared._csrf_token(),
        # an earlier try is evidence only: labels and flags belong to the trials the job counts
        failure=_failure_panel(j, t) if attempt is t else None,
    )


def _failure_panel(j, t):
    from . import failure          # here: failure imports this module
    return failure.panel(j, t)


@app.route("/trial/<job_id>/<trial_name>/attempts")
def trial_attempts(job_id, trial_name):
    """Every attempt at this trial's task, one line each, and a line for each
    earlier try its runner kept."""
    j, t = _trial_evidence(job_id, trial_name, tries=True)
    t = _attempt_of(j, t)
    # a task tried once has no tab that leads here; its one line still answers
    attempts = task_attempts.of(j, t) or task_attempts.Attempts((t,))
    tries = {a.trial_name: _tries(a) for a in attempts.trials if a.n_retries}
    return render_template("traces/attempts.html", job=j, trial=t, attempts=attempts, tries=tries,
                           imported_from=_imported_from(j))


def _judgement_panel(job, trial, lane="decision"):
    from . import judge
    return judge.panel(job, trial, lane)


@app.route("/trial/<job_id>/<trial_name>/save-trace", methods=["POST"])
def save_trace(job_id, trial_name):
    """Write the filtered view to a new trace file beside the trial's own.

    One of two routes that write into a job directory (the other is the Failure
    analysis tab's, `failure.failure_route`, which writes only its own file). Three refusals
    keep that safe: the name must be a plain `*.json` filename, the destination
    must not exist, and it must not be the file being read — a producer's capture
    is not reproducible, so nothing here may overwrite one.
    """
    shared._require_csrf()
    _, t = _trial_evidence(job_id, trial_name, tries=True)

    # The zoom rides along only so the page comes back as it was; the file written
    # is the filtered tree whole.
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
    created = False
    try:
        with open(source, encoding="utf-8") as fh:
            data = json.load(fh)
        # Written from the raw document, so the saved file keeps every field the
        # producer wrote — including the content this app redacts at render. It is
        # exactly as sensitive as its source.
        out = trace.filtered_document(data, filt)
        text = json.dumps(out, ensure_ascii=False, indent=1)
        # Exclusive create: a file that appeared since the name was chosen is refused, never replaced.
        with open(dest, "x", encoding="utf-8") as fh:
            created = True
            fh.write(text)
    except FileExistsError:
        flash(Markup(f"{shown} already exists; nothing was overwritten."), "error")
        return redirect(back)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        if created:
            with contextlib.suppress(OSError):
                os.remove(dest)
        flash(Markup(f"Could not write {shown}: {escape(str(exc))}"), "error")
        return redirect(back)

    view = url_for("trial", job_id=job_id, trial_name=trial_name, trace=os.path.basename(dest))
    flash(Markup(
        f"Saved {shown} beside the trial's own trace — {out['filtered']['spans_removed']} "
        f'span(s) removed, totals recomputed. <a href="{view}">Open it</a>.'
    ), "")
    return redirect(back)
