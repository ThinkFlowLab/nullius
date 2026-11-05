"""The publisher must reuse a published release's exact, verified downloads."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'packaging/publish.py'
SPEC = importlib.util.spec_from_file_location('publish_downloads', SCRIPT)
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)
VERSION = '0.1.0rc1'
TAG = 'v' + VERSION
REPOSITORY = 'thinkflowlab/nullius'
WHEEL = f'nullius_eval-{VERSION}-py3-none-any.whl'
SOURCE = f'nullius_eval-{VERSION}.tar.gz'


class PublishDownloads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nullius-publish-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'downloads'
        self.data = {
            'tag_name': TAG, 'draft': False, 'prerelease': True,
            'assets': [{'id': identifier, 'name': name, 'state': 'uploaded',
                        'browser_download_url': 'https://untrusted.invalid/ignored'}
                       for identifier, name in enumerate((WHEEL, SOURCE, 'SHA256SUMS'), 10)],
        }
        content = f'Metadata-Version: 2.4\nName: nullius-eval\nVersion: {VERSION}\n'.encode()
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr(f'nullius_eval-{VERSION}.dist-info/METADATA', content)
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode='w:gz') as archive:
            member = tarfile.TarInfo(f'nullius_eval-{VERSION}/PKG-INFO')
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        self.downloads = {10: wheel.getvalue(), 11: source.getvalue()}
        self.downloads[12] = ''.join(
            f'{hashlib.sha256(self.downloads[identifier]).hexdigest()}  {name}\n'
            for identifier, name in ((10, WHEEL), (11, SOURCE))).encode()
        self.requests = []
        candidate = patch.object(publish.release, 'candidate', return_value=VERSION)
        candidate.start()
        self.addCleanup(candidate.stop)
        metadata = patch.object(publish.subprocess, 'check_output', side_effect=self.metadata)
        self.metadata_request = metadata.start()
        self.addCleanup(metadata.stop)
        downloads = patch.object(publish.subprocess, 'run', side_effect=self.download)
        self.download_request = downloads.start()
        self.addCleanup(downloads.stop)

    def metadata(self, command, *, text):
        self.assertEqual(command, ['gh', 'api', f'repos/{REPOSITORY}/releases/tags/{TAG}'])
        self.assertTrue(text)
        return json.dumps(self.data)

    def download(self, command, *, stdout, check):
        self.assertEqual(command[:2], ['gh', 'api'])
        self.assertEqual(command[3:], ['-H', 'Accept: application/octet-stream'])
        self.assertTrue(check)
        prefix = f'repos/{REPOSITORY}/releases/assets/'
        self.assertTrue(command[2].startswith(prefix))
        identifier = int(command[2].removeprefix(prefix))
        self.requests.append(identifier)
        stdout.write(self.downloads[identifier])

    def test_preparation_exports_verified_bytes_and_hashes_from_asset_ids(self):
        result = publish.prepare(REPOSITORY, TAG, self.output)
        self.assertEqual(self.requests, [10, 11, 12])
        self.assertEqual(set(path.name for path in self.output.iterdir()),
                         {WHEEL, SOURCE, 'SHA256SUMS'})
        self.assertEqual(result['version'], VERSION)
        for label, identifier, name in (('wheel', 10, WHEEL), ('source', 11, SOURCE)):
            self.assertEqual((self.output / name).read_bytes(), self.downloads[identifier])
            self.assertEqual(result[f'{label}_id'], str(identifier))
            self.assertEqual(result[f'{label}_name'], name)
            self.assertEqual(result[f'{label}_sha256'],
                             hashlib.sha256(self.downloads[identifier]).hexdigest())

    def test_draft_stable_and_other_tags_are_rejected_before_download(self):
        original = self.data
        for changes in ({'draft': True}, {'draft': None}, {'prerelease': False},
                        {'prerelease': None}, {'tag_name': 'v0.1.0rc2'}):
            with self.subTest(changes=changes):
                self.data = {**original, **changes}
                with self.assertRaisesRegex(ValueError, 'published GitHub prerelease'):
                    publish.prepare(REPOSITORY, TAG, self.output)
        self.download_request.assert_not_called()

    def test_missing_extra_and_duplicate_assets_are_rejected(self):
        assets = self.data['assets']
        for entries in (assets[:-1], assets + [assets[0]],
                        [assets[0], assets[0], assets[2]], None):
            with self.subTest(assets=entries), self.assertRaises(ValueError):
                publish.assets_for({**self.data, 'assets': entries}, TAG, VERSION)

    def test_asset_names_ids_and_upload_state_are_checked(self):
        for changes in ({'name': '../outside.whl'}, {'name': []}, {'name': 'nullius.whl'},
                        {'id': '10'}, {'id': True}, {'id': 0}, {'id': 11},
                        {'state': 'new'}):
            with self.subTest(changes=changes):
                data = copy.deepcopy(self.data)
                data['assets'][0].update(changes)
                with self.assertRaises(ValueError):
                    publish.assets_for(data, TAG, VERSION)

    def test_corrupt_downloads_are_not_exported(self):
        self.downloads[10] += b'changed after checksums were recorded'
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            publish.prepare(REPOSITORY, TAG, self.output)
        self.assertFalse(self.output.exists())

    def test_api_and_download_errors_export_nothing(self):
        self.metadata_request.side_effect = subprocess.CalledProcessError(1, ['gh', 'api'])
        with self.assertRaises(subprocess.CalledProcessError):
            publish.prepare(REPOSITORY, TAG, self.output)
        self.download_request.assert_not_called()
        self.metadata_request.side_effect = self.metadata
        self.download_request.side_effect = subprocess.CalledProcessError(1, ['gh', 'api'])
        with self.assertRaises(subprocess.CalledProcessError):
            publish.prepare(REPOSITORY, TAG, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_repository_and_nonempty_output_fail_before_network(self):
        for repository in ('../outside', 'owner/..', 'owner/.', 'owner/repo/extra',
                           'owner/repo\n', '-R owner/repo'):
            with self.subTest(repository=repository), self.assertRaisesRegex(ValueError, 'owner/name'):
                publish.prepare(repository, TAG, self.output)
        self.output.mkdir()
        existing = self.output / 'keep.whl'
        existing.write_bytes(b'previous download')
        with self.assertRaisesRegex(ValueError, 'absent or empty'):
            publish.prepare(REPOSITORY, TAG, self.output)
        self.assertEqual(existing.read_bytes(), b'previous download')
        self.metadata_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
