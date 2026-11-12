"""A page out of date: the form token a restart forgot. The server says so,
apart from its other refusals."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from .test_routes import web

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


if __name__ == "__main__":
    unittest.main()
