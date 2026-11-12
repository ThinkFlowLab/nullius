"""Custom keys in Settings: what each needs and whether it is set
- dates, never a value - with set, replace, remove and,
for a value in saved settings, *Move from saved settings*; every write
from this machine only, with the form token, each answered by a toast. The
builder's Dataset step reads the same states, by name. A data provider's
token is never one of them: its provider's store answers for it."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from html.parser import HTMLParser
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
LICENCES_OF = standard_recipe.licences_of
HF = {"name": "HF_TOKEN", "to": "driver", "provider": "huggingface", "summary": "a data provider's token"}


class Forms(HTMLParser):
    """Record form nesting before a browser can repair invalid markup."""
    def __init__(self, html):
        super().__init__()
        self.stack, self.forms, self.nested = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            attrs = dict(attrs)
            self.forms.append(attrs)
            if self.stack:
                self.nested.append((self.stack[-1], attrs))
            self.stack.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form" and self.stack:
            self.stack.pop()


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
        self.listed(envbench_as("gsm8k"))
        self.client = web.app.test_client()

    def listed(self, bench):
        """gsm8k's credentials, as ``bench`` declares them."""
        def licences_of(key):
            if key != "gsm8k":
                return []
            with patch.object(standard_recipe, "_all", lambda: {"benchmark": [bench], "agent": [], "capability": []}):
                return LICENCES_OF(key)
        self.stack.enter_context(patch.object(standard_recipe, "licences_of", licences_of))

    def page(self, path="/settings", **kwargs):
        r = self.client.get(path, **kwargs); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def panel(self):
        html = self.page()
        start = html.index('id="custom-keys"')
        return html[start:html.index("</section>", start)]

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

    def test_the_panel_lists_benchmark_access_without_runtime_details(self):
        p = self.panel()
        self.assertIn('id="access-gsm8k"', p)
        self.assertIn('<h3 class="st-key-group-title">GSM8K</h3>', p)                     # its benchmark's title, not its key
        for name in ("FAKE_TOOL_LICENSE", "FAKE_TOOL_LICENSE_FILE", "FAKE_LICENSE_SERVER", "FAKE_SCORER_TOKEN"):
            self.assertIn(f'<h3>{name}</h3>', p)
        self.assertIn("the licensed tool&#39;s licence file", p)
        self.assertIn("the licensed tool&#39;s licence server", p)
        self.assertNotIn("/opt/licensed-tool/license.lic", p)
        self.assertEqual(p.count("Not set"), 4)
        self.assertRegex(p, r'<input[^>]*type="file"[^>]*name="file"')
        self.assertIn('type="password"', p)
        self.assertIn('form="access-gsm8k-FAKE_TOOL_LICENSE-set"', p)
        self.assertNotIn("Move from saved settings", p)
        key_card = p.split('data-key-card="access-gsm8k-FAKE_TOOL_LICENSE"', 1)[1].split('</article>', 1)[0]
        file_card = p.split('data-key-card="access-gsm8k-FAKE_TOOL_LICENSE_FILE"', 1)[1].split('</article>', 1)[0]
        self.assertNotIn('name="expires"', key_card)
        self.assertIn('name="expires"', file_card)

    def test_custom_keys_combines_benchmark_and_agent_fields_in_one_tab(self):
        html = self.page()
        panel = self.panel()
        self.assertIn('id="access-gsm8k"', panel)
        self.assertIn('id="AGENT_KEYS"', panel)
        self.assertIn('name="agent_key_name"', panel)
        self.assertEqual(html.count('data-section="custom-keys"'), 1)
        for old in ("agent-keys", "benchmark-access"):
            self.assertNotIn(f'data-section="{old}"', html)

    def test_expired_credential_has_an_edit_action_and_expired_state(self):
        secrets.set("gsm8k", "FAKE_SCORER_TOKEN", KEY, expires="2001-01-01")
        panel = self.panel()
        card = panel.split('data-key-card="access-gsm8k-FAKE_SCORER_TOKEN"', 1)[1].split('</article>', 1)[0]
        self.assertIn('data-key-state="expired">Expired', card)
        self.assertIn('data-key-label="Edit"', card)
        self.assertIn('aria-controls="access-gsm8k-FAKE_SCORER_TOKEN-editor"', card)
        self.assertNotIn(KEY, card)

    def test_benchmark_page_omits_credential_help_and_editor(self):
        html = self.page("/benchmarks/gsm8k")
        self.assertNotIn('/settings#access-gsm8k', html)
        self.assertNotIn('action="/benchmarks/gsm8k/licences"', html)
        self.assertNotIn('name="expires"', html)
        self.assertNotIn("FAKE_TOOL_LICENSE", html)

    def test_access_forms_are_outside_the_general_settings_form(self):
        forms = Forms(self.page())
        self.assertFalse(forms.nested)
        self.assertTrue(any(f.get("id") == "settings-form" for f in forms.forms))
        self.assertTrue(any(f.get("action") == "/benchmarks/gsm8k/licences" for f in forms.forms))

    def test_set_says_so_in_a_toast_and_never_shows_the_value(self):
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY, "expires": "2030-01-31"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/settings#access-gsm8k"))
        self.assertEqual(self.toast(), [("ok", "FAKE_TOOL_LICENSE set.")])
        p = self.panel()
        self.assertIn("Expires 2030-01-31", p)
        self.assertEqual(p.count("Not set"), 3)
        self.assertIn("Saved", p)
        self.assertNotIn(KEY, self.page())
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE"), KEY.encode())
        self.assertNotIn("FAKE_TOOL_LICENSE", Path(self.env_file).read_text())       # never in Settings' file

    def test_embedded_settings_keeps_the_embedded_return(self):
        html = self.page("/settings?embedded=1")
        form = re.search(r'<form id="access-gsm8k-FAKE_TOOL_LICENSE-set".*?</form>', html, re.S).group()
        self.assertIn('name="embedded" value="1"', form)
        self.assertIn('name="csrf"', form)
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY, "embedded": "1"})
        self.assertEqual(r.headers["Location"], "/settings?embedded=1#access-gsm8k")

    def test_replace_keeps_the_new_value_out_of_the_response(self):
        secrets.set("gsm8k", "FAKE_TOOL_LICENSE", KEY)
        self.assertIn('data-key-label="Edit"', self.panel())
        replacement = "replacement-licence-not-real"
        self.post({"name": "FAKE_TOOL_LICENSE", "value": replacement})
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE"), replacement.encode())
        self.assertNotIn(replacement, self.page())

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
        self.assertEqual(self.toast(), [("bad", "NOT_DECLARED is not a credential this benchmark needs.")])
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
        self.assertEqual(self.toast(), [("ok", "FAKE_TOOL_LICENSE moved out of saved settings.")])
        self.assertEqual(secrets.value("gsm8k", "FAKE_TOOL_LICENSE"), KEY.encode())
        self.assertEqual(json.loads(Path(self.env_file).read_text()), {"OPENAI_API_KEY": "sk-test-0123456789"})
        self.assertNotIn("Move from saved settings", self.panel())

    def test_only_this_machine_with_the_form_token(self):
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY}, csrf=False)
        self.assertEqual(r.status_code, 403)
        r = self.post({"name": "FAKE_TOOL_LICENSE", "value": KEY}, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(secrets.status("gsm8k"), {})
        html = self.page(environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertIn("Settings are written from a browser on the application host only", html)
        self.assertNotIn('action="/benchmarks/gsm8k/licences"', html)

    def test_a_data_providers_token_is_never_a_custom_key(self):
        bench = envbench_as("gsm8k")
        bench["environments"]["licensed"]["needs_secrets"].append(dict(HF))
        self.listed(bench)
        secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", KEY)
        needs = {s["name"]: s for e in standard_recipe.licences_of("gsm8k") for s in e["needs"]}
        self.assertTrue(needs["HF_TOKEN"]["status"]["set"])                          # its provider's, never gsm8k's
        p = self.panel()
        self.assertNotIn("HF_TOKEN", p)
        self.assertEqual(p.count("Not set"), 4)
        self.post({"name": "HF_TOKEN", "value": "hf-stale-not-real"})
        self.assertEqual(self.toast(), [("bad", "HF_TOKEN is set in Settings → Data providers.")])
        self.assertEqual(secrets.status("gsm8k"), {})


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

    def test_the_builder_reads_a_data_providers_token_from_its_provider_and_names_it(self):
        stack = ExitStack(); self.addCleanup(stack.close)
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        bench = envbench_as("envbench")
        bench["environments"]["licensed"]["needs_secrets"].append(dict(HF))
        stack.enter_context(patch.dict(web.app.config, TESTING=True))
        stack.enter_context(patch.dict(os.environ, {"SECRETS_DIR": tmp}))
        stack.enter_context(patch.object(definitions, "load", lambda kind, root=None, every=False:
                                         ([bench], []) if kind == "benchmark" else fixture_load(kind, root, every)))
        def schema():
            r = web.app.test_client().get("/new-run"); self.addCleanup(r.close)
            return json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))
        secrets.set("envbench", "HF_TOKEN", "hf-stale-not-real")
        self.assertNotIn("HF_TOKEN", schema()["standardHere"]["licences"]["envbench"])
        secrets.set(secrets.HUGGINGFACE, "HF_TOKEN", KEY)
        got = schema()
        self.assertTrue(got["standardHere"]["licences"]["envbench"]["HF_TOKEN"]["set"])
        page = next(b for b in got["standard"]["benchmarks"] if b["key"] == "envbench")
        self.assertIn({"name": "HF_TOKEN", "to": "driver", "kind": "value", "summary": HF["summary"], "provider": "huggingface"},
                      page["environments"]["licensed"]["needs_secrets"])


if __name__ == "__main__":
    unittest.main()
