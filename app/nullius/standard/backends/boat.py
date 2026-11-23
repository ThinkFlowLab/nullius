"""The ``boat`` backend: each unit's agent or capability in a boat.dev VM of its own.

A boat VM is a cloud Ubuntu machine created by its API (``API``) and reached
over plain OpenSSH. Everything that holds an answer or a key stays on this
machine: the driver, the unit's relay, the spending cap, the scoring. Only
the process of the agent (or the capability) runs in the VM, as ``USER``, a
user of its own whose only way out is the VM's loopback (``LOCK``): no
public address, no DNS. Its model calls reach the unit's relay through a
reverse tunnel on the same port (``ssh -R``), so the address it is given is
the one the local backend gives, and the relay records every call here.

One unit, one VM (:class:`VM`) of the run's size (``SIZES``, ``small`` by
default; :func:`size_problem` says when the account cannot start it): created when the unit's agent is about to
start, deleted once its workspace is copied back. A run's concurrency is
therefore the number of VMs at once, never more than the account allows at
once (:func:`slots`); a VM the account cannot start yet (its limit, its rate
of starts) is waited for, and the wait ends with the run's Stop. Every VM
carries the run's label, so whatever an interrupted unit left is deleted by
:func:`sweep` when the run ends; a VM of a run whose process was killed stops
at its auto-stop: as long as its unit may take, at least ``TTL``, and ``TTL``
at most on a free trial, which allows no more.

What the VM is given, each at the same absolute path it has here so every
path in the agent's configuration still holds: the agent's install, its
definition, the runtimes the install links to outside itself (a ``uv``
Python, never a folder holding this user's home, keys or Nullius's own
storage), sent before the agent's time starts; then, as the agent starts, its
home and the unit's workspace, which its setup writes up to then. The workspace comes back with
links that leave it dropped (``rsync --safe-links``): the driver reads it to
score, and a link the agent left must not reach a file of this machine.
Nothing of this machine's environment goes with it but its locale.

The API key is kept in the owner-only store (``nullius.secrets``, the folder
``STORE``), set in Settings → Sandbox; ``BOAT_API_KEY`` in the environment
wins. It is sent to boat.dev only, never to the VM. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from nullius import paths, secrets
from nullius.standard.requirements import install

API = "https://boat.dev/api/v1"
STORE, NAME = "sandbox-provider-boat", "BOAT_API_KEY"
# Boat's machine types, by name: its label and, where measured, its vCPUs and memory. The VM runs the agent's
# process, never its model: the smallest is enough, unless a task boots a VM of its own inside it (fits).
SIZES = {"small": {"label": "Small", "cpus": 2, "memory_gb": 4}, "default": {"label": "Default", "cpus": 4, "memory_gb": 8},
         "large": {"label": "Large"}, "xlarge": {"label": "XLarge"}}
SIZE = "small"
TTL = 2 * 3600                       # a VM's auto-stop at least, and a free trial's at most
USER = "agent"
LABEL = "nullius-job"
VM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
KEEP = ("LANG", "LC_ALL", "TERM")    # of the local environment, what makes sense on another machine
WAIT_SECONDS = 3600                  # an account's starts an hour come back within one
READY_SECONDS = 300
SSH_SECONDS = 900
DRIVER = 1800                        # a unit's time beyond its agent's: sending, bringing back

# The agent's user reaches the VM's loopback and nothing else: no public address, no DNS. Inserted
# first in OUTPUT, before the chains boat's own firewall accepts in; each at 1, so the last inserted
# is matched first: DNS on loopback refused, then loopback accepted, then everything else refused.
LOCK = ("U=$(id -u {user}) && for t in iptables ip6tables; do "
        "sudo -n $t -I OUTPUT 1 -m owner --uid-owner $U -j REJECT && "
        "sudo -n $t -I OUTPUT 1 -o lo -m owner --uid-owner $U -j ACCEPT && "
        "sudo -n $t -I OUTPUT 1 -o lo -p tcp --dport 53 -m owner --uid-owner $U -j REJECT && "
        "sudo -n $t -I OUTPUT 1 -o lo -p udp --dport 53 -m owner --uid-owner $U -j REJECT; done")
_SYSTEM = ("/usr/", "/bin/", "/lib", "/sbin/", "/etc/", "/opt/")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")    # a variable a shell can export


class Unavailable(Exception):
    """boat cannot run this unit: no key, a refusal of its API, a VM that never came up."""


def key(env=os.environ) -> Optional[str]:
    """The account's API key: ``BOAT_API_KEY``, else the store's; None when neither is set."""
    if env.get(NAME):
        return env[NAME].strip()
    v = secrets.value(STORE, NAME)
    return v.decode().strip() if v else None


PROGRAMS = ("ssh", "ssh-keygen", "rsync", "tar")     # what starts, reaches and fills a VM, where the run runs


def unusable(here: bool = True, env=os.environ, which: Callable = shutil.which) -> Optional[str]:
    """Why the boat backend cannot run, or None: its key, and with ``here`` this machine's programs."""
    if not key(env):
        return "no Boat API key: set it in Settings → Sandbox"
    lacking = [p for p in PROGRAMS if not which(p)] if here else []
    if lacking:
        return "missing on this machine: " + ", ".join(lacking)
    return None


class Client:
    """boat.dev's API, with one account's key."""

    def __init__(self, api_key: str, base: str = API, opener: Callable = urllib.request.urlopen, timeout: float = 60):
        self.key, self.base, self.opener, self.timeout = api_key, base, opener, timeout

    def call(self, method: str, path: str, body: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                                              "Accept": "application/json", **(headers or {})})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read() or b"{}")
            except ValueError:
                d = {}
            return {"ok": False, "status": e.code, **d} if isinstance(d, dict) else {"ok": False, "status": e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "code": "unreachable", "message": f"Boat did not answer: {e}"}

    def limits(self) -> dict:
        return self.call("GET", "/limits")

    def create(self, labels: dict, size: str = SIZE, ttl: int = TTL) -> dict:
        return self.call("POST", "/sandboxes", {"type": size, "ttlSeconds": ttl, "name": "nullius-unit", "labels": labels})

    def get(self, vm: str) -> dict:
        return self.call("GET", f"/sandboxes/{urllib.parse.quote(vm)}")

    def ssh_key(self, vm: str, public: str) -> dict:
        return self.call("POST", f"/sandboxes/{urllib.parse.quote(vm)}/sshkey", {"key": public})

    def delete(self, vm: str) -> dict:
        return self.call("DELETE", f"/sandboxes/{urllib.parse.quote(vm)}", headers={"X-Ascii-Confirm-Delete": vm})

    def labelled(self, label: str, value: str) -> List[dict]:
        d = self.call("GET", "/sandboxes?" + urllib.parse.urlencode({"label": f"{label}={value}", "limit": 100}))
        return [s for s in d.get("sandboxes") or [] if (s.get("labels") or {}).get(label) == value]


def said(d: dict) -> str:
    return str(d.get("message") or d.get("code") or d.get("status") or "no answer")


def slots(client: Client, limits: Optional[dict] = None) -> Optional[int]:
    """How many VMs the account may run at once; None when its API does not say. ``limits``: its
    answer to ``GET /limits`` when already asked."""
    n = (client.limits() if limits is None else limits).get("maxActiveSandboxes")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def starts_problem(client: Client, n: int) -> Optional[str]:
    """What the account's starts left say of a run of ``n`` VMs, one per unit, or None."""
    left = client.limits().get("starts") or {}
    day, hour = ((left.get(k) or {}).get("remaining") for k in ("day", "hour"))
    if isinstance(day, int) and n > day:
        return (f"Boat can start {day} more VMs today on this account: {n - day} of the {n} units "
                "will end without running")
    if isinstance(hour, int) and n > hour:
        return (f"Boat can start {hour} more VMs this hour on this account: the other units wait "
                "for the next hour")
    return None


def machine_types(client: Client, limits: Optional[dict] = None) -> Dict[str, Optional[str]]:
    """Each machine type the account knows: None when it may start one, else boat's reason why not
    ("not during the free trial"). Empty when its API does not say."""
    out = {}
    for t in (client.limits() if limits is None else limits).get("machineTypes") or []:
        if isinstance(t, dict) and isinstance(t.get("type"), str):
            out[t["type"]] = None if t.get("available") else str(t.get("reason") or "not available on this account")
    return out


def spec(size: str) -> str:
    """A size's vCPUs and memory as a card says them, or "" when unmeasured."""
    z = SIZES.get(size) or {}
    return f"{z['cpus']} vCPU, {z['memory_gb']} GB" if "cpus" in z and "memory_gb" in z else ""


def fits(size: str, vm_memory_gb) -> bool:
    """Whether a VM of ``size`` can boot a task's own VM of ``vm_memory_gb`` (OSWorld's guest): the guest's
    memory must be free inside it, so the size needs more than the guest; a size of unmeasured memory, or
    no guest, is not refused here."""
    have = (SIZES.get(size) or {}).get("memory_gb")
    return not vm_memory_gb or not isinstance(have, (int, float)) or have > vm_memory_gb


def size_problem(client: Client, size: str) -> Optional[str]:
    """Why this account cannot start a VM of ``size``, or None."""
    if size not in SIZES:
        return f"{size!r} is not one of Boat's sizes: {', '.join(SIZES)}"
    return machine_types(client).get(size)


def gone(d: dict) -> bool:
    """A delete's answer: deleted, or not there to delete."""
    return bool(d.get("ok")) or d.get("status") == 404


def sweep(job: str, client: Optional[Client] = None, say: Optional[Callable[[str], None]] = None) -> int:
    """Deletes every VM labelled with ``job``: what a crash or a kill left. The number deleted;
    each one Boat would not delete is said."""
    if client is None:
        k = key()
        if not k:
            return 0
        client = Client(k)
    n = 0
    for s in client.labelled(LABEL, job):
        d = client.delete(s["id"])
        if gone(d):
            n += 1
        elif say is not None:
            say(f"Boat did not delete VM {s['id']}: {said(d)}. It stops by itself at its auto-stop; delete it at boat.dev")
    return n


def _private(env=os.environ) -> tuple:
    """What a folder sent to a VM may not hold, and what it may not be inside of."""
    home = os.path.realpath(os.path.expanduser("~"))
    # where this user's tools keep their logins: never sent, nor a folder holding one (``~/.local`` holds keyrings)
    keys = [os.path.join(home, d) for d in (".ssh", ".gnupg", ".aws", ".docker", ".kube", ".password-store", ".config")] \
        + [os.path.realpath(paths.config_dir(env))]
    holds = [os.path.join(home, d) for d in (".local", ".cache", ".netrc", ".git-credentials")]
    return [home, *holds, os.path.realpath(paths.data_dir(env)), os.path.realpath(paths.cache_dir(env)), *keys], keys


def outside_roots(where: str, env=os.environ) -> List[str]:
    """The folders an install links to outside itself and outside the system's own: a ``uv``
    environment's Python, kept in uv's folder of this user. Each must reach the VM too. A link
    whose folder would hold this user's home, keys or Nullius's storage is the install's
    ``Unavailable``: none of it may leave this machine."""
    roots = {}
    real_where = os.path.realpath(where)
    for top, dirs, files in os.walk(where):
        for n in dirs + files:
            p = os.path.join(top, n)
            if not os.path.islink(p):
                continue
            t = os.path.realpath(p)
            if t == real_where or t.startswith(real_where + os.sep) or t.startswith(_SYSTEM) or not os.path.exists(t):
                continue
            if os.path.isdir(t):
                r = t
            else:
                d = os.path.dirname(t)
                r = os.path.dirname(d) if os.path.basename(d) == "bin" else d
            roots.setdefault(r, p)
    holds, inside = _private(env)
    for r, link in roots.items():
        if any(r == s or s.startswith(r.rstrip(os.sep) + os.sep) for s in holds) or any(r.startswith(s + os.sep) for s in inside):
            raise Unavailable(f"{link} links to {r}, which would send this user's own files to a Boat VM")
    out: List[str] = []
    for r in sorted(roots, key=len):           # a root inside another is sent with it
        if not any(r == o or r.startswith(o + os.sep) for o in out):
            out.append(r)
    return out


class Keys:
    """A job's SSH key (made for it, never this user's own) and the host keys it pinned."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="nullius-boat-")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nullius-boat", "-f", os.path.join(self.dir, "id")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(self.dir, "id.pub")) as f:
            self.public = f.read().strip()

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class VM:
    """One unit's VM, ready: created, its host key pinned, the agent's user made and locked in.
    ``stopped``: the run's Stop, which ends a wait for the account to start it, or for it to come up. ``ttl``: its
    auto-stop, asked for; ``self.ttl`` the one it has, ``TTL`` on a free trial."""

    def __init__(self, client: Client, keys: Keys, job: str, *, size: str = SIZE, stopped: Optional[threading.Event] = None,
                 wait: float = WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep, ttl: int = TTL, lock: bool = True):
        self.client, self.keys, self.id, self.size, self.ttl = client, keys, None, size, max(int(ttl), TTL)
        self.stopped = stopped
        t0 = time.time()
        pause = 5.0
        while True:
            d = client.create({LABEL: job}, size=size, ttl=self.ttl)
            if d.get("ok"):
                break
            if d.get("code") == "trial_auto_stop_required" and self.ttl > TTL:
                self.ttl = TTL
                continue
            # the account's limit of VMs at once, or of starts a minute: another unit's VM ends, a minute passes
            if d.get("status") != 429 and d.get("code") not in ("limit_reached", "rate_limited"):
                raise Unavailable("Boat refused a VM: " + said(d))
            if (stopped is not None and stopped.is_set()) or time.time() - t0 > wait:
                raise Unavailable("Boat could not start a VM in time: " + said(d))
            sleep(pause)
            pause = min(pause * 1.5, 30.0)
        self.id = d["sandbox"]["id"]
        try:
            self._ready(sleep)
            if lock:                       # a whole unit (boat_units) is the run's own code: no agent's user to lock in
                self.sh(f"sudo -n useradd -m -s /bin/bash {USER} && " + LOCK.format(user=USER))
        except BaseException:
            self.delete()
            raise
        self.seconds = round(time.time() - t0, 1)

    def _halt(self) -> None:
        if self.stopped is not None and self.stopped.is_set():
            raise Unavailable(f"Boat VM {self.id}: the run was stopped")

    def _ready(self, sleep) -> None:
        t0 = time.time()
        while True:
            self._halt()
            s = self.client.get(self.id).get("sandbox") or {}
            if s.get("state") in ("ready", "idle", "running"):
                break
            if s.get("state") in ("archived", "deleted", "failed", "error") or time.time() - t0 > READY_SECONDS:
                raise Unavailable(f"Boat VM {self.id} did not come up (state {s.get('state')})")
            sleep(2)
        k = self.client.ssh_key(self.id, self.keys.public)
        if not k.get("ok") or not k.get("hostKey"):
            raise Unavailable(f"Boat VM {self.id} took no SSH key: " + said(k))
        ep = k.get("sshEndpoint") or f"{k['machineIp']}:22"
        self.host, self.port = ep.rsplit(":", 1)
        self.user = k.get("sshUser") or "user"
        known = os.path.join(self.keys.dir, f"known-{self.id}")
        with open(known, "w") as f:
            f.write(f"{self.id} {k['hostKey']}\n")
        self.ssh = ["ssh", "-F", "/dev/null", "-i", os.path.join(self.keys.dir, "id"), "-p", self.port, "-o", "IdentitiesOnly=yes",
                    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
                    "-o", f"HostKeyAlias={self.id}", "-o", "ServerAliveInterval=30", "-o", "LogLevel=ERROR",
                    "-o", "ConnectTimeout=20"]
        self.target = f"{self.user}@{self.host}"
        for _ in range(30):                     # sshd answers a moment after the key is in, or hangs while it boots
            self._halt()
            try:
                if subprocess.run(self.ssh + [self.target, "true"], capture_output=True, timeout=60).returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            sleep(2)
        raise Unavailable(f"Boat VM {self.id} did not answer over SSH")

    def sh(self, cmd: str, stdin=None) -> str:
        try:
            r = subprocess.run(self.ssh + [self.target, cmd], stdin=stdin, capture_output=True, timeout=SSH_SECONDS)
        except subprocess.TimeoutExpired:
            raise Unavailable(f"Boat VM {self.id}: {cmd.split(' ', 3)[:3]}: no answer in {SSH_SECONDS} s") from None
        if r.returncode:
            raise Unavailable(f"Boat VM {self.id}: {cmd.split(' ', 3)[:3]}: "
                              + r.stderr.decode(errors="replace").strip()[-300:])
        return r.stdout.decode(errors="replace")

    def send(self, read: Iterable[str], owned: Iterable[str] = ()) -> None:
        """Each folder at its own path in the VM, in one compressed stream: ``read`` the agent's user
        may read, ``owned`` it owns (its home, its workspace)."""
        read, owned = [p.rstrip("/") for p in read if p], [p.rstrip("/") for p in owned if p]
        rel = [os.path.relpath(p, "/") for p in dict.fromkeys(read + owned)]
        q = " ".join(shlex.quote("/" + r) for r in rel)
        mine = " ".join(shlex.quote(p) for p in owned)
        failed = None
        with tempfile.TemporaryFile() as err:      # a file, never a pipe: tar's warnings cannot stall the stream
            tar = subprocess.Popen(["tar", "-C", "/", "-czf", "-", "--", *rel], stdout=subprocess.PIPE, stderr=err)
            try:
                self.sh("sudo -n tar -C / -xzpf - --no-same-owner && "
                        f"for p in {q}; do while [ \"$p\" != / ]; do sudo -n chmod o+rx \"$p\"; p=$(dirname \"$p\"); done; done"
                        + (f" && sudo -n chown -R {USER}:{USER} {mine}" if mine else "")
                        + f" && sudo -n chmod -R o+rX {q}", stdin=tar.stdout)
            except Unavailable as e:
                failed = e
            finally:
                tar.stdout.close()
                code = tar.wait()
            err.seek(0)
            said = err.read().decode(errors="replace").strip()[-300:]
        # tar's 1 is a file that changed as it was read: what the VM received still stands; a stream the
        # VM stopped reading is the VM's failure, said by the VM
        cut = "status 141" in said or "Broken pipe" in said
        if (code > 1 and not (failed and cut)) or (code and failed and not cut):
            raise Unavailable(f"could not pack what Boat VM {self.id} needs: {said}")
        if failed:
            raise failed

    def fetch(self, path: str) -> None:
        """The folder back from the VM, as it is there: what the agent removed is removed here, a link
        leaving the folder is not copied, nor a device or a pipe."""
        path = path.rstrip("/")
        rsh = " ".join(shlex.quote(w) for w in self.ssh)
        try:
            r = subprocess.run(["rsync", "-rlt", "--delete", "--safe-links", "--no-devices", "--no-specials",
                                "--rsync-path=sudo -n rsync", "-e", rsh, f"{self.target}:{path}/", path + "/"],
                               capture_output=True, timeout=SSH_SECONDS)
        except subprocess.TimeoutExpired:
            raise Unavailable(f"the workspace did not come back from Boat VM {self.id} in {SSH_SECONDS} s") from None
        if r.returncode:
            raise Unavailable(f"the workspace did not come back from Boat VM {self.id}: "
                              + r.stderr.decode(errors="replace").strip()[-300:])

    def argv(self, cmd: List[str], env: Dict[str, str], workdir: str, port: Optional[int]) -> List[str]:
        """``cmd`` run in the VM as the agent's user, in ``workdir``, with ``env`` alone; its stdin and
        stdout this process's (ACP, JSON lines). ``port``: the relay's, the same port in the VM. The
        environment (the relay's word, a licence's value) never goes on a command line, here or in the
        VM: it is sent first as a file only the agent's user reads, which its shell reads and removes."""
        lines = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items() if _NAME.match(k))
        with tempfile.TemporaryFile() as f:
            f.write(lines.encode())
            f.seek(0)
            made = self.sh(f"f=$(mktemp /tmp/.nullius-env.XXXXXXXX) && cat > \"$f\" && sudo -n chown {USER} \"$f\" && echo \"$f\"",
                           stdin=f).strip()
        remote = (f"cd {shlex.quote(workdir)} && exec sudo -n -u {USER} env -i /bin/bash -c "
                  + shlex.quote('. "$0" && rm -f "$0" && exec "$@"') + f" {shlex.quote(made)} "
                  + " ".join(shlex.quote(w) for w in cmd))
        tunnel = ["-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{port}:127.0.0.1:{port}"] if port else []
        return self.ssh + ["-T", *tunnel, self.target, remote]

    def delete(self) -> Optional[str]:
        """Deletes the VM, a second time if Boat did not; why it is still there, or None."""
        if not self.id:
            return None
        for _ in range(2):
            d = self.client.delete(self.id)
            if gone(d):
                break
        else:
            return said(d)
        try:
            os.remove(os.path.join(self.keys.dir, f"known-{self.id}"))
        except OSError:
            pass
        return None


def remote_env(env: Dict[str, str], bin_dirs: List[str], local: Sequence[str] = ()) -> Dict[str, str]:
    """A process's environment as the VM gives it: ``local``, the variables taken of this machine's
    environment, dropped but for its locale; its ``PATH`` the install's programs, then the VM's."""
    out = {k: v for k, v in env.items() if k not in local or k in KEEP}
    out["PATH"] = os.pathsep.join(bin_dirs + [VM_PATH])
    return out


MCP_REFUSAL = ("the benchmark's MCP servers run from this machine, which an agent in a Boat VM cannot start "
               "(--backend local or container)")


def refusal(env_def: Optional[dict]) -> Optional[str]:
    """Why an environment's agent cannot run in a Boat VM, or None: the public web it is granted
    (``network``), a licence file or server of this machine it must reach."""
    if (env_def or {}).get("network"):
        return "grants its agent the public web, which a Boat VM does not yet (--backend container)"
    for s in (env_def or {}).get("needs_secrets") or []:
        if s.get("to") == "agent" and s.get("kind") in ("file", "server"):
            return f"gives its agent the licence {s['name']} of this machine, which a Boat VM cannot reach (--backend container)"
    return None


class Backend:
    """The boat backend for one run: its account, the label its VMs carry (this run's alone, though a
    job's name may be used again), its SSH key, made with its first VM, and the VM each unit's thread
    holds while its agent runs (:meth:`unit`, :meth:`place`). :meth:`close` deletes what is left."""

    def __init__(self, job: str, env=os.environ, client: Optional[Client] = None, size: str = SIZE,
                 say: Callable[[str], None] = lambda s: None):
        self.client = client or Client(key(env) or "")
        self.env = env
        self.size, self.say, self._cut = size, say, False
        self.label = f"{job}.{os.urandom(4).hex()}"
        self._keys: Optional[Keys] = None
        self._roots: Dict[str, List[str]] = {}
        self._lock = threading.Lock()
        self._held = threading.local()

    def starts(self, n: int) -> Optional[str]:
        return starts_problem(self.client, n)

    def most(self) -> Optional[int]:
        """How many units may run at once: the VMs the account runs at once."""
        return slots(self.client)

    @contextmanager
    def unit(self, trial: str, ws: str, home: str, stopped: Optional[threading.Event] = None, *,
             inst: Optional[str] = None, here: Optional[str] = None, seconds: float = 0):
        """The unit's VM while its agent or capability runs, given before it starts, each at its own
        path: the install (``inst``) and the runtimes it links to outside itself, and the definition
        (``here``). ``agent/boat.json`` says which VM, and how long it took to come up and be sent
        all that. Its workspace and home (sent by :meth:`place`) are brought back
        from it, then the VM deleted. A failure to bring them back after the agent ended is the
        unit's ``Unavailable``; after the agent failed, its own failure stands, and after a Stop
        nothing is brought back. ``seconds``: the longest the unit may take, its VM's auto-stop
        with the sending and the bringing back (``DRIVER``); a trial's shorter one is said once."""
        with self._lock:
            if self._keys is None:
                self._keys = Keys()
            if inst and inst not in self._roots:
                self._roots[inst] = outside_roots(inst, self.env) if os.path.isdir(inst) else []
        read = [p for p in [inst, here, *self._roots.get(inst or "", [])] if p and os.path.isdir(p)]
        t0 = time.time()
        # once a trial's cut is known, asked at once: a refused create would count against the account's starts
        vm = VM(self.client, self._keys, self.label, size=self.size, stopped=stopped, ttl=TTL if self._cut else seconds + DRIVER)
        try:
            if vm.ttl < seconds + DRIVER and not self._cut:
                self._cut = True
                self.say(f"Boat stops a free trial's VMs after {TTL // 3600} hours: a unit still running then ends there")
            vm.send(read)
            os.makedirs(os.path.join(trial, "agent"), exist_ok=True)
            with open(os.path.join(trial, "agent", "boat.json"), "w") as f:
                json.dump({"vm": vm.id, "size": self.size, "ttl_seconds": vm.ttl, "ready_seconds": vm.seconds,
                           "sent_seconds": round(time.time() - t0, 1)}, f)
            self._held.vm = vm
            try:
                yield vm
            except BaseException as e:
                stop = isinstance(e, KeyboardInterrupt) or (stopped is not None and stopped.is_set())
                for d in () if stop else (ws, home):     # what it left, for the evidence
                    try:
                        vm.fetch(d)
                    except Unavailable:
                        pass
                raise
            for d in (ws, home):
                vm.fetch(d)
        finally:
            self._held.vm = None
            why = vm.delete()
            if why:
                # still billing until its auto-stop: said, and kept with the unit's evidence
                self.say(f"Boat did not delete VM {vm.id}: {why}. It stops by itself within {vm.ttl // 3600} hours; "
                         "delete it at boat.dev")
                try:
                    with open(os.path.join(trial, "agent", "boat.json")) as f:
                        rec = json.load(f)
                    with open(os.path.join(trial, "agent", "boat.json"), "w") as f:
                        json.dump(dict(rec, delete_error=why), f)
                except (OSError, ValueError):
                    pass

    def place(self, cmd: List[str], env: Dict[str, str], workdir: str, *, inst: Optional[str], owned: List[str],
              relay_url: Optional[str], local: Sequence[str] = ()) -> tuple:
        """``cmd`` and the environment to start it with here: ``ssh`` to this thread's VM, where it runs as
        its locked user, the install's programs (``inst``) first on its ``PATH``. ``owned``, its home and
        workspace as its setup left them, are sent first and become its user's."""
        vm = getattr(self._held, "vm", None)
        if vm is None:
            raise Unavailable("the unit has no Boat VM")
        vm.send([], owned)
        port = urllib.parse.urlsplit(relay_url).port if relay_url else None
        env = remote_env(env, install.bin_dirs(inst) if inst else [], local)
        return vm.argv(list(cmd), env, workdir, port), {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}

    def close(self) -> None:
        sweep(self.label, self.client, self.say)
        if self._keys is not None:
            self._keys.close()
