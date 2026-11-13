"""The Settings page and its one mutation: every variable listed with its
state, secrets by length only, a save that lands in app/.env and applies at
once, a hand edit seen on the next request, the guards
a write takes — form token, same origin, a loopback client — and validation
that refuses before anything is written."""
from __future__ import annotations

import html as html_text
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from nullius import constraints, hosts, prices, providers, sandbox, settings
from .test_routes import web


class SettingsPage(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-settings-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = str(self.tmp / ".env")
        self.stack.enter_context(patch.object(web.shared, "ENV_FILE", self.env_file))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=False))
        for v in settings.VARIABLES:
            os.environ.pop(v.name, None)
        self.addCleanup(settings.LOADED_KEYS.clear); settings.LOADED_KEYS.clear()
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()

    def page(self, **kw):
        r = self.client.get("/settings", **kw); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def token(self):
        self.page()
        with self.client.session_transaction() as s:
            return s["csrf"]

    def post(self, values=None, clear=(), csrf=None, **kw):
        """The one form: every plain variable's current value plus the edits
        in `values` (the page re-posts what it shows), secrets only when typed."""
        current = {s["name"]: s for s in settings.states(self.env_file)}
        data = {"csrf": self.token() if csrf is None else csrf}
        for var in settings.VARIABLES:
            if current[var.name]["source"] != "environment":
                data["value:" + var.name] = "" if var.secret else current[var.name]["shown"]
        for name, value in (values or {}).items():
            data["value:" + name] = value
        for name in clear:
            data["clear:" + name] = "1"; data["value:" + name] = ""
        r = self.client.post("/settings/save", data=data, headers=kw.pop("headers", {"Origin": "http://localhost"}), **kw)
        self.addCleanup(r.close)
        return r

    def file(self):
        return Path(self.env_file).read_text() if os.path.exists(self.env_file) else None

    def test_a_file_edited_by_hand_is_seen_on_the_next_request(self):
        # a key written after start counts on the next request, by the page and by the process: no button, no restart
        Path(self.env_file).write_text("MODEL_API_KEY=sk-fake-0123456789\nHOSTS=bench\n")
        settings.load(self.env_file)
        before = self.page()
        self.assertNotIn("Not read by Nullius", before)
        self.assertNotIn("/settings/load", before); self.assertNotIn("Load .env", before)
        Path(self.env_file).write_text("MODEL_API_KEY=sk-fake-9876543210\nTYPESAFE_API_KEY=ts-fake-0123456789\nAPI_TOKEN=sk-other-tool\n")   # changed, added, HOSTS removed
        html = self.page()
        seen = re.search(r'<article[^>]*id="TYPESAFE_API_KEY".*?</article>', html, re.S).group(0)
        self.assertIn('data-source="file"', seen); self.assertIn("********", seen); self.assertNotIn("characters", seen); self.assertNotIn("not set", seen)
        self.assertNotIn('System One <span class="st-legend-state">needs the TypeSafe key</span>', html)
        self.assertEqual(os.environ["TYPESAFE_API_KEY"], "ts-fake-0123456789")         # the judge sees it too, not only the page
        self.assertEqual(os.environ["MODEL_API_KEY"], "sk-fake-9876543210"); self.assertNotIn("HOSTS", os.environ)
        notice = html[html.index('id="settings-unread"'):]
        self.assertIn('<span class="mono">API_TOKEN</span> (this page reads <a class="mono" href="#MODEL_API_KEY">MODEL_API_KEY</a>)', notice[:notice.index("</div>")])
        for value in ("sk-other-tool", "sk-fake-", "ts-fake-"):
            self.assertNotIn(value, html)                                               # never a value
        self.assertEqual(self.client.post("/settings/load", data={"csrf": self.token()}, headers={"Origin": "http://localhost"}).status_code, 404)   # the route is gone

    def test_the_unread_notice_says_which_keys_a_run_still_inherits(self):
        Path(self.env_file).write_text("API_KEY_TYPESAFE=ts-old-name-0123\nSOMETHING_ELSE=1\n")
        settings.load(self.env_file)
        html = self.page()
        notice = html[html.index('id="settings-unread"'):]
        notice = notice[:notice.index("</div>")]
        self.assertIn('href="#TYPESAFE_API_KEY">TYPESAFE_API_KEY</a>', notice)              # the old spelling, named for what it was
        self.assertIn('A run Nullius starts still inherits <span class="mono">SOMETHING_ELSE</span> in its environment.', notice)
        self.assertNotIn("inherits <span class=\"mono\">API_KEY_TYPESAFE", notice)           # withheld from every run
        self.assertNotIn("ts-old-name", html)

    def test_host_and_port_are_not_on_the_page_and_a_leftover_line_names_the_start_up_option(self):
        """The running server cannot move itself: APP_HOST and APP_PORT are
        read at start, so they have no row, and a PORT line left from when
        the page wrote one is named with what Nullius reads instead - not a
        link to a row that does not exist."""
        Path(self.env_file).write_text("PORT=6000\nHOST=0.0.0.0\n")
        settings.load(self.env_file)
        html = self.page()
        for name in ("HOST", "PORT", "APP_HOST", "APP_PORT"):
            self.assertNotIn(f'id="{name}"', html); self.assertNotIn(f'name="value:{name}"', html)
        self.assertNotIn("Nullius server", html)
        notice = html[html.index('id="settings-unread"'):]
        notice = notice[:notice.index("</div>")]
        self.assertIn('<span class="mono">PORT</span> (Nullius reads <span class="mono">APP_PORT</span> at start)', notice)
        self.assertIn('<span class="mono">HOST</span> (Nullius reads <span class="mono">APP_HOST</span> at start)', notice)
        self.assertNotIn('href="#APP_', notice)

    def test_every_variable_is_listed_with_its_state_and_no_secret_value(self):
        os.environ["MODEL_API_KEY"] = "sk-0fromtheenvironment"
        Path(self.env_file).write_text("OPENROUTER_API_KEY=sk-or-fromthefile\nOPENAI_API_KEY=sk-oa-fromthefile0\nHOSTS=bench\n")
        settings.load(self.env_file)
        html = self.page()
        for v in settings.VARIABLES:
            self.assertIn(f'id="{v.name}"', html)
        self.assertIn('title="Held by the process', html)                                            # the token, from the process
        self.assertEqual(html.count('<span class="st-shown">********</span>'), 3)                   # the token and the OpenRouter and OpenAI keys: eight stars each,
        self.assertNotIn("characters", html)                                                         # never their length
        self.assertNotIn("sk-0fromtheenvironment", html); self.assertNotIn("sk-or-fromthefile", html); self.assertNotIn("sk-oa-fromthefile", html)
        self.assertIn('name="value:HOSTS" value="bench"', html)                                     # a plain value is editable
        self.assertIn('type="password"', html); self.assertNotIn('autocomplete="new-password"', html)
        self.assertIn("the running process keeps that value until it is restarted", html)         # environment: editable, applies next start
        self.assertEqual(html.count("data-unset="), 1)                                            # a provider's key is replaced, not removed (the card's Remove does that); the optional key of an endpoint is removed
        self.assertIn('>Remove key</button>', html)

    def test_each_judge_card_starts_on_its_provider_and_holds_each_provider_s_key_row(self):
        Path(self.env_file).write_text("OPENROUTER_API_KEY=sk-or-fromthefile\n")
        settings.load(self.env_file)
        html = self.page()
        self.assertIn('name="value:SYSTEM_ONE_PROVIDER" value="openrouter" checked', html)      # the one in use: OpenRouter, the only key
        self.assertIn('name="value:LLM_JUDGE_PROVIDER" value="openrouter" checked', html)
        self.assertNotIn('value="typesafe" checked', html)
        self.assertIn('data-provider="typesafe" data-available="false" hidden', html)
        self.assertIn('data-provider="openrouter" data-available="true"', html)
        self.assertIn('data-provider="openai" data-available="false" hidden', html)
        judge_section = html.split('id="judge"')[1].split('id="custom-keys"')[0]
        self.assertNotIn('id="TYPESAFE_API_KEY"', judge_section)
        self.assertNotIn('type="password"', judge_section)
        self.assertIn('data-provider-card="typesafe"', html)
        self.assertIn('data-default="~typesafe/jev-latest" data-models="~typesafe/jev-latest|typesafe/jev-1.13|typesafe/jev-router|jaredpalmer/kev-4b"', html)
        self.assertNotIn("data-model-default", html)                       # the default is the model field's placeholder (judges.js)

    def test_a_section_per_thing_a_run_needs_with_its_index_and_what_a_run_can_use(self):
        Path(self.env_file).write_text("OPENROUTER_API_KEY=sk-or-fromthefile\nHOSTS=bench\n")
        settings.load(self.env_file)
        html = self.page()
        for section in ("general", "appearance", "providers", "data-providers", "custom-keys", "judge", "hosts", "sandbox", "storage"):
            self.assertIn(f'id="{section}"', html); self.assertIn(f'href="#{section}"', html)
        for title in ("Evaluation", "Execution", "Application", "General", "Appearance", "Judges", "Local data"):
            self.assertIn(">" + title + "<", html)
        start = html.index('class="st-ready"'); ready = html[start:html.index("</header>", start)]
        self.assertIn('class="st-ready-i on"><span aria-hidden="true">✓</span> OpenRouter', ready)
        self.assertIn('class="st-ready-i"><span aria-hidden="true">○</span> OpenAI', ready)
        self.assertIn('<span class="mono">bench</span>', ready)
        for mark in ("#openrouter", "#openai"):
            self.assertIn(f'brand/marks.svg{mark}"', html)                                          # the providers' own marks
        self.assertIn('<span class="st-row-label">Endpoint</span><span class="st-row-value mono">https://openrouter.ai/api/v1</span>', html)
        self.assertIn('data-provider-card="openrouter" data-configured="true" data-ready="true"', html)
        self.assertIn('data-provider-select="openai"', html)
        self.assertIn('data-provider-add aria-label="Add a provider"', html)
        self.assertNotIn("<details", html.split('<div id="add-host"')[0])
        for section in ("general", "appearance", "providers", "data-providers", "custom-keys", "judge", "hosts", "storage"):
            self.assertIn(f'href="#{section}" data-section="{section}"', html)                      # the menu: a link per section, one shown at a time

    def test_nothing_is_assumed_and_nothing_is_suggested(self):
        """No baked-in repository, host or example on the page: an unset
        list is its Add button and nothing else, and an unset field is empty
        with no placeholder."""
        Path(self.env_file).write_text("")
        settings.load(self.env_file)
        html = self.page()
        def row(name):
            return re.search(r'<article[^>]*id="%s".*?</article>' % name, html, re.S).group(0)
        hosts_row, key = row("HOSTS"), row("MODEL_API_KEY")
        self.assertNotIn(">not set<", hosts_row); self.assertNotIn('data-entry=', hosts_row); self.assertIn('data-add-host aria-label="Add a host"', html)
        self.assertEqual(re.findall(r'placeholder="([^"]*)"', html.split('<div id="add-host"')[0]), ["Search settings", "Search providers…", "(optional)", "(optional)"])                           # no field suggests anything
        self.assertNotIn("agtai", html); self.assertNotIn("10.0.0.4", html)                      # (ssh aliases offered come from this machine's own config)
        self.assertIn('data-source="unset"', key); self.assertIn('data-provider-card="local" data-configured="false" data-ready="false"', html)

    def test_autosave_returns_masked_state_and_keeps_validation_and_guards(self):
        data = {"csrf": self.token(), "value:ANTHROPIC_API_KEY": "synthetic-autosave-secret"}
        response = self.client.post("/settings/save", data=data, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertIn("********", response.json["html"])
        self.assertNotIn("synthetic-autosave-secret", response.get_data(as_text=True))
        self.assertIn("synthetic-autosave-secret", self.file())
        data["value:ANTHROPIC_API_KEY"] = "has space"
        response = self.client.post("/settings/save", data=data, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])
        self.assertFalse(response.json["retryable"])
        self.assertIn("without spaces", response.json["message"])
        self.assertIn("synthetic-autosave-secret", self.file())
        data["csrf"] = "invalid"
        self.assertEqual(self.client.post("/settings/save", data=data, headers={"Accept": "application/json"}).status_code, 403)

    def test_short_and_long_agent_keys_save_without_length_limits(self):
        name = "AGENT_" + "X" * 80
        for key in ("x", "v" * 4096):
            response = self.client.post("/settings/save", data={"csrf": self.token(), "agent_key_name": name, "agent_key_value": key}, headers={"Accept": "application/json"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(settings.in_file(self.env_file)[name], key)
        html = self.page()
        self.assertIn('placeholder="(optional)"', html)
        self.assertNotIn('maxlength="400"', html)

    def test_local_data_uses_active_storage_paths_and_theme_is_browser_only(self):
        with patch.dict(os.environ, {"NULLIUS_HOME": "/tmp/settings-profile-example", "NULLIUS_CATALOGUE": "/tmp/catalogue-override"}):
            html = self.page()
        storage = html.split('id="storage"')[1].split('</section>')[0]
        self.assertIn(self.env_file, storage)
        self.assertIn("/tmp/catalogue-override", storage)
        self.assertIn("/tmp/settings-profile-example/data/workspaces", storage)
        self.assertIn("/tmp/settings-profile-example/cache", storage)
        appearance = html.split('id="appearance"')[1].split('</section>')[0]
        for choice in ("system", "light", "charcoal", "graphite", "midnight"):
            self.assertIn('data-theme-choice="' + choice + '"', appearance)
        self.assertNotIn('name="value:', appearance)

    def test_embedded_settings_save_stays_in_modal_and_keeps_write_guards(self):
        html = self.client.get("/settings?embedded=1").get_data(as_text=True)
        self.assertIn('class="settings-embedded"', html)
        self.assertIn('name="embedded" value="1"', html)
        self.assertNotIn('id="settings-modal"', html)
        self.assertNotIn('src="/static/onboarding.js"', html)
        data = {"csrf": self.token(), "embedded": "1", "value:MODEL_API_URL": "https://llm.example/v1"}
        response = self.client.post("/settings/save", data=data)
        self.assertEqual(response.location, "/settings?embedded=1")
        self.assertIn("MODEL_API_URL=https://llm.example/v1", self.file())
        self.assertEqual(self.client.post("/settings/save", data={"embedded": "1"}).status_code, 403)
        self.assertEqual(self.client.post("/settings/save", data=data,
                                         environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code, 403)

    def test_embedded_validation_and_empty_save_keep_modal_context(self):
        for extra in ({}, {"agent_key_name": "EXAMPLE_KEY"}):
            response = self.client.post("/settings/save", data={"csrf": self.token(), "embedded": "1", **extra})
            self.assertTrue(response.location.startswith("/settings?embedded=1"))
        self.assertIsNone(self.file())

    def test_one_save_lands_every_change_in_the_file_and_applies_now(self):
        r = self.post({"HOSTS": "bench", "MODEL_API_URL": "https://llm.example/v1"})
        self.assertEqual(r.status_code, 302); self.assertTrue(r.headers["Location"].endswith("/settings"))
        self.assertEqual(self.file(), "MODEL_API_URL=https://llm.example/v1\nHOSTS=bench\n")
        self.assertEqual(os.environ["HOSTS"], "bench"); self.assertEqual(os.environ["MODEL_API_URL"], "https://llm.example/v1")
        html = self.page()
        self.assertIn("Saved", html); self.assertNotIn("Applies now.", html); self.assertNotIn("Saved to .env", html); self.assertIn('data-entry="bench"', html)
        form = html[html.index('id="settings-form"'):html.index('</form>', html.index('id="settings-form"'))]
        self.assertEqual(form.count('type="submit"'), 2)                                            # one Save for the page
        # re-posting the page unchanged changes nothing; an emptied field keeps the value; × removes it
        self.post(); self.assertIn("Nothing changed", self.page())
        self.post({"HOSTS": ""}); self.assertIn("HOSTS=bench", self.file())
        self.post(clear=["HOSTS"]); self.assertEqual(self.file(), "MODEL_API_URL=https://llm.example/v1\n"); self.assertNotIn("HOSTS", os.environ)

    def test_a_secret_saves_without_ever_being_shown(self):
        self.post({"MODEL_API_KEY": "sk-0abcdefghijklmnopqrstuvwxyz"})
        self.assertEqual(self.file(), "MODEL_API_KEY=sk-0abcdefghijklmnopqrstuvwxyz\n")
        self.assertEqual(oct(os.stat(self.env_file).st_mode & 0o777), "0o600")
        html = self.page()
        self.assertNotIn("sk-0abcdefghijklmnopqrstuvwxyz", html); self.assertIn("********", html); self.assertNotIn("30 characters", html)
        # an empty secret leaves it alone; the Clear box removes it
        self.post(); self.assertIn("MODEL_API_KEY=", self.file())
        self.post(clear=["MODEL_API_KEY"])
        self.assertEqual(self.file(), ""); self.assertNotIn("MODEL_API_KEY", os.environ)

    def test_an_invalid_value_or_unknown_name_writes_nothing(self):
        self.post({"HOSTS": "bench, not a host", "MODEL_API_KEY": "sk-0abcdefghijklmnop"})
        self.assertIn("HOSTS must be", self.page()); self.assertIsNone(self.file())                 # one bad value: nothing written
        self.post({"OPENAI_BASE_URL": "https://whatever/v1", "PORT": "6000", "APP_PORT": "6000"})
        self.assertIsNone(self.file())                                                              # not a setting: ignored
        self.post({"MODEL_API_KEY": "sk-0x\nAPP_PORT=1"})
        self.assertIsNone(self.file())

    def test_a_rebound_name_is_refused_before_any_guard(self):
        """DNS rebinding: a page on attacker.example resolved to 127.0.0.1 is
        same-origin with itself, so the Host decides, on every route."""
        rebound = {"base_url": "http://rebind.attacker.example:5057"}
        self.assertEqual(self.client.get("/settings", **rebound).status_code, 421)
        r = self.post({"HOSTS": "bench"}, headers={"Origin": "http://rebind.attacker.example:5057"}, **rebound)
        self.assertEqual(r.status_code, 421); self.assertIsNone(self.file())
        for served in ("http://127.0.0.1:5057", "http://[::1]:5057", "http://localhost:5057"):
            self.assertEqual(self.client.get("/settings", base_url=served).status_code, 200, served)

    def test_an_origin_that_only_starts_like_this_one_is_refused(self):
        self.assertEqual(self.post({"HOSTS": "bench"}, headers={"Origin": "http://localhost.evil.example"}).status_code, 403)
        self.assertIsNone(self.file())

    def test_only_a_loopback_client_may_write(self):
        r = self.post({"HOSTS": "bench"}, environ_base={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual(r.status_code, 403); self.assertIsNone(self.file())
        forwarded = self.post({"HOSTS": "bench"}, headers={"Origin": "http://localhost", "X-Forwarded-For": "10.0.0.7"})
        self.assertEqual(forwarded.status_code, 403); self.assertIsNone(self.file())
        html = self.page(environ_base={"REMOTE_ADDR": "10.0.0.7"})
        self.assertIn("Read-only from here", html); self.assertNotIn('id="settings-form"', html); self.assertNotIn('name="value:HOSTS"', html)

    def test_endpoints_are_added_named_edited_and_removed_each_a_card(self):
        """OpenAI-compatible endpoints: one added by name and URL (its key
        optional) gets an id from its name and a card of its own; a rename
        keeps the id; Remove on its card removes it whole. The default one, at
        MODEL_API_URL, stays as it was. Never a key on the page."""
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in settings.endpoint_names()])
        def save(data):
            r = self.client.post("/settings/save", data={"csrf": self.token(), **data}, headers={"Origin": "http://localhost", "Accept": "application/json"})
            self.addCleanup(r.close)
            return r.status_code, r.get_json()
        self.assertEqual(save({"value:MODEL_API_URL": "http://default/v1"})[1]["reload"], False)
        status, body = save({"endpoint_name": "GPU box", "endpoint_url": "http://gpu:8000/v1", "endpoint_key": "sk-gpu-0123456789"})
        self.assertEqual((status, body["ok"], body["reload"]), (200, True, True))
        self.assertEqual(self.file(), "MODEL_API_URL=http://default/v1\nMODEL_GPU_BOX_NAME=GPU box\n"
                                      "MODEL_GPU_BOX_API_URL=http://gpu:8000/v1\nMODEL_GPU_BOX_API_KEY=sk-gpu-0123456789\n")
        save({"endpoint_name": "GPU box", "endpoint_url": "http://gpu2:8000/v1"})                          # the same name: an id of its own
        self.assertEqual(settings.endpoint_ids(), ["GPU_BOX", "GPU_BOX_2"]); self.assertNotIn("MODEL_GPU_BOX_2_API_KEY", self.file())
        html = self.page()
        for key, name in (("local", "OpenAI-compatible"), ("local:GPU_BOX", "GPU box"), ("local:GPU_BOX_2", "GPU box")):
            card = re.search(r'<div class="st-card st-provider-card" data-provider-card="%s".*?<h3>([^<]*)</h3>' % re.escape(key), html, re.S)
            self.assertEqual(card.group(1), name, key)
        self.assertIn('id="MODEL_GPU_BOX_API_URL"', html); self.assertNotIn("sk-gpu-0123456789", html)
        self.assertIn('name="value:LLM_JUDGE_PROVIDER" value="local:GPU_BOX"', html)                    # a judge choice too
        self.assertNotIn("Local or self-hosted", html)
        # a rename keeps the id, so a recipe on it keeps running; an empty key field keeps the key
        self.assertEqual(save({"value:MODEL_GPU_BOX_NAME": "Big GPU", "value:MODEL_GPU_BOX_API_KEY": ""})[1]["reload"], True)
        self.assertEqual(os.environ["MODEL_GPU_BOX_NAME"], "Big GPU"); self.assertEqual(os.environ["MODEL_GPU_BOX_API_KEY"], "sk-gpu-0123456789")
        save({"remove_provider": "local:GPU_BOX"})                                                      # removed from its card: the endpoint goes whole
        self.assertEqual(settings.endpoint_ids(), ["GPU_BOX_2"]); self.assertNotIn("GPU_BOX_API_KEY", self.file())
        self.assertIn("MODEL_API_URL=http://default/v1\n", self.file())
        status, body = save({"endpoint_name": "No URL"})
        self.assertEqual((status, body["ok"], body["message"]), (400, False, "An endpoint needs its name and its URL."))
        self.assertEqual(save({"endpoint_name": "Bad", "endpoint_url": "has space"})[0], 400)
        self.assertEqual(settings.endpoint_ids(), ["GPU_BOX_2"])
        before = self.file()
        r = self.client.post("/settings/save", data={"endpoint_name": "X", "endpoint_url": "http://x/v1"}, headers={"Origin": "http://localhost"})   # no form token
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403); self.assertEqual(self.file(), before)

    def test_the_editor_shows_an_endpoint_url_and_hides_every_key(self):
        """A name and an endpoint's URL are edited in place; a key is written,
        never shown, and replaced; only an endpoint's optional key is removed
        from its row. A URL with a credential is hidden, said, and refused."""
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in settings.endpoint_names()])
        Path(self.env_file).write_text("OPENAI_API_KEY=sk-oa-0123456789\nMODEL_GPU_NAME=GPU box\nMODEL_GPU_API_URL=http://[::1]:8000/v1\n"
                                        "MODEL_GPU_API_KEY=sk-gpu-0123456789\nMODEL_OLD_API_URL=https://me:hunter2@old.example/v1\n")
        settings.load(self.env_file)
        html = self.page()
        def fields(card):
            return re.search(r'data-provider-card="%s".*?<div class="st-provider-fields">(.*?)<button type="button" class="btn ghost st-provider-done"' % re.escape(card), html, re.S).group(1)
        def row(text, name):
            return re.search(r'<article[^>]*id="%s".*?</article>' % name, text, re.S).group(0)
        gpu = fields("local:GPU")
        for name, value in (("MODEL_GPU_NAME", "GPU box"), ("MODEL_GPU_API_URL", "http://[::1]:8000/v1")):
            r = row(gpu, name)
            self.assertIn(f'name="value:{name}" aria-label="{"Name" if name.endswith("NAME") else "Endpoint"}" value="{value}"', r)
            self.assertNotIn("data-replace", r); self.assertNotIn("data-unset", r)
        key = row(gpu, "MODEL_GPU_API_KEY")
        self.assertIn("data-replace>Replace", key); self.assertIn(">Remove key</button>", key); self.assertNotIn("sk-gpu", html)
        openai = row(fields("openai"), "OPENAI_API_KEY")
        self.assertIn("data-replace>Replace", openai); self.assertNotIn("data-unset", openai); self.assertNotIn("sk-oa", html)
        old = row(fields("local:OLD"), "MODEL_OLD_API_URL")
        self.assertIn('value="https://[REDACTED]@old.example/v1"', old); self.assertNotIn("hunter2", html)
        self.assertIn('aria-invalid="true"', old)
        self.assertIn("This URL must be a URL without a user or password in it: put the key in the Key field.", old)
        self.assertNotIn('value="local:OLD"', html)                                                      # not a judge choice either
        self.assertIn('data-provider-card="local:OLD" data-configured="true" data-ready="false"', html)
        self.assertRegex(html, r'(?s)data-provider-card="local:OLD".*?data-provider-state>Fix the endpoint URL<')
        r = self.client.post("/settings/save", data={"csrf": self.token(), "value:MODEL_GPU_API_URL": "http://gpu/v1?api_key=abc"},
                             headers={"Origin": "http://localhost", "Accept": "application/json"})
        self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.get_json()["message"]),
                         (400, "MODEL_GPU_API_URL must be a URL without a key in its query: put the key in the Key field."))
        r = self.client.post("/settings/save", data={"csrf": self.token(), "value:MODEL_GPU_API_URL": "http://gpu:9000/v2"},
                             headers={"Origin": "http://localhost", "Accept": "application/json"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200); self.assertEqual(os.environ["MODEL_GPU_API_URL"], "http://gpu:9000/v2")

    def test_a_provider_card_removes_what_it_holds_unless_the_environment_holds_it(self):
        """Remove on a card: a named endpoint goes whole, a built-in provider's
        key is cleared, a value the process environment holds is refused, and
        nothing is written without the form token."""
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in settings.endpoint_names()])
        Path(self.env_file).write_text("OPENAI_API_KEY=sk-oa-0123456789\nMODEL_API_URL=http://default/v1\nMODEL_API_KEY=sk-def-0123456789\n"
                                        "MODEL_GPU_NAME=GPU box\nMODEL_GPU_API_URL=http://gpu/v1\nMODEL_GPU_API_KEY=sk-gpu-0123456789\n")
        settings.load(self.env_file)
        def remove(card, csrf=None):
            r = self.client.post("/settings/save", data={"csrf": self.token() if csrf is None else csrf, "remove_provider": card},
                                 headers={"Origin": "http://localhost", "Accept": "application/json"})
            self.addCleanup(r.close)
            return r.status_code, r.get_json(silent=True)
        html = self.page()
        self.assertEqual(html.count("data-provider-remove-yes="), 3)                                      # OpenAI, the default endpoint, GPU box
        self.assertIn('Remove <b>GPU box</b> and its URL and key?', html); self.assertIn('Remove <b>OpenAI</b> and its key?', html)
        card = re.search(r'data-provider-card="local:GPU".*?<div class="st-provider-fields">(.*?)</div>\s*<button type="button" class="btn ghost st-provider-done"', html, re.S)
        summary = card.group(0)[:card.group(0).index('<div class="st-provider-fields">')]
        self.assertIn('data-provider-edit="local:GPU"', summary)                                          # Remove sits beside Edit, in the summary
        self.assertRegex(summary, r'data-provider-edit="local:GPU"[^>]*>Edit</button>\s*<button [^>]*data-provider-remove-ask aria-label="Remove GPU box"')
        self.assertNotIn("data-provider-remove", card.group(1))                                         # and not in the editor
        before = self.file()
        self.assertEqual(remove("local:GPU", csrf="stale")[0], 403); self.assertEqual(self.file(), before)
        status, body = remove("local:GPU")
        self.assertEqual((status, body["ok"], body["reload"]), (200, True, True))
        self.assertEqual(settings.endpoint_ids(), []); self.assertNotIn("MODEL_GPU", self.file())
        remove("openai")
        self.assertNotIn("OPENAI_API_KEY", self.file()); self.assertNotIn("OPENAI_API_KEY", os.environ)
        card = re.search(r'data-provider-card="openai" data-configured="(\w+)".*?data-provider-state>([^<]*)<', self.page(), re.S)
        self.assertEqual(card.groups(), ("false", "Not configured"))
        remove("local")
        self.assertEqual(self.file(), "")
        self.assertEqual(remove("nothing")[0], 400)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fromtheenvironment"                                       # held by the process: refused
        html = self.page()
        self.assertIn("Set in the process environment: remove it there.", html)
        self.assertRegex(html, r'aria-label="Remove OpenRouter" aria-expanded="false" aria-controls="[^"]*" disabled')
        status, body = remove("openrouter")
        self.assertEqual((status, body["message"]), (400, "OPENROUTER_API_KEY is set in the process environment; remove it there."))
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-or-fromtheenvironment")

    def test_a_save_brings_remove_to_a_card_and_takes_it_away(self):
        """providers.js on the rendered cards: a first key saved brings Remove
        and its question from the page the save returns, a cleared key takes
        them away, and Remove still asks before Yes removes."""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the provider cards' script")
        void = ("input", "img", "br", "hr")
        def section(html):
            class Tree(HTMLParser):           # the providers section as {t, a, c} nodes, text as strings
                root, open = None, []
                def handle_starttag(self, tag, attrs):
                    if not self.open and dict(attrs).get("id") != "providers":
                        return
                    el = {"t": tag, "a": {k: v or "" for k, v in attrs}, "c": []}
                    if self.open: self.open[-1]["c"].append(el)
                    else: self.root = el
                    if tag not in void: self.open.append(el)
                def handle_startendtag(self, tag, attrs):
                    if self.open: self.open[-1]["c"].append({"t": tag, "a": {k: v or "" for k, v in attrs}, "c": []})
                def handle_endtag(self, tag):
                    if self.open and tag not in void: self.open.pop()
                def handle_data(self, data):
                    if self.open: self.open[-1]["c"].append(data)
            tree = Tree(); tree.open = []; tree.feed(html)
            return tree.root
        empty = section(self.page())
        self.post({"OPENAI_API_KEY": "sk-synthetic-remove-0123456789"}); saved = section(self.page())
        self.post(clear=["OPENAI_API_KEY"]); cleared = section(self.page())
        program = r'''
            const fs = require('fs'), input = JSON.parse(fs.readFileSync(0, 'utf8'));
            const match = (el, selectors) => selectors.split(',').some(one => {
              const [, tag, classes, attrs, notHidden] = one.trim().match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)(:not\(\[hidden\]\))?$/) || [];
              if (classes === undefined) return false;
              return (!tag || el.t === tag) && (!notHidden || !el.hidden)
                && classes.split('.').filter(Boolean).every(c => (el.a.class || '').split(' ').includes(c))
                && [...attrs.matchAll(/\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g)].every(([, k, v]) => k in el.a && (v === undefined || el.a[k] === v));
            });
            const listen = el => Object.assign(el, {listeners: {}, addEventListener(type, f) { (this.listeners[type] ||= []).push(f); }});
            const data = k => 'data-' + k.replace(/[A-Z]/g, m => '-' + m.toLowerCase());
            class Element {        // just enough of the DOM for providers.js
              constructor(n, parent) {
                Object.assign(this, {t: n.t, a: n.a, parent, hidden: 'hidden' in n.a, value: n.a.value});
                this.c = n.c.map(x => typeof x === 'string' ? x : new Element(x, this));
                this.dataset = new Proxy({}, {get: (_, k) => this.a[data(k)], set: (_, k, v) => (this.a[data(k)] = String(v), true)});
                listen(this);
              }
              get id() { return this.a.id; }
              get textContent() { return this.c.map(x => typeof x === 'string' ? x : x.textContent).join(''); }
              set textContent(v) { this.c = [String(v)]; }
              getAttribute(k) { return this.a[k] ?? null; }
              setAttribute(k, v) { this.a[k] = String(v); }
              *all() { for (const x of this.c) if (typeof x !== 'string') { yield x; yield* x.all(); } }
              querySelectorAll(s) { return [...this.all()].filter(el => match(el, s)); }
              querySelector(s) { return this.querySelectorAll(s)[0] || null; }
              closest(s) { for (let el = this; el; el = el.parent) if (match(el, s)) return el; return null; }
              contains(o) { for (let el = o; el; el = el.parent) if (el === this) return true; return false; }
              remove() { this.parent.c.splice(this.parent.c.indexOf(this), 1); this.parent = null; }
              append(x) { this.c.push(x); x.parent = this; }
              after(x) { this.parent.c.splice(this.parent.c.indexOf(this) + 1, 0, x); x.parent = this.parent; }
              focus() { global.focused = this; }
              scrollIntoView() {}
              dispatchEvent(event) { event.target ||= this; for (let el = this; el; el = el.parent) (el.listeners[event.type] || []).forEach(f => f(event)); }
            }
            global.Event = class { constructor(type) { this.type = type; } preventDefault() {} stopPropagation() {} };
            global.CustomEvent = class extends Event { constructor(type, o) { super(type); this.detail = o.detail; } };
            const page = new Element(input.empty, null);
            global.document = listen({
              getElementById: id => id === 'settings-form' ? {} : id === 'providers' ? page : page.querySelector('[id="' + id + '"]'),
              querySelector: s => page.querySelector(s),
              dispatchEvent(event) { (this.listeners[event.type] || []).forEach(f => f(event)); }});
            global.window = listen({}); global.location = {hash: ''};
            require(input.script);
            // what autosave.js sends after a save, from the page the save returned
            const saved = tree => document.dispatchEvent(new CustomEvent('nullius-settings-saved', {detail: {removedKeys: [],
              providers: new Element(tree, null).querySelectorAll('[data-provider-card]').map(card => ({
                key: card.dataset.providerCard, configured: card.dataset.configured, ready: card.dataset.ready,
                label: card.querySelector('[data-provider-state]')?.textContent ?? '',
                remove: card.querySelector('[data-provider-remove-ask]'), ask: card.querySelector('.st-provider-ask')}))}}));
            const card = () => page.querySelector('[data-provider-card="openai"]');
            const click = el => page.dispatchEvent(Object.assign(new Event('click'), {target: el}));
            const state = () => {
              const remove = card().querySelector('[data-provider-remove-ask]'), ask = card().querySelector('.st-provider-ask');
              return {shown: !card().hidden, remove: remove && !remove.hidden, ask: ask && !ask.hidden,
                      controls: remove && ask && remove.getAttribute('aria-controls') === ask.id,
                      field: document.querySelector('[name="remove_provider"]').value};
            };
            (async () => {
              const out = {before: state()};
              click(page.querySelector('[data-provider-select="openai"]'));     // Add a provider, OpenAI, then the key is saved
              saved(input.saved); out.editing = state();
              click(card().querySelector('[data-provider-done]')); await new Promise(r => setTimeout(r));
              out.listed = state();
              click(card().querySelector('[data-provider-remove-ask]')); out.asked = state();
              out.focus = global.focused === card().querySelector('[data-provider-remove-keep]');
              click(card().querySelector('[data-provider-remove-keep]')); out.kept = state();
              click(card().querySelector('[data-provider-remove-ask]'));
              let changed = false;
              document.querySelector('[name="remove_provider"]').addEventListener('change', () => changed = true);
              click(card().querySelector('[data-provider-remove-yes]')); out.yes = changed;
              saved(input.cleared); out.cleared = state();
              process.stdout.write(JSON.stringify(out));
            })();
        '''
        script = Path(__file__).resolve().parents[1] / "static" / "settings" / "providers.js"
        result = subprocess.run([node, "-e", program], input=json.dumps({"script": str(script), "empty": empty, "saved": saved, "cleared": cleared}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["before"], {"shown": False, "remove": None, "ask": None, "controls": None, "field": ""})
        self.assertEqual(out["editing"], {"shown": True, "remove": False, "ask": False, "controls": True, "field": ""})   # in the editor: hidden
        self.assertEqual(out["listed"], {"shown": True, "remove": True, "ask": False, "controls": True, "field": ""})    # in the list, no reload
        self.assertEqual((out["asked"]["ask"], out["focus"], out["asked"]["field"]), (True, True, ""))               # asks first, writes nothing
        self.assertEqual((out["kept"]["ask"], out["yes"]), (False, True))
        self.assertEqual(out["cleared"], {"shown": False, "remove": None, "ask": None, "controls": None, "field": "openai"})

    def test_an_environment_variable_is_written_for_the_next_start(self):
        os.environ["HOSTS"] = "bench-env"
        self.post({"HOSTS": "bench-file", "OPENAI_API_KEY": "sk-oa-0123456789"})
        self.assertEqual(sorted(self.file().splitlines()), ["HOSTS=bench-file", "OPENAI_API_KEY=sk-oa-0123456789"]); self.assertEqual(os.environ["HOSTS"], "bench-env")   # the process keeps its value
        self.assertIn("Saved. Environment overrides still apply.", self.page())

    def test_hosts_are_a_list_with_the_ssh_config_offered_and_saved_by_name_only(self):
        Path(self.env_file).write_text("HOSTS=bench, ubuntu@10.0.0.4:22\n"); settings.load(self.env_file)
        scanned = [{"alias": "bench", "hostname": "10.0.0.4", "user": "ubuntu", "port": "22"}, {"alias": "lab", "hostname": None, "user": None, "port": None}]
        with patch.object(hosts, "scan_ssh_config", return_value=scanned):
            html = self.page()
        self.assertIn('data-entry="ubuntu@10.0.0.4:22"', html); self.assertIn('data-entry="bench"', html)
        self.assertEqual(re.findall(r'<th scope="row" class="host-name">([^<]*)<', html), ["This machine", "bench", "ubuntu@10.0.0.4:22"])   # one list, this machine first
        self.assertIn('data-host-chip="bench"', html); self.assertIn('data-host-chip="lab"', html)   # the config's aliases, in the Add host dialog
        self.assertIn('data-tab="config"', html); self.assertIn('data-tab="manual"', html); self.assertIn("data-dialog-test", html); self.assertIn("data-dialog-add", html); self.assertIn("data-test-all>", html)
        self.assertEqual(html.count('data-field="alias" value="bench"'), 1)                        # an alias is edited in its row alone
        self.assertNotIn("IdentityFile", html); self.assertNotIn("id_bench", html)
        self.post({"HOSTS": "bench, lab"})
        self.assertEqual(self.file(), "HOSTS=bench, lab\n"); self.assertEqual(os.environ["HOSTS"], "bench, lab")
        self.post({"HOSTS": "bench, not a host"})
        self.assertIn("not a host", self.page()); self.assertEqual(self.file(), "HOSTS=bench, lab\n")

    def test_manual_host_name_survives_save_and_labels_the_builder(self):
        entry = "GPU lab=ubuntu@192.0.2.10:22"
        self.post({"HOSTS": entry})
        self.assertEqual(os.environ["HOSTS"], entry)
        html = self.page()
        self.assertIn('<th scope="row" class="host-name">GPU lab<', html)
        self.assertEqual(hosts.parse_list(os.environ["HOSTS"])[0].to_dict()["label"], "GPU lab")
        for field in ('name="display_name"', 'name="host"', 'name="user"', 'name="port"'):
            self.assertIn(field, html)

    def test_the_add_host_dialog_is_whole_without_an_ssh_config(self):
        """No `~/.ssh/config` — the ordinary Windows case, and any fresh
        machine — left `dialog-panels` unopened while its closing tag
        stayed: the browser closed an ancestor instead, taking the
        dialog's footer out of the dialog, and the script died on a null
        `[data-cancel]` so the button did nothing. The dialog is
        whole either way; only the tabs and the config panel depend on
        the config."""
        with patch.object(hosts, "scan_ssh_config", return_value=[]):
            html = self.page()
        panel = r'<div id="add-host".*?(?=\s*<article class="st-field st-hosts)'
        dialog = re.search(panel, html, re.S)
        self.assertIsNotNone(dialog); dialog = dialog.group(0)
        self.assertEqual(dialog.count('<div class="dialog-panels">'), 1)      # opened, and inside the dialog
        for needed in ('data-panel="manual"', 'class="dialog-foot"', 'data-cancel', 'data-dialog-test', 'data-dialog-add', 'class="settings-test dialog-status"'):
            self.assertIn(needed, dialog, needed)
        self.assertNotIn('data-tab="config"', dialog); self.assertNotIn('data-panel="config"', dialog)
        self.assertNotIn(' hidden>', dialog.split('data-panel="manual"')[1].split('>')[0] + '>')   # the only panel is not hidden
        self.assertEqual(dialog.count('</div>'), dialog.count('<div '))              # balanced, so nothing above is closed
        scanned = [{"alias": "bench", "hostname": "10.0.0.4", "user": "ubuntu", "port": "22"}]
        with patch.object(hosts, "scan_ssh_config", return_value=scanned):
            with_config = re.search(panel, self.page(), re.S).group(0)
        self.assertEqual(with_config.count('<div class="dialog-panels">'), 1)
        self.assertIn('data-panel="config"', with_config); self.assertIn('data-cancel', with_config)
        self.assertEqual(with_config.count('</div>'), with_config.count('<div '))
        # in the page below the heading, as the providers' Add is; its fields belong to a form of their own,
        # outside the settings form, so autosave never reads them (a password above all)
        page = self.page()
        head = re.search(r'<h2 id="hosts-h">Run hosts</h2>.*?</div>', page, re.S).group(0)
        self.assertIn('data-test-all>Test all</button>', head); self.assertIn('class="btn sm ghost st-provider-add" data-add-host aria-label="Add a host"', head)
        for name in ("display_name", "host", "port", "user", "auth", "key_path", "key_file", "password"):
            self.assertIn(f'form="add-host-form"', re.search(rf'<input [^>]*name="{name}"', with_config).group(0), name)
        self.assertGreater(page.index('<form id="add-host-form"'), page.index('</form>', page.index('id="settings-form"')))

    def test_a_host_test_sends_its_probe_then_measures_and_keeps_only_what_it_found(self):
        seen = []
        measured = "ncpu=8\nmemtotal_kb=8388608\nengine=29.9.3|Engine,|[name=seccomp]\ncpus=1000 100000 \nmemory_mb=67108864 0 \nprocesses=1 \ncpu_set=0 \n"
        def run(argv, timeout, env=None):
            seen.append(argv)
            return (0, "ok\nuv=yes\ndocker=yes\nharbor=no\ndocker_ok=yes\nhostname=lab\n", "") if argv[-1] == hosts.PROBE else (0, measured, "")
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        os.environ[constraints.SETTING] = '{"ubuntu@10.0.0.4:2222": {"cpus": 16}}'
        with patch.object(hosts, "subprocess_run", run):
            r = self.client.post("/settings/hosts/test", data={"entry": "ubuntu@10.0.0.4:2222"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json["ok"], r.json["uv"], r.json["harbor"], r.json["hostname"]), (True, True, False, "lab"))
        self.assertEqual(seen[0][-2:], ["ubuntu@10.0.0.4", hosts.PROBE]); self.assertIn("BatchMode=yes", seen[0])
        self.assertIn("docker run --rm --network none", seen[1][-1]); self.assertIn("BatchMode=yes", seen[1])   # the measure, over the same ssh
        self.assertEqual(r.json["constraints"]["capacity"], {"cpus": 8, "memory_mb": 8192, "gpus": []})
        self.assertEqual(r.json["constraints"]["warnings"], {"cpus": "More than the machine has."})
        self.assertIn("8 CPUs, 8 GB", r.json["info"]); self.assertEqual(r.json["cells"], {"engine": "Docker", "capacity": "8 CPUs · 8 GB"}); self.assertIn("lab", r.json["info"])          # its Info, drawn again
        self.assertEqual(constraints.load()["ubuntu@10.0.0.4:2222"]["enforced"], ["cpus", "memory_mb", "processes", "cpu_set"])
        self.assertIsNone(self.file())                                                     # nothing in the settings file
        bad = self.client.post("/settings/hosts/test", data={"entry": "not a host"}, headers=headers); self.addCleanup(bad.close)
        self.assertEqual(bad.status_code, 400); self.assertIn("not a host", bad.json["error"])
        # the same guards as Save: token, origin, loopback
        self.assertEqual(self.client.post("/settings/hosts/test", data={"entry": "bench"}, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.client.post("/settings/hosts/test", data={"entry": "bench"}, headers=headers, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)
        self.assertEqual(len(seen), 2)
        # this machine: no ssh, docker asked here, the same measure here
        which = lambda p: "/usr/bin/" + p if p in ("uvx", "docker") else None                      # uv and docker here, no harbor
        with patch.object(hosts, "subprocess_run", run), patch.object(web.settings.prerequisites, "docker_here", lambda fresh=False: None), \
                patch.object(web.settings.container, "DOCKER", "docker"), patch("shutil.which", which):
            here = self.client.post("/settings/hosts/test", data={"entry": "local"}, headers=headers); self.addCleanup(here.close)
        self.assertEqual(seen[2][:2], ["sh", "-c"]); self.assertTrue(here.json["ok"]); self.assertIn("local", constraints.load())
        # This machine's Info: the rows a host's has, but Reached, from the checks the Review step makes here
        import socket
        info = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", here.json["info"]))
        for said in ("Host name " + socket.gethostname(), "Programs uv, docker · missing Harbor", "Engine Docker 29.9.3", "Capacity 8 CPUs, 8 GB", "Reached This machine"):
            self.assertIn(said, info)
        self.assertNotIn(" ms", info)
        # no engine here: still reached and measured, the engine said absent
        with patch.object(hosts, "subprocess_run", lambda argv, timeout, env=None: (seen.append(argv), (0, "ncpu=4\nmemtotal_kb=2048000\n", ""))[1]), \
                patch.object(web.settings.prerequisites, "docker_here", lambda fresh=False: "docker is not installed on this machine"):
            down = self.client.post("/settings/hosts/test", data={"entry": "local"}, headers=headers); self.addCleanup(down.close)
        self.assertEqual((down.json["ok"], down.json["docker_ok"], down.json["error"]), (True, False, "docker is not installed on this machine"))
        self.assertEqual((down.json["constraints"]["capacity"]["cpus"], down.json["constraints"]["engine"]), (4, {}))
        self.assertIn("docker is not installed on this machine", down.json["info"]); self.assertEqual(len(seen), 4)

    def test_constraints_are_a_panel_per_machine_saved_as_one_setting(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        constraints.save("bench", {"capacity": {"cpus": 4, "gpus": [{"index": 0, "name": "g", "memory_mb": 1}]},
                                   "enforced": ["cpus"], "refused": {"processes": "max"}})
        value = '{"local": {"cpus": 2}, "bench": {"cpus": 8, "processes": 64, "gpu_ids": []}}'
        r = self.post({"HOSTS": "bench", "HOST_CONSTRAINTS": value})
        self.assertEqual(json.loads(settings.in_file(self.env_file)[constraints.SETTING]), json.loads(value))
        html = self.page()
        card = re.search(r'<article[^>]*id="HOSTS".*?</article>', html, re.S).group(0)
        self.assertEqual(re.findall(r'data-machine="([^"]*)"', card), ["local", "bench"])          # a row per machine
        for c in constraints.TABLE:
            self.assertEqual(card.count(f">{html_text.escape(c.label)}</label>"), 2, c.key)   # each machine has each field
        bench = card.split('data-machine="bench"')[1]
        self.assertIn('data-key="gpu_ids" data-type="list" value="none"', bench)
        self.assertIn("More than the machine has.", bench); self.assertIn("Containers here do not enforce it: see Info, or remove it.", bench)
        said = "1 constraint is not enforced here: see Constraints."                               # processes: the mark by its name
        self.assertIn(f'<svg class="ic host-warn" role="img" aria-label="{said}"><title>{said}</title>', bench)
        self.assertNotIn("host-warn", card.split('data-machine="bench"')[0])                         # this machine: never tested
        self.assertIn('title="The time of this many logical CPUs, on any allowed core. This machine has 4."', bench)
        self.assertNotIn("st-warn", card.split('data-machine="bench"')[0])               # this machine: never tested
        # a slider where the capacity is known: CPUs up to the 4 tested, none for memory without a capacity
        self.assertIn('<input type="range" min="0" step="1" data-range-for="c-2-cpus" data-step="0.1" aria-label="CPUs">', bench)
        self.assertIn('<span class="ui-range-at-mark" data-range-max></span><span title="No limit">∞</span>', bench)
        self.assertEqual(card.count('type="range"'), 1)                                  # never tested: number fields only
        self.post({"HOST_CONSTRAINTS": '{"local": {"disk_gb": 1}}'})
        self.assertIn("not a constraint", self.page())
        self.assertEqual(json.loads(settings.in_file(self.env_file)[constraints.SETTING]), json.loads(value))
        with patch.dict(os.environ, {constraints.SETTING: '{"local": {"cpus": Infinity}}'}):     # set in the environment: said, never read as none
            card = re.search(r'<article[^>]*id="HOSTS".*?</article>', self.page(), re.S).group(0)
        self.assertIn('<p class="st-note st-bad">HOST_CONSTRAINTS must be valid constraints (local: CPUs: a number of at least 0.01).</p>', card)

    def change_host(self, old, new=None, csrf=None):
        data = {"csrf": self.token() if csrf is None else csrf, "host_from": old, **({"host_to": new} if new is not None else {})}
        r = self.client.post("/settings/save", data=data, headers={"Origin": "http://localhost", "Accept": "application/json"})
        self.addCleanup(r.close)
        return r

    def test_each_machine_is_a_row_with_its_actions_and_what_its_last_test_found(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "bench, GPU lab=ubuntu@192.0.2.10:22"})
        constraints.save("bench", {"test": {"ok": True, "latency_ms": 42, "hostname": "ecs-1", "uv": True, "docker": True, "docker_ok": True,
                                            "harbor": False}, "engine": {"kind": "podman", "version": "5.8.9", "rootless": True},
                                   "capacity": {"cpus": 16, "memory_mb": 31789, "gpus": [{"index": 0, "name": "RTX", "memory_mb": 4096}]},
                                   "enforced": ["cpus", "memory_mb"], "refused": {"cpu_set": "cpuset is not available"},
                                   "systemd": {"why": "", "enforced": ["cpus", "memory_mb", "processes"],
                                               "refused": {"cpu_set": "cat: cpuset.cpus.effective: No such file or directory"}}})
        html = self.page()
        table = re.search(r'<table class="host-table">.*?</table>', html, re.S).group(0)
        head = re.search(r"<thead>.*?</thead>", table, re.S).group(0)
        self.assertEqual(re.findall(r'<th scope="col"[^>]*>(.*?)</th>', head),                   # a status and actions header for screen readers alone
                         ['<span class="sr">Status</span>', "Host", "Engine", "Capacity", '<span class="sr">Actions</span>'])
        rows = {m.group(1): m.group(0) for m in re.finditer(r'<tbody class="host-row" data-machine="([^"]*)".*?</tbody>', table, re.S)}
        self.assertEqual(list(rows), ["local", "bench", "GPU lab=ubuntu@192.0.2.10:22"])
        acts = lambda row: re.findall(r'aria-label="(\w+): [^"]*" title="\w+"><svg class="ic" aria-hidden="true"><use href="#i-', row)
        self.assertEqual(acts(rows["local"]), ["Test", "Info", "Constraints"])                   # this machine: no connection, no delete
        self.assertEqual(acts(rows["bench"]), ["Test", "Info", "Connection", "Constraints", "Delete"])
        for row in rows.values():                                                                   # Test is the row's, Info only reads
            self.assertEqual(row.count("data-test-machine"), 1); self.assertNotIn("data-test-machine", row.split('data-panel="info"')[1].split('data-panel="')[0])
        self.assertIn('class="host-dot st-runs"', rows["bench"]); self.assertIn('class="host-dot st-watch"', rows["local"])
        cells = lambda row: re.findall(r'<td class="host-(?:engine|capacity)" data-cell="\w+">([^<]*)</td>', row)
        self.assertEqual(cells(rows["bench"]), ["Podman", "16 CPUs · 31 GB · 1 GPU, 4 GB"])      # whole GB here; Info keeps 31789 MB
        self.assertEqual(cells(rows["local"]), ["Not tested", "Not tested"])
        self.assertIn('<span class="sr" data-status>Reached</span>', rows["bench"]); self.assertIn('<span class="sr" data-status>Not tested</span>', rows["local"])
        for why in ("Connection: not for this machine", "Delete: this machine cannot be removed"):   # in their places, off, saying why
            self.assertIn(f'<button type="button" class="host-act" disabled aria-label="{why}" title="{why}">', rows["local"])
        self.assertNotIn("host-act-gap", rows["local"])
        self.assertEqual(len(re.findall(r'<tr class="host-panel-row"><td colspan="5">', rows["bench"])), 4)   # a full-width row per panel
        info = lambda row: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row.split('data-panel="info"')[1].split('data-panel="')[0]))
        labels = lambda text: re.findall(r"(Reached|Host name|Programs|Engine|Capacity|Containers|Local processes|Drivers|Tested) ", text)
        for row in rows.values():                                                                   # the same rows, in the same order
            self.assertEqual(labels(info(row))[:9], ["Reached", "Host name", "Programs", "Engine", "Capacity", "Containers", "Local processes", "Drivers", "Tested"])
        self.assertIn("Reached This machine", info(rows["local"]))
        never = info(rows["GPU lab=ubuntu@192.0.2.10:22"])
        for said in ("Reached Not tested", "Host name Not tested", "Programs Not tested", "Engine Not tested", "Capacity Not tested",
                     "Containers Not tested", "Local processes Not tested", "Drivers Not tested", "Tested Never"):
            self.assertIn(said, never)
        for said in ("In 42 ms", "ecs-1", "uv, docker", "missing Harbor", "Podman 5.8.9, rootless", "16 CPUs, 31789 MB", "GPU 0: RTX, 4096 MB",
                     'title="Asked CPUs 0: the engine did not apply it (cpuset is not available)">CPUs Nullius may use',
                     'title="Asked CPUs 0: systemd did not apply it (cat: cpuset.cpus.effective: No such file or directory); '
                     'applied instead: the process can widen it">CPUs Nullius may use'):
            self.assertIn(said, rows["bench"])
        for said in ("Containers CPUs, Memory · not CPUs Nullius may use", "Local processes CPUs, Memory, Processes and threads · not CPUs Nullius may use",
                     "Drivers Limited"):
            self.assertIn(said, info(rows["bench"]))
        # a list in Info is one line: its items in one inline element, never a line each in the row's column
        held = re.search(r'Containers</span><span class="st-row-value">(.*?)</span></div>', rows["bench"], re.S).group(1)
        self.assertTrue(held.startswith("<span>CPUs, Memory<span class=\"faint\"> · not <span title=") and held.endswith("</span></span></span>"), held)
        programs = re.search(r'Programs</span><span class="st-row-value">(.*?)</div>', rows["bench"], re.S).group(1)
        self.assertTrue(programs.startswith("<span>"), programs)
        self.assertIn('data-field="display_name" value="GPU lab"', rows["GPU lab=ubuntu@192.0.2.10:22"])   # Connection, filled in
        self.assertIn("Delete <b>GPU lab</b> and its constraints?", html)

    def test_a_rootless_engine_without_a_cgroup_says_so_in_info(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "pod"})
        constraints.save("pod", {"test": {"ok": True, "podman": True, "engine": "podman"}, "capacity": {"cpus": 2},
                                 "engine": {"kind": "podman", "version": "5.8.9", "rootless": True}, "enforced": [],
                                 "refused": {"cpus": "max 100000", "memory_mb": "max max", "processes": "max", "cpu_set": "cat: no cgroup"}})
        constraints.save("local", {"test": {"ok": True}, "capacity": {"cpus": 2}, "engine": {"kind": "podman", "version": "5.8.9", "rootless": True},
                                   "enforced": ["cpus", "memory_mb", "processes"], "refused": {"cpu_set": "controller cpuset is not available"}})
        html = self.page()
        line = "The engine has no cgroup delegated to this user: limits cannot apply here."
        self.assertEqual(html.count(line), 1); self.assertIn(line, html.split('data-machine="pod"')[1])

    def test_a_podman_only_host_reads_as_podman(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "pod, gone"})
        constraints.save("pod", {"test": {"ok": True, "uv": True, "docker": False, "podman": True, "podman_ok": True, "engine": "podman", "harbor": False},
                                 "engine": {"kind": "podman", "version": "5.8.9", "rootless": True}, "capacity": {"cpus": 4}, "enforced": ["cpus"]})
        constraints.save("gone", {"test": {"ok": True, "uv": True, "docker": False, "podman": True, "podman_ok": False, "engine": "podman"},
                                  "capacity": {"cpus": 4}, "engine": {}})
        html = self.page()
        pod, gone = html.split('data-machine="pod"')[1].split('data-machine="gone"')
        self.assertIn("uv, podman", pod); self.assertIn("missing Harbor", pod); self.assertIn("Podman 5.8.9, rootless", pod)
        self.assertNotIn("missing Docker", pod)
        self.assertIn("Podman does not answer", gone)

    def test_info_shows_the_local_capacity_only_where_it_differs(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "split"})
        constraints.save("split", {"test": {"ok": True, "latency_ms": 9}, "engine": {"kind": "docker", "version": "29.9.3"},
                                   "capacity": {"cpus": 16, "memory_mb": 32768, "local": {"cpus": 1.5, "memory_mb": 4096}, "gpus": []}})
        text = lambda: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.page().split('data-machine="split"')[1]))
        self.assertIn("Container capacity 16 CPUs, 32 GB", text()); self.assertIn("Local capacity 1.5 CPUs, 4 GB", text())
        self.assertIn('data-capacity="16"', self.page().split('data-machine="split"')[1])          # the slider: where the runs land
        constraints.save("split", {"capacity": {"cpus": 4, "memory_mb": 4096, "gpus": []}})
        self.assertIn("Capacity 4 CPUs, 4 GB", text()); self.assertNotIn("Local capacity", text())

    def test_a_machine_without_an_engine_keeps_its_caps_visible_but_off(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "bare", "HOST_CONSTRAINTS": '{"bare": {"cpus": 2, "gpu_ids": [0]}}'})
        constraints.save("bare", {"test": {"ok": True, "latency_ms": 9, "uv": True, "docker": False, "harbor": False},
                                  "capacity": {"cpus": 4, "memory_mb": 2000, "cpu_ids": "0-3", "gpus": []}, "engine": {}, "enforced": [], "refused": {}})
        row = self.page().split('data-machine="bare"')[1]
        self.assertEqual(row.count("Needs Docker or Podman, or systemd."), 1)
        field = lambda key: re.search(rf'<input class="st-input" id="c-2-{key}"[^>]*>', row).group(0)
        for key in ("cpus", "memory_mb", "processes"):
            self.assertIn(" disabled", field(key), key)                                         # shown, but off
        for key in ("concurrency", "cpu_set", "gpu_ids"):
            self.assertNotIn(" disabled", field(key), key)                                      # these apply without an engine
        self.assertIn('value="2"', field("cpus")); self.assertIn('class="st-clear" data-clear-constraint aria-label="Remove CPUs"', row)   # still removable
        self.assertIn('data-range-for="c-2-cpus" data-step="0.1" aria-label="CPUs" disabled', row)
        self.assertIn("Engine None", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row))); self.assertIn("4 CPUs, 2000 MB", row)       # Info: the capacity, without an engine
        self.assertNotIn("does not enforce", row); self.assertIn("More than the machine has.", row)   # gpu 0: none there
        # systemd holds the caps for local runs: on, and Info says which way each holds
        constraints.save("bare", {"systemd": {"why": "", "enforced": ["cpus", "memory_mb", "processes"], "refused": {}}})
        row = self.page().split('data-machine="bare"')[1]
        self.assertNotIn("data-engine-less", row); self.assertNotIn(" disabled", field("cpus"))
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row))
        self.assertIn("Containers No engine", text); self.assertIn("Local processes CPUs, Memory, Processes and threads", text)
        constraints.save("bare", {"systemd": {"why": "lingering is off: limits would end with the SSH session", "enforced": [], "refused": {}}})
        row = self.page().split('data-machine="bare"')[1]
        self.assertIn("Needs Docker or Podman, or systemd (lingering is off: limits would end with the SSH session).", row)
        self.assertIn("Local processes Lingering is off", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)))
        self.assertIn("Drivers Not limited", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)))

    def test_a_host_is_edited_its_constraints_follow_and_a_duplicate_is_refused(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "bench, ubuntu@10.0.0.4:22", "HOST_CONSTRAINTS": '{"local": {"cpus": 1}, "ubuntu@10.0.0.4:22": {"cpus": 2}}'})
        constraints.save("ubuntu@10.0.0.4:22", {"capacity": {"cpus": 8}})
        file = lambda: settings.in_file(self.env_file)
        r = self.change_host("ubuntu@10.0.0.4:22", "Lab=ubuntu@10.0.0.4:22:~/.ssh/lab")             # a name and a key: the same machine
        self.assertEqual((r.status_code, r.json["message"]), (200, "Host saved"))
        self.assertEqual(file()["HOSTS"], "bench, Lab=ubuntu@10.0.0.4:22:~/.ssh/lab")
        self.assertEqual(json.loads(file()[constraints.SETTING]), {"local": {"cpus": 1}, "Lab=ubuntu@10.0.0.4:22:~/.ssh/lab": {"cpus": 2}})
        self.assertEqual(constraints.load(), {"Lab=ubuntu@10.0.0.4:22:~/.ssh/lab": {"capacity": {"cpus": 8}, "at": constraints.load()["Lab=ubuntu@10.0.0.4:22:~/.ssh/lab"]["at"]}})
        self.change_host("Lab=ubuntu@10.0.0.4:22:~/.ssh/lab", "Lab=ubuntu@10.0.0.9:22")             # another address: its test is stale
        self.assertTrue(constraints.load()["Lab=ubuntu@10.0.0.9:22"]["stale"])
        self.assertIn("Tested before the last edit.", self.page())
        for bad, why in (("bench", "is a host already"), ("not a host", "not a host")):
            r = self.change_host("Lab=ubuntu@10.0.0.9:22", bad)
            self.assertEqual(r.status_code, 400); self.assertIn(why, r.json["message"])
        self.assertEqual(file()["HOSTS"], "bench, Lab=ubuntu@10.0.0.9:22")
        self.assertNotEqual(self.change_host("bench", "lab", csrf="not-the-token").status_code, 200)
        self.assertEqual(file()["HOSTS"], "bench, Lab=ubuntu@10.0.0.9:22")                        # no form token: nothing written

    def test_a_deleted_host_takes_its_constraints_and_its_test_with_it(self):
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        self.post({"HOSTS": "bench, lab", "HOST_CONSTRAINTS": '{"lab": {"cpus": 2}, "bench": {"cpus": 4}}'})
        constraints.save("lab", {"capacity": {"cpus": 8}}); constraints.save("bench", {"capacity": {"cpus": 8}})
        r = self.change_host("lab")
        self.assertEqual((r.status_code, r.json["message"]), (200, "Host deleted"))
        self.assertEqual(settings.in_file(self.env_file)["HOSTS"], "bench")
        self.assertEqual(json.loads(settings.in_file(self.env_file)[constraints.SETTING]), {"bench": {"cpus": 4}})
        self.assertEqual(list(constraints.load()), ["bench"])
        self.change_host("bench")                                                                  # the last: the setting goes too
        self.assertNotIn("HOSTS", settings.in_file(self.env_file)); self.assertNotIn(constraints.SETTING, settings.in_file(self.env_file))

    def test_hosts_set_in_the_environment_are_read_only(self):
        os.environ["HOSTS"] = "bench"
        html = self.page()
        self.assertIn('aria-label="Connection: bench" title="Set in the environment" disabled', html)
        self.assertIn('aria-label="Delete: bench" title="Set in the environment" disabled', html)
        self.assertRegex(html, r'data-add-host [^>]* disabled title="Set in the environment"'); self.assertIn("HOSTS is set in the process environment", html)
        self.assertNotIn('data-panel="edit"', html)
        r = self.change_host("bench")
        self.assertEqual(r.status_code, 400); self.assertIn("set in the environment", r.json["message"]); self.assertEqual(os.environ["HOSTS"], "bench")

    def test_memory_entered_in_gb_is_kept_in_whole_mb_beside_its_slider(self):
        """1.5 GB as the page sends it (1536 MB) on a host with a capacity: saved
        as is, and drawn again as MB for the page to show in the unit chosen. The
        number box carries the key, never the slider before it, which once took
        the unit and left a GB value to be read as MB."""
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        constraints.save("host3", {"capacity": {"cpus": 3, "memory_mb": 31789, "gpus": []}, "engine": {"kind": "podman", "version": "5.8.9"},
                                   "enforced": ["cpus", "memory_mb", "processes"]})
        self.post({"HOSTS": "host3", "HOST_CONSTRAINTS": '{"host3": {"memory_mb": 1536}}'})
        self.assertEqual(json.loads(settings.in_file(self.env_file)[constraints.SETTING]), {"host3": {"memory_mb": 1536}})
        field = re.search(r'<div class="st-constraint">(?:(?!<div class="st-constraint">).)*?data-key="memory_mb".*?</div>\s*</div>',
                          self.page().split('data-machine="host3"')[1], re.S).group(0)
        self.assertLess(field.index('type="range"'), field.index('data-key="memory_mb"'))          # the slider comes first
        self.assertEqual(field.count("data-key="), 1); self.assertNotIn("data-key", field.split('type="range"')[1].split(">")[0])
        self.assertIn('value="1536"', field); self.assertIn('data-unit="MB"', field); self.assertIn('data-unit-choice="GB"', field)
        self.post({"HOST_CONSTRAINTS": '{"host3": {"memory_mb": 2}}'})                                # 2 read as MB: refused
        self.assertEqual(json.loads(settings.in_file(self.env_file)[constraints.SETTING]), {"host3": {"memory_mb": 1536}})

    def test_a_constraint_and_a_machine_s_constraints_are_removed_through_save(self):
        saved = lambda: json.loads(settings.in_file(self.env_file)[constraints.SETTING])
        self.post({"HOSTS": "bench", "HOST_CONSTRAINTS": '{"local": {"cpus": 2, "memory_mb": 2048}, "bench": {"processes": 64}}'})
        card = re.search(r'<article[^>]*id="HOSTS".*?</article>', self.page(), re.S).group(0)
        here, bench = card.split('data-machine="bench"')
        self.assertEqual(here.count('class="st-clear" data-clear-constraint aria-label="Remove CPUs"'), 1)     # set: its × shows
        self.assertEqual(here.count('class="st-clear off" data-clear-constraint aria-label="Remove Processes and threads"'), 1)   # not set: none
        self.assertEqual(card.count("data-clear-machine>Remove all"), 2)                           # each machine sets one
        self.assertIn('data-unit="MB"', here); self.assertIn('data-unit-choice="GB"', here)       # memory: MB as kept, GB offered
        self.post({"HOST_CONSTRAINTS": '{"local": {"memory_mb": 2048}, "bench": {"processes": 64}}'})      # one constraint
        self.assertEqual(saved(), {"local": {"memory_mb": 2048}, "bench": {"processes": 64}})
        self.post({"HOST_CONSTRAINTS": '{"local": {"memory_mb": 2048}}'})                                 # a machine's all
        self.assertEqual(saved(), {"local": {"memory_mb": 2048}})
        self.assertIn("data-clear-machine hidden>Remove all", self.page().split('data-machine="bench"')[1])
        self.post({"HOST_CONSTRAINTS": "{}"})
        self.assertEqual((saved(), constraints.of("local")), ({}, {}))
        # the same guards as any save: no form token, nothing written
        r = self.post({"HOST_CONSTRAINTS": '{"local": {"cpus": 1}}'}, csrf="not-the-token")
        self.assertNotEqual(r.status_code, 200); self.assertEqual(saved(), {})

    def test_a_manual_host_is_added_from_the_form_with_a_key_file_or_a_password_used_once(self):
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        html = self.page()
        self.assertIn('id="add-host"', html); self.assertIn('name="password"', html); self.assertIn("never stored", html)
        # a key file lands under the key directory, mode 0600, and the entry names it
        from io import BytesIO
        with patch.object(hosts, "KEY_DIR", str(self.tmp / "keys")):
            r = self.client.post("/settings/hosts/key", data={"user": "ubuntu", "host": "10.0.0.4", "port": "2222", "key": (BytesIO(b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"), "id_bench")}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        key = str(self.tmp / "keys" / "ubuntu@10.0.0.4")
        self.assertEqual(r.json["entry"], f"ubuntu@10.0.0.4:2222:{key}"); self.assertEqual(oct(os.stat(key).st_mode & 0o777), "0o600")
        bad = self.client.post("/settings/hosts/key", data={"user": "ubuntu", "host": "10.0.0.4", "key": (BytesIO(b"ssh-ed25519 AAAA"), "id.pub")}, headers=headers); self.addCleanup(bad.close)
        self.assertEqual(bad.status_code, 400); self.assertIn("not a private key", bad.json["error"])
        # a password goes to ssh once, through the enrolment, and is not in the answer, the file or the entry
        with patch.object(hosts, "enrol", return_value=hosts.parse("ubuntu@10.0.0.4:/k/id_ed25519")) as enrol:
            r = self.client.post("/settings/hosts/enrol", data={"user": "ubuntu", "host": "10.0.0.4", "port": "", "password": "hunter2"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json["entry"], "ubuntu@10.0.0.4:/k/id_ed25519")
        self.assertEqual(enrol.call_args.args, ("ubuntu", "10.0.0.4", None, "hunter2"))
        with patch.object(hosts, "enrol", side_effect=RuntimeError("the password was not accepted")):
            r = self.client.post("/settings/hosts/enrol", data={"user": "ubuntu", "host": "10.0.0.4", "password": "wrong"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 502); self.assertEqual(r.json["error"], "the password was not accepted")
        self.assertIsNone(self.file())
        # the same guards as Save
        self.assertEqual(self.client.post("/settings/hosts/enrol", data={"user": "u", "host": "h", "password": "p"}, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.client.post("/settings/hosts/key", data={"user": "u", "host": "h"}, headers=headers, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)

    def test_a_removed_host_takes_the_key_file_saved_for_it(self):
        from io import BytesIO
        keys = self.tmp / "keys"
        self.stack.enter_context(patch.object(hosts, "KEY_DIR", str(keys)))
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        def upload(user, host):
            r = self.client.post("/settings/hosts/key", data={"user": user, "host": host, "key": (BytesIO(b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"), "id")}, headers=headers); self.addCleanup(r.close)
            return r.json["entry"]
        own, mine = keys / "id_ed25519", self.tmp / "my_key"
        keys.mkdir(); own.write_text("own"); (keys / "id_ed25519.pub").write_text("pub"); mine.write_text("mine")
        a, b = upload("ubuntu", "10.0.0.4"), upload("ubuntu", "10.0.0.5")
        enrolled, by_path = f"root@10.0.0.6:{own}", f"me@10.0.0.7:{mine}"
        twice = f"Lab=ubuntu@10.0.0.5:2222:{keys / 'ubuntu@10.0.0.5'}"   # a second entry naming b's key
        self.post({"HOSTS": ", ".join([a, b, twice, enrolled, by_path])})
        # without the form token: nothing written, nothing deleted
        self.assertEqual(self.post({"HOSTS": "bench"}, csrf="wrong").status_code, 403)
        self.assertTrue((keys / "ubuntu@10.0.0.4").exists())
        self.post({"HOSTS": ", ".join([twice, enrolled, by_path])})
        self.assertFalse((keys / "ubuntu@10.0.0.4").exists())
        self.assertTrue((keys / "ubuntu@10.0.0.5").exists())                # another entry still uses it
        self.post(clear=["HOSTS"])
        self.assertEqual(sorted(p.name for p in keys.iterdir()), ["id_ed25519", "id_ed25519.pub"])   # Nullius's own key stays
        self.assertTrue(mine.exists())                                        # a key named by path is the user's
        # HOSTS from the process environment is not this page's: nothing deleted
        c = upload("ubuntu", "10.0.0.8")
        os.environ["HOSTS"] = c
        self.post(clear=["HOSTS"])
        self.assertTrue((keys / "ubuntu@10.0.0.8").exists())

    def test_delete_and_connection_take_the_key_file_saved_for_the_host(self):
        keys = self.tmp / "keys"
        self.stack.enter_context(patch.object(hosts, "KEY_DIR", str(keys)))
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        a, b = (f"ubuntu@{ip}:{hosts.save_key('ubuntu', ip, b'-----BEGIN OPENSSH PRIVATE KEY-----' + chr(10).encode())}" for ip in ("10.0.0.4", "10.0.0.5"))
        self.post({"HOSTS": f"{a}, {b}"})
        self.assertTrue(self.change_host(a).json["ok"])                              # Delete
        self.assertFalse((keys / "ubuntu@10.0.0.4").exists())
        self.assertTrue(self.change_host(b, b.replace("10.0.0.5:", "10.0.0.5:2222:")).json["ok"])   # Connection, the same key
        self.assertTrue((keys / "ubuntu@10.0.0.5").exists())
        self.assertTrue(self.change_host(b.replace("10.0.0.5:", "10.0.0.5:2222:"), "ubuntu@10.0.0.5:2222").json["ok"])   # Connection, no key
        self.assertFalse((keys / "ubuntu@10.0.0.5").exists())

    def test_a_key_file_that_cannot_be_removed_is_said_and_the_list_is_saved(self):
        self.stack.enter_context(patch.object(hosts, "KEY_DIR", str(self.tmp / "keys")))
        key = hosts.save_key("ubuntu", "10.0.0.4", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
        self.post({"HOSTS": f"ubuntu@10.0.0.4:{key}, bench"})
        def refuse(path):
            raise PermissionError(13, "Permission denied", path)
        with patch.object(os, "unlink", refuse):
            r = self.post({"HOSTS": "bench"}, headers={"Origin": "http://localhost", "Accept": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["message"], "Saved. The key file for ubuntu@10.0.0.4 could not be removed: Permission denied.")
        self.assertEqual(self.file(), "HOSTS=bench\n"); self.assertTrue(os.path.exists(key))

    def test_the_sandbox_is_asked_when_shown_and_a_login_goes_to_docker_only(self):
        html = self.page()
        section = html[html.index('id="sandbox"'):html.index('id="general"')]
        self.assertIn('data-sandbox="/settings/sandbox"', section)
        self.assertNotIn("data-images", section)                                         # no Images card
        self.assertNotIn('name="value:', section)                                       # outside Save: nothing of it lands in the file
        for login in ("Docker Hub", 'type="password"', "data-login", "data-logout"):      # signed in once, under Data providers
            self.assertNotIn(login, section)
        self.assertNotIn('data-data-form', section)
        providers = html[html.index('id="data-providers"'):html.index('id="sandbox"')]
        self.assertIn('data-data-search', providers)
        self.assertIn('<h3>Docker Hub</h3>', providers)
        self.assertIn('<h3>Hugging Face</h3>', providers)
        self.assertIn('name="token" type="password"', providers)
        with patch.object(sandbox, "docker_state", lambda: {"ok": True, "version": "29.0"}), \
                patch.object(sandbox, "login_user", lambda: self.fail("asked")):
            r = self.client.get("/settings/sandbox"); self.addCleanup(r.close)
        self.assertEqual(r.json, {"docker": {"ok": True, "version": "29.0"}})
        seen = []
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        with patch.object(sandbox, "login", lambda u, t: seen.append((u, t)) or {"ok": True, "user": u}):
            r = self.client.post("/settings/sandbox/login", data={"username": "someone", "token": "dckr_pat_synthetic"}, headers=headers); self.addCleanup(r.close)
            self.assertEqual(r.json, {"ok": True, "user": "someone"})
            self.assertNotIn("dckr_pat_synthetic", r.get_data(as_text=True))
            # the same guards as Save: token, origin, loopback
            for kw in ({"headers": {"Origin": "http://localhost"}}, {"headers": headers, "environ_base": {"REMOTE_ADDR": "10.0.0.7"}}):
                self.assertEqual(self.client.post("/settings/sandbox/login", data={"username": "x", "token": "y"}, **kw).status_code, 403)
        self.assertEqual(seen, [("someone", "dckr_pat_synthetic")])
        self.assertIsNone(self.file())

    def test_data_provider_save_is_local_guarded_and_never_echoes_token(self):
        from nullius import secrets
        from nullius.standard import definitions, run
        gaia = run.Licences(definitions.get("benchmark", "gaia"))                  # GAIA's HF_TOKEN: this provider's alone
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"SECRETS_DIR": folder}):
            headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
            url = "/settings/data-providers/huggingface"
            for kwargs in ({"headers": {"Origin": "http://localhost"}},
                           {"headers": headers, "environ_base": {"REMOTE_ADDR": "10.0.0.7"}}):
                self.assertEqual(self.client.post(url, data={"token": "synthetic-data-token"}, **kwargs).status_code, 403)
            response = self.client.post(url, data={"token": "synthetic-data-token"}, headers=headers)
            self.assertTrue(response.json["ok"])
            self.assertNotIn("synthetic-data-token", response.get_data(as_text=True))
            self.assertEqual(secrets.value(secrets.HUGGINGFACE, "HF_TOKEN"), b"synthetic-data-token")
            self.assertEqual(gaia.get("HF_TOKEN"), {"kind": "value", "value": "synthetic-data-token"})
            with patch.object(sandbox, "login_user", return_value=None):
                state = self.client.get("/settings/data-providers").json
            self.assertEqual(state, {"huggingface": True, "huggingface_endpoint": "", "huggingface_endpoint_env": False,
                                     "huggingface_endpoint_default": "https://huggingface.co", "dockerhub": None})
            self.assertIsNone(self.file())
            response = self.client.post(url, data={"action": "remove"}, headers=headers)
            self.assertTrue(response.json["ok"])
            self.assertIsNone(gaia.get("HF_TOKEN"))

    def test_the_hugging_face_endpoint_is_saved_apart_from_the_token(self):
        from nullius import secrets
        self.stack.enter_context(patch.dict(os.environ, {"SECRETS_DIR": str(self.tmp / "secrets")}))
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        url = "/settings/data-providers/huggingface"
        state = lambda: self.client.get("/settings/data-providers").json
        with patch.object(sandbox, "login_user", return_value=None):
            self.assertEqual(self.client.post(url, data={}, headers=headers).status_code, 400)       # neither: nothing to save
            r = self.client.post(url, data={"endpoint": "ftp://mirror"}, headers=headers)
            self.assertEqual((r.status_code, r.json["ok"]), (400, False))
            r = self.client.post(url, data={"endpoint": "https://hf-mirror.com/"}, headers=headers)   # the endpoint alone
            self.assertTrue(r.json["ok"])
            self.assertEqual(settings.in_file(self.env_file)["HF_ENDPOINT"], "https://hf-mirror.com")
            self.assertIsNone(secrets.value(secrets.HUGGINGFACE, "HF_TOKEN"))
            self.assertEqual((state()["huggingface"], state()["huggingface_endpoint"]), (False, "https://hf-mirror.com"))
            r = self.client.post(url, data={"token": "synthetic-data-token", "endpoint": "https://hf-mirror.com"}, headers=headers)
            self.assertTrue(r.json["ok"])
            self.assertEqual(secrets.value(secrets.HUGGINGFACE, "HF_TOKEN"), b"synthetic-data-token")
            self.assertNotIn("synthetic-data-token", json.dumps(settings.in_file(self.env_file)))     # the token is never a setting
            r = self.client.post(url, data={"endpoint": "https://huggingface.co"}, headers=headers)   # the default: removed, token kept
            self.assertTrue(r.json["ok"])
            self.assertNotIn("HF_ENDPOINT", settings.in_file(self.env_file))
            self.assertEqual((state()["huggingface"], state()["huggingface_endpoint"]), (True, ""))
            # the environment wins: shown, marked, never written over
            with patch.dict(os.environ, {"HF_ENDPOINT": "https://env-mirror.example"}):
                self.assertEqual((state()["huggingface_endpoint"], state()["huggingface_endpoint_env"]), ("https://env-mirror.example", True))
                self.assertTrue(self.client.post(url, data={"endpoint": "https://other.example"}, headers=headers).json["ok"])
                self.assertNotIn("HF_ENDPOINT", settings.in_file(self.env_file))

    def test_the_price_list_is_updated_and_priced_only_from_this_machines_browser(self):
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_PRICES": str(self.tmp / "prices.json")}))
        page = self.page()
        section = page[page.index('id="prices"'):page.index('id="general"')]
        self.assertIn('name="cache_write"', section)
        self.assertIn('name="cache_write_1h"', section)
        self.assertNotIn('name="value:', section)                                       # outside Save: nothing of it lands in the file
        state = json.loads(html_text.unescape(re.search(r"data-state='([^']*)'", section).group(1)))
        self.assertEqual(state, {"n": 0, "from": "", "on": "", "own": {}})               # nothing ships with Nullius
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        listing = json.dumps({"model-a": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05}}).encode()
        with patch.object(prices, "_fetch", lambda source: listing):
            r = self.client.post("/settings/prices/update", headers=headers); self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.json["ok"], r.json["n"], r.json["from"]), (200, True, 1, prices.SOURCE))
        self.assertEqual(prices.table(), {"model-a": {"input": 2.5, "output": 10.0}})
        with patch.object(prices, "_fetch", lambda source: b"<html>not a list"):
            r = self.client.post("/settings/prices/update", headers=headers); self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.json["ok"]), (502, False))
        self.assertEqual(prices.state()["n"], 1)                                        # the list held stays
        r = self.client.post("/settings/prices/set", data={"model": "model-a", "input": "1", "output": "2", "cached": ""}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.json["own"], {"model-a": {"input": 1.0, "output": 2.0}})
        self.assertEqual(prices.table()["model-a"]["input"], 1.0)                       # the user's own, over the list
        r = self.client.post("/settings/prices/set", data={"model": "model-a", "input": "1", "output": "2",
                             "cache_write": "1.25", "cache_write_1h": "2"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.json["own"]["model-a"], {"input": 1.0, "output": 2.0, "cache_write": 1.25, "cache_write_1h": 2.0})
        for bad in ({"model": "m", "input": "free", "output": "2"}, {"model": "m", "input": "1"}, {"model": "", "input": "1", "output": "2"}):
            r = self.client.post("/settings/prices/set", data=bad, headers=headers); self.addCleanup(r.close)
            self.assertEqual((r.status_code, r.json["ok"]), (400, False), bad)
        r = self.client.post("/settings/prices/remove", data={"model": "model-a"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.json["own"], {})
        self.assertEqual(self.client.post("/settings/prices/remove", data={"model": "model-a"}, headers=headers).status_code, 404)
        # the same guards as Save: token, origin, loopback
        for route in ("update", "set", "remove"):
            for kw in ({"headers": {"Origin": "http://localhost"}}, {"headers": headers, "environ_base": {"REMOTE_ADDR": "10.0.0.7"}}):
                self.assertEqual(self.client.post(f"/settings/prices/{route}", data={"model": "x", "input": "1", "output": "2"}, **kw).status_code, 403)
        self.assertIsNone(self.file())

    def ask(self, provider, csrf=None, **kw):
        r = self.client.post("/settings/providers/test", data={"csrf": self.token() if csrf is None else csrf, "provider": provider},
                             headers={"Origin": "http://localhost"}, **kw)
        self.addCleanup(r.close)
        return r

    def card(self, html, key):
        return re.search(r'data-provider-card="%s".*?</h3>(.*?)<div class="st-provider-fields' % re.escape(key), html, re.S).group(1)

    def test_a_provider_card_tests_on_a_click_and_keeps_its_result(self):
        key = "sk-synthetic-route-key-0123456789"
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp)}))
        asked = []
        def test(choice, env=None, **kw):
            asked.append(choice)
            return {"at": "2026-11-12T10:00:00Z", "ok": False, "reached": True, "key": "refused", "models": None, "ms": 7, "error": ""}
        self.stack.enter_context(patch.object(providers, "test", test))
        self.post({"OPENAI_API_KEY": key, "TYPESAFE_API_KEY": "ts-synthetic-0123456789"})
        html = self.page()
        self.assertEqual(asked, [])                                         # never on save or on a page load
        self.assertIn('data-provider-test="openai"', self.card(html, "openai"))
        self.assertNotIn("data-provider-test=", self.card(html, "typesafe"))
        self.assertIn("data-provider-test-all", html)
        with self.assertNoLogs(level="DEBUG"):
            r = self.ask("openai")
        self.assertEqual((r.status_code, r.get_json()["said"], r.get_json()["mark"]), (200, "Key refused · 2026-11-12 10:00 UTC", "bad"))
        self.assertNotIn(key, r.get_data(as_text=True))
        card = self.card(self.page(), "openai")
        self.assertIn('data-state="bad"', card); self.assertIn("Key refused · 2026-11-12 10:00 UTC", card)
        self.assertNotIn(key, Path(self.tmp, "data", "provider-tests.json").read_text())
        self.post({"OPENAI_API_KEY": key + "x"})                            # a new key: the old result no longer describes it
        self.assertNotIn("Key refused", self.card(self.page(), "openai"))
        self.assertEqual(asked, ["openai"])

    def test_typesafe_has_no_test_and_a_test_keeps_the_write_guards(self):
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp)}))
        self.stack.enter_context(patch.object(providers, "_get", lambda *a, **kw: self.fail("must not fetch")))
        os.environ["TYPESAFE_API_KEY"] = "ts-synthetic-0123456789"
        self.assertEqual(self.ask("typesafe").status_code, 400)
        self.assertEqual(self.ask("openai", csrf="invalid").status_code, 403)
        self.assertEqual(self.ask("openai", environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)
        self.assertEqual(providers.tests(), {})


class StartUp(unittest.TestCase):
    """app.py's command line: the address and port come from APP_HOST and
    APP_PORT (app/.env is loaded before it is read), the flags win, and a
    port that is not one stops the start with one line, not a traceback."""

    def args(self, argv=(), **env):
        with patch.dict(os.environ, {}, clear=False):
            for name in ("APP_HOST", "APP_PORT", "HOST", "PORT", "DEBUG"):
                os.environ.pop(name, None)
            os.environ.update(env)
            return web.parser().parse_args(list(argv))

    def test_app_host_and_app_port_are_the_defaults_and_the_flags_win(self):
        a = self.args(APP_HOST="0.0.0.0", APP_PORT="6000")
        self.assertEqual((a.host, a.port), ("0.0.0.0", 6000))
        a = self.args(["--host", "::1", "--port", "7000"], APP_HOST="0.0.0.0", APP_PORT="6000")
        self.assertEqual((a.host, a.port), ("::1", 7000))
        a = self.args(HOST="0.0.0.0", PORT="6000")                                   # the old names are read by nobody
        self.assertEqual((a.host, a.port), ("127.0.0.1", 5057))
        a = self.args(APP_HOST="", APP_PORT="")                                      # an empty line is unset
        self.assertEqual((a.host, a.port), ("127.0.0.1", 5057))

    def test_debug_is_opt_in_and_flags_override_environment(self):
        self.assertFalse(self.args().debug)
        for value in ("", "0", "false", "no"):
            self.assertFalse(self.args(DEBUG=value).debug)
        for value in ("1", "true", "yes"):
            self.assertTrue(self.args(DEBUG=value).debug)
            self.assertFalse(self.args(["--no-debug"], DEBUG=value).debug)
        self.assertTrue(self.args(["--debug"], DEBUG="0").debug)

    def test_a_bad_app_port_stops_the_start_with_one_line(self):
        import io
        from contextlib import redirect_stderr
        for bad in ("0", "70000", "5o57"):
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                self.args(APP_PORT=bad)
            self.assertEqual(stop.exception.code, 2)
            self.assertNotIn("Traceback", err.getvalue())
            last = err.getvalue().strip().splitlines()[-1]
            self.assertIn(f"{bad!r} is not a port between 1 and 65535 (APP_PORT in saved settings or environment, or --port)", last)

if __name__ == "__main__":
    unittest.main()
