"""The builder's saved recipes from a shell, and a recipe launched as the builder's Run launches it.

    nullius recipes list [--json]
    nullius recipes launch <recipe> [--where MACHINE]... [--workspace NAME] [--server URL] [--yes] [--wait | --dry-run] [--json]

``list`` reads the saved recipes (``store.listing``) and writes nothing.

``launch`` sends a recipe of the Nullius standard to the Nullius server running on this machine, as the builder's
Run sends it: the builder's data (``GET /new-run``, which holds the form token), the compile its Review step shows
(``POST /new-run/runner``), what each machine lacks (``POST /new-run/prerequisites``), then ``POST /run``. The
server makes the launch and follows it, as one it started from the page: one process follows a runs folder
(``execution``), and the pages list the runs it follows. The recipe is a saved one, by its name or its id, or a
recipe file as the builder exports it. ``--dry-run`` stops before ``POST /run`` and prints what the Review step
shows: nothing starts, no job or workspace is made.

The exit codes are the contract in ``docs/cli/exit-codes.md``: 0 done (with ``--dry-run``: Run would start it), 2 usage,
3 no such recipe, 4 refused (by the compile, a machine, the question Run asks, the server, or with ``--wait`` a launch
that ended otherwise than done or paused).
"""
from __future__ import annotations

import argparse
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from nullius import execution, workspace
from nullius.recipes import store

EXIT_OK, EXIT_NOT_FOUND, EXIT_REFUSED = 0, 3, 4
STANDARD = "standard"
SCHEMA = re.compile(r'<script id="nb-schema" type="application/json">(.*?)</script>', re.S)
POLL_SECONDS = 5.0


class Refused(Exception):
    """Why nothing more is sent: one line."""


class Server:
    """The Nullius server at ``url``, asked as a page asks it: one session (its cookie), its origin, the form token
    the builder's data holds."""

    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = ""

    def send(self, method: str, path: str, body=None) -> tuple:
        """(status, the answer's text)."""
        headers = {"Origin": self.url, **({"X-CSRF-Token": self.token} if self.token else {})}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise Refused(f"no Nullius server answers at {self.url}: {getattr(e, 'reason', e)}") from None

    def json(self, method: str, path: str, body=None) -> tuple:
        status, text = self.send(method, path, body)
        try:
            return status, json.loads(text)
        except ValueError:
            raise Refused(f"{method} {path}: HTTP {status}, not a JSON answer") from None


def default_server() -> str:
    """The server ``nullius serve`` starts here: its port from saved settings or the environment, else 5057."""
    return f"http://127.0.0.1:{os.environ.get('APP_PORT') or 5057}"


def find(ref: str) -> dict:
    """The recipe ``ref`` names: a recipe file (one recipe, or a saved recipe's file), else a saved recipe by its
    id, display name or internal name, in that order. ``LookupError`` when there is none."""
    if os.path.isfile(ref):
        with open(ref, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, list) and len(got) == 1:
            got = got[0]
        got = got.get("recipe") if isinstance(got, dict) and isinstance(got.get("recipe"), dict) else got
        if not isinstance(got, dict):
            raise Refused(f"{ref} holds no single recipe")
        return got
    items = store.listing()
    for key in ("id", "name", "internal"):
        matches = [item for item in items if ref == (item["recipe"].get("name") if key == "internal" else item.get(key))]
        if len(matches) > 1:
            raise Refused(f"ambiguous recipe {ref!r}: " + "; ".join(f"{x.get('name')!r} ({x.get('id')})" for x in matches))
        if matches:
            return matches[0]["recipe"]
    raise LookupError(f"no saved recipe or recipe file {ref}")


def review(server: Server, recipe: dict, where=None, into: str = "", yes: bool = False, now=None) -> tuple:
    """What the builder's Review step shows for ``recipe``, nothing started: (the recipe as Run sends it, the answer
    to ``POST /new-run/runner`` with ``prerequisites``, the answer to ``POST /new-run/prerequisites``, and
    ``refused``, why Run would not start it, else "")."""
    if (recipe.get("harness") or "harbor") != STANDARD:
        raise Refused("a Harbor recipe runs by the script its Review step generates: docs/builder.md")
    status, text = server.send("GET", "/new-run")
    found = SCHEMA.search(text) if status == 200 else None
    if not found:
        raise Refused(f"{server.url} is not a Nullius server (GET /new-run: HTTP {status})")
    data = json.loads(found.group(1))
    if not data.get("local"):
        raise Refused(f"{server.url} starts runs for a client on its own machine only")
    server.token = data.get("csrf") or ""
    # each Run is a new job: the recipe's name and the second (command-builder.js, startRun)
    stamp = (now or datetime.datetime.now()).strftime("%m%d-%H%M%S")
    recipe = dict(recipe, name=f"{recipe.get('name') or ''}-{stamp}", **({"where": where[0] if len(where) == 1 else list(where)} if where else {}))
    _, out = server.json("POST", "/new-run/runner", {"recipe": recipe})
    pre = {"machines": [], "ready": False, "errors": []}           # an error of the compile: no machine is asked
    if not out.get("errors"):
        _, pre = server.json("POST", "/new-run/prerequisites", {"recipe": recipe, "provider": recipe.get("provider") or ""})
    lacks = [f"{m.get('label')}: {r.get('label')}" + (f" ({r['detail']})" if r.get("detail") else "")
             for m in pre.get("machines") or [] for r in m.get("rows") or [] if r.get("state") == "missing"]
    why = ("A workspace's name is lowercase letters, digits and dashes." if into and workspace.slug(into) != into
           else " ".join(e.get("message", "") for e in out["errors"]) if out.get("errors")
           else out["provider_error"] if out.get("provider_error")
           else f"Run asks first: {out['confirm']} --yes answers yes" if out.get("confirm") and not yes
           else "" if pre.get("ready")
           else "a machine lacks what the run needs: " + ("; ".join(lacks + list(pre.get("errors") or [])) or "see Review"))
    return recipe, dict(out, prerequisites=pre, refused=why)


def launch(server: Server, recipe: dict, where=None, into: str = "", yes: bool = False, now=None) -> dict:
    """Send ``recipe`` as the builder's Run does; the server's answer to ``POST /run``, with ``review``, the rows of
    the Review step, and ``launch``, the launch's key. ``Refused`` when the builder would not start it."""
    recipe, seen = review(server, recipe, where, into, yes, now)
    if seen["refused"]:
        raise Refused(seen["refused"])
    body = {"name": recipe["name"], "where": recipe.get("where") or "local", "recipe": recipe, **({"workspace": into} if into else {})}
    status, got = server.json("POST", "/run", body)
    if not got.get("ok"):
        raise Refused(got.get("error") or f"POST /run: HTTP {status}")
    return dict(got, review=seen.get("review") or [], launch=f"run:{got['id']}")


def wait(server: Server, run_ids, every: float = POLL_SECONDS) -> dict:
    """{run id: its summary} once none of the runs is going (``GET /run/<id>``, the route the page polls): each has
    ended, or is paused on the question the page answers, which a shell cannot."""
    ended = {}
    while True:
        for rid in run_ids:
            if rid not in ended:
                status, s = server.json("GET", f"/run/{urllib.parse.quote(rid)}")
                if status != 200:
                    raise Refused(f"GET /run/{rid}: HTTP {status}")
                if s.get("state") not in execution.ALIVE or s.get("state") == "paused":
                    ended[rid] = {k: v for k, v in s.items() if k != "log"}
        if len(ended) == len(run_ids):
            return ended
        time.sleep(every)


def _list(a) -> int:
    items = store.listing()
    if a.json:
        json.dump(items, sys.stdout, indent=1, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    for x in items:
        r = x["recipe"]
        print(f"{x['name']}\t{x['id']}\t{r.get('harness') or 'harbor'}\t{r.get('benchmark') or ''}\t{x.get('saved', '')}")
    return EXIT_OK


def _launch(a) -> int:
    try:
        recipe = find(a.recipe)
    except LookupError as e:
        print(e, file=sys.stderr)
        return EXIT_NOT_FOUND
    except (OSError, ValueError, Refused) as e:
        print(e, file=sys.stderr)
        return EXIT_REFUSED
    server = Server(a.server or default_server())
    if a.dry_run:
        return _dry_run(server, recipe, a)
    try:
        got = launch(server, recipe, a.where, a.workspace or "", a.yes)
        runs = got.get("runs") or [got]
        ended = wait(server, [r["id"] for r in runs]) if a.wait else {}
    except Refused as e:
        print(e, file=sys.stderr)
        return EXIT_REFUSED
    page = server.url + "/job/{}" + (f"?workspace={urllib.parse.quote(a.workspace)}" if a.workspace else "")
    if a.json:
        json.dump(dict(got, **({"ended": ended} if a.wait else {})), sys.stdout, indent=1, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(f"launch {got['launch']}")
        for r in runs:
            print(f"run {r['id']} on {r.get('host')}: {(ended.get(r['id']) or r).get('state')}")
            print(f"  jobs {r.get('jobs')}")
            for j in r.get("job_ids") or []:
                print(f"  job {j} {page.format(urllib.parse.quote(j))}")
    return EXIT_REFUSED if any(s.get("state") != "done" for s in ended.values()) else EXIT_OK


def _dry_run(server: Server, recipe: dict, a) -> int:
    """What Run would launch, as the Review step shows it; exit 0 when Run would start it."""
    try:
        _, seen = review(server, recipe, a.where, a.workspace or "", a.yes)
    except Refused as e:
        print(e, file=sys.stderr)
        return EXIT_REFUSED
    pre = seen["prerequisites"]
    if a.json:
        json.dump(seen, sys.stdout, indent=1, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for _, label, value in seen.get("review") or []:
            print(f"{label}: {value}")
        for m in pre.get("machines") or []:
            lacks = [f"{r.get('label')} {r.get('state')}" + (f" ({r['detail']})" if r.get("detail") else "")
                     for r in m.get("rows") or [] if r.get("state") != "ok"]
            print(f"Prerequisites, {m.get('label')}: {'; '.join(lacks) or 'ready'}")
        for note in (seen.get("notes") or []) + (pre.get("notes") or []):
            print(f"Note: {note}")
        print("Not launched: --dry-run")
    if seen["refused"]:
        print(seen["refused"], file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nullius recipes", description=__doc__.split("\n\n")[0],
                                 epilog="exit codes: 0 done, 2 usage, 3 no such recipe, 4 refused")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="the saved recipes: name, id, runner, benchmark, when saved")
    ls.add_argument("--json", action="store_true", help="one JSON document: the saved recipes as stored")
    la = sub.add_parser("launch", help="launch a recipe of the standard on the server here, as the builder's Run")
    la.add_argument("recipe", help="a saved recipe's name or id, or a recipe file (JSON)")
    la.add_argument("--where", action="append", metavar="MACHINE",
                    help="local, the server's machine, or a host's entry in Settings, in place of the recipe's Where (repeatable: one shard each)")
    la.add_argument("--workspace", metavar="NAME", help="the workspace the launch goes into, made when there is none (default: the open one, as Run)")
    la.add_argument("--server", metavar="URL", help="the Nullius server on this machine (default http://127.0.0.1:APP_PORT, else 5057)")
    la.add_argument("--yes", action="store_true", help="answer yes to the question Run asks before a run of every unit")
    ends = la.add_mutually_exclusive_group()
    ends.add_argument("--wait", action="store_true", help="return when the launch has ended or paused on a question; exit 4 unless it ended done")
    ends.add_argument("--dry-run", action="store_true", help="print what the Review step shows and launch nothing; exit 0 when Run would start it")
    la.add_argument("--json", action="store_true", help="one JSON document: the server's answer to the launch")
    a = ap.parse_args(argv)
    return {"list": _list, "launch": _launch}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
