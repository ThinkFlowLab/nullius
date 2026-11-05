"""Where what a run needs is installed, and installing it once.

Everything goes under ``STANDARD_DIR`` (``standard/`` in the persistent user data directory):

- ``agents/<key>/<digest>/`` and ``capabilities/<key>/<digest>/``: the packages a
  definition's ``install`` names (``npm`` into ``node_modules``, ``pip`` and
  ``wheels`` - a wheel by its URL, checked by its sha256 - into a ``uv`` environment), the digest taken over that list so a re-pin installs
  beside the old one rather than over it;
- ``drivers/<key>/<digest>/``: a benchmark's ``driver.install`` the same way -
  the environment its upstream's own code needs, which its driver imports
  (``driver_argv``), never this application's and never an agent's. Its
  ``project`` is installed from the benchmark's upstream clone (below), the
  same commit, rather than fetched a second time;
- ``upstreams/<key>/<commit>/``: a benchmark's upstream repository at its
  tested commit;
- ``datasets/<sha256>/<file>``: a dataset served outside its upstream, under
  ``STANDARD_DATA_DIR`` instead when that is set - datasets can be gigabytes
  where installs are not, so they may sit on a bigger disk than the installs.

A folder is complete when its ``.installed`` marker is; a failed install
leaves no marker and is tried again from scratch. Two runs installing the same
thing at once take turns (a lock file beside the folder): the second finds the
first one's marker rather than removing its half-made folder. A dataset file is checked
against its sha256 before a run reads it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional

from nullius.standard import definitions

from nullius import paths

HOME = os.environ.get("STANDARD_DIR") or os.path.join(paths.data_dir(), "standard")
MARK = ".installed"


def data_dir(home: str = HOME, env=os.environ) -> str:
    """Where downloaded datasets are kept: ``STANDARD_DATA_DIR``, else
    ``datasets/`` in the standard's home. A dataset already downloaded
    elsewhere is not moved: setting the variable downloads it once more."""
    return env.get("STANDARD_DATA_DIR") or os.path.join(home, "datasets")


class InstallFailed(Exception):
    pass


# A variable an install step is never given: a package's install script runs
# as published, and a run's process holds the model key (under --from-run as
# OPENAI_API_KEY, API_TOKEN, LLM_API_KEY) - the relay is the only holder meant.
SECRET = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)$", re.I)      # the last word: GIT_CONFIG_KEY_0 is git's own


def install_env(env=None) -> dict:
    """This process's environment less every variable named like a secret."""
    return {k: v for k, v in (os.environ if env is None else env).items() if not SECRET.search(k)}


def git_env() -> dict:
    """What a fetch of an upstream runs with: the installer's environment and
    no prompt. A private repository - a fork of the user's - is fetched with
    this machine's own git credentials (its credential helper, its ssh agent,
    in its HOME); without them the fetch fails, said, rather than waiting on a
    terminal for a password."""
    return dict(install_env(), GIT_TERMINAL_PROMPT="0")


def _run(cmd: List[str], cwd: str, log: str, env: Optional[dict] = None):
    with open(log, "a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        try:
            r = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=install_env() if env is None else env)
        except FileNotFoundError:
            raise InstallFailed(f"{cmd[0]} is not on this machine")
    if r.returncode != 0:
        with open(log, errors="replace") as f:
            tail = f.read().strip().splitlines()[-3:]
        raise InstallFailed(f"{' '.join(cmd[:3])}...: exit {r.returncode}: {' | '.join(tail)[-300:]}")


@contextlib.contextmanager
def _locked(where: str):
    os.makedirs(os.path.dirname(where), exist_ok=True)
    with open(where + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


def packages(d: dict, home: str = HOME, npm_in: Optional[Callable[[List[str], str], List[str]]] = None,
             src: Optional[str] = None) -> str:
    """The folder an agent's or a capability's packages are installed in,
    installing them first when they are not. A definition installing nothing
    gets an empty folder all the same, so ``{install}`` always names one.

    ``npm_in``: how to run ``npm`` where this machine has none - the docker
    backend's base image, which carries Node (``container.install_argv``):
    given the command and the folder, the argument list that runs it there.
    A host is not asked for Node to run an agent it will only start in a
    container. Without it, a machine with no ``npm`` is refused, said.

    ``src``: a checkout of the definition's upstream at ``upstream.tested``
    already on this machine, which ``project`` is installed from instead of
    a clone of its own (a benchmark's driver: its upstream clone)."""
    inst = d.get("install") or {}
    up = (d.get("upstream") or {}) if inst.get("project") else {}
    where = packages_dir(d, home)
    if os.path.exists(os.path.join(where, MARK)):
        return where
    with _locked(where):
        return _packages(inst, where, up, npm_in, src)


def packages_dir(d: dict, home: str = HOME) -> str:
    """Where ``packages`` installs a definition's packages, installed or not."""
    inst = d.get("install") or {}
    kind = {"agent": "agents", "driver": "drivers"}.get(d.get("_kind"), "capabilities")
    up = (d.get("upstream") or {}) if inst.get("project") else {}
    return os.path.join(home, kind, d["key"], _digest(dict(inst, _at=up.get("tested")) if up else inst))


def ready(where: Optional[str]) -> bool:
    """Whether an install folder is complete (its marker is there): a run says
    the stage was there already rather than that it took no time."""
    return bool(where) and os.path.exists(os.path.join(where, MARK))


HF = "hf"         # inside an install: the models prefetched from the hub (HF_HUB_CACHE at run time)
WHEELS = "wheels"  # inside an install: the wheels it names by URL, as downloaded
SRC = "src"       # inside an install: the definition's upstream, at its tested commit, for install.project


def _packages(inst: dict, where: str, up: Optional[dict] = None, npm_in: Optional[Callable] = None,
              src: Optional[str] = None) -> str:
    if os.path.exists(os.path.join(where, MARK)):
        return where                                  # another run finished it while this one waited
    if os.path.isdir(where):
        shutil.rmtree(where)
    os.makedirs(where)
    log = os.path.join(where, "install.log")
    if inst.get("npm"):
        cmd = ["npm", "install", "--no-audit", "--no-fund", "--prefix", where] + list(inst["npm"])
        if shutil.which("npm"):
            _run(cmd, where, log)
        elif npm_in is None:
            raise InstallFailed("npm is not on this machine: install Node.js 22 here, or run on the docker backend, "
                                "which installs with the Node in its base image")
        else:
            _run(npm_in(cmd, where), where, log)
            for scratch in (".npm-home", ".npm-cache"):       # what npm kept of its own, in the container
                shutil.rmtree(os.path.join(where, scratch), ignore_errors=True)
    if inst.get("pip") or inst.get("project") or inst.get("indexes") or inst.get("wheels"):
        py = os.path.join(where, ".venv", "bin", "python")
        # uv's own interpreter, never the system's: an own image mounts the one the
        # environment links to (container.python_homes), and /usr/bin is the image's
        _run(["uv", "venv", "--quiet", "--python-preference", "only-managed", os.path.join(where, ".venv")]
             + (["--python", inst["python"]] if inst.get("python") else []), where, log)
        for x in inst.get("indexes") or []:          # first: the project's resolution then keeps them
            _run(["uv", "pip", "install", "--quiet", "--python", py, "--index-url", x["url"]] + list(x["packages"]), where, log)
        wheels = []                                  # each checked by its sha256 before it is installed
        for x in inst.get("wheels") or []:
            os.makedirs(os.path.join(where, WHEELS), exist_ok=True)
            wheels.append(os.path.join(where, WHEELS, x["url"].rsplit("/", 1)[1]))
            _fetch(x["url"], x["sha256"], wheels[-1], "wheel")
        locked = bool(inst.get("project") and inst.get("locked"))
        if (inst.get("pip") or wheels) and not locked:
            # resolved together: a wheel's dependencies are pip's pins
            _run(["uv", "pip", "install", "--quiet", "--python", py] + list(inst.get("pip") or []) + wheels, where, log)
        if inst.get("project"):
            if src is None:
                src = os.path.join(where, SRC)
                os.makedirs(src)
                for cmd in (["git", "init", "--quiet"], ["git", "fetch", "--quiet", "--depth", "1", up["repository"], up["tested"]],
                            ["git", "checkout", "--quiet", "FETCH_HEAD"]):
                    _run(cmd, src, log, env=git_env())
            proj = os.path.normpath(os.path.join(src, inst["project"]))
            extras = "[" + ",".join(inst["extras"]) + "]" if inst.get("extras") else ""
            if locked:
                # the project's own lock file, its pins as published, and pip's packages
                # resolved beside them in one go: a conflict fails the install, said
                lock = os.path.join(where, "requirements.lock")
                _run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--quiet",
                      "--project", proj, "-o", lock] + [w for x in inst.get("extras") or [] for w in ("--extra", x)], where, log)
                _run(["uv", "pip", "install", "--quiet", "--python", py, "-r", lock] + list(inst.get("pip") or []) + wheels, where, log)
                _run(["uv", "pip", "install", "--quiet", "--python", py, "--no-deps", "-e", proj + extras], where, log)
            else:
                _run(["uv", "pip", "install", "--quiet", "--python", py, "-e", proj + extras], where, log)
        if inst.get("prefetch"):
            env = dict(install_env(), HF_HUB_CACHE=os.path.join(where, HF))
            _run([py, "-c", "import sys\nfrom huggingface_hub import snapshot_download\nfor m in sys.argv[1:]: snapshot_download(m)"]
                 + list(inst["prefetch"]), where, log, env=env)
    with open(os.path.join(where, MARK), "w") as f:
        json.dump(inst, f)
    return where


def process_env(d: dict, where: str, home: str, options: Optional[dict] = None) -> dict:
    """What an install adds to its process's environment: the definition's own
    ``env`` rendered (``{opt_<name>}`` from ``options``, the run's values), and
    ``HF_HUB_CACHE`` when it prefetched models."""
    slots = {"install": where, "home": home, "here": d.get("_here", ""),
             **{"opt_" + k: v for k, v in (options or {}).items()}}
    out = {}
    if (d.get("install") or {}).get("prefetch"):
        out["HF_HUB_CACHE"] = os.path.join(where, HF)
    for k, v in (d.get("env") or {}).items():
        for s, val in slots.items():
            v = v.replace("{" + s + "}", val)
        out[k] = v
    return out


def prune(d: dict, home: str) -> List[str]:
    """Remove what the definition declares as ``caches`` from a unit's home,
    once the unit has ended and its trial is written: the paths it removed.
    A path is resolved under ``home`` and refused when it would leave it
    (a symlink the agent made included); the definition's check already
    holds them to ``{home}/...``."""
    root, gone = os.path.realpath(home), []
    for c in d.get("caches") or []:
        if not isinstance(c, str) or not c.startswith("{home}/"):
            continue
        path = os.path.realpath(os.path.join(home, c[len("{home}/"):]))
        if not path.startswith(root + os.sep) or not os.path.lexists(path):
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.remove(path)
        gone.append(path)
    return gone


def bin_dirs(where: str) -> List[str]:
    """The folders of an install whose programs a start command may name."""
    return [p for p in (os.path.join(where, "node_modules", ".bin"), os.path.join(where, ".venv", "bin")) if os.path.isdir(p)]


def driver_packages(bench: dict, home: str = HOME) -> Optional[str]:
    """The folder a benchmark's ``driver.install`` is installed in, installing
    it first when it is not (once per digest, as an agent's packages); None
    for a driver that declares none and runs on what the machine has. Its
    ``project`` comes from the benchmark's upstream clone, cloned first when
    it is not there: the same commit, fetched once."""
    d = definitions.driver_install(bench)
    if not d["install"]:
        return None
    src = upstream(bench, home) if d["install"].get("project") else None
    return packages(d, home, src=src)


def driver_dir(bench: dict, home: str = HOME) -> Optional[str]:
    """Where ``driver_packages`` installs, installed or not; None when it declares nothing."""
    d = definitions.driver_install(bench)
    return packages_dir(d, home) if d["install"] else None


def driver_argv(bench: dict, home: str = HOME) -> tuple:
    """How a benchmark's driver is started: its ``driver.command`` with
    ``{here}`` (the definition's folder) and ``{install}`` (its
    ``driver.install`` folder, installed first when it is not) filled, and
    what that install adds to the driver's environment (``HF_HUB_CACHE``
    when it prefetched models). Every place that starts a driver - a run, a
    shard, a proof's checks - starts it here."""
    where = driver_packages(bench, home)
    extra = process_env(definitions.driver_install(bench), where, home) if where else {}
    return words(bench["driver"]["command"], {"here": bench["_here"], "install": where or ""}), extra


def upstream(bench: dict, home: str = HOME) -> Optional[str]:
    """A benchmark's upstream at its tested commit, cloned once; None when the
    definition has no upstream repository (its data sits beside it)."""
    u = bench.get("upstream") or {}
    where = upstream_dir(bench, home)
    if where is None:
        return None
    if os.path.exists(os.path.join(where, MARK)):
        return where
    with _locked(where):
        return _upstream(u, where)


def upstream_dir(bench: dict, home: str = HOME) -> Optional[str]:
    """Where ``upstream`` clones a benchmark's upstream; None when it has none."""
    u = bench.get("upstream") or {}
    return os.path.join(home, "upstreams", bench["key"], u["tested"]) if u.get("repository") else None


def _upstream(u: dict, where: str) -> str:
    if os.path.exists(os.path.join(where, MARK)):
        return where
    if os.path.isdir(where):
        shutil.rmtree(where)
    os.makedirs(where)
    log = os.path.join(os.path.dirname(where), u["tested"] + ".log")
    _run(["git", "init", "--quiet"], where, log)
    _run(["git", "fetch", "--quiet", "--depth", "1", u["repository"], u["tested"]], where, log, env=git_env())
    _run(["git", "checkout", "--quiet", "FETCH_HEAD"], where, log)
    open(os.path.join(where, MARK), "w").close()
    return where


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset(bench: dict, ds: dict, up: Optional[str], progress: Optional[Callable] = None) -> Optional[str]:
    """The dataset's file, checked by its sha256: in the upstream when there
    is one, else beside the definition. None for a dataset with no file (its
    driver makes its units). ``progress``: told a download's bytes as they
    come (``download``)."""
    if ds.get("url"):
        return download(ds["url"], ds["sha256"], progress=progress)
    if not ds.get("path"):
        return None
    path = os.path.join(up or bench["_here"], ds["path"])
    if not os.path.isfile(path):
        raise InstallFailed(f"dataset {ds['id']}: {ds['path']} is not in {'the upstream' if up else 'the definition folder'}")
    got = sha256_of(path)
    if got != ds["sha256"]:
        raise InstallFailed(f"dataset {ds['id']}: sha256 {got[:12]}..., the definition says {ds['sha256'][:12]}...")
    return path


def download_path(url: str, sha256: str, home: str = HOME) -> str:
    """Where ``download`` keeps a dataset, downloaded or not."""
    return os.path.join(data_dir(home), sha256, os.path.basename(url.split("?", 1)[0]) or "data")


def download(url: str, sha256: str, home: str = HOME, progress: Optional[Callable] = None) -> str:
    """A dataset served outside its upstream, downloaded once under its
    digest (``data_dir``), and checked by it. ``progress(done, total)`` is
    told the bytes so far after each chunk (``total`` from the server's
    length, None when it gives none), and once more with ``final=True``."""
    where = download_path(url, sha256, home)
    if os.path.isfile(where):
        return where
    with _locked(where):
        if not os.path.isfile(where):
            _fetch(url, sha256, where, "dataset", progress)
    return where


def _fetch(url: str, sha256: str, where: str, what: str, progress: Optional[Callable] = None) -> None:
    """``url`` downloaded to ``where``, kept only when it is the file ``sha256`` names."""
    import urllib.request
    part = where + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nullius"}), timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) or None
            done = 0
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            if progress:
                progress(done, total, final=True)
    except OSError as e:
        raise InstallFailed(f"{what} {url}: {e}")
    got = sha256_of(part)
    if got != sha256:
        os.remove(part)
        raise InstallFailed(f"{what} {url}: sha256 {got[:12]}..., the definition says {sha256[:12]}...")
    os.replace(part, where)


def words(cmd: List[str], slots: Dict[str, str]) -> List[str]:
    """A definition's command with its slots filled: each slot is replaced
    inside its word, and the word stays one argument - no shell reads it."""
    out = []
    for w in cmd:
        for k, v in slots.items():
            w = w.replace("{" + k + "}", v)
        out.append(w)
    return out
