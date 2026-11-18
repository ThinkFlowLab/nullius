"""The runs list, a job's page and its live board, the workspace picker and upload."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import replace
from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    url_for,
)
from markupsafe import Markup, escape
from nullius import execution
from nullius import importers as harnesses
from nullius.redact import redact
from nullius.runs import attempts, classify, feasibility, launches, live, parse
from nullius import workspace
from nullius.standard import definitions as standard_definitions

from .core import app
from . import shared
from . import settings as settings_web



@app.route("/")
def home():
    """Open the application on Runs, with its current workspace or first steps."""
    return redirect(url_for("index"))


@app.route("/runs")
def index():
    if shared.STATE["workspace"] is None:
        return render_template("runs/start.html", workspaces=workspace.list_all(shared.WS_HOME))
    jobs, have, mine, grouped = _grouped()
    # Runs started from the builder whose job Harbor has not written yet:
    # listed above the table, each a link to the page that follows it.
    preparing = [(jid, e) for e in mine if e.state in execution.ALIVE for jid in e.summary()["job_ids"] if jid not in have]
    # a launch whose runs wrote no job yet (or never will: refused, stopped) is still shown
    if not jobs and not mine:
        return render_template("runs/setup.html", scanned=bool(shared.STATE["roots"]), mode="add",
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
        if verdicts[j.job_id] is None:          # agents only, as the header counts them
            entry["runs"] += 1
            entry["trials"] += len(j.trials)
    datasets = [
        {"name": name, "info": shared.dataset_info(name), **counts}
        for name, counts in sorted(used.items(), key=lambda kv: -kv[1]["trials"])
    ]

    # Distinct values behind the Filter menu's lists, as (value, label).
    # Value is what the row carries; label is what the menu shows.
    choices = {
        "dataset": sorted(
            {(j.dataset, shared.dataset_title(j.dataset)) for j in jobs}, key=lambda p: p[1]
        ),
        "model": sorted({(j.model_name, j.model_name) for j in jobs}),
        "agent": sorted({(j.agent_label, j.agent_label or "—") for j in jobs}),
    }
    by_id = {j.job_id: j for j in jobs}
    groups = [_launch_view(g, by_id, verdicts, ended) for g in grouped]
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
        n_checks=sum(g["n_checks"] for g in groups),
    )


def _grouped(ws=None):
    """The open workspace's jobs (less those a merge stands for), the ids of all
    it parsed, the run records that belong to it, and the launches By launch
    shows: the runs grouped by the launch that made them (nullius.runs.launches)
    - the builder's run record, else the same dataset within half an hour.
    ``ws``: another workspace's, read from its cache without opening it."""
    jobs = shared._load_jobs() if ws is None else workspace.load_jobs(ws, ws.job_roots or ws.roots)
    have = {j.job_id for j in jobs}
    # The run records are the app's, not the workspace's: only those launched
    # into this workspace's roots, or with a job already written here, belong.
    real = _real_roots(None if ws is None else list(ws.roots) + list(ws.job_roots))
    mine = [e for e in execution.EXECUTIONS.values() if _launched_here(e, have, real)]
    merged = launches.merged_sources(jobs)
    jobs = [j for j in jobs if j.job_id not in merged]
    return jobs, have, mine, launches.group(jobs, {e.id: _record(e, merged) for e in mine})


def _record(ex, merged=()) -> tuple:
    """A run record as ``launches.group`` reads it, less the jobs a merge stands for."""
    names = [str(x.get("name")) for x in ex.plan.jobs if str(x.get("name")) not in merged]
    return ex.plan.name, names, ex.started, {str(x.get("launch") or "") for x in ex.plan.jobs}


def _every() -> tuple:
    """Every launch Nullius made, newest first: every run record, grouped as
    the Runs page groups them, each record's jobs read without their trials,
    and a merge of them written beside them. Returns (job id -> job, launches)."""
    recs = list(execution.EXECUTIONS.values())
    named = {str(x.get("name")) for e in recs for x in e.plan.jobs}
    jobs = [parse.parse_job(d, trials=False) for d in parse.find_job_dirs([e.jobs for e in recs if os.path.isdir(e.jobs)])]
    jobs = [j for j in jobs if j.job_id in named or named & set(getattr(j, "merged_from", []))]
    return {j.job_id: j for j in jobs}, launches.group(jobs, {e.id: _record(e) for e in recs})


def _tally(g, by_id) -> dict:
    """A launch's runs, its checks (nop, oracle) apart, as the Runs page and
    the Launches page count them: its jobs written (less those a merge stands
    for) and those its records name and that are not written yet, each shard
    one."""
    merged = launches.merged_sources([by_id[i] for i in g.job_ids])
    written, pending = [i for i in g.job_ids if i not in merged], [i for i in g.pending if i not in merged]
    checks = sum(1 for i in written if feasibility.control_of(by_id[i])) + sum(1 for i in pending if _control_planned(i))
    return {"n_runs": len(written) + len(pending) - checks, "n_checks": checks}


def _real_roots(roots=None) -> set:
    """``roots`` (the open workspace's when None), links resolved: what ``_launched_here`` matches against."""
    if roots is None:
        roots = list(shared.STATE.get("roots") or []) + list(shared.STATE.get("job_roots") or [])
    return {os.path.realpath(r) for r in roots}


def _launched_here(ex, have, real) -> bool:
    """A run record belongs to a workspace when its job root is one of the
    workspace's roots, ``real`` (``_real_roots``; a Run adopts it there), or
    one of its jobs is here, ``have``. Each record's root is resolved once per
    request, however many workspaces ask."""
    if any(str(x.get("name")) in have for x in ex.plan.jobs):
        return True
    jobs_dir = getattr(ex, "jobs", None)
    if not jobs_dir:
        return False
    memo = shared.request_memo("real_jobs")
    if jobs_dir not in memo:
        memo[jobs_dir] = os.path.realpath(jobs_dir)
    return memo[jobs_dir] in real


# A launch's runs, in the order the block lists them: finished first, best
# adjusted rate on top; then those still going; then the ones that stopped.
_STATE_ORDER = {"finished": 0, "running": 1, "preparing": 2, "queued": 3, "stopping": 4, "paused": 5, "failed": 6, "refused": 7, "stopped": 8, execution.NOT_RUN: 8.5,
                "not in this workspace": 9}


def _said(state) -> str:
    """A run record's state in the words the pages show."""
    return "finished" if state == "done" else state


def _state_of(job_id, j, ended):
    """What a run is doing now, in the words the block shows. The runner's
    record wins over Harbor's result record, which a Stop leaves saying
    "running"."""
    if j is not None and ended.get(job_id):
        return _said(ended[job_id][0])
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
    return "" if ex is None else next((_control_of(j) for j in ex.plan.jobs if str(j.get("name")) == job_id), "")


def _control_of(job: dict) -> str:
    """``nop`` or ``oracle`` when a plan's job is a control, else ''."""
    agent = str(job.get("agent") or "").lower()
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


# A shard's job as a recipe names it (standard/recipe.py): ``<launch>-shard-<I>of<N>``.
_SHARD = re.compile(r"-shard-(\d+)of\d+$")
# A participant's state is its shards' most telling: one still working, else the one that ended worst.
_TELLING = ("running", "preparing", "stopping", "paused", "queued", "failed", "refused", "stopped", execution.NOT_RUN,
            "not in this workspace", "finished")


def _most_telling(states) -> str:
    """The most telling of several states (``_TELLING``); '' for none."""
    return min(states, default="", key=lambda s: _TELLING.index(s) if s in _TELLING else len(_TELLING))


def _host_of(job_id) -> str:
    ex = execution.for_job(job_id)
    return "" if ex is None else ex.plan.host.label if ex.plan.host else "this machine"


def _fold(rows, ended, control=False) -> list:
    """A participant's shards as one row, ``shards`` its shards' own rows, when
    it has two or more and ``cli merge`` would put them together: their numbers
    are the merged job's (``launches.fold``). Missing evidence is not a zero:
    ``not_run`` is the units no shard ran, beside the count, never in it."""
    def key(r):
        # a written shard by the launch id its config records; one not written yet by the name its record gives it
        if r["job"] is not None:
            return (r.get("control"), r["job"].launch_id) if r["job"].launch_id else id(r)
        return (r.get("control"), _SHARD.sub("", r["job_id"])) if _SHARD.search(r["job_id"]) else id(r)
    by = {}
    for r in rows:
        by.setdefault(key(r), []).append(r)
    out = []
    for k, group in by.items():
        written = [r["job"] for r in group if r["job"] is not None]
        got = launches.fold(written) if len(group) > 1 and written else None
        if len(group) < 2 or (written and got is None):
            out += group
            continue
        job, check = got or (None, None)
        index = {s["name"]: s["shard"].get("index") for s in check["shards"]} if check else {}
        for r in group:
            m = _SHARD.search(r["job_id"])
            r["shard"] = index.get(r["job_id"]) or (int(m.group(1)) if m else None)
            r["host"] = _host_of(r["job_id"])
        group.sort(key=lambda r: (r["shard"] is None, r["shard"] or 0, r["job_id"]))
        states = [r["state"] for r in group]
        state = _most_telling(states)
        bad = [s for s in dict.fromkeys(states) if s in _ENDED_SHORT]
        fold = {"job": job, "job_id": k[1], "state": state, "shards": group, "control": k[0],
                "note": ", ".join(f"{states.count(s)} of {len(group)} shards {s}" for s in bad),
                "notes": check["notes"] if check else []}
        if job is not None:
            total = check["units_total"] or job.n_planned
            fold["total"] = total
            # while a shard still works, its units are to come, not missing
            fold["not_run"] = max(0, total - len(check["by_unit"])) if state in _ENDED_SHORT + ("finished",) else 0
        if control:
            v = feasibility.summary_of(job) if job is not None else None
            fold["verdict"] = v and replace(v, running=any(r["verdict"] and r["verdict"].running for r in group))
        out.append(fold)
    return out


def offered(ws=None) -> list:
    """The runs Compare offers from the open workspace (or ``ws``): (launch, run, the job ids it stands for, its
    shards' rows as Runs lists them), newest launch first, checks included, a participant's shards one run as
    the Runs page folds them."""
    jobs, _, _, grouped = _grouped(ws)
    by_id = {j.job_id: j for j in jobs}
    out = []
    for g in grouped:
        rows = [{"job": by_id[i], "job_id": i, "state": "finished", "control": feasibility.control_of(by_id[i])} for i in g.job_ids]
        for r in _fold(rows, {}):
            out.append((g, r["job"], [s["job_id"] for s in r.get("shards", [r])] + list(r["job"].merged_from), r.get("shards", [])))
    return out


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
    rows = _fold(rows, ended)
    rate = lambda r: r["job"].adjusted_solve_rate if r["job"] is not None and r["job"].adjusted_solve_rate is not None else -1.0
    rows.sort(key=lambda r: (_STATE_ORDER.get(r["state"], 9), -rate(r), r["job_id"]))
    # The controls are rows of their own at the top of the table, written or still to come, nop before oracle:
    # a check on the tasks, never ranked with the agents nor offered to Compare, and kept once they end.
    controls = [{"job": j, "job_id": j.job_id, "state": _state_of(j.job_id, j, ended), "control": v.control, "verdict": v} for j, v in checks]
    controls += [{"job": None, "job_id": i, "state": _state_of(i, None, ended), "control": c, "verdict": None} for i, c in waiting]
    controls = _fold(controls, ended, control=True)
    controls.sort(key=lambda r: (feasibility.CONTROLS.index(r["control"]) if r["control"] in feasibility.CONTROLS else 9, r["job_id"]))
    # judged over every shard: a folded control's verdict is over all its trials, and what one shard lacks is said of that shard
    checks = [(r["job"], r["verdict"]) for r in controls if r["verdict"] is not None]
    ok = {v.control for _, v in checks if v.state == "ok"}
    lacks = lambda r: [f"{c} on shard {s['shard']}" for s in r["shards"] for c in [_cut_short(s)] if c] if "shards" in r else [_cut_short(r)]
    short = list(dict.fromkeys(filter(None, (x for r in controls for x in lacks(r)))))
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
    n_tasks = max([r.get("total") or r["job"].n_planned or len(r["job"].trials) for r in controls + rows if r["job"] is not None] + [0]) or None
    # each shard is a run of its own: it is counted, offered to Compare and opened as one
    leaves = lambda rs: [s for r in rs for s in r.get("shards", [r])]
    finished = [r["job_id"] for r in leaves(rows) if r["state"] == "finished" and r["job"] is not None and r["job"].has_verdict]
    counts = {}
    for r in leaves(controls + rows):
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    ex = execution.for_job((g.job_ids + g.pending)[0])
    return {"gone": _deletion(_runs_of(g)),
            "launch": g, "checks": checks, "verdict": verdict,
            "controls": controls, "checked": checked, "agents": rows,
            "n_tasks": n_tasks, "compare": finished,
            **_tally(g, by_id),
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
    marks = [(f"run:{e.id}", e.state, 0) for e in execution.EXECUTIONS.values() if _launched_here(e, have, _real_roots())]
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


def _gone_into(name):
    """``into`` names a workspace that is gone: nothing is added, not even to the open one."""
    flash(Markup('No workspace called <span class="mono">{}</span>: nothing added.').format(name), "error")
    return redirect(url_for("workspaces_page"))


def _after_import(good, refused, new, into=""):
    """Where a submission lands: the runs list, or the Workspaces page for
    roots added ``into`` a named workspace — or, when something was refused,
    the picker, which is the page that shows the message. The runs list does
    not, and a refusal nobody sees is a silent success."""
    if refused:
        flash(_refused_message(refused), "error")
        return redirect(url_for("import_page", new="1" if new and not good else None, into=into or None))
    return redirect(url_for("workspaces_page" if into else "index"))


@app.route("/use-path", methods=["POST"])
def use_path():
    shared._require_csrf()
    new, into = request.form.get("new") == "1", request.form.get("into", "")
    if into and workspace.load(shared.WS_HOME, into) is None:
        return _gone_into(into)
    label = (request.form.get("name") or "").strip()
    if new and not any(p.strip() for p in request.form.getlist("path")):
        shared.open_workspace(workspace.create(shared.WS_HOME, [], label))   # no path: an empty workspace
        return redirect(url_for("index"))
    good, bad = shared.resolve_submitted_paths(request.form.getlist("path"))
    if bad:
        flash(shared._bad_paths_message(bad), "error")
    good, refused = _readable(good)
    if good:
        shared.adopt_roots(good, label=label, new=new, into=into)
    return _after_import(good, refused, new, into)


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
    """The picker: add runs to the open workspace or the one ``?into=`` names, or start a new one."""
    into = workspace.load(shared.WS_HOME, request.args.get("into", ""))
    if request.args.get("into") and into is None:
        return _gone_into(request.args["into"])
    target = into or shared.STATE["workspace"]
    mode = "new" if request.args.get("new") == "1" or target is None else "add"
    return render_template("runs/setup.html", scanned=False, mode=mode, target=target, into=into,
                           workspaces=workspace.list_all(shared.WS_HOME))


@app.route("/upload", methods=["POST"])
def upload():
    shared._require_csrf()
    if request.form.get("into") and workspace.load(shared.WS_HOME, request.form["into"]) is None:
        return _gone_into(request.form["into"])
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
    new, into = request.form.get("new") == "1", request.form.get("into", "")
    if roots:
        shared.adopt_roots(roots, label=(request.form.get("name") or "").strip() or label,
                    new=new, into=into)
    return _after_import(roots, refused, new, into)


@app.route("/job/<job_id>", methods=["GET", "HEAD"])
def job(job_id):
    # every job, not the one alone: its Delete (_deletable) walks the workspace anyway, and one folder first costs more
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
            return render_template("runs/preparing.html", job_id=job_id, ex=ex, csrf=shared._csrf_token(), own=own, deletable=_deletable(job_id, ex))
        abort(404)
    from . import judge
    board = _live_board_state(j)
    return render_template("runs/job.html", job=j, live_board=board, view=_view_of(board, j),
                           execution=ex, csrf=shared._csrf_token(),
                           judgement=judge.job_summary(j) if j.trials else None,
                           control=feasibility.summary_of(j), ended=execution.ended_for(job_id), own=own, deletable=_deletable(job_id, ex),
                           trial_rows=attempts.grouped(j), pass_k=attempts.for_job(j))


def _runs_of(g, owner=execution.for_job) -> list:
    """The runs Nullius made that hold every row of the launch *g*, and no job
    outside it; [] when a row is not one of theirs. ``owner``: a job's run."""
    ids = g.job_ids + g.pending
    runs = list({id(e): e for e in map(owner, ids)}.values())
    if None in runs or any(not {str(x.get("name")) for x in e.plan.jobs} <= set(ids) for e in runs):
        return []
    return runs


def _undeletable(runs) -> str:
    """Why Delete cannot take these runs, in a few words; '' when it can."""
    if not runs:
        return "no run record"
    if any(e.state in execution.ALIVE for e in runs):
        return "not while it runs"
    try:
        execution.deletable([e.plan.name for e in runs], runs[0].root)
    except (OSError, ValueError):
        return "its run record cannot be read"
    return ""


def _deletion(runs, held=None):
    """What deleting these runs removes, for the question Delete asks; None while they cannot all be deleted."""
    if _undeletable(runs):
        return None
    try:
        return execution.deletion([e.plan.name for e in runs], shared.WS_HOME, runs[0].root, held)
    except (OSError, ValueError):
        return None


def _deletable(job_id, ex):
    """The key of this job's launch when the Runs page offers its Delete, else None."""
    if ex is None or ex.state in execution.ALIVE:
        return None
    g = next((g for g in _grouped()[3] if job_id in g.job_ids + g.pending), None)
    return g.key if g is not None and _deletion(_runs_of(g)) else None


def _run_state(e) -> str:
    """A run record's state in the Runs page's words: ``refused`` when it failed with every job refused before it started."""
    jobs = [str(x.get("name")) for x in e.plan.jobs]
    return "refused" if e.state == "failed" and jobs and all(map(e.refused, jobs)) else _said(e.state)


def _catalogued(recipe, recorded, std) -> tuple:
    """A launch's benchmark and datasets, each a (title, link) pair, from the recipe its runs were compiled
    from: by the catalogue's names, linked to the benchmark's page (a dataset of the standard, ``std``, to its
    Proofs); what the catalogue does not list stays as written, unlinked. A launch with no recipe has only
    the dataset its jobs recorded."""
    cat = shared._catalog_cached()
    ref = str(recipe.get("benchmark") or "")
    b = next((x for x in cat.listed_benchmarks if x.key == ref.split("@")[0]), None)
    bench = (b.title, url_for("benchmark_page", key=b.key)) if b else (ref, None) if ref else None
    ours = {x["id"]: x["label"] for x in (standard_definitions.find(std, ref) or {}).get("datasets", [])} if b else {}
    datasets = []
    wanted = recipe.get("datasets") if isinstance(recipe.get("datasets"), list) else [recipe.get("dataset")]   # as recipe.compile reads them
    for w in (str(w) for w in wanted if w):
        d = next((d for d in cat.datasets_of(b) if w in (d.key, d.registry, d.hub)), None) if b else None
        datasets.append((d.title, url_for("benchmark_page", key=b.key, _anchor=d.key)) if d else
                        (ours[w], url_for("benchmark_page", key=b.key, _anchor="standard")) if w in ours else (w, None))
    if not datasets and recorded and recorded != "mixed":
        datasets.append((shared.dataset_title(recorded), None))
    return bench, datasets


def _held_launches() -> tuple:
    """Every launch Nullius made (``_every``) as the Launches page lists it:
    [(launch, its runs, the workspaces with a root in their folders)];
    ``held``: a record's real folder -> ``workspace.holding`` of it; and
    ``_every``'s job id -> job. The Workspaces page counts each workspace's
    launches from it."""
    recs = sorted(execution.EXECUTIONS.values(), key=lambda e: e.started)
    held = workspace.holding_all(shared.WS_HOME, {os.path.realpath(e.dir) for e in recs})
    owner = {str(x.get("name")): e for e in recs for x in e.plan.jobs}     # the newest run naming a job, as for_job
    by_id, every = _every()
    for j in by_id.values():                                                # a merge beside its shards: their run's
        owner.setdefault(j.job_id, next((owner[m] for m in getattr(j, "merged_from", []) if m in owner), None))
    rows = [(g, runs, list({w.name: w for e in runs for w, _ in held.get(os.path.realpath(e.dir), [])}.values()))
            for g in every for runs in [_runs_of(g, owner.get)]]
    return rows, held, by_id


def _inventory(g, runs, wss, by_id) -> dict:
    """One launch as a row of the Launches page (``_held_launches``' row); what
    its Delete would take is asked only when it opens (``launch_ask``)."""
    return {"launch": g, "runs": runs, "why": _undeletable(runs), "shards": len(runs), "state": _most_telling(map(_run_state, runs)),
            **_tally(g, by_id), "machines": list(dict.fromkeys(e.plan.host.label if e.plan.host else "this machine" for e in runs)),
            "bytes": sum(map(execution.run_size, runs)), "workspaces": wss,
            # the agents its jobs recorded, and those its records name for jobs not written yet
            "agents": sorted({by_id[i].agent_label for i in g.job_ids if by_id[i].agent_label} | {str(x["agent"]) for e in runs for x in e.plan.jobs if x.get("agent")})}


@app.route("/launches")
def launches_page():
    """Every launch Nullius made, in a workspace or not. ``?ws=<name>``: the
    Workspaces filter starts on that workspace alone."""
    wss = workspace.list_all(shared.WS_HOME)
    held_rows, _, by_id = _held_launches()
    rows = [_inventory(g, runs, holders, by_id) for g, runs, holders in held_rows]
    std = standard_definitions.load("benchmark")[0]
    for r in rows:
        r["bench"], r["datasets"] = _catalogued(r["runs"][0].plan.recipe if r["runs"] else {}, r["launch"].dataset, std)
    choices = {"bench": [("", "none")] + sorted({(r["bench"][0], r["bench"][0]) for r in rows if r["bench"]}),
               "dataset": [("", "none")] + sorted({(d, d) for r in rows for d, _ in r["datasets"]}),
               "state": [(s, s) for s in _TELLING if any(r["state"] == s for r in rows)],
               "machines": sorted({(m, m) for r in rows for m in r["machines"]}),
               "ws": [("", "none")] + [(w.name, w.label) for w in wss]}
    only = {"ws": request.args["ws"]} if request.args.get("ws") in {w.name for w in wss} else {}
    return render_template("runs/launches.html", rows=rows, workspaces=wss, choices=choices, telling=_TELLING, only=only)


@app.route("/launch/<key>/ask")
def launch_ask(key):
    """What the Launches page's Delete asks of a launch, in the dialog: the
    Runs page's question, worked out only now; 409 when it cannot be deleted."""
    g = next((g for g in _every()[1] if g.key == key), None)
    if g is None:
        abort(404)
    runs = _runs_of(g)
    gone = _deletion(runs)
    if gone is None:
        return f"Delete launch: {_undeletable(runs) or 'its run record cannot be read'}", 409
    items = [str(x.get("agent") or x.get("name")) + (f" ({len(runs)} shards)" if len(runs) > 1 else "") for x in runs[0].plan.jobs]
    return render_template_string("{% from 'runs/_launch_ask.html' import ask %}{% call ask(L, gone, 'launches') %}"
                                  "{% for i in items %}<li>{{ i }}</li>{% endfor %}{% endcall %}", L=g, gone=gone, items=items)


def _key_here(key) -> str:
    """The key the open workspace's Runs page gives the launch *key* of the
    Launches page: its block keys a launch by the records launched there."""
    g = next((g for g in _every()[1] if g.key == key), None)
    if g is None or shared.STATE["workspace"] is None:
        return key
    ids = set(g.job_ids + g.pending)
    return next((h.key for h in _grouped()[3] if ids & set(h.job_ids + h.pending)), key)


@app.route("/launch/<key>/open")
def launch_open(key):
    """The open workspace's Runs page at the launch *key*."""
    return redirect(url_for("index", view="launch", _anchor=_key_here(key).replace(":", "-")))


@app.route("/launch/<key>/add", methods=["POST"])
def launch_add(key):
    """Add a launch's job roots to the workspace the form names, or, when it
    names none, to a new workspace called the form's ``label`` and opened."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403)
    g = next((g for g in _every()[1] if g.key == key), None)
    name = request.form.get("workspace", "")
    ws = workspace.load(shared.WS_HOME, name) if name else None
    if g is None or (name and ws is None):
        abort(404)
    roots = [e.jobs for e in _runs_of(g) if os.path.isdir(e.jobs)]
    if roots:
        if ws:
            shared.adopt_roots(roots, into=ws.name)
        else:   # adopt_roots(new=True) without a label reopens a workspace that holds these roots; the user asked for a new one
            shared.open_workspace(ws := workspace.create(shared.WS_HOME, roots, request.form.get("label", "").strip()))
        flash(Markup('Added launch <span class="mono">{}</span> to <b>{}</b>.').format(g.title, ws.label), "")
    else:
        flash(Markup('Launch <span class="mono">{}</span> has no runs on this machine.').format(g.title), "error")
    return redirect(url_for("launches_page"))


@app.route("/launch/<key>/delete", methods=["POST"])
def launch_delete(key):
    """Delete a launch that has ended: every run of it (``execution.delete``),
    each shard's copy on its own host when ticked. A launch with a run still
    going, or with a job from elsewhere, is refused. The launch's block on
    the Runs page and the Launches page (``back=launches``: every launch,
    in a workspace or not) ask first, in a dialog."""
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403)
    every = request.form.get("back") == "launches"
    if shared.STATE["workspace"] is None and not every:
        abort(404)
    back = url_for("launches_page") if every else url_for("index", view="launch")
    g = next((g for g in (_every()[1] if every else _grouped()[3]) if g.key == key), None)
    if g is None:
        abort(404)
    runs = _runs_of(g)
    if not runs:
        flash(f"{g.title} has runs that Nullius did not start: it cannot be deleted here.", "error")
        return redirect(back)
    try:
        gone = execution.delete([e.plan.name for e in runs], shared.WS_HOME, runs[0].root, copies=set(request.form.getlist("host")))
    except (OSError, ValueError) as e:
        flash(str(e), "error")
        return redirect(back)
    ws = shared.STATE["workspace"]
    if ws is not None and any(w["ws"].name == ws.name for r in gone["runs"] for w in r["workspaces"]):
        shared.open_workspace(workspace.load(shared.WS_HOME, ws.name))      # read without the runs' roots from now on
    said = Markup('{} launch <span class="mono">{}</span>').format("Deleted part of" if gone["stays"] else "Deleted", g.title)
    flash(said + "".join(f"; {k}" for k in gone["kept"]) + ".", "error" if gone["stays"] else "")
    return redirect(back)


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
    j = shared._job_for_request(job_id)
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
