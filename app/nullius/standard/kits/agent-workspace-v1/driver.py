"""Kit for agent.workspace/v1: the agent reads and writes in its working directory."""
import os
import stat
import subprocess
import sys

from nullius.standard.backends.container import check_argv
from nullius.standard.protocols.jsonl import serve

CALC = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, add(2, 3)\nprint('OK')\n"


def units(dataset):
    return {"units": ["write", "fix"]}


def setup(unit, dir):
    if "--fixture-image" in sys.argv:
        image = sys.argv[sys.argv.index("--fixture-image") + 1]
        normal = setup_plain(unit, dir)
        files = {"/workspace/calc.py": CALC, "/workspace/test_calc.py": TEST} if unit == "fix" else {}
        # The same kit input and original test; only the execution location differs.
        return {**normal, "image": {"pull": image}, "workdir": "/workspace",
                "prepare": {"write": files, "run": ["sh", "-c",
                    "git init -q && git -c user.name=kit -c user.email=kit.invalid commit -qm initial --allow-empty && "
                    "git add . && git -c user.name=kit -c user.email=kit.invalid commit -qm files --allow-empty"]},
                "verify": {"write": {"/workspace/test_calc.py": TEST} if unit == "fix" else {},
                           "run": ["python3", "-c", "from pathlib import Path; p = Path('result.txt'); assert not p.is_symlink() and p.is_file() and p.read_text().strip() == 'nullius-ok'"]
                                  if unit == "write" else ["python3", "test_calc.py"]}}
    return setup_plain(unit, dir)


def setup_plain(unit, dir):
    if unit == "write":
        return {"prompt": "In the current working directory, create a file named result.txt whose whole content is: nullius-ok"}
    with open(os.path.join(dir, "calc.py"), "w") as f:
        f.write(CALC)
    with open(os.path.join(dir, "test_calc.py"), "w") as f:
        f.write(TEST)
    return {"prompt": "In the current working directory, calc.py has a bug: `python3 test_calc.py` fails. Fix calc.py so the test prints OK. Do not change test_calc.py."}


def score(unit, dir, transcript, answer, verified=None):
    if "--fixture-image" in sys.argv:
        ok = bool(verified) and verified.get("exit") == 0 and (unit == "write" or "OK" in verified.get("output", ""))
        return {"reward": float(ok), "passed": ok, "detail": verified or {}}
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
    if "--fixture-image" in sys.argv:
        files = {"/workspace/result.txt": "nullius-ok\n"} if unit == "write" else {"/workspace/calc.py": CALC.replace("a - b", "a + b")}
        return {"answer": "", "solve": {"write": files, "run": ["true"]}}
    if unit == "write":
        with open(os.path.join(dir, "result.txt"), "w") as f:
            f.write("nullius-ok\n")
    else:
        with open(os.path.join(dir, "calc.py"), "w") as f:
            f.write(CALC.replace("a - b", "a + b"))
    return {"answer": ""}


serve({"units": units, "setup": setup, "score": score, "oracle": oracle})
