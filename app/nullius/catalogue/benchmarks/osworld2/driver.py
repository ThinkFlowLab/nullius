"""OSWorld-V2's driver for the Nullius standard (JSON lines on stdin and stdout): the upstream unchanged.

It runs the Original setup and the null control. OSWorld-V2's own code at the
tested commit ($NULLIUS_UPSTREAM) loads, acts and scores, in processes of its
own; this driver imports none of it and patches nothing. Each unit's script
runs in the unit's folder, so its logs and results stay with the trial. Every
process gets the upstream's own variables: the assets as
OSWORLD_FILE_BASE_URL, the websites option as WEBSITE_HOST_SUFFIX. The mock
websites are started beforehand as their README says. GitLab tasks and those
that ask for the paid proxy are refused before a VM starts.
osworld2@hardened (hardened/) is the patched version.

units    - every task of the dataset, as domain__id
setup    - the task's instruction, for the null control, from nop.py
           --instruction: loading a task runs its code, so it runs apart
answer   - the Original setup: the agent option's script (agents.json) on the
           one task, its model calls through the unit's relay; a guard's stop
           file or the turn limit ends its processes from outside, unscored
score    - the upstream's result.txt and result.json as written; a failed
           LLM grader call is noted beside the score, never changes it; a
           failed null-control setup is the score's error

Units on one machine start their VMs one after another under START_LOCK,
OSWorld1's own file. A reaper in a session of its own, armed as the script
starts, ends the script's processes once the driver lets it go or dies,
removes the unit's containers and volumes, and packs its api_logs
(api_logs.py)."""
import contextlib
import fcntl
import glob
import json
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time

if sys.argv[1:2] == ["--reap"]:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
from unit import (PROVIDER_IMAGE, RESULTS, STARTED, START_SECONDS, GRACE, DOCKER_SECONDS, VOLUMES, VOLUME,
                  NOTE_SECONDS, SNAPSHOT_SECONDS, NOTE_BYTES, CLOSE_SECONDS, SETUP_FAILED, COLOUR, LISTS, JUDGED,
                  ASKS_USER, GRADER_FAILED, JUDGE_ROUTE, IMAGE_BYTES, GRADER_STATUS, GRADER_RECORD, SETUP_DOC,
                  PACK_LOG, _unit, units, _gone, _pack, EnvironmentSetupError, CancelledError, CauseUnknownError,
                  VerifierError, NoAnswer, _setup_failed, _tail, _base, _graded, _asks_user, _nat, _fill, _result,
                  _traj, _stop_file, _setup_errors, _grader_cause, _refusal)

HERE = os.path.dirname(os.path.abspath(__file__))
UP = os.environ["NULLIUS_UPSTREAM"]
_ARGS = sys.argv[1:3] if sys.argv[1:2] != ["--reap"] else []
AGENTS_FILE, NOP = (_ARGS + [os.path.join(HERE, n) for n in ("agents.json", "nop.py")][len(_ARGS):])[:2]
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
HELD_SECONDS = 300
LISTS["websites98"] = sorted(LISTS["no_services63"] + LISTS["websites35"])
WEBSITE_TASKS = frozenset("005 007 008 013 015 016 018 019 020 021 026 028 031 035 036 038 039 043 046 050 052 056 "
                          "057 058 060 065 068 069 070 072 073 077 079 081 082 091 098 100 101 102".split())
GITLAB_TASKS = frozenset(("026", "041"))
SUFFIX = re.compile(r"(?=.{3,200}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+")
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
         dir, _env(), os.path.join(dir, "setup.log"), "the task's loading")
    with open(out, encoding="utf-8") as f:
        task = json.load(f)
    os.remove(out)
    return task


def _suffix():
    """The websites option, the host suffix the upstream's mock websites are served under; None for none."""
    value = os.environ.get("NULLIUS_OPTION_WEBSITES", "none")
    if value == "none":
        return None
    if not SUFFIX.fullmatch(value):
        raise RuntimeError(f"Website host suffix {value!r} is not a host suffix: give the one the mock websites are "
                           "served under, or none")
    return value


def _sites(eid):
    """A task that reads WEBSITE_HOST_SUFFIX or GITLAB_URL at import cannot load without it: refused before a VM starts."""
    if eid in GITLAB_TASKS:
        raise RuntimeError(f"task {eid} needs the upstream's GitLab, which takes ports 80 and 443 as the mock websites "
                           f"do: the original does not run it ({SETUP_DOC})")
    if eid in WEBSITE_TASKS and not _suffix():
        raise RuntimeError(f"task {eid} needs the mock websites and this run gives them none: start them as their "
                           f"README says and set Website host suffix ({SETUP_DOC})")


def _offered(dir, domain, eid):
    """The task as its class loads; one that asks for the upstream's paid proxy is refused before a VM starts."""
    task = _task(dir, domain, eid)
    if task.get("proxy"):
        raise RuntimeError(f"task {eid} asks for the upstream's residential proxy (DataImpulse), whose credentials "
                           f"the release leaves as placeholders: the original does not run it ({SETUP_DOC})")
    return task


def _opened(path):
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
        raise OSError(f"{path} is not a plain file")
    return fd


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
    _sites(eid)
    _workspace(dir)
    return {"prompt": _offered(dir, domain, eid)["instruction"]}


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
                         pass_fds=tuple(_OWN))
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


def _started(p, log, what, until=None):
    """Until the VM container runs, the process ends, START_SECONDS pass, ``until`` or a guard's stop, so the next start
    does not queue."""
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < START_SECONDS and not (until and time.time() > until) \
            and not _guard(os.path.dirname(log)):
        if CLOSED.wait(1):
            _stop(p, log, what, True)
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                if STARTED in f.read():
                    return
        except OSError:
            pass




def _run(argv, cwd, env, log, what, stagger=False, failed=None, until=None):
    """``argv`` in a session of its own, handed to the reaper when it ends or the unit is stopped.
    Once ``failed`` exists, the run is an EnvironmentSetupError in the driver's words: the log can quote the task."""
    lock = None
    if stagger:
        with _set_up():
            lock = os.fdopen(_opened(START_LOCK))
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _Waiting(until).wait(1):
                    lock.close()
                    raise EnvironmentSetupError(f"{what} was not started: the unit's time ran out")
                if _guard(cwd):
                    lock.close()
                    raise CancelledError(f"{what} was not started: a guard asked the unit to stop ({_guard(cwd)})")
    try:
        with open(log, "a") as f:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
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
    done, seen, given = threading.Event(), [], None
    try:
        if stagger:
            threading.Thread(target=_noting, args=(cwd, done, seen), daemon=True).start()
        while p.poll() is None:
            if CLOSED.wait(1):
                _stop(p, log, what, stagger, seen)
            if until and time.time() > until:
                with contextlib.suppress(OSError):
                    os.makedirs(os.path.dirname(_stop_file(cwd)), exist_ok=True)
                    with open(_stop_file(cwd), "a") as f:
                        f.write("turn limit\n")
            guard = _guard(cwd)
            if guard:
                _reaper(p.pid, cwd, stagger, seen)
                raise CancelledError(f"{what} was stopped from outside, unscored: a guard asked the unit to stop ({guard})")
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




def _env(**extra):
    """The upstream on PYTHONPATH and the variables its wrapper sets, nothing of Nullius's."""
    sites = _suffix()
    return dict(os.environ, PYTHONPATH=UP, PYTHONIOENCODING="utf-8", OSWORLD_FILE_BASE_URL=ASSETS,
                **({"WEBSITE_HOST_SUFFIX": sites} if sites else {}), **extra)








def vm_image():
    """A unit never fetches the image: an interrupted unpacking leaves a broken one."""
    if not os.path.isfile(VM):
        raise RuntimeError(f"no VM image at {VM}: put osworld-v2-ubuntu-x86.qcow2 there before a run ({SETUP_DOC})")
    return VM




def _provider_image():
    """Pulled inside the provider's port lock, it would hold the next units' starts past their wait."""
    if _asked("image", "inspect", PROVIDER_IMAGE).returncode != 0:
        raise RuntimeError(f"no {PROVIDER_IMAGE} on this machine: docker pull {PROVIDER_IMAGE} before a run ({SETUP_DOC})")


def _ready(domain, eid):
    """What a VM start of the task needs, checked before one, so it does not fail minutes later; no /dev/kvm is a warning."""
    image = vm_image()
    _task_file(domain, eid)
    _sites(eid)
    if not os.path.isdir(ASSETS) or not os.listdir(ASSETS):
        raise RuntimeError(f"no assets at {ASSETS}: put the gated assets there before a run ({SETUP_DOC})")
    _provider_image()
    if not _nat():
        raise RuntimeError("the kernel module iptable_nat is not loaded, and the VM's port forwarding needs it: "
                           "sudo modprobe ip_tables iptable_nat")
    if not os.path.exists("/dev/kvm"):
        print("no /dev/kvm: the VM runs without KVM, slowly, and a slow VM can change a task's outcome",
              file=sys.stderr, flush=True)
    return image


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
    except (RuntimeError, OSError, LookupError, ValueError, subprocess.SubprocessError) as e:
        raise e if isinstance(e, EnvironmentSetupError) else EnvironmentSetupError(f"{type(e).__name__}: {e}") from None


@_letting_go
def answer(unit, dir, setup, model, base_url, key, judge=None, **_):
    if setup != "original":
        raise RuntimeError(f"unknown setup {setup}: OSWorld2 runs original and its null control")
    until = time.time() + _turn_seconds() - CLOSE_SECONDS
    domain, eid = _unit(unit)
    with _set_up():
        _fresh(dir)
        script = os.path.basename(_agent()[1]["script"])
        log = os.path.join(dir, script.replace(".py", ".log"))
        _sites(eid)
        graded = _graded(eid, judge)
        _asks_user(eid, *_paired(model))
        vm = _vm(dir, domain, eid)
        _offered(dir, domain, eid)
        spec, meta, argv, extra = _command(dir, model, base_url, key, vm)
        extra.update(graded)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({domain: [eid]}, f)
    _run(argv, dir, _env(**extra), log, script, stagger=True, until=until)
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
    _OWN.append(_opened(os.path.join(dir, "unit.lck")))
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
    with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
        d = json.load(f)
    return dict(d, **(d.get("versions") or {}).get(os.environ.get("NULLIUS_VERSION") or "", {}))


def _turn_seconds():
    return _version()["setups"]["original"]["turn_seconds"]


def _score_seconds():
    return (_version().get("limits") or {}).get("score_seconds") or 600




def _guard(dir):
    """What a guard's stop file says, as the relay reads it: a line other than its own context window's, else None."""
    try:
        with open(_stop_file(dir), encoding="utf-8") as f:
            said = [ln.strip() for ln in f.read().splitlines() if ln.strip() and ln.strip() != "context window"]
    except OSError:
        return None
    return said[0][:200] if said else None


def _scored(where, how):
    """The upstream's number, as written. The safety checks' own words are the task's, so they stay in the trial's
    result.json."""
    try:
        with open(os.path.join(where, "result.txt"), encoding="utf-8") as f:
            value = float(f.read().strip())
    except (OSError, ValueError):
        raise VerifierError("result.txt could not be read as a number") from None
    steps = _traj(where)
    detail = {"result": value, "by": how, "steps": len(steps),
              "last_action": steps[-1].get("action") if steps else None}
    if not -1e-9 <= value <= 1 + 1e-9:
        detail["out_of_range"] = "outside 0 to 1, kept as the upstream wrote it"
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






def _grader_failed(*logs, refused=0):
    """A note when the upstream counted a grader error as a failed comparison; only its own ERROR record counts
    (_grader_cause). Each of the ``refused`` calls of the agent's files accounts for one 400 or 422 record."""
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
                        return (f"the LLM grader failed in {m.group(1)[len('Error in '):-2]} "
                                f"({_grader_cause(m.group(2))}), and the upstream counted its comparison as failed")
        except OSError:
            continue
    return None




def _grader_refused(dir, judge):
    """The class of each call that refused the agent's files, and a note of a judge-route call not 200 that no later
    call with the same request recovered, by its status alone. Some task classes catch the grader's error and score it
    themselves."""
    route = JUDGE_ROUTE.search(str(judge.get("base_url") or "")) if isinstance(judge, dict) else None
    if not route:
        return [], None
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(dir)), ".work", "relay", route.group(1) + ".jsonl"),
                  encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return [], None
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
    return refused, failed and f"the LLM grader's endpoint answered {failed.get('status')}, and the task was scored without it"


def _judged(scored, dir, judge, *logs):
    """The upstream's score as it wrote it; a failed or refused grader call noted beside it, never changing it."""
    refused, failed = _grader_refused(dir, judge)
    failed = failed or _grader_failed(*logs, refused=len(refused))
    return dict(scored, **({"grader_refused": sorted(set(refused))} if refused else {}),
                **({"grader_failed": failed} if failed else {}), **_setup_errors(*logs))


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
        logs = os.path.join(where, "eval.log"), os.path.join(dir, script.replace(".py", ".log"))
        return _judged(_scored(where, script + ", DesktopEnv.evaluate"), dir, judge, *logs)
    if setup is not None:
        raise RuntimeError(f"unknown setup {setup}: OSWorld2 runs original and its null control")
    if answer:
        raise RuntimeError("OSWorld2 has no agent side yet: agent.computer/v1 has no kit")
    until = time.time() + _score_seconds() - 2 * CLOSE_SECONDS
    where = os.path.join(dir, RESULTS, "nop", domain, eid)
    failed = os.path.join(where, SETUP_FAILED)
    with _set_up():
        _sites(eid)
        graded = _graded(eid, judge)
        vm = _vm(dir, domain, eid)
        _offered(dir, domain, eid)
        os.makedirs(where, exist_ok=True)
        if os.path.lexists(failed):
            os.remove(failed)
    _run([sys.executable, NOP, vm, PASSWORD, EXAMPLES, domain, eid, where],
         dir, _env(**graded), os.path.join(dir, "nop.log"), "the null control's run",
         stagger=True, failed=failed, until=until)
    if not os.path.exists(os.path.join(where, "result.txt")):
        kind, said = _tail(os.path.join(dir, "nop.log"))
        raise kind(f"the null control's run wrote no result.txt: {said}")
    return _judged(_scored(where, "nop.py, DesktopEnv.evaluate"), dir, judge, os.path.join(dir, "nop.log"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reap"]:
        reap(int(sys.argv[2]), sys.argv[3], sys.argv[4:5] == ["vms"], sys.stdin.read().split())
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
