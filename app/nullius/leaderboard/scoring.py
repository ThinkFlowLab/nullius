"""Select one run per dataset and score distinct tasks with their recorded attempts.

Dataset manifests map recorded task names to canonical identities. None means
identity/coverage is unknown; it never means an empty dataset. This module reads
no files and does not modify the jobs whose raw metrics other pages display.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from math import isfinite
from typing import Mapping, Sequence

from nullius.runs import attempts, classify
from nullius.runs.parse import Job, Trial


@dataclass(frozen=True)
class Contribution:
    score: float | None = None
    covered: int = 0
    total: int | None = None
    excluded: int = 0
    run_ids: tuple[str, ...] = ()
    reason: str = "not run"
    note: str = ""
    # Selected task evidence, for efficiency deduplication and run links.
    tasks: tuple[tuple[str, str, tuple[Trial, ...]], ...] = ()


def eligible(job: Job) -> bool:
    return bool(job.finished_at and not job.is_running and not job.is_preparing and not job.is_control)


def _groups(job, manifest=None):
    result = {}
    for trial in job.trials:
        key = manifest.get(trial.task_name, trial.task_name) if manifest is not None else trial.task_name
        result.setdefault(key, []).append(trial)
    return result


def _precedence(job, manifest=None):
    count = sum(bool(attempts.ran(ts)) for ts in _groups(job, manifest).values())
    at = job.finished_at
    stamp = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()
    return -count, -stamp, job.job_id


def _score(ts, graded):
    ran = attempts.ran(ts)
    if not graded:
        grouped = attempts.Attempts(tuple(ts))
        return grouped.rate if grouped.has_verdicts else None
    values = []
    for t in ran:
        if t.outcome == classify.AGENT_ERROR:
            values.append((0, t.n_parts or 1))
        elif t.score is not None:
            if isinstance(t.score, bool) or not isinstance(t.score, (int, float)) or not isfinite(t.score) or not 0 <= t.score <= 1:
                raise ValueError("Unsupported score scale")
            values.append((t.score, t.n_parts or 1))
    return sum(score * parts for score, parts in values) / sum(parts for _, parts in values) if values else None


def contribution(jobs: Sequence[Job], *, benchmark: str, version: str,
                 datasets: Mapping[str, Mapping[str, str] | None], dataset: str = "",
                 metric: str = "Score", graded: bool = False) -> Contribution:
    """Select by coverage, finish and qualified run id, never by outcome.

The caller passes one agent/version/setup row and qualified job ids. A selected
dataset reads only that dataset. Whole entries require explicit identities to
join several datasets; they cannot infer overlap from short task names.
"""
    if metric not in ("Score", "pass^3") or (graded and metric != "Score"):
        raise ValueError("Choose Score or supported pass^3")
    scope = {dataset: datasets[dataset]} if dataset in datasets else ({} if dataset else dict(datasets))
    if not scope:
        return Contribution(reason="Dataset unavailable")
    manifests = list(scope.values())
    total = len({canonical for manifest in manifests for canonical in manifest.values()}) if all(m is not None for m in manifests) else None
    pool = [j for j in jobs if eligible(j) and j.dataset in scope
            and j.identity.get("benchmark") == benchmark
            and j.identity.get("benchmark_version", "") == version]
    sources = {source for j in pool for source in j.merged_from}
    pool = [j for j in pool if j.job_id not in sources]
    selected = {}
    precedence = lambda j: _precedence(j, scope[j.dataset])
    for j in sorted(pool, key=precedence):
        selected.setdefault(j.dataset, j)
    ids = tuple(j.job_id for j in selected.values())
    if not selected:
        return Contribution(total=total)
    if len(selected) > 1 and any(scope[d] is None for d in selected):
        return Contribution(total=total, run_ids=ids, reason="Task identity unavailable for aggregation")
    union = {}
    for j in sorted(selected.values(), key=precedence):
        manifest = scope[j.dataset]
        if manifest is not None and any(t.task_name not in manifest for t in j.trials):
            return Contribution(total=total, run_ids=ids, reason="Task identity unavailable for aggregation")
        for canonical, ts in _groups(j, manifest).items():
            union.setdefault(canonical, (j.job_id, canonical, tuple(attempts.ordered(ts))))
    tasks = tuple(union.values())
    groups = [ts for _, _, ts in tasks]
    excluded = sum(not attempts.ran(ts) for ts in groups)
    covered = sum(bool(attempts.ran(ts)) for ts in groups)
    note = ""
    if graded:
        n_grades = sum(t.score is not None and not t.is_infra for ts in groups for t in ts)
        crashes = sum(t.outcome == classify.AGENT_ERROR for ts in groups for t in ts)
        note = f"{n_grades} graded, {crashes} agent crashes; crashes count as 0"
    reason = ""
    if metric == "pass^3":
        estimate = attempts.for_tasks(groups)
        score = estimate["hat"].get(3) if estimate else None
        if score is None:
            reason = "Three recorded attempts per eligible task required"
        # An infra attempt makes the entire unit ineligible for pass^k.
        covered = sum(bool(ts) and len(ts) >= 3 and not any(t.is_infra or t.outcome == classify.SCORED for t in ts) for ts in groups)
    else:
        try:
            scores = [_score(ts, graded) for ts in groups]
            measured = [v for v in scores if v is not None]
            score = sum(measured) / len(measured) if measured else None
            if score is None:
                reason = "No measured outcomes"
        except ValueError as error:
            score, reason = None, str(error)
    return Contribution(score, covered, total, excluded, ids, reason, note, tasks)
