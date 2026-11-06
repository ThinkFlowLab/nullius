"""Build and verify a release candidate's downloads; never upload or publish.

    python3 app/packaging/release.py build --tag v0.1.0rc1 --out-dir /tmp/nullius-release
    python3 app/packaging/release.py verify --tag v0.1.0rc1 --dist-dir /tmp/nullius-release

uv builds the wheel from the source archive, then verify.py exercises its
installed commands, pages and user storage. Only verified downloads and their
checksums are copied into the requested empty output directory.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
NAME = 'nullius-eval'
ARCHIVE_NAME = 'nullius_eval'


def candidate(tag: str, root: Path = ROOT) -> str:
    """Require an explicit RC tag, matching project metadata and authored notes."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+rc[1-9]\d*", tag):
        raise ValueError("Expected a release-candidate tag such as v0.1.0rc1")
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    if project['name'] != NAME or tag != 'v' + project['version']:
        raise ValueError("Release tag must match the Nullius version in pyproject.toml")
    notes = root / 'docs/releases' / (tag + '.md')
    if not notes.is_file() or not notes.read_text().strip():
        raise ValueError(f"Missing release notes: {notes}")
    return project['version']


def _files(directory: Path, version: str) -> tuple[Path, Path]:
    return (directory / f'{ARCHIVE_NAME}-{version}-py3-none-any.whl',
            directory / f'{ARCHIVE_NAME}-{version}.tar.gz')


def _metadata(files: tuple[Path, Path], version: str) -> None:
    wheel, source = files
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(f'{ARCHIVE_NAME}-{version}.dist-info/METADATA')
        with tarfile.open(source, 'r:gz') as archive:
            member = archive.extractfile(f'{ARCHIVE_NAME}-{version}/PKG-INFO')
            if member is None:
                raise ValueError("Source archive has no package metadata")
            source_metadata = member.read()
    except (zipfile.BadZipFile, tarfile.TarError, KeyError, EOFError) as exc:
        raise ValueError("Invalid distribution archive metadata") from exc
    for content in (metadata, source_metadata):
        parsed = BytesParser().parsebytes(content)
        if parsed['Name'] != NAME or parsed['Version'] != version:
            raise ValueError("Distribution metadata does not match the release version")
        if any(value.strip().startswith('Private ::')
               for value in parsed.get_all('Classifier', [])):
            raise ValueError("Distribution contains a Private :: classifier and cannot be published")


def distributions(directory: Path, version: str) -> tuple[Path, Path]:
    """Reject missing, extra or incorrectly versioned build outputs."""
    files = _files(directory, version)
    if set(directory.iterdir()) != set(files):
        raise ValueError("Expected exactly one Nullius wheel and source archive for this version")
    _metadata(files, version)
    return files


def checksums(files: tuple[Path, ...]) -> str:
    return ''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
                   for path in sorted(files))


def verify_downloads(directory: Path, version: str) -> tuple[Path, Path]:
    """Check the exact release asset set, its hashes, then package metadata."""
    files = _files(directory, version)
    manifest = directory / 'SHA256SUMS'
    if set(directory.iterdir()) != {*files, manifest}:
        raise ValueError("Expected exactly the release wheel, source archive and SHA256SUMS")
    if any(path.is_symlink() or not path.is_file() for path in (*files, manifest)):
        raise ValueError("Release downloads must be regular files")
    recorded = {}
    for line in manifest.read_text(encoding='ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if match is None or match[2] in recorded:
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        recorded[match[2]] = match[1]
    if set(recorded) != {path.name for path in files}:
        raise ValueError("SHA256SUMS must name exactly the two release distributions")
    for path in files:
        if hashlib.sha256(path.read_bytes()).hexdigest() != recorded[path.name]:
            raise ValueError(f"Checksum mismatch: {path.name}")
    _metadata(files, version)
    return files


def build(tag: str, output: Path) -> None:
    version = candidate(tag)
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output directory must be absent or empty; existing downloads are not replaced")
    uv = shutil.which('uv')
    if not uv:
        raise ValueError("uv is required to build and verify release downloads")
    with tempfile.TemporaryDirectory(prefix='nullius-release-') as temporary:
        directory = Path(temporary)
        # Without --wheel/--sdist, uv builds the sdist first and the wheel from it.
        subprocess.run([uv, 'build', '--no-sources', '--no-create-gitignore',
                        '--out-dir', str(directory), str(ROOT)], check=True)
        files = distributions(directory, version)
        subprocess.run([sys.executable, str(ROOT / 'app/packaging/verify.py'),
                        str(files[0])], check=True)
        (directory / 'SHA256SUMS').write_text(checksums(files))
        output.mkdir(parents=True, exist_ok=True)
        for path in (*files, directory / 'SHA256SUMS'):
            shutil.copy2(path, output / path.name)
    print(f'Verified release downloads: {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build', help='build and verify downloads locally')
    command.add_argument('--tag', required=True)
    command.add_argument('--out-dir', required=True, type=Path)
    command = commands.add_parser('verify', help='check downloaded release assets without installing')
    command.add_argument('--tag', required=True)
    command.add_argument('--dist-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            build(args.tag, args.out_dir)
        else:
            verify_downloads(args.dist_dir, candidate(args.tag))
            print(f'Verified release downloads: {args.dist_dir.resolve()}')
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Release preparation failed: {exc}\n')


if __name__ == '__main__':
    main()
