"""Release downloads must match their tag and pass verification before export."""
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'packaging/release.py'
SPEC = importlib.util.spec_from_file_location('release_downloads', SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
VERSION = '0.1.0rc1'
TAG = 'v' + VERSION


def metadata(version=VERSION, name='nullius-eval', private=False):
    content = f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n'
    if private:
        content += 'Classifier: Private :: Do Not Upload\n'
    return content.encode()


def artifacts(directory, wheel_metadata=None, source_metadata=None):
    wheel = directory / f'nullius_eval-{VERSION}-py3-none-any.whl'
    source = directory / f'nullius_eval-{VERSION}.tar.gz'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr(f'nullius_eval-{VERSION}.dist-info/METADATA',
                         metadata() if wheel_metadata is None else wheel_metadata)
    content = metadata() if source_metadata is None else source_metadata
    with tarfile.open(source, 'w:gz') as archive:
        member = tarfile.TarInfo(f'nullius_eval-{VERSION}/PKG-INFO')
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return wheel, source


class ReleaseDownloads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nullius-release-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def project(self):
        (self.root / 'pyproject.toml').write_text(
            f'[project]\nname = "nullius-eval"\nversion = "{VERSION}"\n')
        notes = self.root / 'docs/releases' / (TAG + '.md')
        notes.parent.mkdir(parents=True)
        notes.write_text('Initial release candidate.\n')
        return notes

    def test_candidate_requires_matching_version_and_authored_notes(self):
        notes = self.project()
        self.assertEqual(release.candidate(TAG, self.root), VERSION)
        with self.assertRaisesRegex(ValueError, 'must match'):
            release.candidate('v0.1.0rc2', self.root)
        notes.write_text(' \n')
        with self.assertRaisesRegex(ValueError, 'Missing release notes'):
            release.candidate(TAG, self.root)
        notes.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing release notes'):
            release.candidate(TAG, self.root)

    def test_candidate_refuses_malformed_and_path_shaped_tags(self):
        for tag in ('0.1.0rc1', 'v0.1.0rc0', 'v0.1.0rc1/../notes', 'v0.1', 'v0.1.0.post1',
                    '../v0.1.0rc1', 'v0.1.0rc1\n', 'v0.1.0\n'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'release tag'):
                release.candidate(tag, self.root)

    def test_a_final_version_is_a_release_and_an_rc_a_prerelease(self):
        self.assertTrue(release.prerelease('v0.1.0rc1'))
        self.assertFalse(release.prerelease('v0.1.0'))
        (self.root / 'pyproject.toml').write_text(
            '[project]\nname = "nullius-eval"\nversion = "0.1.0"\n')
        notes = self.root / 'docs/releases/v0.1.0.md'
        notes.parent.mkdir(parents=True)
        notes.write_text('First release.\n')
        self.assertEqual(release.candidate('v0.1.0', self.root), '0.1.0')

    def test_distributions_accepts_only_the_requested_pair(self):
        pair = artifacts(self.root)
        self.assertEqual(release.distributions(self.root, VERSION), pair)
        extra = self.root / 'nullius_eval-0.0.9-py3-none-any.whl'
        extra.write_bytes(b'stale wheel')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            release.distributions(self.root, VERSION)
        extra.unlink()
        pair[1].unlink()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            release.distributions(self.root, VERSION)

    def test_archive_names_cannot_hide_wrong_package_metadata(self):
        for bad_metadata in (metadata(version='0.0.9'), metadata(name='another-project'),
                             metadata(name='nullius')):
            for argument in ('wheel_metadata', 'source_metadata'):
                with self.subTest(archive=argument, metadata=bad_metadata):
                    artifacts(self.root, **{argument: bad_metadata})
                    with self.assertRaisesRegex(ValueError, 'does not match'):
                        release.distributions(self.root, VERSION)

    def test_both_distributions_reject_private_classifiers(self):
        for argument in ('wheel_metadata', 'source_metadata'):
            for classifier in (b'Private :: Do Not Upload', b'Private :: Internal'):
                with self.subTest(archive=argument, classifier=classifier):
                    private = metadata(private=True).replace(b'Private :: Do Not Upload', classifier)
                    artifacts(self.root, **{argument: private})
                    with self.assertRaisesRegex(ValueError, 'Private :: classifier'):
                        release.distributions(self.root, VERSION)

    def test_checksum_file_uses_sha256_and_download_basenames(self):
        empty = self.root / 'a.tar.gz'
        abc = self.root / 'b.whl'
        empty.write_bytes(b'')
        abc.write_bytes(b'abc')
        self.assertEqual(release.checksums((abc, empty)),
                         'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  a.tar.gz\n'
                         'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  b.whl\n')

    def test_verify_downloads_accepts_both_manifest_orders(self):
        pair = artifacts(self.root)
        manifest = self.root / 'SHA256SUMS'
        lines = release.checksums(pair).splitlines()
        for order in (lines, list(reversed(lines))):
            manifest.write_text('\n'.join(order) + '\n')
            self.assertEqual(release.verify_downloads(self.root, VERSION), pair)

    def test_verify_downloads_rejects_missing_or_extra_assets(self):
        pair = artifacts(self.root)
        manifest = self.root / 'SHA256SUMS'
        with self.assertRaisesRegex(ValueError, 'exactly the release'):
            release.verify_downloads(self.root, VERSION)
        manifest.write_text(release.checksums(pair))
        extra = self.root / 'extra.whl'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'exactly the release'):
            release.verify_downloads(self.root, VERSION)
        extra.unlink()
        pair[0].unlink()
        with self.assertRaisesRegex(ValueError, 'exactly the release'):
            release.verify_downloads(self.root, VERSION)

    def test_verify_downloads_rejects_ambiguous_or_path_shaped_entries(self):
        pair = artifacts(self.root)
        manifest = self.root / 'SHA256SUMS'
        valid = release.checksums(pair)
        first = valid.splitlines()[0]
        cases = (valid + first + '\n', first + '\n', '',
                 valid + '0' * 64 + '  other.whl\n',
                 valid.replace(pair[0].name, '../' + pair[0].name),
                 valid.replace(pair[0].name, '/' + pair[0].name),
                 valid.replace('  ', ' *'), 'not a checksum\n')
        for content in cases:
            with self.subTest(content=content):
                manifest.write_text(content)
                with self.assertRaisesRegex(ValueError, 'SHA256SUMS'):
                    release.verify_downloads(self.root, VERSION)

    def test_verify_downloads_checks_bytes_before_opening_archives(self):
        pair = artifacts(self.root)
        (self.root / 'SHA256SUMS').write_text(release.checksums(pair))
        pair[0].write_bytes(b'corrupted wheel')
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            release.verify_downloads(self.root, VERSION)

    def test_correct_hashes_do_not_bypass_metadata_validation(self):
        pair = artifacts(self.root, source_metadata=metadata(version='0.0.9'))
        (self.root / 'SHA256SUMS').write_text(release.checksums(pair))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            release.verify_downloads(self.root, VERSION)

    def test_verify_command_does_not_build_or_install(self):
        pair = artifacts(self.root)
        (self.root / 'SHA256SUMS').write_text(release.checksums(pair))
        with patch('sys.argv', ['release.py', 'verify', '--tag', TAG,
                                '--dist-dir', str(self.root)]), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch.object(release, 'candidate', return_value=VERSION), \
                patch.object(release.subprocess, 'run') as run:
            release.main()
        run.assert_not_called()
        self.assertIn('Verified release downloads:', output.getvalue())

    def test_existing_downloads_are_not_overwritten(self):
        existing = self.root / 'already-verified.whl'
        existing.write_bytes(b'keep this download')
        with patch.object(release, 'candidate', return_value=VERSION), \
                patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'absent or empty'):
                release.build(TAG, self.root)
        run.assert_not_called()
        self.assertEqual(existing.read_bytes(), b'keep this download')

    def test_failed_build_or_install_verification_exports_no_downloads(self):
        for fail_verification in (False, True):
            with self.subTest(fail_verification=fail_verification):
                output = self.root / 'downloads'
                def run(command, **kwargs):
                    if fail_verification and '--out-dir' in command:
                        artifacts(Path(command[command.index('--out-dir') + 1]))
                        return subprocess.CompletedProcess(command, 0)
                    raise subprocess.CalledProcessError(1, command)
                with patch.object(release, 'candidate', return_value=VERSION), \
                        patch.object(release.shutil, 'which', return_value='/test/uv'), \
                        patch.object(release.subprocess, 'run', side_effect=run):
                    with self.assertRaises(subprocess.CalledProcessError):
                        release.build(TAG, output)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
