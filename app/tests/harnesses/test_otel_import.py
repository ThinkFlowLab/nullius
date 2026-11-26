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
from unittest import mock

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

from nullius.importers import otel as importer


def cut_at_config(dump):
    """*dump*, failing on the job's ``config.json``: a refresh cut short before it publishes."""
    def cut(path, data):
        if os.path.basename(path) == "config.json":
            raise OSError("cut short")
        return dump(path, data)
    return cut


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

    def test_a_later_colliding_trace_never_renames_the_trial_already_there(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        first, later = "abcdef1" + "f" * 25, "abcdef1" + "0" * 25

        def export(name, tid):
            with open(os.path.join(src, name), "w") as fh:
                json.dump({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
                    {"traceId": tid, "spanId": "01", "name": "agent run",
                     "startTimeUnixNano": "1", "endTimeUnixNano": "9"}]}]}]}, fh)

        def files(trial):
            root = Path(job_dir, trial)
            return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        export("one.json", first)
        job_dir = importer.convert(src, self.out)[0]
        before = files("agent_run__abcdef1")
        export("two.json", later)
        importer.convert(src, self.out)
        trials = sorted(n for n in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, n)))
        self.assertEqual(trials, ["agent_run__abcdef1", "agent_run__" + later])
        self.assertEqual(files("agent_run__abcdef1"), before)
        os.remove(os.path.join(src, "one.json"))           # gone: its name is still not another trace's
        os.remove(os.path.join(src, "two.json"))
        export("three.json", "abcdef1" + "1" * 25)
        importer.convert(src, self.out)
        trials = sorted(n for n in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, n)))
        self.assertEqual(trials, ["agent_run__abcdef1" + "1" * 25])

    def test_a_name_once_given_to_a_trace_stays_its_after_a_conversion_without_it(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        gone, other, newcomer = "abcdef1" + "f" * 25, "1234567" + "0" * 25, "abcdef1" + "0" * 25

        def export(name, tid, span="agent run"):
            with open(os.path.join(src, name), "w") as fh:
                json.dump({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
                    {"traceId": tid, "spanId": "01", "name": span,
                     "startTimeUnixNano": "1", "endTimeUnixNano": "9"}]}]}]}, fh)

        def trials():
            return sorted(n for n in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, n)))

        for cut in (False, True):
            with self.subTest(cut_before_config=cut):
                for name in os.listdir(src):
                    os.remove(os.path.join(src, name))
                export("a.json", gone)
                export("x.json", other, "other")
                job_dir = importer.convert(src, self.out, job_name=f"cut-{cut}")[0]
                os.remove(os.path.join(src, "a.json"))
                if cut:                                     # a job without reservations, its first refresh cut short
                    config = json.loads(Path(job_dir, "config.json").read_text())
                    del config["trial_documents"]
                    Path(job_dir, "config.json").write_text(json.dumps(config))
                    with mock.patch.object(importer, "_dump", cut_at_config(importer._dump)), \
                            self.assertRaises(OSError):
                        importer.convert(src, self.out, job_name=f"cut-{cut}")
                importer.convert(src, self.out, job_name=f"cut-{cut}")    # its folder is pruned, its name kept
                self.assertEqual(trials(), ["other__1234567"])
                export("b.json", newcomer)
                importer.convert(src, self.out, job_name=f"cut-{cut}")
                self.assertEqual(trials(), ["agent_run__" + newcomer, "other__1234567"])
                export("a.json", gone)                      # back: its own name again
                importer.convert(src, self.out, job_name=f"cut-{cut}")
                self.assertEqual(trials(), ["agent_run__abcdef1", "agent_run__" + newcomer, "other__1234567"])

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
