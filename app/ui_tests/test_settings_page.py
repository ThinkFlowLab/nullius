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
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import hosts, prices, sandbox, settings
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
        self.assertEqual(html.count("data-unset="), 3)                                            # × on every set variable but the list (its chips have their own), none on an unset one

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
        self.assertNotIn("<details", html.split('<dialog id="add-host"')[0])
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
        self.assertNotIn(">not set<", hosts_row); self.assertNotIn('class="chip st-watch host-pill"', hosts_row); self.assertIn("+ Add host", hosts_row)
        self.assertEqual(re.findall(r'placeholder="([^"]*)"', html.split('<dialog id="add-host"')[0]), ["Search settings", "Search providers…", "(optional)", "Search data providers…"])                           # no field suggests anything
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
        self.assertIn('data-list="HOSTS"', html); self.assertIn('data-entry="ubuntu@10.0.0.4:22"', html); self.assertIn('data-entry="bench"', html)
        self.assertEqual(html.count('class="chip st-watch host-pill"'), 2); self.assertIn('class="host-name" data-test-host', html)   # the hosts chosen, chips; the name tests
        self.assertIn('data-host-chip="bench"', html); self.assertIn('data-host-chip="lab"', html)   # the config's aliases, in the Add host dialog
        self.assertIn('data-tab="config"', html); self.assertIn('data-tab="manual"', html); self.assertIn("data-dialog-test", html); self.assertIn("data-dialog-add", html); self.assertIn("data-test-all>", html)
        self.assertNotIn('value="bench"', html)                                                    # an alias is not a field
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
        self.assertIn('data-test-host>GPU lab</button>', html)
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
        dialog = re.search(r'<dialog id="add-host".*?</dialog>', html, re.S)
        self.assertIsNotNone(dialog); dialog = dialog.group(0)
        self.assertEqual(dialog.count('<div class="dialog-panels">'), 1)      # opened, and inside the dialog
        for needed in ('data-panel="manual"', 'class="dialog-foot"', 'data-cancel', 'data-dialog-test', 'data-dialog-add', 'class="settings-test dialog-status"'):
            self.assertIn(needed, dialog, needed)
        self.assertNotIn('data-tab="config"', dialog); self.assertNotIn('data-panel="config"', dialog)
        self.assertNotIn(' hidden>', dialog.split('<form')[1].split('>')[0] + '>')   # the only panel is not hidden
        self.assertEqual(dialog.count('</div>'), dialog.count('<div '))              # balanced, so nothing above is closed
        scanned = [{"alias": "bench", "hostname": "10.0.0.4", "user": "ubuntu", "port": "22"}]
        with patch.object(hosts, "scan_ssh_config", return_value=scanned):
            with_config = re.search(r'<dialog id="add-host".*?</dialog>', self.page(), re.S).group(0)
        self.assertEqual(with_config.count('<div class="dialog-panels">'), 1)
        self.assertIn('data-panel="config"', with_config); self.assertIn('data-cancel', with_config)
        self.assertEqual(with_config.count('</div>'), with_config.count('<div '))

    def test_a_host_test_sends_one_probe_and_stores_nothing(self):
        seen = []
        def run(argv, timeout):
            seen.append(argv); return 0, "ok\nuv=yes\ndocker=yes\nharbor=no\ndocker_ok=yes\nhostname=lab\n", ""
        headers = {"Origin": "http://localhost", "X-CSRF-Token": self.token()}
        with patch.object(hosts, "subprocess_run", run):
            r = self.client.post("/settings/hosts/test", data={"entry": "ubuntu@10.0.0.4:2222"}, headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json["ok"], r.json["uv"], r.json["harbor"], r.json["hostname"]), (True, True, False, "lab"))
        self.assertEqual(seen[0][-2:], ["ubuntu@10.0.0.4", hosts.PROBE]); self.assertIn("BatchMode=yes", seen[0])
        self.assertIsNone(self.file())
        bad = self.client.post("/settings/hosts/test", data={"entry": "not a host"}, headers=headers); self.addCleanup(bad.close)
        self.assertEqual(bad.status_code, 400); self.assertIn("not a host", bad.json["error"])
        # the same guards as Save: token, origin, loopback
        self.assertEqual(self.client.post("/settings/hosts/test", data={"entry": "bench"}, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.client.post("/settings/hosts/test", data={"entry": "bench"}, headers=headers, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)
        self.assertEqual(len(seen), 1)

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
            self.assertEqual(state, {"huggingface": True, "dockerhub": None})
            self.assertIsNone(self.file())
            response = self.client.post(url, data={"action": "remove"}, headers=headers)
            self.assertTrue(response.json["ok"])
            self.assertIsNone(gaia.get("HF_TOKEN"))

    def test_the_price_list_is_updated_and_priced_only_from_this_machines_browser(self):
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_PRICES": str(self.tmp / "prices.json")}))
        page = self.page()
        section = page[page.index('id="prices"'):page.index('id="general"')]
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
