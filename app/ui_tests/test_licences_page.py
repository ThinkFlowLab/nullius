"""A benchmark's licences on its page: per environment, what each needs and
whether it is set - dates, never a value - with set, replace, remove and,
for a value in saved settings, *Move from saved settings*; every write
from this machine only, with the form token, each answered by a toast. The
builder's Dataset step reads the same states, by name."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import secrets
from nullius.standard import definitions
from nullius.standard import recipe as standard_recipe
from .test_agents import fixture_load
from .test_routes import web

ENVBENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "standard", "envbench")
KEY = "lic-0123456789-not-real"
FILE = b"FAKE-LICENCE-FILE 0123456789\n"


def envbench_as(key: str) -> dict:
    d, bad = definitions._read(os.path.join(ENVBENCH, "standard.json"), "benchmark", "envbench")
    assert not bad, bad
    return dict(definitions.resolve(d), key=key)


class LicencesPanel(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-licences-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = str(self.tmp / "settings.json")
        Path(self.env_file).write_text(json.dumps({"OPENAI_API_KEY": "sk-test-0123456789"}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(os.environ, {"SECRETS_DIR": str(self.tmp / "secrets"), "HOSTS": ""}))
        self.stack.enter_context(patch.object(web.shared, "ENV_FILE", self.env_file))
        # gsm8k, a listed benchmark of the standard, given the fixture's environments and their licences
        real = standard_recipe.licences_of
        def licences_of(key):
            with patch.object(standard_recipe, "_all", lambda: {"benchmark": [envbench_as(key)], "agent": [], "capability": []}):
                return real(key)
        self.stack.enter_context(patch.object(standard_recipe, "licences_of", licences_of))
        self.client = web.app.test_client()

    def page(self):
        r = self.client.get("/benchmarks/gsm8k"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def panel(self):
        html = self.page()
        return html[html.index('id="licences"'):html.index('id="datasets"')]

    def post(self, data, csrf=True, files=None, **kw):
        if csrf:
            self.page()
            with self.client.session_transaction() as s:
                data = dict(data, csrf=s["csrf"])
        if files:
            data.update(files)
        r = self.client.post("/benchmarks/gsm8k/licences", data=data, headers={"Origin": "http://localhost"},
                             content_type="multipart/form-data" if files else None, **kw)
        self.addCleanup(r.close)
        return r

    def toast(self):
        html = self.page()
        return re.findall(r'<div class="toast (ok|bad)"><span class="toast-text">(.*?)</span>', html)

    def test_the_panel_lists_each_licence_by_environment_and_shape(self):
        p = self.panel()
        self.assertIn('<span class="mono">licensed</span>', p)
        for name in ("FAKE_TOOL_LICENSE", "FAKE_TOOL_LICENSE_FILE", "FAKE_LICENSE_SERVER", "FAKE_SCORER_TOKEN"):
            self.assertIn(f'<b class="mono">{name}</b>', p)
        self.assertIn("a licence file", p)
        self.assertIn("/opt/licensed-tool/license.lic", p)
        self.assertIn("a licence server", p)
        self.assertEqual(p.count(">missing<"), 4)
        self.assertIn('type="file" name="file"', p)
        self.assertIn('name="csrf"', p)
        self.assertNotIn("Move from saved settings", p)

    def test_set_says_so_in_a_toast_and_never_shows_the_value(self):
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY, "expires": "2030-01-31"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/benchmarks/gsm8k#licences"))
        self.assertEqual(self.toast(), [("ok", "FAKE_TOOL_LICENSE set.")])
        p = self.panel()
        self.assertIn("expires 2030-01-31", p)
        self.assertEqual(p.count(">missing<"), 3)
        self.assertNotIn(KEY, self.page())
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE"), KEY.encode())
        self.assertNotIn("FAKE_TOOL_LICENSE", Path(self.env_file).read_text())       # never in Settings' file

    def test_a_licence_file_is_uploaded_whole(self):
        r = self.post({"name": "FAKE_TOOL_LICENSE_FILE"}, files={"file": (io.BytesIO(FILE), "tool.lic")})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE_FILE"), FILE)
        self.assertEqual(secrets.status("gsm8k")["FAKE_TOOL_LICENSE_FILE"]["filename"], "tool.lic")
        self.assertIn("tool.lic", self.panel())
        self.assertNotIn("FAKE-LICENCE-FILE", self.page())

    def test_what_is_refused_is_a_bad_toast_and_nothing_kept(self):
        self.post({"name": "FAKE_LICENSE_SERVER", "value": "not an address"})
        (kind, said), = self.toast()
        self.assertEqual(kind, "bad")
        self.assertIn("host:port", said)
        self.post({"name": "NOT_DECLARED", "value": KEY})
        self.assertEqual(self.toast(), [("bad", "NOT_DECLARED is not a licence this benchmark&#39;s environments need.")])
        self.assertEqual(secrets.status("gsm8k"), {})

    def test_remove(self):
        secrets.set("gsm8k", "FAKE_SCORER_TOKEN", KEY)
        self.assertIn("Remove", self.panel())
        self.post({"name": "FAKE_SCORER_TOKEN", "action": "delete"})
        self.assertEqual(self.toast(), [("ok", "FAKE_SCORER_TOKEN removed.")])
        self.assertEqual(secrets.status("gsm8k"), {})

    def test_a_migrated_licence_is_moved_from_settings_only_on_the_click(self):
        Path(self.env_file).write_text(json.dumps({"OPENAI_API_KEY": "sk-test-0123456789", "FAKE_TOOL_LICENSE": KEY}))
        p = self.panel()
        self.assertIn("Move from saved settings", p)
        self.assertEqual(secrets.status("gsm8k"), {})                                # never silently
        self.post({"name": "FAKE_TOOL_LICENSE", "action": "move"})
        self.assertEqual(self.toast(), [("ok", "FAKE_TOOL_LICENSE moved here from saved settings, and removed there.")])
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE"), KEY.encode())
        self.assertEqual(json.loads(Path(self.env_file).read_text()), {"OPENAI_API_KEY": "sk-test-0123456789"})
        self.assertNotIn("Move from saved settings", self.panel())

    def test_only_this_machine_with_the_form_token(self):
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY}, csrf=False)
        self.assertEqual(r.status_code, 403)
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY}, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(secrets.status("gsm8k"), {})
        r = self.client.get("/benchmarks/gsm8k", environ_base={"REMOTE_ADDR": "10.0.0.2"}); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        self.assertIn("set from a browser on the application host only", html)
        self.assertNotIn('action="/benchmarks/gsm8k/licences"', html)


class BuilderStates(unittest.TestCase):
    def test_the_builder_reads_each_benchmarks_licence_states_by_name(self):
        stack = ExitStack(); self.addCleanup(stack.close)
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        stack.enter_context(patch.dict(web.app.config, TESTING=True))
        stack.enter_context(patch.dict(os.environ, {"SECRETS_DIR": tmp}))
        stack.enter_context(patch.object(definitions, "load", lambda kind, root=None, every=False:
                                         ([envbench_as("envbench")], []) if kind == "benchmark" else fixture_load(kind, root, every)))
        secrets.set("envbench", "FAKE_TOOL_LICENSE", KEY, expires="2001-01-01")
        r = web.app.test_client().get("/new-run"); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        got = schema["standardHere"]["licences"]
        self.assertEqual(list(got), ["envbench"])
        self.assertEqual(got["envbench"]["FAKE_TOOL_LICENSE"]["expired"], True)
        self.assertNotIn(KEY, html)
        self.assertNotIn("secrets", schema["standardHere"])
        self.assertNotIn("Set it in Settings", html)


if __name__ == "__main__":
    unittest.main()
