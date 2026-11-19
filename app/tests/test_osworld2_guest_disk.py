"""OSWorld2's guest disk on the docker provider: at least GUEST_GB, in nop.py and the Original's runner alike.
A fake desktop_env stands in."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
STARTUP = os.path.join(HERE, "startup")
DESKTOP_ENV = """import json, os
MAX_RETRIES = 5
class EnvironmentSetupError(RuntimeError):
    pass
class DesktopEnv:
    def __init__(self, provider_name="vmware", region=None, path_to_vm=None, snapshot_name="init_state",
                 action_space="pyautogui", cache_dir="cache", screen_size=(1920, 1080), headless=False,
                 require_a11y_tree=True, require_terminal=False, os_type="Ubuntu", enable_proxy=False,
                 client_password="", instance_type=None, volume_size=None, use_public_ip=False,
                 force_disable_vnc=False, force_disable_recording=False):
        with open(os.environ["FAKE_SIZES"], "a") as f:
            f.write(json.dumps([provider_name, volume_size]) + "\\n")
    def close(self):
        pass
"""
TASK_LOADER = """import json, os
def find_task_class_path(*a):
    return True
def resolve_task_json_path(**kw):
    return ""
def load_task_config(path, **kw):
    return dict(json.loads(os.environ["FAKE_TASK"]), instruction="", proxy=False)
"""
LIB_RUN_SINGLE = """def run_single_example(*a, **kw):
    pass
"""
RUNNER = """import json, multiprocessing, os, sys
from desktop_env.desktop_env import DesktopEnv
def worker(size):
    DesktopEnv(path_to_vm="vm", action_space="pyautogui", provider_name=sys.argv[1], region=None,
               snapshot_name="init_state", screen_size=(1920, 1080), headless=True, os_type="Ubuntu",
               require_a11y_tree=False, enable_proxy=True, client_password="pw", instance_type=None,
               volume_size=size, use_public_ip=False, force_disable_vnc=True, force_disable_recording=True)
if __name__ == "__main__":
    size = json.loads(os.environ["FAKE_TASK"]).get("volume_size")
    for how in ("fork", "spawn"):
        p = multiprocessing.get_context(how).Process(target=worker, args=(size,))
        p.start()
        p.join()
        assert p.exitcode == 0, how
"""


DOCKER_PY = """class ContainerCollection:
    def create(self, image, **kwargs):
        pass
"""
AGAIN = """import importlib, json, os, runpy, sys
import desktop_env.desktop_env as module
runpy.run_path(os.environ["SITE"], run_name="sitecustomize")
runpy.run_path(os.environ["SITE"], run_name="sitecustomize")
module.DesktopEnv(provider_name="docker")
module = importlib.reload(module)
module.DesktopEnv(provider_name="docker")
from docker.models.containers import ContainerCollection
layers, create = 0, ContainerCollection.create
while hasattr(create, "__wrapped__"):
    layers, create = layers + 1, create.__wrapped__
print(json.dumps({"finders": sum(bool(getattr(f, "_nullius_osworld2", False)) for f in sys.meta_path),
                  "layers": layers}))
"""


def fake_class():
    class DesktopEnv:
        def __init__(self, provider_name="docker", volume_size=None):
            self.volume_size = volume_size

    return DesktopEnv


def load_site():
    spec = importlib.util.spec_from_file_location("osworld2_startup_disk", os.path.join(STARTUP, "sitecustomize.py"))
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    return site


class Size(unittest.TestCase):
    def setUp(self):
        self.site = load_site()

    def test_the_floor_is_the_aws_providers_root(self):
        self.assertEqual(self.site.GUEST_GB, 40)

    def test_docker_gets_the_floor_or_the_tasks_larger_size(self):
        for given, wanted in ((None, 40), (0, 40), (30, 40), (40, 40), (60, 60), ("60", 60)):
            with self.subTest(given=given):
                self.assertEqual(self.site.guest_gb("docker", given), wanted)
        self.assertEqual(self.site.guest_gb(" Docker ", None), 40)

    def test_another_provider_is_left_alone(self):
        for provider in ("aws", "vmware", "virtualbox"):
            for given in (None, 30, 60):
                with self.subTest(provider=provider, given=given):
                    self.assertEqual(self.site.guest_gb(provider, given), given)

    def test_a_module_already_loaded_is_wrapped_and_one_finder_added(self):
        module = types.SimpleNamespace(DesktopEnv=fake_class())
        with mock.patch.dict(sys.modules, {self.site.DESKTOP_ENV: module}), mock.patch.object(sys, "meta_path", []):
            self.site.wrap_desktop_env()
            load_site().wrap_desktop_env()
            self.assertEqual(len(sys.meta_path), 1)
        self.assertEqual(module.DesktopEnv().volume_size, 40)

    def test_the_finders_loader_is_wrapped_not_changed(self):
        """A loader whose methods cannot be set, or that loads other modules too, is left as it was."""
        class Slotted:
            __slots__ = ()

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                module.DesktopEnv = fake_class()

        loader = Slotted()
        finder = types.SimpleNamespace(find_spec=lambda name, path, target=None: importlib.machinery.ModuleSpec(
            name, loader))
        with mock.patch.object(sys, "meta_path", [self.site._Floor(), finder]):
            spec = sys.meta_path[0].find_spec(self.site.DESKTOP_ENV, None)
        self.assertIsNot(spec.loader, loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.DesktopEnv().volume_size, 40)
        other = types.ModuleType("other")
        loader.exec_module(other)
        self.assertIsNone(other.DesktopEnv().volume_size)

    def test_a_class_without_those_arguments_is_left_alone(self):
        class DesktopEnv:
            def __init__(self, provider="docker", size=None):
                pass

        init = DesktopEnv.__init__
        self.site.floor_desktop_env(types.SimpleNamespace(DesktopEnv=DesktopEnv))
        self.assertIs(DesktopEnv.__init__, init)

    def test_the_class_is_wrapped_once_however_it_is_called(self):
        made = []

        class DesktopEnv:
            def __init__(self, provider_name="vmware", region=None, path_to_vm=None, volume_size=None):
                made.append((provider_name, volume_size, path_to_vm))

        module = types.SimpleNamespace(DesktopEnv=DesktopEnv)
        self.site.floor_desktop_env(module)
        wrapped = DesktopEnv.__init__
        self.site.floor_desktop_env(module)
        self.assertIs(DesktopEnv.__init__, wrapped)
        DesktopEnv("docker", None, "vm")
        DesktopEnv("docker", None, "vm", 60)
        DesktopEnv(provider_name="docker", volume_size=30)
        DesktopEnv()
        DesktopEnv("aws", volume_size=None)
        self.assertEqual(made, [("docker", 40, "vm"), ("docker", 60, "vm"), ("docker", 40, None),
                                ("vmware", None, None), ("aws", None, None)])


class BothPaths(unittest.TestCase):
    """nop.py and a runner built as run_multienv_m3.py, each with the startup folder first on PYTHONPATH."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.up = os.path.join(self.tmp, "up")
        os.makedirs(os.path.join(self.up, "desktop_env"))
        for name, text in (("task_loader.py", TASK_LOADER), ("lib_run_single.py", LIB_RUN_SINGLE),
                           ("runner.py", RUNNER), (os.path.join("desktop_env", "__init__.py"), ""),
                           (os.path.join("desktop_env", "desktop_env.py"), DESKTOP_ENV)):
            with open(os.path.join(self.up, name), "w") as f:
                f.write(text)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def sizes(self, argv, task, **env):
        log = os.path.join(self.tmp, "sizes")
        if os.path.exists(log):
            os.remove(log)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("NULLIUS_OPTION_")}
        environ.update(PYTHONPATH=os.pathsep.join([STARTUP, self.up]), FAKE_SIZES=log, FAKE_TASK=json.dumps(task),
                       **env)
        done = subprocess.run([sys.executable, *argv], cwd=self.up, env=environ, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertNotIn("Error in sitecustomize", done.stderr)
        with open(log) as f:
            return [json.loads(ln) for ln in f.read().splitlines()]

    def nop(self, task, **env):
        return self.sizes([os.path.join(HERE, "nop.py"), "vm", "pw", "examples", "tasks", "001", self.out], task,
                          **env)

    def test_the_null_control_and_the_original_setup_get_the_same_disk(self):
        for task, wanted in (({}, 40), ({"volume_size": 30}, 40), ({"volume_size": 60}, 60)):
            with self.subTest(task=task):
                self.assertEqual(self.nop(task), [["docker", wanted]])
                self.assertEqual(self.sizes(["runner.py", "docker"], task), [["docker", wanted]] * 2)

    def test_another_provider_is_left_alone_on_the_runners_path(self):
        self.assertEqual(self.sizes(["runner.py", "aws"], {"volume_size": 30}), [["aws", 30]] * 2)

    def test_another_vm_provider_option_wraps_nothing(self):
        self.assertEqual(self.nop({}, NULLIUS_OPTION_VM_PROVIDER="aws"), [["docker", None]])


class Again(unittest.TestCase):
    """desktop_env imported before the startup module runs, the startup module run twice, then a reload."""
    def test_a_reload_keeps_the_floor_and_a_second_run_wraps_nothing_twice(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, text in ((os.path.join("desktop_env", "__init__.py"), ""),
                           (os.path.join("desktop_env", "desktop_env.py"), DESKTOP_ENV),
                           (os.path.join("docker", "__init__.py"), ""), (os.path.join("docker", "models", "__init__.py"), ""),
                           (os.path.join("docker", "models", "containers.py"), DOCKER_PY), ("again.py", AGAIN)):
            os.makedirs(os.path.dirname(os.path.join(tmp, name)), exist_ok=True)
            with open(os.path.join(tmp, name), "w") as f:
                f.write(text)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("NULLIUS_OPTION_")}
        environ.update(PYTHONPATH=tmp, FAKE_SIZES=os.path.join(tmp, "sizes"),
                       SITE=os.path.join(STARTUP, "sitecustomize.py"))
        done = subprocess.run([sys.executable, "again.py"], cwd=tmp, env=environ, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(json.loads(done.stdout), {"finders": 1, "layers": 1})
        with open(os.path.join(tmp, "sizes")) as f:
            self.assertEqual([json.loads(ln) for ln in f.read().splitlines()], [["docker", 40]] * 2)


if __name__ == "__main__":
    unittest.main()
