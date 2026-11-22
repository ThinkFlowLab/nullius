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
        self.path = os.path.join(self.tmp, "file.json")

    def mode(self, path=None):
        return stat.S_IMODE(os.stat(path or self.path).st_mode)

    def left(self):
        return sorted(os.listdir(self.tmp))

    def test_text_and_bytes_land_whole(self):
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

    def test_a_mode_the_umask_leaves_whole_is_the_files_from_its_creation(self):
        with mock.patch.object(os, "fchmod", side_effect=PermissionError("no chmod here")):
            ondisk.write_whole(self.path, "x", mode=0o600)         # as mkstemp gives it: asked for when made, never set after
        self.assertEqual(self.mode(), 0o600)

    def test_a_mode_that_is_refused_leaves_no_file_and_no_open_descriptor(self):
        old = os.umask(0o027)
        self.addCleanup(os.umask, old)
        held = []
        def refuse(fd, mode):
            held.append(fd)
            raise PermissionError("no chmod here")
        with mock.patch.object(os, "fchmod", refuse):
            with self.assertRaises(PermissionError):
                ondisk.write_whole(self.path, "x", mode=0o644)     # the umask took a bit off: it has to be set
        self.assertEqual(self.left(), [])
        with self.assertRaises(OSError):
            os.fstat(held[0])

    def test_it_is_first_written_beside_its_path_under_a_name_of_its_own_with_its_mode(self):
        seen, rename = [], os.replace
        def spy(src, dst):
            seen.append((os.path.dirname(src), os.path.basename(src), stat.S_IMODE(os.stat(src).st_mode)))
            return rename(src, dst)
        with mock.patch.object(os, "replace", spy):
            ondisk.write_whole(self.path, "x", mode=0o600)
            ondisk.write_whole(self.path, "y", mode=0o600)
        self.assertEqual([s[0] for s in seen], [self.tmp, self.tmp])   # a rename within one folder, never across devices
        self.assertNotEqual(seen[0][1], seen[1][1])
        self.assertEqual([s[2] for s in seen], [0o600, 0o600])

    def test_a_file_whose_name_is_as_long_as_a_name_may_be_is_written(self):
        path = os.path.join(self.tmp, "n" * os.pathconf(self.tmp, "PC_NAME_MAX"))
        ondisk.write_whole(path, "x")
        self.assertEqual(self.left(), [os.path.basename(path)])

    def test_a_folder_that_is_gone_is_an_error_and_is_not_made_again(self):
        gone = os.path.join(self.tmp, "gone")
        with self.assertRaises(FileNotFoundError):
            ondisk.write_whole(os.path.join(gone, "file.json"), "x")
        self.assertEqual(self.left(), [])

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
        gate, rename, failed = threading.Barrier(len(texts), timeout=5), os.replace, []
        def held_rename(src, dst):
            gate.wait()                                            # every writer has written its own file, none has renamed
            return rename(src, dst)
        def write(text):
            try:
                ondisk.write_whole(self.path, text)
            except Exception as e:                                 # noqa: BLE001 - the failure under test
                failed.append(e)
        with mock.patch.object(os, "replace", held_rename):
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
