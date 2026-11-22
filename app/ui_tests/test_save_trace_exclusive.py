"""Saving a trace view never overwrites a file, even one that appears
after the route checked the name was free, and leaves no partial file."""
from __future__ import annotations

import builtins
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace

from .test_routes import web

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
TRIAL = "hello-world__mAXMKXR"


class SaveTraceExclusive(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nullius-save-trace-")))
        shutil.copytree(FIX / "harbor-jobs" / "liveboard-oracle-x4", tmp / "root" / "job-x")
        agent = tmp / "root" / "job-x" / TRIAL / "agent"
        shutil.copyfile(FIX / "trace_rich.json", agent / "trace.json")
        stack.enter_context(patch.object(web.shared, "WS_HOME", str(tmp / "workspaces")))
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}, clear=True))
        web.shared.open_workspace(workspace.create(web.shared.WS_HOME, [str(tmp / "root")], "w"))
        self.client = web.app.test_client()
        self.client.get(f"/trial/job-x/{TRIAL}")
        self.dest = agent / "view.json"

    def save(self):
        with self.client.session_transaction() as s:
            token = s["csrf"]
        return self.client.post(f"/trial/job-x/{TRIAL}/save-trace", follow_redirects=True,
                                data={"csrf": token, "filename": "view.json", "hide_span": "fbe9aa3b5d05bfec"})

    def test_a_file_that_appears_after_the_check_is_not_overwritten(self):
        real = web.traces.trace.filtered_document

        def another_writer_first(data, filt):                     # while the route reads the source
            self.dest.write_text('{"someone else": "wrote this"}')
            return real(data, filt)

        with patch.object(web.traces.trace, "filtered_document", another_writer_first):
            r = self.save()
        self.assertEqual(self.dest.read_text(), '{"someone else": "wrote this"}', "the other file was overwritten")
        self.assertIn("already exists; nothing was overwritten.", r.get_data(as_text=True))

    def test_a_failed_write_leaves_no_partial_file(self):
        def disk_full(path, mode="r", *args, **kwargs):
            fh = builtins.open(path, mode, *args, **kwargs)
            if mode == "x":
                real_write = fh.write

                def write(text):
                    real_write(text[:10])
                    fh.flush()
                    raise OSError(28, "No space left on device")
                fh.write = write
            return fh

        with patch.object(web.traces, "open", disk_full, create=True):
            r = self.save()
        self.assertFalse(self.dest.exists(), "a partial view was left behind")
        self.assertIn("No space left on device", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
