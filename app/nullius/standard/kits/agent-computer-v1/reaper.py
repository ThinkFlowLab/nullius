"""The computer kit's reaper: what a driver made, removed once the driver is gone, however it went.

    reaper.py ENGINE

The kit's driver starts it at its first unit, in a session of its own (a run that kills the driver's process
group does not kill it), and keeps its stdin open. Each line names what the driver made or let go:
``+container NAME``, ``-container NAME``, ``+folder PATH``, ``-folder PATH``. Once its stdin ends (the driver
ended, closed it, or was killed), it removes each container still named, with ENGINE (``docker`` or
``podman``), and each folder still named, then exits. Standard library only.
"""
import shutil
import subprocess
import sys

REMOVE_SECONDS = 120


def held(lines):
    """What the lines leave named: {"container": [...], "folder": [...]}, in the order made."""
    out = {"container": [], "folder": []}
    for line in lines:
        sign, _, rest = line.rstrip("\n").partition(" ")
        kind, what = sign[1:], rest
        if sign[:1] not in "+-" or kind not in out or not what:
            continue
        if sign[0] == "+" and what not in out[kind]:
            out[kind].append(what)
        elif sign[0] == "-" and what in out[kind]:
            out[kind].remove(what)
    return out


def main(argv):
    engine = argv[1]
    left = held(sys.stdin)
    for name in left["container"]:
        try:
            subprocess.run([engine, "rm", "-f", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=REMOVE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
    for path in left["folder"]:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
