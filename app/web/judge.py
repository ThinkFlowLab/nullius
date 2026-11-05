"""Judgement: asking the judge about a trial, a job's trials, or matched
pairs; what the pages show comes from the workspace store. Advisory beside
the recorded evidence, never in its place."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import abort, jsonify, redirect, request, url_for
from nullius.judge import batch, client, lanes, questions as Q, state as judge_state, store
from nullius.runs import feasibility
from nullius.traces import trajectory

from .core import app
from . import shared
from . import settings as settings_web
from . import traces

def key(lane: str = "system-one") -> client.Judge:
    """A judgement's judge, with its key and model: true when it can be
    asked. System One unless the LLM judgement is named; passed down to
    every call as the key is."""
    return client.judge_for(lane, os.environ)


# Judge calls in flight at once for a batch (Judge all, a comparison's sides, task instructions). One
# at a time, a 500-trial job at 2-5 s a call took most of an hour inside the click's request.
CONCURRENCY = 8


def run_batch(items, ask, name) -> tuple:
    """``ask(item)`` for every item, ``CONCURRENCY`` at a time; (how many were
    judged, the failures named by ``name(item)``), in the items' order. A
    refused key or a judge that cannot be asked (status 401, 0), a spent
    credit (402), or a judge still busy after its retries (429, 503, 529)
    starts no further call: every other one would fail the same way, and a
    batch going on through a rate limit only lengthens it."""
    stop = threading.Event()

    def one(item):
        if stop.is_set():
            return None
        try:
            ask(item)
            return True
        except client.JudgeError as e:
            if e.status in (401, 0, 402, 429, 503, 529):
                stop.set()
            return f"{name(item)}: {e}"
    done, failed = 0, []
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for r in pool.map(one, items):
            if r is True:
                done += 1
            elif r:
                failed.append(r)
    return done, failed


def ws_dir():
    ws = shared.STATE.get("workspace")
    return ws.dir if ws else None


def judgement_of(job, trial, lane: str = "system-one") -> dict | None:
    d = ws_dir()
    return store.load(d, job.job_id, trial.trial_name, lane=lane) if d else None


def panel(job, trial, lane: str = "system-one") -> dict:
    """What the trial page renders: each question with its answer or 'not
    judged', the record's provenance, whether a Judge action is possible.
    A record asked under an earlier vocabulary is shown under its own
    version and marked `earlier`; Judge again asks the current one."""
    rec = judgement_of(job, trial, lane) or {}
    ids = Q.for_trial(trial.outcome == "resolved", trial.outcome)
    answers = rec.get("answers") or {}
    hygiene = rec.get("hygiene") or {}
    rows = [row_of(q, answers.get(q), hygiene.get(Q.state_of(q))) for q in ids]
    flags = [r for r in rows if r["flag"]]
    sections = [("Outcome", [r for r in rows if not r["flag"]]), ("Behaviour flags", flags)]
    return {"rows": rows, "sections": [(name, rs) for name, rs in sections if rs], "record": rec, "judged": bool(answers), "keyed": bool(key(lane)), "lane": lane, "judge": key(lane),
            "cards": [r for r in rows if not r["flag"]], "flags": flags,
            "earlier": bool(answers) and not store.is_current(rec),
            "raised": [r["id"] for r in flags if r["raised"] and not r["undecided"]], "undecided": [r["id"] for r in flags if r["undecided"]],
            "cleared": [r["id"] for r in flags if r["answer"] and not r["raised"] and not r["undecided"]],
            "flags_judged": sum(1 for r in flags if r["answer"]), "floors_measured": bool(Q.floors().get("floors")),
            "hygiene": hygiene_summary(hygiene)}


FLAG_THRESHOLD = 0.5   # a Noul at or above this reads as raised; a threshold for Nouls only, never carried to a Choice or Score


def row_of(q: str, a, hygiene: dict | None = None) -> dict:
    """One question's row: the answer, and the judge's own `confidence`
    (Choice and Score) or Nullius's `decisiveness` (Noul) - never both.
    A behaviour flag (`flag` in the vocabulary) is `raised` when its Noul
    is at or above `FLAG_THRESHOLD`. `hygiene` is the reading over the
    state this question read; `warnings` names the hygiene flags raised on
    it, shown beside the answer and changing nothing about it."""
    kind = Q.QUESTIONS[q]["type"]
    flag = bool(Q.QUESTIONS[q].get("flag"))
    floor = Q.floor_of(q)
    undecided = is_undecided(kind, a, floor)
    warnings = hygiene_raised(hygiene)
    row = {"floor": floor, "undecided": undecided, "id": q, "type": kind, "instructions": Q.QUESTIONS[q]["instructions"], "answer": a,
           "label": Q.QUESTIONS[q].get("label") or q.replace("_", " "), "blurb": Q.QUESTIONS[q].get("blurb") or "",
           "confidence": confidence_of(kind, a), "decisiveness": decisiveness_of(kind, a),
           "flag": flag, "raised": is_raised(kind, a) if flag else False,
           "state": Q.state_of(q), "warnings": warnings, "tone": tone_of(q, kind, flag, a, undecided, warnings)}
    row.update(headline_of(row))
    return row


def headline_of(row: dict) -> dict:
    """What a card or a result cell shows for one answer: the `headline`
    number, its `unit`, the `pill` (Yes / No / Undecided / Raised / Clear /
    the score's level label) and the `measure` beside it (the judge's
    confidence, or Nullius's decisiveness for a yes/no). Nothing here is a
    new number: it is the answer, formatted."""
    a, kind = row["answer"], row["type"]
    if not a:
        return {"headline": None, "unit": "", "pill": "Not judged", "measure_label": "", "measure": None}
    if kind == "noul":
        p = a.get("noul") or 0.0
        pill = "Undecided" if row["undecided"] else (("Raised" if p >= FLAG_THRESHOLD else "Clear") if row["flag"] else ("Yes" if p >= FLAG_THRESHOLD else "No"))
        return {"headline": f"{p * 100:.0f}", "unit": "%", "pill": pill, "measure_label": "Decisiveness", "measure": row["decisiveness"]}
    if kind == "score":
        legend = a.get("legend") or list(Q.QUESTIONS[row["id"]]["criteria"])
        level = min(int((a.get("score") or 0) + 0.5), len(legend) - 1)
        return {"headline": f"{a.get('score') or 0:.1f}", "unit": f"/ {len(legend) - 1}", "pill": legend[level], "measure_label": "Confidence", "measure": row["confidence"]}
    p = (a.get("probabilities") or {}).get(a.get("choice"))
    return {"headline": f"{(p or 0) * 100:.0f}" if p is not None else None, "unit": "%", "pill": a.get("choice") or "", "measure_label": "Confidence", "measure": row["confidence"]}


def tone_of(q: str, kind: str, flag: bool, a, undecided: bool, warnings) -> str:
    """The row's background: `good` when the answer reads well for the
    agent (a flag clear, a yes on an outcome question, a top score), `bad`
    when it reads badly (a flag raised, a no, a bottom score), `warn` when
    it is undecided within the judge's spread, a middle score, or drawn
    from a state the hygiene reading flagged; `none` with no answer, and for
    a Choice, whose options are not ranked. Advisory colour, nothing else."""
    if not a:
        return "none"
    if undecided:
        return "warn"
    if kind == "noul":
        p = a.get("noul")
        if not isinstance(p, (int, float)):
            return "none"
        yes = p >= FLAG_THRESHOLD
        tone = ("bad" if yes else "good") if flag else ("good" if yes else "bad")
    elif kind == "score":
        legend = a.get("legend") or list(Q.QUESTIONS[q]["criteria"])
        level = min(int((a.get("score") or 0) + 0.5), len(legend) - 1)
        tone = "good" if level == len(legend) - 1 else ("bad" if level == 0 else "warn")
    else:
        tone = "none"
    return "warn" if warnings and tone == "none" else tone


# --- hygiene: the state is untrusted text, and the judge read it as such -------------

HYGIENE_THRESHOLD = 0.5   # a hygiene Noul at or above this is a warning; its own constant, shared with no other question type or flag

HYGIENE_LABELS = {"addresses_the_judge": "text addresses the judge", "credential_visible": "a credential may still be visible"}


def hygiene_raised(reading: dict | None) -> list:
    """The hygiene flags raised on one state kind's reading: [(id, label,
    p, decided)] - `decided` False when p sits within the judge's measured
    spread of the threshold (`floors.json`), so the warning says it is
    undecided rather than certain. Without a measured floor every raised
    flag is decided."""
    out = []
    for q in Q.hygiene_ids():
        p = (reading or {}).get(q)
        if isinstance(p, (int, float)) and p >= HYGIENE_THRESHOLD:
            floor = Q.floor_of(q)
            out.append((q, HYGIENE_LABELS.get(q, q), p, not (isinstance(floor, (int, float)) and abs(p - HYGIENE_THRESHOLD) < floor)))
    return out


def hygiene_summary(hygiene: dict) -> dict:
    """For the panel head: which state kinds were read, and on which a
    flag is raised - `{kinds: [...], flagged: {kind: [(id, label, p)]}}`."""
    flagged = {kind: hygiene_raised(r) for kind, r in hygiene.items()}
    return {"kinds": list(hygiene), "flagged": {k: v for k, v in flagged.items() if v}}


def is_undecided(kind: str, a, floor) -> bool:
    """A yes/no answer whose probability sits within the judge's measured
    run-to-run spread of the 50% line: another ask could land on the other
    side, so it is shown as undecided rather than raised or clear. Without
    a measured floor nothing is undecided - and the row says no floor was
    measured."""
    if not a or kind != "noul" or not isinstance(floor, (int, float)):
        return False
    p = a.get("noul")
    return isinstance(p, (int, float)) and abs(p - FLAG_THRESHOLD) < floor


def is_raised(kind: str, a) -> bool:
    """Whether a yes/no answer reads as yes: its probability at or above the threshold."""
    if not a or kind != "noul":
        return False
    p = a.get("noul")
    return isinstance(p, (int, float)) and p >= FLAG_THRESHOLD


def confidence_of(kind: str, a) -> float | None:
    """The confidence the judge returned: Choice and Score carry one, a Noul does not."""
    if not a or kind == "noul":
        return None
    return a.get("confidence")


def decisiveness_of(kind: str, a) -> float | None:
    """How far a Noul's probability sits from 0.5, scaled to 0..1 - derived
    here, not returned by the judge, and not a confidence: 0.9 and 0.1 are
    equally decisive, and a decisive answer can still be wrong."""
    if not a or kind != "noul":
        return None
    p = a.get("noul")
    return abs(p - 0.5) * 2 if isinstance(p, (int, float)) else None


def _states_for(trial, traj, ids) -> dict:
    """The questions grouped by the state they read, each state built once."""
    return batch.states_for(ids, lambda kind: judge_state.build(kind, traj, trial=trial, trial_dir=trial.dir))


def _trial_ids_and_states(trial, traj) -> dict:
    """Everything one Judge click asks of a trial, grouped by state: the
    trial page's questions."""
    ids = Q.for_trial(trial.outcome == "resolved", trial.outcome)
    return _states_for(trial, traj, ids)


def judge_trial(job, trial, api_key: str, post=None) -> dict:
    """Ask every question the trial page shows, one call per state, and keep
    the merged record. Returns the record as stored."""
    if not trial.has_trajectory:
        raise client.JudgeError(0, "this trial has no trajectory to judge")
    traj = trajectory.load(trial.trajectory_path)
    if traj.error:
        raise client.JudgeError(0, "the trajectory could not be read: " + traj.error)
    return batch.ask_and_store(ws_dir(), job.job_id, trial.trial_name, _trial_ids_and_states(trial, traj), api_key, post=post)


def _guard():
    shared._require_csrf()
    if not settings_web._local_client():
        abort(403, description="Judging is asked from a browser on the application host only: it is a paid call.")
    if ws_dir() is None:
        abort(400, description="No workspace is open; judgements live in the workspace.")


@app.route("/judge/trial/<job_id>/<trial_name>", methods=["POST"])
def judge_trial_route(job_id, trial_name):
    """One click, one trial: the page's questions asked and stored."""
    _guard()
    job, trial = traces._trial_evidence(job_id, trial_name)
    lane = request.form.get("lane") if request.form.get("lane") in client.LANES else "system-one"
    try:
        rec = judge_trial(job, trial, key(lane))
    except client.JudgeError as e:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error=str(e)), 502 if e.status not in (401, 422) else 400
        from flask import flash
        flash(str(e), "error")
        return redirect(url_for("trial", job_id=job_id, trial_name=trial_name))
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, record=rec)
    tab = "llm-judge" if lane == "llm" else "judgement"
    return redirect(url_for("trial", job_id=job_id, trial_name=trial_name, tab=tab) + "#" + tab)


@app.route("/judge/job/<job_id>", methods=["POST"])
def judge_job_route(job_id):
    """Judge the job's unjudged trials with one judge, the form's ``lane``
    (System One unless it names the LLM judge): the click is the request."""
    job = shared._find_job(shared._jobs_for_request(), job_id)
    if job is None:
        abort(404)
    lane = request.form.get("lane") if request.form.get("lane") in client.LANES else "system-one"
    d = ws_dir()
    judged = store.for_job(d, job_id, lane=lane) if d else {}
    todo = [t for t in job.trials if t.has_trajectory and store._SAFE.sub("_", t.trial_name) not in judged]
    _guard()
    k = key(lane)
    done, failed = run_batch(todo, lambda t: judge_trial(job, t, k), lambda t: t.trial_name)
    from flask import flash
    flash(f"{'LLM judge' if lane == 'llm' else 'System One'}: judged {done} of {len(todo)} trial(s)." + (" Failed: " + "; ".join(failed[:3]) if failed else ""), "error" if failed else "")
    return redirect(url_for("job", job_id=job_id) + "#judgement")


def job_summary(job) -> dict:
    """For the job page: the failure_cause distribution over judged trials,
    how many trials raised each behaviour flag (counted here, over the
    judged trials only), how many are judged at all, and the same reading
    per trial (`by_trial`) so a chip's count is exactly the rows its filter
    shows. `flag_ids` is every flag in the vocabulary's order - a flag's
    number on the page is its place here, raised or not - and `lanes` is
    the board's record -> judge lanes (`nullius.judge.lanes`), whose status
    for each trial is also in `by_trial`."""
    d = ws_dir()
    judged = store.for_job(d, job.job_id) if d else {}
    flag_ids = [q for q in Q.group_ids("trial") if Q.QUESTIONS[q].get("flag")]
    board = lanes.board(job, judged, key=lambda name: store._SAFE.sub("_", name))
    by_trial = {}
    for t in job.trials:
        rec = judged.get(store._SAFE.sub("_", t.trial_name))
        if rec is None:
            continue
        answers = rec.get("answers") or {}
        a = answers.get("failure_cause")
        by_trial[t.trial_name] = {
            "cause": a["choice"] if a and a.get("choice") else None,
            # raised outside the judge's measured spread; an undecided one is not counted
            "flags": [q for q in flag_ids if is_raised(Q.QUESTIONS[q]["type"], answers.get(q)) and not is_undecided(Q.QUESTIONS[q]["type"], answers.get(q), Q.floor_of(q))],
            "status": board["status"].get(t.trial_name) if board else None,
        }
    causes, raised = {}, {}
    for r in by_trial.values():
        if r["cause"]:
            causes[r["cause"]] = causes.get(r["cause"], 0) + 1
        for q in r["flags"]:
            raised[q] = raised.get(q, 0) + 1
    return {"judged": len(judged), "trials": len(job.trials), "unjudged": len([t for t in job.trials if store._SAFE.sub("_", t.trial_name) not in judged]),
            "causes": sorted(causes.items(), key=lambda kv: -kv[1]), "n_cause": sum(causes.values()), "keyed": bool(key()),
            "flags": sorted(raised.items(), key=lambda kv: (-kv[1], kv[0])), "by_trial": by_trial,
            "truncation": store.truncation_report(d, job.job_id) if d and judged else None,
            "unresolved": sum(1 for t in job.trials if t.outcome != "resolved"),
            "flag_ids": flag_ids, "lanes": board, "llm": _llm_count(d, job)}


def _llm_count(d, job) -> dict:
    """The LLM judge's own count for the job's button: it judges apart from
    System One, so a trial judged by one is still unjudged by the other."""
    judged = store.for_job(d, job.job_id, lane="llm") if d else {}
    return {"unjudged": len([t for t in job.trials if store._SAFE.sub("_", t.trial_name) not in judged]), "keyed": bool(key("llm"))}


# --- the compare page: each side scored alone, the pick made in code ----------------

SIDE_QUESTION = "task_satisfaction"
LEGACY_QUESTION = "better_of_two"          # v1 only: a pair state, a pick by the model; shown, never asked


def pairs_of(view) -> list:
    """The matched pairs a two-run comparison can judge: (task, trial A, trial B),
    both with a trajectory. Empty for anything but two runs."""
    if len(view.jobs) != 2:
        return []
    out = []
    for row in view.tasks:
        a, b = (list(row.trials) + [None, None])[:2]
        if a and b and a.has_trajectory and b.has_trajectory:
            out.append((row.task, a, b))
    return out


def side_ids(trial) -> list:
    """What judging one side asks: the pair group's questions over the trial
    state, and - free on the same call - everything the trial page asks."""
    own = [q for q in Q.group_ids("pair") if Q.state_of(q) == "trial"]
    return own + [q for q in Q.for_trial(trial.outcome == "resolved", trial.outcome) if q not in own]


def side_of(job, trial) -> dict | None:
    """One side's score as the compare page shows it, from the trial's own
    current record: the expectation, the level it rounds to, the judge's
    confidence, the provenance. None until scored under the current vocabulary."""
    d = ws_dir()
    rec = store.load(d, job.job_id, trial.trial_name) if d else None
    if not rec or not store.is_current(rec):
        return None
    a = (rec.get("answers") or {}).get(SIDE_QUESTION)
    if not a or not isinstance(a.get("score"), (int, float)):
        return None
    legend = a.get("legend") or list(Q.QUESTIONS[SIDE_QUESTION]["criteria"])
    level = min(int(a["score"] + 0.5), len(legend) - 1)
    return {"score": a["score"], "top": len(legend) - 1, "label": legend[level], "confidence": a.get("confidence"),
            "asked_at": rec.get("asked_at"), "model": rec["judge"]["model"]}


def pair_records(view) -> dict:
    """{task: record} for the pairs judged under v1 - the pair state, the
    model's own pick - under job A in the open workspace. Read, never written."""
    d = ws_dir()
    if d is None or len(view.jobs) != 2:
        return {}
    out = {}
    for task, a, b in pairs_of(view):
        rec = store.load(d, view.jobs[0].job_id, store.pair_key(a.trial_name, b.trial_name))
        if rec and (rec.get("answers") or {}).get(LEGACY_QUESTION):
            out[task] = rec
    return out


def compare_summary(view) -> dict | None:
    """For the compare page, per matched task: each side's score, the pick
    made in code (`comparison.better_of`) with ties explicit; a v1 pair
    record shown under its version, marked earlier, where no side is scored
    under v2; the judged-pair population; the unjudged count."""
    if len(view.jobs) != 2:
        return None
    from nullius.compare import comparison
    pairs = pairs_of(view)
    legacy = pair_records(view)
    rows, judged, earlier = {}, 0, 0
    for task, a, b in pairs:
        sa, sb = side_of(view.jobs[0], a), side_of(view.jobs[1], b)
        if sa or sb:
            verdict = comparison.better_of(sa and sa["score"], sb and sb["score"])
            rows[task] = {"a": sa, "b": sb, "verdict": verdict, "earlier": False}
            judged += bool(verdict)
        elif task in legacy:
            ans = legacy[task]["answers"][LEGACY_QUESTION]
            rows[task] = {"a": None, "b": None, "verdict": None, "earlier": True, "choice": ans["choice"], "p": (ans.get("probabilities") or {}).get(ans["choice"]),
                          "probabilities": ans.get("probabilities") or {}, "questions": legacy[task].get("questions"), "asked_at": legacy[task].get("asked_at"), "model": legacy[task]["judge"]["model"]}
            earlier += 1
    return {"rows": rows, "pairs": len(pairs), "judged": judged, "earlier": earlier, "unjudged": len(pairs) - judged, "keyed": bool(key()),
            "margin": comparison.TIE_MARGIN, "question": SIDE_QUESTION}


def judge_side(job, trial, api_key: str, post=None) -> dict | None:
    """Score one side: the questions it still lacks under the current
    vocabulary, one call per state, merged over the trial's own record -
    the same record the trial page shows. Nothing when it has them all."""
    if not trial.has_trajectory:
        raise client.JudgeError(0, "this trial has no trajectory to judge")
    d = ws_dir()
    rec = store.load(d, job.job_id, trial.trial_name)
    have = (rec.get("answers") or {}) if store.is_current(rec) else {}
    ids = [q for q in side_ids(trial) if q not in have]
    if not ids:
        return rec
    traj = trajectory.load(trial.trajectory_path)
    if traj.error:
        raise client.JudgeError(0, "the trajectory could not be read: " + traj.error)
    return batch.ask_and_store(d, job.job_id, trial.trial_name, _states_for(trial, traj, ids), api_key, post=post)


def _sides_todo(view) -> list:
    """(job, trial) for every side of an unjudged pair that lacks its score."""
    out = []
    for task, a, b in pairs_of(view):
        for job, t in ((view.jobs[0], a), (view.jobs[1], b)):
            if side_of(job, t) is None:
                out.append((task, job, t))
    return out


@app.route("/judge/compare", methods=["POST"])
def judge_compare_route():
    """Judge the unjudged pairs of this comparison - every side that lacks
    its score - on the click of the button."""
    from . import compare as compare_web
    view = compare_web._comparison_requested()
    if len(view.jobs) != 2:
        abort(400, description="A judged comparison needs exactly two runs.")
    sides = _sides_todo(view)
    todo = list(dict.fromkeys(task for task, _, _ in sides))
    _guard()
    k = key()
    done, failed = run_batch(sides, lambda s: judge_side(s[1], s[2], k), lambda s: f"{s[0]} ({s[1].agent_label or s[1].job_id})")
    from flask import flash
    flash(f"Judged {done} of {len(sides)} side(s) over {len(todo)} pair(s)." + (" Failed: " + "; ".join(failed[:3]) if failed else ""), "error" if failed else "")
    return redirect(url_for("compare", pick=view.references, cohort=view.cohort) + "#judgement")


# --- feasibility triage, task-instruction quality
# Each over a small, purpose-built state (`state.control_state`,
# `task_state`); every count is code's; every answer is
# advisory beside the number, the verdict or the outcome it sits next to.

def _choice_row(q: str, a) -> dict:
    """A Choice's answer as a page shows it: the pick, its probability, the judge's confidence."""
    row = row_of(q, a)
    if a and a.get("choice"):
        row["p"] = (a.get("probabilities") or {}).get(a["choice"])
    return row


# -- feasibility triage: the control sides that did not behave -------------------------

def triage_sides(report) -> list:
    """(task_row, side) for every side that did not behave, with a trial to
    read: nop accepted or breaking tests, oracle rejected, either failed
    before the verifier. Missing sides have nothing to judge."""
    out = []
    for row in report.rows:
        if row.verdict == feasibility.FEASIBLE:
            continue
        for s in (row.nop, row.oracle):
            if not s.present:
                continue
            misbehaved = s.failed or s.broke_tests or (s.accepted if s.control == feasibility.NOP else s.rejected)
            if misbehaved:
                out.append((row, s))
    return out


def _control_trial(side, jobs):
    job = next((j for j in jobs if j.job_id == side.job_id), None)
    trial = next((t for t in job.trials if t.trial_name == side.trial_name), None) if job else None
    return job, trial


def control_states(side, trial) -> dict:
    traj = None
    if trial is not None and trial.has_trajectory:
        traj = trajectory.load(trial.trajectory_path)
        if traj.error:
            traj = None
    build = lambda kind: judge_state.build(kind, traj, trial=trial, trial_dir=trial.dir if trial else None, control=side.control, outcome_label=side.label)
    return batch.states_for(Q.group_ids("feasibility"), build)


def judge_control(side, jobs, api_key: str, post=None) -> dict | None:
    """Ask why one control side did not behave, over its own `control` state,
    stored as that control trial's record. The verdict is not touched."""
    job, trial = _control_trial(side, jobs)
    if trial is None:
        raise client.JudgeError(0, f"{side.control} trial {side.trial_name} is not in this workspace")
    return batch.ask_and_store(ws_dir(), job.job_id, trial.trial_name, control_states(side, trial), api_key, post=post)


def triage_summary(report, jobs) -> dict:
    """For the feasibility page: per task and side the judged cause, the
    counts, and what judging the rest would cost."""
    d = ws_dir()
    sides = triage_sides(report)
    rows, judged, todo = {}, 0, []
    for row, s in sides:
        rec = store.load(d, s.job_id, s.trial_name) if d else None
        a = ((rec.get("answers") or {}).get("control_failure_cause")) if store.is_current(rec) else None
        if a:
            rows.setdefault(row.task_base, {})[s.control] = _choice_row("control_failure_cause", a)
            judged += 1
        else:
            todo.append((row, s))
    return {"rows": rows, "sides": len(sides), "judged": judged, "unjudged": len(todo), "todo": todo, "keyed": bool(key()),
            "question": Q.QUESTIONS["control_failure_cause"]["instructions"] if "control_failure_cause" in Q.QUESTIONS else ""}


# -- task-instruction quality: once per dataset and task ---------------------------------

def task_key(job) -> str:
    """The store's job slot for a dataset's task judgements: `task:<dataset>`,
    so an instruction is judged once for every run of that dataset."""
    return "task:" + (job.dataset or "unknown")


def task_record(job, task_base: str) -> dict | None:
    d = ws_dir()
    rec = store.load(d, task_key(job), task_base) if d else None
    return rec if store.is_current(rec) else None


@app.template_global()
def task_quality_of(job, trial) -> dict | None:
    """The instruction's score for a trial's task: the expectation, the level
    it rounds to (display only), the judge's confidence; None until judged."""
    rec = task_record(job, trial.task_base)
    a = (rec.get("answers") or {}).get("instruction_quality") if rec else None
    if not a or not isinstance(a.get("score"), (int, float)):
        return None
    legend = a.get("legend") or list(Q.QUESTIONS["instruction_quality"]["criteria"])
    level = min(int(a["score"] + 0.5), len(legend) - 1)
    return {"score": a["score"], "top": len(legend) - 1, "level": level, "label": legend[level], "legend": legend, "confidence": a.get("confidence"),
            "asked_at": rec.get("asked_at"), "model": rec["judge"]["model"]}


@app.template_global()
def task_tab(job, trial, traj) -> dict:
    """The trial page's Task instruction tab: the score (None until judged),
    the rubric it was scored on - the record's own legend once judged, the
    current question's until then - and the instruction text as the agent
    received it, which is what the judge read."""
    quality = task_quality_of(job, trial)
    rubric = quality["legend"] if quality else list(Q.QUESTIONS["instruction_quality"]["criteria"])
    instruction = judge_state.instruction_of(traj) if traj is not None and not getattr(traj, "error", None) else ""
    return {"quality": quality, "rubric": rubric, "instruction": instruction}


def _task_instruction(trial) -> str:
    """The task's text as the agent received it, from the trial's trajectory."""
    if not trial.has_trajectory:
        return ""
    traj = trajectory.load(trial.trajectory_path)
    return "" if traj.error else judge_state.instruction_of(traj)


def tasks_todo(job) -> list:
    """One trial per task of the job whose instruction is not yet judged and can be read."""
    seen, out = set(), []
    for t in job.trials:
        if t.task_base in seen:
            continue
        seen.add(t.task_base)
        if task_record(job, t.task_base) is None and t.has_trajectory:
            out.append(t)
    return out


def task_summary(job) -> dict:
    """For the job page: how many of the job's tasks have their instruction judged."""
    tasks = {t.task_base for t in job.trials}
    judged = sum(1 for tb in tasks if task_record(job, tb) is not None)
    return {"tasks": len(tasks), "judged": judged, "unjudged": len(tasks_todo(job)), "keyed": bool(key())}


def judge_task(job, trial, api_key: str, post=None) -> dict | None:
    """Score one task's instruction over the `task` state alone, stored under
    the dataset's task slot - never under the trial."""
    instruction = _task_instruction(trial)
    if not instruction:
        raise client.JudgeError(0, f"no instruction can be read for {trial.task_base}")
    states = batch.states_for(Q.group_ids("task"), lambda kind: judge_state.build(kind, None, instruction=instruction))
    return batch.ask_and_store(ws_dir(), task_key(job), trial.task_base, states, api_key, post=post)
