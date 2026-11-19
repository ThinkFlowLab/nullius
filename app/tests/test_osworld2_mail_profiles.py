"""OSWorld2's mail tasks get their profiles signed out of the live mailboxes their assets ship, and started offline."""
from __future__ import annotations

import contextlib
import io
import os
import tarfile
import tempfile
import unittest
import zlib
from unittest import mock

from tests.test_osworld2_driver import load

PROFILE = ".thunderbird/abc.default-release/"
PREFS = ('user_pref("mail.server.server1.hostname", "mail.example.com");\n'
         'user_pref("mail.server.server1.login_at_startup", true);\n'
         'user_pref("mail.server.server1.check_new_mail", true);\n'
         'user_pref("mail.server.server1.download_on_biff", true);\n'
         '  user_pref("mail.server.server2.check_new_mail",true);\r\n'
         'user_pref("offline.startup_state", 2);\n')
SIGNED_IN = ("logins.json", "logins-backup.json", "signons.sqlite", "cookies.sqlite", "cookies.sqlite-journal", "user.js",
             "storage/default/https+++login.example.com/ls/data.sqlite")


def archive(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 1700000000
            t.addfile(info, io.BytesIO(data))


def members(path):
    with tarfile.open(path) as t:
        return {m.name: t.extractfile(m).read() for m in t if m.isfile()}


class SignedOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        up = os.path.join(self.tmp.name, "up")
        assets = os.path.join(up, "cache", "osworld_v2_assets")
        for d in ("task_006", "task_007", "shared"):
            os.makedirs(os.path.join(assets, d))
        self.files = {PROFILE + "prefs.js": PREFS.encode(), PROFILE + "key4.db": b"k",
                      PROFILE + "Mail/host/Inbox": b"From a\n\nbody\n", PROFILE + "storage/permanent/chrome/idb/x": b"i",
                      **{PROFILE + n: b"fake" for n in SIGNED_IN}}
        archive(os.path.join(assets, "task_006", "thunderbird-profile.tar.gz"), self.files)
        with open(os.path.join(assets, "task_006", "sheet.xlsx"), "wb") as f:
            f.write(b"sheet")
        self.driver = load(up)
        self.unit = os.path.join(self.tmp.name, "unit")
        os.makedirs(self.unit)

    def tearDown(self):
        self.tmp.cleanup()

    def test_other_tasks_keep_the_assets_folder(self):
        self.assertEqual(self.driver._assets(self.unit, "007"), self.driver.ASSETS)
        self.assertFalse(os.path.exists(os.path.join(self.unit, "assets")))

    def test_the_profile_loses_its_sign_ins_and_keeps_its_mail(self):
        root = self.driver._assets(self.unit, "006")
        got = members(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"))
        self.assertEqual(sorted(got), sorted(n for n in self.files if n[len(PROFILE):] not in SIGNED_IN))
        self.assertEqual(got[PROFILE + "Mail/host/Inbox"], self.files[PROFILE + "Mail/host/Inbox"])
        prefs = got[PROFILE + "prefs.js"].decode()
        self.assertNotIn("true", prefs)
        self.assertIn('"mail.server.server1.hostname", "mail.example.com"', prefs)
        self.assertTrue(prefs.endswith('user_pref("offline.autoDetect", false);\n'
                                       'user_pref("offline.startup_state", 3);\n'))

    def test_the_rest_of_the_assets_are_the_originals(self):
        root = self.driver._assets(self.unit, "006")
        for rel in ("shared", "task_007", os.path.join("task_006", "sheet.xlsx")):
            self.assertEqual(os.path.realpath(os.path.join(root, rel)),
                             os.path.realpath(os.path.join(self.driver.ASSETS, rel)))
        self.assertNotIn("thunderbird-profile.tar.gz.part", os.listdir(os.path.join(root, "task_006")))

    def test_the_copy_is_the_same_each_time(self):
        root = self.driver._assets(self.unit, "006")
        with open(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"), "rb") as f:
            first = f.read()
        self.driver._assets(self.unit, "006")
        with open(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"), "rb") as f:
            self.assertEqual(f.read(), first)

    def test_a_missing_profile_is_refused(self):
        os.remove(os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz"))
        with self.assertRaisesRegex(RuntimeError, "no thunderbird-profile.tar.gz"):
            self.driver._assets(self.unit, "006")

    def test_a_hard_link_is_refused(self):
        path = os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            link = tarfile.TarInfo(PROFILE + "prefs.js")
            link.type, link.linkname = tarfile.LNKTYPE, PROFILE + "key4.db"
            t.addfile(link)
        with self.assertRaisesRegex(RuntimeError, "does not rewrite"):
            self.driver._assets(self.unit, "006")

    def test_a_broken_profile_archive_is_a_setup_error(self):
        path = os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz")
        with open(path, "rb") as f:
            whole = f.read()
        patches = {"_vm": lambda *a: "vm", "_task": lambda *_: {"instruction": "", "proxy": False}}
        for name, data, fault in (("not an archive", b"x" * 600, None), ("cut short", whole[:len(whole) // 2], None),
                                  ("bad deflate data", whole, zlib.error("invalid stored block lengths"))):
            with open(path, "wb") as f:
                f.write(data)
            with self.subTest(name), mock.patch.multiple(self.driver, **patches), \
                    mock.patch.object(self.driver, "_signed_out", side_effect=fault) if fault else contextlib.nullcontext(), \
                    self.assertRaises(self.driver.EnvironmentSetupError):
                self.driver.score("tasks__006", self.unit)

    def test_the_null_control_and_the_original_get_the_copy(self):
        envs = []

        def run(argv, cwd, env, *a, **kw):
            envs.append(env)
            raise KeyboardInterrupt

        with mock.patch.object(self.driver, "_vm", lambda *a: "vm"), mock.patch.object(self.driver, "_run", run), \
                mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": False}):
            for start in (lambda: self.driver.score("tasks__006", self.unit),
                          lambda: self.driver.answer("tasks__006", self.unit, "original", "MiniMax-M3",
                                                     "http://127.0.0.1:9/v1", "k")):
                with self.assertRaises(KeyboardInterrupt):
                    start()
        self.assertEqual([e["OSWORLD_FILE_BASE_URL"] for e in envs], [os.path.join(self.unit, "assets")] * 2)
        self.assertEqual(self.driver._env("007")["OSWORLD_FILE_BASE_URL"], self.driver.ASSETS)


if __name__ == "__main__":
    unittest.main()
