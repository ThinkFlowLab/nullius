"""Compare runs from a shell: the totals /compare renders, whole or matched.

    PYTHONPATH=app python3 -m nullius.compare.cli <root> <job a> <job b> [<job c>...]
    PYTHONPATH=app python3 -m nullius.compare.cli <root> <job a> <job b> --matched
    PYTHONPATH=app python3 -m nullius.compare.cli <root> <job a> <job b> --json

``<root>`` is what the other CLIs take: a Harbor job directory, a parent of
several, or an imported tree; ``parse.load_all`` walks it. The jobs are
resolved and the cohort built by ``nullius.compare.comparison`` — the page's
own computation, not a second one — so ``--matched`` keeps exactly the
trials the page keeps (the intersection of exact dataset/task identifiers,
every recorded attempt), and the totals printed per column are the ``Job``
properties the page's summary table reads over that cohort: trials, the
mean score and its breakdown when the benchmark scores, solved, the two
solve rates, infra errors, regressions, the token block, the
estimated cost and, for whole runs only, the wall-clock interval. A matched
cohort has no truthful interval, so none is printed for it — as on the page.

The task rows below the totals are the page's: one per dataset/task, the
outcome recorded in each column with its score, the attempts each column made, the rows
that differ first. A development build (source or unknown runtime) is
compared when named, with a note on stderr; the page asks for a tick.

Reading only: nothing is written, anywhere, and no socket is opened. The
exit codes are the contract in ``docs/cli.md``, shared by the CLIs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from nullius.compare import comparison
from nullius.runs import parse

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_FAILED = 4

EPILOG = """\
examples:
  PYTHONPATH=app python3 -m nullius.compare.cli jobs 2026-09-12__22-53-55 2026-09-13__09-10-11
  PYTHONPATH=app python3 -m nullius.compare.cli jobs 2026-09-12__22-53-55 2026-09-13__09-10-11 --matched
  PYTHONPATH=app python3 -m nullius.compare.cli jobs 2026-09-12__22-53-55 2026-09-13__09-10-11 --json

exit codes:
  0  done
  1  unexpected error (a traceback: report it)
  2  usage: a bad flag, a missing argument, fewer than two different job ids
  3  input not found: no such root, no job under it, no job of that id
  4  the operation could not be completed: a job id that occurs more than once under the root
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""

# The totals the page's summary table reads off each column's Job, in its
# order. Each is a Job property over the cohort's trials — comparison.build
# replaced the trial list, the arithmetic stayed Job's.
TOTALS = (
    ("n_trials", "Trials"),
    ("mean_score", "Mean score"),
    ("n_resolved", "Solved"),
    ("raw_solve_rate", "Raw rate"),
    ("adjusted_solve_rate", "Adjusted rate"),
    ("n_infra", "Infra errors"),
    ("n_regressions", "Broke tests"),
    ("total_input_tokens", "Prompt size"),
    ("fresh_input_tokens", "Fresh prompt"),
    ("total_cache_tokens", "Cached"),
    ("cache_ratio", "Cached ratio"),
    ("total_output_tokens", "Output tokens"),
    ("total_cost_usd", "Cost (estimated)"),
    ("duration_min", "Run wall time"),
)


def _stamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _ratio(part, whole):
    return (part / whole) if (part and whole) else None


def totals_of(job: parse.Job, cohort: str) -> dict:
    """The column's totals as the page's summary rows read them."""
    doc = {
        "n_trials": len(job.trials),
        # A benchmark that scores (an F1, partial credit): the mean over every
        # part scored, and by the field it breaks the score down by. A run
        # with a score and no pass or fail has nothing solved to count.
        "mean_score": job.mean_score,
        "score_by_group": job.score_by_group,
        "parts_by": job.parts_by,
        "scored_only": job.scored_only,
        "n_resolved": job.n_resolved,
        "raw_solve_rate": job.raw_solve_rate,
        "adjusted_solve_rate": job.adjusted_solve_rate,
        "n_infra": job.n_infra,
        "n_regressions": job.n_regressions,
        "total_input_tokens": job.total_input_tokens,
        "fresh_input_tokens": job.total_input_tokens - job.total_cache_tokens,
        "total_cache_tokens": job.total_cache_tokens,
        "cache_ratio": _ratio(job.total_cache_tokens, job.total_input_tokens),
        "total_output_tokens": job.total_output_tokens,
        "total_cost_usd": job.total_cost_usd,
    }
    if cohort == "whole":
        # No truthful wall-clock interval exists for a subset of parallel trials.
        doc["duration_min"] = job.duration_min
    return doc


def column(job: parse.Job, cohort: str) -> dict:
    return {
        "job_id": job.job_id, "dir": job.dir, "dataset": job.dataset,
        "model": job.model_name, "agent": job.agent_label, "kind": job.kind,
        "is_control": job.is_control,
        "counts": job.counts, "totals": totals_of(job, cohort),
    }


def document(view: comparison.Comparison) -> dict:
    """The comparison as the page renders it: the columns with their totals
    over the cohort, and the task rows."""
    return {
        "cohort": view.cohort,
        "runs": [column(job, view.cohort) for job in view.jobs],
        "shared_tasks": view.shared_tasks,
        "excluded_trials": list(view.excluded_trials),
        "repeated_trials": list(view.repeated_trials),
        "datasets_match": view.datasets_match,
        "datasets_known": view.datasets_known,
        "n_differing": view.n_differing,
        "tasks": [{
            "dataset": row.dataset, "task": row.task, "differs": row.differs,
            "shared": row.shared, "attempts": list(row.attempts),
            "trials": [t.trial_name if t else None for t in row.trials],
            "outcomes": [t.outcome if t else None for t in row.trials],
            "scores": [t.score if t else None for t in row.trials],
        } for row in view.tasks],
    }


def _cell(key: str, value) -> str:
    if value is None:
        return "—"
    if key in ("raw_solve_rate", "adjusted_solve_rate", "cache_ratio"):
        return f"{100 * value:.0f}%"
    if key == "total_cost_usd":
        return f"${value:.4f}"
    if key == "mean_score":
        return f"{value:.2f}"
    if key == "duration_min":
        return f"{value:.0f}m"
    if key in ("n_infra", "n_regressions") and not value:
        return "—"
    return str(value)


def _print(view: comparison.Comparison) -> None:
    doc = document(view)
    columns = doc["runs"]
    width = max(12, *(len(c["job_id"]) for c in columns))
    print(f"{view.cohort} runs: " + " vs ".join(c["job_id"] for c in columns))
    if view.cohort == "matched":
        print(f"  shared tasks {view.shared_tasks} · excluded trials "
              + ", ".join(str(n) for n in view.excluded_trials)
              + (" · repeated attempts " + ", ".join(str(n) for n in view.repeated_trials)
                 if any(view.repeated_trials) else ""))
    if not view.datasets_match:
        print("  note: the runs record different datasets; task rows are kept apart by dataset")
    if not view.datasets_known:
        print("  note: a run records no dataset; its tasks cannot be matched by name alone")
    rows = []
    for key, label in TOTALS:
        if key not in columns[0]["totals"]:
            continue
        if key == "mean_score":
            if all(c["totals"]["mean_score"] is None for c in columns):
                continue
            rows.append((label, [_cell(key, c["totals"][key]) for c in columns]))
            by = next((c["totals"]["parts_by"] for c in columns if c["totals"]["parts_by"]), "")
            groups = dict.fromkeys(g["group"] for c in columns for g in c["totals"]["score_by_group"])
            for g in groups:
                means = [next((x["mean"] for x in c["totals"]["score_by_group"] if x["group"] == g), None) for c in columns]
                rows.append((f"{by.capitalize()} {g}", [_cell(key, m) for m in means]))
            continue
        if key == "n_resolved":
            rows.append((label, ["—" if c["totals"]["scored_only"] else _cell(key, c["totals"][key]) for c in columns]))
            continue
        rows.append((label, [_cell(key, c["totals"][key]) for c in columns]))
    label_width = max(len(label) for label, _ in rows)
    print(f"  {'':<{label_width}}  " + "  ".join(f"{c['job_id']:>{width}}" for c in columns))
    for label, cells in rows:
        print(f"  {label:<{label_width}}  " + "  ".join(f"{cell:>{width}}" for cell in cells))
    if not doc["tasks"]:
        print("  no tasks in this comparison cohort")
        return
    print(f"  tasks: {len(doc['tasks'])} · differing {view.n_differing}")
    task_width = max(len(row["task"]) for row in doc["tasks"])
    for row in doc["tasks"]:
        cells = []
        for outcome, score, attempts in zip(row["outcomes"], row["scores"], row["attempts"]):
            cell = outcome or "—"
            if score is not None:
                cell += f" {score:.2f}"
            if attempts > 1:
                cell += f" ×{attempts}"
            cells.append(cell)
        mark = "≠" if row["differs"] else " "
        print(f"    {mark} {row['task']:<{task_width}}  " + "  ".join(f"{c:>{width}}" for c in cells))


def _emit(doc) -> None:
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=1, default=_stamp)
    sys.stdout.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m nullius.compare.cli", description=__doc__.splitlines()[0],
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", metavar="ROOT", help="the root the jobs are under")
    ap.add_argument("job_ids", metavar="JOB_ID", nargs="+",
                    help="two or more job ids, as `runs.cli list` prints them; one column each")
    ap.add_argument("--matched", action="store_true",
                    help="the matched cohort: only the tasks every run attempted, by exact "
                         "dataset and task id (the page's cohort=matched); whole runs otherwise")
    ap.add_argument("--json", action="store_true",
                    help="one JSON document: the columns with their totals and the task rows")
    args = ap.parse_args(argv)
    ids = list(dict.fromkeys(args.job_ids))
    if len(ids) < 2:
        ap.error("two different job ids are needed to compare")
    if not os.path.isdir(args.root):
        print(f"no such directory: {args.root}", file=sys.stderr)
        return EXIT_NOT_FOUND
    jobs = parse.load_all([args.root])
    if not jobs:
        print(f"no job directory found under {args.root}", file=sys.stderr)
        return EXIT_NOT_FOUND
    known = ", ".join(j.job_id for j in jobs)
    for job_id in ids:
        n = sum(1 for j in jobs if j.job_id == job_id)
        if n == 0:
            print(f"no job named {job_id!r} under {args.root} (jobs there: {known})", file=sys.stderr)
            return EXIT_NOT_FOUND
        if n > 1:
            print(f"job id {job_id!r} occurs {n} times under {args.root}; "
                  "name a root where it is unique", file=sys.stderr)
            return EXIT_FAILED
    selected = comparison.resolve([comparison.RunRef("", job_id).value for job_id in ids],
                                  {"": jobs}, {"": args.root})
    view = comparison.build(selected, "matched" if args.matched else "whole")
    if args.json:
        _emit(document(view))
    else:
        _print(view)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
