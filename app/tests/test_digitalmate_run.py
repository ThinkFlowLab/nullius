"""The Nullius adapter preserves product answers and owns its trial processes.

No model, network, runtime installation or real process is needed by these tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, call, patch


AGENT = Path(__file__).resolve().parents[1] / "nullius/catalogue/agents/digitalmate"
sys.path.insert(0, str(AGENT))
from tests.test_digitalmate_gateway import stub_websockets  # noqa: E402
stub_websockets()
SPEC = importlib.util.spec_from_file_location(
    "digitalmate_nullius_run", AGENT / "run.py"
)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class PreparedInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="digitalmate-nullius-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.relay = {"API_BASE": "http://relay:8080/trial/v1", "API_KEY": "placeholder", "MODEL_NAME": "test-model"}

    def prepare(self):
        for relative in (
            "runtime/bin/python",
            "src/harness/personal-agent/services/agent_bridge/src/server_engine/workswarm_launcher.py",
        ):
            file = self.install / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()

    def test_runtime_must_be_installed_before_a_process_starts(self):
        with patch.object(adapter.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "missing its runtime"):
                adapter.run(self.install, "question")
            self.prepare()
            (self.install / "runtime/bin/python").unlink()
            with self.assertRaisesRegex(RuntimeError, "missing its runtime"):
                adapter.run(self.install, "question")
            launch.assert_not_called()

    def test_product_entry_points_must_exist(self):
        self.prepare()
        (self.install / "src/harness/personal-agent/services/agent_bridge/src/server_engine/workswarm_launcher.py").unlink()
        with patch.object(adapter.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "product launcher"):
                adapter.run(self.install, "question")
            launch.assert_not_called()

    def test_each_relay_setting_is_required_before_a_process_starts(self):
        self.prepare()
        for missing in self.relay:
            environment = {key: value for key, value in self.relay.items() if key != missing}
            with self.subTest(missing=missing), patch.dict(adapter.os.environ, environment, clear=True):
                with patch.object(adapter.subprocess, "Popen") as launch:
                    with self.assertRaisesRegex(RuntimeError, "relay setting is missing: " + missing):
                        adapter.run(self.install, "question")
                    launch.assert_not_called()

    def test_client_failure_still_stops_owned_service(self):
        self.prepare()
        service = Mock(pid=43210)
        service.poll.return_value = None
        response = MagicMock()
        response.__enter__.return_value.status = 200
        failed = RuntimeError("Gateway disconnected")
        with patch.dict(adapter.os.environ, self.relay, clear=True), \
                patch.object(adapter, "require_container"), \
                patch.object(adapter.Path, "home", return_value=self.root), \
                patch.object(adapter.subprocess, "Popen", return_value=service) as launch, \
                patch.object(adapter, "submit_task", side_effect=failed) as submit, \
                patch.object(adapter, "urlopen", return_value=response), \
                patch.object(adapter.signal, "signal", return_value=signal.SIG_DFL), \
                patch.object(adapter, "stop") as stop:
            with self.assertRaisesRegex(RuntimeError, "Gateway disconnected"):
                adapter.run(self.install, "Keep this prompt unchanged.")
            stop.assert_called_once_with(service)
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            self.assertEqual(submit.call_args.args[:2], ("Keep this prompt unchanged.", Path.cwd()))
            self.assertEqual(submit.call_args.args[2], launch.call_args.kwargs["env"]["DIGITALMATE_ACCESS_TOKEN"])

    def test_local_backend_is_refused(self):
        with patch.object(adapter.Path, "exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "requires --backend container"):
                adapter.require_container()


class ProcessCleanupTests(unittest.TestCase):
    def test_cleanup_terminates_group_and_reaps_service_even_after_timeout(self):
        for first_wait in (0, subprocess.TimeoutExpired("service", 15)):
            with self.subTest(first_wait=first_wait):
                service = Mock(pid=43210)
                service.wait.side_effect = [first_wait, 0]
                with patch.object(adapter.os, "killpg") as kill_group:
                    adapter.stop(service)
                self.assertEqual(kill_group.call_args_list, [
                    call(43210, signal.SIGTERM), call(43210, signal.SIGKILL),
                ])
                self.assertEqual(service.wait.call_args_list, [call(timeout=15), call()])

    def test_group_disappearing_during_cleanup_is_harmless(self):
        service = Mock(pid=43210)
        with patch.object(adapter.os, "killpg", side_effect=[None, ProcessLookupError]):
            adapter.stop(service)
        service.wait.assert_any_call()


if __name__ == "__main__":
    unittest.main()
