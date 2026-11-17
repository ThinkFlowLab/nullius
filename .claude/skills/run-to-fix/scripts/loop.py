#!/usr/bin/env python3
"""Counting and bookkeeping of the run-to-fix loop. Standard library only.

  read      stage 1: health and metrics of the run and its baseline; refuses a run that is not analysable
  classify  stage 2: one class and signature per trial, groups and their warnings
  candidate stage 3: register a fix candidate of a cause
  check     stage 3: the candidate's test, the agent's suite and the replay, each in a worktree
  decide    stop 1 or stop 2: write the decision page
  answer    record the person's answer of a stop
  select    stage 4: the tasks of the trial run and their price; with --size, the build and the run command
  compare   stage 5: a new run against the round's run or baseline, the verdict, the stop rule

Every file is written under the loop folder; see references/files.md.
"""
from __future__ import annotations

import argparse
import sys
from typing import List

from adapter import AdapterError
from checking import cmd_candidate, cmd_check
from classifying import cmd_classify
from comparing import cmd_compare
from deciding import cmd_answer, cmd_decide
from found import RuleError
from reading import cmd_read
from rounds import LoopError
from selecting import cmd_select
from trials import TrialError


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rd = sub.add_parser("read", help="stage 1: read the run and its baseline")
    rd.add_argument("--loop", required=True, help="the loop folder a person named; it holds adapter.json")
    rd.add_argument("--run", required=True, help="the job folder of the run, or a folder holding it as PATH::JOB")
    rd.add_argument("--baseline", help="the job folder the run is compared with, as for --run")
    rd.add_argument("--budget-usd", type=float, required=True, help="the budget the person named for this round")
    rd.add_argument("--pass-threshold", type=float, default=1.0, help="reward at or above this is a pass (default 1)")
    rd.add_argument("--continue-after-stop", help="the person's words to start a round after a stop rule said stop")
    rd.set_defaults(func=cmd_read)
    timeout = argparse.ArgumentParser(add_help=False)
    timeout.add_argument("--command-timeout-s", type=float, default=3600.0,
                         help="an adapter command still running after this many seconds is stopped (default 3600)")
    cl = sub.add_parser("classify", parents=[timeout], help="stage 2: classes, signatures and groups of the open round")
    cl.add_argument("--loop", required=True)
    cl.set_defaults(func=cmd_classify)
    ca = sub.add_parser("candidate", help="stage 3: register a fix candidate")
    ca.add_argument("--loop", required=True)
    ca.add_argument("--id", required=True, help="the candidate's id, as in candidates/<id>/")
    ca.add_argument("--cause", required=True, help="the cause it fixes, as in causes/<cause>.md")
    ca.add_argument("--ref", required=True, help="the commit of the fix in the agent's checkout")
    ca.add_argument("--test", help="the test file of the cause, relative to the checkout")
    ca.add_argument("--test-ref", help="the commit that adds the test, before the fix")
    ca.set_defaults(func=cmd_candidate)
    ch = sub.add_parser("check", parents=[timeout], help="stage 3: check a candidate")
    ch.add_argument("--loop", required=True)
    ch.add_argument("--candidate", required=True)
    ch.set_defaults(func=cmd_check)
    de = sub.add_parser("decide", help="write the page of stop 1 or stop 2")
    de.add_argument("--loop", required=True)
    de.add_argument("--stop", type=int, required=True, choices=(1, 2))
    de.add_argument("--trial-job", help="stop 2: the job folder of the trial run")
    de.set_defaults(func=cmd_decide)
    an = sub.add_parser("answer", help="record the person's answer of a stop")
    an.add_argument("--loop", required=True)
    an.add_argument("--stop", type=int, required=True, choices=(1, 2))
    an.add_argument("--text", required=True, help="the person's words")
    an.add_argument("--keep", help="stop 1: candidate ids separated by commas, or none")
    an.add_argument("--choice", help="stop 2: stop, rerun or full")
    an.set_defaults(func=cmd_answer)
    se = sub.add_parser("select", parents=[timeout], help="stage 4: propose and choose the trial run")
    se.add_argument("--loop", required=True)
    se.add_argument("--ref", required=True, help="the commit to try; it contains every kept candidate")
    se.add_argument("--canaries", type=int, required=True, help="how many passing tasks to draw as canaries")
    se.add_argument("--size", choices=("targets", "guards", "canaries"),
                    help="the person's choice: targets, targets and guards, or all three")
    se.add_argument("--budget-usd", type=float, help="the budget the person named for the trial run")
    se.add_argument("--text", help="the person's words")
    se.set_defaults(func=cmd_select)
    cp = sub.add_parser("compare", help="stage 5: compare a new run and close the round")
    cp.add_argument("--loop", required=True)
    cp.add_argument("--new", required=True, help="the job folder of the new run, as for read --run")
    cp.add_argument("--against", required=True, choices=("run", "baseline"),
                    help="compare with the round's run (the code before the fix) or with its baseline")
    cp.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (LoopError, AdapterError, RuleError, TrialError) as exc:
        print(f"run-to-fix: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
