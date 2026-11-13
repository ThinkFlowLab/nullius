"""Constraints: what Nullius may use on a machine, one table.

Each entry of ``TABLE`` is one constraint: its key, its label, its kind
(``cap``: the lowest value wins; ``pin``: which devices Nullius may use),
its type, its minimum, how each backend applies a value (``apply``:
``docker``, its flags on a container; ``systemd``, its properties on a scope), the cgroup v2 files a container or a scope reads
back to show the value holds (``files``, ``enforced``), the capacity it is checked
against (``capacity``, ``fits``) and what it gives the processes Nullius
starts on the machine (``environment``). A new constraint is one entry.

Constraints are set per machine in Settings, ``HOST_CONSTRAINTS``: a JSON
object from a machine (``local`` for this machine, else a host's entry) to
``{key: value}``. A key not set is no constraint; a setting that is not valid
refuses every run (``of``). Each container constraint is a ceiling per
container, on every container a run starts on the machine, whatever the
backend (``container.cap``): a unit's agent, its own image, its relay,
doors, forwarders and proxy, the installs, the checks and a setup's driver.
The connection test's probe containers are not capped. A task's own ``cpus``
or ``memory_mb`` stays when it is lower (``argv``). An image build or pull
takes none: the engine applies no limit there.

Three constraints act beyond containers. ``concurrency`` caps the units a run
starts at a time on the machine; the recipe's own ``concurrency`` is a cap
too, and the lower wins. ``gpu_ids`` gives the benchmark's driver
``CUDA_VISIBLE_DEVICES``. It says only which GPUs Nullius may use: several
processes or units can share one GPU. ``cpu_set`` is ``--cpuset-cpus`` on the
containers and the CPU affinity of the run's own process, which its drivers
inherit. What a process on the machine gets is applied, not enforced: it can
see other GPUs or widen its own affinity.

The processes a run starts on the machine (its drivers, and its agents on the
local backend) each get a scope of the user's systemd manager (``wrap``):
``systemd-run --user --scope`` with the ``systemd`` properties of the values
set, so the kernel holds them as it does a container's. systemd drops a
property whose controller is not delegated without an error, so
``scope_probe`` reads each one back from a scope's cgroup files. Without a
user systemd manager a local-backend run is refused its caps; a
container-backend run keeps them on its containers, and its drivers run
unlimited. ``cpu_set`` falls back to the run's affinity.

``probe`` is the shell script the connection test runs on a machine: its
capacity from ``nproc``, ``MemTotal`` and the GPUs ``nvidia-smi`` lists, then
one throwaway container per container constraint, each started with that
constraint alone, which prints its cgroup files back. The capacity is a
ceiling: the lowest of ``nproc``, ``MemTotal`` and every cgroup v2 limit on
the path from where the work lands up to the root, and the CPUs a process
there may run on (``WALK``). Local processes land in the user's cgroup (a scope's, else
the probe's own); containers land where the engine puts them, which a probe
container in the host's cgroup namespace reads. ``read`` turns what it printed into ``{capacity, enforced,
refused, applied, systemd}``; ``save`` keeps that per machine, and ``warnings`` is
what Settings says of a value against it. A run checks again on its own
engine and its own systemd before its first unit (``standard/run.py``).
"""
from __future__ import annotations

import functools
import json
import math
import os
import re
import shlex
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from nullius import paths

SETTING = "HOST_CONSTRAINTS"
LOCAL = "local"                       # this machine, as a recipe's Where names it
PROBE_IMAGE = "docker.io/library/ubuntu:24.04"   # the base image's own FROM, in full as podman needs it: a later base build reuses it
MIB = 1024 * 1024
MAX_CPUS = 8192                       # the kernel's largest NR_CPUS
SYSTEMD_RUN = ("systemd-run", "--user", "--scope", "-q")
LITERAL = "--expand-environment=no"   # systemd 254 and later expand $ in a scope's command unless told not to
CGROUP = "/sys/fs/cgroup"             # cgroup v2's mount
BOOTED = "/run/systemd/system"        # there when systemd runs the machine (sd_booted)
# its own cgroup v2 path, in $d
_OWN = "d=; while IFS= read -r l; do case $l in 0::*) d=${l#0::};; esac; done </proc/self/cgroup; "
# shortcut: cgroup v2 only; under cgroup v1 the walk reads no limit and the capacity is the whole machine's, as on macOS
# nproc, the CPUs it may run on (its cpuset and affinity), then memory.max and cpu.max of each cgroup from its own
# up to the root: n:4 a:6-9 m:max c:max/100000 m:4294967296 ...
WALK = (_OWN + 'd=${d%/}; printf "n:%s " "$(nproc)"; '
        'while IFS= read -r l; do case $l in Cpus_allowed_list:*) printf "a:%s " ${l#*:};; esac; done </proc/self/status; '
        'while :; do p=' + CGROUP + '$d; '
        'printf "m:%s c:%s " "$(cat $p/memory.max 2>/dev/null)" "$(tr " " / 2>/dev/null <$p/cpu.max)"; '
        '[ -n "$d" ] || break; d=${d%/*}; done')
_CPUSET = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")    # fullmatch: $ allows a final newline
# cpu_set's sample: the first CPU a placement allows, which only the machine knows; the probe sets it from $w, a WALK's output
FIRST_CPU = "$first_cpu"
_FIRST = 'first_cpu=0; case $w in *a:[0-9]*) first_cpu=${w#*a:}; first_cpu=${first_cpu%%[!0-9]*};; esac'


def _quote(word: str) -> str:
    """``word`` for the shell, :data:`FIRST_CPU` left to expand."""
    return shlex.quote(word).replace(FIRST_CPU, f"'\"{FIRST_CPU}\"'")


def cpu_list(text: str) -> List[int]:
    """A cpuset (``0-3,6``) as its CPUs; ValueError when it is not one."""
    text = str(text)
    if not _CPUSET.fullmatch(text):
        raise ValueError(f"{text!r} is not a list of CPUs such as 0-3,6")
    out = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        if b and int(b) < int(a):
            raise ValueError(f"{part}: a range runs upwards")
        if int(b or a) >= MAX_CPUS:          # else a typing error such as 0-3000000000 fills the memory
            raise ValueError(f"{part}: Linux numbers its CPUs below {MAX_CPUS}")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


def _cpus(out: List[str], v: float) -> bool:
    try:
        return abs(int(out[0]) / int(out[1]) - v) < 0.01
    except (ValueError, IndexError, ZeroDivisionError):
        return False


def _same_cpus(out: List[str], v: str) -> bool:
    try:
        return len(out) == 1 and cpu_list(out[0]) == cpu_list(v)
    except ValueError:
        return False


def _cpus_within(v: str, online: str) -> bool:
    return set(cpu_list(v)) <= set(cpu_list(online))


@dataclass(frozen=True)
class Constraint:
    key: str
    label: str
    kind: str                                        # "cap": the lowest value wins; "pin": which devices
    type: type                                       # int, float, str (a cpuset) or list (GPU indices)
    minimum: float
    apply: Dict[str, Callable] = field(default_factory=dict)   # backend -> the value as that backend says it
    files: Tuple[str, ...] = ()                      # the cgroup v2 files a container or a scope reads back
    enforced: Optional[Callable] = None              # (the files' contents, the value) -> whether they say it
    sample: object = None                            # what the connection test probes with (default: minimum)
    unit: str = ""                                   # what the value counts, when the label does not say
    step: float = 0                                  # the Settings slider's resolution, up to the capacity; 0: no slider
    capacity: str = ""                               # the capacity key it is checked against
    fits: Callable = lambda v, c: v <= c             # (the value, the capacity) -> whether it fits
    environment: Optional[Callable] = None           # the variables it gives the processes on the machine
    machine: str = ""                                # beyond containers: "nullius" (the run keeps it), "applied" (without a container or a scope: not enforced)
    tip: str = ""                                    # what the Settings field says on hover


TABLE: List[Constraint] = [
    # the time of N logical CPUs, on any allowed core
    # 0.01: the kernel's smallest quota, 1 ms of each 100 ms period; Docker and podman refuse less
    Constraint("cpus", "CPUs", "cap", float, 0.01, {"docker": lambda v: ["--cpus", spell(v)], "systemd": lambda v: ["-p", f"CPUQuota={spell(round(v * 100, 2))}%"]},
               ("cpu.max",), _cpus, capacity="cpus", tip="The time of this many logical CPUs, on any allowed core.",
               step=0.1),
    # 6: Docker's minimum (podman takes less). The same --memory-swap: no swap above the memory
    # sample 64: rootless Docker under systemd cannot start a container limited to 6 MB
    Constraint("memory_mb", "Memory", "cap", int, 6,
               {"docker": lambda v: ["--memory", f"{int(v)}m", "--memory-swap", f"{int(v)}m"],
                "systemd": lambda v: ["-p", f"MemoryMax={int(v)}M", "-p", "MemorySwapMax=0"]},
               ("memory.max", "memory.swap.max"), lambda out, v: out == [str(int(v) * MIB), "0"], capacity="memory_mb",
               unit="MB", tip="1 GB is 1024 MB.", step=16, sample=64),
    # the kernel's pids controller counts threads too: each takes a process id
    Constraint("processes", "Processes and threads", "cap", int, 1, {"docker": lambda v: ["--pids-limit", str(int(v))],
                                                                     "systemd": lambda v: ["-p", f"TasksMax={int(v)}"]},
               ("pids.max",), lambda out, v: out == [str(int(v))]),
    # the recipe sets its own (its concurrency, -j): the lower wins
    Constraint("concurrency", "Concurrent units", "cap", int, 1, machine="nullius"),
    # with cpus, a quota over these CPUs. systemd delegates no cpuset to users by default: the affinity stays
    Constraint("cpu_set", "CPUs Nullius may use", "pin", str, 0,
               {"docker": lambda v: ["--cpuset-cpus", v], "systemd": lambda v: ["-p", f"AllowedCPUs={v}"]},
               ("cpuset.cpus.effective",), _same_cpus, sample=FIRST_CPU, capacity="cpu_ids", fits=_cpus_within, machine="applied",
               tip="Such as 0-3,6. Empty: every CPU."),
    # [] allows none
    Constraint("gpu_ids", "GPUs Nullius may use", "pin", list, 0, capacity="gpus",
               fits=lambda v, gpus: set(v) <= {g["index"] for g in gpus},
               # nvidia-smi numbers GPUs by PCI bus, CUDA by default fastest first: on mixed GPUs the same index is another card
               environment=lambda v: {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": ",".join(map(str, v))}, machine="applied",
               tip="Such as 0,1. Empty: every GPU. none: no GPU."),
]
BY_KEY: Dict[str, Constraint] = {c.key: c for c in TABLE}


def _bad(c: Constraint, v) -> Optional[str]:
    """What is wrong with one value, or None."""
    if c.type is str:
        try:
            if not isinstance(v, str):
                raise ValueError(f"{v!r} is not a list of CPUs such as 0-3,6")
            cpu_list(v)
            return None
        except ValueError as e:
            return f"{c.label}: {e}"
    if c.type is list:
        ok = isinstance(v, list) and len(set(map(str, v))) == len(v) and \
            all(isinstance(i, int) and not isinstance(i, bool) and i >= c.minimum for i in v)
        return None if ok else f"{c.label}: a list of distinct GPU indices, [] for none"
    # isfinite first: int() of an infinity or a NaN raises
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (isinstance(v, int) or math.isfinite(v)) \
        and (c.type is not int or v == int(v)) and v >= c.minimum
    return None if ok else f"{c.label}: {'a whole number' if c.type is int else 'a number'} of at least {c.minimum:g}" + (f" {c.unit}" if c.unit else "")


def problems(values) -> List[str]:
    """What is wrong with one machine's ``{key: value}``: an unknown key, a
    value of the wrong type or under the minimum."""
    if not isinstance(values, dict):
        return ["constraints are an object of names and values"]
    out = []
    for k, v in values.items():
        c = BY_KEY.get(k)
        if c is None:
            out.append(f"{k} is not a constraint ({', '.join(BY_KEY)})")
        elif _bad(c, v):
            out.append(_bad(c, v))
    return out


def validate_setting(text: str) -> Optional[str]:
    """None when ``HOST_CONSTRAINTS`` is acceptable, else what it should be."""
    from nullius import hosts
    try:
        doc = json.loads(text)                     # Infinity and NaN read as floats: _bad refuses them
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return "a JSON object of machines and their constraints"
    for machine, values in doc.items():
        if machine != LOCAL:
            try:
                hosts.parse(machine)
            except ValueError as e:
                return f"valid constraints ({e})"
        bad = problems(values)
        if bad:
            return f"valid constraints ({machine}: " + "; ".join(bad) + ")"
    return None


def of(machine: str, env=None, strict: bool = True) -> dict:
    """One machine's constraints as Settings hold them; none when unset.
    ValueError when the setting is invalid, so a run is refused and not run
    without them; without ``strict``, none then (a hint, or a page that says
    the problem itself)."""
    text = (os.environ if env is None else env).get(SETTING, "")
    why = validate_setting(text) if text else None
    if why and strict:
        raise ValueError(f"{SETTING} must be {why}")
    return {} if not text or why else dict(json.loads(text).get(machine) or {})


def lowest(*sets: Optional[dict]) -> dict:
    """Several sets of caps as one: each key's lowest value."""
    out: dict = {}
    for s in sets:
        for k, v in (s or {}).items():
            out[k] = min(out[k], v) if k in out else v
    return out


def container(values: dict) -> dict:
    """The values the container backend applies to containers, in the table's order."""
    return {c.key: values[c.key] for c in TABLE if "docker" in c.apply and c.key in values}


def argv(values: dict, task: Optional[dict] = None) -> List[str]:
    """The docker flags for every container constraint that is set, and none
    other: the machine's ``values``, or a task's own (``task``: its ``cpus``
    and ``memory_mb``) where it is lower (:func:`lowest`). A machine's memory
    allows no swap above it, whichever of the two is applied; a task's memory
    alone is ``--memory`` alone, so its container keeps the swap Docker gives it."""
    flags = [w for k, v in container(lowest(values, task)).items() for w in BY_KEY[k].apply["docker"](v)]
    if "memory_mb" in (task or {}) and "memory_mb" not in values:
        i = flags.index("--memory-swap")
        del flags[i:i + 2]
    return flags


def scoped(values: dict) -> dict:
    """The values a systemd scope takes, in the table's order."""
    return {c.key: values[c.key] for c in TABLE if "systemd" in c.apply and c.key in values}


def scope_argv(values: dict, run: Sequence[str] = SYSTEMD_RUN) -> List[str]:
    """``run`` (``systemd-run`` and its options) with a property for every value a scope takes, up to its ``--``."""
    return [*run, *(w for k, v in scoped(values).items() for w in BY_KEY[k].apply["systemd"](v)), "--"]


@functools.lru_cache(maxsize=None)
def _literal(program: str) -> Tuple[str, ...]:
    """:data:`LITERAL` where ``program`` has it, else nothing: an older systemd-run expands nothing."""
    try:
        said = subprocess.run([program, "--help"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    return (LITERAL,) if LITERAL.split("=")[0] in said else ()


def session(env=None) -> Dict[str, str]:
    """What ``systemd-run --user`` needs to find the user manager: ``env``'s
    ``XDG_RUNTIME_DIR`` (else ``/run/user/<uid>``) and its
    ``DBUS_SESSION_BUS_ADDRESS`` (else the bus socket there, when it exists)."""
    env = os.environ if env is None else env
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    bus = env.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus:
        try:
            bus = f"unix:path={run}/bus" if stat.S_ISSOCK(os.stat(f"{run}/bus").st_mode) else ""
        except OSError:
            bus = ""
    return {"XDG_RUNTIME_DIR": run, **({"DBUS_SESSION_BUS_ADDRESS": bus} if bus else {})}


def wrap(cmd: List[str], env: dict, values: dict) -> Tuple[List[str], dict]:
    """``cmd`` in a systemd scope of its own, with the values a scope takes,
    and ``env`` with what ``systemd-run`` needs (:func:`session`); ``env -u``
    takes those away again, so the command gets ``env`` as it was. A scope
    keeps the process, its group, its streams and its exit code: ``systemd-run``
    and ``env`` exec the command. Unchanged without such a value."""
    if not scoped(values):
        return list(cmd), env
    added = {k: v for k, v in session().items() if k not in env}
    unset = [w for k in added for w in ("-u", k)]
    run = (*SYSTEMD_RUN, *_literal(SYSTEMD_RUN[0]))
    return [*scope_argv(values, run), *(["env", *unset] if unset else []), *cmd], {**env, **added}


def environment(values: dict) -> Dict[str, str]:
    """The variables the set constraints give the processes on the machine (a benchmark's driver)."""
    return {n: x for c in TABLE if c.environment and c.key in values for n, x in c.environment(values[c.key]).items()}


def confine(values: dict) -> None:
    """This process, and every process it starts, on the CPUs ``cpu_set`` names."""
    if "cpu_set" in values and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cpu_list(values["cpu_set"]))


def concurrency(asked: int, values: dict) -> int:
    """The units a run starts at a time on a machine: what it asked, no more than the machine allows."""
    return min(asked, int(values["concurrency"])) if "concurrency" in values else asked


def no_gpu(values: dict) -> bool:
    """Whether the machine allows Nullius no GPU at all."""
    return values.get("gpu_ids") == []


def cli_value(key: str, text: str):
    """A value as ``--constraint KEY=VALUE`` spells it."""
    c = BY_KEY[key]
    if c.type is list:
        return [int(i) for i in text.split(",") if i.strip()]
    return text if c.type is str else c.type(text)


def spell(v) -> str:
    """A value as one word that reads back exactly: a number (a whole one
    without a point), a cpuset, GPU indices separated by commas."""
    if isinstance(v, (list, str)):
        return ",".join(map(str, v)) if isinstance(v, list) else v
    return str(int(v)) if isinstance(v, int) or v.is_integer() else repr(v)


def cli_words(values: dict) -> List[str]:
    """``values`` as ``--constraint`` words: the inverse of :func:`cli_value`."""
    return [w for k, v in values.items() for w in ("--constraint", f"{k}={spell(v)}")]


# -- the probe ------------------------------------------------------------------

def probe(values: dict, image: str = PROBE_IMAGE, docker: str = "docker",
          options: Sequence[str] = ("--security-opt", "no-new-privileges")) -> str:
    """The shell script that reports a machine's capacity, from the machine
    itself whatever runs on it (``nproc``, ``MemTotal``, the
    GPUs ``nvidia-smi`` lists), then, when its engine (``docker``: docker or
    podman) answers, the engine and, for each container constraint in
    ``values``, what a container started with it and ``options`` (a unit's
    user namespace and security options) reads back: ``key=<the files'
    contents>`` on one line, or the engine's error."""
    # shortcut: Linux machines only (nproc, /proc/meminfo); a macOS host reads as no capacity
    lines = ["printf 'ncpu=%s\\n' \"$(nproc 2>/dev/null)\"",
             f"printf 'place.local=%s\\n' \"$({WALK})\"",
             "printf 'memtotal_kb=%s\\n' \"$(sed -n 's/^MemTotal: *\\([0-9]*\\) kB$/\\1/p' /proc/meminfo 2>/dev/null)\"",
             "command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,name,memory.total "
             "--format=csv,noheader,nounits 2>/dev/null | sed 's/^/gpu=/'"]
    # version|components|security options, podman's in Docker's terms (as container._security_options reads them)
    engine = [f"printf 'engine=%s|Podman,|%s\\n' \"$({docker} version --format '{{{{.Client.Version}}}}' 2>/dev/null)\" "
              f"\"$({docker} info --format '{{{{if .Host.Security.Rootless}}}}name=rootless{{{{end}}}}' 2>/dev/null)\""
              if docker == "podman" else
              f"printf 'engine=%s|%s\\n' \"$({docker} version --format '{{{{.Server.Version}}}}|{{{{range .Server.Components}}}}{{{{.Name}}}},{{{{end}}}}' "
              f"2>/dev/null)\" \"$({docker} info --format '{{{{.SecurityOptions}}}}' 2>/dev/null)\""]
    if container(values):     # pulled first, so a pull's progress is not read as a container's answer
        engine.append(f"{{ {docker} image inspect {image} >/dev/null 2>&1 || {docker} pull -q {image} >/dev/null 2>&1; }}")
        # where the engine puts a unit's container: only the host's cgroup namespace shows the cgroups above it
        run = [docker, "run", "--rm", "--network", "none", "--cap-drop", "ALL", *options, "--cgroupns=host", image, "sh", "-c", WALK]
        engine.append(f"w=$({' '.join(shlex.quote(w) for w in run)} 2>/dev/null); printf 'place.containers=%s\\n' \"$w\"; {_FIRST}")
    for k, v in container(values).items():
        c = BY_KEY[k]
        run = [docker, "run", "--rm", "--network", "none", "--cap-drop", "ALL", *options,
               *c.apply["docker"](v), image, "cat", *(f"/sys/fs/cgroup/{f}" for f in c.files)]
        engine.append(f"printf '{k}=%s\\n' \"$({' '.join(map(_quote, run))} 2>&1 | tr '\\n' ' ')\"")
    lines.append(f"if {docker} version >/dev/null 2>&1; then {'; '.join(engine)}; fi")
    return "; ".join(lines) + "; true"


def scope_probe(values: dict) -> str:
    """The shell script that says whether the user's systemd manager here can
    hold the values a scope takes: ``scope=ok``, then for each value what a
    scope started with it alone reads back (``scope.key=<the files' contents>``),
    or ``scope=<why not>``. Under an SSH session it needs lingering: without
    it the manager, and every scope, ends with the session."""
    # builtins and exec only: a scope of one process and thread (TasksMax=1) still answers
    here = f'{_OWN}cd "{CGROUP}$d" && exec cat '
    # where a scope lands: the ceiling of the processes a run starts here
    walk = f"w=$({' '.join([*SYSTEMD_RUN, '$x', 'sh', '-c', shlex.quote(WALK)])} 2>/dev/null); printf 'place.scope=%s\\n' \"$w\"; {_FIRST}; "
    # $x: LITERAL where this machine's systemd-run has it (wrap asks the same of the machine it runs on)
    tries = [f"printf 'scope.{k}=%s\\n' \"$({' '.join([*SYSTEMD_RUN, '$x', *map(_quote, [*scope_argv({k: v}, ()), 'sh', '-c', here + ' '.join(BY_KEY[k].files)])])}"
             f" 2>&1 | tr '\\n' ' ')\"" for k, v in scoped(values).items()]
    return "; ".join([
        'u=$(id -u); export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$u}"',
        '[ -n "$DBUS_SESSION_BUS_ADDRESS" ] || ! [ -S "$XDG_RUNTIME_DIR/bus" ] || export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"',
        "if ! command -v systemd-run >/dev/null 2>&1; then echo 'scope=systemd-run is not on this machine'; "
        f"elif ! [ -d {BOOTED} ]; then echo 'scope=systemd does not run this machine'; "
        f"elif ! [ -f {CGROUP}/cgroup.controllers ]; then echo 'scope=no cgroup v2'; "
        'elif ! systemctl is-active --quiet "user@$u.service"; then echo "scope=user@$u.service is not active"; '
        'elif [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then echo "scope=no session bus at $XDG_RUNTIME_DIR/bus"; '
        'elif [ -n "$SSH_CONNECTION" ] && [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; '
        "then echo 'scope=lingering is off: limits would end with the SSH session'; "
        "elif ! e=$(systemd-run --user --scope -q true 2>&1); then printf 'scope=%s\\n' \"$(printf %s \"$e\" | tr '\\n' ' ')\"; "
        f"else echo scope=ok; x=$(systemd-run --help 2>/dev/null | grep -q -e {LITERAL.split('=')[0]} && echo {LITERAL}); "
        + walk + "".join(t + "; " for t in tries) + "fi"])


def scope_check(values: dict) -> dict:
    """What a scope here holds of ``values`` (:func:`scope_probe`):
    ``{"why": why not, or empty, "enforced": [...], "refused": {key: what it read}}``."""
    try:
        out = subprocess.run(["sh", "-c", scope_probe(values)], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"why": f"the check failed: {e}", "enforced": [], "refused": {}}
    return read(out, values).get("systemd") or {"why": "the check did not answer", "enforced": [], "refused": {}}


def _held(said: dict, values: dict, prefix: str = "") -> Tuple[List[str], Dict[str, str]]:
    """The values whose files read back as asked, and what the others read."""
    enforced, refused = [], {}
    for k, v in values.items():
        got = said.get(prefix + k, "")
        if BY_KEY[k].enforced(got.split(), v):
            enforced.append(k)
        else:
            refused[k] = got[:200] or "no answer"
    return enforced, refused


def ceiling(walk: str) -> dict:
    """The lowest CPUs (``nproc``, each ``cpu.max`` quota over its period) and
    memory (each ``memory.max``, in MB) that a :data:`WALK` read; ``max`` is no
    limit; and the CPUs it may run on (``cpu_ids``)."""
    cpus, mb, ids = [], [], {}
    for w in walk.split():
        k, _, v = w.partition(":")
        q, _, period = v.partition("/")
        if k == "n" and v.isdigit():
            cpus.append(int(v))
        elif k == "m" and v.isdigit():
            mb.append(int(v) // MIB)
        elif k == "c" and q.isdigit() and period.isdigit() and int(period):
            cpus.append(int(q) / int(period))
        elif k == "a" and _CPUSET.fullmatch(v):
            ids = {"cpu_ids": v}
    n = round(min(cpus), 2) if cpus else None
    return {**({"cpus": int(n) if n == int(n) else n} if cpus else {}), **({"memory_mb": min(mb)} if mb else {}), **ids}


def read(out: str, values: dict) -> dict:
    """What ``probe`` (and ``scope_probe``) printed: the capacity (``cpus``,
    ``memory_mb``, ``cpu_ids``, ``gpus``) where the machine's runs land (in
    containers where an engine answers), and ``local``, the ceiling of local
    processes, where it differs; the engine (empty when none
    answers), the container constraints it enforced, why each other one was
    not, the constraints applied but not enforced, and, when the scope was
    probed, ``systemd``: ``{why, enforced, refused}`` for a scope."""
    said, gpus = {}, []
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "gpu":
            parts = [p.strip() for p in v.split(",")]
            if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
                gpus.append({"index": int(parts[0]), "name": parts[1], "memory_mb": int(parts[2])})
        elif k:
            said[k] = v.strip()
    whole: dict = {}
    if said.get("ncpu", "").isdigit():
        whole["cpus"] = int(said["ncpu"])
    if said.get("memtotal_kb", "").isdigit():
        whole["memory_mb"] = int(said["memtotal_kb"]) // 1024
    version, _, rest = said.get("engine", "").partition("|")
    # a placement that was not read is the whole machine's
    local = lowest(whole, ceiling(said.get("place.scope") or said.get("place.local", "")))
    capacity = lowest(whole, ceiling(said.get("place.containers", ""))) if version else local
    if capacity != local:
        capacity["local"] = local
    if capacity:                         # measured: no GPU listed is none there
        capacity["gpus"] = gpus
    engine = {"kind": "podman" if "Podman" in rest else "docker", "version": version, "rootless": "name=rootless" in rest,
              "userns_remap": "name=userns" in rest} if version else {}
    enforced, refused = _held(said, _taken(container(values), ceiling(said.get("place.containers", ""))) if engine else {})
    found = {"capacity": capacity, "engine": engine, "enforced": enforced, "refused": refused,
             "applied": [c.key for c in TABLE if c.machine == "applied"]}
    if "scope" in said:
        ok = said["scope"] == "ok"
        found["systemd"] = dict(zip(("enforced", "refused"), _held(said, _taken(scoped(values), ceiling(said.get("place.scope", ""))), "scope.") if ok else ([], {})),
                                why="" if ok else said["scope"][:200])
    return found


def _amount(key: str, v) -> str:
    """A value with what it counts, as a person reads it."""
    if key == "memory_mb":
        return f"{int(v) // 1024} GB" if v and int(v) % 1024 == 0 else f"{int(v)} MB"
    if key == "cpus":
        return f"{spell(v)} CPU" + ("" if v == 1 else "s")
    if key == "processes":
        return f"{int(v)} process" + ("" if int(v) == 1 else "es")
    return f"CPUs {v}" if key == "cpu_set" else spell(v)


def explain(key: str, value, out: str, scope: bool = False) -> str:
    """Why a constraint did not hold, in words: what was asked and what the
    container (``scope``: the scope) had, or the engine's (systemd's) own
    message, shortened."""
    asked, words = f"Asked {_amount(key, value)}", out.split()
    by, it = ("systemd", "the scope") if scope else ("the engine", "the container")
    if not words or out == "no answer":
        return f"{asked}: {by} did not answer"
    if re.search(r"error|warning|no such file|not available|cat:|failed", out, re.I):
        return f"{asked}: {by} did not apply it ({textwrap.shorten(' '.join(words), 160, placeholder=' …')})"
    if "max" in words[:1]:
        return f"{asked}, {it} had no limit"
    try:
        if key == "cpus":
            return f"{asked}, {it} had {_amount(key, int(words[0]) / int(words[1]))}"
        if key == "memory_mb":
            mb = int(words[0]) // MIB
            return f"{asked}, {it} had {_amount(key, mb)}" if mb != int(value) else f"{asked}, {it} could also use swap"
        if key == "processes":
            return f"{asked}, {it} had {_amount(key, int(words[0]))}"
    except (ValueError, IndexError, ZeroDivisionError):
        pass
    return f"{asked}, {it} read {' '.join(words)[:80]}"


def _taken(values: dict, cap: dict) -> dict:
    """``values`` with :data:`FIRST_CPU` as the CPU the probe took where ``cap`` was measured, as ``_FIRST`` does."""
    first = re.match(r"\d*", cap.get("cpu_ids", "")).group() or "0"
    return {k: first if v == FIRST_CPU else v for k, v in values.items()}


def samples(cap: Optional[dict] = None) -> dict:
    """Each constraint a container or a scope takes, at its sample or its minimum: what the connection test probes with;
    with ``cap``, a placement's capacity, the values it took there."""
    out = {c.key: c.minimum if c.sample is None else c.sample for c in TABLE if "docker" in c.apply}
    return out if cap is None else _taken(out, cap)


# -- what the last test found, per machine ----------------------------------------

_STORE = threading.Lock()                 # one load-modify-write at a time: Test all tests every machine at once


def store_path(env=None) -> str:
    return os.path.join(paths.data_dir(env), "capacity.json")


def load(env=None) -> Dict[str, dict]:
    """Every machine's last test: ``{machine: {capacity, enforced, refused, applied, at}}``; empty when unread."""
    try:
        with open(store_path(env), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save(machine: str, found: dict, env=None) -> None:
    """One machine's test over what it found before (a test that did not reach
    the engine keeps the last capacity), kept beside the others."""
    with _STORE:
        doc = load(env)
        doc[machine] = {**(doc.get(machine) or {}), **found, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        doc[machine].pop("stale", None)
        _write(doc, env)


def move(old: str, new: Optional[str], stale: bool = False, env=None) -> None:
    """A machine's last test under its new entry (``stale`` when it now names
    another machine), or forgotten when ``new`` is None."""
    with _STORE:
        doc = load(env)
        found = doc.pop(old, None)
        if found is not None and new is not None:
            doc[new] = dict(found, stale=True) if stale else found
        _write(doc, env)


def rekey(text: str, old: str, new: Optional[str]) -> Optional[str]:
    """``HOST_CONSTRAINTS`` with a machine's values under its new entry, or
    without them when ``new`` is None; None when nothing is left. ValueError
    when the setting is invalid: changing it then would drop every machine's."""
    why = validate_setting(text) if text else None
    if why:
        raise ValueError(f"{SETTING} must be {why}")
    doc = json.loads(text) if text else {}
    values = doc.pop(old, None)
    if values is not None and new is not None:
        doc[new] = values
    return json.dumps(doc) if doc else None


def _write(doc: dict, env=None) -> None:
    """The store, written whole to a file of its own, then renamed."""
    out = store_path(env)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fd, part = tempfile.mkstemp(dir=os.path.dirname(out), prefix="capacity.json.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        os.replace(part, out)
    except BaseException:
        os.unlink(part)
        raise


def engine_less(found: Optional[dict]) -> bool:
    """Whether a machine's last test found no engine answering there, so no container constraint can hold."""
    return bool((found or {}).get("capacity")) and not (found.get("engine") or found.get("enforced") or found.get("refused"))


def scope_ok(found: Optional[dict]) -> bool:
    """Whether a machine's last test started a systemd scope there."""
    sd = (found or {}).get("systemd")
    return sd is not None and not sd.get("why")


def not_enforced(values: dict, found: Optional[dict], backend: Optional[str] = None, own: bool = False) -> Dict[str, str]:
    """The constraints set for a machine that its last test found will not
    hold for a run there, so the run is refused, each with what does not hold
    it: ``engine``, a container constraint the engine does not hold (on the
    container backend, or with ``own``, units in their own images whatever
    the backend); ``systemd``, on the local backend, a cap no scope held.
    ``backend`` None: on either backend, as Settings says it. Nothing of what
    a test did not measure (the machine, or its scope): the run checks again
    as it starts."""
    if not (found or {}).get("capacity"):
        return {}
    lost = {}
    sd = found.get("systemd")
    if backend in (None, "local") and sd is not None:
        held = sd.get("enforced") or []
        lost.update({k: "systemd" for k in scoped(values) if not BY_KEY[k].machine and k not in held})
    # either backend: an engine's, where one answers (without one no container runs there, and Review says so)
    if backend == "container" or own or (backend is None and not engine_less(found)):
        lost.update({k: "engine" for k in container(values) if k not in (found.get("enforced") or [])})
    return {k: lost[k] for k in values if k in lost}


def undelegated(found: Optional[dict]) -> bool:
    """Whether a rootless engine was given no cgroup to limit: it enforced none
    of the container constraints, each read back without its limit or refused
    for want of a controller."""
    found = found or {}
    refused = found.get("refused") or {}
    return bool((found.get("engine") or {}).get("rootless")) and not found.get("enforced") and \
        set(refused) >= set(samples()) and all(re.match(r"max\b", why) or re.search(r"controller|cgroup", why) for why in refused.values())


# what Settings says of a constraint that does not hold, by what does not hold it (not_enforced)
UNENFORCED = {"engine": "Containers here do not enforce it: see Info, or remove it.",
              "systemd": "Local processes here do not enforce it: run in a container, or remove it."}


def warnings(values: dict, found: Optional[dict]) -> Dict[str, str]:
    """One line per key that the machine's last test argues against: a value
    beyond its capacity, a constraint it does not enforce (:func:`not_enforced`),
    or concurrent units whose containers together can take more than it has.
    None for a machine never tested."""
    if not found:
        return {}
    cap, out = found.get("capacity") or {}, {}
    # without an engine or a scope the caps are off in Settings, which says what they need
    lost = {} if engine_less(found) and not scope_ok(found) else not_enforced(values, found)
    for k, v in values.items():
        c = BY_KEY.get(k)
        if c is None:
            continue
        if c.capacity and c.capacity in cap and not c.fits(v, cap[c.capacity]):
            out[k] = "More than the machine has."
        elif k in lost:
            out[k] = UNENFORCED[lost[k]]
    n = values.get("concurrency")
    if n and any(k not in out and c.kind == "cap" and c.capacity in cap and n * values[k] > cap[c.capacity]
                 for k, c in BY_KEY.items() if "docker" in c.apply and k in values):
        out["concurrency"] = "Together these units can take more than the machine has."
    return out
