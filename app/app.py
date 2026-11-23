"""Nullius: the run behind a benchmark score, as a Flask app.

Reads job directories and runs recipes from the builder.

Point it at one or more roots, any of:

    python app/app.py /path/to/jobs             # positional args
    JOB_ROOTS=/a:/b python app/app.py    # env var
    (nothing)                                    # then pick a path in the UI

A root may be a single job directory or a parent holding several.
"""

from __future__ import annotations

import os
import sys

# The package root on the path, so `python app/app.py` and `uv run app/app.py`
# find nullius and web exactly as `-m` would.
if not __package__:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402

from werkzeug.serving import is_running_from_reloader  # noqa: E402

if __package__:  # the wheel's private web namespace
    from .web.core import app
    from .web import shared
    from .web import filters, runs, traces, compare, builder, catalogue, agents, settings, workspaces, execute, judge, feasibility, reconcile, judge_tasks, onboarding
else:  # python app/app.py, and the source route suite
    from web.core import app
    from web import shared
    from web import filters, runs, traces, compare, builder, catalogue, agents, settings, workspaces, execute, judge, feasibility, reconcile, judge_tasks, onboarding
from nullius import execution, workspace  # noqa: E402  - recorded runs, picked up again at start; the workspace left open


def _port(value: str) -> int:
    """A port, from --port or APP_PORT: argparse converts a string default
    too, so a bad APP_PORT stops the start with this line, not a traceback."""
    if value.isdigit() and 1 <= int(value) <= 65535:
        return int(value)
    raise argparse.ArgumentTypeError(f"{value!r} is not a port between 1 and 65535 (APP_PORT in saved settings or environment, or --port)")


def parser() -> argparse.ArgumentParser:
    """The command line; the server's address and port default to APP_HOST
    and APP_PORT (an empty one is unset), read once, here, at start."""
    ap = argparse.ArgumentParser(description="Nullius: the run behind a benchmark score.")
    ap.add_argument("paths", nargs="*", help="job directory (or parent) to scan")
    ap.add_argument("--host", default=os.environ.get("APP_HOST") or "127.0.0.1")
    ap.add_argument("--port", type=_port, default=os.environ.get("APP_PORT") or "5057")
    # The Werkzeug debugger needs /dev/shm; some confined environments deny it,
    # so both debugger and reloader are off unless explicitly enabled.
    ap.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes"),
        help="enable the development debugger and reloader (default: off; DEBUG=1 also enables them)",
    )
    return ap


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    # Runs started from the builder before this start: the ones still going
    # are followed again (they run detached on their target), the rest listed.
    # With --debug the reloader's watcher runs this too; the server it starts follows them.
    try:
        back = execution.restore() if not args.debug or is_running_from_reloader() else []
    except execution.Followed as exc:
        sys.exit(f"nullius: {exc}")

    values = args.paths or [
        p for p in os.environ.get("JOB_ROOTS", "").split(os.pathsep) if p
    ]
    # Same expansion as the web form, so `app.py 'current/jobs_*'` works too.
    roots, unmatched = shared.resolve_submitted_paths(values)

    for miss in unmatched:
        print(f"  {miss}  ({'no match' if shared.is_pattern(miss) else 'MISSING'})")
    if roots:
        ws = shared.adopt_roots(roots, label=os.environ.get("WORKSPACE", ""))
        print(f"Workspace {ws.name!r} ({ws.dir}) over:")
        for r in ws.roots:
            print(f"  {r}")
    elif not unmatched:
        ws = workspace.last_open(shared.WS_HOME)
        if ws:
            shared.open_workspace(ws)
            print(f"Workspace {ws.name!r} ({ws.dir}) reopened")
        else:
            print("No job path given — the web page will ask for one, or offer a workspace.")
    for ex in back:
        print(f"Run {ex.plan.name} picked up again on {ex.plan.host.label if ex.plan.host else 'this machine'} ({ex.state})")
        shared.adopt_roots([ex.jobs], into=ex.workspace)   # its job root back in the workspace it started in
    app.config["SERVED_HOST"] = args.host                 # a name the Host header may carry (shared._served_name)
    print(f"Serving on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
