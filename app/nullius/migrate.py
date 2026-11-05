"""Preview and copy an existing checkout's user data into a persistent profile.

Sources are never changed. Existing catalogue entries must have identical
inventories and bytes; conflicting data stops migration before any writes.
Only explicit --apply writes. No output includes credential values.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import sys

from nullius import paths, settings

SKIP = {".git", "__pycache__", ".pytest_cache", ".venv"}
PATH_SETTINGS = {"RUNS_DIR", "WORKSPACES_DIR", "HARBOR_PINS", "STANDARD_DIR", "STANDARD_DATA_DIR",
                 "SECRETS_DIR", "NULLIUS_CATALOGUE"}


@dataclass
class CopyPlan:
    files: list[tuple[Path, bytes, int, bytes | None]] = field(default_factory=list)
    directories: set[Path] = field(default_factory=set)
    catalogue_entries: dict[Path, dict | None] = field(default_factory=dict)
    catalogue_snapshot: bool = False


def _no_symlinks(path: Path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"Refusing a symbolic link: {path}")


def _inventory(root: Path, skip=()) -> dict | None:
    """Relative directories (None) and file bytes, including an empty root."""
    _no_symlinks(root)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    inventory = {Path("."): None}

    def visit(directory):
        for source in sorted(directory.iterdir()):
            if source.is_symlink():
                raise ValueError(f"Refusing a symbolic link: {source}")
            relative = source.relative_to(root)
            if source.is_dir():
                if source.name not in skip:
                    inventory[relative] = None
                    visit(source)
            elif stat.S_ISREG(source.stat().st_mode):
                inventory[relative] = source.read_bytes()
            else:
                raise ValueError(f"Not a regular file: {source}")

    visit(root)
    return inventory


def _files(root: Path):
    _no_symlinks(root)
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs + sorted(files):
            source = Path(directory, name)
            if source.is_symlink():
                raise ValueError(f"Refusing a symbolic link: {source}")
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            source = Path(directory, name)
            if not stat.S_ISREG(source.stat().st_mode):
                raise ValueError(f"Not a regular file: {source}")
            yield source


def _absolute(value: str, checkout: Path) -> str:
    p = Path(value).expanduser()
    return os.path.abspath(p if p.is_absolute() else checkout / p)


def prepare(checkout: Path, licence_source: Path | None = None):
    """Return a copy plan and counts; create nothing, including during preview."""
    checkout = checkout.expanduser().resolve()
    if not (checkout / "app" / "nullius").is_dir():
        raise ValueError("--from must name a Nullius checkout containing app/nullius.")
    old_file = checkout / "app" / ".env"
    _no_symlinks(old_file)
    values = settings.in_file(str(old_file))
    for name, value in list(values.items()):
        if not settings._ENVNAME_OK.fullmatch(name) or any(c in value for c in "\0\r\n"):
            raise ValueError("The old settings file contains an invalid variable name or value.")
        if value and name in PATH_SETTINGS:
            values[name] = _absolute(value, checkout)
        elif value and name in ("JOB_ROOTS", "DATASET_ROOTS"):
            values[name] = os.pathsep.join(_absolute(v, checkout) for v in value.split(os.pathsep) if v)
    # Explicit workspace locations stay in use, without rewriting their records.
    old_workspaces = None
    if not values.get("WORKSPACES_DIR"):
        values.pop("WORKSPACES_DIR", None)
        old_workspaces = (checkout / "workspaces").resolve()
    # Large raw runs stay where they are. Their location is made explicit.
    if not values.get("RUNS_DIR") and (checkout / "runs").is_dir():
        values["RUNS_DIR"] = str(checkout / "runs")
    if licence_source is not None:
        values.pop("SECRETS_DIR", None)

    # Resolve selected storage roots once; links inside their contents remain refused.
    target = Path(paths.config_dir()).expanduser().resolve() / "settings.json"
    catalogue_target = Path(paths.catalogue_dir()).expanduser().resolve()
    workspaces_target = Path(paths.workspaces_dir()).expanduser().resolve()
    secrets_target = Path(paths.secrets_dir()).expanduser().resolve()
    _no_symlinks(target)
    held = settings.in_file(str(target))
    for name, value in values.items():
        if name in held and held[name] != value:
            raise ValueError(f"Existing setting differs: {name}. Choose a separate NULLIUS_HOME or resolve it first.")
    merged = {**held, **values}
    pending = CopyPlan()
    counts = {"settings": len(values), "catalogue_entries": 0, "workspace_files": 0, "licence_files": 0}

    def destination(dest):
        _no_symlinks(dest)
        if dest.resolve().is_relative_to(checkout):
            raise ValueError("Migration destinations must be outside the source checkout.")

    def add(dest, data, mode=0o600):
        dest = Path(dest)
        destination(dest)
        if dest.exists():
            if not dest.is_file() or dest.read_bytes() != data:
                raise ValueError(f"Destination already contains different data: {dest}")
        else:
            pending.files.append((dest, data, mode, None))

    if merged != held or (merged and not target.exists()):
        # Merging non-conflicting settings is the one allowed replacement.
        data = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode()
        destination(target)
        pending.files.append((target, data, 0o600, target.read_bytes() if target.exists() else None))

    bundled = Path(__file__).resolve().parent / "catalogue"
    source_catalogue = checkout / "app" / "nullius" / "catalogue"
    pending.catalogue_snapshot = source_catalogue.resolve() == bundled.resolve()
    for kind in ("benchmarks", "agents", "features", "capabilities"):
        parent = source_catalogue / kind
        _no_symlinks(parent)
        if not parent.exists():
            continue
        for entry in sorted(parent.iterdir()):
            _no_symlinks(entry)
            if entry.name in SKIP or not entry.is_dir():
                continue
            inventory = _inventory(entry, skip=SKIP)
            if any(p.name == ".env" for p in inventory):
                raise ValueError("A catalogue entry contains .env; move its credentials to Settings before migration.")
            expected = bundled / kind / entry.name
            if not pending.catalogue_snapshot and inventory == _inventory(expected, skip=SKIP):
                continue
            counts["catalogue_entries"] += 1
            dest = catalogue_target / kind / entry.name
            destination(dest)
            existing = _inventory(dest)
            if existing is not None and inventory != existing:
                raise ValueError(f"Destination catalogue entry differs: {dest}")
            pending.catalogue_entries[dest] = existing
            if existing is not None:
                continue
            for relative, data in inventory.items():
                if data is None:
                    pending.directories.add(dest / relative)
                else:
                    source = entry / relative
                    add(dest / relative, data, 0o700 if source.stat().st_mode & 0o111 else 0o600)

    for source in _files(old_workspaces) if old_workspaces is not None else ():
        data = source.read_bytes()
        if source.name == "workspace.json":
            try:
                record = json.loads(data)
                record["roots"] = [_absolute(v, checkout) for v in record["roots"]]
                data = (json.dumps(record, indent=1, ensure_ascii=False) + "\n").encode()
            except (ValueError, KeyError, TypeError):
                raise ValueError(f"Invalid workspace record: {source}") from None
        add(workspaces_target / source.relative_to(old_workspaces), data)
        counts["workspace_files"] += 1
    if licence_source is not None:
        licence_source = licence_source.expanduser().resolve()
        if not licence_source.is_dir():
            raise ValueError("--secrets must name an existing licence directory.")
        for source in _files(licence_source):
            add(secrets_target / source.relative_to(licence_source), source.read_bytes())
            counts["licence_files"] += 1
    return pending, counts


def apply(pending):
    """Write the prepared copy using private files. Original data is untouched."""
    import tempfile
    for dest, inventory in pending.catalogue_entries.items():
        if _inventory(dest) != inventory:
            raise ValueError(f"Destination changed since preview: {dest}")
    for dest, _, _, previous in pending.files:
        _no_symlinks(dest)
        if (dest.read_bytes() if dest.exists() else None) != previous:
            raise ValueError(f"Destination changed since preview: {dest}")

    def mkdir(parent):
        missing = []
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)

    for directory in sorted(pending.directories):
        mkdir(directory)
    for dest, data, mode, _ in pending.files:
        mkdir(dest.parent)
        fd, temporary = tempfile.mkstemp(prefix=".migrate-", dir=dest.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, dest)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Copy legacy settings, custom catalogue entries and workspaces; preview by default.")
    parser.add_argument("--from", dest="checkout", type=Path, required=True, help="existing Nullius checkout (left unchanged)")
    parser.add_argument("--secrets", type=Path, help="also copy a legacy benchmark licence directory")
    parser.add_argument("--apply", action="store_true", help="write the previewed copy; refuse conflicting destination data")
    args = parser.parse_args(argv)
    try:
        pending, counts = prepare(args.checkout, args.secrets)
        if args.apply:
            apply(pending)
    except (OSError, ValueError) as error:
        print(f"Migration stopped: {error}", file=sys.stderr)
        return 1
    print("Copied." if args.apply else "Preview only. Add --apply to copy; source files are kept.")
    if pending.catalogue_snapshot:
        print("Source is the running catalogue; all entries are copied as persistent overlays "
              "that retain these versions across upgrades.")
    for name, count in counts.items():
        print(f"{name}: {count}")
    print(f"directories_to_create: {len(pending.directories)}")
    print(f"files_to_write: {len(pending.files)}")
    print(f"bytes_to_write: {sum(len(data) for _, data, _, _ in pending.files)}")
    print(f"settings: {paths.settings_file()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
