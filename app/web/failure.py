"""The trial page's User judge tab: why a failed trial failed (agent, model, environment or evaluator), and
whether its task is flawed or ambiguous. A person writes both; they live in the job's ``failure-analysis.json``
(`nullius.runs.failure_analysis`). Beside them, each model judge's own answer to the same two questions
(`failure_label`, `task_flag`) for comparison; a trial judged before those questions shows the judges'
failure_cause as a suggestion instead. Only the person's answers count."""
from __future__ import annotations

import os

from flask import abort, redirect, request, url_for

from nullius.runs import attempts, classify, failure_analysis as fa, parse

from .core import app
from . import judge, shared, traces

# offered in the category box; any other words are accepted
CATEGORIES = ("unreachable", "contradicts a document", "several valid answers", "unseen fact", "exact name",
              "two readings", "depends on simulator")
LABEL_NAMES = {"agent": "Agent", "model": "Model", "environment": "Environment", "evaluator": "Evaluator"}
# outcomes that are not a failure to explain: a pass, a score with no verdict, nothing recorded, a run cancelled
NOT_FAILED = {classify.RESOLVED, classify.SCORED, classify.UNKNOWN, classify.CANCELLED}


def failed(trial) -> bool:
    return trial.outcome not in NOT_FAILED


def _judge_cause(job, trial):
    """The failure_cause of the Decision model, else of the LLM judge, when asked under the current questions."""
    for lane in ("decision", "llm"):
        rec = judge.judgement_of(job, trial, lane) or {}
        if rec.get("questions") == judge.Q.VERSION:
            choice = ((rec.get("answers") or {}).get("failure_cause") or {}).get("choice")
            if choice:
                return choice
    return None


LANES = (("decision", "Decision model judge"), ("llm", "LLM judge"))
FLAG_NAMES = {"flawed": "Flawed", "ambiguous": "Ambiguous", "fair": "Fair", "cannot_tell": "Cannot tell"}


def _models(job, trial) -> list:
    """Each model judge's answers to the User judge's two questions, under the current questions:
    [{name, label, label_p, flag, flag_p}] with a lane left out when it answered neither."""
    out = []
    for lane, name in LANES:
        rec = judge.judgement_of(job, trial, lane) or {}
        if rec.get("questions") != judge.Q.VERSION:
            continue
        answers = rec.get("answers") or {}
        row = {"name": name}
        for q, key in (("failure_label", "label"), ("task_flag", "flag")):
            a = answers.get(q) or {}
            row[key] = a.get("choice")
            row[key + "_p"] = (a.get("probabilities") or {}).get(a.get("choice"))
        if row["label"] or row["flag"]:
            out.append(row)
    return out


def _attempt(job, trial):
    """(this attempt's number, the task's attempts) when the task was attempted more than once, else None: the
    label belongs to the attempt, the flag to every attempt of the task."""
    group = attempts.of(job, trial)
    return (group.number(trial), group.n) if group else None


def _categories(job, analysis) -> list:
    """The categories to suggest: those of the accepted flags on this benchmark, most used first, then the defaults
    not yet among them. The benchmark's other runs are the open workspace's jobs whose flags name a task of this
    one; only their failure-analysis files are read, not the jobs."""
    tasks = {t.task_base for t in job.trials}
    used = [analysis]
    for d in parse.find_job_dirs(shared.STATE["job_roots"]):
        if d == job.dir or not os.path.isfile(fa.path(d)):
            continue
        try:
            other = fa.load(d)
        except fa.FailureAnalysisError:
            continue
        if tasks & set(other.flags):
            used.append(other)
    seen = fa.categories(used)                                     # as written, so new flags spell them alike
    norm = lambda c: c.replace("_", " ").lower()
    same = {norm(c) for c in seen}
    return seen + [c for c in CATEGORIES if norm(c) not in same]


def panel(job, trial) -> dict:
    """What the tab shows for *trial*. Only a browser on this machine gets the forms."""
    try:
        analysis, error = fa.load(job.dir), None
    except fa.FailureAnalysisError as e:
        analysis, error = fa.Analysis(), str(e)
    cause = _judge_cause(job, trial) if failed(trial) else None
    models = _models(job, trial) if trial.outcome != classify.RESOLVED else []   # asked of every unresolved trial
    return {"models": models, "model_labels": [m for m in models if m["label"]],
            "model_flags": [m for m in models if m["flag"]], "flag_names": FLAG_NAMES,
            "attempt": _attempt(job, trial), "failed": failed(trial), "label": analysis.labels.get(trial.trial_name),
            "flag": analysis.flags.get(trial.task_base), "suggests": fa.suggestion(cause),
            "labels": LABEL_NAMES, "kinds": fa.KINDS, "categories": _categories(job, analysis), "error": error,
            "can_write": shared._local_client() and error is None}


@app.route("/failure/<job_id>/<trial_name>", methods=["POST"])
def failure_route(job_id, trial_name):
    """One change from the tab: set or clear the trial's label; set, accept or remove its task's flag. A trial is
    one the job counts: an earlier try of an attempt is evidence only, as for the judges."""
    shared._require_csrf()
    if not shared._local_client():
        abort(403, description="The User judge is written from a browser on the application host only.")
    job, trial = traces._trial_evidence(job_id, trial_name)
    form, action = request.form, request.form.get("action")

    def change(a: fa.Analysis):
        if action == "label":
            if not failed(trial):
                raise fa.FailureAnalysisError("only a failed trial takes a label")
            a.set_label(trial.trial_name, form.get("label", ""), by=fa.who(), note=form.get("note", "").strip(),
                        judge_cause=_judge_cause(job, trial))
        elif action == "clear_label":
            a.clear_label(trial.trial_name)
        elif action == "flag":
            a.set_flag(trial.task_base, form.get("kind", ""), form.get("category", "").strip(), by=fa.who(),
                       evidence=form.get("evidence", "").strip(),
                       task_version=fa.task_versions(job, [trial.task_base]).get(trial.task_base))
        elif action == "accept":
            a.accept(trial.task_base, by=fa.who())
        elif action == "remove_flag":
            a.remove_flag(trial.task_base)
        else:
            raise fa.FailureAnalysisError(f"unknown action {action!r}")

    try:
        fa.update(job.dir, change)
    except fa.FailureAnalysisError as e:
        abort(400, description=str(e))
    return redirect(url_for("trial", job_id=job_id, trial_name=trial_name, tab="failure") + "#failure")
