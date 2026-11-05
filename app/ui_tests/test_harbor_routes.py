"""The Harbor a recipe pins, from the builder: PyPI's releases and a
repository's refs read after the page loads, the agents of a pin asked on an
explicit scan (a POST with the form token, from this machine) and read back
on a GET, and the pin travelling with the run. Fake upstreams only: an
injected PyPI, bare git repositories on disk, uv offline."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib import error

from nullius import execution, gitrefs
from nullius.harbor import source
from tests import fake_harbor
from .test_routes import web

HAVE_UV = bool(shutil.which("uv") and shutil.which("uvx"))


class HarborRoutes(unittest.TestCase):
    def setUp(self):
        from web import builder as builder_web
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-harbor-route-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"HOSTS": "", "RUNS_DIR": str(self.tmp / "runs"), "HARBOR_PINS": str(self.tmp / "pins"),
                                                         "OPENROUTER_API_KEY": "sk-or-0123456789abcdef", "UV_OFFLINE": "1"}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.dict(builder_web._HARBOR_RELEASES, clear=True))
        self.stack.enter_context(patch.dict(builder_web._HARBOR_REFS, clear=True))
        self.started = []
        def start(plan):
            self.started.append(plan)
            return SimpleNamespace(jobs=str(self.tmp / "runs" / plan.name / "jobs"), summary=lambda: {"id": plan.name + "-1", "state": "preparing"})
        self.stack.enter_context(patch.object(execution, "start", start))
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: None))
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]
        self.headers = {"Origin": "http://localhost", "X-CSRF-Token": self.csrf}

    def get(self, url, **kw):
        r = self.client.get(url, **kw); self.addCleanup(r.close); return r

    def post(self, url, body, **kw):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json", headers=kw.pop("headers", self.headers), **kw)
        self.addCleanup(r.close); return r

    def test_the_releases_are_read_after_the_page_and_a_failed_read_is_asked_again(self):
        answers = [error.URLError("no route"), {"releases": {"0.24.0": [{"filename": "harbor-0.24.0-py3-none-any.whl", "requires_python": ">=3.12"}],
                                                             "0.9.0": [{"filename": "harbor-0.9.0-py3-none-any.whl"}]}}]
        asked = []
        def fetch(url, timeout=20.0):
            asked.append(url); a = answers[len(asked) - 1]
            if isinstance(a, Exception):
                raise a
            return json.dumps(a).encode()
        self.stack.enter_context(patch.object(source, "pypi_fetch", fetch))
        self.assertEqual(self.get("/new-run/harbor/releases").json, {"versions": [], "error": "PyPI could not be reached: no route"})
        got = self.get("/new-run/harbor/releases").json
        self.assertEqual([v["version"] for v in got["versions"]], ["0.24.0", "0.9.0"])
        self.assertEqual(self.get("/new-run/harbor/releases").json, got); self.assertEqual(len(asked), 2)   # kept once read

    def test_a_repository_s_refs_are_listed_with_this_machine_s_git(self):
        up = fake_harbor.upstream(str(self.tmp))
        calls = []
        real = gitrefs.ls_remote
        self.stack.enter_context(patch.object(gitrefs, "ls_remote", lambda url, **kw: calls.append(url) or real(url, **kw)))
        got = self.get("/new-run/harbor/refs?repo=" + up["url"]).json
        self.assertEqual((got["error"], got["default_branch"], got["label"]), ("", "main", up["url"]))
        self.assertEqual([(t["name"], t["commit"]) for t in got["tags"]], [("v0.10", up["v010"]), ("v0.9", up["v09"])])
        self.get("/new-run/harbor/refs?repo=" + up["url"]); self.assertEqual(len(calls), 1)               # kept
        self.get("/new-run/harbor/refs?fresh=1&repo=" + up["url"]); self.assertEqual(len(calls), 2)      # a branch moves: asked again on request
        missing = self.get("/new-run/harbor/refs?repo=file://" + str(self.tmp / "none.git"))
        self.assertEqual(missing.status_code, 200); self.assertTrue(missing.json["error"].startswith("could not list refs: ")); self.assertTrue(missing.json["error"].endswith("; type a commit"))
        self.assertEqual(self.get("/new-run/harbor/refs?repo=--upload-pack%3Dx").status_code, 400)
        self.assertEqual(self.get("/new-run/harbor/refs?repo=" + up["url"], environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)

    def test_a_get_reads_what_a_scan_kept_and_only_a_post_from_here_builds(self):
        self.stack.enter_context(patch.object(source, "pypi_fetch", lambda url, timeout=20.0: json.dumps({"info": {"requires_python": ">=3.12", "provides_extra": ["cloud"]}}).encode()))
        ran = []
        self.stack.enter_context(patch.object(source, "run_command", lambda argv, timeout=600.0, stop=None: ran.append(argv) or (0, '["nop", "oracle", "claude-code"]')))
        q = "/new-run/harbor/agents?kind=release&version=0.24.0"
        self.assertEqual((self.get(q).json["state"], ran), ("absent", []))                                # a page load never builds
        pin = {"kind": "release", "version": "0.24.0", "extras": []}
        self.assertEqual(self.post("/new-run/harbor/agents", {"harbor": pin}, headers={"Origin": "http://localhost"}).status_code, 403)   # no token
        self.assertEqual(self.post("/new-run/harbor/agents", {"harbor": pin}, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)
        got = self.post("/new-run/harbor/agents", {"harbor": pin}).json
        self.assertEqual((got["state"], got["agents"], got["extras"], got["key"]), ("ready", ["claude-code", "nop", "oracle"], ["cloud"], "release-0.24.0"))
        self.assertEqual(len(ran), 1)
        self.assertEqual(self.get(q).json["agents"], ["claude-code", "nop", "oracle"]); self.assertEqual(len(ran), 1)   # read back
        self.assertTrue((self.tmp / "pins" / "release-0.24.0" / "agents.json").is_file())
        self.assertEqual(self.get("/new-run/harbor/agents?kind=release&version=1;x").status_code, 400)

    @unittest.skipUnless(HAVE_UV, "uv is not installed here")
    def test_a_fork_is_built_here_and_scanned_from_the_page(self):
        up = fake_harbor.upstream(str(self.tmp))
        pin = {"kind": "repo", "repo": up["url"], "revisionKind": "branch", "revision": "main", "commit": up["commit"], "extras": []}
        got = self.post("/new-run/harbor/agents", {"harbor": pin}).json
        self.assertEqual(got["state"], "ready", got.get("error"))
        self.assertEqual((got["agents"], got["version"]), (["claude-code", "nop", "oracle", "terminus-2"], "0.22.0+fork.1"))
        self.assertTrue((self.tmp / "pins" / ("git-" + up["commit"]) / "wheels").is_dir())

    def test_the_pin_travels_with_the_run(self):
        script = '"$NULLIUS_HARBOR" run --agent nop'
        pin = {"kind": "repo", "repo": "owner/harbor", "revisionKind": "tag", "revision": "v1", "commit": "c" * 40, "extras": ["jw"]}
        r = self.post("/run", {"name": "pinned-0101", "where": "local", "script": script, "recipe": {"version": 1, "harness": "harbor", "harbor": pin}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.started[-1].harbor_pin, dict(pin, url="https://github.com/owner/harbor.git"))
        bad = self.post("/run", {"name": "pinned-0102", "where": "local", "script": script, "recipe": {"version": 1, "harness": "harbor", "harbor": dict(pin, commit="main")}})
        self.assertEqual(bad.status_code, 400); self.assertIn("the pin is a commit", bad.json["error"])
        self.post("/run", {"name": "pinned-0103", "where": "local", "script": script, "recipe": {"version": 1, "harness": "harbor"}})
        self.assertEqual(self.started[-1].harbor_pin, {})                                               # a recipe from before the pin: harbor on PATH
