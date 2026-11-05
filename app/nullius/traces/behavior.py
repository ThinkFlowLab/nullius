"""Behavioral metrics over a parsed trajectory.

The outcome says *whether* the agent solved the task; these numbers say *how*
it worked — how long before it committed to an edit, whether it re-read what
it had already seen, whether it re-ran commands that had already failed. Two
resolved trials can hide very different amounts of wasted work, and the waste
is what token cost scales with.

Everything here is a pure function of data the app already loads; nothing
reaches back into the job directory.
"""

from __future__ import annotations

from dataclasses import dataclass

# Public in trajectory.py so this module need not reach into an underscore.
from nullius.traces.trajectory import PATCH_PATHS as _PATCH_PATHS
from nullius.traces.trajectory import parse_ts as _parse_ts


@dataclass
class Behavior:
    # 1-based position of the first edit among the run's tool calls, and the
    # wall-clock seconds it took to get there (None without a step clock).
    first_edit_call: int | None = None
    time_to_first_edit: float | None = None
    # read/search calls whose exact target had already been fetched before.
    n_redundant_reads: int = 0
    # calls repeating a (name, target) pair that had already failed — retrying
    # the same thing and hoping is the signature of a flailing run.
    n_repeated_failed_calls: int = 0
    # completed edit→execute alternations: how many times an edit was actually
    # followed by running something (tests, the repro, anything).
    edit_test_cycles: int = 0
    n_search: int = 0
    n_read: int = 0
    n_edit: int = 0
    # (search + read) per edit — how much looking preceded each change.
    exploration_per_edit: float | None = None


def analyze(traj) -> Behavior:
    """Compute a Behavior from a loaded Trajectory."""
    b = Behavior()

    seen_fetches: set = set()
    failed_keys: set = set()
    edit_pending = False
    first_edit_step_id = None

    for i, call in enumerate(traj.calls, start=1):
        cat = call["category"]
        target = call["target"]
        key = (call["name"], target)

        if cat == "search":
            b.n_search += 1
        elif cat == "read":
            b.n_read += 1
        elif cat == "edit":
            b.n_edit += 1

        if cat in ("read", "search") and target:
            fetch = (cat, target)
            if fetch in seen_fetches:
                b.n_redundant_reads += 1
            seen_fetches.add(fetch)

        if key in failed_keys:
            b.n_repeated_failed_calls += 1
        if call["failure"]:
            failed_keys.add(key)

        if cat == "edit":
            edit_pending = True
            if b.first_edit_call is None:
                b.first_edit_call = i
                first_edit_step_id = call["step_id"]
        elif cat == "execute" and edit_pending:
            b.edit_test_cycles += 1
            edit_pending = False

    if b.n_edit:
        b.exploration_per_edit = round((b.n_search + b.n_read) / b.n_edit, 2)

    if traj.has_step_clock and first_edit_step_id is not None:
        start = next((t for t in (_parse_ts(s.timestamp) for s in traj.steps) if t), None)
        at_edit = next(
            (_parse_ts(s.timestamp) for s in traj.steps if s.step_id == first_edit_step_id),
            None,
        )
        if start and at_edit and at_edit >= start:
            b.time_to_first_edit = (at_edit - start).total_seconds()

    return b


def gold_files(solve_text: str) -> list:
    """File paths named in a task's gold patch (``solution/solve.sh``).

    A solve script that embeds the gold diff in a heredoc is read with the
    diff-header patterns used on agent patch payloads, unchanged.
    """
    found = []
    for pattern in _PATCH_PATHS:
        for path in pattern.findall(solve_text or ""):
            if path not in found and path != "/dev/null":
                found.append(path)
    return found


@dataclass
class Localization:
    hit: list        # gold files the agent edited
    missed: list     # gold files it never touched
    extra: list      # files it edited that the gold patch doesn't name


def _same_file(a: str, b: str) -> bool:
    # Agent paths may be container-absolute (/testbed/sklearn/...), gold paths
    # repo-relative (sklearn/...). Suffix match on a path boundary covers both.
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def localization(touched, gold) -> Localization:
    """Compare the files the agent edited against the gold patch's files."""
    hit = [g for g in gold if any(_same_file(t, g) for t in touched)]
    missed = [g for g in gold if g not in hit]
    extra = [t for t in touched if not any(_same_file(t, g) for g in gold)]
    return Localization(hit=hit, missed=missed, extra=extra)
