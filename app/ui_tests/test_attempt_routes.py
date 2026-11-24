"""A task attempted several times: its tabs on the trial page, All attempts,
and the attempt number on the job page."""

from __future__ import annotations

from contextlib import ExitStack
from html import unescape
from pathlib import Path
import json
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.exceptions import NotFound

from nullius.runs import parse
from nullius import workspace
from .test_routes import web, write_json


def repeated_job(root: Path, job_id="repeat-run", single=True):
    """`auth-token` three times (pass, fail, pass, started in that order) and, unless ``single`` is
    False, `single` once."""
    job_dir = root / job_id
    write_json(job_dir / "result.json", {"stats": {"n_trials": 4}})
    write_json(job_dir / "config.json", {"agents": [{"name": "fixture-agent", "model_name": "fixture-model"}],
                                         "datasets": [{"name": "terminal-bench", "n_tasks": 2}], "n_attempts": 3})
    # directory names out of start order, so numbering cannot lean on them
    for name, task, reward, minute in (("auth-token__c", "auth-token", 1, 0), ("auth-token__a", "auth-token", 0, 5),
                                       ("auth-token__b", "auth-token", 1, 9), ("single__a", "single", 1, 1)):
        if task == "single" and not single:
            continue
        write_json(job_dir / name / "result.json", {
            "task_name": f"terminal-bench/{task}", "trial_name": name, "source": "terminal-bench",
            "started_at": f"2026-09-01T10:{minute:02d}:00Z", "finished_at": f"2026-09-01T10:{minute + 1:02d}:00Z",
            "verifier_result": {"rewards": {"reward": reward}},
            "agent_result": {"n_input_tokens": 100 * (minute + 1), "n_output_tokens": 10},
        })
        (job_dir / name / "verifier").mkdir()
        (job_dir / name / "verifier" / "reward.txt").write_text(str(reward), encoding="utf-8")
    return parse.parse_job(str(job_dir))


def written_job(root: Path, job_id: str, attempts):
    """A job of ``(trial name, task, minute, reward, exception type)`` attempts:
    a reward of None leaves no verifier result, an exception type records one."""
    job_dir = root / job_id
    write_json(job_dir / "result.json", {"stats": {"n_trials": len(attempts)}})
    write_json(job_dir / "config.json", {"agents": [{"name": "fixture-agent", "model_name": "fixture-model"}],
                                         "datasets": [{"name": "terminal-bench", "n_tasks": 1}], "n_attempts": len(attempts)})
    for name, task, minute, reward, exception in attempts:
        write_json(job_dir / name / "result.json", {
            "task_name": f"terminal-bench/{task}", "trial_name": name, "source": "terminal-bench",
            "started_at": f"2026-09-01T10:{minute:02d}:00Z", "finished_at": f"2026-09-01T10:{minute + 1:02d}:00Z",
            **({"verifier_result": {"rewards": {"reward": reward}}} if reward is not None else {}),
            **({"exception_info": {"exception_type": exception, "exception_message": "429"}} if exception else {}),
            "agent_result": {"n_input_tokens": 100 * (minute + 1), "n_output_tokens": 10},
        })
        (job_dir / name / "verifier").mkdir()
        if reward is not None:
            (job_dir / name / "verifier" / "reward.txt").write_text(str(reward), encoding="utf-8")
    return parse.parse_job(str(job_dir))


def tabs(html):
    """(href, current) for every attempt tab, in order."""
    return [(unescape(href), bool(cur)) for href, cur in
            re.findall(r'<a class="attempt-tab[^"]*"[^>]*?href="([^"]*)"[^>]*?( aria-current="page")?>', html, re.S)]


class AttemptRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-attempts-")
        cls.root = Path(cls.temp.name)
        cls.job = repeated_job(cls.root)
        cls.ws = workspace.Workspace("active", "Active workspace", ["/fixture"], "2026-01-01", "2026-01-01",
                                     str(cls.root / "ws"), ["/indexed"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws, "roots": [], "job_roots": [],
                                                               "label": self.ws.label}, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.root / "store")))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.object(workspace, "list_all", return_value=[self.ws]))
        self.stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        self.stack.enter_context(patch.object(workspace, "load", return_value=self.ws))
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[self.job]))
        for obj, name in [(web.shared, "open_workspace"), (web.shared, "_import_roots"),
                          (workspace, "save"), (workspace, "touch")]:
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(name)))
        self.client = web.app.test_client()

    def get(self, path, status=200):
        response = self.client.get(path)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, status, path)
        return response.get_data(as_text=True)

    def test_trial_page_has_a_tab_per_attempt_in_start_order(self):
        html = self.get("/trial/repeat-run/auth-token__a")
        self.assertEqual(tabs(html), [
            ("/trial/repeat-run/auth-token__c", False),
            ("/trial/repeat-run/auth-token__a", True),
            ("/trial/repeat-run/auth-token__b", False),
            ("/trial/repeat-run/auth-token__a/attempts", False),
        ])
        self.assertIn(">Attempt 2</a>", html)
        self.assertIn("<b>2/3</b> passed", html)
        # the attempt tabs carry the evidence tab along; the All attempts tab does not
        self.assertEqual(html.count("data-keep-tab"), 3)

    def test_a_task_attempted_once_has_no_attempt_tabs(self):
        html = self.get("/trial/repeat-run/single__a")
        self.assertEqual(tabs(html), [])
        self.assertNotIn("attempt-head", html)
        single = self.get("/trial/repeat-run/single__a/attempts")
        self.assertEqual(re.findall(r'>Attempt (\d)</a></td>', single), ["1"])

    def test_all_attempts_lists_each_attempt_once_with_its_link(self):
        html = self.get("/trial/repeat-run/auth-token__b/attempts")
        self.assertNotIn('class="th-card"', html)
        self.assertEqual(tabs(html)[-1], ("/trial/repeat-run/auth-token__c/attempts", True))
        rows = re.findall(r'<td><a href="(/trial/repeat-run/[^"]+)">Attempt (\d)</a></td>', html)
        self.assertEqual(rows, [("/trial/repeat-run/auth-token__c", "1"), ("/trial/repeat-run/auth-token__a", "2"),
                                ("/trial/repeat-run/auth-token__b", "3")])
        self.assertEqual(html.count('class="tag oc-resolved"'), 2)

    def test_job_page_groups_a_tasks_attempts_under_one_row(self):
        html = self.get("/job/repeat-run")
        self.assertEqual(html.count('<tr class="task-group"'), 1)
        head = re.search(r'<tr class="task-group" data-group="([^"]+)"[^>]*>(.*?)</tr>', html, re.S)
        self.assertEqual(head.group(1), "g-auth-token__c")
        self.assertIn("<b>2/3</b>", head.group(2))
        self.assertEqual(re.findall(r'class="sw oc-(\w+)"', head.group(2)), ["resolved", "failed_verification", "resolved"])
        self.assertIn('href="/trial/repeat-run/auth-token__c/attempts"', head.group(2))
        # tokens and times are the mean over the attempts, the range on hover; sorting reads the mean
        self.assertIn('data-tip="Mean of 3 attempts · 110 to 1.0k">577</span>', head.group(2))
        self.assertIn('data-tokens="576.6666666666666"', head.group(0))
        # its attempts follow it, in start order, closed until it is opened
        attempts = re.findall(r'data-trial="([^"]+)"[^>]*class="in-group" data-group="g-auth-token__c" hidden>', html)
        self.assertEqual(attempts, ["auth-token__c", "auth-token__a", "auth-token__b"])
        self.assertEqual(re.findall(r'>attempt (\d)</a></td>', html), ["1", "2", "3"])
        # a task attempted once is an ordinary row
        self.assertRegex(html, r'data-trial="single__a"[^>]*>\s*<td><a class="task" [^>]*>single</a></td>')

    def test_job_page_has_pass_at_k_only_when_every_task_was_tried_twice(self):
        self.assertNotIn('id="pass-k"', self.get("/job/repeat-run"))
        only = repeated_job(self.root, "repeat-only", single=False)
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[only]))
        table = re.search(r'<table class="pass-k" id="pass-k"(.*?)</table>', self.get("/job/repeat-only"), re.S).group(1)
        self.assertIn("1 task, each read on its first 3 attempts", table)
        rows = re.findall(r'<th scope="row">(\d+)</th><td>(\d+)%</td><td>(\d+)%</td>', table)
        # 2 of 3 passed: pass@k 2/3, 1, 1; pass^k 2/3, 1/3, 0
        self.assertEqual(rows, [("1", "67", "67"), ("2", "100", "33"), ("3", "100", "0")])

    def test_an_infra_attempt_is_counted_apart_on_the_job_page(self):
        run = written_job(self.root, "infra-run", [("t__a", "t", 0, 1, None), ("t__b", "t", 2, None, "ApiRateLimitError"),
                                                   ("t__c", "t", 4, 0, None)])
        self.assertEqual([t.outcome for t in run.trials if t.trial_name == "t__b"], ["rate_limited"])
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[run]))
        head = re.search(r'<tr class="task-group"[^>]*>(.*?)</tr>', self.get("/job/infra-run"), re.S)
        # one pass over the two attempts that ran; the rate-limited one is apart, and out of the means
        self.assertIn('<b>1/2</b><span class="faint"> · 1 infra</span>', head.group(1))
        self.assertIn('data-tip="Mean of 2 attempts · 110 to 510">310</span>', head.group(1))
        self.assertIn('data-tokens="310.0"', head.group(0))

    def test_all_attempts_writes_a_reward_as_the_score_beside_it(self):
        run = written_job(self.root, "partial-run", [("p__a", "p", 0, 1, None), ("p__b", "p", 2, 2 / 3, None)])
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[run]))
        html = self.get("/trial/partial-run/p__a/attempts")
        rewards = [re.findall(r'<td class="num">([^<]*)</td>', row)[0] for row in re.findall(r"<tr>(.*?)</tr>", html, re.S)[1:]]
        self.assertEqual(rewards, ["1", "0.67"])

    def test_a_retried_attempt_shows_each_try_it_kept(self):
        run = written_job(self.root, "retried-run", [("0001__u0000r1", "t", 0, 1, None), ("0001__u0000r2", "t", 4, 1, None),
                                                     ("0001__u0000r3", "t", 2, 0, None)])
        # the standard's record of a retry: the run allowed an attempt one, and
        # attempt 2 took it after the others, its first try (rate limited) kept
        # under retried/
        job_dir = self.root / "retried-run"
        cfg = json.loads((job_dir / "config.json").read_text()); cfg["standard"] = {"benchmark": "t", "retries": 1}
        (job_dir / "config.json").write_text(json.dumps(cfg))
        path = job_dir / "0001__u0000r2" / "result.json"
        res = json.loads(path.read_text())
        write_json(job_dir / "retried" / "0001__u0000r2.try-1" / "result.json", dict(
            res, verifier_result=None, exception_info={"exception_type": "RateLimitError", "exception_message": "429"}))
        res["standard"] = {"unit": "t", "retries": 1}; path.write_text(json.dumps(res))
        run = parse.parse_job(str(job_dir))
        self.assertEqual((run.retries, len(run.trials)), (1, 3))          # a kept try is no trial of the job
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[run]))

        job = self.get("/job/retried-run")
        self.assertIn('<th scope="col" class="num">Retries</th>', job)
        self.assertEqual(re.findall(r'<td class="num run-retries">(.*?)</td>', job),
                         ['<span class="faint">0/1</span>', '1<span class="faint">/1</span>', '<span class="faint">0/1</span>'])

        def try_tabs(html):
            return [(oc, unescape(href), bool(cur), n) for oc, href, cur, n in re.findall(
                r'<a class="try-tab oc-([a-z_]+)"[^>]*?href="([^"]*)"[^>]*?( aria-current="page")?>Try (\d)', html, re.S)]

        kept, first = "/trial/retried-run/0001__u0000r2", "/trial/retried-run/0001__u0000r2.try-1"
        page = self.get(kept)
        self.assertEqual(try_tabs(page), [("rate_limited", first, False, "1"), ("resolved", kept, True, "2")])
        self.assertIn('title="Attempt 2: Resolved on try 2" aria-current="page">Attempt 2<svg class="ic attempt-retried"', page)
        # The attempts, this attempt's tries, then this try's numbers.
        order = [page.index(part) for part in ('<div class="attempt-head">', '<nav class="try-tabs"', 'class="th-strip"')]
        self.assertEqual(order, sorted(order))
        self.assertIn('id="tab-judgement"', page)
        # the first try: its own evidence, under its attempt's tab, never judged
        with patch.object(parse, "tries", wraps=parse.tries) as read:
            page = self.get(first)
        self.assertEqual(read.call_count, 1)          # the attempt's tries read once: the try found in the list its tabs show
        self.assertIn('<span class="tag oc-rate_limited">Rate limited</span>', page)
        # the page is Try 1's: its attempt's tab, which leads to the attempt's last try, is where it is, not it
        self.assertEqual([cur for _, cur in tabs(page)], [False, False, False, False])
        self.assertIn('title="Attempt 2: Resolved on try 2" aria-current="true">Attempt 2<svg class="ic attempt-retried"', page)
        self.assertEqual(try_tabs(page), [("rate_limited", first, True, "1"), ("resolved", kept, False, "2")])
        self.assertNotIn('id="tab-judgement"', page)
        self.assertNotIn('id="tab-llm-judge"', page)
        self.assertNotIn('id="trial-judgement"', page)
        self.assertEqual(self.client.get(first + "/conversation").status_code, 200)
        # its own records, read and saved from its own page as its last try's are
        self.assertIn(f'href="{first}/artifact/result/download" download>Download</a>', page)
        self.assertEqual(self.client.get(first + "/artifact/result").status_code, 200)
        saved = self.client.get(first + "/artifact/result/download")
        self.assertEqual(saved.status_code, 200)
        self.assertIn("RateLimitError", saved.get_data(as_text=True))     # the try's result, not the one that replaced it
        with web.app.test_request_context(first):
            with self.assertRaises(NotFound):                         # what the judge resolves: never a try
                web.traces._trial_evidence("retried-run", "0001__u0000r2.try-1")
        self.get("/trial/retried-run/0001__u0000r2.try-2", status=404)     # the attempt's last try is the trial
        self.get("/trial/retried-run/0001__u0000r1.try-1", status=404)     # an attempt that took no retry
        self.assertNotIn("try-tabs", self.get("/trial/retried-run/0001__u0000r1"))

        table = self.get(first + "/attempts")
        self.assertIn('<th class="num">Retries</th>', table)
        rows = re.findall(r'<tr( class="replaced"[^>]*)?>\s*<td><a href="([^"]*)">([^<]*)</a></td>.*?<td class="num">(.*?)</td>', table, re.S)
        self.assertEqual([(bool(rep), unescape(href), label, retries) for rep, href, label, retries in rows], [
            (False, "/trial/retried-run/0001__u0000r1", "Attempt 1", '<span class="faint">0/1</span>'),
            (True, first, "Attempt 2 · Try 1", '<span class="faint">0/1</span>'),
            (False, kept, "Attempt 2 · Try 2", '1<span class="faint">/1</span>'),
            (False, "/trial/retried-run/0001__u0000r3", "Attempt 3", '<span class="faint">0/1</span>')])

    def test_no_retries_column_without_the_runs_limit(self):
        # a trial that says it was retried, in a run that allowed none: Harbor
        # says no attempt's retries, so there is no count to put over a limit
        run = written_job(self.root, "unlimited-run", [("u__a", "u", 0, 1, None), ("u__b", "u", 2, 0, None)])
        path = self.root / "unlimited-run" / "u__a" / "result.json"
        res = json.loads(path.read_text()); res["standard"] = {"unit": "u", "retries": 1}; path.write_text(json.dumps(res))
        run = parse.parse_job(str(self.root / "unlimited-run"))
        self.assertEqual(run.retries, 0)
        self.stack.enter_context(patch.object(workspace, "load_jobs", return_value=[run]))
        self.assertNotIn("Retries</th>", self.get("/job/unlimited-run"))
        self.assertNotIn("run-retries", self.get("/job/unlimited-run"))
        self.assertNotIn("Retries</th>", self.get("/trial/unlimited-run/u__a/attempts"))

    def test_an_unknown_trial_has_no_attempts_page(self):
        self.get("/trial/repeat-run/nope/attempts", status=404)


if __name__ == "__main__":
    unittest.main()
