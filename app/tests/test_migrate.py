"""Explicit migration copies user data without exposing values or changing sources."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nullius import migrate, paths, settings


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "source"
        (self.checkout / "app" / "nullius").mkdir(parents=True)
        self.old = self.checkout / "app" / ".env"
        self.old.write_text('OPENAI_API_KEY=fixture-migration-secret\nHOSTS=host-alias\n')
        self.profile = self.root / "profile"
        env = patch.dict(os.environ, {"NULLIUS_HOME": str(self.profile),
                                     "HOME": str(self.root / "home")}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.bundled = self.root / "bundle/app/nullius/catalogue"
        bundle = patch.object(migrate, "__file__", str(self.bundled.parent / "migrate.py"))
        bundle.start(); self.addCleanup(bundle.stop)

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = migrate.main(["--from", str(self.checkout), *args])
        self.assertNotIn("fixture-migration-secret", output.getvalue())
        self.assertNotIn("fixture-licence-bytes", output.getvalue())
        return code, output.getvalue()

    def entry(self, base=None, name="custom", kind="benchmarks"):
        base = base or self.checkout / "app/nullius/catalogue"
        entry = base / kind / name
        entry.mkdir(parents=True)
        return entry

    def test_preview_then_copy_and_repeat_keep_sources_and_permissions(self):
        before = self.old.read_bytes()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertEqual(settings.in_file(paths.settings_file())["OPENAI_API_KEY"], "fixture-migration-secret")
        self.assertEqual(Path(paths.settings_file()).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(paths.config_dir()).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.old.read_bytes(), before)
        self.assertIn("files_to_write: 0", self.run_cli("--apply")[1])

    def test_conflict_prevents_all_writes_and_never_prints_values(self):
        dest = Path(paths.settings_file()); dest.parent.mkdir(parents=True)
        dest.write_text(json.dumps({"OPENAI_API_KEY": "fixture-existing-secret"}))
        code, output = self.run_cli("--apply")
        self.assertEqual(code, 1)
        self.assertNotIn("fixture-existing-secret", output)
        self.assertEqual(json.loads(dest.read_text()), {"OPENAI_API_KEY": "fixture-existing-secret"})

    def test_workspaces_judgements_and_custom_entries_survive_without_moving_raw_runs(self):
        entry = self.checkout / "app/nullius/catalogue/benchmarks/my-custom"
        entry.mkdir(parents=True)
        (entry / "benchmark.json").write_text('{"key":"my-custom"}')
        (entry / "driver.py").write_text("print('fixture')\n")
        workspace = self.checkout / "workspaces/one"
        workspace.mkdir(parents=True)
        (workspace / "workspace.json").write_text(json.dumps({"name":"one", "roots":["runs"]}))
        (workspace / "judgements").mkdir()
        (workspace / "judgements/one.json").write_text('{"fixture":true}')
        (self.checkout / "runs").mkdir()
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertTrue(Path(paths.catalogue_dir(), "benchmarks/my-custom/driver.py").is_file())
        migrated = json.loads(Path(paths.workspaces_dir(), "one/workspace.json").read_text())
        self.assertEqual(migrated["roots"], [str(self.checkout / "runs")])
        self.assertTrue(Path(paths.workspaces_dir(), "one/judgements/one.json").is_file())
        self.assertEqual(settings.in_file(paths.settings_file())["RUNS_DIR"], str(self.checkout / "runs"))
        self.assertTrue((workspace / "workspace.json").exists())

    def test_licences_are_opt_in_and_copied_privately(self):
        secret = self.root / "legacy-secrets/bench"
        secret.mkdir(parents=True)
        (secret / "LICENSE_KEY").write_bytes(b"fixture-licence-bytes")
        self.assertEqual(self.run_cli("--apply", "--secrets", str(secret.parent))[0], 0)
        dest = Path(paths.secrets_dir(), "bench/LICENSE_KEY")
        self.assertEqual(dest.read_bytes(), b"fixture-licence-bytes")
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(dest.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(dest.parent.parent.stat().st_mode & 0o777, 0o700)

    def test_linked_checkout_profile_workspaces_and_licence_roots(self):
        disk = self.root / "other-disk"
        disk.mkdir()
        alias = self.root / "linked-disk"
        alias.symlink_to(disk, target_is_directory=True)
        checkout_link = alias / "old-checkout"
        checkout_link.symlink_to(self.checkout, target_is_directory=True)
        workspaces = disk / "workspaces/one"
        workspaces.mkdir(parents=True)
        (workspaces / "workspace.json").write_text('{"roots":["runs"]}')
        (self.checkout / "workspaces").symlink_to(workspaces.parent, target_is_directory=True)
        licence = disk / "licences/bench"
        licence.mkdir(parents=True)
        (licence / "LICENSE_KEY").write_bytes(b"fixture-licence-bytes")
        (self.entry() / "definition.json").write_text("{}")
        with patch.dict(os.environ, {"NULLIUS_HOME": str(alias / "profile")}):
            pending, counts = migrate.prepare(checkout_link, alias / "licences")
            self.assertFalse((disk / "profile").exists())
            migrate.apply(pending)
            self.assertEqual(counts["workspace_files"], 1)
            self.assertEqual(json.loads(Path(paths.workspaces_dir(), "one/workspace.json").read_text())["roots"],
                             [str(self.checkout / "runs")])
            self.assertTrue(Path(paths.catalogue_dir(), "benchmarks/custom/definition.json").is_file())
            copied = Path(paths.secrets_dir(), "bench/LICENSE_KEY")
            self.assertEqual(copied.read_bytes(), b"fixture-licence-bytes")
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertIn("files_to_write: 0", self.run_cli("--apply", "--secrets", str(alias / "licences"))[1])
        self.assertTrue((workspaces / "workspace.json").is_file())
        self.assertTrue(alias.is_symlink())

    def test_individually_linked_destination_roots(self):
        (self.entry() / "definition.json").write_text("{}")
        for root in (paths.config_dir(), paths.catalogue_dir(), paths.workspaces_dir(), paths.secrets_dir()):
            dest = Path(root)
            physical = self.root / "disk" / dest.name
            physical.mkdir(parents=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(physical, target_is_directory=True)
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertTrue((self.root / "disk/config/settings.json").is_file())
        self.assertTrue((self.root / "disk/catalogue/benchmarks/custom/definition.json").is_file())

    def test_links_inside_linked_roots_still_stop_copying(self):
        source = self.root / "licences"
        source.mkdir()
        alias = self.root / "linked-licences"
        alias.symlink_to(source, target_is_directory=True)
        (source / "LICENSE_KEY").symlink_to(self.old)
        code, output = self.run_cli("--apply", "--secrets", str(alias))
        self.assertEqual(code, 1, output)
        self.assertIn("symbolic link", output)
        self.assertFalse(self.profile.exists())

    def test_empty_paths_use_legacy_defaults_from_an_unrelated_directory(self):
        self.old.write_text(self.old.read_text() + "WORKSPACES_DIR=\nRUNS_DIR=\n")
        workspace = self.checkout / "workspaces/one"
        workspace.mkdir(parents=True)
        (workspace / "workspace.json").write_text('{"roots":["runs/../runs"]}')
        (self.checkout / "runs").mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "unrelated.txt").write_text("do not migrate")
        cwd = os.getcwd()
        try:
            os.chdir(elsewhere)
            self.assertEqual(self.run_cli()[0], 0)
            self.assertFalse(self.profile.exists())
            self.assertEqual(self.run_cli("--apply")[0], 0)
        finally:
            os.chdir(cwd)
        saved = settings.in_file(paths.settings_file())
        self.assertNotIn("WORKSPACES_DIR", saved)
        self.assertEqual(saved["RUNS_DIR"], str(self.checkout / "runs"))
        dest = Path(paths.workspaces_dir())
        self.assertEqual(json.loads((dest / "one/workspace.json").read_text())["roots"],
                         [str(self.checkout / "runs")])
        self.assertFalse((dest / "unrelated.txt").exists())
        self.assertEqual((workspace / "workspace.json").read_text(), '{"roots":["runs/../runs"]}')

    def test_explicit_workspace_paths_are_normalized_and_preserved_without_copying(self):
        for index, value in enumerate(("./unused/../workspaces", "~/unused/../workspaces",
                                       str(self.root / "unused/../external-workspaces"))):
            with self.subTest(value=value), patch.dict(os.environ, {"NULLIUS_HOME": str(self.root / f"profile-{index}")}):
                expected = Path(os.path.abspath(os.path.expanduser(
                    str(self.checkout / value) if not value.startswith(("/", "~")) else value)))
                expected.mkdir(parents=True)
                record = expected / "workspace.json"
                record.write_text('{"roots":["runs"]}')
                self.old.write_text("OPENAI_API_KEY=fixture-migration-secret\nWORKSPACES_DIR=" + value + "\n")
                before = self.old.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns
                code, output = self.run_cli("--apply")
                self.assertEqual(code, 0, output)
                self.assertIn("workspace_files: 0", output)
                self.assertEqual(settings.in_file(paths.settings_file())["WORKSPACES_DIR"], str(expected))
                self.assertFalse(Path(paths.workspaces_dir()).exists())
                self.assertEqual((self.old.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns), before)

    def test_same_real_catalogue_copies_all_entries_with_upgrade_notice(self):
        source = self.checkout / "app/nullius/catalogue"
        for kind in ("benchmarks", "agents", "capabilities"):
            entry = self.entry(kind=kind)
            (entry / "definition.json").write_text('{"fixture":true}')
            (entry / "empty").mkdir()
        self.entry(name="empty")
        alias = self.root / "running-package"
        alias.symlink_to(source.parent, target_is_directory=True)
        with patch.object(migrate, "__file__", str(alias / "migrate.py")):
            code, output = self.run_cli()
            self.assertEqual(code, 0, output)
            self.assertIn("catalogue_entries: 4", output)
            self.assertIn("persistent overlays", output)
            self.assertIn("retain these versions across upgrades", output)
            self.assertFalse(self.profile.exists())
            self.assertEqual(self.run_cli("--apply")[0], 0)
            self.assertIn("files_to_write: 0", self.run_cli("--apply")[1])
        for kind in ("benchmarks", "agents", "capabilities"):
            dest = Path(paths.catalogue_dir()) / kind / "custom"
            self.assertEqual((dest / "definition.json").read_text(), '{"fixture":true}')
            self.assertTrue((dest / "empty").is_dir())
        self.assertTrue(Path(paths.catalogue_dir(), "benchmarks/empty").is_dir())

    def test_separate_bundle_skips_identical_entries_but_preserves_empty_custom_entries(self):
        source = self.entry(name="identical")
        bundled = self.entry(self.bundled, name="identical")
        for entry in (source, bundled):
            (entry / "definition.json").write_text('{"fixture":true}')
            (entry / "empty").mkdir()
        self.entry(name="new-empty")
        self.entry(name="emptied")
        (self.entry(self.bundled, name="emptied") / "removed.json").write_text("{}")
        code, output = self.run_cli("--apply")
        self.assertEqual(code, 0, output)
        self.assertIn("catalogue_entries: 2", output)
        dest = Path(paths.catalogue_dir(), "benchmarks")
        self.assertFalse((dest / "identical").exists())
        for name in ("new-empty", "emptied"):
            self.assertTrue((dest / name).is_dir())
            self.assertEqual(list((dest / name).iterdir()), [])
        self.assertIn("directories_to_create: 0", self.run_cli("--apply")[1])

    def test_catalogue_conflicts_compare_complete_inventory_and_bytes(self):
        source = self.entry()
        (source / "definition.json").write_text("{}")
        for index, mismatch in enumerate(("extra-file", "extra-directory", "cache-directory",
                                           "missing-file", "different-bytes", "file-to-directory")):
            with self.subTest(mismatch=mismatch), patch.dict(os.environ, {"NULLIUS_HOME": str(self.root / f"conflict-{index}")}):
                dest = self.entry(Path(paths.catalogue_dir()))
                (dest / "definition.json").write_text("{}")
                if mismatch == "extra-file":
                    (dest / "extra.py").write_text("fixture")
                elif mismatch == "extra-directory":
                    (dest / "empty").mkdir()
                elif mismatch == "cache-directory":
                    (dest / "__pycache__").mkdir()
                elif mismatch == "missing-file":
                    (dest / "definition.json").unlink()
                elif mismatch == "different-bytes":
                    (dest / "definition.json").write_text('{"changed":true}')
                else:
                    (dest / "definition.json").unlink()
                    (dest / "definition.json").mkdir()
                before = migrate._inventory(dest)
                code, output = self.run_cli("--apply")
                self.assertEqual(code, 1, output)
                self.assertIn("Destination catalogue entry differs", output)
                self.assertFalse(Path(paths.settings_file()).exists())
                self.assertEqual(migrate._inventory(dest), before)

    def test_identical_catalogue_entry_is_accepted_without_rewriting_it(self):
        for entry in (self.entry(), self.entry(Path(paths.catalogue_dir()))):
            (entry / "definition.json").write_text("{}")
            (entry / "empty").mkdir()
        dest = Path(paths.catalogue_dir(), "benchmarks/custom/definition.json")
        before = dest.stat().st_mtime_ns
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertEqual(dest.stat().st_mtime_ns, before)
        self.assertIn("files_to_write: 0", self.run_cli("--apply")[1])

    def test_apply_rechecks_whole_entries_even_when_no_files_need_copying(self):
        source = self.entry()
        (source / "definition.json").write_text("{}")
        for index, change in enumerate(("extra-file", "extra-directory", "different-bytes", "symlink", "new-entry")):
            with self.subTest(change=change), patch.dict(os.environ, {"NULLIUS_HOME": str(self.root / f"changed-{index}")}):
                dest = Path(paths.catalogue_dir(), "benchmarks/custom")
                if change != "new-entry":
                    dest.mkdir(parents=True)
                    (dest / "definition.json").write_text("{}")
                pending, _ = migrate.prepare(self.checkout)
                if change == "extra-file":
                    (dest / "extra").write_text("fixture")
                elif change == "extra-directory":
                    (dest / "empty").mkdir()
                elif change == "different-bytes":
                    (dest / "definition.json").write_text("changed")
                elif change == "symlink":
                    (dest / "definition.json").unlink()
                    dest.rmdir()
                    dest.symlink_to(source, target_is_directory=True)
                else:
                    dest.mkdir(parents=True)
                with self.assertRaises(ValueError):
                    migrate.apply(pending)
                self.assertFalse(Path(paths.settings_file()).exists())

    def test_catalogue_symlinks_are_rejected_before_traversal(self):
        for index, location in enumerate(("catalogue", "catalogue/benchmarks", "catalogue/benchmarks/dangling")):
            with self.subTest(location=location):
                link = self.checkout / "app/nullius" / location
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(self.root / f"absent-{index}", target_is_directory=True)
                code, output = self.run_cli("--apply")
                self.assertEqual(code, 1, output)
                self.assertIn("symbolic link", output)
                self.assertFalse(self.profile.exists())
                link.unlink()

    def test_identical_settings_symlink_is_rejected_without_rewriting_target(self):
        dest = Path(paths.settings_file())
        dest.parent.mkdir(parents=True)
        external = self.root / "external.json"
        external.write_text(json.dumps(settings.in_file(str(self.old))))
        dest.symlink_to(external)
        code, output = self.run_cli("--apply")
        self.assertEqual(code, 1, output)
        self.assertIn("symbolic link", output)
        self.assertTrue(dest.is_symlink())

    def test_empty_entry_destination_symlink_is_rechecked_before_any_writes(self):
        self.entry()
        pending, _ = migrate.prepare(self.checkout)
        outside = self.root / "outside"
        outside.mkdir()
        dest = Path(paths.catalogue_dir())
        dest.parent.mkdir(parents=True)
        dest.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            migrate.apply(pending)
        self.assertFalse(Path(paths.settings_file()).exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_catalogue_env_is_rejected_even_if_it_matches_the_bundle(self):
        for entry in (self.entry(), self.entry(self.bundled)):
            (entry / ".env").write_text("OPENAI_API_KEY=fixture-migration-secret")
        code, output = self.run_cli("--apply")
        self.assertEqual(code, 1, output)
        self.assertIn("contains .env", output)
        self.assertFalse(self.profile.exists())

    def test_destination_inside_checkout_is_refused(self):
        with patch.dict(os.environ, {"NULLIUS_HOME": str(self.checkout / "profile")}):
            code, output = self.run_cli("--apply")
        self.assertEqual(code, 1, output)
        self.assertIn("outside the source checkout", output)
        self.assertFalse((self.checkout / "profile").exists())

    def test_symlinks_and_changed_destinations_are_refused(self):
        self.old.unlink()
        self.old.symlink_to(self.root / "outside")
        self.assertEqual(self.run_cli("--apply")[0], 1)
        self.assertFalse(self.profile.exists())
        self.old.unlink(); self.old.write_text("HOSTS=alias\n")
        pending, _ = migrate.prepare(self.checkout)
        dest = Path(paths.settings_file()); dest.parent.mkdir(parents=True)
        dest.write_text('{"HOSTS":"another"}')
        with self.assertRaises(ValueError):
            migrate.apply(pending)
        self.assertEqual(dest.read_text(), '{"HOSTS":"another"}')
