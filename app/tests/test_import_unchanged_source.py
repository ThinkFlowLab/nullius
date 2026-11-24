"""Opening a workspace again over an unchanged ATIF or OpenTelemetry source
leaves the converted job as it was, so its cached parse is used; a changed
source is converted and parsed again. Each source has a folder and an
identity of its own, whatever its basename."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from nullius import importers, workspace
from nullius.runs import parse
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



def trials(job_dir):
    return sorted(n for n in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, n)))


class OneFolderPerSource(unittest.TestCase):
    """Two sources of one basename, ``a/run`` and ``b/run``, in one imports folder."""

    SOURCES = {"atif": lambda d, n: write_doc(d, f"{n}.json", minimal()),
               "otel": lambda d, n: write_doc(d, f"{n}.json", otlp_doc())}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-import-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def sources(self, fmt):
        a, b, out = (str(self.tmp / fmt / p) for p in ("a/run", "b/run", "imported"))
        self.SOURCES[fmt](a, "one")
        self.SOURCES[fmt](b, "two")
        return a, b, out

    def test_two_sources_of_one_basename_get_two_folders_and_two_identities(self):
        for fmt in self.SOURCES:
            with self.subTest(fmt):
                a, b, out = self.sources(fmt)
                [ja], [jb] = open_again(a, out), open_again(b, out)
                self.assertNotEqual(ja, jb)
                ua, ub = parse.parse_job(ja), parse.parse_job(jb)
                self.assertEqual((ua.uid_source, ub.uid_source), ("declared", "declared"))
                self.assertNotEqual(ua.uid, ub.uid)
                kept, before = trials(ja), mtimes(jb)
                time.sleep(0.05)
                self.assertEqual(open_again(a, out), [ja])               # reconverted: its own folder
                self.assertEqual((parse.parse_job(ja).uid, trials(ja)), (ua.uid, kept))
                self.assertEqual(mtimes(jb), before)                     # the other's untouched

    def test_a_conversion_from_before_ids_keeps_its_folder_and_identity(self):
        for fmt in self.SOURCES:
            with self.subTest(fmt):
                a, b, out = self.sources(fmt)
                [ja] = open_again(a, out)
                cfg_path = os.path.join(ja, "config.json")
                with open(cfg_path) as fh:
                    cfg = json.load(fh)
                del cfg["nullius"]
                with open(cfg_path, "w") as fh:
                    json.dump(cfg, fh, indent=2)
                old = parse.parse_job(ja)
                self.assertEqual(old.uid_source, "derived")
                self.assertEqual(open_again(a, out), [ja])               # reconverted where it was, no id gained
                with open(cfg_path) as fh:
                    self.assertNotIn("nullius", json.load(fh))
                self.assertEqual(parse.parse_job(ja).uid, old.uid)
                [jb] = open_again(b, out)                                # another source: not that folder
                self.assertNotEqual(jb, ja)
                self.assertEqual(parse.parse_job(ja).uid, old.uid)


if __name__ == "__main__":
    unittest.main()
