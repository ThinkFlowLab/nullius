"""Read workspace composite scores without launching evaluations."""
from __future__ import annotations

import argparse
import json
import os
import sys

from nullius.runs import parse
from . import configuration, service, weights


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nullius leaderboard", description=__doc__,
        epilog="Configuration: scoring.json in the profile configuration directory (nullius paths). Edit it through Scoring settings or as a complete JSON document. Exit 0: read; 2: usage; 3: missing root. Unavailable measurements and invalid-settings fallback are disclosed in the output.")
    parser.add_argument("roots", nargs="*", help="result roots in one workspace; omit for unscored catalogue rows")
    parser.add_argument("--json", action="store_true", help="full precision scores, identity, evidence, ranks/reasons and observed metric counts")
    parser.add_argument("--all-scores", action="store_true", help="include unverified manual scores; default: verified only")
    args = parser.parse_args(argv)
    if any(not os.path.isdir(root) for root in args.roots):
        print("A result root does not exist or is not a directory.", file=sys.stderr)
        return 3
    result = service.document(parse.load_all(args.roots), workspace_open=bool(args.roots), all_scores=args.all_scores, deferred=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        if result["warning"]:
            print(result["warning"])
        if result["empty"]:
            print(result["empty"])
        for row in result["rows"]:
            print(f"{row['rank'] or ''}\t{row['name']}\t{row['company']}\t{row['version_label']}\t{weights.percent(row['overall'])}")
            setup = " · ".join(str(row["identity"][key]) for key in ("model", "effort") if row["identity"].get(key))
            if setup:
                print(f"  {setup}")
            for name in configuration.AXES:
                axis = row["axes"][name]
                print(f"  {name}: {weights.percent(axis['score'])} · weight {axis['weight']:g} · share {weights.percent(axis['share'])}")
                for entry in axis["entries"]:
                    version = f" · {entry['version'] or 'Official'}" if "custom" not in entry else ""
                    reading = weights.percent(entry["score"]) if entry["score"] is not None else entry["reason"]
                    trust = " · not verified" if entry["unverified"] else ""
                    tasks = f" · {entry['covered']}/{entry['tasks']} tasks" if entry["tasks"] is not None else ""
                    runs = " · runs " + ", ".join(entry["runs"]) if entry["runs"] else ""
                    print(f"    {entry['name']}{version} · {reading}{trust} · weight {entry['weight']:g} · share {weights.percent(entry['share'])}{tasks}{runs}")
            for reason in row["reasons"]:
                print(f"  Not ranked: {reason}")
            for metric, data in row["metrics"].items():
                print(f"  {metric}: {data['value'] if data['value'] is not None else 'unavailable'} ({data['count']} observed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
