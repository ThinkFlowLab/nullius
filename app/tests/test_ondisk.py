"""A file written whole, a lock, a digest (``nullius.ondisk``): what every
module that keeps a file relies on."""
import hashlib
import os
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from nullius import ondisk


class Whole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "deep", "file.json")

    def mode(self, path=None):
        return stat.S_IMODE(os.stat(path or self.path).st_mode)

    def left(self):
        return sorted(os.listdir(os.path.dirname(self.path)))

    def test_text_and_bytes_land_whole_in_a_folder_made_for_them(self):
        ondisk.write_whole(self.path, "é\nsecond line\n")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), "é\nsecond line\n".encode())
        ondisk.write_whole(self.path, b"\x00\xff")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"\x00\xff")
        self.assertEqual(self.left(), ["file.json"])

    def test_the_mode_is_the_process_default_unless_one_is_asked(self):
        old = os.umask(0o027)
        self.addCleanup(os.umask, old)
        ondisk.write_whole(self.path, "x")
        self.assertEqual(self.mode(), 0o640)                       # 0666 less the umask, as open() gives
        ondisk.write_whole(self.path, "x", mode=0o600)
        self.assertEqual(self.mode(), 0o600)
        ondisk.write_whole(self.path, "x", mode=0o644)
        self.assertEqual(self.mode(), 0o644)                       # the mode asked for, whatever the umask

    def test_a_write_that_fails_leaves_the_file_as_it_was_and_nothing_beside_it(self):
        ondisk.write_whole(self.path, "kept")
        with mock.patch.object(os, "replace", side_effect=OSError("no rename")):
            with self.assertRaises(OSError):
                ondisk.write_whole(self.path, "lost")
        with self.assertRaises(TypeError):
            ondisk.write_whole(self.path, 42)                      # neither text nor bytes
        with open(self.path) as f:
            self.assertEqual(f.read(), "kept")
        self.assertEqual(self.left(), ["file.json"])

    def test_writers_of_one_file_at_once_each_finish_and_one_of_them_wins(self):
        texts = [str(i) * 4096 for i in range(16)]
        failed = []
        def write(text):
            try:
                ondisk.write_whole(self.path, text)
            except Exception as e:                                 # noqa: BLE001 - the failure under test
                failed.append(e)
        threads = [threading.Thread(target=write, args=(t,)) for t in texts]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(failed, [])
        with open(self.path) as f:
            self.assertIn(f.read(), texts)
        self.assertEqual(self.left(), ["file.json"])


class Lock(unittest.TestCase):
    def test_one_holder_at_a_time(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "thing.lock")
        inside, order = threading.Event(), []
        def second():
            inside.wait(5)
            with ondisk.locked(path):
                order.append("second")
        t = threading.Thread(target=second)
        t.start()
        with ondisk.locked(path):
            inside.set()
            t.join(0.3)                                            # it waits while the lock is held
            self.assertTrue(t.is_alive())
            order.append("first")
        t.join(5)
        self.assertEqual(order, ["first", "second"])
        with self.assertRaises(ValueError):                        # given back when the holder fails too
            with ondisk.locked(path):
                raise ValueError("inside")
        with ondisk.locked(path):
            pass


class Digest(unittest.TestCase):
    def test_it_is_the_sha256_of_the_bytes(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"a" * (3 << 20) + b"tail")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(ondisk.sha256_of(f.name), hashlib.sha256(b"a" * (3 << 20) + b"tail").hexdigest())


if __name__ == "__main__":
    unittest.main()
