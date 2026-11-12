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
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from nullius import cli
from nullius.standard import cli as standard_cli, definitions, pins

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


class ImagePins(unittest.TestCase):
    OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    REF = "docker.io/example/image@" + OLD

    def test_both_pin_sources_are_read_without_executing_or_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            driver = root / "driver.py"
            driver.write_text(f'raise RuntimeError("must not execute")\nIMAGES = {{"task": "{self.REF}"}}\n')
            self.assertEqual(pins.references(folder), [(self.REF, self.REF)])
            (root / "images.json").write_text(json.dumps({"task": self.REF}))
            self.assertEqual(pins.references(folder), [("task", self.REF)])
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with patch.object(definitions, "get", return_value={"_here": folder}), \
                    patch.object(pins, "current", return_value=self.OLD), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(standard_cli.main(["pins", "fixture", "--limit", "1"]), 0)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_tags_registry_ports_and_docker_hub_shorthand(self):
        for reference, override, expected in (
                ("ubuntu@" + self.OLD, None, ("registry-1.docker.io", "library/ubuntu", "latest", self.OLD)),
                ("registry.example:5443/team/image:stable@" + self.OLD, None,
                 ("registry.example:5443", "team/image", "stable", self.OLD)),
                (self.REF, "v2", ("registry-1.docker.io", "example/image", "v2", self.OLD))):
            self.assertEqual(pins.target(reference, override), expected)
        for ref in ("not-pinned", "https://example/image@" + self.OLD, "user:password@host/image@" + self.OLD):
            with self.assertRaises(ValueError):
                pins.target(ref)

    @staticmethod
    def response(body=b"", **headers):
        response = io.BytesIO(body)
        response.headers = headers
        return response

    def test_registry_head_and_anonymous_bearer_challenge(self):
        challenge = HTTPError("https://registry.example/v2/example/image/manifests/latest", 401, "auth", {
            "WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="registry.example",scope="repository:other:push"'}, None)
        with patch.object(pins, "build_opener") as opener:
            open_ = opener.return_value.open
            open_.side_effect = [challenge, self.response(b'{"token":"fixture-token"}'),
                                 self.response(**{"Docker-Content-Digest": self.OLD})]
            self.assertEqual(pins.current("registry.example", "example/image", "latest"), self.OLD)
            requests = [call.args[0] for call in open_.call_args_list]
            self.assertEqual([r.get_method() for r in requests], ["HEAD", "GET", "HEAD"])
            self.assertIn("scope=repository%3Aexample%2Fimage%3Apull", requests[1].full_url)
            self.assertEqual(requests[2].get_header("Authorization"), "Bearer fixture-token")
            self.assertIn("application/vnd.oci.image.index.v1+json", requests[0].get_header("Accept"))

    def test_missing_manifest_is_distinct_from_auth_rate_limits_and_connection_failures(self):
        for failure, state in ((HTTPError("https://registry.example", 404, "gone", {}, None), "gone"),
                               (HTTPError("https://registry.example", 429, "limited", {}, None), "not checked"),
                               (HTTPError("https://registry.example", 403, "denied", {}, None), "not checked"),
                               (URLError("unreachable"), "not checked"), (TimeoutError(), "not checked")):
            with self.subTest(failure=failure), patch.object(pins, "current", side_effect=failure):
                self.assertEqual(pins.check(self.REF)[0], state)
        challenge = 'Bearer realm="https://auth.example/token"'
        with patch.object(pins, "build_opener") as opener:
            opener.return_value.open.side_effect = [HTTPError("https://registry.example", 401, "auth", {"WWW-Authenticate": challenge}, None),
                                                    HTTPError("https://auth.example/token", 404, "gone", {}, None)]
            state, detail = pins.check(self.REF)
            self.assertEqual(state, "not checked")
            self.assertIn("token service returned HTTP 404", detail)

    def test_missing_digest_and_unsafe_auth_are_not_used(self):
        with self.assertRaisesRegex(ValueError, "no valid sha256"):
            pins.current("registry.example", "example/image", "latest", open_=lambda *a, **k: self.response())
        for challenge in ('Basic realm="private"', 'Bearer realm="http://auth.example/token"'):
            with self.assertRaisesRegex(ValueError, "unsupported authentication"):
                pins._token(challenge, "example/image", lambda *a, **k: self.fail("must not request"))

    def test_cli_limits_and_exit_precedence(self):
        refs = [("first", self.REF), ("second", self.REF)]
        with patch.object(definitions, "get", return_value={"_here": "unused"}), patch.object(pins, "references", return_value=refs), \
                patch.object(pins, "current") as current:
            for results, expected in (([self.OLD, self.OLD], 0), ([self.OLD, self.NEW], 1),
                                       ([self.NEW, URLError("down")], 2)):
                current.side_effect = results
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(standard_cli.main(["pins", "fixture"]), expected)
                if expected == 1:
                    self.assertIn(f"{self.OLD} -> {self.NEW}", out.getvalue())
            current.reset_mock(side_effect=True)
            current.return_value = self.OLD
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(standard_cli.main(["pins", "fixture", "--tag", "stable", "--limit", "1"]), 0)
            current.assert_called_once_with("registry-1.docker.io", "example/image", "stable")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(standard_cli.main(["pins", "fixture", "--limit", "0"]), 2)
                self.assertEqual(standard_cli.main(["pins", "fixture", "--tag", "bad/tag"]), 2)


if __name__ == "__main__":
    unittest.main()
