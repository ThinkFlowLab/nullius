"""Reconciliation: the classifier's outcome beside the judge's
failure_cause, per trial of a job, from the workspace store - where they
disagree, ranked strongest first, and a suggestion for the trials the
classifier could not place. Reads the store; asks the judge nothing;
writes nothing. Advisory beside the recorded evidence, never in its
place."""
from __future__ import annotations

from flask import abort, render_template, request
from nullius.judge import questions, reconcile, store
from nullius.runs import classify

from .core import app
from . import shared


def ws_dir():
    ws = shared.STATE.get("workspace")
    return ws.dir if ws else None


def threshold_requested() -> float:
    """`?threshold=` as a number in 0..1, else the module's default."""
    raw = request.args.get("threshold")
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return reconcile.THRESHOLD
    return min(1.0, max(0.0, t))


def view_of(job, threshold: float) -> dict:
    d = ws_dir()
    records = store.for_job(d, job.job_id) if d else {}
    return reconcile.reconcile(job, records, threshold, key=lambda name: store._SAFE.sub("_", name))


@app.route("/job/<job_id>/reconcile")
def reconcile_page(job_id):
    j = shared._find_job(shared._jobs_for_request(), job_id)
    if not j:
        abort(404)
    return render_template("runs/reconcile.html", job=j, view=view_of(j, threshold_requested()),
                           STATUSES=reconcile.STATUSES, STATUS_LABELS=reconcile.STATUS_LABELS,
                           COMPATIBLE=reconcile.COMPATIBLE, SUGGESTS=reconcile.SUGGESTS,
                           C_ORDER=classify.ORDER, C_LABELS=classify.LABELS, default_threshold=reconcile.THRESHOLD,
                           QUESTIONS_VERSION=questions.VERSION)
