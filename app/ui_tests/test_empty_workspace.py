"""New workspace with no path makes an empty workspace; adding runs still needs a path."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ui_tests.test_routes import web
from nullius import workspace


class EmptyWorkspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "workspaces"
        for target in (patch.object(web.shared, "WS_HOME", str(self.home)),
                       patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []})):
            target.start()
            self.addCleanup(target.stop)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "empty-test"

    def post(self, route, **data):
        response = self.client.post(route, data={"csrf": "empty-test", **data})
        self.addCleanup(response.close)
        return response

    def test_a_name_alone_creates_and_opens_an_empty_workspace(self):
        response = self.post("/use-path", name="Scratch", path="", new="1")
        self.assertEqual(response.status_code, 302)
        ws = web.shared.STATE["workspace"]
        self.assertEqual((ws.name, ws.label, ws.roots), ("scratch", "Scratch", []))
        self.assertEqual(json.loads((self.home / "scratch/workspace.json").read_text())["roots"], [])
        html = self.client.get("/runs").get_data(as_text=True)
        self.assertIn("Add runs to Scratch", html)
        self.assertNotIn("Nothing job-shaped", html)

    def test_a_blank_name_gives_two_random_words(self):
        self.post("/use-path", new="1")
        ws = web.shared.STATE["workspace"]
        adjective, noun = ws.name.split("-")
        self.assertIn(adjective, workspace.ADJECTIVES)
        self.assertIn(noun, workspace.NOUNS)

    def test_adding_nothing_to_the_open_workspace_creates_nothing(self):
        for route in ("/use-path", "/upload"):
            with self.subTest(route=route):
                self.post(route, new="0")
                self.assertIsNone(web.shared.STATE["workspace"])
                self.assertFalse(self.home.exists())
        with self.client.session_transaction() as session:
            self.assertIn("nothing to scan", str(session.get("_flashes")))

    def test_a_path_still_goes_to_its_roots(self):
        job = self.root / "jobs/test-run"
        job.mkdir(parents=True)
        (job / "config.json").write_text(json.dumps({"agents": [{"name": "fixture"}]}))
        with patch.object(web.shared, "adopt_roots") as adopt:
            self.post("/use-path", name="", path=str(self.root / "jobs"), new="1")
        adopt.assert_called_once_with([str(self.root / "jobs")], label="", new=True)
        self.assertFalse(self.home.exists())

    def test_a_missing_path_creates_nothing(self):
        self.post("/use-path", name="Scratch", path=str(self.root / "missing"), new="1")
        self.assertIsNone(web.shared.STATE["workspace"])
        self.assertFalse(self.home.exists())

    def test_the_form_needs_its_token(self):
        response = self.client.post("/use-path", data={"name": "Scratch", "new": "1"})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.home.exists())
