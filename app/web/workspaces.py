"""Workspaces: list, open, refresh, rename, delete."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from nullius.runs import parse
from nullius import workspace

from .core import app
from . import shared



@app.route("/workspaces")
def workspaces_page():
    return render_template("workspaces/workspaces.html", workspaces=workspace.list_all(shared.WS_HOME),
                           home=shared.WS_HOME, csrf=shared._csrf_token())


@app.route("/workspaces/open/<name>", methods=["POST"])
def workspace_open(name):
    shared._require_csrf()
    ws = workspace.load(shared.WS_HOME, name)
    if ws is None:
        flash(Markup(f'No workspace called <span class="mono">{escape(name)}</span>.'), "error")
        return redirect(url_for("workspaces_page"))
    shared.open_workspace(ws)
    return redirect(url_for("index"))


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
    is read from where it is now. Takes the form token: a rename moves a
    directory."""
    shared._require_csrf()
    open_ws = shared.STATE["workspace"]
    name = request.form.get("name") or (open_ws.name if open_ws is not None else "")
    label = (request.form.get("label") or "").strip()
    is_open = open_ws is not None and open_ws.name == name
    ws = open_ws if is_open else workspace.load(shared.WS_HOME, name)
    if ws is None:
        flash(Markup(f'No workspace called <span class="mono">{escape(name)}</span>.'), "error")
    elif label:
        try:
            workspace.rename(shared.WS_HOME, ws, label)
        except OSError as exc:
            flash(Markup(f'<span class="mono">{escape(ws.label)}</span> could not be renamed: {escape(exc.strerror or str(exc))}.'), "error")
            return redirect(url_for("workspaces_page"))
        if is_open:
            shared.open_workspace(ws)                   # its imports moved with it: read from where it is now
    return redirect(url_for("workspaces_page"))


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
