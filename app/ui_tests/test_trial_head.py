"""The trial page's head: what ran and how it went, in one row of blocks.

A block says only what the trial has: no dash under a label, no benchmark or
runner for a trial an importer wrote, and Harbor named only for Harbor's own
job trees. Jobs are synthetic, from tests.test_feasibility, and the scored
runner fixture; nothing is run."""
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
from unittest.mock import patch

from nullius import workspace
from nullius.judge import store
from nullius.runs import artifacts, parse
from tests.test_feasibility import swe_report, write_job
from tests.test_reconcile import record
from .test_routes import APP_DIR, web

NATIVE = APP_DIR / "tests" / "fixtures" / "native"


def text_of(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class TrialHead(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = web.app.test_client()

    def head(self, job, trial):
        """(the head's text, its HTML) of one trial's page."""
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            response = self.client.get(f"/trial/{job.job_id}/{trial}")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        block = html.split('<div class="trial-head">', 1)[1].split('<nav class="trial-tabs"', 1)[0]
        return text_of(block), block

    def test_a_harbor_trial_names_its_benchmark_harbor_its_agent_and_its_model(self):
        job = write_job(self.tmp.name, "tb-agent", "my-agent", {"good": 1})
        said, html = self.head(job, "good__x")
        self.assertEqual(said, "Benchmark Terminal-Bench Runner Harbor m my-agent openai/x Reward 1")
        self.assertRegex(html, r'<span class="th-mark" aria-hidden="true">\s*m\s*</span>')   # no mark in the catalogue: its initial

    def test_a_value_the_trial_has_not_has_no_cell_and_no_dash(self):
        """No verifier record: no Reward. No trajectory: no Steps. No agent
        window: no Agent time. With nothing to say, the strip is not drawn."""
        job = write_job(self.tmp.name, "tb-none", "my-agent", {"lost": None})
        said, html = self.head(job, "lost__x")
        self.assertEqual(said, "Benchmark Terminal-Bench Runner Harbor m my-agent openai/x")
        self.assertNotIn("th-strip", html)
        self.assertNotIn("—", said)

    def test_a_swe_bench_trial_shows_the_checks_it_ran_and_none_it_did_not(self):
        held, fix = ["tests/test_a.py::test_ok", "tests/test_b.py::test_ok"], "tests/test_fix.py::test_it"
        job = write_job(self.tmp.name, "swe-agent", "my-agent", {"fixed": swe_report(resolved=True, f2p=([fix], []), p2p=(held, [])),
                                                                 "bare": swe_report()}, dataset="../datasets/swebench-verified")
        os.remove(os.path.join(self.tmp.name, "swe-agent", "bare__x", "verifier", "report.json"))     # a trial that ended before its report
        job = parse.parse_job(os.path.join(self.tmp.name, "swe-agent"))
        said, _ = self.head(job, "fixed__x")
        self.assertIn("Fail → Pass 1 /0 Pass → Pass 2 /0 Patch applied Yes Reward 1.0", said)
        bare, _ = self.head(job, "bare__x")
        self.assertNotIn("Fail → Pass", bare); self.assertNotIn("Pass → Pass", bare); self.assertNotIn("Patch applied", bare)
        self.assertNotIn("—", bare)

    def test_an_imported_trial_says_its_importer_and_no_benchmark_or_runner(self):
        """An importer writes its name as the job's ``harness`` and each trial's
        ``source``: the head says where the trial came from, and claims neither
        a benchmark nor that Harbor ran it."""
        write_job(self.tmp.name, "imported-run", "claude-code", {"session": None})
        at = os.path.join(self.tmp.name, "imported-run", "config.json")
        with open(at) as f:
            cfg = json.load(f)
        with open(at, "w") as f:
            json.dump(dict(cfg, harness="atif"), f)
        said, _ = self.head(parse.parse_job(os.path.join(self.tmp.name, "imported-run")), "session__x")
        self.assertEqual(said, "Imported from ATIF c claude-code openai/x")
        for word in ("Harbor", "Benchmark", "Runner", "Reward"):
            self.assertNotIn(word, said)

    def test_a_scored_trial_shows_its_score_beside_no_reward(self):
        job = parse.parse_job(str(NATIVE / "parts-arm"))
        trial = next(t for t in job.trials if t.reward is None and t.score is not None)
        said, html = self.head(job, trial.trial_name)
        self.assertIn(f"Score {trial.score:.2f}", said)
        self.assertNotIn("Reward", said)
        self.assertIn(f"The mean score of its {trial.n_parts} parts", html)
        self.assertNotIn("Harbor", said)                             # a runner's job: its runner, in its own word
        self.assertIn(f"Runner {job.runner}", said)


class RecordDownload(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = write_job(self.tmp.name, "tb-agent", "my-agent", {"good": 1})
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=[self.job]))
        self.client = web.app.test_client()

    def get(self, path):
        response = self.client.get(path)
        self.addCleanup(response.close)
        return response

    def test_a_record_past_the_readers_limit_is_saved_under_a_name_that_says_it_is_cut(self):
        whole = self.get("/trial/tb-agent/good__x/artifact/result/download")
        self.assertEqual(whole.headers["Content-Disposition"], 'attachment; filename="result.json"')
        self.assertEqual(whole.headers["X-Content-Type-Options"], "nosniff")
        with patch.object(artifacts, "MAX_READ", 40):
            cut = self.get("/trial/tb-agent/good__x/artifact/result/download")
        self.assertEqual(cut.headers["Content-Disposition"], 'attachment; filename="result.partial.json"')
        self.assertEqual(len(cut.get_data(as_text=True)), 40)
        self.assertEqual(whole.get_data(as_text=True)[:40], cut.get_data(as_text=True))

    def test_computer_records_appear_on_the_trial_and_use_the_same_download_guard(self):
        from pathlib import Path
        trial = Path(self.job.trials[0].dir)
        path = trial / "verifier/computer-calls.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"tool":"click","note":"sk-abcdefgh1234567890"}\n')
        for name in ("verifier/osworld/computer-calls.jsonl", ".work/osworld/computer-calls.jsonl"):
            calls = trial / name
            calls.parent.mkdir(parents=True, exist_ok=True)
            calls.write_text(path.read_text())
        page = self.get("/trial/tb-agent/good__x").get_data(as_text=True)
        self.assertIn("Computer tool calls", page)
        self.assertIn("OSWorld runner tool calls", page)
        self.assertNotIn("OSWorld computer tool calls", page)
        download = self.get("/trial/tb-agent/good__x/artifact/computer-calls/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn("[REDACTED]", download.get_data(as_text=True))
        self.assertNotIn("abcdefgh", download.get_data(as_text=True))
        path.unlink()
        path.symlink_to(trial.parent / "outside.jsonl")
        self.assertEqual(self.get("/trial/tb-agent/good__x/artifact/computer-calls/download").status_code, 404)


class JudgeTabNotes(unittest.TestCase):
    """What changes how a judgement reads - questions the judge refused, a
    state it was cut for - is said on one line, each note apart from the next;
    a judgement with nothing of the kind has no such line."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = write_job(self.tmp.name, "r-agent", "my-agent", {"failed": 0, "plain": 0})
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", return_value=[self.job]))
        self.ws = workspace.create(self.tmp.name + "/home", [self.tmp.name], label="notes")
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws}))
        self.client = web.app.test_client()

    def notes(self, trial):
        response = self.client.get(f"/trial/r-agent/{trial}")
        self.addCleanup(response.close)
        panel = response.get_data(as_text=True).split('id="judgement"', 1)[1].split('<div class="jcards">', 1)[0]
        found = re.search(r'<p class="small muted judgement-provenance">(.*?)</p>', panel, re.S)
        return text_of(found.group(1)) if found else None

    def test_a_refusal_and_a_truncated_state_are_two_notes_and_a_clean_judgement_has_none(self):
        store.save(self.ws.dir, "r-agent", "failed__x", dict(record("environment_error", 0.9), truncated=True,
                                                             refusals={"task_completion": "policy"}, states={"trial": {"truncated": True}}))
        store.save(self.ws.dir, "r-agent", "plain__x", record("environment_error", 0.9))
        self.assertEqual(self.notes("failed__x"), "1 refused · state truncated (trial)")
        self.assertIsNone(self.notes("plain__x"))

    def test_the_last_call_says_its_time_model_tokens_and_cost_beside_the_button(self):
        """After a judgement, beside the button: how long the calls took, the model, the tokens and
        the cost, each only when the record has it."""
        rec = record("environment_error", 0.9)
        store.save(self.ws.dir, "r-agent", "failed__x", dict(rec, usage={"input_tokens": 1200, "output_tokens": 34,
                                                                       "seconds": 12.4, "cost_usd": 0.0031}))
        store.save(self.ws.dir, "r-agent", "plain__x", rec)          # an older record: no time, no cost

        def said(trial):
            response = self.client.get(f"/trial/r-agent/{trial}")
            self.addCleanup(response.close)
            panel = response.get_data(as_text=True).split('id="judgement"', 1)[1]
            row = re.search(r'<div class="judge-actions">(.*?)</div>', panel, re.S).group(1)
            self.assertIn("Judge again</button>", row) if "<form" in row else None
            found = re.search(r'<span class="small muted judge-call">(.*?)</span>\s*$', row.strip(), re.S)
            return text_of(found.group(1)) if found else None
        self.assertEqual(said("failed__x"), "Judged in 12.40s · jev-latest · 1.2k tokens · $0.0031")
        self.assertEqual(said("plain__x"), "jev-latest · 2 tokens")


class DurationTwins(unittest.TestCase):
    """A duration reads the same wherever it is drawn: the ``secs`` filter, a
    span's ``nsdur`` once it reaches a minute, and the run page's script, which
    redraws the stage times the template drew."""
    VALUES = (0, 0.04, 0.05, 9.96, 41, 59.994, 59.996, 135, 754.3)

    def test_a_spans_minutes_read_as_a_phases_do(self):
        secs, nsdur = web.app.jinja_env.filters["secs"], web.app.jinja_env.filters["nsdur"]
        for v in (59.994, 59.996, 135, 754.3):
            self.assertEqual(nsdur(int(v * 10**9)), secs(v), v)
        self.assertEqual(secs(754.3), "12.57m")

    def test_the_run_pages_script_draws_a_stages_time_as_the_template_does(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        source = (APP_DIR / "templates" / "runs" / "_execution.html").read_text(encoding="utf-8")
        rule = re.search(r"^\s*const secs = .*;$", source, re.M)
        self.assertIsNotNone(rule, "the stages' script no longer has its own secs")
        program = rule.group(0) + "process.stdout.write(JSON.stringify(" + json.dumps(self.VALUES) + ".map(secs)))"
        drawn = json.loads(subprocess.run([node, "-"], input=program, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(drawn, [web.app.jinja_env.filters["secs"](v) for v in self.VALUES])


if __name__ == "__main__":
    unittest.main()
