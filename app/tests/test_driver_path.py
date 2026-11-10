"""A driver's PYTHONPATH gives it the nullius package and nothing else of the
environment Nullius runs in, from a checkout and from a wheel's site-packages."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from nullius.standard import run


def imports(path: str, *names: str) -> dict:
    """Which of ``names`` a fresh interpreter finds with ``path`` as its PYTHONPATH."""
    code = ("import importlib.util, json, sys\n"
            "print(json.dumps({n: importlib.util.find_spec(n) is not None for n in sys.argv[1:]}))")
    with tempfile.TemporaryDirectory() as cwd:
        out = subprocess.run([sys.executable, "-c", code, *names], cwd=cwd, capture_output=True, text=True, timeout=60,
                             env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": path, "PYTHONNOUSERSITE": "1"})
    return json.loads(out.stdout)


class DriverPath(unittest.TestCase):
    def test_from_a_checkout_the_driver_sees_nullius_and_not_the_rest_of_app(self):
        path = run.driver_path()
        self.assertEqual(os.listdir(path), ["nullius"])
        self.assertEqual(os.path.realpath(os.path.join(path, "nullius")), os.path.realpath(os.path.join(run.APP, "nullius")))
        self.assertEqual(imports(path, "nullius.standard.jsonl", "web"), {"nullius.standard.jsonl": True, "web": False})

    def test_from_a_wheel_the_driver_does_not_see_the_tools_site_packages(self):
        with tempfile.TemporaryDirectory() as site:
            os.symlink(os.path.join(run.APP, "nullius"), os.path.join(site, "nullius"))
            os.makedirs(os.path.join(site, "nullius_tool_only_dep"))    # a package of the tool's own environment
            open(os.path.join(site, "nullius_tool_only_dep", "__init__.py"), "w").close()
            app, run.APP = run.APP, site
            try:
                path = run.driver_path()
            finally:
                run.APP = app
            self.assertNotEqual(path, site)
            self.assertEqual(os.listdir(path), ["nullius"])
            self.assertEqual(imports(path, "nullius.standard.jsonl", "nullius_tool_only_dep"),
                             {"nullius.standard.jsonl": True, "nullius_tool_only_dep": False})

    def test_it_is_made_once_and_kept(self):
        self.assertEqual(run.driver_path(), run.driver_path())


if __name__ == "__main__":
    unittest.main()
