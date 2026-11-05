"""Switching catalogue sources removes only the chosen user override."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nullius.catalogue import catalog, storage
from nullius.standard import definitions
from .test_routes import web


class UseBundled(unittest.TestCase):
    entries = (("benchmarks", "gsm8k", "benchmark.json"),
               ("agents", "reference", "agent.json"),
               ("capabilities", "keyword-memory", "capability.json"))

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, NULLIUS_HOME=str(self.root),
                                     NULLIUS_CATALOGUE=str(self.root / "catalogue")))
        self.enterContext(patch.dict(web.shared.STATE, workspace=None, roots=[], job_roots=[], label=""))
        self.enterContext(patch.object(web.shared, "WS_HOME", str(self.root / "workspaces")))
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "restore-test"

    def override(self, group, key, filename):
        bundled = catalog.ROOT if group == "benchmarks" else definitions.CATALOGUE
        folder = Path(storage.writable_dir(group, key, bundled))
        file = folder / filename
        data = json.loads(file.read_text())
        data["title" if group == "benchmarks" else "label"] = "Customized entry"
        file.write_text(json.dumps(data))
        return folder

    def test_all_entry_pages_restore_their_bundle_and_clear_the_action(self):
        for group, key, filename in self.entries:
            with self.subTest(group=group):
                page = f"/{group}/{key}"
                self.assertNotIn(b"Use bundled entry", self.client.get(page).data)
                folder = self.override(group, key, filename)
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Customized entry", response.data)
                self.assertIn(b"Keep my copy", response.data)
                endpoint = f"/catalogue/{group}/{key}/use-bundled"
                self.assertIn(f'action="{endpoint}"'.encode(), response.data)
                self.assertTrue(folder.exists(), "viewing the action must not remove anything")
                self.assertEqual(self.client.get(endpoint).status_code, 405)
                response = self.client.post(endpoint, data={"csrf": "restore-test"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, page)
                self.assertFalse(folder.exists())
                response = self.client.get(response.location)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"Customized entry", response.data)
                self.assertNotIn(b"Use bundled entry", response.data)

    def test_restore_requires_local_same_origin_csrf_and_a_bundled_counterpart(self):
        folder = self.override(*self.entries[0])
        endpoint = "/catalogue/benchmarks/gsm8k/use-bundled"
        for options in ({"data": {}}, {"data": {"csrf": "wrong"}},
                        {"data": {"csrf": "restore-test"}, "headers": {"Origin": "https://foreign.invalid"}},
                        {"data": {"csrf": "restore-test"}, "environ_overrides": {"REMOTE_ADDR": "203.0.113.9"}}):
            self.assertEqual(self.client.post(endpoint, **options).status_code, 403)
            self.assertTrue(folder.exists())
        custom = folder.with_name("custom")
        custom.mkdir()
        self.assertEqual(self.client.post("/catalogue/benchmarks/custom/use-bundled", data={"csrf": "restore-test"}).status_code, 409)
        self.assertTrue(custom.exists())
        self.assertEqual(self.client.post("/catalogue/other/gsm8k/use-bundled", data={"csrf": "restore-test"}).status_code, 404)

    def test_selected_user_version_is_dropped_when_restoring(self):
        folder = Path(storage.writable_dir("agents", "codex", definitions.CATALOGUE))
        response = self.client.post("/catalogue/agents/codex/use-bundled?v=custom-version", data={"csrf": "restore-test"})
        self.assertEqual(response.location, "/agents/codex")
        self.assertFalse(folder.exists())
