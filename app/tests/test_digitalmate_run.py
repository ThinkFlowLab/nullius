"""The Nullius adapter preserves product answers and owns its trial processes.

No model, network, runtime installation or real process is needed by these tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import json
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
            env = launch.call_args.kwargs["env"]
            self.assertEqual(submit.call_args.args, ("Keep this prompt unchanged.", env["DIGITALMATE_ACCESS_TOKEN"]))
            # The release serves only as Hive Home, under direct instance authentication.
            self.assertEqual(env["DIGITALMATE_CLIENT_AUTH"], "instance")
            self.assertEqual(json.loads(env["DIGITALMATE_WORKSWARM_COMMAND"])[1:], ["-m", "server_engine.workswarm_launcher"])
            argv = launch.call_args.args[0]
            for flag in ("--hive-results", "--hive-turns", "--hive-bees"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--hive-beebou-id") + 1], "beebou")

    def test_fixed_version_binds_its_project_and_hands_over_its_mcp_servers(self):
        self.prepare()
        service = Mock(pid=43210)
        service.poll.return_value = None
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict(adapter.os.environ, dict(self.relay, DIGITALMATE_HIVE_HOME_MCP_SERVERS="/elsewhere"), clear=True), \
                patch.object(adapter, "require_container"), \
                patch.object(adapter.Path, "home", return_value=self.root), \
                patch.object(adapter.subprocess, "run") as bind, \
                patch.object(adapter.subprocess, "Popen", return_value=service) as launch, \
                patch.object(adapter, "urlopen", return_value=response), \
                patch.object(adapter.signal, "signal", return_value=signal.SIG_DFL), \
                patch.object(adapter, "stop"):
            with adapter.product(self.install, port=4321, project=Path("/workspace"),
                                 mcp_config=Path("/private/servers.json")):
                pass
        argv = bind.call_args.args[0]
        self.assertEqual(argv[1:3], ["-m", "dm_hive_tools.workspace"])
        self.assertEqual(argv[argv.index("--project-dir") + 1], "/workspace")
        state = argv[argv.index("--state-dir") + 1]
        serve = launch.call_args.args[0]
        self.assertEqual(serve[serve.index("--workspace") + 1], state)
        self.assertEqual(serve[serve.index("--port") + 1], "4321")
        self.assertEqual(launch.call_args.kwargs["env"]["DIGITALMATE_HIVE_HOME_MCP_SERVERS"], "/private/servers.json")

    def test_release_runs_have_no_project_binding_or_inherited_mcp(self):
        self.prepare()
        service = Mock(pid=43210)
        service.poll.return_value = None
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict(adapter.os.environ, dict(self.relay, DIGITALMATE_HIVE_HOME_MCP_SERVERS="/elsewhere"), clear=True), \
                patch.object(adapter, "require_container"), \
                patch.object(adapter.Path, "home", return_value=self.root), \
                patch.object(adapter.subprocess, "run") as bind, \
                patch.object(adapter.subprocess, "Popen", return_value=service) as launch, \
                patch.object(adapter, "urlopen", return_value=response), \
                patch.object(adapter.signal, "signal", return_value=signal.SIG_DFL), \
                patch.object(adapter, "stop"):
            with adapter.product(self.install):
                pass
        bind.assert_not_called()
        self.assertNotIn("DIGITALMATE_HIVE_HOME_MCP_SERVERS", launch.call_args.kwargs["env"])

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
