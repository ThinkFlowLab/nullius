"""The live board on the job page: the route, the page, and the renderer's contract.

Three things, each in the pattern this suite already uses. The `/job/<id>/live`
route is exercised against a synthetic job directory carrying the live
fixture's `events.jsonl`. The job page is checked for its two views — Live open
while the feed says the job is running, Results open once it says `job.end` (Live
still there, a click away), and no views at all for a job with no feed. And the renderer is executed under Node against
the fixture's `expected.json`, the way the run builder's core is, so the
strings a user reads are asserted rather than described: "not reported" for a
null cost, an absent counter absent, the redacted line redacted.
"""

from __future__ import annotations

import importlib.util
import json
import re
from html import escape, unescape
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from nullius.runs import parse
from nullius import workspace

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
spec = importlib.util.spec_from_file_location("nullius_ui_live_test_app", APP_DIR / "app.py")
web = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = web
spec.loader.exec_module(web)

FIXTURE = APP_DIR / "tests" / "fixtures" / "live"
NATIVE = APP_DIR / "tests" / "fixtures" / "native"     # job trees with one trial per upstream question
NOW = "2026-09-12T18:29:20Z"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def visible(html):
    """What a user reads of a rendered fragment: tags dropped, entities
    resolved, whitespace collapsed — so a test can quote the board's text."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def job_with_feed(root, job_id, *, feed=True, ended=False):
    """A minimal Harbor-shaped job directory, still running, with the fixture feed."""
    job_dir = root / job_id
    write_json(job_dir / "result.json", {"stats": {"n_trials": 3, "n_running_trials": 1, "n_pending_trials": 0}})
    write_json(job_dir / "config.json", {"agents": [{"name": "my-agent", "model_name": "m"}],
                                          "datasets": [{"name": "tau3-bench", "n_tasks": 3}]})
    if feed:
        shutil.copy(FIXTURE / "events.jsonl", job_dir / "events.jsonl")
        if ended:
            # The fixture feed ends with a deliberately torn line. Ending the job
            # means replacing that torn tail with a job.end, not appending after it —
            # a completed malformed line would be a broken writer, and is refused.
            text = (job_dir / "events.jsonl").read_text(encoding="utf-8")
            complete = text[: text.rfind("\n") + 1]
            (job_dir / "events.jsonl").write_text(
                complete + '{"ts":"2026-09-12T18:40:00Z","seq":900,"job":"demo-x3","trial":null,"event":"job.end","wall_sec":840}\n',
                encoding="utf-8")
    return parse.parse_job(str(job_dir))


class LiveBoard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-ui-live-")
        cls.root = Path(cls.temp.name)
        cls.running = job_with_feed(cls.root, "running-run")
        cls.ended = job_with_feed(cls.root, "ended-run", ended=True)
        cls.no_feed = job_with_feed(cls.root, "old-run", feed=False)
        cls.ws = workspace.Workspace("active", "Active", ["/fixture"], "2026-01-01", "2026-01-01",
                                         str(cls.root / "ws"), ["/indexed"])
        cls.node = shutil.which("node")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws, "roots": list(self.ws.roots),
                                                        "job_roots": list(self.ws.job_roots), "label": self.ws.label}, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.root / "store")))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.ws]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name: self.ws))
        self.stack.enter_context(patch.object(workspace, "load_jobs",
                                              side_effect=lambda ws, roots: [self.running, self.ended, self.no_feed]))
        for obj, name in [(web.shared, "open_workspace"), (web.shared, "_import_roots"), (workspace, "save"), (workspace, "touch")]:
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(f"read-only route called {name}")))
        self.client = web.app.test_client()

    def test_live_route_folds_the_feed_fresh_and_never_caches(self):
        r = self.client.get("/job/running-run/live"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        s = r.get_json()
        self.assertTrue(s["running"])
        self.assertEqual(s["counts"]["total"], 3)
        self.assertIsNone(s["totals"]["cost_usd"])           # null, never 0
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234567890", json.dumps(s))   # the fixture's key, scrubbed on read
        self.assertIn("[REDACTED]", json.dumps(s))
        # the clock is the request's, so the board moves between polls
        self.assertNotEqual(s["now"], NOW)

    def test_a_run_the_runner_ended_reads_stopped_not_running(self):
        """Stop killed Harbor: the feed has no job.end and result.json still
        says one trial runs. The runner's record ends it: /live
        settles with the trial cancelled, the job page says the run was
        stopped, the runs list badge reads `stopped`, not `running`."""
        from nullius import execution
        plan = execution.Plan(name="running-run", script="true", jobs=[{"name": "running-run"}])
        ex = execution.Execution(plan, root=str(self.root / "runs"), run=lambda *a, **k: (0, ""))
        ex.state, ex.finished = "stopped", 1789773420.0                  # 2026-09-18T23:17:00Z
        with patch.dict(execution.EXECUTIONS, {ex.id: ex}, clear=True):
            s = self.client.get("/job/running-run/live").get_json()
            self.assertEqual((s["state"], s["running"], s["ended_by"], s["finished_at"]), ("finished", False, "stopped", "2026-09-18T23:17:00Z"))
            self.assertEqual(s["counts"]["running"], 0)
            self.assertTrue(any(t["outcome"] == "cancelled" for t in s["trials"]))
            self.assertTrue(any("The run was stopped at 23:17:00Z" in n for n in s["notices"]))
            page = self.client.get("/job/running-run"); self.addCleanup(page.close)
            html = page.get_data(as_text=True)
            self.assertIn('id="ended-by-runner"', html); self.assertIn("This run was stopped", html)
            self.assertNotIn("This run is still going", html); self.assertNotIn("Results as of this page's load", html)
            self.assertIn('aria-controls="view-results" aria-selected="true"', html)                 # settled: Results opens
            self.assertIn('id="view-live" aria-labelledby="tab-live" hidden', html)                   # and Live is a click away
            runs = self.client.get("/runs"); self.addCleanup(runs.close)
            row = next(r for r in re.findall(r"<tr [^>]*>.*?</tr>", runs.get_data(as_text=True), re.S) if '/job/running-run"' in r)
            self.assertIn(">stopped</span>", row); self.assertNotIn(">running</span>", row)
        # without the record, as before: running
        self.assertTrue(self.client.get("/job/running-run/live").get_json()["running"])

    def test_live_route_is_404_without_a_feed_or_a_job(self):
        for path in ("/job/old-run/live", "/job/nope/live"):
            r = self.client.get(path); self.addCleanup(r.close)
            self.assertEqual(r.status_code, 404, path)

    def test_a_broken_feed_is_reported_not_fatal(self):
        """A malformed line in the middle: the page still renders, the board says why, /live is 422."""
        broken = self.root / "broken-run"
        job_with_feed(self.root, "broken-run")
        text = (broken / "events.jsonl").read_text(encoding="utf-8")
        complete = text[: text.rfind("\n") + 1]
        (broken / "events.jsonl").write_text(complete + "this is not json\n" + complete.splitlines()[0] + "\n", encoding="utf-8")
        job = parse.parse_job(str(broken))
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: [job]):
            page = self.client.get("/job/broken-run"); self.addCleanup(page.close)
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("The live feed could not be read", html); self.assertNotIn('id="live-board"', html)
            r = self.client.get("/job/broken-run/live"); self.addCleanup(r.close)
            self.assertEqual(r.status_code, 422); self.assertIn("not a feed", r.get_json()["error"])

    def test_live_route_reads_harbors_recorded_run(self):
        """The feed Harbor's writer really emitted (oracle x 4): every trial names
        its task although the writer stamps it on trial.phase, not trial.queued;
        the recording is finished, so the page opens on Results and /live says so."""
        recorded = self.root / "oracle-x4"
        job_with_feed(self.root, "oracle-x4")
        shutil.copy(FIXTURE / "harbor-oracle-x4" / "events.jsonl", recorded / "events.jsonl")
        job = parse.parse_job(str(recorded))
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: [job]):
            r = self.client.get("/job/oracle-x4/live"); self.addCleanup(r.close)
            self.assertEqual(r.status_code, 200)
            s = r.get_json()
            self.assertFalse(s["running"]); self.assertEqual(s["counts"]["resolved"], 4)
            self.assertEqual({t["task"] for t in s["trials"]},
                             {"harbor/hello-world", "harbor/hello-alpine", "reward-kit-example", "harbor/hello-user"})
            self.assertEqual({t["agent"] for t in s["trials"]}, {"oracle"})
            self.assertEqual(s["totals"], {"n_input_tokens": None, "n_cache_tokens": None,
                                           "n_output_tokens": None, "cost_usd": None})
            self.assertIn("timeline", s)                                            # the phases' own times, beside the snapshot
            page = self.client.get("/job/oracle-x4"); self.addCleanup(page.close)
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('aria-controls="view-results" aria-selected="true"', html)
            self.assertIn('id="view-live" aria-labelledby="tab-live" hidden', html)

    # -- launched, not started ------------------------------------------------
    def test_a_launched_job_is_listed_as_preparing_and_its_page_waits_for_the_feed(self):
        launched = self.root / "launched-run"
        write_json(launched / "config.json", {"agents": [{"name": "my-agent", "model_name": "m"}],
                                              "datasets": [{"name": "tau3-demo", "n_tasks": 3}], "n_concurrent_trials": 3})
        job = parse.parse_job(str(launched))
        self.assertTrue(job.is_preparing)
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: [job]):
            runs = self.client.get("/runs"); self.addCleanup(runs.close)
            html = runs.get_data(as_text=True)
            self.assertIn(">preparing</span>", html)
            self.assertNotIn(">running</span>", html)
            self.assertIn("/runs/pulse", html)                                # and the list watches the roots
            page = self.client.get("/job/launched-run"); self.addCleanup(page.close)
            self.assertEqual(page.status_code, 200)
            body = page.get_data(as_text=True)
            self.assertIn('id="preparing"', body); self.assertIn("no trial has started yet", body)
            self.assertNotIn('id="live-board"', body)                          # nothing to fold yet
            live = self.client.get("/job/launched-run/live"); self.addCleanup(live.close)
            self.assertEqual(live.status_code, 404)                            # what the page polls until the feed lands

    def test_benchmark_ending_before_its_feed_stops_the_preparing_poll(self):
        from nullius import execution
        folder = self.root / "no-feed"
        write_json(folder / "config.json", {"agents": [{"name": "oracle"}], "datasets": []})
        job = parse.parse_job(str(folder))
        plan = execution.Plan(name="no-feed", script="true", jobs=[{"name": "no-feed"}])
        ex = execution.Execution(plan, root=str(self.root / "runs"), run=lambda *a, **k: (0, ""))
        with patch.object(workspace, "load_jobs", return_value=[job]), patch.dict(execution.EXECUTIONS, {ex.id: ex}, clear=True):
            for state in ("failed", "stopped", "done"):
                with self.subTest(state=state):
                    ex.state, ex.finished = state, 1789773420.0
                    response = self.client.get("/job/no-feed/live")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), {"state": "finished", "running": False, "ended_by": state})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    html = self.client.get("/job/no-feed").get_data(as_text=True)
                    self.assertIn('id="ended-by-runner"', html)
                    self.assertNotIn('id="preparing"', html)
                    self.assertNotIn('setInterval(tick, 1000)', html)

    def test_the_pulse_changes_when_a_run_is_launched_and_when_its_feed_grows(self):
        with patch.dict(web.shared.STATE, {"job_roots": [str(self.root)]}):
            r = self.client.get("/runs/pulse"); self.addCleanup(r.close)
            self.assertEqual(r.status_code, 200); self.assertEqual(r.headers.get("Cache-Control"), "no-store")
            before = r.get_json()["signature"]
            launched = self.root / "pulse-run"
            write_json(launched / "config.json", {"agents": [{"name": "oracle"}], "datasets": []})
            r2 = self.client.get("/runs/pulse"); self.addCleanup(r2.close)
            after_launch = r2.get_json()["signature"]
            self.assertNotEqual(before, after_launch)                          # a directory appeared
            (launched / "events.jsonl").write_text('{"ts":"2026-09-12T18:00:00Z","seq":1,"job":"pulse-run","trial":null,"event":"job.start"}\n', encoding="utf-8")
            r3 = self.client.get("/runs/pulse"); self.addCleanup(r3.close)
            self.assertNotEqual(after_launch, r3.get_json()["signature"])     # the feed grew
            r4 = self.client.get("/runs/pulse"); self.addCleanup(r4.close)
            self.assertEqual(r3.get_json()["signature"], r4.get_json()["signature"])   # nothing changed: same signature

    def test_a_feed_with_only_job_prepare_puts_the_preparation_window_on_the_board(self):
        """Harbor's launcher writes job.prepare at Run: the page shows the board
        in its preparing state — no trial cards, the stage and the time in it —
        and keeps polling, instead of the pre-feed banner."""
        launched = self.root / "prepare-run"
        write_json(launched / "config.json", {"agents": [{"name": "my-agent", "model_name": "m"}], "datasets": [{"name": "tau3-demo", "n_tasks": 2}]})
        shutil.copy(FIXTURE / "prepare-window" / "events.jsonl", launched / "events.jsonl")
        text = (launched / "events.jsonl").read_text(encoding="utf-8").splitlines()
        (launched / "events.jsonl").write_text("\n".join(text[:2]) + "\n", encoding="utf-8")    # as a poller saw it at 08:00:20
        job = parse.parse_job(str(launched))
        self.assertTrue(job.is_preparing)                                  # no result record yet
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: [job]):
            r = self.client.get("/job/prepare-run/live"); self.addCleanup(r.close)
            snap = r.get_json()
            self.assertEqual((snap["state"], snap["running"], snap["preparing"]["stage"], snap["trials"]), ("preparing", False, "image", []))
            self.assertEqual(snap["counts"]["total"], 2)                   # job.prepare already said how many
            page = self.client.get("/job/prepare-run"); self.addCleanup(page.close)
            html = page.get_data(as_text=True)
            self.assertIn('id="live-board"', html)                          # the board, in its preparing state
            self.assertIn('aria-controls="view-live" aria-selected="true"', html)   # and the view it opens on
            self.assertNotIn('id="preparing"', html)                        # not the pre-feed banner
            out = self.render(snap)
            self.assertTrue(out["status"].startswith("preparing — image"))
            self.assertIn("in this stage", out["status"])
            self.assertFalse(out["finished"]); self.assertEqual(out["tiles"].count("lb-tile"), 0)
            self.assertIn("Preparing <b>image</b>", out["flight"]); self.assertIn("Nothing is scheduled yet", out["flight"])

    def test_a_runner_s_arm_is_watched_on_its_board_then_read_as_results(self):
        """A LongMemEval arm's config and live feed open before its trials exist.
        Trials appear one per question on completion. The page then opens
        Results, with links to the trial pages."""
        arm = self.root / "lme-arm"
        shutil.copytree(NATIVE / "lme-arm", arm)
        # mid-run, as the folder was before the arm ended: its config and feed only
        running = self.root / "lme-running"
        running.mkdir(); shutil.copy(arm / "config.json", running); shutil.copy(arm / "events.jsonl", running)
        cases = {"lme-running": parse.parse_job(str(running)), "lme-arm": parse.parse_job(str(arm))}
        self.assertTrue(cases["lme-running"].is_preparing)
        self.assertEqual(sorted((t.trial_name, t.outcome) for t in cases["lme-arm"].trials), [("q0", "failed_verification"), ("q1", "resolved")])
        # the relay's record is each trial's evidence: tokens, cached, cost, the 429 the client survived
        q0 = next(t for t in cases["lme-arm"].trials if t.trial_name == "q0")
        self.assertEqual((q0.n_input_tokens, q0.n_cache_tokens, q0.cost_usd, q0.n_rate_limited_retries, q0.has_trajectory, q0.has_trace),
                         (120, 60, 0.0004, 1, True, True))
        self.assertIsNotNone(cases["lme-arm"].duration_min)
        self.assertEqual((cases["lme-arm"].runner_judge, cases["lme-arm"].questions_from), ("official gpt-4o · evaluate_qa.py", "longmemeval_s"))
        verdict = json.loads((arm / "q0" / "verifier" / "judge.json").read_text())                    # the benchmark's judge: asked, said
        self.assertEqual((verdict["said"], verdict["model"]), ("no", "openai/gpt-4o-2024-08-06")); self.assertIn("Where did I move?", verdict["asked"])
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: list(cases.values())):
            for job_id, opens, base in (("lme-running", "live", ""), ("lme-arm", "results", "/job/lme-arm")):
                with self.subTest(job_id):
                    page = self.client.get("/job/" + job_id); self.addCleanup(page.close)
                    self.assertEqual(page.status_code, 200)
                    html = page.get_data(as_text=True)
                    self.assertIn('id="live-board"', html); self.assertNotIn('id="preparing"', html)
                    self.assertIn('LongMemEval <span class="mono">9e0b455f4ef0</span>', html)          # what ran: the runner and its commit
                    self.assertNotIn(">harbor ", html); self.assertNotIn("Copy command", html)          # never Harbor, never a harbor run line
                    self.assertNotIn("oracle · passed", html)                                         # LongMemEval's oracle arm is not Harbor's control
                    self.assertNotIn("runtime <", html)                                               # no agent runtime to name
                    # the judge reads the trajectories the arm wrote; a running arm has none yet
                    (self.assertIn if base else self.assertNotIn)('id="judgement"', html)
                    self.assertIn(f'data-trial-base="{base}"', html)                                  # a trial link only once there is a trial page
                    self.assertRegex(html, rf'id="tab-{opens}"[^>]*aria-selected="true"')
            trial = self.client.get("/trial/lme-arm/q0"); self.addCleanup(trial.close)
            html = trial.get_data(as_text=True)
            self.assertEqual(trial.status_code, 200)
            self.assertIn("1 rate-limited call retried", html)
            self.assertIn("“Other” is the wait for the judge", html); self.assertNotIn("Harbor", html.split("<main", 1)[1])   # a reader's run, in its own words
            self.assertNotIn("How it worked", html); self.assertNotIn(">Tool calls</h3>", html)                    # edits and tests are an agent's, not one call's
            self.assertIn("LongMemEval's judge", html); self.assertIn("Said <b class=\"mono\">no</b>", html)   # the judge that gave the outcome
            self.assertIn("official gpt-4o · evaluate_qa.py", html)
            self.assertIn('id="usage-mix"', html); self.assertIn("served from cache <b>60</b>", html)  # the one call drawn: from cache, new, written
            self.assertIn("asks its model once for each of its questions, so the bar is that one call", html)   # the definition's word for an item
            job = self.client.get("/job/lme-arm"); self.addCleanup(job.close)
            page = job.get_data(as_text=True)
            self.assertIn("Trajectory judge", page); self.assertIn("The outcomes are LongMemEval's own judge: official gpt-4o · evaluate_qa.py.", page)
            runs = self.client.get("/runs"); self.addCleanup(runs.close)
            listing = runs.get_data(as_text=True)
            self.assertNotIn("No nop or oracle ran", listing)                                              # a runner's controls are its own arms
            self.assertIn(" · 2 questions", listing); self.assertNotIn("mixed · ", listing)                 # the questions every arm answers
            live = self.client.get("/job/lme-arm/live"); self.addCleanup(live.close)
            board = live.get_json()
            self.assertEqual((board["state"], board["counts"]["resolved"], board["counts"]["unresolved"]), ("finished", 1, 1))

    def test_a_task_scored_on_its_parts_shows_its_score_and_no_solve_rate(self):
        """Each task's cases form one trial with a mean score. The run shows the
        mean across cases, no solve rate, and each trial's answers and scores."""
        arm = self.root / "parts-arm"
        shutil.copytree(NATIVE / "parts-arm", arm)
        job = parse.parse_job(str(arm))
        self.assertEqual(sorted((t.trial_name, t.outcome_label, t.score, t.n_parts) for t in job.trials), [("t1", "Scored", 0.5, 2), ("t2", "Scored", 1.0, 1)])
        with patch.object(workspace, "load_jobs", side_effect=lambda ws, roots: [job]):
            page = self.client.get("/job/parts-arm"); self.addCleanup(page.close)
            html = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            hero = html.split('id="run-hero"', 1)[1].split("</div>\n  </div>", 1)[0]
            self.assertIn(">Mean score<", hero); self.assertIn('id="adjusted-rate">0.67<', hero)        # two of the three cases, not the mean of two tasks
            self.assertNotIn("Adjusted rate", hero); self.assertIn("No pass or fail in this benchmark: no rate", hero)
            self.assertIn('<span class="oc oc-scored"><i></i>Scored</span>', html)                     # neither resolved nor failed: an outcome of its own
            self.assertRegex(html, r'class="badge trial-score" title="The mean score of its 2 parts[^"]*">0\.50<')
            trial = self.client.get("/trial/parts-arm/t1"); self.addCleanup(trial.close)
            html = trial.get_data(as_text=True)
            self.assertEqual(trial.status_code, 200)
            self.assertIn("Sums's score", html); self.assertNotIn("Sums's judge", html)
            self.assertIn("the mean over its 2 cases", html)
            table = html.split('id="runner-parts"', 1)[1].split("</table>", 1)[0]
            self.assertIn("<th>Expected</th>", table)
            rows = [" ".join(r.split()) for r in table.split("<tr>")[2:]]
            self.assertIn('<td class="num mono">3</td><td>Add 2 and 2.</td><td>5</td><td>4</td>', rows[1])   # at its published place: the trap was the second
            self.assertIn(">0.000<", rows[1]); self.assertNotIn("Divide by nothing", html)
            runs = self.client.get("/runs"); self.addCleanup(runs.close)
            self.assertEqual(runs.status_code, 200)
            live = self.client.get("/job/parts-arm/live"); self.addCleanup(live.close)
            board = live.get_json()
            self.assertEqual((board["state"], board["counts"]["scored"], board["counts"]["resolved"]), ("finished", 2, 0))
            self.assertEqual([(t["trial"], t["outcome"], t["score"], t["parts"]) for t in board["trials"]], [("t1", "scored", 0.5, 2), ("t2", "scored", 1.0, 1)])

    def test_job_page_opens_on_live_while_running_and_on_results_after(self):
        running = self.client.get("/job/running-run"); self.addCleanup(running.close)
        html = running.get_data(as_text=True)
        self.assertIn('id="live-board"', html)
        self.assertIn('data-endpoint="/job/running-run/live"', html)
        self.assertIn('id="live-board-initial"', html)        # first paint needs no fetch
        self.assertIn("live-board.js", html); self.assertIn("live-board.css", html)
        self.assertIn('aria-controls="view-live" aria-selected="true"', html)
        self.assertIn('id="view-results" aria-labelledby="tab-results" hidden', html)
        self.assertIn('id="results-badge" data-trials="0">as of load<', html)                       # Results is the page as it loaded
        self.assertIn("Results as of this page's load", html); self.assertNotIn("This run is still going", html)
        # ?view= wins, both ways: a link opens the view it was copied from
        asked = self.client.get("/job/running-run?view=results"); self.addCleanup(asked.close)
        self.assertIn('aria-controls="view-results" aria-selected="true"', asked.get_data(as_text=True))
        ended = self.client.get("/job/ended-run?view=live"); self.addCleanup(ended.close)
        self.assertIn('aria-controls="view-live" aria-selected="true"', ended.get_data(as_text=True))
        ended = self.client.get("/job/ended-run"); self.addCleanup(ended.close)
        body = ended.get_data(as_text=True)
        self.assertIn('aria-controls="view-results" aria-selected="true"', body)          # ended: Results opens
        self.assertIn('id="live-board"', body); self.assertNotIn("as of load", body)      # Live stays, as the record of the run
        old = self.client.get("/job/old-run?view=live"); self.addCleanup(old.close)
        body = old.get_data(as_text=True)
        self.assertEqual(old.status_code, 200)
        self.assertNotIn('id="live-board"', body); self.assertNotIn('role="tablist"', body)   # no feed: one view, no tabs

    # -- the renderer, executed --------------------------------------------
    def render(self, snapshot, **opts):
        if not self.node:
            self.skipTest("node not available")
        prog = ("const L=require(" + json.dumps(str(APP_DIR / "static" / "runs" / "live-board.js")) + ");"
                "const s=JSON.parse(process.argv[1]);const o=JSON.parse(process.argv[2]);"
                "process.stdout.write(JSON.stringify(L.render(s,Object.assign({trialBase:'/job/demo-x3'},o))));")
        out = subprocess.run([self.node, "-e", prog, json.dumps(snapshot), json.dumps(opts)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def board(self, out):
        """Every part a user reads, in the board's order, as one text."""
        return visible(" ".join(out[k] for k in ("head", "meta", "stats", "legend", "lanesLine", "winners", "tiles", "flight", "drawer")))

    def test_renderer_reads_the_reference_snapshot(self):
        s = json.loads((FIXTURE / "expected.json").read_text())
        out = self.render(s)
        self.assertFalse(out["finished"]); self.assertEqual(out["state"], "running")
        self.assertNotIn("$0", self.board(out)); self.assertNotIn("spent", out["meta"])   # null cost: left out, never $0
        self.assertEqual(out["tiles"].count('class="lb-tile'), len(s["trials"]))         # every trial, one tile
        running = [t for t in s["trials"] if t["state"] == "running"]
        done = [t for t in s["trials"] if t["state"] == "done"]
        self.assertEqual(out["flight"].count('<tr class="lb-row'), len(running) + len(done))   # a row per trial in flight, then each that ended
        self.assertEqual(out["flight"].count('<tr class="lb-row lb-ended'), len(done))
        self.assertLess(out["flight"].rfind('lbp-'), out["flight"].find('lb-ended'))          # the running on top, the ended below
        self.assertIn('class="lb-open-row" href="/trial/demo-x3/', out["flight"])              # an ended trial opens its page
        named = self.render(dict(s, trials=[dict(running[0], task="4")]), noun="task")
        self.assertIn(">Task 4<", named["flight"])                                            # a bare number reads with its noun
        self.assertIn('class="lb-tile done lbo-resolved', out["tiles"])                  # a resolved trial's tile
        self.assertEqual(out["drawer"], "")                                               # nothing selected
        opened = self.render(s, selected=running[0]["trial"])
        self.assertIn('href="/trial/demo-x3/', opened["drawer"])                          # the drawer links to its trial
        self.assertIn(escape(running[0]["task"].split("__")[-1]), opened["drawer"])

    def test_renderer_shows_the_last_log_line_when_no_activity_and_it_is_redacted(self):
        s = json.loads((FIXTURE / "expected.json").read_text())
        retail = next(t for t in s["trials"] if t["last_line"])
        out = self.render(dict(s, trials=[dict(retail, activity=None)]))
        self.assertIn("[REDACTED]", out["flight"])                     # the fixture's key-shaped string, scrubbed
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234567890", self.board(out))

    def test_renderer_leaves_unreported_counters_absent_not_zero(self):
        s = json.loads((FIXTURE / "expected.json").read_text())
        quiet = next(t for t in s["trials"] if t["state"] == "running" and t["step"] is None)
        self.assertIsNone(quiet["n_input_tokens"]); self.assertIsNone(quiet["cost_usd"])
        out = self.render(dict(s, trials=[dict(quiet, timeout_sec=None)]), selected=quiet["trial"])
        self.assertNotIn(">Steps<", out["flight"]); self.assertNotIn(">Tokens<", out["flight"])   # nothing reported them: no column
        self.assertIn("of —", visible(out["flight"]))                                           # no limit: "—", not a number
        text = self.board(out)
        self.assertNotIn("step 0", text); self.assertNotIn("tokens 0", text); self.assertNotIn("$0", text)
        self.assertNotIn("Steps", out["drawer"]); self.assertNotIn("Cost", out["drawer"])        # the drawer lists only what was reported

    def test_renderer_reads_harbors_recorded_run(self):
        s = json.loads((FIXTURE / "harbor-oracle-x4" / "expected.json").read_text())
        out = self.render(s)
        self.assertTrue(out["finished"]); self.assertEqual(out["status"], "")
        self.assertEqual(out["tiles"].count('class="lb-tile done lbo-resolved'), 4)
        for task in ("harbor/hello-world", "harbor/hello-alpine", "reward-kit-example", "harbor/hello-user"):
            self.assertIn(escape(task.split("/")[-1]), out["tiles"])      # no tile is blank
        self.assertIn("The feed saw 4 of 4 trials end, 4 resolved", visible(out["flight"]))
        self.assertIn('href="?view=results"', out["flight"])              # the settled board points at Results
        self.assertIn("100", out["stats"])                                 # raw and adjusted so far, from the feed's outcomes
        self.assertNotIn("$0", self.board(out)); self.assertNotIn("tokens", out["meta"])   # the oracle reports no counters
        opened = self.render(s, selected=s["trials"][0]["trial"])
        self.assertEqual(opened["drawer"].count('class="lb-seg lbp-'), 0)  # no timeline in a bare snapshot: no phases drawn
        self.assertEqual(opened["drawer"].count('<span class="lbp-'), 4)   # but the four phases are named, each with its time

    def test_renderer_reads_harbors_recorded_progress_run(self):
        # The P3 recording: four reporting trials carry step, tokens and a
        # redacted activity line; four silent ones read "—" in those cells;
        # the header has a measured rate for the first time.
        s = json.loads((FIXTURE / "harbor-progress-x8" / "expected.json").read_text())
        self.assertGreater(s["tokens_per_sec"], 0)
        live = dict(s, state="running", running=True, finished_at=None,
                    trials=[dict(t, state="running", phase="agent_run", outcome=None, timeout_sec=None) for t in s["trials"]])
        out = self.render(live)
        self.assertEqual(out["flight"].count('<tr class="lb-row'), 8)
        self.assertIn(">Steps<", out["flight"]); self.assertIn(">Tokens<", out["flight"])   # some trial reported them: the columns show
        self.assertEqual(out["flight"].count('class="lb-line"'), 4)          # one activity line per reporting trial
        self.assertEqual(out["flight"].count('class="lb-line na"'), 4)       # and "—" for the silent ones
        self.assertIn("[REDACTED]", out["flight"])
        meta = visible(out["meta"])
        self.assertNotIn("cached", meta); self.assertIn("2.7k out", meta)      # no cache count was reported: absent, not 0
        self.assertIn("tokens / s, last 30 s", meta)
        self.assertNotIn("$0", self.board(out)); self.assertNotIn("step 0", self.board(out))

    def test_renderer_says_starting_while_no_trial_has_entered_a_phase(self):
        s = json.loads((FIXTURE / "expected.json").read_text())
        queued = dict(s, state="starting", running=True, finished_at=None,
                      counts=dict(s["counts"], running=0, completed=0, pending=s["counts"]["total"], resolved=0, unresolved=0),
                      trials=[dict(t, state="waiting", phase=None, outcome=None) for t in s["trials"]])
        out = self.render(queued)
        self.assertTrue(out["status"].startswith("starting —"))
        self.assertIn("none has entered a phase yet", out["status"])
        self.assertIn("none has entered a phase yet", out["flight"])
        self.assertFalse(out["finished"])

    def test_renderer_leaves_an_unknown_total_absent_while_preparing(self):
        # Harbor's prepare lines carry no n_total: the board must not read "0 / 0".
        s = json.loads((FIXTURE / "harbor-prepare-x4" / "expected-preparing.json").read_text())
        self.assertIsNone(s["counts"]["total"])
        out = self.render(s)
        self.assertTrue(out["status"].startswith("preparing — tasks"))
        self.assertIn("0<small>/?</small>", out["stats"]); self.assertNotIn("/0", out["stats"]); self.assertNotIn("null", out["stats"])
        self.assertIn('class="v na">—</span>', out["stats"])                  # queued: not yet known
        after = self.render(json.loads((FIXTURE / "harbor-prepare-x4" / "expected.json").read_text()))
        self.assertIn("4<small>/4</small>", after["stats"]); self.assertTrue(after["finished"])

    # -- rate, ETA and the leader states, as the text a user reads -----------
    def test_renderer_shows_rate_and_eta_as_text_beside_phase_counters_outcome_and_activity(self):
        # rate-eta: hand-written so that one snapshot carries everything the
        # board can say — a measured rate (126100 tokens over the 30 s window →
        # 4203/s), an ETA (median 172 s × 3 remaining ÷ 2), a phase with time in
        # it, counters, two outcomes, and an activity line the writer put a key
        # into. The strings are pinned as rendered, not as folded: test_live pins
        # the numbers, this pins their text.
        s = json.loads((FIXTURE / "rate-eta" / "expected.json").read_text())
        self.assertEqual((s["tokens_per_sec"], s["eta_sec"]), (126100 / 30, 92.0))
        out = self.render(s)
        self.assertIn("4.2k tokens / s, last 30 s", visible(out["meta"]))
        self.assertIn("wall 20:00 per trial", visible(out["meta"]))
        self.assertIn("795.5k tokens (590.0k cached, 10.5k out)", visible(out["meta"]))
        self.assertEqual(out["head"], "started 10:00:00, 6:00 elapsed")
        stats = visible(out["stats"])
        self.assertIn("Done 2 /5 Running 2 Queued 1 Rate 0.3 /min", stats)
        self.assertNotIn("ETA", stats)                                          # no ETA card: a guess, not a count
        self.assertIn("Raw solve rate so far 50 % Adjusted so far 50 %", stats)   # 1 resolved of 2 ended
        self.assertNotIn("spent", out["meta"]); self.assertNotIn("$0", self.board(out))   # cost not reported: left out
        # phase and time in phase against its limit, counters, the outcomes as chips
        flight = visible(out["flight"])
        self.assertIn("telecom-0 agent run 2:40 of 1:00:00 8 182.4k", flight)
        self.assertIn("Resolved 1", visible(out["legend"])); self.assertIn("Unresolved 1", visible(out["legend"]))
        self.assertIn('aria-label="tau3-banking_knowledge-task-002: queued"', out["tiles"])
        # the activity line, redacted on read: the key is on the feed, not on the board
        self.assertIn('tool call tau3-runtime_send_message_to_user token=<span class="lb-red">[REDACTED]</span>', out["flight"])
        feed = (FIXTURE / "rate-eta" / "events.jsonl").read_text()
        self.assertIn("sk-test-abcdefghijklmnopqrstuvwxyz1234", feed)
        self.assertNotIn("sk-test-", json.dumps(out))
        self.assertEqual(out["status"], "")                                    # running: the board says it all, no footer
        self.assertFalse(out["finished"])

    def test_renderer_shows_the_four_leader_states_as_text(self):
        # single-agent, strict win, tie, undecided — one fixture each (two-agents
        # carries the strict win beside a tie and an undecided task; the tie and
        # undecided fixtures carry nothing else, so the table reads one word).
        single = self.render(json.loads((FIXTURE / "rate-eta" / "expected.json").read_text()))
        self.assertEqual((single["lanes"], single["winners"], single["lanesLine"]), (0, "", ""))   # one lane, nothing to win
        self.assertNotIn("won", visible(single["meta"] + single["stats"]))

        win = self.render(json.loads((FIXTURE / "two-agents" / "expected.json").read_text()))
        self.assertIn('<tr data-task="tau3-bench__tau3-airline-0"><td class="task" title="tau3-bench__tau3-airline-0">tau3-bench__tau3-airline-0</td>'
                      '<td class="num">1.00</td><td class="num">0.00</td><td><b class="lb-win">my-agent</b></td></tr>', win["winners"])
        self.assertIn("tau3-bench__tau3-airline-0 1.00 0.00 my-agent", visible(win["winners"]))
        self.assertIn("my-agent 3 / 3 completed · 1.00 mean reward · 1 won", visible(win["lanesLine"]))
        self.assertIn("opencode 2 / 3 completed · 1 running · 0.50 mean reward · 0 won", visible(win["lanesLine"]))   # 1.0 and 0.0 finished
        self.assertEqual(win["tiles"].count('class="lb-tile'), 6)                                       # every trial, once
        self.assertNotIn("$0", visible(win["lanesLine"])); self.assertNotIn("spent", visible(win["lanesLine"]))

        tie = self.render(json.loads((FIXTURE / "two-agents-tie" / "expected.json").read_text()))
        self.assertEqual(tie["lanes"], 2); self.assertIn('data-winners="2"', tie["winners"])
        self.assertIn('<td class="num">1.00</td><td class="num">1.00</td><td><span class="na">tie</span></td></tr>', tie["winners"])
        self.assertIn('<td class="num">0.00</td><td class="num">0.00</td><td><span class="na">tie</span></td></tr>', tie["winners"])
        text = visible(tie["winners"])
        self.assertIn("Winner per task my-agent opencode winner tau3-bench__tau3-airline-0 1.00 1.00 tie tau3-bench__tau3-retail-0 0.00 0.00 tie", text)
        self.assertEqual(text.count("tie"), 2 + 1)                                      # two rows, and the footnote
        self.assertNotIn("undecided", text); self.assertNotIn('class="lb-win"', tie["winners"])   # nobody is named
        self.assertEqual(visible(tie["lanesLine"]).count("0 won"), 2)

        early = self.render(json.loads((FIXTURE / "two-agents-undecided" / "expected.json").read_text()))
        self.assertEqual(early["lanes"], 2); self.assertIn('data-winners="2"', early["winners"])
        self.assertIn('<td class="num">1.00</td><td class="num"><span class="na">—</span></td><td><span class="lb-open">undecided</span></td></tr>', early["winners"])
        self.assertIn('<td class="num"><span class="na">—</span></td><td class="num"><span class="na">—</span></td><td><span class="lb-open">undecided</span></td></tr>', early["winners"])
        text = visible(early["winners"])
        self.assertIn("tau3-bench__tau3-airline-0 1.00 — undecided tau3-bench__tau3-retail-0 — — undecided", text)
        self.assertEqual(text.count("undecided"), 2)
        self.assertNotIn("tie", text.replace("A tie names nobody", "")); self.assertNotIn('class="lb-win"', early["winners"])
        self.assertEqual(visible(early["lanesLine"]).count("0 won"), 2)
        self.assertNotIn("tokens / s", early["meta"])                                  # nothing in the window: no rate, not 0
        self.assertIn("wall 20:00 per trial", visible(early["meta"]))

    def test_renderer_marks_a_finished_job(self):
        s = json.loads((FIXTURE / "expected.json").read_text())
        out = self.render(dict(s, state="finished", running=False, finished_at="2026-09-12T18:40:00Z"))
        self.assertTrue(out["finished"]); self.assertEqual(out["status"], "")
        self.assertNotIn('ETA', out["stats"])
        stopped = self.render(dict(s, state="finished", running=False, ended_by="stopped", finished_at="2026-09-12T18:40:00Z"))
        self.assertEqual(stopped["stateLabel"], "Stopped"); self.assertIn("Stopped at 18:40:00", visible(stopped["flight"]))
        # a feed written before the snapshot carried a state: derived from running and the counts
        legacy = {k: v for k, v in s.items() if k not in ("state", "launched_at", "preparing")}
        self.assertTrue(self.render(dict(legacy, running=False))["finished"])
        self.assertTrue(self.render(dict(legacy, running=True, counts=dict(s["counts"], running=0, completed=0)))["status"].startswith("starting"))

    def test_the_drawer_draws_one_trials_phases_from_the_feeds_own_times(self):
        root = FIXTURE / "harbor-oracle-x4"
        from nullius.runs import live
        s = live.snapshot(str(root), with_timeline=True)
        trial = s["trials"][0]
        out = self.render(s, selected=trial["trial"])
        self.assertEqual(out["drawer"].count('class="lb-seg lbp-'), 4)              # four phases, each a segment on its own clock
        self.assertIn('class="lb-cap lbo-resolved"', out["drawer"])                  # and its outcome where it ended
        self.assertIn("ended " + s["timeline"][trial["trial"]]["ended"][11:19], visible(out["drawer"]))
        self.assertNotIn(" so far", visible(out["drawer"]))                          # nothing is still running in a finished trial

    def test_a_crowd_in_flight_shows_the_nearest_their_limit_first(self):
        s = json.loads((FIXTURE / "rate-eta" / "expected.json").read_text())
        base = next(t for t in s["trials"] if t["state"] == "running")
        crowd = [dict(base, trial=f"t{k:02d}", task=f"x__task-{k:02d}", elapsed_sec=10 * k, timeout_sec=300) for k in range(20)]
        out = self.render(dict(s, trials=crowd, counts=dict(s["counts"], running=20, total=20, completed=0)))
        self.assertEqual(out["flight"].count('<tr class="lb-row'), 12)
        self.assertIn("12 of 20 in flight, nearest their limit first", visible(out["flight"]))
        first = re.search(r'data-trial="(t\d+)"', out["flight"]).group(1)
        self.assertEqual(first, "t19")                                                # the one nearest its limit on top
        self.assertEqual(self.render(dict(s, trials=crowd, counts=dict(s["counts"], running=20, total=20, completed=0)), showAll=True)["flight"].count('<tr class="lb-row'), 20)
        many = self.render(dict(s, trials=crowd * 4))
        self.assertTrue(many["manyTiles"])                                             # past sixty trials, smaller tiles

if __name__ == "__main__":
    unittest.main()
