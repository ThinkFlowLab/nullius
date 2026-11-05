"""A standard runner snapshot made from this runtime and its effective catalogue.

Only backend Python modules, definition documents and their local resources
travel. The installation's neighbours, user configuration, licences and SSH
keys are never inputs. Source checkouts and installed wheels use the same
content address, including executable bits; neither Git nor the viewer is
needed on the target. Credential transport belongs to ``execution``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from nullius import paths

PACKAGE = Path(__file__).resolve().parents[1]
FORMAT = 1
_KINDS = {"benchmarks": ("benchmark.json", "standard.json"),
          "agents": ("agent.json",), "capabilities": ("capability.json",)}
_DOCUMENTS = {"feature.json", "benchmark.json", "standard.json", "agent.json", "capability.json",
              "upstream-settings.json", "images.json"}
_PRIVATE = {"credentials", "credentials.json", "secret", "secret.json", "secrets", "secrets.json", "settings.json",
            "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "node_modules", "__pycache__"}
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_HERE = re.compile(r"\{here\}/([A-Za-z0-9_./-]+)")


def _safe(relative: str) -> bool:
    p = Path(relative)
    return (bool(relative) and not p.is_absolute() and "\\" not in relative
            and all(not s.startswith(".") and s.lower() not in _PRIVATE for s in p.parts)
            and p.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".pyc"})


def _read(root: Path, relative: str) -> tuple[bytes, int]:
    """Read a regular file below root, refusing symlinks at every segment."""
    if not _safe(relative):
        raise ValueError(f"not a runner resource: {relative}")
    p = root
    for segment in Path(relative).parts:
        p = p / segment
        if p.is_symlink():
            raise ValueError(f"runner resources cannot be symlinks: {relative}")
    mode = p.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"runner resource is not a file: {relative}")
    return p.read_bytes(), 0o755 if mode & 0o111 else 0o644


def _python(root: Path):
    """Backend/glue code only: never follow a directory link or copy caches."""
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(n for n in folders if _safe(n) and n != "_resources"
                            and not (Path(directory) / n).is_symlink())
        for name in sorted(files):
            if name.endswith(".py") and _safe(name):
                yield (Path(directory) / name).relative_to(root).as_posix()


def _resources(d: dict) -> set[str]:
    """Local files named by commands, model templates and image contexts.

    Walk version blocks too. Licence declarations name runtime destinations,
    not resources; they are deliberately not interpreted as local paths.
    """
    out: set[str] = set()
    def visit(value):
        if isinstance(value, str):
            out.update(_HERE.findall(value))
        elif isinstance(value, list):
            for v in value:
                visit(v)
        elif isinstance(value, dict):
            for k, v in value.items():
                if k != "needs_secrets":
                    visit(v)
            model = value.get("model")
            if isinstance(model, dict) and isinstance(model.get("files"), dict):
                out.update(v for v in model["files"].values() if isinstance(v, str))
            environments = value.get("environments")
            if isinstance(environments, dict):
                for environment in environments.values():
                    if isinstance(environment, dict):
                        if isinstance(environment.get("dockerfile"), str):
                            out.add(environment["dockerfile"])
                        out.update(v for v in environment.get("files", []) if isinstance(v, str))
    visit(d)
    return out


def _imports(root: Path, relative: str, content: bytes) -> set[str]:
    """Follow local Python imports from a declared driver without sweeping its folder."""
    if not relative.endswith(".py"):
        return set()
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return set()                  # Python itself reports invalid source when run
    found = set()
    for node in ast.walk(tree):
        names = []
        base = Path(relative).parent
        if isinstance(node, ast.Import):
            names = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                for _ in range(node.level - 1):
                    base = base.parent
            prefix = (node.module + ".") if node.module else ""
            names = ([node.module] if node.module else []) + [prefix + n.name for n in node.names if n.name != "*"]
        for name in names:
            for origin in (base, Path()):
                module = origin / name.replace(".", "/")
                for candidate in (module.with_suffix(".py"), module / "__init__.py"):
                    r = candidate.as_posix()
                    if _safe(r) and (root / r).is_file():
                        found.add(r)
                for parent in module.parents:
                    r = (parent / "__init__.py").as_posix()
                    if _safe(r) and (root / r).is_file():
                        found.add(r)
    return found


def _entry(root: Path, *, kit: bool = False) -> dict[str, tuple[bytes, int]]:
    if root.is_symlink():
        raise ValueError("catalogue entries cannot be symlinks")
    files = {}
    resources = set(_python(root)) if kit else set()
    for name in sorted(_DOCUMENTS):
        if (root / name).exists():
            content = _read(root, name)
            d = json.loads(content[0])
            if name in {"standard.json", "agent.json", "capability.json", "feature.json", "benchmark.json"} and not isinstance(d, dict):
                raise ValueError(f"{name}: expected a definition object")
            files[name] = content
            if name in {"standard.json", "agent.json", "capability.json", "feature.json"}:
                resources.update(_resources(d))
    while resources - files.keys():
        name = min(resources - files.keys())
        files[name] = _read(root, name)
        resources.update(_imports(root, name, files[name][0]))
    return files


def _cached(destination: Path, marker: str, files: dict) -> bool:
    """Do not reuse or transfer a cache into which another file was written."""
    if not destination.exists():
        return False
    manifest = destination / "manifest.json"
    expected = {"manifest.json", *("app/nullius/" + name for name in files)}
    found = set()
    if destination.is_symlink():
        raise ValueError("runner snapshot cache cannot be a symlink")
    for directory, folders, names in os.walk(destination, followlinks=False):
        for name in folders + names:
            p = Path(directory) / name
            if p.is_symlink():
                raise ValueError("runner snapshot cache contains a symlink; remove " + str(destination))
        found.update((Path(directory) / name).relative_to(destination).as_posix() for name in names)
    valid = found == expected and manifest.read_text() == marker
    if valid:
        for name, value in files.items():
            if _read(destination / "app/nullius", name) != value:
                valid = False
                break
    if not valid:
        raise ValueError("runner snapshot cache has changed; remove " + str(destination))
    return True


def build(cache: str, *, package: Path | None = None, catalogue: str | None = None) -> tuple[str, str]:
    """Return (content ID, snapshot root), with ``app/nullius`` below it.

    A user entry replaces the whole bundled entry, as the normal catalogue
    loader does. Nothing is created in either input tree. The staged tree is
    replaced atomically, and the address covers the exact bytes being sent.
    """
    package = Path(package) if package is not None else PACKAGE
    if package.is_symlink():
        raise ValueError("the runtime package cannot be a symlink")
    files = {}
    for relative in _python(package):
        pieces = Path(relative).parts
        if ((len(pieces) > 2 and pieces[0] == "catalogue" and pieces[1] in _KINDS)
                or pieces[:2] == ("standard", "kits")):
            continue
        files[relative] = _read(package, relative)
    if "standard/cli.py" not in files or "__init__.py" not in files:
        raise ValueError("the standard runtime package is incomplete")
    bundled = package / "catalogue"
    user = Path(catalogue if catalogue is not None else paths.catalogue_dir())
    if user.is_symlink() or bundled.is_symlink():
        raise ValueError("catalogue roots cannot be symlinks")
    for kind in _KINDS:
        entries = {}
        for catalogue_root in (bundled, user):
            for directory in (("features", "capabilities") if kind == "capabilities" else (kind,)):
                base = catalogue_root / directory
                if base.is_symlink():
                    raise ValueError("catalogue folders cannot be symlinks")
                if not base.is_dir():
                    continue
                for entry in sorted(base.iterdir()):
                    if (_KEY.fullmatch(entry.name) and entry.is_dir()
                            and sorted(p.name for p in entry.iterdir()) != ["__pycache__"]):
                        entries[entry.name] = entry
        for name, entry in sorted(entries.items()):
            try:
                contents = _entry(entry)
            except json.JSONDecodeError:
                continue  # Unfinished JSON drafts are not runnable catalogue entries.
            files.update({f"catalogue/{kind}/{name}/{r}": value for r, value in contents.items()})
    kits = package / "standard" / "kits"
    if kits.is_dir():
        if kits.is_symlink():
            raise ValueError("the kit folder cannot be a symlink")
        for entry in sorted(kits.iterdir()):
            if _KEY.fullmatch(entry.name) and (entry / "standard.json").is_file():
                files.update({f"standard/kits/{entry.name}/{r}": value for r, value in _entry(entry, kit=True).items()})
    digest = hashlib.sha256(f"nullius-standard-payload-v{FORMAT}\0".encode())
    manifest = {}
    for name, (content, mode) in sorted(files.items()):
        manifest[name] = {"sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
    identity = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest.update(identity)
    key = "v%d-%s" % (FORMAT, digest.hexdigest())
    destination = Path(cache) / key
    marker = json.dumps({"format": FORMAT, "files": manifest}, sort_keys=True, indent=2) + "\n"
    if _cached(destination, marker, files):
        return key, str(destination)
    Path(cache).mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".payload-", dir=cache))
    try:
        for name, (content, mode) in sorted(files.items()):
            target = staging / "app" / "nullius" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        (staging / "manifest.json").write_text(marker)
        try:
            staging.rename(destination)
        except OSError:
            if not _cached(destination, marker, files):
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return key, str(destination)
