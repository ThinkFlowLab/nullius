"""A task's repeats in one job.

A repeat is one run of a task, counted whatever its result: Harbor's
``n_attempts`` and the standard's ``--repeat`` say how many. An attempt is one
try within a repeat: a repeat is tried again only when the infrastructure or
the agent failed (Harbor's ``max_retries``), never because the task was failed,
and only its last try is kept as its trial - Harbor deletes the others, the
standard moves them into the job's ``retried/``. So a job records one trial per
repeat, and nothing in a trial says which repeat it was. Two trials are repeats
of the same task when they carry the same ``task_name``; they are numbered by
when they started, a trial with no
recorded start after every one that has, ties by trial name. The number is
this module's, not the producer's: it is stable for one job tree and says
nothing about the order Harbor indexed them in.

The repeats of a task are read as the run's adjusted rate reads its trials:
an infra repeat (a rate limit, a provider or setup error, the spending cap, a
cancellation) never got a fair try, so it is left out of the pass count, the
rate and the means and counted apart; an unknown outcome stays in once anything
at the task was passed or failed. ``has_verdict`` is that rule, for every page.

pass@k and pass^k are here too: ``estimate`` is the estimators, which the
standard's ``pass_hat_k`` uses as well, and ``for_tasks`` the pages' reading of
a run's tasks.

Nothing here reads a file: it takes the parsed job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from math import comb
from typing import Iterable, Sequence

from . import classify

_LAST = datetime.max.replace(tzinfo=timezone.utc)


def _start(trial) -> datetime:
    at = trial.started_at
    if at is None:
        return _LAST
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def ordered(trials) -> list:
    """Repeats in the order they are numbered."""
    return sorted(trials, key=lambda t: (_start(t), t.trial_name))


# Outcomes that are neither a pass nor a fail: nothing scored the repeat, or
# the benchmark gives a score and no pass or fail.
NO_VERDICT = frozenset({classify.UNKNOWN, classify.SCORED})

# The numbers a task's row shows as a mean over its repeats.
MEANS = ("tokens", "dur_total", "dur_env", "dur_agent", "dur_verifier")


def ran(trials) -> tuple:
    """The repeats that got a fair try: every one but infra."""
    return tuple(t for t in trials if t.outcome not in classify.INFRA_OUTCOMES)


def has_verdict(trials) -> bool:
    """Whether a repeat that ran was passed or failed, so a pass count over
    them means something."""
    return any(t.outcome not in NO_VERDICT for t in ran(trials))


@dataclass(frozen=True)
class Repeats:
    """Every repeat of one task in one job, numbered from 1."""
    trials: tuple

    @property
    def n(self) -> int:
        return len(self.trials)

    @cached_property
    def ran(self) -> tuple:
        return ran(self.trials)

    @property
    def n_ran(self) -> int:
        return len(self.ran)

    @property
    def n_infra(self) -> int:
        return self.n - self.n_ran

    @property
    def n_passed(self) -> int:
        return sum(1 for t in self.trials if t.outcome == classify.RESOLVED)

    @property
    def has_verdicts(self) -> bool:
        """False when no repeat that ran was passed or failed (all unknown,
        scored or infra), so a pass count would say nothing."""
        return has_verdict(self.trials)

    @property
    def scores(self) -> list:
        return [t.score for t in self.ran if t.score is not None]

    @property
    def n_scored(self) -> int:
        return len(self.scores)

    @property
    def mean_score(self) -> float | None:
        return sum(self.scores) / len(self.scores) if self.scores else None

    @property
    def rate(self) -> float | None:
        """The share of the repeats that ran which passed, when one was passed
        or failed; else their mean score; else None (nothing ran, or nothing
        scored it)."""
        if self.has_verdicts:
            return self.n_passed / self.n_ran
        return self.mean_score

    @property
    def endings(self) -> frozenset:
        """How the repeats that ran ended, each way once: an outcome and its
        score. Empty when none ran."""
        return frozenset((t.outcome, None if t.score is None else round(t.score, 3)) for t in self.ran)

    def spread(self, field: str, kind: str = "agent") -> dict | None:
        """The mean of one number over the repeats that ran and recorded it,
        with its lowest and highest: ``field`` a trial attribute, or ``tokens``
        for input and output together. An agent's 0 tokens is a count that was
        not captured, as the token column reads it; a control's is real
        (``kind`` is the job's). None when no repeat that ran recorded it."""
        if field == "tokens":
            values = [(t.n_input_tokens or 0) + (t.n_output_tokens or 0) for t in self.ran
                      if t.n_input_tokens is not None or t.n_output_tokens is not None]
            if kind != "control":
                values = [v for v in values if v]
        else:
            values = [v for v in (getattr(t, field) for t in self.ran) if v is not None]
        if not values:
            return None
        return {"mean": sum(values) / len(values), "lo": min(values), "hi": max(values), "n": len(values)}

    def means(self, kind: str = "agent") -> dict:
        """``spread`` of each number a task's row shows, by field."""
        return {field: self.spread(field, kind) for field in MEANS}

    def number(self, trial) -> int | None:
        return next((i for i, t in enumerate(self.trials, 1) if t.trial_name == trial.trial_name), None)


def _by_task_name(job) -> dict[str, list]:
    """The job's trials by task name, the tasks in the order they first appear:
    what makes two trials repeats of one task, for every page of one run."""
    groups: dict[str, list] = {}
    for t in job.trials:
        groups.setdefault(t.task_name, []).append(t)
    return groups


def by_task(job) -> dict[str, Repeats]:
    """Every task of the job that was run more than once, by task name."""
    return {task: Repeats(tuple(ordered(ts))) for task, ts in _by_task_name(job).items() if len(ts) > 1}


def of(job, trial) -> Repeats | None:
    """The repeats of this trial's task, or None when it was run once."""
    same = [t for t in job.trials if t.task_name == trial.task_name]
    return Repeats(tuple(ordered(same))) if len(same) > 1 else None


def grouped(job) -> list[tuple]:
    """``(trial, repeats)`` for every trial, each task's repeats together and in
    number order, the tasks in the order they first appear. ``repeats`` is None
    for a task run once."""
    groups = by_task(job)
    out, seen = [], set()
    for t in job.trials:
        group = groups.get(t.task_name)
        if group is None:
            out.append((t, None))
        elif t.task_name not in seen:
            seen.add(t.task_name)
            out.extend((a, group) for a in group.trials)
    return out


def pass_at(n: int, c: int, k: int) -> float:
    """pass@k, as HumanEval's estimator reads n repeats with c passed: the
    chance that at least one of k drawn from them passed."""
    return 1 - comb(n - c, k) / comb(n, k)


def pass_hat(n: int, c: int, k: int) -> float:
    """pass^k, as τ-bench defines it: the chance that all k drawn passed."""
    return comb(c, k) / comb(n, k)


def estimate(per_task: Iterable[Sequence[bool]]) -> dict | None:
    """pass^k (``hat``) and pass@k (``at``) for k from 1 to n, averaged over the
    tasks, n the smallest repeat count among them and each task read on its
    first n repeats. None below two repeats."""
    tasks = [list(v) for v in per_task]
    n = min((len(v) for v in tasks), default=0)
    if n < 2:
        return None
    passed = [sum(v[:n]) for v in tasks]
    return {"units": len(tasks), "repeats": n,
            "hat": {k: sum(pass_hat(n, c, k) for c in passed) / len(tasks) for k in range(1, n + 1)},
            "at": {k: sum(pass_at(n, c, k) for c in passed) / len(tasks) for k in range(1, n + 1)}}


def for_tasks(tasks: Iterable[Sequence]) -> dict | None:
    """``estimate`` over a run's tasks, each the trials of its repeats. Every
    repeat that ran counts: a pass when resolved, else a fail - an agent's
    timeout or crash is its own result, and an unknown outcome stays in, as in
    the adjusted rate. A task with an infra repeat is left out, as τ-bench
    leaves out an infrastructure error, and so is one with a scored repeat,
    which has no pass or fail. None when nothing left was passed or failed, or
    below two repeats."""
    whole = [ordered(ts) for ts in tasks
             if not any(t.outcome in classify.INFRA_OUTCOMES or t.outcome == classify.SCORED for t in ts)]
    if not any(t.outcome != classify.UNKNOWN for ts in whole for t in ts):
        return None
    return estimate([t.outcome == classify.RESOLVED for t in ts] for ts in whole)


def for_job(job) -> dict | None:
    """``for_tasks`` over a parsed job, its tasks by task name."""
    return for_tasks(_by_task_name(job).values())
