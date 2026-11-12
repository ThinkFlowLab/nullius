"""Original setup containers keep credentials scoped and relay doors temporary."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard import container, setup_container, jsonl
from tests import docker_tier

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DriverContainer(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(container, "_security", ""))
        self.enterContext(patch.object(container, "DOCKER", "docker"))

    def test_podman_keeps_its_host_home_and_uses_the_dns_bridge(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(root + "/.work")
            with patch.object(setup_container.install, "HOME", root + "/installs"):
                driver = setup_container.Driver({"_here": root + "/benchmark", "key": "fixture"},
                    image="img", app="/source/app", job="fixture", work=root, upstream=None, data=None, needs=[])
            with patch.object(container, "DOCKER", "podman"), \
                    patch.dict(os.environ, {"HOME": "/host-home"}):
                argv, env, _ = driver.command({"HOME": root + "/driver-home"}, root)
            self.assertEqual(env["HOME"], "/host-home")
            self.assertIn("HOME=" + root + "/driver-home", argv)
            self.assertEqual(argv[argv.index("--network") + 1], container.BRIDGE)

    def test_mounts_and_environment_do_not_include_personal_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(root + '/.work')
            bench = {'_here': root + '/benchmark', 'key': 'fixture'}
            with patch.object(setup_container.install, 'HOME', root + '/installs'):
                driver = setup_container.Driver(bench, image='nullius/setup:test', app='/source/app',
                    job='fixture', work=root, upstream='/upstream', data='/data/tasks.json', needs=[])
            argv, env, _ = driver.command({'HF_TOKEN': 'fixture-only'}, root, files=['/licence/key', '/licence/key'])
            self.assertNotIn('fixture-only', ' '.join(argv))
            self.assertEqual(env['HF_TOKEN'], 'fixture-only')
            self.assertEqual(env['PATH'], os.environ['PATH'])
            self.assertIn('PATH=' + setup_container.container.IMAGE_PATH, argv)
            mounts = [argv[i + 1] for i, a in enumerate(argv) if a == '-v']
            self.assertIn('/source/app/nullius:/source/app/nullius:ro', mounts)
            self.assertEqual(mounts.count('/licence/key:/licence/key:ro'), 1)
            self.assertIn('/data/tasks.json:/data/tasks.json:ro', mounts)
            self.assertNotIn('/source/app:/source/app:ro', mounts)
            self.assertIn('/docker-setups/test/', env['UV_PYTHON_INSTALL_DIR'])
            self.assertNotIn('MODEL_API_KEY', env)

    def test_nested_relay_urls_are_forwarded_and_closed_on_failure(self):
        doors = []
        class Door:
            def __init__(self, url, **kw):
                self.container = None
                self.base = url.replace('127.0.0.1', 'host.docker.internal')
                self.closed = False
                doors.append(self)
            def start(self): return self
            def stop(self): self.closed = True
        original = {'base_url': 'http://127.0.0.1:345/v1',
                    'judge': {'base_url': 'http://127.0.0.1:678/v1'},
                    'prompt': 'http://127.0.0.1:345/v1'}
        proc = setup_container.Process([], '/', {}, '/tmp/unused', name='fixture', doors=[])
        with patch.object(setup_container.container, 'Loopback', Door), \
             patch.object(jsonl.Proc, 'request', side_effect=RuntimeError('fixture')) as request:
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                proc.request('answer', **original)
        sent = request.call_args.kwargs
        self.assertIn('host.docker.internal', sent['base_url'])
        self.assertIn('host.docker.internal', sent['judge']['base_url'])
        self.assertEqual(sent['prompt'], original['prompt'])
        self.assertIn('127.0.0.1', original['judge']['base_url'])
        self.assertEqual(len(doors), 2)
        self.assertTrue(all(d.closed for d in doors))

    def test_rootless_relay_doors_get_an_image_unique_names_and_reachable_addresses(self):
        from types import SimpleNamespace
        opened = []
        def door(url, *, image, job):
            d = SimpleNamespace(base='http://host.docker.internal:8000/v1',
                                hosts={'host.docker.internal': f'10.88.0.{len(opened) + 2}'},
                                container='door', closed=False, image=image, job=job)
            d.start = lambda: d
            d.stop = lambda: setattr(d, 'closed', True)
            opened.append(d)
            return d
        proc = setup_container.Process([], '/', {}, '/tmp/unused', name='fixture', image='base-img', doors=[])
        with patch.object(container, 'Loopback', side_effect=door), \
                patch.object(jsonl.Proc, 'request', return_value={}) as request:
            proc.request('answer', base_url='http://127.0.0.1:345/v1', judge={'base_url': 'http://127.0.0.1:678/v1'})
        self.assertEqual(request.call_args.kwargs['base_url'], 'http://10.88.0.2:8000/v1')
        self.assertEqual(request.call_args.kwargs['judge']['base_url'], 'http://10.88.0.3:8000/v1')
        self.assertEqual(len({d.job for d in opened}), 2)
        self.assertTrue(all(d.image == 'base-img' and d.closed for d in opened))

    def test_close_removes_container_even_if_protocol_cleanup_fails(self):
        proc = setup_container.Process([], '/', {}, '/tmp/unused', name='fixture', doors=[])
        with patch.object(jsonl.Proc, 'close', side_effect=RuntimeError('fixture')), \
             patch.object(setup_container.container, '_docker') as docker:
            with self.assertRaises(RuntimeError):
                proc.close()
        docker.assert_called_once_with(['rm', '-f', 'fixture'], check=False)

    def test_declared_credentials_are_a_read_only_file_never_the_containers_environment(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(root + '/.work')
            bench = {'_here': root + '/benchmark', 'key': 'fixture', 'environments': {'e': {'needs_secrets': [
                {'name': 'HF_TOKEN', 'to': 'driver'}, {'name': 'LIC', 'to': 'driver', 'kind': 'file', 'env': 'LIC_PATH'}]}}}
            with patch.object(setup_container.install, 'HOME', root + '/installs'), \
                 patch.dict(os.environ, {'NULLIUS_HOME': root + '/profile'}), \
                 patch.object(jsonl.Proc, 'start', lambda self: self), \
                 patch.object(setup_container.container, '_docker'):
                driver = setup_container.Driver(bench, image='nullius/setup:test', app='/source/app', job='fixture',
                                                work=root, upstream=None, data=None, needs=[])
                proc = driver.start({'HF_TOKEN': 'fixture-only', 'LIC_PATH': '/lic', 'NULLIUS_DATASET': '/data'},
                                    root, root + '/log')
                mount = proc.cmd[proc.cmd.index('--mount') + 1]
                src = mount.split(',')[1].removeprefix('source=')
                self.assertEqual(mount, f'type=bind,source={src},target={setup_container.CREDENTIALS_IN},readonly')
                self.assertEqual(proc.cmd[-2:], ['run', setup_container.CREDENTIALS_IN])
                with open(src) as f:
                    self.assertEqual(json.load(f), {'HF_TOKEN': 'fixture-only', 'LIC_PATH': '/lic'})
                self.assertEqual((stat.S_IMODE(os.stat(src).st_mode), os.path.dirname(os.path.dirname(src))),
                                 (0o600, container.key_root()))
                self.assertNotIn('fixture-only', repr((proc.cmd, proc.env)))      # no argument, no variable
                self.assertNotIn('HF_TOKEN', proc.cmd)
                self.assertEqual(proc.env['NULLIUS_DATASET'], '/data')               # the rest as before, by name
                with patch.object(jsonl.Proc, 'request', return_value={}):
                    proc.request('units')
                self.assertFalse(os.path.exists(src))                                 # gone once the driver answers
                proc.close()

    def test_the_launcher_puts_the_credentials_in_the_drivers_environment(self):
        with tempfile.TemporaryDirectory() as root:
            show = "import os, sys; sys.stdout.write(os.environ.get('HF_TOKEN', 'unset'))"
            with open(root + '/setup.json', 'w') as f:
                json.dump({'bench': {'key': 'fixture', '_here': root, 'driver': {'command': [sys.executable, '-c', show]}},
                           'upstream': None, 'needs': []}, f)
            with open(root + '/credentials', 'w') as f:
                json.dump({'HF_TOKEN': 'fixture-only'}, f)
            env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': APP, 'NULLIUS_HOME': root + '/profile'}

            def launch(*credentials):
                return subprocess.run([sys.executable, '-m', 'nullius.standard.setup_container', root + '/setup.json',
                                       root + '/cache', 'run', *credentials], env=env, capture_output=True, text=True,
                                      timeout=60).stdout
            self.assertEqual(launch(root + '/credentials'), 'fixture-only')
            self.assertEqual(launch(), 'unset')

    def test_the_container_starts_the_driver_with_the_command_a_host_run_starts(self):
        from nullius.standard import install
        with tempfile.TemporaryDirectory() as root:
            bench = {'key': 'fixture', '_here': root, 'driver': {'command': [sys.executable, '{here}/show.py']}}
            with open(root + '/show.py', 'w') as f:
                f.write("import sys; sys.stdout.write(' '.join(sys.argv))")
            with open(root + '/setup.json', 'w') as f:
                json.dump({'bench': bench, 'upstream': None, 'needs': []}, f)
            env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': APP, 'NULLIUS_HOME': root + '/profile'}
            said = subprocess.run([sys.executable, '-m', 'nullius.standard.setup_container', root + '/setup.json',
                                   root + '/cache', 'run'], env=env, capture_output=True, text=True, timeout=60).stdout
            command, extra = install.driver_argv(bench, root + '/cache')
            self.assertEqual((said, extra), (' '.join(command[1:]), {}))


@unittest.skipIf(docker_tier.why_not(), docker_tier.why_not() or "")
class DockerDriver(unittest.TestCase):
    """A setup driver in its container, for real: it reads its credential,
    which ``docker inspect`` does not show."""

    def test_the_driver_gets_its_credential_and_the_containers_configuration_does_not(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        here, work = os.path.join(tmp, 'benchmark'), os.path.join(tmp, 'job')
        cwd, home = os.path.join(work, '.work', 'driver'), os.path.join(work, '.work', 'driver-home')
        for d in (here, cwd, home):
            os.makedirs(d)
        with open(os.path.join(here, 'driver.py'), 'w') as f:
            f.write("import json, os, sys\n"
                    "for line in sys.stdin:\n"
                    "    print(json.dumps({'got': os.environ.get('HF_TOKEN', '')}), flush=True)\n")
        bench = {'key': 'fixture', '_here': here, 'driver': {'command': ['python3', '{here}/driver.py']},
                 'environments': {'e': {'needs_secrets': [{'name': 'HF_TOKEN', 'to': 'driver'}]}}}
        job = 'test-setup-' + os.path.basename(tmp)
        with patch.object(setup_container.install, 'HOME', os.path.join(tmp, 'installs')), \
             patch.dict(os.environ, {'NULLIUS_HOME': os.path.join(tmp, 'profile')}):
            driver = setup_container.Driver(bench, image=container.base_image(), app=APP, job=job, work=work,
                                            upstream=None, data=None, needs=[])
            proc = driver.start({'HOME': home, 'HF_TOKEN': 'v4lue-9q'}, cwd, os.path.join(tmp, 'driver.log'))
            try:
                self.assertEqual(proc.request('units', timeout=120), {'got': 'v4lue-9q'})
                seen = subprocess.run(['docker', 'inspect', proc.name], capture_output=True, text=True).stdout
                self.assertIn(setup_container.CREDENTIALS_IN, seen)
                self.assertNotIn('v4lue-9q', seen)
            finally:
                proc.close()
                container.sweep(job)


class SetupDriverFiles(unittest.TestCase):
    """A benchmark's downloaded files reach its Docker setup driver as they reach a local one."""

    def test_the_files_are_in_the_drivers_environment_and_mounted(self):
        from nullius.standard import run
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        got = {}

        class FakeSetup:
            def start(self, env, cwd, log, *, files=(), servers=(), stopped=None):
                got.update(env=env, files=list(files))
                return object()
        r = run.Run.__new__(run.Run)
        r.local, r.drivers, r.setup_driver, r.job_dir = run.threading.local(), [], FakeSetup(), tmp
        r.up, r.data, r.ds, r.bench, r.bench_options, r.licences = "/up", "/data", {"id": "all"}, {"key": "b"}, {}, {}
        r.bench_relay, r._stopped = None, run.threading.Event()
        r.file_env = {"NULLIUS_FILE_INDEX": os.path.join(tmp, "index.bin")}
        r.driver()
        self.assertEqual(got["env"]["NULLIUS_FILE_INDEX"], r.file_env["NULLIUS_FILE_INDEX"])
        self.assertIn(r.file_env["NULLIUS_FILE_INDEX"], got["files"])
