"""OSWorld's driver for the Nullius standard (JSON lines on stdin and stdout).

An agent benchmark: it runs as its setups, its null control and an agent on
agent.computer/v1. Everything that acts and scores is OSWorld's own code at
the tested commit ($NULLIUS_UPSTREAM), run as published; this driver names the
task, starts the upstream's script (or, for an agent, runner.py beside it) and
reads what it wrote. Each unit's script runs in the unit's folder, so its logs, its task
cache and its results stay with the trial.

units    - every task of the dataset (one of the upstream's
           evaluation_examples/test_*.json), as domain__id, in the file's order
setup    - the task's instruction, for the null control. For an agent
           (participant "agent", which the definition's driver.participant
           asks Nullius to send): runner.py started in the trial's
           .work/osworld/, behind the start gate. The runner holds the
           upstream's DesktopEnv and calls its run_single_example, unchanged,
           with the computer port's bridge as the loop's agent. Once the loop
           asks for the agent's first step, setup answers the prompt (the
           instruction, a blank line and the desktop user's password line),
           the MCP server computer (the port's pipe, joined to the runner's
           socket) and the agent's time. The options max_steps and pause are
           the loop's
answer   - a setup's (original, model): the script of the run's agent option
           (agents.json: the upstream's scripts/python/run_*.py) as published,
           on the one task (a test_all_meta file naming it), with the arguments
           of its own documented invocation, one environment (--num_envs 1:
           Nullius runs units side by side, each its own VM) on the docker
           provider, the VM image passed as --path_to_vm, --test_config_base_dir
           the upstream's evaluation_examples, and its model client pointed at
           the unit's relay by the flags or variables it reads. The original
           asks one of the agent's own models; the model setup asks the agent's
           first, and the relay sends the model chosen. M3's M3_THINKING_MODE
           is the run's thinking option. The task's last action is the answer;
           no result.txt is the unit's error, with the upstream's own words
score    - for a setup, the upstream's result.txt: what the agent's runner
           (lib_run_single's, or its script's own) wrote from
           DesktopEnv.evaluate() on the VM's final state.
           For an agent: the turn's end on the runner's lifeline; the runner's
           loop passes its steps left with no action and evaluates; score
           waits for the runner to end and reads result.txt from the runner's
           folder, never the workspace, and only from a runner this driver
           started that ended by itself with status 0, through folders opened
           without following a link. It copies result.txt and the call log to
           the trial's verifier/osworld/.
           For the null control, nop.py: the upstream's run_single_example
           with an agent that answers DONE at its first step, its result.txt
           read the same way
end      - an agent's unit has ended: its runner, if it still has one, goes

The VM image is the upstream's own: its docker provider's DockerVMManager
downloads it into the upstream's docker_vm_data once, under a lock, for every
unit and run of this commit. Units on one machine start their VMs one after
another - each waits until the one before has logged its container started -
since the provider fails a start that queues more than 10 s for its port lock;
the VMs then run side by side. Each unit passes the image as a link in its own
folder (vm/Ubuntu.qcow2), inside the setup's unique socket folder for an agent
through the port, so every container it starts - a second VM of an
agent's own included - mounts its image from that folder. A unit stopped by
Nullius (its time ran out) has its script's processes killed and those
containers removed by a reaper of its own session, which outlives this driver.

An agent's runner holds the read end of a pipe from this driver (its
lifeline): a line turn-end ends the agent's turn, and the pipe's close stops
the runner, which closes its desktop and ends - at its unit's score, at the
unit's end (end, which the definition's driver.unit_end asks Nullius to send),
at the next unit's setup, and when this driver ends, by itself or killed.

An agent's unit has one budget, the unit's own limits as the core keeps them:
setup's (the definition's limits.setup_seconds), the agent's (AGENT_SECONDS,
which setup answers), score's and end's (the core's CORE_DRIVER_SECONDS and
CORE_END_SECONDS). Each bound here follows from it. A runner let go has
REAP_SECONDS to end by itself, more than its own clean-up takes (runner.py's
STOP_SECONDS); then its group is killed and the unit's VMs removed, the docker
commands within REMOVE_SECONDS. Every wait in setup ends at the setup's
deadline, early enough for that reap to end before the core's: the start gate
a unit waits for, its runner's VM start, its task's setup. A unit holds the
start gate until its VM start has ended, with no bound of its own. Score waits
SCORE_SECONDS for the runner, the reap fitting after it within the core's.
When this driver's input ends, each runner left, and one being reaped, is let
go and handed to a reaper in a session of its own, which outlives the driver
and does the same reap.
"""
import fcntl
import glob
import importlib.util
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UP = os.environ["NULLIUS_UPSTREAM"]
# driver.py AGENTS_JSON NOP_PY RUNNER_PY: the definition's command names them, so a host's runner snapshot carries them
_ARGS = sys.argv[1:4] if sys.argv[1:2] not in (["--reap"], ["--reap-runner"]) else []
AGENTS_FILE, NOP, RUNNER_FILE = (_ARGS + [os.path.join(HERE, "agents.json"), os.path.join(HERE, "nop.py"),
                                          os.path.join(HERE, "runner.py")][len(_ARGS):])[:3]
with open(AGENTS_FILE, encoding="utf-8") as _f:
    AGENTS = json.load(_f)
VM =os.path.join(UP, "docker_vm_data", "Ubuntu.qcow2")     # where the upstream's DockerVMManager puts it
RESULTS = "results"
# VM starts on one machine, one after another: the upstream's docker provider creates each container under
# /tmp/docker_port_allocation.lck, and a start that waits there more than 10 s fails (LOCK_TIMEOUT)
START_LOCK = "/tmp/nullius-osworld1-vm-start.lck"
STARTED = "Started container with ports"                     # the provider's line once the container runs
START_SECONDS = 120
# an agent's unit (agent.computer/v1): the desktop user's password line, as OSWorld's own agents are told it; the
# agent's time, the setups'; the unit's budget (the module's text) and the bounds that follow from it
PASSWORD_LINE = "If you need a password for sudo, the password of the computer is '{password}'."
AGENT_SECONDS = 7200
CORE_DRIVER_SECONDS = 600                            # the core's wait on score, and on setup when no limit is given
CORE_END_SECONDS = 120                               # the core's wait on end
ANSWER_SECONDS = 10                                  # this driver's own answer once a bound is reached
REMOVE_SECONDS = 30                                  # the docker commands that remove a unit's VMs, together
FALLBACK_SECONDS = 2                                 # this driver's removal when no reaper could start: inside the core's 3 s
RUNNERS = {}                                         # the runner folder of each unit this driver set up -> its process


def _load(path):
    spec = importlib.util.spec_from_file_location("osworld1_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup_seconds():
    """The definition's limits.setup_seconds (standard.json beside this file), the core's wait on an agent's setup."""
    try:
        with open(os.path.join(HERE, "standard.json"), encoding="utf-8") as f:
            return float(json.load(f)["limits"]["setup_seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return float(CORE_DRIVER_SECONDS)


RUNNER = _load(RUNNER_FILE)
REAP_SECONDS = RUNNER.STOP_SECONDS + 5               # a runner let go ends by itself first, its clean-up done
SETUP_SECONDS = _setup_seconds()
SCORE_SECONDS = CORE_DRIVER_SECONDS - REAP_SECONDS - REMOVE_SECONDS - ANSWER_SECONDS


def _task(unit):
    domain, _, eid = unit.partition("__")
    with open(os.path.join(UP, "evaluation_examples", "examples", domain, eid + ".json"), encoding="utf-8") as f:
        return domain, eid, json.load(f)


def units(dataset=None, setup=None):
    with open(os.environ["NULLIUS_DATASET"], encoding="utf-8") as f:
        meta = json.load(f)
    return {"units": [f"{d}__{e}" for d, ids in meta.items() for e in ids]}


def setup(unit, dir, participant=None, **_):
    until = time.time() + SETUP_SECONDS - REAP_SECONDS - REMOVE_SECONDS - ANSWER_SECONDS   # an agent's setup's own
    instruction = _task(unit)[2]["instruction"]
    if participant != "agent":
        return {"prompt": instruction}
    _reap_all()                                      # a unit before this one that never reached score
    return _start_runner(unit, dir, instruction, until)


# -- the upstream's processes: started apart, stopped by the upstream's own cleanup --------------------
LINES = queue.Queue()
CLOSED = threading.Event()          # Nullius closed our stdin: the unit is over, whatever runs for it stops


def _read():
    for line in sys.stdin:
        LINES.put(line)
    CLOSED.set()
    LINES.put(None)


def _stop(p, log, what):
    """The unit is over before its process: a reaper in a session of its own - Nullius kills this driver's
    group 3 s after closing its stdin - kills the process's whole group and removes the VM containers that
    mount the unit's image link. The upstream's own signal handlers are not relied on: they skip the
    provider's cleanup while a VM boots, and the M3 runner's worker fails in its handler."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--reap", str(p.pid), os.path.dirname(log)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    raise RuntimeError(f"{what} was stopped: the unit's time ran out")


def reap(pgid, dir):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    for wait in (2, 8, 20):                          # a create the daemon was already running lands a few seconds on
        time.sleep(wait)
        _remove_vms(dir)


def _remove_vms(dir, seconds=None):
    """Remove the VM containers that mount the unit's image link: a stopped unit's, or those a script left
    behind when it ended without closing its environment. ``seconds``: the docker commands' bound, together (an
    agent's unit); past it the rest is left."""
    until = None if seconds is None else time.time() + seconds

    def docker(*argv, **kw):
        left = None if until is None else until - time.time()
        if left is not None and left <= 0:
            raise subprocess.TimeoutExpired(argv, 0)
        return subprocess.run(["docker", *argv], timeout=left, check=True, **kw)

    mine = os.path.join(dir, "vm") + os.sep
    try:
        out = docker("ps", "-a", "-q", "--filter", "ancestor=happysixd/osworld-docker",
                     capture_output=True, text=True).stdout.split()
        ids = []
        for cid in out:
            sources = docker("inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", cid,
                             capture_output=True, text=True).stdout.split()
            if any(s.startswith(mine) for s in sources):
                ids.append(cid)
        if ids:
            docker("rm", "-f", *ids, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"driver: the unit's VMs were not all removed within {seconds} s ({dir})\n")
        raise


def _started(p, log, what):
    """Wait until the process's VM container runs (the upstream docker provider's own log line), the
    process ends, or START_SECONDS pass: the next unit's VM start then does not queue behind it."""
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < START_SECONDS:
        if CLOSED.wait(1):
            _stop(p, log, what)
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                if STARTED in f.read():
                    return
        except OSError:
            pass


def _run(argv, cwd, env, log, what, stagger=False):
    """``argv`` in a session of its own, its output in ``log``; when Nullius ends the unit first, the
    process and its VM are reaped (``_stop``), and once it ends, any VM of the unit it left running is
    removed. ``stagger``: the process starts a VM, and starts it after the units before it on this
    machine have started theirs."""
    lock = None
    if stagger:
        lock = open(START_LOCK, "a")
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if CLOSED.wait(1):
                    lock.close()
                    raise RuntimeError(f"{what} was not started: the unit's time ran out")
    try:
        with open(log, "a") as f:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        if lock:
            _started(p, log, what)
    finally:
        if lock:
            lock.close()
    while p.poll() is None:
        if CLOSED.wait(1):
            _stop(p, log, what)
    _remove_vms(cwd)
    if p.returncode != 0:
        raise RuntimeError(f"{what} exited {p.returncode}: " + _tail(log))


def _tail(log, n=3):
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return "no log"
    return " | ".join(lines[-n:])[:600] or "nothing logged"


def _env(**extra):
    return dict(os.environ, PYTHONPATH=UP, PYTHONIOENCODING="utf-8", **extra)


def vm_image():
    """The upstream's Ubuntu image, fetched by its own docker provider when it is not there yet."""
    if os.path.exists(VM):
        return VM
    with open(os.path.join(UP, "docker_vm_data.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.path.exists(VM):
            _run([sys.executable, "-c", "from desktop_env.providers.docker.manager import DockerVMManager\n"
                  "DockerVMManager().get_vm_path(os_type='Ubuntu', region=None)"],
                 UP, _env(), os.path.join(UP, "docker_vm_data.log"), "the upstream's VM image download")
    if not os.path.exists(VM):
        raise RuntimeError("the upstream's docker provider wrote no " + VM)
    return VM


def _workspace(dir, vm_root=None):
    """The unit's folder as the upstream's root for the paths its tasks name (evaluation_examples/settings/...),
    with the logs/ folder a script's log handlers open; answers the unit's link to the VM image, which the
    docker provider mounts by the path it is given (the reaper's mark)."""
    link = os.path.join(dir, "evaluation_examples")
    if not os.path.lexists(link):
        os.symlink(os.path.join(UP, "evaluation_examples"), link)
    os.makedirs(os.path.join(dir, "logs"), exist_ok=True)
    vm = os.path.join(vm_root or dir, "vm", os.path.basename(VM))
    os.makedirs(os.path.dirname(vm), exist_ok=True)
    if not os.path.lexists(vm):
        os.symlink(vm_image(), vm)
    return vm


def _agent():
    name = os.environ.get("NULLIUS_OPTION_AGENT", "m3")
    if name not in AGENTS["agents"]:
        raise RuntimeError(f"no agent {name} in agents.json")
    return name, AGENTS["agents"][name]


def with_steps(argv, steps):
    """The script's argv with the run's ``max_steps`` option as its --max_steps: the agent's own (its
    invocation's, else its script's default) at ``default``."""
    if steps == "default":
        return argv
    if "--max_steps" in argv:
        i = argv.index("--max_steps")
        return argv[:i + 1] + [steps] + argv[i + 2:]
    return argv + ["--max_steps", steps]


def _fill(value, slots):
    for k, v in slots.items():
        value = value.replace("{" + k + "}", v)
    return value


def _result(dir, domain, eid):
    found = glob.glob(os.path.join(dir, RESULTS, "**", domain, eid, "result.txt"), recursive=True)
    return os.path.dirname(found[0]) if found else None


def _traj(where):
    try:
        with open(os.path.join(where, "traj.jsonl"), encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, ValueError):
        return []


def answer(unit, dir, setup, model, base_url, key, **_):
    domain, eid, _example = _task(unit)
    name, spec = _agent()
    if setup == "model":
        model = spec["models"][0]                    # the agent's own: the relay sends the model chosen
    elif model not in spec["models"]:
        raise RuntimeError(f"{model} is not one of the {name} agent's models ({', '.join(spec['models'])}): "
                           "choose one of them, or the model setup")
    vm = _workspace(dir)
    meta = os.path.join(dir, "test_unit.json")
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({domain: [eid]}, f)
    slots = {"model": model, "base_url": base_url, "key": key, "vm": vm, "up": UP, "meta": meta,
             "results": os.path.join(dir, RESULTS), "examples": os.path.join(UP, "evaluation_examples"),
             "base": base_url[:-3] if base_url.endswith("/v1") else base_url}     # an Anthropic client adds /v1/messages
    common, drop = list(AGENTS["common"]), spec.get("drop", [])
    for flag in drop:
        i = common.index(flag)
        del common[i:i + (1 if flag == "--headless" else 2)]
    argv = [sys.executable, os.path.join(UP, spec["script"])] + [_fill(a, slots) for a in
                                                                 common + spec["argv"] + spec.get("model_argv", {}).get(model, [])]
    argv = with_steps(argv, os.environ.get("NULLIUS_OPTION_MAX_STEPS", "default"))
    extra = {k: _fill(v, slots) for k, v in spec["env"].items()}
    if spec.get("aws"):
        extra.update(AGENTS["aws"])
    if name == "m3" and os.environ.get("NULLIUS_OPTION_THINKING", "adaptive") == "adaptive":
        extra["M3_THINKING_MODE"] = "adaptive"
    script = os.path.basename(spec["script"])
    log = os.path.join(dir, script.replace(".py", ".log"))
    _run(argv, dir, _env(**extra), log, script, stagger=True)
    where = _result(dir, domain, eid)
    if where is None:
        errors = [s["Error"] for d in glob.glob(os.path.join(dir, RESULTS, "**", domain, eid), recursive=True)
                  for s in _traj(d) if "Error" in s]
        raise RuntimeError(f"{script} wrote no result.txt: " + (errors[-1] if errors else _tail(log)))
    steps = _traj(where)
    last = steps[-1].get("action") if steps else ""
    return {"answer": last if isinstance(last, str) else json.dumps(last), "model": model}


# -- an agent's unit: the runner, its readiness, its end ----------------------------------------------------------
# A runner's life is tied to this driver's: it holds the read end of a pipe whose write end this driver keeps (its
# lifeline), and closes its desktop and ends once that pipe closes - this driver ended, was killed, or let the unit go.
# Each unit's runner stays in RUNNERS until it is gone. Its exit is seen without reaping it (_exited), so that its
# process group is signalled only while its leader still holds the group's number.


def _port():
    """The computer port's shared code, from the nullius package on this driver's import path."""
    from nullius.standard import computer_port
    return computer_port


def _runner_dir(dir):
    """The runner's folder: the trial's .work/osworld/, beside the agent's workspace and outside it."""
    return os.path.join(os.path.dirname(os.path.abspath(dir)), ".work", "osworld")


def _held_dir(dir):
    """The run's folder of files sent once: the job's .work/osworld-held/, shared by the run's units; it goes with
    the job."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(dir))), ".work", "osworld-held")


def _option(name, default, kind):
    return kind(os.environ.get("NULLIUS_OPTION_" + name.upper(), default))


def _runner_env():
    """The runner's environment: this driver's, its import path the upstream's folder and the nullius package's."""
    package = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_port().__file__))))
    return dict(os.environ, PYTHONPATH=os.pathsep.join([UP, package]), PYTHONIOENCODING="utf-8")


LOST = "lost"                                        # _exited: the runner was reaped elsewhere; its number is not ours


def _exited(p):
    """None while the runner runs; once it has ended, (whether it exited by itself, its status), without reaping it;
    LOST once it was reaped elsewhere (waitid finds no such child): its number may be another process's now."""
    if p.returncode is not None:
        return (p.returncode >= 0, p.returncode)
    try:
        info = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return LOST
    if info is None:
        return None
    return (info.si_code == os.CLD_EXITED, info.si_status)


def _start_time(pid):
    """The process's start time (field 22 of /proc/PID/stat), which tells it from a later process with its number."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _start_runner(unit, dir, instruction, until):
    domain, eid, example = _task(unit)
    where = _runner_dir(dir)
    os.makedirs(where, exist_ok=True)
    task = os.path.join(where, f"{domain}__{eid}.json")
    with open(task, "w", encoding="utf-8") as f:
        json.dump(example, f)
    held = _held_dir(dir)
    os.makedirs(held, mode=0o700, exist_ok=True)
    log = os.path.join(where, "runner.log")
    sock_dir = life_r = life_w = lock = p = None
    try:                                             # until RUNNERS holds it, what was made here is undone here
        sock_dir = tempfile.mkdtemp(prefix="osworld1-")  # 0700, unique to this setup: the socket, ready.json and VM link
        vm = _workspace(where, sock_dir)
        ready = os.path.join(sock_dir, "ready.json")
        life_r, life_w = os.pipe()
        steps = _option("max_steps", "default", str)
        argv = [sys.executable, RUNNER_FILE, "--example", task, "--result-dir", os.path.join(where, RESULTS),
                "--socket", os.path.join(sock_dir, "s"), "--ready", ready, "--lifeline", str(life_r),
                "--steps", "100" if steps == "default" else steps, "--pause", str(_option("pause", "3.0", float)),
                "--vm", vm, "--calls", os.path.join(where, "computer-calls.jsonl"), "--held", held]
        lock = open(START_LOCK, "a")
        while True:                                  # VM starts one after another, as the setups' do
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = until - time.time()
                if left <= 0:
                    raise RuntimeError("the runner was not started: the start gate, held by another unit's VM start, "
                                       "was not free before the end of this unit's setup")
                if CLOSED.wait(min(1.0, left)):
                    raise RuntimeError("the runner was not started: the unit's time ran out")
        with open(log, "a") as f:
            launched = f.tell()                      # the start line of this launch is read from here on
            p = subprocess.Popen(argv, cwd=where, env=_runner_env(), stdout=f, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(life_r,))
        RUNNERS[where] = {"p": p, "sock_dir": sock_dir, "life": life_w, "start": _start_time(p.pid)}
    except BaseException:
        for fd in (life_r, life_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if lock is not None:
            lock.close()
        if sock_dir is not None:
            shutil.rmtree(sock_dir, ignore_errors=True)
        raise
    os.close(life_r)
    try:
        with open(os.path.join(where, "runner.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": p.pid}, f)
        # the gate is held until the VM's start has ended, the provider's port lock with it: no bound of its own
        _wait(lambda: os.path.exists(ready) or _has(log, STARTED, launched), p, where, until, "its VM's start")
    finally:
        lock.close()
    _wait(lambda: os.path.exists(ready), p, where, until, "its task's setup")
    try:
        with open(ready, encoding="utf-8") as f:
            said = json.load(f)
    finally:
        try:
            os.unlink(ready)                         # it holds the desktop user's password: read once, then gone
        except OSError:
            pass
    if not said.get("ok"):
        _reap_runner(where)
        raise RuntimeError("the runner's setup failed: " + str(said.get("error")))
    pipe = os.path.join(os.path.dirname(os.path.abspath(_port().__file__)), "computer_pipe.py")
    return {"prompt": instruction + "\n\n" + PASSWORD_LINE.format(password=said.get("password") or ""),
            "mcp": [{"name": "computer", "server": "computer", "args": [pipe, os.path.join(sock_dir, "s")]}],
            "agent_seconds": AGENT_SECONDS}


def _has(log, text, start=0):
    """Whether ``text`` is in ``log`` from its byte ``start`` on."""
    try:
        with open(log, "rb") as f:
            f.seek(start)
            return text.encode() in f.read()
    except OSError:
        return False


def _wait(done, p, where, until, what):
    """Until ``done()``; raises when the runner ends first, Nullius ends the unit (the runner handed to a reaper), or
    the setup's deadline ``until`` comes (the runner reaped)."""
    while not done():
        ended = _exited(p)
        if ended is not None:
            _reap_runner(where)
            raise RuntimeError(f"the runner ended (exit {'unknown' if ended == LOST else ended[1]}) before {what}: "
                               + _tail(os.path.join(where, "runner.log")))
        if CLOSED.wait(0.2):
            _hand_over(where)
            raise RuntimeError("the runner was stopped: the unit's time ran out")
        if time.time() > until:
            _reap_runner(where)
            raise RuntimeError(f"the runner was not ready by the end of the unit's setup, {SETUP_SECONDS:g} s ({what})")


def _let_go(entry):
    """Close the runner's lifeline: it closes its desktop and ends by itself."""
    if entry.get("life") is not None:
        try:
            os.close(entry["life"])
        except OSError:
            pass
        entry["life"] = None


def _turn_end(entry):
    """The agent's turn has ended: one line on the runner's lifeline (the port's turn-end)."""
    if entry.get("life") is not None:
        try:
            os.write(entry["life"], _port().TURN_END + b"\n")
        except OSError:
            pass


def _reap_runner(where, seconds=None):
    """The unit's runner let go, given ``seconds`` (REAP_SECONDS) to end by itself, its whole group killed while its
    leader is still unreaped, then reaped; the VM containers of the unit's image link removed within REMOVE_SECONDS,
    the socket's folder gone. It leaves RUNNERS only then. When this driver's input ends meanwhile, the rest goes to
    a reaper of its own (_hand_over). Returns how it ended before any kill (``_exited``): None when it was still
    running."""
    entry = RUNNERS.get(where)
    if entry is None:
        return None
    seconds = REAP_SECONDS if seconds is None else seconds
    p = entry["p"]
    _let_go(entry)
    t0 = time.time()
    while p.returncode is None and _exited(p) is None and time.time() - t0 < seconds:
        if CLOSED.wait(0.1):
            _hand_over(where)
            return None
    ended = _exited(p)
    killed = p.returncode is None and ended != LOST
    if killed:                                       # running or a zombie: the group's number is still its own
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
    if shutil.which("docker") is not None and not _removed(where, entry["sock_dir"], again=ended is None):
        RUNNERS.pop(where, None)                     # a reaper of its own has the removal and the socket's folder
        return ended
    shutil.rmtree(entry["sock_dir"], ignore_errors=True)
    RUNNERS.pop(where, None)
    return ended


def _remove_passes(where, seconds, again=True):
    """The unit's VMs removed within ``seconds``: once, then (``again``: the runner was killed, a create the engine had
    accepted may still land) once more after each of 2, 8 and 20 s while time is left, as reap does."""
    until = time.time() + seconds
    _remove_vms(where, seconds)
    for wait in (2, 8, 20) if again else ():
        if until - time.time() <= wait:
            return
        time.sleep(wait)
        _remove_vms(where, until - time.time())


def _removed(where, sock_dir, again):
    """The unit's VMs removed (_remove_passes) in a thread this driver watches: True once done here; False when this
    driver's input ended meanwhile and the removal and the socket's folder went to a reaper of its own."""
    failed = []

    def remove():
        try:
            _remove_passes(sock_dir, REMOVE_SECONDS, again)
        except Exception as e:
            failed.append(e)

    worker = threading.Thread(target=remove, name="vm-removal", daemon=True)
    worker.start()
    while worker.is_alive():
        if CLOSED.wait(0.1):
            try:
                _spawn_reaper(0, None, where, sock_dir, 0)
                return False
            except OSError:                          # no reaper: this driver's removal goes on while it lives
                worker.join(REMOVE_SECONDS)
                if failed:
                    raise failed[0]
                return True
    if failed:
        raise failed[0]
    return True


def _reap_all():
    for where in list(RUNNERS):
        _reap_runner(where)


def _hand_over(where):
    """The runner let go and handed to a reaper in a session of its own (driver.py --reap-runner), which outlives this
    driver: Nullius kills this driver's group 3 s after closing its stdin. The runner stays in RUNNERS until its reaper
    has started; when none can start, it goes here at once (_reap_now)."""
    entry = RUNNERS.get(where)
    if entry is None:
        return
    _let_go(entry)
    try:
        _spawn_reaper(entry["p"].pid, entry.get("start"), where, entry["sock_dir"], REAP_SECONDS)
    except OSError:
        _reap_now(where)
        return
    RUNNERS.pop(where, None)


def _spawn_reaper(pid, start, where, sock_dir, seconds):
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--reap-runner", str(pid), str(start), where,
                      sock_dir, str(seconds)],
                     env=dict(os.environ, NULLIUS_UPSTREAM=UP), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _reap_now(where):
    """The runner reaped here at once, when no reaper could start: its group killed while its leader is unreaped, the
    unit's VMs removed within FALLBACK_SECONDS, the socket's folder gone."""
    entry = RUNNERS.pop(where, None)
    if entry is None:
        return
    p = entry["p"]
    if p.returncode is None and _exited(p) != LOST:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
    if shutil.which("docker") is not None:
        _remove_vms(entry["sock_dir"], FALLBACK_SECONDS)
    shutil.rmtree(entry["sock_dir"], ignore_errors=True)


def reap_runner(pid, start, where, sock_dir, seconds):
    """The reaper of a runner handed over (_hand_over), or of a removal this driver's end cut (_removed, pid 0): the
    runner has ``seconds`` to end by itself; then, when it is still the process started (same number, same start time),
    its group is killed; the unit's VMs removed within REMOVE_SECONDS, looked for again as reap does; the socket's
    folder gone."""
    def same():
        return start is not None and _start_time(pid) == start

    t0 = time.time()
    while same() and time.time() - t0 < seconds:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break                            # ended, not yet reaped by its new parent: its number is still its own
        except (OSError, IndexError):
            break
        time.sleep(0.2)
    if same():
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    if shutil.which("docker") is not None:
        _remove_passes(sock_dir, REMOVE_SECONDS)    # again, for a create the engine had accepted before the kill
    shutil.rmtree(sock_dir, ignore_errors=True)


def _reap_at_exit():
    """Nullius closed this driver's stdin and kills its group 3 s later: each runner is let go and handed to a reaper
    of its own, which outlives this driver."""
    for where in list(RUNNERS):
        _hand_over(where)


def end(unit, dir, **_):
    """The unit has ended, scored or not: its runner, when it still has one, goes now."""
    _reap_runner(_runner_dir(dir))
    return {}


def _plain(base, *parts):
    """The bytes of the file ``base/parts...``: each folder from ``base`` on opened without following a link, the
    file opened relative to the last one without following a link or waiting on a pipe, and read only when it is a
    plain file; raises otherwise."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(base, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        ffd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(ffd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise RuntimeError(f"{parts[-1]} is not a plain file")
        return f.read()


def _agent_score(where, dir, domain, eid):
    """The agent's turn ended: the runner this driver started for the unit is told so, ends by itself with status 0,
    and the result.txt it wrote is read through folders opened without following a link."""
    entry = RUNNERS.get(where)
    if entry is None:
        raise RuntimeError("no runner of this driver's for this unit: its result is not read")
    _turn_end(entry)
    p, t0 = entry["p"], time.time()
    while _exited(p) is None and time.time() - t0 < SCORE_SECONDS and not CLOSED.wait(0.5):
        pass
    if CLOSED.is_set() and _exited(p) is None:       # Nullius ended the unit: the reaper ends what is left
        _hand_over(where)
        raise RuntimeError("the runner was stopped: the unit's time ran out")
    ended = _exited(p)
    if ended is None:                                # one still running is let go; an end by itself meanwhile counts
        ended = _reap_runner(where)
    else:
        _reap_runner(where, seconds=0)
    if ended is None:
        raise RuntimeError(f"the runner did not end within {SCORE_SECONDS} s of the agent's end")
    if ended == LOST or not (ended[0] and ended[1] == 0):
        how = "was reaped elsewhere" if ended == LOST else ("exited " + str(ended[1]) if ended[0] else "was killed")
        raise RuntimeError(f"the runner {how}: its result is not read: " + _tail(os.path.join(where, "runner.log")))
    trial = os.path.dirname(os.path.abspath(dir))
    try:
        result = _plain(trial, ".work", "osworld", RESULTS, domain, eid, "result.txt")
        value = float(result.decode().strip())
    except (OSError, ValueError, RuntimeError) as e:
        raise RuntimeError(f"the runner's result could not be read ({e}): " + _tail(os.path.join(where, "runner.log")))
    try:
        calls = _plain(trial, ".work", "osworld", "computer-calls.jsonl")
    except (OSError, RuntimeError):
        calls = b""
    out = os.path.join(trial, "verifier", "osworld")
    os.makedirs(out, exist_ok=True)
    for name, data in (("result.txt", result), ("computer-calls.jsonl", calls)):
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
    return {"reward": value, "detail": {"result": value, "by": "runner.py, run_single_example, DesktopEnv.evaluate",
                                        "tool_calls": len(calls.splitlines())}}


def _scored(where, how):
    with open(os.path.join(where, "result.txt"), encoding="utf-8") as f:
        value = float(f.read().strip())
    steps = _traj(where)
    return {"reward": value, "detail": {"result": value, "by": how, "steps": len(steps),
                                        "last_action": steps[-1].get("action") if steps else None}}


def score(unit, dir, transcript=None, answer=None, setup=None, **_):
    domain, eid, _example = _task(unit)
    if setup in ("original", "model"):
        where = _result(dir, domain, eid)
        if where is None:
            raise RuntimeError("no result.txt: the upstream's runner did not finish the task")
        return _scored(where, os.path.basename(_agent()[1]["script"]) + ", DesktopEnv.evaluate")
    runner = _runner_dir(dir)
    if runner in RUNNERS or os.path.lexists(os.path.join(runner, "runner.json")):
        return _agent_score(runner, dir, domain, eid)
    if answer:
        raise RuntimeError("no runner was set up for this unit: an agent's setup starts one")
    vm = _workspace(dir)                                           # the null control: nothing acts, the upstream evaluates
    where = os.path.join(dir, RESULTS, "nop", domain, eid)
    os.makedirs(where, exist_ok=True)
    _run([sys.executable, NOP, vm,
          os.path.join(UP, "evaluation_examples", "examples", domain, eid + ".json"), where],
         dir, _env(), os.path.join(dir, "nop.log"), "the null control's run", stagger=True)
    if not os.path.exists(os.path.join(where, "result.txt")):
        raise RuntimeError("the null control's run wrote no result.txt: " + _tail(os.path.join(dir, "nop.log")))
    return _scored(where, "nop.py, DesktopEnv.evaluate")


HANDLERS = {"units": units, "setup": setup, "answer": answer, "score": score, "end": end}


def main():
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
    _reap_at_exit()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reap"]:                 # _stop's reaper: driver.py --reap PGID LOG
        reap(int(sys.argv[2]), sys.argv[3])
        sys.exit(0)
    if sys.argv[1:2] == ["--reap-runner"]:          # _hand_over's: driver.py --reap-runner PID START WHERE SOCKETS SECONDS
        reap_runner(int(sys.argv[2]), None if sys.argv[3] == "None" else int(sys.argv[3]), sys.argv[4], sys.argv[5],
                    float(sys.argv[6]))
        sys.exit(0)
    main()
