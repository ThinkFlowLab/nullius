"""Run a test suite one module per process, several at a time.

    python3 app/tests/parallel.py                      # the backend suite
    python3 app/tests/parallel.py --docker             # with the docker tier: the tests that run containers
    python3 app/tests/parallel.py app/ui_tests         # the route suite (needs Flask)

The same tests `unittest discover -s <dir> -t app` runs, in about half the
time on four cores: each module is its own `python -m unittest` from `app/`,
so a module that patches a global patches only its own process. Prints the
output of every module that failed and exits 1 if any did. Standard library
only.

The docker tier (``tests/docker_tier.py``) is the few tests that build and run
containers for real, most of the suite's time on a machine that has the base
image; they are skipped unless ``--docker`` is given, and run nightly in CI.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def modules(start: str) -> list:
    """Every test module discover would load under ``start``, by dotted name."""
    def walk(suite):
        for t in suite:
            if isinstance(t, unittest.TestSuite):
                yield from walk(t)
            else:
                yield t
    sys.path.insert(0, APP)
    found = unittest.defaultTestLoader.discover(start, top_level_dir=APP)
    names = set()
    for t in walk(found):
        name = type(t).__module__
        if name == "unittest.loader":                        # a module that failed to import: run it to see why
            name = t.id().split(".", 1)[1]
        names.add(name)
    return sorted(names)


def run(name: str):
    t = time.time()
    p = subprocess.run([sys.executable, "-m", "unittest", name], cwd=APP, capture_output=True, text=True)
    return name, p.returncode, p.stderr, time.time() - t


def _suite(argv) -> int:
    start = os.path.abspath(argv[0] if argv else os.path.join(APP, "tests"))
    names = modules(start)
    t = time.time()
    with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2) * 2) as pool:
        results = list(pool.map(run, names))
    failed = [r for r in results if r[1] != 0]
    for name, _, err, _ in failed:
        print(f"== {name}\n{err}")
    tests = sum(int(line.split()[1]) for _, _, err, _ in results for line in err.splitlines() if line.startswith("Ran "))
    print(f"{tests} tests in {len(names)} modules, {time.time() - t:.0f} s: "
          + (f"{len(failed)} module(s) failed" if failed else "OK"))
    return 1 if failed else 0


def main(argv) -> int:
    argv = list(argv)
    docker = "--docker" in argv
    if docker:
        argv.remove("--docker")
    # Route modules load settings during discovery, before setUp can isolate
    # them. Never let the test runner open a developer's saved profile.
    with tempfile.TemporaryDirectory(prefix="nullius-tests-") as profile:
        with patch.dict(os.environ, {"NULLIUS_HOME": profile, **({"NULLIUS_TEST_DOCKER": "1"} if docker else {})}):
            if not docker:
                os.environ.pop("NULLIUS_TEST_DOCKER", None)
            for name in ("NULLIUS_CATALOGUE", "WORKSPACES_DIR", "RUNS_DIR", "SECRETS_DIR",
                         "STANDARD_DIR", "STANDARD_DATA_DIR", "HARBOR_PINS", "NULLIUS_PRICES"):
                os.environ.pop(name, None)
            return _suite(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
