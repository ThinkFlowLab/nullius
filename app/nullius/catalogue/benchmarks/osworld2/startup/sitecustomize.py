"""OSWorld2's VM container create and guest disk, in every Python process the driver starts.

The driver puts this folder first on PYTHONPATH. For the provider's image only,
it wraps docker-py's ContainerCollection.create: CREATE_SECONDS a call, a
create that timed out on reading looked for by its label instead of sent
again, the container on NETWORK (on the default bridge a Docker in the VM
takes the host's address), the write limits on the disk under Docker's root
and its ports, the guest's PORTS too, on 127.0.0.1. A docker DesktopEnv gets a disk of at
least GUEST_GB, as the upstream's aws provider gives. With another vm_provider
it wraps nothing. Once OSWORLD2_STOP's file exists, an agent's step ends with DONE.
A log record never shows the password of PROXY_CONFIG_FILE's proxy."""
import contextlib
import functools
import inspect
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid

PROVIDER = "happysixd/osworld-docker"
CREATE_SECONDS = 120
ADOPT_SECONDS = 60
LOOK_SECONDS = 10
POLL_SECONDS = 2
SWEEP_SECONDS = 1800
LABEL = "nullius.osworld2.create"
NETWORK = "nullius-osworld2"
SUBNET = "10.213.0.0/24"
GATEWAY = "10.213.0.1"
NETWORK_LABEL = "nullius.osworld2.network"
PORTS = "OSWORLD2_GUEST_PORTS"
LOOPBACK = "127.0.0.1"
DOCKER_PY_SECONDS = 60
PORT_LISTS = 4
LONGEST_START = (PORT_LISTS * DOCKER_PY_SECONDS + 5 * CREATE_SECONDS
                 + ADOPT_SECONDS + POLL_SECONDS + 2 * LOOK_SECONDS)
LIMITS = (("device_write_bps", "NULLIUS_OPTION_WRITE_MBPS", 1024 * 1024, "BlkioDeviceWriteBps", "--device-write-bps"),
          ("device_write_iops", "NULLIUS_OPTION_WRITE_IOPS", 1, "BlkioDeviceWriteIOps", "--device-write-iops"))
TAKEN = {"device_write_bps": "OSWORLD2_PROXY_WRITE_BPS", "device_write_iops": "OSWORLD2_PROXY_WRITE_IOPS"}
NAMES = {"device_write_bps": "Disk write MB/s", "device_write_iops": "Disk write IOPS"}
SYS_BLOCK = "/sys/dev/block"
DEV = "/dev"
ACTION = "run where Docker limits writes on the disk under its root, or set Disk write MB/s and Disk write IOPS to none"
_DISK = {}
GUEST_GB = 40
DESKTOP_ENV = "desktop_env.desktop_env"
EPISODE = "lib_run_single"
DOCKER_PROVIDER = "desktop_env.providers.docker.provider"
STOP = "OSWORLD2_STOP"
MARK = "_nullius_osworld2"
HIDDEN = "<proxy password>"


class WriteLimitError(RuntimeError):
    pass


class NetworkError(RuntimeError):
    pass


def network(docker):
    """Made when missing, a concurrent maker no error; one of that name on another subnet or gateway is a NetworkError."""
    shape = "{{index .Labels \"" + NETWORK_LABEL + "\"}}|{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}} {{end}}"
    seen = docker("network", "inspect", "-f", shape, NETWORK)
    if seen.returncode != 0:
        made = docker("network", "create", "--driver", "bridge", "--subnet", SUBNET, "--gateway", GATEWAY,
                      "--label", f"{NETWORK_LABEL}=1", NETWORK)
        seen = docker("network", "inspect", "-f", shape, NETWORK)
        if seen.returncode != 0:
            said = ((made.stderr or "").strip().splitlines() or ["no reason"])[-1][:300]
            raise NetworkError(f"the VMs' network {NETWORK} ({SUBNET}) could not be made: {said}; free that "
                               "subnet on this machine, or make the network with that subnet and gateway "
                               f"{GATEWAY}")
    label, _, found = seen.stdout.strip().partition("|")
    if found.split() != [SUBNET, GATEWAY]:
        have = f"the network {NETWORK} has {found.strip() or 'no subnet'}, not {SUBNET} with gateway {GATEWAY}"
        if label:
            raise NetworkError(f"{have}: remove it (docker network rm {NETWORK}) once no container uses it")
        raise NetworkError(f"{have}, and Nullius did not make it: leave it to its owner, who can give it another "
                           f"name, or run on a machine where the name {NETWORK} and {SUBNET} are free")
    return NETWORK


def wanted(environ):
    """The options' limits as (argument, rate, HostConfig field, CLI flag), less the proxy's share."""
    out = []
    for key, name, unit, field, flag in LIMITS:
        value = environ.get(name, "none")
        if value != "none":
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise WriteLimitError(f"the VM's disk write limit cannot be applied: {NAMES[key]} is {value!r}; {ACTION}")
            taken = environ.get(TAKEN[key], "0")
            if not re.fullmatch(r"0|[1-9][0-9]*", taken):
                raise WriteLimitError(f"the unit's proxy gave no write share it can take: {TAKEN[key]} is {taken!r}")
            if int(value) * unit <= int(taken):
                raise WriteLimitError(f"the VM's disk write limit cannot be applied: {NAMES[key]} is {value}, "
                                      "no more than the unit's proxy takes; give the VM more")
            out.append((key, int(value) * unit - int(taken), field, flag))
    return out


def published(environ):
    """The image forwards every TCP port but its web page's to the guest, so the same number reaches it."""
    return {int(p): (LOOPBACK, int(p)) for p in environ.get(PORTS, "").split()}


def disk(path):
    """The block device under ``path``, a partition's parent disk; None when there is no block device."""
    st = os.stat(path).st_dev
    node = os.path.join(SYS_BLOCK, f"{os.major(st)}:{os.minor(st)}")
    if not os.path.isdir(node):
        return None
    node = os.path.realpath(node)
    if os.path.exists(os.path.join(node, "partition")):
        node = os.path.dirname(node)
    try:
        with open(os.path.join(node, "uevent"), encoding="utf-8") as f:
            name = dict(ln.strip().partition("=")[::2] for ln in f).get("DEVNAME")
    except OSError:
        return None
    return os.path.join(DEV, name) if name and os.path.exists(os.path.join(DEV, name)) else None


def root_disk(root):
    """The disk under Docker's root ``root``, found once in a process; no disk is a WriteLimitError."""
    if root not in _DISK:
        try:
            device = disk(root)
        except OSError as e:
            raise WriteLimitError(f"the VM's disk write limit cannot be applied: Docker's root {root}: "
                                  f"{type(e).__name__}; {ACTION}") from e
        if device is None:
            raise WriteLimitError(f"the VM's disk write limit cannot be applied: Docker's root {root} is on no "
                                  f"block device {SYS_BLOCK} names; {ACTION}")
        _DISK[root] = device
    return _DISK[root]


def _throttle(client, limits, kwargs):
    if not _DISK:
        try:
            root = client.info()["DockerRootDir"]
        except Exception as e:
            raise WriteLimitError(f"the VM's disk write limit cannot be applied: no Docker root from the daemon, "
                                  f"{type(e).__name__}; {ACTION}") from e
        root_disk(root)
    device = next(iter(_DISK.values()))
    return {key: list(kwargs.get(key) or []) + [{"Path": device, "Rate": rate}] for key, rate, _, _ in limits}


def _confirm(container, limits, given):
    """A daemon that cannot limit writes drops them silently, so a container without them is removed before it starts."""
    try:
        container.reload()
        host = container.attrs.get("HostConfig") or {}
        kept = all(given[key][-1] in (host.get(field) or []) for key, _, field, _ in limits)
    except Exception:
        kept = False
    if not kept:
        try:
            container.remove(force=True, v=True)
        except Exception:
            pass
        raise WriteLimitError(f"the VM's disk write limit cannot be applied: Docker created the VM's container "
                              f"without it; {ACTION}")


def _patient(client):
    api = getattr(client, "api", None)
    if api is not None and isinstance(getattr(api, "timeout", None), (int, float)) and api.timeout < CREATE_SECONDS:
        api.timeout = CREATE_SECONDS


def _gave_up(error):
    """Only a read timeout leaves the create undecided; any other error stands."""
    try:
        from requests import exceptions
    except ImportError:
        return False
    return isinstance(error, exceptions.ReadTimeout)


def _labelled(labels, token):
    if isinstance(labels, dict):
        out = dict(labels)
    else:
        out = dict.fromkeys(labels or [], "")
    out[LABEL] = token
    return out


def _made_late(collection, token):
    """Each look waits LOOK_SECONDS at most, so the search ends within ADOPT_SECONDS, a pause and one look."""
    api = getattr(collection.client, "api", None)
    kept = getattr(api, "timeout", None)
    if api is not None and kept is not None:
        api.timeout = LOOK_SECONDS
    t0 = time.monotonic()
    try:
        while True:
            try:
                found = collection.list(all=True, filters={"label": f"{LABEL}={token}"})
            except Exception:
                found = []
            if found:
                return found[0]
            if time.monotonic() - t0 >= ADOPT_SECONDS:
                return None
            time.sleep(POLL_SECONDS)
    finally:
        if api is not None and kept is not None:
            api.timeout = kept


def _sweeper(token):
    """A sweeper for the create's container, called off by _kept, and set going when this process closes its pipe or dies."""
    r, w = os.pipe()
    try:
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), token], stdin=r,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(r)
    return w


def _kept(fd):
    try:
        os.write(fd, b"kept")
    except OSError:
        pass
    finally:
        os.close(fd)


def sweep(token):
    """The container of an abandoned create, should Docker make it within SWEEP_SECONDS, removed with its volume."""
    until = time.monotonic() + SWEEP_SECONDS
    while time.monotonic() < until:
        try:
            found = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"label={LABEL}={token}"],
                                   capture_output=True, text=True, timeout=CREATE_SECONDS).stdout.split()
            if found and subprocess.run(["docker", "rm", "-f", "-v", *found], capture_output=True,
                                        timeout=CREATE_SECONDS).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(LOOK_SECONDS)


def wrap_docker_create(environ=os.environ):
    """Once a process: a create already wrapped, by an earlier run of this module, is left as it is."""
    limits, ports = wanted(environ), published(environ)
    try:
        from docker.models.containers import ContainerCollection
    except ImportError:
        return
    create = ContainerCollection.create
    if getattr(create, MARK, False):
        return

    @functools.wraps(create)
    def wrapped(self, image, *args, **kwargs):
        if not (isinstance(image, str) and image.split("@")[0].split(":")[0] == PROVIDER):
            return create(self, image, *args, **kwargs)
        _patient(self.client)
        given = _throttle(self.client, limits, kwargs) if limits else {}
        token = uuid.uuid4().hex
        swept = _sweeper(token)
        placed = {} if kwargs.get("network") or kwargs.get("network_mode") else {"network": NETWORK}
        if ports or kwargs.get("ports"):
            placed["ports"] = {k: v if isinstance(v, tuple) else (LOOPBACK, v)
                               for k, v in {**(kwargs.get("ports") or {}), **ports}.items()}
        try:
            container = create(self, image, *args, **dict(kwargs, labels=_labelled(kwargs.get("labels"), token),
                                                          **placed, **given))
        except Exception as e:
            if not _gave_up(e):
                _kept(swept)
                raise
            container = _made_late(self, token)
            if container is None:
                os.close(swept)
                raise
            logging.getLogger("nullius.osworld2").warning(
                "Docker made the VM's container after its client gave up (%s); that one is started, none is "
                "created again", type(e).__name__)
        _kept(swept)
        if limits:
            _confirm(container, limits, given)
        container.remove = functools.partial(container.remove, v=True)
        return container

    setattr(wrapped, MARK, True)
    ContainerCollection.create = wrapped


def guest_gb(provider_name, volume_size):
    """The task's own volume_size, or GUEST_GB when that is less; another provider's is left alone."""
    if str(provider_name).lower().strip() != "docker":
        return volume_size
    return max(int(volume_size or 0), GUEST_GB)


def floor_desktop_env(module):
    """DesktopEnv's __init__ given guest_gb's volume_size, however its caller passes it."""
    cls = getattr(module, "DesktopEnv", None)
    if cls is None or getattr(cls.__init__, MARK, False):
        return
    init = cls.__init__
    signature = inspect.signature(init)
    if not {"provider_name", "volume_size"} <= set(signature.parameters):
        return
    default = signature.parameters["provider_name"].default

    @functools.wraps(init)
    def wrapped(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        given = bound.arguments
        given["volume_size"] = guest_gb(given.get("provider_name", default), given.get("volume_size"))
        return init(*bound.args, **bound.kwargs)

    setattr(wrapped, MARK, True)
    cls.__init__ = wrapped


def queue_starts(module):
    """Every VM start waits its turn for the provider's port lock, a restart as the first start."""
    module.LOCK_TIMEOUT = -1


def _took(path):
    """The stop's reasons as it ended a step, in ``path``.taken: one written once the episode ended is not its own."""
    with contextlib.suppress(OSError), open(path, encoding="utf-8") as f, open(path + ".taken", "x") as out:
        out.write(f.read())


def _stopping(predict, path):
    @functools.wraps(predict)
    def wrapped(*args, **kwargs):
        if os.path.exists(path):
            _took(path)
            return "", ["DONE"]
        try:
            out = predict(*args, **kwargs)
        except Exception:
            if os.path.exists(path):
                _took(path)
                return "", ["DONE"]
            raise
        if not os.path.exists(path):
            return out
        _took(path)
        return out[0], list(out[1]) + ["DONE"]

    setattr(wrapped, MARK, True)
    return wrapped


def stop_when_asked(module):
    run, path = getattr(module, "run_single_example", None), os.environ.get(STOP)
    if run is None or not path or getattr(run, MARK, False):
        return

    @functools.wraps(run)
    def wrapped(agent, *args, **kwargs):
        if not getattr(agent.predict, MARK, False):
            agent.predict = _stopping(agent.predict, path)
        return run(agent, *args, **kwargs)

    setattr(wrapped, MARK, True)
    module.run_single_example = wrapped


class _Loader:
    """The finder's loader, unchanged, with the module's PATCHES after its exec_module; the rest is the loader's."""

    def __init__(self, loader):
        self.loader = loader

    def __getattr__(self, name):
        return getattr(self.loader, name)

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        PATCHES[module.__name__](module)


class _Floor:
    """Gives a PATCHES module's spec a _Loader on each import and reload; known by MARK, so a second run adds none."""

    def find_spec(self, name, path, target=None):
        if name not in PATCHES:
            return None
        for finder in sys.meta_path:
            if getattr(finder, MARK, False) or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _Loader(spec.loader)
        return spec


setattr(_Floor, MARK, True)
PATCHES = {DESKTOP_ENV: floor_desktop_env, EPISODE: stop_when_asked, DOCKER_PROVIDER: queue_starts}


def wrap_desktop_env():
    """A module of PATCHES already loaded is wrapped, and the finder goes on for its later loads."""
    for name in PATCHES:
        if name in sys.modules:
            PATCHES[name](sys.modules[name])
    if not any(getattr(f, MARK, False) for f in sys.meta_path):
        sys.meta_path.insert(0, _Floor())


def hide_proxy_password(environ=os.environ):
    """Once a process, as wrap_docker_create."""
    try:
        with open(environ["PROXY_CONFIG_FILE"], encoding="utf-8") as f:
            words = {p["password"] for p in json.load(f) if isinstance(p.get("password"), str)} - {""}
    except Exception:
        return
    plain = logging.Formatter.format
    if not words or getattr(plain, MARK, False):
        return

    @functools.wraps(plain)
    def hidden(self, record):
        text = plain(self, record)
        for word in words:
            text = text.replace(word, HIDDEN)
        return text

    setattr(hidden, MARK, True)
    logging.Formatter.format = hidden
    logging.raiseExceptions = False


if __name__ == "sitecustomize":
    hide_proxy_password()
if __name__ == "sitecustomize" and os.environ.get("NULLIUS_OPTION_VM_PROVIDER", "docker") == "docker":
    wrap_docker_create()
    wrap_desktop_env()
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if not sys.stdin.buffer.read():
        sweep(sys.argv[1])
