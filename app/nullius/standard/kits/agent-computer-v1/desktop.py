"""The computer kit's desktop: one container of the kit's image, a small X
desktop the agent sees and acts on, and the five operations a benchmark's
side gives the port's server.

**The image** is an environment of the kit, as a benchmark's environments
are (``container.env_image``): ``desktop.Dockerfile`` on Nullius's base
image, its build context the three files :data:`KIT` lists, tagged
``nullius/env-agent-computer-v1-desktop:<hash>`` by the sha256 of that text
and those files, and built the first time it is asked for (:func:`image`).

**A desktop** (:meth:`Desktop.start`) is that image run with the engine's
init, no network and no new privileges; its command (``session.py``) starts
the X display, the session bus and the window manager, and the desktop is
ready once the window manager holds the display. Everything after is
``docker exec``:

- :meth:`Desktop.python` runs code as ``user`` in the desktop's Python and
  answers ``{status, output, error, returncode}``, or None when it did not
  end within its bound (it is killed then);
- :meth:`Desktop.upload` and :meth:`Desktop.read` write and read a file as
  ``user``; :meth:`Desktop.capture` is the screen as PNG bytes;
  :meth:`Desktop.password` is ``""``: ``user`` has no password and no sudo;
- :meth:`Desktop.act` runs one action, Python text with ``pyautogui`` and
  ``time`` imported, as ``user``, and keeps its start and end, read on the
  desktop's clock by the root process that runs it: the agent's code never
  writes them.

The kit's own reads go apart: :meth:`Desktop.start_fixture` and
:meth:`Desktop.events` run as ``fixture``, whose folder ``user`` cannot read.
Standard library only, and the engine's program.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from typing import Dict, List, Optional

from nullius.standard import container

HERE = os.path.dirname(os.path.abspath(__file__))
ENVIRONMENT = "desktop"
KIT = {"key": "agent-computer-v1", "_here": HERE,
       "environments": {ENVIRONMENT: {"dockerfile": "desktop.Dockerfile",
                                      "files": ["desktop-requirements.txt", "fixture.py", "session.py"]}}}
SCREEN = (1920, 1080)
USER = "1000:1000"
FIXTURE = "1001:1001"
ROOT = "0:0"
HOME = "/home/user"
FIXTURE_LOG = "/var/lib/fixture/events.jsonl"
PYTHON_SECONDS = 90
START_SECONDS = 60
OUTPUT_LIMIT = 4 * 1024 * 1024          # of each of output and error, as the reply gives them
ACTION_PREFIX = "import pyautogui\nimport time\npyautogui.FAILSAFE = False\n"

# Run as root in the desktop: the request on stdin, the code as `user`, the times around it.
_RUN = r"""
import json, os, signal, subprocess, sys, tempfile, time
req = json.loads(sys.stdin.buffer.read())
env = {"HOME": "/home/user", "USER": "user", "LOGNAME": "user", "SHELL": "/bin/bash", "LANG": "C.UTF-8",
       "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "DISPLAY": ":0",
       "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
start = time.time()
p = subprocess.Popen(["python3", "-c", req["code"]], user=1000, group=1000, extra_groups=[], env=env,
                     cwd="/home/user", stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
try:
    rc, late = p.wait(timeout=req["seconds"]), False
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL)
    rc, late = p.wait(), True
end = time.time()
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
import os, sys
try:
    f = open(os.path.join(os.path.expanduser("~"), sys.argv[1]), "rb")
except OSError:
    sys.exit(3)
with f:
    sys.stdout.buffer.write(f.read())
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


def image(log_dir: Optional[str] = None) -> str:
    """The desktop's image, built the first time it is asked for, on the base image."""
    return container.env_image(KIT, ENVIRONMENT, log_dir)


def tag() -> str:
    """The desktop image's tag on this machine, built or not."""
    return container.env_tag(KIT, ENVIRONMENT, container.base_tag())


def _engine(args: List[str], *, input: Optional[bytes] = None, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([container.DOCKER, *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=container.client_env())
    except FileNotFoundError:
        raise DesktopError(f"{container.DOCKER} is not installed on this machine")


def _said(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()[-300:]


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
        r = _engine(["run", "-d", "--init", "--name", d.name, "--network", "none", "--security-opt", "no-new-privileges",
                     "--shm-size", "256m", *label, *container.capped(), image], timeout=120)
        if r.returncode != 0:
            raise DesktopError(f"starting the desktop {d.name}: {_said(r)}")
        try:
            r = _engine(["exec", d.name, "sh", "-c", _READY.format(n=int(seconds * 10))], timeout=seconds + 30)
        except subprocess.TimeoutExpired:
            r = None
        if r is None or r.returncode != 0:
            logs = _engine(["logs", "--tail", "20", d.name], timeout=30)
            d.remove()
            raise DesktopError(f"the desktop {d.name} was not ready in {seconds:.0f} s: {_said(logs)}")
        return d

    def _exec(self, user: str, argv: List[str], *, input: Optional[bytes] = None, timeout: float,
              env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        flags = ["-i"] if input is not None else []
        for k, v in (env or {}).items():
            flags += ["-e", f"{k}={v}"]
        return _engine(["exec", *flags, "-u", user, self.name, *argv], input=input, timeout=timeout)

    def _run(self, code: str, seconds: float) -> Optional[dict]:
        """``code`` run as ``user`` by the desktop's root: the times and the reply, or None past ``seconds``."""
        req = json.dumps({"code": code, "seconds": seconds, "limit": OUTPUT_LIMIT}).encode()
        try:
            r = self._exec(ROOT, ["python3", "-I", "-c", _RUN], input=req, timeout=seconds + 30)
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
        ``{start, end, reply}``, the times on the desktop's clock, ``reply`` as
        :meth:`python` gives it. The times are kept in :attr:`actions`, also
        for an action that ran past its bound."""
        said = self._run(ACTION_PREFIX + code, seconds)
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
        """The bytes of ``path`` as ``user`` reads them, or None when it cannot."""
        r = self._exec(USER, ["python3", "-I", "-c", _READ, path], timeout=120, env={"HOME": HOME})
        if r.returncode == 3:
            return None
        if r.returncode != 0:
            raise DesktopError(f"read of {path}: {_said(r)}")
        return r.stdout

    def capture(self) -> bytes:
        """The screen, as PNG bytes."""
        r = self._exec(ROOT, ["python3", "-I", "-c", _CAPTURE], timeout=60)
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DesktopError(f"the capture: {_said(r)}")
        return r.stdout

    def password(self) -> str:
        return ""

    def start_fixture(self, values: dict, seconds: float = 60) -> None:
        """Start the fixture as ``fixture``, ``values`` on its stdin; returns once its window is up."""
        r = self._exec(FIXTURE, ["python3", "-I", "/opt/desktop/fixture.py"], input=json.dumps(values).encode(),
                       timeout=seconds, env={"DISPLAY": ":0", "HOME": "/var/lib/fixture"})
        if r.returncode != 0:
            raise DesktopError(f"the fixture did not start: {_said(r)}")

    def events(self) -> List[dict]:
        """The fixture's log, an event a dict; lines that are not JSON are left out."""
        r = self._exec(FIXTURE, ["cat", FIXTURE_LOG], timeout=60)
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def remove(self) -> None:
        """Remove the desktop's container, running or not."""
        _engine(["rm", "-f", self.name], timeout=120)
