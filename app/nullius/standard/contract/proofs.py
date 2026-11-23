"""Generated evidence beside an authored definition, in ``proofs.json``.

One document owns one entry's evidence, keyed by the declared version (the
empty string for a definition without a version). A version block holds
``proven`` and, for a flat agent or capability, its generated ``offers``.
Versioned participants derive offers from their own proven ports as before.

An existing document replaces all embedded evidence; an absent document keeps
legacy definitions readable. Never combine evidence from different folders.
Writers hold the definition's lock while reading, changing and replacing it.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile

from . import names

FILE = "proofs.json"
SCHEMA = "nullius.proofs/v1"
FIELDS = ("offers", "proven")


def blocks(d: dict) -> dict:
    """The raw version blocks, including the single unnamed version."""
    if "versions" in d:
        if not isinstance(d["versions"], dict) or not all(isinstance(b, dict) for b in d["versions"].values()):
            raise ValueError("versions: an object of version objects")
        return d["versions"]
    return {str((d.get("upstream") or {}).get("version") or ""): d}


def document(d: dict, kind: str) -> dict:
    """Extract evidence without modifying the combined definition."""
    return {"schema": SCHEMA, "kind": names.kind(kind), "key": d["key"],
            "versions": {v: {f: copy.deepcopy(b[f]) for f in FIELDS if f in b}
                         for v, b in blocks(d).items()}}


def strip(d: dict) -> dict:
    """The authored definition, without generated fields or loader metadata."""
    out = copy.deepcopy(d)
    for b in [out, *blocks(out).values()]:
        for f in list(b):
            if f in FIELDS or f.startswith("_"):
                b.pop(f)
    return out


def validate(value: dict, d: dict, kind: str) -> None:
    """Check ownership and structure before evidence reaches a definition."""
    def refuse(message):
        raise ValueError(f"{FILE}: {message}")

    if not isinstance(value, dict):
        refuse("a JSON object")
    expected = {"schema": SCHEMA, "kind": names.kind(kind), "key": d.get("key")}
    for field, wanted in expected.items():
        if value.get(field) != wanted:
            refuse(f"{field}: expected {wanted!r}")
    if set(value) != {*expected, "versions"}:
        refuse("only schema, kind, key and versions are allowed")
    vs = value["versions"]
    if not isinstance(vs, dict):
        refuse("versions: an object")
    declared = blocks(d)
    for v, block in vs.items():
        if v not in declared:
            refuse(f"versions.{v}: not a declared version")
        if not isinstance(block, dict) or set(block) - set(FIELDS):
            refuse(f"versions.{v}: only proven and offers are allowed")
        if "offers" in block:
            offers = block["offers"]
            if kind == "benchmark":
                refuse(f"versions.{v}.offers: only an agent or capability stores offers")
            if not isinstance(offers, list) or not all(isinstance(p, str) for p in offers) or len(set(offers)) != len(offers):
                refuse(f"versions.{v}.offers: a list of distinct port names")
        proven = block.get("proven", {})
        if not isinstance(proven, dict):
            refuse(f"versions.{v}.proven: an object")
        for name, rec in proven.items():
            if not isinstance(rec, dict):
                refuse(f"versions.{v}.proven.{name}: an object")
            # Flat legacy entries can retain an old record after a pin changes:
            # match.fit reports it as stale while each port is proven again.
            if "versions" in d and kind != "benchmark" and rec.get("version") not in (None, "", v):
                refuse(f"versions.{v}.proven.{name}: proven on {rec['version']}, not {v}")


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"{FILE}: duplicate key {key!r}")
        out[key] = value
    return out


def attach(d: dict, kind: str, folder: str) -> dict:
    """Read sibling evidence, or keep embedded legacy evidence when absent."""
    if not isinstance(d, dict):
        return d
    try:
        with open(os.path.join(folder, FILE)) as f:
            value = json.load(f, object_pairs_hook=_unique)
    except FileNotFoundError:
        return d
    validate(value, d, kind)
    out = strip(d)
    for v, b in blocks(out).items():
        evidence = names.definition(copy.deepcopy(value["versions"].get(v, {})))
        if "versions" in out:
            # Flat -> versioned promotion retains its old proof file first.
            # Once promoted, offers are derived from proven, never inherited.
            evidence.pop("offers", None)
        b.update(evidence)
    return out


def write(folder: str, value: dict) -> None:
    """Atomically replace evidence; the caller holds the definition's lock."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=".proofs-", suffix=".part", dir=folder, delete=False) as f:
            path = f.name
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(path, os.path.join(folder, FILE))
    finally:
        if path is not None and os.path.exists(path):
            os.unlink(path)
