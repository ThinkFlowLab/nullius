"""Qualified run selections and honest task cohorts, with no I/O or Flask.

Run ids are unique only inside a workspace. Task ids are unique only inside a
recorded dataset. Keeping both identities explicit prevents a comparison from
quietly joining two unrelated rows merely because their short names match.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from nullius.runs.parse import Job, Trial


@dataclass(frozen=True)
class RunRef:
    workspace: str
    job_id: str

    @property
    def value(self) -> str:
        return f"{self.workspace}:{self.job_id}"

    @classmethod
    def parse(cls, value: str) -> "RunRef":
        name, separator, job_id = value.partition(":")
        if not separator or not job_id:
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
    dataset: str
    task: str
    trials: tuple[Trial | None, ...]
    attempts: tuple[int, ...]

    @property
    def differs(self) -> bool:
        # a scored task differs by its score too: two readers that both scored a
        # conversation are told apart by how much
        return len({(trial.outcome, None if trial.score is None else round(trial.score, 3)) for trial in self.trials if trial}) > 1

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

    @property
    def references(self) -> list[str]:
        return [selection.value for selection in self.selected]

    @property
    def n_differing(self) -> int:
        return sum(row.differs for row in self.tasks)


def build(selected: Sequence[SelectedRun], cohort: str = "whole") -> Comparison:
    """Build a whole-run or exact shared-task view without changing any Job.

    Matching uses the dataset string as recorded, without alias/path guessing.
    Every attempt on a shared task remains in the rates, costs and token sums;
    task rows retain the last indexed attempt, as the existing table does.
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
        counts = Counter(key(selection, trial) for trial in trials)
        repeated.append(sum(count - 1 for count in counts.values()))
        for trial in trials:
            task_key = key(selection, trial)
            row, attempts = by_task.setdefault(task_key, ([None] * len(selected), [0] * len(selected)))
            row[column] = trial
            attempts[column] += 1

    rows = [TaskRow(dataset, task, tuple(trials), tuple(attempts))
            for (dataset, task), (trials, attempts) in by_task.items()]
    rows.sort(key=lambda row: (not row.differs, row.dataset, row.task))
    return Comparison(selected, cohort, tuple(jobs), tuple(rows), len(common),
                      tuple(excluded), tuple(repeated), datasets_match, known)



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
