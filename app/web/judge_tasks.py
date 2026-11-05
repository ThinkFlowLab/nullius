"""Task-instruction quality: the judge's score over a dataset's task text,
once per dataset and task, independent of any run. An ambiguous instruction
caps every agent on that task, which is a harness finding, not an agent one.
`/judge/tasks/<job_id>` (POST) asks the job's unjudged tasks on the user's
click; the score is shown beside the task on the job page and the trial
page, advisory, and changes no outcome."""
from __future__ import annotations

from flask import abort, flash, redirect, url_for

from .core import app
from . import shared


@app.route("/judge/tasks/<job_id>", methods=["POST"])
def judge_tasks_route(job_id):
    """Ask the instruction score of every task of the job that has none, on the click of the button."""
    from . import judge
    job = shared._find_job(shared._jobs_for_request(), job_id)
    if job is None:
        abort(404)
    todo = judge.tasks_todo(job)
    judge._guard()
    k = judge.key()
    done, failed = judge.run_batch(todo, lambda t: judge.judge_task(job, t, k), lambda t: t.task_base)
    flash(f"Judged {done} of {len(todo)} task instruction(s)." + (" Failed: " + "; ".join(failed[:3]) if failed else ""), "error" if failed else "")
    return redirect(url_for("job", job_id=job_id) + "#task-quality")


@app.template_global()
def task_quality_summary(job) -> dict:
    from . import judge
    return judge.task_summary(job)
