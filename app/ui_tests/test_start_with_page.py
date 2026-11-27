"""A job planned on a whole and started on some of it, on its page: how many of the plan have run, the tasks not
run as rows with no attempt under an outcome of their own, the rest and a set's tasks not run each one click from
the question Run selected asks, and the runs list's count over the trials run."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nullius.harbor import feed
from nullius.standard.runner import sets
from ui_tests.test_capped_job import BENCH
from ui_tests import test_rerun_page as rerun_page, test_task_set_pages as task_set_pages
from ui_tests.test_rerun_page import text, trial_name

PLAN = [f"t-{k}" for k in range(6)]


class StartWithPage(unittest.TestCase):
    run_of, open, page, post, interrupted = (rerun_page.RerunPage.__dict__[k] for k in ("run_of", "open", "page", "post", "interrupted"))

    def setUp(self):
        rerun_page.RerunPage.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_CATALOGUE": str(self.tmp / "catalogue")}))

    def started_on_some(self, d: Path):
        """The job as ``run --start-with 3`` leaves it: six tasks planned, as its feed and its config say, the first
        three run."""
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        writer.emit("job.start", n_total=6, tasks=PLAN)
        for k in range(6):
            writer.emit("trial.queued", f"t-{k}__u{k:04d}", agent="fake", position=k)
        writer.emit("job.end")
        cfg = json.loads((d / "config.json").read_text())
        cfg["standard"]["start_with"] = {"count": 3, "units": PLAN[:3]}
        cfg["datasets"][0]["n_tasks"] = 6
        (d / "config.json").write_text(json.dumps(cfg))
        top = json.loads((d / "result.json").read_text())
        top["n_total_trials"] = 6
        (d / "result.json").write_text(json.dumps(top))

    def test_the_page_says_how_many_of_the_plan_ran_and_lists_the_rest_with_no_attempt(self):
        d = self.run_of()
        self.started_on_some(d)
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<span id="plan-run".*?</span>'), "3 of 6 planned tasks run")
        self.assertEqual(text(html, r'<span class="k">Trials</span>.*?</div>'), "Trials 3 of 6 planned tasks run")   # the head's Trials cell
        self.assertEqual(text(html, r'<dt>tasks run</dt><dd id="tasks-run".*?</dd>'), "tasks run 3 of 6")
        self.assertIn('id="tasks-run" class="warn"', html)
        self.assertEqual(text(html, r'<div class="row tot">.*?</div>'), "3 Trials of 6 planned")
        self.assertEqual(text(html, r'<a class="fx" data-o="not_run".*?</a>'), "Not run 3")
        self.assertIn('data-o="not_run" href="/job/r1?outcome=not_run"', html)     # a link to the job's record, as the outcome links are
        self.assertNotIn('data-o="no_result"', html)
        rest = [f"t-{k}__u{k:04d}" for k in (3, 4, 5)]
        rows = {n: text(html, rf'<tr data-outcome="not_run"[^>]*data-trial="{n}".*?</tr>') for n in rest}
        self.assertEqual(rows, {n: f"{n[:3]} — Not run" for n in rest})
        self.assertEqual(re.findall(r'<input class="pick-row" type="checkbox" value="([^"]+)" data-first', html), rest)
        self.assertEqual(text(html, r'<span class="cnt" id="trial-count">.*?</span>'), "6")
        self.assertRegex(html, r'<button class="btn sm ghost" id="pick-rest" type="button" aria-haspopup="dialog" '
                               r'data-pick="t-3__u0003 t-4__u0004 t-5__u0005">Run the rest · 3</button>')
        self.assertNotIn('id="plan-sets"', html)                                 # no set names one
        # the runs list: solved over the trials run, and how many of the plan
        runs = self.page("/runs")
        self.assertIn('title="Started on some of its plan: the rates and the mean are over the tasks run">3 of 6 tasks run</span>', runs)
        self.assertIn("3 on disk of 6 planned · started on some of its plan, 3 of 6 tasks run", self.page("/runs?view=all"))

    def test_a_set_s_tasks_not_run_and_the_rest_go_to_the_question_as_ticks(self):
        d = self.run_of()
        self.started_on_some(d)
        both = dict(BENCH, datasets=[{"id": "all"}, {"id": "other"}], label="Fake bench")
        sets.create(both, "late", dataset="all", units=["t-4", "t-0"])
        sets.create(both, "done", dataset="all", units=["t-1"])                  # every task of it has run
        sets.create(both, "elsewhere", dataset="other", units=["t-5"])           # another dataset
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        menu = re.search(r'<details class="pop plan-sets" id="plan-sets">.*?</details>', html, re.S)[0]
        self.assertEqual(re.findall(r'data-pick="([^"]*)">([^<]*)<b>(\d+)</b>', menu), [("t-4__u0004", "late", "1")])
        self.assertNotIn("revision", menu)
        self.assertEqual(text(html, r'<summary class="btn sm ghost" aria-haspopup="true">.*?</summary>'), "Run a set")
        # what a set's or the rest's click ticks is posted as Run selected posts it: the path a re-run takes
        r = self.post("r1", {"trials": ["t-4__u0004"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("--unit t-4__u0004 --from-run", self.asked[-1][1]["script"])
        r = self.post("r1", {"trials": ["t-3__u0003", "t-4__u0004", "t-5__u0005"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("--unit t-3__u0003 --unit t-4__u0004 --unit t-5__u0005 --from-run", self.asked[-1][1]["script"])

    def test_on_a_host_the_later_runs_go_back_there(self):
        d = self.run_of(host="bench")
        self.started_on_some(d)
        self.open(d.parent)
        self.assertIn('data-first="On bench, in this job:', self.page("/job/r1?view=results"))
        r = self.post("r1", {"trials": ["t-5__u0005"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ((ex, part),) = self.asked
        self.assertEqual((ex.plan.host.label, part["trials"]), ("bench", ["t-5__u0005"]))

    def test_one_unrun_row_selects_every_planned_attempt_by_tick_set_or_rest(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the selection controls")
        d = self.run_of(repeat=2)
        self.started_on_some(d)
        shutil.rmtree(d / trial_name(2))
        for k in range(2):
            for attempt in (1, 2):
                dest = d / f"{trial_name(k)}r{attempt}"
                shutil.copytree(d / trial_name(k), dest)
                result = json.loads((dest / "result.json").read_text())
                result["trial_name"] = dest.name
                (dest / "result.json").write_text(json.dumps(result))
            shutil.rmtree(d / trial_name(k))
        cfg = json.loads((d / "config.json").read_text())
        cfg["standard"]["start_with"] = {"count": 2, "units": PLAN[:2]}
        (d / "config.json").write_text(json.dumps(cfg))
        top = json.loads((d / "result.json").read_text())
        top["n_total_trials"] = 12
        (d / "result.json").write_text(json.dumps(top))
        (d / "events.jsonl").unlink()
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        writer.emit("job.start", n_total=12, tasks=PLAN)
        for k in range(6):
            for attempt in (1, 2):
                writer.emit("trial.queued", f"t-{k}__u{k:04d}r{attempt}", agent="fake", position=k)
        writer.emit("job.end")
        sets.create(dict(BENCH, datasets=[{"id": "all"}]), "late", dataset="all", units=["t-4", "t-0"])
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<span id="plan-run".*?</span>'), "2 of 6 planned tasks run")
        rows = re.findall(r'<tr data-outcome="not_run".*?</tr>', html, re.S)
        self.assertEqual(len(rows), 4)
        self.assertEqual([text(row, r".*") for row in rows], [f"t-{k} — Not run" for k in range(2, 6)])
        ticks = [dict(value=re.search(r'value="([^"]+)"', row)[1],
                      trials=re.search(r'data-trials="([^"]+)"', row)[1]) for row in rows]
        picks = re.findall(r'data-pick="([^"]+)"', html)
        board = Path(__file__).resolve().parents[1] / "static/runs/board.js"
        program = r"""
const [file, values, picks] = process.argv.slice(1).map(JSON.parse), elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {dataset: {}, listeners: {}, textContent: '',
    addEventListener(k, fn) { this.listeners[k] = fn; }, click() { this.listeners.click(); },
    replaceChildren() {}, showModal() {}, closest() { return null; }});
  return elements.get(id);
}
const ticks = values.map(x => Object.assign(element(x.value), {value: x.value,
  dataset: {first: '', trials: x.trials}, getAttribute() { return 'Choose ' + x.value; }}));
const buttons = picks.map(pick => Object.assign(element('button:' + pick), {dataset: {pick}})), posted = [];
element('form').elements = {reason: {value: ''}, max_usd: {value: ''}};
const source = require('node:fs').readFileSync(file, 'utf8');
new Function('$', '$$', 'rows', 'document', 'nulliusFetch', 'URL', 'location',
  source.slice(source.indexOf('  const ticks ='), source.lastIndexOf('  apply();')))(
  element, s => s === '#trial-rows .pick-row' ? ticks : buttons, [], {createElement: () => ({})},
  async (url, options) => { posted.push(JSON.parse(options.body)); return {ok: true, json: async () => ({ok: true})}; },
  URL, {href: 'http://localhost/job/r1', assign() {}});
(async () => {
  ticks[0].checked = true; element('#rerun-open').click();
  const submit = () => element('form').listeners.submit({submitter: {value: 'run'}, preventDefault() {}});
  await submit();
  for (const button of buttons) { button.click(); await submit(); }
  process.stdout.write(JSON.stringify(posted));
})();
"""
        posted = json.loads(subprocess.run([node, "-e", program, json.dumps(str(board)), json.dumps(ticks), json.dumps(picks)],
                                          capture_output=True, text=True, check=True, timeout=10).stdout)
        expected = [[f"t-{k}__u{k:04d}r{attempt}" for k in units for attempt in (1, 2)]
                    for units in ([2], [4], range(2, 6))]
        self.assertEqual([body["trials"] for body in posted], expected)
        for body in posted:
            response = self.post("r1", body)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(self.asked[-1][1]["trials"], body["trials"])

    def test_tasks_never_run_get_one_short_question_and_a_reason_is_asked_only_beside_a_result(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the question")
        d = self.run_of()
        self.started_on_some(d)
        top = json.loads((d / "result.json").read_text())
        top["budget"] = {"max_usd": 4.0, "spent_usd": 0.31, "stopped": False, "cut_off": [], "not_run": []}
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertIn('data-where="this machine"', html)
        self.assertEqual(text(html, r'<p class="small" id="rerun-budget">.*?</p>'), "Budget for the whole run: $4.00, $3.69 left.")
        self.assertNotIn('name="max_usd" min="0" step="any" inputmode="decimal">', html.split('id="cap-ask"')[0])   # no cap asked
        ticks = [dict(value=v, first=f) for v, f in re.findall(r'<input class="pick-row" type="checkbox" value="([^"]+)"( data-first)?', html)]
        board = Path(__file__).resolve().parents[1] / "static/runs/board.js"
        program = r"""
const [file, values, picks] = process.argv.slice(1).map(JSON.parse), elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {dataset: {}, listeners: {}, textContent: '', hidden: false,
    addEventListener(k, fn) { this.listeners[k] = fn; }, click() { this.listeners.click(); },
    replaceChildren(...xs) { this.children = xs; }, showModal() {}, closest() { return null; }});
  return elements.get(id);
}
element('#rerun-ask').dataset.where = 'this machine';
const ticks = values.map(x => Object.assign(element(x.value), {value: x.value,
  dataset: x.first ? {first: ''} : {}, getAttribute() { return 'Choose ' + x.value; }}));
element('form').elements = {reason: {value: 'typed before'}};
const source = require('node:fs').readFileSync(file, 'utf8');
new Function('$', '$$', 'rows', 'document', 'nulliusFetch', 'URL', 'location',
  source.slice(source.indexOf('  const ticks ='), source.lastIndexOf('  apply();')))(
  element, s => s === '#trial-rows .pick-row' ? ticks : [], [], {createElement: () => ({})},
  async () => ({ok: true, json: async () => ({ok: true})}), URL, {href: 'http://localhost/job/r1', assign() {}});
const out = [];
for (const pick of picks) {
  ticks.forEach(x => { x.checked = pick.includes(x.value); });
  element('#rerun-open').click();
  out.push({head: element('#rerun-ask-h').textContent, go: element('[data-go]').textContent, reason: !element('[data-reason]').hidden,
            names: (element('[data-names]').children || []).length, where: !element('p.muted').hidden,
            typed: element('form').elements.reason.value});
}
process.stdout.write(JSON.stringify(out));
"""
        rest = [t["value"] for t in ticks if t["first"]]
        picks = [rest, rest[:1], [trial_name(0), rest[0]], [trial_name(0)]]
        got = json.loads(subprocess.run([node, "-e", program, json.dumps(str(board)), json.dumps(ticks), json.dumps(picks)],
                                        capture_output=True, text=True, check=True, timeout=10).stdout)
        short = dict(go="Run", reason=False, names=0, where=False, typed="")
        self.assertEqual(got[0], dict(short, head="Run the 3 tasks not yet run, on this machine?"))
        self.assertEqual(got[1], dict(short, head="Run the 1 task not yet run, on this machine?"))
        self.assertEqual(got[2], dict(head="Run 2 tasks, 1 again", go="Run 2 tasks, 1 again", reason=True, names=2, where=True, typed=""))
        self.assertEqual(got[3], dict(head="Run 1 task again", go="Run 1 task again", reason=True, names=1, where=True, typed=""))

    def test_while_a_run_of_some_of_the_plan_goes_on_the_board_queues_its_tasks_and_says_not_run_for_the_rest(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the board")
        d = self.tmp / "loose" / "r1"                                            # no run record: the feed alone says it runs
        rerun_page.standard_job(d, "r1")
        self.started_on_some(d)
        board = Path(__file__).resolve().parents[1] / "static/runs/live-board.js"
        program = ("const L=require(" + json.dumps(str(board)) + ");const s=JSON.parse(process.argv[1]);"
                   "process.stdout.write(JSON.stringify(L.render(s,{selected:'t-5__u0005'})));")
        name = lambda k: f"t-{k}__u{k:04d}"
        first = [("job.start", None, dict(n_total=6, tasks=PLAN))] + [("trial.queued", name(k), dict(position=k)) for k in range(6)]
        later = [("trial.end", name(k), dict(outcome="resolved", reward=1.0)) for k in range(3)]
        later += [("job.end", None, {}), ("job.start", None, dict(n_total=6, tasks=["t-4"])), ("trial.queued", name(4), dict(position=0))]
        for events, this_run, said in [(first + [("trial.phase", name(0), dict(task="t-0", phase="agent_run"))], [name(k) for k in range(3)],
                                    "Done 0 /6 Running 1 Queued 2 Not run 3"),
                                   (first + later, [name(4)], "Done 3 /6 Running 0 Queued 1 Not run 2")]:
            (d / "events.jsonl").unlink()
            writer = feed.Writer(str(d / "events.jsonl"), "r1")
            for event, trial, data in events:
                writer.emit(event, trial, **data)
            self.open(d.parent)
            snap = json.loads(self.page("/job/r1/live"))
            self.assertEqual((snap["state"], snap["this_run"]), ("running", this_run))
            out = json.loads(subprocess.run([node, "-e", program, json.dumps(snap)], capture_output=True, text=True,
                                            check=True, timeout=10).stdout)
            self.assertEqual(text(out["stats"], r".*").split(" Rate")[0], said)
            self.assertIn('u0005: not run"', out["tiles"])
            self.assertIn('u0002: queued"' if len(this_run) == 3 else 'u0002: Resolved"', out["tiles"])
            self.assertTrue(text(out["drawer"], r"<h3>.*?</h3>").endswith("Not run"), out["drawer"])
            initial = re.search(r'<script id="live-board-initial" type="application/json">(.*?)</script>', self.page("/job/r1?view=live"), re.S)[1]
            self.assertEqual(json.loads(initial)["this_run"], this_run)                  # the page's first draw counts as its polls do
        writer.emit("job.end")                                                   # ended: the board's own count, no run of the plan
        self.assertNotIn("this_run", json.loads(self.page("/job/r1/live")))

    def test_while_a_later_run_goes_on_each_of_its_tasks_not_begun_has_a_row_marked_queued(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the board")
        d = self.tmp / "loose" / "r1"
        rerun_page.standard_job(d, "r1")
        self.started_on_some(d)
        board = Path(__file__).resolve().parents[1] / "static/runs/live-board.js"
        program = ("const L=require(" + json.dumps(str(board)) + ");const s=JSON.parse(process.argv[1]);"
                   "process.stdout.write(JSON.stringify(L.render(s,{})));")
        name = lambda k: f"t-{k}__u{k:04d}"
        events = [("job.start", None, dict(n_total=6, tasks=PLAN))] + [("trial.queued", name(k), dict(position=k)) for k in range(6)]
        events += [("trial.end", name(k), dict(outcome="resolved", reward=1.0)) for k in range(3)]
        events += [("job.end", None, {}), ("job.start", None, dict(n_total=6, tasks=["t-3", "t-4"])),
                   ("trial.queued", name(3), dict(position=0)), ("trial.phase", name(3), dict(task="t-3", phase="agent_run"))]
        (d / "events.jsonl").unlink()
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        for event, trial, data in events:
            writer.emit(event, trial, **data)
        self.open(d.parent)
        snap = json.loads(self.page("/job/r1/live"))
        out = json.loads(subprocess.run([node, "-e", program, json.dumps(snap)], capture_output=True, text=True,
                                        check=True, timeout=10).stdout)
        rows = {re.search(r'data-trial="([^"]+)"', row)[1]: text(row, r".*")
                for row in re.findall(r'<tr class="lb-row.*?</tr>', out["flight"], re.S)}
        self.assertEqual(sorted(rows), [name(k) for k in range(5)])               # t-5 is outside the run: no row
        self.assertEqual(rows[name(4)], "t-4 Queued —")                          # its name, less the runner's tag
        self.assertIn('class="lb-row lb-queued"', out["flight"])
        self.assertTrue(rows[name(3)].startswith("t-3 agent run"), rows[name(3)])
        self.assertEqual(text(out["stats"], r".*").split(" Rate")[0], "Done 3 /6 Running 1 Queued 1 Not run 1")
        # a later run that runs a task again beside one never run: the one never run is counted queued, as its row says
        events[-4:] = [("job.end", None, {}), ("job.start", None, dict(n_total=6, tasks=["t-2", "t-4"])),
                       ("trial.phase", name(2), dict(task="t-2", phase="agent_run"))]
        (d / "events.jsonl").unlink()
        writer = feed.Writer(str(d / "events.jsonl"), "r1")
        for event, trial, data in events:
            writer.emit(event, trial, **data)
        out = json.loads(subprocess.run([node, "-e", program, self.page("/job/r1/live")], capture_output=True, text=True,
                                        check=True, timeout=10).stdout)
        self.assertEqual(text(out["stats"], r".*").split(" Rate")[0], "Done 3 /6 Running 1 Queued 1 Not run 2")
        self.assertEqual(re.findall(r'<tr class="lb-row lb-queued" data-trial="([^"]+)"', out["flight"]), [name(4)])

    def test_a_task_first_run_in_a_later_run_names_that_try_by_what_it_was(self):
        d = self.run_of()
        name, first, again = trial_name(1), "2026-11-18T15:40:12+00:00", "2026-11-18T15:50:00+00:00"
        (d / "retried").mkdir()
        shutil.copytree(d / name, d / "retried" / f"{name}.try-1")
        for path, mark in ((d / "retried" / f"{name}.try-1", {"at": first, "reason": "the rest of the plan", "begun": True}),
                           (d / name, {"at": again, "reason": "the provider failed", "replaced": {"reward": 0.0}})):
            res = json.loads((path / "result.json").read_text())
            res["standard"] = dict(res["standard"], retries=int(path == d / name), rerun=dict(mark, by="hand"))
            (path / "result.json").write_text(json.dumps(res))
        top = json.loads((d / "result.json").read_text())
        top["reruns"] = [{"at": first, "finished_at": first, "reason": "the rest of the plan", "trials": [name], "replaced": [],
                          "begun": [{"trial": name, "unit": "t-1", "now": {"reward": 0.0}}]},
                         {"at": again, "finished_at": again, "reason": "the provider failed", "trials": [name],
                          "replaced": [{"trial": name, "unit": "t-1", "try": 1, "was": {"reward": 0.0}, "now": {"reward": 0.0}}]}]
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        trial = self.page(f"/trial/r1/{name}")
        tabs = text(trial, r'<nav class="try-tabs".*?</nav>')
        self.assertRegex(tabs, r"^Try 1 first run, in a later run of the job \S+ Try 2 run again by hand"); self.assertNotIn("Try 1 run again", tabs)
        self.assertRegex(trial, r'title="Try 1: [^"]*, first run, in a later run of the job: the rest of the plan"')
        self.assertEqual(text(trial, r'<p class="try-why muted small".*?</p>'), "Run again by hand: the provider failed")
        earlier = self.page(f"/trial/r1/{name}.try-1")
        self.assertEqual(text(earlier, r'<p class="try-why muted small".*?</p>'), "First run, in a later run of the job: the rest of the plan")

    def test_first_run_label_strips_only_the_archive_suffix(self):
        name = "case.try-1__u0000"
        job = SimpleNamespace(reruns=[{"at": "later", "begun": [{"trial": name}]}])
        mark = rerun_page.web.app.jinja_env.filters["try_mark"]
        for suffix in ("", ".try-1", ".try-12", ".try-note", ".try-1.more"):
            with self.subTest(suffix=suffix):
                trial = SimpleNamespace(trial_name=name + suffix, rerun={"at": "later"})
                expected = "run again by hand" if suffix in (".try-note", ".try-1.more") else "first run, in a later run of the job"
                self.assertEqual(mark(trial, job), expected)
        self.assertEqual(mark(SimpleNamespace(trial_name=name, rerun={"at": "again"}), job), "run again by hand")

    def test_a_later_run_contains_only_the_selected_attempt(self):
        from nullius.runs import live, parse
        from nullius.standard.runner import rerun, run
        from tests import test_standard_rerun as fixtures

        fixture = fixtures.RerunRecords()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path = run.run(fixture.bench, fixture.other, model="", provider="custom",
                       jobs_root=str(fixture.tmp), name="selected-attempt", concurrency=1,
                       units_file=dict(fixtures.UNITS, units=["good", "wrong"]),
                       start_with={"count": 1}, repeat=2, env={}, say=lambda _: None)
        selected = "wrong__u0001r1"
        snapshots = []
        phase = run.Run.phase

        def observe(runner, name, unit, stage, timeout=None):
            phase(runner, name, unit, stage, timeout)
            if name == selected:
                job, snap = parse.parse_job(path), live.snapshot(path)
                snapshots.append(rerun_page.web.runs._with_this_run(job, snap))
                record = SimpleNamespace(again_of=lambda _: {"trials": [selected]})
                with patch.object(rerun_page.web.runs.execution, "for_job", return_value=record):
                    self.assertEqual(rerun_page.web.runs._with_this_run(job, snap)["this_run"], [selected])

        with patch.object(run.Run, "phase", observe):
            result = rerun.rerun(path, [selected], root=str(fixture.root), env={}, say=lambda _: None)
        self.assertEqual(result["trials"], [selected])
        self.assertTrue(snapshots)
        for snapshot in snapshots:          # no record of the choice: every attempt of the task the start names
            self.assertEqual(snapshot["this_run"], [selected, "wrong__u0001r2"])

    def test_run_later_counts_the_tasks_of_a_later_run_not_their_attempts(self):
        d = self.run_of()
        top = json.loads((d / "result.json").read_text())
        top["reruns"] = [{"at": "2026-11-18T15:40:12+00:00", "finished_at": "2026-11-18T15:42:00+00:00", "reason": "the rest of the plan",
                          "trials": [f"t-{k}__u{k:04d}r{a}" for k in (3, 4) for a in (1, 2)], "replaced": [],
                          "begun": [{"trial": f"t-{k}__u{k:04d}r{a}", "unit": f"t-{k}", "now": {"reward": 1.0}} for k in (3, 4) for a in (1, 2)],
                          "max_usd": None, "spent_usd": 0.0012}]
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertTrue(text(html, r'<details id="reruns" open>.*?</details>').endswith("2 tasks · $0.0012 spent"))

    def test_the_duration_adds_each_later_run_s_time_to_the_first_s(self):
        d = self.run_of()                                                        # its first run: one minute
        top = json.loads((d / "result.json").read_text())
        top["reruns"] = [{"at": "2026-11-18T15:40:00+00:00", "finished_at": "2026-11-18T15:46:00+00:00", "trials": ["t-3__u0003"],
                          "replaced": [], "begun": [{"trial": "t-3__u0003", "unit": "t-3", "now": {"reward": 1.0}}]},
                         {"at": "2026-11-18T16:00:00+00:00", "finished_at": "2026-11-18T16:02:00+00:00", "trials": ["t-0__u0000"],
                          "replaced": [{"trial": "t-0__u0000", "unit": "t-0", "try": 1, "was": {}, "now": {}}]}]
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<div class="kv"[^>]*><span class="k">Duration</span>.*?</div>'), "Duration 9m")
        self.assertIn('<span class="k">Duration</span><span class="v">9m</span>', html)   # the head says the same
        top["reruns"] = []
        (d / "result.json").write_text(json.dumps(top))
        self.open(d.parent)
        self.assertEqual(text(self.page("/job/r1?view=results"), r'<div class="kv"[^>]*><span class="k">Duration</span>.*?</div>'), "Duration 1m")

    def test_a_job_run_whole_shows_none_of_it(self):
        d = self.run_of()
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        for absent in ('id="plan-run"', 'id="tasks-run"', 'id="pick-rest"', 'id="plan-sets"', "not_run"):
            self.assertNotIn(absent, html)
        self.assertNotIn("tasks run</span>", self.page("/runs"))

    def test_an_interrupted_job_keeps_no_result_and_offers_the_rest(self):
        d = self.run_of()
        self.interrupted(d)
        self.open(d.parent)
        html = self.page("/job/r1?view=results")
        self.assertEqual(text(html, r'<a class="fx" data-o="no_result".*?</a>'), "No result 3")
        self.assertNotIn('data-o="not_run"', html)
        self.assertIn(f'data-pick="{trial_name(2)} t-3__u0003 t-4__u0004">Run the rest · 3</button>', html)


class TheBuilder(task_set_pages.BuilderCase):
    """Start with in the builder: All of it or Specific tasks, ticked in a dialog holding the tasks page's list; Review,
    compiled by the server, says what runs first and what stays for later; the core keeps the choice in the recipe,
    drops it with its dataset, its plan and its benchmark, and reads it back from a saved recipe."""

    def test_review_says_what_runs_first_and_what_stays_for_later(self):
        recipe = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
                  "other": "fake", "provider": "openai", "model": "gpt-5-mini", "concurrency": 1, "unsandboxed": True, "where": "local",
                  "start_with": {"all": ["wrong", "good"]}}
        out = self.post("/new-run/runner", {"recipe": recipe})
        self.assertEqual(out["errors"], [])
        self.assertIn(["Dataset", "all · 2 tasks run first (the tasks ticked); the other 2 stay planned, to run later from the job's page"],
                      [r[1:] for r in out["review"]])
        self.assertEqual(out["confirm"], "")                     # no popup asks again
        recipe["start_with"] = {"all": "set:dev"}                # a recipe from before
        out = self.post("/new-run/runner", {"recipe": recipe})
        self.assertIn(["Dataset", "all · 2 tasks run first (task set dev); the other 2 stay planned, to run later from the job's page"],
                      [r[1:] for r in out["review"]])
        out = self.post("/new-run/runner", {"recipe": dict(recipe, start_with={"all": "9"})})
        self.assertEqual([e["message"] for e in out["errors"]], ["Start with 9 tasks: all plans 4."])

    def test_the_core_keeps_start_with_with_its_dataset(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the recipe core")
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        core = Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder-core.js"
        program = (f"const C=require({json.dumps(str(core))})({json.dumps(schema)});\n"
                   "const r=C.fromJSON({version:1,harness:'standard',benchmark:'fakebench',datasets:['all'],others:['fake'],"
                   "start_with:{all:'set:dev@1',gone:'2',bad:3}});\n"
                   "const read=JSON.parse(JSON.stringify(r.start_with));\n"
                   "C.setStartWith(r,'all','');const cleared=r.start_with===undefined;\n"
                   "C.setStartWith(r,'all','3');const count=r.start_with.all;r.datasets=[];C.keepStartWith(r);const dropped=r.start_with===undefined;\n"
                   "r.datasets=['all'];C.setStartWith(r,'all','3');C.setStandardBenchmark(r,'fakememo');\n"
                   "process.stdout.write(JSON.stringify({read,cleared,count,dropped,other:r.start_with===undefined}));")
        got = json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(got, {"read": {"all": "set:dev@1"}, "cleared": True, "count": "3", "dropped": True, "other": True})
        program = (f"const C=require({json.dumps(str(core))})({json.dumps(schema)});\n"
                   "const r=C.fromJSON({version:1,harness:'standard',benchmark:'fakebench',datasets:['all'],others:['fake'],"
                   "start_with:{all:['good','wrong'],bad:[1]}});\n"
                   "const read=JSON.parse(JSON.stringify(r.start_with));\n"
                   "C.setStartWith(r,'all',['slow']);const ticked=r.start_with.all;\n"
                   "C.setTaskSet(r,'all','dev');const replanned=r.start_with===undefined;\n"
                   "process.stdout.write(JSON.stringify({read,ticked,replanned}));")
        got = json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(got, {"read": {"all": ["good", "wrong"]}, "ticked": ["slow"], "replanned": True})

    def test_the_step_offers_all_of_it_and_specific_tasks_alone(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the dataset step")
        sets.create(self.fake, "other", dataset="all", units=["good"])
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        folder = Path(__file__).resolve().parents[1] / "static/builder"
        page = (folder / "command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        program = (f"const schema={json.dumps(schema)};const C=require({json.dumps(str(folder / 'command-builder-core.js'))})(schema);\n"
                   + line("esc") + cut("  const tile = t =>", "  // The grids drawn by the server") + cut("  function startChoice(", "  // Specific tasks:")
                   + "const r=C.createStandard('fakebench'),b=C.standardBench(r),x=b.datasets[0],u={noun:'task',nouns:'tasks'};"
                   + "const all=startChoice(r,b,x,u);C.setStartWith(r,x.id,['good','slow']);const ticked=startChoice(r,b,x,u);"
                   + "C.setStartWith(r,x.id,'2');const older=startChoice(r,b,x,u);"
                   + "process.stdout.write(JSON.stringify({all,ticked,older}));")
        got = json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True, timeout=10).stdout)
        titles = lambda h: re.findall(r"<strong>(.*?)</strong>", h)
        pressed = lambda h: re.findall(r'aria-pressed="true"[^>]*>.*?<strong>(.*?)</strong>', h)
        self.assertEqual((titles(got["all"]), pressed(got["all"])), (["All of it", "Specific tasks"], ["All of it"]))
        self.assertEqual((titles(got["ticked"]), pressed(got["ticked"])), (["All of it", "Specific tasks"], ["Specific tasks"]))
        self.assertIn("2 chosen", got["ticked"])
        self.assertIn('data-std-start-pick="all"', got["ticked"])
        self.assertNotIn("other", task_set_pages.visible(got["all"]))           # the other sets are not offered there
        self.assertEqual(titles(got["older"]), ["All of it", "Specific tasks", "The first 2"])    # a recipe from before, shown chosen

    def test_the_page_holds_the_dialog_with_the_tasks_list(self):
        html = self.get("/new-run").get_data(as_text=True)
        dialog = re.search(r'<dialog id="nb-tasks".*?</dialog>', html, re.S).group(0)
        self.assertIn('id="nb-tasks-done"', dialog)
        template = re.search(r'<template id="nb-tasks-list">(.*?)</template>', html, re.S).group(1)
        for marker in ('data-manual="true"', 'id="tasks-q"', 'id="tasks-cats"', 'id="tasks-rows"', 'id="tasks-match"', 'id="tasks-show"',
                     'id="tasks-bar"', 'id="tasks-state-text"', 'id="tasks-state-link"', 'id="tasks-list-go"', 'id="tasks-paste-open"'):
            self.assertIn(marker, template)
        self.assertIn("catalogue/tasks.js", html)

    def test_deselecting_a_dataset_removes_its_start_choice_before_review(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the dataset control")
        load = task_set_pages.definitions.load
        def with_more(kind, root=None, every=False):
            entries, errors = load(kind, root, every=every)
            if kind == "benchmark":
                entries = [dict(b, datasets=[*b["datasets"], {"id": "more", "units": 4}])
                           if b["key"] == "fakebench" else b for b in entries]
            return entries, errors
        self.stack.enter_context(patch.object(task_set_pages.definitions, "load", with_more))
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S)[1])
        folder = Path(__file__).resolve().parents[1] / "static/builder"
        handler = next(line.strip()[5:] for line in (folder / "command-builder.js").read_text().splitlines()
                       if "else if(d.stdDataset)" in line)
        program = (f"const C=require({json.dumps(str(folder / 'command-builder-core.js'))})({json.dumps(schema)});"
                   "const rec=C.createStandard('fakebench','fake');rec.datasets=['all','more'];"
                   "rec.start_with={all:'1',more:'2'};rec.model='gpt-5-mini';rec.concurrency=1;rec.unsandboxed=true;"
                   f"new Function('C','r','d',{json.dumps(handler)})(C,()=>rec,{{stdDataset:'all'}});"
                   "process.stdout.write(JSON.stringify(rec));")
        recipe = json.loads(subprocess.run([node, "-e", program], capture_output=True, text=True, check=True, timeout=10).stdout)
        self.assertEqual(recipe["start_with"], {"more": "2"})
        self.assertEqual(self.post("/new-run/runner", {"recipe": recipe})["errors"], [])
