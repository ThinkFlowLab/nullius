"""The HTTP API (``web/api.py``): the token every route takes and what each
scope may do, the guards a page takes that it does not, the reads as the
commands print them, a recipe checked and started as Review does it, a run
followed and stopped; then the client, the command and the MCP tools against
the application served on a socket. The execution's runners are fakes."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from flask.logging import default_handler
from werkzeug.serving import make_server

from nullius import access, cli, client, execution, mcp_server, workspace
from nullius.catalogue import catalog
from nullius.compare import cli as compare_cli, comparison
from nullius.judge import client as judge_client, questions as Q, store as judge_store
from nullius.leaderboard import service as leaderboard_service
from nullius.recipes import store
from nullius.runs import cli as runs_cli
from nullius.standard import cli as standard_cli
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe as standard_recipe
from nullius.standard.runner import sets as task_sets
from tests.test_comparison import job as scored_job, trial as scored_trial
from .test_routes import web

api = web.api
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
JOB = FIXTURES / "harbor-jobs" / "liveboard-oracle-x4"


def doc(value):
    """A document as the API sends it: through JSON, timestamps as the commands print them."""
    return json.loads(json.dumps(value, default=runs_cli._stamp))


def gsm8k(**kw):
    return dict({"version": 1, "harness": "standard", "name": "g", "title": "GSM8K", "benchmark": "gsm8k", "datasets": ["test"],
                 "others": ["original"], "model": "openai/x", "provider": "openrouter", "where": "local"}, **kw)


class Case(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-api-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("oracle-a", "oracle-b"):
            shutil.copytree(JOB, self.tmp / "jobs" / name)
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp / "home"), "HOSTS": "bench", "RUNS_DIR": str(self.tmp / "runs"),
                                                         "OPENROUTER_API_KEY": "sk-testkey-0123456789"}))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "workspaces")))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(default_handler, "level", logging.WARNING))     # one line a write otherwise
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.object(execution, "SYNC_EVERY", 0.05))
        ws = workspace.create(web.shared.WS_HOME, [str(self.tmp / "jobs")], "api")
        workspace.save(ws)
        web.shared.open_workspace(ws)
        self.reader, _ = access.create("reader", "read")
        self.runner, self.runner_record = access.create("runner", "run")
        self.client = web.app.test_client()

    def ask(self, method, path, token="runner", body=None, **kw):
        headers = dict(kw.pop("headers", {}))
        if token:
            headers["Authorization"] = "Bearer " + {"runner": self.runner, "reader": self.reader}.get(token, token)
        r = self.client.open("/api/v1" + path, method=method, headers=headers,
                             **({"data": json.dumps(body), "content_type": "application/json"} if body is not None else {}), **kw)
        self.addCleanup(r.close)
        return r

    def get(self, path, token="reader", **kw):
        return self.ask("GET", path, token, **kw)

    def post(self, path, body=None, token="runner", **kw):
        return self.ask("POST", path, token, body if body is not None else {}, **kw)


class Tokens(Case):
    def test_every_route_takes_a_token_in_the_authorization_header(self):
        rules = [r for r in web.app.url_map.iter_rules() if str(r).startswith("/api/v1")]
        self.assertGreater(len(rules), 15)
        for rule in rules:
            url = str(rule).replace("<job_id>", "oracle-a").replace("<trial_name>", "t").replace("<key>", "result") \
                .replace("<run_id>", "r").replace("<recipe_id>", "x")
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                r = self.client.open(url, method=method); self.addCleanup(r.close)
                self.assertEqual((r.status_code, r.headers.get("WWW-Authenticate")), (401, "Bearer"), (method, url))
                self.assertIn("nullius access create", r.json["error"])
        for sent in ("nope", self.runner + "x", "Bearer", ""):
            self.assertEqual(self.get("", token=None, headers={"Authorization": f"Bearer {sent}"}).status_code, 401, sent)
        self.assertEqual(self.get("", token=None, headers={"Authorization": f"Basic {self.runner}"}).status_code, 401)
        # a token is read from the header only: never the query, a form or a cookie, which a page can be made to send
        self.assertEqual(self.get(f"?token={self.runner}&access_token={self.runner}", token=None).status_code, 401)
        self.client.set_cookie("token", self.runner)
        self.assertEqual(self.get("", token=None).status_code, 401)
        self.assertEqual(self.get("").status_code, 200)

    def test_a_revoked_token_and_a_server_without_tokens_answer_nothing(self):
        self.assertEqual(self.get("/jobs", token="runner").status_code, 200)
        access.revoke(self.runner_record["id"])
        self.assertEqual(self.get("/jobs", token="runner").status_code, 401)
        os.unlink(access.file())
        self.assertEqual(self.get("/jobs").status_code, 401)

    def test_read_reads_and_only_run_writes(self):
        for path in ("/recipes", "/runs/check", "/runs", "/runs/r/stop", "/runs/r/continue"):
            r = self.post(path, token="reader")
            self.assertEqual((r.status_code, r.json), (403, {"error": "This token's scope is read: this needs run."}), path)
        self.assertEqual(execution.EXECUTIONS, {})
        for path in ("", "/catalogue", "/workspaces", "/jobs", "/recipes", "/runs"):
            self.assertEqual(self.get(path).status_code, 200, path)
        info = self.get("", token="runner").json
        self.assertEqual((info["api"], info["token"], info["where"], info["workspace"]),
                         (1, access.shown(self.runner_record), ["local", "bench"], web.shared.STATE["workspace"].name))

    def test_no_route_reads_settings_or_sends_a_key(self):
        """Nothing under the API names Settings, and no answer carries the provider's key."""
        self.assertEqual([str(r) for r in web.app.url_map.iter_rules() if str(r).startswith("/api/v1") and "setting" in str(r)], [])
        for path in ("", "/catalogue", "/workspaces", "/jobs", "/recipes", "/runs"):
            self.assertNotIn("sk-testkey-0123456789", self.get(path).get_data(as_text=True), path)

    def test_the_api_keeps_no_session_and_answers_in_json_with_no_store(self):
        for r in (self.get("/jobs"), self.get("/jobs/nope"), self.get("", token=None), self.post("/runs/check", {"recipe": gsm8k()})):
            self.assertIsNone(r.headers.get("Set-Cookie"), r.request.path)
            self.assertEqual((r.mimetype, r.headers["Cache-Control"]), ("application/json", "no-store"), r.request.path)

    def test_a_token_stands_where_a_page_s_guards_would_refuse(self):
        """A page's write takes the form token, the same origin and a loopback browser, and a page is served
        under the server's own name only. A client on another machine has none of these: its token is the guard."""
        far = {"environ_base": {"REMOTE_ADDR": "10.0.0.7"}, "headers": {"Host": "bench.internal:5057"}}
        self.assertEqual(self.client.get("/runs", headers={"Host": "bench.internal:5057"}).status_code, 421)       # a page: refused
        self.assertEqual(self.get("/jobs", **far).status_code, 200)
        self.assertEqual(self.get("/jobs", token=None, **far).status_code, 401)
        self.assertEqual(self.post("/runs/check", {"recipe": gsm8k()}, **far).status_code, 200)
        self.assertEqual(self.client.post("/run", data="{}", content_type="application/json", headers={"Authorization": "Bearer " + self.runner}).status_code, 403)   # the page's route keeps its own


class Reads(Case):
    def setUp(self):
        super().setUp()
        with web.app.test_request_context("/"):
            self.jobs = web.shared._load_jobs()
        self.job = next(j for j in self.jobs if j.job_id == "oracle-a")
        self.trial = next(t for t in self.job.trials if t.has_trajectory) if any(t.has_trajectory for t in self.job.trials) else self.job.trials[0]

    def test_jobs_and_a_job_are_what_the_command_prints(self):
        self.assertEqual(self.get("/jobs").json, doc([runs_cli.job_row(j) for j in self.jobs]))
        self.assertEqual(sorted(j["job_id"] for j in self.get("/jobs").json), ["oracle-a", "oracle-b"])
        got = self.get("/jobs/oracle-a").json
        self.assertEqual(got, doc(runs_cli.job_detail(self.job)))
        self.assertEqual(len(got["trials"]), len(self.job.trials))
        self.assertEqual((got["adjusted_solve_rate"], got["counts"]), (self.job.adjusted_solve_rate, doc(self.job.counts)))
        r = self.get("/jobs/nope")
        self.assertEqual((r.status_code, r.json), (404, {"error": "no job 'nope' in this workspace"}))

    def test_a_trial_its_records_and_its_conversation(self):
        name = self.trial.trial_name
        self.assertEqual(self.get(f"/jobs/oracle-a/trials/{name}").json, doc(runs_cli.trial_detail(self.job, self.trial)))
        listed = self.get(f"/jobs/oracle-a/trials/{name}/artifacts").json
        self.assertIn("result", [r["key"] for r in listed])
        self.assertTrue(all(sorted(r) == ["filename", "key", "label", "size"] and r["size"] is not None for r in listed))
        got = self.get(f"/jobs/oracle-a/trials/{name}/artifacts/result").json
        self.assertEqual((got["key"], got["filename"], got["truncated"]), ("result", "result.json", False))
        self.assertEqual(json.loads(got["text"])["trial_name"], name)
        page = self.client.get(f"/trial/oracle-a/{name}/conversation"); self.addCleanup(page.close)
        self.assertEqual(self.get(f"/jobs/oracle-a/trials/{name}/conversation").json, page.json)       # the page's own
        for path in ("/jobs/oracle-a/trials/nope", f"/jobs/oracle-a/trials/{name}/artifacts/nope", f"/jobs/oracle-a/trials/{name}/artifacts/..%2Fconfig",
                     f"/jobs/oracle-a/trials/{name}/artifacts/..%2F..%2Fconfig.json", f"/jobs/nope/trials/{name}/conversation", "/no-such-route"):
            r = self.get(path)
            self.assertEqual((r.status_code, sorted(r.json)), (404, ["error"]), path)

    def test_a_trace_is_sent_redacted_and_a_trial_without_one_has_none(self):
        name = self.trial.trial_name
        r = self.get(f"/jobs/oracle-a/trials/{name}/trace")
        self.assertEqual((r.status_code, r.json), (404, {"error": f"trial {name!r} has no trace that can be read"}))
        shutil.copytree(JOB, self.tmp / "jobs" / "traced")
        agent = self.tmp / "jobs" / "traced" / name / "agent"; agent.mkdir(exist_ok=True)
        secret = "sk-or-v1-" + "a" * 64
        (agent / "trace.json").write_text(json.dumps({"schema_version": "harbor-trace-v1", "spans": [{"name": "call", "attributes": {"auth": "Bearer " + secret, "password": "hunter2hunter2",
                                                                         "url": "http://relay:8080", "agent": "x@1.0", "token": 123456789}}]}))
        web.shared.open_workspace(web.shared.STATE["workspace"], refresh=True)
        r = self.get(f"/jobs/traced/trials/{name}/trace")
        self.assertEqual((r.status_code, r.json["schema_version"], r.json["spans"][0]["name"]), (200, "harbor-trace-v1", "call"))
        self.assertNotIn(secret, r.get_data(as_text=True))
        self.assertEqual(r.json["spans"][0]["attributes"], {"auth": "Bearer [REDACTED]", "password": "[REDACTED]",      # each string alone: the JSON stays JSON
                                                            "url": "http://relay:8080", "agent": "x@1.0", "token": 123456789})

    def test_a_trace_that_is_a_link_or_no_trace_is_not_read(self):
        name = self.trial.trial_name
        shutil.copytree(JOB, self.tmp / "jobs" / "linked")
        agent = self.tmp / "jobs" / "linked" / name / "agent"; agent.mkdir(exist_ok=True)
        (self.tmp / "outside.json").write_text('{"private": "file"}')
        (agent / "trace.json").symlink_to(self.tmp / "outside.json")           # the agent writes this folder
        web.shared.open_workspace(web.shared.STATE["workspace"], refresh=True)
        self.assertEqual(self.get(f"/jobs/linked/trials/{name}/trace").status_code, 404)
        (agent / "trace.json").unlink()
        for text in ("[]", "PRIVATE KEY", "[" * 20000):
            (agent / "trace.json").write_text(text)
            self.assertEqual(self.get(f"/jobs/linked/trials/{name}/trace").status_code, 404, text[:20])
        shutil.rmtree(agent)
        (self.tmp / "elsewhere").mkdir(); (self.tmp / "elsewhere" / "trace.json").write_text("{}")
        agent.symlink_to(self.tmp / "elsewhere")
        self.assertEqual(self.get(f"/jobs/linked/trials/{name}/trace").status_code, 404)

    def test_the_live_board_is_the_page_s(self):
        page = self.client.get("/job/oracle-a/live"); self.addCleanup(page.close)
        r = self.get("/jobs/oracle-a/live")
        self.assertEqual((r.status_code, r.json["state"], sorted(r.json)), (page.status_code, page.json["state"], sorted(page.json)))
        self.assertEqual(len(r.json["trials"]), len(self.job.trials))
        r = self.get("/jobs/nope/live")
        self.assertEqual((r.status_code, r.json), (404, {"error": "no job 'nope' in this workspace"}))
        shutil.copytree(JOB, self.tmp / "jobs" / "no-feed", ignore=shutil.ignore_patterns("events.jsonl"))
        r = self.get("/jobs/no-feed/live")                                       # the job is there: what it lacks is said
        self.assertEqual((r.status_code, r.json), (404, {"error": "job 'no-feed' kept no feed for a live board"}))
        r = self.get("/jobs/oracle-a/trials/nope/conversation")
        self.assertEqual((r.status_code, r.json), (404, {"error": "no trial 'nope' in job 'oracle-a'"}))

    def test_a_comparison_is_what_the_command_prints(self):
        selected = comparison.resolve([":oracle-a", ":oracle-b"], {"": self.jobs})
        for query, cohort in (("", "whole"), ("&matched=1", "matched")):
            self.assertEqual(self.get("/compare?job=oracle-a&job=oracle-b" + query).json, doc(compare_cli.document(comparison.build(selected, cohort))), cohort)
        self.assertEqual(self.get("/compare?job=oracle-a&job=oracle-a").status_code, 400)       # one run twice is one run
        r = self.get("/compare?job=oracle-a&job=nope")
        self.assertEqual((r.status_code, r.json), (404, {"error": "Run nope is unavailable in the active source."}))

    def test_the_leaderboard_is_the_one_its_page_reads_of_the_workspace_asked(self):
        recorded = str(FIXTURES / "native" / "parts-arm" / "t1")                # a trial that kept its trajectory

        def scored(name, folder, second, benchmark="gaia", dataset="gaia/gaia-validation"):
            """A run by Codex, half passed or all, in ``folder``."""
            run = replace(scored_job([scored_trial("a", dir=recorded), scored_trial("b", second)], dataset=dataset, agent="codex", job_id=name),
                          dir=f"{folder}/{name}")
            run.identity = {"agent": "codex", "agent_version": "2.2.2", "benchmark": benchmark,
                            "benchmark_version": definitions.version_identity(definitions.get("benchmark", benchmark))}
            return run
        other = workspace.create(web.shared.WS_HOME, [str(self.tmp / "jobs" / "oracle-b")], "only-b")
        workspace.save(other)
        held = {web.shared.STATE["workspace"].name: [*self.jobs, scored("same", "/one", "agent_error"),
                                                     scored("same", "/two", "agent_error", "tau3-bench", "tau3-bench/banking_knowledge")],
                other.name: [scored("solo", "/three", "resolved")]}
        self.stack.enter_context(patch.object(workspace, "load_jobs", lambda ws, roots, ends=None: held[ws.name]))
        read = self.stack.enter_context(patch("nullius.leaderboard.metrics.Cache.read"))
        got = self.get("/leaderboard").json
        page = self.client.get("/api/leaderboard"); self.addCleanup(page.close)
        self.assertEqual(got, page.json)                                         # the document its page reads, of the open workspace
        row = next(r for r in got["rows"] if r["has_runs"])
        self.assertEqual((row["name"], row["axes"]["Retrieval"]["score"], row["rank"], got["empty"]), ("Codex", .5, 1, ""))
        runs = sorted({i for axis in row["axes"].values() for e in axis["entries"] for i in e["runs"]})
        self.assertEqual(runs, ["/one/same", "/two/same"])                       # two runs of one name, told apart by folder
        # another workspace's runs, read for it without opening it, each linked there
        there = self.get(f"/leaderboard?workspace={other.name}").json
        row = next(r for r in there["rows"] if r["has_runs"])
        links = sorted({x["url"] for axis in row["axes"].values() for e in axis["entries"] for x in e["links"]})
        self.assertEqual((row["axes"]["Retrieval"]["score"], links), (1.0, [f"/job/solo?workspace={other.name}"]))
        # no file of a trial is opened: an agent wrote them, and the counts read from them stay uncounted
        read.assert_not_called()
        self.assertEqual(row["metrics"]["steps"], {"value": None, "count": 0})
        with patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "job_roots": []}):
            self.assertEqual(self.get("/leaderboard").json["empty"], "No workspace open")
        self.assertEqual(self.get("/leaderboard", token=None).status_code, 401)

    def test_another_workspace_by_name_and_one_that_is_not_there(self):
        other = workspace.create(web.shared.WS_HOME, [str(self.tmp / "jobs" / "oracle-b")], "only-b")
        workspace.save(other)
        self.assertEqual([j["job_id"] for j in self.get(f"/jobs?workspace={other.name}").json], ["oracle-b"])
        self.assertEqual(self.get(f"/jobs/oracle-a?workspace={other.name}").status_code, 404)
        self.assertEqual(sorted(w["name"] for w in self.get("/workspaces").json), sorted([other.name, web.shared.STATE["workspace"].name]))
        for path in ("/jobs", "/jobs/oracle-a", "/leaderboard", "/compare?job=oracle-a&job=oracle-b", "/jobs/oracle-a/judgements"):
            r = self.get(path + ("&" if "?" in path else "?") + "workspace=nope")
            self.assertEqual((r.status_code, r.json), (404, {"error": "no workspace 'nope'"}), path)
        r = self.post("/jobs/oracle-a/judge?workspace=nope", {"confirm": True})
        self.assertEqual((r.status_code, r.json), (404, {"error": "no workspace 'nope'"}))

    def test_a_job_s_record_is_read_in_the_workspace_asked(self):
        """A job of another workspace than the open one: each route of its record finds it there, and nowhere else."""
        shutil.copytree(JOB, self.tmp / "elsewhere" / "oracle-c")
        other = workspace.create(web.shared.WS_HOME, [str(self.tmp / "elsewhere")], "elsewhere")
        workspace.save(other)
        t = self.trial.trial_name
        for path in ("", "/live", f"/trials/{t}", f"/trials/{t}/conversation", f"/trials/{t}/artifacts", f"/trials/{t}/artifacts/result",
                     "/judgements"):
            self.assertEqual(self.get("/jobs/oracle-c" + path).status_code, 404, path)
            self.assertEqual(self.get(f"/jobs/oracle-c{path}?workspace={other.name}").status_code, 200, path)
        for path, error in ((f"/trials/{t}/trace", f"trial {t!r} has no trace that can be read"), (f"/trials/{t}/judgement", f"trial {t!r} has no decision judgement")):
            r = self.get(f"/jobs/oracle-c{path}?workspace={other.name}")                  # found there: what it lacks is said
            self.assertEqual((r.status_code, r.json["error"]), (404, error), path)

    def test_the_catalogue_names_what_a_recipe_names(self):
        got = self.get("/catalogue").json
        g = next(b for b in got["benchmarks"] if b["key"] == "gsm8k")
        self.assertEqual((g["runners"], [d["id"] for d in g["datasets"]]), (["standard"], ["test", "platinum"]))
        self.assertIn("reference", g["fits"])
        self.assertIn("reference", [a["ref"] for a in got["agents"]])
        self.assertTrue(all(sorted(c) == ["key", "label", "offers", "ref", "summary"] for c in got["capabilities"]))
        self.assertTrue(all(sorted(c) == ["key", "label", "offers", "origin", "ref", "summary"] for c in got["agents"]))
        self.assertLessEqual({x["origin"] for x in got["benchmarks"] + got["agents"]}, set(catalog.ORIGINS))
        self.assertEqual(g["fits"][0], "original")                                 # its own procedure, the builder's first choice
        self.assertEqual(standard_recipe.compile(gsm8k(others=g["fits"][:1], limit=1))["errors"], [])
        gaia = next(b for b in got["benchmarks"] if b["key"] == "gaia")            # lists cut from one list: a recipe takes one of them
        self.assertEqual({d["id"]: d["subset_of"] for d in gaia["datasets"]},
                         {x["id"]: x.get("subset_of") for x in definitions.get("benchmark", "gaia")["datasets"]})
        self.assertIn("gaia-validation", {d["subset_of"] for d in gaia["datasets"]})
        # a task set kept on the server is listed under its benchmark, as its command lists it: what a recipe's task_sets names
        self.assertEqual(g["task_sets"], [])
        entry = task_sets.create(definitions.get("benchmark", "gsm8k"), "First two", dataset="test", units=["0001", "0002"])
        g = next(b for b in self.get("/catalogue").json["benchmarks"] if b["key"] == "gsm8k")
        self.assertEqual(g["task_sets"], [standard_cli._set_rows(e) for e in task_sets.entries("gsm8k")[0]])
        self.assertEqual([(s["name"], s["label"], s["parts"], s["id"]) for s in g["task_sets"]], [(entry["name"], "First two", {"test": 2}, entry["id"])])
        self.assertEqual([b["key"] for b in self.get("/catalogue").json["benchmarks"] if b.get("task_sets")], ["gsm8k"])
        task_sets.rename("gsm8k", entry["name"], "Smoke pair")                     # renamed, it is the same set: the id a job records
        g = next(b for b in self.get("/catalogue").json["benchmarks"] if b["key"] == "gsm8k")
        self.assertEqual([(s["name"], s["id"]) for s in g["task_sets"]], [("smoke-pair", entry["id"])])
        # sets that cannot be read cost the catalogue nothing else
        folder = Path(task_sets.root_dir()) / task_sets.FOLDER
        folder.rename(folder.with_name("aside"))
        folder.symlink_to(folder.with_name("aside"))
        r = self.get("/catalogue")
        self.assertEqual((r.status_code, [b["key"] for b in r.json["benchmarks"] if b.get("task_sets")], len(r.json["benchmarks"])), (200, [], len(got["benchmarks"])))


class Judgements(Case):
    """A judge's answers read, and a judge asked: one trial by the request, several only with a yes. The model's
    answer is a fake; the record is kept and read back by the pages' own store."""

    def setUp(self):
        super().setUp()
        self.asked, self.failing = [], {}
        judges = {"decision": judge_client.Judge("typesafe", "k", "m"), "llm": judge_client.Judge("openrouter", "k", "m", url="http://127.0.0.1:9/v1", lane="llm")}
        self.stack.enter_context(patch.object(web.judge, "key", lambda lane="decision": judges[lane]))

        def ask_states(states, k, post=None, calibration=None):
            self.asked.append((*states["of"], k.lane))
            if states["of"][1] in self.failing:
                raise judge_client.JudgeError(self.failing[states["of"][1]], "the judge is busy")
            return {"questions": Q.VERSION, "judge": {"model": k.lane}, "answers": {"task_completed": {"choice": "yes", "confidence": 0.9}}}
        self.stack.enter_context(patch.object(web.judge.batch, "ask_states", ask_states))
        self.stack.enter_context(patch.object(web.judge, "_trial_ids_and_states", lambda trial, traj: {"of": (trial.job_id, trial.trial_name)}))
        for trial in sorted((self.tmp / "jobs" / "oracle-a").glob("*__*"))[:3]:               # a judge reads a trajectory: three trials kept one
            (trial / "agent").mkdir(exist_ok=True)
            (trial / "agent" / "trajectory.json").write_text("{}")
        web.shared.open_workspace(web.shared.STATE["workspace"], refresh=True)
        with web.app.test_request_context("/"):
            self.job = next(j for j in web.shared._load_jobs() if j.job_id == "oracle-a")
        self.judgeable = [t.trial_name for t in self.job.trials if t.has_trajectory]
        self.assertGreater(len(self.judgeable), 1)
        self.trial = self.judgeable[0]

    def test_a_trial_is_judged_by_the_request_and_its_record_read_back(self):
        r = self.get(f"/jobs/oracle-a/trials/{self.trial}/judgement")
        self.assertEqual((r.status_code, r.json), (404, {"error": f"trial {self.trial!r} has no decision judgement"}))
        r = self.post(f"/jobs/oracle-a/trials/{self.trial}/judge")
        self.assertEqual((r.status_code, r.json["answers"]["task_completed"]["choice"]), (200, "yes"))
        self.assertEqual(self.asked, [("oracle-a", self.trial, "decision")])
        self.assertEqual(self.get(f"/jobs/oracle-a/trials/{self.trial}/judgement").json["answers"], r.json["answers"])
        # the LLM judge judges apart: its own records, asked by name
        self.assertEqual(self.get(f"/jobs/oracle-a/trials/{self.trial}/judgement?lane=llm").status_code, 404)
        self.assertEqual(self.post(f"/jobs/oracle-a/trials/{self.trial}/judge", {"lane": "llm"}).json["judge"], {"model": "llm"})
        self.assertEqual(self.get(f"/jobs/oracle-a/trials/{self.trial}/judgement?lane=llm").json["judge"], {"model": "llm"})
        got = self.get("/jobs/oracle-a/judgements").json
        self.assertEqual((got["lane"], got["judged"], got["trials"], list(got["records"])), ("decision", 1, len(self.job.trials), [self.trial]))
        self.assertEqual(self.get("/jobs/oracle-b/judgements").json["judged"], 0)             # its twin was not asked about

    def test_a_job_is_judged_only_with_a_yes_and_only_where_unjudged(self):
        self.post(f"/jobs/oracle-a/trials/{self.trial}/judge")
        n = len(self.judgeable) - 1
        for body in ({}, {"confirm": "true"}, {"confirm": 1}):
            r = self.post("/jobs/oracle-a/judge", body)
            self.assertEqual((r.status_code, r.json["todo"]), (409, n), body)
            self.assertEqual(r.json["error"], f'This asks the judge about {n} trial(s), a paid call each. Send "confirm": true to ask.')
        self.assertEqual(len(self.asked), 1)                                                   # nothing asked meanwhile
        r = self.post("/jobs/oracle-a/judge", {"confirm": True})
        self.assertEqual((r.status_code, r.json), (200, {"lane": "decision", "todo": n, "judged": n, "failed": []}))
        self.assertEqual(sorted(t for _, t, _ in self.asked[1:]), sorted(self.judgeable[1:]))
        self.assertEqual(self.get("/jobs/oracle-a/judgements").json["judged"], len(self.judgeable))
        again = self.post("/jobs/oracle-a/judge")                                             # nothing left: nothing to confirm, nothing asked
        self.assertEqual((again.status_code, again.json["todo"], len(self.asked)), (200, 0, 1 + n))

    def test_a_request_that_names_no_judge_it_knows_asks_none(self):
        path, headers = f"/api/v1/jobs/oracle-a/trials/{self.trial}/judge", {"Authorization": f"Bearer {self.runner}"}
        for raw in ('{"lane":"llm"', '["llm"]', '"llm"', "lane=llm", '{"lane":[]}', '{"lane":""}', "[" * 20000):
            self.assertEqual(self.client.post(path, data=raw, headers=headers).status_code, 400, raw[:20])
        self.assertEqual(self.asked, [])
        for ask in ({"data": '{"lane":"llm"}'}, {"data": '{"lane":"llm"}', "content_type": "application/x-www-form-urlencoded"},
                    {"query_string": "lane=llm"}):
            self.assertEqual(self.client.post(path, headers=headers, **ask).status_code, 200, ask)
        self.assertEqual({lane for *_, lane in self.asked}, {"llm"})

    def test_an_attempt_s_earlier_try_is_read_and_never_judged(self):
        with patch.object(web.traces, "_find_try", lambda job, name: self.job.trials[0] if name == "t.try-1" else None):
            self.assertEqual(self.get("/jobs/oracle-a/trials/t.try-1").status_code, 200)
            self.assertEqual(self.post("/jobs/oracle-a/trials/t.try-1/judge").status_code, 404)
        self.assertEqual(self.asked, [])

    def test_a_job_asked_through_another_workspace_reads_and_keeps_what_that_one_kept_before(self):
        kept = Path(web.shared.STATE["workspace"].dir) / "judgements" / "oracle-a"            # records from before judgements were the job's
        kept.mkdir(parents=True)
        (kept / f"{self.trial}.json").write_text(json.dumps({"retired": {"questions": "retired", "answers": {"kept": {"choice": "x"}}}}))
        (kept / f"{self.judgeable[1]}.json").write_text(json.dumps({Q.VERSION: {"questions": Q.VERSION, "answers": {"kept": {"choice": "x"}}}}))
        web.shared.open_workspace(workspace.create(web.shared.WS_HOME, [str(self.tmp / "jobs")], "other"))
        n = len(self.judgeable) - 1                                                            # one is judged already: its record is still read
        self.assertEqual(self.get("/jobs/oracle-a/judgements?workspace=api").json["judged"], 1)
        r = self.post("/jobs/oracle-a/judge?workspace=api", {"confirm": True})
        self.assertEqual((r.status_code, r.json["judged"], len(self.asked)), (200, n, n))
        saved = json.loads((Path(judge_store.home()) / "judgements" / self.job.uid / f"{self.trial}.json").read_text())
        self.assertLessEqual({"retired", Q.VERSION}, set(saved))                                # the workspace asked's: the batch's threads have no request
        self.assertEqual(self.get("/jobs/oracle-a/judgements").json["judged"], n)              # the new ones are the job's, in the open workspace too
        self.assertEqual((self.post("/jobs/oracle-a/judge?workspace=api").json["todo"], len(self.asked)), (0, n))

    def test_a_judge_that_fails_is_said_and_a_refused_key_is_the_request_s_fault(self):
        self.failing = {self.trial: 500}
        r = self.post(f"/jobs/oracle-a/trials/{self.trial}/judge")
        self.assertEqual((r.status_code, r.json), (502, {"error": "the judge is busy"}))
        self.failing = {self.trial: 401}
        self.assertEqual(self.post(f"/jobs/oracle-a/trials/{self.trial}/judge").status_code, 400)
        self.failing = {self.judgeable[1]: 500}
        r = self.post("/jobs/oracle-a/judge", {"confirm": True})
        self.assertEqual((r.json["judged"], r.json["failed"]), (len(self.judgeable) - 1, [f"{self.judgeable[1]}: the judge is busy"]))

    def test_asking_takes_the_run_scope_and_reading_does_not(self):
        for path in (f"/jobs/oracle-a/trials/{self.trial}/judge", "/jobs/oracle-a/judge"):
            self.assertEqual(self.post(path, {"confirm": True}, token="reader").status_code, 403, path)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.get("/jobs/oracle-a/judgements", token="reader").status_code, 200)
        for path in ("/jobs/oracle-a/judgements?lane=other", f"/jobs/oracle-a/trials/{self.trial}/judgement?lane=other"):
            self.assertEqual(self.get(path).json, {"error": "A judge is one of: decision, llm."}, path)
        self.assertEqual(self.post("/jobs/nope/judge", {"confirm": True}).status_code, 404)
        self.assertEqual(self.post("/jobs/oracle-a/trials/nope/judge").status_code, 404)


class SettingsTokens(Case):
    """Settings' API access section: a token made, shown once, listed without its value, revoked; a browser on this machine only."""

    def setUp(self):
        super().setUp()
        page = self.client.get("/settings"); self.addCleanup(page.close)
        self.html = page.get_data(as_text=True)
        with self.client.session_transaction() as s:
            self.headers = {"Origin": "http://localhost", "X-CSRF-Token": s["csrf"]}

    def form(self, path, data, **kw):
        r = self.client.post(path, data=data, headers=kw.pop("headers", self.headers), **kw); self.addCleanup(r.close)
        return r

    def test_the_section_lists_the_tokens_and_never_a_value_or_a_hash(self):
        self.assertIn('<a href="#api-access" data-section="api-access"', self.html)
        self.assertIn('<symbol id="i-api"', self.html)
        state = json.loads(re.search(r'id="api-access"[^>]*data-state=\'([^\']*)\'', self.html).group(1).replace("&#34;", '"'))
        self.assertEqual([(t["name"], t["scope"], t["expired"]) for t in state], [("reader", "read", False), ("runner", "run", False)])
        self.assertEqual(sorted(state[0]), ["created", "expired", "expires", "id", "name", "scope"])
        for secret in (self.reader, self.runner, self.reader.split("_", 2)[2], self.runner_record["sha256"]):
            self.assertNotIn(secret, self.html)
        self.assertIn("data-api-add", self.html)
        far = self.client.get("/settings", environ_base={"REMOTE_ADDR": "10.0.0.7"}); self.addCleanup(far.close)
        self.assertNotIn("data-api-add", far.get_data(as_text=True))                       # read-only from another machine: no form, no routes
        self.assertNotIn("data-revoke", far.get_data(as_text=True))

    def test_a_token_is_created_shown_once_and_opens_the_api(self):
        r = self.form("/settings/api/create", {"name": "laptop", "scope": "run", "days": "30"})
        self.assertEqual((r.status_code, r.json["ok"], r.headers["Cache-Control"]), (200, True, "no-store"))
        self.assertEqual((r.json["token"]["name"], r.json["token"]["scope"], r.json["token"]["expires"][:4].isdigit()), ("laptop", "run", True))
        self.assertEqual([t["name"] for t in r.json["tokens"]], ["reader", "runner", "laptop"])
        self.assertNotIn(r.json["value"], json.dumps(r.json["tokens"]))
        self.assertEqual(self.get("", token=r.json["value"]).json["token"]["name"], "laptop")
        page = self.client.get("/settings"); self.addCleanup(page.close)
        self.assertNotIn(r.json["value"], page.get_data(as_text=True))                     # the page after it holds the name only
        never = self.form("/settings/api/create", {"name": "forever"})
        self.assertEqual((never.json["token"]["scope"], never.json["token"]["expires"]), ("read", None))

    def test_what_is_refused_and_from_where(self):
        for data, said in (({"name": "runner"}, "A token is named 'runner' already: revoke it first, or choose another name."),
                           ({"name": "a/b"}, "A token's name is 1 to 64 letters, digits, spaces, '.', '_' or '-'."),
                           ({"name": "x", "scope": "admin"}, "A token's scope is one of: read, run."),
                           ({"name": "x", "days": "soon"}, "A token expires after 1 to 3650 days, or never."),
                           ({"name": "x", "days": "0"}, "A token expires after 1 to 3650 days, or never.")):
            r = self.form("/settings/api/create", data)
            self.assertEqual((r.status_code, r.json), (400, {"ok": False, "error": said}), data)
        for path, data in (("/settings/api/create", {"name": "x"}), ("/settings/api/revoke", {"id": self.runner_record["id"]})):
            self.assertEqual(self.form(path, data, headers={"Origin": "http://localhost"}).status_code, 403, path)                   # no form token
            self.assertEqual(self.form(path, data, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403, path)               # not this machine
            self.assertEqual(self.form(path, data, headers={**self.headers, "Authorization": "Bearer " + self.runner, "X-CSRF-Token": "x"}).status_code, 403, path)   # a token makes no token
        self.assertEqual([r["name"] for r in access.listing()], ["reader", "runner"])

    def test_a_revoked_token_is_refused_at_once(self):
        self.assertEqual(self.get("/jobs", token="runner").status_code, 200)
        r = self.form("/settings/api/revoke", {"id": self.runner_record["id"]})
        self.assertEqual((r.status_code, [t["name"] for t in r.json["tokens"]]), (200, ["reader"]))
        self.assertEqual(self.get("/jobs", token="runner").status_code, 401)
        gone = self.form("/settings/api/revoke", {"id": self.runner_record["id"]})
        self.assertEqual((gone.status_code, gone.json), (404, {"ok": False, "error": "That token is gone already."}))


class Recipes(Case):
    def test_a_recipe_is_saved_listed_and_read_by_id_or_name(self):
        saved = self.post("/recipes", {"recipe": gsm8k(title="Nightly")}).json
        self.assertEqual((saved["name"], saved["recipe"]["benchmark"]), ("Nightly", "gsm8k"))
        self.assertEqual(self.get("/recipes").json, [saved])
        self.assertEqual(store.listing(), [saved])                              # the builder's own store
        for key in (saved["id"], "Nightly"):
            self.assertEqual(self.get(f"/recipes/{key}").json, saved)
        again = self.post("/recipes", {"recipe": gsm8k(title="Nightly", model="openai/y")}).json
        self.assertEqual((again["id"], again["recipe"]["model"]), (saved["id"], "openai/y"))        # the same name: updated
        self.assertEqual(self.get("/recipes/nope").status_code, 404)
        r = self.post("/recipes", {"recipe": {"version": 2}})
        self.assertEqual((r.status_code, r.json), (400, {"error": 'a recipe is a JSON object with "version": 1'}))


class Runs(Case):
    def setUp(self):
        super().setUp()
        self.calls = []
        real = execution.subprocess_run

        def run(argv, timeout=600, stdin=None, stop=None):
            if argv[0] == "bash":
                return real(argv, timeout, stdin, stop=stop)
            self.calls.append(argv)
            return 0, ""
        self.stack.enter_context(patch.object(execution, "subprocess_run", run))
        self.stack.enter_context(patch.object(execution, "docker_here", lambda sh, needs="", program="docker": None))
        self.adopted = []
        self.stack.enter_context(patch.object(web.shared, "adopt_runs", lambda runs: self.adopted.extend(ex.plan.name for ex in runs)))

    def scripted(self, script="echo started; echo \"key=${#OPENAI_API_KEY}\""):
        """A recipe of the first two problems, whose run is this test's script in place of the standard's command."""
        real = standard_recipe.compile

        def compile_(recipe, *a, **kw):
            out = real(recipe, *a, **kw)
            for shard in out.get("shards") or []:
                shard.update(script=script, parts=[])
            return out
        self.stack.enter_context(patch.object(standard_recipe, "compile", compile_))
        return gsm8k(limit=2)

    def wait(self):
        for ex in list(execution.EXECUTIONS.values()):
            ex.thread.join(10)

    def test_a_check_is_review_s_and_starts_nothing(self):
        got = self.post("/runs/check", {"recipe": gsm8k()})
        self.assertEqual(got.status_code, 200)
        self.assertEqual(sorted(got.json), ["confirm", "errors", "jobs", "machines", "notes", "ready", "review"])
        self.assertEqual((got.json["errors"], got.json["ready"], got.json["confirm"]), ([], True, "Run all problems of GSM8K with GSM8K original?"))
        self.assertEqual([(m["where"], m["label"]) for m in got.json["machines"]], [("local", "this machine")])
        self.assertIn([2, "Dataset", "GSM8K test · 1319 problems"], got.json["review"])
        self.assertEqual((execution.EXECUTIONS, os.path.exists(self.tmp / "runs")), ({}, False))
        # a recipe that does not compile says what Review says, and its machines are not asked
        bad = self.post("/runs/check", {"recipe": gsm8k(model="")}).json
        self.assertEqual((bad["errors"], bad["ready"], bad["machines"]), (["Choose a model."], False, []))
        # a saved recipe by its name, and the request's Where over the recipe's
        store.save(gsm8k(title="Nightly"))
        there = self.post("/runs/check", {"recipe": "Nightly", "where": ["nobody@10.9.9.9"]}).json
        self.assertEqual(there["ready"], False)
        self.assertEqual([r["detail"] for m in there["machines"] for r in m["rows"] if r["state"] == "missing"], ["nobody@10.9.9.9 is not one of the hosts in Settings"])
        for where in ("", None, []):                                             # blank is no machine: the recipe's own stay
            got = self.post("/runs/check", {"recipe": gsm8k(where=["nobody@10.9.9.9"]), "where": where}).json
            self.assertEqual([m["where"] for m in got["machines"]], ["nobody@10.9.9.9"], where)
        long = self.post("/runs/check", {"recipe": gsm8k(name="n" * 70)}).json     # named as the run would be
        self.assertEqual((long["ready"], len(long["errors"])), (False, 1))
        for body in ({}, {"recipe": 7}, {"recipe": "no-such-recipe"}):
            self.assertIn(self.post("/runs/check", body).status_code, (400, 404), body)

    def test_a_start_is_refused_as_review_would_refuse_it(self):
        r = self.post("/runs", {"recipe": gsm8k(model="")})
        self.assertEqual((r.status_code, r.json["error"], r.json["errors"]), (400, "Choose a model.", ["Choose a model."]))
        r = self.post("/runs", {"recipe": gsm8k(where=["nobody@10.9.9.9"]), "confirm": True})
        self.assertEqual((r.status_code, r.json["error"], r.json["ready"]), (409, "A machine lacks what this recipe needs: see machines.", False))
        r = self.post("/runs", {"recipe": gsm8k()})                              # the whole dataset is a decision: asked, never assumed
        self.assertEqual((r.status_code, r.json["confirm"]), (409, "Run all problems of GSM8K with GSM8K original?"))
        self.assertEqual(r.json["error"], 'Run all problems of GSM8K with GSM8K original? Send "confirm": true to start it.')
        for yes in ("true", 1, "yes"):                                           # only true is a yes
            self.assertEqual(self.post("/runs", {"recipe": gsm8k(), "confirm": yes}).status_code, 409, yes)
        with patch.dict(os.environ):
            os.environ.pop("OPENROUTER_API_KEY")
            r = self.post("/runs", {"recipe": gsm8k(), "confirm": True})
            check = self.post("/runs/check", {"recipe": gsm8k()}).json             # and the check says so first
            self.assertEqual((check["ready"], check["errors"]), (False, r.json["errors"]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("OPENROUTER_API_KEY is not set in Settings", r.json["error"])
        self.assertEqual((execution.EXECUTIONS, os.path.exists(self.tmp / "runs"), self.adopted), ({}, False, []))

    def test_a_standard_recipe_starts_as_the_page_starts_it(self):
        """What reaches the execution is what the page's Run posts: the recipe, named by its name and the second,
        compiled on the server from the definitions."""
        seen = []
        real = web.execute.start
        self.stack.enter_context(patch.object(web.execute, "start", lambda body: seen.append(body) or real(body)))
        self.stack.enter_context(patch.object(execution.Execution, "_main", lambda ex: None))       # followed elsewhere: the plan is what is read here
        r = self.post("/runs", {"recipe": gsm8k(), "confirm": True})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertRegex(r.json["name"], r"^g-\d{4}-\d{6}$")
        self.assertEqual(seen, [{"name": r.json["name"], "where": "local", "recipe": gsm8k(name=r.json["name"])}])
        self.assertEqual((r.json["ok"], [x["id"] for x in r.json["runs"]], "open" in r.json), (True, [r.json["id"]], False))
        plan = execution.EXECUTIONS[r.json["id"]].plan
        self.assertEqual((plan.runner, plan.recipe["benchmark"], plan.key_var, plan.host), ("standard", "gsm8k", "OPENROUTER_API_KEY", None))
        self.assertIn("standard", plan.script); self.assertIn("gsm8k", plan.script)
        self.assertEqual(self.adopted, [r.json["name"]])
        named = self.post("/runs", {"recipe": gsm8k(), "confirm": True, "name": "mine"})
        self.assertEqual((named.status_code, named.json["name"]), (200, "mine"))
        again = self.post("/runs", {"recipe": gsm8k(), "confirm": True, "name": "mine"})         # a name is a run, once
        self.assertEqual(again.status_code, 409, again.json)

    def test_a_recipe_runs_a_task_set_kept_on_the_server(self):
        entry = task_sets.create(definitions.get("benchmark", "gsm8k"), "First two", dataset="test", units=["0001", "0002"])
        recipe = gsm8k(task_sets={"test": entry["name"]})
        got = self.post("/runs/check", {"recipe": recipe}).json
        self.assertEqual((got["errors"], got["ready"], got["confirm"]), ([], True, ""))      # its tasks are chosen already: no question
        self.assertIn([2, "Dataset", "GSM8K test · task set First two · 2 problems"], got["review"])
        missing = self.post("/runs/check", {"recipe": gsm8k(task_sets={"test": "nope"})}).json
        self.assertEqual((missing["ready"], missing["errors"]), (False, ["GSM8K has no task set nope: choose another, or all of test's tasks."]))
        self.stack.enter_context(patch.object(execution.Execution, "_main", lambda ex: None))
        r = self.post("/runs", {"recipe": recipe})                                           # started without a yes, as Review starts it
        self.assertEqual(r.status_code, 200, r.json)
        plan = execution.EXECUTIONS[r.json["id"]].plan
        self.assertIn(f"--set {entry['name']}", plan.script)
        self.assertEqual(plan.recipe["task_sets"], {"test": entry["name"]})

    def test_a_run_is_followed_listed_and_stopped_by_its_id(self):
        r = self.post("/runs", {"recipe": self.scripted()})
        self.assertEqual(r.status_code, 200, r.json)
        run_id = r.json["id"]; self.wait()
        got = self.get(f"/runs/{run_id}").json
        self.assertEqual((got["state"], got["host"], got["exit_code"], got["job_ids"]), ("done", "this machine", 0, [r.json["name"]]), got["log"])
        self.assertIn("key=21", "\n".join(got["log"]))                           # the run had its provider's key
        self.assertNotIn("sk-testkey-0123456789", json.dumps(got))               # and its log does not
        self.assertEqual(self.get(f"/runs/{run_id}?since={got['lines']}").json["log"], [])
        self.assertEqual([x["id"] for x in self.get("/runs").json], [run_id])
        self.assertEqual(self.get("/runs/nope").status_code, 404)
        for verb in ("stop", "continue"):
            self.assertEqual(self.post(f"/runs/nope/{verb}").status_code, 404)

    def test_a_running_run_is_stopped_from_another_machine(self):
        r = self.post("/runs", {"recipe": self.scripted("echo started; sleep 60")})
        run_id = r.json["id"]
        for _ in range(400):
            if "started" in "\n".join(self.get(f"/runs/{run_id}").json["log"]): break
            time.sleep(0.02)
        s = self.post(f"/runs/{run_id}/stop", environ_base={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual((s.status_code, s.json["id"]), (200, run_id))
        self.assertIn(s.json["state"], ("stopping", "stopped"))
        self.wait()
        self.assertEqual(self.get(f"/runs/{run_id}").json["state"], "stopped")

    def test_a_write_leaves_its_token_s_name_in_the_server_s_log(self):
        """Who started, saved or stopped what: the token's name and id, the address that sent it, the request, its
        status and what it made. A read leaves no line, nor does a token the server does not know."""
        self.assertTrue(web.app.logger.isEnabledFor(logging.INFO))              # else a served application prints none of them
        self.stack.enter_context(patch.object(execution.Execution, "_main", lambda ex: None))
        with self.assertLogs(web.app.logger, "INFO") as logs:
            run = self.post("/runs", {"recipe": gsm8k(), "confirm": True}, environ_base={"REMOTE_ADDR": "10.0.0.7"}).json
            saved = self.post("/recipes", {"recipe": gsm8k(title="Nightly")}).json
            self.post("/runs", {"recipe": gsm8k()})                                 # the whole dataset: asked first
            self.post("/runs", token="reader")
            self.post("/runs/a%0Ab/stop")                                           # a line break in a name stays out of the log
            self.get("/runs"); self.get("/jobs", token="runner"); self.post("/runs", token="nls_000000000000_" + "x" * 43)
        runner, reader = self.runner_record["id"], self.reader.split("_")[1]
        self.assertEqual([r.getMessage() for r in logs.records], [
            f"token 'runner' ({runner}) from 10.0.0.7: POST /api/v1/runs 200: {run['id']}",
            f"token 'runner' ({runner}) from 127.0.0.1: POST /api/v1/recipes 200: {saved['id']}",
            f"token 'runner' ({runner}) from 127.0.0.1: POST /api/v1/runs 409",
            f"token 'reader' ({reader}) from 127.0.0.1: POST /api/v1/runs 403",
            f"token 'runner' ({runner}) from 127.0.0.1: POST /api/v1/runs/a%0Ab/stop 404",
        ])
        for token in (self.runner, self.reader):
            self.assertNotIn(token.split("_", 2)[2], "\n".join(logs.output))

    def test_a_harbor_recipe_is_started_from_the_builder(self):
        """Its compiler is the builder's page: over the API it is refused with where to start it, checked or started."""
        harbor = json.loads((FIXTURES / "recipes" / "harbor-swe-bench-release-pin.json").read_text())
        for recipe in (harbor, {"version": 1, "name": "h"}, {}):                  # a recipe naming no runner is Harbor's
            for path in ("/runs/check", "/runs"):
                r = self.post(path, {"recipe": recipe, "confirm": True})
                self.assertEqual((r.status_code, r.json), (400, {"error": api.STANDARD_ONLY}), (path, recipe.get("name")))
        self.assertEqual(execution.EXECUTIONS, {})


class OverASocket(Case):
    """The client, the command and the MCP tools against the application itself, served on the loopback."""

    def setUp(self):
        super().setUp()
        log = logging.getLogger("werkzeug"); level = log.level
        log.setLevel(logging.ERROR); self.addCleanup(log.setLevel, level)        # one line a request otherwise
        self.httpd = make_server("127.0.0.1", 0, web.app, threaded=True)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.httpd.server_close); self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.nx = client.Client(self.url, self.reader)

    def test_the_client_reads_what_the_routes_answer(self):
        self.assertEqual(self.nx.info()["token"]["scope"], "read")
        jobs = self.nx.jobs()
        self.assertEqual(jobs, self.get("/jobs").json)
        job = self.nx.job("oracle-a")
        self.assertEqual(job, self.get("/jobs/oracle-a").json)
        trial = job["trials"][0]["trial_name"]
        self.assertEqual(self.nx.trial("oracle-a", trial)["trial"]["trial_name"], trial)
        self.assertEqual(json.loads(self.nx.artifact("oracle-a", trial, "result")["text"])["trial_name"], trial)
        self.assertEqual(self.nx.compare("oracle-a", "oracle-b", matched=True), self.get("/compare?job=oracle-a&job=oracle-b&matched=1").json)
        self.assertEqual([b["key"] for b in self.nx.catalogue()["benchmarks"]], [b["key"] for b in self.get("/catalogue").json["benchmarks"]])

    def test_the_client_s_refusals_are_the_server_s(self):
        for call, status, said in ((lambda: self.nx.job("nope"), 404, "no job 'nope' in this workspace"),
                                   (lambda: self.nx.check(gsm8k()), 403, "This token's scope is read: this needs run."),
                                   (lambda: client.Client(self.url, "not-a-token").jobs(), 401, None)):
            with self.assertRaises(client.ApiError) as c:
                call()
            self.assertEqual(c.exception.status, status)
            if said:
                self.assertEqual(str(c.exception), said)

    def test_a_recipe_is_checked_started_followed_and_read_back(self):
        runs = Runs("test_a_run_is_followed_listed_and_stopped_by_its_id")
        runs.stack, runs.adopted = self.stack, []
        recipe = Runs.scripted(runs, "echo one; echo two")
        self.stack.enter_context(patch.object(web.shared, "adopt_runs", lambda started: None))
        self.stack.enter_context(patch.object(execution, "docker_here", lambda sh, needs="", program="docker": None))
        nx = client.Client(self.url, self.runner)
        nx.save_recipe(dict(recipe, title="Nightly"))
        self.assertEqual((nx.check("Nightly")["ready"], nx.check("Nightly")["confirm"]), (True, ""))
        run = nx.start("Nightly")["runs"][0]
        lines = list(nx.follow(run["id"], every=0.05))
        self.assertEqual([l for l in lines if l in ("one", "two")], ["one", "two"])
        ended = nx.wait(run["id"], every=0.05, timeout=10)
        self.assertEqual((ended["state"], ended["job_ids"]), ("done", [run["name"]]))
        self.assertEqual([r["id"] for r in nx.runs()], [run["id"]])

    def test_the_command_and_the_mcp_tools_ask_the_same_server(self):
        out = io.StringIO()
        with patch.dict(os.environ, {"NULLIUS_SERVER": self.url, "NULLIUS_TOKEN": self.reader}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["remote", "job", "oracle-a"]), 0)
        self.assertEqual(json.loads(out.getvalue()), self.get("/jobs/oracle-a").json)
        call = lambda name, args: mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, self.nx)["result"]
        got = call("get_job", {"job_id": "oracle-a"})
        self.assertEqual((got["isError"], json.loads(got["content"][0]["text"])), (False, self.get("/jobs/oracle-a").json))
        refused = call("start_run", {"recipe": gsm8k(), "confirm": True})          # a read token starts nothing, by any door
        self.assertEqual((refused["isError"], json.loads(refused["content"][0]["text"])["error"]), (True, "This token's scope is read: this needs run."))


if __name__ == "__main__":
    unittest.main()
