"""SWE-bench captures the submitted diff before its evaluator changes the checkout."""
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests import APP

DRIVER = Path(APP, "nullius/catalogue/benchmarks/swe-bench-verified/driver.py")


class Patch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "edit").write_text("base\n")
        (self.repo / "delete").write_text("delete\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

        self.grade = mock.Mock(return_value={"unit": {"resolved": False, "patch_successfully_applied": False}})
        modules = {name: types.ModuleType(name) for name in
                   ("pyarrow", "pyarrow.parquet", "swebench", "swebench.harness",
                    "swebench.harness.grading", "swebench.harness.utils")}
        modules["swebench.harness.grading"].get_eval_report = self.grade
        modules["swebench.harness.utils"].make_test_spec = lambda _: types.SimpleNamespace(eval_script="unused")
        spec = importlib.util.spec_from_file_location("swebench_driver", DRIVER)
        self.driver = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "stdin", io.StringIO()):
            spec.loader.exec_module(self.driver)
        self.driver.WORKDIR = str(self.repo)
        self.driver.RESULTS = str(self.root / "results")
        Path(self.driver.RESULTS).mkdir()
        self.driver._rows = {"unit": {"instance_id": "unit", "base_commit": self.base, "problem_statement": "Fix"}}
        self.driver.IMAGES = {"unit": "unused"}
        self.eval = self.root / "eval.sh"
        self.eval.write_text("echo evaluator > eval-only\n")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def capture(self):
        step = self.driver.setup("unit", str(self.root))["verify"]
        command = step["run"][-1].replace("/eval.sh", str(self.eval))
        subprocess.run([*step["run"][:-1], command], check=True, capture_output=True)
        self.assertEqual(step["collect"], self.driver.RESULTS)
        return Path(self.driver.RESULTS, "model.patch")

    def test_empty_patch_reaches_the_grader_as_no_submission(self):
        patch = self.capture()
        self.assertEqual(patch.read_bytes(), b"")
        self.driver.score("unit", str(self.root), [], "", verified={"dir": self.driver.RESULTS})
        self.assertIsNone(self.grade.call_args.args[1]["model_patch"])
        self.assertTrue(Path(self.driver.RESULTS, "report.json").is_file())

    def test_committed_staged_unstaged_new_deleted_and_binary_changes_are_kept(self):
        (self.repo / "edit").write_text("committed\n")
        self.git("commit", "-qam", "agent commit")
        (self.repo / "staged").write_text("staged\n")
        self.git("add", "staged")
        (self.repo / "edit").write_text("final\n")
        (self.repo / "new file").write_text("new\n")
        (self.repo / "binary").write_bytes(b"\x00\xff\x01")
        (self.repo / "delete").unlink()
        patch = self.capture()
        self.assertNotIn("eval-only", patch.read_text())
        self.assertTrue((self.repo / "eval-only").is_file())
        self.driver.score("unit", str(self.root), [], "", verified={"dir": self.driver.RESULTS})
        self.assertEqual(self.grade.call_args.args[1]["model_patch"], patch.read_text())

        self.git("reset", "--hard", self.base)
        self.git("clean", "-fd")
        self.git("apply", str(patch))
        self.assertEqual((self.repo / "edit").read_text(), "final\n")
        self.assertEqual((self.repo / "staged").read_text(), "staged\n")
        self.assertEqual((self.repo / "new file").read_text(), "new\n")
        self.assertEqual((self.repo / "binary").read_bytes(), b"\x00\xff\x01")
        self.assertFalse((self.repo / "delete").exists())

    def test_capture_failure_does_not_run_evaluator(self):
        self.driver._rows["unit"]["base_commit"] = "missing-commit"
        with self.assertRaises(subprocess.CalledProcessError):
            self.capture()
        self.assertFalse((self.repo / "eval-only").exists())

    def test_missing_patch_is_not_treated_as_a_submission(self):
        with self.assertRaises(FileNotFoundError):
            self.driver.score("unit", str(self.root), [], "", verified={"dir": self.driver.RESULTS})
        self.grade.assert_not_called()
