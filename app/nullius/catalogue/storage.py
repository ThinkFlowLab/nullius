"""Whole-entry catalogue overlays, shared by the catalogue and the standard.

An explicit root is a single isolated tree. Otherwise the user's folder wins
over the bundled folder of the same name, including when its entry is invalid.
We never combine files from different copies of an entry.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile

from nullius import paths


def roots(bundled: str, root: str | None = None) -> tuple:
    if root is not None:
        return (os.fspath(root),)
    user = paths.catalogue_dir()
    return (bundled,) if os.path.realpath(user) == os.path.realpath(bundled) else (bundled, user)


def folders(group: str, bundled: str, root: str | None = None) -> dict:
    """Selected entry folders, sorted by key; an empty folder still wins."""
    found = {}
    for base in roots(bundled, root):
        for directory in (("features", "capabilities") if group == "capabilities" else (group,)):
            parent = os.path.join(base, directory)
            try:
                names = os.listdir(parent)
            except FileNotFoundError:
                continue
            for name in sorted(names):
                folder = os.path.join(parent, name)
                if name.startswith((".", "_")) or not os.path.isdir(folder):
                    continue
                if os.listdir(folder) == ["__pycache__"]:
                    continue
                found[name] = folder
    return dict(sorted(found.items()))


def entry_dir(group: str, key: str, bundled: str, root: str | None = None) -> str | None:
    return folders(group, bundled, root).get(key)


def writable_root(bundled: str, root: str | None = None) -> str:
    """The destination, refusing paths that would mutate package resources."""
    target = os.fspath(root) if root is not None else paths.catalogue_dir()
    ensure_writable(target, bundled)
    return target


def ensure_writable(target: str, bundled: str) -> None:
    bundle = os.path.realpath(bundled)
    try:
        common = os.path.commonpath((os.path.realpath(target), bundle))
    except ValueError:  # different drives on Windows cannot overlap
        return
    if common == bundle:
        raise ValueError("the bundled catalogue is read-only; use the user catalogue")


def writable_dir(group: str, key: str, bundled: str, root: str | None = None) -> str:
    """Copy a selected bundled folder in full before returning its user copy.

    An existing user folder is left intact. Staging keeps an interrupted copy
    from shadowing a bundled entry with half of its files.
    """
    if not key or key in (".", "..") or os.path.basename(key) != key:
        raise ValueError("an entry key is a folder name")
    target = os.path.join(writable_root(bundled, root), group, key)
    ensure_writable(target, bundled)
    source = entry_dir(group, key, bundled, root)
    if source is None or os.path.realpath(source) == os.path.realpath(target):
        return target
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".catalogue-", dir=parent) as stage:
        copied = os.path.join(stage, key)
        shutil.copytree(source, copied)
        # Package resources may be read-only. The copy belongs to its user.
        for folder, _, files in os.walk(copied):
            os.chmod(folder, os.stat(folder).st_mode | stat.S_IWUSR)
            for name in files:
                path = os.path.join(folder, name)
                os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
        os.rename(copied, target)
    return target


def _restore_targets(group: str, key: str, bundled: str) -> list[str]:
    if group not in ("benchmarks", "agents", "capabilities"):
        raise ValueError("unknown catalogue")
    if not key or key in (".", "..") or "/" in key or "\\" in key:
        raise ValueError("an entry key is a folder name")
    if entry_dir(group, key, bundled, root=bundled) is None:
        raise ValueError("this entry has no bundled copy")
    user = writable_root(bundled)
    groups = ("features", "capabilities") if group == "capabilities" else (group,)
    targets = [os.path.join(user, directory, key) for directory in groups
               if os.path.isdir(os.path.join(user, directory, key))]
    if not targets:
        raise ValueError("this entry has no user copy")
    for target in targets:
        ensure_writable(target, bundled)
    return targets


def can_restore(group: str, key: str, bundled: str) -> bool:
    """Whether removing the user override can reveal a bundled entry."""
    try:
        return bool(_restore_targets(group, key, bundled))
    except (OSError, ValueError):
        return False


def use_bundled(group: str, key: str, bundled: str) -> None:
    """Remove complete user overrides, including a capability's legacy alias."""
    for target in _restore_targets(group, key, bundled):
        if os.path.islink(target):
            os.unlink(target)
        else:
            shutil.rmtree(target)
