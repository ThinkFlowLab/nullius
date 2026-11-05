"""Open a workspace and render its jobs without installing Harbor.

The interpreter cannot import the runtime before or after each test. The
workspace is opened through `/use-path` under a temporary home, fixture
files remain unchanged, and network access is blocked.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
import tempfile
import unittest
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from flask import url_for

from tests.test_dependency_surface import FORBIDDEN, RUNTIME_MODULES
from .test_routes import web

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
#: The job the walk reads: a small hand-made Harbor oracle run with a live feed.
ROOTS = [FIXTURES / "harbor-jobs"]
#: What must not be importable — the dependency-surface test's own list, so
#: there is one list of runtime modules excluded from the viewer.
RUNTIME = RUNTIME_MODULES

ORACLE_JOB = "liveboard-oracle-x4"


def runtime_present():
    """Which of RUNTIME the interpreter could import, and which it already has."""
    loaded = sorted({name.split(".")[0] for name in sys.modules if name.split(".")[0] in RUNTIME})
    findable = []
    for name in RUNTIME:
        try:
            if importlib.util.find_spec(name) is not None:
                findable.append(name)
        except ValueError:   # in sys.modules without a spec: present, whatever put it there
            findable.append(name)
    return findable, loaded


def listing(roots):
    """Every file under *roots* with its size and mtime: the read-only proof."""
    found = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                stat = path.stat()
                found[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return found


def no_network(*args, **kwargs):
    raise AssertionError("a viewer workflow tried to open a network connection")


class ViewerWithoutRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        findable, loaded = runtime_present()
        assert not findable and not loaded, (
            f"this interpreter can import {findable or loaded}; the walk proves nothing "
            f"unless the runtime is absent — run it with app/requirements.txt alone")
        cls.stack = ExitStack()
        cls.tmp = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-viewer-")))
        cls.stack.enter_context(patch.object(web.shared, "WS_HOME", str(cls.tmp / "workspaces")))
        cls.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "",
                                                              "job_roots": []}, clear=True))
        cls.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        cls.stack.enter_context(patch.object(urllib.request, "urlopen", no_network))
        cls.sources = listing(ROOTS)
        cls.client = web.app.test_client()
        # The workflow a person starts with: paths typed into the picker, whose
        # page hands out the form token.
        cls.client.get("/import?new=1")
        with cls.client.session_transaction() as s:
            token = s["csrf"]
        response = cls.client.post("/use-path", data={"path": [str(r) for r in ROOTS], "csrf": token,
                                                      "name": "recorded fixtures"})
        assert response.status_code == 302 and response.headers["Location"].endswith("/runs"), response.status
        cls.ws = web.shared.STATE["workspace"]
        cls.jobs = {j.job_id: j for j in web.shared._load_jobs()}

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def tearDown(self):
        findable, loaded = runtime_present()
        self.assertEqual((findable, loaded), ([], []), "the walk imported a runtime module")
        self.assertEqual(listing(ROOTS), self.sources, "a viewer workflow wrote into a fixture root")

    def get_ok(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, path)
        return response

    def page(self, path, *expected, **kwargs):
        html = self.get_ok(path, **kwargs).get_data(as_text=True)
        for text in expected:
            self.assertIn(text, html, path)
        return html

    @staticmethod
    def url(endpoint, **values):
        """The path the app itself would link, quoting included."""
        with web.app.test_request_context():
            return url_for(endpoint, **values)

    def trial_url(self, job_id, trial_name):
        return self.url("trial", job_id=job_id, trial_name=trial_name)


    def test_the_interpreter_has_no_runtime_and_the_app_is_loaded(self):
        self.assertEqual(runtime_present(), ([], []))
        installed = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
        self.assertFalse(installed & {n.lower() for n in FORBIDDEN}, installed)
        self.assertIn("flask", installed)
        # The whole application — every capability module, every route — is
        # already imported by now, and nothing above changed.
        self.assertGreater(len(list(web.app.url_map.iter_rules())), 40)


    def test_every_job_page_renders_its_trials(self):
        for job_id, job in self.jobs.items():
            with self.subTest(job_id):
                # A control's headline is its verdict, not a rate.
                headline = '<div class="lab">oracle · passed</div>' if job.is_control else 'id="adjusted-rate"'
                html = self.page(self.url("job", job_id=job_id), f"<h1>{job_id}</h1>",
                                 'aria-label="Trials in this run"', headline)
                for trial in job.trials:
                    self.assertIn(f'href="{self.trial_url(job_id, trial.trial_name)}"', html)
        # the oracle run recorded a feed: its board is served, settled
        snapshot = self.get_ok(f"/job/{ORACLE_JOB}/live").get_json()
        self.assertEqual(snapshot["state"], "finished")


if __name__ == "__main__":
    unittest.main()
