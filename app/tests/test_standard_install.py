"""Installs happen once, are checked, and two runs installing the same thing
at once take turns instead of undoing each other."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from nullius.standard import install


class UpstreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "data"))
        with open(os.path.join(self.repo, "data", "items.json"), "w") as f:
            f.write("[1, 2, 3]\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(git + ["-C", self.repo, "add", "."], check=True)
        subprocess.run(git + ["-C", self.repo, "commit", "-qm", "data"], check=True)
        self.commit = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.bench = {"key": "b", "_here": self.tmp, "upstream": {"repository": self.repo, "tested": self.commit}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_runs_installing_at_once_both_get_the_whole_clone(self):
        home, got, errors = os.path.join(self.tmp, "home"), [], []

        def go():
            try:
                got.append(install.upstream(self.bench, home))
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(got)), 1)
        self.assertTrue(os.path.isfile(os.path.join(got[0], "data", "items.json")))

    def test_a_dataset_is_checked_by_its_digest(self):
        up = install.upstream(self.bench, os.path.join(self.tmp, "home"))
        right = install.sha256_of(os.path.join(up, "data", "items.json"))
        self.assertTrue(install.dataset(self.bench, {"id": "d", "path": "data/items.json", "sha256": right}, up))
        with self.assertRaisesRegex(install.InstallFailed, "sha256"):
            install.dataset(self.bench, {"id": "d", "path": "data/items.json", "sha256": "0" * 64}, up)

    def test_slots_stay_inside_their_word(self):
        self.assertEqual(install.words(["{here}/a b.py", "--x={install}"], {"here": "/p q", "install": "/i"}),
                         ["/p q/a b.py", "--x=/i"])



class InstallEnv(unittest.TestCase):
    def test_a_process_gets_its_own_variables_and_the_prefetched_models(self):
        d = {"_here": "/def", "install": {"prefetch": ["a/b"]}, "env": {"HF_HUB_OFFLINE": "1", "WHERE": "{install}/x:{home}"}}
        self.assertEqual(install.process_env(d, "/inst", "/home"), {"HF_HUB_CACHE": "/inst/hf", "HF_HUB_OFFLINE": "1", "WHERE": "/inst/x:/home"})

    def test_a_downloaded_dataset_is_kept_only_when_its_digest_matches(self):
        import hashlib, http.server
        body = b"[1, 2]"
        class One(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), One)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        home = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, home, True)
        url = f"http://127.0.0.1:{srv.server_address[1]}/d.json"
        got = install.download(url, hashlib.sha256(body).hexdigest(), home)
        self.assertEqual(open(got, "rb").read(), body)
        with self.assertRaises(install.InstallFailed):
            install.download(url, "0" * 64, home)
        self.assertFalse(os.path.exists(os.path.join(home, "datasets", "0" * 64, "d.json")))

    def test_an_install_step_is_never_given_a_key(self):
        env = {"PATH": "/bin", "HOME": "/h", "OPENAI_API_KEY": "sk-x", "API_TOKEN": "sk-x", "LLM_API_KEY": "sk-x", "GH_TOKEN": "t", "npm_config_cache": "/c",
               "GIT_CONFIG_KEY_0": "safe.directory"}
        self.assertEqual(install.install_env(env), {"PATH": "/bin", "HOME": "/h", "npm_config_cache": "/c", "GIT_CONFIG_KEY_0": "safe.directory"})


class DataDirTest(unittest.TestCase):
    def test_datasets_go_where_standard_data_dir_says_else_into_the_home(self):
        self.assertEqual(install.data_dir("/st", env={}), "/st/datasets")
        self.assertEqual(install.data_dir("/st", env={"STANDARD_DATA_DIR": "/big/data"}), "/big/data")
        self.assertEqual(install.data_dir("/st", env={"STANDARD_DATA_DIR": ""}), "/st/datasets")

    def test_a_download_lands_in_the_data_dir_not_the_home(self):
        import hashlib, http.server
        from unittest import mock
        body = b"[3]"
        class One(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), One)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        digest = hashlib.sha256(body).hexdigest()
        with mock.patch.dict(os.environ, {"STANDARD_DATA_DIR": os.path.join(tmp, "big")}):
            got = install.download(f"http://127.0.0.1:{srv.server_address[1]}/d.json", digest, os.path.join(tmp, "home"))
        self.assertEqual(got, os.path.join(tmp, "big", digest, "d.json"))
        self.assertFalse(os.path.exists(os.path.join(tmp, "home")))


class PruneTest(unittest.TestCase):
    """A definition's caches leave the unit's home when it ends, and nothing
    outside the home ever does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "trial", ".home")
        for p in (".codex/.tmp/plugins/x", ".codex/sessions", ".npm/_cacache"):
            os.makedirs(os.path.join(self.home, p))
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.outside)
        open(os.path.join(self.outside, "keep"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_declared_caches_go_and_the_rest_of_the_home_stays(self):
        gone = install.prune({"caches": ["{home}/.codex/.tmp", "{home}/.npm", "{home}/not-made"]}, self.home)
        self.assertEqual(len(gone), 2)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", ".tmp")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".npm")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".codex", "sessions")))

    def test_a_path_out_of_the_home_is_left_alone(self):
        os.symlink(self.outside, os.path.join(self.home, "link"))
        install.prune({"caches": ["{home}/../../outside", "{home}/link", "/tmp", "{install}/x"]}, self.home)
        self.assertTrue(os.path.isfile(os.path.join(self.outside, "keep")))

    def test_no_caches_removes_nothing(self):
        self.assertEqual(install.prune({}, self.home), [])
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".npm")))


class NpmElsewhereTest(unittest.TestCase):
    """A host that will only start an agent in a container need not have Node:
    on the docker backend npm runs in the base image (``npm_in``); without
    that, a machine with no npm is refused with a sentence, not a traceback."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.agent = {"key": "a", "_kind": "agent", "install": {"npm": ["pkg@1.0.0"]}}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_no_npm_and_no_container_is_refused_said(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            with self.assertRaisesRegex(install.InstallFailed, "npm is not on this machine.*docker backend"):
                install.packages(self.agent, self.home)

    def test_no_npm_runs_it_where_npm_in_says_and_leaves_no_npm_scratch(self):
        seen = []

        def npm_in(cmd, where):
            seen.append((cmd, where))
            for scratch in (".npm-home", ".npm-cache"):          # what npm leaves in the container
                os.makedirs(os.path.join(where, scratch), exist_ok=True)
            return ["true"]
        with mock.patch.object(install.shutil, "which", return_value=None):
            where = install.packages(self.agent, self.home, npm_in=npm_in)
        self.assertEqual(seen[0][0][:2], ["npm", "install"])
        self.assertEqual(seen[0][0][-1], "pkg@1.0.0")
        self.assertEqual(seen[0][1], where)
        self.assertTrue(os.path.exists(os.path.join(where, install.MARK)))
        self.assertFalse(os.path.exists(os.path.join(where, ".npm-home")))
        self.assertFalse(os.path.exists(os.path.join(where, ".npm-cache")))

    def test_a_program_missing_is_an_install_failure(self):
        with self.assertRaisesRegex(install.InstallFailed, "no-such-program-here is not on this machine"):
            install._run(["no-such-program-here"], self.home, os.path.join(self.home, "x.log"))



class WheelsTest(unittest.TestCase):
    """A wheel served by URL only (a model package) is downloaded into the
    install, kept only when its sha256 matches, and resolved with pip's pins."""

    def setUp(self):
        import hashlib, http.server
        self.body = b"PK not really a wheel"
        body = self.body
        class One(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), One)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.home = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.home, True)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}/m/model-1.0-py3-none-any.whl"
        self.sha = hashlib.sha256(body).hexdigest()

    def capability(self, sha):
        return {"key": "c", "_kind": "capability", "install": {"pip": ["lib==1.0"], "wheels": [{"url": self.url, "sha256": sha}]}}

    def test_a_wheel_is_installed_with_the_pins_once_its_digest_matches(self):
        runs = []
        with mock.patch.object(install, "_run", side_effect=lambda cmd, *a, **k: runs.append(cmd)):
            where = install.packages(self.capability(self.sha), self.home)
        wheel = os.path.join(where, install.WHEELS, "model-1.0-py3-none-any.whl")
        self.assertEqual(open(wheel, "rb").read(), self.body)
        self.assertEqual(runs[-1][-2:], ["lib==1.0", wheel])          # one resolution: the wheel's dependencies are pip's

    def test_a_wheel_whose_digest_differs_is_refused_and_not_kept(self):
        with mock.patch.object(install, "_run"):
            with self.assertRaisesRegex(install.InstallFailed, "wheel .*sha256"):
                install.packages(self.capability("0" * 64), self.home)
        where = install.packages_dir(self.capability("0" * 64), self.home)
        self.assertFalse(os.path.exists(os.path.join(where, install.WHEELS, "model-1.0-py3-none-any.whl")))
        self.assertFalse(os.path.exists(os.path.join(where, install.MARK)))

if __name__ == "__main__":
    unittest.main()
