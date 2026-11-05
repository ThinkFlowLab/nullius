"""User catalogues survive installation changes without modifying the bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nullius import paths
from nullius.catalogue import catalog, harbor_entry, storage
from nullius.standard import definitions, install
from .test_harbor_entry import form
from .test_standard_definitions import AGENT, BENCH, CAPABILITY
from .test_standard_versions import VERSIONED


class Overlay(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="nullius-overlay-"))
        self.bundle = Path(tmp, "bundle")
        self.user = Path(tmp, "profile", "data", "catalogue")
        self.enterContext(patch.dict(os.environ, {"NULLIUS_HOME": str(Path(tmp, "profile")),
                                                "NULLIUS_CATALOGUE": str(self.user)}))
        self.enterContext(patch.object(catalog, "ROOT", str(self.bundle)))
        self.enterContext(patch.object(definitions, "CATALOGUE", str(self.bundle)))

    def write(self, root, group, key, name, data):
        folder = Path(root, group, key)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data), encoding="utf-8")
        return folder

    def benchmark(self, root, key="toy", **extra):
        entry = harbor_entry.build(form(key=key, title=key.title()))
        entry.update(extra)
        return self.write(root, "benchmarks", key, "benchmark.json", entry)

    def definition(self, root, kind, data):
        spec = definitions.KINDS[kind]
        return self.write(root, spec["dir"], data["key"], spec["file"], data)

    def tree(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}

    def test_user_folder_wins_whole_and_order_is_by_key(self):
        bundled = self.benchmark(self.bundle, title="Bundled", datasets=[{"key": "old", "title": "Old", "source": "GitHub"}])
        (bundled / "driver.py").write_text("bundled driver")
        self.benchmark(self.bundle, "zulu")
        user = self.benchmark(self.user, title="User")
        (user / "notes.txt").write_text("user notes")
        self.benchmark(self.user, "alpha")
        cat = catalog.load()
        self.assertEqual([b.key for b in cat.benchmarks], ["alpha", "toy", "zulu"])
        self.assertEqual(cat.benchmark("toy").title, "User")
        self.assertIsNone(cat.dataset("old"))
        self.assertEqual(cat.path, str(self.user))
        self.assertEqual(cat.entry_dir("toy"), str(user))
        self.assertEqual(cat.files("toy"), ["benchmark.json", "notes.txt"])
        self.assertEqual(catalog.validate(cat), [])
        self.assertEqual(catalog.load(str(self.bundle)).benchmark("toy").title, "Bundled")
        self.assertIsNone(catalog.load(str(self.bundle)).benchmark("alpha"))

    def test_missing_or_invalid_override_never_reveals_bundle(self):
        self.benchmark(self.bundle)
        user = self.user / "benchmarks" / "toy"
        user.mkdir(parents=True)
        self.assertIsNone(catalog.load().benchmark("toy"))
        self.assertEqual(catalog.validate(catalog.load()), ["benchmarks/toy/ has no benchmark.json"])
        (user / "benchmark.json").write_text("{")
        self.assertIsNone(catalog.load().benchmark("toy"))          # left out, the bundled one not shown instead
        self.assertTrue(any("benchmark.json" in error for error in catalog.layout()))

    def test_partial_standard_override_is_reported_not_mixed_with_bundle(self):
        self.benchmark(self.bundle, harness=["standard"])
        self.definition(self.bundle, "benchmark", BENCH)
        self.benchmark(self.user, harness=["standard"])
        self.assertIsNone(definitions.get("benchmark", "toy"))
        self.assertIn("benchmarks/toy/ names the Nullius standard but has no standard.json",
                      catalog.validate(catalog.load()))

    def test_layout_validates_selected_folders_in_both_roots(self):
        self.benchmark(self.bundle, key="a")
        self.benchmark(self.user, key="b")
        self.write(self.bundle, "benchmarks", "wrong", "benchmark.json",
                   {"key": "different", "title": "Wrong", "category": "Coding", "datasets": []})
        self.assertTrue(any("not its folder's name" in p for p in catalog.validate(catalog.load())))
        self.benchmark(self.user, key="wrong")
        self.assertEqual(catalog.validate(catalog.load()), [])

    def test_signature_tracks_override_add_update_delete_and_profile_switch(self):
        self.benchmark(self.bundle)
        before = catalog.signature()
        folder = self.benchmark(self.user, title="User")
        added = catalog.signature()
        self.assertNotEqual(before, added)
        self.benchmark(self.user, title="User with a longer title")
        self.assertNotEqual(added, catalog.signature())
        shutil.rmtree(folder)
        self.assertEqual(before, catalog.signature())
        with patch.dict(os.environ, NULLIUS_CATALOGUE=str(self.user.parent / "other")):
            self.assertNotEqual(before, catalog.signature())
        self.assertTrue(all(len(pair) == 2 for pair in before))  # web.shared's cache contract

    def test_save_uses_user_root_and_collisions_include_bundle(self):
        self.benchmark(self.bundle)
        before = self.tree(self.bundle)
        entry = harbor_entry.build(form())
        path = harbor_entry.save(entry)
        self.assertEqual(path, str(self.user / "benchmarks" / "code-bench" / "benchmark.json"))
        self.assertIsNotNone(catalog.load().benchmark("code-bench"))
        for collision in (harbor_entry.build(form(key="toy")), entry):
            with self.assertRaisesRegex(ValueError, "exists already"):
                harbor_entry.save(collision)
        with self.assertRaisesRegex(ValueError, "dataset key 'toy' used twice"):
            harbor_entry.save(harbor_entry.build(form(key="third", dataset_key="toy")))
        self.assertEqual(self.tree(self.bundle), before)

    def test_edit_copies_complete_read_only_folder_and_keeps_metadata(self):
        folder = self.benchmark(self.bundle, report={"file": "result.json"}, extra={"private": "metadata"})
        self.definition(self.bundle, "benchmark", BENCH)
        (folder / "driver.py").write_text("driver body")
        (folder / "assets").mkdir()
        (folder / "assets" / "settings.json").write_text('{"kept":true}')
        (folder / "benchmark.json").chmod(0o444)
        (folder / "assets").chmod(0o555)
        folder.chmod(0o555)
        before = self.tree(self.bundle)
        old = harbor_entry.entry_of("toy")
        changed = harbor_entry.merged(old, harbor_entry.build(form(key="toy", title="Edited")))
        written = harbor_entry.update(changed)
        self.assertEqual(written, str(self.user / "benchmarks" / "toy" / "benchmark.json"))
        self.assertEqual(harbor_entry.entry_of("toy")["extra"], old["extra"])
        self.assertEqual(harbor_entry.entry_of("toy")["report"], old["report"])
        for relative, contents in before.items():
            if relative.endswith("benchmark.json"):
                continue
            self.assertEqual((self.user / relative).read_bytes(), contents)
        self.assertEqual(self.tree(self.bundle), before)
        self.assertTrue(os.stat(Path(written)).st_mode & 0o200)
        self.assertEqual(definitions.get("benchmark", "toy")["_here"], str(self.user / "benchmarks" / "toy"))
        changed["title"] = "Second edit"
        harbor_entry.update(changed)
        self.assertEqual(catalog.load().benchmark("toy").title, "Second edit")
        # Restore permissions only in this fixture, so temporary cleanup works.
        folder.chmod(0o755)
        (folder / "assets").chmod(0o755)

    def test_user_rename_keeps_files_but_bundle_backed_key_cannot_rename(self):
        harbor_entry.save(harbor_entry.build(form()))
        source = self.user / "benchmarks" / "code-bench"
        (source / "extra.txt").write_text("keep")
        entry = harbor_entry.entry_of("code-bench")
        entry["key"] = "renamed"
        harbor_entry.update(entry, was="code-bench")
        self.assertFalse(source.exists())
        self.assertEqual((self.user / "benchmarks" / "renamed" / "extra.txt").read_text(), "keep")
        self.benchmark(self.bundle)
        changed = harbor_entry.entry_of("toy")
        changed["key"] = "new-name"
        with self.assertRaisesRegex(ValueError, "bundled entry keeps its key"):
            harbor_entry.update(changed, was="toy")

    def test_delete_user_override_restores_bundle_and_cannot_remove_bundle(self):
        self.benchmark(self.bundle)
        before = self.tree(self.bundle)
        with self.assertRaisesRegex(ValueError, "read-only"):
            harbor_entry.delete("toy")
        harbor_entry.update(dict(harbor_entry.entry_of("toy"), title="Edited"))
        harbor_entry.delete("toy")
        self.assertEqual(catalog.load().benchmark("toy").title, "Toy")
        self.assertEqual(self.tree(self.bundle), before)
        harbor_entry.save(harbor_entry.build(form()))
        harbor_entry.delete("code-bench")
        self.assertIsNone(catalog.load().benchmark("code-bench"))

    def test_explicit_fixture_roots_are_isolated_for_read_and_write(self):
        self.benchmark(self.bundle)
        self.benchmark(self.user, "user-only")
        isolated = self.bundle.parent / "isolated"
        harbor_entry.save(harbor_entry.build(form(key="toy")), str(isolated))
        self.assertEqual([b.key for b in catalog.load(str(isolated)).benchmarks], ["toy"])
        changed = dict(harbor_entry.entry_of("toy", str(isolated)), title="Fixture")
        harbor_entry.update(changed, str(isolated))
        self.assertEqual(catalog.load().benchmark("toy").title, "Toy")
        self.assertEqual(harbor_entry.entry_of("toy", str(isolated))["title"], "Fixture")
        harbor_entry.delete("toy", str(isolated))
        self.assertIsNone(harbor_entry.entry_of("toy", str(isolated)))

    def test_writes_cannot_target_bundle_even_explicitly_or_through_symlink(self):
        self.benchmark(self.bundle)
        with self.assertRaisesRegex(ValueError, "read-only"):
            harbor_entry.save(harbor_entry.build(form()), str(self.bundle))
        self.user.mkdir(parents=True)
        (self.user / "benchmarks").symlink_to(self.bundle / "benchmarks", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "read-only"):
            harbor_entry.update(dict(harbor_entry.entry_of("toy"), title="No"))

    def test_definition_overlays_apply_to_all_kinds_and_driver_versions(self):
        for kind, data in (("agent", AGENT), ("capability", CAPABILITY), ("benchmark", BENCH)):
            self.definition(self.bundle, kind, data)
            folder = self.definition(self.user, kind, dict(data, label="User " + data["label"]))
            got = definitions.get(kind, data["key"])
            self.assertEqual(got["_here"], str(folder))
            self.assertTrue(got["label"].startswith("User "))
            self.assertEqual(definitions.load(kind)[0], [got])
            self.assertEqual(definitions.get(kind, data["key"], str(self.bundle))["label"], data["label"])
        versioned = dict(BENCH, upstream={"license": "MIT"}, default="1",
                         versions={"1": {}, "2": {"driver": {"command": ["python3", "{here}/second.py"]}}})
        folder = self.definition(self.user, "benchmark", versioned)
        (folder / "second.py").write_text("# fixture driver\n")
        got = definitions.get("benchmark", "toy@2")
        self.assertEqual(install.driver_argv(got, str(self.user))[0], ["python3", str(folder / "second.py")])
        self.assertIsNone(definitions.get("benchmark", "toy@absent"))
        self.assertEqual([d["_ref"] for d in definitions.load("benchmark", every=True)[0]], ["toy@1", "toy@2"])

    def test_invalid_definition_override_is_reported_without_fallback(self):
        self.definition(self.bundle, "agent", AGENT)
        folder = self.definition(self.user, "agent", dict(AGENT, key="bot", schema="bad"))
        self.assertIsNone(definitions.get("agent", "bot"))
        loaded, errors = definitions.load("agent")
        self.assertEqual(loaded, [])
        self.assertTrue(any("schema" in p for p in errors))
        (folder / "agent.json").write_text("{")
        self.assertIsNone(definitions.get("agent", "bot"))
        self.assertTrue(definitions.load("agent")[1])
        (folder / "agent.json").unlink()
        self.assertIsNone(definitions.get("agent", "bot"))
        self.assertEqual(definitions.load("agent"), ([], ["agent bot: missing agent.json"]))

    def test_proof_save_copies_bundle_and_preserves_other_version_and_files(self):
        folder = self.definition(self.bundle, "agent", VERSIONED)
        (folder / "template.toml").write_text("template contents")
        before = self.tree(self.bundle)
        loaded = definitions.get("agent", "bot@2.0.0")
        loaded["proven"] = {"agent.task/v1": {"passed": "1/1", "on": "2026-10-27"}}
        definitions.save(loaded)
        path = self.user / "agents" / "bot" / "agent.json"
        raw = json.loads(path.read_text())
        self.assertEqual(raw["versions"]["1.0.0"], VERSIONED["versions"]["1.0.0"])
        self.assertEqual(raw["versions"]["2.0.0"]["proven"], loaded["proven"])
        self.assertEqual(raw["start"], VERSIONED["start"])
        self.assertEqual(loaded["_here"], str(path.parent))
        self.assertEqual((path.parent / "template.toml").read_text(), "template contents")
        self.assertEqual(self.tree(self.bundle), before)

    def test_flat_definition_save_and_explicit_fixture_save(self):
        self.definition(self.bundle, "capability", CAPABILITY)
        before = self.tree(self.bundle)
        loaded = definitions.get("capability", "mem")
        loaded["summary"] = "Updated"
        definitions.save(loaded)
        self.assertEqual(definitions.get("capability", "mem")["summary"], "Updated")
        self.assertEqual(self.tree(self.bundle), before)
        fixture = self.bundle.parent / "fixture"
        folder = self.definition(fixture, "capability", CAPABILITY)
        loaded = definitions.get("capability", "mem", str(fixture))
        loaded["summary"] = "Fixture"
        definitions.save(loaded)
        self.assertEqual(json.loads((folder / "capability.json").read_text())["summary"], "Fixture")
        self.assertEqual(definitions.get("capability", "mem")["summary"], "Updated")

    def test_staged_snapshot_uses_only_its_own_catalogue(self):
        self.benchmark(self.bundle)
        self.definition(self.bundle, "agent", AGENT)
        self.benchmark(self.user, "unrelated")
        self.definition(self.user, "capability", CAPABILITY)
        with patch.dict(os.environ, NULLIUS_CATALOGUE=str(self.bundle)):
            self.assertEqual(storage.roots(str(self.bundle)), (str(self.bundle),))
            self.assertEqual([b.key for b in catalog.load().benchmarks], ["toy"])
            self.assertEqual(definitions.load("capability"), ([], []))
            self.assertEqual(definitions.get("agent", "bot")["_here"], str(self.bundle / "agents" / "bot"))
            with self.assertRaisesRegex(ValueError, "read-only"):
                definitions.writable_dir("agent", "bot")
        self.assertEqual(paths.catalogue_dir(), str(self.user))

    def test_use_bundled_restores_whole_entries_and_keeps_saved_data(self):
        self.benchmark(self.bundle)
        self.definition(self.bundle, "agent", VERSIONED)
        self.definition(self.bundle, "capability", CAPABILITY)
        saved = Path(paths.runs_dir(), "saved-run/result.json")
        licence = Path(paths.secrets_dir(), "toy/LICENSE_KEY")
        for path in (saved, licence):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        before = self.tree(self.bundle)
        for group, key in (("benchmarks", "toy"), ("agents", "bot"), ("capabilities", "mem")):
            folder = Path(storage.writable_dir(group, key, str(self.bundle)))
            (folder / "custom.txt").write_text("custom")
            self.assertTrue(storage.can_restore(group, key, str(self.bundle)))
            storage.use_bundled(group, key, str(self.bundle))
            self.assertFalse(folder.exists())
            self.assertFalse(storage.can_restore(group, key, str(self.bundle)))
            self.assertEqual(storage.entry_dir(group, key, str(self.bundle)), str(self.bundle / group / key))
        self.assertEqual(self.tree(self.bundle), before)
        self.assertEqual(saved.read_text(), "keep")
        self.assertEqual(licence.read_text(), "keep")

    def test_use_bundled_removes_both_capability_aliases(self):
        self.definition(self.bundle, "capability", CAPABILITY)
        self.definition(self.user, "capability", CAPABILITY)
        legacy = self.write(self.user, "features", "mem", "feature.json", CAPABILITY)
        storage.use_bundled("capabilities", "mem", str(self.bundle))
        self.assertFalse(legacy.exists())
        self.assertEqual(definitions.get("capability", "mem")["_here"], str(self.bundle / "capabilities/mem"))

    def test_use_bundled_refuses_custom_only_bundled_only_and_unsafe_paths(self):
        self.benchmark(self.bundle, "bundled")
        self.benchmark(self.user, "custom")
        before = self.tree(self.bundle), self.tree(self.user)
        for group, key in (("benchmarks", "bundled"), ("benchmarks", "custom"),
                           ("benchmarks", ".."), ("benchmarks", "../custom"),
                           ("benchmarks", "..\\custom"), ("../benchmarks", "custom")):
            with self.subTest(group=group, key=key):
                self.assertFalse(storage.can_restore(group, key, str(self.bundle)))
                with self.assertRaises(ValueError):
                    storage.use_bundled(group, key, str(self.bundle))
        with patch.dict(os.environ, NULLIUS_CATALOGUE=str(self.bundle)):
            with self.assertRaisesRegex(ValueError, "read-only"):
                storage.use_bundled("benchmarks", "bundled", str(self.bundle))
        self.assertEqual((self.tree(self.bundle), self.tree(self.user)), before)

    def test_use_bundled_unlinks_an_override_without_deleting_its_external_target(self):
        self.benchmark(self.bundle)
        external = self.benchmark(self.bundle.parent / "external")
        link = self.user / "benchmarks/toy"
        link.parent.mkdir(parents=True)
        link.symlink_to(external, target_is_directory=True)
        storage.use_bundled("benchmarks", "toy", str(self.bundle))
        self.assertFalse(link.exists())
        self.assertTrue((external / "benchmark.json").is_file())
        link.symlink_to(self.bundle / "benchmarks/toy", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "read-only"):
            storage.use_bundled("benchmarks", "toy", str(self.bundle))
        self.assertTrue((self.bundle / "benchmarks/toy/benchmark.json").is_file())

    def test_use_bundled_can_recover_an_invalid_override(self):
        self.definition(self.bundle, "agent", AGENT)
        folder = self.user / "agents/bot"
        folder.mkdir(parents=True)
        (folder / "agent.json").write_text("{")
        self.assertIsNone(definitions.get("agent", "bot"))
        storage.use_bundled("agents", "bot", str(self.bundle))
        self.assertEqual(definitions.get("agent", "bot")["_here"], str(self.bundle / "agents/bot"))


if __name__ == "__main__":
    unittest.main()
