"""Install a private wheel into a temporary environment and check its payload.

    python3 app/packaging/verify.py /path/to/nullius.whl [--offline]

Requires uv. Installs only into the temporary environment; never runs a
benchmark, opens a browser/server, or reads the caller's settings or secrets.
The backend is exercised before Flask is installed, then the installed web
application is rendered through its test client. Nothing is published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


BACKEND = r'''
import importlib.metadata, importlib.util, json, pathlib, sys
import nullius
from nullius.catalogue import catalog
from nullius.standard import definitions
assert importlib.util.find_spec('flask') is None
assert importlib.util.find_spec('harbor') is None
assert importlib.util.find_spec('app') is None
assert importlib.util.find_spec('web') is None
providers = {str((pathlib.Path(p or '.').resolve() / 'nullius')) for p in sys.path
             if (pathlib.Path(p or '.') / 'nullius').is_dir()}
assert providers == {str(pathlib.Path(nullius.__file__).parent)}, providers
metadata = importlib.metadata.metadata('nullius')
assert metadata.get_all('Requires-Dist') == json.loads(sys.argv[1]), metadata.get_all('Requires-Dist')
assert 'Private :: Do Not Upload' in metadata.get_all('Classifier')
assert catalog.load()
for kind in definitions.KINDS:
    entries, problems = definitions.load(kind)
    assert entries, kind
    assert not problems, (kind, problems)
assert (pathlib.Path(nullius.__file__).parent / 'judge' / 'floors.json').is_file()
print(json.dumps({'backend': nullius.__file__, 'providers': len(providers),
                  'version': metadata['Version']}))
'''

WEB = r'''
import hashlib, importlib.metadata, json, pathlib
import _nullius_app.app as web
root = pathlib.Path(web.__file__).parent
dist = importlib.metadata.distribution('nullius')
def digest():
    return {str(p): hashlib.sha256(dist.locate_file(p).read_bytes()).hexdigest()
            for p in dist.files if str(p).endswith(('.py', '.json', '.html', '.css', '.js'))}
before = digest()
web.app.config.update(TESTING=True)
client = web.app.test_client()
pages = ['/', '/benchmarks', '/agents', '/capabilities', '/settings',
         '/static/style.css', '/static/fonts/DMSans-Regular.woff2',
         '/static/fonts/dmsans-OFL.txt', '/static/brand/favicon.svg',
         '/static/brand/typesafe.png']
for page in pages:
    response = client.get(page, follow_redirects=True)
    assert response.status_code == 200, (page, response.status_code, response.data[:500])
    assert response.data, page
assert before == digest(), 'serving mutated installed package files'
assert len(list(web.app.url_map.iter_rules())) > 40
print(json.dumps({'web': str(root), 'pages': pages, 'package_unchanged': True}))
'''

PACKAGE_DIGEST = r'''
import hashlib, importlib.util, json, pathlib
files = {}
for name in ('nullius', '_nullius_app'):
    root = pathlib.Path(next(iter(importlib.util.find_spec(name).submodule_search_locations)))
    for path in root.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            files[name + '/' + path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(files, sort_keys=True))
'''

PROFILE_WRITE = r'''
from nullius import paths, settings
from nullius.catalogue import harbor_entry
settings.save(paths.settings_file(), 'OPENAI_API_KEY', 'sk-' + 'packaging-fixture-' + '0' * 32)
entry = harbor_entry.build({
    'source': 'registry', 'ref': 'packaging-fixture@1.0',
    'key': 'packaging-harbor', 'title': 'Packaging fixture', 'category': 'Coding',
    'domain': 'A synthetic packaging fixture.', 'scoring': 'Tests', 'tasks': '1',
    'verifier': 'A synthetic test; never executed.', 'official': 'https://example.org/fixture',
})
harbor_entry.save(entry)
'''

PROFILE_CHECK = r'''
import json
from nullius import paths, settings
from nullius.catalogue import harbor_entry
assert settings.in_file(paths.settings_file()).get('OPENAI_API_KEY') == 'sk-' + 'packaging-fixture-' + '0' * 32, 'saved fixture credential changed'
entry = harbor_entry.entry_of('packaging-harbor')
assert entry and entry['title'] == 'Packaging fixture', 'saved catalogue addition changed'
print(json.dumps({'settings': paths.settings_file(), 'catalogue': paths.catalogue_dir()}))
'''


def payload(wheel: Path) -> int:
    """Check the archive against the checked-out runtime resource inventory."""
    app = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert {n.split('/')[0] for n in names} == {
            'nullius', '_nullius_app', next(n.split('/')[0] for n in names if '.dist-info/' in n)}
        for tree in ('templates', 'static'):
            for source in (app / tree).rglob('*'):
                if source.is_file():
                    target = '_nullius_app/' + source.relative_to(app).as_posix()
                    assert target in names, f'missing resource: {target}'
                    assert archive.read(target) == source.read_bytes(), target
        for tree in ('catalogue', 'standard/kits'):
            for source in (app / 'nullius' / tree).rglob('*'):
                if source.is_file() and not any(p.startswith('.') or p == '__pycache__'
                                               for p in source.relative_to(app).parts):
                    target = source.relative_to(app).as_posix()
                    assert target in names, f'missing catalogue/kit payload: {target}'
                    assert archive.read(target) == source.read_bytes(), target
        assert any(n.endswith('.dist-info/licenses/LICENSE') for n in names)
        licences = {Path(n).name: n for n in names if '.dist-info/licenses/' in n and n.endswith('-OFL.txt')}
        expected = {p.name: p for p in (app / 'static/fonts').glob('*-OFL.txt')}
        assert licences.keys() == expected.keys(), 'font licence inventory differs'
        for name, source in expected.items():
            assert archive.read(licences[name]) == source.read_bytes(), f'font licence changed: {name}'
        assert not any('/tests/' in n or '/site/' in n or '__pycache__' in n or
                       n.endswith(('.pyc', '/.env', '.pkl')) for n in names), 'unexpected runtime/user data'
        return len(names)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--offline', action='store_true', help='use only cached dependencies')
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve()
    files = payload(wheel)
    requirements = [line.strip() for line in
                    (Path(__file__).resolve().parents[1] / 'requirements.txt').read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
    uv = shutil.which('uv')
    if not uv:
        parser.error('uv must be on PATH')
    # Resolve before HOME changes so --offline can reuse the caller's normal
    # uv cache. This does not inspect application settings or credentials.
    cache = subprocess.run([uv, 'cache', 'dir'], check=True, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='nullius-wheel-') as temporary:
        root = Path(temporary)
        home, profile, cwd = root / 'home', root / 'profile', root / 'cwd'
        for path in (home, cwd):
            path.mkdir()
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home),
               'NULLIUS_HOME': str(profile), 'PYTHONDONTWRITEBYTECODE': '1',
               'UV_PYTHON_DOWNLOADS': 'never',
               'UV_TOOL_DIR': str(root / 'tools'), 'UV_TOOL_BIN_DIR': str(root / 'bin'),
               'UV_CACHE_DIR': cache}
        if os.name == 'nt':
            env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        def run(*command, expected=0):
            result = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                                    capture_output=True, text=True)
            if result.returncode != expected:
                raise RuntimeError(f'{command}: exit {result.returncode}\n{result.stdout}\n{result.stderr}')
            return result.stdout
        run(uv, 'venv', '--python', sys.executable, root / 'venv')
        binaries = root / 'venv' / ('Scripts' if os.name == 'nt' else 'bin')
        python = binaries / ('python.exe' if os.name == 'nt' else 'python')
        command = binaries / ('nullius.exe' if os.name == 'nt' else 'nullius')
        options = ['--offline'] if args.offline else []
        run(uv, 'pip', 'install', '--python', python, '--no-deps', *options, wheel)
        backend = json.loads(run(python, '-I', '-c', BACKEND, json.dumps(requirements)))
        for prefix in ((python, '-I', '-m', 'nullius'), (command,)):
            assert 'serve' in run(*prefix, '--help')
            assert set(json.loads(run(*prefix, 'import', '--list', '--json'))) == {'atif', 'otel'}
            assert 'nullius ' in run(*prefix, '--version')
            for subcommand in ('runs', 'compare', 'standard', 'workspace', 'paths', 'migrate'):
                assert 'usage:' in run(*prefix, subcommand, '--help')
            run(*prefix, 'import', root / 'missing', expected=3)
        locations = json.loads(run(command, 'paths', '--json'))
        assert all(Path(path).is_relative_to(profile) for path in locations.values()), locations
        run(command, 'standard', 'new', 'agent', 'packaging-fixture')
        assert (profile / 'data/catalogue/agents/packaging-fixture/agent.json').is_file()
        run(uv, 'pip', 'install', '--python', python, *options, wheel)
        assert 'usage:' in run(command, 'serve', '--help')
        web = json.loads(run(python, '-I', '-c', WEB))
        run(uv, 'tool', 'install', '--python', sys.executable, *options, wheel)
        tool = root / 'bin' / command.name
        tool_python = root / 'tools' / 'nullius' / binaries.name / python.name
        assert 'nullius ' in run(tool, '--version')
        assert 'usage:' in run(tool, 'standard', '--help')
        assert set(json.loads(run(tool, 'import', '--list', '--json'))) == {'atif', 'otel'}
        package_before = run(tool_python, '-I', '-c', PACKAGE_DIGEST)
        run(tool_python, '-I', '-c', PROFILE_WRITE)
        installed_profile = run(tool_python, '-I', '-c', PROFILE_CHECK)
        source_app = str(Path(__file__).resolve().parents[1])
        source_check = f'import sys; sys.path.insert(0, {source_app!r})\n' + PROFILE_CHECK
        assert run(sys.executable, '-I', '-c', source_check) == installed_profile
        def profile_digest():
            return {p.relative_to(profile).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in profile.rglob('*') if p.is_file()}
        saved = profile_digest()
        assert run(tool_python, '-I', '-c', PACKAGE_DIGEST) == package_before
        run(uv, 'tool', 'install', '--force', '--python', sys.executable, *options, wheel)
        assert profile_digest() == saved, 'tool replacement changed user storage'
        assert run(tool_python, '-I', '-c', PROFILE_CHECK) == installed_profile
        assert run(sys.executable, '-I', '-c', source_check) == installed_profile
        assert run(tool_python, '-I', '-c', PACKAGE_DIGEST) == package_before
        assert 'usage:' in run(tool, 'standard', '--help')
        print(json.dumps({'wheel': str(wheel), 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                          'files': files, 'backend': backend, 'web': web, 'uv_tool': True,
                          'profile_preserved_after_replacement': True,
                          'source_and_installed_share_profile': True}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
