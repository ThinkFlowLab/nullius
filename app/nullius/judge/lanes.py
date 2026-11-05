"""The run page's record -> judge lanes: reconcile's rows, grouped the way
the board draws them.

Each recorded outcome is a row of the board's Outcomes legend, and the row
starts its own readings. One line per (status, cause) pair the judge gave
that outcome's trials:

- **agree** — the recorded outcome allows the cause: a straight line into
  the *agrees* lane;
- **disagree** — ``DISAGREE`` or ``OTHER_CAUSE``, a cause the outcome does
  not allow: a jog into the *disagrees* lane;
- **gutter** — everything that is not a finding about the classifier's
  claim, which never enters a lane: *not asked* (a resolved trial is never
  asked its cause), *not judged* (asked, no record yet), *below gate* (a
  pick under the threshold: noise, never ranked), and an Unknown outcome's
  readings (no claim to test: *suggests* a label, or *not comparable*).

Every status and every count is reconcile's (``reconcile.row_of``); this
module only groups. The tally under the lanes is reconcile's counts in its
own words, grouped by the line each is drawn with. Layout is the page's
business: nothing here is a pixel.
"""
from __future__ import annotations

from nullius.judge import reconcile as R
from nullius.runs import classify

AGREE_LANE, DISAGREE_LANE, GUTTER = "agree", "disagree", "gutter"
LANE_OF = {R.AGREE: AGREE_LANE, R.DISAGREE: DISAGREE_LANE, R.OTHER_CAUSE: DISAGREE_LANE}

# the tally's groups: the line each status is drawn with, in reconcile's order
TALLY = (("jog", (R.DISAGREE, R.OTHER_CAUSE)), ("straight", (R.AGREE,)), ("dotted", (R.GATED,)),
         ("open", (R.SUGGESTION, R.NOT_COMPARABLE)), ("ring", (R.NOT_JUDGED,)))
ALWAYS = (R.DISAGREE, R.AGREE)          # shown at zero too: "Agree 0" is news on a run where every reading disagrees

NOT_ASKED = "not_asked"                 # not a reconcile status: a resolved trial is never asked failure_cause
GUTTER_WORD = {NOT_ASKED: "not asked", R.NOT_JUDGED: "not judged", R.GATED: "below gate", R.NOT_COMPARABLE: "not comparable"}
_LINE_ORDER = {AGREE_LANE: 0, DISAGREE_LANE: 1, GUTTER: 2}


def board(job, records: dict, threshold: float = R.THRESHOLD, key=None) -> dict | None:
    """The lanes for one job, or None for a control (nothing to reconcile).

    Returns ``rows`` (one per outcome present, in the classifier's order,
    each with its ``lines``), ``tally`` (groups of ``(status, label, n)``),
    ``of`` (the comparable judged trials Agree and Disagree are counted
    over), ``status`` ({trial name: status}, ``not_asked`` for a resolved
    trial) and which lanes any line reaches."""
    if not R.applies(job):
        return None
    key = key or (lambda name: name)
    rows = {t.trial_name: R.row_of(t, records.get(key(t.trial_name)), threshold) for t in job.trials}
    status = {}
    out = []
    for label in classify.ORDER:
        trials = [t for t in job.trials if t.outcome == label]
        if not trials:
            continue
        groups: dict[tuple, dict] = {}
        for t in trials:
            r = rows[t.trial_name]
            s = NOT_ASKED if label in (classify.RESOLVED, classify.SCORED) else r["status"]
            status[t.trial_name] = s
            # the gutter's statuses group by status alone, except a suggestion, which names what it suggests
            cause = r["choice"] if s in LANE_OF or s == R.GATED else None
            suggests = r["suggests"][0] if s == R.SUGGESTION and r["suggests"] else None
            g = groups.setdefault((s, cause, suggests), {
                "lane": LANE_OF.get(s, GUTTER), "status": s, "status_label": R.STATUS_LABELS.get(s, GUTTER_WORD.get(s, s)),
                "cause": cause, "suggests": suggests, "n": 0, "trials": [],
                "word": ("suggests " + classify.LABELS.get(suggests, suggests)) if suggests else GUTTER_WORD.get(s)})
            g["n"] += 1
            g["trials"].append(t.trial_name)
        lines = sorted(groups.values(), key=lambda g: (_LINE_ORDER[g["lane"]], _status_rank(g["status"]), -g["n"], g["cause"] or ""))
        out.append({"outcome": label, "label": classify.LABELS.get(label, label), "n": len(trials), "lines": lines})
    counts = {s: sum(1 for v in status.values() if v == s) for s in R.STATUSES}
    tally = []
    for shape, statuses in TALLY:
        items = [(s, R.STATUS_LABELS[s], counts[s]) for s in statuses if counts[s] or s in ALWAYS]
        if items:
            tally.append({"shape": shape, "items": items})
    lanes = {g["lane"] for r in out for g in r["lines"]}
    of = sum(1 for t in job.trials if rows[t.trial_name]["compatible"] and rows[t.trial_name]["choice"] is not None
             and t.outcome not in (classify.RESOLVED, classify.SCORED))
    return {"rows": out, "tally": tally, "of": of, "status": status, "threshold": threshold,
            "has_agree": AGREE_LANE in lanes, "has_disagree": DISAGREE_LANE in lanes}


def _status_rank(s: str) -> int:
    return R.STATUSES.index(s) if s in R.STATUSES else len(R.STATUSES)
