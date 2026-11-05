"""The trial and workspace CLIs agree with their pages.

`runs.cli show <root> <job> <trial>` is checked against `/trial/<job>/<trial>`,
and `workspace_cli list` against `/workspaces`. A synthetic errored trial
checks credential redaction. The workspace uses temporary storage; the
pages open, refresh and save nothing.
"""
from __future__ import annotations

import html
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius import workspace_cli
from nullius.runs import cli as runs_cli
from nullius.runs import parse

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
spec = importlib.util.spec_from_file_location("nullius_ui_cli_agreement_more_app", APP_DIR / "app.py")
web = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = web
spec.loader.exec_module(web)

from tests.test_cli import JOBS, SECRET, errored_job  # noqa: E402


def cli(main, argv: list) -> tuple:
    """(exit code, stdout) of a CLI's `main` in this process; stderr dropped."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:          # argparse's own usage exit
            code = exc.code
    return code, out.getvalue()


class Agreement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-cli-agreement-more-")
        root = Path(cls.temp.name)
        cls.errored = str(root / "errored")
        os.makedirs(cls.errored)
        errored_job(cls.errored)
        cls.roots = [str(JOBS), cls.errored]
        # a real workspace over the roots, its cache filled as the app fills it
        cls.home = str(root / "workspaces")
        cls.ws = workspace.create(cls.home, list(cls.roots), "Agreement more")
        workspace.load_jobs(cls.ws, cls.ws.roots)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(web.shared.STATE, {
            "workspace": self.ws, "roots": list(self.roots),
            "job_roots": list(self.roots), "label": self.ws.label,
        }, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", self.home))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        for obj, name in [(web.shared, "open_workspace"), (web.shared, "_import_roots"),
                          (workspace, "save"), (workspace, "touch")]:
            self.stack.enter_context(patch.object(obj, name, side_effect=AssertionError(
                f"read-only route unexpectedly called {name}")))
        self.client = web.app.test_client()

    def page(self, path: str) -> str:
        response = self.client.get(path)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)


    def test_an_errored_trial_shows_the_exception_the_page_shows_and_neither_the_credential(self):
        code, out = cli(runs_cli.main, ["show", self.errored, "errored-job", "task-a__x1", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        page = self.page("/trial/errored-job/task-a__x1")
        self.assertNotIn(SECRET, page)
        self.assertNotIn(SECRET, out)
        self.assertIn(f'<h3 style="margin-top:0">{doc["trial"]["exception_type"]}</h3>', page)
        self.assertIn(f"<pre>{html.escape(doc['trial']['exception_message'])}</pre>", page)
        self.assertEqual(doc["trial"]["exception_message"], "401 for api_key=[REDACTED]")
        self.assertIn("Trajectory · absent", page)
        self.assertIsNone(doc["trajectory"])

    def test_an_unknown_trial_is_404_on_the_page_and_3_from_the_shell(self):
        self.assertEqual(self.client.get("/trial/1/no-such-trial").status_code, 404)
        code, out = cli(runs_cli.main, ["show", str(JOBS), "liveboard-oracle-x4", "no-such-trial"])
        self.assertEqual((code, out), (3, ""))


    def test_workspace_list_says_what_the_workspaces_page_renders(self):
        code, out = cli(workspace_cli.main, ["list", "--home", self.home, "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        page = self.page("/workspaces")
        listed = re.findall(r'<td class="mono faint">([^<]+)</td>', page)
        self.assertEqual([r["name"] for r in rows], listed[::2])   # name cells; the opened stamps between
        for row in rows:
            with self.subTest(workspace=row["name"]):
                block = page.split(f'<td class="mono faint">{row["name"]}</td>', 1)[1].split("</tr>", 1)[0]
                self.assertIn(f'<td class="num">{row["n_cached"]}</td>', block)
                for root in row["roots"]:
                    self.assertIn(f'<span class="mono">{html.escape(root)}</span>', block)
                self.assertIn(row["opened"][:16].replace("T", " "), block)
        # the open workspace is the one the page marks, its label in the rename box
        self.assertEqual(rows[0]["name"], self.ws.name)
        self.assertIn(f'value="{html.escape(self.ws.label)}"', page)
        # and the cache the CLI counts is what the page counts: the jobs the pages read
        self.assertEqual(rows[0]["n_cached"], len(parse.find_job_dirs(self.roots)))
        self.assertEqual(sorted(j["job_id"] for j in rows[0]["jobs"]),
                         sorted(j.job_id for j in parse.load_all(self.roots)))
        code, text = cli(workspace_cli.main, ["list", "--home", self.home])
        self.assertEqual(code, 0)
        self.assertIn(f"{self.ws.name}  {self.ws.label}", text)
        self.assertNotIn(SECRET, text + out + page)


if __name__ == "__main__":
    unittest.main()
