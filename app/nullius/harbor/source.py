"""The Harbor a recipe pins: a release from PyPI, or a repository at a commit
- built here, kept here, scanned for its agents.

A Harbor recipe names its Harbor (the recipe's ``harbor`` field, checked by
:func:`pin_of`):

- ``{"kind": "release", "version": "0.24.0", "extras": []}`` - the official
  package, ``harbor`` on PyPI, at that version; nothing is built;
- ``{"kind": "repo", "repo": "owner/name" or a git URL, "revisionKind":
  "branch" | "tag" | "commit", "revision": ..., "commit": <40 hex>,
  "extras": []}`` - a repository, a private fork included, at the commit the
  picker resolved the branch or tag to: the commit is the pin.

A repository pin is **built on this machine**, once, with this machine's own
git credentials (an ssh key, a credential helper): the commit is fetched,
Harbor's wheel built with ``uv build``, and so is a wheel for each dependency
it declares that PyPI does not serve - one its ``[tool.uv.sources]`` names (a
git repository, a path, a workspace member, a URL) or a direct-URL
requirement - since a fork often pins a private package. The wheels and a
record (``pin.json``) go into a directory of their own under the pins root
(:func:`root`: ``HARBOR_PINS``, else ``harbor-pins/`` beside the runs), built
aside and renamed into place, so a pin is ready or absent. A host is sent
that directory and resolves everything else from PyPI: only this machine
needs access to a private fork.

Harbor runs from the pin through ``uvx`` (:func:`wrapper`), its command
line started by ``nullius.harbor.feed`` so every job writes the live feed
the job page folds: ``harbor==X`` for
a release, the built wheel with ``--find-links`` beside it for a repository
(and an override for a dependency named by URL, which a find-links
directory cannot satisfy). The scan (:func:`scan`) asks that same command
``harbor agents list --json`` - Harbor's own registry - and keeps the answer
beside the pin (``agents.json``), so it is asked once per pin. Building and
scanning run on an explicit action - the builder's scan, or a Run - never on
a page load; two asks for one pin build it once.

Nothing here imports Flask; every command goes through a ``run`` seam and
PyPI through a ``fetch`` seam.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.parse
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from typing import Callable, Dict, List, Optional
from urllib import error, request

from nullius import gitrefs

PACKAGE = "harbor"
PYPI = "https://pypi.org/pypi"
RECORD = "pin.json"
AGENTS = "agents.json"
WHEELS = "wheels"
BUILD_TIMEOUT = 1800.0
SCAN_TIMEOUT = 1800.0
KINDS = ("release", "repo")
REVISION_KINDS = ("branch", "tag", "commit")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+!-]{0,63}$")
_EXTRA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_WHEEL = re.compile(r"^[A-Za-z0-9_.+!-]+\.whl$")
_AGENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PRE = re.compile(r"(a|b|rc|dev)\d*$")
_UV_SAYS = re.compile(r"^\s*(Installed|Uninstalled|Downloading|Downloaded|Resolved|Prepared|Audited|Built|Building) ")


class BuildError(Exception):
    """A pin that could not be made ready here, and why."""


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def pin_of(raw) -> tuple:
    """``(pin, problem)`` for a recipe's ``harbor`` field: the pin
    normalised, or None with no problem when the recipe names none (a recipe
    from before the field, or a release not chosen yet) - that recipe runs
    the harbor on the target's PATH - or None and what is wrong."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        return None, None
    extras = raw.get("extras") or []
    if not isinstance(extras, list) or not all(isinstance(x, str) and _EXTRA.match(x) for x in extras):
        return None, "the Harbor extras must be names of the package's optional dependencies"
    extras = sorted(set(extras))
    if raw["kind"] == "release":
        version = str(raw.get("version") or "").strip()
        if not version:
            return None, None
        if not _VERSION.match(version):
            return None, f"{version!r} is not a Harbor release version"
        return {"kind": "release", "version": version, "extras": extras}, None
    url = gitrefs.url_of(str(raw.get("repo") or ""))
    if not url:
        return None, "the Harbor repository must be owner/name on GitHub, or a git URL"
    commit = str(raw.get("commit") or "")
    if not gitrefs.COMMIT.match(commit):
        return None, f"choose a branch, a tag or a full commit of {gitrefs.label_of(url)}: the pin is a commit"
    kind = raw.get("revisionKind") if raw.get("revisionKind") in REVISION_KINDS else "commit"
    revision = str(raw.get("revision") or commit)
    if not _REVISION.match(revision):
        return None, f"{revision!r} is not a branch, a tag or a commit"
    return {"kind": "repo", "repo": str(raw.get("repo")).strip(), "url": url, "revisionKind": kind, "revision": revision,
            "commit": commit, "extras": extras}, None


def key_of(pin: dict) -> str:
    """The pin's directory name: ``release-<version>``, ``git-<commit>``."""
    return f"release-{pin['version']}" if pin["kind"] == "release" else f"git-{pin['commit']}"


def label(pin: dict) -> str:
    """The pin as a sentence fragment: ``harbor 0.24.0 from PyPI``,
    ``harbor from owner/name, branch main at 0123456789ab``."""
    extras = f" [{','.join(pin['extras'])}]" if pin.get("extras") else ""
    if pin["kind"] == "release":
        return f"harbor {pin['version']}{extras} from PyPI"
    at = pin["commit"][:12]
    how = f"commit {at}" if pin["revisionKind"] == "commit" else f"{pin['revisionKind']} {pin['revision']} at {at}"
    return f"harbor{extras} from {gitrefs.label_of(pin['url'])}, {how}"


def root(runs: str, env=os.environ) -> str:
    """Where pins are built and kept on this machine: ``HARBOR_PINS``, else
    ``harbor-pins/`` beside the runs."""
    return env.get("HARBOR_PINS") or os.path.join(os.path.dirname(os.path.abspath(runs)), "harbor-pins")



def pypi_fetch(url: str, timeout: float = 20.0) -> bytes:
    req = request.Request(url, headers={"Accept": "application/json", "User-Agent": "nullius"})
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def releases(fetch: Optional[Callable] = None) -> dict:
    """The official Harbor's releases on PyPI, newest first: ``{versions:
    [{version, date, requires_python}], error}``. Pre-releases and versions
    with no wheel are left out; on any failure, no versions and why."""
    try:
        data = json.loads((fetch or pypi_fetch)(f"{PYPI}/{PACKAGE}/json"))
        rels = data["releases"]
        if not isinstance(rels, dict):
            raise ValueError("no releases")
    except error.HTTPError as e:
        return {"versions": [], "error": f"PyPI answered {e.code} for {PACKAGE}"}
    except (error.URLError, OSError, TimeoutError) as e:
        return {"versions": [], "error": f"PyPI could not be reached: {getattr(e, 'reason', e)}"}
    except (ValueError, KeyError, TypeError):
        return {"versions": [], "error": "PyPI's answer names no releases"}
    out = []
    for v, files in rels.items():
        if not _VERSION.match(v) or _PRE.search(v) or not isinstance(files, list):
            continue
        wheels = [f for f in files if isinstance(f, dict) and str(f.get("filename", "")).endswith(".whl") and not f.get("yanked")]
        if wheels:
            out.append({"version": v, "date": str(wheels[0].get("upload_time_iso_8601") or "")[:10],
                        "requires_python": str(wheels[0].get("requires_python") or "")})
    out.sort(key=lambda r: gitrefs.version_key(r["version"]), reverse=True)
    return {"versions": out, "error": ""}


def _release_record(pin: dict, fetch: Optional[Callable]) -> dict:
    """A release's record from PyPI: its Python requirement and its extras.
    A version PyPI does not have is an error; PyPI out of reach is not - the
    record is then the version alone, and not kept."""
    record = {"kind": "release", "version": pin["version"], "requires_python": "", "extras": [], "wheels": [], "private": []}
    try:
        info = json.loads((fetch or pypi_fetch)(f"{PYPI}/{PACKAGE}/{pin['version']}/json"))["info"]
    except error.HTTPError as e:
        if e.code == 404:
            raise BuildError(f"harbor {pin['version']} is not a release on PyPI")
        return dict(record, partial=True)
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        return dict(record, partial=True)
    return dict(record, requires_python=str(info.get("requires_python") or ""),
                extras=sorted(x for x in (info.get("provides_extra") or []) if isinstance(x, str) and _EXTRA.match(x)))



def run_command(argv: List[str], timeout: float = 600.0, stop: Optional[threading.Event] = None) -> tuple:
    """(returncode, combined output) of one command, with git unable to
    prompt; a timeout is 124, a stop (``stop`` set) is 130."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=gitrefs.env())
    except OSError as e:
        return 127, f"{argv[0]}: {e.strerror or e}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, _ = proc.communicate(timeout=0.5)
            return proc.returncode, out or ""
        except subprocess.TimeoutExpired:
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                proc.kill(); proc.communicate()
                return (130, "stopped") if stop is not None and stop.is_set() else (124, f"no answer within {int(timeout)} s")


def _tail(out: str, n: int = 6) -> str:
    lines = [l for l in (out or "").strip().splitlines() if l.strip()]
    return " / ".join(lines[-n:])


def _check(run: Callable, argv: List[str], what: str, timeout: float = BUILD_TIMEOUT) -> str:
    rc, out = run(argv, timeout)
    if rc != 0:
        raise BuildError(f"{what} failed: {_tail(out) or f'exit {rc}'}")
    return out


def _fetch_commit(run: Callable, url: str, rev: str, dest: str) -> None:
    """``dest`` checked out at ``rev`` (a commit, a branch or a tag) of
    ``url``: a shallow fetch of that one revision, else the whole repository."""
    os.makedirs(dest, exist_ok=True)
    git = gitrefs.GIT
    _check(run, [git, "init", "-q", dest], "git init")
    rc, _ = run([git, "-C", dest, "fetch", "-q", "--depth", "1", url, rev], BUILD_TIMEOUT)
    if rc == 0:
        _check(run, [git, "-C", dest, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], "git checkout")
        return
    _check(run, [git, "-C", dest, "fetch", "-q", url, "+refs/heads/*:refs/remotes/upstream/*", "+refs/tags/*:refs/tags/*"],
           f"fetching {gitrefs.label_of(url)}")
    _check(run, [git, "-C", dest, "-c", "advice.detachedHead=false", "checkout", "-q", rev], f"checking out {rev}")


def _wheel_metadata(path: str):
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA"))
        return Parser().parsestr(z.read(name).decode("utf-8"))


def _built(before: set, wheels: str, what: str) -> str:
    new = sorted(set(os.listdir(wheels)) - before)
    found = [n for n in new if n.endswith(".whl")]
    if not found:
        raise BuildError(f"{what}: no wheel was built")
    return found[0]


def _build_dir(run: Callable, src: str, wheels: str, what: str) -> str:
    before = set(os.listdir(wheels))
    _check(run, ["uv", "build", "--wheel", "--out-dir", wheels, src], f"building {what}")
    return _built(before, wheels, what)


def _requirement(text: str) -> tuple:
    """``(name, url or None)`` of one Requires-Dist line."""
    body = text.split(";", 1)[0].strip()
    if " @ " in body:
        name, url = body.split(" @ ", 1)
        return _norm(re.sub(r"\[.*\]", "", name).strip()), url.strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", body)
    return (_norm(m.group(1)) if m else "", None)


def _git_url(text: str) -> tuple:
    """``git+URL@rev#subdirectory=sub`` as (url, rev, sub)."""
    url = text[4:]
    sub = ""
    if "#" in url:
        url, frag = url.split("#", 1)
        sub = urllib.parse.parse_qs(frag).get("subdirectory", [""])[0]
    rev = ""
    tail = url.rsplit("/", 1)[-1]
    if "@" in tail:
        url, rev = url.rsplit("@", 1)
    return url, rev, sub


def _download(url: str, dest_dir: str) -> str:
    name = os.path.basename(urllib.parse.urlparse(url).path) or "download"
    path = os.path.join(dest_dir, name)
    req = request.Request(url, headers={"User-Agent": "nullius"})
    with request.urlopen(req, timeout=300) as resp, open(path, "wb") as out:
        shutil.copyfileobj(resp, out)
    return path


def _build_private(run: Callable, name: str, how: dict, src: str, work: str, wheels: str, log: Callable) -> str:
    """One dependency PyPI does not serve, as a wheel in ``wheels``. ``how``
    is its ``[tool.uv.sources]`` entry, or ``{"url": ...}`` for a
    direct-URL requirement."""
    if how.get("git"):
        # the pinned repository's own metadata says where: checked as the pin is, before git is given any of it
        url, rev, sub = gitrefs.url_of(str(how["git"])), str(how.get("rev") or how.get("tag") or how.get("branch") or "HEAD"), str(how.get("subdirectory") or "")
        if not url:
            raise BuildError(f"{name}: {how['git']!r} is not a git URL")
        if not _REVISION.match(rev):
            raise BuildError(f"{name}: {rev!r} is not a branch, a tag or a commit")
        if sub and (os.path.isabs(sub) or ".." in sub.replace("\\", "/").split("/")):
            raise BuildError(f"{name}: the subdirectory {sub!r} is outside its repository")
        log(f"  building {name} from {gitrefs.label_of(url)} at {rev}{' (' + sub + ')' if sub else ''}")
        dest = os.path.join(work, _norm(name))
        _fetch_commit(run, url, rev, dest)
        return _build_dir(run, os.path.join(dest, sub) if sub else dest, wheels, name)
    if how.get("url", "").startswith("git+"):
        url, rev, sub = _git_url(how["url"])
        return _build_private(run, name, {"git": url, "rev": rev, "subdirectory": sub}, src, work, wheels, log)
    if how.get("path") or how.get("workspace"):
        if how.get("workspace"):
            members = []
            for pattern in (_project(src).get("tool", {}).get("uv", {}).get("workspace", {}).get("members") or []):
                members += glob.glob(os.path.join(src, pattern))
            found = [m for m in members if _norm(_project(m).get("project", {}).get("name", "")) == _norm(name)]
            if not found:
                raise BuildError(f"{name} is a workspace member the repository does not hold")
            path = found[0]
        else:
            path = os.path.normpath(os.path.join(src, how["path"]))
            if not path.startswith(os.path.abspath(src) + os.sep):
                raise BuildError(f"{name}'s path leaves the repository")
        log(f"  building {name} from {os.path.relpath(path, src)} in the repository")
        if path.endswith(".whl"):
            shutil.copy(path, wheels)
            return os.path.basename(path)
        return _build_dir(run, path, wheels, name)
    url = how.get("url", "")
    if url.startswith(("https://", "http://")):
        log(f"  fetching {name} from {url}")
        try:
            got = _download(url, work)
        except (error.URLError, OSError) as e:
            raise BuildError(f"{name} could not be downloaded from {url}: {getattr(e, 'reason', e)}")
        if got.endswith(".whl"):
            shutil.move(got, os.path.join(wheels, os.path.basename(got)))
            return os.path.basename(got)
        return _build_dir(run, got, wheels, name)
    raise BuildError(f"{name} comes from {url or how}, which cannot be built here")


def _project(directory: str) -> dict:
    try:
        with open(os.path.join(directory, "pyproject.toml"), "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _build_repo(pin: dict, tmp: str, run: Callable, log: Callable) -> dict:
    src, work, wheels = os.path.join(tmp, "src"), os.path.join(tmp, "work"), os.path.join(tmp, WHEELS)
    os.makedirs(work); os.makedirs(wheels)
    log(f"  fetching {gitrefs.label_of(pin['url'])} at {pin['commit'][:12]}")
    _fetch_commit(run, pin["url"], pin["commit"], src)
    head = _check(run, [gitrefs.GIT, "-C", src, "rev-parse", "HEAD"], "git rev-parse").strip()
    if head != pin["commit"]:
        raise BuildError(f"{gitrefs.label_of(pin['url'])} checked out at {head[:12]}, not {pin['commit'][:12]}")
    project = _project(src)
    named = _norm(project.get("project", {}).get("name", ""))
    if named != PACKAGE:
        raise BuildError(f"{gitrefs.label_of(pin['url'])} at {pin['commit'][:12]} is the project {named or '(none)'}, not harbor")
    log("  building harbor")
    wheel = _build_dir(run, src, wheels, "harbor")
    meta = _wheel_metadata(os.path.join(wheels, wheel))
    sources = project.get("tool", {}).get("uv", {}).get("sources", {}) or {}
    sources = {_norm(k): (v[0] if isinstance(v, list) and v else v) for k, v in sources.items()}
    private, queue, seen = [], list(meta.get_all("Requires-Dist") or []), {PACKAGE}
    while queue:
        name, url = _requirement(queue.pop(0))
        if not name or name in seen:
            continue
        how = {"url": url} if url else sources.get(name) if isinstance(sources.get(name), dict) else None
        if not how or not any(how.get(k) for k in ("git", "path", "url", "workspace")):
            continue                                       # served by an index (PyPI, or one a source names): not built here
        seen.add(name)
        built = _build_private(run, name, how, src, work, wheels, log)
        private.append({"name": name, "wheel": built, "direct": bool(url), "from": url or {k: str(v) for k, v in how.items()}})
        queue += list(_wheel_metadata(os.path.join(wheels, built)).get_all("Requires-Dist") or [])   # what it names by URL, in turn
    for name in os.listdir(wheels):
        if not name.endswith(".whl"):
            os.remove(os.path.join(wheels, name))           # uv build's .gitignore
    if not all(_WHEEL.match(n) for n in os.listdir(wheels)):
        raise BuildError("a built wheel has a name this cannot pass on safely")
    return {"kind": "repo", "url": pin["url"], "repo": pin["repo"], "commit": pin["commit"], "version": meta.get("Version", ""),
            "requires_python": meta.get("Requires-Python") or "", "wheel": wheel, "private": private,
            "extras": sorted(x for x in (meta.get_all("Provides-Extra") or []) if _EXTRA.match(x)), "built_at": _now()}


_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.RLock] = {}


def _lock(key: str) -> threading.RLock:
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def record_of(pin: dict, pins: str) -> Optional[dict]:
    """The pin's record when it is ready here, else None."""
    try:
        with open(os.path.join(pins, key_of(pin), RECORD), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("kind") == pin["kind"] else None


def ensure(pin: dict, pins: str, run: Optional[Callable] = None, fetch: Optional[Callable] = None,
           log: Optional[Callable] = None) -> dict:
    """The pin ready on this machine; its record. A repository is built once
    (see the module docstring), a release is read from PyPI once; a ready pin
    is read back. Raises :class:`BuildError` with the reason."""
    run, log = run or run_command, log or (lambda line: None)
    key = key_of(pin)
    with _lock(key):
        rec = record_of(pin, pins)
        if rec is not None:
            return rec
        if pin["kind"] == "release":
            rec = _release_record(pin, fetch)
            if not rec.get("partial"):
                _write(os.path.join(pins, key), RECORD, rec)
            return rec
        os.makedirs(pins, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{key}-", dir=pins)
        try:
            rec = _build_repo(pin, tmp, run, log)
            for name in ("src", "work"):
                shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
            _write(tmp, RECORD, rec)
            dest = os.path.join(pins, key)
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(tmp, dest)
            return rec
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _write(directory: str, name: str, data: dict) -> None:
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, os.path.join(directory, name))



def spec(record: dict, extras=(), wheels_sh: str = "") -> str:
    """What ``uvx --from`` is given, as shell text: ``'harbor==0.24.0'``, or
    the built wheel under ``wheels_sh`` (a shell expression)."""
    ex = f"[{','.join(sorted(extras))}]" if extras else ""
    if record["kind"] == "release":
        return shlex.quote(f"{PACKAGE}{ex}=={record['version']}")
    return f'"{PACKAGE}{ex} @ {wheels_sh}/"{shlex.quote(record["wheel"])}'


FEED_SUFFIX = "-feed.py"     # the live feed's program, beside the harbor script it serves


def feed_source() -> str:
    """``nullius.harbor.feed`` as a file to send: what the harbor script runs."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.py"), encoding="utf-8") as fh:
        return fh.read()


def _harbor_call() -> str:
    """The pinned Harbor's command line, as uvx is given it: its Python running
    the feed beside this script, which registers the live feed and runs ``harbor``."""
    return f'python "$0{FEED_SUFFIX}" "$@"'


def wrapper(record: dict, pin_dir_sh: str, extras=()) -> str:
    """A ``harbor`` that runs the pinned Harbor: a small sh script for
    ``$NULLIUS_HARBOR``. ``pin_dir_sh`` is the pin's directory on the target
    as shell text (quoted, or ``"$HOME/…"``); a release needs none."""
    extras = sorted(x for x in extras if _EXTRA.match(x))
    py = f" --python {shlex.quote(record['requires_python'])}" if record.get("requires_python") else ""
    lines = ["#!/bin/sh", "# Written by Nullius: " + (f"harbor {record['version']} from PyPI" if record["kind"] == "release"
             else f"harbor {record.get('version', '')} built from {gitrefs.label_of(record['url'])} at {record['commit']}") + ", as the recipe pins it.",
             'command -v uvx >/dev/null 2>&1 || PATH="$HOME/.local/bin:$PATH"']
    if record["kind"] == "release":
        lines.append(f"exec uvx --isolated{py} --from {spec(record, extras)} {_harbor_call()}")
        return "\n".join(lines) + "\n"
    lines += [f"w={pin_dir_sh}/{WHEELS}",
              f'[ -f "$w"/{shlex.quote(record["wheel"])} ] || {{ echo "harbor: the wheels built for this pin are not in $w" >&2; exit 2; }}']
    direct = [p for p in record.get("private") or [] if p.get("direct")]
    call = f"uvx --isolated{py} --find-links \"$w\"" + (' --overrides "$o"' if direct else "") + f" --from {spec(record, extras, '$w')} {_harbor_call()}"
    if not direct:
        return "\n".join(lines + ["exec " + call]) + "\n"
    # a dependency named by URL is not satisfied by a find-links directory: an override names the wheel built for it
    lines += ['o=$(mktemp "${TMPDIR:-/tmp}/nullius-harbor.XXXXXX") || exit 1',
              "u=$(printf %s \"$w\" | sed 's/%/%25/g; s/ /%20/g')",
              ": >\"$o\""]
    lines += [f'printf \'%s\\n\' {shlex.quote(p["name"] + " @ file://")}"$u/"{shlex.quote(p["wheel"])} >>"$o"' for p in direct]
    lines += [call, 'rc=$?; rm -f "$o"; exit $rc']
    return "\n".join(lines) + "\n"


def write_wrapper(path: str, record: dict, pin_dir_sh: str, extras=()) -> str:
    with open(path + FEED_SUFFIX, "w", encoding="utf-8") as fh:
        fh.write(feed_source())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(wrapper(record, pin_dir_sh, extras))
    os.chmod(path, 0o755)
    return path



def scanned(pin: dict, pins: str) -> Optional[dict]:
    """The scan kept for this pin, or None."""
    try:
        with open(os.path.join(pins, key_of(pin), AGENTS), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("agents"), list) else None


def _answer(state: str, pin: dict, rec: Optional[dict] = None, agents=None, error: str = "", at: str = "") -> dict:
    rec = rec or {}
    return {"state": state, "key": key_of(pin), "label": label(pin), "agents": agents, "error": error, "at": at,
            "version": rec.get("version", pin.get("version", "")), "extras": rec.get("extras", [])}


def scan(pin: dict, pins: str, build: bool = True, run: Optional[Callable] = None, fetch: Optional[Callable] = None,
         log: Optional[Callable] = None) -> dict:
    """The agents the pinned Harbor registers - ``harbor agents list --json``
    through the same command a run uses - kept beside the pin. With ``build``
    False only what is kept is read (``state`` ``absent`` when nothing is);
    else the pin is made ready and asked once. A failure is said and not
    kept, so asking again tries again."""
    run = run or run_command
    rec, kept = record_of(pin, pins), scanned(pin, pins)
    if kept is not None:
        return _answer("ready", pin, rec or kept, kept["agents"], at=kept.get("at", ""))
    if not build:
        return _answer("absent", pin, rec)
    with _lock(key_of(pin)):
        kept = scanned(pin, pins)
        if kept is not None:
            return _answer("ready", pin, record_of(pin, pins) or kept, kept["agents"], at=kept.get("at", ""))
        try:
            rec = ensure(pin, pins, run=run, fetch=fetch, log=log)
        except BuildError as e:
            return _answer("failed", pin, None, error=str(e))
        d = os.path.join(pins, key_of(pin))
        os.makedirs(d, exist_ok=True)
        exe = write_wrapper(os.path.join(d, "harbor"), rec, shlex.quote(d))
        rc, out = run([exe, "agents", "list", "--json"], SCAN_TIMEOUT)
        names, why = _agents_of(rc, out)
        if names is None:
            return _answer("failed", pin, rec, error=why)
        at = _now()
        if not rec.get("partial"):
            _write(d, AGENTS, {"agents": names, "at": at, "version": rec.get("version", ""), "extras": rec.get("extras", [])})
        return _answer("ready", pin, rec, names, at=at)


def _agents_of(rc: int, out: str) -> tuple:
    """(names, None) from what ``harbor agents list --json`` printed, or
    (None, why). Only a list of agent names is a list."""
    if rc != 0:
        said = "\n".join(l for l in (out or "").splitlines() if not _UV_SAYS.match(l))     # uv's narration is not the reason
        return None, f"harbor agents list failed: {_tail(said, 4) or f'exit {rc}'}"
    text, names = out or "", None
    # uv says what it installed on the same stream: the list is the first line that starts one
    for m in re.finditer(r"(?m)^\s*\[", text):
        try:
            names = json.JSONDecoder().raw_decode(text[m.start():].lstrip())[0]
            break
        except ValueError:
            continue
    if not isinstance(names, list) or not all(isinstance(n, str) and _AGENT.match(n) for n in names):
        return None, "harbor agents list printed something other than a list of agent names"
    return sorted(set(names)), None
