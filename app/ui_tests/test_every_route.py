"""Every GET route in the app's own url_map answers 200 over the fixtures.

The rule list comes from ``app.url_map`` rather than a list written here, so a
route added later is covered without anyone remembering to add it. Arguments
are filled from the jobs and trials the fixture workspace actually loads. A
rule that cannot be filled is never dropped: it is reported as a skipped
sub-test with the argument it lacks, and a rule whose arguments only mean
something inside a selection is driven below as a workflow instead.

The workspace is opened through ``open_workspace`` on a copy of the recorded
Harbor jobs, under a temporary workspace home: the live-board workflow writes
a feed into a job directory, and that must never be a fixture's. No request
leaves the process: ``urllib.request.urlopen`` refuses for the whole class, and
so does the program a runner's versions are listed with where that is ``git``
(``nullius.gitrefs``, when present, is pointed at one that does not exist).
What the runner-versions route exercises is therefore the offline answer - the
tested version, and the reason the rest is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.catalogue import catalog, harbor_entry
from nullius.traces import trace as trace_mod

try:                    # a repository's refs listed by `git ls-remote`, where that has landed
    from nullius import gitrefs
except ImportError:
    gitrefs = None

from .test_routes import web

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
JOB = FIXTURES / "harbor-jobs" / "liveboard-oracle-x4"     # a hand-made job, copied twice: the comparison routes need two
FEED = FIXTURES / "live" / "events.jsonl"

# Rules driven under a workflow instead of one-shot, because their arguments
# only mean something inside a selection or a running job.
WORKFLOW_ONLY = {
    "/compare/task/<path:task>": "driven under the comparison workflow with a real two-run cohort",
    "/job/<job_id>/live": "driven under the live-board workflow with a job carrying a feed",
}

# Rules that answer 404 without a query argument by design - the argument names
# a record inside the trial, not a page - so they are asked with a real one.
NEEDS_SPAN = ("/trial/<job_id>/<trial_name>/trace-span",)

# The Harbor source routes answer 400 without the source they are asked
# about; asked with one, offline: refs with git unreachable (NO_GIT), the
# agents of a release pin from the cache only (a GET never builds).
HARBOR_QUERY = {"/new-run/harbor/refs": "repo=example/harbor",
                "/new-run/harbor/agents": "kind=release&version=0.24.0"}
NO_GIT = "/nonexistent/nullius-route-test/git"


def offline(*args, **kwargs):
    raise urllib.error.URLError("the route test opens no connection")


class EveryRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        tmp = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-every-route-")))
        cls.jobs_root = tmp / "harbor-jobs"
        for name in ("oracle-a", "oracle-b"):
            shutil.copytree(JOB, cls.jobs_root / name)
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "",
                                                              "job_roots": []}, clear=True))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(os.environ))
        os.environ.pop("TYPESAFE_API_KEY", None)
        cls.stack.enter_context(patch.object(urllib.request, "urlopen", offline))
        if gitrefs is not None:
            cls.stack.enter_context(patch.object(gitrefs, "GIT", NO_GIT))
        roots = [str(cls.jobs_root)]
        ws = workspace.create(web.shared.WS_HOME, roots, "every-route")
        workspace.save(ws)
        web.shared.open_workspace(ws)
        cls.ws_name = ws.name
        cls.client = web.app.test_client()
        cls.values = cls.fixture_args()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    @classmethod
    def fixture_args(cls) -> dict:
        """Values for the rule arguments, taken from what actually parsed."""
        jobs = web.shared._load_jobs()
        args: dict = {}
        if jobs:
            job = jobs[0]
            args["job_id"] = job.job_id
            trials = [t for t in job.trials if t.has_trajectory] or list(job.trials)
            if trials:
                args["trial_name"] = trials[0].trial_name
                args["key"] = "result"
            args["span"] = cls.a_span_id(job)
        return args

    @staticmethod
    def a_span_id(job) -> str:
        """A span id that really exists in a fixture trace, or "" if none does."""
        for trial in job.trials:
            if not trial.has_trace:
                continue
            loaded = trace_mod.load(trial.trace_path)
            for span in trace_mod.all_spans(loaded.roots):
                if span.span_id:
                    return span.span_id
        return ""

    def get(self, url):
        resp = self.client.get(url)
        self.addCleanup(resp.close)
        return resp

    def test_the_fixture_workspace_loads_jobs(self):
        self.assertIn("job_id", self.values, "no job parsed from the fixtures: every job route would go unexercised")
        self.assertIn("trial_name", self.values)

    def test_every_get_rule_answers(self):
        attempted = 0
        for rule in sorted(web.app.url_map.iter_rules(), key=str):
            if "GET" not in (rule.methods or set()) or rule.endpoint == "static":
                continue
            raw = str(rule)
            with self.subTest(rule=raw):
                if raw in WORKFLOW_ONLY:
                    continue
                missing = [a for a in rule.arguments if a not in self.values]
                if missing:
                    self.skipTest(f"no fixture value for {missing}")
                local = dict(self.values)
                # `/benchmarks/<key>` shares it with the artifact route but means a benchmark
                if raw == "/benchmarks/<key>":
                    local["key"] = catalog.load().listed_benchmarks[0].key
                # an agent's or a capability's page is the standard's definition of that key
                if raw in ("/agents/<key>", "/capabilities/<key>", "/features/<key>"):
                    from nullius.standard import definitions
                    local["key"] = definitions.load("agent" if raw.startswith("/agents") else "capability")[0][0]["key"]
                if raw == "/kits/<key>":
                    from nullius.standard import definitions
                    local["key"] = definitions.kits()[0]["key"]
                # its edit form is for a Harbor entry on the hub or in the registry
                if raw == "/benchmarks/<key>/edit":
                    editable = [b.key for b in catalog.load().listed_benchmarks
                                if not harbor_entry.not_editable(harbor_entry.entry_of(b.key))]
                    if not editable:                       # every shipped Harbor benchmark runs on the standard too
                        self.skipTest("no Harbor-only entry in the catalogue to edit")
                    local["key"] = editable[0]
                url = rule.build(local, append_unknown=False)[1] if rule.arguments else raw
                if raw in NEEDS_SPAN:
                    if not self.values.get("span"):
                        self.skipTest("no span id in the fixture traces")
                    url = f"{url}?span={self.values['span']}"
                if raw in HARBOR_QUERY:
                    url = f"{url}?{HARBOR_QUERY[raw]}"
                resp = self.get(url)
                attempted += 1
                if raw == "/":
                    self.assertEqual(resp.status_code, 302, url)
                    self.assertEqual(resp.location, "/runs")
                else:
                    self.assertEqual(resp.status_code, 200, url)
        self.assertGreater(attempted, 0)

    def test_the_trial_tabs_answer(self):
        """The trial page's tabs are query-string views of one rule, and each
        renders a different half of the data layer, so each is asked by name."""
        base = f"/trial/{self.values['job_id']}/{self.values['trial_name']}"
        for tab in ("conversation", "trace", "artifacts", "behavior"):
            with self.subTest(tab=tab):
                self.assertEqual(self.get(f"{base}?tab={tab}").status_code, 200)

    def test_the_comparison_workflow_answers(self):
        """A selection is `<workspace>:<job_id>`, so a cohort cannot come from
        the url_map alone. Both fixture jobs run the same task, which is what
        makes the matched-task view reachable at all."""
        jobs = web.shared._load_jobs()
        ids = sorted({j.job_id for j in jobs})[:2]
        self.assertEqual(len(ids), 2, "fewer than two jobs in the fixtures")
        picks = "&".join(f"pick={self.ws_name}:{i}" for i in ids)
        task = next((t.task_base for j in jobs for t in j.trials if t.task_base), None)
        self.assertTrue(task, "no task shared by the fixture jobs")
        for url in (f"/compare?{picks}", f"/compare/pick?{picks}",
                    f"/compare/task/{task}?{picks}"):
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 200)

    def test_the_batch_judge_routes_judge_on_a_post(self):
        """A batch button's click is the request: the POST asks the judge
        about everything unjudged, and a GET is not answered - there is no
        estimate to fetch. The judge is stubbed, so nothing leaves; the oracle
        fixtures carry no trajectory or instruction, so the tasks and the
        comparison sides to ask are given here."""
        judge = web.judge
        jobs = web.shared._load_jobs()
        job = jobs[0]
        picks = "&".join(f"pick={self.ws_name}:{i}" for i in sorted({j.job_id for j in jobs})[:2])
        asked = []
        stub = lambda *a, **k: asked.append(a) or {}
        with patch.object(judge, "judge_trial", stub), patch.object(judge, "judge_side", stub), \
             patch.object(judge, "judge_task", stub), patch.object(judge, "judge_control", stub), \
             patch.object(judge, "tasks_todo", lambda j: list(j.trials)), \
             patch.object(judge, "_sides_todo", lambda view: [(t.task_base, job, t) for t in job.trials]), \
             patch.dict(os.environ, TYPESAFE_API_KEY="ts-fake"):
            with self.client.session_transaction() as sess:
                sess["csrf"] = "route-token"
            for url, todo in ((f"/judge/job/{job.job_id}", [t for t in job.trials if t.has_trajectory]),
                              (f"/judge/tasks/{job.job_id}", job.trials),
                              (f"/judge/feasibility/{job.job_id}", None),
                              (f"/judge/compare?{picks}", job.trials)):
                with self.subTest(url=url):
                    self.assertEqual(self.get(url).status_code, 405)
                    before = len(asked)
                    resp = self.client.post(url, data={"csrf": "route-token"})
                    self.addCleanup(resp.close)
                    self.assertEqual(resp.status_code, 302)
                    if todo is not None:
                        self.assertEqual(len(asked) - before, len(todo))
                    refused = self.client.post(url, data={"csrf": "stale"})
                    self.addCleanup(refused.close)
                    self.assertEqual(refused.status_code, 403)
        self.assertTrue(asked, "no batch route asked the judge anything")

    def test_the_job_page_offers_each_judge_its_own_button(self):
        """System One and the LLM judge judge apart, so the job page has a
        button for each, and the one a post names is the one asked."""
        judge = web.judge
        job = web.shared._load_jobs()[0]                         # the fixtures are controls: read as an agent's job here
        self.enterContext(patch.object(web.runs.feasibility, "summary_of", lambda j: None))
        env = dict(TYPESAFE_API_KEY="ts-fake", OPENROUTER_API_KEY="sk-or-fake", LLM_JUDGE_PROVIDER="openrouter", LLM_JUDGE_MODEL="vendor/judge-1")
        with patch.dict(os.environ, env):
            page = self.get(f"/job/{job.job_id}").get_data(as_text=True)
            for lane in ("system-one", "llm"):
                self.assertIn(f'id="judge-all-{lane}"', page)
            asked = []
            with patch.object(judge, "judge_trial", lambda j, t, k: asked.append(k.lane) or {}):
                with self.client.session_transaction() as sess:
                    sess["csrf"] = "route-token"
                resp = self.client.post(f"/judge/job/{job.job_id}", data={"csrf": "route-token", "lane": "llm"})
                self.addCleanup(resp.close)
            self.assertEqual(set(asked), {"llm"} if asked else set())
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-fake"}):
            page = self.get(f"/job/{job.job_id}").get_data(as_text=True)
            self.assertIn("LLM judge · set up", page)

    def test_the_live_board_workflow_answers(self):
        """`/job/<id>/live` answers 404 for a job with no feed by design, so it is
        walked the way a user meets it: a job carrying the recorded feed, its
        page showing the board, and the poll answering that it runs."""
        target = web.shared._load_jobs()[0]
        feed = os.path.join(target.dir, "events.jsonl")
        self.assertTrue(feed.startswith(str(self.jobs_root)), "the feed must land in the copy, never a fixture")
        shutil.copy(FEED, feed)
        self.addCleanup(os.remove, feed)
        page = self.get(f"/job/{target.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="live-board"', page.get_data(as_text=True))
        poll = self.get(f"/job/{target.job_id}/live")
        self.assertEqual(poll.status_code, 200)
        self.assertIs(json.loads(poll.get_data(as_text=True)).get("running"), True)


if __name__ == "__main__":
    unittest.main()
