"""Workspaces: list, open, refresh, rename, delete, and each one's roots: unlink one, delete its folder."""
from __future__ import annotations

import json
import os
from dataclasses import replace

from flask import abort, flash, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from nullius.runs import parse
from nullius import execution, workspace
from nullius.judge import store

from .core import app
from . import shared
from . import runs as runs_web


def _not_a_run(real) -> str:
    """Why the folder *real* is not one a root may be deleted as (``execution.run_of``)."""
    runs = os.path.realpath(execution.runs_root())
    top = os.path.relpath(real, runs).split(os.sep)[0]
    if top == os.pardir:
        return "Outside Nullius's storage"
    return "Inside a run, not a run folder" if os.path.isfile(os.path.join(runs, top, execution.RECORD)) else "Not a run folder"


def _launch_of(name, launched):
    """The Launches page's row (``runs._held_launches``) whose runs hold the run *name*, or None."""
    return next((row for row in launched[0] if any(e.plan.name == name for e in row[1])), None)


def _root(path, launched, memo) -> dict:
    """A root as its Roots dialog lists it: ``run``, the run Nullius made whose
    folder it is (``execution.run_of``), and ``why`` that folder cannot be
    deleted here, '' when it can: its launch has ended and its record reads,
    as Delete launch asks (``execution.deletable``). ``launched``:
    ``runs._held_launches()``. ``memo``: by real path, a folder two roots
    name read once."""
    real = os.path.realpath(path)
    if real not in memo:
        name = execution.run_of(real)
        row = _launch_of(name, launched) if name else None
        if not name:
            why = _not_a_run(real)
        elif row and any(e.state in execution.ALIVE for e in row[1]):
            why = "Not while its launch is going"
        else:
            try:
                execution.deletable([name])
                why = ""
            except json.JSONDecodeError:
                why = "Cannot read its run record"
            except ValueError:
                why = "Not while its run is going"
            except OSError:
                why = "Cannot read its run record"
        memo[real] = {"run": name, "why": why}
    return {**memo[real], "path": path}


def _counts(ws, recs) -> dict:
    """The runs and the checks (nop, oracle) of *ws*, summed over the launches
    its Runs page shows, each counted as there and on the Launches page
    (``runs._tally``). Its roots are walked, each unchanged job read from its
    cache. A closed workspace whose jobs had all ended keeps its counts while
    its roots, their heads (``workspace.heads``) and ``recs``, the run records'
    ids and states, are unchanged."""
    is_open = shared.STATE["workspace"] is not None and shared.STATE["workspace"].name == ws.name
    key = None if is_open else (tuple(ws.roots), tuple(ws.job_roots), workspace.heads(ws.job_roots or ws.roots), recs)
    last = _COUNTS.get(ws.name)
    if key is not None and last and last[0] == key:
        return last[1]
    jobs, _, _, groups = runs_web._grouped(None if is_open else ws)
    by_id = {j.job_id: j for j in jobs}
    tallies = [runs_web._tally(g, by_id) for g in groups]
    counts = {"runs": sum(t["n_runs"] for t in tallies), "checks": sum(t["n_checks"] for t in tallies)}
    if key is not None and all(j.state == "finished" for j in jobs):
        _COUNTS[ws.name] = (key, counts)
    else:
        _COUNTS.pop(ws.name, None)
    return counts


_COUNTS: dict = {}      # workspace name -> (key, _counts) of a workspace whose jobs had all ended


@app.route("/workspaces")
def workspaces_page():
    wss, launched = workspace.list_all(shared.WS_HOME), runs_web._held_launches()
    held = [w.name for _, _, holders in launched[0] for w in holders]
    recs = tuple((e.id, e.state) for e in execution.EXECUTIONS.values())
    return render_template("workspaces/workspaces.html", workspaces=wss, csrf=shared._csrf_token(),
                           counts={ws.name: _counts(ws, recs) for ws in wss}, n_judged={ws.name: store.count(ws.dir) for ws in wss}, n_launches={ws.name: held.count(ws.name) for ws in wss})


@app.route("/workspaces/open/<name>", methods=["POST"])
def workspace_open(name):
    shared._require_csrf()
    ws = workspace.load(shared.WS_HOME, name)
    if ws is None:
        flash(Markup(f'No workspace called <span class="mono">{escape(name)}</span>.'), "error")
        return redirect(url_for("workspaces_page"))
    shared.open_workspace(ws)
    launch = request.form.get("launch")                 # the Launches page: open at that launch
    return redirect(url_for("launch_open", key=launch) if launch else url_for("index"))


@app.route("/workspaces/refresh", methods=["POST"])
def workspace_refresh():
    """Throw the parse cache away and re-convert imported trees. The only way
    a changed *foreign* run tree is noticed; a changed Harbor job is noticed
    by its signature without this."""
    shared._require_csrf()
    ws = shared.STATE["workspace"]
    if ws is None:
        return redirect(url_for("index"))
    shared.open_workspace(ws, refresh=True)
    n = len(parse.find_job_dirs(shared.STATE["job_roots"]))
    flash(Markup(f"Refreshed <span class=\"mono\">{escape(ws.label)}</span> — {n} run(s) "
                 "re-read; imported trees converted again."), "")
    return redirect(request.referrer or url_for("index"))


@app.route("/workspaces/rename", methods=["POST"])
def workspace_rename():
    """Rename the workspace a row names, open or not: its label, and its
    directory moved to the label's slug (``workspace.rename``). The open one
    is read from where it is now, and the runs filed under it are filed
    under its new name. Takes the form token: a rename moves a directory."""
    shared._require_csrf()
    open_ws = shared.STATE["workspace"]
    name = request.form.get("name") or (open_ws.name if open_ws is not None else "")
    label = (request.form.get("label") or "").strip()
    is_open = open_ws is not None and open_ws.name == name
    back = url_for("workspaces_page", view=request.form.get("view") or None)   # the view it was renamed from
    ws = open_ws if is_open else workspace.load(shared.WS_HOME, name)
    if ws is None:
        flash(Markup(f'No workspace called <span class="mono">{escape(name)}</span>.'), "error")
    elif label:
        old = ws.name
        try:
            workspace.rename(shared.WS_HOME, ws, label)
        except OSError as exc:
            flash(Markup(f'<span class="mono">{escape(ws.label)}</span> could not be renamed: {escape(exc.strerror or str(exc))}.'), "error")
            return redirect(back)
        for ex in execution.EXECUTIONS.values():
            if old != ws.name and ex.workspace == old:
                ex.file_under(ws.name)                  # a restart puts the run back in it by this name
        if is_open:
            shared.open_workspace(ws)                   # its imports moved with it: read from where it is now
    return redirect(back)


@app.route("/workspaces/delete/<name>", methods=["POST"])
def workspace_delete(name):
    """Remove a workspace - its cache and imports, never the job roots it
    read. The open one closes first. Takes the form token."""
    shared._require_csrf()
    ws = shared.STATE["workspace"]
    if ws is not None and ws.name == name:
        shared.close_workspace()
    if workspace.delete(shared.WS_HOME, name):
        flash(Markup(f'Removed workspace <span class="mono">{escape(name)}</span>; its job roots are untouched.'), "")
    else:
        flash(Markup(f'No workspace called <span class="mono">{escape(name)}</span>.'), "error")
    return redirect(url_for("workspaces_page"))


def _named_root(name):
    """The workspace *name* and the root the form names, one of its own; 404 otherwise."""
    shared._require_csrf()
    ws = workspace.load(shared.WS_HOME, name)
    root = request.form.get("root", "")
    if ws is None or root not in ws.roots:
        abort(404)
    return ws, root


def _reopen(names) -> None:
    """The open workspace read again when it is one of *names*: without the roots it lost."""
    ws = shared.STATE["workspace"]
    if ws is not None and ws.name in names:
        shared.open_workspace(workspace.load(shared.WS_HOME, ws.name))


@app.route("/workspaces/<name>/roots/unlink", methods=["POST"])
def workspace_unlink(name):
    """Take a root out of one workspace, with its parsed runs; its files stay."""
    ws, root = _named_root(name)
    workspace.forget(ws, [root], parse.find_job_dirs([root]))
    _reopen([ws.name])
    flash(Markup('Unlinked <span class="mono">{}</span> from <b>{}</b>; its files stay.').format(root, ws.label), "")
    return redirect(url_for("workspaces_page"))


@app.route("/workspaces/<name>/roots")
def workspace_roots(name):
    """The Roots dialog of one workspace, fetched when it opens: each root as
    ``_root`` reads it, with why its folder cannot be deleted here."""
    ws = workspace.load(shared.WS_HOME, name)
    if ws is None:
        abort(404)
    launched, memo = runs_web._held_launches(), {}
    clash = [w.label for w in workspace.list_all(shared.WS_HOME)].count(ws.label) > 1
    return render_template("workspaces/_roots.html", ws=ws, rs=[_root(r, launched, memo) for r in ws.roots], clash=clash)


@app.route("/workspaces/<name>/roots/ask")
def workspace_root_ask(name):
    """What the Roots dialog's Delete asks of the root ``?root=``, worked out
    only when it opens: the folder, its runs and checks (``runs._tally``, as
    the Launches page counts that run), its size and judgements, the other
    workspaces that lose it. 409 with the reason when it cannot be deleted."""
    ws, root = workspace.load(shared.WS_HOME, name), request.args.get("root", "")
    if ws is None or root not in ws.roots:
        abort(404)
    launched = runs_web._held_launches()
    info = _root(root, launched, {})
    if info["why"]:
        return f"Delete folder: {info['why']}", 409
    row = _launch_of(info["run"], launched)
    try:
        gone = execution.deletion([info["run"]], shared.WS_HOME, holding=lambda p: launched[1].get(p, []))
    except (OSError, ValueError):
        return "Delete folder: Cannot read its run record", 409
    if row:   # this run's own part of its launch, counted as the Launches page counts a launch
        g, runs, _ = row
        mine = {str(x.get("name")) for e in runs if e.plan.name == info["run"] for x in e.plan.jobs}
        own = replace(g, job_ids=[i for i in g.job_ids if i in mine], pending=[i for i in g.pending if i in mine])
        tally = runs_web._tally(own, launched[2])
    else:
        tally = {"n_runs": 0, "n_checks": 0}
    clash = [w.label for w in workspace.list_all(shared.WS_HOME)].count(ws.label) > 1
    return render_template("workspaces/_root_ask.html", ws=ws, root=root, gone=gone, clash=clash, **tally)


@app.route("/workspaces/<name>/roots/delete", methods=["POST"])
def workspace_root_delete(name):
    """Delete a root's folder from the disk: only a run Nullius made, that has
    ended (``_root``), as its launch's Delete does - its roots in every
    workspace, parsed runs and judgements go with it."""
    ws, root = _named_root(name)
    if not shared._local_client():
        abort(403)
    info = _root(root, runs_web._held_launches(), {})
    if info["why"]:
        flash(Markup('<span class="mono">{}</span> stays: {}.').format(root, info["why"].lower()), "error")
        return redirect(url_for("workspaces_page"))
    try:
        gone = execution.delete([info["run"]], shared.WS_HOME, copies=set(request.form.getlist("host")))
    except (OSError, ValueError) as e:
        flash(str(e), "error")
        return redirect(url_for("workspaces_page"))
    _reopen({w["ws"].name for r in gone["runs"] for w in r["workspaces"]})
    said = Markup('{} <span class="mono">{}</span>').format("Could not delete" if gone["stays"] else "Deleted", gone["runs"][0]["dir"])
    flash(said + "".join(f"; {k}" for k in gone["kept"]) + ".", "error" if gone["stays"] else "")
    return redirect(url_for("workspaces_page"))
