"""A benchmark's own records on a trial's Artifacts tab: those its benchmark.json declares and the trial holds,
after Nullius's, read and downloaded through the same routes; a name is text, never markup. The runner fixture is
copied and the records written by each test; the declaration is given by a stand-in; nothing is run."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from urllib.parse import quote
from werkzeug.http import parse_options_header
from contextlib import ExitStack
from unittest.mock import patch

from nullius.runs import artifacts, parse
from .test_routes import APP_DIR, web

NATIVE = APP_DIR / "tests" / "fixtures" / "native"
RECORDS = [{"label": "The task's text", "path": "verifier/own/brief.md"}, {"label": "Its folder", "path": "verifier/own"}]


class TrialRecords(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "parts-arm")
        shutil.copytree(NATIVE / "parts-arm", self.root)
        own = os.path.join(self.root, "t1", "verifier", "own")
        os.makedirs(own)
        with open(os.path.join(own, "brief.md"), "w") as f:
            f.write("Set the field, then press Apply.\n")
        with open(os.path.join(own, "<i>odd<i>.log"), "w") as f:
            f.write("a line\n")
        self.job = parse.parse_job(self.root)
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=[self.job]))
        self.stack.enter_context(patch.object(artifacts, "declared", side_effect=lambda source: [dict(r) for r in RECORDS]
                                              if source == "sums" else []))
        self.client = web.app.test_client()

    def get(self, path):
        response = self.client.get(path)
        self.addCleanup(response.close)
        return response

    def test_the_tab_lists_the_declared_records_after_nullius_s(self):
        html = self.get(f"/trial/{self.job.job_id}/t1").get_data(as_text=True)
        tab = html.split('id="trial-artifacts"', 1)[1].split("</section>", 1)[0]
        self.assertIn("<td>The task's text</td>", tab.replace("&#39;", "'"))
        self.assertIn('Its folder <span class="mono muted">brief.md</span>', tab)
        self.assertIn('Its folder <span class="mono muted">&lt;i&gt;odd&lt;i&gt;.log</span>', tab)
        self.assertNotIn("<i>odd", tab)
        self.assertIn(f"/trial/{self.job.job_id}/t1/artifact/r0", tab)
        self.assertIn(f"/trial/{self.job.job_id}/t1/artifact/r1-brief.md/download", tab)
        head = '<tr class="artifact-declared"><td colspan="3" class="muted">The benchmark\'s own records</td></tr>'
        self.assertEqual(tab.replace("&#39;", "'").count(head), 1)      # one heading, between Nullius's and its own
        before, after = tab.replace("&#39;", "'").split(head)
        self.assertIn("Result record", before)
        self.assertNotIn("Its folder", before)
        self.assertIn("<td>The task's text</td>", after.replace("&#39;", "'"))

    def test_the_tab_says_when_a_folder_has_more_records(self):
        path = f"/trial/{self.job.job_id}/t1"
        with patch.object(artifacts, "FOLDER_FILES", 1):
            html = self.get(path).get_data(as_text=True)
        tab = html.split('id="trial-artifacts"', 1)[1].split("</section>", 1)[0]
        self.assertIn("More records in Its folder.", tab)
        self.assertNotIn("More records in", self.get(path).get_data(as_text=True))
        with patch.object(artifacts, "declared", return_value=[]):
            plain = self.get(path).get_data(as_text=True)
        self.assertNotIn("More records in", plain)
        self.assertNotIn("Its folder", plain)

    def test_a_declared_record_is_read_and_downloaded_by_its_key(self):
        page = self.get(f"/trial/{self.job.job_id}/t1/artifact/r1-brief.md")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("verifier/own/brief.md", html)
        self.assertIn("<title>Its folder: brief.md", html)                     # a folder's file under its own name
        self.assertIn("Set the field, then press Apply.", html)
        got = self.get(f"/trial/{self.job.job_id}/t1/artifact/r0/download")
        self.assertEqual((got.status_code, got.headers["Content-Disposition"]), (200, 'attachment; filename="brief.md"'))
        self.assertEqual(got.get_data(as_text=True), "Set the field, then press Apply.\n")

    def test_download_names_are_quoted_without_controls_and_keep_unicode(self):
        for name, clean in [('a"quoted\\name.txt', 'a"quoted\\name.txt'),
                            ('résumé-文件.txt', 'résumé-文件.txt')]:
            with self.subTest(name=name):
                with open(os.path.join(self.root, "t1", "verifier", "own", name), "w") as handle:
                    handle.write("record")
                response = self.get(f"/trial/{self.job.job_id}/t1/artifact/{quote('r1-' + name, safe='')}/download")
                self.assertEqual(response.status_code, 200)
                header = response.headers["Content-Disposition"]
                self.assertTrue(header.startswith('attachment; filename="'))
                self.assertFalse(any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in header))
                self.assertEqual(parse_options_header(header), ("attachment", {"filename": clean}))
                if not clean.isascii():
                    self.assertIn("filename*=UTF-8''", header)
                    self.assertTrue(header.isascii())
                self.assertEqual(response.get_data(as_text=True), "record")

    def test_a_key_outside_the_declaration_or_of_a_name_not_printable_is_not_found(self):
        own = os.fsencode(os.path.join(self.root, "t1", "verifier", "own"))
        for name in (b"bad-\xff.txt", b"line\nbreak\r\t\x01\x7f\x85.txt"):
            with open(os.path.join(own, name), "w") as handle:
                handle.write("record")
        page = self.get(f"/trial/{self.job.job_id}/t1")
        self.assertEqual(page.status_code, 200)
        tab = page.get_data(as_text=True).split('id="trial-artifacts"', 1)[1].split("</section>", 1)[0]
        self.assertIn("/artifact/r1-brief.md", tab)
        self.assertNotIn("/artifact/r1-bad", tab)                      # a name not printable, or not UTF-8, is left out
        self.assertNotIn("/artifact/r1-line", tab)
        for key in ("r2", "r1-..", "r1-%2E%2E", "r1-missing.md",
                    quote(b"r1-bad-\xff.txt"), quote("r1-line\nbreak\r\t\x01\x7f\x85.txt", safe="")):
            with self.subTest(key=key):
                self.assertEqual(self.get(f"/trial/{self.job.job_id}/t1/artifact/{key}").status_code, 404)
                self.assertEqual(self.get(f"/trial/{self.job.job_id}/t1/artifact/{key}/download").status_code, 404)


if __name__ == "__main__":
    unittest.main()
