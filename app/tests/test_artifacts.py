"""Evidence views are bounded, redacted, and confined to named trial files."""
import json
import tempfile
import unittest
from pathlib import Path

from nullius.runs import artifacts


class ArtifactViews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trial = Path(self.tmp.name) / "trial"
        self.trial.mkdir()

    def test_redacts_before_paging_and_retains_later_content(self):
        secret = "sk-abcdefgh1234567890"
        (self.trial / "result.json").write_text("a" * (artifacts.CHUNK_SIZE - 3) + " " + secret + "\n" + "z" * 9000)
        first = artifacts.read(self.trial, "result")
        second = artifacts.read(self.trial, "result", 2)
        self.assertLessEqual(len(first["text"]), artifacts.CHUNK_SIZE)
        self.assertNotIn("abcdefgh", first["text"] + second["text"])
        self.assertIn("[REDACTED]", first["text"] + second["text"])
        self.assertIn("zzz", second["text"])
        self.assertIn(secret, (self.trial / "result.json").read_text())

    def test_unknown_keys_and_escaping_symlinks_are_refused(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("not a trial record")
        (self.trial / "result.json").symlink_to(outside)
        for key in ("result", "../outside.txt", "verifier/report.json"):
            with self.subTest(key=key), self.assertRaises(LookupError):
                artifacts.read(self.trial, key)
        self.assertFalse(artifacts.listing(self.trial)[0]["exists"])

    def test_empty_absent_and_clamped_page_are_distinct(self):
        records = artifacts.listing(self.trial)
        self.assertTrue(all(not r["exists"] and r["size"] is None for r in records))
        (self.trial / "result.json").write_text("")
        self.assertTrue(artifacts.read(self.trial, "result", 100)["empty"])
        self.assertEqual(artifacts.read(self.trial, "result", "invalid")["page"], 1)
        self.assertEqual(artifacts.listing(self.trial)[0]["size"], 0)


    def test_the_artifact_manifest_is_listed_and_read_redacted(self):
        # Harbor's artifacts/manifest.json — the last of a trial's nine files
        # to reach a page. Listed by name like the rest, read through the
        # same redaction, and never reachable under another key.
        keys = [r["key"] for r in artifacts.listing(self.trial)]
        self.assertIn("manifest", keys)
        (self.trial / "artifacts").mkdir()
        (self.trial / "artifacts" / "manifest.json").write_text(json.dumps([
            {"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts",
             "type": "directory", "status": "empty", "service": None},
            {"source": "/root/.env", "destination": "artifacts/env",
             "type": "file", "status": "ok", "service": "MODEL_API_KEY=sk-abcdefgh1234567890"},
        ]))
        record = next(r for r in artifacts.listing(self.trial) if r["key"] == "manifest")
        self.assertEqual((record["filename"], record["exists"]), ("artifacts/manifest.json", True))
        page = artifacts.read(self.trial, "manifest")
        self.assertIn('"status": "empty"', page["text"])
        self.assertIn("[REDACTED]", page["text"])
        self.assertNotIn("abcdefgh", page["text"])
        with self.assertRaises(LookupError):
            artifacts.read(self.trial, "artifacts/manifest.json")


if __name__ == "__main__":
    unittest.main()
