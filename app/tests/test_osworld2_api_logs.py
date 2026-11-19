"""OSWorld2's api_logs: each image kept once, every file given back byte for byte, a failure leaving each whole."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
LIMIT = 20


def module(name, path, **env):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", [path]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, **env):
        spec.loader.exec_module(loaded)
    return loaded


api_logs = module("osworld2_api_logs", os.path.join(HERE, "api_logs.py"))


def jpeg(n, size=3000):
    return b"\xff\xd8\xff\xe0" + bytes([n % 256]) * size


def png(n):
    return b"\x89PNG\r\n\x1a\n" + bytes([n % 256]) * 2000


def b64(data):
    return base64.b64encode(data).decode()


def call(i, window=3):
    """One model call as the agent logs it: its window's screenshots, newest last, in Anthropic blocks."""
    blocks = [{"type": "text", "text": f"step {i} é中 \"quoted\" \\ /"}]
    blocks += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(jpeg(j))}}
               for j in range(max(0, i - window + 1), i + 1)]
    return {"model": "m", "request_body": {"system": "s", "messages": [{"role": "user", "content": blocks}],
                                           "temperature": 0.6, "max_tokens": 1e-05},
            "response_body": {"content": [{"type": "text", "text": "pyautogui.click(1, 2)"}], "usage": None},
            "request_time": 1700000000000 + i, "response_time": 1700000000500 + i}


def odd():
    """Strings pack must leave inline, beside a PNG it packs."""
    text = b64(b"plain text, no image " * 40)
    loose = b64(jpeg(7, 600))
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    loose = loose[:-3] + letters[letters.index(loose[-3]) | 1] + "=="
    return {"a": b64(png(1)), "b": text, "c": loose, "d": b64(jpeg(8, 30)), "e": "x" + b64(jpeg(9)),
            "f": [b64(jpeg(1)), b64(png(1))]}


def read(path):
    with open(path, "rb") as f:
        return f.read()


def digest(folder):
    return {name: hashlib.sha256(read(os.path.join(folder, name))).hexdigest()
            for name in sorted(os.listdir(folder)) if os.path.isfile(os.path.join(folder, name))}


class Logs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = os.path.join(self.tmp, "api_logs")
        os.makedirs(self.dir)
        for i, entry in enumerate([call(i) for i in range(6)] + [odd()]):
            with open(os.path.join(self.dir, f"{i + 1:03d}_{1700000000000 + i}.json"), "w") as f:
                json.dump(entry, f, ensure_ascii=False)
        os.chmod(os.path.join(self.dir, "001_1700000000000.json"), 0o640)
        os.utime(os.path.join(self.dir, "001_1700000000000.json"), ns=(1, 2_000_000_000))
        self.before = digest(self.dir)

    def images(self):
        return sorted(os.listdir(os.path.join(self.dir, api_logs.IMAGES)))

    def test_each_image_is_written_once_and_every_file_restores_byte_for_byte(self):
        done = api_logs.pack(self.dir)
        self.assertEqual((done["files"], done["packed"], done["images"], done["written"]), (7, 7, 7, 7))
        self.assertLess(done["after"] * 5, done["before"])
        names = self.images()
        self.assertEqual(len(names), 7)
        self.assertEqual(sum(n.endswith(".png") for n in names), 1)
        for name in names:
            with open(os.path.join(self.dir, api_logs.IMAGES, name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), name.split(".")[0])
        with open(os.path.join(self.dir, "006_1700000000005.json"), encoding="utf-8") as f:
            entry = json.load(f)
        self.assertEqual(entry["_packed_images"], "images")
        links = [b["source"]["data"] for b in entry["request_body"]["messages"][0]["content"] if b["type"] == "image"]
        self.assertEqual(links, [f"images/{hashlib.sha256(jpeg(j)).hexdigest()}.jpg" for j in (3, 4, 5)])
        with open(os.path.join(self.dir, "007_1700000000006.json"), encoding="utf-8") as f:
            kept = json.load(f)
        self.assertEqual({k: v == odd()[k] for k, v in kept.items() if k != "_packed_images"},
                         {"a": False, "b": True, "c": True, "d": True, "e": True, "f": False})
        self.assertEqual(api_logs.restore(self.dir), {"restored": 7, "failed": 0})
        self.assertEqual({k: v for k, v in digest(self.dir).items()}, self.before)
        st = os.stat(os.path.join(self.dir, "001_1700000000000.json"))
        self.assertEqual((st.st_mode & 0o777, st.st_mtime_ns), (0o640, 2_000_000_000))

    def test_running_either_again_changes_nothing(self):
        api_logs.pack(self.dir)
        packed, images = digest(self.dir), digest(os.path.join(self.dir, api_logs.IMAGES))
        again = api_logs.pack(self.dir)
        self.assertEqual((again["packed"], again["written"]), (0, 0))
        self.assertEqual((digest(self.dir), digest(os.path.join(self.dir, api_logs.IMAGES))), (packed, images))
        api_logs.restore(self.dir)
        self.assertEqual(api_logs.restore(self.dir), {"restored": 0, "failed": 0})
        self.assertEqual(digest(self.dir), self.before)

    def test_a_file_pack_did_not_mark_is_left_alone_by_both(self):
        name = hashlib.sha256(jpeg(0)).hexdigest() + ".jpg"
        shapes = {"x_link.json": json.dumps({"a": "images/" + name, "b": b64(jpeg(0))}),
                  "x_list.json": json.dumps([b64(jpeg(0))]),
                  "x_empty.json": "{}",
                  "x_broken.json": '{"a": "' + b64(jpeg(0))}
        for file, text in shapes.items():
            with open(os.path.join(self.dir, file), "w") as f:
                f.write(text)
        api_logs.pack(self.dir)
        for file, text in shapes.items():
            with open(os.path.join(self.dir, file)) as f:
                self.assertEqual(f.read(), text, file)
        self.assertIn(name, self.images())
        api_logs.restore(self.dir)
        for file, text in shapes.items():
            with open(os.path.join(self.dir, file)) as f:
                self.assertEqual(f.read(), text, file)

    def test_a_failed_write_leaves_every_file_whole_and_a_second_run_finishes(self):
        replace = os.replace
        for failing in ("images", "001_"):
            with self.subTest(failing=failing):
                def broken(src, dst):
                    if failing in dst:
                        raise OSError(28, "No space left on device")
                    return replace(src, dst)

                with mock.patch.object(api_logs.os, "replace", broken), self.assertRaises(OSError):
                    api_logs.pack(self.dir)
                self.assertEqual(digest(self.dir), self.before)
                self.assertEqual([n for n in os.listdir(os.path.join(self.dir, api_logs.IMAGES))
                                  if n.endswith(api_logs.TEMPORARY)], [])
        done = api_logs.pack(self.dir)
        self.assertEqual(done["packed"], 7)
        api_logs.restore(self.dir)
        self.assertEqual(digest(self.dir), self.before)

    def test_images_are_on_disk_before_a_file_names_them(self):
        written = []
        write = api_logs._write
        with mock.patch.object(api_logs, "_write", lambda path, *a: (written.append(path), write(path, *a))):
            api_logs.pack(self.dir)
        for i, path in enumerate(written):
            if os.path.dirname(path) == self.dir:
                with open(path, encoding="utf-8") as f:
                    named = {v.split("/")[-1] for v in json.dumps(json.load(f)).split('"') if v.startswith("images/")}
                self.assertTrue(named <= {os.path.basename(p) for p in written[:i]}, path)

    def test_what_a_stopped_pack_left_half_written_is_removed(self):
        api_logs.pack(self.dir)
        left = [os.path.join(self.dir, ".001_x.json.abc" + api_logs.TEMPORARY),
                os.path.join(self.dir, api_logs.IMAGES, ".a.jpg.abc" + api_logs.TEMPORARY)]
        for path in left:
            open(path, "w").close()
        api_logs.pack(self.dir)
        self.assertFalse(any(os.path.exists(p) for p in left))

    def test_a_folder_another_process_works_on_is_left_alone(self):
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; fcntl.flock(int(sys.argv[1]), "
                                   "fcntl.LOCK_EX); print(flush=True); time.sleep(30)", str(fd)],
                                  pass_fds=(fd,), stdout=subprocess.PIPE)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        holder.stdout.readline()
        self.assertTrue(api_logs.pack(self.dir)["busy"])
        self.assertTrue(api_logs.restore(self.dir)["busy"])
        self.assertEqual(digest(self.dir), self.before)

    def test_a_missing_or_wrong_image_keeps_its_file_packed(self):
        api_logs.pack(self.dir)
        packed = digest(self.dir)["001_1700000000000.json"]
        first = os.path.join(self.dir, api_logs.IMAGES, hashlib.sha256(jpeg(0)).hexdigest() + ".jpg")
        os.remove(first)
        self.assertEqual(api_logs.restore(self.dir), {"restored": 4, "failed": 3})
        self.assertEqual(digest(self.dir)["001_1700000000000.json"], packed)
        with open(first, "wb") as f:
            f.write(jpeg(99))
        self.assertEqual(api_logs.restore(self.dir), {"restored": 0, "failed": 3})
        self.assertEqual(digest(self.dir)["001_1700000000000.json"], packed)
        with open(first, "wb") as f:
            f.write(jpeg(0))
        api_logs.restore(self.dir)
        self.assertEqual(digest(self.dir), self.before)

    def test_links_are_not_followed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        left = ".x" + api_logs.TEMPORARY
        open(os.path.join(outside, left), "w").close()
        os.symlink(outside, os.path.join(self.dir, api_logs.IMAGES))
        with self.assertRaises(OSError):
            api_logs.pack(self.dir)
        self.assertEqual(os.listdir(outside), [left])
        self.assertEqual(digest(self.dir), self.before)
        os.remove(os.path.join(self.dir, api_logs.IMAGES))
        api_logs.pack(self.dir)
        packed = digest(self.dir)
        os.rename(os.path.join(self.dir, api_logs.IMAGES), os.path.join(self.tmp, "kept"))
        for name in os.listdir(os.path.join(self.tmp, "kept")):
            shutil.copy(os.path.join(self.tmp, "kept", name), outside)
        os.symlink(outside, os.path.join(self.dir, api_logs.IMAGES))
        self.assertEqual(api_logs.restore(self.dir), {"restored": 0, "failed": 7})
        self.assertIn(left, os.listdir(outside))
        self.assertEqual(digest(self.dir), packed)
        linked = os.path.join(self.tmp, "linked")
        os.symlink(self.dir, linked)
        with self.assertRaises(OSError):
            api_logs.pack(linked)

    def test_the_command_line_packs_and_restores(self):
        for act in ("pack", "restore"):
            out = subprocess.run([sys.executable, os.path.join(HERE, "api_logs.py"), act, self.dir],
                                 capture_output=True, text=True, check=True)
            self.assertIn(self.dir, json.loads(out.stdout))
        self.assertEqual(digest(self.dir), self.before)
        self.assertNotEqual(subprocess.run([sys.executable, os.path.join(HERE, "api_logs.py"), "unpack", self.dir],
                                           capture_output=True).returncode, 0)


SCRIPT = """import json, os, sys, time
folder = os.path.join("results", "pyautogui", "screenshot", "m", "tasks", "001", "api_logs")
os.makedirs(folder, exist_ok=True)
for i, entry in enumerate(json.loads(sys.argv[1])):
    with open(os.path.join(folder, "%03d_1.json" % (i + 1)), "w") as f:
        json.dump(entry, f, ensure_ascii=False)
    if i == 0 and sys.argv[2] == "stay":
        open("ready", "w").close()
        time.sleep(60)
"""


class Reaper(unittest.TestCase):
    """The driver's reaper packs the unit's logs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        env = mock.patch.dict(os.environ, NULLIUS_UPSTREAM=self.tmp)
        env.start()
        self.addCleanup(env.stop)
        self.driver = module("osworld2_driver_logs", os.path.join(HERE, "driver.py"), NULLIUS_UPSTREAM=self.tmp)
        self.folder = os.path.join(self.unit, "results", "pyautogui", "screenshot", "m", "tasks", "001", "api_logs")
        self.addCleanup(self.driver.CLOSED.clear)

    def packed(self, n):
        t0 = time.time()
        while time.time() - t0 < LIMIT:
            try:
                files = [f for f in os.listdir(self.folder) if f.endswith(".json")]
                if len(files) == n and all(read(os.path.join(self.folder, f)).startswith(api_logs.MARK)
                                           for f in files):
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def run_script(self, stay, failed):
        argv = [sys.executable, "-c", SCRIPT, json.dumps([call(i) for i in range(3)]), stay]
        try:
            self.driver._run(argv, self.unit, dict(os.environ), os.path.join(self.unit, "nop.log"), "the run")
        except RuntimeError as e:
            failed.append(str(e))

    def test_an_ended_run_is_packed(self):
        failed = []
        self.run_script("go", failed)
        self.assertEqual(failed, [])
        self.assertTrue(self.packed(3))
        self.assertEqual(api_logs.restore(self.folder)["restored"], 3)

    def test_a_failed_run_is_packed(self):
        argv = [sys.executable, "-c", SCRIPT + "sys.exit(3)\n", json.dumps([call(i) for i in range(2)]), "go"]
        with self.assertRaises(RuntimeError):
            self.driver._run(argv, self.unit, dict(os.environ), os.path.join(self.unit, "nop.log"), "the run")
        self.assertTrue(self.packed(2))

    def test_a_stopped_run_is_packed_once_its_group_is_gone(self):
        failed = []
        t = threading.Thread(target=self.run_script, args=("stay", failed), daemon=True)
        t.start()
        t0 = time.time()
        while not os.path.exists(os.path.join(self.unit, "ready")) and time.time() - t0 < LIMIT:
            time.sleep(0.05)
        self.driver.CLOSED.set()
        t.join(LIMIT)
        self.assertTrue(failed and "was stopped" in failed[0])
        self.assertTrue(self.packed(1))

    def test_a_group_still_there_is_not_packed(self):
        packing = []
        with mock.patch.object(self.driver.os, "killpg"), mock.patch.object(self.driver, "_gone", lambda *_: False), \
                mock.patch.object(self.driver, "_pack", packing.append):
            self.driver.reap(1, self.unit, False)
        self.assertEqual(packing, [])
        with mock.patch.object(self.driver.os, "killpg", side_effect=ProcessLookupError), \
                mock.patch.object(self.driver, "_pack", packing.append):
            self.driver.reap(1, self.unit, False)
        self.assertEqual(packing, [self.unit])

    def test_no_packer_leaves_the_logs_and_the_unit_alone(self):
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "001_1.json"), "w") as f:
            json.dump(call(0), f, ensure_ascii=False)
        before = digest(self.folder)
        with mock.patch.dict(sys.modules, api_logs=None):
            self.driver._pack(self.unit)
        self.assertEqual(digest(self.folder), before)
        self.assertIn("api_logs not packed: ModuleNotFoundError", self.said()[0])

    def said(self):
        with open(os.path.join(self.unit, self.driver.PACK_LOG), encoding="utf-8") as f:
            return f.read().splitlines()

    def test_each_folder_is_packed_on_its_own_and_said_in_one_line(self):
        other = self.folder.replace("001", "002")
        for folder in (self.folder, other):
            os.makedirs(folder)
            with open(os.path.join(folder, "001_1.json"), "w") as f:
                json.dump(call(1), f, ensure_ascii=False)
        before = digest(self.folder)
        pack = api_logs.pack

        def broken(folder):
            if folder == self.folder:
                raise OSError(28, "No space left on device")
            return pack(folder)

        with mock.patch.dict(sys.modules, api_logs=mock.Mock(pack=broken)):
            self.driver._pack(self.unit)
        self.assertEqual(digest(self.folder), before)
        self.assertTrue(read(os.path.join(other, "001_1.json")).startswith(api_logs.MARK))
        lines = sorted(self.said(), key=lambda ln: "002" in ln)
        self.assertEqual(len(lines), 2)
        self.assertIn("001/api_logs {\"error\": \"OSError: [Errno 28] No space left on device\"}", lines[0])
        self.assertIn("002/api_logs {\"files\": 1, \"packed\": 1, \"images\": 2,", lines[1])
        self.assertRegex(lines[1], self.driver.RECORD)

    def test_a_unit_with_no_api_logs_says_nothing(self):
        self.driver._pack(self.unit)
        self.assertFalse(os.path.exists(os.path.join(self.unit, self.driver.PACK_LOG)))


class Snapshot(unittest.TestCase):
    def test_a_hosts_snapshot_carries_the_packer(self):
        from pathlib import Path

        from nullius.standard import payload
        self.assertIn("api_logs.py", payload._entry(Path(HERE)))


if __name__ == "__main__":
    unittest.main()
