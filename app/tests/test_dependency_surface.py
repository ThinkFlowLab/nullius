"""What this application is allowed to install.

Nullius installs only Flask into its application's environment. Agents and
benchmark runtimes run in separate environments; reading their output must
not import their dependencies.

`OneName` checks that `nullius` resolves to this application's package from
exactly one place. The requirements and import checks together keep the
viewer independent of the runtime.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
REQUIREMENTS = APP / "requirements.txt"

#: Every distribution this application may install, and why it is here.
#: Adding a line to requirements.txt without adding it here fails this test,
#: which is the point: the surface grows only on purpose.
ALLOWED = {
    "flask": "the web framework the routes are written against",
}

#: Names that must never appear: the harness whose output this application
#: reads, and what an agent runtime drags in, none of which renders anything.
FORBIDDEN = (
    "chromadb", "faiss-cpu", "faiss", "pgvector", "sqlite-vec",
    "onnxruntime", "transformers", "tokenizers", "torch",
    "python-telegram-bot", "discord.py", "dingtalk-stream",
    "wecom-aibot-sdk", "lark-oapi",
    "harbor",
)

#: The importable names a viewer could plausibly reach for and must not: the
#: harness whose output this application reads. The backend suite asserts it
#: is not importable where it runs;
#: `ui_tests/test_viewer_without_runtime.py` opens a workspace and job pages
#: with it absent.
RUNTIME_MODULES = ("harbor",)


def requirements() -> list[str]:
    """The requirement lines, comments and blanks dropped."""
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return [s for s in (line.split("#")[0].strip() for line in lines) if s]


def name_of(requirement: str) -> str:
    """The distribution name, without version specifier or extras."""
    return re.split(r"[<>=!~\[;\s]", requirement, 1)[0].strip().lower()


class TheSurfaceIsDeclared(unittest.TestCase):
    def test_the_wheel_uses_the_same_runtime_requirements(self):
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("dependencies", config["project"]["dynamic"])
        self.assertEqual(config["tool"]["setuptools"]["dynamic"]["dependencies"],
                         {"file": ["app/requirements.txt"]})
        self.assertIn("Private :: Do Not Upload", config["project"]["classifiers"])
        self.assertFalse(config["tool"]["uv"]["managed"],
                         "source uv run commands must not install a second nullius")

    def test_every_backend_package_is_shipped_once(self):
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        packages = config["tool"]["setuptools"]["packages"]
        source = {".".join(p.parent.relative_to(APP).parts)
                  for p in (APP / "nullius").rglob("__init__.py")}
        self.assertEqual({p for p in packages if p == "nullius" or p.startswith("nullius.")}, source)
        self.assertEqual({p.split(".")[0] for p in packages}, {"nullius", "_nullius_app"})

    def test_every_requirement_is_on_the_allowlist(self):
        for req in requirements():
            with self.subTest(req):
                self.assertIn(
                    name_of(req), ALLOWED,
                    f"{name_of(req)!r} is not in ALLOWED. Adding a dependency to "
                    f"this application is a deliberate act: name it and say why.",
                )

    def test_the_allowlist_is_not_stale(self):
        """An entry nothing installs is a claim about a dependency that is gone."""
        named = {name_of(r) for r in requirements()}
        self.assertEqual(
            set(ALLOWED) - named, set(),
            "ALLOWED names something requirements.txt no longer installs.",
        )


class TheRuntimeStaysOut(unittest.TestCase):
    """Viewing must not acquire a dependency on executing."""

    def test_no_runtime_or_execution_dependency(self):
        text = REQUIREMENTS.read_text(encoding="utf-8").lower()
        for name in FORBIDDEN:
            with self.subTest(name):
                self.assertNotIn(
                    name, text,
                    f"{name!r} appears in app/requirements.txt. Reading a job "
                    f"directory never needs the runtime that produced it.",
                )

    def test_the_backend_suite_runs_where_the_runtime_is_absent(self):
        """This suite is documented to run on a bare `python3`, and it does:
        every backend test imports nothing but the standard library and
        `nullius`. Assert that the runtime is absent so imports cannot hide
        an undeclared dependency."""
        for name in RUNTIME_MODULES:
            with self.subTest(name):
                self.assertIsNone(
                    importlib.util.find_spec(name),
                    f"{name!r} is importable by this interpreter; the backend suite "
                    f"is meant to run where it is not.",
                )


#: Run in a fresh interpreter laid out as the suites run it - the repository
#: root as the working directory, `app` first on the path - so that nothing
#: another test did to this process's `sys.path` can hide a second provider.
NAMESPACE_PROBE = r"""
import importlib.util, json, os, sys
sys.path.insert(0, sys.argv[1])
out = {}
for name in sys.argv[2:]:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    origin = getattr(spec, "origin", None)
    if spec is not None and origin in (None, "namespace"):
        origin = next(iter(spec.submodule_search_locations or []), None)
    providers = set()
    for entry in sys.path:
        base = os.path.join(entry or os.getcwd(), name)
        if os.path.isdir(base) or os.path.isfile(base + ".py"):
            providers.add(os.path.realpath(base))
    out[name] = {"origin": origin, "providers": sorted(providers)}
print(json.dumps(out))
"""


class OneName(unittest.TestCase):
    """One name, one implementation in the source test interpreter.

    Counting *providers* rather than testing that an import succeeded is the
    whole point. While a local package shared a top-level name with an
    installed distribution, the local copy won at `sys.path[0]` and 595 tests
    passed against code that was meant to have been replaced; only a provider
    count noticed. Wheel tests use an isolated interpreter outside the source
    checkout: installing Nullius there must still expose exactly one provider.
    """

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([sys.executable, "-c", NAMESPACE_PROBE, str(APP), "nullius"],
                             cwd=REPO, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        cls.found = json.loads(out.stdout.strip().splitlines()[-1])

    def test_nullius_resolves_to_this_application_from_one_place(self):
        own = os.path.realpath(APP / "nullius")
        entry = self.found["nullius"]
        self.assertTrue(entry["origin"], "nullius is not importable")
        self.assertTrue(os.path.realpath(entry["origin"]).startswith(own + os.sep),
                        f"nullius resolved outside the application, at {entry['origin']}")
        self.assertEqual(entry["providers"], [own], "more than one provider of nullius: one shadows the other")


if __name__ == "__main__":
    unittest.main()
