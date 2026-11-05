"""The Review step's Prerequisites: what the recipe needs of the machines it
runs on, asked of this machine."""
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

from nullius import execution, prerequisites
from .test_routes import web

STOPPED = "docker is installed but not usable by this user: Cannot connect to the Docker daemon"


class PrerequisitesRoute(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-prereq-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"HOSTS": "", "RUNS_DIR": str(self.tmp / "runs"), "OPENAI_API_KEY": "sk-0123456789abcdef"}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.started = []
        self.stack.enter_context(patch.object(execution, "start", lambda plan: self.started.append(plan) or SimpleNamespace(
            jobs=str(self.tmp), summary=lambda: {"id": "x", "state": "preparing"})))
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: None))
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.headers = {"Origin": "http://localhost", "X-CSRF-Token": s["csrf"]}

    def post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json", headers=self.headers)
        self.addCleanup(r.close); return r

    def harbor(self, **kw):
        return dict({"harness": "harbor", "name": "h", "where": "local", "harbor": {"kind": "release", "version": "0.4.0"}}, **kw)

    def test_a_harbor_recipe_here_lists_docker_and_uv_and_waits_on_a_stopped_engine(self):
        with patch.object(prerequisites, "docker_here", lambda fresh=False: STOPPED), patch("shutil.which", lambda p: "/x/" + p):
            j = self.post("/new-run/prerequisites", {"recipe": self.harbor()}).get_json()
        rows = {r["name"]: r for r in j["machines"][0]["rows"]}
        self.assertEqual((rows["docker"]["state"], rows["docker"]["detail"]), ("missing", STOPPED))
        self.assertEqual(rows["uv"]["state"], "ok")
        self.assertFalse(j["ready"])
        with patch.object(prerequisites, "docker_here", lambda fresh=False: None), patch("shutil.which", lambda p: "/x/" + p):
            self.assertTrue(self.post("/new-run/prerequisites", {"recipe": self.harbor()}).get_json()["ready"])

    def test_a_standard_recipe_on_local_processes_needs_no_docker(self):
        rec = {"version": 1, "harness": "standard", "name": "s", "title": "s", "benchmark": "tau3-bench", "datasets": ["airline"],
               "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": "local"}
        with patch.object(prerequisites, "docker_here", lambda fresh=False: self.fail("docker asked")):
            j = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
        self.assertEqual((j["ready"], j["machines"][0]["rows"]), (True, []))


    def test_hosts_are_probed_at_once_and_listed_in_order(self):
        import time
        names = [f"bench{i}" for i in range(4)]
        hosts = [SimpleNamespace(entry=n, label=n) for n in names]
        rec = {"version": 1, "harness": "standard", "name": "s", "title": "s", "benchmark": "tau3-bench", "datasets": ["airline"],
               "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": names}

        def slow(host, needs, setups=None):
            time.sleep(0.4)
            return [prerequisites.row("connect", "ok", host.label, label="Connection")]
        with patch.object(web.execute, "_hosts", lambda: hosts), patch.object(prerequisites, "check_host", slow):
            t0 = time.monotonic()
            j = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
            took = time.monotonic() - t0
        self.assertEqual([m["where"] for m in j["machines"]], names)
        self.assertLess(took, 1.2)                                    # not 4 x 0.4 s


if __name__ == "__main__":
    unittest.main()
