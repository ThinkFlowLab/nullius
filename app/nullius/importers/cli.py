"""Turn a foreign harness's run tree into a job directory this app can walk.

    PYTHONPATH=app python3 -m nullius.importers.cli <run tree> -o <job root>
    PYTHONPATH=app python3 -m nullius.importers.cli <run tree> -o <job root> --json
    PYTHONPATH=app python3 -m nullius.importers.cli --list

The harness is detected from the directory's own shape; ``--harness NAME``
names one outright when detection would be ambiguous or a tree is unusual.
Each importer lives in ``nullius/importers/`` and registers by being
there, so this CLI never names one and gains nothing when one is added or
loses anything when one is deleted. With none registered ``--list`` says
``no importers registered`` rather than printing nothing, and every run tree
is refused as unrecognised.

Every job written is named on stdout by its id — the directory's basename,
which is what ``nullius.runs.cli list`` prints and ``/job/<id>`` opens — and
``--json`` gives the same as one document. The source tree is never edited;
the only writes land under ``-o``. The exit codes are the contract in
``docs/cli.md``, shared by every CLI here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from nullius import importers as harnesses

# Exit codes — docs/cli.md is the contract; the other two CLIs use the same
# numbers. 1 is left to Python itself: an uncaught exception is a bug.
EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_FAILED = 4

EPILOG = """\
examples:
  PYTHONPATH=app python3 -m nullius.importers.cli /path/to/logs -o jobs_imported
  PYTHONPATH=app python3 -m nullius.importers.cli /path/to/logs -o jobs_imported --json
  PYTHONPATH=app python3 -m nullius.importers.cli --list

exit codes:
  0  done
  1  unexpected error (a traceback: report it)
  2  usage: a bad flag, a missing argument, an unknown --harness
  3  input not found: no such run tree, or no importer recognised it
  4  the importer refused the tree: nothing under it could be converted
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m nullius.importers.cli", description=__doc__.splitlines()[0],
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="the harness's run tree")
    ap.add_argument("-o", "--out", default="jobs_imported",
                    help="job root to write into (default: jobs_imported)")
    ap.add_argument("--harness", default="",
                    help="name an importer instead of detecting one")
    ap.add_argument("--job-name", default="", help="name for the job directory")
    ap.add_argument("--command", default="",
                    help="the invocation that produced the run, shown verbatim "
                         "on the job page")
    ap.add_argument("--list", action="store_true", help="list known harnesses")
    ap.add_argument("--json", action="store_true",
                    help="one JSON document naming the harness and every job written")
    args = ap.parse_args(argv)

    known = harnesses.available()
    if args.list:
        if args.json:
            json.dump({name: (module.__doc__ or "").strip().splitlines()[0]
                       for name, module in known.items()}, sys.stdout, indent=1)
            sys.stdout.write("\n")
            return EXIT_OK
        # An empty registry is a state, not a crash - but printing nothing
        # would read exactly like a CLI that imported and did no work.
        if not known:
            print("no importers registered")
        for name, module in known.items():
            summary = (module.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:<10} {summary}")
        return EXIT_OK

    if not args.run_dir:
        ap.error("run_dir is required (or pass --list)")

    if args.harness:
        module = harnesses.get(args.harness)
        if module is None:
            ap.error(f"unknown harness {args.harness!r}; known: {', '.join(known)}")
    if not os.path.isdir(args.run_dir):
        print(f"no such directory: {args.run_dir}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if not args.harness:
        module = harnesses.detect(args.run_dir)
        if module is None:
            print(f"no importer recognised {args.run_dir}; "
                  f"pass --harness (known: {', '.join(known) or 'none'})", file=sys.stderr)
            return EXIT_NOT_FOUND

    # Use the importer's tree boundaries so a parent can yield several jobs.
    # Report refusal messages on stderr with EXIT_FAILED; preserve numeric exits.
    try:
        job_dirs = []
        for tree in harnesses.trees(module, args.run_dir) or [args.run_dir]:
            job_dirs += module.convert(tree, args.out, args.job_name if tree == args.run_dir else "", args.command)
    except SystemExit as e:
        if isinstance(e.code, int):
            raise
        print(e.code, file=sys.stderr)
        return EXIT_FAILED

    jobs = []
    for job_dir in job_dirs:
        trials = sum(
            1 for name in os.listdir(job_dir)
            if os.path.isfile(os.path.join(job_dir, name, "result.json"))
        )
        jobs.append({"job_id": os.path.basename(os.path.normpath(job_dir)),
                     "dir": job_dir, "trials": trials})
    if args.json:
        json.dump({"harness": module.NAME, "out": args.out, "jobs": jobs},
                  sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return EXIT_OK
    for job in jobs:
        print(f"{module.NAME}: wrote {job['trials']} trial(s) to {job['dir']} "
              f"(job id {job['job_id']})")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
