"""Proof persistence leaves authored definitions intact and never borrows evidence."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard.contract import definitions, match, proofs
from tests import APP
from .test_standard_definitions import AGENT, BENCH, CAPABILITY
from .test_standard_versions import FLAT, PROOF, VERSIONED


class ProofFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nullius-proofs-")))
        self.root = self.tmp / "catalogue"
        self.enterContext(patch.dict(os.environ, {
            "NULLIUS_HOME": str(self.tmp / "profile"),
            "NULLIUS_CATALOGUE": str(self.root),
        }))

    def manifest(self, kind, value):
        spec = definitions.KINDS[kind]
        folder = self.root / spec["dir"] / value["key"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / spec["file"]
        path.write_text(json.dumps(value, indent=4) + "\n\n", encoding="utf-8")
        return path

    def envelope(self, kind, value, versions):
        return {"schema": "nullius.proofs/v1", "kind": kind, "key": value["key"], "versions": versions}

    def write_proofs(self, path, value):
        target = path.with_name("proofs.json")
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    def get(self, kind, ref):
        value = definitions.get(kind, ref, str(self.root))
        self.assertIsNotNone(value, definitions.load(kind, str(self.root))[1])
        return value

    def test_missing_file_keeps_legacy_evidence_without_rewriting_it(self):
        for kind, value in (("agent", AGENT), ("capability", CAPABILITY), ("agent", VERSIONED)):
            with self.subTest(kind=kind, key=value["key"], versioned="versions" in value):
                path = self.manifest(kind, value)
                before = path.read_bytes()
                got = self.get(kind, value["key"])
                self.assertEqual(got["proven"], definitions.resolve(value)["proven"])
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_name("proofs.json").exists())

    def test_every_bundled_definition_keeps_its_resolved_data_when_split(self):
        entries = []
        for kind, spec in definitions.KINDS.items():
            entries += [(kind, p) for p in Path(definitions.CATALOGUE, spec["dir"]).glob("*/" + spec["file"])]
        entries += [("benchmark", p) for p in Path(definitions.KITS).glob("*/standard.json")]
        self.assertTrue(entries)
        for index, (kind, source) in enumerate(entries):
            with self.subTest(kind=kind, entry=source.parent.name):
                folder = self.tmp / "roundtrips" / str(index) / source.parent.name
                shutil.copytree(source.parent, folder)
                path = folder / source.name
                legacy = definitions.read_document(str(path), kind)
                path.with_name("proofs.json").unlink(missing_ok=True)
                path.write_text(json.dumps(legacy), encoding="utf-8")
                before, bad = definitions._read(str(path), kind, legacy["key"])
                self.assertEqual(bad, [])
                path.write_text(json.dumps(proofs.strip(legacy)), encoding="utf-8")
                self.write_proofs(path, proofs.document(legacy, kind))
                after, bad = definitions._read(str(path), kind, legacy["key"])
                self.assertEqual(bad, [])
                self.assertEqual(after, before)
                for version in definitions.versions(legacy):
                    self.assertEqual(definitions.resolve(after, version), definitions.resolve(before, version))

    def test_proof_save_preserves_authored_bytes_and_all_other_versions(self):
        path = self.manifest("agent", VERSIONED)
        before = path.read_bytes()
        old = self.get("agent", "bot@1.0.0")
        current = self.get("agent", "bot@2.0.0")
        current["summary"] = "A caller's stale description must not be written."
        current["proven"] = {"agent.task/v1": dict(PROOF, version="2.0.0")}
        definitions.save(current)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.get("agent", "bot@1.0.0"), old)
        reloaded = self.get("agent", "bot@2.0.0")
        self.assertEqual(reloaded["proven"], current["proven"])
        self.assertEqual(reloaded["summary"], VERSIONED["summary"])
        self.assertEqual(reloaded["offers"], ["agent.task/v1"])
        written = json.loads(path.with_name("proofs.json").read_text())
        self.assertEqual(written["versions"]["1.0.0"]["proven"], old["proven"])
        self.assertEqual(set(written), {"schema", "kind", "key", "versions"})

    def test_an_external_file_cannot_resurrect_missing_legacy_versions(self):
        path = self.manifest("agent", VERSIONED)
        self.write_proofs(path, self.envelope("agent", VERSIONED, {
            "2.0.0": {"proven": {"agent.task/v1": dict(PROOF, version="2.0.0")}},
        }))
        old = self.get("agent", "bot@1.0.0")
        self.assertEqual(old["proven"], {})
        self.assertEqual(old["offers"], [])
        self.assertTrue(match.fit(BENCH, old))
        self.assertEqual(self.get("agent", "bot@2.0.0")["offers"], ["agent.task/v1"])

    def test_empty_external_evidence_withdraws_flat_legacy_offers(self):
        for kind, value in (("agent", AGENT), ("capability", CAPABILITY)):
            with self.subTest(kind=kind):
                path = self.manifest(kind, value)
                self.write_proofs(path, self.envelope(kind, value, {}))
                got = self.get(kind, value["key"])
                self.assertEqual(got.get("offers") or [], [])
                self.assertEqual(got.get("proven") or {}, {})

    def test_a_user_definition_never_borrows_the_bundled_proof_file(self):
        path = self.manifest("agent", proofs.strip(FLAT))
        evidence = self.write_proofs(path, proofs.document(FLAT, "agent"))
        bundle = self.tmp / "bundle"
        shutil.copytree(path.parent, bundle / "agents" / "flat")
        evidence.unlink()
        with patch.object(definitions, "CATALOGUE", str(bundle)):
            got = definitions.get("agent", "flat")
        self.assertIsNotNone(got)
        self.assertEqual(got["_here"], str(path.parent))
        self.assertEqual(got.get("offers") or [], [])
        self.assertEqual(got.get("proven") or {}, {})

    def test_failed_atomic_replacement_preserves_previous_evidence(self):
        path = self.manifest("agent", FLAT)
        value = proofs.document(FLAT, "agent")
        target = self.write_proofs(path, value)
        before = target.read_bytes()
        with patch.object(proofs.os, "replace", side_effect=OSError("cannot replace")):
            with self.assertRaisesRegex(OSError, "cannot replace"):
                proofs.write(str(path.parent), self.envelope("agent", FLAT, {}))
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["agent.json", "proofs.json"])

    def test_invalid_external_evidence_never_falls_back_to_embedded_proofs(self):
        path = self.manifest("agent", VERSIONED)
        valid = self.envelope("agent", VERSIONED, {
            "1.0.0": {"proven": {"agent.workspace/v1": dict(PROOF, version="1.0.0")}},
        })
        cases = {
            "schema": dict(valid, schema="nullius.proofs/v2"),
            "kind": dict(valid, kind="capability"),
            "key": dict(valid, key="another-agent"),
            "unknown field": dict(valid, extra="ignored?"),
            "versions type": dict(valid, versions=[]),
            "unknown version": dict(valid, versions={"9.0.0": {"proven": {}}}),
            "block type": dict(valid, versions={"1.0.0": []}),
            "unknown block field": dict(valid, versions={"1.0.0": {"proven": {}, "summary": "ignored?"}}),
            "proven type": dict(valid, versions={"1.0.0": {"proven": []}}),
            "record version": dict(valid, versions={"1.0.0": {"proven": {
                "agent.workspace/v1": dict(PROOF, version="2.0.0"),
            }}}),
        }
        for label, value in cases.items():
            with self.subTest(problem=label):
                self.write_proofs(path, value)
                got, bad = definitions.load("agent", str(self.root))
                self.assertEqual(got, [])
                self.assertTrue(bad)
                self.assertIsNone(definitions.get("agent", "bot", str(self.root)))
        target = path.with_name("proofs.json")
        for content in ("{", "[]", json.dumps(valid).replace('"key": "bot"', '"key": "wrong", "key": "bot"')):
            with self.subTest(content=content):
                target.write_text(content, encoding="utf-8")
                got, bad = definitions.load("agent", str(self.root))
                self.assertEqual(got, [])
                self.assertTrue(bad)

    def test_a_corrupt_proof_file_is_not_overwritten_by_a_stale_writer(self):
        path = self.manifest("agent", FLAT)
        loaded = self.get("agent", "flat")
        target = path.with_name("proofs.json")
        target.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises((OSError, ValueError)):
            definitions.save(loaded)
        self.assertEqual(target.read_text(), "{broken")
        self.assertEqual(path.read_bytes(), before)

    def test_reproving_a_flat_legacy_entry_keeps_other_stale_records_visible(self):
        bumped = copy.deepcopy(FLAT)
        bumped["upstream"]["version"] = "0.4.0"
        bumped["install"] = {"npm": ["flat-acp@0.4.0"]}
        path = self.manifest("agent", bumped)
        before = path.read_bytes()
        loaded = self.get("agent", "flat")
        reason = match.fit(BENCH, loaded)
        self.assertTrue(any("passed on version 0.3.0, not 0.4.0" in r for r in reason))
        definitions.save(loaded, lambda cur: cur.update(proven=dict(
            cur["proven"], fidelity={"verdict": "same", "version": "0.4.0"})))
        reloaded = self.get("agent", "flat")
        self.assertEqual(reloaded["proven"]["fidelity"]["version"], "0.4.0")
        self.assertEqual(reloaded["proven"]["agent.workspace/v1"], FLAT["proven"]["agent.workspace/v1"])
        self.assertEqual(match.fit(BENCH, reloaded), reason)
        self.assertEqual(path.read_bytes(), before)

    def test_a_stale_save_cannot_recreate_a_deleted_definition(self):
        path = self.manifest("agent", FLAT)
        loaded = self.get("agent", "flat")
        path.unlink()
        with self.assertRaises((OSError, ValueError)):
            definitions.save(loaded)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name("proofs.json").exists())

    def test_a_stale_save_cannot_write_a_removed_or_changed_version(self):
        for value, ref in ((VERSIONED, "bot@2.0.0"), (FLAT, "flat")):
            with self.subTest(ref=ref):
                path = self.manifest("agent", value)
                loaded = self.get("agent", ref)
                edited = copy.deepcopy(value)
                if "versions" in edited:
                    del edited["versions"]["2.0.0"]
                else:
                    edited["upstream"]["version"] = "0.4.0"
                path.write_text(json.dumps(edited), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises((OSError, ValueError)):
                    definitions.save(loaded)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_name("proofs.json").exists())

    def test_overlapping_processes_merge_latest_evidence_under_one_lock(self):
        path = self.manifest("agent", VERSIONED)
        before = path.read_bytes()
        script = """
import sys, time
from nullius.standard.contract import definitions, proofs
root, port = sys.argv[1:]
d = definitions.get('agent', 'bot@2.0.0', root)
assert d is not None
print('ready', flush=True)
sys.stdin.readline()
def change(current):
    existing = dict(current.get('proven') or {})
    time.sleep(0.1)
    existing[port] = {'passed': '1/1', 'on': '2026-11-19', 'version': '2.0.0'}
    current['proven'] = existing
definitions.save(d, change)
"""
        children = []
        try:
            for port in ("agent.task/v1", "agent.workspace/v1"):
                children.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(self.root), port],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, PYTHONPATH=str(APP)), cwd=self.tmp,
                ))
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), "ready")
            for child in children:
                child.stdin.write("\n")
                child.stdin.flush()
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, output + error)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        got = self.get("agent", "bot@2.0.0")
        self.assertEqual(set(got["proven"]), {"agent.task/v1", "agent.workspace/v1"})
        self.assertEqual(self.get("agent", "bot@1.0.0")["proven"], VERSIONED["versions"]["1.0.0"]["proven"])
        self.assertEqual(path.read_bytes(), before)
