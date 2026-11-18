"""OSWorld2's mock websites: Task-Web/OSWorld-web at the release's tag, as its tasks reach them.

The sites are OSWorld-web's compose project at COMMIT with websites.compose.yml
over it; tasks of TASKS reach them at <name>.<suffix>, the run's websites
option. Caddy listens on the VMs' network's gateway only. Overleaf keeps no
slice per task, so its tasks run one at a time under LOCK, on fresh volumes;
a unit that needs the sites holds SITES_LOCK shared, reset, smoke and compose's changes alone.

python3 websites.py configure STACK SUFFIX --write-mbps N --write-iops N
python3 websites.py builder STACK up --write-mbps N --write-iops N | down
python3 websites.py build STACK [--group NAME]... [--reserve-gb 25]
python3 websites.py compose STACK ARGS...
python3 websites.py check SUFFIX | smoke STACK | reset STACK"""
import argparse
import contextlib
import errno
import fcntl
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDE = os.path.join(HERE, "websites.compose.yml")
SITE = os.path.join(HERE, "startup", "sitecustomize.py")
COMMIT = "60c89fe6a8ed934668619d8d26132848239eb8ee"
PROJECT = "osworld2-web"
SCHEME = "http://"
OPTION = "NULLIUS_OPTION_WEBSITES"
SETUP_DOC = "docs/benchmarks/osworld2-machine.md"
STATEFUL = ("awsconsole", "budgetwise", "calendar", "careerlink", "cloudcrm", "dinogame", "eventix", "expenseflow",
            "formcraft", "glbviewer", "insurance-claim", "mailhub", "overleaf-collab", "reviewsphere", "slidepuzzle",
            "streamview", "teamchat", "travelhubpro", "trippza", "vaultbank", "visaapplication", "wandb")
NAMES = STATEFUL + ("studio.streamview", "interfacinglinux", "overleaf")
TASKS = frozenset("005 007 008 013 015 016 018 019 020 021 026 028 031 035 036 038 039 043 046 050 052 056 057 058 "
                  "060 065 068 069 070 072 073 077 079 081 082 091 098 100 101 102".split())
OVERLEAF_TASKS = frozenset(("057", "072"))
OVERLEAF = ("overleaf_web_mongo", "overleaf_web_mongo_init", "overleaf_web_redis", "overleaf_web_admin_bootstrap",
            "overleaf_web_app")
OVERLEAF_VOLUMES = ("overleaf_web_data", "overleaf_web_mongo_data", "overleaf_web_redis_data")
LOCK = "/tmp/nullius-osworld2-overleaf.lck"
SITES_LOCK = "/tmp/nullius-osworld2-websites.lck"
READING = frozenset(("config", "ps", "ls", "logs", "images", "top", "port", "events", "version", "stats", "wait"))
VALUED = frozenset(("-f", "--file", "-p", "--project-name", "--project-directory", "--profile", "--env-file",
                    "--ansi", "--progress", "--parallel"))
COMPOSE_VERSION = (2, 24, 4)
_HELD = []
_SITE = []
COOKIE = "nullius-osworld2-ready"
SUFFIX = re.compile(r"(?=.{3,200}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+")
GROUPS = (("pulled", ("caddy", "overleaf_web_mongo", "overleaf_web_mongo_init", "overleaf_web_redis",
                       "overleaf_web_admin_bootstrap"), 7),
          ("overleaf", ("overleaf_web_app", "overleaf_collab_compiler"), 12),
          ("next-a", ("awsconsole", "calendar_web", "careerlink_web", "dinogame_web", "glbviewer_web", "reviewsphere"), 9),
          ("next-b", ("slidepuzzle_web", "travelhub_pro_web", "trippza_web", "visaapplication_web", "wandb",
                      "overleaf_collab_web"), 9),
          ("frontends", ("budgetwise_web_frontend", "cloudcrm_web_frontend", "eventix_web_frontend",
                         "expenseflow_web_frontend", "formcraft_web_frontend", "mailhub_web_frontend",
                         "streamview_web_frontend", "teamchat_web_frontend", "vaultbank_web_frontend",
                         "insurance_claim_web_frontend", "interfacinglinux_web"), 7),
          ("backends", ("budgetwise_web_backend", "cloudcrm_web_backend", "eventix_web_backend",
                        "expenseflow_web_backend", "formcraft_web_backend", "insurance_claim_web_backend",
                        "mailhub_web_backend", "streamview_web_backend", "teamchat_web_backend",
                        "vaultbank_web_backend"), 4))
SERVICES = tuple(s for _, group, _ in GROUPS for s in group)
WEIGHTS = {"overleaf_web_mongo": (30, 10), "overleaf_web_app": (30, 6), "overleaf_web_redis": (10, 4),
           "overleaf_collab_web": (46, 22), "careerlink_web": (6, 2), "caddy": (4, 2)}
QUIET = (1, 1)
TIERS = ("QUIET",) + tuple(s.upper() for s in WEIGHTS)
SETTINGS = ("HOST_SUFFIX", "CADDY_SCHEME", "OSWORLD2_WEB_ADDRESS", "OSWORLD2_WEB_DISK") + tuple(
    f"OSWORLD2_WEB_{t}_{k}" for t in TIERS for k in ("BPS", "IOPS"))
MB = 1024 * 1024
BUILDER = "osw2-web"
BUILDKIT = "moby/buildkit@sha256:de10faf919fc71ba4eb1dd7bd6449566d012b0c9436b1c61bfee21d621b009aa"
BUILDKITD = "nullius-osworld2-buildkitd"
BUILDKIT_VOLUME = "nullius-osworld2-buildkit"
BUILDKIT_PARALLEL = 4
RESERVE_GB = 25
IMAGES_FILE = ".osworld2.images.json"
BUILDKIT_FILE = ".osworld2.buildkitd.toml"
ENV_FILE = ".osworld2.env"
COMPOSE_FILE = ".osworld2.compose.yml"
LABEL = "com.docker.compose."
CADDY = re.compile(r"^\s+image: (lucaslorentz/caddy-docker-proxy@sha256:[0-9a-f]{64})$", re.M)
PROBE = "nullius-osworld2-web-probe-"
HTTP_SECONDS = 10
DOCKER_SECONDS = 300
OVERLEAF_SECONDS = 600
RESET_SECONDS = 900
BUILD_SECONDS = 4 * 3600
POLL = 5
PAGE_BYTES = 1 << 20
PROXIES = {}
START = "start the stack (python3 websites.py compose STACK up -d) and check it (python3 websites.py check SUFFIX)"


class WebsiteError(RuntimeError):
    pass


def suffix(environ=os.environ):
    """The run's websites option, the sites' host suffix; None for none."""
    value = environ.get(OPTION, "none")
    if value == "none":
        return None
    if not SUFFIX.fullmatch(value):
        raise WebsiteError(f"Website host suffix {value!r} is not a host suffix: give the suffix the stack serves "
                           f"(such as {_site().GATEWAY}.nip.io), or none")
    return value


def env(environ=os.environ):
    """The variables the upstream reads the sites by, for every process the driver starts."""
    value = suffix(environ)
    return {"WEBSITE_HOST_SUFFIX": value} if value else {}


def needed(eid, environ=os.environ):
    """A task of TASKS without a suffix is refused, since its class cannot even load."""
    value = suffix(environ)
    if eid not in TASKS:
        return None
    if not value:
        raise WebsiteError(f"task {eid} needs the mock websites and this run gives it none: set Website host suffix "
                           f"to the suffix the stack serves ({SETUP_DOC})")
    return value


def _resolves(host):
    try:
        return {a[4][0] for a in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)}
    except OSError:
        return set()


def _fetch(opener, url):
    """Any failure, a status of 400 or more among them, is a WebsiteError."""
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "nullius-osworld2"}),
                         timeout=HTTP_SECONDS) as r:
            return r.read(PAGE_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        raise WebsiteError(f"{url} answered {e.code}: {START}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        raise WebsiteError(f"{url} did not answer ({type(reason).__name__}: {reason}): {START}") from None


def ready(value, overleaf=False, state=True):
    """The number of names checked; Overleaf's only with ``overleaf``, each /api/state only with ``state``."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES))
    names = NAMES if overleaf else tuple(n for n in NAMES if n != "overleaf")
    gateway = _site().GATEWAY
    for name in names:
        host = f"{name}.{value}"
        found = _resolves(host)
        if not found:
            raise WebsiteError(f"{host} does not resolve: Website host suffix must be a wildcard DNS name of the "
                               f"address the stack listens on, {gateway} (such as {gateway}.nip.io)")
        if gateway not in found:
            raise WebsiteError(f"{host} resolves to {', '.join(sorted(found))}, not {gateway}, the VMs' network's "
                               f"gateway the stack listens on: set Website host suffix to a wildcard DNS name of "
                               f"{gateway} (such as {gateway}.nip.io)")
        if not _fetch(opener, f"{SCHEME}{host}/").strip():
            raise WebsiteError(f"{SCHEME}{host}/ answered an empty page: the stack serves another suffix than "
                               f"{value}, or not this site; {START}")
    for name in STATEFUL if state else ():
        url = f"{SCHEME}{name}.{value}/api/state?cookie={COOKIE}"
        try:
            said = json.loads(_fetch(opener, url))
        except ValueError:
            said = None
        if not isinstance(said, dict) or said.get("user_id") != COOKIE or not isinstance(said.get("state"), dict):
            raise WebsiteError(f"{url} answered no state of the site's: {START}")
    return len(names)


def _docker(*args, seconds=DOCKER_SECONDS):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=seconds,
                              env=_clean(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise WebsiteError(f"docker {args[0]} got no answer in {seconds} s: is its daemon running?") from None
    except OSError:
        raise WebsiteError("no docker on this machine's PATH: the mock websites run on Docker") from None


def _clean():
    """Without the stack's settings, so compose reads them from its environment file alone."""
    return {k: v for k, v in os.environ.items() if k not in SETTINGS}


def _done(said, what):
    if said.returncode != 0:
        last = ((said.stderr or "").strip().splitlines() or ["no reason"])[-1]
        raise WebsiteError(f"{what} failed: {last[:300]}")
    return said


def _settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return dict(ln.rstrip("\n").partition("=")[::2] for ln in f if "=" in ln)
    except OSError as e:
        raise WebsiteError(f"no settings of the stack at {path} ({type(e).__name__}): run python3 websites.py "
                           "configure first") from None


def _project():
    """The running project's compose command and settings, from any of its containers' labels."""
    fields = ("project.config_files", "project.working_dir", "project.environment_file")
    said = _docker("ps", "-a", "--filter", f"label={LABEL}project={PROJECT}",
                   "--format", "\t".join('{{.Label "' + LABEL + f + '"}}' for f in fields))
    rows = [ln.split("\t") for ln in said.stdout.splitlines() if ln.strip()] if said.returncode == 0 else []
    if not rows:
        raise WebsiteError(f"no container of the {PROJECT} project on this machine: {START}")
    files, where, envfile = (rows[0] + ["", "", ""])[:3]
    if not envfile or not files:
        raise WebsiteError(f"the {PROJECT} project was not started by websites.py compose, which gives it its "
                           "settings: start it that way")
    return (["compose", "-p", PROJECT, "--project-directory", where,
             *[x for f in files.split(",") for x in ("-f", f)], "--env-file", envfile], _settings(envfile))


def _waited(url, seconds, closed, what):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES))
    t0, last = time.time(), "no answer"
    while True:
        try:
            if _fetch(opener, url).strip():
                return
            last = "an empty page"
        except WebsiteError as e:
            last = str(e).split(": ", 1)[0]
        if time.time() - t0 >= seconds:
            raise WebsiteError(f"{what} did not come up in {seconds} s ({last})")
        if closed is None:
            time.sleep(POLL)
        elif closed.wait(POLL):
            raise WebsiteError(f"{what} was not up when the unit's time ran out")


def reset_overleaf(value, closed=None):
    """Overleaf's services removed with their volumes, started again, and waited on until its login answers."""
    compose, settings = _project()
    if settings.get("HOST_SUFFIX") != value:
        raise WebsiteError(f"the stack serves {settings.get('HOST_SUFFIX') or 'no suffix'} and this run asks for "
                           f"{value}: set Website host suffix to the stack's")
    _done(_docker(*compose, "rm", "--stop", "--force", "-v", *OVERLEAF), "removing Overleaf's services")
    _done(_docker("volume", "rm", "--force", *[f"{PROJECT}_{v}" for v in OVERLEAF_VOLUMES]), "removing Overleaf's volumes")
    _done(_docker(*compose, "up", "-d", "--no-build", "--pull", "never", OVERLEAF[-1]), "starting Overleaf")
    _waited(f"{SCHEME}overleaf.{value}/login", OVERLEAF_SECONDS, closed, "Overleaf")


def opened(path):
    """Read-only, so another user's lock in /tmp serves too; kept only as a plain file, since a FIFO would hang the open."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    while True:
        try:
            fd = os.open(path, flags)
            break
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
            os.fchmod(fd, 0o644)
            break
        except FileExistsError:
            pass
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a plain file", path)
    return fd


@contextlib.contextmanager
def locked(path, mode, closed, busy):
    """Waited on until ``closed`` is set, or refused at once with no ``closed``; one file for every user of the machine."""
    try:
        fd = opened(path)
    except OSError as e:
        raise WebsiteError(f"the lock {path} cannot be opened ({type(e).__name__}): remove it if it is not a plain "
                           "file every user can read") from None
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if closed is None:
                    raise WebsiteError(busy) from None
                if closed.wait(1):
                    raise WebsiteError(f"the unit's time ran out while {busy}") from None
        _HELD.append(fd)
        try:
            yield
        finally:
            _HELD.remove(fd)
    finally:
        os.close(fd)


def held_fds():
    """For a process that keeps the locks held after this one lets go."""
    return tuple(_HELD)


@contextlib.contextmanager
def held(eid, value, closed):
    """For a task of OVERLEAF_TASKS, Overleaf alone and anew."""
    if not value:
        yield
        return
    with locked(SITES_LOCK, fcntl.LOCK_SH, closed, "the stack was being reset or smoked"):
        if eid not in OVERLEAF_TASKS:
            yield
            return
        with locked(LOCK, fcntl.LOCK_EX, closed, "another unit's Overleaf task ran"):
            reset_overleaf(value, closed)
            yield


def _alone():
    return locked(SITES_LOCK, fcntl.LOCK_EX, None, "a unit is using the mock websites: reset or smoke the stack "
                   "when no task that needs them runs")


def _override():
    try:
        with open(OVERRIDE, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise WebsiteError(f"no {OVERRIDE} ({type(e).__name__}): this benchmark's folder is incomplete; "
                           "install Nullius again") from None


def _compose_version():
    """Docker Compose COMPOSE_VERSION or later, which websites.compose.yml's !override needs."""
    said = _docker("compose", "version", "--short")
    found = re.match(r"v?(\d+)\.(\d+)\.(\d+)", said.stdout.strip()) if said.returncode == 0 else None
    if not found or tuple(int(x) for x in found.groups()) < COMPOSE_VERSION:
        raise WebsiteError("websites.compose.yml needs Docker Compose " + ".".join(map(str, COMPOSE_VERSION))
                           + f" or later (its !override); this machine has {said.stdout.strip() or 'none'}")


def _pinned(stack):
    """``stack`` is OSWorld-web at COMMIT, its submodules at the commits it names, nothing tracked changed."""
    def git(*args):
        try:
            return subprocess.run(["git", "-C", stack, *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WebsiteError(f"git could not read {stack}: {type(e).__name__}") from None
    head = git("rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != COMMIT:
        raise WebsiteError(f"{stack} is not OSWorld-web at {COMMIT} (the tag osworld-v2.1): clone it with its "
                           f"submodules at that tag ({SETUP_DOC})")
    modules = git("submodule", "status")
    off = [ln.split()[1] for ln in modules.stdout.splitlines() if ln[:1] != " " and len(ln.split()) > 1]
    if modules.returncode != 0 or off:
        raise WebsiteError(f"{stack}'s submodules are not at the commits it names ({', '.join(off) or 'unreadable'}): "
                           "git submodule update --init --recursive")
    changed = git("status", "--porcelain", "--untracked-files=no")
    if changed.returncode != 0 or changed.stdout.strip():
        raise WebsiteError(f"{stack} has changes to its files: the stack runs as published")
    return os.path.abspath(stack)


def _beside(stack, name):
    return os.path.abspath(stack).rstrip(os.sep) + name


def _write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".nullius-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def allocation(mbps, iops):
    """Each tier's per-service limit by weight, rounded down, so all services together stay within the share."""
    if mbps < 1 or iops < 1:
        raise WebsiteError("the stack's write share is whole numbers above 0")
    weights = [WEIGHTS.get(s, QUIET) for s in SERVICES]
    total = [sum(w[i] for w in weights) for i in (0, 1)]
    out = {}
    for tier, (wb, wi) in [("QUIET", QUIET)] + [(s.upper(), w) for s, w in WEIGHTS.items()]:
        out[tier] = (mbps * MB * wb // total[0], iops * wi // total[1])
        if not all(out[tier]):
            raise WebsiteError(f"a write share of {iops} IOPS leaves the {tier.lower()} services none: give the "
                               f"stack at least {total[1]} IOPS")
    return out


def limits(settings):
    """Every service's limit as the settings give it, and their sum."""
    each = {s: (int(settings[f"OSWORLD2_WEB_{t}_BPS"]), int(settings[f"OSWORLD2_WEB_{t}_IOPS"]))
            for s in SERVICES for t in [s.upper() if s in WEIGHTS else "QUIET"]}
    return each, tuple(sum(v[i] for v in each.values()) for i in (0, 1))


def _site():
    """Loaded under another name, so it wraps nothing in this process."""
    if not _SITE:
        spec = importlib.util.spec_from_file_location("osworld2_startup", SITE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SITE.append(module)
    return _SITE[0]


def _network():
    """Made when missing; its gateway is the address Caddy listens on."""
    site = _site()
    try:
        return site.network(_docker)
    except site.NetworkError as e:
        raise WebsiteError(str(e)) from None


def _root_disk():
    info = _done(_docker("info", "--format", "{{.DockerRootDir}}"), "docker info")
    site = _site()
    try:
        return info.stdout.strip(), site.root_disk(info.stdout.strip())
    except site.WriteLimitError as e:
        raise WebsiteError(f"the write limit cannot be applied: {e}") from None


def configure(stack, value, mbps, iops):
    """The stack's settings: Caddy on the VMs' network's gateway, each tier with its write limit."""
    stack = _pinned(stack)
    if not SUFFIX.fullmatch(value):
        raise WebsiteError(f"{value!r} is not a host suffix")
    tiers = allocation(mbps, iops)
    _network()
    settings = {"HOST_SUFFIX": value, "CADDY_SCHEME": SCHEME, "OSWORLD2_WEB_ADDRESS": _site().GATEWAY,
                "OSWORLD2_WEB_DISK": _root_disk()[1],
                **{f"OSWORLD2_WEB_{t}_{k}": str(v[i]) for t, v in tiers.items() for i, k in enumerate(("BPS", "IOPS"))}}
    path = _beside(stack, ENV_FILE)
    _write(path, "".join(f"{k}={settings[k]}\n" for k in SETTINGS))
    return path, settings


def compose(stack, args, capture=False, builder=None):
    """Compose on the stack, websites.compose.yml copied beside it and the VMs' network made first."""
    stack = _pinned(stack)
    envfile = _beside(stack, ENV_FILE)
    _settings(envfile)
    _compose_version()
    _network()
    copy = _beside(stack, COMPOSE_FILE)
    _write(copy, _override())
    argv = ["docker", "compose", "-p", PROJECT, "--project-directory", stack, "-f",
            os.path.join(stack, "docker-compose.yml"), "-f", copy, "--env-file", envfile, *args]
    env = dict(_clean(), **({"BUILDX_BUILDER": builder} if builder else {}))
    if capture:
        return subprocess.run(argv, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                              timeout=BUILD_SECONDS if builder else RESET_SECONDS, pass_fds=held_fds())
    return subprocess.run(argv, env=env, pass_fds=held_fds()).returncode


def builder(stack, op, mbps=0, iops=0):
    """BuildKit, privileged as buildx's own builders, with its own write limit; down removes it with its cache."""
    if op == "down":
        gone = [_docker("buildx", "rm", BUILDER), _docker("rm", "-f", BUILDKITD), _docker("volume", "rm", "--force", BUILDKIT_VOLUME)]
        failed = [g for g in gone[1:] if g.returncode != 0]
        if failed:
            _done(failed[0], "removing the builder")
        return {"builder": BUILDER, "removed": True}
    if mbps < 1 or iops < 1:
        raise WebsiteError("the builder's write limit is whole numbers above 0")
    stack = _pinned(stack)
    disk = _root_disk()[1]
    config = _beside(stack, BUILDKIT_FILE)
    _write(config, f"[worker.oci]\n  max-parallelism = {BUILDKIT_PARALLEL}\n")
    _done(_docker("run", "-d", "--name", BUILDKITD, "--privileged", "--restart", "no",
                  "--device-write-bps", f"{disk}:{mbps * MB}", "--device-write-iops", f"{disk}:{iops}",
                  "-v", f"{BUILDKIT_VOLUME}:/var/lib/buildkit", "-v", f"{config}:/etc/buildkit/buildkitd.toml:ro",
                  BUILDKIT), "starting the builder")
    host = json.loads(_done(_docker("inspect", "-f", "{{json .HostConfig}}", BUILDKITD), "inspecting the builder").stdout)
    kept = {"Path": disk, "Rate": mbps * MB} in (host.get("BlkioDeviceWriteBps") or []) and \
        {"Path": disk, "Rate": iops} in (host.get("BlkioDeviceWriteIOps") or [])
    if not kept:
        _docker("rm", "-f", BUILDKITD)
        raise WebsiteError(f"Docker kept no write limit on {disk} for the builder: it is removed, nothing was built")
    _done(_docker("buildx", "create", "--name", BUILDER, "--driver", "remote", f"docker-container://{BUILDKITD}"),
          "registering the builder")
    return {"builder": BUILDER, "disk": disk, "write_bps": mbps * MB, "write_iops": iops}


def _free_gb(root):
    st = os.statvfs(root)
    return st.f_bavail * st.f_frsize / 1e9


def build(stack, groups=(), reserve=RESERVE_GB, say=print):
    """A group that would leave less than ``reserve`` GB free stops the build; the cache is removed after each group."""
    stack = _pinned(stack)
    names = [g[0] for g in GROUPS]
    unknown = [g for g in groups if g not in names]
    if unknown:
        raise WebsiteError(f"no group {', '.join(unknown)}: the groups are {', '.join(names)}")
    if _docker("buildx", "inspect", BUILDER).returncode != 0:
        raise WebsiteError(f"no builder {BUILDER}: start it (python3 websites.py builder STACK up ...)")
    root = _root_disk()[0]
    done = []
    for name, services, estimate in GROUPS:
        if groups and name not in groups:
            continue
        free = _free_gb(root)
        if free - estimate < reserve:
            raise WebsiteError(f"stopped before {name}: {free:.1f} GB free under {root}, it needs about {estimate} GB "
                               f"and {reserve} GB stay free (done: {', '.join(done) or 'none'})")
        said = compose(stack, ["pull", *services] if name == "pulled" else ["build", *services], capture=True,
                       builder=BUILDER)
        _done(said, f"the {name} group")
        if name != "pulled":
            _done(_docker("buildx", "prune", "--builder", BUILDER, "--all", "--force"), "removing the builder's cache")
        done.append(name)
        say(json.dumps({"group": name, "free_gb": round(_free_gb(root), 1)}))
    config = json.loads(_done(compose(stack, ["config", "--format", "json"], capture=True), "reading the project").stdout)
    ids = {}
    for s in SERVICES:
        ref = config["services"].get(s, {}).get("image") or f"{PROJECT}-{s}"
        said = _docker("image", "inspect", "--format", "{{.Id}}", ref)
        ids[s] = {"image": ref, "id": said.stdout.strip() if said.returncode == 0 else None}
    path = _beside(stack, IMAGES_FILE)
    _write(path, json.dumps({"commit": COMMIT, "groups": done, "images": ids}, indent=1) + "\n")
    return {"groups": done, "images": path}


def _until_ready(value, seconds):
    t0 = time.time()
    while True:
        try:
            return ready(value, overleaf=True)
        except WebsiteError:
            if time.time() - t0 >= seconds:
                raise
            time.sleep(POLL)


def reset(stack):
    value = _settings(_beside(os.path.abspath(stack), ENV_FILE)).get("HOST_SUFFIX", "")
    with _alone():
        _done(compose(stack, ["down", "--volumes"], capture=True), "taking the stack down")
        _done(compose(stack, ["up", "-d", "--no-build", "--pull", "never"], capture=True), "starting the stack")
        return _until_ready(value, RESET_SECONDS)


def _from_vms(value):
    """Each name from a container on the VMs' network, by Caddy's own busybox wget."""
    image = CADDY.search(_override()).group(1)
    loop = 'for h; do wget -q -O /dev/null -T 10 "http://$h/" || { echo "$h"; exit 1; }; done'
    network = _network()
    said = _docker("run", "--rm", "--name", f"{PROBE}{os.getuid()}-{os.getpid()}", "--network", network,
                   "--pull", "never", "--entrypoint", "sh", image, "-c", loop, "sh",
                   *[f"{n}.{value}" for n in NAMES])
    if said.returncode != 0:
        failed = (said.stdout.strip().splitlines() or [""])[-1]
        raise WebsiteError(f"from the VMs' network {network}, {failed or 'a name'} did not answer: "
                           + ((said.stderr or "").strip().splitlines() or ["no reason"])[-1][:300])


def smoke(stack):
    stack = _pinned(stack)
    value = _settings(_beside(stack, ENV_FILE)).get("HOST_SUFFIX", "")
    with _alone():
        return _smoked(stack, value)


def _smoked(stack, value):
    out = {"suffix": value, "names": ready(value, overleaf=True)}
    _from_vms(value)
    out["network"] = len(NAMES)
    node = shutil.which("node")
    if not node:
        raise WebsiteError("no node on PATH: the upstream's scripts/verify-control-plane.mjs needs Node 18 or later")
    script = os.path.join(stack, "scripts", "verify-control-plane.mjs")
    said = subprocess.run([node, script], capture_output=True, text=True, timeout=RESET_SECONDS, stdin=subprocess.DEVNULL,
                          env=dict(_clean(), OSWORLD_HOST_SUFFIX=value, OSWORLD_SCHEME=SCHEME[:-3]))
    passed = [ln.split()[1] for ln in said.stdout.splitlines() if ln.startswith("PASS ")]
    if said.returncode != 0:
        raise WebsiteError(f"the upstream's control-plane check failed after {len(passed)} sites: "
                           + ((said.stderr or said.stdout).strip().splitlines() or ["no reason"])[-1][:300])
    out["control_plane"] = len(passed)
    reset_overleaf(value)
    out["overleaf"] = "anew"
    return out


def reads(args):
    """Whether compose ``args`` only read: their subcommand is one of READING."""
    skip = False
    for x in args:
        if skip:
            skip = False
        elif x in VALUED:
            skip = True
        elif not x.startswith("-"):
            return x in READING
    return False


def main(argv):
    p = argparse.ArgumentParser(prog="websites.py", description="OSWorld2's mock websites")
    sub = p.add_subparsers(dest="op", required=True)
    c = sub.add_parser("configure")
    c.add_argument("stack")
    c.add_argument("suffix")
    c.add_argument("--write-mbps", type=int, required=True)
    c.add_argument("--write-iops", type=int, required=True)
    d = sub.add_parser("compose")
    d.add_argument("stack")
    d.add_argument("args", nargs=argparse.REMAINDER)
    b = sub.add_parser("builder")
    b.add_argument("stack")
    b.add_argument("op_", metavar="up|down", choices=("up", "down"))
    b.add_argument("--write-mbps", type=int, default=0)
    b.add_argument("--write-iops", type=int, default=0)
    g = sub.add_parser("build")
    g.add_argument("stack")
    g.add_argument("--group", action="append", default=[])
    g.add_argument("--reserve-gb", type=float, default=RESERVE_GB)
    sub.add_parser("check").add_argument("suffix")
    sub.add_parser("smoke").add_argument("stack")
    sub.add_parser("reset").add_argument("stack")
    a = p.parse_args(argv)
    try:
        if a.op == "compose":
            args = a.args[1:] if a.args[:1] == ["--"] else a.args
            with contextlib.nullcontext() if reads(args) else _alone():
                return compose(a.stack, args)
        if a.op == "configure":
            path, settings = configure(a.stack, a.suffix, a.write_mbps, a.write_iops)
            out = {"settings": path, **settings, "sum_of_limits": dict(zip(("bps", "iops"), limits(settings)[1]))}
        elif a.op == "builder":
            out = builder(a.stack, a.op_, a.write_mbps, a.write_iops)
        elif a.op == "build":
            out = build(a.stack, a.group, a.reserve_gb)
        elif a.op == "check":
            if not SUFFIX.fullmatch(a.suffix):
                raise WebsiteError(f"{a.suffix!r} is not a host suffix")
            out = {"suffix": a.suffix, "names": ready(a.suffix, overleaf=True)}
        elif a.op == "smoke":
            out = smoke(a.stack)
        else:
            out = {"names": reset(a.stack)}
    except (WebsiteError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
