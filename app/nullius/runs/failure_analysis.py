"""A run's failure analysis: why each failed trial failed, and which tasks a person found flawed or ambiguous.

One file per job, ``failure-analysis.json`` beside the job's ``result.json``. A person writes it from the trial
page's User judge tab or the ``nullius flags`` command; nothing here calls a model. Every writer goes through
:func:`update`, which holds a lock for the whole read, change and write, so the tab and the command never drop each
other's changes.

- A *label* belongs to one trial (its ``trial_name``, a retry's ``.try-N`` included): the cause of its failure, one
  of ``LABELS``.
- A *flag* belongs to one task of the job's dataset (its ``task_base``, the key Compare uses within a dataset):
  ``flawed`` (its expected outcome is wrong) or ``ambiguous`` (more than one reading is defensible). A task has at
  most one flag. A flag can name the task's version; when the task changes, that flag no longer applies. A flag
  with no version applies to any version.

A flag a person sets is accepted at once. A flag that comes from elsewhere (an import, a copy from another run, a
proposal such as an agent's) starts ``pending``, counts only once a person accepts it, and never replaces a flag a person accepted.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from functools import lru_cache
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from nullius import ondisk

FILE = "failure-analysis.json"
SCHEMA = "nullius.failure-analysis/v1"

LABELS = ("agent", "model", "environment", "evaluator")
KINDS = ("flawed", "ambiguous")
STATUSES = ("pending", "accepted")

# The judge's failure_cause answer (judge/questions.py), read as a suggestion for a label. agent_error covers both
# the agent and the model, so it suggests both and the person picks.
JUDGE_CAUSE = {
    "agent_error": ("agent", "model"),
    "environment_error": ("environment",),
    "rate_limit_or_timeout": ("environment",),
    "task_ambiguous": ("evaluator",),
    "verifier_disagrees": ("evaluator",),
}


class FailureAnalysisError(ValueError):
    """A label, a flag or a file that does not follow the rules above."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_ok(source: str) -> bool:
    prefixes = ("import:", "copy:", "proposed:")
    return source == "person" or (source.startswith(prefixes) and len(source.split(":", 1)[1]) > 0)


@dataclass
class Label:
    label: str
    note: str = ""
    by: str = ""
    at: str = ""
    judge_cause: Optional[str] = None   # the judge's failure_cause when the label was set, if any


@dataclass
class Flag:
    kind: str
    category: str
    evidence: str = ""                  # a pointer (a document, a tool), never the expected outcome
    task_version: Optional[str] = None  # the task_checksum its trial recorded when the flag was set
    status: str = "accepted"
    source: str = "person"              # person | import:<what> | copy:<job> | proposed:<who>, e.g. an agent
    by: str = ""                        # who set or proposed it
    at: str = ""
    accepted_by: str = ""
    accepted_at: str = ""


@dataclass
class Analysis:
    labels: Dict[str, Label] = field(default_factory=dict)   # by trial name
    flags: Dict[str, Flag] = field(default_factory=dict)     # by task_base

    def set_label(self, trial: str, label: str, *, by: str, note: str = "", judge_cause: Optional[str] = None) -> Label:
        if not trial:
            raise FailureAnalysisError("a label needs a trial name")
        if label not in LABELS:
            raise FailureAnalysisError(f"label {label!r} is not one of {', '.join(LABELS)}")
        self.labels[trial] = Label(label, note, by, _now(), judge_cause)
        return self.labels[trial]

    def clear_label(self, trial: str) -> None:
        self.labels.pop(trial, None)

    def set_flag(self, task: str, kind: str, category: str, *, by: str, evidence: str = "",
                 task_version: Optional[str] = None, source: str = "person") -> Optional[Flag]:
        """Set the task's flag. A flag from elsewhere never replaces one a person accepted for the same task version: it
        returns None. An accepted flag on another version is inert, so a flag for the current one replaces it."""
        if not task:
            raise FailureAnalysisError("a flag needs a task")
        if kind not in KINDS:
            raise FailureAnalysisError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
        if not category:
            raise FailureAnalysisError("a flag needs a category")
        if not _source_ok(source):
            raise FailureAnalysisError(f"source {source!r} is not person, import:<what>, copy:<job> or proposed:<who>")
        old = self.flags.get(task)
        stale = (old is not None and old.task_version is not None and task_version is not None
                 and old.task_version != task_version)
        if source != "person" and old is not None and old.status == "accepted" and not stale:
            return None
        now = _now()
        person = source == "person"
        self.flags[task] = Flag(kind, category, evidence, task_version, "accepted" if person else "pending", source,
                                by, now, by if person else "", now if person else "")
        return self.flags[task]

    def accept(self, task: str, *, by: str) -> Flag:
        flag = self.flags.get(task)
        if flag is None:
            raise FailureAnalysisError(f"no flag on task {task!r}")
        flag.status, flag.accepted_by, flag.accepted_at = "accepted", by, _now()
        return flag

    def remove_flag(self, task: str) -> None:
        """Remove the task's flag: how a person rejects a pending flag or withdraws an accepted one."""
        self.flags.pop(task, None)

    def set_aside(self, kinds: Iterable[str], versions: Optional[Dict[str, str]] = None) -> Set[str]:
        """The tasks the filtered score leaves out: accepted flags of the chosen kinds. With *versions* (task to its current
        digest), a flag that names another version does not count; a flag that names none still does."""
        chosen = set(kinds)
        out = set()
        for task, flag in self.flags.items():
            if flag.status != "accepted" or flag.kind not in chosen:
                continue
            if versions is not None and flag.task_version is not None and versions.get(task) != flag.task_version:
                continue
            out.add(task)
        return out


@lru_cache(maxsize=1)
def who() -> str:
    """The name a label or a flag is recorded under: git's global user.name on this machine, else the login name.
    Read once per process."""
    try:
        name = subprocess.run(["git", "config", "--global", "--get", "user.name"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    if name:
        return name
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def suggestion(judge_cause: Optional[str]) -> Tuple[str, ...]:
    """The labels a judge's failure_cause points to: none, one, or two for the person to pick from."""
    return JUDGE_CAUSE.get(judge_cause or "", ())


def path(job_dir: str) -> str:
    return os.path.join(job_dir, FILE)


def _records(raw: dict, name: str, cls):
    block = raw.get(name) or {}
    if not isinstance(block, dict):
        raise FailureAnalysisError(f"{FILE}: {name} is not an object")
    out = {}
    for key, value in block.items():
        if not isinstance(value, dict):
            raise FailureAnalysisError(f"{FILE}: {name} {key!r} is not an object")
        try:
            out[key] = cls(**value)
        except TypeError as e:
            raise FailureAnalysisError(f"{FILE}: {name} {key!r}: {e}") from e
    return out


def load(job_dir: str) -> Analysis:
    """The job's analysis; an empty one when the job has none yet."""
    try:
        with open(path(job_dir), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return Analysis()
    except (OSError, json.JSONDecodeError) as e:
        raise FailureAnalysisError(f"{FILE} cannot be read: {e}") from e
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise FailureAnalysisError(f"{FILE} is not a {SCHEMA} file")
    labels = _records(raw, "labels", Label)
    flags = _records(raw, "flags", Flag)
    for key, lab in labels.items():
        if lab.label not in LABELS:
            raise FailureAnalysisError(f"trial {key!r}: label {lab.label!r} is not one of {', '.join(LABELS)}")
    for key, fl in flags.items():
        if fl.kind not in KINDS or fl.status not in STATUSES or not fl.category or not _source_ok(fl.source):
            raise FailureAnalysisError(f"task {key!r}: kind, status, category or source is not allowed")
    return Analysis(labels, flags)


def _save(job_dir: str, analysis: Analysis) -> None:
    body = {"schema": SCHEMA,
            "labels": {k: asdict(v) for k, v in sorted(analysis.labels.items())},
            "flags": {k: asdict(v) for k, v in sorted(analysis.flags.items())}}
    ondisk.write_whole(path(job_dir), lambda fh: (json.dump(body, fh, indent=1, ensure_ascii=False), fh.write("\n")))


def update(job_dir: str, change: Callable[[Analysis], object]) -> object:
    """Read the job's analysis, apply *change*, write it back, all under one lock. Returns what *change* returns."""
    # the lock is a file of its own: the analysis file is replaced on every write, so a lock on it would be lost
    with ondisk.locked(path(job_dir) + ".lock"):
        analysis = load(job_dir)
        result = change(analysis)
        _save(job_dir, analysis)
        return result


@dataclass(frozen=True)
class Filtered:
    """What the job page needs for its Filtered score."""
    flagged: Dict[str, int]       # accepted flags on this job's tasks, by kind: what the switches offer
    other_version: int            # accepted flags that name another version of their task: they do not count
    kinds: Tuple[str, ...]        # the switches that are on
    set_aside: Dict[str, int]     # tasks left out, by kind
    filtered_score: Optional[float] = None  # the adjusted rate without those tasks; None if no switch is on or none left
    solved: int = 0
    ran: int = 0                  # trials left that ran (infra left out, as in the adjusted rate)


def filtered(job, analysis: Analysis, kinds: Iterable[str], versions: Optional[Dict[str, str]] = None) -> Filtered:
    """The job's adjusted rate with the chosen filters on. It is computed by the job's own code on a copy that keeps
    every trial except those of set-aside tasks; only the rate and its counts are read from that copy. With
    *versions* (`task_versions`), a flag on another version of its task does not count."""
    tasks = {t.task_base for t in job.trials}
    on = tuple(k for k in KINDS if k in set(kinds))
    flagged = {k: len(analysis.set_aside([k], versions) & tasks) for k in KINDS}
    stale = len(analysis.set_aside(KINDS) & tasks) - len(analysis.set_aside(KINDS, versions) & tasks)
    if not on:
        return Filtered(flagged, stale, on, {})
    out = analysis.set_aside(on, versions) & tasks
    by_kind = {k: sum(1 for t in out if analysis.flags[t].kind == k) for k in on}
    rest = replace(job, trials=[t for t in job.trials if t.task_base not in out])
    return Filtered(flagged, stale, on, by_kind, rest.adjusted_solve_rate, rest.n_resolved,
                    len(rest.trials) - rest.n_infra)


def task_versions(job, tasks: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Each task's version: the ``task_checksum`` Harbor records in a trial's ``result.json``. Only *tasks* when
    given; a task whose trials record none is left out."""
    wanted = None if tasks is None else set(tasks)
    out: Dict[str, str] = {}
    for t in job.trials:
        if t.task_base in out or (wanted is not None and t.task_base not in wanted):
            continue
        try:
            with open(os.path.join(t.dir, "result.json"), encoding="utf-8") as fh:
                checksum = (json.load(fh) or {}).get("task_checksum")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(checksum, str) and checksum:
            out[t.task_base] = checksum
    return out


def copy_flags(source: Analysis, target: Analysis, source_job: str, tasks: Iterable[str], *,
               by: str) -> Tuple[list, list, list]:
    """Copy *source*'s accepted flags on the target job's *tasks* into *target* as pending flags, keeping each flag's
    task version. Returns the tasks copied, the tasks the target job does not hold, and the tasks skipped because
    *target* already holds an accepted flag."""
    known = set(tasks)
    copied, unknown, skipped = [], [], []
    for task, flag in sorted(source.flags.items()):
        if flag.status != "accepted":
            continue
        if task not in known:
            unknown.append(task)
            continue
        got = target.set_flag(task, flag.kind, flag.category, by=by, evidence=flag.evidence,
                              task_version=flag.task_version, source=f"copy:{source_job}")
        (copied if got is not None else skipped).append(task)
    return copied, unknown, skipped


def import_flags(target: Analysis, records: Iterable[dict], tasks: Iterable[str], *, source: str, by: str,
                 prefix: str = "", versions: Optional[Dict[str, str]] = None) -> Tuple[list, list, list]:
    """Add flags from *records* (each with ``task``, ``kind``, ``category`` and an optional ``evidence``) as pending
    flags. A task name is *prefix* plus the record's ``task``; a name the job does not hold is not guessed. Returns
    the tasks added, the names not found, and the tasks skipped because they hold an accepted flag."""
    known = set(tasks)
    added, unknown, skipped = [], [], []
    for rec in records:
        if not isinstance(rec, dict):
            raise FailureAnalysisError("each flag to import is an object")
        if not rec.get("task"):
            raise FailureAnalysisError("each flag to import names a task")
        task = prefix + str(rec["task"])
        if task not in known:
            unknown.append(task)
            continue
        got = target.set_flag(task, str(rec.get("kind", "")), str(rec.get("category", "")), by=by,
                              evidence=str(rec.get("evidence", "") or ""), source=source,
                              task_version=(versions or {}).get(task))
        (added if got is not None else skipped).append(task)
    return added, unknown, skipped


def categories(analyses: Iterable[Analysis]) -> list:
    """The categories of the accepted flags in *analyses*, most used first: what the tab suggests next to its
    defaults. A pending flag does not count, so an unreviewed proposal never promotes its own category."""
    counts: Dict[str, int] = {}
    for a in analyses:
        for f in a.flags.values():
            if f.status == "accepted":
                counts[f.category] = counts.get(f.category, 0) + 1
    return sorted(counts, key=lambda c: (-counts[c], c))
