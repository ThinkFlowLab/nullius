"""The importer registry: adding a harness is a file, removing one is a file.

The point of discovery over a hand-maintained table is that no second place
keeps naming a harness after its module is deleted. These pin that property
rather than the current contents, so the suite does not have to be edited
every time the benchmarks program picks up another runner — or drops one.
The empty registry that dropping the last one would leave is pinned below
against a patched registry.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

from nullius import importers as harnesses
from nullius.importers import cli


class Registry(unittest.TestCase):
    def test_every_importer_satisfies_the_contract(self):
        # An empty registry is a valid state, not a failure: a harness is
        # removed by deleting its module and its tests, and the last one
        # leaving must not fail a test that is not about it (the CLI class
        # below pins what the CLI says then).
        for name, module in harnesses.available().items():
            with self.subTest(harness=name):
                self.assertTrue(callable(module.detect))
                self.assertTrue(callable(module.convert))
                self.assertEqual(module.NAME, name)
                self.assertIs(harnesses.get(name), module)
                self.assertTrue((module.__doc__ or "").strip(),
                                "an importer documents what it reads")

    def test_an_unknown_name_is_none_not_an_error(self):
        # The CLI turns this into a usage message listing what exists; raising
        # here would make a typo look like a crash.
        self.assertIsNone(harnesses.get("no-such-harness"))
        self.assertIsNone(harnesses.get(""))

    def test_nothing_claims_an_unrelated_directory(self):
        # detect() runs against whatever a caller points at, including a plain
        # directory. A harness that answers yes to everything would capture
        # every future import.
        empty = tempfile.mkdtemp()
        self.assertIsNone(harnesses.detect(empty))

    def test_detect_survives_a_path_that_does_not_exist(self):
        self.assertIsNone(harnesses.detect("/nonexistent/run/tree"))

    def test_the_registry_names_no_harness_itself(self):
        # The removal property: deleting a module must be the whole deletion,
        # so this package cannot mention one by name.
        source = (APP / "nullius" / "importers" / "__init__.py").read_text()
        for name in harnesses.available():
            with self.subTest(harness=name):
                self.assertNotIn(name, source.split('"""')[-1],
                                 f"{name} is named in the registry's code")

    def test_the_cli_names_no_harness_either(self):
        source = (APP / "nullius" / "importers" / "cli.py").read_text()
        body = source.split('"""')[-1]
        for name in harnesses.available():
            with self.subTest(harness=name):
                self.assertNotIn(name, body,
                                 f"{name} is named in the CLI's code")


class FirstMatchIsStable(unittest.TestCase):
    """Name order is the tie-break, so no two importers may claim one tree.

    A Harbor job is the one input no importer may claim at all - it is the
    reader's.
    """
    EXAMPLES = APP / "tests" / "fixtures"


    def test_no_importer_claims_a_harbor_job(self):
        for job in ("liveboard-oracle-x4",):
            path = str(self.EXAMPLES / "harbor-jobs" / job)
            with self.subTest(job=job):
                self.assertEqual(
                    [name for name, m in harnesses.available().items() if m.detect(path)], [])
                self.assertIsNone(harnesses.detect(path))
        self.assertIsNone(harnesses.detect(str(self.EXAMPLES / "harbor-jobs")))

FAKE_KEY = "sk-test-0123456789abcdefghij"


def stub(name, *, detect=True, convert=None, check=None):
    """A harness module in miniature, so these pin the registry's behaviour
    and not any one importer's."""
    module = types.ModuleType(f"stub_{name}")
    module.NAME = name
    module.detect = lambda path: detect
    module.convert = convert or (lambda src, out, job_name="", command="": [])
    if check is not None:
        module.check = check
    return module


class Refusal(unittest.TestCase):
    """`problem`: what the picker asks before a workspace adopts a root.

    A malformed tree must come back as a message that names what is wrong
    and carries no secret — not as a job with no trials, and not as an
    exception the page turns into a 500.
    """

    def registry(self, *modules):
        found = {m.NAME: m for m in modules}
        self.addCleanup(patch.stopall)
        patch.object(harnesses, "available", return_value=found).start()

    def test_nothing_claimed_is_no_problem(self):
        # An unclaimed root is one for the Harbor reader; whether it holds a
        # job is that reader's question, not the registry's.
        self.assertIsNone(harnesses.problem(tempfile.mkdtemp()))
        self.assertIsNone(harnesses.problem("/nonexistent/run/tree"))

    def test_a_plain_system_exit_and_an_os_error_are_problems_too(self):
        # An importer may raise SystemExit; a tree that
        # cannot be read at all raises OSError. Neither is a crash.
        for exc in (SystemExit(f"nothing converts under /x/{FAKE_KEY}"),
                    PermissionError(13, "Permission denied", f"/x/{FAKE_KEY}/run.json")):
            with self.subTest(exc=type(exc).__name__):
                def convert(src, out, job_name="", command="", exc=exc):
                    raise exc
                self.registry(stub("x", convert=convert))
                why = harnesses.problem("/x")
                self.assertTrue(why)
                self.assertNotIn(FAKE_KEY, why)

    def test_check_is_preferred_and_convert_is_then_never_called(self):
        calls = []
        def check(path):
            calls.append(("check", path))
            raise SystemExit("run.json: not a run record")
        def convert(src, out, job_name="", command=""):
            calls.append(("convert", src))
            return []
        self.registry(stub("x", check=check, convert=convert))
        self.assertEqual(harnesses.problem("/t"), "run.json: not a run record")
        self.assertEqual(calls, [("check", "/t")])

    def test_without_check_a_dry_conversion_is_made_and_discarded(self):
        outs = []
        def convert(src, out, job_name="", command=""):
            outs.append(out)
            os.makedirs(os.path.join(out, "job"))
            return [os.path.join(out, "job")]
        self.registry(stub("x", convert=convert))
        self.assertIsNone(harnesses.problem("/t"))
        self.assertEqual(len(outs), 1)
        self.assertFalse(os.path.exists(outs[0]), "the scratch directory is removed")

    def test_a_good_tree_is_no_problem(self):
        self.registry(stub("x", check=lambda path: None))
        self.assertIsNone(harnesses.problem("/t"))

    def test_labels_name_the_importers_for_a_person(self):
        a, b = stub("a"), stub("b")
        b.LABEL = "Bee"
        self.registry(a, b)
        self.assertEqual(harnesses.labels(), "a, Bee")

class Disjoint(unittest.TestCase):
    """No two importers claim one directory.

    `detect` resolves by first match in name order, so an overlap would be
    silent: the alphabetically earlier harness would convert the other's run
    tree and misread it. Each shape another harness writes is laid out here
    minimally — enough for its own detector, which is exactly what must not
    be enough for anyone else's.
    """

    FIXTURES = APP / "tests" / "fixtures"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def shaped(self, name, files):
        directory = self.root / name
        directory.mkdir()
        for relative, content in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))
        return str(directory)

    def candidates(self):
        return {
            "harbor": str(self.FIXTURES / "harbor-jobs"),
            # an ATIF trajectory on its own, the shape a trajectory reader claims
            "atif": self.shaped("atif", {"trajectory.json": {
                "schema_version": "ATIF-v1.6", "session_id": "s", "agent": {}, "steps": []}}),
        }

    def test_at_most_one_importer_claims_each_shape(self):
        for shape, path in self.candidates().items():
            claimants = [name for name, module in harnesses.available().items()
                         if module.detect(path)]
            with self.subTest(shape=shape):
                self.assertLessEqual(len(claimants), 1, f"{shape} is claimed by {claimants}")


    def test_the_otel_importer_claims_none_of_the_other_shapes(self):
        module = harnesses.get("otel")
        self.assertIsNotNone(module)
        for shape, path in self.candidates().items():
            with self.subTest(shape=shape):
                self.assertFalse(module.detect(path))


class CLI(unittest.TestCase):
    """`--list` and refusal, against the registry there is and an empty one."""

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:           # argparse's error path
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def empty(self):
        self.addCleanup(patch.stopall)
        patch.object(harnesses, "available", return_value={}).start()

    def test_list_names_every_registered_importer(self):
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        for name in harnesses.available():
            self.assertIn(name, out)
        self.assertNotIn("no importers registered", out)

    def test_list_with_nothing_registered_says_so_and_exits_zero(self):
        # With no importer registered, printing nothing would read exactly
        # like a CLI that imported and did no work.
        self.empty()
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no importers registered")
        code, out, _ = self.run_cli("--list", "--json")
        self.assertEqual((code, json.loads(out)), (0, {}))

    def test_an_unrecognised_tree_is_refused_with_nothing_registered(self):
        self.empty()
        with tempfile.TemporaryDirectory() as run:
            code, _, err = self.run_cli(run, "-o", os.path.join(run, "out"))
        self.assertEqual(code, cli.EXIT_NOT_FOUND)
        self.assertIn("no importer recognised", err)
        self.assertIn("known: none", err)

    def test_the_module_runs_under_dash_m(self):
        # Exit status alone cannot distinguish a working CLI from an empty one.
        proc = subprocess.run([sys.executable, "-m", "nullius.importers.cli", "--list"],
                              cwd=APP, env={**os.environ, "PYTHONPATH": str(APP)},
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "--list printed nothing")


class Trees(unittest.TestCase):
    """`trees`: which directories under a root the picker converts one job
    each — the importer's answer, with the CLI's reading as the default.

    The registry asks the importer how to group directories. An importer
    without an answer uses the CLI's default: the root, as one job.
    """

    def test_a_claimed_root_is_one_tree_by_default(self):
        self.assertEqual(harnesses.trees(stub("x"), "/exp"), ["/exp"])

    def test_an_unclaimed_root_is_no_tree_by_default(self):
        self.assertEqual(harnesses.trees(stub("x", detect=False), "/exp"), [])

    def test_an_importer_with_its_own_answer_is_asked_and_the_default_is_not(self):
        module = stub("x", detect=False)          # detect says no; trees decides
        module.trees = lambda root: [os.path.join(root, "a"), os.path.join(root, "b")]
        self.assertEqual(harnesses.trees(module, "/exp"), ["/exp/a", "/exp/b"])

    def test_trees_is_not_part_of_the_contract(self):
        # A module without it still registers: the hook is optional, and
        # the default is the CLI's reading rather than a missing name.
        self.assertNotIn("trees", harnesses._CONTRACT)

    def test_every_registered_importer_answers_and_an_empty_directory_is_nothing(self):
        # Each importer there is says what one job is under a root — the
        # answers differ (a nested directory of trajectories is one job, a
        # parent of export directories is one per directory), which is why
        # the importer is asked — and none of them makes a job of nothing.
        for name, module in harnesses.available().items():
            with self.subTest(harness=name):
                self.assertTrue(callable(getattr(module, "trees", None)),
                                f"{name} says what one job is under a root")
                self.assertEqual(harnesses.trees(module, tempfile.mkdtemp()), [],
                                 "an empty directory is nothing to convert")


if __name__ == "__main__":
    unittest.main()
