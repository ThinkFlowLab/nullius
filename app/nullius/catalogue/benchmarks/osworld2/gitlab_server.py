"""OSWorld2's GitLab: Task-Web/gitlab at COMMIT, for the tasks of TASKS.

GitLab is that repository's compose project with gitlab.compose.yml over it.
It publishes no port: the mock websites' Caddy serves it as gitlab.<suffix>.
A unit of TASKS holds LOCK, has prepare() clone it beside the upstream, make
this machine's root token, register the runner and keep the volumes as the
snapshot, once, then starts GitLab from the snapshot; a warden in a session of
its own removes it once the unit lets go or its driver dies.

python3 gitlab_server.py prepare STACK SUFFIX | up STACK | down STACK
python3 gitlab_server.py compose STACK ARGS..."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import websites

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDE = os.path.join(HERE, "gitlab.compose.yml")
COMMIT = "8655d651722f4254e59e813de9f68a6732ea525c"
REPOSITORY = "https://github.com/Task-Web/gitlab.git"
PROJECT = "osworld2-gitlab"
VM_WRITE = ("NULLIUS_OPTION_WRITE_MBPS", "NULLIUS_OPTION_WRITE_IOPS")
TOKEN = "GITLAB_PRIVATE_TOKEN"
TASKS = frozenset(("026", "041"))
RUNNER_TASKS = frozenset(("041",))
LOCK = "/tmp/nullius-osworld2-gitlab.lck"
ENV_FILE = ".osworld2.env"
COMPOSE_FILE = ".osworld2.compose.yml"
RB_FILE = ".osworld2.gitlab.rb"
TOKEN_FILE = ".osworld2.token"
RB = """puma['worker_processes'] = 2
sidekiq['concurrency'] = 5
prometheus_monitoring['enable'] = false
"""
SHARES = (("GITLAB", 6), ("RUNNER", 1), ("DIND", 1))
LIMITS = tuple(f"OSWORLD2_GITLAB_{s}_{k}" for s, _ in SHARES for k in ("BPS", "IOPS"))
SETTINGS = ("GITLAB_URL", "GITLAB_PRIVATE_TOKEN", "OSWORLD2_GITLAB_HOST", "OSWORLD2_GITLAB_INGRESS",
            "OSWORLD2_GITLAB_DISK", "OSWORLD2_GITLAB_RB", "OSWORLD2_GITLAB_SHARE") + LIMITS
UNUSED = "unused"
SNAPSHOT = "nullius-osworld2-gitlab-snapshot"
STAMP = "made"
SHARE_DEFAULT = (16, 160)
TOKEN_DAYS = 300
GITLAB_IMAGE = re.compile(r"^\s+image: (gitlab/gitlab-ce@sha256:[0-9a-f]{64})$", re.M)
SHARE_LABEL = "nullius.osworld2.share"
ARCHIVE = "gitlab.tar.gz"
VOLUMES = ("gitlab_config", "gitlab_data", "gitlab_runner_config")
MARK = "gitlab.stack"
WARD_LOG = "gitlab-warden.log"
WARD_SWEEP = 20
COPY_LABEL = "nullius.osworld2.gitlab.copy"
UNIT_LIMITS = "gitlab.limits"
JOB_IMAGE = "alpine:3.20"
RUNNER_NAME = "osworld-docker-runner"
DOCKER_HOST = "tcp://docker:2376"
MIN_TOKEN = 20
HTTP_SECONDS = 15
START_SECONDS = 3600
RUNNER_SECONDS = 300
COPY_SECONDS = 3600
POLL = 10
PROXIES = {}


class GitLabError(RuntimeError):
    pass


def url(environ=os.environ):
    """GitLab's URL, gitlab. before the mock websites' suffix, as their Caddy serves it; None for no sites."""
    value = websites.suffix(environ)
    return f"{websites.SCHEME}gitlab.{value}" if value else None


def home(environ=os.environ):
    """Where this machine's GitLab is cloned: beside the upstream's clone."""
    return os.path.join(os.path.dirname(os.path.abspath(environ["NULLIUS_UPSTREAM"])), f"gitlab-{COMMIT}")


def needed(eid, environ=os.environ):
    """A task of TASKS without the mock websites, which serve GitLab, is refused, since its class cannot even load."""
    value = url(environ)
    if eid not in TASKS:
        return None
    if not value:
        raise GitLabError(f"task {eid} needs GitLab, which the mock websites serve, and this run gives it none: set "
                          f"Website host suffix to the suffix the stack serves ({websites.SETUP_DOC})")
    return value


def env(eid, environ=os.environ):
    """The variables the upstream reads GitLab by, for a task of TASKS alone: its URL and this machine's token."""
    value = needed(eid, environ)
    return {"GITLAB_URL": value, TOKEN: token(home(environ))} if value else {}


def share(environ=os.environ):
    """GitLab's write share, (MB/s, IOPS): a VM's own limits, SHARE_DEFAULT's where one is none; None with neither."""
    given = [environ.get(n, "none") for n in VM_WRITE]
    if given == ["none", "none"]:
        return None
    found = tuple(d if v == "none" else int(v) for v, d in zip(given, SHARE_DEFAULT))
    allocation(*found)
    return found


def token(stack):
    """This machine's root token for GitLab, made once and kept beside the stack, for this user alone."""
    path = _beside(stack, TOKEN_FILE)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".nullius-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secrets.token_hex(24))
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            os.remove(tmp)
    with open(path, encoding="utf-8") as f:
        value = f.read()
    if len(value) < MIN_TOKEN or not value.isalnum():
        raise GitLabError(f"{path} holds no token: remove it, and GitLab is made anew")
    return value


def _stamp(stack, value):
    return hashlib.sha256(f"{stack}\n{value}\n{token(stack)}\n{_override()}\n{RB}".encode()).hexdigest()


def _made():
    """The stamp init left in the snapshot, read read-only; None without one, or once the token is TOKEN_DAYS
    old, since the upstream's token step makes it for a year."""
    image = GITLAB_IMAGE.search(_override())
    if not _snapshot() or not image or not _found(_docker("image", "inspect", image.group(1)), "no such image",
                                                  "looking for GitLab's image"):
        return None
    said = _docker("run", "--rm", "--network", "none", "--pull", "never", "-v", f"{SNAPSHOT}:/snapshot:ro",
                   "--entrypoint", "cat", image.group(1), f"/snapshot/{STAMP}")
    if said.returncode != 1 or "no such file" not in said.stderr.lower():
        _done(said, f"reading the snapshot {SNAPSHOT}")
    found = said.stdout.split() if said.returncode == 0 else []
    fresh = len(found) == 2 and found[1].isdigit() and time.time() - int(found[1]) < TOKEN_DAYS * 86400
    return found[0] if fresh else None


def prepare(stack, environ=os.environ, say=None, closed=None, limits=None):
    """GitLab made on this machine for the run's sites, once: cloned, configured, pulled, then init; made anew
    when the snapshot was made for another stack, host, token or pin, or its token is old."""
    suffix, value = websites.suffix(environ), url(environ)
    if not value:
        raise GitLabError("GitLab is served by the mock websites: give their suffix")
    made = _made()
    with contextlib.suppress(GitLabError):
        if made == _stamp(stack, value) and _settings(_pinned(stack))["GITLAB_URL"] == value:
            return stack
    _clone(stack)
    configure(stack, suffix, *SHARE_DEFAULT)
    _done(compose(stack, ["pull"], seconds=START_SECONDS), "pulling GitLab's images")
    down(stack)
    if _snapshot():
        _done(_docker("volume", "rm", SNAPSHOT), "removing the old snapshot")
    init(stack, say, closed, limits)
    return stack


def _clone(stack):
    with contextlib.suppress(GitLabError):
        return _pinned(stack)
    shutil.rmtree(stack, ignore_errors=True)
    os.makedirs(os.path.dirname(stack), exist_ok=True)
    tmp = tempfile.mkdtemp(dir=os.path.dirname(stack), prefix=".nullius-")
    try:
        for args in (("init", "--quiet"), ("fetch", "--quiet", "--depth", "1", REPOSITORY, COMMIT),
                     ("checkout", "--quiet", "FETCH_HEAD")):
            try:
                said = subprocess.run(["git", "-C", tmp, *args], capture_output=True, text=True,
                                      timeout=START_SECONDS, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise GitLabError(f"git could not clone {REPOSITORY}: {type(e).__name__}") from None
            _done(said, f"cloning {REPOSITORY} at {COMMIT}")
        os.replace(tmp, stack)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _pinned(stack)


def _request(path, value, token=None, data=None, method=None):
    """``path`` of GitLab at ``value``, as (status, body); no answer is a GitLabError."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES))
    headers = {"User-Agent": "nullius-osworld2", **({"PRIVATE-TOKEN": token} if token else {})}
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        with opener.open(urllib.request.Request(value + path, data=body, headers=headers, method=method),
                         timeout=HTTP_SECONDS) as r:
            return r.status, r.read(1 << 20)
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b""
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        raise GitLabError(f"{value}{path} did not answer ({type(reason).__name__}: {reason})") from None


def _json(said, what):
    try:
        return json.loads(said[1])
    except ValueError:
        raise GitLabError(f"{what} answered no JSON") from None


def ready(value, token, runner=False):
    """GitLab's sign-in page, the root token as an administrator's and, with ``runner``, an online runner."""
    host = urllib.parse.urlsplit(value).hostname
    if not websites._resolves(host):
        raise GitLabError(f"{host} does not resolve: Website host suffix must be a wildcard DNS name")
    if not _signed_in(value):
        raise GitLabError(f"{value}/users/sign_in answered no page")
    said = _request("/api/v4/user", value, token)
    if said[0] != 200 or _json(said, f"{value}/api/v4/user").get("is_admin") is not True:
        raise GitLabError(f"{value}/api/v4/user answered {said[0]} for the token {TOKEN}, or not as an "
                          f"administrator's: docker volume rm {SNAPSHOT}, and GitLab is made anew")
    if runner:
        said = _request("/api/v4/runners/all?type=instance_type&status=online", value, token)
        if said[0] != 200 or not _json(said, f"{value}/api/v4/runners/all"):
            raise GitLabError(f"GitLab at {value} has no runner online, and task 041 deploys with one")
    return value


@contextlib.contextmanager
def held(value, closed):
    """With GitLab's URL, GitLab alone, and the mock websites, which serve it, kept from a reset."""
    if not value:
        yield
        return
    with websites.locked(websites.SITES_LOCK, fcntl.LOCK_SH, closed, "the stack was being reset or smoked"), \
            websites.locked(LOCK, fcntl.LOCK_EX, closed, "another unit's GitLab task ran"):
        yield


@contextlib.contextmanager
def _alone():
    with websites.locked(websites.SITES_LOCK, fcntl.LOCK_SH, None, "the mock websites are being reset or smoked: "
                         "start or remove GitLab by hand when they are not"), \
            websites.locked(LOCK, fcntl.LOCK_EX, None, "a unit's GitLab task runs: start or remove GitLab by hand "
                            "when no task that needs it runs"):
        yield


def unit(where, dir, environ=os.environ):
    """The unit's mark for its reaper and, with a write share, the limits its GitLab takes from it."""
    given = share(environ)
    limits = os.path.join(dir, UNIT_LIMITS) if given else None
    if limits:
        lines = {f"OSWORLD2_GITLAB_{s}_{k}": str(v[i]) for s, v in allocation(*given).items()
                 for i, k in enumerate(("BPS", "IOPS"))}
        websites._write(limits, "".join(f"{k}={v}\n" for k, v in lines.items()) + "OSWORLD2_GITLAB_SHARE=unit\n")
    websites._write(os.path.join(dir, MARK), json.dumps({"stack": where, "limits": limits}))
    return limits


def reap(dir):
    """GitLab removed for a unit that started it, and again WARD_SWEEP seconds later; nothing without its mark."""
    mark = os.path.join(dir, MARK)
    try:
        with open(mark, encoding="utf-8") as f:
            said = json.load(f)
    except (OSError, ValueError):
        return False
    down(said["stack"], said.get("limits"))
    time.sleep(WARD_SWEEP)
    if _running():
        down(said["stack"], said.get("limits"))
    os.remove(mark)
    return True


def ward(fd, dir):
    """The warden: once ``fd`` closes, GitLab is removed, then the locks let go; failures go to WARD_LOG."""
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass
    try:
        reap(dir)
    except (GitLabError, websites.WebsiteError, OSError, ValueError, KeyError) as e:
        with open(os.path.join(dir, WARD_LOG), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {type(e).__name__}: {e}\n")


def _running():
    said = _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}")
    return said.returncode == 0 and bool(said.stdout.split())


def _docker(*args, seconds=websites.DOCKER_SECONDS, **kw):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=seconds,
                              env=kw.pop("env", _clean()), stdin=subprocess.DEVNULL, **kw)
    except subprocess.TimeoutExpired:
        raise GitLabError(f"docker {args[0]} got no answer in {seconds} s") from None
    except OSError:
        raise GitLabError("no docker on this machine's PATH: GitLab runs on Docker") from None


def _clean():
    """Without the project's settings or the token, so compose reads them from its environment files alone."""
    return {k: v for k, v in os.environ.items() if k not in SETTINGS}


def _done(said, what):
    if said.returncode != 0:
        last = ((said.stderr or said.stdout or "").strip().splitlines() or ["no reason"])[-1]
        raise GitLabError(f"{what} failed: {last[:300]}")
    return said


def _pinned(stack):
    """``stack`` is Task-Web/gitlab at COMMIT, nothing tracked changed."""
    def git(*args):
        try:
            return subprocess.run(["git", "-C", stack, *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitLabError(f"git could not read {stack}: {type(e).__name__}") from None
    if git("rev-parse", "HEAD").stdout.strip() != COMMIT:
        raise GitLabError(f"{stack} is not Task-Web/gitlab at {COMMIT}: give the stack Nullius cloned")
    changed = git("status", "--porcelain", "--untracked-files=no")
    if changed.returncode != 0 or changed.stdout.strip():
        raise GitLabError(f"{stack} has changes to its files: GitLab runs as published")
    return os.path.abspath(stack)


def _beside(stack, name):
    return os.path.abspath(stack).rstrip(os.sep) + name


def _read(path):
    with open(path, encoding="utf-8") as f:
        return dict(ln.rstrip("\n").partition("=")[::2] for ln in f if "=" in ln)


def _settings(stack, limits=None):
    path = _beside(stack, ENV_FILE)
    try:
        return dict(_read(path), **(_read(limits) if limits else {}))
    except OSError as e:
        raise GitLabError(f"no settings of GitLab at {path} ({type(e).__name__}): run python3 gitlab_server.py "
                          "prepare STACK SUFFIX first") from None


def allocation(mbps, iops):
    """Each service's limit by its share, rounded down, so all of them stay within the project's."""
    if mbps < 1 or iops < 1:
        raise GitLabError("GitLab's write share is whole numbers above 0")
    total = sum(w for _, w in SHARES)
    out = {s: (mbps * websites.MB * w // total, iops * w // total) for s, w in SHARES}
    if not all(v for pair in out.values() for v in pair):
        raise GitLabError(f"a write limit of {iops} IOPS leaves a GitLab service none: give at least {total}")
    return out


def configure(stack, suffix, mbps, iops):
    stack = _pinned(stack)
    if not websites.SUFFIX.fullmatch(suffix):
        raise GitLabError(f"{suffix!r} is not a host suffix")
    shares = allocation(mbps, iops)
    websites._network()
    rb = _beside(stack, RB_FILE)
    websites._write(rb, RB)
    settings = {"GITLAB_URL": f"{websites.SCHEME}gitlab.{suffix}", "GITLAB_PRIVATE_TOKEN": UNUSED,
                "OSWORLD2_GITLAB_HOST": f"gitlab.{suffix}", "OSWORLD2_GITLAB_INGRESS": f"{websites.PROJECT}_web",
                "OSWORLD2_GITLAB_DISK": websites._root_disk()[1], "OSWORLD2_GITLAB_RB": rb,
                "OSWORLD2_GITLAB_SHARE": "stack",
                **{f"OSWORLD2_GITLAB_{s}_{k}": str(v[i])
                   for s, v in shares.items() for i, k in enumerate(("BPS", "IOPS"))}}
    path = _beside(stack, ENV_FILE)
    websites._write(path, "".join(f"{k}={settings[k]}\n" for k in SETTINGS))
    return path, settings


def _override():
    try:
        with open(OVERRIDE, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise GitLabError(f"no {OVERRIDE} ({type(e).__name__}): this benchmark's folder is incomplete; "
                          "install Nullius again") from None


def _argv(stack, args, limits=None):
    copy, text = _beside(stack, COMPOSE_FILE), _override()
    try:
        with open(copy, encoding="utf-8") as f:
            same = f.read() == text
    except OSError:
        same = False
    if not same:
        websites._write(copy, text)
    return ["compose", "-p", PROJECT, "--project-directory", stack, "-f", os.path.join(stack, "docker-compose.yml"),
            "-f", copy, "--env-file", _beside(stack, ENV_FILE), *(["--env-file", limits] if limits else []), *args]


def compose(stack, args, capture=True, seconds=websites.RESET_SECONDS, limits=None):
    stack = _pinned(stack)
    _settings(stack, limits)
    websites._compose_version()
    if capture:
        return _docker(*_argv(stack, args, limits), seconds=seconds)
    return subprocess.run(["docker", *_argv(stack, args, limits)], env=_clean(), pass_fds=websites.held_fds()).returncode


def _container(stack, service):
    said = _done(compose(stack, ["ps", "-a", "-q", service]), f"finding {service}")
    found = said.stdout.split()
    if len(found) != 1:
        raise GitLabError(f"no one container of {service} in {PROJECT}")
    return found[0]


def _until(seconds, what, check, closed=None):
    t0, last = time.time(), "no answer"
    while True:
        try:
            if check():
                return round(time.time() - t0)
        except GitLabError as e:
            last = str(e)[:200]
        if time.time() - t0 >= seconds:
            raise GitLabError(f"{what} did not come up in {seconds} s ({last})")
        if closed is None:
            time.sleep(POLL)
        elif closed.wait(POLL):
            raise GitLabError(f"{what} was not up when the unit's time ran out")


def _signed_in(value):
    """Caddy answers a name it serves no site for with an empty page."""
    status, body = _request("/users/sign_in", value)
    return status == 200 and bool(body.strip())


def _upstream_script(stack):
    """The upstream's gitlab-init-token script, from its own compose file: the override disarms that service."""
    said = _done(_docker("compose", "-p", PROJECT, "--project-directory", stack, "-f",
                         os.path.join(stack, "docker-compose.yml"), "--env-file", _beside(stack, ENV_FILE),
                         "config", "--format", "json"), "reading the upstream's project")
    return json.loads(said.stdout)["services"]["gitlab-init-token"]["command"][-1]


def _make_token(stack):
    """The upstream's own token step, run with this machine's docker: no socket is mounted in a container."""
    script, name = _upstream_script(stack), _container(stack, "gitlab")
    swaps = ((f'-e {TOKEN}="$${TOKEN}" gitlab ', f"-e {TOKEN} {name} "),
             ("docker exec gitlab ", f"docker exec {name} "))
    for old, new in swaps:
        if script.count(old) != 1:
            raise GitLabError("the upstream's token step is not the one this file knows: check gitlab_server.py "
                              f"against Task-Web/gitlab at {COMMIT}")
        script = script.replace(old, new)
    value = token(stack)
    done = subprocess.run(["timeout", "--foreground", str(START_SECONDS), "sh", "-c", script.replace("$$", "$")],
                          capture_output=True, text=True, timeout=START_SECONDS + 60, env=dict(_clean(), **{TOKEN: value}), stdin=subprocess.DEVNULL)
    done.stdout, done.stderr = (x.replace(value, TOKEN) for x in (done.stdout, done.stderr))
    _done(done, "making the root token")


def _register(stack, value):
    """The README's runner registration; a runner the container could not take is removed from GitLab again."""
    status, body = _request("/api/v4/user/runners", value, token(stack), {
        "runner_type": "instance_type", "description": RUNNER_NAME, "tag_list": "osworld,docker",
        "run_untagged": "true", "locked": "false", "paused": "false"})
    if status != 201:
        raise GitLabError(f"GitLab refused a runner ({status})")
    made = json.loads(body)
    runner = _container(stack, "gitlab-runner")
    said = _docker("exec", "-e", "CI_SERVER_TOKEN", runner, "gitlab-runner", "register", "--non-interactive",
                   "--url", value, "--executor", "docker", "--docker-image", JOB_IMAGE, "--description", RUNNER_NAME,
                   "--docker-host", DOCKER_HOST, "--docker-tlsverify", "--docker-cert-path", "/certs/client",
                   env=dict(_clean(), CI_SERVER_TOKEN=made["token"]))
    if said.returncode != 0:
        _request(f"/api/v4/runners/{made['id']}", value, token(stack), method="DELETE")
    _done(said, "registering the runner")
    return made["id"]


def _copy(stack, settings, tar, keep):
    """``tar`` in a container of the project's, under GitLab's write limit; ``keep`` writes the snapshot."""
    image = _done(compose(stack, ["config", "--images"]), "reading the images").stdout.split()
    gitlab = next(i for i in image if i.startswith("gitlab/gitlab-ce"))
    disk = settings["OSWORLD2_GITLAB_DISK"]
    mounts = [x for v in VOLUMES for x in ("-v", f"{PROJECT}_{v}:/v/{v}")]
    return _done(_docker("run", "--rm", "--network", "none", "--label", f"com.docker.compose.project={PROJECT}",
                         "--label", f"{COPY_LABEL}=1",
                         "--label", f"{SHARE_LABEL}={settings.get('OSWORLD2_GITLAB_SHARE') or 'stack'}",
                         "--device-write-bps", f"{disk}:{settings['OSWORLD2_GITLAB_GITLAB_BPS']}",
                         "--device-write-iops", f"{disk}:{settings['OSWORLD2_GITLAB_GITLAB_IOPS']}",
                         *mounts, "-v", f"{SNAPSHOT}:/snapshot" + ("" if keep else ":ro"),
                         "--entrypoint", "sh", gitlab, "-c", tar, seconds=COPY_SECONDS), "copying the volumes")


def _volumes():
    said = _done(_docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"),
                 "listing the volumes")
    return said.stdout.split()


def _found(said, gone, what):
    """Whether Docker found it: False only where its error says ``gone``, any other failure a GitLabError."""
    if said.returncode and gone not in said.stderr.lower():
        _done(said, what)
    return said.returncode == 0


def _snapshot():
    return _found(_docker("volume", "inspect", SNAPSHOT), "no such volume", f"looking for the snapshot {SNAPSHOT}")


def init(stack, say=None, closed=None, limits=None):
    """GitLab made once from nothing: its first start, the root token, the runner; then kept as the snapshot."""
    stack = _pinned(stack)
    settings = _settings(stack, limits)
    value, said, say = settings["GITLAB_URL"], token(stack), say or (lambda _: None)
    if _volumes():
        raise GitLabError(f"{PROJECT} has volumes already: python3 gitlab_server.py down STACK first")
    if _snapshot():
        raise GitLabError(f"the snapshot {SNAPSHOT} is there already: remove it (docker volume rm {SNAPSHOT}) to "
                          "make GitLab anew")
    t0 = time.time()
    _done(compose(stack, ["up", "-d", "--pull", "never", "gitlab"], limits=limits), "starting GitLab")
    say(json.dumps({"started": "gitlab", "up_s": _until(START_SECONDS, "GitLab", lambda: _signed_in(value), closed)}))
    _make_token(stack)
    ready(value, said)
    say(json.dumps({"token": "made", "s": round(time.time() - t0)}))
    _done(compose(stack, ["up", "-d", "--pull", "never", "dind", "gitlab-runner"], limits=limits), "starting the runner")
    runner = _register(stack, value)
    say(json.dumps({"runner": runner, "online_s": _until(RUNNER_SECONDS, "the runner",
                                                         lambda: _online(value, said, runner), closed)}))
    _done(compose(stack, ["stop"], limits=limits), "stopping GitLab")
    _done(_docker("volume", "create", SNAPSHOT), "making the snapshot's volume")
    _copy(stack, settings, f"tar -C /v -czf /snapshot/{ARCHIVE} {' '.join(VOLUMES)} && "
                           f"printf '%s %s' {_stamp(stack, value)} {int(time.time())} > /snapshot/{STAMP}", keep=True)
    _done(compose(stack, ["down", "--volumes"], limits=limits), "removing GitLab")
    return {"snapshot": SNAPSHOT, "s": round(time.time() - t0)}


def _online(value, token, runner):
    said = _request(f"/api/v4/runners/{runner}", value, token)
    return said[0] == 200 and _json(said, "the runner").get("status") == "online"


def up(stack, limits=None, closed=None, say=None):
    """GitLab anew from the snapshot, waited on until ready() and reached from the VMs' network."""
    stack = _pinned(stack)
    settings = _settings(stack, limits)
    value, said = settings["GITLAB_URL"], token(stack)
    if not _snapshot():
        raise GitLabError(f"no snapshot {SNAPSHOT}: python3 gitlab_server.py prepare STACK SUFFIX")
    t0 = time.time()
    _done(compose(stack, ["down", "--volumes"], limits=limits), "removing GitLab")
    _done(compose(stack, ["create", "--pull", "never"], limits=limits), "making GitLab's containers")
    _copy(stack, settings, f"tar -C /v -xzf /snapshot/{ARCHIVE}", keep=False)
    if say:
        say(json.dumps({"restored_s": round(time.time() - t0)}))
    _done(compose(stack, ["start"], limits=limits), "starting GitLab")
    _until(START_SECONDS, "GitLab", lambda: ready(value, said, runner=True), closed)
    _from_vms(settings["OSWORLD2_GITLAB_HOST"])
    return {"url": value, "up_s": round(time.time() - t0)}


def _from_vms(host):
    """GitLab's sign-in page from a container on the VMs' network, by the mock websites' Caddy's busybox wget."""
    image = websites.CADDY.search(websites._override()).group(1)
    said = _docker("run", "--rm", "--network", websites._network(), "--pull", "never", "--entrypoint", "wget",
                   image, "-q", "-O", "/dev/null", "-T", "10", f"http://{host}/users/sign_in")
    if said.returncode != 0:
        raise GitLabError(f"from the VMs' network, {host} did not answer: "
                          + ((said.stderr or "").strip().splitlines() or ["no reason"])[-1][:300])


def down(stack, limits=None):
    """The volumes' copy is no service of the project's, so it is removed by its label first."""
    said = _done(_docker("ps", "-aq", "--filter", f"label={COPY_LABEL}"), "listing the volumes' copy")
    if said.stdout.split():
        _done(_docker("rm", "-f", *said.stdout.split()), "removing the volumes' copy")
    _done(compose(stack, ["--profile", "upstream-init", "down", "--volumes", "--remove-orphans"], limits=limits),
          "removing GitLab")
    return {"removed": PROJECT}


def main(argv):
    p = argparse.ArgumentParser(prog="gitlab_server.py", description="OSWorld2's GitLab")
    sub = p.add_subparsers(dest="op", required=True)
    c = sub.add_parser("prepare")
    c.add_argument("stack")
    c.add_argument("suffix")
    d = sub.add_parser("compose")
    d.add_argument("stack")
    d.add_argument("args", nargs=argparse.REMAINDER)
    for op in ("up", "down"):
        sub.add_parser(op).add_argument("stack")
    a = p.parse_args(argv)
    try:
        if a.op == "compose":
            args = a.args[1:] if a.args[:1] == ["--"] else a.args
            with contextlib.nullcontext() if websites.reads(args) else _alone():
                return compose(a.stack, args, capture=False)
        with _alone():
            if a.op == "prepare":
                if os.path.lexists(a.stack):
                    _pinned(a.stack)
                out = {"prepared": prepare(os.path.abspath(a.stack), {websites.OPTION: a.suffix}, say=print)}
            else:
                out = {"up": lambda s: up(s, say=print), "down": down}[a.op](a.stack)
    except (GitLabError, websites.WebsiteError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
