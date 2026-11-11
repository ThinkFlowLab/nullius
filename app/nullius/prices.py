"""Model prices, the user's own: what a call's cost is estimated from when
the provider's reply does not carry one.

Nullius ships no price. The list lives in one file of the user's
configuration, ``prices.json`` (``NULLIUS_PRICES`` names another: the file a
host is sent), in two parts:

``models``  a public list, fetched on request (``update``) and kept whole
            with where it came from and when: LiteLLM's
            ``model_prices_and_context_window.json``, keyed by the model
            names the providers themselves use
``own``     the user's rows, which win over the list: a price it lacks, a
            negotiated one, a model served under a name of one's own

A row is USD per million tokens: ``input`` and ``output``, optionally
``cached`` (input a cache served) and ``cache_write``, and ``max_output``,
the model's output limit in tokens. A model is priced by exactly its name or
not at all: a name that only starts like another's is another model.

Nothing here runs during a run: a run reads the file, never the network.

    nullius prices                     # where the list came from, how many models, the user's own rows
    nullius prices list MODEL ...      # those models' rows
    nullius prices update [--from URL-or-file]
    nullius prices set MODEL INPUT OUTPUT [--cached X] [--cache-write X] [--max-output N]
    nullius prices remove MODEL
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from typing import Callable, Dict, Iterable, Optional, Tuple

from nullius import paths

SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
RATES = ("input", "output", "cached", "cache_write")            # USD per million tokens
_LISTED = {"input": "input_cost_per_token", "output": "output_cost_per_token",
           "cached": "cache_read_input_token_cost", "cache_write": "cache_creation_input_token_cost"}
_MOST = 64 * 1024 * 1024                                        # a list larger than this is not a price list


def path(env=None) -> str:
    return paths.prices_file(env)


def load(env=None) -> dict:
    """The file as ``{from, on, models, own}``; missing or unreadable, empty:
    no price is better than a run that cannot start for its price list."""
    doc = {"from": "", "on": "", "models": {}, "own": {}}
    try:
        with open(path(env), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return doc
    if isinstance(got, dict):
        for k in ("from", "on"):
            if isinstance(got.get(k), str):
                doc[k] = got[k]
        for k in ("models", "own"):
            if isinstance(got.get(k), dict):
                doc[k] = {n: r for n, r in got[k].items() if _usable(r)}
    return doc


def _usable(r) -> bool:
    return isinstance(r, dict) and all(_number(r.get(k)) for k in ("input", "output"))


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def save(doc: dict, env=None) -> None:
    """Written whole beside the file, then renamed: a reader never finds half a list."""
    out = path(env)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    part = f"{out}.{os.getpid()}.part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(part, out)


def table(env=None) -> Dict[str, dict]:
    """Every priced model: the list, then the user's own rows over it."""
    doc = load(env)
    return {**doc["models"], **doc["own"]}


def given(names: Iterable[Optional[str]], prices: Optional[Dict[str, dict]]) -> Dict[str, dict]:
    """The rows of ``prices`` for exactly ``names``: what a relay started as
    a process of its own is handed, which cannot take the whole list."""
    prices = prices or {}
    return {n: prices[n] for n in dict.fromkeys(n for n in names if n) if n in prices}


def row(input, output, cached=None, cache_write=None, max_output=None) -> dict:
    """One row, checked: rates are numbers not below zero, the limit a whole number above it."""
    out = {}
    for name, v in (("input", input), ("output", output), ("cached", cached), ("cache_write", cache_write)):
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
    """``MODEL=INPUT,OUTPUT[,CACHED]``, a run's ``--price``."""
    name, eq, rest = str(text).partition("=")
    bits = [b.strip() for b in rest.split(",")] if eq else []
    if not name.strip() or not 2 <= len(bits) <= 3 or not all(bits):
        raise ValueError(f"--price {text!r}: MODEL=INPUT,OUTPUT[,CACHED], in USD per million tokens")
    try:
        return name.strip(), row(*bits)
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
        for k in ("cached", "cache_write"):
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
    doc = load(env)
    doc.update({"from": source, "on": today or time.strftime("%Y-%m-%d"), "models": models})
    save(doc, env)
    return {"n": len(models), "from": doc["from"], "on": doc["on"]}


def set_own(name: str, r: dict, env=None) -> None:
    name = str(name).strip()
    if not name or not _usable(r):
        raise ValueError("a model's name, and its input and output prices")
    doc = load(env)
    doc["own"][name] = r
    save(doc, env)


def remove_own(name: str, env=None) -> bool:
    doc = load(env)
    if doc["own"].pop(str(name).strip(), None) is None:
        return False
    save(doc, env)
    return True


def state(env=None) -> dict:
    """What Settings shows: how many models the list prices, where it came
    from and when, and the user's own rows."""
    doc = load(env)
    return {"n": len(doc["models"]), "from": doc["from"], "on": doc["on"], "own": doc["own"]}


def said(name: str, r: dict) -> str:
    bits = [f"input {r['input']:g}", f"output {r['output']:g}"]
    bits += [f"{label} {r[k]:g}" for k, label in (("cached", "cached"), ("cache_write", "cache write")) if k in r]
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
            r = row(a.input, a.output, a.cached, a.cache_write, a.max_output)
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
        print(json.dumps({"file": path(), "from": doc["from"], "on": doc["on"], "n": len(doc["models"]), "own": doc["own"],
                          "models": {n: both.get(n) for n in names}}, indent=2))
        return 0
    print(f"file: {path()}")
    print(f"list: {len(doc['models'])} models, from {doc['from']} on {doc['on']}" if doc["models"]
          else "list: none yet - nullius prices update")
    for n, r in sorted(doc["own"].items()):
        print("own  " + said(n, r))
    for n in names:
        print(said(n, both[n]) if n in both else f"{n}: no price")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
