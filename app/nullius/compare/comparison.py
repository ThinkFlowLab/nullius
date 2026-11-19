"""Qualified run selections and honest task cohorts, with no I/O or Flask.

Run ids are unique only inside a workspace. Task ids are unique only inside a
recorded dataset. Keeping both identities explicit prevents a comparison from
quietly joining two unrelated rows merely because their short names match.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from nullius.runs import attempts as task_attempts
from nullius.runs.parse import Job, Trial


@dataclass(frozen=True)
class RunRef:
    workspace: str
    job_id: str

    @property
    def value(self) -> str:
        return f"{self.workspace}:{self.job_id}"

    @classmethod
    def parse(cls, value: str, empty: bool = False) -> "RunRef":
        """``empty``: also a column with no run yet, ``<workspace>:``."""
        name, separator, job_id = value.partition(":")
        if not separator or not (job_id or empty):
            raise ValueError("A run selection must include its workspace and run id.")
        # Empty means the active source, for legacy selections without a saved
        # workspace. Named workspaces use the same slug alphabet as workspace.py.
        if name and (len(name) > 48 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)):
            raise ValueError("Invalid workspace in the run selection.")
        return cls(name, job_id)


@dataclass(frozen=True)
class SelectedRun:
    ref: RunRef
    job: Job
    workspace_label: str

    @property
    def value(self) -> str:
        return self.ref.value

    @property
    def workspace(self) -> str:
        return self.ref.workspace


def resolve(values: Sequence[str], jobs_by_workspace: Mapping[str, Sequence[Job]],
            labels: Mapping[str, str] | None = None) -> tuple[SelectedRun, ...]:
    """Resolve references in request order; never substitute another workspace.

    A duplicate selection is one column. Duplicate ids *inside* a workspace are
    ambiguous and refused, while the same id in two workspaces is valid.
    """
    labels = labels or {}
    selected, seen = [], set()
    for value in values:
        ref = RunRef.parse(value)
        if ref in seen:
            continue
        seen.add(ref)
        if ref.workspace not in jobs_by_workspace:
            raise ValueError(f"Workspace {ref.workspace or 'active source'} is unavailable.")
        matches = [job for job in jobs_by_workspace[ref.workspace] if job.job_id == ref.job_id]
        if not matches:
            raise ValueError(f"Run {ref.job_id} is unavailable in {ref.workspace or 'the active source'}.")
        if len(matches) > 1:
            raise ValueError(f"Run id {ref.job_id} occurs more than once in this workspace.")
        selected.append(SelectedRun(ref, matches[0], labels.get(ref.workspace, ref.workspace or "Active source")))
    return tuple(selected)


@dataclass(frozen=True)
class TaskRow:
    """One task across the runs. ``trials`` holds each run's last indexed
    attempt, the one its cell links to; ``attempts`` counts each run's
    attempts and ``tried`` holds them, read as ``attempts.Attempts`` reads
    them (infra apart)."""
    dataset: str
    task: str
    trials: tuple[Trial | None, ...]
    attempts: tuple[int, ...]
    tried: tuple[task_attempts.Attempts | None, ...] = ()

    def _compared(self) -> list:
        """The runs whose rates are set against each other, one kind of number
        only: those that passed or failed the task when any did, else those
        with a mean score. A pass rate is never set against a mean score."""
        tried = [a for a in self.tried if a]
        return [a for a in tried if a.has_verdicts] or [a for a in tried if a.mean_score is not None]

    @property
    def rate_moved(self) -> bool:
        """The runs' pass rates (mean scores) differ."""
        return len({round(a.rate, 3) for a in self._compared()}) > 1

    @property
    def differs(self) -> bool:
        """The pass rate moved, or the attempts that ran ended differently:
        one failed verification, another timed out. A run none of whose
        attempts ran differs from one that ran."""
        return self.rate_moved or len({a.endings for a in self.tried if a}) > 1

    @property
    def movement(self) -> float:
        """How far apart the runs' pass rates are, each read as if the run had
        one more pass and one more fail, so a gap over many attempts outranks
        the same gap over one: 1/1 against 0/1 reads 67% against 33%, 2/3
        against 0/3 reads 60% against 20%. Mean scores are taken as they are.
        0 when the rates are the same, and it can be 0 when they are not (1/1
        against 3/4 reads 67% against 67%): ``rate_moved`` says which."""
        if not self.rate_moved:
            return 0.0
        values = [(a.n_passed + 1) / (a.n_ran + 2) if a.has_verdicts else a.mean_score for a in self._compared()]
        return max(values) - min(values)

    @property
    def shared(self) -> bool:
        return all(self.trials)


@dataclass(frozen=True)
class Comparison:
    selected: tuple[SelectedRun, ...]
    cohort: str
    jobs: tuple[Job, ...]
    tasks: tuple[TaskRow, ...]
    shared_tasks: int
    excluded_trials: tuple[int, ...]
    repeated_trials: tuple[int, ...]
    datasets_match: bool
    datasets_known: bool
    pass_k: tuple[dict | None, ...] = ()

    @property
    def references(self) -> list[str]:
        return [selection.value for selection in self.selected]

    @property
    def n_differing(self) -> int:
        return sum(row.differs for row in self.tasks)


def build(selected: Sequence[SelectedRun], cohort: str = "whole") -> Comparison:
    """Build a whole-run or exact shared-task view without changing any Job.

    Matching uses the dataset string as recorded, without alias/path guessing.
    Every attempt on a shared task remains in the rates, costs and token sums.
    A task row holds every attempt and keeps the last indexed one to link to;
    rows sort by how far the pass rate moved (``TaskRow.movement``), the
    largest first, then the other rows whose rate moved, then the rest that
    differ.
    Unknown outcomes remain in the adjusted-rate denominator because Job owns
    the arithmetic. A cohort cannot manufacture a new wall-clock run interval.
    """
    if cohort not in {"whole", "matched"}:
        raise ValueError("Choose whole runs or matched tasks as the comparison cohort.")
    selected = tuple(selected)
    known = all(selection.job.dataset for selection in selected)
    datasets_match = len({selection.job.dataset for selection in selected}) <= 1

    def key(selection, trial):
        # Missing dataset identities cannot prove that identical short names
        # refer to the same task. A per-column sentinel keeps their rows apart.
        dataset = selection.job.dataset or f"[unidentified:{selection.value}]"
        return dataset, trial.task_base

    sets = [{key(selection, trial) for trial in selection.job.trials} for selection in selected]
    common = set.intersection(*sets) if sets else set()
    # each task's attempts, one list per run, the tasks in the order they appear
    jobs, excluded, repeated, by_task = [], [], [], {}
    for column, selection in enumerate(selected):
        original = selection.job
        trials = [trial for trial in original.trials
                  if cohort == "whole" or key(selection, trial) in common]
        # Aggregate properties derive from trials; copied clocks are suppressed
        # for matched views because their interval still belongs to the full run.
        view = replace(original, trials=trials,
                       **({"started_at": None, "finished_at": None} if cohort == "matched" else {}))
        jobs.append(view)
        excluded.append(len(original.trials) - len(trials))
        repeated.append(len(trials) - len({key(selection, trial) for trial in trials}))
        for trial in trials:
            by_task.setdefault(key(selection, trial), [[] for _ in selected])[column].append(trial)

    def task_row(task_key, columns):
        # the cell links to the last indexed attempt
        return TaskRow(*task_key, tuple(ts[-1] if ts else None for ts in columns), tuple(len(ts) for ts in columns),
                       tuple(task_attempts.Attempts(tuple(task_attempts.ordered(ts))) if ts else None for ts in columns))

    rows = [task_row(task_key, columns) for task_key, columns in by_task.items()]
    rows.sort(key=lambda row: (-round(row.movement, 3), not row.rate_moved, not row.differs, row.dataset, row.task))
    # pass@k from the rows' own tasks, so the summary and the table count alike
    pass_k = tuple(task_attempts.for_tasks(row.tried[column].trials for row in rows if row.tried[column])
                   for column in range(len(selected)))
    return Comparison(selected, cohort, tuple(jobs), tuple(rows), len(common),
                      tuple(excluded), tuple(repeated), datasets_match, known, pass_k)



# Two sides' scores closer than this are a tie. Half a rubric level: the
# judge's own spread on one state has been measured at 0.06, so anything
# under a level's half is within what a re-ask could flip. Change it here;
# nothing asks the model to compare.
TIE_MARGIN = 0.5


def better_of(score_a: float | None, score_b: float | None, margin: float = TIE_MARGIN) -> str | None:
    """'A', 'B' or 'same' from two independent scores on one rubric, the
    higher side winning by at least `margin`; None until both are scored.
    The scores are the judge's expectations over the rubric's levels,
    compared as given - never a magnitude read between two levels.
    Symmetric by construction: swapping the sides mirrors the answer."""
    if score_a is None or score_b is None:
        return None
    if abs(score_a - score_b) < margin:
        return "same"
    return "A" if score_a > score_b else "B"
