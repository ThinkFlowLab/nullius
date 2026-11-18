"""Saved builder recipes: one owner-only JSON file per recipe under the
profile's data directory (``recipes/<id>.json``).

A file holds ``id``, ``name`` (the title the builder shows), ``recipe`` (the
recipe as the builder saves it) and ``saved`` (UTC, ISO 8601). The id is
random, so two names can never share a file; names are unique instead. A save
names the recipe it updates by id (a rename keeps the file), or else updates
the recipe of the same name. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from typing import Dict, Iterable, List, Optional

from nullius import paths

ID = re.compile(r"^[0-9a-f]{16}$")
_HARNESS = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,71}$")
MAX_BYTES = 200_000                 # as the builder's other recipe routes
MAX_NAME = 200


def root(env=None) -> str:
    return paths.recipes_dir(env)


def _path(recipe_id: str) -> str:
    if not isinstance(recipe_id, str) or not ID.match(recipe_id):
        raise ValueError(f"{recipe_id!r} is not a recipe id")
    return os.path.join(root(), recipe_id + ".json")


def entry(recipe) -> Dict:
    """The stored form of one recipe, without its id, or ValueError saying what is wrong with it."""
    if not isinstance(recipe, dict) or recipe.get("version") != 1:
        raise ValueError("a recipe is a JSON object with \"version\": 1")
    harness, name = recipe.get("harness") or "harbor", recipe.get("name")
    if not isinstance(harness, str) or not _HARNESS.match(harness):
        raise ValueError(f"{harness!r} is not a runner")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"{name!r} is not a recipe name (lowercase letters, digits, '.', '_', '-')")
    if len(json.dumps(recipe)) > MAX_BYTES:
        raise ValueError(f"recipe {name!r} is larger than {MAX_BYTES // 1000} kB")
    title = recipe.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else name
    if len(title) > MAX_NAME:
        raise ValueError(f"a recipe's name is at most {MAX_NAME} characters")
    return {"name": title, "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "recipe": recipe}


def _write(item: Dict) -> None:
    """Written whole under a temporary 0600 name beside it, then renamed over the old one."""
    path = _path(item["id"])
    os.makedirs(root(), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=root())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(item, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def listing() -> List[Dict]:
    """Every saved recipe, the latest saved first; an unreadable file is left out."""
    out = []
    try:
        names = os.listdir(root())
    except OSError:
        return []
    for f in names:
        if not f.endswith(".json") or not ID.match(f[:-5]):
            continue
        try:
            with open(os.path.join(root(), f), encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(got, dict) and got.get("id") == f[:-5] and isinstance(got.get("recipe"), dict):
            out.append(got)
    return sorted(out, key=lambda x: (str(x.get("saved", "")), x["id"]), reverse=True)


def save(recipe, recipe_id: Optional[str] = None) -> Dict:
    """Update the recipe ``recipe_id`` (renamed when its name changed), or else the
    recipe of the same name, or else add one. A name another recipe holds is refused."""
    item = entry(recipe)
    held = recipe_id if recipe_id and os.path.exists(_path(recipe_id)) else None
    same = next((x["id"] for x in listing() if x.get("name") == item["name"]), None)
    if held and same and same != held:
        raise ValueError(f"another saved recipe is named {item['name']!r}")
    item = {"id": held or same or secrets.token_hex(8), **item}
    _write(item)
    return item


def delete(recipe_id: str) -> bool:
    try:
        os.unlink(_path(recipe_id))
        return True
    except FileNotFoundError:
        return False


def import_(items: Iterable, lenient: bool = False) -> Dict[str, list]:
    """Add each recipe whose name is not saved yet; never replace one. An item is a
    recipe, or ``{"recipe": ...}`` as the builder's earlier browser list held them.
    Strict: one bad item refuses them all, before anything is written. Lenient: the
    good ones are added and each bad one is returned with its reason. A skipped one is
    returned with its index and name."""
    items = list(items)
    checked, rejected = [], []
    for n, item in enumerate(items):
        recipe = item.get("recipe") if isinstance(item, dict) and "recipe" in item else item
        try:
            checked.append((n, entry(recipe)))
        except ValueError as e:
            if not lenient:
                raise ValueError(f"recipe {n + 1} of {len(items)}: {e}") from None
            rejected.append({"index": n, "reason": str(e)})
    taken = {x.get("name") for x in listing()}
    added, skipped = [], []
    for n, item in checked:
        if item["name"] in taken:
            skipped.append({"index": n, "name": item["name"]})
            continue
        item = {"id": secrets.token_hex(8), **item}
        _write(item)
        taken.add(item["name"])
        added.append(item["id"])
    return {"added": added, "skipped": skipped, "rejected": rejected}
