"""Write a run's failure analysis from a shell: the task flags the trial page's User judge tab writes, and
their import, copy and acceptance.

    nullius flags list        <root> <job id> [--json]
    nullius flags set         <root> <job id> <task> --kind flawed|ambiguous --category C [--evidence E] [--pending [--by NAME]]
    nullius flags accept      <root> <job id> (<task> | --all-pending)
    nullius flags remove      <root> <job id> <task>
    nullius flags copy        <root> <from job id> <to job id>
    nullius flags import      <root> <job id> --from FILE [--prefix P] [--by NAME]
    nullius flags feasibility <root> <job id>

``<root>`` is anything ``nullius runs`` reads. A task is named as the job names it (its ``task_base``, the name
``nullius runs show`` prints). A flag set here is accepted at once, unless ``--pending`` (a flag an agent proposes); a flag copied, imported
or read from the feasibility controls is pending until a person accepts it, and never replaces an accepted one. Each change writes
only the job's ``failure-analysis.json``. Exit codes are those of ``docs/cli/exit-codes.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from nullius.runs import failure_analysis as fa, feasibility, parse

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_INVALID = 4

# the feasibility verdicts that say the task, not the agent, is at fault
FEASIBILITY = {feasibility.TASK_BROKEN: "task broken", feasibility.REFERENCE_REJECTED: "reference rejected",
               feasibility.UNEXPECTED_REWARD: "unexpected reward"}


def _jobs(root):
    if not os.path.isdir(root):
        print(f"no such directory: {root}", file=sys.stderr)
        return None
    return parse.load_all([root])


def _job(jobs, job_id, root):
    """The job of that id, or its exit code: not found, or ambiguous when two jobs under *root* share it."""
    found = [j for j in jobs if j.job_id == job_id]
    if not found:
        known = ", ".join(j.job_id for j in jobs) or "none"
        print(f"no job named {job_id!r} under {root} (jobs there: {known})", file=sys.stderr)
        return EXIT_NOT_FOUND
    if len(found) > 1:
        print(f"ambiguous job id {job_id!r}: {', '.join(j.dir for j in found)}", file=sys.stderr)
        return EXIT_INVALID
    return found[0]


def _task(job, task):
    if task in {t.task_base for t in job.trials}:
        return True
    print(f"no task named {task!r} in {job.job_id}", file=sys.stderr)
    return False


def _list(job, args) -> int:
    a = fa.load(job.dir)
    if args.json:
        print(json.dumps({"job": job.job_id, "labels": {k: asdict(v) for k, v in sorted(a.labels.items())},
                          "flags": {k: asdict(v) for k, v in sorted(a.flags.items())}}, indent=1, ensure_ascii=False))
        return EXIT_OK
    versions = fa.task_versions(job, a.flags)
    rows = []
    for task, f in sorted(a.flags.items()):
        inert = f.task_version is not None and versions.get(task) != f.task_version
        rows.append((task, f.kind, f.category, f.status + (" (another version)" if inert else ""), f.by))
    for trial, lab in sorted(a.labels.items()):
        rows.append((trial, "label", lab.label, lab.note, lab.by))
    # columns padded to their widest cell, the last one left as it is
    widths = [max((len(r[i]) for r in rows), default=0) for i in range(4)]
    for r in rows:
        print(("  ".join(cell.ljust(w) for cell, w in zip(r, widths)) + "  " + r[4]).rstrip())
    return EXIT_OK


def _report(verb, done, unknown=(), skipped=()):
    print(f"{verb} {len(done)}")
    for name in unknown:
        print(f"not in the job: {name}", file=sys.stderr)
    for name in skipped:
        print(f"kept, already accepted: {name}", file=sys.stderr)


def run(args) -> int:
    jobs = _jobs(args.root)
    if jobs is None:
        return EXIT_NOT_FOUND
    job = _job(jobs, args.job_id, args.root)
    if isinstance(job, int):
        return job
    if args.action == "list":
        return _list(job, args)
    by = fa.who()
    tasks = {t.task_base for t in job.trials}

    if args.action == "accept" and args.task and args.all_pending:
        print("accept takes a task or --all-pending, not both", file=sys.stderr)
        return EXIT_USAGE
    if args.action in ("set", "remove") or (args.action == "accept" and not args.all_pending):
        if not args.task:
            print(f"{args.action} needs a task" + (" or --all-pending" if args.action == "accept" else ""),
                  file=sys.stderr)
            return EXIT_USAGE
        if not _task(job, args.task):
            return EXIT_NOT_FOUND

    if args.action == "set":
        version = fa.task_versions(job, [args.task]).get(args.task)
        if args.by and not args.pending:
            print("--by names who proposes a flag: it goes with --pending", file=sys.stderr)
            return EXIT_USAGE
        proposer = args.by or by
        source = f"proposed:{proposer}" if args.pending else "person"
        before = fa.load(job.dir).flags.get(args.task)
        got = fa.update(job.dir, lambda a: a.set_flag(args.task, args.kind, args.category, by=proposer,
                                                      evidence=args.evidence or "", task_version=version,
                                                      source=source))
        if got is None:
            print(f"kept, already accepted: {args.task}", file=sys.stderr)
        else:
            if before is not None and before.status == "pending":
                print(f"replaced the pending flag from {before.source}: {args.task}", file=sys.stderr)
            print(f"flagged {args.task} {args.kind}" + (", pending" if args.pending else ""))
    elif args.action == "accept":
        if not args.all_pending and args.task not in fa.load(job.dir).flags:
            print(f"no flag on {args.task}", file=sys.stderr)
            return EXIT_NOT_FOUND

        def accept(a):
            names = sorted(t for t, f in a.flags.items() if f.status == "pending") if args.all_pending else [args.task]
            for name in names:
                a.accept(name, by=by)
            return names
        _report("accepted", fa.update(job.dir, accept))
    elif args.action == "remove":
        fa.update(job.dir, lambda a: a.remove_flag(args.task))
        print(f"removed {args.task}")
    elif args.action == "copy":
        target = _job(jobs, args.to_job_id, args.root)
        if isinstance(target, int):
            return target
        if target is job:
            print("copy needs two different jobs", file=sys.stderr)
            return EXIT_USAGE
        if job.dataset and target.dataset and job.dataset != target.dataset:
            print(f"{job.job_id} ran {job.dataset}, {target.job_id} ran {target.dataset}: not copied", file=sys.stderr)
            return EXIT_INVALID
        source = fa.load(job.dir)
        target_tasks = {t.task_base for t in target.trials}
        copied, unknown, skipped = fa.update(target.dir, lambda a: fa.copy_flags(source, a, job.job_id, target_tasks,
                                                                                  by=by))
        _report(f"copied to {target.job_id}, pending:", copied, unknown, skipped)
    elif args.action == "import":
        try:
            with open(args.from_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as e:
            print(f"cannot read {args.from_file}: {e}", file=sys.stderr)
            return EXIT_NOT_FOUND
        except ValueError as e:
            print(f"{args.from_file} is not JSON: {e}", file=sys.stderr)
            return EXIT_INVALID
        records = raw.get("flags") if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            print(f"{args.from_file} holds no list of flags", file=sys.stderr)
            return EXIT_INVALID
        versions = fa.task_versions(job, {args.prefix + str(r.get("task", "")) for r in records if isinstance(r, dict)})
        source = "import:" + os.path.basename(args.from_file)
        added, unknown, skipped = fa.update(job.dir, lambda a: fa.import_flags(
            a, records, tasks, source=source, by=args.by or by, prefix=args.prefix, versions=versions))
        _report("imported, pending:", added, unknown, skipped)
    elif args.action == "feasibility":
        report = feasibility.report(job, jobs)
        if not report.executed:
            print(f"no nop or oracle control found for {job.job_id}", file=sys.stderr)
            return EXIT_NOT_FOUND
        versions = fa.task_versions(job)
        controls = [j for c in report.executed for j in jobs if j.job_id == c.job_id and j is not job]
        other = {t for cj in controls for t, v in fa.task_versions(cj).items() if versions.get(t) not in (None, v)}
        for t in sorted(other):
            print(f"control ran another version: {t}", file=sys.stderr)
        records = [{"task": r.task_base, "kind": "flawed", "category": FEASIBILITY[r.verdict], "evidence": r.why}
                   for r in report.rows if r.verdict in FEASIBILITY and r.task_base not in other]
        added, unknown, skipped = fa.update(job.dir, lambda a: fa.import_flags(
            a, records, tasks, source="import:feasibility", by=by, versions=versions))
        _report("imported, pending:", added, unknown, skipped)
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nullius flags", description="Write a run's task flags.",
                                epilog="exit codes: 0 done, 2 usage, 3 not found, 4 invalid input")
    sub = p.add_subparsers(dest="action", required=True)
    for name, helptext in (("list", "show the run's flags and labels"), ("set", "flag a task: accepted at once, or pending with --pending"),
                           ("accept", "accept a pending flag"), ("remove", "remove a task's flag"),
                           ("copy", "copy accepted flags to another run of the same dataset, as pending"),
                           ("import", "import flags from a file, as pending"),
                           ("feasibility", "import the feasibility controls' task verdicts, as pending")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("root")
        s.add_argument("job_id")
        if name in ("set", "accept", "remove"):
            s.add_argument("task", nargs="?" if name == "accept" else None)
        if name == "list":
            s.add_argument("--json", action="store_true")
        if name == "set":
            s.add_argument("--kind", required=True, choices=fa.KINDS)
            s.add_argument("--category", required=True)
            s.add_argument("--evidence")
            s.add_argument("--pending", action="store_true",
                           help="wait for a person to accept it: for a flag an agent proposes")
            s.add_argument("--by", help="who proposes it, e.g. the agent's name (with --pending)")
        if name == "accept":
            s.add_argument("--all-pending", action="store_true")
        if name == "copy":
            s.add_argument("to_job_id")
        if name == "import":
            s.add_argument("--from", dest="from_file", required=True)
            s.add_argument("--prefix", default="")
            s.add_argument("--by", help="who proposes the flags, e.g. the skill's name")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except fa.FailureAnalysisError as e:
        print(str(e), file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
