"""One entry point for the application and its existing command lines.

Dispatch is lazy: reading runs or asking for help never imports the web stack.
The original ``python -m nullius.<capability>.cli`` commands remain available.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
from pathlib import Path


COMMANDS = {
    "serve": "open the web application",
    "runs": "inspect run output",
    "compare": "compare runs",
    "import": "import ATIF or OpenTelemetry output",
    "standard": "inspect and run standard definitions",
    "workspace": "manage workspaces",
    "paths": "show persistent storage locations",
    "migrate": "copy legacy storage into the user profile",
}
MODULES = {
    "runs": "nullius.runs.cli",
    "compare": "nullius.compare.cli",
    "import": "nullius.importers.cli",
    "standard": "nullius.standard.cli",
    "workspace": "nullius.workspace_cli",
    "paths": "nullius.paths",
    "migrate": "nullius.migrate",
}


def _serve(argv):
    """Import the web entry point only when the server is requested.

    The wheel keeps the web layer and assets in ``_nullius_app``; a source
    checkout keeps them beside the backend in ``app/``. Neither layout adds
    a second implementation of ``nullius`` to the interpreter's path.
    """
    if importlib.util.find_spec("_nullius_app") is not None:
        module = importlib.import_module("_nullius_app.app")
    else:
        source = Path(__file__).resolve().parents[1] / "app.py"
        spec = importlib.util.spec_from_file_location("_nullius_source_app", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.main(argv)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="nullius", description="Bring anything, run it, prove it.",
        epilog="\n".join(f"  {name:<10} {help}" for name, help in COMMANDS.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="show the installed version")
    parser.add_argument("command", nargs="?", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.version:
        try:
            version = importlib.metadata.version("nullius")
        except importlib.metadata.PackageNotFoundError:
            version = "source checkout"
        print(f"nullius {version}")
        return 0
    if not args.command:
        parser.print_help()
        return 0
    # Some command modules compute their storage locations at import time.
    from nullius import paths, settings
    if not (args.command == "standard" and "--from-run" in args.arguments):
        try:
            settings.load(paths.settings_file())
        except ValueError as e:
            if args.command != "paths":
                parser.exit(2, f"{e} Fix {paths.settings_file()}.\n")
    if args.command == "serve":
        return _serve(args.arguments) or 0
    return importlib.import_module(MODULES[args.command]).main(args.arguments) or 0
