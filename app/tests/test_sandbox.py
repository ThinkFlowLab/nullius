"""Docker's state, without a daemon: the Docker Hub login Data providers uses
hands the token to docker over stdin and keeps nothing; the username is all
that is read back; the images runs keep are counted and removed per benchmark,
only those Nullius fetched."""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from nullius import sandbox
from nullius.standard import container, install


def done(code=0, out="", err=""):
    return mock.Mock(returncode=code, stdout=out, stderr=err)


class Login(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"DOCKER_CONFIG": self.tmp}); patcher.start(); self.addCleanup(patcher.stop)

    def config(self, body):
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump(body, f)

    def test_the_token_goes_over_stdin_never_on_the_command_line(self):
        calls = []
        with mock.patch.object(container, "_docker", lambda args, **kw: calls.append((args, kw.get("input"))) or done()):
            got = sandbox.login(" Someone ", "dckr_pat_synthetic")
        self.assertEqual(got, {"ok": True, "user": "someone"})
        (args, given), = calls
        self.assertEqual(args, ["login", "--username", "someone", "--password-stdin"])
        self.assertEqual(given, "dckr_pat_synthetic\n")
        self.assertNotIn("dckr_pat_synthetic", " ".join(args))
        self.assertEqual(os.listdir(self.tmp), [])                          # nothing written by Nullius

    def test_a_bad_username_or_empty_token_asks_docker_nothing(self):
        with mock.patch.object(container, "_docker", lambda *a, **kw: self.fail("docker asked")):
            self.assertFalse(sandbox.login("", "t")["ok"])
            self.assertFalse(sandbox.login("a b", "t")["ok"])
            self.assertFalse(sandbox.login("someone", "")["ok"])

    def test_dockers_refusal_is_said(self):
        with mock.patch.object(container, "_docker", lambda *a, **kw: done(1, err="Error response from daemon: unauthorized: incorrect username or password\n")):
            got = sandbox.login("someone", "wrong")
        self.assertEqual(got, {"ok": False, "error": "Error response from daemon: unauthorized: incorrect username or password"})

    def test_only_the_username_is_read_back(self):
        self.assertIsNone(sandbox.login_user())
        self.config({"auths": {sandbox.REGISTRY: {"auth": base64.b64encode(b"someone:dckr_pat_secret").decode()}}})
        self.assertEqual(sandbox.login_user(), "someone")

    def test_a_credential_helper_is_asked_and_its_secret_dropped(self):
        self.config({"auths": {sandbox.REGISTRY: {}}, "credsStore": "desktop"})
        seen = []
        def run(argv, **kw):
            seen.append((argv, kw["input"]))
            return done(out=json.dumps({"ServerURL": sandbox.REGISTRY, "Username": "someone", "Secret": "dckr_pat_secret"}))
        with mock.patch("subprocess.run", run):
            self.assertEqual(sandbox.login_user(), "someone")
        self.assertEqual(seen, [(["docker-credential-desktop", "get"], sandbox.REGISTRY)])


class Images(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(install, "HOME", self.tmp); patcher.start(); self.addCleanup(patcher.stop)

    def test_a_pulled_image_is_remembered_with_its_benchmark(self):
        def docker(args, **kw):
            return done(1) if args[:2] == ["image", "inspect"] else done()
        self.addCleanup(container._fetched.discard, "img@sha256:1"); self.addCleanup(container._built.pop, "img@sha256:1", None)
        with mock.patch.object(container, "_docker", docker):
            container.own_image({"key": "bench-a"}, {"pull": "img@sha256:1"}, "/r")
        self.assertEqual(container.fetched_images(), {"img@sha256:1": "bench-a"})

    def test_counted_per_benchmark_and_one_removed_outside_is_forgotten(self):
        for ref, key in (("a1", "bench-a"), ("a2", "bench-a"), ("b1", "bench-b"), ("gone", "bench-b")):
            container.remember(ref, key)
        sizes = {"a1": "4000000000", "a2": "1000000000", "b1": "9000000000"}

        def docker(args, **kw):
            refs = args[4:]
            if any(r not in sizes for r in refs):
                return done(1, out="\n".join(sizes[r] for r in refs if r in sizes))
            return done(out="\n".join(sizes[r] for r in refs))
        with mock.patch.object(container, "_docker", docker), mock.patch.object(sandbox, "_label", lambda k: k.upper()):
            got = sandbox.images()
        self.assertEqual(got, [{"key": "bench-b", "label": "BENCH-B", "images": 1, "bytes": 9000000000},
                               {"key": "bench-a", "label": "BENCH-A", "images": 2, "bytes": 5000000000}])
        self.assertNotIn("gone", container.fetched_images())

    def test_remove_takes_one_benchmarks_images_and_names_one_still_in_use(self):
        for ref, key in (("a1", "bench-a"), ("a2", "bench-a"), ("b1", "bench-b")):
            container.remember(ref, key)
        asked = []

        def docker(args, **kw):
            asked.append(args)
            if args[0] == "rmi":
                return done(1, err="conflict: image is being used") if args[1] == "a2" else done()
            return done()                                        # still there
        with mock.patch.object(container, "_docker", docker):
            got = sandbox.remove("bench-a")
        self.assertEqual(got, {"ok": False, "removed": 1, "failed": ["a2"]})
        self.assertEqual([a for a in asked if a[0] == "rmi"], [["rmi", "a1"], ["rmi", "a2"]])
        self.assertEqual(container.fetched_images(), {"a2": "bench-a", "b1": "bench-b"})

    def test_nothing_fetched_asks_docker_nothing(self):
        with mock.patch.object(container, "_docker", lambda *a, **kw: self.fail("docker asked")):
            self.assertEqual(sandbox.images(), [])


if __name__ == "__main__":
    unittest.main()
