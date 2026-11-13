"""Download and check a published GitHub release; never upload to PyPI.

The publishing workflow uses these verified asset IDs and hashes to retrieve
the same bytes again in its separate, credentialed publishing job.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SPEC = importlib.util.spec_from_file_location('release_downloads',
                                             Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def assets_for(data: dict, tag: str, version: str) -> dict[str, int]:
    """Accept only the published release, a prerelease exactly when the tag is an
    RC, and exactly its three expected downloads."""
    if (data.get('tag_name') != tag or data.get('draft') is not False
            or data.get('prerelease') is not release.prerelease(tag)):
        raise ValueError('Expected the published GitHub release for the exact tag, '
                         'a prerelease only for a release candidate')
    expected = {f'nullius_eval-{version}-py3-none-any.whl',
                f'nullius_eval-{version}.tar.gz', 'SHA256SUMS'}
    assets = data.get('assets')
    if not isinstance(assets, list) or len(assets) != len(expected):
        raise ValueError('Expected exactly the release wheel, source archive and SHA256SUMS')
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError('Invalid release asset')
        name, identifier = asset.get('name'), asset.get('id')
        if (not isinstance(name, str) or name not in expected or name in selected
                or type(identifier) is not int or identifier < 1
                or identifier in selected.values() or asset.get('state') != 'uploaded'):
            raise ValueError('Invalid, unexpected or duplicate release asset')
        selected[name] = identifier
    return selected


def prepare(repository: str, tag: str, output: Path) -> dict[str, str]:
    """Download by API asset ID, verify, then export files and immutable inputs."""
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repository)
            or repository.rsplit('/', 1)[-1] in ('.', '..')):
        raise ValueError('Expected a GitHub repository in owner/name form')
    version = release.candidate(tag)
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Output directory must be absent or empty; existing downloads are not replaced')
    response = subprocess.check_output(
        ['gh', 'api', f'repos/{repository}/releases/tags/{tag}'], text=True)
    data = json.loads(response)
    if not isinstance(data, dict):
        raise ValueError('Invalid GitHub release response')
    selected = assets_for(data, tag, version)
    with tempfile.TemporaryDirectory(prefix='nullius-publish-') as temporary:
        directory = Path(temporary)
        for name, identifier in selected.items():
            with (directory / name).open('wb') as stream:
                subprocess.run(
                    ['gh', 'api', f'repos/{repository}/releases/assets/{identifier}',
                     '-H', 'Accept: application/octet-stream'], stdout=stream, check=True)
        wheel, source = release.verify_downloads(directory, version)
        result = {'version': version}
        for label, path in (('wheel', wheel), ('source', source)):
            result.update({f'{label}_id': str(selected[path.name]),
                           f'{label}_name': path.name,
                           f'{label}_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        output.mkdir(parents=True, exist_ok=True)
        for path in (wheel, source, directory / 'SHA256SUMS'):
            shutil.copy2(path, output / path.name)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.repository, args.tag, args.out_dir)
        if args.github_output:
            with args.github_output.open('a') as stream:
                stream.writelines(f'{key}={value}\n' for key, value in result.items())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Publication preparation failed: {exc}\n')
    print(f'Checked published release downloads: {args.out_dir.resolve()}')


if __name__ == '__main__':
    main()
