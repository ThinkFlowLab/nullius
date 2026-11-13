"""The container backend: each unit's agent, or capability, in a container of its
own, and its relay in another beside it. This module shells out to the
engine's program (:data:`DOCKER`) and imports nothing outside the standard
library. That is ``docker``, or ``podman`` on a machine without Docker or
where ``docker`` is podman's own (:func:`engine`). Podman needs no Docker API
socket and no settings of its own for Nullius.

**One base image** (``nullius/base:<hash>``), built once and named by the
hash of its Dockerfile, so a change to it builds a new one beside the old:
Ubuntu 24.04 with git, python3, curl, CA certificates, tmux (Terminus's
terminal), Node 22 from its official tarball, checked by its sha256, and uv
from its official image, pinned by digest. A unit's image is chosen by
:func:`image_for`: the base image, or the benchmark's environment the unit
asks for - a layer on the base, its Dockerfile in the benchmark's folder,
named by the hash of what it is built from
(``nullius/env-<benchmark>-<name>:<hash>``) and built the first time a unit
uses it (:func:`env_image`).

**The same absolute paths inside.** A unit's workspace and home are mounted
read-write; the agent's install and its definition's folder read-only, and
each file a unit's stdio MCP server is started from, alone (:func:`mcp_mounts`)
- never this repository's ``app/`` (it holds the keys' file; ``Unit.wrap``
refuses it) nor the benchmark's folder or upstream (their data holds the
answers). All where they are on this machine, so every path a
definition's templates rendered (``{home}``, ``{install}``, ``{here}``) holds
inside unchanged. A uv environment's interpreter is in the install
(``.python``), installed with it in the base image (:func:`install_argv`).
The container runs as this machine's user, with no capabilities and no way to gain
privileges, so what it writes in the workspace is the user's to read and
remove. That is ``--user uid:gid``, with ``--userns=keep-id`` on rootless
podman, which maps that uid to this user, or ``--user 0:0`` on rootless
Docker, where only a container's root is this machine's user. On a Docker with
``userns-remap``, a container that uses this machine's user's files also
runs in this machine's user namespace (:func:`_userns`). On podman it
runs with no SELinux label (:func:`_security_opt`), as on Docker's default
daemon.

**The relay is a sidecar, and the agent's only way out** but for the public
web proxy of an environment that declares ``network``. Per unit, an
internal network (``docker network create --internal``: no route off it); a
relay container (``python3 -m nullius.standard.relay serve``) on the default
bridge *and* on that network as ``relay``; the agent's container on the
internal network alone. Its model endpoint is ``http://relay:8080/<tag>/v1``
and nothing else answers it. The real key reaches the relay's container
only, as an owner-only file under :func:`key_root`, mounted read-only and
removed from this machine once the relay listens: never on an argument list
(``ps``) or in the container's environment (``docker inspect``). The relay
publishes its port on 127.0.0.1 so the driver (on the machine for
agent/capability runs) reaches the judge's route and a capability
benchmark's reader. Its record folder is mounted read-write, so the host
reads each call as the local relay's are read. On podman the default bridge
is Nullius's own (:data:`BRIDGE`, :func:`way_out`). A model endpoint on this
machine's loopback is reached by the relays alone (:class:`Loopback`):
through a forwarder on the default bridge's gateway, or, on a rootless
engine or podman, a door container on the engine's default network.

**A licence server is one more door.** A unit whose environment needs a
licence of kind ``server`` (``host:port``) gets a forwarder beside its relay
(``forward.py``, mounted alone into the base image): on the default bridge
(``BRIDGE`` on podman), and on the unit's network under the server's own name, listening on its
port and passing every connection to that one address. The agent reaches
the server by the address it was given; nothing else is opened. A server
named by an IPv4 address is reached as ``licence-server-<i>`` instead, a
network alias being a name; ``host.docker.internal`` is the machine the run
is on. On a rootless engine or podman on Linux, a server at
``host.docker.internal`` is reached through a :class:`SocketDoor` instead, as ``licence-server-<i>``
on the unit's network alone. A licence file is mounted read-only, alone, at
the path its environment declares (``wrap``'s ``binds``).

**A benchmark's MCP server on the machine is one door more.** A server the
benchmark declares in ``mcp_servers`` runs on this machine, in the driver's
environment, behind a Unix socket (``mcp_machine.Listener``). The unit gets
a forwarder for it (``doors``): on the unit's internal network *only* - no
bridge, no route anywhere - under the alias the agent's entry names, the
socket alone mounted into it, passing every connection to that socket and
nothing else. On macOS, where the socket cannot be mounted, the forwarder
is on the default bridge too and goes to a loopback TCP bridge here, which
wants the door's nonce first, given as a key file (``forward.py``). The
agent starts ``mcp_bridge.py`` (mounted alone, read-only), which joins its
stdio to that door.

**An own image** (an environment with ``"image": "own"``) is the other way
round: the task's image, built from its own Dockerfile - its own ``FROM``,
its own folder as the build context (:func:`own_image`) - or pulled by its
digest, and the agent added to it at run time, never built in. A unit's
:class:`Box` is that image kept running (``sh -c 'sleep'``) for the whole
unit: the agent is started in it with ``docker exec``, so it works on the
container's own state in the image's working directory, and the benchmark's
check runs in it after the agent ends - what the agent left running stopped
first, its files copied in only then, and a way out, when it asks for one,
through a proxy to public addresses only (:meth:`Box.settle`,
:meth:`Box.online`). What
the agent needs that the image may not have is mounted read-only under
``/opt/nullius``: Node, unpacked once from the same pinned tarball the base
image is built from (:func:`node_runtime`), and a standalone Python
(:func:`python_runtime`); its install, its definition and its home are
mounted at their own paths, as on the base. A runtime that cannot start in
the image - a glibc build in a musl one - is found before the agent starts
(:meth:`Box.runtime_problem`), and the unit is not run.

Everything a unit starts carries the label ``nullius.job=<job>`` and is
removed when the unit ends, after an error or an interrupt too; :func:`sweep`
removes whatever a killed run left behind.
Docker Original setup drivers are handled by ``setup_container.py``.
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import socketserver
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from secrets import compare_digest, token_hex
from typing import Dict, Iterable, List, Optional, Tuple

from nullius import paths
from nullius.standard import definitions, install

def _runs_podman(path: str) -> bool:
    """Whether the program at ``path`` is podman under another name: a link to
    it, or a script that runs it (``podman-docker``'s ``docker``)."""
    if os.path.basename(os.path.realpath(path)) == "podman":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and re.search(rb"^\s*exec\s+\S*podman\b", head, re.M) is not None


def engine() -> str:
    """``podman`` when it is on the PATH and ``docker`` is not, or is podman
    under that name; else ``docker``."""
    docker, podman = shutil.which("docker"), shutil.which("podman")
    return "podman" if podman and (not docker or _runs_podman(docker)) else "docker"


DOCKER = engine()                          # the engine's program, chosen once
LABEL = "nullius.job"
RELAY_PORT = 8080
RELAY_ALIAS = "relay"
FORWARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forward.py")
FORWARD_IN = "/opt/nullius/forward.py"     # where the forwarder's one file is mounted in its container
FORWARD_NONCE_IN = "/opt/nullius/forward-nonce"    # a door's nonce's one file, in its forwarder's container
EGRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "egress.py")
EGRESS_IN = "/opt/nullius/egress.py"       # the check's proxy's one file, in its container
EGRESS_PORT = 3128
EGRESS_ALIAS = "egress"
BRIDGE = "nullius-bridge"                  # podman: the network a container with a way out starts on (way_out)
RELAY_KEY_IN = "/opt/nullius/relay-key"    # the key's one file, in the relay's container
KEY_STALE = 600                            # older than any start can take: a killed run left it
IMAGE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# uv and PYTHON_VERSION move together: this uv must know that Python
UV_VERSION = "0.13.5"
UV_DIGEST = "sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff"    # its index: every platform
NODE_VERSION = "22.23.3"
NODE_SHA256 = {
    "x64": "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de",
    "arm64": "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f",
}
_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}

# FROM in full: podman resolves a short name by this machine's registries
DOCKERFILE = """\
FROM docker.io/library/ubuntu:24.04
COPY --from=ghcr.io/astral-sh/uv:{uv_version}@{uv_digest} /uv /uvx /usr/local/bin/
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git python3 ca-certificates curl tmux xz-utils \\
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v{node}/node-v{node}-linux-{arch}.tar.xz \\
 && echo "{sha}  /tmp/node.tar.xz" | sha256sum -c - \\
 && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \\
 && rm /tmp/node.tar.xz
ENV PATH={path}
"""

# What a unit's process holds of this machine's environment that names a
# place on this machine: meaningless, or wrong, inside.
HOST_ONLY = ("TMPDIR", "NVM_DIR", "NODE_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
# What the engine's program keeps of this process's environment, to find the
# daemon, or podman its storage and settings. Not a key among them, though
# CONTAINER_HOST may name an ssh URL with a password: it is never on an argument list.
CLIENT_KEEP = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
               "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF",
               "CONTAINERS_REGISTRIES_CONF", "REGISTRY_AUTH_FILE", "CONTAINER_HOST", "CONTAINER_CONNECTION")


class Unavailable(Exception):
    """The engine is missing, not usable by this user, or failed at something."""


def podman() -> bool:
    return DOCKER == "podman"


def _docker(args: List[str], *, env: Optional[dict] = None, input: Optional[str] = None, timeout: float = 120,
            check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run([DOCKER] + args, env=env if env is not None else client_env(), input=input, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        raise Unavailable(f"{DOCKER} is not installed on this machine")
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{DOCKER} {args[0]} did not answer in {int(timeout)} s")
    if check and r.returncode != 0:
        raise Unavailable(f"{DOCKER} {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def client_env(extra: Optional[dict] = None) -> dict:
    """The engine program's environment: what it needs to find the daemon, or
    podman its storage and settings (``CLIENT_KEEP``), and the values of the
    variables a ``-e NAME`` passes on."""
    env = {**_context(), **{k: os.environ[k] for k in CLIENT_KEEP if k in os.environ}}
    env.setdefault("PATH", os.defpath)
    env.update(extra or {})
    return env


@functools.cache
def _context() -> Dict[str, str]:
    """The daemon that ``docker context use`` selected, as the variables that
    name it. The user's ``~/.docker`` keeps that selection, and a unit or a
    driver has a home of its own. Only the endpoint goes on: the registry
    logins in ``~/.docker`` do not. Read once per process."""
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        return {}
    try:
        r = subprocess.run([DOCKER, "context", "inspect"], capture_output=True, text=True, timeout=30)
        c = json.loads(r.stdout)[0]
        ep = c["Endpoints"]["docker"]
    except (OSError, subprocess.SubprocessError, ValueError, LookupError):
        return {}
    if c.get("Name") == "default":
        return {}
    env = {"DOCKER_HOST": ep["Host"]}
    tls = bool(c.get("TLSMaterial", {}).get("docker"))
    if tls:
        env["DOCKER_CERT_PATH"] = os.path.join(c["Storage"]["TLSPath"], "docker")
    # shortcut: docker reads a ca.pem from DOCKER_CERT_PATH, so a skip-verify context with a client certificate and no CA is not reached
    if ep.get("SkipTLSVerify"):
        env["DOCKER_TLS"] = "1"
    elif tls:
        env["DOCKER_TLS_VERIFY"] = "1"
    return env


def unusable() -> Optional[str]:
    """Why the container backend cannot run here, or None when it can."""
    try:
        r = _docker(["version", "--format", "{{.Server.Version}}"], timeout=30, check=False)
    except Unavailable as e:
        return str(e)
    if r.returncode != 0:
        said = (r.stderr or r.stdout).strip().splitlines()
        hint = (" (add this user to the docker group, then log in again)"
                if not podman() and "permission denied" in r.stderr.lower() else "")
        if not podman() and shutil.which("podman"):     # docker wins (engine), so say podman is there
            hint += ". podman is on this machine: Nullius uses it when docker is not on the PATH"
        return f"{DOCKER} is installed but not usable by this user: " + (said[-1][:200] if said else f"exit {r.returncode}") + hint
    if podman():
        try:
            net = _docker(["info", "--format", "{{.Host.NetworkBackend}}"], timeout=30, check=False).stdout.strip()
        except Unavailable as e:
            return str(e)
        if net != "netavark":
            return (f"podman's network backend is {net or 'unknown'}: Nullius needs netavark"
                    ' (podman 4.7 or later, network_backend = "netavark")')
    return None


def dockerfile(machine: Optional[str] = None) -> str:
    arch = _ARCH.get((machine or platform.machine()).lower())
    if not arch:
        raise Unavailable(f"no Node build pinned for {machine or platform.machine()}")
    return DOCKERFILE.format(uv_version=UV_VERSION, uv_digest=UV_DIGEST, node=NODE_VERSION, arch=arch, sha=NODE_SHA256[arch], path=IMAGE_PATH)


def image_tag(text: str) -> str:
    return "nullius/base:" + hashlib.sha256(text.encode()).hexdigest()[:12]


_built: Dict[str, bool] = {}
# the own images this process pulled or built itself - never one that was there already, which is the user's
_fetched: set = set()


def base_tag() -> str:
    """The base image's tag on this machine, built or not."""
    return image_tag(dockerfile())


def present(tag: str) -> bool:
    """Whether this machine has an image by that tag: a run says it is there
    already, or that it builds it now (``progress``)."""
    if _built.get(tag) or _docker(["image", "inspect", tag], check=False).returncode == 0:
        _built[tag] = True
    return bool(_built.get(tag))


def base_image(log: Optional[str] = None) -> str:
    """The base image's tag, building it first when this machine has not."""
    text = dockerfile()
    tag = image_tag(text)
    if present(tag):
        return tag
    r = _docker(["build", "--label", "nullius.base=1", "-t", tag, "-"], input=text, timeout=1800, check=False)
    if log:
        with open(log, "a") as f:
            f.write(r.stdout + r.stderr)
    if r.returncode != 0:
        raise Unavailable(f"building {tag}: {(r.stderr or r.stdout).strip()[-300:]}")
    _built[tag] = True
    return tag


def env_context(bench: dict, name: str, base: str) -> Tuple[str, Dict[str, bytes]]:
    """An environment's Dockerfile as it is built - ``FROM <base>``, then the
    benchmark's text - and its build context: the files it lists, by their
    path in the benchmark's folder. Nothing else of that folder goes in."""
    e = bench["environments"][name]
    with open(os.path.join(bench["_here"], e["dockerfile"])) as f:
        text = f"FROM {base}\n" + f.read()
    files = {}
    for rel in sorted(e.get("files") or []):
        with open(os.path.join(bench["_here"], rel), "rb") as f:
            files[rel] = f.read()
    return text, files


def env_tag(bench: dict, name: str, base: str) -> str:
    """``nullius/env-<benchmark>-<name>:<hash>``: the sha256 of the Dockerfile
    as built (its base's tag included, so a new base is a new layer) and of
    each file it copies, path and bytes."""
    text, files = env_context(bench, name, base)
    h = hashlib.sha256(text.encode())
    for rel, data in files.items():
        h.update(b"\0" + rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return f"nullius/env-{bench['key']}-{name}:{h.hexdigest()[:12]}"


_locks: Dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def env_image(bench: dict, name: str, log_dir: Optional[str] = None) -> str:
    """An environment's image, built the first time a unit asks for it - so a
    run builds only the environments its units use - and kept by its tag
    after. Two units asking at once wait for one build. Its build output is
    ``env-<name>.log`` in ``log_dir`` (the job's ``.work``). A secret is never
    given to a build: an image is shared, a secret is the run's."""
    base = base_image(os.path.join(log_dir, "image.log") if log_dir else None)
    tag = env_tag(bench, name, base)
    with _locks_lock:
        lock = _locks.setdefault(tag, threading.Lock())
    with lock:
        if _built.get(tag) or _docker(["image", "inspect", tag], check=False).returncode == 0:
            _built[tag] = True
            return tag
        text, files = env_context(bench, name, base)
        ctx = tempfile.mkdtemp(prefix="nullius-env-")
        try:
            with open(os.path.join(ctx, "Dockerfile"), "w") as f:
                f.write(text)
            for rel, data in files.items():
                os.makedirs(os.path.dirname(os.path.join(ctx, rel)), exist_ok=True)
                with open(os.path.join(ctx, rel), "wb") as f:
                    f.write(data)
            r = _docker(["build", "--label", f"nullius.env={bench['key']}/{name}", "-t", tag, ctx], timeout=3600, check=False)
        finally:
            shutil.rmtree(ctx, ignore_errors=True)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"env-{name}.log"), "a") as f:
                f.write(f"# {tag}\n" + r.stdout + r.stderr)
        if r.returncode != 0:
            raise Unavailable(f"building the environment {name} ({tag}): {(r.stderr or r.stdout).strip()[-300:]}")
        _built[tag] = True
        return tag


def image_for(bench: dict, unit_setup: Optional[dict], dataset: Optional[dict] = None,
              log_dir: Optional[str] = None) -> str:
    """The image a unit runs in: the environment its setup names, else its
    dataset's default (``definitions.environment_of``), built on first use;
    the base image when it has none. An environment the benchmark does not
    declare raises ValueError, as does an own image, which the unit's setup
    names itself (:func:`own_image`)."""
    name, e = definitions.environment_of(bench, unit_setup, dataset)
    if name is None:
        return base_image(os.path.join(log_dir, "image.log") if log_dir else None)
    if definitions.own_image(e):
        raise ValueError(f"environment {name!r} is an own image: the unit's setup names it")
    return env_image(bench, name, log_dir)


# -- names, mounts, argument lists -----------------------------------------------
def slug(job: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", job).strip("-.") or "job"
    if len(s) > 80:
        s = s[:72] + "-" + hashlib.sha256(job.encode()).hexdigest()[:7]
    return s


def names(job: str, tag: str) -> Dict[str, str]:
    base = f"nullius-{slug(job)}-{tag}"
    return {"network": base, "agent": base, "relay": base + "-relay"}


def mcp_mounts(servers: Iterable[dict], trial: str) -> Tuple[List[str], List[str]]:
    """What a unit's stdio MCP servers need inside the agent's container,
    which starts them: each file their command, arguments or environment
    name (read-only, the file alone - never the folder it sits in, which may
    hold the benchmark's data and answers), and each folder inside the trial
    they will write to (read-write: a server's call log). (rw, ro_files)."""
    rw, ro = [], []
    trial = os.path.abspath(trial)
    for s in servers or []:
        if not isinstance(s, dict):
            continue
        env = s.get("env") or {}
        values = [s.get("command")] + list(s.get("args") or []) + \
            [v for v in (env.values() if isinstance(env, dict) else (e.get("value") for e in env if isinstance(e, dict)))]
        for v in values:
            if not isinstance(v, str) or not os.path.isabs(v):
                continue
            v = os.path.abspath(v)
            if os.path.isfile(v) and not v.startswith(trial + os.sep):
                ro.append(v)
            elif v.startswith(trial + os.sep):
                d = v if os.path.isdir(v) else os.path.dirname(v)
                os.makedirs(d, exist_ok=True)
                rw.append(d)
    return rw, ro


def mounts(rw: Iterable[str], ro: Iterable[str], files: Iterable[str] = ()) -> List[Tuple[str, bool]]:
    """Each folder once, at its own path; a read-only folder already inside
    another read-only one is not mounted twice. ``files``: single files,
    read-only, unless a folder mounted already holds them."""
    out: List[Tuple[str, bool]] = []
    seen = set()
    for p in rw:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            out.append((p, False))
    ros = sorted({os.path.abspath(p) for p in ro if p and os.path.isdir(p)} - seen, key=len)
    kept: List[str] = []
    for p in ros:
        if not any(p == k or p.startswith(k + os.sep) for k in kept):
            kept.append(p)
            out.append((p, True))
    held = [p for p, _ in out]
    for f in sorted({os.path.abspath(f) for f in files if f and os.path.isfile(f)}):
        if not any(f.startswith(h + os.sep) for h in held):
            out.append((f, True))
    return out


_security: Optional[str] = None


def _security_options() -> str:
    """The engine's security options (``docker info``), or podman's answer in
    their terms (``name=rootless``): podman has no ``SecurityOptions``. The
    first answer is kept. An engine that does not answer gives none and is
    asked again."""
    global _security
    if _security is None:
        asked = "{{if .Host.Security.Rootless}}name=rootless{{end}}" if podman() else "{{.SecurityOptions}}"
        try:
            r = _docker(["info", "--format", asked], check=False, timeout=30)
        except Unavailable:
            return ""
        if r.returncode != 0:
            return ""
        _security = r.stdout
    return _security


def rootless() -> bool:
    """Whether the engine is rootless (Docker or podman): its uid 0 is this
    machine's user, every other uid a sub-uid."""
    return "name=rootless" in _security_options()


def userns_remap() -> bool:
    """Whether a rootful Docker remaps its containers' users (``userns-remap``):
    every uid inside, root too, is then a sub-uid of this machine."""
    return "name=userns" in _security_options()


def _keep_id() -> List[str]:
    """``--userns=keep-id`` on rootless podman, which maps :func:`_user`'s uid
    to this machine's user, as on rootful Docker. Rootless Docker cannot map
    a uid, and runs its containers as root, which is this user there."""
    return ["--userns=keep-id"] if podman() and rootless() else []


def _userns() -> List[str]:
    """The user namespace of a container that uses this machine's user's files:
    :func:`_keep_id` on podman, ``--userns=host`` on a Docker with
    ``userns-remap``, where a container's uids are otherwise sub-uids that
    cannot use those files."""
    if podman():
        return _keep_id()
    return ["--userns=host"] if userns_remap() else []


def make_bridge() -> None:
    """On podman, make ``BRIDGE`` when it is missing (``--ignore``: asked each
    time, so a network removed between units is made again): a bridge with
    DNS, as podman's default network has DNS off. Docker has its default bridge."""
    if podman():
        _docker(["network", "create", "--ignore", "--label", "nullius.bridge=1", BRIDGE])


def way_out() -> List[str]:
    """The network of a container that reaches out, or that the relays reach
    (the relay, a licence server's forwarder, the check's proxy, the door to a
    model server): the engine's default, or ``BRIDGE`` on podman
    (:func:`make_bridge`). A rootless podman container starts on pasta, which
    cannot join a network after it starts."""
    return ["--network", BRIDGE] if podman() else []


def _user() -> str:
    """The ``--user`` that gives a container's files to this machine's user:
    ``0:0`` on rootless Docker, ``uid:gid`` otherwise (:func:`_keep_id`)."""
    return "0:0" if rootless() and not podman() else f"{os.getuid()}:{os.getgid()}"


def _security_opt() -> List[str]:
    """Every container's ``--security-opt``: no way to gain privileges, and on
    podman no SELinux label. Docker's default daemon (``selinux-enabled`` off)
    gives none, so a container reads the folders it mounts, which are never
    relabelled (``:z`` would relabel the user's). Labels are future work."""
    return ["--security-opt", "no-new-privileges", *(["--security-opt", "label=disable"] if podman() else [])]


def by_value(env: Dict[str, str]) -> Dict[str, str]:
    """What of a unit's environment podman itself reads (``CLIENT_KEEP``:
    ``HOME``, the XDG folders...) but ``PATH``: on podman passed by value
    (``-e NAME=value``), so the podman program keeps this machine's own and
    finds its images. Nothing on Docker."""
    return {k: v for k, v in env.items() if k in CLIENT_KEEP and k != "PATH"} if podman() else {}


def program_env(env: Dict[str, str]) -> dict:
    """The engine program's environment for a unit's process given ``env``:
    the values of what goes by name (``-e NAME``), never ``PATH`` nor
    :func:`by_value`'s."""
    given = by_value(env)
    return client_env({k: v for k, v in env.items() if k != "PATH" and k not in given})


def env_args(env: Dict[str, str], path: str) -> Tuple[List[str], List[str]]:
    """A unit's process's ``-e`` words: ``PATH``, the container's and no
    secret, and :func:`by_value`'s by value; then the rest by name, the value
    in the engine program's own environment (:func:`program_env`), never on
    an argument list."""
    given = by_value(env)
    return (["-e", "PATH=" + path, *[w for k, v in sorted(given.items()) for w in ("-e", f"{k}={v}")]],
            [w for k in sorted(set(env) - {"PATH"} - set(given)) for w in ("-e", k)])


def run_argv(image: str, cmd: List[str], *, name: str, network: str, job: str, workdir: str,
             mounted: List[Tuple[str, bool]], env: Dict[str, str], path: str, interactive: bool = True,
             binds: Iterable[Tuple[str, str]] = ()) -> List[str]:
    """``docker run`` for a unit's agent or capability, its environment as
    :func:`env_args` gives it. ``binds``: (a file on this machine, its path
    inside), each read-only - a licence file where its environment says the
    software looks for it."""
    values, named = env_args(env, path)
    a = [DOCKER, "run", "--rm", "--name", name, "--network", network, "--label", f"{LABEL}={job}",
         *_userns(), "--user", _user(), "--cap-drop", "ALL", *_security_opt(), "--init",
         "-w", workdir, *values]
    if interactive:
        a.insert(2, "-i")
    for p, ro in mounted:
        a += ["-v", f"{p}:{p}:{'ro' if ro else 'rw'}"]
    for src, dest in binds:
        a += ["-v", f"{src}:{dest}:ro"]
    return a + named + [image] + list(cmd)


def plain_only(root: str) -> List[str]:
    """Remove from ``root`` everything that is not a regular file or a folder -
    links above all - and return what went. What the agent could write (a
    folder mounted into its container, what a check collects out of it) is
    then read and written on this machine, where a link it planted would
    reach any file the user can (verifier/reward.txt -> ~/.bashrc)."""
    gone = []
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + files:
            path = os.path.join(here, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode) or (stat.S_ISDIR(mode) and name in dirs):
                continue
            os.unlink(path)
            gone.append(os.path.relpath(path, root))
            if name in dirs:
                dirs.remove(name)
    return gone


CHECK_IMAGE = "NULLIUS_CHECK_IMAGE"     # driver checks: the unit's image, or the base image for a local setup
CHECK_ENGINE = "NULLIUS_CHECK_ENGINE"   # beside it on podman: check_start as podman runs from the driver, JSON
_XDG = (("XDG_DATA_HOME", ".local/share"), ("XDG_CONFIG_HOME", ".config"))     # where podman keeps its images and settings


def check_start() -> List[str]:
    """A check's container command up to its mounts (:func:`check_argv`): no
    network, no capabilities, as this machine's user."""
    return [DOCKER, "run", "--rm", "--network", "none", *_userns(), "--user", _user(), "--cap-drop", "ALL",
            *_security_opt()]


def check_env() -> dict:
    """What a driver, which runs with a home of its own, gets to run
    :func:`check_argv` on the same engine as the units. On Docker: the
    ``DOCKER_*`` variables of :func:`client_env`. On podman: the variables of
    ``CLIENT_KEEP`` (``CONTAINER_HOST``, ``REGISTRY_AUTH_FILE``...) except ``PATH``, ``HOME``,
    ``XDG_DATA_HOME`` and ``XDG_CONFIG_HOME``, and ``CHECK_ENGINE``. That is
    :func:`check_start` behind ``env``, which gives podman this user's XDG data
    and config folders. The driver's own processes do not use these folders."""
    if not podman():
        return {k: v for k, v in client_env().items() if k.startswith("DOCKER")}
    home = os.path.expanduser("~")
    xdg = [f"{k}={os.environ.get(k) or os.path.join(home, d)}" for k, d in _XDG]
    return {**{k: os.environ[k] for k in CLIENT_KEEP if k not in ("PATH", "HOME", *dict(_XDG)) and k in os.environ},
            CHECK_ENGINE: json.dumps(["env", *xdg, *check_start()])}


def check_argv(workdir: str, here: List[str], inside: List[str]) -> List[str]:
    """With CHECK_IMAGE, run in a throwaway container with no network or
    capabilities and only the workspace mounted. Otherwise return ``here``."""
    image = os.environ.get(CHECK_IMAGE)
    if not image:
        return list(here)
    start = json.loads(os.environ[CHECK_ENGINE]) if os.environ.get(CHECK_ENGINE) else check_start()
    return [*start, "-v", f"{workdir}:{workdir}:rw", "-w", workdir, image, *inside]


# where this machine gets its packages from, passed by name: a proxy's URL may hold its password
INSTALL_KEEP = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
                "UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_PYTHON_INSTALL_MIRROR",
                "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "npm_config_registry", "NPM_CONFIG_REGISTRY", "HF_ENDPOINT")


def install_argv(image: str, where: str, cmd: List[str], env: Optional[dict] = None) -> List[str]:
    """``docker run`` for an install step (``install.packages``' ``run_in``):
    in the base image, as this machine's user, with the install folder alone
    mounted, at its own path. The home and npm's and uv's caches are inside
    it, removed after (``install.SCRATCH``). uv's Python is inside it too
    (``.python``), and kept: the environment links to it, and the agent's
    container mounts the folder. ``env``: the step's own variables. This
    machine's proxy and package indexes (``INSTALL_KEEP``) are passed by name.
    It has the network - it downloads the packages - and nothing of a run: no
    key, no unit."""
    home, npm, uv = (f"{where}/{s}" for s in install.SCRATCH)
    env = {"HOME": home, "npm_config_cache": npm, "UV_CACHE_DIR": uv,
           "UV_PYTHON_INSTALL_DIR": f"{where}/.python", "UV_PYTHON_DOWNLOADS": "automatic", **(env or {})}
    return [DOCKER, "run", "--rm", "--label", "nullius.install=1", *_userns(), "--user", _user(), "--cap-drop", "ALL",
            *_security_opt(), "-v", f"{where}:{where}:rw", "-w", where,
            *[w for k, v in env.items() for w in ("-e", f"{k}={v}")],
            *[w for k in INSTALL_KEEP if k in os.environ for w in ("-e", k)], image] + list(cmd)


def relay_argv(image: str, *, name: str, job: str, app: str, record: str, key_file: str, base: str,
               model: Optional[str], routes: Dict[str, str], prices: Optional[dict] = None, side_models=(),
               key_header: str = "authorization", hosts: Optional[Dict[str, str]] = None,
               ledger: Optional[str] = None, serving: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """``docker run`` for a unit's relay: on the default bridge (its way to the
    provider), its port published on 127.0.0.1 only. The key is never given:
    ``key_file``, its file, is mounted read-only at ``RELAY_KEY_IN`` with
    ``--mount``, which fails when the engine cannot see it (``-v`` would mount
    an empty folder). ``hosts``: names it resolves
    (``--add-host``), the machine's own for an endpoint on its loopback.
    ``ledger``: the file of the run's spending cap, its folder mounted so
    this relay books its calls where every other relay of the job does.
    ``serving``: by model, the serving providers that may answer it, as the
    unit's own relay is given them (``relay.Relay``)."""
    a = [DOCKER, "run", "-d", "--name", name, "--label", f"{LABEL}={job}", *way_out(), *_userns(), "--user", _user(),
         "--cap-drop", "ALL", *_security_opt(), "--init",
         *[w for n, ip in sorted((hosts or {}).items()) for w in ("--add-host", f"{n}:{ip}")],
         "-p", f"127.0.0.1::{RELAY_PORT}", "-v", f"{app}:{app}:ro", "-v", f"{record}:{record}:rw",
         *(["-v", f"{os.path.dirname(ledger)}:{os.path.dirname(ledger)}:rw"] if ledger else []),
         "--mount", f"type=bind,source={key_file},target={RELAY_KEY_IN},readonly", "-w", app,
         "-e", "PYTHONPATH=" + app, "-e", "PYTHONDONTWRITEBYTECODE=1",
         image, "python3", "-m", "nullius.standard.relay", "serve", "--listen", f"0.0.0.0:{RELAY_PORT}",
         "--base", base, "--record", record, "--key-file", RELAY_KEY_IN, "--public", f"http://{RELAY_ALIAS}:{RELAY_PORT}"]
    if model:
        a += ["--model", model]
    for tag, m in sorted(routes.items()):
        a += ["--route", f"{tag}={m}"]
    if prices:
        a += ["--prices", json.dumps(prices, separators=(",", ":"))]
    if ledger:
        a += ["--ledger", ledger]
    for m in side_models:
        a += ["--side-model", m]
    # the pins of the models this relay sends; the run's other models (an option's embeddings) are another relay's
    sends = {model} | set(routes.values()) | {str(s).partition("=")[2] or str(s) for s in side_models}
    for m, slugs in sorted((serving or {}).items()):
        if m in sends:
            a += ["--served-by", f"{m}={','.join(slugs)}"]
    if key_header != "authorization":
        a += ["--key-header", key_header]
    return a


_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def on_loopback(base: str) -> bool:
    """Whether a model endpoint is on this machine's loopback (a local model
    server: ``http://localhost:11434/v1``), which a container cannot reach as
    written - there, 127.0.0.1 is the container itself."""
    host = (urllib.parse.urlsplit(base or "").hostname or "").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def bridge_gateway() -> str:
    """This machine's address on docker's default bridge, or ""."""
    r = _docker(["network", "inspect", "bridge", "-f", "{{range .IPAM.Config}}{{.Gateway}} {{end}}"], check=False)
    return next((w for w in r.stdout.split() if _IPV4.match(w)), "")


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sk in (src, dst):
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _pass_each(server: socket.socket, target: Tuple[str, int]) -> None:
    """Pass each connection ``server`` accepts to ``target``, until it is closed."""
    while True:
        try:
            inside, _ = server.accept()
        except OSError:
            return
        try:
            outside = socket.create_connection(target, timeout=10)
            outside.settimeout(None)
        except OSError:
            inside.close()
            continue
        for a, b in ((inside, outside), (outside, inside)):
            threading.Thread(target=_pump, args=(a, b), daemon=True).start()


def _close(server: socket.socket) -> None:
    """Close a listening socket. The shutdown wakes the thread blocked in its
    ``accept``, which a close alone does not do on Linux."""
    try:
        server.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    server.close()


def use_socket_doors() -> bool:
    """Whether a server on this machine's loopback is reached through a
    :class:`SocketDoor`: on a rootless engine on Linux, or on podman there,
    whose bridge has no address on this machine until a container joins it.
    Elsewhere the engine runs in a virtual machine, which this machine's
    sockets may not reach."""
    return (rootless() or podman()) and platform.system() == "Linux"


class SocketDoor:
    """A Unix socket in a folder of its own that passes each connection to one
    ``host:port`` here: a rootless engine's door container mounts it to reach
    this machine's loopback. ``stop`` closes it and removes the folder."""

    def __init__(self, host: str, port: int):
        self.dir = tempfile.mkdtemp(prefix="nullius-door-")
        self.path, self.target = os.path.join(self.dir, "s"), (host, port)     # short: a socket path has ~108 bytes
        self._server: Optional[socket.socket] = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server.bind(self.path)
            self._server.listen(64)
        except OSError:
            self.stop()
            raise
        threading.Thread(target=_pass_each, args=(self._server, self.target), daemon=True).start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            _close(server)
        shutil.rmtree(self.dir, ignore_errors=True)


class Loopback:
    """An endpoint on this machine's loopback, as the relays' containers reach
    it: ``base`` and the ``hosts`` they are given. On Linux, a forwarder for
    the run, listening on the default bridge's gateway alone - the address a
    container there has for this machine, reached by the relays and by no
    agent, whose networks are internal - and passing each connection to the
    endpoint; the relays call it ``host.docker.internal``. Where the bridge
    is not on this machine and the engine runs as root (Docker Desktop's
    virtual machine), that name already reaches its loopback: only the name
    changes. On Linux, a rootless engine's gateway is in its own network
    namespace, and podman's bridge has no address here until a container
    joins it, so a :class:`SocketDoor` (``door``) passes each connection
    instead, and one door container for the run reaches it, on the engine's
    default network as the relays are. The relays call it
    ``host.docker.internal`` there too, at the door's address, on port 8000."""

    NAME = "host.docker.internal"
    DOOR_PORT = 8000                      # not the endpoint's: below 1024 needs a capability on podman

    def __init__(self, base: str, *, image: str = "", job: str = ""):
        self.target = urllib.parse.urlsplit(base)
        self.base, self.hosts = base, {}
        self.image, self.job = image, job       # the door container's (use_socket_doors)
        self._server: Optional[socket.socket] = None
        self.door: Optional[SocketDoor] = None
        self.container: Optional[str] = None

    def _url(self, port: Optional[int]) -> str:
        t = self.target
        netloc = (t.netloc.rpartition("@")[0] + "@" if "@" in t.netloc else "") + self.NAME + (f":{port}" if port else "")
        return urllib.parse.urlunsplit((t.scheme, netloc, t.path, t.query, t.fragment))

    def start(self) -> "Loopback":
        if use_socket_doors():
            self.door = SocketDoor(*self._endpoint())
            self.container = f"nullius-{slug(self.job)}-model"
            try:
                make_bridge()
                _docker(door_argv(self.image, name=self.container, job=self.job, port=self.DOOR_PORT,
                                  socket_path=self.door.path)[1:],
                        env=client_env({"NULLIUS_FORWARD_TO": "unix:" + self.door.path}))
                wait_listening(self.container, "the door to the model server")
                ip = _docker(["inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                              self.container]).stdout.split()
                if not ip:
                    raise Unavailable("the door to the model server has no address on the default network")
            except Exception:
                self.stop()
                raise
            self.base, self.hosts = self._url(self.DOOR_PORT), {self.NAME: ip[0]}
            return self
        gateway = bridge_gateway()
        try:
            if not gateway:
                raise OSError("no bridge gateway")
            self._server = socket.create_server((gateway, 0))
        except OSError:
            self.base, self.hosts = self._url(self.target.port), {self.NAME: "host-gateway"}
            return self
        self.base, self.hosts = self._url(self._server.getsockname()[1]), {self.NAME: gateway}
        threading.Thread(target=_pass_each, args=(self._server, self._endpoint()), daemon=True).start()
        return self

    def _endpoint(self) -> Tuple[str, int]:
        t = self.target
        return t.hostname, t.port or (443 if t.scheme == "https" else 80)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            _close(server)
        name, self.container = self.container, None
        if name is not None:
            try:
                _docker(["rm", "-f", name], check=False, timeout=60)
            except Unavailable:
                pass
        if self.door is not None:
            self.door.stop()


def server_alias(address: str, i: int = 1) -> Tuple[str, str, int]:
    """(the name the agent reaches it by, host, port) for a licence server
    ``host:port``: its own name, or ``licence-server-<i>`` for an IPv4
    address, which cannot be a network alias."""
    host, _, port = address.strip().rpartition(":")
    return (f"licence-server-{i}" if _IPV4.match(host) else host), host, int(port)


def forward_argv(image: str, *, name: str, job: str, port: int, nonce_file: Optional[str] = None) -> List[str]:
    """``docker run`` for a licence server's forwarder: on the default bridge
    (``BRIDGE`` on podman), its one file mounted, listening on the server's
    port. Where it forwards is named, never given: ``-e NULLIUS_FORWARD_TO``.
    ``host.docker.internal`` is the machine the run is on, for a server there
    (where :func:`use_socket_doors`, ``Unit._forward`` gives such a server a
    door instead). A macOS door mounts its nonce read-only."""
    mount = ["--mount", f"type=bind,source={nonce_file},target={FORWARD_NONCE_IN},readonly"] if nonce_file else []
    return [DOCKER, "run", "-d", "--name", name, "--label", f"{LABEL}={job}", *way_out(), *_keep_id(), "--user", _user(),
            "--cap-drop", "ALL", *_security_opt(), "--init",
            "--add-host", "host.docker.internal:host-gateway", "-v", f"{FORWARD}:{FORWARD_IN}:ro", *mount,
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "NULLIUS_FORWARD_TO",
            image, "python3", FORWARD_IN, "--listen", f"0.0.0.0:{port}",
            *(["--nonce-file", FORWARD_NONCE_IN] if nonce_file else [])]


def door_argv(image: str, *, name: str, job: str, port: int, socket_path: str,
              network: Optional[str] = None, alias: Optional[str] = None) -> List[str]:
    """``docker run`` for a door to a server on this machine (``mcp_machine``,
    a :class:`SocketDoor`): on one internal network alone, as ``alias``, or
    without ``network`` on the default one (``BRIDGE`` on podman: a
    :class:`Loopback`'s), its one
    file and the server's socket mounted, each alone and read-only. Where it
    forwards is named, never given: ``-e NULLIUS_FORWARD_TO``
    (``unix:<socket>``)."""
    return [DOCKER, "run", "-d", "--name", name, "--label", f"{LABEL}={job}", *_userns(), "--user", _user(),
            "--cap-drop", "ALL", *_security_opt(), "--init",
            *(["--network", network, "--network-alias", alias] if network else way_out()),
            "-v", f"{FORWARD}:{FORWARD_IN}:ro", "-v", f"{socket_path}:{socket_path}:ro",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "NULLIUS_FORWARD_TO",
            image, "python3", FORWARD_IN, "--listen", f"0.0.0.0:{port}"]


def proxy_env(direct: Iterable[str] = ()) -> Dict[str, str]:
    """The variables that send HTTP(S) through the ``egress`` proxy; this
    machine's loopback and ``direct`` (the unit's relay, doors and servers) are
    reached as they are."""
    proxy = f"http://{EGRESS_ALIAS}:{EGRESS_PORT}"
    local = ",".join(dict.fromkeys(["localhost", "127.0.0.1", "::1", *direct]))
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": local, "no_proxy": local}


def egress_argv(image: str, *, name: str, job: str) -> List[str]:
    """``docker run`` for an own image's check's proxy (``egress.py``): on the
    default bridge (``BRIDGE`` on podman), its one file mounted, nothing else given."""
    return [DOCKER, "run", "-d", "--name", name, "--label", f"{LABEL}={job}", *way_out(), *_keep_id(), "--user", _user(),
            "--cap-drop", "ALL", *_security_opt(), "--init",
            "-v", f"{EGRESS}:{EGRESS_IN}:ro", "-e", "PYTHONDONTWRITEBYTECODE=1",
            image, "python3", EGRESS_IN, "--listen", f"0.0.0.0:{EGRESS_PORT}"]


def wait_listening(name: str, what: str, timeout: float = 30):
    """Until a sidecar says ``listening`` on its output; Unavailable when it
    ends first, or does not in ``timeout``."""
    end = time.time() + timeout
    while time.time() < end:
        r = _docker(["logs", name], check=False)
        if '"listening"' in r.stdout:
            return
        state = _docker(["inspect", "-f", "{{.State.Running}}", name], check=False).stdout.strip()
        if state == "false":
            raise Unavailable(f"{what} ended: " + (r.stdout + r.stderr).strip()[-300:])
        time.sleep(0.2)
    raise Unavailable(f"{what} did not listen in {int(timeout)} s")


class Unit:
    """One unit's network and relay; what wraps its agent's command.

    It answers as a relay does - ``url`` (the host's way in, for the driver),
    ``calls``, ``stop`` - and ``inside``, the address the agent is given."""

    def __init__(self, job: str, tag: str, record_dir: str, *, image: str, app: str, base: str, key: str,
                 model: Optional[str] = None, routes: Optional[Dict[str, str]] = None, prices: Optional[dict] = None,
                 side_models=(), key_header: str = "authorization", servers: Iterable[str] = (),
                 doors: Iterable[Tuple[str, int, str]] = (), hosts: Optional[Dict[str, str]] = None,
                 ledger: Optional[str] = None, network_probe: Optional[str] = None, probe_now: bool = True,
                 serving: Optional[Dict[str, List[str]]] = None):
        self.job, self.tag, self.dir, self.image, self.app = job, tag, record_dir, image, app
        self.ledger = ledger                     # the file of the run's spending cap, when it has one
        self.serving = dict(serving or {})       # by model, the serving providers that may answer it
        self.servers = list(servers)             # licence servers, host:port each: a forwarder apiece
        self.doors = list(doors)                 # servers on this machine: (alias, port, socket), a door apiece
        self.side_models, self.key_header = tuple(side_models), key_header
        self.base, self.key, self.model, self.routes, self.prices = base, key, model, dict(routes or {}), prices
        self.hosts = dict(hosts or {})
        self.names = names(job, tag)
        self.host: Optional[str] = None
        self.socket_bridges = []
        self.started: List[str] = []           # containers to remove
        self.network = False
        self.key_dir: Optional[str] = None     # its secret files' folder, until what reads them listens
        self.network_probe = network_probe
        self.probe_now = probe_now               # the run's first unit checks the proxy route; the rest trust it
        self.network_env = {}
        self.socket_doors: List[SocketDoor] = []
        os.makedirs(record_dir, exist_ok=True)

    def start(self) -> "Unit":
        try:
            make_bridge()
            _docker(["network", "create", "--internal", "--label", f"{LABEL}={self.job}", self.names["network"]])
            self.network = True
            argv = relay_argv(self.image, name=self.names["relay"], job=self.job, app=self.app, record=self.dir,
                              key_file=self._key_file(), base=self.base, model=self.model, routes=self.routes,
                              prices=self.prices, side_models=self.side_models, key_header=self.key_header,
                              hosts=self.hosts, ledger=self.ledger, serving=self.serving)
            self.started.append(self.names["relay"])
            _docker(argv[1:])
            _docker(["network", "connect", "--alias", RELAY_ALIAS, self.names["network"], self.names["relay"]])
            self._wait(self.names["relay"], "the relay's container")
            self._forget_key()
            port = _docker(["port", self.names["relay"], f"{RELAY_PORT}/tcp"]).stdout.strip().splitlines()[0]
            self.host = "http://" + port.replace("0.0.0.0", "127.0.0.1")
            self.reach = {}
            for i, address in enumerate(self.servers, 1):
                self.reach[address] = self._forward(i, address)
            for i, (alias, port, sock) in enumerate(self.doors, 1):
                self._door(f"{self.names['agent']}-mcp{i}", alias, port, sock, "the door to the benchmark's MCP server")
            if self.network_probe:
                self._online()
        except Exception:
            self.stop()
            raise
        return self

    def direct(self) -> List[str]:
        """What the agent reaches without the public web proxy: its relay, its
        doors and its licence servers' forwarders."""
        return [RELAY_ALIAS, *[alias for alias, _, _ in self.doors],
                *[address.rsplit(":", 1)[0] for address in getattr(self, "reach", {}).values()]]

    def _key_file(self, name: str = "key", text: Optional[str] = None) -> str:
        """Write the key, or ``text``, to an owner-only file in the unit's key
        folder, a new one under :func:`key_root` when it has none, and return
        its path. Not the record folder: it is kept."""
        if not self.key_dir:
            self.key_dir = key_folder(self.job)
        return secret_file(os.path.join(self.key_dir, name), self.key if text is None else text)

    def _forget_key(self):
        if self.key_dir:
            shutil.rmtree(self.key_dir, ignore_errors=True)
            self.key_dir = None

    def _online(self):
        """Opt-in public HTTP(S), with private destinations blocked by the proxy."""
        name = self.names["agent"] + "-egress"
        self.started.append(name)
        _docker(egress_argv(self.image, name=name, job=self.job)[1:])
        _docker(["network", "connect", "--alias", EGRESS_ALIAS, self.names["network"], name])
        self._wait(name, "the agent's public web proxy")
        self.network_env = proxy_env(self.direct())
        if not self.probe_now:
            return
        # Check the actual network path, without mounts or credentials.
        code = ("import sys,urllib.request; "
                "r=urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers={'User-Agent': 'Nullius-connectivity-check'}), timeout=10); "
                "assert 200 <= r.status < 400")
        probe = self.names["agent"] + "-network-check"
        # Docker startup competes with other units for daemon/network locks.
        # Its deadline must not consume the HTTP check's own execution budget.
        # Track the name before starting so cancellation also removes it.
        self.started.append(probe)
        args = ["run", "-d", "--name", probe, "--label", f"{LABEL}={self.job}",
                "--init", "--network", self.names["network"],
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        for key in self.network_env:
            args += ["-e", key]
        try:
            _docker(args + [self.image, "python3", "-c", code, self.network_probe],
                    env=client_env(self.network_env), timeout=120)
            result = _docker(["wait", probe], timeout=30)
            if result.stdout.strip() != "0":
                logs = _docker(["logs", probe], check=False, timeout=10)
                raise Unavailable((logs.stderr or logs.stdout).strip()[-500:] or "probe exited unsuccessfully")
        except Unavailable as error:
            raise Unavailable(f"the public HTTPS connectivity check: {error}") from error

    def _forward(self, i: int, address: str) -> str:
        """A licence server's forwarder, started and on the unit's network
        under the server's name: the address the agent reaches it by. It joins
        that network once it listens, after it resolves the server's name. One
        at ``host.docker.internal``, where :func:`use_socket_doors`, is a door
        to a :class:`SocketDoor` instead, as ``licence-server-<i>``."""
        alias, host, port = server_alias(address, i)
        name = f"{self.names['agent']}-lic{i}"
        if use_socket_doors() and host == Loopback.NAME:
            door = SocketDoor("127.0.0.1", port)
            self.socket_doors.append(door)
            alias = f"licence-server-{i}"
            self._door(name, alias, port, door.path, "the licence server's forwarder")
            return f"{alias}:{port}"
        self.started.append(name)
        _docker(forward_argv(self.image, name=name, job=self.job, port=port)[1:],
                env=client_env({"NULLIUS_FORWARD_TO": f"{host}:{port}"}))
        self._wait(name, "the licence server's forwarder")
        _docker(["network", "connect", "--alias", alias, self.names["network"], name])
        return f"{alias}:{port}"

    def _door(self, name: str, alias: str, port: int, sock: str, what: str) -> None:
        """A door to a server on this machine, on the unit's network alone. On
        macOS, a forwarder to a loopback TCP bridge here that serves a
        connection only after the door's nonce line, given as a key file."""
        self.started.append(name)
        if platform.system() == "Darwin":
            # Docker Desktop cannot connect to host Unix sockets through a
            # file mount. Expose only this socket via a loopback TCP bridge.
            from nullius.standard import forward
            nonce = token_hex(32)
            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    self.request.settimeout(5)
                    expected = (nonce + "\n").encode()
                    received = b""
                    try:
                        while len(received) < len(expected):
                            chunk = self.request.recv(len(expected) - len(received))
                            if not chunk:
                                return
                            received += chunk
                    except OSError:
                        return
                    if not compare_digest(received, expected):
                        return
                    self.request.settimeout(None)
                    forward._serve(self.request, [(socket.AF_UNIX, sock)])
            bridge = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
            bridge.daemon_threads = True
            self.socket_bridges.append(bridge)
            threading.Thread(target=bridge.serve_forever, daemon=True).start()
            _docker(forward_argv(self.image, name=name, job=self.job, port=port,
                                 nonce_file=self._key_file(name, nonce))[1:],
                    env=client_env({"NULLIUS_FORWARD_TO": f"host.docker.internal:{bridge.server_address[1]}"}))
            _docker(["network", "connect", "--alias", alias, self.names["network"], name])
        else:
            _docker(door_argv(self.image, name=name, job=self.job, network=self.names["network"], alias=alias,
                              port=port, socket_path=sock)[1:],
                    env=client_env({"NULLIUS_FORWARD_TO": "unix:" + sock}))
        self._wait(name, what)
        self._forget_key()                       # read before it listens

    def _wait(self, name: str, what: str, timeout: float = 30):
        wait_listening(name, what, timeout)

    def url(self, tag: str, model: Optional[str] = None) -> str:
        """The host's way to a route. A route's model is fixed when the relay
        starts (``routes``); asking for another is an error, not a silent switch."""
        if model and self.routes.get(tag) != model:
            raise ValueError(f"route {tag} was not started as {model}")
        return f"{self.host}/{tag}/v1"

    def inside(self, tag: str) -> str:
        return f"http://{RELAY_ALIAS}:{RELAY_PORT}/{tag}/v1"

    def calls(self, tag: str) -> List[dict]:
        try:
            with open(os.path.join(self.dir, tag + ".jsonl")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def wrap(self, cmd: List[str], env: dict, workdir: str, mounted: List[Tuple[str, bool]], *,
             image: Optional[str] = None, interactive: bool = True,
             binds: Iterable[Tuple[str, str]] = ()) -> Tuple[List[str], dict]:
        """A command and its environment turned into ``docker run`` and the
        engine program's environment (:func:`env_args`, :func:`program_env`);
        ``binds``, licence files mounted read-only where they are declared."""
        app = os.path.abspath(self.app)
        binds = [(os.path.abspath(src), dest) for src, dest in binds]
        for p, _ in mounted:                     # the application's folder holds the keys' file: never it, nor a folder above it
            if app == p or app.startswith(p.rstrip(os.sep) + os.sep):
                raise ValueError(f"{p} would show the agent this application's folder and its keys")
        for src, _ in binds:                     # a licence comes from its store, never from in here
            if src.startswith(app + os.sep):
                raise ValueError(f"{src} is in this application's folder: a licence is never mounted from there")
        inside = {k: v for k, v in {**env, **self.network_env}.items() if k not in HOST_ONLY}
        argv = run_argv(image or self.image, cmd, name=self.names["agent"], network=self.names["network"], job=self.job,
                        workdir=workdir, mounted=mounted, env=inside, path=inside.get("PATH") or IMAGE_PATH,
                        interactive=interactive, binds=binds)
        self.started.append(self.names["agent"])
        return argv, program_env(inside)

    def stop(self):
        """Remove the unit's containers and network, and the key's file if the
        relay has not read it; the relay's log is kept beside its record first."""
        self._forget_key()
        if self.names["relay"] in self.started:
            try:
                r = _docker(["logs", self.names["relay"]], check=False, timeout=30)
                with open(os.path.join(self.dir, "relay.log"), "w") as f:
                    f.write(r.stdout + r.stderr)
            except (Unavailable, OSError):
                pass
        if self.network_probe and self.names["agent"] + "-egress" in self.started:
            try:
                r = _docker(["logs", self.names["agent"] + "-egress"], check=False, timeout=30)
                with open(os.path.join(self.dir, "egress.log"), "w") as f:
                    f.write(r.stdout + r.stderr)
            except (Unavailable, OSError):
                pass
        for n in dict.fromkeys(self.started):
            try:
                _docker(["rm", "-f", n], check=False, timeout=60)
            except Unavailable:
                pass
        self.started = []
        for bridge in self.socket_bridges:
            bridge.shutdown()
            bridge.server_close()
        self.socket_bridges = []

        for d in self.socket_doors:
            d.stop()
        self.socket_doors = []
        if self.network:
            try:
                _docker(["network", "rm", self.names["network"]], check=False, timeout=60)
            except Unavailable:
                pass
            self.network = False


# -- an own image: the task's container, the agent added at run time -------------
RUNTIMES = "/opt/nullius"                # where an own image is given what the agent runs on
PYTHON_VERSION = "3.12.14"               # the standalone Python an agent started as python3 runs on there; moves with UV_VERSION
_PULL = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
IMAGE_SPEC = ("build", "pull", "memory_mb", "cpus")
STEP = ("copy", "write", "run", "collect", "seconds", "network", "keep_processes")
KEEP_ALIVE = ["sh", "-c", "while :; do sleep 3600; done"]
INIT = "/usr/sbin/docker-init"           # what --init mounts: docker's, not the unit's

# The box's processes, read from /proc with the shell's own builtins only - a
# task's image may have no ps, no pkill, not even tr - and with no subshell,
# so the script never sees a child of its own. A line of /proc/<pid>/stat is
# "pid (comm) state ppid ...", comm free to hold spaces and parentheses: what
# follows its last ") " is the state, then the parent.
_SCAN = r"""
    line=
    read -r line 2>/dev/null < "$d/stat" || [ -n "$line" ] || continue
    set -- ${line##*) }
"""
# The keep-alive: the one child of PID 1 (docker's init) right after the start.
KEEPER = r"""for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ "$p" = "$$" ] && continue""" + _SCAN + r"""    [ "$2" = 1 ] && echo "$p"
done
"""
# Every process but PID 1, the keep-alive ($1) and its own sleep, and this
# script: stopped first (SIGSTOP - a stopped process cannot fork), then
# killed, round after round until a scan finds none; a zombie is init's to
# reap and is passed over. Says each it stopped; exit 1 with what is left when
# twenty rounds were not enough.
SETTLE = r"""keep=$1; n=0
while :; do
  left=
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    case " 1 $keep $$ " in *" $p "*) continue ;; esac""" + _SCAN + r"""    case $1 in Z|X|x) continue ;; esac
    [ "$2" = "$keep" ] && continue
    left="$left $p"
  done
  [ -z "$left" ] && exit 0
  n=$((n+1))
  if [ "$n" -gt 20 ]; then echo "still running:$left"; exit 1; fi
  for p in $left; do kill -STOP "$p" 2>/dev/null; done
  for p in $left; do
    c=; read -r c 2>/dev/null < "/proc/$p/comm"
    echo "stopped $p $c"
    kill -KILL "$p" 2>/dev/null
  done
  sleep 1
done
"""


def runtimes_dir() -> str:
    return os.path.join(install.HOME, "runtimes")


def within(root: str, rel) -> str:
    """A path a driver named relative to its upstream (or its own folder),
    resolved, and refused unless it stays inside: a driver names the task's
    files, never anything else of this machine."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise ValueError(f"{rel!r}: a path relative to the benchmark's upstream")
    r = os.path.realpath(root)
    p = os.path.realpath(os.path.join(r, rel))
    if not p.startswith(r + os.sep):
        raise ValueError(f"{rel} is outside the benchmark's upstream")
    if not os.path.exists(p):
        raise ValueError(f"{rel} is not in the benchmark's upstream")
    return p


def spec_problems(spec) -> List[str]:
    """What is wrong with a unit's ``image`` (its setup's): ``build``, a
    folder holding a Dockerfile, or ``pull``, a reference pinned by digest;
    ``memory_mb`` and ``cpus``, the limits the task asks for."""
    if not isinstance(spec, dict):
        return ["image: {build: <folder in the upstream>} or {pull: <name>@sha256:<digest>}"]
    bad = [f"image.{k}: not a field ({', '.join(IMAGE_SPEC)})" for k in spec if k not in IMAGE_SPEC]
    if ("build" in spec) == ("pull" in spec):
        bad.append("image: one of build (a folder with a Dockerfile) or pull (a reference by digest)")
    if "pull" in spec and not _PULL.match(str(spec["pull"])):
        bad.append(f"image.pull: {spec['pull']!r} is not pinned by digest (<name>@sha256:<64 hex>)")
    for k in ("memory_mb", "cpus"):
        if k in spec and (not isinstance(spec[k], (int, float)) or isinstance(spec[k], bool) or spec[k] <= 0):
            bad.append(f"image.{k}: a positive number")
    return bad


def task_env_problems(env, reserved: Iterable[str] = ()) -> List[str]:
    """Task variables for an agent in its own image, never its model configuration."""
    if not isinstance(env, dict):
        return ["env: an object of variable names and string values"]
    bad = []
    for name, value in env.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            bad.append("env: variable names use capitals, digits and underscores")
            continue
        if (name in reserved or name.startswith(("NULLIUS_", "DOCKER_"))
                or re.search(r"(^|_)(API|AUTH|KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|BASE_URL|ENDPOINT|MODEL)(_|$)", name)):
            bad.append(f"env.{name}: model settings and credentials belong to the run")
        if not isinstance(value, str) or "\0" in value:
            bad.append(f"env.{name}: a string without NUL")
        elif name == "PATH" and any(not part.startswith("/") for part in value.split(":")):
            bad.append("env.PATH: absolute folders separated by ':'")
    return bad


def step_problems(step, *, collect: bool) -> List[str]:
    """What is wrong with a step run in a unit's box (``verify``, ``solve``):
    ``copy`` {relative path in the upstream: absolute path inside}, ``write``
    {absolute path inside: its text - a file the driver has by content, an
    eval script or a patch the dataset carries}, ``run``
    (argv), ``collect`` (a folder inside, ``verify`` only), ``seconds``,
    ``network`` (true: a way out to public addresses, :meth:`Box.online`),
    ``keep_processes`` (``verify`` only - true: what the agent left running is
    not stopped before the check, :meth:`Box.settle`)."""
    if not isinstance(step, dict):
        return ["a step: {copy?, write?, run, collect?, seconds?, network?, keep_processes?}"]
    bad = [f"{k}: not a field ({', '.join(STEP)})" for k in step
           if k not in STEP or (k in ("collect", "keep_processes") and not collect)]
    run = step.get("run")
    if not isinstance(run, list) or not run or not all(isinstance(w, str) and w for w in run):
        bad.append("run: the command, a list of words")
    copy = step.get("copy") or {}
    if not isinstance(copy, dict) or not all(isinstance(d, str) and d.startswith("/") for d in copy.values()):
        bad.append("copy: {path in the upstream: absolute path inside}")
    write = step.get("write") or {}
    if not isinstance(write, dict) or not all(isinstance(d, str) and d.startswith("/") and isinstance(t, str)
                                              for d, t in write.items()):
        bad.append("write: {absolute path inside: the file's text}")
    if "collect" in step and not (isinstance(step["collect"], str) and step["collect"].startswith("/")):
        bad.append("collect: an absolute path inside")
    if "seconds" in step and (not isinstance(step["seconds"], (int, float)) or step["seconds"] <= 0):
        bad.append("seconds: a positive number")
    if "network" in step and not isinstance(step["network"], bool):
        bad.append("network: true or false")
    if "keep_processes" in step and not isinstance(step["keep_processes"], bool):
        bad.append("keep_processes: true or false")
    return bad


def own_context(path: str) -> Dict[str, str]:
    """An own image's build context: every file of its folder, by its path
    there, and its sha256."""
    out = {}
    for d, dirs, files in os.walk(path):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            out[os.path.relpath(p, path)] = "link:" + os.readlink(p) if os.path.islink(p) else install.sha256_of(p)
    return out


def own_tag(bench: dict, rel: str, path: str) -> str:
    """``nullius/own-<benchmark>-<folder>:<hash>``: the sha256 of every file
    the build reads, so the same folder is the same image, and a changed one a
    new image beside it. Its ``FROM`` is the task's, as published."""
    h = hashlib.sha256()
    for f, digest in own_context(path).items():
        h.update(f.encode() + b"\0" + digest.encode() + b"\0")
    word = re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")[:60] or "task"
    return f"nullius/own-{bench['key']}-{word}:{h.hexdigest()[:12]}"


def own_image(bench: dict, spec: dict, root: str, log_dir: Optional[str] = None) -> str:
    """A unit's own image: ``pull``ed by its digest, or built from the folder
    ``build`` names in the benchmark's upstream (``root``) - the task's own
    Dockerfile, its own ``FROM``, its folder the whole build context - the
    first time a unit asks for it, and kept by its tag after. Two units
    asking at once wait for one build. No secret is given to a build."""
    if spec.get("pull"):
        ref = spec["pull"]
        if not present(ref):
            r = _docker(["pull", ref], timeout=3600, check=False)
            if r.returncode != 0:
                raise Unavailable(f"pulling {ref}: {(r.stderr or r.stdout).strip()[-300:]}")
            _built[ref] = True
            _fetched.add(ref)
        return ref
    path = within(root, spec["build"])
    if not os.path.isfile(os.path.join(path, "Dockerfile")):
        raise Unavailable(f"{spec['build']} holds no Dockerfile")
    tag = own_tag(bench, spec["build"], path)
    with _locks_lock:
        lock = _locks.setdefault(tag, threading.Lock())
    with lock:
        if present(tag):
            return tag
        # --force-rm: a build that fails (a task's Dockerfile fetching what is gone) leaves no container behind
        r = _docker(["build", "--force-rm", "--label", f"nullius.own={bench['key']}", "-t", tag, path], timeout=3600, check=False)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "own-" + tag.split(":")[0].split("/")[-1] + ".log"), "a") as f:
                f.write(f"# {tag}\n" + r.stdout + r.stderr)
        if r.returncode != 0:
            raise Unavailable(f"building {spec['build']} ({tag}): {(r.stderr or r.stdout).strip()[-300:]}")
        _built[tag] = True
        _fetched.add(tag)
        return tag


def fetched(image: str) -> bool:
    """Whether this process pulled or built ``image`` (``own_image``): an image
    that was there before it is the user's, never removed here. ``_built``
    is not the answer: it also remembers every image found present."""
    return image in _fetched


def drop_image(image: str) -> None:
    """Remove an own image this process fetched, once no unit needs it: one
    per SWE-bench Verified instance, kept, filled docker's disk part-way
    through a run (the upstream harness removes its instance images too)."""
    if image in _fetched:
        _fetched.discard(image)
        _built.pop(image, None)
        _docker(["rmi", image], timeout=600, check=False)


def image_config(image: str) -> dict:
    """What an image says of itself that a box keeps: its working directory,
    its user and its PATH."""
    c = json.loads(_docker(["image", "inspect", "-f", "{{json .Config}}", image]).stdout or "null") or {}
    variables = dict(e.split("=", 1) for e in c.get("Env") or [] if "=" in e)       # the image's, not this machine's
    return {"workdir": c.get("WorkingDir") or "/", "user": c.get("User") or "", "path": variables.get("PATH") or IMAGE_PATH}


def node_runtime(machine: Optional[str] = None) -> str:
    """Node for an own image: the official tarball the base image is built
    from, checked by the same sha256, unpacked once into
    ``<STANDARD_DIR>/runtimes/node-v<version>-linux-<arch>``. A glibc build:
    a musl image cannot run it (:meth:`Box.runtime_problem`)."""
    arch = _ARCH.get((machine or platform.machine()).lower())
    if not arch:
        raise Unavailable(f"no Node build pinned for {machine or platform.machine()}")
    name = f"node-v{NODE_VERSION}-linux-{arch}"
    where = os.path.join(runtimes_dir(), name)
    if install.ready(where):
        return where
    with install._locked(where):
        if install.ready(where):
            return where
        tmp = tempfile.mkdtemp(prefix="node-", dir=runtimes_dir())
        try:
            tarball = os.path.join(tmp, name + ".tar.xz")
            with urllib.request.urlopen(f"https://nodejs.org/dist/v{NODE_VERSION}/{name}.tar.xz", timeout=60) as r, \
                    open(tarball, "wb") as f:
                shutil.copyfileobj(r, f)
            if install.sha256_of(tarball) != NODE_SHA256[arch]:
                raise Unavailable(f"{name}.tar.xz is not the one pinned (sha256)")
            with tarfile.open(tarball, "r:xz") as t:
                t.extractall(tmp, filter="data")
            if os.path.isdir(where):
                shutil.rmtree(where)
            os.rename(os.path.join(tmp, name), where)
            open(os.path.join(where, install.MARK), "w").close()
        except OSError as e:
            raise Unavailable(f"fetching Node {NODE_VERSION}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return where


def python_runtime() -> str:
    """A standalone Python for an own image (python-build-standalone, by
    ``uv python install``, which checks it by its own pinned digests),
    installed once into ``<STANDARD_DIR>/runtimes/python-<version>``: the
    interpreter's folder. Installed in the base image, so it is a glibc
    build, as Node's is, whatever this machine's libc."""
    where = os.path.join(runtimes_dir(), f"python-{PYTHON_VERSION}")
    found = lambda: sorted(glob.glob(os.path.join(where, f"cpython-{PYTHON_VERSION}-linux-*-gnu")))
    if install.ready(where) and found():
        return found()[0]
    with install._locked(where):
        if not (install.ready(where) and found()):
            os.makedirs(where, exist_ok=True)
            try:
                r = subprocess.run(install_argv(base_image(), where, ["uv", "python", "install", PYTHON_VERSION,
                                                                     "--install-dir", where, "--no-bin"]),
                                   env=client_env({k: os.environ[k] for k in INSTALL_KEEP if k in os.environ}),
                                   capture_output=True, text=True, timeout=1800)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise Unavailable(f"installing Python {PYTHON_VERSION}: {e}")
            finally:
                install.remove_scratch(where)
            if r.returncode != 0 or not found():
                raise Unavailable(f"installing Python {PYTHON_VERSION}: {(r.stderr or r.stdout).strip()[-300:]}")
            open(os.path.join(where, install.MARK), "w").close()
    return found()[0]


def runtime_mounts(names: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    """Each runtime an agent needs in an own image: name -> (its folder on
    this machine, where it is mounted inside)."""
    out = {}
    for n in names:
        out[n] = ({"node": node_runtime, "python": python_runtime}[n](), f"{RUNTIMES}/{n}")
    return out


def bare_python(word: str) -> bool:
    """``python3`` or ``python`` with no environment of its own - not an
    environment's interpreter (``{install}/.venv/bin/python``), which must
    stay itself or the agent runs without its packages."""
    return os.path.basename(word) in ("python3", "python") and "/.venv/" not in word


def in_runtime(cmd: List[str], runtimes: Dict[str, Tuple[str, str]]) -> List[str]:
    """An agent's start command as an own image runs it: ``python3`` or
    ``node`` - the word, or a script's ``#!/usr/bin/env`` line - becomes the
    runtime under ``/opt/nullius``, so the agent runs on what it was installed
    for while the image's own ``python3`` and ``node`` stay the task's."""
    cmd = list(cmd)
    first = os.path.basename(cmd[0]) if cmd else ""
    if cmd and bare_python(cmd[0]) and "python" in runtimes:
        return [runtimes["python"][1] + "/bin/python3"] + cmd[1:]
    if first == "node" and "node" in runtimes:
        return [runtimes["node"][1] + "/bin/node"] + cmd[1:]
    try:
        with open(cmd[0], "rb") as f:
            line = f.readline(200).decode("utf-8", "replace").split()
    except (OSError, IndexError):
        return cmd
    if line[:1] == ["#!/usr/bin/env"] and len(line) > 1:
        word = {"node": "node", "python3": "python", "python": "python"}.get(line[1])
        if word in runtimes:
            return [runtimes[word][1] + ("/bin/node" if word == "node" else "/bin/python3")] + cmd
    return cmd


class Box:
    """A unit's own container: the task's image, kept running for the whole
    unit so the agent, a control's reference solution and the benchmark's
    check all act on one container's state, one after the other.

    It runs as the image's own user, as the task was written for - usually
    root, which its tasks need (a package to install, a file in ``/etc``) -
    with docker's default capabilities, no way to gain more
    (``no-new-privileges``), and on the unit's internal network (or one of its
    own, for a control): the relay, when there is one, is its only way out.
    The folders it writes on this machine (the agent's home) are given back to
    this machine's user before it is removed (``give_back``).

    **Before the check, nothing of the agent runs** (:meth:`settle`). A
    process the agent started and left - a server, a loop, a watcher - would
    otherwise still run while the check's files are copied in and while the
    check has a way out: free to rewrite the tests, or to reach what the
    check's network reaches. So every process but docker's init and the
    keep-alive is stopped, as root in the box, and the box is confirmed empty
    of them, before anything is copied in. A task about a service the agent
    must leave running opts out with its ``verify`` step's ``keep_processes``;
    what that keeps runs on beside the tests, which is the task's trade.

    **The check's way out is a proxy, not a bridge** (:meth:`online`). The
    box stays on its internal network; a proxy container joins it as
    ``egress`` (``egress.py``) and reaches public addresses only - never
    link-local (cloud metadata), private ranges, the shared range some clouds
    keep their metadata in, nor the machine itself through the docker
    bridge. A benchmark may also grant it for the agent's work with
    ``internet: true``; it is removed before the check. Each phase is told
    of its proxy through ``HTTP_PROXY`` and ``HTTPS_PROXY``. It needs no root
    on the machine, so every host runs the check the same way; ``egress.py`` says why that beat firewall rules.

    **The agent's mounts stay for the check.** A running container's mounts
    cannot be changed, and taking its home away would mean a second container
    for the check, without the state the agent left in this one. They are
    harmless there: the install, definition and runtimes are read-only, the
    home is the trial's own folder and nothing else of this machine, the check
    does not read them, and nothing of the agent is left running to use them
    (unless the task keeps its processes)."""

    def __init__(self, job: str, tag: str, image: str, *, workdir: str, path: str = IMAGE_PATH,
                 network: Optional[str] = None, mounted: Iterable[Tuple[str, bool]] = (),
                 runtimes: Optional[Dict[str, Tuple[str, str]]] = None, memory_mb=None, cpus=None,
                 app: Optional[str] = None, env: Optional[dict] = None):
        n = names(job, tag)
        self.job, self.image, self.workdir, self.path = job, image, workdir, path
        self.env = dict(env or {})
        self.name = n["agent"]
        self.net = network or n["network"]
        self.own_net = network is None
        self.mounted, self.runtimes = list(mounted), dict(runtimes or {})
        self.memory_mb, self.cpus, self.app = memory_mb, cpus, app
        self.up = False
        self.keep: Optional[str] = None          # the keep-alive's PID inside, found at the start
        self.egress: Optional[str] = None        # the check's proxy container, once online

    def start(self) -> "Box":
        if self.app:
            app = os.path.abspath(self.app)
            for p, _ in self.mounted:            # the application's folder holds the keys' file
                if app == p or app.startswith(p.rstrip(os.sep) + os.sep):
                    raise ValueError(f"{p} would show the agent this application's folder and its keys")
        try:
            if self.own_net:
                _docker(["network", "create", "--internal", "--label", f"{LABEL}={self.job}", self.net])
            # in the machine's user namespace on podman (its root stays this user on rootless podman whatever
            # containers.conf sets: keep-id makes it a sub-uid) and on a Docker with userns-remap
            a = ["run", "-d", "--name", self.name, "--label", f"{LABEL}={self.job}", "--network", self.net, "--init",
                 *_security_opt(), "-w", self.workdir, *(["--userns=host"] if podman() else _userns())]
            if self.memory_mb:
                a += ["--memory", f"{int(self.memory_mb)}m"]
            if self.cpus:
                a += ["--cpus", str(self.cpus)]
            for p, ro in self.mounted:
                a += ["-v", f"{p}:{p}:{'ro' if ro else 'rw'}"]
            for host, inside in self.runtimes.values():
                a += ["-v", f"{host}:{inside}:ro"]
            self.up = True
            _docker(a + ["--entrypoint", KEEP_ALIVE[0], self.image] + KEEP_ALIVE[1:], timeout=300)
            self.keep = self.keeper()
        except Exception:
            self.stop()
            raise
        return self

    def exec_argv(self, cmd: List[str], env: Dict[str, str], path: str, *, interactive: bool = True,
                  workdir: Optional[str] = None) -> List[str]:
        """``docker exec`` of a command in the box, its environment as
        :func:`env_args` gives it."""
        values, named = env_args(env, path)
        return [DOCKER, "exec", *(["-i"] if interactive else []), "-w", workdir or self.workdir,
                *values, *named, self.name, *cmd]

    def run(self, cmd: List[str], seconds: float, *, user: Optional[str] = None, env: Optional[dict] = None) -> dict:
        """A command run in the box, to its end or ``seconds``: {exit, output,
        timed_out}. ``exit`` is None when it timed out."""
        a = ["exec", "-w", self.workdir] + (["-u", user] if user else [])
        for k in sorted(env or {}):
            a += ["-e", k]
        try:
            r = subprocess.run([DOCKER] + a + [self.name] + list(cmd), env=client_env(env), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=seconds)
        except subprocess.TimeoutExpired as e:
            out = e.stdout or b""
            return {"exit": None, "output": out.decode("utf-8", "replace") if isinstance(out, bytes) else out, "timed_out": True}
        except FileNotFoundError:
            raise Unavailable(f"{DOCKER} is not installed on this machine")
        return {"exit": r.returncode, "output": r.stdout.decode("utf-8", "replace"), "timed_out": False}

    def copy_in(self, src: str, dest: str):
        """A file or a folder of this machine into the box at ``dest``: a
        folder's contents become ``dest``'s."""
        self.run(["mkdir", "-p", dest if os.path.isdir(src) else os.path.dirname(dest) or "/"], 60, user="0")
        _docker(["cp", src + ("/." if os.path.isdir(src) else ""), f"{self.name}:{dest}"], timeout=600)

    def write_in(self, dest: str, text: str):
        """A file given by its text into the box at ``dest`` (a step's
        ``write``): written here first, then copied in as ``copy_in`` does."""
        tmp = tempfile.mkdtemp(prefix="write-")
        try:
            path = os.path.join(tmp, os.path.basename(dest) or "file")
            with open(path, "w") as f:
                f.write(text)
            self.copy_in(path, dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def copy_out(self, src: str, dest: str) -> bool:
        """The contents of a folder in the box into ``dest`` on this machine;
        False when the box has no such folder."""
        os.makedirs(dest, exist_ok=True)
        ok = _docker(["cp", f"{self.name}:{src.rstrip('/')}/.", dest], timeout=600, check=False).returncode == 0
        plain_only(dest)                 # docker cp keeps the box's links, absolute targets included
        return ok

    def keeper(self, timeout: float = 10) -> str:
        """The keep-alive's PID inside: the one child of docker's init, read
        right after the start, before anything else runs in the box."""
        end = time.time() + timeout
        while True:
            r = self.run(["sh", "-c", KEEPER], 30, user="0")
            pids = r["output"].split()
            if r["exit"] == 0 and len(pids) == 1 and pids[0].isdigit():
                return pids[0]
            if time.time() > end:
                raise Unavailable("the container's keep-alive was not found: " + r["output"].strip()[-200:])
            time.sleep(0.2)

    @staticmethod
    def settle_argv(keep: str) -> List[str]:
        """The command that stops every process but init and the keep-alive."""
        return ["sh", "-c", SETTLE, "settle", keep]

    def settle(self) -> List[str]:
        """Every process the agent (or a control's solution) left in the box
        stopped, as root: "<pid> <name>" for each. Unavailable when some are
        still running after it - the check is not run beside them."""
        if not self.keep:
            raise Unavailable("the container's keep-alive is not known: its processes cannot be told apart")
        r = self.run(self.settle_argv(self.keep), 120, user="0")
        said = r["output"].splitlines()
        if r["exit"] != 0:
            raise Unavailable("what the agent left running could not be stopped: " + (said[-1] if said else
                              ("timed out" if r["timed_out"] else f"exit {r['exit']}")))
        return [l[len("stopped "):] for l in said if l.startswith("stopped ")]

    def online(self) -> Dict[str, str]:
        """A way out for the check: its proxy (``egress.py``, in the base image)
        started on the default bridge (``BRIDGE`` on podman) and joined to the box's network as
        ``egress``. The box itself stays on its internal network. The
        variables the check is run with, which name the proxy - and leave
        the box's own services (``localhost``) to be reached directly."""
        if self.egress is None:
            image = base_image()
            name = self.name + "-egress"
            self.egress = name
            make_bridge()
            _docker(egress_argv(image, name=name, job=self.job)[1:])
            _docker(["network", "connect", "--alias", EGRESS_ALIAS, self.net, name])
            wait_listening(name, "the task's proxy")
        return proxy_env()

    def offline(self):
        """The way out :meth:`online` gave taken away again: its proxy
        container removed, then shown gone - no container of that name, and
        none answering to ``egress`` on the box's network. Unavailable when
        either is still there: the agent is never started beside it."""
        name = self.egress
        if not name:
            return
        _docker(["rm", "-f", name], check=False, timeout=60)
        left = _docker(["ps", "-aq", "--filter", f"name=^/{name}$"], check=False, timeout=30)
        if left.returncode != 0 or left.stdout.strip():
            raise Unavailable(f"the way out ({name}) could not be removed")
        on = _docker(["network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", self.net], check=False, timeout=30)
        if on.returncode != 0 or name in on.stdout.split():
            raise Unavailable(f"the way out ({name}) is still on the unit's network")
        self.egress = None

    def egress_said(self) -> str:
        """What the check's proxy let through and refused, a JSON line each."""
        if not self.egress:
            return ""
        try:
            r = _docker(["logs", self.egress], check=False, timeout=30)
        except Unavailable:
            return ""
        return r.stdout + r.stderr

    def runtime_problem(self, name: str) -> Optional[str]:
        """Why the agent's runtime ``name`` cannot start in this image, or
        None. A glibc build in a musl image is the usual reason, said as such."""
        inside = self.runtimes[name][1] + ("/bin/node" if name == "node" else "/bin/python3")
        r = self.run([inside, "--version"], 60)
        if r["exit"] == 0:
            return None
        musl = self.run(["sh", "-c", "ls /lib/ld-musl-* 2>/dev/null"], 30)
        if musl["exit"] == 0 and musl["output"].strip():
            return f"the agent's runtime ({name}) needs glibc; this task's image is musl-based (Alpine, say)"
        return f"the agent's runtime ({name}) does not start in this task's image: " + r["output"].strip()[-200:]

    def diff(self, limit: int = 2000) -> str:
        """What changed in the box's file system since its image (``docker
        diff``), less the folders Nullius mounted and the ones above them."""
        r = _docker(["diff", self.name], check=False, timeout=120)
        ours = [p for p, _ in self.mounted] + [inside for _, inside in self.runtimes.values()] + [INIT]
        mine = lambda path: any(t == path or t.startswith(path.rstrip("/") + "/") or path.startswith(t + "/") for t in ours)
        lines = [l for l in r.stdout.splitlines() if not mine(l[2:])]
        return "\n".join(lines[:limit]) + (f"\n... {len(lines) - limit} more\n" if len(lines) > limit else "\n")

    def give_back(self, paths: Iterable[str]):
        """The folders of this machine the box wrote into, given back to this
        machine's user: what root wrote there is the user's to read and remove.
        On a rootless engine the box's root is that user."""
        paths = [p for p in paths if p]
        if self.up and paths:
            try:
                self.run(["chown", "-R", "0:0" if rootless() else _user()] + paths, 120, user="0")
            except Unavailable:
                pass

    def stop(self):
        if self.egress:
            try:
                _docker(["rm", "-f", self.egress], check=False, timeout=60)
            except Unavailable:
                pass
            self.egress = None
        if self.up:
            try:
                _docker(["rm", "-f", self.name], check=False, timeout=60)
            except Unavailable:
                pass
            self.up = False
        if self.own_net:
            try:
                _docker(["network", "rm", self.net], check=False, timeout=60)
            except Unavailable:
                pass
            self.own_net = False


def key_root() -> str:
    """The key folders (a relay's key, a door's nonce, a setup driver's
    credentials), in the user's cache: Colima and Lima share the home with
    their engine, not the temporary folder."""
    return os.path.join(paths.cache_dir(), "relay-keys")


def key_folder(job: str) -> str:
    """A new owner-only folder for a job's secret files under :func:`key_root`."""
    root = key_root()
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o700)
    forget_stale_keys()
    return tempfile.mkdtemp(prefix=key_prefix(job), dir=root)


def secret_file(path: str, text: str) -> str:
    """``text`` in a new file at ``path`` that only its owner reads."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(text)
    return path


def forget_stale_keys():
    """Remove every job's key folders older than ``KEY_STALE``."""
    old = time.time() - KEY_STALE
    for d in glob.glob(os.path.join(glob.escape(key_root()), "*")):
        try:
            if os.lstat(d).st_mtime < old:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def key_prefix(job: str) -> str:
    """A job's key folder prefix: a hash, so no job's prefix starts another's."""
    return f"nullius-relay-key-{hashlib.sha256(job.encode()).hexdigest()[:12]}-"


def sweep(job: str):
    """Remove every container and network a job's units left, by its label,
    its key folders, and stale key folders."""
    for d in glob.glob(os.path.join(glob.escape(key_root()), glob.escape(key_prefix(job)) + "*")):
        shutil.rmtree(d, ignore_errors=True)
    forget_stale_keys()
    try:
        ids = _docker(["ps", "-aq", "--filter", f"label={LABEL}={job}"], check=False).stdout.split()
        if ids:
            _docker(["rm", "-f"] + ids, check=False, timeout=120)
        nets = _docker(["network", "ls", "-q", "--filter", f"label={LABEL}={job}"], check=False).stdout.split()
        if nets:
            _docker(["network", "rm"] + nets, check=False, timeout=120)
    except Unavailable:
        pass
