"""API tokens (``nullius.access``): shown once, kept as a hash in one owner-only
file, checked by id and secret, revoked by id; ``run`` reads too, ``read`` does
not run; and the ``nullius access`` command."""
from __future__ import annotations

import calendar
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from nullius import access, cli


class Tokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"NULLIUS_HOME": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)

    def test_tokens_created_at_once_are_all_kept(self):
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: access.create(f"t{n}"), range(24)))
        self.assertEqual({r["name"] for r in access.listing()}, {f"t{n}" for n in range(24)})

    def test_a_token_is_verified_and_its_secret_is_kept_nowhere(self):
        token, record = access.create("ci", "run")
        self.assertEqual(access.verify(token), record)
        path = os.path.join(self.tmp, "config", "api-tokens.json")
        self.assertEqual(access.file(), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn(token, text)
        self.assertNotIn(token.split("_", 2)[2], text)
        self.assertEqual(sorted(json.loads(text)["tokens"][0]), ["created", "expires", "id", "name", "scope", "sha256"])
        self.assertEqual(sorted(access.shown(record)), ["created", "expired", "expires", "id", "name", "scope"])
        self.assertEqual((record["expires"], access.shown(record)["expired"]), (None, False))         # until revoked

    def test_what_is_not_a_token_of_this_server(self):
        token, record = access.create("ci")
        other, _ = access.create("other")
        prefix, token_id, secret = token.split("_", 2)
        for bad in ("", None, 7, "Bearer " + token, token + "x", token[:-1], f"{prefix}_{token_id}_{other.split('_', 2)[2]}",
                    f"{prefix}_{'0' * 12}_{secret}", secret):
            self.assertIsNone(access.verify(bad), bad)
        self.assertTrue(access.revoke(record["id"]))
        self.assertIsNone(access.verify(token))
        self.assertIsNotNone(access.verify(other))                      # the other token still stands
        self.assertFalse(access.revoke(record["id"]))

    def test_no_file_or_a_broken_one_holds_no_token(self):
        self.assertEqual(access.listing(), [])
        token, _ = access.create("ci")
        with open(access.file(), "w") as fh:
            fh.write("{not json")
        self.assertEqual(access.listing(), [])
        self.assertIsNone(access.verify(token))
        for change in (lambda: access.create("other"), lambda: access.revoke("x")):        # never written over
            with self.assertRaisesRegex(ValueError, "cannot be read"):
                change()
        with open(access.file()) as fh:
            self.assertEqual(fh.read(), "{not json")
        with open(access.file(), "w") as fh:                               # a record with no hash, or a scope this does not know
            json.dump({"tokens": [{"id": "a", "scope": "read"}, {"id": "b", "sha256": "x", "scope": "admin"}]}, fh)
        self.assertEqual(access.listing(), [])

    def test_run_reads_too_and_read_does_not_run(self):
        _, reader = access.create("reader", "read")
        _, runner = access.create("runner", "run")
        self.assertEqual([access.allows(reader, s) for s in access.SCOPES], [True, False])
        self.assertEqual([access.allows(runner, s) for s in access.SCOPES], [True, True])
        self.assertFalse(access.allows(None, "read"))

    def test_a_token_with_days_is_refused_once_they_have_passed(self):
        token, record = access.create("ci", "run", days=30)
        made = calendar.timegm(time.strptime(record["created"], "%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(calendar.timegm(time.strptime(record["expires"], "%Y-%m-%dT%H:%M:%SZ")) - made, 30 * 86400)
        self.assertEqual(access.verify(token), record)
        with mock.patch.object(access.time, "time", lambda: made + 30 * 86400 - 1):
            self.assertEqual(access.verify(token), record)
        with mock.patch.object(access.time, "time", lambda: made + 30 * 86400):
            self.assertIsNone(access.verify(token))
            self.assertTrue(access.shown(record)["expired"])
            self.assertEqual([r["id"] for r in access.listing()], [record["id"]])          # still listed, to be revoked
        self.assertTrue(access.expired({"expires": "next week"}))                          # an expiry that cannot be read has passed
        for days in (0, -1, access.MAX_DAYS + 1, 1.5, "30", True):
            with self.assertRaises(ValueError, msg=days):
                access.create("x", "read", days=days)

    def test_names_and_scopes_refused(self):
        access.create("ci")
        for name, scope in (("ci", "read"), ("", "read"), ("a/b", "read"), ("x" * 65, "read"), ("ok", "admin")):
            with self.assertRaises(ValueError, msg=(name, scope)):
                access.create(name, scope)
        self.assertEqual([r["name"] for r in access.listing()], ["ci"])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["access", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_prints_the_token_once_and_lists_no_secret(self):
        code, out, err = self.run_cli("create", "laptop", "--scope", "run")
        token = out.strip()
        self.assertEqual(code, 0)
        self.assertEqual(access.verify(token)["scope"], "run")
        self.assertIn("shown once", err)
        code, out, _ = self.run_cli("list", "--json")
        rows = json.loads(out)
        self.assertEqual([(r["name"], r["scope"]) for r in rows], [("laptop", "run")])
        self.assertNotIn("sha256", out)
        self.assertNotIn(token.split("_", 2)[2], out)
        self.assertEqual(self.run_cli("create", "laptop")[0], 2)           # the name is taken
        code, out, err = self.run_cli("create", "ci", "--days", "7")
        self.assertEqual(code, 0)
        self.assertIn("until " + access.verify(out.strip())["expires"][:10], err)
        self.assertIn("  never ", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("create", "soon", "--days", "0")[0], 2)
        self.assertEqual(self.run_cli("revoke", "nope")[0], 3)
        self.assertEqual(self.run_cli("revoke", rows[0]["id"])[0], 0)
        self.assertIsNone(access.verify(token))


if __name__ == "__main__":
    unittest.main()
