"""API tokens: who may ask this server over HTTP (``/api/v1``, ``web/api.py``).

    nullius access create NAME [--scope read|run] [--days N]   # prints the token, once
    nullius access list [--json]
    nullius access revoke ID

One owner-only file, ``api-tokens.json`` in the user configuration directory,
holds each token's id, name, scope, creation time, the day it expires when it
has one, and the SHA-256 of its secret, never the secret: a token is shown when
it is created and cannot be read back. With no token in the file the API
answers nothing; an expired token is refused as a revoked one is.

``read`` reads what the pages show: the catalogue, jobs, trials, judgements,
comparisons, the leaderboard, saved recipes, runs. ``run`` also checks, starts,
continues and stops runs, asks a judge and saves recipes. No scope writes Settings
or reads a key or a licence.
Standard library only.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from typing import List, Optional

from nullius import ondisk, paths

SCOPES = ("read", "run")            # each includes the ones before it
PREFIX = "nls"
MAX_DAYS = 3650
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
_TOKEN = re.compile(PREFIX + r"_([0-9a-f]{12})_([A-Za-z0-9_-]{40,})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def file(env=None) -> str:
    return os.path.join(paths.config_dir(env), "api-tokens.json")


def listing() -> List[dict]:
    """Every token's record, oldest first; a file that is missing or unreadable holds none."""
    try:
        rows = _kept()
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("scope") in SCOPES
            and isinstance(r.get("sha256"), str) and _SHA256.fullmatch(r["sha256"])]


def _kept() -> list:
    """The file's rows as they are, [] without a file; ValueError when it cannot be read."""
    try:
        with open(file(), encoding="utf-8") as fh:
            rows = json.load(fh)["tokens"]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        rows = None
    if not isinstance(rows, list):
        raise ValueError(f"the tokens' file cannot be read: repair or remove {file()}")
    return rows


@contextlib.contextmanager
def _changing():
    """The rows to change, written back on leaving: one change at a time, the page's and the command's. A file that
    cannot be read is left as it is (ValueError), never written over."""
    os.makedirs(os.path.dirname(file()), mode=0o700, exist_ok=True)
    with ondisk.locked(file() + ".lock"):
        rows = _kept()
        yield rows
        ondisk.write_whole(file(), json.dumps({"tokens": rows}, indent=2) + "\n", mode=0o600)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create(name: str, scope: str = "read", days: Optional[int] = None) -> tuple:
    """(token, record): a new token of that scope, good for ``days`` days or until revoked.
    The token is returned here and kept nowhere."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("a token's name is 1 to 64 letters, digits, spaces, '.', '_' or '-'")
    if scope not in SCOPES:
        raise ValueError(f"a token's scope is one of: {', '.join(SCOPES)}")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS):
        raise ValueError(f"a token expires after 1 to {MAX_DAYS} days, or never")
    token_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    record = {"id": token_id, "name": name, "scope": scope, "sha256": _digest(secret),
              "created": time.strftime(_STAMP, time.gmtime()),
              "expires": time.strftime(_STAMP, time.gmtime(time.time() + days * 86400)) if days else None}
    with _changing() as rows:
        if any(isinstance(r, dict) and r.get("name") == name for r in rows):
            raise ValueError(f"a token is named {name!r} already: revoke it first, or choose another name")
        rows.append(record)
    return f"{PREFIX}_{token_id}_{secret}", record


def revoke(token_id: str) -> bool:
    with _changing() as rows:
        kept = [r for r in rows if not (isinstance(r, dict) and r.get("id") == token_id)]
        gone = len(kept) < len(rows)
        rows[:] = kept
    return gone


def expired(record: dict, now: Optional[float] = None) -> bool:
    """Whether a token's day has passed; an expiry that cannot be read has."""
    if not record.get("expires"):
        return False
    try:
        return calendar.timegm(time.strptime(record["expires"], _STAMP)) <= (time.time() if now is None else now)
    except (TypeError, ValueError):
        return True


def verify(token) -> Optional[dict]:
    """The record of a token this server issued, has not revoked and that has not expired, or None."""
    m = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if not m:
        return None
    digest = _digest(m.group(2))
    for r in listing():
        if r["id"] == m.group(1) and hmac.compare_digest(r["sha256"], digest):
            return None if expired(r) else r
    return None


def allows(record: Optional[dict], scope: str) -> bool:
    """Whether a token's scope includes ``scope``: ``run`` reads too."""
    return bool(record) and record.get("scope") in SCOPES and SCOPES.index(record["scope"]) >= SCOPES.index(scope)


def shown(record: dict) -> dict:
    """A record without its hash, and whether its day has passed: what a listing prints."""
    return {**{k: record.get(k) for k in ("id", "name", "scope", "created", "expires")}, "expired": expired(record)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nullius access", description="The tokens this server's HTTP API accepts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="a new token, printed once")
    c.add_argument("name")
    c.add_argument("--scope", choices=SCOPES, default="read", help="read: results only (default). run: also start and stop runs, ask a judge, save recipes")
    c.add_argument("--days", type=int, help="expires after this many days (default: never)")
    ls = sub.add_parser("list", help="the tokens, never their secrets")
    ls.add_argument("--json", action="store_true")
    r = sub.add_parser("revoke", help="stop accepting a token")
    r.add_argument("id")
    a = ap.parse_args(argv)
    if a.cmd == "create":
        try:
            token, record = create(a.name, a.scope, a.days)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        print(f"{record['name']} ({record['scope']}{', until ' + record['expires'][:10] if record['expires'] else ''}), shown once:", file=sys.stderr)
        print(token)
        return 0
    if a.cmd == "list":
        rows = [shown(r) for r in listing()]
        if a.json:
            print(json.dumps(rows, indent=1))
        else:
            for row in rows:
                until = "expired" if row["expired"] else (row["expires"] or "")[:10] or "never"
                print(f"{row['id']}  {row['scope']:<4}  {(row['created'] or '')[:10]:<10}  {until:<10}  {row['name']}")
        return 0
    try:
        gone = revoke(a.id)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if not gone:
        print(f"no token with id {a.id!r}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
