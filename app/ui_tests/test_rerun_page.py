"""Tasks of an ended job run again from its page: a tick on each row, the rows
shown chosen at once, Run selected again with a reason and a cap; started where
the job ran (this machine or its host), refused with one line where the server
cannot; the earlier result while a task runs again; afterwards the list of
re-runs, the column of tries and the try's mark."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import execution, hosts, workspace
from nullius.harbor import feed
from nullius.standard.runner import job
from ui_tests.test_capped_job import AGENT, BENCH
from .test_routes import web

UNITS = ["t-0", "t-1", "t-2"]


def trial_name(k):
    return f"{UNITS[k]}__u{k:04d}"


def standard_job(d: Path, name: str, rewards=(1.0, 0.0, 1.0), **std):
    """A standard job of three units, each ended with its reward; ``std`` more of its config's ``standard``."""
    d.mkdir(parents=True)
    job.write_config(str(d), job=name, bench=BENCH, other=AGENT, model="m-1", provider="openai", dataset="all", units=UNITS,
                     concurrency=1, granted=True)
    if std:
        cfg = json.loads((d / "config.json").read_text())
        cfg["standard"].update(std)
        (d / "config.json").write_text(json.dumps(cfg))
    results = []
    for k, u in enumerate(UNITS):
        trial = d / trial_name(k)
        trial.mkdir()
        results.append(job.write_trial(str(trial), job=name, bench=BENCH, other=AGENT, model="m-1", unit=u, t0=1_760_000_000.0 + k,
                                       t1=1_760_000_005.0 + k, produce=(1_760_000_000.0 + k, 1_760_000_004.0 + k),
                                       score={"reward": rewards[k]}, exception=None, calls=[], trajectory=None))
    job.write_result(str(d), name, 1_760_000_000.0, 1_760_000_060.0, results, len(UNITS))


def text(html, pattern):
    got = re.search(pattern, html, re.S)
    assert got is not None, pattern
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", got[0]))).strip()


class RerunPage(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-rerun-page-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"RUNS_DIR": str(self.tmp / "runs"), "HOSTS": "bench",
                                                         "OPENAI_API_KEY": "sk-testkey-0123"}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "ws")))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        self.asked = []
        self.stack.enter_context(patch.object(execution.Execution, "again", lambda ex, part, env=os.environ: self.asked.append((ex, part)) or ex))
        self.held = None
        self.stack.enter_context(patch.object(execution.Execution, "holds", lambda ex, job_id: self.held))

    def run_of(self, name="r1", host=None, state="done", **std):
        d = self.tmp / "runs" / name
        standard_job(d / "jobs" / name, name, **std)
        plan = execution.Plan(name=name, script="", runner="standard", host=hosts.parse(host) if host else None, model="m-1",
                              key_var="OPENAI_API_KEY", url_var="", recipe={"provider": "openai"},
                              parts=[{"id": "fake", "label": "Fake", "script": "true"}], jobs=[{"name": name, "agent": "fake"}])
        (d / "run.json").write_text(json.dumps({"id": f"{name}-1", "plan": plan.to_dict(), "state": state, "step": "run",
                                                "ends": {"fake": {"rc": 0, "at": 1_760_000_060.0, "label": "Fake"}}}))
        return d / "jobs" / name

    def open(self, *roots):
        execution.restore()
        web.shared.open_workspace(workspace.create(str(self.tmp / "ws"), [str(r) for r in roots], label="again"))
        self.client = web.app.test_client()
        r = self.client.get("/runs"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]

    def page(self, path):
        r = self.client.get(path); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def post(self, name, body, csrf=None):
        r = self.client.post(f"/job/{name}/rerun", json=body, headers={"X-CSRF-Token": self.csrf if csrf is None else csrf,
                                                                        "Origin": "http://localhost"})
        self.addCleanup(r.close)
        return r

    def test_each_row_has_a_tick_and_the_bar_the_rows_shown_the_count_and_the_button(self):
        d = self.run_of()
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(re.findall(r'<input class="pick-row" type="checkbox" value="([^"]+)"', html), [trial_name(k) for k in range(3)])
        self.assertIn('<button class="fclear plain" id="pick-shown" type="button">Select the rows shown</button>', html)
        self.assertIn('<span class="pick-n small muted" id="pick-count" aria-live="polite">0 chosen</span>', html)
        self.assertIn('<button class="btn sm" id="rerun-open" type="button" aria-haspopup="dialog" disabled>Run selected again</button>', html)
        # the filter the bar's button reads: each row's outcome, the tick inside it
        self.assertRegex(html, r'<tr data-outcome="failed_verification"[^>]*data-trial="t-1__u0001"[^>]*>\s*<td><input class="pick-row"')
        ask = text(html, r'<dialog class="std-how rerun-ask".*?</dialog>')
        self.assertEqual(ask, "Run 0 tasks again On this machine, in this job: each result replaces the task's, and the earlier tries stay. "
                              "Reason (optional) Cancel Run 0 tasks again")
        self.assertIn('data-post="/job/r1/rerun"', html)
        self.assertNotIn('id="rerun-why"', html)

    def test_run_selected_again_starts_it_where_the_job_ran_with_its_reason(self):
        d = self.run_of()
        self.open(d.parent)
        r = self.post("r1", {"trials": [trial_name(1), trial_name(1), trial_name(2)], "reason": "  the provider\nfailed ", "max_usd": "0.5"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ((ex, part),) = self.asked
        self.assertEqual((ex.plan.name, part["id"], part["again"], part["trials"]), ("r1", "again-1", "r1", [trial_name(1), trial_name(2)]))
        self.assertIn(f"rerun \"${{NULLIUS_JOBS:-jobs_standard}}\"/r1 --unit {trial_name(1)} --unit {trial_name(2)} "
                      "--reason 'the provider failed' --from-run", part["script"])     # one cap for the job: none is posted
        r = self.post("r1", {"trials": [trial_name(0)], "reason": ""})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("--max-usd", self.asked[-1][1]["script"]); self.assertNotIn("--reason", self.asked[-1][1]["script"])

    def test_what_the_route_refuses_starts_nothing(self):
        d = self.run_of()
        self.open(d.parent)
        for body, code, said in [({"trials": []}, 400, "Tick the tasks to run again."),
                                 ({"trials": ["nope"]}, 400, "r1 holds no trial nope"),
                                 ({"trials": [trial_name(0)], "reason": "x" * 201}, 400, "The reason is one line of at most 200 characters.")]:
            r = self.post("r1", body)
            self.assertEqual((r.status_code, r.json["error"]), (code, said), body)
        self.assertEqual(self.post("r1", {"trials": [trial_name(0)]}, csrf="wrong").status_code, 403)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            r = self.post("r1", {"trials": [trial_name(0)]})
        self.assertEqual(r.status_code, 400); self.assertIn("OPENAI_API_KEY", r.json["error"])
        self.assertEqual(self.asked, [])

    def test_a_job_on_a_host_goes_back_there_and_a_host_without_it_is_refused(self):
        d = self.run_of(host="bench")
        self.open(d.parent)
        self.assertIn("On bench, in this job", self.page("/job/r1?view=results"))
        self.held = "bench no longer holds r1: nothing was started"
        r = self.post("r1", {"trials": [trial_name(1)]})
        self.assertEqual((r.status_code, r.json["error"]), (409, "bench no longer holds r1: nothing was started"))
        self.assertEqual(self.asked, [])
        self.held = None
        self.assertEqual(self.post("r1", {"trials": [trial_name(1)]}).status_code, 200)
        self.assertEqual(self.asked[0][0].plan.host.label, "bench")

    def test_each_job_the_server_cannot_run_again_shows_no_button_and_says_why(self):
        here = self.run_of("r1")
        gone = self.run_of("r2", host="ubuntu@10.0.0.9")                       # a host no longer in Settings
        merged = self.run_of("r3", merged_from=[{"job": "a"}, {"job": "b"}])
        loose = self.tmp / "loose"
        standard_job(loose / "cli-job", "cli-job")                               # started outside: no run record
        self.open(here.parent, gone.parent, merged.parent, loose)
        execution.EXECUTIONS["r1-1"].state = "running"
        for name, said in [("r1", "Still running: more tasks can be started once it has ended."),
                           ("r2", "Its host, ubuntu@10.0.0.9, is no longer in Settings."),
                           ("r3", "Merged from its shards' jobs: run a task again in its shard's job."),
                           ("cli-job", "Not started from here: no record of where it ran.")]:
            html = self.page(f"/job/{name}?view=results")
            self.assertNotIn('class="pick-row"', html, name); self.assertNotIn("Run selected again", html, name)
            self.assertEqual(text(html, r'<span class="pick-why[^>]*>.*?</span>'), said, name)
            self.assertEqual((self.post(name, {"trials": [trial_name(0)]}).status_code), 409, name)
        self.assertEqual(self.asked, [])

    def test_while_a_task_runs_again_its_earlier_result_stands_with_a_mark(self):
        d = self.run_of()
        self.open(d.parent)
        name = trial_name(1)
        (d / "retried").mkdir()
        os.replace(d / name, d / "retried" / f"{name}.try-1")
        (d / name).mkdir()                                                       # the new try: no result yet
        ex = execution.EXECUTIONS["r1-1"]
        ex.plan.parts.append({"id": "again-1", "label": "1 task of r1 again", "again": "r1", "trials": [name], "script": "true"})
        ex.part, ex.state, ex.launched = 1, "running", "again-1"
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<span class="cnt" id="trial-count">.*?</span>'), "3")
        row = re.search(rf'<tr data-outcome="failed_verification"[^>]*data-trial="{name}".*?</tr>', html, re.S)[0]
        self.assertIn('<span class="badge again-now"', row)
        self.assertNotIn("href=", row)                                           # its new try has no page yet
        self.assertIn("Still running: more tasks can be started once it has ended.", html)
        self.assertIn('id="execution-state">running', html)
        self.assertNotIn("again-now", self.page("/job/r1?view=results").split(f'data-trial="{trial_name(0)}"')[1].split("</tr>")[0])

    def test_the_waiting_page_refreshes_when_the_rerun_ends_before_its_first_poll(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        d = self.run_of()
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        writer.emit("job.start", n_total=3)
        writer.emit("job.end")
        self.open(d.parent)
        ex = execution.EXECUTIONS["r1-1"]
        ex.plan.parts.append({"id": "again-1", "again": "r1", "trials": [trial_name(1)], "script": "true"})
        ex.part, ex.state = 1, "preparing"
        html = self.page("/job/r1?view=results")
        script = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "setInterval(tick, 2000)" in s)
        program = """
const [script, replies] = process.argv.slice(1).map(JSON.parse);
let tick, destination = null;
const location = {href: 'http://localhost/job/r1?view=results', assign: u => { destination = String(u); }};
const fetch = async url => ({ok: true, json: async () => replies[url]});
new Function('document', 'nulliusFetch', 'setInterval', 'URL', 'location', script)(
  {hidden: false}, fetch, fn => { tick = fn; }, URL, location);
tick().then(() => process.stdout.write(JSON.stringify(destination)));
"""
        for state in ("preparing", "running", "done", "failed", "stopped"):
            with self.subTest(state=state):
                ex.state = state
                replies = {url: json.loads(self.page(url)) for url in ("/job/r1/live", "/run/r1-1")}
                self.assertEqual(replies["/job/r1/live"]["state"], "finished")
                result = subprocess.run([node, "-e", program, json.dumps(script), json.dumps(replies)],
                                        capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual(json.loads(result.stdout),
                                 "http://localhost/job/r1" if state in execution.ENDED else None)

    def rerun_record(self, d):
        """The job as a re-run of t-1 leaves it: its earlier try kept, the trial marked, the job's list."""
        name = trial_name(1)
        (d / "retried").mkdir()
        shutil.copytree(d / name, d / "retried" / f"{name}.try-1")
        res = json.loads((d / name / "result.json").read_text())
        res["verifier_result"] = {"rewards": {"reward": 1.0}}
        res["standard"] = dict(res["standard"], retries=1, rerun={"by": "hand", "at": "2026-11-18T15:40:12+00:00",
                                                                  "reason": "the provider failed", "replaced": {"reward": 0.0}})
        (d / name / "result.json").write_text(json.dumps(res))
        top = json.loads((d / "result.json").read_text())
        top["reruns"] = [{"at": "2026-11-18T15:40:12+00:00", "finished_at": "2026-11-18T15:42:00+00:00", "reason": "the provider failed",
                          "trials": [name], "replaced": [{"trial": name, "unit": "t-1", "try": 1, "was": {"reward": 0.0}, "now": {"reward": 1.0}}],
                          "max_usd": 0.5, "spent_usd": 0.0012}]
        (d / "result.json").write_text(json.dumps(top))

    def test_afterwards_the_list_of_re_runs_the_column_of_tries_and_the_try_s_mark(self):
        d = self.run_of()
        self.rerun_record(d)
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<details id="reruns" open>.*?</details>'),
                         "Run later 1 2026-11-18 15:40Z the provider failed t-1 reward 0.0 → reward 1.0 try 1 "
                         "1 task · $0.0012 spent")
        self.assertIn('<th scope="col" class="num">Retries</th>', html)
        cells = re.findall(r'<td class="num run-retries">(.*?)</td>', html)
        self.assertEqual(cells, ['<span class="faint">0</span>', "1", '<span class="faint">0</span>'])
        trial = self.page(f"/trial/r1/{trial_name(1)}")
        tabs = text(trial, r'<nav class="try-tabs".*?</nav>')
        self.assertTrue(tabs.startswith("Try 1"), tabs); self.assertIn("Try 2 run again by hand", tabs)
        self.assertIn('title="Try 2: Resolved, run again by hand: the provider failed"', trial)
        self.assertEqual(text(trial, r'<p class="try-why muted small".*?</p>'), "Run again by hand: the provider failed")
        earlier = self.page(f"/trial/r1/{trial_name(1)}.try-1")
        self.assertNotIn('class="try-why', earlier)                              # the earlier try was the run's own

    def interrupted(self, d):
        """The job as an interrupted run leaves it: five tasks planned, as its feed says; t-2 begun and killed (a
        folder, no result), the last two never begun."""
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        writer.emit("job.start", n_total=5, tasks=[f"t-{k}" for k in range(5)])
        for k in range(5):
            writer.emit("trial.queued", f"t-{k}__u{k:04d}", agent="fake", position=k)
        writer.emit("job.end")
        os.remove(d / trial_name(2) / "result.json")
        top = json.loads((d / "result.json").read_text())
        top["n_total_trials"] = 5
        (d / "result.json").write_text(json.dumps(top))

    def test_the_tasks_with_no_result_are_rows_to_tick_under_an_outcome_of_their_own(self):
        d = self.run_of()
        self.interrupted(d)
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        waiting = ["t-2__u0002", "t-3__u0003", "t-4__u0004"]
        self.assertEqual(re.findall(r'<input class="pick-row" type="checkbox" value="([^"]+)"', html), [trial_name(0), trial_name(1)] + waiting)
        self.assertEqual(re.findall(r'<input class="pick-row" type="checkbox" value="([^"]+)" data-first', html), waiting)
        self.assertEqual(text(html, r'<span class="cnt" id="trial-count">.*?</span>'), "5")
        self.assertEqual(text(html, r'<a class="fx" data-o="no_result".*?</a>'), "No result 3")
        rows = {n: text(html, rf'<tr data-outcome="no_result"[^>]*data-trial="{n}".*?</tr>') for n in waiting}
        self.assertEqual(rows, {"t-2__u0002": "t-2 — No result begun, ended with none", "t-3__u0003": "t-3 — No result never run",
                                "t-4__u0004": "t-4 — No result never run"})
        self.assertIn('data-first="On this machine, in this job: a task with no result gets its first;', html)
        for n in waiting:                                                        # the question says "with no result"
            self.assertIn(f'value="{n}" data-first="none"', html)
        r = self.post("r1", {"trials": ["t-3__u0003", trial_name(1)]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("--unit t-3__u0003 --unit t-1__u0001 --from-run", self.asked[-1][1]["script"])
        r = self.post("r1", {"trials": ["t-5__u0005"]})
        self.assertEqual((r.status_code, r.json["error"]), (400, "r1 holds no trial t-5__u0005"))

    def test_a_job_that_ran_on_a_host_is_finished_there(self):
        d = self.run_of(host="bench")
        self.interrupted(d)
        self.open(d.parent)
        self.assertIn('data-first="On bench, in this job:', self.page("/job/r1?view=results"))
        r = self.post("r1", {"trials": ["t-4__u0004", "t-2__u0002"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ((ex, part),) = self.asked
        self.assertEqual((ex.plan.host.label, part["trials"]), ("bench", ["t-4__u0004", "t-2__u0002"]))
        self.assertIn("--unit t-4__u0004 --unit t-2__u0002 --from-run", part["script"])

    def test_the_record_says_which_tasks_were_begun_and_which_had_no_result(self):
        d = self.run_of()
        top = json.loads((d / "result.json").read_text())
        top["reruns"] = [{"at": "2026-11-18T15:40:12+00:00", "finished_at": "2026-11-18T15:42:00+00:00", "trials": [trial_name(1), "t-3__u0003"],
                          "replaced": [{"trial": trial_name(1), "unit": "t-1", "try": 1, "was": {}, "now": {"reward": 1.0}}],
                          "begun": [{"trial": "t-3__u0003", "unit": "t-3", "now": {"reward": 0.0}}], "no_result": [trial_name(1)],
                          "max_usd": None, "spent_usd": 0.0012}]
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<details id="reruns" open>.*?</details>'),
                         "Run later 1 2026-11-18 15:40Z t-1 no result → reward 1.0 try 1 t-3 first run → reward 0.0 "
                         "2 tasks · $0.0012 spent")

    def capped(self, d, budget, reruns=None):
        top = json.loads((d / "result.json").read_text())
        top["budget"] = budget
        if reruns is not None:
            top["reruns"] = reruns
        (d / "result.json").write_text(json.dumps(top))

    def cap(self, name, body):
        r = self.client.post(f"/job/{name}/cap", json=body, headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
        self.addCleanup(r.close)
        return r

    def test_the_page_shows_the_job_s_one_cap_and_a_change_holds_from_the_next_part(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 4.0, "spent_usd": 0.31, "stopped": False, "cut_off": [], "not_run": []})
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $4.00")
        self.assertEqual(text(html, r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'), "$0.31 spent · Change")
        self.assertEqual(text(html, r'<p class="small" id="rerun-budget">.*?</p>'), "Budget for the whole run: $4.00, $3.69 left.")
        self.assertEqual(text(html, r'<dialog class="std-how rerun-ask" id="cap-ask".*?</dialog>'),
                         "Spending cap for the whole run $0.31 spent so far. The new cap applies from the next part. Cap, USD Cancel Save")
        r = self.cap("r1", {"max_usd": "6"})
        self.assertEqual((r.status_code, r.json["left_usd"]), (200, 5.69), r.get_data(as_text=True))
        record = json.loads((self.tmp / "runs" / "r1" / "run.json").read_text())
        self.assertEqual(record["plan"]["caps"], {"r1": 6.0})                     # kept with the run, for its next part
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $6.00")
        self.assertEqual(text(html, r'<p class="small" id="rerun-budget">.*?</p>'), "Budget for the whole run: $6.00, $5.69 left.")
        self.assertEqual(self.post("r1", {"trials": [trial_name(0)]}).status_code, 200)
        self.assertIn(f"--unit {trial_name(0)} --max-usd 6 --from-run", self.asked[-1][1]["script"])
        # the part recorded it: the job's own cap now, and the page's is dropped
        self.capped(d, {"max_usd": 6.0, "spent_usd": 0.31, "spent_total_usd": 0.4, "closed": False,
                        "changes": [{"at": "2026-11-20T20:00:00+00:00", "max_usd": 6.0, "was": 4.0}]})
        self.assertEqual(self.post("r1", {"trials": [trial_name(1)]}).status_code, 200)
        self.assertIn(f"--unit {trial_name(1)} --from-run", self.asked[-1][1]["script"])
        self.assertEqual(json.loads((self.tmp / "runs" / "r1" / "run.json").read_text())["plan"]["caps"], {})
        self.assertEqual(text(self.page("/job/r1?view=results"), r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'), "$0.40 spent · Change")

    def test_a_change_is_refused_at_or_under_what_was_spent_and_while_a_part_runs(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 4.0, "spent_usd": 0.31, "stopped": False, "cut_off": [], "not_run": []})
        self.open(d.parent)
        for body, said in [({"max_usd": "0.31"}, "The cap must be above what this run has spent, 0.31 USD."),
                           ({"max_usd": "0"}, "The spending cap is an amount in USD above zero."),
                           ({"max_usd": "lots"}, "The spending cap is an amount in USD above zero."),
                           ({}, "The spending cap is an amount in USD above zero.")]:
            r = self.cap("r1", body)
            self.assertEqual((r.status_code, r.json["error"]), (400, said), body)
        ex = execution.EXECUTIONS["r1-1"]
        ex.plan.parts.append({"id": "again-1", "label": "1 task of r1 again", "again": "r1", "trials": [trial_name(1)], "script": "true"})
        ex.part, ex.state, ex.launched = 1, "running", "again-1"
        r = self.cap("r1", {"max_usd": "6"})
        self.assertEqual((r.status_code, r.json["error"]), (409, "Still running: more tasks can be started once it has ended."))
        self.assertEqual(text(self.page("/job/r1?view=results"), r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'),
                         "$0.31 spent · change it when this part ends")
        self.assertEqual(ex.plan.caps, {})

    def test_a_job_whose_cap_is_spent_is_refused_and_the_question_says_so(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 0.5, "spent_usd": 0.5, "stopped": True, "cut_off": [], "not_run": []})
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<p class="small" id="rerun-budget">.*?</p>'), "Budget for the whole run: $0.50, all spent. Change it to run more.")
        self.assertIn("data-go data-spent>", html)
        r = self.post("r1", {"trials": [trial_name(0)]})
        self.assertEqual((r.status_code, r.json["error"]),
                         (409, "The job's spending cap of 0.50 USD is spent (0.50 spent): change it to run more."))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.cap("r1", {"max_usd": "1"}).status_code, 200)
        self.assertEqual(self.post("r1", {"trials": [trial_name(0)]}).status_code, 200)
        self.assertIn("--max-usd 1 --from-run", self.asked[-1][1]["script"])

    def test_a_job_whose_last_part_stopped_at_its_cap_is_refused_until_it_changes(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 0.8, "spent_usd": 0.5445, "stopped": False, "cut_off": [], "not_run": [],
                        "spent_total_usd": 0.726, "closed": True})
        self.open(d.parent)
        self.assertEqual(text(self.page("/job/r1?view=results"), r'<p class="small" id="rerun-budget">.*?</p>'),
                         "Budget for the whole run: $0.80, $0.726 spent; the last part stopped at it. Change it to run more.")
        r = self.post("r1", {"trials": [trial_name(0)]})
        self.assertEqual((r.status_code, r.json["error"]), (409, "The job's spending cap of 0.80 USD is spent (0.726 spent, and a "
                                                                 "call did not fit what was left): change it to run more."))
        self.assertEqual(self.asked, [])

    def test_an_older_job_with_caps_of_its_own_parts_shows_what_every_part_spent(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 0.5, "spent_usd": 0.1, "stopped": False, "cut_off": [], "not_run": []},
                    reruns=[{"at": "2026-11-18T15:40:12+00:00", "trials": [trial_name(1)], "replaced": [],
                             "max_usd": 2.0, "spent_usd": 0.0012}])
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $0.50")
        self.assertEqual(text(html, r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'), "$0.1012 spent · Change")
        later = text(html, r'<details id="reruns" open>.*?</details>')
        self.assertIn("1 task · $0.0012 spent", later)
        self.assertNotIn("$2.00", later)                                          # that part's own cap: the job has one now
        self.assertTrue(later.endswith("Spending cap for the whole run $0.50 · $0.1012 spent"), later)

    def test_run_later_and_the_stop_notice_name_the_job_s_cap_and_spend_as_its_cap_line_does(self):
        d = self.run_of()
        part = lambda k, spent, stopped: {"at": f"2026-11-20T20:3{k}:00+00:00", "trials": [trial_name(k)], "replaced": [],
                                          "max_usd": 0.8, "left_usd": 0.2555, "spent_usd": spent, "stopped": stopped}
        # the first run stopped at its cap; a later part, under the same cap, stopped at it too
        self.capped(d, {"max_usd": 0.8, "spent_usd": 0.5445, "stopped": True, "cut_off": [], "not_run": [trial_name(2)],
                        "spent_total_usd": 0.726, "closed": True}, reruns=[part(1, 0.1815, True)])
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        line = text(html, r'<div class="kv" id="run-cap".*?</div>') + " · " + text(html, r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>')
        self.assertEqual(line, "Spending cap $0.80 · $0.726 spent · Change")
        self.assertEqual(text(html, r'<div class="alert" id="cap-stopped">.*?</div>'),
                         "Stopped at its spending cap of $0.80, with $0.726 spent.")         # the job's, not the first run's $0.5445
        later = text(html, r'<details id="reruns" open>.*?</details>')
        self.assertIn("1 task · $0.18 spent", later)
        self.assertNotIn("of a $0.80 cap", later)
        self.assertTrue(later.endswith("Spending cap for the whole run $0.80 · $0.726 spent"), later)
        # the cap changed on the page: the job is no longer stopped at it, and every line names the new cap
        self.assertEqual(self.cap("r1", {"max_usd": "2"}).status_code, 200)
        html = self.page("/job/r1?view=results")
        self.assertNotIn('id="cap-stopped"', html)
        self.assertEqual(text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap $2.00")
        self.assertTrue(text(html, r'<details id="reruns" open>.*?</details>').endswith("Spending cap for the whole run $2.00 · $0.726 spent"))

    def test_a_first_run_stopped_at_its_cap_and_a_later_part_that_ended_within_it_show_no_stop(self):
        d = self.run_of()
        self.capped(d, {"max_usd": 2.0, "spent_usd": 0.5445, "stopped": True, "cut_off": [], "not_run": [],
                        "spent_total_usd": 1.089, "closed": False,
                        "changes": [{"at": "2026-11-20T20:40:00+00:00", "max_usd": 2.0, "was": 0.8}]},
                    reruns=[{"at": "2026-11-20T20:41:00+00:00", "trials": [trial_name(1)], "replaced": [],
                             "max_usd": 2.0, "left_usd": 1.274, "spent_usd": 0.363, "stopped": False}])
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertNotIn('id="cap-stopped"', html)

    def test_a_cap_equal_to_the_spend_the_page_shows_is_refused(self):
        d = self.run_of()
        noisy = 0.0
        for _ in range(8):
            noisy += 0.1                                                         # 0.7999999999999999, as a ledger adds its calls
        self.capped(d, {"max_usd": 0.5, "spent_usd": noisy, "stopped": True, "cut_off": [], "not_run": [], "spent_total_usd": noisy})
        self.open(d.parent)
        self.assertEqual(text(self.page("/job/r1?view=results"), r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'), "$0.80 spent · Change")
        for typed in ("0.8", "0.80000000001"):
            with self.subTest(typed=typed):
                r = self.cap("r1", {"max_usd": typed})
                self.assertEqual((r.status_code, r.json["error"]), (400, "The cap must be above what this run has spent, 0.80 USD."))
        self.assertEqual(self.cap("r1", {"max_usd": "0.81"}).status_code, 200)

    def test_a_job_with_no_cap_has_none_until_one_is_set(self):
        d = self.run_of()
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<div class="kv" id="run-cap".*?</div>'), "Spending cap none")
        self.assertEqual(text(html, r'<div class="sp cap-spent small muted" id="cap-spent">.*?</div>'), "Set")
        self.assertNotIn('id="rerun-budget"', html)
        self.assertEqual(self.post("r1", {"trials": [trial_name(0)]}).status_code, 200)
        self.assertNotIn("--max-usd", self.asked[-1][1]["script"])
        self.assertEqual(self.cap("r1", {"max_usd": "2"}).status_code, 200)
        self.assertIn("Budget for the whole run: $2.00, $2.00 left.", self.page("/job/r1?view=results"))

    def test_a_job_never_run_again_shows_none_of_it(self):
        d = self.run_of()
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        for absent in ('id="reruns"', 'class="num">Retries<', "run-retries", "again-now", "running again", "no_result", "data-first"):
            self.assertNotIn(absent, html)
        trial = self.page(f"/trial/r1/{trial_name(1)}")
        self.assertNotIn("try-tabs", trial); self.assertNotIn("try-why", trial)


if __name__ == "__main__":
    unittest.main()
