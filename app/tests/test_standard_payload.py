"""Installed standard snapshots: exact effective code, no configuration transport."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from nullius import execution, paths
from nullius.standard import payload


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-payload-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.package = self.tmp / "site-packages" / "nullius"
        self.catalogue = self.tmp / "profile" / "data" / "catalogue"
        self.cache = self.tmp / "cache"
        environment = patch.dict(os.environ, {"HOME": str(self.tmp / "home"), "NULLIUS_HOME": str(self.tmp / "profile"),
                                               "NULLIUS_CATALOGUE": str(self.catalogue), "SECRETS_DIR": str(self.tmp / "secrets")})
        environment.start(); self.addCleanup(environment.stop)
        self.write(self.package / "__init__.py", "# backend\n")
        self.write(self.package / "standard" / "cli.py", "print('offline runner')\n")

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) if isinstance(data, dict) else data)

    def build(self):
        return payload.build(str(self.cache), package=self.package)

    def bench(self, root, label="Bundled"):
        folder = root / "benchmarks" / "sample"
        self.write(folder / "standard.json", {"key": "sample", "label": label,
                                               "driver": {"command": ["python3", "{here}/driver.py"]}})
        self.write(folder / "driver.py", "print('sample')\n")
        return folder

    def test_a_broken_unrelated_draft_does_not_block_a_runner_snapshot(self):
        self.bench(self.package / "catalogue")
        self.write(self.package / "catalogue/agents/wip/agent.json", "{broken")
        _, here = self.build()
        self.assertTrue((Path(here) / "app/nullius/catalogue/benchmarks/sample/standard.json").is_file())
        self.assertFalse((Path(here) / "app/nullius/catalogue/agents/wip").exists())

    def test_gitless_package_is_content_addressed_and_importable_elsewhere(self):
        self.bench(self.package / "catalogue")
        with patch.object(subprocess, "run", side_effect=AssertionError("packing must not need Git")):
            key, here = self.build()
        self.assertRegex(key, r"^v1-[0-9a-f]{64}$")
        self.assertEqual(self.build(), (key, here))
        manifest = json.loads((Path(here) / "manifest.json").read_text())
        self.assertEqual(manifest["format"], 1)
        other = self.tmp / "elsewhere"; other.mkdir()
        ran = subprocess.run([sys.executable, "-m", "nullius.standard.cli"], cwd=other,
                             env=dict(os.environ, PYTHONPATH=str(Path(here) / "app")), capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "offline runner\n")

    def test_effective_user_entry_replaces_the_whole_bundled_folder(self):
        bundled = self.bench(self.package / "catalogue")
        self.write(bundled / "obsolete.py", "# never part of the override\n")
        self.bench(self.catalogue, "User benchmark")
        _, here = self.build()
        entry = Path(here) / "app/nullius/catalogue/benchmarks/sample"
        self.assertEqual(json.loads((entry / "standard.json").read_text())["label"], "User benchmark")
        self.assertFalse((entry / "obsolete.py").exists())
        self.assertEqual((entry / "driver.py").read_text(), "print('sample')\n")

    def test_definition_code_template_and_permissions_all_change_the_address(self):
        entry = self.bench(self.catalogue)
        self.write(entry / "standard.json", {"driver": {"command": ["python3", "{here}/driver.py"]},
                                              "model": {"files": {"{home}/config.toml": "config.toml"}}})
        self.write(entry / "config.toml", 'model = "{model}"\n')
        keys = [self.build()[0]]
        self.write(entry / "driver.py", "print('changed')\n"); keys.append(self.build()[0])
        self.write(entry / "config.toml", '# template changed\nmodel = "{model}"\n'); keys.append(self.build()[0])
        (entry / "driver.py").chmod(0o755); keys.append(self.build()[0])
        self.write(self.package / "standard/cli.py", "print('new release')\n"); keys.append(self.build()[0])
        self.assertEqual(len(keys), len(set(keys)))
        _, here = self.build()
        self.assertEqual((Path(here) / "app/nullius/catalogue/benchmarks/sample/driver.py").stat().st_mode & 0o777, 0o755)

    def test_an_empty_user_override_does_not_resurrect_a_bundled_definition(self):
        self.bench(self.package / "catalogue")
        (self.catalogue / "benchmarks/sample").mkdir(parents=True)
        _, here = self.build()
        self.assertFalse((Path(here) / "app/nullius/catalogue/benchmarks/sample/standard.json").exists())

    def test_no_settings_credentials_env_caches_or_unrelated_files_travel(self):
        entry = self.bench(self.catalogue)
        excluded = [self.package.parent / ".env", self.package / ".env", self.package / "settings.json",
                    self.package / "credentials.json", self.package / "__pycache__/secret.py",
                    self.package / "_resources/secret.py", self.catalogue / "settings.json",
                    entry / "credentials.json", entry / "id_ed25519", entry / "unused.json",
                    entry / "unrelated.py", entry / ".env", entry / "password.key",
                    Path(paths.settings_file()), Path(paths.secrets_dir()) / "sample" / "key"]
        for file in excluded:
            self.write(file, "private-fixture-value-never-sent")
        _, here = self.build()
        files = [p for p in Path(here).rglob("*") if p.is_file()]
        self.assertEqual({p.relative_to(here).as_posix() for p in files},
                         {"manifest.json", "app/nullius/__init__.py", "app/nullius/standard/cli.py",
                          "app/nullius/catalogue/benchmarks/sample/standard.json", "app/nullius/catalogue/benchmarks/sample/driver.py"})
        self.assertFalse(any(b"private-fixture-value-never-sent" in p.read_bytes() for p in files))

    def test_declared_templates_images_and_locally_imported_helpers_travel(self):
        entry = self.bench(self.catalogue)
        self.write(entry / "standard.json", {"driver": {"command": ["python3", "{here}/driver.py"]},
                  "versions": {"one": {"model": {"files": {"{home}/config": "templates/config.txt"}}}},
                  "environments": {"sandbox": {"dockerfile": "Dockerfile", "files": ["setup.sh"]}},
                  "mcp_servers": {"tools": {"command": ["python3", "{here}/tools.py"]}}})
        for name in ("templates/config.txt", "Dockerfile", "setup.sh", "tools.py"):
            self.write(entry / name, "# resource\n")
        self.write(entry / "driver.py", "from helpers import value\n")
        self.write(entry / "helpers/__init__.py", "from .value import value\n")
        self.write(entry / "helpers/value.py", "value = 42\n")
        self.write(entry / "helpers/unused.py", "# unrelated\n")
        _, here = self.build()
        copied = Path(here) / "app/nullius/catalogue/benchmarks/sample"
        for name in ("templates/config.txt", "Dockerfile", "setup.sh", "tools.py", "helpers/__init__.py", "helpers/value.py"):
            self.assertTrue((copied / name).is_file(), name)
        self.assertFalse((copied / "helpers/unused.py").exists())

    def test_symlinks_and_secret_resources_are_refused_without_reading_targets(self):
        entry = self.bench(self.catalogue)
        sentinel = self.tmp / "outside"
        self.write(sentinel, "private-fixture-value-never-sent")
        (entry / "driver.py").unlink(); (entry / "driver.py").symlink_to(sentinel)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.build()
        (entry / "driver.py").unlink(); self.write(entry / "driver.py", "# safe\n")
        for resource in ("../outside", ".env", "credentials.json", "secrets/key", "key.pem"):
            with self.subTest(resource=resource):
                self.write(entry / "standard.json", {"driver": {"command": ["python3", "{here}/" + resource]}})
                with self.assertRaisesRegex(ValueError, "not a runner resource"):
                    self.build()

    def test_runs_default_to_the_user_profile_and_honor_the_existing_override(self):
        self.assertEqual(execution.runs_root({"NULLIUS_HOME": str(self.tmp / "custom")}), str(self.tmp / "custom/data/runs"))
        self.assertEqual(execution.runs_root({"RUNS_DIR": str(self.tmp / "explicit")}), str(self.tmp / "explicit"))

    def test_an_altered_or_contaminated_cached_snapshot_is_never_reused(self):
        _, here = self.build()
        self.write(Path(here) / "app/.env", "private-fixture-value-never-sent")
        with self.assertRaisesRegex(ValueError, "cache has changed"):
            self.build()
        (Path(here) / "app/.env").unlink()
        self.write(Path(here) / "app/nullius/standard/cli.py", "print('changed outside the build')\n")
        with self.assertRaisesRegex(ValueError, "cache has changed"):
            self.build()

    def test_real_runtime_snapshot_loads_user_definition_from_an_unrelated_directory(self):
        source = payload.PACKAGE / "catalogue/agents/reference"
        entry = self.catalogue / "agents/reference"
        shutil.copytree(source, entry)
        definition = json.loads((entry / "agent.json").read_text())
        definition["label"] = "User reference"
        self.write(entry / "agent.json", definition)
        key, here = payload.build(str(self.cache))
        self.assertFalse((Path(here) / "app/.env").exists())
        other = self.tmp / "elsewhere"; other.mkdir()
        ran = subprocess.run([sys.executable, "-c", "from nullius.standard import definitions; print(definitions.get('agent', 'reference')['label'])"],
                             cwd=other, env=dict(os.environ, PYTHONPATH=str(Path(here) / "app"),
                                                  NULLIUS_CATALOGUE=str(Path(here) / "app/nullius/catalogue")),
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "User reference\n")

    def test_proof_metadata_saves_to_the_selected_catalogue_without_mutating_the_snapshot(self):
        source = payload.PACKAGE / "catalogue/agents/reference"
        local = self.catalogue / "agents/reference"
        shutil.copytree(source, local)
        definition = json.loads((local / "agent.json").read_text())
        definition["label"] = "Selected reference"
        self.write(local / "agent.json", definition)
        key, here = payload.build(str(self.cache))
        frozen = Path(here) / "app/nullius/catalogue"
        snapshot_before = {p.relative_to(here): p.read_bytes() for p in Path(here).rglob("*") if p.is_file()}
        remote = self.tmp / "remote-run/catalogue"
        shutil.copytree(frozen, remote)
        ambient = self.tmp / "host-profile/data/catalogue/agents/reference"
        shutil.copytree(source, ambient)
        untouched = (ambient / "agent.json").read_bytes()
        other = self.tmp / "elsewhere"; other.mkdir()
        for selected in (self.catalogue, remote):
            with self.subTest(selected=selected):
                ran = subprocess.run([sys.executable, "-c",
                    "from nullius.standard import definitions; "
                    "d = definitions.get('agent', 'reference'); "
                    "assert d['label'] == 'Selected reference'; "
                    "d.setdefault('proven', {})['fixture-proof'] = {'job': 'fixture-evidence'}; "
                    "definitions.save(d); print(d['_here'])"],
                    cwd=other, env=dict(os.environ, PYTHONPATH=str(Path(here) / "app"),
                                       PYTHONDONTWRITEBYTECODE="1", NULLIUS_HOME=str(self.tmp / "host-profile"),
                                       NULLIUS_CATALOGUE=str(selected)), capture_output=True, text=True)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertEqual(ran.stdout.strip(), str(selected / "agents/reference"))
                saved = json.loads((selected / "agents/reference/agent.json").read_text())
                self.assertEqual(saved["proven"]["fixture-proof"], {"job": "fixture-evidence"})
        self.assertEqual((ambient / "agent.json").read_bytes(), untouched)
        self.assertEqual({p.relative_to(here): p.read_bytes() for p in Path(here).rglob("*") if p.is_file()}, snapshot_before)

    def test_installed_local_execution_has_one_backend_provider_and_keeps_helpers_and_proof_writes(self):
        """A real venv site-packages copy plus a real execution snapshot.

        Only the fixture CLI is replaced: it inspects the running interpreter
        and saves synthetic proof metadata instead of running a benchmark.
        Execution, recipe dispatch, payload building and definitions.save are
        the installed implementations, exercised through actual subprocesses.
        """
        environment = self.tmp / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.tmp / "home"),
               "NULLIUS_HOME": str(self.tmp / "profile"), "NULLIUS_CATALOGUE": str(self.catalogue),
               "PYTHONDONTWRITEBYTECODE": "1", "NULLIUS_PROBE": str(self.tmp / "probe.json")}
        site = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                              env=env, capture_output=True, text=True, check=True).stdout.strip()
        installed = Path(site) / "nullius"
        shutil.copytree(payload.PACKAGE, installed, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(installed / "catalogue/agents/reference", self.catalogue / "agents/reference")
        self.write(installed / "standard/cli.py", '''import json, os, pathlib, sys
import nullius
from nullius.standard import definitions
providers = sorted({str((pathlib.Path(p or os.getcwd()) / "nullius").resolve()) for p in sys.path
                    if (pathlib.Path(p or os.getcwd()) / "nullius").is_dir()})
definition = definitions.get("agent", "reference")
if "--no-proof" not in sys.argv:
    definition.setdefault("proven", {})["fixture-proof"] = {"job": "fixture-evidence"}
    definitions.save(definition)
helper = pathlib.Path(os.environ["NULLIUS_STANDARD"]) / "app/nullius/standard/cli.py"
pathlib.Path(os.environ["NULLIUS_PROBE"]).write_text(json.dumps({
    "providers": providers, "backend": str(pathlib.Path(nullius.__file__).parent),
    "helper": str(helper), "helper_exists": helper.is_file(),
    "isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
    "no_bytecode": sys.dont_write_bytecode,
    "catalogue_entry": definition["_here"], "args": sys.argv[1:]}))
''')
        cwd = self.tmp / "outside-checkout"; cwd.mkdir()
        driver = '''import hashlib, json, pathlib, sys
import nullius
from nullius import execution, paths
from nullius.standard import recipe
execution.SYNC_EVERY = 0.02
def make_plan(index):
    # Repeat without changing any input, then verify proof persistence too.
    command = "nullius standard probe --from-run" + (" --no-proof" if index < 2 else "")
    return execution.Plan(name="provider-probe-" + str(index), runner="standard", keyless=True,
                          script=recipe._script(command))
_, root = execution.Execution(make_plan(0))._app_here()
snapshot = pathlib.Path(root)
def inventory():
    return {p.relative_to(snapshot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "directory"
            for p in snapshot.rglob("*")}
before = inventory()
for index in range(3):
    execution_ = execution.Execution(make_plan(index)).start()
    execution_.thread.join(15)
    assert not execution_.thread.is_alive(), "fixture did not finish"
    assert execution_.state == "done", execution_.lines
    assert execution_.app_on_target == str(snapshot), "a later launch must reuse the same snapshot"
    assert inventory() == before, "execution changed the snapshot inventory or content"
    assert not list(snapshot.rglob("*.pyc")), "bytecode contaminated the snapshot"
    assert not list(snapshot.rglob("__pycache__")), "bytecode directory contaminated the snapshot"
providers = sorted({str((pathlib.Path(p or '.').resolve() / 'nullius')) for p in sys.path
                    if (pathlib.Path(p or '.') / 'nullius').is_dir()})
print(json.dumps({"providers": providers, "backend": nullius.__file__,
                  "snapshot": execution_.app_on_target, "successful_launches": 3}))
'''
        ran = subprocess.run([str(python), "-I", "-B", "-c", driver], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        parent = json.loads(ran.stdout)
        self.assertEqual(parent["providers"], [str(installed)])
        self.assertEqual(parent["successful_launches"], 3)
        probe = json.loads((self.tmp / "probe.json").read_text())
        snapshot = Path(parent["snapshot"])
        # The same installed interpreter with only PYTHONPATH (the previous
        # dispatch) really does see both providers; a successful import alone
        # would therefore miss the regression this test guards.
        old = subprocess.run([str(python), "-c",
            "import json, pathlib, sys; print(json.dumps(sorted({"
            "str((pathlib.Path(p or '.').resolve() / 'nullius')) for p in sys.path "
            "if (pathlib.Path(p or '.') / 'nullius').is_dir()})))"],
            cwd=cwd, env=dict(env, PYTHONPATH=str(snapshot / "app")), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(old.stdout), sorted([str(installed), str(snapshot / "app/nullius")]))
        self.assertEqual(probe["providers"], [str(snapshot / "app/nullius")])
        self.assertEqual(probe["backend"], str(snapshot / "app/nullius"))
        self.assertEqual((probe["isolated"], probe["no_site"]), (1, 1))
        self.assertTrue(probe["no_bytecode"])
        self.assertTrue(probe["helper_exists"])
        self.assertEqual(probe["catalogue_entry"], str(self.catalogue / "agents/reference"))
        self.assertEqual(probe["args"], ["probe", "--from-run"])
        saved = json.loads((self.catalogue / "agents/reference/agent.json").read_text())
        self.assertEqual(saved["proven"]["fixture-proof"], {"job": "fixture-evidence"})
        for root in (installed, snapshot / "app/nullius"):
            self.assertNotIn("fixture-proof", json.loads((root / "catalogue/agents/reference/agent.json").read_text()).get("proven", {}))


if __name__ == "__main__":
    unittest.main()
