"""OSWorld2's GitLab: Task-Web/gitlab at COMMIT, for the tasks of TASKS.

GitLab is that repository's compose project with gitlab.compose.yml over it.
It publishes no port: the mock websites' Caddy serves it as gitlab.<suffix>.
init makes the root token, registers the runner and keeps the volumes as the
snapshot. A unit of TASKS holds LOCK and starts GitLab from the snapshot; a
warden in a session of its own removes it once the unit lets go or its driver
dies.

python3 gitlab_server.py configure STACK SUFFIX --write-mbps N --write-iops N
python3 gitlab_server.py init STACK | up STACK | down STACK | check HOST [--runner]
python3 gitlab_server.py compose STACK ARGS..."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import websites

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDE = os.path.join(HERE, "gitlab.compose.yml")
COMMIT = "8655d651722f4254e59e813de9f68a6732ea525c"
PROJECT = "osworld2-gitlab"
OPTION = "NULLIUS_OPTION_GITLAB"
SHARE_OPTION = "NULLIUS_OPTION_GITLAB_WRITE"
VM_WRITE = ("NULLIUS_OPTION_WRITE_MBPS", "NULLIUS_OPTION_WRITE_IOPS")
TOKEN = "GITLAB_PRIVATE_TOKEN"
TASKS = frozenset(("026", "041"))
RUNNER_TASKS = frozenset(("041",))
LOCK = "/tmp/nullius-osworld2-gitlab.lck"
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,56}[a-z0-9])?(?::[1-9][0-9]{0,4})?")
SHARE = re.compile(r"[1-9][0-9]{0,5}:[1-9][0-9]{0,5}")
ENV_FILE = ".osworld2.env"
COMPOSE_FILE = ".osworld2.compose.yml"
RB_FILE = ".osworld2.gitlab.rb"
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
STACK_FILE = "stack"
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
INIT = "make GitLab once (python3 gitlab_server.py init STACK, from this benchmark's folder)"


class GitLabError(RuntimeError):
    pass


def url(environ=os.environ):
    """GitLab's URL from the run's GitLab option, its host; None for none."""
    value = environ.get(OPTION, "none")
    if value == "none":
        return None
    if not HOST.fullmatch(value):
        raise GitLabError(f"the GitLab option {value!r} is not GitLab's host: give the name it answers at "
                          f"(such as gitlab.{websites._site().GATEWAY}.nip.io), or none")
    return websites.SCHEME + value


def needed(eid, environ=os.environ):
    """A task of TASKS without GitLab's host and token is refused, since its class cannot even load."""
    value = url(environ)
    if eid not in TASKS:
        return None
    if not value or not environ.get(TOKEN):
        raise GitLabError(f"task {eid} needs GitLab and this run gives it no "
                          + ("host: set the GitLab option" if not value else f"token: set the licence {TOKEN}")
                          + f" ({websites.SETUP_DOC})")
    return value


def env(eid, environ=os.environ):
    """The variables the upstream reads GitLab by, for a task of TASKS; its token is in the driver's own."""
    value = url(environ) if eid in TASKS else None
    return {"GITLAB_URL": value} if value else {}


def share(environ=os.environ):
    """The run's GitLab write share, (MB/s, IOPS); None for none, refused while the VMs' writes are limited."""
    value = environ.get(SHARE_OPTION, "none")
    found = SHARE.fullmatch(value)
    if value != "none" and not found:
        raise GitLabError(f"the GitLab write share {value!r} is not MB/s:IOPS, or none")
    if not found and any(environ.get(n, "none") != "none" for n in VM_WRITE):
        raise GitLabError("the VMs' writes are limited and GitLab has no write share of its own: take a slot for it "
                          "beside the VM's and give it as the GitLab write share option, MB/s:IOPS")
    given = tuple(int(x) for x in value.split(":")) if found else None
    if given:
        allocation(*given)
    return given


def stack():
    """The stack's folder, as init wrote it beside the snapshot's archive."""
    if not _snapshot():
        raise GitLabError(f"no snapshot {SNAPSHOT} on this machine: {INIT}")
    image = GITLAB_IMAGE.search(_override())
    said = _docker("run", "--rm", "--network", "none", "--pull", "never", "-v", f"{SNAPSHOT}:/snapshot:ro",
                   "--entrypoint", "cat", image.group(1) if image else "", f"/snapshot/{STACK_FILE}")
    found = said.stdout.strip() if said.returncode == 0 else ""
    if not os.path.isabs(found):
        raise GitLabError(f"the snapshot {SNAPSHOT} names no stack's folder: {INIT}")
    return found


def prepared(environ=os.environ):
    """What a unit's GitLab needs before its VM starts: the snapshot, the stack's settings, the write share."""
    where = stack()
    served = _settings(where).get("GITLAB_URL")
    if url(environ) != served:
        raise GitLabError(f"the GitLab option asks for {url(environ)} and the stack at {where} serves {served}: "
                          "set the GitLab option to the stack's host")
    share(environ)
    return where


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
        raise GitLabError(f"{host} does not resolve: the GitLab option must be the host GitLab answers at")
    if not _signed_in(value):
        raise GitLabError(f"{value}/users/sign_in answered no page")
    said = _request("/api/v4/user", value, token)
    if said[0] != 200 or _json(said, f"{value}/api/v4/user").get("is_admin") is not True:
        raise GitLabError(f"{value}/api/v4/user answered {said[0]} for the token {TOKEN}, or not as an "
                          "administrator's: give the token GitLab was made with (python3 gitlab_server.py init)")
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
        raise GitLabError(f"{stack} is not Task-Web/gitlab at {COMMIT}: clone it at that commit")
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
                          "configure first") from None


def allocation(mbps, iops):
    """Each service's limit by its share, rounded down, so all of them stay within the project's."""
    if mbps < 1 or iops < 1:
        raise GitLabError("GitLab's write share is whole numbers above 0")
    total = sum(w for _, w in SHARES)
    out = {s: (mbps * websites.MB * w // total, iops * w // total) for s, w in SHARES}
    if not all(v for pair in out.values() for v in pair):
        raise GitLabError(f"a write share of {iops} IOPS leaves a service none: give GitLab at least {total}")
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


def _token():
    value = os.environ.get(TOKEN, "")
    if len(value) < MIN_TOKEN or not value.isprintable() or value.strip() != value:
        raise GitLabError(f"give GitLab's root token as {TOKEN} in this command's environment, at least "
                          f"{MIN_TOKEN} printable characters")
    return value


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
    done = subprocess.run(["sh", "-c", script.replace("$$", "$")], capture_output=True, text=True,
                          timeout=START_SECONDS, env=dict(_clean(), **{TOKEN: _token()}), stdin=subprocess.DEVNULL)
    _done(done, "making the root token")


def _register(stack, value):
    """The README's runner registration; a runner the container could not take is removed from GitLab again."""
    status, body = _request("/api/v4/user/runners", value, _token(), {
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
        _request(f"/api/v4/runners/{made['id']}", value, _token(), method="DELETE")
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


def _snapshot():
    return _docker("volume", "inspect", SNAPSHOT).returncode == 0


def init(stack, say=print):
    """GitLab made once from nothing: its first start, the root token, the runner; then kept as the snapshot."""
    stack = _pinned(stack)
    settings = _settings(stack)
    value, token = settings["GITLAB_URL"], _token()
    if _volumes():
        raise GitLabError(f"{PROJECT} has volumes already: python3 gitlab_server.py down STACK first")
    if _snapshot():
        raise GitLabError(f"the snapshot {SNAPSHOT} is there already: remove it (docker volume rm {SNAPSHOT}) to "
                          "make GitLab anew")
    t0 = time.time()
    _done(compose(stack, ["up", "-d", "--pull", "never", "gitlab"]), "starting GitLab")
    say(json.dumps({"started": "gitlab", "up_s": _until(START_SECONDS, "GitLab", lambda: _signed_in(value))}))
    _make_token(stack)
    ready(value, token)
    say(json.dumps({"token": "made", "s": round(time.time() - t0)}))
    _done(compose(stack, ["up", "-d", "--pull", "never", "dind", "gitlab-runner"]), "starting the runner")
    runner = _register(stack, value)
    say(json.dumps({"runner": runner, "online_s": _until(RUNNER_SECONDS, "the runner",
                                                         lambda: _online(value, token, runner))}))
    _done(compose(stack, ["stop"]), "stopping GitLab")
    _done(_docker("volume", "create", SNAPSHOT), "making the snapshot's volume")
    _copy(stack, settings, f"tar -C /v -czf /snapshot/{ARCHIVE} {' '.join(VOLUMES)} && "
                           f"printf %s {shlex.quote(stack)} > /snapshot/{STACK_FILE}", keep=True)
    _done(compose(stack, ["down", "--volumes"]), "removing GitLab")
    return {"snapshot": SNAPSHOT, "s": round(time.time() - t0)}


def _online(value, token, runner):
    said = _request(f"/api/v4/runners/{runner}", value, token)
    return said[0] == 200 and _json(said, "the runner").get("status") == "online"


def up(stack, limits=None, closed=None, say=None):
    """GitLab anew from the snapshot, waited on until ready() and reached from the VMs' network."""
    stack = _pinned(stack)
    settings = _settings(stack, limits)
    value, token = settings["GITLAB_URL"], _token()
    if not _snapshot():
        raise GitLabError(f"no snapshot {SNAPSHOT}: {INIT}")
    t0 = time.time()
    _done(compose(stack, ["down", "--volumes"], limits=limits), "removing GitLab")
    _done(compose(stack, ["create", "--pull", "never"], limits=limits), "making GitLab's containers")
    _copy(stack, settings, f"tar -C /v -xzf /snapshot/{ARCHIVE}", keep=False)
    if say:
        say(json.dumps({"restored_s": round(time.time() - t0)}))
    _done(compose(stack, ["start"], limits=limits), "starting GitLab")
    _until(START_SECONDS, "GitLab", lambda: ready(value, token, runner=True), closed)
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
    c = sub.add_parser("configure")
    c.add_argument("stack")
    c.add_argument("suffix")
    c.add_argument("--write-mbps", type=int, required=True)
    c.add_argument("--write-iops", type=int, required=True)
    d = sub.add_parser("compose")
    d.add_argument("stack")
    d.add_argument("args", nargs=argparse.REMAINDER)
    for op in ("init", "up", "down"):
        sub.add_parser(op).add_argument("stack")
    k = sub.add_parser("check")
    k.add_argument("host")
    k.add_argument("--runner", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.op == "compose":
            args = a.args[1:] if a.args[:1] == ["--"] else a.args
            with contextlib.nullcontext() if websites.reads(args) else _alone():
                return compose(a.stack, args, capture=False)
        if a.op == "configure":
            path, settings = configure(a.stack, a.suffix, a.write_mbps, a.write_iops)
            out = {"settings": path, **{k: v for k, v in settings.items() if k != TOKEN}}
        elif a.op == "check":
            if not HOST.fullmatch(a.host):
                raise GitLabError(f"{a.host!r} is not GitLab's host")
            out = {"url": ready(websites.SCHEME + a.host, _token(), runner=a.runner)}
        else:
            with _alone():
                out = {"init": init, "up": lambda s: up(s, say=print), "down": down}[a.op](a.stack)
    except (GitLabError, websites.WebsiteError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
