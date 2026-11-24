"""OpenTelemetry importer detection and refusal behavior.

Unrelated inputs are not claimed. Unreadable exports return redacted errors
with source-relative filenames and leave no partial output."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

from nullius.importers import otel as importer


class Detect(unittest.TestCase):

    def test_a_directory_of_only_refused_files_is_not_claimed(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "broken.json"), "w") as fh:
            fh.write('{"resourceSpans": [')
        with open(os.path.join(root, "other.json"), "w") as fh:
            json.dump({"schema_version": "ATIF-v1.6", "session_id": "s", "agent": {}, "steps": []}, fh)
        self.assertFalse(importer.detect(root))


class Convert(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_nothing_readable_is_an_error_naming_the_refusals(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        with open(os.path.join(src, "broken.json"), "w") as fh:
            fh.write('{"resourceSpans": [')
        with open(os.path.join(src, "spans.json"), "w") as fh:
            json.dump({"spans": [], "password": "hunter2hunter2"}, fh)
        with self.assertRaises(SystemExit) as ctx:
            importer.convert(src, self.out)
        message = str(ctx.exception)
        self.assertIn("no OTLP/JSON trace exports", message)
        self.assertIn("broken.json: not JSON", message)
        self.assertEqual(message.count("broken.json"), 1)
        self.assertNotIn("hunter2", message)
        self.assertEqual(os.listdir(self.out), [])          # nothing half-written

    def test_traces_whose_short_names_collide_stay_two_trials(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        ids = ["abcdef1" + "0" * 25, "abcdef1" + "f" * 25, "1234567" + "0" * 25]
        spans = [{"traceId": tid, "spanId": "01", "name": "agent run",
                  "startTimeUnixNano": "1", "endTimeUnixNano": "9"} for tid in ids]
        with open(os.path.join(src, "export.json"), "w") as fh:
            json.dump({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": spans}]}]}, fh)
        job_dir = importer.convert(src, self.out)[0]
        trials = sorted(n for n in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, n)))
        self.assertEqual(trials, ["agent_run__1234567", "agent_run__" + ids[0], "agent_run__" + ids[1]])
        recorded = {json.load(open(os.path.join(job_dir, t, "result.json")))["source_document"]["trace_id"]
                    for t in trials}
        self.assertEqual(recorded, set(ids))

    def test_a_refused_file_deeper_down_is_named_by_its_path_under_the_root_once(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        os.makedirs(os.path.join(src, "batch-2"))
        with open(os.path.join(src, "batch-2", "jaeger-trace.json"), "w") as fh:
            # mentions the key in prose, so the head check lets it through to the reader
            json.dump({"data": [{"traceID": "abc", "spans": []}], "format": "jaeger",
                       "note": "converted from resourceSpans by hand; not OTLP/JSON"}, fh)
        self.assertEqual(len(importer.refused(src)), 1)
        reason = importer.refused(src)[0]
        self.assertTrue(reason.startswith("batch-2/jaeger-trace.json: not an OTLP/JSON trace export"), reason)
        self.assertEqual(reason.count("jaeger-trace.json"), 1)


if __name__ == "__main__":
    unittest.main()
