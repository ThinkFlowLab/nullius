"""User settings survive changes of cwd/install; secrets stay masked and private."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nullius import paths, settings


class Locations(unittest.TestCase):
    def test_profile_overrides_xdg_and_never_creates_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NULLIUS_HOME": tmp + "/profile", "XDG_CONFIG_HOME": tmp + "/xdg"}
            self.assertEqual(paths.settings_file(env), tmp + "/profile/config/settings.json")
            self.assertEqual(paths.catalogue_dir(env), tmp + "/profile/data/catalogue")
            self.assertFalse(Path(tmp, "profile").exists())

    def test_xdg_and_explicit_storage_overrides(self):
        self.assertEqual(paths.config_dir({"XDG_CONFIG_HOME": "/tmp/config"}), "/tmp/config/nullius")
        self.assertEqual(paths.runs_dir({"RUNS_DIR": "/tmp/jobs"}), "/tmp/jobs")
        self.assertEqual(paths.secrets_dir({"SECRETS_DIR": "/tmp/keys"}), "/tmp/keys")
        self.assertEqual(paths.catalogue_dir({"NULLIUS_CATALOGUE": "/tmp/catalogue"}), "/tmp/catalogue")
        self.assertEqual(paths.config_dir({"XDG_CONFIG_HOME": "relative"}), paths.config_dir({}))


class JsonSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = str(Path(self.tmp.name, "config", "settings.json"))
        for context in (patch.dict(os.environ, {}, clear=True), patch.object(settings, "LOADED_KEYS", set()),
                        patch.object(settings, "_FROM", {}), patch.object(settings, "_SEEN", {})):
            context.start(); self.addCleanup(context.stop)

    def test_save_reload_update_and_clear_preserve_secret_bytes_and_permissions(self):
        secret = 'fixture-key-with-"quotes"-and-=-signs'
        settings.save(self.file, "OPENAI_API_KEY", secret)
        self.assertEqual(json.loads(Path(self.file).read_text()), {"OPENAI_API_KEY": secret})
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(Path(self.file).parent).st_mode), 0o700)
        os.environ.pop("OPENAI_API_KEY")
        settings.LOADED_KEYS.clear()
        settings.load(self.file)
        self.assertEqual(os.environ["OPENAI_API_KEY"], secret)
        self.assertNotIn(secret, repr(settings.states(self.file)))
        settings.save(self.file, "MODEL_API_URL", "http://localhost:1234/v1")
        self.assertEqual(settings.in_file(self.file)["OPENAI_API_KEY"], secret)
        settings.save(self.file, "OPENAI_API_KEY", None)
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("OPENAI_API_KEY", settings.in_file(self.file))

    def test_environment_wins_and_bad_json_is_never_overwritten(self):
        os.environ["OPENAI_API_KEY"] = "fixture-environment-key"
        settings.save(self.file, "OPENAI_API_KEY", "fixture-saved-key")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "fixture-environment-key")
        Path(self.file).write_text('{broken "sensitive-value"')
        with self.assertRaises(ValueError) as said:
            settings.save(self.file, "MODEL_API_URL", "http://localhost:1234/v1")
        self.assertNotIn("sensitive-value", str(said.exception))
        self.assertEqual(Path(self.file).read_text(), '{broken "sensitive-value"')

    def test_external_edit_applies_without_restarting(self):
        settings.save(self.file, "OPENAI_API_KEY", "fixture-first-key")
        Path(self.file).write_text(json.dumps({"OPENAI_API_KEY": "fixture-next-key"}))
        settings.sync(self.file)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "fixture-next-key")

    def test_legacy_licence_take_works_in_json(self):
        Path(self.file).parent.mkdir()
        Path(self.file).write_text(json.dumps({"LEGACY_LICENSE": "fixture-licence", "HOSTS": "alias"}))
        settings.load(self.file)
        self.assertEqual(settings.take(self.file, "LEGACY_LICENSE"), "fixture-licence")
        self.assertEqual(settings.in_file(self.file), {"HOSTS": "alias"})
        self.assertNotIn("LEGACY_LICENSE", os.environ)
