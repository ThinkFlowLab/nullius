"""Read the runs under a job root from a shell: what /runs and /job/<id> show.

    PYTHONPATH=app python3 -m nullius.runs.cli list <root>...          # job ids, one per line
    PYTHONPATH=app python3 -m nullius.runs.cli list <root> --json      # the runs table
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id>    # the trials and their outcomes
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id> --json
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id> <trial>   # one trial: what /trial/<job>/<trial> shows
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id> <trial> --json
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id> <trial> --call N --request   # call N's request, rebuilt
    PYTHONPATH=app python3 -m nullius.runs.cli show <root> <job id> <trial> --calls   # the conversation by model call

``<root>`` is anything ``app.py`` accepts as a job root: a Harbor job
directory, a parent of several, or a tree the import CLI has already
converted. The same reader the pages use (``parse.load_all``) walks it, so a
job id printed here is the one ``/job/<id>`` opens, and the outcomes are the
ones its table shows — from the files on disk, never from a workspace cache.
With a trial name, ``show`` reads that trial the way the trial page does —
the outcome and checks, the usage and cost, the exception, and the steps of
``agent/trajectory.json`` through ``traces.trajectory``. With ``--call N
--request`` it prints the request the trial's agent sent in its call N, put
back together from the relay's store of its exchanges
(``standard/calls/exchanges.py``) as the bytes sent, unredacted, and says on stderr
whether their sha256 is the one recorded. With ``--calls`` it prints the
trial's model calls as the Conversation tab shows them (``traces.calls``):
what each sent that the call before had not, its answer whole, the steps
under it; redacted, pictures by their hash.

Reading only: nothing is written, anywhere, and no socket is opened. The
exit codes are the contract in ``docs/cli.md``, shared by every CLI here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime

from nullius.runs import parse
from nullius.standard.calls import exchanges
from nullius.traces import calls as by_call, trajectory

# Exit codes — docs/cli.md is the contract. 1 is left to Python itself: an uncaught exception is a bug, and
# its traceback is the report.
EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_FAILED = 4

EPILOG = """\
examples:
  PYTHONPATH=app python3 -m nullius.runs.cli list jobs
  PYTHONPATH=app python3 -m nullius.runs.cli list jobs jobs_imported --json
  PYTHONPATH=app python3 -m nullius.runs.cli show jobs 2026-09-12__22-53-55 --json
  PYTHONPATH=app python3 -m nullius.runs.cli show jobs 2026-09-12__22-53-55 astropy__astropy-8707__agWFDYq
  PYTHONPATH=app python3 -m nullius.runs.cli show jobs 2026-09-12__22-53-55 astropy__astropy-8707__agWFDYq --call 3 --request
  PYTHONPATH=app python3 -m nullius.runs.cli show jobs 2026-09-12__22-53-55 astropy__astropy-8707__agWFDYq --calls

exit codes:
  0  done
  1  unexpected error (a traceback: report it)
  2  usage: a bad flag, a missing argument
  3  input not found: no such root, no job under it, no job of that id, no trial of that name, no such call
  4  the operation could not be completed: a request that does not rebuild to its recorded sha256
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""


def _stamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def job_row(job: parse.Job) -> dict:
    """One job as the runs table shows it: identity, state, the outcome mix
    and the two solve rates. Derived properties are included by name because
    ``asdict`` cannot see them, and they are what the page renders."""
    return {
        "job_id": job.job_id,
        "dir": job.dir,
        "dataset": job.dataset,
        "model": job.model_name,
        "agent": job.agent_label,
        "kind": job.kind,
        "state": job.state,
        "n_trials": len(job.trials),
        "n_planned": job.n_planned,
        # a job begun on a part of its plan: how the part was named, and how many of the planned tasks have run
        "start_with": job.start_with,
        "n_tasks": job.n_tasks,
        "n_tasks_run": job.n_tasks_run,
        "counts": job.counts,
        "n_resolved": job.n_resolved,
        "n_infra": job.n_infra,
        "raw_solve_rate": job.raw_solve_rate,
        "adjusted_solve_rate": job.adjusted_solve_rate,
        # the mean graded score, over the trials run: a benchmark scored, not solved, has it and no rate
        "mean_score": job.mean_score,
        "started_at": _stamp(job.started_at),
        "finished_at": _stamp(job.finished_at),
    }


def job_detail(job: parse.Job) -> dict:
    """The whole parsed job — every field the workspace cache keeps, plus the
    derived ones the job page renders, per trial too. ``exception_message``
    was redacted on the way in (``parse.parse_trial``), so this is as safe to
    print as the page is to render."""
    data = asdict(job)
    data.update(job_row(job))
    data["harbor_label"] = job.harbor_label
    data["mean_score"] = job.mean_score
    data["mean_score_planned"] = job.mean_score_planned
    for trial, raw in zip(job.trials, data["trials"]):
        raw["outcome_label"] = trial.outcome_label
        raw["ended_label"] = trial.ended_label
        raw["is_infra"] = trial.is_infra
        raw["is_regression"] = trial.is_regression
        raw["has_trace"] = trial.has_trace
        raw["started_at"] = _stamp(trial.started_at)
        raw["finished_at"] = _stamp(trial.finished_at)
        raw["phases"] = {k: [_stamp(a), _stamp(b)] for k, (a, b) in trial.phases.items()}
    return data


def _trial_record(trial: parse.Trial) -> dict:
    """One trial as ``job_detail`` carries it: every parsed field plus the
    derived ones the pages render, timestamps as ISO strings."""
    raw = asdict(trial)
    raw["outcome_label"] = trial.outcome_label
    raw["ended_label"] = trial.ended_label
    raw["is_infra"] = trial.is_infra
    raw["is_regression"] = trial.is_regression
    raw["has_trace"] = trial.has_trace
    raw["started_at"] = _stamp(trial.started_at)
    raw["finished_at"] = _stamp(trial.finished_at)
    raw["phases"] = {k: [_stamp(a), _stamp(b)] for k, (a, b) in trial.phases.items()}
    return raw


def trial_detail(job: parse.Job, trial: parse.Trial) -> dict:
    """What the trial page shows: the trial's record, the check vocabulary
    the dataset names its cards with, and the trajectory as ``trajectory.load``
    reads it — steps with their tool calls by name, category, target and
    failure, and the totals — or the reason it could not be read. Every
    string came through ``redact`` in the reader; nothing is added here.
    Arguments and observations are left out: they are the conversation
    browser's, paged and bounded there, and can be the whole patch."""
    vocab = trial.vocab
    doc = {"job_id": job.job_id, "trial": _trial_record(trial),
           "vocab": {"gained": vocab.gained, "held": vocab.held, "has_patch": vocab.has_patch},
           "trajectory": None, "steps": []}
    if not trial.has_trajectory:
        return doc
    traj = trajectory.load(trial.trajectory_path)
    doc["trajectory"] = {
        "error": traj.error, "n_steps": traj.n_steps, "n_tool_calls": traj.n_tool_calls,
        "n_failed_calls": traj.n_failed_calls, "category_freq": dict(traj.category_freq),
        "files_touched": list(traj.files_touched), "final_metrics": traj.final_metrics,
        "has_step_clock": traj.has_step_clock, "last_agent_message": traj.last_agent_message,
        "total_reasoning_tokens": traj.total_reasoning_tokens,
    }
    doc["steps"] = [{
        "step_id": s.step_id, "source": s.source, "timestamp": s.timestamp, "dur": s.dur,
        "message": s.message,
        "tool_calls": [{"name": c["name"], "category": c["category"], "target": c["target"],
                        "failure": c["failure"], "obs_bytes": c["obs_bytes"]} for c in s.tool_calls],
    } for s in traj.steps]
    return doc


def _first_line(text: str, width: int = 96) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= width else line[:width - 1] + "…"


def _secs(value) -> str:
    return "—" if value is None else f"{value:.2f}s"


def _print_trial(job: parse.Job, trial: parse.Trial) -> None:
    doc = trial_detail(job, trial)
    t, vocab = trial, doc["vocab"]
    print(f"{job.job_id}/{t.trial_name}")
    print(f"  task {t.task_base} · {t.outcome_label} ({t.outcome}) · "
          f"model {t.model_name or job.model_name or '—'} · reward "
          f"{'—' if t.reward is None else t.reward}")
    checks = []
    if vocab["gained"]:
        checks.append(f"{vocab['gained']} {t.f2p_pass} passed, {t.f2p_fail} failed")
    if vocab["held"]:
        checks.append(f"{vocab['held']} {t.p2p_pass} passed, {t.p2p_fail} failed")
    if vocab["has_patch"]:
        checks.append("patch " + ("—" if t.patch_exists is None else
                                  ("applied" if t.patch_applied else "not applied")))
    if checks:
        print("  checks: " + " · ".join(checks))
    cost = "—" if t.cost_usd is None else f"${t.cost_usd:.4f}"
    print(f"  usage: input {t.n_input_tokens} · cached {t.n_cache_tokens} · "
          f"output {t.n_output_tokens} · cost {cost} (estimated)")
    print(f"  time: total {_secs(t.dur_total)} · env {_secs(t.dur_env)} · "
          f"setup {_secs(t.dur_agent_setup)} · agent {_secs(t.dur_agent)} · "
          f"verifier {_secs(t.dur_verifier)}")
    print(f"  evidence: trajectory {'recorded' if t.has_trajectory else 'absent'} · "
          f"trace {'recorded' if t.has_trace else 'absent'} · "
          f"reward {'recorded' if t.reward is not None else 'absent'}")
    if t.ended_by:
        print(f"  ended by: {t.ended_label} ({t.ended_by})")
    if t.exception_type:
        print(f"  exception: {t.exception_type}: {_first_line(t.exception_message)}")
    for name in t.p2p_failures:
        print(f"  broke: {name}")
    traj = doc["trajectory"]
    if traj is None:
        return
    if traj["error"]:
        print(f"  trajectory unreadable: {_first_line(traj['error'])}")
        return
    mix = " · ".join(f"{k} {v}" for k, v in sorted(traj["category_freq"].items()))
    print(f"  steps: {traj['n_steps']} · tool calls {traj['n_tool_calls']} · "
          f"failed {traj['n_failed_calls']}" + (f" · {mix}" if mix else ""))
    width = len(str(max((s["step_id"] for s in doc["steps"]), default=0)))
    for s in doc["steps"]:
        calls = ", ".join(c["name"] + (f" ✗ {c['failure']}" if c["failure"] else "")
                          for c in s["tool_calls"])
        text = _first_line(s["message"]) if s["message"] and s["message"] != "(tool use)" else ""
        body = " · ".join(x for x in (text, calls) if x)
        dur = "" if s["dur"] is None else f"  {s['dur']:.2f}s"
        print(f"    {s['step_id']:>{width}} {s['source']:<6} {body}{dur}")


def _request(trial: parse.Trial, n: int) -> int:
    """Call ``n`` of the trial's agent route rebuilt from its store: the bytes on stdout, the verdict on stderr."""
    folder = os.path.join(trial.dir, ".work", "relay", exchanges.FOLDER)
    routes = exchanges.tags(folder)
    tag = by_call.route_tag(trial.dir, routes)
    if tag not in routes:
        print(f"{trial.trial_name} kept no exchanges (a run before them, or --exchanges off)" if not routes
              else f"{trial.trial_name} holds the exchanges of several routes: {', '.join(routes)}", file=sys.stderr)
        return EXIT_NOT_FOUND
    try:
        got = exchanges.check(folder, tag, n)
    except exchanges.Missing as e:
        print(str(e), file=sys.stderr)
        return EXIT_NOT_FOUND
    if got["request"] is not None:
        sys.stdout.buffer.write(got["request"] + b"\n")
        sys.stdout.flush()
    if got["same"]:
        print(f"call {n}: sha256 {got['sha256']}, the recorded sent_sha256", file=sys.stderr)
        return EXIT_OK
    print(f"call {n}: " + (f"sha256 {got['sha256']}, not the recorded sent_sha256 {got['sent_sha256']}: " if got["sha256"] else "")
          + got["why"], file=sys.stderr)
    return EXIT_FAILED


def _emit(doc) -> None:
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=1, default=_stamp)
    sys.stdout.write("\n")


def _load(roots) -> list | None:
    """The jobs under *roots*, or None (said on stderr) when a root is missing."""
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        for r in missing:
            print(f"no such directory: {r}", file=sys.stderr)
        return None
    return parse.load_all(roots)


def _list(args) -> int:
    jobs = _load(args.roots)
    if jobs is None:
        return EXIT_NOT_FOUND
    if not jobs:
        print(f"no job directory found under {', '.join(args.roots)}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        _emit([job_row(j) for j in jobs])
    else:
        for j in jobs:
            print(j.job_id)
    return EXIT_OK


def _show(args) -> int:
    jobs = _load([args.root])
    if jobs is None:
        return EXIT_NOT_FOUND
    job = next((j for j in jobs if j.job_id == args.job_id), None)
    if job is None:
        known = ", ".join(j.job_id for j in jobs) or "none"
        print(f"no job named {args.job_id!r} under {args.root} (jobs there: {known})",
              file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.trial_name:
        trial = next((t for t in job.trials if t.trial_name == args.trial_name), None)
        if trial is None and (args.call is not None or args.calls):
            trial = next((t for current in job.trials for _, t in parse.tries(current)
                          if t.trial_name == args.trial_name), None)
        if trial is None:
            known = ", ".join(t.trial_name for t in job.trials) or "none"
            print(f"no trial named {args.trial_name!r} in {job.job_id} (trials there: {known})",
                  file=sys.stderr)
            return EXIT_NOT_FOUND
        if args.call is not None:
            return _request(trial, args.call)
        if args.calls:
            traj = trajectory.load(trial.trajectory_path) if trial.has_trajectory else None
            _emit(by_call.document(by_call.view(trial.dir, traj)))
            return EXIT_OK
        if args.json:
            _emit(trial_detail(job, trial))
        else:
            _print_trial(job, trial)
        return EXIT_OK
    if args.json:
        _emit(job_detail(job))
        return EXIT_OK
    row = job_row(job)
    rate = lambda v: "—" if v is None else f"{100 * v:.0f}%"
    print(f"{job.job_id}")
    print(f"  dataset {row['dataset'] or '—'} · model {row['model'] or '—'} · "
          f"agent {row['agent'] or '—'} · {row['state']}")
    print(f"  {row['n_trials']} trial(s) of {row['n_planned']} planned: "
          f"resolved {row['n_resolved']} · infra {row['n_infra']} · "
          f"raw {rate(row['raw_solve_rate'])} · adjusted {rate(row['adjusted_solve_rate'])}")
    if job.start_with:
        words = (job.runner_items or ["task", "tasks"])[1]
        print(f"  begun on a part: {row['n_tasks_run']} of {row['n_tasks']} planned {words} run; "
              f"the rates are over those run")
    mix = " · ".join(f"{k} {v}" for k, v in row["counts"].items() if v)
    if mix:
        print(f"  outcomes: {mix}")
    if job.mean_score is not None:
        planned = job.mean_score_planned
        print(f"  mean score {job.mean_score:.2f} over the trials scored"
              + ("" if planned is None else f" · {planned:.2f} over the {max(job.n_planned, len(job.trials))} planned"))
    width = max((len(t.trial_name) for t in job.trials), default=0)
    for t in job.trials:
        line = f"  {t.trial_name:<{width}}  {t.outcome}"
        if t.ended_by:
            line += f"  ended by {t.ended_by}"
        if t.exception_type:
            line += f"  {t.exception_type}"
        print(line)
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m nullius.runs.cli", description=__doc__.splitlines()[0],
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    ls = sub.add_parser("list", help="the job ids under one or more roots, newest first",
                        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ls.add_argument("roots", nargs="+", metavar="ROOT",
                    help="a job directory, a parent of job directories, or an imported root")
    ls.add_argument("--json", action="store_true",
                    help="one JSON array of job rows (what the runs table shows) instead of ids")
    ls.set_defaults(run=_list)
    sh = sub.add_parser("show", help="one job: its trials and their outcomes; or one trial",
                        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sh.add_argument("root", metavar="ROOT", help="the root the job is under")
    sh.add_argument("job_id", metavar="JOB_ID", help="a job id, as `list` prints it")
    sh.add_argument("trial_name", metavar="TRIAL", nargs="?", default="",
                    help="a trial name, as `show` on the job prints it: that trial's outcome, "
                         "checks, usage, exception and steps, as /trial/<job>/<trial> shows them")
    sh.add_argument("--json", action="store_true",
                    help="the whole parsed job (or the one trial, with its steps) as one JSON document")
    sh.add_argument("--call", type=int, metavar="N",
                    help="with --request and a trial: its agent's call N, as the call record numbers it (n)")
    sh.add_argument("--request", action="store_true",
                    help="with --call: print the request that call sent, rebuilt from the trial's exchanges, and say "
                         "on stderr whether its sha256 is the recorded one")
    sh.add_argument("--calls", action="store_true",
                    help="with a trial: its model calls as one JSON document, as the Conversation tab shows them")
    sh.set_defaults(run=_show)
    args = ap.parse_args(argv)
    if not args.command:
        ap.error("a command is required: list or show")
    if args.command == "show" and (args.call is not None or args.request) and not (args.call is not None and args.request
                                                                                   and args.trial_name and not args.json):
        sh.error("--call N and --request go together, with a trial, without --json")
    if args.command == "show" and args.calls and (not args.trial_name or args.call is not None):
        sh.error("--calls goes with a trial, without --call")
    return args.run(args)


if __name__ == "__main__":  # pragma: no cover - entry point
    # A module, not a
    # script, like its two siblings: without this guard `-m` imports the
    # module, runs nothing and exits 0, which reads exactly like success.
    raise SystemExit(main())
