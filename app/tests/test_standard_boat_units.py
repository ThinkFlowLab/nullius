"""A benchmark whose units boot their own VM, on Boat: each unit whole in a VM of its own, as a shard of one."""
from __future__ import annotations

import shlex
import io
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from nullius import secrets
from nullius.standard import cli
from nullius.standard.backends import container
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.runner import boat_units, run

OSWORLD1 = definitions.get("benchmark", "osworld1")


class Plan(unittest.TestCase):
    def test_each_shard_is_one_unit_and_a_limit_runs_the_first_ones(self):
        self.assertEqual(boat_units.plan(OSWORLD1, "test_small", None), (39, list(range(1, 40))))
        self.assertEqual(boat_units.plan(OSWORLD1, "test_small", 2), (39, [1, 2]))
        self.assertEqual(boat_units.plan(OSWORLD1, "test_small", 500)[1][-1], 39)
        with self.assertRaisesRegex(boat_units.Refused, "no dataset"):
            boat_units.plan(OSWORLD1, "nowhere", None)
        bare = dict(OSWORLD1, datasets=[{"id": "x"}])
        with self.assertRaisesRegex(boat_units.Refused, "declares no number of units"):
            boat_units.plan(bare, "x", None)

    def test_the_vm_runs_this_runners_snapshot_on_one_unit_on_local_processes(self):
        line = boat_units.child_argv(["osworld1", "nop", "--dataset", "test_small"], app="/home/user/.nullius/app/v1-x",
                                     index=3, n=39, launch="L", name="job__shard-3of39", jobs="/j")
        argv = shlex.split(line.split(" && ", 1)[1])
        self.assertEqual(argv[:4], ["python3", "-I", "-S", "-B"])
        self.assertEqual(argv[argv.index("--shard") + 1], "3/39")
        self.assertEqual(argv[argv.index("--shard-by") + 1], "position")
        self.assertIn("--relay-container", argv)
        self.assertIn("--unsandboxed", argv)
        self.assertNotIn("--backend", argv)                        # local processes: the VM is the sandbox
        self.assertIn("NULLIUS_CATALOGUE=/home/user/.nullius/app/v1-x/catalogue", line)


class Compile(unittest.TestCase):
    def test_on_boat_a_vm_benchmark_runs_its_null_control_whole_in_each_vm_and_asks_this_machine_nothing(self):
        base = {"harness": recipe.HARNESS, "name": "os", "benchmark": "osworld1", "datasets": ["test_small"],
                "others": ["nop"], "provider": "openrouter", "where": "local", "backend": "boat", "boat_size": "default", "limit": 2}
        out = recipe.compile(base, boat_unusable=lambda here=True: None)
        self.assertEqual(out["errors"], [])
        self.assertIn("--backend boat --boat-size default", out["script"])
        self.assertEqual(out["host"]["needs"], [])                  # each VM installs what the options need
        self.assertFalse(out["docker"])
        # Model calls use a separate relay container in the Boat VM.
        agent = recipe.compile(dict(base, others=["original"], model="gpt-4o"), boat_unusable=lambda here=True: None)
        self.assertEqual(agent["errors"], [])




class RelayPlacement(unittest.TestCase):
    def test_local_task_uses_container_relay_with_host_url_and_judge_route(self):
        with tempfile.TemporaryDirectory() as d:
            r = run.Run.__new__(run.Run)
            r.base, r.key, r.backend, r.relay_container = "https://example.test/v1", "fake-secret", "local", True
            r.other, r.key_header = {}, "authorization"
            r.judge_model, r.simulator_model = "grader", None
            r.name, r.image, r.model = "job", "image", "agent-model"
            r.prices, r.serving, r.max_calls, r.call_seconds = {}, {}, 3, 12
            r.ledger_file = lambda: "/ledger"
            r.models_called = lambda: ["agent-model", "grader"]
            with patch.object(container, "Unit") as unit, patch.object(run.relay, "Relay") as local:
                unit.return_value.start.return_value = unit.return_value
                unit.return_value.url.return_value = "http://127.0.0.1:1234/u0001/v1"
                got = r.unit_relay("u0001", d, {})
            local.assert_not_called()
            self.assertEqual(got.unit_url, "http://127.0.0.1:1234/u0001/v1")
            args = unit.call_args.kwargs
            self.assertEqual(args["key"], "fake-secret")
            self.assertEqual(args["routes"], {"u0001-judge": "grader"})
            self.assertEqual((args["ledger"], args["max_calls"]), ("/ledger", 3))

    def test_bootstrap_gets_credentials_from_stdin_not_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d + "/nullius/standard")
            with open(d + "/nullius/standard/cli.py", "w") as f:
                f.write('import os; print(os.environ["OPENAI_API_KEY"])')
            result = subprocess.run(["python3", "-I", "-S", "-B", "-c", boat_units.RUNPY, d],
                                    input=json.dumps({"OPENAI_API_KEY": "fake-secret"}) + "\n",
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "fake-secret")
            broken = subprocess.run(["python3", "-I", "-S", "-B", "-c", boat_units.RUNPY, d],
                                    input="", text=True, capture_output=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertEqual(broken.stdout, "")

    def test_credentials_include_only_selected_provider_and_declared_licences(self):
        args = SimpleNamespace(provider="openrouter", from_run=True)
        held = SimpleNamespace(kinds={"HF_TOKEN": "token"}, get=lambda name: {"kind": "token", "value": "fake-hf-token"})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-model-key", "BOAT_API_KEY": "fake-boat-key",
                                     "UNRELATED_KEY": "unrelated"}, clear=True), \
             patch.object(cli, "licences", return_value=(held, None)):
            got = cli.boat_credentials(args, OSWORLD1, {"_kind": "upstream"}, {})
        self.assertEqual(set(got), {"OPENAI_API_KEY", "OPENAI_BASE_URL", secrets.HANDED})
        self.assertEqual(got["OPENAI_API_KEY"], "fake-model-key")
        self.assertEqual(secrets.unpack(got[secrets.HANDED]), {"HF_TOKEN": b"fake-hf-token"})

    def test_cli_forwards_models_options_and_divides_cap(self):
        with patch.object(boat_units, "run", return_value=("/job", False)) as launch, \
             patch.object(cli, "boat_credentials", return_value={"OPENAI_API_KEY": "fake-secret"}), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = cli.main(["run", "osworld1", "original", "--backend", "boat", "--boat-size", "default",
                               "--model", "gpt-4o", "--provider", "openrouter", "--dataset", "test_small",
                               "--limit", "2", "--max-usd", "1", "--max-calls", "5", "--served-by", "openai",
                               "--price", "openai/gpt-4o=1,2", "--timeout", "agent=90"])
        self.assertEqual(result, 0)
        words = launch.call_args.args[1]
        for flag, value in (("--model", "gpt-4o"), ("--max-usd", "0.5"), ("--max-calls", "5"),
                            ("--served-by", "openai"), ("--price", "openai/gpt-4o=1,2"), ("--timeout", "agent=90")):
            self.assertEqual(words[words.index(flag) + 1], value)
        self.assertNotIn("fake-secret", " ".join(words))
        self.assertEqual(launch.call_args.kwargs["credentials"], {"OPENAI_API_KEY": "fake-secret"})


if __name__ == "__main__":
    unittest.main()
