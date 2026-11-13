"""The settings table and the one file it writes: loading with the
environment winning, saving one variable at a time, validation per kind,
secrets shown by presence only, the example rendered from the same table."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nullius import settings

HERE = Path(__file__).resolve().parent


class Table(unittest.TestCase):
    def test_every_variable_the_application_reads_is_in_the_table(self):
        # the programs the standard starts - a benchmark's driver, an agent, a
        # capability, a conformance kit - read their own environment, not Nullius's
        own = lambda p: not any(part in ("agents", "capabilities", "kits") for part in p.parts) and not (
            "benchmarks" in p.parts and p.name != "__init__.py")
        src = (HERE.parent / "app.py").read_text() + "".join(p.read_text() for p in list((HERE.parent / "nullius").rglob("*.py")) + list((HERE.parent / "web").glob("*.py")) if p.name != "settings.py" and own(p))
        import re
        read = set(re.findall(r'environ(?:\.get\(|\[)\s*"([A-Z][A-Z0-9_]*)"', src)) | set(re.findall(r'env\.get\("([A-Z][A-Z0-9_]*)"', src))
        read -= {"HOME"}                                                  # the user's home directory, not a Nullius setting
        read -= {"GH_TOKEN"}                                              # stripped from subprocesses, never read
        read -= {"LOCALAPPDATA"}                                          # Windows's user directory, not a Nullius setting
        read -= {"OPENAI_BASE_URL"}                                       # what a run's launcher hands it (execution.URL_NAMES), read by the run: standard cli --from-run
        read -= {"NULLIUS_LICENCE_DIR"}                                   # the same: where a host's run writes a licence file (execution, standard cli)
        read -= {"NULLIUS_FORWARD_TO"}                                    # a licence server's forwarder's, in its own container (standard/forward.py)
        read -= {"NULLIUS_MCP_AT"}                                        # the MCP bridge's, in the agent's container (standard/mcp_bridge.py)
        read -= {"DOCKER_CONFIG"}                                         # docker's own: where it keeps a login (sandbox.py reads the username)
        read -= {"DOCKER_HOST", "DOCKER_CONTEXT"}                         # docker's own: when set, container.py leaves the current context to them
        read -= {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}          # the user's session: where systemd-run finds its manager (constraints.session)
        missing = sorted(v for v in read if v not in settings.BY_NAME and v not in settings.STARTUP)
        self.assertEqual(missing, [], f"read by the application but neither on the page nor a named start-up option: {missing}")
        self.assertEqual(sorted(set(settings.BY_NAME) & set(settings.STARTUP)), [])
        stale = sorted(v for v in settings.STARTUP if v not in read)
        self.assertEqual(stale, [], f"named as start-up options but no longer read: {stale}")

    def test_the_page_offers_what_a_person_sets_and_no_more(self):
        self.assertEqual([v.name for v in settings.VARIABLES],
                         ["MODEL_API_KEY", "MODEL_API_URL", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_PROVIDER", "SYSTEM_ONE_MODEL", "LLM_JUDGE_PROVIDER", "LLM_JUDGE_MODEL", "AGENT_KEYS", "HOSTS", "HF_ENDPOINT", "HOST_CONSTRAINTS"])
        self.assertEqual(sorted(v.name for v in settings.VARIABLES if v.secret), ["ANTHROPIC_API_KEY", "MODEL_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"])
        self.assertEqual(settings.BY_NAME["MODEL_API_URL"].kind, "endpoint")                         # shown, so it may hold no credential
        for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
            self.assertEqual(settings.BY_NAME[name].example, "")                               # no placeholder on a settings field
        self.assertEqual({settings.BY_NAME[n].group for n in ("MODEL_API_KEY", "MODEL_API_URL", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY")}, {"Model credentials"})
        from nullius import providers
        self.assertIn(providers.PROVIDER_BASE["openrouter"], settings.BY_NAME["OPENROUTER_API_KEY"].purpose)   # the endpoint is fixed, and said
        self.assertIn("OpenAI-compatible", settings.BY_NAME["MODEL_API_KEY"].purpose)

    def test_the_servers_address_and_port_are_start_up_options(self):
        """APP_HOST and APP_PORT are read at startup; the running Settings page
        cannot change the server's listening address."""
        for name in ("APP_HOST", "APP_PORT"):
            self.assertIn(name, settings.STARTUP); self.assertNotIn(name, settings.BY_NAME)
        for old in ("HOST", "PORT"):
            self.assertNotIn(old, settings.BY_NAME); self.assertNotIn(old, settings.STARTUP)
        self.assertEqual((settings.INSTEAD["HOST"], settings.INSTEAD["PORT"]), ("APP_HOST", "APP_PORT"))
        self.assertEqual(settings.GROUPS, ["Model credentials", "Run hosts", "Custom keys", "Data providers"])
        example = settings.example()
        self.assertIn("#   APP_PORT: ", example); self.assertNotIn("\nAPP_PORT=", example); self.assertNotIn("\nPORT=", example)

    def test_example_file_matches_the_table(self):
        example = settings.example()
        for v in settings.VARIABLES:
            self.assertIn(f"\n{v.name}=\n", example)
        self.assertNotIn("ghp_x", example)
        on_disk = (HERE.parent / ".env.example").read_text()
        self.assertEqual(on_disk, example, "app/.env.example drifted from nullius.settings.example(); regenerate it")


class Validation(unittest.TestCase):
    def ok(self, name, value):
        self.assertIsNone(settings.BY_NAME[name].validate(value), (name, value))

    def bad(self, name, value):
        self.assertIsNotNone(settings.BY_NAME[name].validate(value), (name, value))

    def test_kinds(self):
        self.ok("HOSTS", "bench"); self.ok("HOSTS", "bench, ubuntu@10.0.0.4:22"); self.bad("HOSTS", "a host")
        self.ok("MODEL_API_KEY", "sk-abcdefghijklmnop"); self.bad("MODEL_API_KEY", "has space"); self.ok("MODEL_API_URL", "https://llm.example/v1")
        self.ok("MODEL_API_KEY", "ollama"); self.ok("MODEL_API_KEY", "EMPTY")     # a local server's key is often a short word
        self.ok("OPENAI_API_KEY", "ollama")                                         # key formats are the provider's responsibility
        for name in ("HOSTS", "MODEL_API_KEY"):
            self.bad(name, "x\ny"); self.bad(name, " padded ")

    def test_keys_and_agent_names_have_no_arbitrary_length_limits(self):
        for value in ("x", "k" * 4096):
            self.ok("OPENAI_API_KEY", value)
            self.ok("MODEL_API_KEY", value)
            self.assertIsNone(settings.agent_variable("EXAMPLE").validate(value))
        self.assertIsNone(settings._agent_key_names("AGENT_" + "X" * 256))
        self.assertIsNotNone(settings._agent_key_names("NOT-A-VARIABLE"))
        self.bad("OPENAI_API_KEY", "a\nb")


class File(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-settings-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, ".env")
        self.addCleanup(settings.LOADED_KEYS.clear)
        settings.LOADED_KEYS.clear()
        self.stack = patch.dict(os.environ, {}, clear=False); self.stack.start(); self.addCleanup(self.stack.stop)
        for v in settings.VARIABLES:
            os.environ.pop(v.name, None)

    def write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)

    def test_load_applies_the_file_but_the_environment_wins(self):
        self.write("# comment\nOPENAI_API_KEY=sk-file-0123456789\nHOSTS=\"bench\"\n\nTYPESAFE_API_KEY=ts-file-0123456789\n")
        os.environ["TYPESAFE_API_KEY"] = "ts-env-0123456789"
        applied = settings.load(self.path)
        self.assertEqual(applied, {"OPENAI_API_KEY", "HOSTS"})
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-file-0123456789"); self.assertEqual(os.environ["HOSTS"], "bench")
        self.assertEqual(os.environ["TYPESAFE_API_KEY"], "ts-env-0123456789")
        by = {s["name"]: s for s in settings.states(self.path)}
        self.assertEqual(by["HOSTS"]["source"], "file"); self.assertEqual(by["HOSTS"]["shown"], "bench")
        self.assertEqual(by["TYPESAFE_API_KEY"]["source"], "environment"); self.assertTrue(by["TYPESAFE_API_KEY"]["shadowed"])
        self.assertEqual(by["MODEL_API_URL"]["source"], "unset"); self.assertEqual(by["MODEL_API_URL"]["shown"], "")

    def test_a_secret_is_shown_as_eight_stars_whatever_it_is(self):
        self.write("MODEL_API_KEY=sk-0secretsecretsecret\n")
        settings.load(self.path)
        row = next(s for s in settings.states(self.path) if s["name"] == "MODEL_API_KEY")
        self.assertEqual(row["shown"], "********"); self.assertTrue(row["secret"])                 # not even its length
        self.assertNotIn("secretsecret", repr(settings.states(self.path)))

    def test_a_local_endpoint_without_a_key_is_advised_one(self):
        def advice():
            return next(s for s in settings.states(self.path) if s["name"] == "MODEL_API_KEY")["advice"]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODEL_API_KEY", None); os.environ.pop("MODEL_API_URL", None)
            self.assertEqual(advice(), "")                                                      # no local endpoint, nothing to say
            os.environ["MODEL_API_URL"] = "http://localhost:11434/v1"
            self.assertIn("a key is advised", advice())
            os.environ["MODEL_API_KEY"] = "ollama"
            self.assertEqual(advice(), "")                                                      # a short key is the user's choice

    def test_save_keeps_other_lines_and_applies_live(self):
        self.write("# kept\nHOSTS=bench-a\n\nOPENAI_API_KEY=sk-kept-0123456789\n")
        settings.load(self.path)
        settings.save(self.path, "HOSTS", "bench-b")
        settings.save(self.path, "MODEL_API_URL", "https://llm.example/v1")
        text = Path(self.path).read_text()
        self.assertEqual(text, "# kept\nHOSTS=bench-b\n\nOPENAI_API_KEY=sk-kept-0123456789\nMODEL_API_URL=https://llm.example/v1\n")
        self.assertEqual(os.environ["HOSTS"], "bench-b"); self.assertEqual(os.environ["MODEL_API_URL"], "https://llm.example/v1")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.tmp) if n != ".env"], [])       # no temporary file left behind

    def test_clear_removes_the_line_and_the_variable(self):
        self.write("HOSTS=bench-a\nHOSTS=bench-b\nOPENAI_API_KEY=sk-kept-0123456789\n")
        settings.load(self.path)
        settings.save(self.path, "HOSTS", None)
        self.assertEqual(Path(self.path).read_text(), "OPENAI_API_KEY=sk-kept-0123456789\n")
        self.assertNotIn("HOSTS", os.environ)

    def test_an_environment_variable_is_written_but_not_overridden(self):
        os.environ["HOSTS"] = "bench-env"
        settings.save(self.path, "HOSTS", "bench-file")
        self.assertEqual(Path(self.path).read_text(), "HOSTS=bench-file\n")
        self.assertEqual(os.environ["HOSTS"], "bench-env")                           # the process keeps what it was started with

    def test_invalid_values_and_unknown_names_write_nothing(self):
        with self.assertRaises(ValueError):
            settings.save(self.path, "HOSTS", "not a host")
        with self.assertRaises(ValueError):
            settings.save(self.path, "OPENAI_BASE_URL", "https://anything/v1")
        for name in ("PORT", "APP_PORT"):                                            # the old name, and a start-up option: not the page's to write
            with self.assertRaises(ValueError):
                settings.save(self.path, name, "6000")
        with self.assertRaises(ValueError):
            settings.save(self.path, "HOSTS", "bench\nAPP_PORT=1")
        self.assertFalse(os.path.exists(self.path))

    def test_a_key_written_after_load_is_applied_once_the_file_changed(self):
        # a key added by hand after start counts without a restart: sync reads the file again when it is not as last read, and nothing otherwise
        self.write("MODEL_API_KEY=sk-fake-0123456789\n")
        settings.load(self.path)
        self.assertEqual(settings.sync(self.path), {"loaded": [], "updated": [], "removed": []})          # first look: as loaded
        with open(self.path, "a") as fh:
            fh.write("TYPESAFE_API_KEY=ts-fake-0123456789\n")
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)                                                 # not seen yet
        self.assertEqual(settings.sync(self.path), {"loaded": ["TYPESAFE_API_KEY"], "updated": [], "removed": []})
        row = next(s for s in settings.states(self.path) if s["name"] == "TYPESAFE_API_KEY")
        self.assertEqual((row["source"], row["shown"]), ("file", "********"))
        self.assertEqual(os.environ["TYPESAFE_API_KEY"], "ts-fake-0123456789")             # applied, as load would have
        self.assertIn("TYPESAFE_API_KEY", settings.LOADED_KEYS)
        self.assertEqual(settings.sync(self.path), {"loaded": [], "updated": [], "removed": []})    # unchanged: not read again
        os.remove(self.path)
        self.assertEqual(settings.sync(self.path), {"loaded": [], "updated": [], "removed": ["MODEL_API_KEY", "TYPESAFE_API_KEY"]})   # gone: its keys with it
        self.assertEqual(settings.sync(self.path), {"loaded": [], "updated": [], "removed": []})

    def test_a_file_key_follows_the_file_and_the_environment_still_wins(self):
        os.environ["TYPESAFE_API_KEY"] = "ts-env-0123456789"
        self.write("OPENAI_API_KEY=sk-one-0123456789\nHOSTS=bench-a\n")
        settings.load(self.path)
        self.write("OPENAI_API_KEY=sk-two-0123456789\nTYPESAFE_API_KEY=ts-file-0123456789\n")   # edited, removed, added by hand
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-one-0123456789")                      # until it is read again
        done = settings.refresh(self.path)
        self.assertEqual(done, {"loaded": [], "updated": ["OPENAI_API_KEY"], "removed": ["HOSTS"]})   # TYPESAFE_API_KEY: the environment's
        self.assertNotIn("sk-two", repr(done))                                                   # names, never values
        by = {s["name"]: s for s in settings.states(self.path)}
        self.assertEqual((by["OPENAI_API_KEY"]["source"], os.environ["OPENAI_API_KEY"]), ("file", "sk-two-0123456789"))
        self.assertEqual(by["HOSTS"]["source"], "unset"); self.assertNotIn("HOSTS", os.environ)
        self.assertEqual((by["TYPESAFE_API_KEY"]["source"], os.environ["TYPESAFE_API_KEY"]), ("environment", "ts-env-0123456789"))
        self.assertTrue(by["TYPESAFE_API_KEY"]["shadowed"])

    def test_a_key_taken_out_of_the_environment_is_not_put_back(self):
        self.write("HOSTS=bench\n")
        settings.load(self.path)
        os.environ.pop("HOSTS")
        settings.refresh(self.path)
        self.assertNotIn("HOSTS", os.environ)

    def test_a_refresh_of_one_file_leaves_another_files_keys(self):
        other = os.path.join(self.tmp, "other.env")
        with open(other, "w") as fh:
            fh.write("HOSTS=bench-other\n")
        settings.load(other)
        self.write("OPENAI_API_KEY=sk-this-0123456789\n")
        settings.refresh(self.path)
        self.assertEqual((os.environ["HOSTS"], os.environ["OPENAI_API_KEY"]), ("bench-other", "sk-this-0123456789"))

    def test_named_endpoints_are_settings_of_their_own_and_the_environment_wins(self):
        """MODEL_<ID>_NAME, _API_URL and _API_KEY: validated as the default
        endpoint's are, a key shown by presence, read by nothing else, and a
        name no agent key may take. The environment still wins over the file."""
        self.write("MODEL_API_URL=http://default/v1\nMODEL_GPU_NAME=GPU box\nMODEL_GPU_API_URL=http://gpu-file/v1\nMODEL_GPU_API_KEY=sk-gpu-0123456789\n")
        os.environ["MODEL_GPU_API_URL"] = "http://gpu-env/v1"
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in settings.endpoint_names()])
        settings.load(self.path)
        self.assertEqual(settings.endpoint_ids(), ["GPU"])
        self.assertEqual(os.environ["MODEL_GPU_API_URL"], "http://gpu-env/v1")                    # the process environment wins
        by = {s["name"]: s for s in settings.states(self.path)}
        self.assertEqual((by["MODEL_GPU_NAME"]["shown"], by["MODEL_GPU_API_KEY"]["shown"]), ("GPU box", "********"))
        self.assertEqual(by["MODEL_GPU_API_URL"]["source"], "environment"); self.assertTrue(by["MODEL_GPU_API_URL"]["shadowed"])
        self.assertEqual(settings.unread(self.path), [])
        settings.save_many(self.path, {"MODEL_GPU_NAME": "GPU", "MODEL_LAPTOP_API_URL": "http://laptop/v1"})
        self.assertEqual(settings.endpoint_ids(), ["GPU", "LAPTOP"]); self.assertEqual(os.environ["MODEL_GPU_NAME"], "GPU")
        for bad in ({"MODEL_GPU_API_KEY": "has space"}, {"MODEL_gpu_API_URL": "http://x/v1"}, {"MODEL_GPU_NAME": "a\x01b"}):
            with self.assertRaises(ValueError):
                settings.save_many(self.path, bad)
        self.assertTrue(settings.reserved_name("MODEL_GPU_API_KEY")); self.assertIsNotNone(settings._agent_key_names("MODEL_X_API_KEY"))
        self.assertEqual(settings.new_endpoint_id("GPU box #2", {"GPU_BOX_2"}), "GPU_BOX_2_2")
        self.assertEqual(settings.new_endpoint_id("Ollama", set()), "OLLAMA"); self.assertEqual(settings.new_endpoint_id("éé", set()), "ENDPOINT")
        self.assertTrue(settings.ENDPOINT_ID.match(settings.new_endpoint_id("x" * 80, set())))
        settings.save_many(self.path, {"LLM_JUDGE_PROVIDER": "local:GPU"})
        with self.assertRaises(ValueError):
            settings.save_many(self.path, {"LLM_JUDGE_PROVIDER": "local:gpu"})

    def test_an_endpoint_url_is_shown_so_it_holds_no_credential(self):
        """An endpoint's URL is shown on the page: an http(s) URL with a host,
        without a user, a password or a key-like query parameter. One set
        before is shown with those parts hidden, said, and not offered."""
        ok = ("http://127.0.0.1:8000/v1", "https://llm.example/v1/", "http://[::1]:11434/v1", "http://gpu-box:8000/openai/v1",
              "https://llm.example/v1?api-version=2024-06-01", "https://llm.example/v1?model=qwen&keyboard=1")
        for url in ok:
            self.assertIsNone(settings.endpoint_url_problem(url), url)
            self.assertEqual(settings.endpoint_url_shown(url), url)
        for url in ("https://user:pw@llm.example/v1", "https://user@llm.example/v1"):
            self.assertIn("without a user or password in it: put the key in the Key field", settings.endpoint_url_problem(url), url)
        for name in ("key", "api_key", "apikey", "api-key", "token", "access_token", "access-token", "secret", "password",
                     "auth", "authorization", "sig", "signature", "API_KEY", "Token", "SIG"):
            url = f"https://llm.example/v1?model=m&{name}=abc"
            self.assertIn("without a key in its query: put the key in the Key field", settings.endpoint_url_problem(url), name)
        for url in ("ftp://llm.example/v1", "file:///etc/passwd", "localhost:8000/v1", "http:///v1", "http://h:99999/v1", "http://h /v1", "llm.example"):
            self.assertEqual(settings.endpoint_url_problem(url), "an http(s) URL with a host", url)
        self.assertEqual(settings.endpoint_url_shown("https://u:pw@llm.example/v1?model=m&api_key=abc"),
                         "https://[REDACTED]@llm.example/v1?model=m&api_key=[REDACTED]")
        with self.assertRaisesRegex(ValueError, "MODEL_API_URL must be a URL without a key in its query"):
            settings.save_many(self.path, {"MODEL_API_URL": "https://llm.example/v1?key=abc"})
        with self.assertRaises(ValueError):
            settings.save_many(self.path, {"MODEL_GPU_API_URL": "https://u:pw@gpu/v1"})
        self.assertFalse(os.path.exists(self.path))
        # one set in the environment before: hidden on the page, said, never offered to a run
        from nullius import providers
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in settings.endpoint_names()])
        os.environ["MODEL_GPU_API_URL"] = "https://u:hunter2@gpu/v1?token=abc"
        os.environ["MODEL_API_URL"] = "http://127.0.0.1:8000/v1"
        by = {s["name"]: s for s in settings.states(self.path)}
        self.assertEqual(by["MODEL_GPU_API_URL"]["shown"], "https://[REDACTED]@gpu/v1?token=[REDACTED]")
        self.assertIn("put the key in the Key field", by["MODEL_GPU_API_URL"]["problem"])
        self.assertEqual((by["MODEL_API_URL"]["shown"], by["MODEL_API_URL"]["problem"]), ("http://127.0.0.1:8000/v1", ""))
        self.assertEqual(by["MODEL_API_URL"]["source"], "environment")                                 # the environment still wins
        self.assertEqual([e["id"] for e in providers.endpoints()], [""]); self.assertEqual(providers.broken(), ["custom:GPU"])

    def test_keys_no_setting_reads_are_named_with_the_one_meant(self):
        self.write("API_TOKEN=sk-x\nAPI_URL=https://x/v1\nSOMETHING_ELSE=1\nRUNS_DIR=/tmp/r\nHOSTS=bench\nAPI_KEY_TYPESAFE=ts-x\n")
        self.assertEqual(settings.unread(self.path), [{"name": "API_TOKEN", "instead": "MODEL_API_KEY"},
                                                      {"name": "API_URL", "instead": "MODEL_API_URL"},
                                                      {"name": "SOMETHING_ELSE", "instead": ""},
                                                      {"name": "API_KEY_TYPESAFE", "instead": "TYPESAFE_API_KEY"}])   # a start-up option and a setting are read; the judge key's old name is not
        self.assertNotIn("API_KEY_TYPESAFE", settings.BY_NAME)                              # renamed, with no fallback to the old name

    def test_a_leftover_port_or_host_line_points_at_the_start_up_option(self):
        self.write("PORT=6000\nHOST=0.0.0.0\nAPP_PORT=6001\n")
        self.assertEqual(settings.unread(self.path), [{"name": "PORT", "instead": "APP_PORT"},
                                                      {"name": "HOST", "instead": "APP_HOST"}])     # APP_PORT is read, at start

    def test_save_creates_the_file_when_absent(self):
        settings.save(self.path, "HOSTS", "bench")
        self.assertEqual(Path(self.path).read_text(), "HOSTS=bench\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertIn("HOSTS", settings.LOADED_KEYS)


if __name__ == "__main__":
    unittest.main()
