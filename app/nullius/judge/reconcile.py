"""Reconciling the classifier with the judge: where a trial's recorded
outcome (``runs.classify``, deterministic, from the evidence on disk) and
the judge's ``failure_cause`` (a Choice over the transcript) disagree.

That disagreement is the investigation Nullius exists to surface. A trial
classified ``agent_error`` that the judge reads as ``environment_error``
is either a classifier gap or a misleading transcript, and both are
findings. Agreement is not news; disagreement is ranked so the strongest
come first.

The two vocabularies do not line up one to one, so the join is a stated
table (``COMPATIBLE``) rather than a rename: a classifier label names the
judge options it is *compatible* with, and a trial agrees when the judge's
pick is one of them. A label compatible with several options is one the
classifier cannot split further - ``patch_failed_tests`` is the verifier
saying no, which the judge may read as the agent's error, the verifier's,
or an undefined task, and none of the three contradicts the record. A
label compatible with nothing (``unknown``) is *not comparable*: the
classifier made no claim to agree or disagree with. For those the judge's
pick is turned round (``SUGGESTS``) into the labels it is compatible with,
shown as an advisory suggestion and never written back.

Every count here is code. Nothing is asked of the judge; the page reads
the store. The confidence gate uses the Choice's own ``confidence``, the
number the judge returned with the pick - never a Noul's probability
turned into one - and a threshold is a number a person can change, so it
is a module constant the page also takes as a parameter.
"""
from __future__ import annotations

from nullius.runs import classify

# Below this confidence a disagreement is noise: counted and listed apart,
# never ranked among the findings. The judge's Choice confidence, 0..1.
THRESHOLD = 0.5

QUESTION = "failure_cause"
OTHER = "other"

# Classifier label -> the failure_cause options that do not contradict it.
# An empty tuple is "no correspondence": nothing to agree or disagree with.
COMPATIBLE = {
    # A resolved trial is never asked failure_cause (questions.for_trial), so
    # there is nothing to reconcile; kept for the reverse map (`not_a_failure`
    # suggests it) and so the table names every label.
    classify.RESOLVED: ("not_a_failure",),
    classify.SCORED: (),                       # never asked, never suggested: only a benchmark's own score makes it
    # The verifier said no after a real attempt. Whether the agent was wrong,
    # the verifier was, or the task had no defined answer is exactly what the
    # record cannot tell and the transcript might: three options, all
    # compatible. `not_a_failure` here is a disagreement worth seeing.
    classify.PATCH_FAILED_TESTS: ("agent_error", "verifier_disagrees", "task_ambiguous"),
    classify.FAILED_VERIFICATION: ("agent_error", "verifier_disagrees", "task_ambiguous"),
    # A patch that would not apply is the agent's doing.
    classify.PATCH_NOT_APPLIED: ("agent_error",),
    # No patch at all: the classifier charges the agent. A judge reading the
    # transcript as an environment failure is a finding, not a join.
    classify.NO_PATCH: ("agent_error",),
    # The agent's own failure (timeout, context, refusal, exit code) is
    # charged to the agent; but the judge's option list folds a timeout in
    # with rate limits, so `rate_limit_or_timeout` on an AgentTimeoutError
    # is the vocabulary's shape, not a disagreement.
    classify.AGENT_ERROR: ("agent_error", "rate_limit_or_timeout"),
    classify.RATE_LIMITED: ("rate_limit_or_timeout",),
    # The judge's option names API errors alongside rate limits and timeouts.
    classify.API_ERROR: ("rate_limit_or_timeout",),
    classify.SETUP_ERROR: ("environment_error",),
    # The relay's own refusal at the run's spending cap: recorded, never read
    # from a transcript, so nothing agrees or disagrees with it and no cause suggests it.
    classify.SPENDING_CAP: (),
    # No claim made: not comparable. The judge's pick becomes a suggestion.
    classify.UNKNOWN: (),
}

# failure_cause option -> the classifier labels it is compatible with: the
# table above turned round, which is what a suggestion for an `unknown`
# trial is drawn from. `other` suggests nothing: it is the escape option,
# a cause outside the list, and names no label.
SUGGESTS = {opt: tuple(label for label in classify.ORDER if opt in COMPATIBLE[label])
            for opt in sorted({o for opts in COMPATIBLE.values() for o in opts})}

# Row statuses, in the order the page lists them.
DISAGREE, OTHER_CAUSE, AGREE, SUGGESTION, GATED, NOT_COMPARABLE, NOT_JUDGED = (
    "disagree", "other", "agree", "suggestion", "gated", "not_comparable", "not_judged")
STATUSES = (DISAGREE, OTHER_CAUSE, AGREE, SUGGESTION, GATED, NOT_COMPARABLE, NOT_JUDGED)

STATUS_LABELS = {
    DISAGREE: "Disagree", OTHER_CAUSE: "Cause outside the list", AGREE: "Agree", SUGGESTION: "Suggested",
    GATED: "Below threshold", NOT_COMPARABLE: "Not comparable", NOT_JUDGED: "Not judged",
}


def applies(job) -> bool:
    """A control job (nop, oracle) is a check on the tasks, not an agent's
    attempt: its outcomes are verdicts on the verifier, and failure_cause
    over its transcript reconciles with nothing."""
    return not job.is_control


def answer_of(record, question: str = None) -> dict | None:
    """The failure_cause answer in a stored record (or `question`'s), if it is a Choice with a pick."""
    a = ((record or {}).get("answers") or {}).get(question or QUESTION)
    if not a or not isinstance(a.get("choice"), str):
        return None
    return a


LIKELY = "likely_outcome"      # the dedicated Choice over the classifier's own labels, asked of `unknown` trials only


def row_of(trial, record, threshold: float = THRESHOLD) -> dict:
    """One trial's reconciliation: the recorded outcome, the judge's pick
    with its confidence and probability, the status, and - for an unknown
    outcome - the labels the pick suggests. The trial is read, never written."""
    label = trial.outcome
    row = {"trial_name": trial.trial_name, "task": trial.task_base, "outcome": label,
           "outcome_label": classify.LABELS.get(label, label), "compatible": list(COMPATIBLE.get(label, ())),
           "choice": None, "confidence": None, "p": None, "suggests": [], "status": NOT_JUDGED,
           "version": (record or {}).get("questions"), "asked_at": (record or {}).get("asked_at")}
    a = answer_of(record)
    if a is None:
        return row
    choice = a["choice"]
    confidence = a.get("confidence")
    row.update(choice=choice, confidence=confidence, p=(a.get("probabilities") or {}).get(choice))
    confident = isinstance(confidence, (int, float)) and confidence >= threshold
    if not COMPATIBLE.get(label):
        # no claim to compare with: a suggestion where the pick names labels, else not comparable. An unknown trial
        # asked `likely_outcome` (a Choice over the classifier's own labels) suggests that pick directly, its own
        # confidence gating it; without one, the failure_cause pick is turned round through SUGGESTS.
        likely = answer_of(record, LIKELY) if label == classify.UNKNOWN else None
        if likely and likely["choice"] in classify.ORDER:
            row.update(likely=likely["choice"], likely_confidence=likely.get("confidence"), likely_p=(likely.get("probabilities") or {}).get(likely["choice"]))
            row["suggests"] = [likely["choice"]]
            confident = isinstance(likely.get("confidence"), (int, float)) and likely["confidence"] >= threshold
        else:
            row["suggests"] = list(SUGGESTS.get(choice, ())) if label == classify.UNKNOWN else []
        row["status"] = NOT_COMPARABLE if not row["suggests"] else (SUGGESTION if confident else GATED)
    elif not confident:
        row["status"] = GATED
    elif choice in COMPATIBLE[label]:
        row["status"] = AGREE
    elif choice == OTHER:
        row["status"] = OTHER_CAUSE
    else:
        row["status"] = DISAGREE
    return row


def _rank(row: dict) -> tuple:
    """Strongest first: the judge's confidence, then the pick's probability;
    ties by trial name so the order is stable."""
    return (-(row["confidence"] or 0.0), -(row["p"] or 0.0), row["trial_name"])


def reconcile(job, records: dict, threshold: float = THRESHOLD, key=None) -> dict:
    """The job's reconciliation: rows by status (each list ranked strongest
    first), and the counts the page states. `records` is the store's
    {stored name: record}; `key` maps a trial name to its stored name (the
    store's own sanitiser), identity when not given. Reads the job; writes
    nothing."""
    key = key or (lambda name: name)
    rows = [row_of(t, records.get(key(t.trial_name)), threshold) for t in job.trials]
    by_status = {s: sorted((r for r in rows if r["status"] == s), key=_rank) for s in STATUSES}
    judged = sum(1 for r in rows if r["choice"] is not None)
    asked = sum(1 for t in job.trials if t.outcome not in (classify.RESOLVED, classify.SCORED))
    comparable = sum(1 for r in rows if r["compatible"] and r["choice"] is not None and r["outcome"] not in (classify.RESOLVED, classify.SCORED))
    return {
        "threshold": threshold, "applies": applies(job), "rows": by_status,
        "counts": {
            "trials": len(job.trials), "asked": asked, "judged": judged, "unjudged": asked - judged, "comparable": comparable,
            "agree": len(by_status[AGREE]), "disagree": len(by_status[DISAGREE]), "other": len(by_status[OTHER_CAUSE]),
            "suggested": len(by_status[SUGGESTION]), "gated": len(by_status[GATED]), "not_comparable": len(by_status[NOT_COMPARABLE]),
        },
    }
