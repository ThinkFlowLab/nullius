"""Model prices, the user's own: what a call's cost is estimated from when
the provider's reply does not carry one.

Nullius ships no price. The prices live in two files of the user's
configuration (``NULLIUS_PRICES`` names another list, the file a host is sent;
the user's rows then sit beside it):

``prices.json``      ``models``, a public list, fetched on request and before a capped run (``update``) and
                     replaced whole, with where it came from and when: LiteLLM's
                     ``model_prices_and_context_window.json``, keyed by the model names the providers themselves use
``prices-own.json``  ``own``, the user's rows, which win over the list: a price it lacks, a
                     negotiated one, a model served under a name of one's own. Written only when
                     the user sets or removes one; a row that cannot price is kept and said (``problems``)

A ``prices.json`` of before held ``own`` too: while there is no ``prices-own.json``, its
``own`` is the user's rows, and a refetch keeps it; the first change of a row moves them.
A host is sent the list and the user's rows as such a file.

A row is USD per million tokens: ``input`` and ``output``, optionally
``cached`` (input a cache served), ``cache_write``, ``cache_write_1h``, and ``max_output``,
the model's output limit in tokens. A model is priced by exactly its name or
not at all: a name that only starts like another's is another model.

A run reads the file; ``cli run`` fetches the list first when the run has a spending cap.

    nullius prices                     # where the list came from, how many models, the user's own rows
    nullius prices list MODEL ...      # those models' rows
    nullius prices update [--from URL-or-file]
    nullius prices set MODEL INPUT OUTPUT [--cached X] [--cache-write X] [--cache-write-1h X] [--max-output N]
    nullius prices remove MODEL
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
import urllib.request
from typing import Callable, Dict, Iterable, Optional, Tuple

from nullius import ondisk, paths

SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
RATES = ("input", "output", "cached", "cache_write", "cache_write_1h")   # USD per million tokens
_LISTED = {"input": "input_cost_per_token", "output": "output_cost_per_token",
           "cached": "cache_read_input_token_cost", "cache_write": "cache_creation_input_token_cost",
           "cache_write_1h": "cache_creation_input_token_cost_above_1hr"}
_MOST = 64 * 1024 * 1024                                        # a list larger than this is not a price list


def path(env=None) -> str:
    return paths.prices_file(env)


def own_path(env=None) -> str:
    return paths.own_prices_file(env)


def _read(file: str):
    """``file``'s JSON object; None when it is missing, ``False`` when it does not read as one."""
    try:
        with open(file, encoding="utf-8") as f:
            got = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    return got if isinstance(got, dict) else False


def _own(env=None):
    """The user's rows as written, a row that cannot price too, and what is wrong with
    their file (None when nothing). Before ``prices-own.json``: the ``own`` of ``prices.json``."""
    got = _read(own_path(env))
    if got is None:
        got = _read(path(env)) or {}
        rows = got.get("own", {})
        return (rows, None) if isinstance(rows, dict) else ({}, None)     # a list of before that does not read: nothing of the user's to keep
    rows = got.get("own", {}) if got is not False else None
    if not isinstance(rows, dict):
        return {}, f"{own_path(env)} does not read as your prices: fix it or remove it"
    return rows, None


def load(env=None) -> dict:
    """The prices as ``{from, on, models, own}``, each row one that can price; a file
    missing or unreadable, empty: no price is better than a run that cannot start for its price list."""
    doc = {"from": "", "on": "", "models": {}, "own": {}}
    got = _read(path(env)) or {}
    for k in ("from", "on"):
        if isinstance(got.get(k), str):
            doc[k] = got[k]
    if isinstance(got.get("models"), dict):
        doc["models"] = {n: r for n, r in got["models"].items() if _usable(r)}
    doc["own"] = {n: r for n, r in _own(env)[0].items() if _usable(r)}
    return doc


def problems(env=None) -> list:
    """What keeps one of the user's rows from pricing, one line each: kept as written, never dropped."""
    rows, why = _own(env)
    return ([why] if why else []) + [f"{n} prices nothing: set its input and output prices again"
                                     for n, r in sorted(rows.items()) if not _usable(r)]


def _usable(r) -> bool:
    return isinstance(r, dict) and all(_number(r.get(k)) for k in ("input", "output"))


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _save(out: str, doc: dict) -> None:
    """Written whole beside the file, then renamed: a reader never finds half a list."""
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    ondisk.write_whole(out, json.dumps(doc, indent=1, sort_keys=True) + "\n")


def table(env=None) -> Dict[str, dict]:
    """Every priced model: the list, then the user's own rows over it."""
    doc = load(env)
    return {**doc["models"], **doc["own"]}


def given(names: Iterable[Optional[str]], prices: Optional[Dict[str, dict]]) -> Dict[str, dict]:
    """The rows of ``prices`` for exactly ``names``: what a relay started as
    a process of its own is handed, which cannot take the whole list."""
    prices = prices or {}
    return {n: prices[n] for n in dict.fromkeys(n for n in names if n) if n in prices}


def row(input, output, cached=None, cache_write=None, max_output=None, *, cache_write_1h=None) -> dict:
    """One row, checked: rates are numbers not below zero, the limit a whole number above it."""
    out = {}
    for name, v in (("input", input), ("output", output), ("cached", cached), ("cache_write", cache_write), ("cache_write_1h", cache_write_1h)):
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: a price in USD per million tokens, not {v!r}")
        if not v >= 0 or v == float("inf"):
            raise ValueError(f"{name}: a price in USD per million tokens, not {v!r}")
        out[name] = v
    if max_output is not None:
        try:
            n = int(max_output)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            raise ValueError(f"max_output: the model's output limit in tokens, not {max_output!r}")
        out["max_output"] = n
    return out


def parse(text: str) -> Tuple[str, dict]:
    """``MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H]``, a run's ``--price``.
    Optional rates may be left empty; existing two/three-rate rows keep their meaning."""
    name, eq, rest = str(text).partition("=")
    bits = [b.strip() for b in rest.split(",")] if eq else []
    if not name.strip() or not 2 <= len(bits) <= 5 or not all(bits[:2]):
        raise ValueError(f"--price {text!r}: MODEL=INPUT,OUTPUT[,CACHED,WRITE,WRITE_1H], in USD per million tokens")
    try:
        return name.strip(), row(**{k: v or None for k, v in zip(RATES, bits)})
    except ValueError as e:
        raise ValueError(f"--price {text!r}: {e}")


def convert(listing) -> Dict[str, dict]:
    """LiteLLM's list as rows: its costs are per token, a row's per million.
    An entry with no input price is not a priced model (the list's own
    sample, an image model priced per picture) and is left out."""
    if not isinstance(listing, dict):
        raise ValueError("not a price list: a JSON object of models was expected")
    out = {}
    for name, e in listing.items():
        if not isinstance(e, dict) or not _number(e.get(_LISTED["input"])):
            continue
        r = {"input": round(e[_LISTED["input"]] * 1e6, 6),
             "output": round(e[_LISTED["output"]] * 1e6, 6) if _number(e.get(_LISTED["output"])) else 0.0}
        for k in ("cached", "cache_write", "cache_write_1h"):
            if _number(e.get(_LISTED[k])):
                r[k] = round(e[_LISTED[k]] * 1e6, 6)
        limit = e.get("max_output_tokens")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            r["max_output"] = limit
        out[str(name)] = r
    if not out:
        raise ValueError("not a price list: no model in it has a price")
    return out


def _fetch(source: str) -> bytes:
    if source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": "nullius"})        # nothing of the user or the machine
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(_MOST + 1)
    if "://" in source:
        raise ValueError("only an https address or a file is read")
    with open(source, "rb") as f:
        return f.read(_MOST + 1)


def update(source: Optional[str] = None, env=None, fetch: Optional[Callable[[str], bytes]] = None,
           today: Optional[str] = None) -> dict:
    """The public list fetched and kept in place of the last one; the user's
    own rows are not touched. Nothing is written when the fetch or the list
    is bad: the list held stays. Returns ``{n, from, on}``."""
    source = source or SOURCE
    try:
        raw = (fetch or _fetch)(source)
    except (OSError, ValueError) as e:
        why = e.strerror if isinstance(e, OSError) and e.filename else e      # a file's error names the file again
        raise ValueError(f"could not fetch the price list from {source}: {why}")
    if len(raw) > _MOST:
        raise ValueError(f"{source}: too large to be a price list")
    try:
        models = convert(json.loads(raw))
    except ValueError as e:
        raise ValueError(f"{source}: {e}")
    doc = {"from": source, "on": today or time.strftime("%Y-%m-%d"), "models": models}
    before = _read(path(env))
    if before and "own" in before and not os.path.exists(own_path(env)):
        doc["own"] = before["own"]              # a list of before holds the user's rows until their first change moves them
    _save(path(env), doc)
    return {"n": len(models), "from": doc["from"], "on": doc["on"]}


@contextlib.contextmanager
def _changing_own(env=None):
    """The user's rows as written, for one change, then written to their own file;
    one change at a time across processes. Refused when their file does not read:
    writing over it would lose every row in it."""
    out = own_path(env)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with ondisk.locked(out + ".lock"):
        rows, why = _own(env)
        if why:
            raise ValueError(why)
        rows = dict(rows)
        yield rows
        _save(out, {"own": rows})


def set_own(name: str, r: dict, env=None) -> None:
    name = str(name).strip()
    if not name or not _usable(r):
        raise ValueError("a model's name, and its input and output prices")
    with _changing_own(env) as rows:
        rows[name] = r


def remove_own(name: str, env=None) -> bool:
    if str(name).strip() not in _own(env)[0]:
        return False
    with _changing_own(env) as rows:
        rows.pop(str(name).strip(), None)
    return True


def state(env=None) -> dict:
    """What Settings shows: how many models the list prices, where it came
    from and when, and the user's own rows."""
    doc = load(env)
    return {"n": len(doc["models"]), "from": doc["from"], "on": doc["on"], "own": doc["own"], "problems": problems(env)}


def said(name: str, r: dict) -> str:
    bits = [f"input {r['input']:g}", f"output {r['output']:g}"]
    bits += [f"{label} {r[k]:g}" for k, label in (("cached", "cached"), ("cache_write", "cache write"),
                                               ("cache_write_1h", "cache write 1h")) if k in r]
    if "max_output" in r:
        bits.append(f"output limit {r['max_output']}")
    return f"{name}: " + ", ".join(bits)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nullius prices",
                                 description="The user's model prices, in USD per million tokens: what a run's cost is estimated from.")
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="the list's source and size, the user's own rows, and the rows of the models named")
    ls.add_argument("models", nargs="*")
    ls.add_argument("--json", action="store_true")
    up = sub.add_parser("update", help="fetch the public list and keep it; the user's own rows stay")
    up.add_argument("--from", dest="source", metavar="URL-or-file", help=f"another list in the same format (default: {SOURCE})")
    st = sub.add_parser("set", help="a price of the user's own, which wins over the list")
    st.add_argument("model")
    st.add_argument("input")
    st.add_argument("output")
    st.add_argument("--cached")
    st.add_argument("--cache-write")
    st.add_argument("--cache-write-1h")
    st.add_argument("--max-output")
    rm = sub.add_parser("remove", help="remove one of the user's own prices")
    rm.add_argument("model")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "update":
            got = update(a.source)
            print(f"{got['n']} models priced, from {got['from']}")
            return 0
        if a.cmd == "set":
            r = row(a.input, a.output, a.cached, a.cache_write, a.max_output, cache_write_1h=a.cache_write_1h)
            set_own(a.model, r)
            print(said(a.model.strip(), r))
            return 0
        if a.cmd == "remove":
            if not remove_own(a.model):
                ap.exit(1, f"{a.model}: not one of your own prices\n")
            return 0
    except ValueError as e:
        ap.exit(1, f"{e}\n")
    doc, names = load(), list(getattr(a, "models", None) or [])
    both = {**doc["models"], **doc["own"]}
    if getattr(a, "json", False):
        print(json.dumps({"file": path(), "own_file": own_path(), "from": doc["from"], "on": doc["on"], "n": len(doc["models"]), "own": doc["own"],
                          "problems": problems(),
                          "models": {n: both.get(n) for n in names}}, indent=2))
        return 0
    print(f"file: {path()}")
    print(f"own:  {own_path()}")
    print(f"list: {len(doc['models'])} models, from {doc['from']} on {doc['on']}" if doc["models"]
          else "list: none yet - nullius prices update")
    for n, r in sorted(doc["own"].items()):
        print("own  " + said(n, r))
    for line in problems():
        print("bad  " + line)
    for n in names:
        print(said(n, both[n]) if n in both else f"{n}: no price")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
