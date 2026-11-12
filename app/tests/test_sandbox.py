"""Docker's state, without a daemon: the Docker Hub login Data providers uses
hands the token to docker over stdin and keeps nothing; the username is all
that is read back."""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from nullius import sandbox
from nullius.standard import container


def done(code=0, out="", err=""):
    return mock.Mock(returncode=code, stdout=out, stderr=err)


class Login(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"DOCKER_CONFIG": self.tmp}); patcher.start(); self.addCleanup(patcher.stop)
        self.enterContext(mock.patch.object(container, "DOCKER", "docker"))

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

    def test_podman_logs_in_to_docker_hub_by_name_and_says_who(self):
        self.enterContext(mock.patch.object(container, "DOCKER", "podman"))
        calls = []
        answer = lambda args, **kw: calls.append((args, kw.get("input"))) or done(out="someone\n" if "--get-login" in args else "")
        with mock.patch.object(container, "_docker", answer):
            self.assertEqual(sandbox.login("someone", "dckr_pat_synthetic"), {"ok": True, "user": "someone"})
            self.assertEqual(sandbox.logout(), {"ok": True})
        self.assertEqual(calls, [(["login", "--username", "someone", "--password-stdin", "docker.io"], "dckr_pat_synthetic\n"),
                                 (["login", "--get-login", "docker.io"], None), (["logout", "docker.io"], None)])
        with mock.patch.object(container, "_docker", side_effect=container.Unavailable("podman is not installed on this machine")):
            self.assertIsNone(sandbox.login_user())                              # the page still answers

    def test_the_engine_is_named_with_its_version(self):
        with mock.patch.object(container, "DOCKER", "podman"), mock.patch.object(container, "unusable", return_value=None), \
                mock.patch.object(container, "_docker", return_value=done(out="6.1.4\n")):
            self.assertEqual(sandbox.docker_state(), {"ok": True, "version": "podman 6.1.4"})

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


if __name__ == "__main__":
    unittest.main()
