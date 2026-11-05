"""The public CLI dispatches existing commands with isolated user storage."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius import cli
from nullius.standard import cli as standard_cli, definitions

APP = Path(__file__).resolve().parents[1]


class UnifiedCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullius-command-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root / "home"),
                    "NULLIUS_HOME": str(self.root / "profile"), "PYTHONPATH": str(APP),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "nullius", *args],
                              cwd=self.root, env=self.env, capture_output=True, text=True)

    def test_invalid_settings_leave_paths_available_and_other_commands_explain_repair(self):
        path = self.root / "profile/config/settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        result = self.run_cli("paths", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)
        failed = self.run_cli("standard", "list")
        self.assertEqual(failed.returncode, 2)
        self.assertIn(str(path), failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)

    def test_top_level_help_and_module_entry_point(self):
        for args in ((), ("--help",)):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            for command in cli.COMMANDS:
                self.assertIn(command, result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_version_uses_distribution_name_and_keeps_command_name(self):
        output = io.StringIO()
        with patch.object(cli.importlib.metadata, "version", return_value="0.1.0rc1") as version, \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--version"]), 0)
        version.assert_called_once_with("nullius-eval")
        self.assertEqual(output.getvalue(), "nullius 0.1.0rc1\n")

    def test_version_without_installed_distribution_reports_source_checkout(self):
        output = io.StringIO()
        with patch.object(cli.importlib.metadata, "version",
                          side_effect=cli.importlib.metadata.PackageNotFoundError), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(output.getvalue(), "nullius source checkout\n")

    def test_existing_backend_commands_run_without_web_imports(self):
        script = """
import importlib.abc, sys
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('flask', 'web', '_nullius_app'):
            raise AssertionError('web imported: ' + fullname)
sys.meta_path.insert(0, NoWeb())
from nullius.cli import main
raise SystemExit(main(['import', '--list', '--json']))
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)), {"atif", "otel"})
        for command in ("runs", "compare", "standard", "workspace", "paths"):
            result = self.run_cli(command, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)

    def test_errors_keep_the_delegated_exit_status(self):
        self.assertEqual(self.run_cli("import", str(self.root / "missing")).returncode, 3)
        self.assertEqual(self.run_cli("runs", "unknown").returncode, 2)
        self.assertEqual(self.run_cli("unknown").returncode, 2)

    def test_settings_are_loaded_before_command_module_initialization(self):
        config = self.root / "profile" / "config" / "settings.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"STANDARD_DIR": str(self.root / "runtime")}))
        for invocation in (
            "from nullius.cli import main\nmain(['standard', '--help'])",
            "import runpy, sys\nsys.argv = ['standard', '--help']\n"
            "runpy.run_module('nullius.standard.cli', run_name='__main__')",
        ):
            script = """
try:
    exec(INVOCATION)
except SystemExit as exc:
    assert exc.code == 0
from nullius.standard import install
import os
assert install.HOME == os.environ['STANDARD_DIR'], install.HOME
""".replace("INVOCATION", repr(invocation))
            result = subprocess.run([sys.executable, "-c", script], cwd=self.root,
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_from_run_does_not_load_saved_credentials(self):
        with patch.dict(os.environ, self.env, clear=True), \
             patch("nullius.settings.load") as load, \
             patch.object(cli.importlib, "import_module") as module:
            module.return_value.main.return_value = 0
            self.assertEqual(cli.main(["standard", "run", "--from-run"]), 0)
            load.assert_not_called()

    def test_abbreviated_from_run_is_rejected_by_both_entry_points(self):
        args = ['run', 'missing-benchmark', 'missing-agent', '--model', 'test', '--from-r']
        for module, prefix in (('nullius', ['standard']), ('nullius.standard.cli', [])):
            result = subprocess.run([sys.executable, '-m', module, *prefix, *args],
                                    cwd=self.root, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('unrecognized arguments: --from-r', result.stderr)


class CatalogueCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullius-command-catalogue-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bundled = root / "bundled"
        self.user = root / "profile" / "data" / "catalogue"
        self.entry = self.bundled / "agents" / "example"
        self.entry.mkdir(parents=True)
        self.original = {"key": "example", "default": "1", "versions": {"1": {
            "install": {"pip": ["example==1"]}, "proven": {"agent.task/v1": {"passed": True}}}}}
        (self.entry / "agent.json").write_text(json.dumps(self.original))
        (self.entry / "agent.py").write_text("# wrapper kept beside its definition\n")
        self.enterContext(patch.dict(os.environ, {"HOME": str(root / "home"),
                                                 "NULLIUS_HOME": str(root / "profile")}, clear=True))
        self.enterContext(patch.object(standard_cli, "ENV_FILE", str(root / "profile/config/settings.json")))
        self.enterContext(patch.object(definitions, "CATALOGUE", str(self.bundled)))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_new_refuses_a_bundled_name_before_writing(self):
        with self.assertRaisesRegex(SystemExit, "exists already"):
            standard_cli.main(["new", "agent", "example"])
        self.assertFalse(self.user.exists())

    def test_new_version_copies_the_complete_entry_and_preserves_the_bundle(self):
        path = Path(standard_cli.new_version("example", "2"))
        self.assertEqual(path, self.user / "agents" / "example" / "agent.json")
        self.assertEqual(json.loads((self.entry / "agent.json").read_text()), self.original)
        self.assertEqual((path.parent / "agent.py").read_bytes(), (self.entry / "agent.py").read_bytes())
        self.assertEqual(json.loads(path.read_text())["versions"]["2"],
                         {"install": {"pip": ["example==2"]}, "proven": {}})
        with self.assertRaisesRegex(SystemExit, "already"):
            standard_cli.new_version("example", "2")

    def test_new_creates_only_a_user_entry_and_unknown_version_has_a_clear_error(self):
        self.assertEqual(standard_cli.main(["new", "agent", "fresh"]), 0)
        self.assertTrue((self.user / "agents" / "fresh" / "agent.json").is_file())
        self.assertFalse((self.bundled / "agents" / "fresh").exists())
        with self.assertRaisesRegex(SystemExit, "no agent, capability or benchmark"):
            standard_cli.new_version("missing", "2")


if __name__ == "__main__":
    unittest.main()
