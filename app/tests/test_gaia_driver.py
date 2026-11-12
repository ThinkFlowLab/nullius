"""GAIA glue contracts with synthetic metadata and stubbed upstream modules.

No Hugging Face packages, downloads, model calls or upstream installation are
needed. Scoring must delegate to the upstream module; this suite does not
reimplement or purport to validate the official matching algorithm.
"""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch


DRIVER = Path(__file__).resolve().parents[1] / "nullius/catalogue/benchmarks/gaia/driver.py"
DATASET = "gaia-validation-level1"


class GaiaDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullius-gaia-driver-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        upstream = self.root / "upstream"
        upstream.mkdir()
        (upstream / "scorer.py").write_text("def question_scorer(*args):\n    raise AssertionError('stub scorer must be configured')\n")
        (upstream / "content.py").write_text(
            "SUBMISSION_TEXT = '```You are a general AI assistant. End with FINAL ANSWER:```'\n")
        environment = patch.dict(os.environ, {"NULLIUS_UPSTREAM": str(upstream),
            "NULLIUS_DATASET_ID": DATASET, "NULLIUS_OPTION_TEMPERATURE": "default", "HF_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location("nullius_test_gaia_driver", DRIVER)
        self.driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.driver)
        self.row = {"task_id": "fixture-task", "Question": "What is shown in the attachment?",
                    "Final answer": "synthetic-reference", "Level": 1,
                    "file_name": "note.txt", "file_path": "2023/validation/note.txt"}
        self.driver.ROWS = {self.row["task_id"]: self.row}
        self.attachment = self.root / "note.txt"
        self.attachment.write_text("synthetic attachment")

    def test_setup_stages_only_the_requested_attachment_and_not_the_reference(self):
        with patch.object(self.driver, "cached_file", return_value=self.attachment) as fetch:
            result = self.driver.setup("fixture-task", str(self.workspace))
        fetch.assert_called_once_with("2023/validation/note.txt")
        self.assertEqual(result["environment"], "data")
        self.assertIn(self.row["Question"], result["prompt"])
        self.assertIn("note.txt", result["prompt"])
        self.assertNotIn(self.row["Final answer"], result["prompt"])
        self.assertEqual([p.name for p in self.workspace.iterdir()], ["note.txt"])
        self.assertEqual((self.workspace / "note.txt").read_text(), "synthetic attachment")

    def test_attachment_rejects_unsafe_or_metadata_paths_before_fetch(self):
        invalid = [
            ("../note.txt", "2023/validation/../note.txt"),
            ("note.txt", "/2023/validation/note.txt"),
            ("note.txt", "2023/validation/../note.txt"),
            ("note.txt", "2023/test/note.txt"),
            ("note.txt", "2023/validation/other.txt"),
            ("note.txt", "2023\\validation\\note.txt"),
            ("metadata.parquet", "2023/validation/metadata.parquet"),
            ("", "2023/validation/note.txt"),
            ("note.txt", ""),
            ("nested/note.txt", "2023/validation/nested/note.txt"),
        ]
        with patch.object(self.driver, "cached_file") as fetch:
            for name, path in invalid:
                with self.subTest(name=name, path=path), self.assertRaisesRegex(ValueError, "plain file"):
                    self.driver.attachment({"file_name": name, "file_path": path})
            fetch.assert_not_called()

    def test_setup_without_attachment_creates_no_files(self):
        self.row.update(file_name="", file_path="")
        with patch.object(self.driver, "cached_file") as fetch:
            self.driver.setup("fixture-task", str(self.workspace))
        fetch.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_setup_preserves_existing_file_and_refuses_a_dangling_symlink(self):
        destination = self.workspace / "note.txt"
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    destination.symlink_to(self.root / "missing-target")
                else:
                    destination.write_text("existing")
                with patch.object(self.driver, "cached_file", return_value=self.attachment), \
                        self.assertRaisesRegex(ValueError, "destination already exists"):
                    self.driver.setup("fixture-task", str(self.workspace))
                if not linked:
                    self.assertEqual(destination.read_text(), "existing")
                destination.unlink()
        self.assertFalse((self.root / "missing-target").exists())

    def test_score_delegates_the_last_answer_marker_to_the_official_scorer(self):
        with patch.object(self.driver.SCORER, "question_scorer", return_value=True) as scorer:
            result = self.driver.score("fixture-task", str(self.workspace), [],
                                       "FINAL ANSWER: earlier\nReasoning\nFINAL ANSWER: chosen answer \n")
        scorer.assert_called_once_with("chosen answer", "synthetic-reference")
        self.assertEqual((result["reward"], result["passed"]), (1.0, True))
        self.assertEqual(result["detail"]["dataset_revision"], self.driver.SOURCE["revision"])
        with patch.object(self.driver.SCORER, "question_scorer", return_value=False) as scorer:
            result = self.driver.score("fixture-task", str(self.workspace), [], "FINAL ANSWER: wrong")
        scorer.assert_called_once_with("wrong", "synthetic-reference")
        self.assertEqual((result["reward"], result["passed"]), (0.0, False))

    def test_a_reply_without_the_marker_is_scored_whole(self):
        for answer, given in (("synthetic-reference", "synthetic-reference"), (" final answer: x \n", "final answer: x")):
            with self.subTest(answer=answer), patch.object(self.driver.SCORER, "question_scorer", return_value=True) as scorer:
                result = self.driver.score("fixture-task", str(self.workspace), [], answer)
                scorer.assert_called_once_with(given, "synthetic-reference")
                self.assertEqual(result["reward"], 1.0)
                self.assertFalse(result["detail"]["marker"])

    def test_empty_answers_fail_without_asking_the_scorer(self):
        for answer in (None, {}, "", "  \n", "FINAL ANSWER: \n"):
            with self.subTest(answer=answer), patch.object(self.driver.SCORER, "question_scorer") as scorer:
                result = self.driver.score("fixture-task", str(self.workspace), [], answer)
                scorer.assert_not_called()
                self.assertEqual(result["reward"], 0.0)

    def test_original_calls_the_model_with_only_the_official_prefix_and_question(self):
        reply = {"choices": [{"message": {"content": "FINAL ANSWER: unchanged"}}]}
        with patch.object(self.driver.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as call:
            result = self.driver.answer("fixture-task", str(self.workspace), "original", "fixture-model",
                                        "https://fixture.invalid/v1/", "fixture-key")
        self.assertEqual(result, {"answer": "FINAL ANSWER: unchanged", "model": "fixture-model"})
        self.assertEqual(call.call_args.kwargs, {"timeout": 600})                     # a stalled endpoint never holds the driver
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://fixture.invalid/v1/chat/completions")
        self.assertEqual(json.loads(request.data), {"model": "fixture-model", "messages": [
            {"role": "system", "content": self.driver.PREFIX},
            {"role": "user", "content": self.row["Question"]}]})
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_original_preserves_temperature_choice_and_rejects_unknown_setup(self):
        reply = b'{"choices":[{"message":{"content":"answer"}}]}'
        with patch.dict(os.environ, {"NULLIUS_OPTION_TEMPERATURE": "0.25"}), \
                patch.object(self.driver.urllib.request, "urlopen", return_value=io.BytesIO(reply)) as call:
            self.driver.answer("fixture-task", str(self.workspace), "original", "model", "https://fixture.invalid", "key")
            self.assertEqual(json.loads(call.call_args.args[0].data)["temperature"], .25)
        with patch.object(self.driver.urllib.request, "urlopen") as call, self.assertRaisesRegex(ValueError, "only the original"):
            self.driver.answer("fixture-task", str(self.workspace), "unknown", "model", "https://fixture.invalid", "key")
        call.assert_not_called()

    def test_original_refuses_missing_or_nontext_model_output(self):
        for reply in ({}, {"choices": []}, {"choices": [{"message": {"content": []}}]},
                      {"choices": [{"message": {"content": " "}}]}):
            with self.subTest(reply=reply), patch.object(self.driver.urllib.request, "urlopen",
                    return_value=io.BytesIO(json.dumps(reply).encode())), self.assertRaisesRegex(RuntimeError, "no textual answer"):
                self.driver.answer("fixture-task", str(self.workspace), "original", "model", "https://fixture.invalid", "key")

    def test_units_names_only_selected_split_and_oracle_exposes_reference_for_leak_check(self):
        self.assertEqual(self.driver.units(DATASET), {"units": ["fixture-task"]})
        with self.assertRaisesRegex(ValueError, "differs"):
            self.driver.units("gaia-validation")
        result = self.driver.oracle("fixture-task", str(self.workspace))
        self.assertEqual(result["answer"], "FINAL ANSWER: synthetic-reference")
        self.assertEqual((self.workspace / ".gaia-oracle-answer.txt").read_text(), "synthetic-reference")

    def test_cached_data_do_not_require_a_token_and_downloads_keep_the_pin(self):
        cache = Mock(return_value=str(self.attachment))
        download = Mock(return_value=str(self.attachment))
        module = SimpleNamespace(try_to_load_from_cache=cache, hf_hub_download=download)
        with patch.dict("sys.modules", {"huggingface_hub": module}):
            self.assertEqual(self.driver.cached_file("2023/validation/note.txt"), self.attachment)
            download.assert_not_called()
            cache.return_value = None
            with self.assertRaisesRegex(RuntimeError, "Settings → Data providers → Hugging Face"):
                self.driver.cached_file("2023/validation/note.txt")
            download.assert_not_called()
            with patch.dict(os.environ, {"HF_TOKEN": "synthetic-fixture-token"}):
                self.driver.cached_file("2023/validation/note.txt")
        self.assertEqual(download.call_args.kwargs["revision"], self.driver.SOURCE["revision"])
        self.assertEqual(download.call_args.kwargs["repo_type"], "dataset")
        self.assertEqual(download.call_args.kwargs["token"], "synthetic-fixture-token")

    def metadata(self, rows):
        self.driver.ROWS = {}
        source = copy.deepcopy(self.driver.SOURCE)
        source["datasets"][DATASET]["units"] = 1
        source["datasets"][DATASET]["sha256"] = hashlib.sha256(self.attachment.read_bytes()).hexdigest()
        self.driver.SOURCE = source
        dataset = SimpleNamespace(load_dataset=Mock(return_value=rows))
        return dataset

    def test_load_checks_pinned_metadata_and_returns_each_task_once(self):
        dataset = self.metadata([self.row])
        with patch.dict("sys.modules", {"datasets": dataset}), patch.object(self.driver, "cached_file", return_value=self.attachment) as fetch:
            self.assertEqual(self.driver.load(), {"fixture-task": self.row})
            self.driver.load()
        fetch.assert_called_once_with(self.driver.SOURCE["datasets"][DATASET]["file"])
        dataset.load_dataset.assert_called_once()

    def test_every_dataset_pins_its_metadata_by_checksum(self):
        for key, definition in self.driver.SOURCE["datasets"].items():
            with self.subTest(key):
                self.assertRegex(definition["sha256"], r"^[0-9a-f]{64}$")

    def test_load_refuses_metadata_whose_checksum_differs(self):
        dataset = self.metadata([self.row])
        self.driver.SOURCE["datasets"][DATASET]["sha256"] = "0" * 64
        with patch.dict("sys.modules", {"datasets": dataset}), patch.object(self.driver, "cached_file", return_value=self.attachment), \
                self.assertRaisesRegex(ValueError, "checksum differs"):
            self.driver.load()
        dataset.load_dataset.assert_not_called()

    def test_load_rejects_invalid_rows_before_caching_them(self):
        cases = [(dict(self.row, task_id=""), "nonempty"),
                 (dict(self.row, Question=" "), "question is missing"),
                 (dict(self.row, **{"Final answer": ""}), "reference answer is missing"),
                 (dict(self.row, Level=2), "level differs")]
        for row, message in cases:
            with self.subTest(message=message):
                dataset = self.metadata([row])
                with patch.dict("sys.modules", {"datasets": dataset}), \
                        patch.object(self.driver, "cached_file", return_value=self.attachment), self.assertRaisesRegex(ValueError, message):
                    self.driver.load()
                self.assertEqual(self.driver.ROWS, {})
        for rows, message in (([self.row, self.row], "unique"), ([], "task count differs")):
            dataset = self.metadata(rows)
            with patch.dict("sys.modules", {"datasets": dataset}), \
                    patch.object(self.driver, "cached_file", return_value=self.attachment), self.assertRaisesRegex(ValueError, message):
                self.driver.load()

    def test_load_rejects_checksum_mismatch_before_parsing(self):
        dataset = self.metadata([self.row])
        self.attachment.write_text("changed bytes")
        with patch.dict("sys.modules", {"datasets": dataset}), \
                patch.object(self.driver, "cached_file", return_value=self.attachment), self.assertRaisesRegex(ValueError, "checksum differs"):
            self.driver.load()
        dataset.load_dataset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
