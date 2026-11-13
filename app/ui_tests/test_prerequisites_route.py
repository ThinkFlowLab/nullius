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

from nullius import execution, prerequisites, providers
from nullius.standard import hostreq
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

    def test_stella_memory_reaches_review_and_the_local_machine_check(self):
        rec = {"version": 1, "harness": "standard", "name": "stella", "title": "Stella", "benchmark": "longmemeval",
               "datasets": ["longmemeval_s"], "others": ["original"], "provider": "openai", "model": "gpt-4o",
               "limit": 1, "concurrency": 1, "where": "local", "bench_options": {"retriever": "flat-stella-session"}}
        why = "needs at least 12000 MiB of GPU memory; largest GPU has 11264 MiB"
        with patch("shutil.which", return_value="/fixture/program"), patch.object(hostreq, "gpu_memory_mb", return_value=11264):
            j = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
            self.assertEqual(j["errors"], [])
            self.assertFalse(j["ready"])
            row = next(r for r in j["machines"][0]["rows"] if r["name"] == "setup:stella_gpu")
            self.assertEqual((row["state"], row["detail"]), ("missing", why))
            response = self.client.post("/new-run/host/check", data={"entry": "local", "benchmark": "longmemeval",
                                        "requirements": "stella_gpu"}, headers=self.headers)
            self.addCleanup(response.close)
            self.assertEqual(response.get_json()["missing"], {"stella_gpu": [why]})
        with patch("shutil.which", return_value="/fixture/program"), patch.object(hostreq, "gpu_memory_mb", return_value=12000):
            self.assertTrue(self.post("/new-run/prerequisites", {"recipe": rec}).get_json()["ready"])


    def test_a_docker_with_userns_remap_is_said_for_a_standard_recipe_alone(self):
        rec = {"version": 1, "harness": "standard", "name": "s", "title": "s", "benchmark": "tau3-bench", "datasets": ["airline"],
               "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": "local", "backend": "container"}
        with patch.object(prerequisites, "docker_here", lambda fresh=False: None), patch("shutil.which", lambda p: "/x/" + p), \
                patch.object(prerequisites.container, "userns_remap", lambda: True):
            standard = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
            harbor = self.post("/new-run/prerequisites", {"recipe": self.harbor()}).get_json()
        engine = lambda j, n: {r["name"]: r for r in j["machines"][0]["rows"]}[n]
        self.assertEqual((engine(standard, "engine")["state"], engine(standard, "engine")["detail"]), ("ok", prerequisites.REMAPPED))
        self.assertTrue(standard["ready"])                            # a notice, not a refusal
        self.assertEqual(engine(harbor, "docker")["detail"], "the engine answers")

    def test_a_constraint_the_engine_does_not_enforce_is_refused_on_the_container_backend(self):
        from nullius import constraints
        self.stack.enter_context(patch.object(constraints, "store_path", lambda env=None: str(self.tmp / "capacity.json")))
        constraints.save("local", {"capacity": {"cpus": 4}, "engine": {"kind": "podman", "version": "5.8.9"}, "enforced": ["memory_mb"]})
        self.stack.enter_context(patch.dict(os.environ, {"HOST_CONSTRAINTS": '{"local": {"cpus": 2}}'}))
        for backend in ("container", "docker"):                      # docker: the container backend's earlier name
            rec = {"version": 1, "harness": "standard", "name": "s", "title": "s", "benchmark": "tau3-bench", "datasets": ["airline"],
                   "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": "local", "backend": backend}
            with patch.object(prerequisites, "docker_here", lambda fresh=False: None), patch("shutil.which", lambda p: "/x/" + p):
                j = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
            row = {r["name"]: r for r in j["machines"][0]["rows"]}["constraints"]
            self.assertEqual((row["state"], j["ready"]), ("missing", False), backend)
            self.assertIn("does not enforce CPUs", row["detail"])

    def test_hosts_are_probed_at_once_and_listed_in_order(self):
        import time
        names = [f"bench{i}" for i in range(4)]
        hosts = [SimpleNamespace(entry=n, label=n) for n in names]
        rec = {"version": 1, "harness": "standard", "name": "s", "title": "s", "benchmark": "tau3-bench", "datasets": ["airline"],
               "others": ["nop"], "provider": "openai", "model": "", "limit": 1, "concurrency": 1, "where": names}

        def slow(host, needs, setups=None, **kw):
            time.sleep(0.4)
            return [prerequisites.row("connect", "ok", host.label, label="Connection")]
        with patch.object(web.execute, "_hosts", lambda: hosts), patch.object(prerequisites, "check_host", slow):
            t0 = time.monotonic()
            j = self.post("/new-run/prerequisites", {"recipe": rec}).get_json()
            took = time.monotonic() - t0
        self.assertEqual([m["where"] for m in j["machines"]], names)
        self.assertLess(took, 1.2)                                    # not 4 x 0.4 s

    def test_review_notes_a_provider_whose_last_test_failed_and_still_runs(self):
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp)}))
        failed = {"at": "2026-11-12T10:00:00Z", "ok": False, "reached": True, "key": "refused", "models": None, "ms": 5, "error": ""}
        providers.remember({"openai": failed, "openrouter": dict(failed, ok=True, key="accepted")})
        with patch.object(prerequisites, "docker_here", lambda fresh=False: None), patch("shutil.which", lambda p: "/x/" + p):
            notes = {p: self.post("/new-run/prerequisites", {"recipe": self.harbor(), "provider": p}).get_json() for p in ("openai", "openrouter", "custom")}
        self.assertEqual(notes["openai"]["notes"], ["OpenAI failed its last test in Settings: Key refused · 2026-11-12 10:00 UTC"])
        self.assertTrue(notes["openai"]["ready"])
        self.assertEqual((notes["openrouter"]["notes"], notes["custom"]["notes"]), ([], []))


if __name__ == "__main__":
    unittest.main()
