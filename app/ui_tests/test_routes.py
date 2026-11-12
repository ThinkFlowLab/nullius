"""Exercise public UI routes against temporary, synthetic benchmark records.

Run separately from app/tests (which deliberately has no Flask dependency):
    uv run --offline --with-requirements app/requirements.txt python -m unittest discover -s app/ui_tests -t app

No real workspace is opened, refreshed, converted, or written.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from nullius.runs import parse
from nullius import workspace
from nullius.catalogue import catalog



APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
spec = importlib.util.spec_from_file_location("nullius_ui_route_test_app", APP_DIR / "app.py")
web = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = web
spec.loader.exec_module(web)


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.text_by_id = {}
        self.ancestors = {}
        self._open = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if attrs.get("id"):
            self.text_by_id[attrs["id"]] = ""
            self.ancestors[attrs["id"]] = tuple(a["id"] for _, a in self._open if a.get("id"))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._open.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break

    def handle_data(self, data):
        for _, attrs in self._open:
            if attrs.get("id"):
                self.text_by_id[attrs["id"]] += data


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def synthetic_job(root, job_id):
    """A hundred large source messages plus a large independent trace payload."""
    job_dir = root / job_id
    trial_dir = job_dir / "task-001"
    write_json(job_dir / "result.json", {"stats": {"n_trials": 1}})
    write_json(job_dir / "config.json", {
        "agents": [{"name": "fixture-agent", "model_name": "fixture-model"}],
        "datasets": [{"name": "terminal-bench", "n_tasks": 1}],
    })
    write_json(trial_dir / "result.json", {
        "task_name": "terminal-bench/task-001", "trial_name": "task-001",
        "source": "terminal-bench", "verifier_result": {"rewards": {"reward": 1}},
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 50},
    })
    steps = [{"step_id": i * 3 + 2, "source": "agent", "message":
              f"source-message-{i}: " + "x" * 20_000 +
              (" late-search-needle" if i == 99 else "")}
             for i in range(100)]
    write_json(trial_dir / "agent" / "trajectory.json", {"steps": steps})
    write_json(trial_dir / "agent" / "trace.json", {
        "schema_version": "harbor-trace-v1", "spans": [{
            "span_id": "session-fixture", "name": "session", "kind": "session",
            "start_ns": 1_000_000_000, "duration_ns": 3_000_000_000,
            "timing_quality": "measured", "source": "fixture", "children": [{
                "span_id": "tool-fixture", "name": "Bash", "kind": "tool",
                "start_ns": 2_000_000_000, "duration_ns": 1_000_000_000,
                "timing_quality": "inferred", "source": "fixture",
                "content": {"result": "trace-payload-marker " + "z" * 100_000},
                "children": [],
            }],
        }],
    })
    (trial_dir / "trial.log").write_text(
        "x" * 7_995 + " sk-ui-secret123456789abcdef log-tail", encoding="utf-8")
    return parse.parse_job(str(job_dir))


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-ui-routes-")
        cls.root = Path(cls.temp.name)
        cls.active_job = synthetic_job(cls.root, "active-run")
        cls.foreign_job = synthetic_job(cls.root, "foreign-run")
        cls.active = workspace.Workspace("active", "Active workspace", ["/fixture/active"],
            "2026-01-01", "2026-01-01", str(cls.root / "active-workspace"), ["/indexed/active"])
        cls.foreign = workspace.Workspace("foreign", "Foreign workspace", ["/fixture/foreign"],
            "2026-01-01", "2026-01-01", str(cls.root / "foreign-workspace"), ["/indexed/foreign"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {
            "workspace": self.active, "roots": list(self.active.roots),
            "job_roots": list(self.active.job_roots), "label": self.active.label,
        }, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.root / "workspace-store")))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.active, self.foreign]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name:
            {"active": self.active, "foreign": self.foreign}.get(name)))
        self.load_jobs = self.stack.enter_context(patch.object(workspace, "load_jobs",
            side_effect=lambda ws, roots: [self.foreign_job if ws.name == "foreign" else self.active_job]))
        for obj, name in [(web.shared, "open_workspace"), (web.shared, "_import_roots"),
                          (workspace, "save"), (workspace, "touch")]:
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(
                f"Read-only UI route unexpectedly called {name}")))
        self.client = web.app.test_client()

    def get_ok(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, path)
        return response

    def test_home_opens_runs_with_or_without_active_workspace(self):
        for active in (self.active, None):
            with self.subTest(active=bool(active)):
                web.shared.STATE["workspace"] = active
                response = self.client.get("/")
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/runs")
                self.assertIs(web.shared.STATE["workspace"], active)
                html = self.get_ok("/", follow_redirects=True).get_data(as_text=True)
                self.assertIn("<h1>Runs</h1>", html)
                self.assertIn("active-run" if active else "Build a run", html)
                self.assertIn('aria-label="Pages"', html)
                self.assertNotIn("So who's actually best?", html)

    def test_command_builder_serves_catalogue_and_adapter_metadata(self):
        html = self.get_ok("/new-run").get_data(as_text=True)
        self.assertIn('id="nullius-builder"', html)
        self.assertNotIn('id="cred-key"', html)
        encoded = re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S)
        data = json.loads(encoded.group(1))
        self.assertEqual(data["version"], 1)
        self.assertIn("tau3-bench", [d["key"] for d in data["datasets"]])
        self.assertEqual(["mcp_timeout_s"], [p["key"] for p in data["parameters"]])
        for asset in ("command-builder.css", "command-builder.js", "command-builder-core.js"):
            self.get_ok("/static/builder/" + asset)

    def test_runs_lists_the_workspace_or_offers_build_and_import(self):
        html = self.get_ok("/runs").get_data(as_text=True)
        self.assertIn("<h1>Runs</h1>", html)
        self.assertIn("active-run", html)
        # the Columns menu lists every column of the table - it rendered empty
        # once, when the context key and the template's name disagreed
        for col, label in web.shared.COLUMNS:
            self.assertIn(f'data-col="{col}"> {label}</label>', html)
        self.assertNotIn('id="landing-title"', html)
        web.shared.STATE["workspace"] = None
        html = self.get_ok("/runs").get_data(as_text=True)
        # no workspace: build a run, or bring runs from elsewhere into a new one, or open one already here
        self.assertIn("Build a run", html)
        self.assertIn('href="/new-run"', html)
        self.assertIn("Import results", html)
        self.assertIn("Open run builder", html)
        self.assertNotIn("Harbor jobs", html)
        self.assertIn('class="start-ascii"', html)
        self.assertIn('href="/import?new=1"', html)                       # dropped folders, a name: the picker
        self.assertIn("Or open a workspace", html)
        self.assertLess(html.index("Or open a workspace"), html.index('class="start-signature"'))   # the mark closes the page
        self.assertNotIn('data-label="Import"', html)                     # the rail has no Import tab

    def test_removed_marketing_assets_are_not_served(self):
        for asset in ("landing.css", "landing-core.js", "landing-logos.js", "landing.js"):
            with self.subTest(asset=asset):
                response = self.client.get("/static/runs/" + asset)
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 404)

    def test_catalogue_routes_keep_native_anchor_targets_and_source_count_meanings(self):
        cat = catalog.load()
        html = self.get_ok("/benchmarks").get_data(as_text=True)
        # Every listed benchmark is a card opening its own page, and every
        # dataset is anchored on that page by its own key.
        for b in cat.listed_benchmarks:
            self.assertIn('href="/benchmarks/%s"' % b.key, html)
            ids = {attrs.get("id") for tag, attrs in Elements(self.get_ok("/benchmarks/%s" % b.key).get_data(as_text=True)).elements}
            for key in b.datasets:
                with self.subTest(benchmark=b.key, dataset=key):
                    self.assertIn(key, ids)
        self.assertIn("Official", html)
        self.assertEqual('data-shelf="community"' in html, any(b.origin == "community" for b in cat.listed_benchmarks))   # a shelf only when something is on it
        self.assertIn("<b>500</b><span>tasks</span>", html)
        # Datasets belong on their benchmark's page.
        response = self.client.get("/datasets")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('href="/datasets', html)
        self.assertNotIn('data-tree="nav-data"', html)
        self.get_ok("/static/catalogue/catalogue.js")

    def test_run_task_labels_remove_only_unambiguous_dataset_prefixes(self):
        job = copy.deepcopy(self.active_job)
        job.dataset = "/datasets/tau3-bench@1.0"
        names = ["tau3-bench__tau3-airline-0", "django__django-10554",
                 "tau3-bench__same", "same", "tau3-bench__"]
        job.trials = [copy.copy(self.active_job.trials[0]) for _ in names]
        for index, (trial, name) in enumerate(zip(job.trials, names)):
            trial.trial_name = f"trial-{index}"
            trial.task_base = name
        self.assertEqual(list(web.shared.task_labels_for(job).values()),
                         ["tau3-airline-0", "django__django-10554", "tau3-bench__same", "same", "tau3-bench__"])
        self.assertEqual([t.task_base for t in job.trials], names)
        job.dataset = "mixed"
        self.assertEqual(list(web.shared.task_labels_for(job).values()), names)

    def test_run_trial_table_uses_recorded_totals_and_keeps_missing_usage_distinct(self):
        job = copy.deepcopy(self.active_job)
        job.dataset = "tau3-bench"
        trial = job.trials[0]
        trial.task_base = "tau3-bench__tau3-airline-0"
        trial.task_name = "sierra-research/tau3-bench__tau3-airline-0"
        trial.dur_env, trial.dur_agent, trial.dur_verifier = 1, 2, 3
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            for agent, duration, usage, total, tokens in (
                ("fixture-agent", 135, (1000, 500, 900), "2.2m", "1.5k"),
                ("fixture-agent", None, (0, 0, 0), "—", "—"),
                ("oracle", 0, (0, 0, 0), "0.0s", "0"),
            ):
                with self.subTest(agent=agent, duration=duration):
                    job.agent_name = agent
                    trial.dur_total = duration
                    trial.n_input_tokens, trial.n_output_tokens, trial.n_cache_tokens = usage
                    html = self.get_ok("/job/active-run").get_data(as_text=True)
                    self.assertIn('title="sierra-research/tau3-bench__tau3-airline-0">tau3-airline-0</a>', html)
                    self.assertRegex(html, rf'class="num tot run-total-time"><span class="mg"[^>]*>{re.escape(total)}</span></td>')
                    self.assertRegex(html, rf'class="num run-token-count"><span class="mg"[^>]*>{re.escape(tokens)}</span></td>')
                    self.assertIn('href="/trial/active-run/task-001"', html)
                    self.assertIn('role="region" aria-label="Trials in this run"', html)
                    self.assertIn('>Total</a></th>', html)
                    self.assertIn('>Tokens</a></th>', html)
                    if usage[0]:
                        self.assertIn('Input: 1,000 · Output: 500 · Cached input: 900 (included in input)', html)

    def test_conversation_returns_a_bounded_page_with_full_text_search_reachable(self):
        endpoint = "/trial/active-run/task-001/conversation"
        response = self.get_ok(endpoint)
        data = response.get_json()
        self.assertEqual(data["total"], 100)
        self.assertLessEqual(len(data["rows"]), 12)
        self.assertLess(len(response.data), 16_000)
        self.assertNotIn(b"source-message-99", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        found = self.get_ok(endpoint, query_string={"q": "late-search-needle"}).get_json()
        self.assertEqual(found["matches"], 1)
        self.assertEqual(found["selected"]["step_id"], 299)
        tail = self.get_ok(endpoint, query_string={"q": "late-search-needle", "chunk": 999}).get_json()
        self.assertIn("late-search-needle", tail["selected"]["content"]["text"])
        last_page = self.get_ok(endpoint, query_string={"page": 9}).get_json()
        self.assertEqual(last_page["rows"][-1]["step_id"], 299)

    def test_a_trial_has_two_judgements_in_two_tabs(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": "sk-or-route-test", "LLM_JUDGE_MODEL": ""}):
            html = self.get_ok("/trial/active-run/task-001").get_data(as_text=True)
        self.assertIn('id="tab-judgement" role="tab" type="button" data-tab="judgement"', html); self.assertIn(">System One judge</button>", html)
        self.assertIn('id="tab-llm-judge"', html); self.assertIn(">LLM judge</button>", html)
        self.assertIn('id="judgement"', html); self.assertIn('id="llm-judgement"', html)
        self.assertIn("LLM judge not configured: needs a model", html)                     # the OpenRouter key is set, the model is not
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-route-test", "LLM_JUDGE_MODEL": "vendor/m"}):
            html = self.get_ok("/trial/active-run/task-001").get_data(as_text=True)
        self.assertIn('<input type="hidden" name="lane" value="llm">', html)
        self.assertIn("asked of OpenRouter · vendor/m", html)
        self.assertNotIn("sk-or-route-test", html)

    def test_trial_html_does_not_inline_large_conversation_or_trace_payloads(self):
        response = self.get_ok("/trial/active-run/task-001")
        self.assertLess(len(response.data), 250_000)
        self.assertNotIn(b"source-message-99", response.data)
        self.assertNotIn(b"trace-payload-marker", response.data)
        html = response.get_data(as_text=True)
        self.assertIn('id="conversation-reader"', html)
        self.assertIn("/conversation", html)
        self.assertIn("/trace-span", html)

    def test_trace_detail_uses_visible_spans_and_rejects_missing_or_hidden_records(self):
        endpoint = "/trial/active-run/task-001/trace-span"
        visible = self.get_ok(endpoint, query_string={"span": "tool-fixture"}).get_json()
        self.assertEqual(visible["timing_quality"], "inferred")
        self.assertEqual(visible["duration_ns"], 1_000_000_000)
        self.assertIn("trace-payload-marker", visible["content"]["text"])
        self.assertLess(len(json.dumps(visible)), 8_000)
        for query in ({"span": "missing"}, {"span": "tool-fixture", "hide": "Bash"},
                      {"span": "tool-fixture", "hide_span": "tool-fixture"},
                      {"span": "tool-fixture", "trace": "not-present.json"}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(endpoint, query_string=query).status_code, 404)

    def test_artifact_pages_redact_before_chunking_and_do_not_modify_sources(self):
        path = Path(self.active_job.trials[0].dir) / "trial.log"
        before = path.read_bytes()
        chunks = []
        for page in (1, 2):
            response = self.get_ok("/trial/active-run/task-001/artifact/trial-log", query_string={"page": page})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn(b"ui-secret123456789abcdef", response.data)
            content = re.search(r'<pre class="artifact-text"[^>]*>(.*?)</pre>', response.get_data(as_text=True), re.S)
            self.assertIsNotNone(content)
            chunks.append(unescape(content.group(1)))
        self.assertIn("[REDACTED]", "".join(chunks))
        self.assertEqual(path.read_bytes(), before)

    def test_artifact_allowlist_and_symlink_boundary_reject_arbitrary_files(self):
        endpoint = "/trial/active-run/task-001/artifact/"
        for key in ("trajectory", "not-a-record", "missing.txt"):
            self.assertEqual(self.client.get(endpoint + key).status_code, 404)
        outside = self.root / "outside.txt"
        outside.write_text("not part of the trial", encoding="utf-8")
        link = Path(self.active_job.trials[0].dir) / "agent" / "gateway.log"
        link.symlink_to(outside)
        self.addCleanup(link.unlink)
        self.assertEqual(self.client.get(endpoint + "gateway").status_code, 404)

    def test_every_mutation_takes_the_form_token(self):
        """A cross-site form or no-cors fetch is a simple request: without the
        token these would plant runs, add roots, write into a run tree or
        switch the open workspace from any page the user visits."""
        cross = {"Origin": "http://evil.example"}
        for path, data in [("/upload", {"path": "/tmp"}), ("/use-path", {"path": "/tmp"}),
                           ("/change-source", {}), ("/workspaces/open/foreign", {}),
                           ("/workspaces/refresh", {}),
                           ("/trial/active-run/task-001/save-trace", {"name": "view.json", "hide": "llm"})]:
            with self.subTest(path=path):
                r = self.client.post(path, data=data, headers=cross)
                self.addCleanup(r.close)
                self.assertEqual(r.status_code, 403)
        self.assertIs(web.shared.STATE["workspace"], self.active)
        html = self.get_ok("/runs").get_data(as_text=True)
        with self.client.session_transaction() as s:
            token = s["csrf"]
        self.assertIn(f'name="csrf" value="{token}"', html)              # the workspace menu's forms send it

    def test_foreign_workspace_evidence_does_not_change_active_workspace(self):
        before = copy.copy(web.shared.STATE)
        for suffix in ("", "/conversation", "/trace-span?span=tool-fixture", "/artifact/result"):
            separator = "&" if "?" in suffix else "?"
            response = self.get_ok("/trial/foreign-run/task-001" + suffix + separator + "workspace=foreign")
            if suffix == "":
                self.assertIn(b"workspace=foreign", response.data)
            self.assertEqual(web.shared.STATE, before)
        self.assertIs(web.shared.STATE["workspace"], self.active)
        self.assertEqual(self.client.get("/trial/foreign-run/task-001/conversation").status_code, 404)
        self.assertEqual(self.client.get("/trial/active-run/task-001/conversation?workspace=missing").status_code, 404)
        self.assertTrue(any(call.args[0] is self.foreign and call.args[1] == self.foreign.job_roots
                            for call in self.load_jobs.call_args_list))

    def test_a_rewardkit_verifier_scores_on_the_job_page(self):
        """Harbor's real liveboard-oracle-x4 tree: reward-kit-example's verifier
        writes verifier/reward.json and no reward.txt. The page reads the run
        as Harbor recorded it — oracle passed 4/4, every row Resolved —
        instead of dropping that trial to Unknown and 3/4. The run is a
        control, so its headline is the verdict, not a Solved figure."""
        tree = APP_DIR / "tests" / "fixtures" / "harbor-jobs" / "liveboard-oracle-x4"
        job = parse.parse_job(str(tree))
        self.assertEqual(job.n_resolved, 4)
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            html = self.get_ok("/job/liveboard-oracle-x4").get_data(as_text=True)
        hero = re.search(r'id="run-hero".*?</dl>', html, re.S).group(0)
        self.assertIn('<div class="lab">oracle · passed</div>', hero); self.assertIn('<div class="hero">4<small>/4</small></div>', hero)
        self.assertRegex(html, r'data-o="resolved" href="\?outcome=resolved"\s+data-tip="[^"]*"><i class="sw"></i><b>4</b><span>Resolved')
        self.assertNotIn('data-o="unknown"', html)
        # the mix bar's legend is the outcome rows right under it, and each segment is the same filter
        bar = re.search(r'<div class="mix run-mix".*?</div>\s*</div>\s*<div class="run-outcome-filters"', html, re.S)
        self.assertIsNotNone(bar); self.assertRegex(bar.group(0), r'<a class="mseg fx hl oc-resolved" style="flex:4" data-o="resolved" href="\?outcome=resolved"')
        self.assertNotIn('id="judgement"', html)                                                        # a control has nothing to reconcile: no lanes
        kit = next(t for t in job.trials if t.task_base.startswith("reward-kit-example"))
        self.assertEqual((kit.outcome, kit.reward), ("resolved", 1.0))
        # and the verifier's own files are readable on the trial page, redacted like the rest
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            for key, fragment in (("reward-json", '"reward": 1.0'), ("reward-details", '"aggregation"')):
                html = self.get_ok(f"/trial/liveboard-oracle-x4/{kit.trial_name}/artifact/{key}").get_data(as_text=True)
                self.assertIn(fragment, unescape(html), key)

    def test_an_unrecognised_exception_reads_unknown_with_its_type_shown(self):
        """A trial whose result.json records an exception the classifier
        has no side for is Unknown — counted on the job page, not infra — and
        the trial page names the type beside the label so a reader can place it."""
        job_dir = self.root / "crash-run"
        write_json(job_dir / "result.json", {"stats": {"n_trials": 2}})
        write_json(job_dir / "config.json", {
            "agents": [{"name": "fixture-agent", "model_name": "fixture-model"}],
            "datasets": [{"name": "terminal-bench", "n_tasks": 2}]})
        write_json(job_dir / "task-ok" / "result.json", {
            "task_name": "terminal-bench/task-ok", "trial_name": "task-ok",
            "source": "terminal-bench", "verifier_result": {"rewards": {"reward": 1}}})
        (job_dir / "task-ok" / "verifier").mkdir()
        (job_dir / "task-ok" / "verifier" / "reward.txt").write_text("1", encoding="utf-8")
        write_json(job_dir / "task-crash" / "result.json", {
            "task_name": "terminal-bench/task-crash", "trial_name": "task-crash",
            "source": "terminal-bench", "exception_info": {
                "exception_type": "SomethingNobodyHasWrittenYetError",
                "exception_message": "the reader fell over",
                "exception_traceback": "Traceback (most recent call last): ...",
                "occurred_at": "2026-09-17T12:00:00"}})
        job = parse.parse_job(str(job_dir))
        crash = next(t for t in job.trials if t.trial_name == "task-crash")
        self.assertEqual((crash.outcome, crash.is_infra), ("unknown", False))
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            page = self.get_ok("/job/crash-run").get_data(as_text=True)
            self.assertIn('<b>1/2</b>', re.search(r'<div class="meter".*?</div>', page, re.S).group(0))       # solved over the trials that ran
            self.assertRegex(page, r'data-o="unknown" href="\?outcome=unknown"\s+data-tip="[^"]*"><i class="sw"></i><b>1</b><span>Unknown')
            self.assertIn('<span class="oc oc-unknown"><i></i>Unknown</span><details class="exd"><summary><span class="tw"></span>SomethingNobodyHasWrittenYetError</summary><code>the reader fell over</code>', page)
            trial_page = self.get_ok("/trial/crash-run/task-crash").get_data(as_text=True)
        head = trial_page.split("<p class=\"subtitle\">", 1)[0]
        self.assertIn('<span class="tag oc-unknown">Unknown</span>', head)
        self.assertIn('class="mono faint exception-type"', head)
        self.assertIn('classify.py.">SomethingNobodyHasWrittenYetError</span>', head)
        self.assertNotIn("Agent error", trial_page)


    def test_retried_rate_limits_reach_the_job_table_and_the_trial_head(self):
        """A 429 the agent's SDK retried is in agent/gateway.log and nowhere
        else: the outcome is untouched. The job table marks the row,
        the trial head counts them, and a trial with no gateway log says the
        count is not recorded rather than showing a clean zero. The manifest
        Harbor writes beside them is listed on the Artifacts tab."""
        job_dir = self.root / "rate-limited-run"
        synthetic_job(self.root, "rate-limited-run")
        trial_dir = job_dir / "task-001"
        (trial_dir / "agent" / "gateway.log").write_text(
            '2026-09-13 17:45:53 | runner | default_trace_id | INFO | {"event_id": "55ad1191-b119-479d-830f-335d97bdb429"}\n'
            "2026-09-13 17:46:07 | httpx | INFO | HTTP Request: POST https://api.openai.com/v1/chat/completions \"HTTP/1.1 429 Too Many Requests\"\n"
            "2026-09-13 17:46:13 | llm | default_trace_id | WARNING | openai.RateLimitError: Error code: 429 - {'code': 'rate_limit_exceeded'}\n"
            "2026-09-13 17:46:19 | llm | default_trace_id | WARNING | upstream answered status_code=429, backing off\n",
            encoding="utf-8")
        write_json(trial_dir / "artifacts" / "manifest.json", [
            {"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts",
             "type": "directory", "status": "empty", "service": None}])
        job = parse.parse_job(str(job_dir))
        self.assertEqual(job.trials[0].n_rate_limited_retries, 3)
        self.assertFalse(job.trials[0].is_infra)   # survived: the outcome does not show them
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            html = self.get_ok("/job/rate-limited-run").get_data(as_text=True)
            self.assertIn('<span class="badge rate-limits" title="3 rate-limited calls retried by the agent', html)
            self.assertIn(">429 ×3</span>", html)
            html = self.get_ok("/trial/rate-limited-run/task-001").get_data(as_text=True)
            self.assertIn('<b class="warn">3 rate-limited calls retried</b>', html)
            self.assertIn("Artifact manifest", html)
            self.assertIn("/trial/rate-limited-run/task-001/artifact/manifest", html)
            manifest = self.get_ok("/trial/rate-limited-run/task-001/artifact/manifest").get_data(as_text=True)
            self.assertIn("artifacts/logs/artifacts", unescape(manifest))
        # the synthetic job writes no gateway log: unmeasured, not zero
        self.assertIsNone(self.active_job.trials[0].n_rate_limited_retries)
        html = self.get_ok("/job/active-run").get_data(as_text=True)
        self.assertNotIn("rate-limits", html)
        html = self.get_ok("/trial/active-run/task-001").get_data(as_text=True)
        self.assertIn("Rate limits · not recorded", html)
        self.assertNotIn("rate-limited call", html)


if __name__ == "__main__":
    unittest.main()
