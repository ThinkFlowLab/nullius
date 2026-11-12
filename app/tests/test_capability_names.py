"""A vocabulary change must not lose user entries or their existing proofs."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from nullius.standard import cli, definitions, match, names, payload, ports
from .test_standard_definitions import BENCH, CAPABILITY


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.user = self.root / 'user'
        self.bundle = self.root / 'bundle'
        self.enterContext(patch.dict(os.environ, {'NULLIUS_HOME': str(self.root / 'profile'),
                                                 'NULLIUS_CATALOGUE': str(self.user)}))
        self.enterContext(patch.object(definitions, 'CATALOGUE', str(self.bundle)))

    def write(self, base, group, name, value):
        folder = base / group / value['key']
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(value))
        return folder

    def legacy(self):
        value = copy.deepcopy(CAPABILITY)
        value['schema'] = 'nullius.feature/v1'
        value['start']['command'][-1] = '{here}/feature.py'
        value['offers'] = ['feature.memory/v1']
        value['proven'] = {'feature.memory/v1': value['proven']['cap.memory/v1']}
        folder = self.write(self.user, 'features', 'feature.json', value)
        (folder / 'feature.py').write_text('print("unchanged wrapper")\n')
        return folder, value

    def test_old_entry_and_proof_are_read_without_rewriting_files(self):
        folder, old = self.legacy()
        before = (folder / 'feature.json').read_bytes()
        got = definitions.get('capability', 'mem')
        self.assertEqual(got['_kind'], 'capability')
        self.assertEqual(got['offers'], ['cap.memory/v1'])
        self.assertEqual(got['proven']['cap.memory/v1'], old['proven']['feature.memory/v1'])
        self.assertEqual(got['start']['command'][-1], '{here}/feature.py')
        self.assertEqual(definitions.get('feature', 'mem'), got)
        self.assertEqual((folder / 'feature.json').read_bytes(), before)
        bench = dict(BENCH, family='feature', needs=['feature.memory/v1'])
        self.write(self.user, 'benchmarks', 'standard.json', bench)
        self.assertEqual(match.fit(definitions.get('benchmark', 'toy'), got), [])
        definitions.save(got)
        self.assertIsNotNone(definitions.get('capability', 'mem'))
        self.assertEqual((folder / 'feature.py').read_text(), 'print("unchanged wrapper")\n')

    def test_user_legacy_folder_overrides_bundle_but_canonical_user_folder_wins(self):
        self.write(self.bundle, 'capabilities', 'capability.json', CAPABILITY)
        folder, _ = self.legacy()
        self.assertEqual(definitions.get('capability', 'mem')['_here'], str(folder))
        canonical = self.write(self.user, 'capabilities', 'capability.json', dict(CAPABILITY, label='New'))
        self.assertEqual(definitions.get('capability', 'mem')['label'], 'New')
        (canonical / 'capability.json').unlink()
        self.assertIsNone(definitions.get('capability', 'mem'))
        self.assertTrue(definitions.load('capability')[1])

    def test_cli_new_capability_and_legacy_alias_create_canonical_definitions(self):
        for word, key in [('capability', 'new-memory'), ('feature', 'old-spelling')]:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['new', word, key]), 0)
            folder = self.user / 'capabilities' / key
            self.assertTrue((folder / 'capability.json').is_file())
            self.assertTrue((folder / 'capability.py').is_file())
        self.assertEqual(ports.family('cap.memory/v1'), 'capability')
        self.assertEqual(names.port('feature.memory/v1'), 'cap.memory/v1')
        self.assertIsNotNone(definitions.kit('feature.memory/v1'))

    def test_snapshot_carries_legacy_wrapper_and_definition(self):
        self.legacy()
        _, snapshot = payload.build(str(self.root / 'cache'), catalogue=str(self.user))
        root = Path(snapshot) / 'app' / 'nullius' / 'catalogue'
        got = definitions.get('capability', 'mem', root=str(root))
        self.assertIsNotNone(got)
        self.assertEqual(got['offers'], ['cap.memory/v1'])
        self.assertTrue((Path(got['_here']) / 'feature.py').is_file())

    def test_versioned_proofs_and_options_are_preserved(self):
        value = {'schema': 'nullius.feature/v1', 'versions': {'1': {'proven': {'feature.memory/v1': {'passed': '3/3'}}}},
                 'options': {'flavour': {'default': 'feature'}}}
        got = names.definition(value)
        self.assertIn('cap.memory/v1', got['versions']['1']['proven'])
        self.assertEqual(got['options'], value['options'])


class Backend(unittest.TestCase):
    """``docker``, the container backend's earlier name, is read as ``container``."""

    def test_docker_reads_as_container_and_other_values_are_kept(self):
        self.assertEqual([names.backend(v) for v in ("docker", "container", "local", "vm", None)],
                         ["container", "container", "local", "vm", None])

    def test_the_cli_takes_either_name_and_refuses_another(self):
        seen = []
        with patch.object(cli, "cmd_run", side_effect=lambda a: seen.append(a.backend)), \
             patch.object(cli, "_exit_on_term"), patch.object(cli.progress, "ECHO"):
            for value in ("container", "docker"):
                cli.main(["run", "fakebench", "fake", "--from-run", "--backend", value])
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                cli.main(["run", "fakebench", "fake", "--from-run", "--backend", "vm"])
        self.assertEqual(seen, ["container", "container"])
