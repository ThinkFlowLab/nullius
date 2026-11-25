"""A runner's scored run, read from disk the way a run writes it: each
trial's score in result.json and its questions in verifier/parts.json. The
job page and the comparison break the mean score down by the field the
parts name (`by`)."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import parse
from .test_routes import web


def scored_job(root: Path, job_id: str, trials: dict):
    """A job of one runner arm: per trial, its parts as (category, score)."""
    job = root / job_id
    job.mkdir(parents=True)
    (job / "config.json").write_text(json.dumps({
        "job_name": job_id, "runner": "locomo", "runner_label": "LoCoMo", "runner_items": ["conversation", "conversations"],
        "agents": [{"name": "conversation", "model_name": "gpt-4-turbo"}], "datasets": [{"name": "locomo10", "n_tasks": len(trials)}]}))
    (job / "result.json").write_text(json.dumps({"stats": {"n_trials": len(trials)}}))
    for name, parts in trials.items():
        t = job / name
        (t / "verifier").mkdir(parents=True)
        mean = sum(s for _, s in parts) / len(parts)
        (t / "result.json").write_text(json.dumps({
            "task_name": f"locomo10/{name}", "trial_name": name, "source": "locomo10",
            "verifier_result": {"rewards": {"score": mean}, "parts": {"n": len(parts), "scored": len(parts)}}}))
        (t / "verifier" / "parts.json").write_text(json.dumps({
            "says": "token F1, the mean over a conversation's questions", "nouns": "questions", "by": "category",
            "parts": [{"n": i + 1, "text": f"question {i + 1}", "score": s, "group": g} for i, (g, s) in enumerate(parts)]}))


class ScoredJobFromDisk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-scored-job-")
        tmp = Path(cls.temp.name)
        # 4-turbo: category 1 at 0.75 over 4, category 4 at 0.5 over 2; 3.5: 0.25 and 1.0
        scored_job(tmp / "jobs", "loco-gpt-4-turbo", {"conv-1": [(1, 1.0), (1, 1.0), (4, 0.5)], "conv-2": [(1, 0.5), (1, 0.5), (4, 0.5)]})
        scored_job(tmp / "jobs", "loco-gpt-3.5", {"conv-1": [(1, 0.0), (1, 0.5), (4, 1.0)], "conv-2": [(1, 0.5), (1, 0.0), (4, 1.0)]})
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "ws")))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        cls.ws = workspace.create(str(tmp / "ws"), [str(tmp / "jobs")], label="scored")
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

    def test_the_job_page_shows_the_score_by_category_read_from_parts_json(self):
        html = self.page("/job/loco-gpt-4-turbo")
        rows = re.findall(r'<div class="kv by-group"[^>]*><span class="k">([^<]*)</span><span class="v">([^<]*) <small class="faint">(\d+)</small>', html)
        self.assertEqual(rows, [("Category 1", "0.75", "4"), ("Category 4", "0.50", "2")])
        mean = re.search(r'<span class="k">Mean score</span><span class="v">([^<]*)</span>', html)
        self.assertEqual(mean.group(1), "0.67")                                      # over the six questions, not the two conversations

    def test_the_comparison_has_a_row_a_category(self):
        name = self.ws.name
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.page(f"/compare?pick={name}:loco-gpt-4-turbo&pick={name}:loco-gpt-3.5")))
        self.assertRegex(text, r"Category 1 0\.75 0\.25 ")
        self.assertRegex(text, r"Category 4 0\.50 1\.00 ")

class ScoredAttempts(unittest.TestCase):
    """A scored task's row: its mean score, and a square per attempt filled by its score."""

    def row(self, scores):
        with tempfile.TemporaryDirectory(prefix="nullius-scored-attempts-") as d:
            job_id = "attempts"
            scored_job(Path(d), job_id, {f"t{n}": [("1", score)] for n, score in enumerate(scores)})
            for n in range(len(scores)):
                path = Path(d, job_id, f"t{n}", "result.json")
                path.write_text(json.dumps(dict(json.loads(path.read_text()), task_name="locomo10/t")))
            job = parse.parse_job(str(Path(d, job_id)))
            with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
                page = web.app.test_client().get(f"/job/{job_id}")
            self.addCleanup(page.close)
            return re.search(r'<span class="group-pass">.*?</td>', page.get_data(as_text=True), re.S)[0]

    def test_squares_are_filled_by_scores_out_of_one_only(self):
        row = self.row([1.0, 0.5, 0.0])
        self.assertIn('title="Mean score of 3 attempts"><b>0.50</b>', row)
        self.assertEqual(re.findall(r"--fill:(\d+)%", row), ["100", "50", "0"])
        self.assertIn('title="Attempt 2: 0.50"', row)
        row = self.row([3.0, 0.5])
        self.assertIn("<b>1.75</b>", row)
        self.assertNotIn("sw-fill", row); self.assertNotIn("--fill", row)


if __name__ == "__main__":
    unittest.main()
