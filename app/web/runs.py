"""The runs list, a job's page and its live board, the workspace picker and upload."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import Markup, escape
from nullius import execution
from nullius import importers as harnesses
from nullius.redact import redact
from nullius.runs import classify, feasibility, launches, live, parse
from nullius import workspace

from .core import app
from . import shared



@app.route("/")
def home():
    """Open the application on Runs, with its current workspace or first steps."""
    return redirect(url_for("index"))


@app.route("/runs")
def index():
    if shared.STATE["workspace"] is None:
        return render_template("runs/start.html", workspaces=workspace.list_all(shared.WS_HOME))
    jobs = shared._load_jobs()
    # Runs started from the builder whose job Harbor has not written yet:
    # listed above the table, each a link to the page that follows it.
    have = {j.job_id for j in jobs}
    # The run records are the app's, not the workspace's: only those launched
    # into this workspace's roots, or with a job already written here, belong.
    mine = [e for e in execution.EXECUTIONS.values() if _launched_here(e, have)]
    merged_sources = launches.merged_sources(jobs)
    jobs = [j for j in jobs if j.job_id not in merged_sources]
    preparing = [(jid, e) for e in mine if e.state in execution.ALIVE for jid in e.summary()["job_ids"] if jid not in have]
    # a launch whose runs wrote no job yet (or never will: refused, stopped) is still shown
    if not jobs and not mine:
        return render_template("runs/setup.html", scanned=True, mode="add",
                               workspaces=workspace.list_all(shared.WS_HOME))
    # A Harbor killed by a Stop (or dead with the run) leaves its result
    # record saying trials are running; the runner's record says otherwise.
    ended = {j.job_id: execution.ended_for(j.job_id) for j in jobs}
    # The header, the chips and the mix bar describe the agents' trials. A
    # control (nop, oracle) is a check on the tasks, not a competitor: it is
    # listed on its own line as behaved or not (`feasibility.summary_of`),
    # and its trials never enter the totals — a nop rejected on every task
    # is the expected result, not five failed verifications.
    verdicts = {j.job_id: feasibility.summary_of(j) for j in jobs}
    verdicts = {i: replace(v, running=False) if v and ended[i] else v for i, v in verdicts.items()}
    agents = [j for j in jobs if verdicts[j.job_id] is None]
    controls = [(j, verdicts[j.job_id]) for j in jobs if verdicts[j.job_id] is not None]
    totals = {k: 0 for k in classify.ORDER}
    n_trials = 0
    for j in agents:
        for k, v in j.counts.items():
            totals[k] += v
        n_trials += len(j.trials)

    # Only describe datasets these runs actually used, most-used first.
    used = {}
    for j in jobs:
        entry = used.setdefault(j.dataset, {"runs": 0, "trials": 0})
        entry["runs"] += 1
        entry["trials"] += len(j.trials)
    datasets = [
        {"name": name, "info": shared.dataset_info(name), **counts}
        for name, counts in sorted(used.items(), key=lambda kv: -kv[1]["trials"])
    ]

    # Distinct values behind the per-column filter menus, as (value, label).
    # Value is what the row carries; label is what the menu shows.
    choices = {
        "dataset": sorted(
            {(j.dataset, shared.dataset_title(j.dataset)) for j in jobs}, key=lambda p: p[1]
        ),
        "model": sorted({(j.model_name, j.model_name) for j in jobs}),
        "agent": sorted({(j.agent_label, j.agent_label or "—") for j in jobs}),
    }
    # The page's first view: the runs grouped by the launch that made them
    # (nullius.runs.launches) - the builder's run record, else the same dataset
    # within half an hour - each with its task checks and its agents side by side.
    records = {e.id: (e.plan.name, [str(x.get("name")) for x in e.plan.jobs
                                  if str(x.get("name")) not in merged_sources], e.started) for e in mine}
    by_id = {j.job_id: j for j in jobs}
    groups = [_launch_view(g, by_id, verdicts, ended) for g in launches.group(jobs, records)]
    view = request.args.get("view") if request.args.get("view") in ("launch", "all") else "launch"
    states = {}
    for g in groups:
        for row in g["agents"]:
            states[row["state"]] = states.get(row["state"], 0) + 1
    return render_template(
        "runs/index.html", jobs=jobs, totals=totals, n_trials=n_trials,
        n_agent_runs=len(agents), controls=controls, verdicts=verdicts, ended=ended,
        datasets=datasets, choices=choices, pulse=_roots_pulse(), preparing=preparing,
        launches=groups, view=view, states=states,
        n_checked=sum(1 for g in groups if g["checked"]),
    )


def _launched_here(ex, have) -> bool:
    """A run record belongs to the open workspace when its job root is one of
    the workspace's roots (a Run adopts it there) or one of its jobs is here."""
    if any(str(x.get("name")) in have for x in ex.plan.jobs):
        return True
    jobs_dir = getattr(ex, "jobs", None)
    roots = list(shared.STATE.get("roots") or []) + list(shared.STATE.get("job_roots") or [])
    return bool(jobs_dir) and os.path.realpath(jobs_dir) in {os.path.realpath(r) for r in roots}


# A launch's runs, in the order the block lists them: finished first, best
# adjusted rate on top; then those still going; then the ones that stopped.
_STATE_ORDER = {"finished": 0, "running": 1, "preparing": 2, "queued": 3, "stopping": 4, "paused": 5, "failed": 6, "refused": 7, "stopped": 8, execution.NOT_RUN: 8.5,
                "not in this workspace": 9}


def _state_of(job_id, j, ended):
    """What a run is doing now, in the words the block shows. The runner's
    record wins over Harbor's result record, which a Stop leaves saying
    "running"."""
    if j is not None and ended.get(job_id):
        how = ended[job_id][0]
        return "finished" if how == "done" else how
    ex = execution.for_job(job_id)
    if ex is not None and ex.state in execution.ALIVE:
        return ex.live_state(job_id)
    if j is None:
        # named by a run record and not under this workspace's roots: still to be written while the run lives, refused
        # before it started (its folder says so, or no part was launched), else elsewhere
        if ex is not None and ex.state not in execution.ALIVE:
            return "refused" if ex.refused(job_id) else execution.NOT_RUN if ex.never_ran(job_id) else "not in this workspace"
        return ex.state if ex is not None else "preparing"
    if j.is_preparing:
        return "preparing"
    if j.is_running:
        return "running"
    return "finished"


def _control_planned(job_id) -> str:
    """``nop`` or ``oracle`` when a job not written yet is one of the run's
    controls by its plan, else ''. A standard plan names a declared control
    by that word; its setups are ``original`` or ``model``, never one."""
    ex = execution.for_job(job_id)
    if ex is None:
        return ""
    agent = next((str(j.get("agent") or "") for j in ex.plan.jobs if str(j.get("name")) == job_id), "").lower()
    return agent if agent in parse.CONTROL_AGENTS else ""


# A control in one of these states ended before it checked every task.
_ENDED_SHORT = ("failed", "refused", "stopped", execution.NOT_RUN, "not in this workspace")


def _cut_short(r) -> str:
    """What a control row lacks to check every task, in the words of the
    task-check line: ``nop stopped``, ``oracle ran 2 of 4``; '' when complete."""
    if r["state"] in _ENDED_SHORT:
        return f"{r['control']} {r['state']}"
    j = r["job"]
    if r["state"] == "finished" and j is not None and j.n_planned and len(j.trials) < j.n_planned:
        return f"{r['control']} ran {len(j.trials)} of {j.n_planned}"
    return ""


def _launch_view(g, by_id, verdicts, ended) -> dict:
    """One launch as its block draws it: the task checks, the agents ranked on
    one scale, the runs its record named that Harbor has not written yet."""
    jobs = [by_id[i] for i in g.job_ids]
    checks = [(j, verdicts[j.job_id]) for j in jobs if verdicts[j.job_id] is not None]
    rows = [{"job": j, "job_id": j.job_id, "state": _state_of(j.job_id, j, ended)} for j in jobs if verdicts[j.job_id] is None]
    waiting = [(i, c) for i, c in ((i, _control_planned(i)) for i in g.pending) if c]
    rows += [{"job": None, "job_id": i, "state": _state_of(i, None, ended)} for i in g.pending if i not in dict(waiting)]
    for r in rows:
        if r["state"] == "refused":
            r["why"] = execution.for_job(r["job_id"]).refused(r["job_id"])
    rate = lambda r: r["job"].adjusted_solve_rate if r["job"] is not None and r["job"].adjusted_solve_rate is not None else -1.0
    rows.sort(key=lambda r: (_STATE_ORDER.get(r["state"], 9), -rate(r), r["job_id"]))
    ok = {v.control for _, v in checks if v.state == "ok"}
    # The controls are rows of their own at the top of the table, written or still to come, nop before oracle:
    # a check on the tasks, never ranked with the agents nor offered to Compare, and kept once they end.
    controls = [{"job": j, "job_id": j.job_id, "state": _state_of(j.job_id, j, ended), "control": v.control, "verdict": v} for j, v in checks]
    controls += [{"job": None, "job_id": i, "state": _state_of(i, None, ended), "control": c, "verdict": None} for i, c in waiting]
    controls.sort(key=lambda r: (feasibility.CONTROLS.index(r["control"]) if r["control"] in feasibility.CONTROLS else 9, r["job_id"]))
    short = list(dict.fromkeys(filter(None, map(_cut_short, controls))))
    if any(r["state"] != "finished" and r["state"] not in _ENDED_SHORT for r in controls):
        verdict, checked = "Not checked yet: a control has not finished.", False
    elif not checks and not short:
        verdict, checked = None, False
    elif any(v.state == "warn" for _, v in checks):
        verdict, checked = "A control did not behave: some tasks may be broken. See Feasibility.", False
    elif short:
        verdict, checked = f"Task checks incomplete: {', '.join(short)}.", False
    elif any(v.state == "pending" for _, v in checks):
        verdict, checked = "Not checked yet: a control has not finished.", False
    elif {"nop", "oracle"} <= ok:
        verdict, checked = "Every task is feasible: an empty answer fails and the reference passes.", True
    elif "nop" in ok:
        verdict, checked = "An empty answer fails every task; no oracle ran to show each can pass.", True
    else:
        verdict, checked = "The reference passes every task; no nop ran to show an empty answer fails.", True
    n_tasks = max([j.n_planned or len(j.trials) for j in jobs] + [0]) or None
    finished = [r["job_id"] for r in rows if r["state"] == "finished" and r["job"] is not None and r["job"].has_verdict]
    counts = {}
    for r in controls + rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    ex = execution.for_job((g.job_ids + g.pending)[0])
    return {"launch": g, "checks": checks, "verdict": verdict,
            "controls": controls, "checked": checked, "agents": rows,
            "n_tasks": n_tasks, "compare": finished,
            "paused": next((i for i in g.job_ids + g.pending
                            if (execution.for_job(i) is not None and execution.for_job(i).state == "paused")), None),
            "summary": " · ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: _STATE_ORDER.get(kv[0], 9))),
            "recipe": (g.job_ids + g.pending)[0] if g.source == "record" and ex is not None and ex.plan.recipe else None,
            "feasibility": checks[0][0].job_id if checks else None,
            "dataset_title": shared.dataset_title(g.dataset) if g.dataset and g.dataset != "mixed" else (g.dataset or "—")}


def _roots_pulse() -> str:
    """A short signature of what the job roots hold right now: each job
    directory's name, whether it has a result record and a feed, and the
    feed's size. It changes when a run is launched (a directory appears),
    while it runs (the feed grows) and when it ends (the result lands) — and
    at no other time, so a page that polls it reloads only for a reason.
    Nothing is parsed; this is a handful of stats per job."""
    dirs = parse.find_job_dirs(shared.STATE["job_roots"] or [])
    have = {os.path.basename(d) for d in dirs}
    marks = [(f"run:{e.id}", e.state, 0) for e in execution.EXECUTIONS.values() if _launched_here(e, have)]
    for d in dirs:
        feed = os.path.join(d, "events.jsonl")
        marks.append((os.path.basename(d), os.path.isfile(os.path.join(d, "result.json")),
                      os.path.getsize(feed) if os.path.isfile(feed) else -1))
    return hashlib.sha1(json.dumps(sorted(marks)).encode()).hexdigest()[:16]


@app.route("/runs/pulse")
def runs_pulse():
    """What the runs list polls. ``signature`` is :func:`_roots_pulse`; a
    change means the list is stale."""
    response = jsonify({"signature": _roots_pulse()})
    response.headers["Cache-Control"] = "no-store"
    return response


def _readable(roots, shown=lambda root: root):
    """Split *roots* into the ones a workspace can adopt and the ones it must
    not, each of those with why.

    A root is refused when the importer that claims it cannot read it — a
    ``run.json`` that is not JSON, a log of the wrong shape — or when nothing
    claims it and nothing in it is job-shaped either. Adopting either would
    have been reported as success: the first as a job with no trials, the
    second as a workspace with nothing new in it, and neither says what was
    wrong with the folder. The message is redacted before it is shown: a
    file's path or content is arbitrary text, and a credential in a bad one
    must not reach the page because the file was bad.
    """
    kept, refused = [], []
    for root in roots:
        why = harnesses.problem(root)
        if why is None and not parse.find_job_dirs([root]) and harnesses.detect(root) is None:
            why = (f"nothing job-shaped here, and no importer recognises it "
                   f"(known: {harnesses.labels()})")
        if why is None:
            kept.append(root)
        else:
            refused.append((shown(root), why))
    return kept, refused


def _refused_message(refused):
    """One line per refused root, the root in monospace like `_bad_paths_message`."""
    return Markup("<br>".join(
        f'Not imported: <span class="mono">{escape(redact(root))}</span> — {escape(why)}'
        for root, why in refused))


def _after_import(good, refused, new):
    """Where a submission lands: the runs list — or, when something was
    refused, the picker, which is the page that shows the message. The runs
    list does not, and a refusal nobody sees is a silent success."""
    if refused:
        flash(_refused_message(refused), "error")
        return redirect(url_for("import_page", new="1" if new and not good else None))
    return redirect(url_for("index"))


@app.route("/use-path", methods=["POST"])
def use_path():
    shared._require_csrf()
    good, bad = shared.resolve_submitted_paths(request.form.getlist("path"))
    if bad:
        flash(shared._bad_paths_message(bad), "error")
    good, refused = _readable(good)
    new = request.form.get("new") == "1"
    if good:
        shared.adopt_roots(good, label=(request.form.get("name") or "").strip(), new=new)
    return _after_import(good, refused, new)


@app.route("/api/folders", methods=["POST"])
def folder_suggestions():
    shared._require_csrf()
    response = jsonify(paths=shared.suggest_directories(request.form.get("path", "")))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/change-source", methods=["POST"])
def change_source():
    """Close the workspace and return to the Runs page's first steps."""
    shared._require_csrf()
    shared.close_workspace()
    return redirect(url_for("index"))


@app.route("/import")
def import_page():
    """The picker: add runs to the open workspace, or start a new one."""
    mode = "new" if request.args.get("new") == "1" or shared.STATE["workspace"] is None else "add"
    return render_template("runs/setup.html", scanned=False, mode=mode,
                           workspaces=workspace.list_all(shared.WS_HOME))


@app.route("/upload", methods=["POST"])
def upload():
    shared._require_csrf()
    files = request.files.getlist("files")
    files = [f for f in files if f.filename]
    # One Parse button serves both halves of the form, so typed paths can
    # arrive alongside uploaded folders. Both end up in the same root list.
    typed, bad = shared.resolve_submitted_paths(request.form.getlist("path"))
    if bad:
        flash(shared._bad_paths_message(bad), "error")
    if not files:
        if typed:
            shared.set_source(typed)
        else:
            flash("No files received — nothing to scan.", "error")
        return redirect(url_for("index"))

    tmp = tempfile.mkdtemp(prefix="nullius_jobs_")
    saved = 0
    for f in files:
        dest = shared._safe_dest(tmp, f.filename)
        if dest is None:
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        f.save(dest)
        saved += 1
    if not saved:
        flash("Upload failed — no files could be saved.", "error")
        return redirect(url_for("index"))

    # Scan uploaded folders without also scanning tmp, which would import them twice.
    # Use tmp only for loose files; name sources and errors after the uploaded paths.
    top = sorted(n for n in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, n)))
    dropped, refused = _readable([os.path.join(tmp, n) for n in top] or [tmp],
                                 shown=lambda root: os.path.relpath(root, tmp))
    typed, refused_typed = _readable(typed)
    refused += refused_typed
    kept_top = [os.path.basename(d) for d in dropped if d != tmp]
    roots = dropped + typed
    if len(kept_top) == 1:
        label = f"{kept_top[0]} (uploaded)"
    elif kept_top:
        label = f"{len(kept_top)} folders (uploaded)"
    else:
        label = f"{saved} files (uploaded)"
    if typed:
        label += f" +{len(typed)} local"
    new = request.form.get("new") == "1"
    if roots:
        shared.adopt_roots(roots, label=(request.form.get("name") or "").strip() or label,
                    new=new)
    return _after_import(roots, refused, new)


@app.route("/job/<job_id>", methods=["GET", "HEAD"])
def job(job_id):
    j = shared._find_job(shared._jobs_for_request(), job_id)
    ex = execution.for_job(job_id)
    # this job's own state beside the run's: ended, or a part still to come while another runs
    own = ((execution.NOT_RUN, None) if ex is not None and ex.state not in execution.ALIVE and ex.never_ran(job_id)
           else execution.ended_for(job_id) or ((ex.live_state(job_id), None)
                if ex is not None and ex.live_state(job_id) in ("queued", execution.NOT_RUN) else None))
    if not j:
        # Started from the builder and not written by Harbor yet: the run's
        # own page, which becomes this one when the job appears.
        if ex is not None and request.method == "GET":
            return render_template("runs/preparing.html", job_id=job_id, ex=ex, csrf=shared._csrf_token(), own=own)
        abort(404)
    from . import judge
    board = _live_board_state(j)
    return render_template("runs/job.html", job=j, live_board=board, view=_view_of(board, j),
                           execution=ex, csrf=shared._csrf_token(),
                           judgement=judge.job_summary(j) if j.trials else None,
                           control=feasibility.summary_of(j), ended=execution.ended_for(job_id), own=own)


def _view_of(board, j=None) -> str | None:
    """Which tab the job page opens on, or None when it has no tabs.

    A job with a board has two views of one page: Live, the feed folded as it
    grows, and Results, the page rendered from the job's files. ``?view=`` wins,
    so a link opens the view it was copied from; otherwise Live while the feed
    says the job is going, Results once it ended - unless there are no results
    to show: a job with a feed and no result record yet (a runner's arm whose
    importer has not written one) stays on Live. A job with no feed, a broken
    one or an empty one has no Live view, and no tabs.
    """
    if not board or not board.get("initial") or board["initial"]["state"] == "unknown":
        return None
    asked = request.args.get("view")
    if asked in ("live", "results"):
        return asked
    return "live" if board["running"] or (j is not None and j.is_preparing) else "results"


def _live_board_state(j):
    """Whether the job page has a live board, and from what.

    The board is the job page's Live view, not a second page: it polls while
    the feed says the job has not ended, and once it ended it stays as the
    record of how the run went, beside Results. A job without a feed has no
    live board.
    """
    if not live.has_feed(j.dir):
        return None
    try:
        snap = live.snapshot(j.dir, ended=execution.ended_for(j.job_id), with_timeline=True)
    except live.FeedError as e:
        # A malformed line in the middle of the feed is a broken writer, not a
        # race. The page must still render — the settled sections read
        # result.json, not the feed — so the board steps aside and says why.
        return {"running": False, "initial": None, "error": str(e)}
    # "running" here means "show the board and keep polling": the preparation
    # window and the starting state are on the board too, so it is every
    # state but finished — and unknown, an empty feed, which has nothing to show.
    return {"running": snap["state"] in ("preparing", "starting", "running"), "initial": snap}


@app.route("/job/<job_id>/live")
def job_live(job_id):
    """The board's snapshot, folded fresh from the feed on every call.

    Deliberately not through the workspace cache: the cache is keyed by the
    files parse reads, and a feed that grows every second would either miss
    the cache on every request or, worse, hit it and show a board that stopped.
    Folding a few thousand lines is cheap enough to do every second.
    """
    j = shared._find_job(shared._jobs_for_request(), job_id)
    if not j:
        abort(404)
    if not live.has_feed(j.dir):
        ended = execution.ended_for(job_id)
        if ended:
            # The preparing page must settle even if the runner never wrote a feed.
            response = jsonify({"state": "finished", "running": False, "ended_by": ended[0]})
            response.headers["Cache-Control"] = "no-store"
            return response
        abort(404)
    try:
        snap = live.snapshot(j.dir, ended=execution.ended_for(j.job_id), with_timeline=True)
    except live.FeedError as e:
        response = jsonify({"error": f"events.jsonl is not a feed: {e}"})
        response.status_code = 422
        response.headers["Cache-Control"] = "no-store"
        return response
    response = jsonify(snap)
    response.headers["Cache-Control"] = "no-store"
    return response
