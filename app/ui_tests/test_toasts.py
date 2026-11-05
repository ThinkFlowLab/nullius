"""A notice a page is sent (Flask's flash) is a toast, the same on every page:
it goes by itself - an error stays twice as long - or on its ×, and it is
never an alert written into the page."""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from .test_routes import web


class Toasts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()

    def page(self, url):
        r = self.client.get(url); self.addCleanup(r.close)
        return r.get_data(as_text=True)

    def test_a_notice_is_a_toast_once_on_any_page(self):
        for url in ("/settings", "/workspaces", "/benchmarks"):
            with self.subTest(url):
                with self.client.session_transaction() as s:
                    s["_flashes"] = [("ok", "Removed workspace glossy-ferret."), ("error", "That did not work.")]
                html = self.page(url)
                self.assertIn('<div class="toast ok"><span class="toast-text">Removed workspace glossy-ferret.</span>', html)
                self.assertIn('<div class="toast bad"><span class="toast-text">That did not work.</span>', html)
                self.assertEqual(html.count("Removed workspace glossy-ferret."), 1)        # not also an alert in the page
                self.assertNotIn("glossy-ferret", self.page(url))                           # and gone on the next page
