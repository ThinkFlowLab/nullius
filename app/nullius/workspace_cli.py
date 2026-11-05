"""List the workspaces from a shell: what /workspaces shows.

    PYTHONPATH=app python3 -m nullius.workspace_cli list            # under WORKSPACES_DIR, or workspaces/
    PYTHONPATH=app python3 -m nullius.workspace_cli list --home DIR
    PYTHONPATH=app python3 -m nullius.workspace_cli list --json

A workspace is a named set of job roots and what the app read from them
(``nullius.workspace``). ``list`` reads each ``workspace.json`` under the
home directory the way the page does (``workspace.list_all``: most recently
opened first) and prints, per workspace, its name, its label, its roots, the
runs its parse cache holds (the page's *Cached runs*, and their ids from the
cache's index, both known without parsing anything) and when it was last
opened. It opens nothing: no workspace becomes active, no root is walked,
no import converted, no cache touched.

The home is ``--home``, else ``WORKSPACES_DIR``, else ``workspaces/`` in the
working directory — the app's own rule (``workspace.home``). Renaming and
deleting stay the page's: a delete removes the cache and the converted
imports behind a confirmation, and nothing here writes.

Reading only. The exit codes are the contract in ``docs/cli.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from nullius import workspace

EXIT_OK = 0
EXIT_NOT_FOUND = 3

EPILOG = """\
examples:
  PYTHONPATH=app python3 -m nullius.workspace_cli list
  WORKSPACES_DIR=/srv/nullius/workspaces PYTHONPATH=app python3 -m nullius.workspace_cli list --json
  PYTHONPATH=app python3 -m nullius.workspace_cli list --home /tmp/ws

exit codes:
  0  done
  1  unexpected error (a traceback: report it)
  2  usage: a bad flag, a missing argument
  3  input not found: no such home directory, no workspace under it
  4  the operation could not be completed
With --json, stdout is exactly one JSON document; everything else goes to stderr.
"""


def row(ws: workspace.Workspace) -> dict:
    """One workspace as the page's table shows it, plus the job ids its
    cache index names — the rail's list, known without a parse."""
    index = workspace.job_index(ws)
    return {
        "name": ws.name,
        "label": ws.label,
        "dir": ws.dir,
        "roots": list(ws.roots),
        "job_roots": list(ws.job_roots),
        "n_cached": ws.n_cached,
        "jobs": [{"dir": job_dir, "job_id": entry.get("job_id"), "n_trials": entry.get("n_trials"),
                  "dataset": entry.get("dataset"), "agent": entry.get("agent")}
                 for job_dir, entry in index.items() if isinstance(entry, dict)],
        "created": ws.created,
        "opened": ws.opened,
    }


def _emit(doc) -> None:
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")


def _list(args) -> int:
    home = args.home or workspace.home()
    if not os.path.isdir(home):
        print(f"no such directory: {home}", file=sys.stderr)
        return EXIT_NOT_FOUND
    found = workspace.list_all(home)
    if not found:
        print(f"no workspace found under {home}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        _emit([row(ws) for ws in found])
        return EXIT_OK
    width = max(len(ws.name) for ws in found)
    for ws in found:
        r = row(ws)
        opened = (ws.opened or "")[:16].replace("T", " ")
        print(f"{ws.name:<{width}}  {ws.label}")
        print(f"{'':<{width}}  cached runs {r['n_cached']} · opened {opened or '—'}")
        for root in ws.roots:
            print(f"{'':<{width}}  root {root}")
        for job in r["jobs"]:
            print(f"{'':<{width}}  job {job['job_id']} ({job['n_trials']} trial(s))")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m nullius.workspace_cli", description=__doc__.splitlines()[0],
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    ls = sub.add_parser("list", help="the workspaces under the home directory, most recently opened first",
                        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ls.add_argument("--home", metavar="DIR", default="",
                    help="the workspaces directory (default: WORKSPACES_DIR, else workspaces/)")
    ls.add_argument("--json", action="store_true",
                    help="one JSON array, a row per workspace, instead of the listing")
    ls.set_defaults(run=_list)
    args = ap.parse_args(argv)
    if not args.command:
        ap.error("a command is required: list")
    return args.run(args)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
