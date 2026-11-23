"""The computer kit's desktop: one container of the kit's image, a small X
desktop the agent sees and acts on, and the five operations a benchmark's
side gives the port's server.

**The image** is an environment of the kit, as a benchmark's environments
are (``container.env_image``): ``desktop.Dockerfile`` on its own base,
Debian slim pinned by digest (:data:`BASE`), its build context the three files :data:`KIT` lists, tagged
``nullius/env-agent-computer-v1-desktop:<hash>`` by the sha256 of that text
and those files, and built the first time it is asked for (:func:`image`).

**A desktop** (:meth:`Desktop.start`) is that image run with the engine's
init, no network and no new privileges; its command (``session.py``) starts
the X display, the session bus and the window manager, and the desktop is
ready once the window manager holds the display. Everything after is
``docker exec``:

- :meth:`Desktop.password` is ``""``: ``user`` has no password and no sudo;
- :meth:`Desktop.act` runs one action, Python text with ``pyautogui`` and
  ``time`` imported, as ``user``, and keeps its start and end, read on the
  desktop's monotonic clock (the fixture's) by the root process that runs it:
  the agent's code never writes them. It answers only :data:`SETTLE` seconds
  after the action's end, by that clock, so no later request falls in the
  time a verdict grants the action.

The kit's own reads go apart: :meth:`Desktop.start_fixture` and
:meth:`Desktop.events` run as ``fixture``, whose folder ``user`` cannot read.
A start that fails removes what it made; :meth:`Desktop.remove` checks that
the container is gone. Standard library only, and the engine's program.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
from typing import Dict, List, Optional

from nullius.standard import container

HERE = os.path.dirname(os.path.abspath(__file__))
ENVIRONMENT = "desktop"
BASE = "docker.io/library/debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
KIT = {"key": "agent-computer-v1", "_here": HERE,
       "environments": {ENVIRONMENT: {"base": BASE, "dockerfile": "desktop.Dockerfile",
                                      "files": ["desktop-requirements.txt", "fixture.py", "session.py"]}}}
SCREEN = (1920, 1080)
USER = "1000:1000"
FIXTURE = "1001:1001"
ROOT = "0:0"
HOME = "/home/user"
FIXTURE_LOG = "/var/lib/fixture/events.jsonl"
PYTHON_SECONDS = 90
START_SECONDS = 60
RUN_SECONDS = 120
ENGINE_GRACE = 30
FIXTURE_SECONDS = 60
CAPTURE_SECONDS = 60
EVENTS_SECONDS = 60
OUTPUT_LIMIT = 4 * 1024 * 1024          # of each of output and error, as the reply gives them
READ_LIMIT = 12 * 1024 * 1024           # the largest file `read` gives
SETTLE = 0.5                            # an action answers this long after its end: at least the verdicts' slack
ACTION_PREFIX = "import pyautogui\nimport time\npyautogui.FAILSAFE = False\n"

# Run as root in the desktop: the request on stdin, the code as `user`, the times around it.
_RUN = r"""
import json, os, signal, subprocess, sys, tempfile, time
req = json.loads(sys.stdin.buffer.read())
env = {"HOME": "/home/user", "USER": "user", "LOGNAME": "user", "SHELL": "/bin/bash", "LANG": "C.UTF-8",
       "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "DISPLAY": ":0",
       "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
start = time.monotonic()
p = subprocess.Popen(["python3", "-c", req["code"]], user=1000, group=1000, extra_groups=[], env=env,
                     cwd="/home/user", stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
try:
    rc, late = p.wait(timeout=req["seconds"]), False
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL)
    rc, late = p.wait(), True
end = time.monotonic()
rest = end + req.get("settle", 0) - time.monotonic()
if rest > 0:
    time.sleep(rest)
def text(f):
    f.seek(0)
    return f.read(req["limit"]).decode("utf-8", "replace")
print(json.dumps({"start": start, "end": end, "timeout": late, "returncode": rc, "output": text(out), "error": text(err)}))
"""

_UPLOAD = r"""
import os, sys
path = os.path.join(os.path.expanduser("~"), sys.argv[1])
if os.path.dirname(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "ab" if sys.argv[2] == "append" else "wb") as f:
    f.write(sys.stdin.buffer.read())
print(os.path.getsize(path))
"""

_READ = r"""
import os, stat, sys
limit = int(sys.argv[2])
try:
    fd = os.open(os.path.join(os.path.expanduser("~"), sys.argv[1]), os.O_RDONLY | os.O_NONBLOCK)
except OSError:
    sys.exit(3)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    sys.exit(4)
with os.fdopen(fd, "rb") as f:
    data = f.read(limit + 1)
if len(data) > limit:
    sys.exit(5)
sys.stdout.buffer.write(data)
"""

_CAPTURE = r"""
import io, sys
from PIL import ImageGrab
b = io.BytesIO()
ImageGrab.grab(xdisplay=":0").save(b, "PNG")
sys.stdout.buffer.write(b.getvalue())
"""

_READY = "i=0; while [ ! -e /run/desktop/ready ]; do i=$((i+1)); [ $i -gt {n} ] && exit 1; sleep 0.1; done"


class DesktopError(Exception):
    """The desktop failed a request: it is gone, or answered nothing a request answers."""


class Refused(Exception):
    """A request the desktop does not serve: a read of a file that is not a regular one, or of one past
    :data:`READ_LIMIT`. The agent's error, never the desktop's."""


def image(log_dir: Optional[str] = None) -> str:
    """The desktop's image, built the first time it is asked for, on the base image."""
    return container.env_image(KIT, ENVIRONMENT, log_dir)


def tag() -> str:
    """The desktop image's tag on this machine, built or not."""
    return container.env_tag(KIT, ENVIRONMENT, container.base_tag())


_CLIENTS: set = set()                   # the engine's clients running now, for end_clients
_CLIENTS_LOCK = threading.Lock()
_CLIENTS_CLOSED = False


def _engine(args: List[str], *, input: Optional[bytes] = None, timeout: float) -> subprocess.CompletedProcess:
    """One call of the engine's program, its stdin the request's bytes or nothing, killed past ``timeout``
    (``subprocess.TimeoutExpired``, as ``subprocess.run`` raises it)."""
    with _CLIENTS_LOCK:
        if _CLIENTS_CLOSED:
            raise DesktopError("the desktop engine clients have been closed")
        try:
            p = subprocess.Popen([container.DOCKER, *args], stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=container.client_env())
        except FileNotFoundError:
            raise DesktopError(f"{container.DOCKER} is not installed on this machine")
        _CLIENTS.add(p)
    try:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except BaseException:
            p.kill()
            p.communicate()
            raise
    finally:
        with _CLIENTS_LOCK:
            _CLIENTS.discard(p)
    return subprocess.CompletedProcess([container.DOCKER, *args], p.returncode, out, err)


def end_clients(*, closing: bool = True) -> int:
    """Every client of the engine still running, from any thread, killed and waited for; how many there were.
    A shutdown also prevents further launches; between units, only the current clients are ended."""
    global _CLIENTS_CLOSED
    with _CLIENTS_LOCK:
        _CLIENTS_CLOSED = _CLIENTS_CLOSED or closing
        running = list(_CLIENTS)
    for p in running:
        try:
            p.kill()
            p.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    return len(running)


def _said(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()[-300:]


def _absent(r: subprocess.CompletedProcess) -> bool:
    """Whether the engine said the container does not exist."""
    return "no such container" in (r.stderr or b"").decode("utf-8", "replace").lower()


class Desktop:
    """One running desktop, by its container's name."""

    def __init__(self, name: str):
        self.name = name
        self.actions: List[Dict[str, float]] = []

    @classmethod
    def start(cls, image: str, name: Optional[str] = None, *, job: Optional[str] = None,
              seconds: float = START_SECONDS) -> "Desktop":
        """Start a desktop of ``image`` and wait until it is ready. ``job``
        labels the container as a unit's are, so a sweep of the job removes it."""
        d = cls(name or f"nullius-desktop-{secrets.token_hex(4)}")
        label = ["--label", f"{container.LABEL}={job}"] if job else []
        try:
            try:
                r = _engine(["run", "-d", "--init", "--name", d.name, "--network", "none", "--security-opt",
                             "no-new-privileges", "--shm-size", "256m", *label, *container.capped(), image], timeout=RUN_SECONDS)
            except subprocess.TimeoutExpired:
                raise DesktopError(f"starting the desktop {d.name}: {container.DOCKER} run did not answer in {RUN_SECONDS:g} s")
            if r.returncode != 0:
                raise DesktopError(f"starting the desktop {d.name}: {_said(r)}")
            try:
                r = _engine(["exec", d.name, "sh", "-c", _READY.format(n=int(seconds * 10))], timeout=seconds + ENGINE_GRACE)
            except subprocess.TimeoutExpired:
                r = None
            if r is None or r.returncode != 0:
                try:
                    said = _said(_engine(["logs", "--tail", "20", d.name], timeout=30))
                except (subprocess.TimeoutExpired, DesktopError):
                    said = "its log could not be read"
                raise DesktopError(f"the desktop {d.name} was not ready in {seconds:.0f} s: {said}")
        except BaseException:                  # an error or an interrupt: what this start made goes
            try:
                d.remove()
            except (DesktopError, subprocess.TimeoutExpired):
                pass
            raise
        return d

    def _exec(self, user: str, argv: List[str], *, input: Optional[bytes] = None, timeout: float,
              env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        flags = ["-i"] if input is not None else []
        for k, v in (env or {}).items():
            flags += ["-e", f"{k}={v}"]
        return _engine(["exec", *flags, "-u", user, self.name, *argv], input=input, timeout=timeout)

    def _run(self, code: str, seconds: float, settle: float = 0.0) -> Optional[dict]:
        """``code`` run as ``user`` by the desktop's root: the times and the reply, or None past ``seconds``.
        The reply comes ``settle`` seconds after the code's end, on the desktop's clock."""
        req = json.dumps({"code": code, "seconds": seconds, "limit": OUTPUT_LIMIT, "settle": settle}).encode()
        try:
            r = self._exec(ROOT, ["python3", "-I", "-c", _RUN], input=req, timeout=seconds + settle + ENGINE_GRACE)
        except subprocess.TimeoutExpired:
            return None
        try:
            said = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise DesktopError(f"the desktop {self.name} did not run the code: {_said(r)}")
        return said

    def python(self, code: str, seconds: float = PYTHON_SECONDS) -> Optional[dict]:
        """Run ``code`` as ``user`` in the desktop's Python: ``{status, output,
        error, returncode}``, or None when it did not end within ``seconds``."""
        said = self._run(code, seconds)
        if said is None or said["timeout"]:
            return None
        return {"status": "success", "output": said["output"], "error": said["error"], "returncode": said["returncode"]}

    def act(self, code: str, seconds: float = PYTHON_SECONDS) -> dict:
        """Run one action, ``code`` with ``pyautogui`` and ``time`` imported:
        ``{start, end, reply}``, the times on the desktop's monotonic clock,
        ``reply`` as :meth:`python` gives it. It answers :data:`SETTLE` seconds
        after ``end``. The times are kept in :attr:`actions`, also for an
        action that ran past its bound."""
        said = self._run(ACTION_PREFIX + code, seconds, SETTLE)
        if said is None:                       # the desktop did not answer: it gave no time to keep
            return {"start": None, "end": None, "reply": None}
        self.actions.append({"start": said["start"], "end": said["end"]})
        reply = None if said["timeout"] else {"status": "success", "output": said["output"], "error": said["error"],
                                              "returncode": said["returncode"]}
        return {"start": said["start"], "end": said["end"], "reply": reply}

    def upload(self, path: str, data: bytes, *, append: bool = False) -> int:
        """Write ``data`` to ``path`` as ``user`` (a relative path is under
        its home), its folders made; the file's size after."""
        r = self._exec(USER, ["python3", "-I", "-c", _UPLOAD, path, "append" if append else "write"],
                       input=data, timeout=600, env={"HOME": HOME})
        if r.returncode != 0:
            raise DesktopError(f"upload to {path}: {_said(r)}")
        return int(r.stdout.decode().strip())

    def read(self, path: str) -> Optional[bytes]:
        """The bytes of ``path`` as ``user`` reads them, or None when it cannot open it. A path that is
        not a regular file, or a file past :data:`READ_LIMIT`, is :class:`Refused`."""
        r = self._exec(USER, ["python3", "-I", "-c", _READ, path, str(READ_LIMIT)], timeout=120, env={"HOME": HOME})
        if r.returncode == 3:
            return None
        if r.returncode == 4:
            raise Refused(f"{path} is not a regular file")
        if r.returncode == 5:
            raise Refused(f"{path} is over {READ_LIMIT // (1024 * 1024)} MiB")
        if r.returncode != 0:
            raise DesktopError(f"read of {path}: {_said(r)}")
        return r.stdout

    def capture(self) -> bytes:
        """The screen, as PNG bytes."""
        r = self._exec(ROOT, ["python3", "-I", "-c", _CAPTURE], timeout=CAPTURE_SECONDS)
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DesktopError(f"the capture: {_said(r)}")
        return r.stdout

    def password(self) -> str:
        return ""

    def start_fixture(self, values: dict, seconds: float = FIXTURE_SECONDS) -> None:
        """Start the fixture as ``fixture``, ``values`` on its stdin; returns once its window is up."""
        r = self._exec(FIXTURE, ["python3", "-I", "/opt/desktop/fixture.py"], input=json.dumps(values).encode(),
                       timeout=seconds, env={"DISPLAY": ":0", "HOME": "/var/lib/fixture"})
        if r.returncode != 0:
            raise DesktopError(f"the fixture did not start: {_said(r)}")

    def events(self) -> List[dict]:
        """The fixture's log, an event a dict. A log that cannot be read, or a line that is not an
        event, is a :class:`DesktopError`: a verdict is never given on part of the log."""
        r = self._exec(FIXTURE, ["cat", FIXTURE_LOG], timeout=EVENTS_SECONDS)
        if r.returncode != 0:
            raise DesktopError(f"the fixture's log could not be read: {_said(r)}")
        out = []
        for i, line in enumerate(r.stdout.decode("utf-8", "replace").splitlines(), 1):
            try:
                e = json.loads(line)
            except ValueError:
                e = None
            if not isinstance(e, dict) or not isinstance(e.get("kind"), str) or not isinstance(e.get("t"), (int, float)):
                raise DesktopError(f"line {i} of the fixture's log is not an event")
            out.append(e)
        return out

    def remove(self) -> None:
        """Remove the desktop's container, running or not, and check that it is gone. A container that
        was gone already is no error; one that stays is a :class:`DesktopError`."""
        try:
            r = _engine(["rm", "-f", self.name], timeout=120)
        except subprocess.TimeoutExpired:
            raise DesktopError(f"removing the desktop {self.name}: {container.DOCKER} rm did not answer in 120 s")
        if r.returncode != 0 and not _absent(r):
            raise DesktopError(f"removing the desktop {self.name}: {_said(r)}")
        try:
            r = _engine(["container", "inspect", "--format", "{{.Id}}", self.name], timeout=60)
        except subprocess.TimeoutExpired:
            raise DesktopError(f"the desktop {self.name}'s removal could not be checked")
        if r.returncode == 0 or not _absent(r):
            raise DesktopError(f"the desktop {self.name} is still there after its removal: {_said(r)}")
