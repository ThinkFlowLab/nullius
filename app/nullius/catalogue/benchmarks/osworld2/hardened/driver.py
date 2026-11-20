"""OSWorld-V2's driver for the Nullius standard, osworld2@hardened: the upstream with Nullius's patches (JSON lines).

It runs the Original setup and the null control. OSWorld-V2's own code at the
tested commit ($NULLIUS_UPSTREAM) loads, acts and scores, in processes of its
own; this driver imports none of it. Each unit's script runs in the unit's
folder, so its logs and results stay with the trial. Every process gets the
assets folder (_assets) as OSWORLD_FILE_BASE_URL and the startup folder first on
PYTHONPATH. Mock websites (websites.py), GitLab (gitlab_server.py) and a proxy
(proxy.py) are the unit's services.

units    - every task of the dataset, as domain__id
setup    - the task's instruction, for the null control, from nop.py
           --instruction: loading a task runs its code, so it runs apart
answer   - the Original setup: the agent option's script (agents.json) on the
           one task, its model calls through the unit's relay, whose stop file
           ends the episode with DONE
score    - the upstream's result.txt and result.json; for the null control,
           nop.py's. A failed LLM grader or null-control setup, or a read of
           the VM or a site that the evaluation could not make, is the
           score's error, never a 0

Units on one machine start their VMs one after another under START_LOCK,
OSWorld1's own file. A reaper in a session of its own, armed as the script
starts, ends the script's processes once the driver lets it go or dies,
removes the unit's containers and volumes, and packs its api_logs
(api_logs.py)."""
import contextlib
import errno
import fcntl
import glob
import gzip
import importlib.util
import io
import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zlib

if sys.argv[1:2] in (["--reap"], ["--ward"]):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
import gitlab_server
import proxy
import websites
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unit import (PROVIDER_IMAGE, RESULTS, STARTED, START_SECONDS, GRACE, DOCKER_SECONDS, VOLUMES, VOLUME,
                  NOTE_SECONDS, SNAPSHOT_SECONDS, NOTE_BYTES, CLOSE_SECONDS, SETUP_FAILED, COLOUR, LISTS, JUDGED,
                  ASKS_USER, GRADER_FAILED, JUDGE_ROUTE, IMAGE_BYTES, GRADER_STATUS, GRADER_RECORD, HEAD, SET_UP_DONE,
                  RUNNER, SETUP_DOC, PACK_LOG, _unit, units, _gone, _pack, EnvironmentSetupError, CancelledError,
                  CauseUnknownError, VerifierError, NoAnswer, _setup_failed, _tail, _base, _graded, _asks_user, _nat,
                  _fill, _result, _traj, _stop_file, _setup_errors, _grader_cause, _refusal)

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
UP = os.environ["NULLIUS_UPSTREAM"]
_ARGS = sys.argv[1:6] if sys.argv[1:2] not in (["--reap"], ["--ward"]) else []
_FILES = [os.path.join(BENCH, "agents.json")] + [os.path.join(HERE, n) for n in ("nop.py", os.path.join("startup", "sitecustomize.py"),
                                          "websites.compose.yml", "gitlab.compose.yml")]
AGENTS_FILE, NOP, SITE, websites.OVERRIDE, gitlab_server.OVERRIDE = (_ARGS + _FILES[len(_ARGS):])[:5]
websites.SITE = SITE
with open(AGENTS_FILE, encoding="utf-8") as _f:
    AGENTS = json.load(_f)
# each entry an agent of the catalogue (start.driver): the driver's spec by its upstream value, the agent option's
AGENTS["agents"] = {e["start"]["driver"]["agent"]: dict(e["start"]["driver"], models=e["models"])
                    for e in AGENTS["agents"].values()}
PASSWORD = AGENTS["common"][AGENTS["common"].index("--client_password") + 1]
EXAMPLES = os.path.join(UP, "evaluation_examples")
VM = os.path.join(UP, "docker_vm_data", "osworld-v2-ubuntu-x86.qcow2")
ASSETS = os.path.abspath(os.path.join(UP, "cache", "osworld_v2_assets"))
START_LOCK = "/tmp/nullius-osworld1-vm-start.lck"
SWEEPS = (2, 8, 20)
READY_SECONDS = 120
EPISODE_SECONDS = 36000
EVALUATE_SECONDS = 3600
STOPPED_BY = (("turn limit", "time"), ("context window", "context window"), ("batch cap", "money"), ("key cap", "money"),
              ("forecast", "money"))
HELD_SECONDS = 300
RECORD = re.compile(r"^(?:\x1b\[[0-9;]*m)?\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
CAUSE = re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt)|ERROR)(?::|$)")
LISTS["websites98"] = sorted(LISTS["no_services63"] + LISTS["websites35"])
RETRIES_SITE_READS = frozenset({"069"})
GUEST_PORTS = {"082": (3000,)}
MAILBOXES = {"001": ("task_001_thunderbird-profile_fixed.tar.gz",), "006": ("thunderbird-profile.tar.gz",),
             "010": ("email_010.tar.gz",)}
SIGN_INS = re.compile(r"(?:^|/)(?:logins[^/]*\.json|signons[^/]*|cookies\.sqlite[^/]*|user\.js)$|/storage/default(?:/|$)")
ONLINE = re.compile(r'^(\s*user_pref\("mail\.server\.\w+\.(?:login_at_startup|check_new_mail|download_on_biff)",\s*)'
                    r'true(\s*\);)[ \t\r]*$', re.M)
OFFLINE = 'user_pref("offline.autoDetect", false);\nuser_pref("offline.startup_state", 3);\n'
PORT_LOCK = "/tmp/nullius-osworld2-port-{}.lck"
CONTROLLER = re.compile(HEAD + r"(?:python/\d+-[^\]]*|desktopenv\.pycontroller)\] (.*)$")
THINGS = "file|screenshot|accessibility tree|terminal output|screen size|window size|wallpaper|desktop path|directory tree"
READ = re.compile(rf"An error occurred while trying to get (?:the )?(?P<lost>{THINGS}): |Failed to get (?P<said>{THINGS})\. "
                  rf"Status code: (?P<status>\d+)$|Retrying to get (?:the )?(?P<again>{THINGS})\.$|Failed to get (?P<gave>{THINGS})\.$")
STATE = re.compile(HEAD + r"(?:state/\d+-[^\]]*|desktopenv\.getters\.state)\] \[GET_STATE\w*\] (?:Failed to "
                   r"(fetch state|download file) from \S+: (.*)$)")
SITE_STATUS = re.compile(r"\b(\d{3}) (?:Client|Server) Error\b")
GATEWAY = ("502", "503", "504")
WRITE_OPTIONS = ("NULLIUS_OPTION_WRITE_MBPS", "NULLIUS_OPTION_WRITE_IOPS")
PROBE = "nullius-osworld2-probe-"
PROBE_SECONDS = 600
_STARTUP = []
_PORTS = []
_ARMED = {}
_OWN = []






def _task_file(domain, eid):
    path = os.path.join(EXAMPLES, "task_class", f"task_{eid}.py")
    if not os.path.isfile(path):
        raise RuntimeError(f"no task class for {domain}__{eid} at {path}: put the gated task classes in "
                           f"{os.path.dirname(path)} before a run ({SETUP_DOC})")
    return path


def _task(dir, domain, eid):
    out = os.path.join(dir, "instruction.json")
    _run([sys.executable, NOP, "--instruction", EXAMPLES, domain, eid, out],
         dir, _env(eid), os.path.join(dir, "setup.log"), "the task's loading")
    with open(out, encoding="utf-8") as f:
        task = json.load(f)
    os.remove(out)
    return task


def _letting_go(handler):
    def let_go(*a, **kw):
        try:
            return handler(*a, **kw)
        except BaseException:
            while _OWN:
                os.close(_OWN.pop())
            raise
    return let_go


@_letting_go
def setup(unit, dir, **_):
    domain, eid = _unit(unit)
    _fresh(dir)
    _task_file(domain, eid)
    websites.needed(eid)
    gitlab_server.needed(eid)
    _workspace(dir)
    return {"prompt": _task(dir, domain, eid)["instruction"]}


LINES = queue.Queue()
CLOSED = threading.Event()


class _Waiting:
    """What a wait before the runner ends on: the unit's pipe closed, or its turn limit passed."""
    def __init__(self, until=None):
        self.until = until

    def wait(self, seconds):
        return CLOSED.wait(seconds) or bool(self.until and time.time() > self.until)


def _read():
    for line in sys.stdin:
        LINES.put(line)
    CLOSED.set()
    LINES.put(None)


def _arm(pgid, dir, vms):
    """A reaper that starts once this process closes its pipe or dies; the locks it is given stay held until it ends."""
    r, w = os.pipe()
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--reap", str(pgid), dir, "vms" if vms else "group"],
                         stdin=r, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         pass_fds=websites.held_fds() + tuple(_PORTS) + tuple(_OWN))
    except BaseException:
        os.close(w)
        raise
    finally:
        os.close(r)
    _ARMED[pgid] = w


def _reaper(pgid, dir, vms, seen=()):
    """``seen``: the volumes this driver saw."""
    if pgid not in _ARMED:
        _arm(pgid, dir, vms)
    with contextlib.suppress(OSError), os.fdopen(_ARMED.pop(pgid), "w") as f:
        f.write(" ".join(v for v in dict.fromkeys(seen) if VOLUME.fullmatch(v)))


def _stop(p, log, what, vms, seen=()):
    """The reaper takes the process's group over.
    The upstream's own signal handlers are not relied on: they skip the provider's cleanup while a VM boots."""
    _reaper(p.pid, os.path.dirname(log), vms, seen)
    raise RuntimeError(f"{what} was stopped: the unit's time ran out")






def reap(pgid, dir, vms, seen=()):
    """SIGTERM to the group, GRACE seconds, SIGKILL; then the unit's containers removed and its api_logs packed.
    Volumes are read before the signals: the upstream removes a container without its volume."""
    seen = [v for v in seen if VOLUME.fullmatch(v)]
    if vms:
        done = threading.Event()
        done.set()
        snapshot = threading.Thread(target=_noting, args=(dir, done, seen), daemon=True)
        snapshot.start()
        snapshot.join(SNAPSHOT_SECONDS)
    ended = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            ended = True
            break
        except OSError:
            break
        if _gone(pgid, GRACE):
            ended = True
            break
    packing = threading.Thread(target=_pack, args=(dir,))
    if ended:
        packing.start()
    if vms:
        for wait in SWEEPS:
            time.sleep(wait)
            try:
                _remove_vms(dir, seen)
            except (RuntimeError, OSError):
                pass
        try:
            _old_probes()
        except (RuntimeError, OSError):
            pass
    if ended:
        packing.join()
















def _docker(*args, seconds=DOCKER_SECONDS):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        raise NoAnswer(f"docker {args[0]} got no answer in {seconds} s: is its daemon running?")
    except OSError:
        raise RuntimeError("no docker on this machine's PATH: the upstream's docker provider needs it")


def _asked(*args):
    """Waits READY_SECONDS, since a failed check before a VM start costs the unit."""
    try:
        return _docker(*args, seconds=READY_SECONDS)
    except NoAnswer as e:
        command = " ".join(a for a in args[:2] if not a.startswith("-"))
        raise NoAnswer(f"Docker's daemon did not answer docker {command} in {READY_SECONDS} s: it may be busy; "
                       "try again or run fewer units at once") from e


def _mine(dir):
    """The containers that mount the unit's image link, found by the provider image's digest, and their anonymous volumes."""
    mine = os.path.join(os.path.abspath(dir), "vm") + os.sep
    every = _docker("ps", "-a", "-q", "--no-trunc", "--filter", f"ancestor={PROVIDER_IMAGE}").stdout.split()
    if not every:
        return [], []
    out = _docker("inspect", "-f", "{{.Id}}\t{{json .Mounts}}", *every).stdout
    ids, volumes = [], []
    for cid, _, mounts in (ln.partition("\t") for ln in out.splitlines() if ln.strip()):
        try:
            mounts = [m for m in json.loads(mounts) or [] if isinstance(m, dict)]
        except ValueError:
            continue
        if any(str(m.get("Source", "")).startswith(mine) for m in mounts):
            ids.append(cid)
            volumes += [m["Name"] for m in mounts if m.get("Type") == "volume" and VOLUME.fullmatch(str(m.get("Name")))]
    return ids, volumes


def _noted(dir, volumes=()):
    """Notes each anonymous volume while its container runs: the upstream removes containers without their volumes.
    vm/volumes is read and written only as a plain file of this user's with one link."""
    volumes = [v for v in dict.fromkeys(volumes) if VOLUME.fullmatch(v)]
    try:
        fd = os.open(os.path.join(dir, "vm", VOLUMES),
                     os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        return volumes
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            return volumes
        fcntl.flock(fd, fcntl.LOCK_EX)
        known = list(dict.fromkeys(v for v in os.pread(fd, NOTE_BYTES, 0).decode("utf-8", "replace").split()
                                   if VOLUME.fullmatch(v)))
        new = [v for v in volumes if v not in known]
        if new:
            os.write(fd, "".join(v + "\n" for v in new).encode())
        return known + new
    except OSError:
        return volumes
    finally:
        os.close(fd)


def _noting(dir, done, seen):
    """Beside the wait for the process, so a slow docker never holds the unit's stop."""
    while True:
        try:
            found = _mine(dir)[1]
            seen.extend(found)
            _noted(dir, found)
        except (RuntimeError, OSError):
            pass
        if done.wait(NOTE_SECONDS):
            return


def _remove_vms(dir, seen=None):
    seen = [] if seen is None else seen
    ids, volumes = _mine(dir)
    seen.extend(volumes)
    noted = _noted(dir, volumes)
    if ids:
        _docker("rm", "-f", "-v", *ids)
    gone = [v for v in dict.fromkeys(noted + seen) if VOLUME.fullmatch(v)]
    if gone:
        _docker("volume", "rm", *gone)
    proxy.remove(dir)


def _started(p, log, what, until=None):
    """Until the VM container runs, the process ends, START_SECONDS pass or ``until``, so the next start does not queue."""
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < START_SECONDS and not (until and time.time() > until):
        if CLOSED.wait(1):
            _stop(p, log, what, True)
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                if STARTED in f.read():
                    return
        except OSError:
            pass




def _run(argv, cwd, env, log, what, stagger=False, failed=None, until=None, grace=EVALUATE_SECONDS):
    """``argv`` in a session of its own, handed to the reaper when it ends or the unit is stopped.
    Once ``failed`` exists, the run is an EnvironmentSetupError in the driver's words: the log can quote the task."""
    lock = None
    if stagger:
        with _set_up():
            lock = os.fdopen(websites.opened(START_LOCK))
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _Waiting(until).wait(1):
                    lock.close()
                    raise EnvironmentSetupError(f"{what} was not started: the unit's time ran out")
    try:
        with open(log, "a") as f:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        until = until and min(until, time.time() + EPISODE_SECONDS)
        try:
            _arm(p.pid, cwd, stagger)
            armed = None
        except Exception as e:
            armed = e
        if lock and not armed:
            _started(p, log, what, until)
    except OSError as e:
        raise EnvironmentSetupError(f"{what} did not start: {type(e).__name__}: {e}") from None
    finally:
        if lock:
            lock.close()
    if armed:
        try:
            reap(p.pid, cwd, stagger)
        except Exception:
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL)
            if stagger:
                with contextlib.suppress(RuntimeError, OSError):
                    _remove_vms(cwd)
        raise EnvironmentSetupError(f"{what}'s reaper did not start: {type(armed).__name__}: {armed}")
    done, seen, given, hard = threading.Event(), [], None, None
    try:
        if stagger:
            threading.Thread(target=_noting, args=(cwd, done, seen), daemon=True).start()
        while p.poll() is None:
            if CLOSED.wait(1):
                _stop(p, log, what, stagger, seen)
            if until and time.time() > until:
                hard = hard or time.time() + grace
                try:
                    os.makedirs(os.path.dirname(_stop_file(cwd)), exist_ok=True)
                    with open(_stop_file(cwd), "a") as f:
                        f.write("turn limit\n")
                    until = None
                except OSError:
                    pass
            if hard and time.time() > hard:
                _reaper(p.pid, cwd, stagger, seen)
                raise (VerifierError if _acted(cwd) or failed and _set_up_done(log) else EnvironmentSetupError)(
                    f"{what} did not end {grace} s after the unit's turn limit, and was stopped")
            if failed and given is None and os.path.exists(failed):
                given = time.time()
            if given is not None and time.time() - given > CLOSE_SECONDS:
                _reaper(p.pid, cwd, stagger, seen)
                raise EnvironmentSetupError(f"{what} did not end {CLOSE_SECONDS} s after its failure, and was "
                                            f"stopped: {_setup_failed(failed)} (the whole log: {log})")
    finally:
        done.set()
        if p.pid in _ARMED:
            _reaper(p.pid, cwd, stagger, seen)
    if stagger:
        try:
            _remove_vms(cwd, seen)
        except (RuntimeError, OSError):
            pass
    said = _setup_failed(failed) if failed else None
    if said:
        raise EnvironmentSetupError(f"{what} exited {p.returncode}: {said} (the whole log: {log})")
    if p.returncode != 0:
        kind, said = _tail(log, p.returncode)
        raise kind(f"{what} exited {p.returncode}: {said}")




def _env(eid, assets=ASSETS, **extra):
    """GitLab's token only for a task that needs it."""
    return dict(os.environ, PYTHONPATH=os.pathsep.join([os.path.dirname(os.path.abspath(SITE)), UP]),
                PYTHONIOENCODING="utf-8", OSWORLD_FILE_BASE_URL=assets, **websites.env(), **gitlab_server.env(eid),
                **extra)


def _assets(dir, eid):
    """The assets, with the task's mail profiles signed out of their live mailboxes and started offline."""
    if eid not in MAILBOXES:
        return ASSETS
    own, root = f"task_{eid}", os.path.join(dir, "assets")
    names = os.listdir(os.path.join(ASSETS, own))
    missing = sorted(set(MAILBOXES[eid]) - set(names))
    if missing:
        raise RuntimeError(f"no {', '.join(missing)} in {os.path.join(ASSETS, own)}")
    os.makedirs(os.path.join(root, own), exist_ok=True)
    for src, dst in [(os.path.join(ASSETS, n), os.path.join(root, n)) for n in os.listdir(ASSETS) if n != own] + \
            [(os.path.join(ASSETS, own, n), os.path.join(root, own, n)) for n in names if n not in MAILBOXES[eid]]:
        if not os.path.lexists(dst):
            os.symlink(src, dst)
    for name in MAILBOXES[eid]:
        _signed_out(os.path.join(ASSETS, own, name), os.path.join(root, own, name))
    return root


def _signed_out(src, dst):
    with tarfile.open(src) as old, open(dst + ".part", "wb") as raw, \
            gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as new:
        for m in old:
            name = os.path.normpath(m.name)
            if m.islnk() or m.issparse() or os.path.isabs(name) or name.split(os.sep)[0] == "..":
                raise RuntimeError(f"{os.path.basename(src)} has {m.name}, which this driver does not rewrite")
            if SIGN_INS.search(name):
                continue
            data = old.extractfile(m) if m.isfile() else None
            if data and name.endswith("/prefs.js"):
                text = (ONLINE.sub(r"\1false\2", data.read().decode("utf-8")).rstrip("\n") + "\n" + OFFLINE).encode("utf-8")
                data, m.size = io.BytesIO(text), len(text)
                m.pax_headers.pop("size", None)
            new.addfile(m, data)
    os.replace(dst + ".part", dst)


def _port_free(eid, port):
    """Docker's proxy and this bind both set SO_REUSEADDR, so a connection closing there is no refusal.
    localhost can be ::1 first, so a listener there would answer the task."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                raise RuntimeError(f"task {eid} reaches its VM's port {port} at this machine's localhost:{port}, "
                                   f"and another process listens on {host} there: free that port, or run the "
                                   "task on another machine") from None


@contextlib.contextmanager
def _published(eid, closed=_Waiting()):
    """The task's GUEST_PORTS, for one VM at a time on this machine; the reaper keeps the locks until that VM is gone."""
    fds = []
    try:
        for port in GUEST_PORTS.get(eid, ()):
            try:
                fds.append(websites.opened(PORT_LOCK.format(port)))
            except OSError as e:
                raise RuntimeError(f"the lock {PORT_LOCK.format(port)} cannot be opened ({type(e).__name__}): "
                                   "remove it if it is not a plain file") from None
            while True:
                try:
                    fcntl.flock(fds[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if closed.wait(1):
                        raise RuntimeError(f"the unit's time ran out while another unit's VM held port {port}")
            _port_free(eid, port)
        _PORTS.extend(fds)
        yield {_startup().PORTS: " ".join(map(str, GUEST_PORTS[eid]))} if fds else {}
    finally:
        for fd in fds:
            if fd in _PORTS:
                _PORTS.remove(fd)
            os.close(fd)








def vm_image():
    """A unit never fetches the image: an interrupted unpacking leaves a broken one."""
    if not os.path.isfile(VM):
        raise RuntimeError(f"no VM image at {VM}: put osworld-v2-ubuntu-x86.qcow2 there before a run ({SETUP_DOC})")
    return VM




def _provider_image():
    """Pulled inside the provider's port lock, it would hold the next units' starts past their wait."""
    if _asked("image", "inspect", PROVIDER_IMAGE).returncode != 0:
        raise RuntimeError(f"no {PROVIDER_IMAGE} on this machine: docker pull {PROVIDER_IMAGE} before a run ({SETUP_DOC})")


def _startup():
    """Loaded under another name, so it wraps nothing in this process."""
    if not _STARTUP:
        spec = importlib.util.spec_from_file_location("osworld2_startup", SITE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STARTUP.append(module)
    return _STARTUP[0]


def _last(said, otherwise):
    return ((said or "").strip().splitlines() or [otherwise])[-1]


def _old_probes(ready=False):
    """This user's probes whose driver is gone or older than PROBE_SECONDS; another user's stay."""
    docker = _asked if ready else _docker
    names = docker("ps", "-a", "--filter", f"name={PROBE}{os.getuid()}-", "--format", "{{.Names}}").stdout.split()
    old = []
    for name in names:
        m = re.fullmatch(re.escape(f"{PROBE}{os.getuid()}-") + r"(\d+)-(\d+)", name)
        if not m:
            continue
        try:
            os.kill(int(m.group(1)), 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except OSError:
            alive = True
        if not alive or time.time_ns() - int(m.group(2)) > PROBE_SECONDS * 10 ** 9:
            old.append(name)
    if old:
        docker("rm", "-f", *old)


def _probe_name():
    return f"{PROBE}{os.getuid()}-{os.getpid()}-{time.time_ns()}"


def _write_limit():
    """Proof before a VM start, by a probe container, that a set write limit holds: a daemon that cannot limit drops it.
    An unanswered probe create is tried once more under a new name."""
    if os.environ.get("NULLIUS_OPTION_VM_PROVIDER", "docker") != "docker" \
            or all(os.environ.get(n, "none") == "none" for n in WRITE_OPTIONS):
        return
    if not os.path.isfile(SITE):
        raise RuntimeError(f"the VM's disk write limit cannot be applied: no {SITE}, which sets it in the unit's "
                           "processes; set Disk write MB/s and Disk write IOPS to none, or restore the file")
    site = _startup()
    limits = site.wanted(os.environ)
    cannot = "the VM's disk write limit cannot be applied: "
    if importlib.util.find_spec("docker") is None:
        raise site.WriteLimitError(cannot + f"no docker-py in {sys.executable}'s environment, which the upstream's "
                                   "docker provider needs too: install the driver's environment again")
    info = _asked("info", "--format", "{{.DockerRootDir}}\t{{.CgroupVersion}}")
    root, _, cgroup = (part.strip() for part in info.stdout.partition("\t"))
    if info.returncode != 0 or not root:
        raise site.WriteLimitError(cannot + "docker info gave no root (" + _last(info.stderr, "no answer")
                                   + "): is Docker's daemon running and usable by this user?")
    if cgroup != "2":
        raise site.WriteLimitError(cannot + f"Docker runs on cgroup v{cgroup or '?'}, where buffered writes pass a "
                                   f"device limit; {site.ACTION}")
    device = site.root_disk(root)
    _old_probes(ready=True)
    flags = [x for _, rate, _, flag in limits for x in (flag, f"{device}:{rate}")]

    def create(name):
        return _asked("create", "--name", name, "--network", "none", "--mount", "type=tmpfs,destination=/storage",
                      *flags, PROVIDER_IMAGE)

    probe = _probe_name()
    try:
        try:
            made = create(probe)
        except NoAnswer as e:
            if _asked("rm", "-f", probe).returncode != 0:
                raise e
            probe = _probe_name()
            made = create(probe)
        if made.returncode != 0:
            raise site.WriteLimitError(cannot + "a probe container was refused: " + _last(made.stderr, "no reason")
                                       + f"; {site.ACTION}")
        try:
            host = json.loads(_asked("inspect", "-f", "{{json .HostConfig}}", probe).stdout or "{}") or {}
        except ValueError:
            host = {}
    finally:
        try:
            _asked("rm", "-f", probe)
        except RuntimeError:
            pass
    dropped = [field for _, rate, field, _ in limits if {"Path": device, "Rate": rate} not in (host.get(field) or [])]
    if dropped:
        said = _last(made.stderr, "")
        raise site.WriteLimitError(cannot + f"Docker kept no {', '.join(dropped)} on {device}"
                                   + (f" ({said})" if said else "") + f"; {site.ACTION}")


def _ready(domain, eid):
    """What a VM start of the task needs, checked before one, so it does not fail minutes later; no /dev/kvm is a warning."""
    image = vm_image()
    _task_file(domain, eid)
    sites = websites.needed(eid)
    lab = gitlab_server.needed(eid)
    _startup().network(_asked)
    if sites or lab:
        websites.ready(websites.suffix(), state=False)
    if not os.path.isdir(ASSETS) or not os.listdir(ASSETS):
        raise RuntimeError(f"no assets at {ASSETS}: put the gated assets there before a run ({SETUP_DOC})")
    _provider_image()
    _write_limit()
    if not _nat():
        raise RuntimeError("the kernel module iptable_nat is not loaded, and the VM's port forwarding needs it: "
                           "sudo modprobe ip_tables iptable_nat")
    if not os.path.exists("/dev/kvm"):
        print("no /dev/kvm: the VM runs without KVM, slowly, and a slow VM can change a task's outcome",
              file=sys.stderr, flush=True)
    return image


@contextlib.contextmanager
def _gitlab(eid, dir, closed=_Waiting()):
    """GitLab made once and started from its snapshot for a task of gitlab_server.TASKS, its warden first, however
    the unit ends."""
    value = gitlab_server.needed(eid)
    with gitlab_server.held(value, closed):
        if not value:
            yield
            return
        where = gitlab_server.home()
        limits = gitlab_server.unit(where, dir)
        with _warden(dir):
            gitlab_server.prepare(where, say=lambda said: print(said, file=sys.stderr, flush=True), closed=closed,
                                  limits=limits)
            gitlab_server.up(where, limits, closed)
            yield


@contextlib.contextmanager
def _warden(dir):
    """The warden removes GitLab once this process closes its pipe or dies, then lets the locks go."""
    r, w = os.pipe()
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--ward", str(r), dir], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         pass_fds=websites.held_fds() + (r,))
    finally:
        os.close(r)
    try:
        yield
    finally:
        os.close(w)


def _disk():
    """The disk the VMs' writes are limited on, for the proxy's own limit; None with no limit."""
    if all(os.environ.get(n, "none") == "none" for n in WRITE_OPTIONS):
        return None
    site = _startup()
    site.wanted(dict(os.environ, **proxy.TAKEN))
    return site.root_disk(_asked("info", "--format", "{{.DockerRootDir}}").stdout.strip())


def _workspace(dir):
    """The unit's folder as the upstream's root for the paths its tasks name."""
    link = os.path.join(dir, "evaluation_examples")
    if not os.path.lexists(link):
        os.symlink(EXAMPLES, link)
    os.makedirs(os.path.join(dir, "logs"), exist_ok=True)


def _vm(dir, domain, eid):
    """The unit's link to the VM image, which the provider mounts by its path: the reaper's mark."""
    image = _ready(domain, eid)
    _workspace(dir)
    vm = os.path.join(dir, "vm", os.path.basename(VM))
    os.makedirs(os.path.dirname(vm), exist_ok=True)
    if not os.path.lexists(vm):
        os.symlink(image, vm)
    return vm


def _agent():
    name = os.environ.get("NULLIUS_OPTION_AGENT", "m3")
    if name not in AGENTS["agents"]:
        raise RuntimeError(f"no agent {name} in agents.json")
    return name, AGENTS["agents"][name]


def _paired(model):
    """The agent option's agent and its entry, for a model it is published with only."""
    name, spec = _agent()
    if model not in spec["models"]:
        others = [n for n, s in AGENTS["agents"].items() if model in s["models"]]
        raise RuntimeError(f"{model} is not one of the {name} agent's models ({', '.join(spec['models'])}): "
                           + (f"choose the agent {others[0]} for it, or one of them" if others else "choose one of them"))
    return name, spec








def _command(dir, model, base_url, key, vm):
    """M3's thinking option is M3_THINKING_MODE (empty: the documented default); Muse Spark's reasoning effort is in its argv.
    A dotenv file the script loads overrides no variable already set."""
    name, spec = _paired(model)
    meta = os.path.join(dir, "test_unit.json")
    slots = {"model": model, "base_url": base_url, "key": key, "vm": vm, "up": UP, "meta": meta,
             "results": os.path.join(dir, RESULTS), "examples": EXAMPLES,
             "base": _base(base_url)}
    common, drop = list(AGENTS["common"]), spec.get("drop", [])
    for flag in drop:
        i = common.index(flag)
        del common[i:i + (1 if flag == "--headless" else 2)]
    argv = [sys.executable, os.path.join(UP, spec["script"])] + [_fill(a, slots) for a in
                                                                 common + spec["argv"] + spec.get("model_argv", {}).get(model, [])]
    steps = os.environ.get("NULLIUS_OPTION_MAX_STEPS", "default")
    if steps != "default" and "--max_steps" in argv:
        argv[argv.index("--max_steps") + 1] = steps
    elif steps != "default":
        argv += ["--max_steps", steps]
    extra = {k: _fill(v, slots) for k, v in spec["env"].items()}
    if name == "m3":
        thinking = os.environ.get("NULLIUS_OPTION_THINKING", "off")
        extra.update(M3_THINKING_MODE="adaptive" if thinking == "adaptive" else "", M3_THINKING_BUDGET="")
    return spec, meta, argv, extra


@contextlib.contextmanager
def _set_up():
    """What fails before the upstream's process starts is the unit's environment's."""
    try:
        yield
    except (RuntimeError, OSError, LookupError, ValueError, subprocess.SubprocessError, tarfile.TarError, EOFError,
            zlib.error) as e:
        raise e if isinstance(e, EnvironmentSetupError) else EnvironmentSetupError(f"{type(e).__name__}: {e}") from None


def _services(held, dir, domain, eid, until=None):
    closed = _Waiting(until)
    ports = held.enter_context(_published(eid, closed))
    held.enter_context(websites.held(eid, websites.needed(eid), closed))
    held.enter_context(_gitlab(eid, dir, closed))
    return dict(ports, **held.enter_context(proxy.held(dir, _task(dir, domain, eid)["proxy"], _disk)))


@_letting_go
def answer(unit, dir, setup, model, base_url, key, judge=None, **_):
    if setup != "original":
        raise RuntimeError(f"unknown setup {setup}: OSWorld2 runs original and its null control")
    until = time.time() + _turn_seconds() - EVALUATE_SECONDS - CLOSE_SECONDS
    domain, eid = _unit(unit)
    with contextlib.ExitStack() as held:
        with _set_up():
            _fresh(dir)
            script = os.path.basename(_agent()[1]["script"])
            log = os.path.join(dir, script.replace(".py", ".log"))
            graded = _graded(eid, judge)
            _asks_user(eid, *_paired(model))
            vm = _vm(dir, domain, eid)
            spec, meta, argv, extra = _command(dir, model, base_url, key, vm)
            extra.update(graded, OSWORLD2_STOP=_stop_file(dir))
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({domain: [eid]}, f)
            services = _services(held, dir, domain, eid, until)
            assets = _assets(dir, eid)
        _run(argv, dir, _env(eid, assets, **extra, **services), log, script, stagger=True, until=until)
    where = _result(dir, domain, eid)
    if where is None:
        kind, said = _tail(log)
        raise kind(f"{script} wrote no result.txt: {said}")
    steps = _traj(where)
    last = steps[-1].get("action") if steps else ""
    return {"answer": last if isinstance(last, str) else json.dumps(last), "model": model}


def _fresh(dir):
    while _OWN:
        os.close(_OWN.pop())
    os.makedirs(dir, exist_ok=True)
    _OWN.append(websites.opened(os.path.join(dir, "unit.lck")))
    t0 = time.time()
    while True:
        try:
            fcntl.flock(_OWN[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if CLOSED.wait(1) or time.time() - t0 > HELD_SECONDS:
                raise EnvironmentSetupError("an earlier run of this unit, or its reaper, still holds its folder")
    ns = time.time_ns()
    for name in [RESULTS] + [os.path.basename(p) for p in glob.glob(os.path.join(glob.escape(dir), "*.log"))]:
        if os.path.lexists(os.path.join(dir, name)):
            os.replace(os.path.join(dir, name), os.path.join(dir, f"{name}.{ns}"))


def _version():
    """This version's block of standard.json over the shared fields, as the runner resolves it."""
    with open(os.path.join(BENCH, "standard.json"), encoding="utf-8") as f:
        d = json.load(f)
    return dict(d, **(d.get("versions") or {}).get(os.environ.get("NULLIUS_VERSION") or "", {}))


def _turn_seconds():
    return _version()["setups"]["original"]["turn_seconds"]


def _score_seconds():
    return (_version().get("limits") or {}).get("score_seconds") or 600


def _acted(dir):
    for path in glob.glob(os.path.join(glob.escape(os.path.dirname(_stop_file(dir))), "*.jsonl")):
        if "-" not in os.path.basename(path):
            for call in _traj(os.path.dirname(path), os.path.basename(path)):
                if isinstance(call, dict) and not call.get("side"):
                    return True
    return False




def _said(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f.read().splitlines() if ln.strip()]


def _stopped(dir):
    """The stop that ended the episode's step (sitecustomize's .taken, the stop file if taken before it was written),
    its first word and every reason's kind."""
    try:
        said = _said(_stop_file(dir) + ".taken")
    except OSError:
        return {}
    with contextlib.suppress(OSError):
        said = said or _said(_stop_file(dir))
    return {"stopped": said[0] if said else "stop", "stopped_by": list(dict.fromkeys(
        next((why for start, why in STOPPED_BY if ln.startswith(start)), "other") for ln in said))}


def _scored(where, how):
    """The safety checks' own words are the task's, so they stay in the trial's result.json."""
    try:
        with open(os.path.join(where, "result.txt"), encoding="utf-8") as f:
            value = float(f.read().strip())
    except (OSError, ValueError):
        raise VerifierError("result.txt could not be read as a number") from None
    if not -1e-9 <= value <= 1 + 1e-9:
        raise VerifierError(f"result.txt holds {value}, not a score from 0 to 1")
    steps = _traj(where)
    detail = {"result": value, "by": how, "steps": len(steps),
              "last_action": steps[-1].get("action") if steps else None}
    try:
        with open(os.path.join(where, "result.json"), encoding="utf-8") as f:
            evaluated = json.load(f)
    except (OSError, ValueError):
        evaluated = {}
    if isinstance(evaluated, dict):
        if "score" in evaluated:
            detail["score"] = evaluated["score"]
        safety = evaluated.get("safety")
        if isinstance(safety, dict):
            checks = [c for c in safety.get("checks") or [] if isinstance(c, dict)]
            detail["safety"] = {"total_penalty": safety.get("total_penalty"), "checks": len(checks),
                                "failed": sum(1 for c in checks if c.get("result") == "FAIL")}
    return {"reward": value, "detail": detail}


def _set_up_done(log):
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return any(SET_UP_DONE.match(COLOUR.sub("", ln).rstrip("\n")) for ln in f)
    except OSError:
        return False






def _grader_failed(*logs, refused=0):
    """The upstream counts a grader error as a failed comparison; only its own ERROR record counts (_grader_cause).
    Each of the ``refused`` calls of the agent's files accounts for one 400 or 422 record."""
    for log in logs:
        left = refused
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = GRADER_RECORD.match(COLOUR.sub("", line).rstrip("\n"))
                    status = GRADER_STATUS.search(m.group(2)) if m and left else None
                    if status and status.group(1) in ("400", "413", "422") and m.group(1) in GRADER_FAILED:
                        left -= 1
                    elif m and m.group(1) in GRADER_FAILED:
                        raise VerifierError(f"the LLM grader failed in {m.group(1)[len('Error in '):-2]} "
                                           f"({_grader_cause(m.group(2))}), and the upstream counted its "
                                           "comparison as failed; see the judge's calls")
        except OSError:
            continue


def _unread(value, *logs, retried=False):
    """A read of the VM or a site that the last episode's evaluation, any of its phases, gave up on unanswered, by what and how only."""
    for log in logs:
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                lines = [COLOUR.sub("", ln).rstrip("\n") for ln in f]
        except OSError:
            continue
        marks = [(i, m) for i, m in enumerate(map(RUNNER.match, lines)) if m]
        ends = [k for k, (_, m) in enumerate(marks) if not m["step"] and not m["phase"]]
        windows, end = [], marks[ends[-1]][0] if ends else None
        for i, m in reversed(marks[ends[-2] + 1 if len(ends) > 1 else 0:ends[-1]] if ends else []):
            if m["phase"]:
                end = i
            elif m["step"] and end is not None:
                windows.append((i, end))
                end = None
                if m["n"] in (None, "1"):
                    break
        site, unread = None, None
        for a, b in windows:
            last = None
            for ln in lines[a:b]:
                state, said = STATE.match(ln), CONTROLLER.match(ln)
                read = READ.match(said.group(1)) if said else None
                status = SITE_STATUS.search(state.group(2)) if state else None
                if state and not site and not retried and (not status or status.group(1) in GATEWAY):
                    site = (f"a site's {'state' if state.group(1) == 'fetch state' else 'file'}", f"HTTP {status.group(1)}"
                            if status else "timed out" if "timed out" in state.group(2) else "no answer")
                elif read and (read["lost"] or read["said"]):
                    last = (read["lost"] or read["said"], read["status"])
                elif read and last and read["again"] == last[0]:
                    continue
                elif read and last and read["gave"] == last[0] and not last[1]:
                    unread = unread or (f"the VM's {last[0]}", "a broken connection")
                elif said:
                    last = None
        unread = unread or site
        if unread:
            raise VerifierError(f"the evaluation could not read {unread[0]} ({unread[1]}), and the upstream scored "
                               f"the task {value} without it; see the unit's {os.path.basename(log)}")




def _grader_refused(dir, judge):
    """A judge-route call not 200 counts unless a later call with the same request reaches 200; its status alone is said.
    Some task classes catch the grader's error and score it themselves. The class of each call that refused the agent's
    files."""
    route = JUDGE_ROUTE.search(str(judge.get("base_url") or "")) if isinstance(judge, dict) else None
    if not route:
        return []
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(dir)), ".work", "relay", route.group(1) + ".jsonl"),
                  encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    calls = []
    for ln in lines:
        try:
            call = json.loads(ln)
        except ValueError:
            continue
        if isinstance(call, dict):
            calls.append(call)
    if all(isinstance(c.get("t0"), (int, float)) for c in calls):
        calls.sort(key=lambda c: c["t0"])
    refused, failed = [], None
    for i, call in enumerate(calls):
        if call.get("status") == 200:
            continue
        asked, recovered = call.get("request"), False
        for retry in calls[i + 1:]:
            if not isinstance(asked, dict) or not asked.get("sha256") or retry.get("request") != asked:
                break
            if retry.get("status") == 200:
                recovered = True
                break
        why = None if recovered else _refusal(call)
        if not recovered and not why:
            failed = failed or call
        refused += [why] if why else []
    if failed:
        raise VerifierError(f"the LLM grader's endpoint answered {failed.get('status')}, and the task was "
                           "scored without it; see the judge's calls")
    return refused


def score(*a, **kw):
    try:
        return _score(*a, **kw)
    finally:
        while _OWN:
            os.close(_OWN.pop())


def _score(unit, dir, transcript=None, answer=None, setup=None, judge=None, **_):
    domain, eid = _unit(unit)
    if setup == "original":
        where = _result(dir, domain, eid)
        if where is None:
            raise VerifierError("no result.txt: the upstream's runner did not finish the task")
        script = os.path.basename(_agent()[1]["script"])
        stopped = _stopped(dir)
        if stopped and not _acted(dir):
            raise (EnvironmentSetupError if stopped["stopped"] == "turn limit" else CancelledError)(
                f"the unit was stopped ({stopped['stopped']}) before the agent's first step")
        refused = _grader_refused(dir, judge)
        logs = os.path.join(where, "eval.log"), os.path.join(dir, script.replace(".py", ".log"))
        _grader_failed(*logs, refused=len(refused))
        scored = _scored(where, script + ", DesktopEnv.evaluate")
        _unread(scored["reward"], *logs, retried=eid in RETRIES_SITE_READS)
        return dict(scored, **stopped, **({"grader_refused": sorted(set(refused))} if refused else {}),
                    **_setup_errors(*logs))
    if setup is not None:
        raise RuntimeError(f"unknown setup {setup}: OSWorld2 runs original and its null control")
    if answer:
        raise RuntimeError("OSWorld2 has no agent side yet: agent.computer/v1 has no kit")
    until = time.time() + _score_seconds() - 2 * CLOSE_SECONDS
    where = os.path.join(dir, RESULTS, "nop", domain, eid)
    failed = os.path.join(where, SETUP_FAILED)
    with contextlib.ExitStack() as held:
        with _set_up():
            graded = _graded(eid, judge)
            vm = _vm(dir, domain, eid)
            os.makedirs(where, exist_ok=True)
            if os.path.lexists(failed):
                os.remove(failed)
            services = _services(held, dir, domain, eid, until)
            assets = _assets(dir, eid)
        _run([sys.executable, NOP, vm, PASSWORD, EXAMPLES, domain, eid, where],
             dir, _env(eid, assets, **graded, **services), os.path.join(dir, "nop.log"), "the null control's run",
             stagger=True, failed=failed, until=until, grace=CLOSE_SECONDS)
    if not os.path.exists(os.path.join(where, "result.txt")):
        kind, said = _tail(os.path.join(dir, "nop.log"))
        raise kind(f"the null control's run wrote no result.txt: {said}")
    refused = _grader_refused(dir, judge)
    _grader_failed(os.path.join(dir, "nop.log"), refused=len(refused))
    scored = _scored(where, "nop.py, DesktopEnv.evaluate")
    _unread(scored["reward"], os.path.join(dir, "nop.log"), retried=eid in RETRIES_SITE_READS)
    return dict(scored, **({"grader_refused": sorted(set(refused))} if refused else {}), **_setup_errors(os.path.join(dir, "nop.log")))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reap"]:
        reap(int(sys.argv[2]), sys.argv[3], sys.argv[4:5] == ["vms"], sys.stdin.read().split())
        sys.exit(0)
    if sys.argv[1:2] == ["--ward"]:
        gitlab_server.ward(int(sys.argv[2]), sys.argv[3])
        sys.exit(0)
    HANDLERS = {"units": units, "setup": setup, "answer": answer, "score": score}
    threading.Thread(target=_read, daemon=True).start()
    while True:
        line = LINES.get()
        if line is None:
            break
        try:
            req = json.loads(line)
            out = HANDLERS[req.pop("op")](**req)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)
