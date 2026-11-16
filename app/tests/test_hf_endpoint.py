"""The Hugging Face endpoint (``HF_ENDPOINT``): read from the environment,
saved settings included, the environment first; in a driver's environment
from its start, so ``huggingface_hub`` reads it when first imported; a
huggingface.co dataset file fetched from it, checked by the same digest."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from nullius import settings
from nullius.standard import install, run


class Endpoint(unittest.TestCase):
    def test_unset_the_default_or_unusable_is_the_default(self):
        for env in ({}, {"HF_ENDPOINT": ""}, {"HF_ENDPOINT": "https://huggingface.co/"}, {"HF_ENDPOINT": "ftp://x"},
                    {"HF_ENDPOINT": "https://user:pw@mirror.example"}):
            self.assertEqual(settings.hf_endpoint(env), "", env)
        self.assertEqual(settings.hf_endpoint({"HF_ENDPOINT": "https://hf-mirror.com/"}), "https://hf-mirror.com")

    def test_the_environment_wins_over_the_saved_setting(self):
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w") as f:
            f.write('{"HF_ENDPOINT": "https://saved.example"}')
        self.addCleanup(settings.LOADED_KEYS.discard, "HF_ENDPOINT")
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://env.example"}):
            settings.load(path)
            self.assertEqual(settings.hf_endpoint(), "https://env.example")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HF_ENDPOINT", None)
            settings.load(path)
            self.assertEqual(settings.hf_endpoint(), "https://saved.example")

    def test_it_is_a_setting_of_its_own_never_withheld_from_a_run(self):
        from nullius import execution
        var = settings.BY_NAME["HF_ENDPOINT"]
        self.assertFalse(var.secret)
        self.assertIsNone(var.validate("https://hf-mirror.com"))
        self.assertIsNotNone(var.validate("https://user:pw@hf-mirror.com"))
        self.assertNotIn("HF_ENDPOINT", execution.withheld())


class DriverEnvironment(unittest.TestCase):
    def test_a_driver_is_started_with_the_endpoint(self):
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}):
            self.assertEqual(run.data_env(), {"HF_ENDPOINT": "https://hf-mirror.com"})
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HF_ENDPOINT", None)
            self.assertEqual(run.data_env(), {})

    def test_every_driver_environment_carries_it(self):
        """The local driver, the Original setup's driver in a container and a
        benchmark's MCP server on the machine: each environment takes data_env."""
        import inspect
        source = inspect.getsource(run.Run)
        self.assertEqual(source.count("**data_env()"), 3)


class DatasetDownload(unittest.TestCase):
    URL = "https://huggingface.co/datasets/o/n/resolve/abc/data.json"

    def test_a_huggingface_file_comes_from_the_endpoint(self):
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com/"}):
            self.assertEqual(install.hf_url(self.URL), "https://hf-mirror.com/datasets/o/n/resolve/abc/data.json")
            self.assertEqual(install.hf_url("https://example.org/data.json"), "https://example.org/data.json")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HF_ENDPOINT", None)
            self.assertEqual(install.hf_url(self.URL), self.URL)

    def test_the_download_is_kept_where_it_always_was(self):
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        fetched = []
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}), \
                mock.patch.object(install, "_fetch", lambda url, sha, where, what, progress=None: fetched.append((url, where))):
            install.download(self.URL, "0" * 64, home=tmp)
        self.assertEqual(fetched, [("https://hf-mirror.com/datasets/o/n/resolve/abc/data.json", install.download_path(self.URL, "0" * 64, tmp))])


if __name__ == "__main__":
    unittest.main()
