"""OSWorld's driver for the Nullius standard (JSON lines on stdin and stdout).

An agent benchmark whose agent side waits for agent.computer/v1's kit: today
it runs as its setups and its null control. Everything that acts and scores is
OSWorld's own code at the tested commit ($NULLIUS_UPSTREAM), run as published;
this driver names the task, starts the upstream's script and reads what it
wrote. Each unit's script runs in the unit's folder, so its logs, its task
cache and its results stay with the trial.

units    - every task of the dataset (one of the upstream's
           evaluation_examples/test_*.json), as domain__id, in the file's order
setup    - the task's instruction, for the null control
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
           For the null control, nop.py: the upstream's run_single_example
           with an agent that answers DONE at its first step, its result.txt
           read the same way

The VM image is the upstream's own: its docker provider's DockerVMManager
downloads it into the upstream's docker_vm_data once, under a lock, for every
unit and run of this commit. Units on one machine start their VMs one after
another - each waits until the one before has logged its container started -
since the provider fails a start that queues more than 10 s for its port lock;
the VMs then run side by side. Each unit passes the image as a link in its own
folder (vm/Ubuntu.qcow2), so every container it starts - a second VM of an
agent's own included - mounts its image from that folder. A unit stopped by
Nullius (its time ran out) has its script's processes killed and those
containers removed by a reaper of its own session, which outlives this driver.
"""
import fcntl
import glob
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UP = os.environ["NULLIUS_UPSTREAM"]
# driver.py AGENTS_JSON NOP_PY: the definition's command names both, so a host's runner snapshot carries them
_ARGS = sys.argv[1:3] if sys.argv[1:2] != ["--reap"] else []
AGENTS_FILE, NOP = (_ARGS + [os.path.join(HERE, "agents.json"), os.path.join(HERE, "nop.py")][len(_ARGS):])[:2]
with open(AGENTS_FILE, encoding="utf-8") as _f:
    AGENTS = json.load(_f)
VM =os.path.join(UP, "docker_vm_data", "Ubuntu.qcow2")     # where the upstream's DockerVMManager puts it
RESULTS = "results"
# VM starts on one machine, one after another: the upstream's docker provider creates each container under
# /tmp/docker_port_allocation.lck, and a start that waits there more than 10 s fails (LOCK_TIMEOUT)
START_LOCK = "/tmp/nullius-osworld1-vm-start.lck"
STARTED = "Started container with ports"                     # the provider's line once the container runs
START_SECONDS = 120


def _task(unit):
    domain, _, eid = unit.partition("__")
    with open(os.path.join(UP, "evaluation_examples", "examples", domain, eid + ".json"), encoding="utf-8") as f:
        return domain, eid, json.load(f)


def units(dataset=None, setup=None):
    with open(os.environ["NULLIUS_DATASET"], encoding="utf-8") as f:
        meta = json.load(f)
    return {"units": [f"{d}__{e}" for d, ids in meta.items() for e in ids]}


def setup(unit, dir, **_):
    return {"prompt": _task(unit)[2]["instruction"]}


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


def _remove_vms(dir):
    """Remove the VM containers that mount the unit's image link: a stopped unit's, or those a script left
    behind when it ended without closing its environment."""
    mine = os.path.join(dir, "vm") + os.sep
    out = subprocess.run(["docker", "ps", "-a", "-q", "--filter", "ancestor=happysixd/osworld-docker"],
                         capture_output=True, text=True).stdout.split()
    ids = []
    for cid in out:
        sources = subprocess.run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", cid],
                                 capture_output=True, text=True).stdout.split()
        if any(s.startswith(mine) for s in sources):
            ids.append(cid)
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def _workspace(dir):
    """The unit's folder as the upstream's root for the paths its tasks name (evaluation_examples/settings/...),
    with the logs/ folder a script's log handlers open; answers the unit's link to the VM image, which the
    docker provider mounts by the path it is given (the reaper's mark)."""
    link = os.path.join(dir, "evaluation_examples")
    if not os.path.lexists(link):
        os.symlink(os.path.join(UP, "evaluation_examples"), link)
    os.makedirs(os.path.join(dir, "logs"), exist_ok=True)
    vm = os.path.join(dir, "vm", os.path.basename(VM))
    os.makedirs(os.path.dirname(vm), exist_ok=True)
    if not os.path.lexists(vm):
        os.symlink(vm_image(), vm)
    return vm


def _agent():
    name = os.environ.get("NULLIUS_OPTION_AGENT", "m3")
    if name not in AGENTS["agents"]:
        raise RuntimeError(f"no agent {name} in agents.json")
    return name, AGENTS["agents"][name]


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
    if answer:
        raise RuntimeError("OSWorld1 has no agent side yet: agent.computer/v1 has no kit")
    vm = _workspace(dir)                                           # the null control: nothing acts, the upstream evaluates
    where = os.path.join(dir, RESULTS, "nop", domain, eid)
    os.makedirs(where, exist_ok=True)
    _run([sys.executable, NOP, vm,
          os.path.join(UP, "evaluation_examples", "examples", domain, eid + ".json"), where],
         dir, _env(), os.path.join(dir, "nop.log"), "the null control's run", stagger=True)
    if not os.path.exists(os.path.join(where, "result.txt")):
        raise RuntimeError("the null control's run wrote no result.txt: " + _tail(os.path.join(dir, "nop.log")))
    return _scored(where, "nop.py, DesktopEnv.evaluate")


if sys.argv[1:2] == ["--reap"]:                     # _stop's reaper: driver.py --reap PGID LOG
    reap(int(sys.argv[2]), sys.argv[3])
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
