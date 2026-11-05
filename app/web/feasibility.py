"""The feasibility report of a run: what its nop and oracle controls said,
task by task. Read from recorded jobs; runs nothing.

Beside each side that did not behave, the judge's triage
(`control_failure_cause`, advisory): why the control misbehaved, over that
control's own small state. The verdict is `feasibility.verdict`'s and is
never touched; `/judge/feasibility/<job_id>` asks the unjudged sides on the
user's click (POST)."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, url_for
from nullius.judge import client
from nullius.runs import feasibility

from .core import app
from . import shared


@app.route("/job/<job_id>/feasibility")
def feasibility_page(job_id):
    from . import judge
    jobs = shared._jobs_for_request()
    j = shared._find_job(jobs, job_id)
    if not j:
        abort(404)
    report = feasibility.report(j, jobs)
    return render_template("runs/feasibility.html", job=j, report=report,
                           triage=judge.triage_summary(report, jobs), csrf=shared._csrf_token(),
                           F_ORDER=feasibility.ORDER, F_LABELS=feasibility.LABELS, F_DESCRIPTIONS=feasibility.DESCRIPTIONS)


@app.route("/judge/feasibility/<job_id>", methods=["POST"])
def judge_feasibility_route(job_id):
    """Ask why each misbehaving control side misbehaved, for every side not yet judged, on the click of the button."""
    from . import judge
    jobs = shared._jobs_for_request()
    j = shared._find_job(jobs, job_id)
    if not j:
        abort(404)
    report = feasibility.report(j, jobs)
    summary = judge.triage_summary(report, jobs)
    todo = summary["todo"]
    judge._guard()
    done, failed = 0, []
    for row, side in todo:
        try:
            judge.judge_control(side, jobs, judge.key()); done += 1
        except client.JudgeError as e:
            failed.append(f"{row.task_base} ({side.control}): {e}")
            if e.status in (401, 0):
                break
    flash(f"Judged {done} of {len(todo)} control side(s)." + (" Failed: " + "; ".join(failed[:3]) if failed else ""), "error" if failed else "")
    return redirect(url_for("feasibility_page", job_id=job_id) + "#triage")


@app.template_global()
def feasibility_controls(job) -> list:
    """The controls recorded with a job, for the link the job page shows
    only when there is a report to read — empty when neither control is."""
    return sorted(feasibility.related(job, shared._jobs_for_request()))
