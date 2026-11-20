"""Opening a workspace again over an unchanged ATIF or OpenTelemetry source
leaves the converted job as it was, so its cached parse is used; a changed
source is converted and parsed again."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from nullius import importers, workspace
from tests.harnesses.test_atif import minimal, write_doc
from tests.test_cli_contract import otlp_doc


def open_again(root, imports):
    """What shared._import_roots does for a root on each open_workspace."""
    module = importers.detect(root)
    return [d for tree in importers.trees(module, root) for d in module.convert(tree, imports)]


def mtimes(job_dir):
    return {os.path.relpath(os.path.join(d, f), job_dir): os.stat(os.path.join(d, f)).st_mtime_ns
            for d, _, files in os.walk(job_dir) for f in files}


class UnchangedSource(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-import-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"

    def check(self, change):
        """Convert, read, open again unchanged, then again after *change*."""
        ws = workspace.create(str(self.tmp / "workspaces"), [str(self.src)], "w")
        [job_dir] = open_again(str(self.src), ws.imports_dir)
        before = mtimes(job_dir)
        self.assertFalse(workspace.load_job(ws, job_dir)[1])     # the first read parses
        time.sleep(0.05)
        open_again(str(self.src), ws.imports_dir)                # opened again; the source unchanged
        moved = sorted(f for f, m in mtimes(job_dir).items() if before.get(f) != m)
        self.assertEqual((moved, workspace.load_job(ws, job_dir)[1]), ([], True))
        change()
        open_again(str(self.src), ws.imports_dir)                # opened again; the source changed
        self.assertNotEqual(mtimes(job_dir), before)
        self.assertFalse(workspace.load_job(ws, job_dir)[1])

    # Each change leaves the job's own result.json and config.json as they were.
    def test_atif(self):
        write_doc(self.src, "run-1.json", minimal())
        edited = minimal()
        edited["steps"][1]["message"] = "redone"
        self.check(lambda: write_doc(self.src, "run-1.json", edited))

    def test_opentelemetry(self):
        write_doc(self.src, "export.json", otlp_doc())
        edited = otlp_doc()
        edited["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "k", "value": {"stringValue": "v"}}]
        self.check(lambda: write_doc(self.src, "export.json", edited))


if __name__ == "__main__":
    unittest.main()
