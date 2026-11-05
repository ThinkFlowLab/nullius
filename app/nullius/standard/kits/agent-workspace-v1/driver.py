"""Kit for agent.workspace/v1: the agent reads and writes in its working directory."""
import os
import stat
import subprocess
import sys

from nullius.standard.container import check_argv
from nullius.standard.jsonl import serve

CALC = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, add(2, 3)\nprint('OK')\n"


def units(dataset):
    return {"units": ["write", "fix"]}


def setup(unit, dir):
    if unit == "write":
        return {"prompt": "In the current working directory, create a file named result.txt whose whole content is: nullius-ok"}
    with open(os.path.join(dir, "calc.py"), "w") as f:
        f.write(CALC)
    with open(os.path.join(dir, "test_calc.py"), "w") as f:
        f.write(TEST)
    return {"prompt": "In the current working directory, calc.py has a bug: `python3 test_calc.py` fails. Fix calc.py so the test prints OK. Do not change test_calc.py."}


def score(unit, dir, transcript, answer):
    if unit == "write":
        got = _read_plain(os.path.join(dir, "result.txt"))
        ok = got == "nullius-ok"
        return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"result.txt": got}}
    test = os.path.join(dir, "test_calc.py")
    if os.path.lexists(test):                                     # the test as given, whatever happened to it - a link included
        os.unlink(test)
    with open(test, "w") as f:
        f.write(TEST)
    argv = check_argv(dir, [sys.executable, "test_calc.py"], ["timeout", "60", "python3", "test_calc.py"])
    r = subprocess.run(argv, cwd=dir, capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and "OK" in r.stdout
    return {"reward": 1.0 if ok else 0.0, "passed": ok, "detail": {"stdout": r.stdout[-500:], "stderr": r.stderr[-500:]}}


def _read_plain(path):
    """A regular file's text, or None: a link the agent planted is not followed
    out of its workspace (on docker it could point at the user's settings)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd) as f:
        return f.read().strip() if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else None


def oracle(unit, dir):
    if unit == "write":
        with open(os.path.join(dir, "result.txt"), "w") as f:
            f.write("nullius-ok\n")
    else:
        with open(os.path.join(dir, "calc.py"), "w") as f:
            f.write(CALC.replace("a - b", "a + b"))
    return {"answer": ""}


serve({"units": units, "setup": setup, "score": score, "oracle": oracle})
