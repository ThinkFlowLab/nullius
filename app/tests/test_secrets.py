"""A benchmark's licences, kept per benchmark outside the repository
(``nullius.secrets``): a folder of its own, 0700, each licence a 0600 file
of the bytes it was given, an index that never holds a value; two
benchmarks' names never mix; what a page reads is dates and names; what a
host is handed is one line; and an older ``app/.env`` line is moved only
when asked (``settings.take``)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from nullius import secrets, settings

KEY = "lic-0123456789-not-real"
FILE = b"FAKE-LICENCE-FILE\n\x00\x01binary\xff tail\n"


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"SECRETS_DIR": os.path.join(self.tmp, "secrets")})
        patch.start()
        self.addCleanup(patch.stop)

    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_kept_per_benchmark_in_private_files(self):
        secrets.set("bench-a", "LICENSE_KEY", KEY)
        secrets.set("bench-a", "TOOL_LIC", FILE, kind="file", filename="/home/me/Downloads/tool.lic", expires="2027-01-31")
        root = os.path.join(self.tmp, "secrets")
        self.assertEqual(self.mode(root), 0o700)
        self.assertEqual(self.mode(os.path.join(root, "bench-a")), 0o700)
        for n in ("LICENSE_KEY", "TOOL_LIC", secrets.INDEX):
            self.assertEqual(self.mode(os.path.join(root, "bench-a", n)), 0o600, n)
        self.assertEqual(secrets.value("bench-a", "TOOL_LIC"), FILE)                 # a file keeps its bytes
        self.assertEqual(secrets.value("bench-a", "LICENSE_KEY"), KEY.encode())
        self.assertEqual(sorted(os.listdir(os.path.join(root, "bench-a"))), sorted(["LICENSE_KEY", "TOOL_LIC", secrets.INDEX]))

    def test_a_name_is_scoped_by_its_benchmark(self):
        secrets.set("bench-a", "LICENSE_KEY", KEY)
        secrets.set("bench-b", "LICENSE_KEY", "another-licence-value")
        self.assertEqual(secrets.value("bench-a", "LICENSE_KEY"), KEY.encode())
        self.assertEqual(secrets.value("bench-b", "LICENSE_KEY"), b"another-licence-value")
        secrets.delete("bench-a", "LICENSE_KEY")
        self.assertIsNone(secrets.value("bench-a", "LICENSE_KEY"))
        self.assertEqual(secrets.value("bench-b", "LICENSE_KEY"), b"another-licence-value")
        self.assertEqual(secrets.status("bench-c"), {})

    def test_status_says_dates_and_never_a_value(self):
        secrets.set("bench-a", "LICENSE_KEY", KEY, expires="2001-01-01")
        secrets.set("bench-a", "TOOL_LIC", FILE, kind="file", filename="tool.lic")
        got = secrets.status("bench-a")
        self.assertEqual(set(got), {"LICENSE_KEY", "TOOL_LIC"})
        self.assertTrue(got["LICENSE_KEY"]["set"] and got["LICENSE_KEY"]["expired"])
        self.assertEqual((got["TOOL_LIC"]["kind"], got["TOOL_LIC"]["filename"], got["TOOL_LIC"]["expires"]), ("file", "tool.lic", ""))
        text = json.dumps(got)
        self.assertNotIn(KEY, text)
        self.assertNotIn("FAKE-LICENCE", text)
        with open(os.path.join(self.tmp, "secrets", "bench-a", secrets.INDEX)) as f:
            index = f.read()
        self.assertNotIn(KEY, index)
        self.assertNotIn("FAKE-LICENCE", index)
        os.unlink(os.path.join(self.tmp, "secrets", "bench-a", "TOOL_LIC"))        # an index line without its file is not set
        self.assertEqual(set(secrets.status("bench-a")), {"LICENSE_KEY"})

    def test_what_is_refused(self):
        for bench, name in (("../x", "A"), ("Bench", "A"), ("b", "lower"), ("b", "A-B")):
            with self.assertRaises(ValueError):
                secrets.set(bench, name, "x")
        for kind, value in (("value", ""), ("value", "two\nlines"), ("server", "no-port"), ("server", "host:99999"),
                            ("server", "host:27000/x"), ("file", b""), ("file", b"x" * (secrets.MAX_BYTES + 1)), ("nope", "x")):
            with self.subTest(kind=kind, value=value[:20]):
                with self.assertRaises(ValueError):
                    secrets.set("b", "A", value, kind=kind)
        with self.assertRaises(ValueError):
            secrets.set("b", "A", "x", expires="next year")
        secrets.set("b", "SERVER", "lic.example.com:27000", kind="server")
        secrets.set("b", "SERVER_IP", "10.0.0.5:1700", kind="server")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "secrets", "Bench")))

    def test_replaced_whole_and_removed(self):
        secrets.set("b", "A", "first-value")
        secrets.set("b", "A", "  second-value \n")
        self.assertEqual(secrets.value("b", "A"), b"second-value")
        self.assertTrue(secrets.delete("b", "A"))
        self.assertFalse(secrets.delete("b", "A"))
        self.assertEqual(secrets.status("b"), {})
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "secrets", "b")) if n.startswith(".tmp")], [])

    def test_unpack_decodes_a_host_payload_and_preserves_binary_bytes(self):
        payload = {"A": base64.b64encode(KEY.encode()).decode(), "F": base64.b64encode(FILE).decode()}
        line = base64.b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(secrets.unpack(line), {"A": KEY.encode(), "F": FILE})
        self.assertEqual(secrets.unpack("not base64 at all"), {})


class MoveFromEnvFile(unittest.TestCase):
    def test_a_line_is_taken_out_on_request_and_settings_are_refused(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, ".env")
        with open(path, "w") as f:
            f.write("OPENAI_API_KEY=sk-x\nOLD_LICENSE=abc-123-not-real\n# a comment\n")
        settings.LOADED_KEYS.add("OLD_LICENSE")
        with mock.patch.dict(os.environ, {"OLD_LICENSE": "abc-123-not-real"}):
            self.assertEqual(settings.take(path, "OLD_LICENSE"), "abc-123-not-real")
            self.assertNotIn("OLD_LICENSE", os.environ)
        with open(path) as f:
            self.assertEqual(f.read(), "OPENAI_API_KEY=sk-x\n# a comment\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIsNone(settings.take(path, "OLD_LICENSE"))
        with self.assertRaises(ValueError):
            settings.take(path, "OPENAI_API_KEY")

    def test_the_settings_page_has_no_group_for_them(self):
        self.assertNotIn("Benchmark secrets", settings.GROUPS)
        self.assertFalse(hasattr(settings, "benchmark_secrets"))


class LicenceFolderTest(unittest.TestCase):
    """A licence file goes only into a folder this user alone owns: a shared
    place like /dev/shm may hold a folder or a link someone else put there."""

    def bench(self):
        return {"key": "b", "environments": {"e": {"needs_secrets": [{"name": "LIC", "to": "agent", "kind": "file",
                                                                       "path": "/opt/x/lic"}]}}}

    def test_written_0600_in_a_0700_folder(self):
        from nullius.standard import run
        with tempfile.TemporaryDirectory() as d:
            spill = os.path.join(d, "spill")
            got = run.Licences.handed(self.bench(), {"LIC": b"secret"}, spill)
            p = got.given["LIC"]["path"]
            self.assertEqual(oct(os.stat(spill).st_mode & 0o777), "0o700")
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")

    def test_a_folder_open_to_others_or_a_link_is_refused(self):
        from nullius.standard import run
        with tempfile.TemporaryDirectory() as d:
            wide = os.path.join(d, "wide")
            os.mkdir(wide, 0o777)
            os.chmod(wide, 0o777)
            with self.assertRaises(run.Refused):
                run.Licences.handed(self.bench(), {"LIC": b"x"}, wide)
            target = os.path.join(d, "target")
            os.mkdir(target, 0o700)
            link = os.path.join(d, "link")
            os.symlink(target, link)
            with self.assertRaises(run.Refused):
                run.Licences.handed(self.bench(), {"LIC": b"x"}, link)
            self.assertEqual(os.listdir(target), [])

    def test_a_link_planted_as_the_file_is_not_followed(self):
        from nullius.standard import run
        with tempfile.TemporaryDirectory() as d:
            spill = os.path.join(d, "spill")
            os.mkdir(spill, 0o700)
            victim = os.path.join(d, "victim")
            open(victim, "w").write("keep")
            os.symlink(victim, os.path.join(spill, "LIC"))
            run.Licences.handed(self.bench(), {"LIC": b"secret"}, spill)
            self.assertEqual(open(victim).read(), "keep")


if __name__ == "__main__":
    unittest.main()
