"""The three definitions, where they live, and what each may say.

- a **benchmark**: ``catalogue/benchmarks/<key>/standard.json``
  (``nullius.benchmark/v1``) beside the catalogue's own ``benchmark.json``;
- an **agent**: ``catalogue/agents/<key>/agent.json`` (``nullius.agent/v1``);
- a **capability**: ``catalogue/capabilities/<key>/capability.json`` (``nullius.capability/v1``).

An agent or a capability may hold several **versions** in its one file:
``versions`` maps each version to what differs in it - its ``upstream``
(package, licence), ``install``, and optionally ``start``, ``model``, ``env``,
``options`` (added to the shared ones), ``fidelity`` - and its own ``proven``; ``default`` names the one offered when none is named.
What every version shares stays at the top. A version is named
``<key>@<version>`` (:func:`split`), and :func:`resolve` turns the file into
one version's definition, as flat as a file without ``versions``: everything
downstream - the matcher, a run, a kit - sees one version at a time, and a
version offers exactly the ports it has proven. A file without ``versions``
is one version, its ``upstream.version``.

The conformance kits are benchmarks too, kept inside this package
(``standard/kits/<name>/standard.json``), so a kit is run the way any
benchmark is.

A benchmark may name its **controls** (``controls``): ``nop``, the null run
that does nothing and answers "", and ``oracle``, the reference run whose
answer (and, for a workspace, files) the driver's ``oracle`` op gives. Each
says who wrote it - ``source``, one of ``SOURCES`` - and where it comes from
(``from``, a sentence), and may hold it to a bound (``expect``). A control
is run as the other side of a pair (:func:`control`), with no agent, no
capability and no model of its own; ``nop`` and ``oracle`` are the words Harbor
calls its controls by, so the runs pages read the jobs as controls. A
benchmark's ``proven`` is what ``cli prove`` found, per dataset
(``standard/prove.py``).

A benchmark's **setups** are driver methods behind one Original procedure.
``original`` uses the upstream's model selection; ``model`` substitutes the
chosen model while preserving the ``runs_as`` window and prompt. The driver's
``answer`` op uses the unit's relay and may declare conversation-sized ``units``.
The builder chooses participant and model separately; the resolved setup's
``comparable`` flag records whether the run can compare with the paper.

A benchmark may hold **versions** the same way (``key@version``): each
says what differs in it - ``upstream``, ``datasets``, ``driver``, ``score``,
``limits``, ``controls``, ``environments`` - and its own ``proven``
(``BENCH_VERSIONED``). A file without ``versions`` is one version, as an
agent's is; :func:`load` and :func:`get` resolve a benchmark as they resolve
an agent.

A benchmark may give its units **environments** (``environments``): named
Dockerfiles in its folder, each a layer on the standard's base image (the
Dockerfile has no ``FROM``: Nullius writes it), with the files it copies
listed (``files``), the licences it needs (``needs_secrets``: each a
``value`` - a key, given as a variable -, a ``file`` - mounted read-only at
its ``path`` -, or a ``server`` - the one ``host:port`` the agent may reach -,
kept per benchmark by ``nullius.secrets``, and where it goes - ``agent`` or
``driver``) and whether the local backend may run it (``local_ok``). A unit's setup names its
environment, or its dataset gives a default; none is the base image
(:func:`environment_of`). ``container.image_for`` builds and picks the image.
An environment may instead be an **own image** (``"image": "own"``): no
Dockerfile here, each unit's setup naming the task's own image - a folder
of the upstream to build, its ``FROM`` its own, or a reference by digest -
to which the agent is added at run time (``container.Box``).

A definition is a bundled file or a file in the user's catalogue, never a form field, and
:func:`problems` checks it before anything offers it: command words from a
safe alphabet, ports that exist, an https upstream at a full commit, every
dataset with its sha256. The placeholders a command may hold are named here
(``SLOTS``) and filled by the runner, quoted, never by a shell.

An agent or a capability may have **options** (``options``): what a run may set
of its own behaviour, each with its allowed values and a default that is the
vendor's own, filled into its templates as ``{opt_<name>}`` (:func:`options_of`).
An agent may name its own non-interactive command (``fidelity.command``), which
``fidelity`` drives beside ACP to show both send the model the same requests.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from nullius.standard import ports
from nullius import paths
from nullius.catalogue import storage
from . import names

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE = os.path.join(os.path.dirname(HERE), "catalogue")
KITS = os.path.join(HERE, "kits")

KINDS = {
    "benchmark": {"schema": "nullius.benchmark/v1", "dir": "benchmarks", "file": "standard.json"},
    "agent": {"schema": "nullius.agent/v1", "dir": "agents", "file": "agent.json"},
    "capability": {"schema": "nullius.capability/v1", "dir": "capabilities", "file": "capability.json"},
}

# What a command word may be: a literal from a safe alphabet, or one slot.
# {here} is the definition's folder, {install} the folder its packages were
# installed into, {prompt} the unit's prompt (a command-template agent only).
SLOTS = {"here", "install", "prompt"}
_WORD = re.compile(r"^(?:[A-Za-z0-9_./:=@+-]|\{[a-z]+\})+$")
_SLOT = re.compile(r"\{([a-z]+)\}")
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HTTPS = re.compile(r"^https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_.%@+-]+)+$")
_RELATIVE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$")
_NPM = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@[0-9][A-Za-z0-9.+-]*$")
_PIP = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+$")
_PIP_GIT = re.compile(r"[A-Za-z0-9_.-]+ @ git\+https://[A-Za-z0-9.-]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+@[0-9a-fA-F]{40}")
FAMILIES = ("agent", "capability")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
APIS = ("chat", "responses", "messages")
SCORES = ("pass", "score")
# A benchmark's controls, by the words Harbor calls them (``runs.parse.CONTROL_AGENTS``),
# and who wrote each: the benchmark's authors, a harness adapter's author, or Nullius.
CONTROLS = ("nop", "oracle")
SOURCES = ("official", "adapter", "nullius")
CONTROL_LABEL = {"nop": "Null control", "oracle": "Reference control"}
# A benchmark's setups (``setups``): the published benchmark as its authors ran it, and the same pipeline with another model.
SETUPS = ("original", "model")


def setup_label(bench, which):
    """The catalogue has one upstream procedure; model choice is independent."""
    return "Original"


# The bound a control is held to unless it says otherwise: the null run reaches nothing, the reference everything.
EXPECT = {"nop": ("at_most", 0.0), "oracle": ("at_least", 1.0)}
# How an agent is told where its model is - the relay - in its definition's
# own terms: variables of its environment, and files written into the home
# it is given for the run, each a template with these slots.
MODEL_SLOTS = {"base_url", "key", "model", "home"}
_SEG = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")          # a segment of a path in the agent's own home: a dot folder is fine
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_TEMPLATE = re.compile(r"^(?:[A-Za-z0-9_./:=@+ -]|\{[a-z_]+\})*$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
# What a version may say for itself; the rest of the file is shared.
VERSIONED = ("upstream", "install", "start", "model", "env", "options", "caches", "fidelity", "proven")
# What a benchmark's version may say for itself: its data, its driver, how it scores, who plays its user, its environments.
BENCH_VERSIONED = ("upstream", "datasets", "driver", "mcp_servers", "score", "simulator", "limits", "controls", "environments",
                   "options", "host", "setups", "proven", "leaderboard")
# What a benchmark's option value may need (``options.<name>.needs``): an embedding model its driver calls
# through the run's relay, and requirements of the machine the driver runs on (``host``).
NEED_KEYS = ("embeddings", "host")
_PROGRAM = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,39}$")
_PACKAGE = re.compile(r"^[@a-z0-9][a-z0-9@/._+-]{0,99}$")
HOST_INSTALLERS = ("apt", "npm")
# A benchmark's environment (``environments``): its name, what it may say, and where a secret it needs may go -
# into the agent's container (``agent``) or the driver's process, which scores (``driver``).
_ENV_KEY = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
ENV_FIELDS = ("summary", "dockerfile", "files", "needs_secrets", "local_ok", "image", "network")
OWN_IMAGE = "own"                    # an environment whose units each bring their own image (``image``)
SECRET_TO = ("agent", "driver")
# A licence's shape (``kind``): a key the process gets as a variable, a file mounted read-only where the
# software looks for it, or a licence server - the one address the agent's container may reach.
SECRET_KINDS = ("value", "file", "server")
SECRET_FIELDS = ("name", "to", "summary", "kind", "path", "env", "provider")
_ABS = re.compile(r"^(?:/[A-Za-z0-9_@+-][A-Za-z0-9_.@+-]*)+$")
_NOT_UNDER = ("/proc", "/sys", "/dev")       # where a licence file is never mounted: the kernel's own
BASE_ENV = "base"                    # the name no environment may take: the base image itself
# An option of an agent or a capability (``options``): a name a template may
# carry as ``{opt_<name>}``, its values a plain word each - they are written
# into variables and config files, so never a quote, a space or a newline.
_OPTION = re.compile(r"^[a-z][a-z_]{0,39}$")
_OPTION_VALUE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,64}$")
_OPT_SLOT = re.compile(r"\{opt_([a-z_]+)\}")


def split(ref: str) -> Tuple[str, Optional[str]]:
    """``codex@2.2.2`` -> (``codex``, ``2.2.2``); a bare key -> (key, None)."""
    key, _, version = str(ref or "").partition("@")
    return key, version or None


def versions(d: dict) -> List[str]:
    """Its versions, in the file's order (a file without ``versions``: its one)."""
    vs = d.get("versions")
    if isinstance(vs, dict) and vs:
        return list(vs)
    return [str((d.get("upstream") or {}).get("version") or "")]


def resolve(d: dict, version: Optional[str] = None) -> Optional[dict]:
    """One version of ``d`` as a flat definition: the shared fields, then the
    version's own (its ``upstream`` over the shared one, ``upstream.version``
    set), ``proven`` its own and ``offers`` the ports it proved. It carries
    ``_version``, ``_versions``, ``_default`` and ``_ref`` (what names it).
    None for a version the file does not have."""
    vs = d.get("versions")
    if not isinstance(vs, dict) or not vs:
        v = versions(d)[0]
        if version not in (None, v):
            return None
        return dict(d, _version=v, _versions=[v], _default=v, _ref=d.get("key"))
    v = version or d.get("default")
    if v not in vs or not isinstance(vs[v], dict):
        return None
    block = vs[v]
    bench = d.get("schema") == KINDS["benchmark"]["schema"]
    out = {k: x for k, x in d.items() if k not in ("versions", "default")}
    for k in (BENCH_VERSIONED if bench else VERSIONED):
        if k in block:
            out[k] = block[k]
    if isinstance(d.get("options"), dict) and isinstance(block.get("options"), dict):
        out["options"] = dict(d["options"], **block["options"])       # a version adds an option or restates one
    up = dict(d.get("upstream") or {})
    own = block.get("upstream") or {}
    if ("repository" in own or "tested" in own) and "tag" not in own:
        up.pop("tag", None)                                           # another repository or commit: the shared tag is not its
    out["upstream"] = dict(up, **own, version=v)
    out["proven"] = dict(block.get("proven") or {})
    if not bench:                                                     # a benchmark offers nothing: it needs
        out["offers"] = [p for p in ports.PORTS if p in out["proven"]]
    return dict(out, _version=v, _versions=list(vs), _default=d.get("default"), _ref=f"{d.get('key')}@{v}")


def title(d: dict) -> str:
    """Its label, with the version when the file has ``versions`` (``Codex 2.2.2``)."""
    return f"{d['label']} {d['_version']}" if "@" in str(d.get("_ref") or "") else d["label"]


def ref_of(d: dict) -> str:
    """What names a resolved definition where a bare key means its default
    version: its key for the default, ``key@version`` for any other."""
    return d["key"] if d.get("_version") == d.get("_default") else (d.get("_ref") or d["key"])


def version_of(d: dict) -> Optional[str]:
    """The version a resolved definition is when its file has ``versions``; else None."""
    return d.get("_version") if "@" in str(d.get("_ref") or "") else None


def find(others: List[dict], ref: str) -> Optional[dict]:
    """Among resolved definitions, the one ``ref`` names: ``key@version``, or a
    bare key for its default version."""
    return next((o for o in others if o.get("_ref") == ref), None) or \
        next((o for o in others if o["key"] == ref and o.get("_version") == o.get("_default")), None)


def job_word(d: dict) -> str:
    """The resolved definition as a word of a job's name: its key, and its
    version when the file has ``versions`` (``codex-2.2.2``)."""
    return f"{d['key']}-{d['_version']}" if "@" in str(d.get("_ref") or "") else d["key"]


def _in_home(path) -> bool:
    """``{home}/a/b``: a path inside the home a run gives, never the home itself nor a way out of it."""
    rel = path[len("{home}/"):] if isinstance(path, str) and path.startswith("{home}/") else None
    segs = rel.split("/") if rel else []
    return bool(segs) and all(_SEG.match(x) and x not in (".", "..") for x in segs)


def _caches(d: dict) -> List[str]:
    """``caches``: what the agent or capability fills its home with and no one
    reads after the unit (a package cache, a synced repository) - removed
    when the unit ends, so a job keeps its evidence and not a copy of the
    same download per unit. Paths under ``{home}/``, nowhere else."""
    c = d.get("caches")
    if c is None:
        return []
    if not isinstance(c, list) or not all(_in_home(x) for x in c):
        return ["caches: a list of paths under {home}/ (the agent's own home, never outside it)"]
    return []


def _model(m: dict, here: Optional[str]) -> List[str]:
    bad = []
    if not m.get("apis") or any(a not in APIS for a in m.get("apis") or []):
        bad.append(f"model.apis: some of {APIS}")
    env, files = m.get("env") or {}, m.get("files") or {}
    if not env and not files:
        bad.append("model: env or files, so the agent can be pointed at the relay")
    for k, v in env.items():
        if not _ENV_NAME.match(k) or not isinstance(v, str) or not _TEMPLATE.match(v):
            bad.append(f"model.env.{k}: a variable name and a plain template")
        bad += [f"model.env.{k}: {{{s}}} is not a slot" for s in _SLOT.findall(str(v)) if s not in MODEL_SLOTS]
    for dest, src in files.items():
        if not _in_home(dest):
            bad.append(f"model.files: {dest!r} is a path under {{home}}/")
        if not isinstance(src, str) or not _RELATIVE.match(src):
            bad.append(f"model.files.{dest}: a template file beside the definition")
        elif here and not os.path.isfile(os.path.join(here, src)):
            bad.append(f"model.files.{dest}: {src} is not beside the definition")
    side = m.get("side_models")
    if side is not None and (not isinstance(side, list) or len(set(map(str, side))) != len(side)
                             or not all(isinstance(x, str) and _MODEL_NAME.match(x) for x in side)):
        bad.append("model.side_models: a list of model names, each once")
    return bad


_PYTHON = re.compile(r"^3\.\d{1,2}$")
_EXTRA = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PKG = re.compile(r"^[A-Za-z0-9_.-]{1,80}(?:==[A-Za-z0-9_.+!-]{1,40})?$")      # a name, or a name pinned
_HF = re.compile(r"^[A-Za-z0-9_.-]{1,96}/[A-Za-z0-9_.-]{1,96}$")
# The slots a definition's own variables (``env``) may hold.
ENV_SLOTS = {"install", "home", "here"}


def _install(d: dict, where: str = "install") -> List[str]:
    """``install``: packages pinned (``npm``, ``pip``), or the definition's own
    upstream as a Python project (``project``, a folder of the repository at
    ``upstream.tested``, with ``extras``), on a ``python`` of its own;
    ``wheels`` for a wheel served by URL only (a model package), each pinned by
    its sha256 and resolved with ``pip``; ``indexes`` for packages served elsewhere (installed first, so the
    project's resolution keeps them; each a name, or a name pinned), and ``prefetch``, models fetched from
    the Hugging Face hub at install time so a run needs no network for them;
    ``locked``, the project's own lock file (``uv.lock``) installed as its
    pins, ``pip`` resolved beside them, rather than its loose requirements.
    A benchmark's driver has the same block, ``driver.install`` (:func:`driver_install`):
    ``where`` is the name its problems are said under."""
    inst = d.get("install") or {}
    if not isinstance(inst, dict):
        return [f"{where}: an object"]
    return [where + p[len("install"):] for p in _install_block(d, inst)]


def driver_install(bench: dict) -> dict:
    """A benchmark's driver as a definition ``install.packages`` installs:
    its ``driver.install`` - the environment the upstream's own code needs,
    which the driver imports - beside the benchmark's ``upstream`` (for
    ``project``), kept under ``drivers/<key>/<digest>/``. Its ``install`` is
    empty when the driver declares none."""
    return {"key": bench.get("key", ""), "_kind": "driver", "_here": bench.get("_here", ""),
            "install": (bench.get("driver") or {}).get("install") or {}, "upstream": bench.get("upstream") or {}}


def _install_block(d: dict, inst: dict) -> List[str]:
    bad = []
    for p in inst.get("npm") or []:
        if not _NPM.match(str(p)):
            bad.append(f"install.npm: {p!r} is not name@version")
    for p in inst.get("pip") or []:
        if not (_PIP.fullmatch(str(p)) or _PIP_GIT.fullmatch(str(p))):
            bad.append(f"install.pip: {p!r} is not name==version or name @ git+https://repository@full-commit")
    if "python" in inst and not _PYTHON.match(str(inst["python"])):
        bad.append("install.python: a version such as 3.12")
    if "project" in inst:
        if not _RELATIVE.match(str(inst["project"])) and inst["project"] != ".":
            bad.append("install.project: a folder of the upstream repository, relative")
        if not (d.get("upstream") or {}).get("repository"):
            bad.append("install.project: needs upstream.repository and upstream.tested, the code it installs")
    if any(not _EXTRA.match(str(x)) for x in inst.get("extras") or []):
        bad.append("install.extras: the project's extras, plain names")
    if inst.get("extras") and "project" not in inst:
        bad.append("install.extras: extras of install.project, which is not set")
    if "locked" in inst and not isinstance(inst["locked"], bool):
        bad.append("install.locked: true or false")
    elif inst.get("locked") and "project" not in inst:
        bad.append("install.locked: the lock file of install.project, which is not set")
    for i, x in enumerate(inst.get("indexes") or []):
        if not isinstance(x, dict) or not _HTTPS.match(str(x.get("url", ""))) or not x.get("packages") \
                or any(not _PKG.match(str(p)) for p in x.get("packages") or []):
            bad.append(f"install.indexes[{i}]: {{url: https, packages: [names]}}")
    for i, x in enumerate(inst.get("wheels") or []):
        if not isinstance(x, dict) or not _HTTPS.match(str(x.get("url", ""))) or not str(x.get("url")).endswith(".whl") \
                or not _SHA256.match(str(x.get("sha256", ""))):
            bad.append(f"install.wheels[{i}]: {{url: https, a .whl, sha256: the file's}}")
    for m in inst.get("prefetch") or []:
        if not _HF.match(str(m)):
            bad.append(f"install.prefetch: {m!r} is not a hub model id (owner/name)")
    if inst.get("prefetch") and not (inst.get("pip") or inst.get("project") or inst.get("indexes") or inst.get("wheels")):
        bad.append("install: prefetch goes with pip, project or indexes, the environment it is installed in")
    return bad


def _env(env) -> List[str]:
    """``env``: the variables the process is started with, beside what Nullius
    gives it - templates of ``{install}``, ``{home}``, ``{here}``."""
    if env is None:
        return []
    if not isinstance(env, dict):
        return ["env: an object of variables"]
    bad = []
    for k, v in env.items():
        if not _ENV_NAME.match(k) or not isinstance(v, str) or not _TEMPLATE.match(v):
            bad.append(f"env.{k}: a variable name and a plain template")
        bad += [f"env.{k}: {{{x}}} is not a slot ({', '.join(sorted(ENV_SLOTS))})" for x in _SLOT.findall(str(v)) if x not in ENV_SLOTS]
    return bad


def option_problem(spec: dict, value) -> Optional[str]:
    """Why ``value`` is not one an option ``spec`` allows; None when it is."""
    if not isinstance(value, str) or not _OPTION_VALUE.match(value):
        return f"{value!r} is not a plain word"
    if "values" in spec:
        return None if value in (spec.get("values") or []) else f"{value!r} is not one of {', '.join(spec.get('values') or [])}"
    try:
        return None if re.fullmatch(str(spec.get("pattern")), value) else f"{value!r} does not match {spec.get('pattern')}"
    except re.error:
        return "its pattern is not a regular expression"


def _option_slots(d: dict) -> List[Tuple[str, str]]:
    """Every ``{opt_<name>}`` a template of ``d`` carries, with where: its
    ``model.env``, its ``env``, and the files ``model.files`` writes."""
    m = d.get("model") if isinstance(d.get("model"), dict) else {}
    found = []
    for where, vals in (("model.env", m.get("env")), ("env", d.get("env"))):
        for k, v in (vals or {}).items() if isinstance(vals, dict) else ():
            found += [(f"{where}.{k}", n) for n in _OPT_SLOT.findall(str(v))]
    for dest, src in (m.get("files") or {}).items() if isinstance(m.get("files"), dict) else ():
        path = os.path.join(d.get("_here") or "", str(src))
        if d.get("_here") and isinstance(src, str) and _RELATIVE.match(src) and os.path.isfile(path):
            with open(path, errors="replace") as f:
                found += [(f"model.files: {src}", n) for n in _OPT_SLOT.findall(f.read())]
    return found


def _options(d: dict) -> List[str]:
    """``options``: what a run may set of the agent's own behaviour, each with
    a ``summary``, its ``values`` (or a ``pattern``) and a ``default`` that is
    the vendor's own - so a run that sets nothing is the agent as it ships.
    Every ``{opt_<name>}`` slot a template carries must be one of them."""
    opts = d.get("options")
    if opts is not None and not isinstance(opts, dict):
        return ["options: an object, name -> {summary, values or pattern, default}"]
    opts, bad = opts or {}, []
    for name, o in opts.items():
        w = f"options.{name}"
        if not _OPTION.match(str(name)):
            bad.append(f"options: {name!r} is not a name (lower case letters and _)")
            continue
        if not isinstance(o, dict):
            bad.append(f"{w}: an object")
            continue
        if not isinstance(o.get("summary"), str) or not o["summary"].strip():
            bad.append(f"{w}.summary: say what it changes, and where its default comes from")
        if ("values" in o) == ("pattern" in o):
            bad.append(f"{w}: values or pattern, one of them")
            continue
        if "values" in o:
            vs = o["values"]
            if not isinstance(vs, list) or not vs or len(set(map(str, vs))) != len(vs) \
                    or any(not isinstance(v, str) or not _OPTION_VALUE.match(v) for v in vs):
                bad.append(f"{w}.values: distinct plain words")
                continue
        else:
            try:
                re.compile(str(o["pattern"]))
            except re.error:
                bad.append(f"{w}.pattern: a regular expression")
                continue
        if option_problem(o, o.get("default")):
            bad.append(f"{w}.default: the vendor's own default, a value it allows")
    for where, n in _option_slots(d):
        if n not in opts:
            bad.append(f"{where}: {{opt_{n}}} is not an option (options.{n})")
    return bad


def _bench_options(d: dict) -> List[str]:
    """A benchmark's ``options``: a choice its upstream offers a run (a search, a mode), each with a
    ``summary``, its ``values``, a ``default`` a run gets when it chooses none, and optionally the
    ``datasets`` it applies to, a line ``about`` each value, what a value ``needs``: an
    ``embeddings`` model called through the run's relay, ``host`` requirements by name - and
    ``when`` it applies: another option's values (a retriever's, only with a retrieval)."""
    opts = d.get("options")
    if opts is None:
        return []
    bad = _options({"options": opts})
    if bad or not isinstance(opts, dict):
        return bad
    ids = {x.get("id") for x in d.get("datasets") or [] if isinstance(x, dict)}
    hosts = d.get("host") or {}
    for name, o in opts.items():
        w = f"options.{name}"
        ds = o.get("datasets")
        if ds is not None and (not isinstance(ds, list) or not ds or any(x not in ids for x in ds)):
            bad.append(f"{w}.datasets: some of the benchmark's dataset ids")
        on = o.get("setups")
        if on is not None and (not isinstance(on, list) or not on or any(x not in SETUPS + ("yours",) for x in on)):
            bad.append(f"{w}.setups: where it applies - some of original, model, yours (an agent or a capability)")
        when = o.get("when") or {}
        if not isinstance(when, dict) or any(k == name or k not in opts or not isinstance(v, list) or not v
                                             or any(x not in opts[k].get("values", []) for x in v) for k, v in when.items()):
            bad.append(f"{w}.when: another option -> the values of it this one applies under")
        about = o.get("about") or {}
        if not isinstance(about, dict) or any(option_problem(o, k) is not None or not isinstance(v, str) for k, v in about.items()):
            bad.append(f"{w}.about: a line for some of its values")
        needs = o.get("needs") or {}
        if not isinstance(needs, dict):
            bad.append(f"{w}.needs: value -> {{embeddings, host}}")
            continue
        for v, n in needs.items():
            if option_problem(o, v) is not None or not isinstance(n, dict) or any(k not in NEED_KEYS for k in n):
                bad.append(f"{w}.needs.{v}: one of its values -> {{embeddings: a model, host: [requirements]}}")
                continue
            if "embeddings" in n and not _OPTION_VALUE.match(str(n["embeddings"])):
                bad.append(f"{w}.needs.{v}.embeddings: the embedding model, as its provider names it")
            if "host" in n and (not isinstance(n["host"], list) or any(h not in hosts for h in n["host"])):
                bad.append(f"{w}.needs.{v}.host: names of the benchmark's host requirements")
    return bad


def _host(d: dict) -> List[str]:
    """A benchmark's ``host``: what the machine its driver runs on must have for some of its options,
    by name - a ``summary``, the programs that ``check`` it (each on PATH) and how to ``install`` them
    (``apt`` and ``npm`` packages, pinned where the upstream pins them); no ``install`` for the
    machine's own hardware (a GPU, checked by its driver's program), which no package puts there."""
    h = d.get("host")
    if h is None:
        return []
    if not isinstance(h, dict):
        return ["host: name -> {summary, check, install}"]
    bad = []
    for name, r in h.items():
        w = f"host.{name}"
        if not _OPTION.match(str(name)) or not isinstance(r, dict):
            bad.append(f"{w}: a plain name -> an object")
            continue
        if not isinstance(r.get("summary"), str) or not r["summary"].strip():
            bad.append(f"{w}.summary: what it is for")
        if not isinstance(r.get("check"), list) or not r["check"] or any(not _PROGRAM.match(str(x)) for x in r["check"]):
            bad.append(f"{w}.check: the programs it puts on PATH")
        if "gpu_memory_mb" in r and (type(r["gpu_memory_mb"]) is not int or r["gpu_memory_mb"] <= 0):
            bad.append(f"{w}.gpu_memory_mb: a positive whole number of MiB")
        if "install" not in r:
            continue
        inst = r["install"]
        if not isinstance(inst, dict) or not inst or any(k not in HOST_INSTALLERS for k in inst) \
                or any(not isinstance(v, list) or any(not _PACKAGE.match(str(x)) for x in v) for v in inst.values()):
            bad.append(f"{w}.install: {{apt: [packages], npm: [packages]}}")
    return bad


def bench_options_of(bench: dict, dataset: str, given: Optional[dict] = None, setup: str = "yours") -> Tuple[dict, List[str]]:
    """The benchmark's options for one run on ``dataset`` in ``setup`` (original, model, or yours: an
    agent or a capability in its port): each that applies to both, at the value ``given`` or its default;
    and what is wrong with the given ones. An option given for a dataset or a setup it does not apply
    to is ignored - a run of several carries one choice for all. An option applies to an agent's or a
    capability's run unless its ``setups`` say otherwise."""
    opts = {n: o for n, o in (bench.get("options") or {}).items()
            if dataset in (o.get("datasets") or [dataset]) and setup in (o.get("setups") or ["yours"])}
    given = {n: v for n, v in (given or {}).items() if n in opts or n not in (bench.get("options") or {})}
    vals, bad = options_of(dict(options=opts, label=bench.get("label"), key=bench.get("key")), given)
    return vals, bad


def option_needs(bench: dict, values: dict) -> dict:
    """What the chosen option values need: {"embeddings": [models], "host": [requirement names]}."""
    out = {"embeddings": [], "host": []}
    for n, v in values.items():
        need = ((bench.get("options") or {}).get(n, {}).get("needs") or {}).get(v) or {}
        if need.get("embeddings") and need["embeddings"] not in out["embeddings"]:
            out["embeddings"].append(need["embeddings"])
        out["host"] += [h for h in need.get("host") or [] if h not in out["host"]]
    return out


def options_of(d: dict, given: Optional[dict] = None) -> Tuple[dict, List[str]]:
    """Every option's value for one run - the ones ``given``, the rest at their
    defaults - and what is wrong with the given ones (a name it does not have,
    a value it does not allow)."""
    opts, given = d.get("options") or {}, given or {}
    bad = [f"{d.get('label') or d.get('key')} has no option {n!r}" + (f" (its options: {', '.join(opts)})" if opts else "")
           for n in given if n not in opts]
    for n, v in given.items():
        why = option_problem(opts[n], v) if n in opts else None
        if why:
            bad.append(f"option {n}: {why}")
    return {n: given.get(n, o.get("default")) for n, o in opts.items()}, bad


def _command(where: str, cmd, slots=SLOTS) -> List[str]:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(w, str) for w in cmd):
        return [f"{where}: a list of words"]
    bad = []
    for w in cmd:
        if not _WORD.match(w):
            bad.append(f"{where}: {w!r} is not a plain word")
        for s in _SLOT.findall(w):
            if s not in slots:
                bad.append(f"{where}: {{{s}}} is not a slot ({', '.join(sorted(slots))})")
    return bad


def _ports(where: str, names, side: Optional[str]) -> List[str]:
    if not isinstance(names, list) or not all(isinstance(p, str) for p in names):
        return [f"{where}: a list of port names"]
    bad = []
    for p in names:
        if not ports.known(p):
            bad.append(f"{where}: {p!r} is not a port ({', '.join(ports.PORTS)})")
        elif side and ports.family(p) != side:
            bad.append(f"{where}: {p} is a port of the {ports.family(p)} family, not the {side}")
    return bad


_SERVER = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _mcp_servers(servers, has_install: bool) -> List[str]:
    """``mcp_servers``: the benchmark's own MCP servers, run **on the
    machine** in the driver's environment rather than in the agent's - each
    a name, its ``command`` (slots ``{here}``, ``{install}``) and a
    ``summary``. A unit's setup asks for one by name in its ``mcp`` entry
    (``{"name", "server", "args", "env"}``); the run gives the agent a stdio
    command that reaches it (``mcp_machine``)."""
    if servers is None:
        return []
    if not isinstance(servers, dict) or not servers:
        return ["mcp_servers: an object, a name for each server"]
    bad = []
    for name, sv in servers.items():
        w = f"mcp_servers.{name}"
        if not _SERVER.match(str(name)):
            bad.append(f"{w}: a name of lower case letters, digits and dashes")
        if not isinstance(sv, dict):
            bad.append(f"{w}: {{command, summary}}")
            continue
        bad += _command(f"{w}.command", sv.get("command"), {"here", "install"})
        if any("{install}" in x for x in sv.get("command") or [] if isinstance(x, str)) and not has_install:
            bad.append(f"{w}.command: {{install}} names driver.install, which is not set")
        if not isinstance(sv.get("summary"), str) or not sv["summary"].strip():
            bad.append(f"{w}.summary: say what it serves")
        extra = set(sv) - {"command", "summary"}
        if extra:
            bad.append(f"{w}: {', '.join(sorted(extra))} is not a field (command, summary)")
    return bad


def tag_ok(tag) -> bool:
    """A plain git tag name: no spaces, no ``..``, nothing git refuses in a ref."""
    t = str(tag) if isinstance(tag, str) else ""
    return bool(_TAG.match(t)) and ".." not in t and not t.endswith((".lock", "/", ".")) and "//" not in t and "@{" not in t


def _upstream(u) -> List[str]:
    if u is None:
        return []
    if not isinstance(u, dict):
        return ["upstream: an object"]
    bad = []
    if "repository" in u:
        if not _HTTPS.match(str(u["repository"])):
            bad.append("upstream.repository: an https address")
        if not _COMMIT.match(str(u.get("tested", ""))):
            bad.append("upstream.tested: the full commit the definition was checked against")
    if "tag" in u and not tag_ok(u["tag"]):
        bad.append("upstream.tag: the release tag the tested commit is, a plain tag name (v1.0.1)")
    elif "tag" in u and "repository" not in u:
        bad.append("upstream.tag: a tag of upstream.repository, which is not set")
    if not u.get("license"):
        bad.append("upstream.license: say it, or 'unknown'")
    return bad


def _proven(d) -> List[str]:
    offers, proven = d.get("offers") or [], d.get("proven") or {}
    if not isinstance(proven, dict):
        return ["proven: an object, written by the conformance run"]
    return [f"offers {p}, which has no conformance record (proven): run the kit"
            for p in offers if p not in proven]


def _versioned(d: dict, kind: str) -> List[str]:
    """A file with ``versions``: the list itself, then each version resolved
    and checked as a flat definition - a problem every version shares said
    once, one of a single version prefixed with it."""
    vs, bad = d["versions"], []
    own = BENCH_VERSIONED if kind == "benchmark" else VERSIONED
    if not isinstance(vs, dict) or not vs:
        return ["versions: an object, version -> what differs in it"]
    for k in (("proven",) if kind == "benchmark" else ("install", "proven", "offers")):
        if k in d:
            bad.append(f"{k}: goes in each version, under versions")
    if (d.get("upstream") or {}).get("version"):
        bad.append("upstream.version: the versions are the keys of versions")
    if d.get("default") not in vs:
        bad.append(f"default: one of the versions ({', '.join(vs)})")
    per = {}
    for v, block in vs.items():
        if not _VERSION.match(str(v)):
            bad.append(f"versions: {v!r} is not a version (letters, digits, . + _ -)")
            continue
        if not isinstance(block, dict):
            bad.append(f"versions.{v}: an object")
            continue
        extra = [k for k in block if k not in own]
        if extra:
            bad.append(f"versions.{v}: {', '.join(extra)} cannot differ between versions (only {', '.join(own)})")
        if (block.get("upstream") or {}).get("version"):
            bad.append(f"versions.{v}.upstream.version: the version is its key")
        if kind != "benchmark" and not block.get("install"):
            bad.append(f"versions.{v}.install: what this version installs")
        for p, rec in (block.get("proven") or {}).items():
            if isinstance(rec, dict) and rec.get("version") not in (None, "", v):
                bad.append(f"versions.{v}.proven.{p}: proven on {rec['version']}, not {v}")
        per[v] = problems(resolve(d, v), kind)
    if per:
        shared = set.intersection(*(set(x) for x in per.values()))
        bad += [p for p in next(iter(per.values())) if p in shared]
        bad += [f"versions.{v}: {p}" for v, x in per.items() for p in x if p not in shared]
    return bad


def _controls(c) -> List[str]:
    """``controls``: {nop|oracle: {source, from, expect?}}. ``expect`` is the
    one bound a control is held to - ``at_most`` for the null run, ``at_least``
    for the reference - as a pass rate or a mean score, from 0 to 1: a judged
    benchmark's reference may fall short of every unit and still be right.
    The reference may also say why a prompt can name its answer (``in_prompt``:
    a choice question), which the leak check then counts instead of failing."""
    if c is None:
        return []
    if not isinstance(c, dict) or not c:
        return ["controls: an object, nop and/or oracle"]
    bad = []
    for k, x in c.items():
        w = f"controls.{k}"
        if k not in CONTROLS:
            bad.append(f"{w}: a control is one of {CONTROLS}")
            continue
        if not isinstance(x, dict):
            bad.append(f"{w}: an object {{source, from}}")
            continue
        if x.get("source") not in SOURCES:
            bad.append(f"{w}.source: one of {SOURCES} (who wrote it)")
        if not isinstance(x.get("from"), str) or not x["from"].strip():
            bad.append(f"{w}.from: say where it comes from, in a sentence")
        e = x.get("expect")
        if e is not None:
            bound = EXPECT[k][0]
            v = e.get(bound) if isinstance(e, dict) else None
            if not isinstance(e, dict) or set(e) != {bound} or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                bad.append(f"{w}.expect: {{{bound}: a number from 0 to 1}}")
        if "in_prompt" in x and (k != "oracle" or not isinstance(x["in_prompt"], str) or not x["in_prompt"].strip()):
            bad.append(f"{w}.in_prompt: the reference's, a sentence on why a prompt may name its answer")
        extra = [f for f in x if f not in ("source", "from", "expect", "in_prompt")]
        if extra:
            bad.append(f"{w}: {', '.join(extra)} is not a field of a control")
    return bad


def dockerfile_lines(text: str) -> List[str]:
    """A Dockerfile's instructions, one per entry: comments and blank lines
    dropped, continuation lines joined."""
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not cur and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        out.append((cur + line).strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def _dockerfile(w: str, text: str, files: List[str]) -> List[str]:
    """An environment's Dockerfile: a layer on the base image, so no ``FROM``
    (Nullius writes it); ``COPY`` only of the files it lists (they are its whole
    build context), and no ``ADD``, which can fetch what nothing pins."""
    bad = []
    for line in dockerfile_lines(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "FROM":
            bad.append(f"{w}: no FROM - Nullius writes FROM nullius/base:<hash>, the layer's base")
        elif word == "ADD":
            bad.append(f"{w}: COPY, not ADD")
        elif word == "COPY":
            args = [a for a in rest.split() if not a.startswith("--")]
            if any(a.startswith("--from") for a in rest.split()):
                bad.append(f"{w}: COPY --from: one stage, on the base image")
            elif rest.lstrip().startswith("["):
                bad.append(f"{w}: COPY src... dest, not its JSON form")
            else:
                bad += [f"{w}: COPY {a}, which is not in files" for a in args[:-1] if a not in files]
    return bad


def _environments(envs, here: Optional[str]) -> List[str]:
    """``environments``: name -> {summary, dockerfile, files?, needs_secrets?,
    local_ok?}, or {summary, image: "own", needs_secrets?} for units that
    each bring their own image. ``dockerfile`` and each of ``files`` are paths in the
    benchmark's folder; ``needs_secrets`` is [{name, to, kind, ...}] - a
    credential the user sets in Settings → Data providers (one with ``provider``)
    or Custom keys (``nullius.secrets``), its
    shape (:func:`_secrets`), and where it goes: ``agent``, into the agent's
    container, or ``driver``, into the driver's process, which scores. One
    name is one kind, and one source, in every environment that asks for it."""
    if envs is None:
        return []
    if not isinstance(envs, dict) or not envs:
        return ["environments: an object, name -> {summary, dockerfile, ...}"]
    from nullius import settings              # the names Settings keeps for itself, which no secret may take
    bad = []
    for name, e in envs.items():
        w = f"environments.{name}"
        if not _ENV_KEY.match(str(name)) or name == BASE_ENV:
            bad.append(f"environments: {name!r} is not a name (lower case letters, digits, dashes; not {BASE_ENV!r})")
            continue
        if not isinstance(e, dict):
            bad.append(f"{w}: an object")
            continue
        extra = [k for k in e if k not in ENV_FIELDS]
        if extra:
            bad.append(f"{w}: {', '.join(extra)} is not a field of an environment ({', '.join(ENV_FIELDS)})")
        if not isinstance(e.get("summary"), str) or not e["summary"].strip():
            bad.append(f"{w}.summary: say what it holds that the base image does not")
        if "image" in e and e["image"] != OWN_IMAGE:
            bad.append(f"{w}.image: {OWN_IMAGE!r} (each unit's own image, its setup naming it) or left out (a layer on the base)")
        if own_image(e):
            # the task's own image: its setup names it, so there is no Dockerfile here, and no local run
            bad += [f"{w}.{k}: not with an own image - the unit's setup names the image" for k in ("dockerfile", "files") if k in e]
            if e.get("local_ok"):
                bad.append(f"{w}.local_ok: an own image runs only in a container")
        files = [] if own_image(e) else e.get("files") or []
        if not isinstance(files, list) or any(not isinstance(f, str) or not _RELATIVE.match(f) for f in files):
            bad.append(f"{w}.files: paths in the benchmark's folder, relative")
            files = []
        for f in files:
            if here and not os.path.isfile(os.path.join(here, f)):
                bad.append(f"{w}.files: {f} is not in the benchmark's folder")
        df = e.get("dockerfile")
        if own_image(e):
            pass
        elif not isinstance(df, str) or not _RELATIVE.match(df):
            bad.append(f"{w}.dockerfile: a Dockerfile in the benchmark's folder, relative")
        elif here:
            try:
                with open(os.path.join(here, df)) as f:
                    bad += _dockerfile(f"{w}.dockerfile", f.read(), files)
            except OSError:
                bad.append(f"{w}.dockerfile: {df} is not in the benchmark's folder")
        if "local_ok" in e and not isinstance(e["local_ok"], bool):
            bad.append(f"{w}.local_ok: true or false")
        if "network" in e:
            from . import network_requirements
            bad += [f"{w}.{message}" for message in network_requirements.problems(e["network"])]
        bad += _secrets(w, e.get("needs_secrets"), bool(e.get("local_ok")), settings)
    kinds: Dict[str, set] = {}
    sources: Dict[str, set] = {}
    for e in envs.values():
        for s in (e.get("needs_secrets") or []) if isinstance(e, dict) and isinstance(e.get("needs_secrets"), list) else []:
            if isinstance(s, dict) and isinstance(s.get("name"), str):
                kinds.setdefault(s["name"], set()).add(s.get("kind") or "value")
                sources.setdefault(s["name"], set()).add(str(s.get("provider") or "the benchmark"))
    bad += [f"environments: {n} is a {' and a '.join(sorted(k))} - one name, one kind, in every environment"
            for n, k in kinds.items() if len(k) > 1]
    bad += [f"environments: {n} comes from {' and from '.join(sorted(k))} - one name, one source, in every environment"
            for n, k in sources.items() if len(k) > 1]
    return bad


def _secrets(w: str, secrets, local_ok: bool, settings) -> List[str]:
    """An environment's ``needs_secrets``: [{name, to, kind?, summary?,
    path?, env?, provider?}]. ``kind`` (``value`` when unsaid): a ``value`` reaches its
    process as the variable ``name``; a ``file`` is mounted read-only at
    ``path`` in the agent's container, and ``env``, when said, names the
    variable that points to it - the only way a driver, or an agent on local
    processes, finds it; a ``server`` (``host:port``) is the variable
    ``name``, and on docker the one address the agent's container may reach.
    ``provider`` (``huggingface``): a ``value`` to the ``driver``, from Settings → Data providers alone."""
    if secrets is None:
        return []
    if not isinstance(secrets, list):
        return [f"{w}.needs_secrets: a list of {{name, to, kind}}"]
    bad, seen, paths = [], set(), set()
    for i, s in enumerate(secrets):
        sw = f"{w}.needs_secrets[{i}]"
        if not isinstance(s, dict) or set(s) - set(SECRET_FIELDS):
            bad.append(f"{sw}: {{{', '.join(SECRET_FIELDS)}}} (name and to always)")
            continue
        n = s.get("name")
        if not isinstance(n, str) or not _ENV_NAME.match(n) or n in seen:
            bad.append(f"{sw}.name: a variable name, each once")
        elif settings.reserved_name(n):
            bad.append(f"{sw}.name: {n} is already the name of something else")
        seen.add(n)
        to, kind = s.get("to"), s.get("kind", "value")
        if to not in SECRET_TO:
            bad.append(f"{sw}.to: one of {SECRET_TO} - where it goes, said explicitly")
        if kind not in SECRET_KINDS:
            bad.append(f"{sw}.kind: one of {SECRET_KINDS}")
            continue
        if "provider" in s:
            from nullius.secrets import PROVIDERS          # the data providers Settings keeps a token for
            if not isinstance(s["provider"], str) or s["provider"] not in PROVIDERS:
                bad.append(f"{sw}.provider: one of {', '.join(PROVIDERS)}")
            if to != "driver":
                bad.append(f"{sw}.to: driver - a data provider's token never reaches an agent")
            if kind != "value":
                bad.append(f"{sw}.kind: value - a data provider's token is a key")
        p, v = s.get("path"), s.get("env")
        if kind != "file":
            if p is not None or v is not None:
                bad.append(f"{sw}: path and env are a file's; a {kind} reaches its process as the variable {n}")
            continue
        if v is not None:
            if not isinstance(v, str) or not _ENV_NAME.match(v) or settings.reserved_name(v) or v in seen:
                bad.append(f"{sw}.env: a variable name of its own, which will hold the file's path")
            seen.add(v)
        if to == "driver":
            if p is not None:
                bad.append(f"{sw}.path: the driver runs on the machine, not in a container - it finds the file by env")
            if v is None:
                bad.append(f"{sw}.env: the variable the driver finds the file by")
            continue
        if not isinstance(p, str) or not _ABS.match(p) or "/../" in p + "/" or "/./" in p + "/" \
                or any(p == u or p.startswith(u + "/") for u in _NOT_UNDER):
            bad.append(f"{sw}.path: an absolute path in the agent's container, where the software looks for it "
                       f"(not under {', '.join(_NOT_UNDER)})")
        elif p in paths:
            bad.append(f"{sw}.path: {p} is another licence's")
        paths.add(p)
        if local_ok and v is None:
            bad.append(f"{sw}.env: on local processes (local_ok) the agent finds the file by a variable, not at {p}")
    return bad


def environment_of(bench: dict, unit_setup: Optional[dict], dataset: Optional[dict] = None) -> Tuple[Optional[str], Optional[dict]]:
    """The environment a unit runs in: the one its setup names
    (``environment``), else its dataset's default, else none - the base image.
    (name, its definition); (None, None) for the base. A name the benchmark
    does not declare raises ValueError."""
    envs = bench.get("environments") or {}
    name = (unit_setup or {}).get("environment") or (dataset or {}).get("environment")
    if not name or name == BASE_ENV:
        return None, None
    if name not in envs:
        raise ValueError(f"environment {name!r} is not one of {bench.get('label') or bench.get('key')}'s "
                         f"({', '.join(envs) or 'it declares none'})")
    return name, envs[name]


def own_image(env_def: Optional[dict]) -> bool:
    """Whether an environment is an own image: each unit's setup names the
    task's own image, and the agent is added to it at run time."""
    return isinstance(env_def, dict) and env_def.get("image") == OWN_IMAGE


def secrets_of(bench: dict) -> List[dict]:
    """Every licence the benchmark's environments need: {name, to, kind,
    environment, summary, path?, env?, provider?}, once per environment that asks."""
    return [dict({k: s[k] for k in ("path", "env", "provider") if k in s}, name=s["name"], to=s["to"], kind=s.get("kind") or "value",
                 environment=n, summary=s.get("summary", ""))
            for n, e in (bench.get("environments") or {}).items() if isinstance(e, dict)
            for s in e.get("needs_secrets") or [] if isinstance(s, dict) and s.get("name")]


def missing_secrets(env_def: Optional[dict], held) -> List[str]:
    """The licences an environment needs that ``held`` (name -> what the run
    holds of it, ``run.Licences``) does not."""
    return [s["name"] for s in (env_def or {}).get("needs_secrets") or [] if not held.get(s["name"])]


def secret_values(env_def: Optional[dict], held, to: str, *, inside: bool = False) -> dict:
    """The variables an environment's licences bound for ``to`` (agent,
    driver) give their process, those that are held: a value or a server as
    ``name``; a file's path as its ``env`` - where it is mounted
    (``inside`` a container) or where it is on the machine."""
    out = {}
    for s in (env_def or {}).get("needs_secrets") or []:
        h = held.get(s["name"])
        if s.get("to") != to or not h:
            continue
        if (s.get("kind") or "value") != "file":
            out[s["name"]] = h["value"]
        elif s.get("env"):
            out[s["env"]] = s["path"] if inside and s.get("path") else h["path"]
    return out


def control(bench: dict, which: str) -> Optional[dict]:
    """The control ``which`` of ``bench`` as the other side of a run, or None
    when the benchmark does not declare it. It installs nothing, starts
    nothing and calls no model: the run answers "" for ``nop`` and asks the
    driver's ``oracle`` for the reference."""
    c = (bench.get("controls") or {}).get(which)
    if which not in CONTROLS or not isinstance(c, dict):
        return None
    bound, value = EXPECT[which]
    return {"key": which, "label": CONTROL_LABEL[which], "summary": c.get("from", ""), "source": c.get("source", ""),
            "from": c.get("from", ""), "expect": {bound: (c.get("expect") or {}).get(bound, value)}, "upstream": {},
            "_kind": "control", "_ref": which, "_here": bench.get("_here", "")}


def _leaderboard(d: dict) -> List[str]:
    """``leaderboard``: {says, simulator?, judge?, bench_options?, repeat?} - what the
    upstream's leaderboard fixes besides the ranked model, from its own submission
    rules: the user simulator's model, the judge's, the benchmark's options (a
    simulator's reasoning effort) and the fewest attempts per unit."""
    lb = d.get("leaderboard")
    if lb is None:
        return []
    w = "leaderboard"
    if not isinstance(lb, dict):
        return [f"{w}: {{says, simulator?, judge?, bench_options?, repeat?}}"]
    bad = [f"{w}.{k}: not a leaderboard key" for k in lb if k not in ("says", "simulator", "judge", "bench_options", "repeat")]
    if not isinstance(lb.get("says"), str) or not lb["says"].strip():
        bad.append(f"{w}.says: which leaderboard, and where its rules say so")
    for k, has in (("simulator", d.get("simulator")), ("judge", (d.get("score") or {}).get("judge"))):
        if k in lb and (not has or not _MODEL_NAME.match(str(lb[k]))):
            bad.append(f"{w}.{k}: the leaderboard's model, as its maker names it, on a benchmark that has one")
    opts, given = d.get("options") or {}, lb.get("bench_options", {})
    if not isinstance(given, dict):
        bad.append(f"{w}.bench_options: an object, option to value")
    else:
        bad += [f"{w}.bench_options.{k}: one of the benchmark's options, at one of its values" for k, v in given.items()
                if k not in opts or v not in (opts[k].get("values") or [])]
    r = lb.get("repeat", 1)
    if isinstance(r, bool) or not isinstance(r, int) or not 1 <= r <= 32:
        bad.append(f"{w}.repeat: the fewest attempts per unit, 1 to 32")
    return bad


def _setups(s) -> List[str]:
    """``setups``: {original: {summary, models, default, served_as?, providers,
    side_models?, units?, turn_seconds?, paper?, comparable?}, model?: {summary, runs_as}}. ``original``'s
    ``models`` are the paper's, by the names the upstream's code knows them;
    ``side_models`` the ones its code asks besides the answerer (an extraction,
    a summary), sent as asked through served_as and the provider's prefix;
    ``providers`` the ones that serve them, each with the prefix its names
    carry there; ``model`` needs ``original`` (its pipeline) and asks as one
    of its models (``runs_as``)."""
    if s is None:
        return []
    if not isinstance(s, dict) or not s:
        return ["setups: an object, original and/or model"]
    bad = []
    for k, x in s.items():
        w = f"setups.{k}"
        if k not in SETUPS:
            bad.append(f"{w}: a setup is one of {SETUPS}")
            continue
        if not isinstance(x, dict):
            bad.append(f"{w}: an object")
            continue
        if not isinstance(x.get("summary"), str) or not x["summary"].strip():
            bad.append(f"{w}.summary: what runs, in a sentence")
        allowed = {"summary"}
        if k == "original":
            allowed |= {"models", "default", "served_as", "providers", "side_models", "units", "turn_seconds", "paper", "comparable", "reference_agent"}
            if "reference_agent" in x and (not isinstance(x["reference_agent"], str) or not x["reference_agent"].strip()):
                bad.append(f"{w}.reference_agent: the upstream agent's display name")
            models = x.get("models")
            if models is None:                     # any model: the upstream's leaderboard ranks models, its pipeline fixed
                models = []
                if "default" in x or "served_as" in x:
                    bad.append(f"{w}: default and served_as come with models")
            elif not isinstance(models, list) or not models or not all(isinstance(m, str) and _MODEL_NAME.match(m) for m in models):
                bad.append(f"{w}.models: the paper's models, by the names the upstream's code knows - or no models: any")
                models = []
            elif x.get("default") not in models:
                bad.append(f"{w}.default: one of its models")
            side = x.get("side_models", [])
            if not isinstance(side, list) or not all(isinstance(m, str) and _MODEL_NAME.match(m) for m in side) \
                    or len(set(side)) != len(side) or (side and "models" not in x):
                bad.append(f"{w}.side_models: the models the upstream's code asks besides its answerer, each once, beside models")
                side = []
            sa = x.get("served_as", {})
            if not isinstance(sa, dict) or not all(m in models + side and isinstance(v, str) and _MODEL_NAME.match(v) for m, v in sa.items()):
                bad.append(f"{w}.served_as: {{one of its models: the name the provider serves it as}}")
            pv = x.get("providers")
            if not isinstance(pv, dict) or not pv or not all(p in ("openrouter", "openai", "custom") and isinstance(v, str) for p, v in pv.items()):
                bad.append(f"{w}.providers: {{openrouter|openai|custom: the prefix its models' names carry there}}")
            u = x.get("units")
            if u is not None and (not isinstance(u, dict) or not all(isinstance(u.get(f), str) and u[f] for f in ("noun", "nouns"))):
                bad.append(f"{w}.units: {{noun, nouns}}, what one of its units is")
            t = x.get("turn_seconds")
            if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t < 1):
                bad.append(f"{w}.turn_seconds: a whole number of seconds")
            if "paper" in x and (not isinstance(x["paper"], str) or not x["paper"].strip()):
                bad.append(f"{w}.paper: where the paper reports its numbers, in a phrase")
            if "comparable" in x and not isinstance(x["comparable"], bool):
                bad.append(f"{w}.comparable: false when no served model is the paper's, so its runs compare with each other only")
        else:
            allowed |= {"runs_as"}
            orig = s.get("original") if isinstance(s.get("original"), dict) else None
            if orig is None:
                bad.append(f"{w}: needs original, whose pipeline it runs")
            elif "models" not in orig:
                bad.append(f"{w}: the original takes any model already")
            elif x.get("runs_as") not in (orig.get("models") or []):
                bad.append(f"{w}.runs_as: one of original's models, the one whose window and batching it takes")
        extra = [f for f in x if f not in allowed]
        if extra:
            bad.append(f"{w}: {', '.join(extra)} is not a field of the {k} setup")
    return bad


# Every setting of a benchmark's upstream, and what Nullius does with it (``upstream-settings.json`` in its folder).
SETTINGS_FILE = "upstream-settings.json"
_DISPOSITION = re.compile(r"^(option:[a-z0-9_]+|setup:(original|model)|dataset:[A-Za-z0-9_.-]+|fixed:.+ - .+|not offered - .+)$")


def settings_problems(versions: List[dict]) -> List[str]:
    """What is wrong with a benchmark's settings inventory: every setting of
    every evaluation entry point of its upstream, one row each,
    ``{entry, setting, values, default, nullius}``, ``nullius`` exactly one of
    ``option:<name>``, ``setup:<original|model>``, ``dataset:<id>``,
    ``fixed:<value> - <why>`` or ``not offered - <why>``; an option, setup or
    dataset named must be one some version of the benchmark has. ``versions``:
    the benchmark resolved at each of its versions. Missing, it is a problem:
    a benchmark's settings are inventoried before it counts as imported."""
    if not versions:
        return []
    here = versions[0].get("_here") or ""
    path = os.path.join(here, SETTINGS_FILE)
    if not os.path.isfile(path):
        return [f"{SETTINGS_FILE}: none - every setting of the upstream's evaluation, and what Nullius does with each"]
    try:
        with open(path) as f:
            rows = json.load(f)
    except (OSError, ValueError) as e:
        return [f"{SETTINGS_FILE}: {e}"]
    if not isinstance(rows, list) or not rows:
        return [f"{SETTINGS_FILE}: a list of rows, one per setting"]
    options = {n for v in versions for n in (v.get("options") or {})}
    setups = {n for v in versions for n in (v.get("setups") or {})}
    datasets = {d.get("id") for v in versions for d in (v.get("datasets") or [])}
    bad, seen = [], set()
    for i, r in enumerate(rows):
        w = f"{SETTINGS_FILE}[{i}]"
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r[k].strip() for k in ("entry", "setting", "nullius")):
            bad.append(f"{w}: {{entry, setting, values, default, nullius}}, the first and last two non-empty")
            continue
        key = (r["entry"], r["setting"])
        if key in seen:
            bad.append(f"{w}: {r['entry']} {r['setting']} twice")
        seen.add(key)
        n = r["nullius"].strip()
        if not _DISPOSITION.match(n):
            bad.append(f"{w} ({r['setting']}): nullius is option:<name>, setup:<original|model>, dataset:<id>, "
                       "fixed:<value> - <why> or not offered - <why>")
        elif n.startswith("option:") and n[7:] not in options:
            bad.append(f"{w} ({r['setting']}): no option {n[7:]} in any version")
        elif n.startswith("setup:") and n[6:] not in setups:
            bad.append(f"{w} ({r['setting']}): no {n[6:]} setup in any version")
        elif n.startswith("dataset:") and n[8:] not in datasets:
            bad.append(f"{w} ({r['setting']}): no dataset {n[8:]} in any version")
    return bad


def setup(bench: dict, which: str, model: str = "", provider: str = "") -> tuple:
    """The setup ``which`` of ``bench`` as the other side of a run, with
    ``model`` on ``provider``: (definition, why not). The definition says the
    model the upstream's code is asked as (``asked``) and the one the relay
    sends (``sent``). ``original`` takes only the paper's models, on a
    provider that serves them; ``model`` takes any, asked as its ``runs_as``.
    (None, why) when the benchmark has no such setup or the model is refused."""
    s = bench.get("setups") or {}
    if which not in SETUPS or not isinstance(s.get(which), dict):
        return None, f"{bench['label']} has no {which} setup"
    orig, x = s["original"], s[which]
    model = (model or "").strip()
    side = []
    if which == "original" and "models" not in orig:      # any model, sent as the provider lists it
        if not model:
            return None, f"the original {bench['label']} needs a model: its leaderboard ranks models"
        if provider and provider not in orig["providers"]:
            return None, f"the original runs on " + " or ".join(orig["providers"])
        asked = sent = model
    elif which == "original":
        model = model or orig["default"]
        if model not in orig["models"]:
            return None, (f"the original {bench['label']} runs only the paper's models ({', '.join(orig['models'])}): "
                          f"{model} is not one - choose the model setup for another")
        if provider and provider not in orig["providers"]:
            return None, (f"the paper's models are not served by {provider}: the original runs on "
                          + " or ".join(orig["providers"]))
        asked = model
        prefix = orig["providers"].get(provider, "") if provider else ""
        served = lambda m: prefix + (orig.get("served_as") or {}).get(m, m)
        sent = served(model)
        # the other models its code asks, each sent as the provider serves it; one the answerer is served as is its own
        side = [f"{m}={served(m)}" for m in orig.get("side_models") or [] if served(m) != sent]
    else:
        if not model:
            return None, f"the model setup needs a model: the one to put in {orig['default']}'s place"
        asked, sent = x["runs_as"], model
    units = orig.get("units") or bench.get("units") or {"noun": "unit", "nouns": "units"}
    return {"model": {"side_models": side}, "key": which, "label": f"{bench['label']} {setup_label(bench, which).lower()}",
            "summary": x["summary"], "asked": asked, "sent": sent, "units": units, "comparable": which == "original" and x.get("comparable", True),
            "turn_seconds": orig.get("turn_seconds"), "upstream": {}, "_kind": "upstream", "_ref": which,
            "_here": bench.get("_here", "")}, ""


def problems(d: dict, kind: str) -> List[str]:
    """What is wrong with definition ``d`` of ``kind``; empty when it may be offered."""
    kind = names.kind(kind)
    d = names.definition(d)
    if kind not in KINDS:
        return [f"{kind!r} is not a kind of definition"]
    if not isinstance(d, dict):
        return ["a JSON object"]
    if "versions" in d:
        return _versioned(d, kind)
    bad = []
    if d.get("schema") != KINDS[kind]["schema"]:
        bad.append(f"schema: {KINDS[kind]['schema']}")
    if not _KEY.match(str(d.get("key", ""))):
        bad.append("key: lower case letters, digits and dashes")
    elif kind != "benchmark" and d.get("key") in CONTROLS:
        bad.append(f"key: {d['key']} is the word for a benchmark's control")
    elif kind != "benchmark" and d.get("key") in SETUPS:
        bad.append(f"key: {d['key']} is the word for a benchmark's setup")
    for f in ("label", "summary"):
        if not isinstance(d.get(f), str) or not d[f].strip():
            bad.append(f"{f}: say it")
    if "example" in d and (kind == "benchmark" or not isinstance(d["example"], bool)):
        bad.append("example: true or false, an agent's or a capability's")
    bad += _upstream(d.get("upstream"))
    if kind == "benchmark":
        if d.get("family") not in FAMILIES:
            bad.append(f"family: one of {FAMILIES}")
        needs = d.get("needs")
        bad += _ports("needs", needs, d.get("family"))
        if isinstance(needs, list) and not needs:
            bad.append("needs: at least one port")
        bad += _host(d)
        bad += _bench_options(d)
        drv = d.get("driver") or {}
        bad += _command("driver.command", drv.get("command") if isinstance(drv, dict) else None, {"here", "install"})
        if isinstance(drv, dict):
            if "install" in drv:
                bad += _install(dict(driver_install(d), install=drv["install"]), "driver.install")
            elif any("{install}" in w for w in drv.get("command") or [] if isinstance(w, str)):
                bad.append("driver.command: {install} names driver.install, which is not set")
        bad += _mcp_servers(d.get("mcp_servers"), isinstance(drv, dict) and "install" in drv)
        ds = d.get("datasets")
        if not isinstance(ds, list) or not ds:
            bad.append("datasets: at least one")
        else:
            ids = set()
            for i, x in enumerate(ds):
                w = f"datasets[{i}]"
                if not isinstance(x, dict):
                    bad.append(f"{w}: an object")
                    continue
                if not _ID.match(str(x.get("id", ""))) or x.get("id") in ids:
                    bad.append(f"{w}.id: a unique plain name")
                ids.add(x.get("id"))
                if x.get("path") is not None and x.get("url") is not None:
                    bad.append(f"{w}: path (in the upstream) or url (downloaded), not both")
                if x.get("path") is not None:
                    if not _RELATIVE.match(str(x["path"])):
                        bad.append(f"{w}.path: a relative path")
                if x.get("url") is not None and not _HTTPS.match(str(x["url"])):
                    bad.append(f"{w}.url: an https address, pinned (a fixed revision, not main)")
                if (x.get("path") is not None or x.get("url") is not None) and not _SHA256.match(str(x.get("sha256", ""))):
                    bad.append(f"{w}.sha256: the file's digest")
                if not isinstance(x.get("units"), int) or x["units"] < 1:
                    bad.append(f"{w}.units: how many units it holds")
        u = d.get("units") or {}
        if not (isinstance(u.get("noun"), str) and isinstance(u.get("nouns"), str)):
            bad.append("units: {noun, nouns}")
        if "redistribute" in u and not isinstance(u["redistribute"], bool):
            bad.append("units.redistribute: false when the data may not be redistributed")
        lim = d.get("limits")
        if lim is not None:
            if not isinstance(lim, dict):
                bad.append("limits: an object")
            else:
                for f in ("turn_seconds", "turns"):
                    v = lim.get(f)
                    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100000):
                        bad.append(f"limits.{f}: a whole number from 1")
                a = lim.get("attempts")
                if a is not None and (not isinstance(a, str) or not a.strip()):
                    bad.append("limits.attempts: how the upstream tells one attempt of a unit from another (its seed per trial), "
                               "in a sentence - the driver is then sent attempt with setup, turn and answer")
        s = d.get("score") or {}
        if s.get("kind") not in SCORES or not s.get("says"):
            bad.append(f"score: {{kind: one of {SCORES}, says}}")
        if "runs_code" in s and not isinstance(s["runs_code"], bool):
            bad.append("score.runs_code: true or false")
        if "kit_version" in d and (isinstance(d["kit_version"], bool) or not isinstance(d["kit_version"], int) or d["kit_version"] < 1):
            bad.append("kit_version: a kit's version, a whole number from 1")
        bad += _skills(d)
        j = s.get("judge")
        if j is not None and (not isinstance(j, dict) or not _MODEL_NAME.match(str(j.get("model", ""))) or not j.get("says")):
            bad.append("score.judge: {model: the official judge, as the provider names it; says: what it is asked}")
        sim = d.get("simulator")
        if sim is not None:
            if not isinstance(sim, dict) or not _MODEL_NAME.match(str(sim.get("model", ""))) or not sim.get("says"):
                bad.append("simulator: {model: the official simulated user's model, as the provider names it; says: whom it plays}")
            elif "agent.conversation/v1" not in (needs if isinstance(needs, list) else []):
                bad.append("simulator: only a benchmark that needs agent.conversation/v1 has a user to simulate")
        bad += _controls(d.get("controls"))
        bad += _leaderboard(d)
        bad += _setups(d.get("setups"))
        bad += _environments(d.get("environments"), d.get("_here"))
        envs = d.get("environments") if isinstance(d.get("environments"), dict) else {}
        if d.get("family") == "capability":
            bad += [f"environments.{n}.image: an own image holds an agent's work; a capability benchmark has none"
                    for n, e in envs.items() if own_image(e)]
        for i, x in enumerate(ds if isinstance(ds, list) else []):
            if isinstance(x, dict) and "environment" in x and x["environment"] not in envs:
                bad.append(f"datasets[{i}].environment: one of environments ({', '.join(envs) or 'none declared'})")
        if d.get("proven") is not None and not isinstance(d.get("proven"), dict):
            bad.append("proven: an object, per dataset, written by cli prove")
    elif kind == "agent":
        st = d.get("start") or {}
        if ("acp" in st) == ("command" in st):
            bad.append("start: acp or command, one of them")
        elif "acp" in st:
            bad += _command("start.acp", st["acp"], {"here", "install"})
        else:
            bad += _command("start.command", st["command"])
            if not any("{prompt}" in w for w in st.get("command") or []):
                bad.append("start.command: carries {prompt}")
            extra = [p for p in d.get("offers") or [] if p not in ("agent.task/v1", "agent.workspace/v1", "agent.search/v1")]
            if extra:
                bad.append(f"offers: a command agent can offer only agent.task/v1, agent.workspace/v1 and agent.search/v1, not {extra}")
        bad += _ports("offers", d.get("offers") or [], "agent")
        only = d.get("only_for")
        if only is not None and (not isinstance(only, list) or not only or len(set(map(str, only))) != len(only)
                                 or not all(isinstance(k, str) and _KEY.match(k) for k in only)):
            bad.append("only_for: the keys of the benchmarks it is the reference agent of, each once")
        bad += _proven(d)
        bad += _install(d)
        bad += _env(d.get("env"))
        bad += _model(d.get("model") or {}, d.get("_here"))
        bad += _options(d)
        bad += _caches(d)
        fid = d.get("fidelity")
        if fid is not None:
            if not isinstance(fid, dict):
                bad.append("fidelity: {command: the agent's own non-interactive command, carrying {prompt}}")
            else:
                bad += _command("fidelity.command", fid.get("command"))
                if not any("{prompt}" in w for w in fid.get("command") or [] if isinstance(w, str)):
                    bad.append("fidelity.command: carries {prompt}")
    else:
        if not _KEY.match(str(d.get("kind", ""))):
            bad.append("kind: what kind of capability (memory, ...)")
        bad += _command("start.command", (d.get("start") or {}).get("command"), {"here", "install"})
        bad += _ports("offers", d.get("offers") or [], "capability")
        bad += _proven(d)
        bad += _install(d)
        bad += _env(d.get("env"))
        bad += _options(d)
        bad += _caches(d)
        if d.get("model") is not None:                 # a capability that calls a model of its own: the relay, as an agent's
            bad += _model(d["model"], d.get("_here"))
        for p in d.get("offers") or []:
            if ports.known(p) and p.split(".", 1)[1].split("/")[0] != d.get("kind"):
                bad.append(f"offers {p}, which is not a port of its kind ({d.get('kind')})")
    return bad


def entry_dir(kind: str, key: str, root: Optional[str] = None) -> Optional[str]:
    """The selected entry folder, or None; a user folder wins in its entirety."""
    return storage.entry_dir(KINDS[names.kind(kind)]["dir"], key, CATALOGUE, root)


def writable_dir(kind: str, key: str, root: Optional[str] = None) -> str:
    """The user folder, copying all bundled files before an edit if needed."""
    if not _KEY.fullmatch(key):
        raise ValueError("a key is lower case letters, digits and dashes")
    return storage.writable_dir(KINDS[names.kind(kind)]["dir"], key, CATALOGUE, root)


def _read(path: str, kind: str, key: str) -> Tuple[Optional[dict], List[str]]:
    try:
        with open(path) as f:
            d = names.definition(json.load(f))
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return None, [f"{kind} {key}: {e}"]
    try:
        found = problems(dict(d, _here=os.path.dirname(path)) if isinstance(d, dict) else d, kind)
    except Exception as e:                       # a field of the wrong type the checks do not expect: left out, said
        found = [f"malformed ({type(e).__name__}: {e}); a field holds a value of the wrong type"]
    bad = [f"{kind} {key}: {p}" for p in found]
    if isinstance(d, dict) and d.get("key") != key:
        bad.append(f"{kind} {key}: key {d.get('key')!r} is not its folder's name")
    if bad:
        return None, bad
    d = dict(d, _here=os.path.dirname(path), _kind=kind)
    return d, []


def load(kind: str, root: Optional[str] = None, every: bool = False) -> Tuple[List[dict], List[str]]:
    """Every definition of ``kind``, in folder order, and errors in those left out.
    With no root the user catalogue overlays the bundle, by whole folder;
    an explicit root reads that tree alone. An invalid override never falls
    back to the bundled definition.
    A loaded definition carries ``_here``, its folder, and ``_kind``, and is
    resolved (:func:`resolve`) to its default version, or, with ``every``, to
    each of its versions in turn - a benchmark as an agent or a capability."""
    kind = names.kind(kind)
    name = KINDS[kind]["file"]
    selected = storage.folders(KINDS[names.kind(kind)]["dir"], CATALOGUE, root)
    out, bad = [], []
    for n, folder in selected.items():
        path = definition_path(folder, kind)
        if not os.path.isfile(path):
            if kind != "benchmark":  # a Harbor-only benchmark needs no standard definition
                bad.append(f"{kind} {n}: missing {name}")
            continue
        d, why = _read(path, kind, n)
        bad += why
        if d:
            out += [resolve(d, v) for v in versions(d)] if every else [resolve(d)]
    return out, bad


def get(kind: str, ref: str, root: Optional[str] = None) -> Optional[dict]:
    """One definition by ``key`` (its default version) or ``key@version``, resolved."""
    kind = names.kind(kind)
    key, version = split(ref)
    if not _KEY.match(key or ""):
        return None
    folder = entry_dir(kind, key, root)
    if folder is None:
        return None
    path = definition_path(folder, kind)
    if not os.path.isfile(path):
        return None
    d, _ = _read(path, kind, key)
    return None if d is None else resolve(d, version)


def kits() -> List[dict]:
    """The conformance kits: benchmarks kept in this package, one per port."""
    out = []
    for n in sorted(os.listdir(KITS)):
        path = os.path.join(KITS, n, "standard.json")
        if os.path.isfile(path):
            d, bad = _read(path, "benchmark", n)
            if bad:
                raise ValueError("; ".join(bad))
            out.append(d)
    return out


def kit(port: str) -> Optional[dict]:
    name = (ports.PORTS.get(names.port(port)) or {}).get("kit")
    return next((k for k in kits() if k["key"] == name), None) if name else None


# A kit is made stronger under the same port - more checks of the same
# contract - and says so by its ``kit_version``; each proof records the one it
# passed. A proof on an older kit stays a proof (a kit's change un-proves no
# one), but it is said to be one, so a re-run can be asked for.
def kit_version(k: Optional[dict]) -> int:
    """A kit's version; 1 for a kit that does not say."""
    return int((k or {}).get("kit_version") or 1)


def proof_kit(record: Optional[dict]) -> int:
    """The kit version a ``proven`` record passed; 1 for a record from before
    kits had versions."""
    return int((record or {}).get("kit_version") or 1)


def on_current_kit(port: str, record: Optional[dict]) -> bool:
    """Whether ``record``, a proof of ``port``, was made on its kit's current version."""
    return proof_kit(record) >= kit_version(kit(port))


def older_kit(port: str, record: Optional[dict]) -> Optional[str]:
    """``proven on kit v1; kit v2 is current`` for a proof on an older kit, else None."""
    if not record or on_current_kit(port, record):
        return None
    return f"proven on kit v{proof_kit(record)}; kit v{kit_version(kit(port))} is current"


# A kit's checks are of two sorts. Its **basic** checks are the port itself:
# the port is proven when every one passes. Its **skills** (``skills``: id ->
# what it shows, in plain words) are harder uses of the same contract - an
# agent that misses one still has the port, and its proof says which skills
# it showed; a benchmark that relies on one says so (``needs_skills``). A
# check a kit does not name a skill is basic, so a kit without ``skills``
# is every check basic, as before skills existed.
def skills_of(k: Optional[dict]) -> dict:
    """A kit's skills, {id: what it shows}; empty for a kit without."""
    s = (k or {}).get("skills")
    return dict(s) if isinstance(s, dict) else {}


def port_skills(port: str) -> dict:
    """The skills of ``port``'s kit, {id: what it shows}."""
    return skills_of(kit(port))


def skills_shown(port: str, record: Optional[dict]) -> List[str]:
    """The skills of ``port`` that ``record``, its proof, showed. A proof from
    before skills were recorded passed every check of its kit - all of them
    basic then - so on the current kit it showed every skill the kit has; on
    an older kit, none can be read from it."""
    if not record:
        return []
    if isinstance(record.get("skills"), dict):
        return [s for s, r in record["skills"].items() if isinstance(r, dict) and r.get("ok")]
    return list(port_skills(port)) if on_current_kit(port, record) else []


def _skills(d: dict) -> List[str]:
    """A kit's ``basic`` and ``skills``, a benchmark's ``needs_skills``."""
    bad = []
    basic, skills = d.get("basic"), d.get("skills")
    if basic is not None and (not isinstance(basic, list) or not all(isinstance(x, str) and _ID.match(x) for x in basic)):
        bad.append("basic: a list of the kit's check ids that are the port itself")
    if skills is not None:
        if not isinstance(skills, dict) or not all(_ID.match(str(k)) and isinstance(v, str) and v.strip() for k, v in skills.items()):
            bad.append("skills: {check id: what the skill shows, in plain words}")
        elif isinstance(basic, list) and set(basic) & set(skills):
            bad.append(f"skills: {', '.join(sorted(set(basic) & set(skills)))} cannot be a basic check and a skill")
    want = d.get("needs_skills")
    if want is None:
        return bad
    if not isinstance(want, dict):
        return bad + ["needs_skills: {port: [skill ids of its kit]}"]
    for p, ids in want.items():
        w = f"needs_skills.{p}"
        if p not in (d.get("needs") or []):
            bad.append(f"{w}: a port the benchmark needs")
        elif not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
            bad.append(f"{w}: a list of skill ids")
        else:
            have = port_skills(p)
            bad += [f"{w}: {x!r} is not a skill of its kit ({', '.join(have) or 'it has none'})" for x in ids if x not in have]
    return bad


def definition_path(folder: str, kind: str) -> str:
    kind = names.kind(kind)
    path = os.path.join(folder, KINDS[kind]["file"])
    legacy = os.path.join(folder, "feature.json")
    return legacy if kind == "capability" and not os.path.exists(path) and os.path.isfile(legacy) else path


def path_of(d: dict) -> str:
    return definition_path(d["_here"], d["_kind"])


@contextlib.contextmanager
def _locked(path: str):
    """One writer of a definition file at a time, across processes (two
    proofs, a conformance check and the user's own edit through the app)."""
    import fcntl
    where = os.path.join(paths.cache_dir(), "locks")
    os.makedirs(where, exist_ok=True)
    with open(os.path.join(where, hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:24]), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save(d: dict, change: Optional[Callable[[dict], None]] = None) -> None:
    """Write a loaded definition back (the conformance run's ``offers`` and
    ``proven``), without the fields loading added. A version of a file with
    ``versions`` writes only its own ``proven``; the rest of the file is kept.

    ``change``: what the run decided, applied to the definition as it is on
    disk now, under a lock - not to the copy loaded when a long run started,
    which would put back what another proof or the user wrote meanwhile. It
    is given the dict holding ``offers`` and ``proven`` (a version's own, for
    a version) and changes it in place; ``d`` then holds the result too."""
    # Explicit fixture roots remain local. Only a loaded bundled entry needs
    # copying; helpers and model files beside its definition must follow it.
    folder = d["_here"]
    bundled = os.path.join(CATALOGUE, KINDS[d["_kind"]]["dir"], d["key"])
    if os.path.realpath(folder) == os.path.realpath(bundled):
        d["_here"] = writable_dir(d["_kind"], d["key"])
    storage.ensure_writable(d["_here"], CATALOGUE)
    path = path_of(d)
    with _locked(path):
        versioned = bool(d.get("_ref") and "@" in str(d["_ref"]))
        if versioned or change is not None:
            try:
                with open(path) as f:
                    clean = names.definition(json.load(f))
            except (OSError, ValueError):
                clean = None
            if not isinstance(clean, dict):
                clean = {k: v for k, v in d.items() if not k.startswith("_")}
            if versioned:              # a version writes its own proven only, seen beside the offers it resolves to
                target = clean["versions"][d["_version"]]
                work = {"offers": d.get("offers") or [], "proven": target.get("proven") or {}}
                if change is not None:
                    change(work)
                else:
                    work["proven"] = d.get("proven") or {}
                target["proven"] = d["proven"] = work["proven"]
            else:
                change(clean)
                for k in ("offers", "proven"):
                    if k in clean:
                        d[k] = clean[k]
        else:
            clean = {k: v for k, v in d.items() if not k.startswith("_")}
        with open(path + ".part", "w") as f:
            json.dump(clean, f, indent=2)
            f.write("\n")
        os.replace(path + ".part", path)
