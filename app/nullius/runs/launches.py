"""Runs grouped by the launch that made them.

Runs started together ran the same tasks: the agents a recipe named, and the
nop and oracle controls that check those tasks. The Runs page shows each launch
as one block, so it needs to know which runs belong together. Two sources, in
this order:

- **The run record.** A run started from the builder's Run writes
  ``runs/<name>/run.json``, and its plan names every job it launches
  (``execution.Plan.jobs``). Those jobs are one launch, including the ones
  Harbor has not written yet.
- **Inferred, for everything else** — an imported tree, a job run by hand, a run
  from before the record existed: runs on the same dataset whose start is within
  ``WINDOW_SEC`` of the launch's first run are one launch. A run with no start
  time at all is a launch of its own.

Either way, the shards of one launch of the Nullius standard - jobs whose
config names the same ``launch`` id (``Job.launch_id``), each run on a machine
of its own, perhaps by a run record of its own, perhaps hours apart - are one
launch: a shard joins the launch that holds another shard of its id before
any other rule is asked.

Nothing here reads a file: it takes the parsed jobs and the records' job lists,
and groups. Newest launch first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

WINDOW_SEC = 30 * 60


@dataclass
class Launch:
    key: str                      # "run:<execution id>" or "inferred:<first job id>"
    source: str                   # "record" or "inferred"
    dataset: str | None
    started: datetime | None      # the earliest start of its runs; none written yet, when it was launched
    job_ids: list = field(default_factory=list)       # written by Harbor, in start order
    pending: list = field(default_factory=list)       # named by the record, not written yet
    name: str | None = None                            # the run record's name
    launch_ids: set = field(default_factory=set)      # the standard's launch ids among its runs (its shards)

    @property
    def title(self) -> str:
        """What the launch is called on the Runs page: the standard's launch id
        when its runs are the shards of one launch - what every shard
        records, and what ``cli merge`` puts together - else ``prefix``."""
        return next(iter(self.launch_ids)) if len(self.launch_ids) == 1 else self.prefix

    @property
    def prefix(self) -> str:
        """What every run of the launch is called, up to the part that differs:
        ``tau-latest3-0919-132944-*`` over ``…-agent`` and ``…-nop``. The
        common prefix of the ids, cut back to a separator; the id itself for
        a launch of one run; the first id "and N more" when they share nothing."""
        ids = self.job_ids + self.pending
        if len(ids) == 1:
            return ids[0]
        common = os.path.commonprefix(ids)
        cut = max(common.rfind("-"), common.rfind("_"))
        if cut <= 0:        # nothing shared worth a wildcard: name the first and count the rest
            return f"{ids[0]} and {len(ids) - 1} more"
        return common[:cut + 1] + "*"


def merged_sources(jobs) -> set[str]:
    """Source jobs represented by a finished merge in this workspace.

    Use the recorded names, never a shared launch id: another agent or an
    unmerged shard may belong to the same launch and must still count.
    """
    return {source for j in jobs if not j.is_running and not j.is_preparing
            for source in getattr(j, "merged_from", []) if source != j.job_id}


def group(jobs, records: dict | None = None, window: float = WINDOW_SEC) -> list[Launch]:
    """``jobs``: the parsed jobs (``job_id``, ``dataset``, ``started_at``).
    ``records``: ``{execution id: (name, [job ids], launched at)}``, the builder's
    run records, the time in seconds. Returns the launches, newest first, each job in exactly one."""
    records = records or {}
    by_id = {j.job_id: j for j in jobs}
    placed: set[str] = set()
    out: list[Launch] = []
    for key, (name, ids, launched) in records.items():
        mine = [i for i in ids if i in by_id and i not in placed]
        pending = [i for i in ids if i not in by_id and i not in placed]
        if not mine and not pending:
            continue
        placed.update(mine + pending)
        written = sorted((by_id[i] for i in mine), key=_start_key)
        ids = {_launch_id(j) for j in written} - {""}
        home = next((l for l in out if l.launch_ids & ids), None)
        if home is not None:                     # another shard of a launch a record placed already
            _join(home, written, pending)
            continue
        out.append(Launch(f"run:{key}", "record", _dataset_of(written), _earliest(written) or _launched_at(launched),
                          [j.job_id for j in written], pending, name, ids))
    rest = sorted((j for j in jobs if j.job_id not in placed), key=_start_key)
    for j in rest:
        dataset = getattr(j, "questions_from", "") or j.dataset
        lid = _launch_id(j)
        home = next((l for l in out if lid and lid in l.launch_ids), None)
        if home is not None:                     # a shard of a launch placed already
            _join(home, [j], [])
            continue
        home = next((l for l in out if l.source == "inferred" and l.dataset == dataset and l.started and j.started_at
                     and 0 <= (j.started_at - l.started).total_seconds() <= window), None)
        if home is None:
            out.append(Launch(f"inferred:{j.job_id}", "inferred", dataset, j.started_at, [j.job_id], launch_ids={lid} - {""}))
        else:
            home.job_ids.append(j.job_id)
            home.launch_ids |= {lid} - {""}
    return sorted(out, key=lambda l: (l.started is not None, l.started.timestamp() if l.started else 0.0), reverse=True)


def _launch_id(j) -> str:
    return str(getattr(j, "launch_id", "") or "")


def _join(home: Launch, jobs, pending) -> None:
    """Shards of a launch placed already join it; its start is the earliest of them all."""
    home.job_ids += [j.job_id for j in jobs]
    home.pending += [p for p in pending if p not in home.pending]
    starts = [s for s in [home.started] + [j.started_at for j in jobs] if s]
    home.started = min(starts) if starts else None
    home.launch_ids |= {_launch_id(j) for j in jobs} - {""}


def _start_key(j):
    s = j.started_at
    return (s is None, s.timestamp() if s else 0.0, j.job_id)


def _launched_at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc) if seconds else None    # aware, as a job's stamps are (parse._iso)


def _earliest(jobs):
    starts = [j.started_at for j in jobs if j.started_at]
    return min(starts) if starts else None


def _dataset_of(jobs):
    # a runner's arms answer the same questions, the oracle's from a file of its own
    names = {getattr(j, "questions_from", "") or j.dataset for j in jobs if j.dataset}
    return names.pop() if len(names) == 1 else (None if not names else "mixed")
