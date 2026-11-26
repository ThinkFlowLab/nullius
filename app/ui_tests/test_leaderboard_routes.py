"""Leaderboard pages, CLI parity, scoring writes and deferred response isolation."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .test_routes import web
from tests.test_comparison import job, trial
from nullius import cli
from nullius.runs import parse
from nullius.leaderboard import configuration


class LeaderboardRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, NULLIUS_HOME=self.folder))
        self.stack.enter_context(patch.dict(web.shared.STATE, workspace=None, roots=[], job_roots=[], label=""))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.jobs = []
        self.stack.enter_context(patch.object(web.shared, "_load_jobs", side_effect=lambda: self.jobs))
        self.client = web.app.test_client()

    def test_empty_pages_keep_catalogue_axes_and_four_blocks(self):
        response = self.client.get("/leaderboard")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("No workspace open", html)
        for name in configuration.AXES:
            self.assertIn(name, html)
        positions = [html.index(f'id="{key}"') for key in ("leaderboard-table", "leaderboard-efficiency", "leaderboard-strips", "leaderboard-radars")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("OpenAI", html)
        self.assertIn("2.2.2", html)
        for route in ("/scoring", "/settings/scoring"):
            response = self.client.get(route, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Scoring settings", response.get_data(as_text=True))

    def test_api_and_cli_use_same_profile_scores_without_eager_trace_loads(self):
        run = job([trial("a"), trial("b", "agent_error")], dataset="gaia/gaia-validation", agent="codex")
        from nullius.standard.contract import definitions
        from nullius.standard.runner import job as recorded_job
        run.identity = parse.identity_of({"standard": {"benchmark": "gaia", "plugged": "codex", **recorded_job._version(definitions.get("benchmark", "gaia"))}, "agents": [{"name": "codex", "model_name": "model"}]}, {}, "2.2.2")
        self.jobs.append(run)
        with patch("nullius.traces.trajectory.load") as load:
            response = self.client.get("/api/leaderboard")
            self.assertEqual(response.status_code, 200)
            document = response.get_json()
            load.assert_not_called()
        row = next(r for r in document["rows"] if r["has_runs"])
        self.assertEqual(row["axes"]["Retrieval"]["score"], .5)
        gaia = next(e for e in row["axes"]["Retrieval"]["entries"] if e.get("benchmark") == "gaia")
        self.assertEqual((len(gaia["version"]), gaia["version_label"]), (40, gaia["version"][:7]))   # a pin reads short
        output = io.StringIO()
        with patch.object(parse, "load_all", return_value=self.jobs), redirect_stdout(output):
            self.assertEqual(cli.main(["leaderboard", self.folder, "--json"]), 0)
        command = json.loads(output.getvalue())
        self.assertEqual(command["rows"], document["rows"])
        response = self.client.get("/api/leaderboard/metrics", query_string={"context": "old-workspace"})
        self.assertEqual(response.status_code, 409)

    def test_runs_of_one_name_in_two_folders_stay_apart_on_the_page_as_in_the_command(self):
        from dataclasses import replace
        from nullius.standard.contract import definitions
        for folder, benchmark, dataset, cost in (("/one", "gaia", "gaia/gaia-validation", 1.0), ("/two", "tau3-bench", "tau3-bench/banking_knowledge", 3.0)):
            run = replace(job([trial("0", cost_usd=cost)], dataset=dataset, agent="codex", job_id="same"), dir=folder + "/same")
            run.identity = {"agent": "codex", "agent_version": "2.2.2", "benchmark": benchmark,
                            "benchmark_version": definitions.version_identity(definitions.get("benchmark", benchmark))}
            self.jobs.append(run)
        row = next(r for r in self.client.get("/api/leaderboard").get_json()["rows"] if r["has_runs"])
        self.assertEqual(row["metrics"]["cost"], {"value": 2.0, "count": 2})          # both trials, each once
        links = [link for e in row["axes"]["Retrieval"]["entries"] for link in e["links"]]
        self.assertEqual([(link["id"], link["url"]) for link in links], [("/one/same", "/job/same"), ("/two/same", "/job/same")])

    def test_new_unknown_dataset_is_rejected_but_saved_removed_part_can_be_edited(self):
        self.client.get("/scoring")
        with self.client.session_transaction() as session:
            headers = {"X-CSRF-Token": session["csrf"], "Origin": "http://localhost"}
        doc = self.client.get("/api/scoring").get_json()["settings"]
        self.assertEqual(self.client.post("/api/scoring", json=doc, headers=headers).status_code, 200)
        before = configuration.location().read_bytes()
        pinned = doc["axes"]["Retrieval"]["entries"][0]["version"]
        doc["axes"]["Retrieval"]["entries"][0]["version"] = "invented-version"
        self.assertEqual(self.client.post("/api/scoring", json=doc, headers=headers).status_code, 400)
        self.assertEqual(configuration.location().read_bytes(), before)
        doc["axes"]["Retrieval"]["entries"][0]["version"] = pinned
        doc["axes"]["Retrieval"]["entries"][0]["dataset"] = "not-a-real-dataset"
        self.assertEqual(self.client.post("/api/scoring", json=doc, headers=headers).status_code, 400)
        self.assertEqual(configuration.location().read_bytes(), before)
        configuration.location().write_text(json.dumps(doc))  # a reference removed from the catalogue
        doc["axes"]["Money"]["weight"] = 1
        self.assertEqual(self.client.post("/api/scoring", json=doc, headers=headers).status_code, 200)
        doc["axes"]["Retrieval"]["entries"][0]["dataset"] = "another-invention"
        self.assertEqual(self.client.post("/api/scoring", json=doc, headers=headers).status_code, 400)

    def test_osworld_parts_read_only_matching_task_names(self):
        # task names as a run records them: <benchmark>/<unit>
        self.jobs.append(job([trial("osworld1/os__one", "scored", score=1), trial("osworld1/libreoffice_writer__two", "scored", score=.5),
                              trial("osworld1/chrome__three", "scored", score=0)], dataset="osworld1/test_nogdrive", agent="codex"))
        self.jobs[0].identity = {"benchmark": "osworld1", "benchmark_version": "desktop-env-v1.0.3", "agent": "codex", "agent_version": "2.2.2"}
        from nullius.leaderboard import service
        benchmarks = service.catalogue()[1]
        settings = configuration.defaults(benchmarks)
        self.assertEqual(settings["axes"]["Home"]["entries"], [])          # starts with no benchmark
        version = next(e["version"] for e in settings["axes"]["Execution"]["entries"] if e["benchmark"] == "osworld1")
        office = {"benchmark": "osworld1", "dataset": "office", "version": version, "metric": "Score", "weight": 1}
        settings["axes"]["Work"]["entries"] = [office]
        settings["axes"]["Home"]["entries"] = [dict(office, dataset="os")]
        configuration.save(configuration.location(), settings, benchmarks)
        doc = self.client.get("/api/leaderboard").get_json()
        row = next(r for r in doc["rows"] if r["has_runs"])
        self.assertEqual(row["axes"]["Home"]["score"], 1)
        self.assertEqual(row["axes"]["Work"]["score"], .5)
        choices = next(b for b in service.options(service.catalogue()[1]) if b["key"] == "osworld1")["datasets"]
        self.assertIn({"id": "os", "label": "OS tasks"}, choices)
        self.assertIn({"id": "office", "label": "Office tasks"}, choices)

    def test_scoring_validates_atomic_documents_and_protects_writes(self):
        self.client.get("/scoring")
        with self.client.session_transaction() as session:
            token = session["csrf"]
        document = self.client.get("/api/scoring").get_json()["settings"]
        headers = {"X-CSRF-Token": token, "Origin": "http://localhost"}
        document["axes"]["Retrieval"]["entries"][0]["weight"] = 3
        response = self.client.post("/api/scoring", json=document, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Scoring saved")
        before = configuration.location().read_bytes()
        document["axes"]["Retrieval"]["weight"] = -1
        self.assertEqual(self.client.post("/api/scoring", json=document, headers=headers).status_code, 400)
        self.assertEqual(configuration.location().read_bytes(), before)
        nested = self.client.post("/api/scoring", data="[" * 100000 + "]" * 100000, headers=headers | {"Content-Type": "application/json"})
        self.assertEqual((nested.status_code, "error" in nested.get_json()), (400, True))
        self.assertEqual(configuration.location().read_bytes(), before)
        self.assertEqual(self.client.post("/api/scoring", json=document).status_code, 403)
        self.assertEqual(self.client.post("/api/scoring", json=document, headers=headers, environ_base={"REMOTE_ADDR": "192.0.2.2"}).status_code, 403)
        # a HEAD reads: it never takes the write's place
        document["axes"]["Retrieval"]["weight"] = .77
        self.assertEqual(self.client.head("/api/scoring", json=document, headers=headers).status_code, 200)
        self.assertEqual(configuration.location().read_bytes(), before)
        configuration.location().write_text("{")
        for path in ("/leaderboard", "/scoring"):
            self.assertIn("defaults are active", self.client.get(path).get_data(as_text=True))
        self.assertEqual(configuration.location().read_text(), "{")
        configuration.location().unlink()
        configuration.location().mkdir()                               # not a file at all: the same defaults, no error
        for path in ("/leaderboard", "/scoring", "/api/scoring", "/api/leaderboard"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_edit_reset_and_conflicting_writes_keep_complete_documents(self):
        self.client.get("/scoring")
        with self.client.session_transaction() as session:
            headers = {"X-CSRF-Token": session["csrf"], "Origin": "http://localhost"}
        initial = self.client.get("/api/scoring").get_json()
        import copy
        doc = copy.deepcopy(initial["settings"])
        legacy = copy.deepcopy(doc) | {"custom": []}
        refused = self.client.post("/api/scoring", json=legacy, headers=headers | {"If-Match": initial["revision"]})
        self.assertEqual(refused.status_code, 400)                    # custom benchmarks are gone
        doc["axes"]["Safety"]["weight"] = .5
        result = self.client.post("/api/scoring", json=doc, headers=headers | {"If-Match": initial["revision"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["settings"]["axes"]["Safety"]["weight"], .5)
        before = configuration.location().read_bytes()
        conflict = self.client.post("/api/scoring", json={"reset": True}, headers=headers | {"If-Match": initial["revision"]})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(configuration.location().read_bytes(), before)
        with patch("nullius.ondisk.write_whole", side_effect=OSError("private path")):
            failed = self.client.post("/api/scoring", json={"reset": True}, headers=headers)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private path", failed.get_data(as_text=True))
        self.assertEqual(configuration.location().read_bytes(), before)
        reset = self.client.post("/api/scoring", json={"reset": True}, headers=headers)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.get_json()["settings"], initial["settings"])


if __name__ == "__main__":
    unittest.main()
