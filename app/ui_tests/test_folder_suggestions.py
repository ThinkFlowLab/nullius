"""The path picker reads only matching folders and accepts linked run roots."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ui_tests.test_routes import web


class FolderSuggestions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "folder-test"

    def suggest(self, path):
        response = self.client.post("/api/folders", data={"path": str(path)},
                                    headers={"X-CSRF-Token": "folder-test"})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json["paths"]

    def test_partial_path_returns_sorted_directories_including_links(self):
        disk = self.root / "mounted disk"
        disk.mkdir()
        (self.root / "run-link").symlink_to(disk, target_is_directory=True)
        (self.root / "run-two").mkdir()
        (self.root / "run-file").write_text("not a folder")
        (self.root / "run-missing").symlink_to(self.root / "missing")
        (disk / "nested").mkdir()
        self.assertEqual(self.suggest(self.root / "run-"),
                         [str(self.root / "run-link") + "/", str(self.root / "run-two") + "/"])
        self.assertEqual(self.suggest(str(self.root / "run-link") + "/"),
                         [str(self.root / "run-link/nested") + "/"])

    def test_home_relative_paths_and_hidden_folders(self):
        (self.root / "visible").mkdir()
        (self.root / ".hidden").mkdir()
        with patch("os.path.expanduser", side_effect=lambda value: value.replace("~", str(self.root), 1)):
            self.assertEqual(self.suggest("~/v"), [str(self.root / "visible") + "/"])
            self.assertEqual(self.suggest("~"), [str(self.root / "visible") + "/"])
            self.assertEqual(self.suggest("~/."), [str(self.root / ".hidden") + "/"])
        with patch("os.getcwd", return_value=str(self.root)):
            self.assertEqual(self.suggest("vis"), [str(self.root / "visible") + "/"])

    def test_results_are_bounded_and_missing_or_unreadable_paths_are_quiet(self):
        for i in range(25):
            (self.root / f"folder-{i:02}").mkdir()
        self.assertEqual(len(self.suggest(str(self.root) + "/")), 20)
        for value in ("", str(self.root / "missing/"), str(self.root / "*/"), "\0", "x" * 4097):
            self.assertEqual(self.suggest(value), [])
        with patch("os.scandir", side_effect=PermissionError):
            self.assertEqual(self.suggest(str(self.root) + "/"), [])

    def test_listing_requires_a_valid_same_origin_form_token(self):
        for headers in ({}, {"X-CSRF-Token": "stale"},
                        {"X-CSRF-Token": "folder-test", "Origin": "https://foreign.invalid"}):
            with patch("os.scandir", side_effect=AssertionError("must not scan")):
                response = self.client.post("/api/folders", data={"path": str(self.root)}, headers=headers)
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 403)

    def test_linked_job_directory_can_be_imported(self):
        disk = self.root / "other-disk"
        job = disk / "jobs/test-run"
        job.mkdir(parents=True)
        (job / "config.json").write_text(json.dumps({"agents": [{"name": "fixture"}]}))
        alias = self.root / "linked-disk"
        alias.symlink_to(disk, target_is_directory=True)
        with patch.object(web.shared, "adopt_roots") as adopt:
            response = self.client.post("/use-path", data={"csrf": "folder-test", "path": str(alias / "jobs"), "new": "1"})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 302)
        adopt.assert_called_once_with([str(alias / "jobs")], label="", new=True)
