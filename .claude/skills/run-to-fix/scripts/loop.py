#!/usr/bin/env python3
"""Counting and bookkeeping of the run-to-fix loop. Standard library only.

  read      stage 1: health and metrics of the run and its baseline; refuses a run that is not analysable
  classify  stage 2: one class and signature per trial, groups and their warnings
  compare   stage 5: a new run against the round's run or baseline, the verdict, the stop rule

Every file is written under the loop folder; see references/files.md.
"""
from __future__ import annotations

import argparse
import sys
from typing import List

from adapter import AdapterError
from classifying import cmd_classify
from comparing import cmd_compare
from found import RuleError
from reading import cmd_read
from rounds import LoopError
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
    cl = sub.add_parser("classify", help="stage 2: classes, signatures and groups of the open round")
    cl.add_argument("--loop", required=True)
    cl.add_argument("--command-timeout-s", type=float, default=3600.0,
                    help="an adapter command still running after this many seconds is stopped (default 3600)")
    cl.set_defaults(func=cmd_classify)
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
