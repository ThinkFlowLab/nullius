"""A benchmark whose units boot their own VM, on Boat: each unit whole in a VM of its own, as a shard of one."""
from __future__ import annotations

import shlex
import unittest

from nullius.standard import boat_units, definitions, recipe

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
        # what calls a model would need the key in the VM: not yet
        agent = recipe.compile(dict(base, others=["original"], model="gpt-4o"), boat_unusable=lambda here=True: None)
        self.assertIn("Original cannot run in a Boat VM yet: choose Hosts.", [e["message"] for e in agent["errors"]])


if __name__ == "__main__":
    unittest.main()
