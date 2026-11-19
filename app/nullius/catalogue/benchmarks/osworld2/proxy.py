"""OSWorld2's proxy for the tasks that ask for one: tinyproxy in a container of the unit's own.

    python3 proxy.py --build
    python3 proxy.py --sweep UNIT_DIR RECORD_FD [CONTAINER]
    python3 proxy.py --watch UNIT_DIR DRIVER_PID RECORD_FD CONTAINER

The driver passes the unit's proxy.json as the upstream's PROXY_CONFIG_FILE.
The container is on NETWORK and publishes no port; tinyproxy logs warnings
only, never a request's URL. A container is labelled with the unit's folder
and named with this user's id and its driver's pid. A start docker did not
answer is never repeated: a sweeper (--sweep) removes the container later,
and a watcher (--watch) when the driver is killed first."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time

DOCKERFILE = r"""FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
RUN apk add --no-cache tinyproxy~1.11
USER 65534:65534
ENTRYPOINT ["/bin/sh", "-c", "printf '%s\\n' \"$TINYPROXY_CONF\" > /tmp/tinyproxy.conf && exec tinyproxy -d -c /tmp/tinyproxy.conf"]
"""
IMAGE = "nullius/osworld2-proxy:" + hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:16]
CONF = "TINYPROXY_CONF"
PORT = 8888
USER = "nullius"
FILE = "proxy.json"
NAME = "nullius-osworld2-proxy-"
UNIT = "nullius.osworld2.proxy"
FOLDER = "nullius.osworld2.proxy.dir"
PACKAGE = "nullius.osworld2.proxy.package"
NETWORK = "nullius-osworld2"
SWEEPS = (2, 8, 20, 90, 180, 300, 300)
RETRY_SECONDS = 120
SWEEP_SECONDS = 1800
CLIENTS = 100
PIDS = 128
WATCH_SECONDS = 1
WRITE_BPS = 52428
WRITE_IOPS = 1
TAKEN = {"OSWORLD2_PROXY_WRITE_BPS": str(WRITE_BPS), "OSWORLD2_PROXY_WRITE_IOPS": str(WRITE_IOPS)}
LOG = "proxy.log"
DOCKER_SECONDS = 120
START_SECONDS = 300
_SEEN = {}
_STARTS = {}


class NoAnswer(RuntimeError):
    pass


def _docker(*args, env=None, until=None):
    seconds = DOCKER_SECONDS if until is None else min(DOCKER_SECONDS, round(until - time.monotonic(), 1))
    if seconds <= 0:
        raise NoAnswer(f"docker {args[0]} was not asked: the time given for it ran out")
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=seconds, env=env)
    except subprocess.TimeoutExpired:
        raise NoAnswer(f"docker {args[0]} got no answer in {seconds:g} s: is its daemon running?")
    except OSError:
        raise RuntimeError("no docker on this machine's PATH: the proxy needs it")


def _last(said, otherwise):
    """The last line docker said, past the pointer to its help it adds to every refusal."""
    lines = [ln.strip() for ln in (said or "").splitlines() if ln.strip() and not ln.startswith("Run 'docker")]
    return (lines or [otherwise])[-1][:300]


def _mark(dir):
    return hashlib.sha256(os.path.abspath(dir).encode()).hexdigest()[:32]


def _prefix():
    return f"{NAME}{os.getuid()}-"


def config(token):
    """No Via header, since the pool's residential proxies add none."""
    return "".join(f"{line}\n" for line in (f"Port {PORT}", "Timeout 600", f"MaxClients {CLIENTS}", "ConnectPort 443",
                                            "ConnectPort 80", "LogLevel Warning", "DisableViaHeader Yes",
                                            f"BasicAuth {USER} {token}"))


def _alive(pid):
    """Whether the process runs; one that has exited and waits for its parent to see it does not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rpartition(")")[2].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return True


def _orphans(until=None):
    """Remove this user's proxies whose driver is gone or whose unit holds no record; another user's stay."""
    seen = _docker("ps", "-a", "--filter", f"name={_prefix()}", "--format", "{{.Names}}\t{{.Label \"" + FOLDER + "\"}}",
                   until=until)
    gone = []
    for name, _, folder in (ln.partition("\t") for ln in seen.stdout.splitlines()):
        m = re.fullmatch(re.escape(_prefix()) + r"(\d+)-\d+", name.strip())
        if m and (not _alive(int(m.group(1))) or (folder and not os.path.lexists(os.path.join(folder, FILE)))):
            gone.append(name.strip())
    if gone:
        _docker("rm", "-f", *gone, until=until)


def _image(dir, until):
    """Orphans removed and the image checked at a process's first start; its ID logged at each."""
    if not _SEEN:
        _orphans(until)
        seen = _docker("image", "inspect", "--format", "{{.Id}}\t{{json .Config}}", IMAGE, until=until)
        if seen.returncode != 0:
            raise RuntimeError(f"no {IMAGE} on this machine, the proxy of the tasks that ask for one: build it "
                               "once with python3 proxy.py --build, from this benchmark's folder (it needs the "
                               "network)")
        ident, _, config = seen.stdout.strip().partition("\t")
        try:
            package = ((json.loads(config) or {}).get("Labels") or {}).get(PACKAGE, "")
        except (ValueError, AttributeError):
            package = ""
        _SEEN.update(id=ident or IMAGE, package=package)
    _note(dir, f"proxy image {_SEEN['id']}, " + (_SEEN["package"] or "its tinyproxy package not labelled: "
                                                  "build it again with python3 proxy.py --build"))


def _ready(name, until):
    """A stopped container is an error with its exit code, never its log, which may name a request's host."""
    host = ""
    late = f"did not answer in its start's {START_SECONDS} s"
    while True:
        try:
            seen = _docker("inspect", "-f", "{{.State.Status}}\t{{.State.ExitCode}}\t{{with index .NetworkSettings."
                           "Networks " + json.dumps(NETWORK) + "}}{{.IPAddress}}{{end}}", name, until=until)
        except NoAnswer:
            if time.monotonic() < until:
                raise
            raise RuntimeError(f"the proxy at {host or 'no address'}:{PORT} {late}") from None
        status, code, host = (seen.stdout.strip().split("\t") + ["", ""])[:3]
        if status not in ("running", "created"):
            raise RuntimeError(f"the proxy's container stopped, exit code {code or 'unknown'}")
        if status == "running" and host:
            try:
                socket.create_connection((host, PORT), timeout=1).close()
                return host
            except OSError:
                pass
        if time.monotonic() >= until:
            raise RuntimeError(f"the proxy at {host or 'no address'}:{PORT} {late}")
        time.sleep(0.2)


def start(dir, disk=None):
    """The unit's proxy started, and its pool file's path; a proxy left from an earlier attempt is removed first."""
    until = time.monotonic() + START_SECONDS
    _image(dir, until)
    remove(dir, final=True, until=until)
    path = os.path.join(dir, FILE)
    name = f"{_prefix()}{os.getpid()}-{time.time_ns()}"
    with _locked(dir):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _STARTS[os.path.abspath(dir)] = name
        _watcher(dir, fd, name)
    finally:
        os.close(fd)
    token = secrets.token_hex(16)
    limits = [] if disk is None else ["--device-write-bps", f"{disk}:{WRITE_BPS}", "--device-write-iops", f"{disk}:{WRITE_IOPS}"]
    try:
        made = _docker("run", "-d", "--name", name, "--label", f"{UNIT}={_mark(dir)}",
                       "--label", f"{FOLDER}={os.path.abspath(dir)}", "--network", NETWORK,
                       "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=1m,mode=1777", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "--memory", "64m", "--pids-limit", str(PIDS),
                       "--cpus", "1", "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
                       "--log-opt", "compress=false", *limits,
                       "-e", CONF, IMAGE, env=dict(os.environ, **{CONF: config(token)}), until=until)
    except NoAnswer as e:
        _found(dir, name, e, until)
        made = None
    _record(path, [])
    if made is not None and made.returncode != 0:
        raise RuntimeError("the proxy's container was refused: " + _last(made.stderr, "no reason"))
    host = _ready(name, until)
    _record(path, [{"host": host, "port": PORT, "username": USER, "password": token, "protocol": "http"}])
    return path


def _found(dir, name, unanswered, until):
    """No container from the unanswered start, or no answer, is an error; the record stays empty for its sweeper."""
    later = "; the unit's sweeper removes one it makes later"
    try:
        found = _docker("ps", "-a", "-q", "--filter", f"label={UNIT}={_mark(dir)}", "--filter", f"name={name}",
                        until=until)
    except NoAnswer as e:
        raise RuntimeError(f"{unanswered}, and then {e}: whether it made the proxy's container is unknown{later}")
    if found.returncode != 0:
        raise RuntimeError(f"{unanswered}, and could not say whether it made the proxy's container: "
                           + _last(found.stderr, "no reason") + later)
    if not found.stdout.split():
        raise RuntimeError(f"{unanswered}, and has made no container for the proxy yet{later}")


@contextlib.contextmanager
def _locked(dir):
    fd = os.open(dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _ours(dir, fd):
    """Whether the unit's record is still the file ``fd`` holds open, so no later start has replaced it."""
    try:
        here, own = os.lstat(os.path.join(dir, FILE)), os.fstat(fd)
    except OSError:
        return False
    return (here.st_dev, here.st_ino) == (own.st_dev, own.st_ino)


def _record(path, pool):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW), "w", encoding="utf-8") as f:
        json.dump(pool, f)


def remove(dir, final=False, until=None, name=None, fd=None):
    """Remove the unit's proxy, then its record; an empty record stays until the ``final`` sweep."""
    path = os.path.join(dir, FILE)
    if not (os.path.lexists(path) if fd is None else _ours(dir, fd)):
        return
    found = _docker("ps", "-a", "-q", "--filter", f"label={UNIT}={_mark(dir)}", "--filter", f"name={name or _prefix()}",
                    until=until)
    if found.returncode != 0:
        raise RuntimeError("the unit's proxy was not looked for: " + _last(found.stderr, "no reason"))
    ids = found.stdout.split()
    if ids:
        gone = _docker("rm", "-f", *ids, until=until)
        if gone.returncode != 0:
            raise RuntimeError("the unit's proxy was not removed: " + _last(gone.stderr, "no reason"))
    if fd is None:
        with contextlib.suppress(FileNotFoundError):
            if final or os.lstat(path).st_size:
                os.remove(path)
        return
    with _locked(dir):
        if _ours(dir, fd) and (final or os.fstat(fd).st_size):
            os.remove(path)


def _removed(dir, final, name, fd, until):
    try:
        remove(dir, final, until=until, name=name, fd=fd)
    except (RuntimeError, OSError) as e:
        return str(e) or type(e).__name__
    return None


def sweep(dir, fd, name=None):
    """Remove the start's container after each of SWEEPS; a removal that never succeeds is a line in LOG."""
    until = time.monotonic() + SWEEP_SECONDS
    failed = None
    for i, wait in enumerate(SWEEPS):
        if not _ours(dir, fd):
            break
        left = until - time.monotonic() - 2 * DOCKER_SECONDS
        final = i == len(SWEEPS) - 1 or left < wait
        time.sleep(max(0, min(wait, left)))
        failed = _removed(dir, final, name, fd, until)
        if final:
            break
    while failed and _ours(dir, fd) and until - time.monotonic() > RETRY_SECONDS:
        time.sleep(RETRY_SECONDS)
        failed = _removed(dir, True, name, fd, until)
    if failed:
        _note(dir, f"the unit's proxy was not removed in {SWEEP_SECONDS // 60} min: {failed}")
    with contextlib.suppress(RuntimeError, OSError):
        _orphans(until)


def watch(dir, pid, fd, name):
    """Sweep the start's container once its driver is gone, while the record is still the start's."""
    while _ours(dir, fd):
        if not _alive(pid):
            sweep(dir, fd, name)
            return
        time.sleep(WATCH_SECONDS)


def _spawn(fd, *args):
    subprocess.Popen([sys.executable, os.path.abspath(__file__), *args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,))


def _later(dir, name=None):
    with contextlib.suppress(OSError):
        fd = os.open(os.path.join(dir, FILE), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            _spawn(fd, "--sweep", os.path.abspath(dir), str(fd), *([name] if name else []))
        finally:
            os.close(fd)


def _watcher(dir, fd, name):
    _spawn(fd, "--watch", os.path.abspath(dir), str(os.getpid()), str(fd), name)


def _note(dir, line):
    with contextlib.suppress(OSError):
        fd = os.open(os.path.join(dir, LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + line + "\n")


@contextlib.contextmanager
def held(dir, wanted, disk=lambda: None):
    """The unit's variables while its proxy runs, when ``wanted``; a record left after the removal gets a sweeper."""
    if not wanted:
        yield {}
        return
    try:
        device = disk()
        yield {"PROXY_CONFIG_FILE": start(dir, device), **({} if device is None else TAKEN)}
    finally:
        try:
            remove(dir)
        except (RuntimeError, OSError) as e:
            _note(dir, f"the unit's proxy was not removed: {e}")
        if os.path.lexists(os.path.join(dir, FILE)):
            _later(dir, _STARTS.get(os.path.abspath(dir)))


def build():
    """Build the image, then again from the cache, labelled with its tinyproxy package."""
    if subprocess.run(["docker", "build", "-t", IMAGE, "-"], input=DOCKERFILE, text=True).returncode != 0:
        return 1
    seen = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "apk", IMAGE, "list",
                           "--installed", "tinyproxy"], capture_output=True, text=True)
    package = (seen.stdout.split() or [""])[0] if seen.returncode == 0 else ""
    if not package.startswith("tinyproxy-"):
        print("the image's tinyproxy package was not found: " + _last(seen.stderr, "apk said nothing"), file=sys.stderr)
        return 1
    if subprocess.run(["docker", "build", "--label", f"{PACKAGE}={package}", "-t", IMAGE, "-"], input=DOCKERFILE,
                      text=True).returncode != 0:
        return 1
    print(IMAGE, package)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--sweep"] and len(sys.argv) in (4, 5):
        sweep(sys.argv[2], int(sys.argv[3]), *sys.argv[4:])
        sys.exit(0)
    if sys.argv[1:2] == ["--watch"] and len(sys.argv) == 6:
        watch(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
        sys.exit(0)
    if sys.argv[1:] != ["--build"]:
        sys.exit("\n".join(ln.strip() for ln in __doc__.splitlines()[2:5]))
    sys.exit(build())
