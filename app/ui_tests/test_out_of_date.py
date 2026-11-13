"""A page out of date: the form token a restart forgot, and the interface an
upgrade replaced. The server says which, and every request of a page goes
through the one helper that reads it."""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from .test_routes import web

APP = Path(__file__).resolve().parents[1]
REASON = "X-Nullius-Reason"


class OutOfDate(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(web.app.config, TESTING=True); patcher.start(); self.addCleanup(patcher.stop)
        self.client = web.app.test_client()

    def token(self):
        self.client.get("/settings").close()
        with self.client.session_transaction() as s:
            return s["csrf"]

    def probe(self, csrf, **kw):
        r = self.client.post("/settings/hosts/test", headers={"X-CSRF-Token": csrf, **kw.pop("headers", {})},
                             data={"entry": "nowhere"}, **kw)
        self.addCleanup(r.close)
        return r

    def test_a_token_the_session_does_not_hold_is_stale(self):
        self.token()
        r = self.probe("from-before-a-restart")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.headers.get(REASON), "stale-form-token")

    def test_other_refusals_are_not_stale(self):
        token = self.token()
        missing = self.client.post("/settings/hosts/test", data={"entry": "nowhere"}); self.addCleanup(missing.close)
        cross = self.probe(token, headers={"Origin": "http://elsewhere.example"})
        remote = self.probe(token, environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        for r in (missing, cross, remote):
            self.assertEqual(r.status_code, 403)
            self.assertIsNone(r.headers.get(REASON))

    def test_every_response_names_the_interface_its_page_was_rendered_with(self):
        page = self.client.get("/settings"); self.addCleanup(page.close)
        served = page.headers["X-Nullius-Interface"]
        self.assertIn(f'<meta name="nullius-interface" content="{served}">', page.get_data(as_text=True))
        with patch.object(web.shared, "INTERFACE", "upgraded"):
            later = self.client.get("/settings"); self.addCleanup(later.close)
        self.assertEqual(later.headers["X-Nullius-Interface"], "upgraded")

    def test_the_digest_covers_templates_static_files_and_web_routes(self):
        walked, walk = [], os.walk
        with patch.object(web.shared.os, "walk", lambda root: walked.append(root) or walk(root)):
            first = web.shared._interface_id()
        self.assertEqual([os.path.basename(root) for root in walked], ["templates", "static", "web"])
        self.assertEqual(first, web.shared._interface_id())

    def test_every_request_of_a_page_goes_through_the_helper(self):
        bare = [f"{path.relative_to(APP)}:{n}" for path in [*APP.glob("static/**/*.js"), *APP.glob("templates/**/*.html")]
                if path.name != "notifications.js"
                for n, line in enumerate(path.read_text().splitlines(), 1) if re.search(r"(?<![\w.$])fetch\(", line)]
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
