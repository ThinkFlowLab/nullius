"""The field contract: what the templates render by attribute name.

The templates' dependency on the backend is wider than its import list. The
templates reach into returned objects by attribute — ``trial.agent_version``,
``span.kind``, ``job.n_trials`` — and no import catches a renamed field. A
rename lands as a blank cell or an UndefinedError in a rendered page, at run
time, in whichever view happened to use it.

So the fields are pinned here rather than trusted. ``snapshot()`` reads the
live classes; ``compare()`` diffs a snapshot against the committed manifest and
reports removals separately from additions, because only removals break a
template. Additions are recorded so the manifest can be refreshed deliberately.
``test_field_contract.py`` runs the check in the backend suite.

    python3 app/tests/field_contract.py --write    # refresh the manifest
    python3 app/tests/field_contract.py            # check against it

Module *names* are the manifest's keys and they survive a module changing
repositories; :data:`PACKAGE_OF` says which package provides each one today.

Properties count as fields: a template cannot tell ``Job.agent_version`` (a
property) from ``Trial.agent_version`` (a dataclass field), so neither can this.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from functools import cached_property

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

MANIFEST = os.path.join(HERE, "field_contract.json")

# Every module whose objects reach a template or a route's return value. Kept
# explicit rather than walked: the point is a deliberate list of what is
# promised, and a module that stops being rendered should leave this list by
# someone's decision rather than by a walker no longer finding it.
# Stable manifest keys map to the modules that provide each contract.
# Moving a module must not reset the field comparison or conceal removals.
PACKAGE_OF = {
    "parse": "nullius.runs", "classify": "nullius.runs", "artifacts": "nullius.runs",
    "trace": "nullius.traces", "trajectory": "nullius.traces", "charts": "nullius.traces",
    "conversation": "nullius.traces", "behavior": "nullius.traces",
    "comparison": "nullius.compare",
    "catalog": "nullius.catalogue", "benchmarks": "nullius.runs",
    "harnesses": "nullius", "workspace": "nullius",
}
# Manifest keys whose importable module names differ.
MODULE_NAME = {"harnesses": "importers", "benchmarks": "verifier"}

MODULES = tuple(PACKAGE_OF)


def _public_attrs(cls) -> list:
    """Dataclass fields plus properties — everything a template may reach."""
    names = set()
    if dataclasses.is_dataclass(cls):
        names.update(f.name for f in dataclasses.fields(cls))
    for base in reversed(getattr(cls, "__mro__", [cls])):
        if base is object:
            continue
        for name, value in vars(base).items():
            if name.startswith("_"):
                continue
            if isinstance(value, (property, cached_property)):
                names.add(name)
    # NamedTuple carries its own field list and is not a dataclass.
    names.update(n for n in getattr(cls, "_fields", ()) if not n.startswith("_"))
    return sorted(names)



def snapshot() -> dict:
    """Every public class in MODULES, with the attribute names it promises."""
    import importlib

    out: dict = {}
    for mod_name in MODULES:
        try:
            mod = importlib.import_module(f"{PACKAGE_OF[mod_name]}.{MODULE_NAME.get(mod_name, mod_name)}")
        except ImportError as exc:  # a module that moved into the package
            out[mod_name] = {"__unimportable__": str(exc)}
            continue
        entries = {}
        for name, obj in sorted(vars(mod).items()):
            if name.startswith("_") or not isinstance(obj, type):
                continue
            # Only classes this module defines: a re-export is the defining
            # module's promise, and pinning it twice would report one rename
            # as two breakages.
            if getattr(obj, "__module__", "") != mod.__name__:
                continue
            attrs = _public_attrs(obj)
            if attrs:
                entries[name] = attrs
        out[mod_name] = entries
    return out


def compare(live: dict, pinned: dict) -> dict:
    """Removals break templates; additions do not. Reported separately."""
    removed, added, vanished = [], [], []
    for mod, classes in pinned.items():
        if mod not in live or "__unimportable__" in live.get(mod, {}):
            vanished.append(mod)
            continue
        for cls, fields in classes.items():
            if cls == "__unimportable__":
                continue
            if cls not in live[mod]:
                vanished.append(f"{mod}.{cls}")
                continue
            gone = sorted(set(fields) - set(live[mod][cls]))
            new = sorted(set(live[mod][cls]) - set(fields))
            removed += [f"{mod}.{cls}.{f}" for f in gone]
            added += [f"{mod}.{cls}.{f}" for f in new]
    return {"removed": removed, "added": added, "vanished": vanished,
            "ok": not removed and not vanished}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="refresh the manifest")
    args = ap.parse_args()
    live = snapshot()
    # Fail closed. A manifest refreshed while a module could not be imported
    # would record its absence as the new truth and bless exactly the breakage
    # this file exists to catch.
    broken = sorted(m for m, v in live.items() if "__unimportable__" in v)
    if broken:
        for m in broken:
            print(f"UNIMPORTABLE {m}: {live[m]['__unimportable__']}")
        print("field contract: REFUSED — cannot read every module")
        return 2
    if args.write:
        with open(MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(live, fh, indent=1, sort_keys=True)
            fh.write("\n")
        n = sum(len(v) for v in live.values())
        print(f"wrote {MANIFEST}: {n} classes")
        return 0
    with open(MANIFEST, encoding="utf-8") as fh:
        pinned = json.load(fh)
    result = compare(live, pinned)
    for name in result["vanished"]:
        print(f"GONE     {name}")
    for name in result["removed"]:
        print(f"REMOVED  {name}")
    for name in result["added"]:
        print(f"added    {name}")
    print("field contract: ok" if result["ok"] else "field contract: BROKEN")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
