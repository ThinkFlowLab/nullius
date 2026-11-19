"""A benchmark whose units boot a VM of their own (``definitions.boots_vms``: OSWorld) on Boat: each
unit whole in a Boat VM of its own.

The ``boat`` backend moves only an agent's process into a VM; such a unit's
weight is elsewhere - its driver, the upstream's script and the VM they boot
on the driver's machine. So here the Boat VM is that machine, for one unit:
this runner's own snapshot (``payload``) is sent to it and run there as a
shard of one unit on local processes (``shard``, by ``position``: shard I of
N, N the dataset's units, is the unit at place I-1), which installs what it
needs itself - the upstream, the driver's environment, the VM image - as on
any machine. The Boat image has the rest (Docker for its user, KVM, git,
uv); what the benchmark's chosen options still need of a machine is
installed with the VM's sudo. Each shard's job comes back to this machine as
its unit ends, the VM is deleted, and the shards are merged here into the
one job the run is named for (``merge``), recorded as a Boat run.

As many VMs at once as the account runs (``boat.slots``), never more than
the run's own concurrency; a VM the account cannot start yet is waited for.
Every VM carries the run's label, so a Stop or a crash leaves none
(``boat.sweep``). The VMs are the run's own code: no locked agent's user
(``boat.VM``, ``lock``). Each model-calling unit uses a separate relay container in its Boat VM.
"""
from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import threading
import uuid
from typing import Callable, Dict, List, Optional

from nullius import paths, secrets
from nullius.standard import boat, definitions, hostreq, merge, payload, shard

RUNPY = "import json, os, runpy, sys; os.environ.update(json.loads(sys.stdin.readline())); sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('nullius.standard.cli', run_name='__main__', alter_sys=True)"
SHARDS = ".boat"                 # the shards' jobs, under the jobs root, until they are merged


class Refused(Exception):
    """The run cannot go this way; the message says why."""


def plan(bench: dict, dataset: str, limit: Optional[int]) -> tuple:
    """(N, the shards to run): N the dataset's units as its definition declares them, so each shard
    is one unit; ``limit`` runs the first ones."""
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise Refused("--limit must be a positive whole number")
    ds = next((d for d in bench["datasets"] if d["id"] == dataset), None)
    if ds is None:
        raise Refused(f"{bench['label']} has no dataset {dataset!r}")
    n = ds.get("units")
    if not isinstance(n, int) or n < 1:
        raise Refused(f"{bench['label']}'s dataset {dataset} declares no number of units, which a Boat run "
                      "of one VM per unit needs")
    return n, list(range(1, min(limit or n, n) + 1))


def child_argv(words: List[str], *, app: str, index: int, n: int, launch: str, name: str, jobs: str) -> str:
    """The shell line a VM runs: this runner's snapshot, the unit's shard on local processes."""
    argv = ["python3", "-I", "-S", "-B", "-c", RUNPY, app + "/app", "run", *words,
            "--shard", f"{index}/{n}", "--shard-by", "position", "--launch", launch,
            "--name", name, "--jobs", jobs, "-j", "1", "--from-run", "--unsandboxed", "--relay-container"]
    env = {"NULLIUS_PROGRESS": "1", "PYTHONDONTWRITEBYTECODE": "1", "NULLIUS_CATALOGUE": app + "/catalogue"}
    return ("export PATH=\"$HOME/.local/bin:$PATH\" " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            + " && " + " ".join(shlex.quote(w) for w in argv))


def prepare(vm: "boat.VM", bench: dict, needs: List[str]) -> None:
    """What the chosen options need of the machine that Boat's image lacks, installed with its sudo."""
    lines = hostreq.install_lines(bench, needs, sudo="sudo -n ")
    if lines:
        vm.sh("export DEBIAN_FRONTEND=noninteractive && sudo -n apt-get update -q >/dev/null && "
              + " && ".join(c.strip() for c in lines))


def run(bench: dict, words: List[str], *, dataset: str, limit: Optional[int], concurrency: int, jobs_root: str,
        name: str, launch: str, size: str, needs: List[str], say: Callable[[str], None],
        credentials: Optional[dict] = None, env=os.environ, client: Optional["boat.Client"] = None, stopped: Optional[threading.Event] = None) -> tuple:
    """(the merged job's folder, whether it failed): every unit run whole in a Boat VM of its own."""
    why = boat.unusable(env=env) or boat.size_problem(client or boat.Client(boat.key(env) or ""), size)
    if why:
        raise Refused(f"Boat cannot run it: {why}")
    client = client or boat.Client(boat.key(env) or "")
    n, wanted = plan(bench, dataset, limit)
    jobs_root = os.path.abspath(jobs_root)
    out = os.path.join(jobs_root, name)
    if os.path.exists(out):
        raise Refused(f"{out} exists: a run's name is used once")
    here = os.path.join(jobs_root, SHARDS, name)
    most = boat.slots(client) or 1
    at_once = max(1, min(concurrency or 1, most, len(wanted)))
    say(f"Boat: {len(wanted)} {bench['units']['nouns'] if len(wanted) > 1 else bench['units']['noun']}, "
        f"each in a {boat.SIZES[size]['label']} VM of its own, {at_once} at a time")
    starts = boat.starts_problem(client, len(wanted))
    if starts:
        say(starts)
    _, app_here = payload.build(os.path.join(paths.cache_dir(), "standard-app"))
    app = "/home/user/.nullius/app/" + os.path.basename(app_here)     # where the VM keeps it, apart from this machine's paths
    label = f"{name}.{uuid.uuid4().hex[:8]}"
    stopped = stopped or threading.Event()
    redactions = list((credentials or {}).values())
    redactions += [v.decode(errors="replace") for v in secrets.unpack((credentials or {}).get(secrets.HANDED, "")).values()]
    keys = boat.Keys()
    queue = list(wanted)
    lock = threading.Lock()
    procs: Dict[int, subprocess.Popen] = {}
    done: Dict[int, int] = {}

    def one(i: int) -> None:
        sname = f"{name}__shard-{i}of{n}"
        vm = None
        try:
            vm = boat.VM(client, keys, label, size=size, stopped=stopped, lock=False)
            say(f"[{i}/{n}] VM {vm.id} ready in {vm.seconds} s")
            # the shard's job at the path it has here, so it comes back by the same path (VM.fetch)
            vm.sh(f"mkdir -p {shlex.quote(os.path.dirname(app))} && sudo -n mkdir -p {shlex.quote(here)} && "
                  f"sudo -n chown \"$(id -u):$(id -g)\" {shlex.quote(here)}")
            # the snapshot, under the VM user's home: tar from here, untarred there
            tar = subprocess.Popen(["tar", "-C", os.path.dirname(app_here), "-czf", "-", os.path.basename(app_here)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                vm.sh(f"tar -C {shlex.quote(os.path.dirname(app))} -xzf - && rm -rf {shlex.quote(app + '/catalogue')} && "
                      f"cp -R {shlex.quote(app + '/app/nullius/catalogue')} {shlex.quote(app + '/catalogue')}", stdin=tar.stdout)
            finally:
                tar.stdout.close()
                tar.wait()
            prepare(vm, bench, needs)
            if stopped.is_set():
                return
            line = child_argv(words, app=app, index=i, n=n, launch=launch, name=sname, jobs=here)
            p = subprocess.Popen(vm.ssh + ["-T", vm.target, line], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace")
            with lock:
                procs[i] = p
            try:
                p.stdin.write(json.dumps(credentials or {}) + "\n")
                p.stdin.close()
            except BrokenPipeError:
                pass
            for ln in p.stdout:
                ln = ln.rstrip()
                if ln:
                    for secret in redactions:
                        if secret:
                            ln = ln.replace(secret, "[redacted]")
                    say(f"[{i}/{n}] {ln}")
            rc = p.wait()
            os.makedirs(os.path.join(here, sname), exist_ok=True)
            vm.fetch(os.path.join(here, sname))
            with lock:
                done[i] = rc
        except boat.Unavailable as e:
            say(f"[{i}/{n}] not run: {e}")
        finally:
            with lock:
                procs.pop(i, None)
            if vm is not None:
                left = vm.delete()
                if left:
                    say(f"[{i}/{n}] Boat would not delete VM {vm.id}: {left}")

    def worker() -> None:
        while not stopped.is_set():
            with lock:
                if not queue:
                    return
                i = queue.pop(0)
            one(i)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(at_once)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(1)
    except KeyboardInterrupt:
        stopped.set()
        with lock:
            for p in procs.values():
                p.terminate()
        for t in threads:
            t.join(60)
        raise
    finally:
        boat.sweep(label, client, say)
        keys.close()
    shards = sorted(os.path.join(here, d) for d in os.listdir(here)) if os.path.isdir(here) else []
    shards = [d for d in shards if os.path.isfile(os.path.join(d, "config.json"))]
    if not shards:
        raise Refused("no unit came back from its Boat VM")
    got = merge.merge(shards, out, force=False)
    for note in got.get("notes") or []:
        if not note.startswith("shard"):        # a shard is a unit here: the units not run are said once, below
            say(note)
    _record(out, size)
    shutil.rmtree(here, ignore_errors=True)
    failed = not done or all(rc not in (0,) for rc in done.values())
    return out, failed


def _record(job_dir: str, size: str) -> None:
    """The merged job says where its units ran: Boat, the VMs' size; its shards ran on local processes there."""
    path = os.path.join(job_dir, "config.json")
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("standard", {}).update(backend="boat", boat_size=size, boat_units="each unit whole in a Boat VM of its own")
    with open(path + ".tmp", "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(path + ".tmp", path)
