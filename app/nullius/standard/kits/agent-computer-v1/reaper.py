"""The computer kit's reaper: what a driver made, removed once the driver is gone, however it went.

    reaper.py ENGINE [START_SECONDS]

The kit's driver starts it at its first unit, in a session of its own (a run that kills the driver's process
group does not kill it), and keeps its stdin open. Each line names what the driver is making, made or let go:

- ``+container NAME`` before the container's start, ``=container NAME`` once that start has succeeded,
  ``-container NAME`` once it is removed;
- ``+folder PATH`` before the folder is made, ``-folder PATH`` once it is removed.

Once its stdin ends (the driver ended, closed it, or was killed), it removes each container still named, with
ENGINE (``docker`` or ``podman``), and each folder still named, then exits. A container whose start had not succeeded
may still be created by the engine after the driver's death: the reaper looks for it again until it has removed
it or START_SECONDS (60 by default) have passed. Standard library only.
"""
import shutil
import subprocess
import sys
import time

REMOVE_SECONDS = 120
START_SECONDS = 60.0


def held(lines):
    """What the lines leave named: {"container": [...], "folder": [...], "started": [...]}, in the order made;
    ``started`` holds the containers whose start had succeeded."""
    out = {"container": [], "folder": [], "started": []}
    for line in lines:
        sign, _, what = line.rstrip("\n").partition(" ")
        kind = sign[1:]
        if sign[:1] not in "+=-" or kind not in ("container", "folder") or not what:
            continue
        if sign[0] == "+" and what not in out[kind]:
            out[kind].append(what)
        elif sign[0] == "=" and kind == "container" and what not in out["started"]:
            out["started"].append(what)
        elif sign[0] == "-" and what in out[kind]:
            out[kind].remove(what)
    out["started"] = [n for n in out["started"] if n in out["container"]]
    return out


def _engine(engine, *args):
    try:
        return subprocess.run([engine, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=REMOVE_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None


def there(engine, name):
    """Whether the engine holds a container of that name; None when it could not say."""
    r = _engine(engine, "container", "inspect", "--format", "{{.Id}}", name)
    if r is None:
        return None
    if r.returncode == 0:
        return True
    return False if b"no such" in (r.stderr or b"").lower() else None


def remove(engine, name, pending, seconds):
    """The container removed. One whose start had not succeeded is looked for until it is gone after being there,
    or ``seconds`` have passed."""
    end = time.monotonic() + seconds
    seen = False
    while True:
        state = there(engine, name)
        if state:
            seen = True
            _engine(engine, "rm", "-f", name)
            if time.monotonic() >= end:
                return
            continue
        if state is False and (seen or not pending):
            return
        if time.monotonic() >= end:
            if state is None:                        # the engine could not say: one last removal asked
                _engine(engine, "rm", "-f", name)
            return
        time.sleep(1)


def main(argv):
    engine = argv[1]
    seconds = float(argv[2]) if len(argv) > 2 else START_SECONDS
    left = held(sys.stdin)
    for name in left["container"]:
        remove(engine, name, name not in left["started"], seconds)
    for path in left["folder"]:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
