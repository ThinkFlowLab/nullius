"""The job page's Filtered score: a switch per kind of accepted flag, off by default, and the adjusted rate of the
job without the trials of the tasks those switches set aside."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.judge import store
from tests.test_reconcile import record
from nullius.runs import failure_analysis as fa
from .test_routes import web


def plain_job(root: Path, job_id: str, rewards: dict):
    """A job of one agent: per trial name, its task and its reward."""
    job = root / job_id
    job.mkdir(parents=True)
    (job / "config.json").write_text(json.dumps({
        "job_name": job_id, "agents": [{"name": "agent-a", "model_name": "model-a"}],
        "datasets": [{"name": "bench", "n_tasks": len({t for t, _ in rewards.values()})}]}))
    (job / "result.json").write_text(json.dumps({"stats": {"n_trials": len(rewards)}}))
    for name, (task, reward) in rewards.items():
        t = job / name
        (t / "verifier").mkdir(parents=True)
        (t / "verifier" / "reward.txt").write_text(str(reward))
        (t / "result.json").write_text(json.dumps({
            "task_name": f"bench/{task}", "trial_name": name, "source": "bench",
            "verifier_result": {"rewards": {"reward": reward}}}))
    return job


class FilteredScore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-filtered-")
        tmp = Path(cls.temp.name)
        # five trials, two solved: 40%; without t2 (flawed) 50%; without t2 and t3 (ambiguous) 67%
        job = plain_job(tmp / "jobs", "run-a", {"t1__1": ("t1", 1), "t1__2": ("t1", 0), "t2__1": ("t2", 0),
                                                "t3__1": ("t3", 0), "t4__1": ("t4", 1)})
        plain_job(tmp / "jobs", "run-b", {"t1__1": ("t1", 1)})
        c = plain_job(tmp / "jobs", "run-c", {"t1__1": ("t1", 1), "t2__1": ("t2", 0)})     # every task flagged
        fa.update(str(c), lambda x: (x.set_flag("t1", "flawed", "x", by="ann"), x.set_flag("t2", "flawed", "x", by="ann")))
        d = plain_job(tmp / "jobs", "run-d", {"t1__1": ("t1", 1)})                         # only a pending flag
        fa.update(str(d), lambda x: x.set_flag("t1", "flawed", "x", by="bot", source="import:feasibility"))
        e = plain_job(tmp / "jobs", "run-e", {"t1__1": ("t1", 1)})                         # a file that cannot be read
        (e / fa.FILE).write_text("{not json")
        f = plain_job(tmp / "jobs", "run-f", {"t1__1": ("t1", 1)})
        fa.update(str(f), lambda x: x.set_flag("t1", "ambiguous", "two readings", by="ann"))
        g = plain_job(tmp / "jobs", "run-g", {"t1__1": ("t1", 1)})
        result = g / "t1__1" / "result.json"
        body = json.loads(result.read_text()); body["task_checksum"] = "v2"; result.write_text(json.dumps(body))
        fa.update(str(g), lambda x: x.set_flag("t1", "flawed", "x", by="ann", task_version="v1"))
        a = fa.Analysis()
        a.set_flag("t2", "flawed", "unreachable", by="ann")
        a.set_flag("t3", "ambiguous", "two_readings", by="ann")
        fa.update(str(job), lambda x: x.flags.update(a.flags))
        fa.update(str(job), lambda x: x.set_label("t2__1", "evaluator", by="ann", note="expected answer is wrong"))
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        cls.ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs")], label="filtered")
        web.shared.open_workspace(cls.ws)
        cls.client = web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        cls.temp.cleanup()

    def page(self, path):
        r = self.client.get(path); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def filtered_rate(self, html):
        m = re.search(r'id="filtered-rate"[^>]*>Filtered score <b>([^<]*)</b>', html)
        return m and m.group(1)

    def test_a_job_with_no_accepted_flag_or_an_unreadable_file_shows_no_switch(self):
        for run in ("run-b", "run-d", "run-e"):
            html = self.page(f"/job/{run}")
            self.assertNotIn('id="filtered-score"', html, run)
            self.assertNotIn('>Exclude:</span>', html, run)
        stale = self.page("/job/run-g")
        self.assertIn('id="filter-other-version"', stale)
        self.assertNotIn('>Exclude:</span>', stale)
        self.assertNotIn('class="filter-choice"', stale)

    def test_the_switches_are_off_by_default_and_the_adjusted_rate_is_unchanged(self):
        html = self.page("/job/run-a")
        self.assertIn('id="filter-flawed"', html)
        self.assertIn('id="filter-ambiguous"', html)
        self.assertIn('>Exclude:</span>', html)
        self.assertIn('> Flawed</label>', html)
        self.assertIn('> Ambiguous</label>', html)
        self.assertIn('aria-label="Exclude 1 flawed task"', html)
        self.assertIn('aria-label="Exclude 1 ambiguous task"', html)
        self.assertIsNone(self.filtered_rate(html))
        self.assertIn('id="adjusted-rate">40%', html)

    def test_every_combination_is_on_the_page_for_switching_in_place(self):
        html = self.page("/job/run-a?flawed=1")
        rates = re.findall(r'<p class="filtered-rate"( id="filtered-rate"| hidden) data-kinds="([^"]*)"[^>]*>Filtered score <b>([^<]*)</b>', html)
        self.assertEqual(rates, [(' id="filtered-rate"', "flawed", "50%"), (" hidden", "ambiguous", "50%"),
                                 (" hidden", "flawed,ambiguous", "67%")])
        self.assertIn('data-kind="flawed" checked', html)

    def test_a_switch_the_page_does_not_offer_is_off(self):
        for run, present, absent in (("run-c", "flawed", "ambiguous"), ("run-f", "ambiguous", "flawed")):
            with self.subTest(run=run):
                html = self.page(f"/job/{run}?flawed=1&ambiguous=1")
                self.assertNotIn(f'id="filter-{absent}"', html)
                self.assertIn(f'id="filter-{present}"', html)
                self.assertIn(f'id="filtered-rate" data-kinds="{present}"', html)

    def test_each_switch_sets_its_tasks_aside(self):
        flawed = self.page("/job/run-a?flawed=1")
        self.assertEqual(self.filtered_rate(flawed), "50%")
        self.assertIn('id="adjusted-rate">40%', flawed)
        self.assertIn('data-kind="flawed" checked', flawed)
        self.assertIn('data-kind="ambiguous">', flawed)
        self.assertEqual(self.filtered_rate(self.page("/job/run-a?flawed=1&ambiguous=1")), "67%")
        self.assertIsNone(self.filtered_rate(self.page("/job/run-a?flawed=0")))

    def test_the_switches_keep_the_pages_other_query_values(self):
        html = self.page("/job/run-a?outcome=failed&flawed=1")
        self.assertIn('<input type="hidden" name="outcome" value="failed">', html)
        self.assertNotIn('<input type="hidden" name="flawed"', html)

    def test_with_every_task_left_out_there_is_no_rate(self):
        self.assertEqual(self.filtered_rate(self.page("/job/run-c?flawed=1")), "\u2014")

    def test_the_trial_table_tags_flagged_tasks_and_labelled_trials(self):
        html = self.page("/job/run-a")
        self.assertRegex(html, r'<tr data-outcome="[^"]*"[^>]*data-trial="t2__1" data-task-flag="flawed"')
        self.assertRegex(html, r'<span class="tflag tf-flawed" data-tip="Task flagged flawed: unreachable">flawed</span>')
        self.assertRegex(html, r'<span class="tflag tf-ambiguous" data-tip="Task flagged ambiguous: two_readings">ambiguous</span>')
        self.assertIn('<span class="tlabel" data-tip="Why it failed, set by ann: expected answer is wrong">evaluator</span>', html)
        self.assertEqual(html.count('class="tflag'), 2)                # t1 and t4 are not flagged
        self.assertRegex(html, r'<td class="fa"><span class="tflag tf-flawed"')   # its own column, after the outcome
        self.assertIn('>User judge</span></th>', html)
        self.assertNotIn('>User judge</span></th>', self.page("/job/run-b"))     # no flag, no label: no column

    def test_the_rows_of_tasks_left_out_follow_the_switches(self):
        self.assertIn('<table class="tt" data-leave-out="">', self.page("/job/run-a"))
        self.assertIn('<table class="tt" data-leave-out="flawed ambiguous">', self.page("/job/run-a?flawed=1&ambiguous=1"))

    def test_a_pending_flag_is_tagged_but_never_left_out(self):
        html = self.page("/job/run-d")
        self.assertIn('class="tflag tf-flawed tf-pending"', html)
        self.assertIn('flawed · pending</span>', html)
        self.assertNotIn('data-task-flag=', html)
        self.assertIn('<table class="tt">', html)



class FailureTab(unittest.TestCase):
    """The trial page's User judge tab: a label for a failed trial, a flag for its task, written by a form."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nullius-failure-tab-")
        self.addCleanup(self.temp.cleanup)
        tmp = Path(self.temp.name)
        self.job = plain_job(tmp / "jobs", "run-t", {"t1__1": ("t1", 1), "t2__1": ("t2", 0),
                                                     "t3__1": ("t3", 0), "t3__2": ("t3", 0)})
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        stack.enter_context(patch.dict(web.app.config, TESTING=True))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        stack.enter_context(patch.object(fa, "who", lambda: "Ann"))
        web.shared.open_workspace(workspace.create(str(tmp / "ws"), [str(tmp / "jobs")], label="tab"))
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "tab-test"

    def page(self, path):
        r = self.client.get(path); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def post(self, trial, **data):
        r = self.client.post(f"/failure/run-t/{trial}", data={"csrf": "tab-test", **data})
        self.addCleanup(r.close)
        return r

    def test_the_tab_sits_after_the_judges_and_asks_for_a_label_only_on_a_failed_trial(self):
        failed = self.page("/trial/run-t/t2__1")
        self.assertIn('id="tab-failure"', failed)
        self.assertLess(failed.index('id="tab-llm-judge"'), failed.index('id="tab-failure"'))
        self.assertIn('id="fa-label"', failed)
        self.assertNotIn('id="fa-label"', self.page("/trial/run-t/t1__1"))
        self.assertIn('id="fa-flag"', self.page("/trial/run-t/t1__1"))

    def test_a_label_is_saved_shown_and_cleared(self):
        r = self.post("t2__1", action="label", label="model", note="rule in context")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("#failure"))
        lab = fa.load(str(self.job)).labels["t2__1"]
        self.assertEqual((lab.label, lab.note, lab.by), ("model", "rule in context", "Ann"))
        self.assertIn('id="fa-label-value">Model</span>', self.page("/trial/run-t/t2__1"))
        self.post("t2__1", action="clear_label")
        self.assertEqual(fa.load(str(self.job)).labels, {})

    def test_a_flag_is_set_for_the_task_shown_on_its_trials_and_removed(self):
        self.post("t1__1", action="flag", kind="flawed", category="unreachable", evidence="tool docs")
        flag = fa.load(str(self.job)).flags["t1"]
        self.assertEqual((flag.kind, flag.status, flag.by), ("flawed", "accepted", "Ann"))
        self.assertIn('id="fa-flag-value">Flawed</b>', self.page("/trial/run-t/t1__1"))
        self.post("t1__1", action="flag", kind="ambiguous", category="two readings", evidence="updated evidence")
        html = self.page("/trial/run-t/t1__1")
        self.assertIn('value="ambiguous" required checked', html)
        self.assertIn('value="two readings"', html)
        self.assertIn('value="updated evidence"', html)
        self.post("t1__1", action="remove_flag")
        self.assertEqual(fa.load(str(self.job)).flags, {})

    def test_the_category_box_suggests_this_benchmarks_categories_first(self):
        fa.update(str(self.job), lambda a: a.set_flag("t1", "flawed", "flaky test", by="Ann"))
        html = self.page("/trial/run-t/t2__1")
        options = re.findall(r'<option value="([^"]*)">', html.split('id="fa-categories"')[1].split("</datalist>")[0])
        self.assertEqual(options[0], "flaky test")
        self.assertIn("unreachable", options)
        fa.update(str(self.job), lambda a: (a.remove_flag("t1"),
                                            a.set_flag("t2", "ambiguous", "two_readings", by="Ann")))
        html = self.page("/trial/run-t/t1__1")                         # t1 has no flag: its form is shown
        options = re.findall(r'<option value="([^"]*)">', html.split('id="fa-categories"')[1].split("</datalist>")[0])
        self.assertIn("two_readings", options)
        self.assertNotIn("two readings", options)

    def test_a_pending_flag_can_be_accepted(self):
        fa.update(str(self.job), lambda a: a.set_flag("t1", "ambiguous", "two_readings", by="bot", source="import:x"))
        self.assertIn(">Accept</button>", self.page("/trial/run-t/t1__1"))
        self.post("t1__1", action="accept")
        flag = fa.load(str(self.job)).flags["t1"]
        self.assertEqual((flag.status, flag.by, flag.accepted_by), ("accepted", "bot", "Ann"))

    def test_the_judges_cause_is_suggested_and_kept_with_the_label(self):
        store.save(web.shared.STATE["workspace"].dir, "run-t", "t2__1", record("agent_error"))
        html = self.page("/trial/run-t/t2__1")
        self.assertIn('id="fa-suggestion">Judge suggests Agent or Model</p>', html)
        self.post("t2__1", action="label", label="agent")
        self.assertEqual(fa.load(str(self.job)).labels["t2__1"].judge_cause, "agent_error")

    def test_each_model_judges_answers_to_the_same_two_questions_are_shown_beside_the_persons(self):
        ws = web.shared.STATE["workspace"].dir
        rec = record("agent_error")
        rec["answers"].update({"failure_label": {"choice": "model", "confidence": 0.7, "probabilities": {"model": 0.62}},
                               "task_flag": {"choice": "ambiguous", "confidence": 0.6, "probabilities": {"ambiguous": 0.55}}})
        store.save(ws, "run-t", "t2__1", rec)
        llm = record("agent_error")
        llm["answers"]["task_flag"] = {"choice": "fair", "confidence": 0.8, "probabilities": {"fair": 0.8}}
        store.save(ws, "run-t", "t2__1", llm, lane="llm")
        html = self.page("/trial/run-t/t2__1")
        self.assertIn('id="fa-model-labels">Decision model judge: <b>Model</b> <span title="The probability the judge gave this answer">62%</span></p>', html)       # the LLM judge gave no label
        self.assertIn('id="fa-model-flags">Decision model judge: <b>Ambiguous</b> <span title="The probability the judge gave this answer">55%</span> · LLM judge: <b>Fair</b> <span title="The probability the judge gave this answer">80%</span></p>', html)
        self.assertNotIn('id="fa-suggestion"', html)                    # their own answers replace the mapped suggestion
        self.assertIn('data-question="failure_label"', html)            # and each model's tab has the two cards
        self.assertIn('data-question="task_flag"', html)
        self.post("t2__1", action="flag", kind="flawed", category="unreachable")
        self.assertEqual(fa.load(str(self.job)).set_aside(["ambiguous"]), set())     # a model's answer never sets a task aside

    def test_the_label_names_its_attempt_and_the_flag_says_it_covers_every_attempt(self):
        once = self.page("/trial/run-t/t2__1")
        self.assertIn("<h3>Why it failed</h3>", once)
        self.assertIn("<h3>Task flag</h3>", once)
        twice = self.page("/trial/run-t/t3__2")
        self.assertIn("<h3>Why attempt 2 failed</h3>", twice)
        self.assertIn("<h3>Task flag · all 2 attempts</h3>", twice)
        self.assertIn('id="fa-flag-scope">One flag per task in this run: it shows on every attempt.</p>', twice)

    def test_a_remote_browser_sees_the_tab_without_forms_and_cannot_write(self):
        self.post("t2__1", action="label", label="model")
        remote = {"REMOTE_ADDR": "10.0.0.9"}
        r = self.client.get("/trial/run-t/t2__1", environ_base=remote); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        self.assertIn('id="fa-label-value">Model</span>', html)
        self.assertNotIn('action="/failure/', html)
        r = self.client.post("/failure/run-t/t2__1", data={"csrf": "tab-test", "action": "clear_label"},
                             environ_base=remote); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)

    def test_an_unreadable_file_is_said_and_offers_no_form(self):
        (self.job / fa.FILE).write_text("{not json")
        html = self.page("/trial/run-t/t2__1")
        self.assertIn('id="failure-error"', html)
        self.assertNotIn('action="/failure/', html)

    def test_a_bad_value_or_a_missing_token_changes_nothing(self):
        self.assertEqual(self.post("t1__1", action="label", label="model").status_code, 400)   # a passed trial
        self.assertEqual(self.post("t2__1", action="label", label="luck").status_code, 400)
        self.assertEqual(self.post("t2__1", action="explode").status_code, 400)
        r = self.client.post("/failure/run-t/t2__1", data={"action": "label", "label": "model"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(fa.load(str(self.job)).labels, {})


class CompareFilters(unittest.TestCase):
    """Compare's flag filters: one list of flagged tasks set aside in every column, and the tasks the runs flag
    differently."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-compare-flags-")
        tmp = Path(cls.temp.name)
        # four tasks in both runs; run A flags t2 flawed, run B flags t2 flawed and t3 ambiguous
        a = plain_job(tmp / "jobs", "run-a", {"t1__1": ("t1", 1), "t2__1": ("t2", 0), "t3__1": ("t3", 0), "t4__1": ("t4", 1)})
        b = plain_job(tmp / "jobs", "run-b", {"t1__1": ("t1", 0), "t2__1": ("t2", 0), "t3__1": ("t3", 1), "t4__1": ("t4", 1)})
        fa.update(str(a), lambda x: x.set_flag("t2", "flawed", "x", by="ann"))
        fa.update(str(b), lambda x: (x.set_flag("t2", "flawed", "x", by="ann"),
                                     x.set_flag("t3", "ambiguous", "two readings", by="ann")))
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        cls.ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs")], label="cmp")
        web.shared.open_workspace(cls.ws)
        cls.client = web.app.test_client()
        cls.base = f"/compare?pick={cls.ws.name}:run-a&pick={cls.ws.name}:run-b"

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        cls.temp.cleanup()

    def page(self, path):
        r = self.client.get(path); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def test_the_switches_count_flags_over_all_runs_and_name_the_tasks_flagged_differently(self):
        html = self.page(self.base)
        self.assertIn('id="compare-filter-flawed"', html)
        self.assertIn('aria-label="Exclude 1 flawed task"', html)
        self.assertIn('aria-label="Exclude 1 ambiguous task"', html)
        self.assertIn('>Exclude:</span>', html)
        self.assertIn('> Flawed</label>', html)
        self.assertNotIn(' checked> Flawed', html)
        self.assertNotIn('id="compare-set-aside"', html)
        self.assertIn('title="t3">Flags differ on 1 task</span>', html)

    def test_a_task_flagged_in_any_run_is_set_aside_in_every_column(self):
        from nullius.compare import comparison
        html = self.page(self.base + "&ambiguous=1")
        self.assertIn('id="compare-set-aside">Filtered: 1 task set aside in every column', html)
        self.assertIn('id="compare-set-aside-0">1 trials of flagged tasks set aside', html)
        with web.app.test_request_context(self.base + "&ambiguous=1"):
            view, _ = web.compare._columns_requested()
        self.assertEqual([sorted(t.task_base for t in j.trials) for j in view.jobs], [["t1", "t2", "t4"]] * 2)
        self.assertEqual(view.set_aside_trials, (1, 1))
        self.assertEqual(view.shared_tasks, 3)                                 # the shared tasks left

    def test_the_sidebars_compare_link_does_not_carry_the_filters(self):
        html = self.page(self.base + "&flawed=1")
        self.assertIn('href="/compare" data-label="Compare"', html)

    def test_matched_tasks_and_the_task_page_take_the_same_filter(self):
        html = self.page(self.base + "&cohort=matched&ambiguous=1")
        self.assertIn('id="compare-set-aside">Filtered: 1 task set aside', html)
        r = self.client.get(f"/compare/task/t3?pick={self.ws.name}:run-a&pick={self.ws.name}:run-b&ambiguous=1")
        self.addCleanup(r.close)
        self.assertIn(r.status_code, (302, 404))                       # t3 is set aside: not in this comparison

    def test_a_task_on_another_version_in_one_column_is_not_set_aside_and_is_named(self):
        tmp = Path(self.temp.name)
        c = plain_job(tmp / "jobs2", "run-c", {"t1__1": ("t1", 1), "t5__1": ("t5", 0)})
        d = plain_job(tmp / "jobs2", "run-d", {"t1__1": ("t1", 0), "t5__1": ("t5", 1)})
        for job, version in ((c, "v1"), (d, "v2")):
            rj = job / "t5__1" / "result.json"
            body = json.loads(rj.read_text()); body["task_checksum"] = version; rj.write_text(json.dumps(body))
        fa.update(str(c), lambda x: x.set_flag("t5", "flawed", "x", by="ann", task_version="v1"))
        ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs2")], label="versions")
        with patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}):
            web.shared.open_workspace(ws)
            html = self.page(f"/compare?pick={ws.name}:run-c&pick={ws.name}:run-d&flawed=1")
            web.shared.open_workspace(self.ws)
        self.assertIn('id="compare-set-aside">Filtered: 0 tasks set aside', html)
        self.assertIn('title="t5">Flags differ on 1 task</span>', html)

    def test_an_unreadable_flag_file_is_named(self):
        tmp = Path(self.temp.name)
        e = plain_job(tmp / "jobs3", "run-e", {"t1__1": ("t1", 1)})
        f = plain_job(tmp / "jobs3", "run-f", {"t1__1": ("t1", 1)})
        (e / fa.FILE).write_text("{not json")
        ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs3")], label="broken")
        with patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}):
            web.shared.open_workspace(ws)
            html = self.page(f"/compare?pick={ws.name}:run-e&pick={ws.name}:run-f")
            web.shared.open_workspace(self.ws)
        self.assertIn('id="compare-flags-unreadable">Flags unreadable: run-e</span>', html)

    def test_the_comparisons_links_keep_the_filters_and_the_form_keeps_its_runs(self):
        html = self.page(self.base + "&flawed=1")
        form = re.search(r'<form[^>]*id="compare-flags".*?</form>', html, re.S).group(0)
        self.assertIn('id="compare-filter-flawed" name="flawed" value="1" checked>', form)
        self.assertIn('id="compare-filter-ambiguous" name="ambiguous" value="1">', form)
        self.assertEqual(re.findall(r'type="hidden" name="([^"]*)" value="([^"]*)"', form),        # every run, no filter
                         [("pick", f"{self.ws.name}:run-a"), ("pick", f"{self.ws.name}:run-b")])
        self.assertIn("cohort=matched", html)
        self.assertRegex(html, r'href="/compare\?[^"]*cohort=matched[^"]*flawed=1')


if __name__ == "__main__":
    unittest.main()
