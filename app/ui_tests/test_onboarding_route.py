"""Getting started on the pages: every page carries the guide and its tour,
whose stops are this application's own pages, and the checklist it reads
holds booleans and addresses, never a key."""
from __future__ import annotations

import html
import json
import os
import re
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from .test_routes import web


class Onboarding(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()

    def get(self, url):
        r = self.client.get(url, follow_redirects=True); self.addCleanup(r.close)
        return r

    def test_every_page_carries_the_guide_and_a_tour_of_real_pages(self):
        for url in ("/", "/runs", "/benchmarks", "/settings"):
            with self.subTest(url):
                page = self.get(url).get_data(as_text=True)
                m = re.search(r'<script src="[^"]*onboarding\.js" data-state="([^"]+)" data-tour="([^"]+)"', page)
                self.assertTrue(m, "no guide on " + url)
                tour = json.loads(html.unescape(m.group(2)))
                self.assertEqual(tour[-1]["sel"], "#onb-panel")
                for stop in tour:
                    self.assertEqual(self.get(stop["url"]).status_code, 200, stop["url"])
        runs = self.get("/runs").get_data(as_text=True)
        self.assertIn('id="onb-open"', runs)                                             # the rail entry, shown once asked
        self.assertIn('data-onb="dismiss"', runs)                                        # and its ×, which removes it unfinished
        settings = self.get("/settings").get_data(as_text=True)
        self.assertIn('href="#general" data-section="general"', settings)             # Settings' General tab
        self.assertIn('data-onb="replay"', settings)                                     # replays the onboarding
        self.assertIn('data-onb="restore"', settings)                                    # and brings a removed checklist back

    def test_the_checklist_holds_no_key(self):
        with patch.dict(os.environ, OPENROUTER_API_KEY="sk-or-secret-1", TYPESAFE_API_KEY="ts-secret-1"):
            r = self.get("/onboarding")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertNotIn("secret", body)
        subs = {x["id"]: x for s in r.get_json()["sections"] for x in s["subs"]}
        self.assertIs(subs["provider"]["done"], True)
        self.assertIs(subs["judge"]["done"], True)
        for x in subs.values():
            self.assertIn(x["done"], (True, False, None))
            self.assertEqual(self.get(x["url"]).status_code, 200, x["url"])


if __name__ == "__main__":
    unittest.main()
