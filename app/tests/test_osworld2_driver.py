"""OSWorld2's driver cleans up after a unit whose docker hangs or whose vm/volumes note cannot be trusted.
A fake ``docker`` on PATH stands in."""
from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")
VOLUME = "a" * 64
OTHER = "b" * 64
HANG = "30"
LIMIT = 10
DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
[ -z "$FAKE_ALIVE" ] || [ -e "$FAKE_ALIVE" ] || exit 0
c=cid1 v="$FAKE_VOLUME"
[ -n "$FAKE_SECOND" ] && [ -e "$FAKE_SECOND" ] && c=cid2 v="$FAKE_OTHER"
case "$1" in
  ps) echo "$c" ;;
  inspect) i=0
    while [ "$i" -lt "$(( ${FAKE_DOCKER_DELAY:-0} * 10 ))" ] && [ ! -e "$FAKE_RELEASE" ]; do sleep 0.1; i=$((i + 1)); done
    printf '%s\\t[{"Type":"bind","Source":"%s/vm/x.qcow2"},{"Type":"volume","Name":"%s","Source":"/v"}]\\n' "$c" "$FAKE_UNIT" "$v" ;;
esac
"""


def load(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver", os.path.join(HERE, "driver.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(module)
    return module


CHILD = """import os, signal, sys, time
def term(*_):
    with open(sys.argv[1], "w") as f:
        f.write(repr(time.time()))
    if len(sys.argv) > 3:
        os.remove(sys.argv[3])
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
open(sys.argv[2], "w").close()
time.sleep(60)
"""

KILLED = """import importlib.util, sys
here, unit, started = sys.argv[1:4]
sys.argv = ["driver.py"]
sys.path.insert(0, here)
spec = importlib.util.spec_from_file_location("osworld2_driver", here + "/driver.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
driver.START_LOCK = unit + "/start.lck"
driver._run(["sh", "-c", "echo '%s'; echo $$ > %s; sleep 60" % (driver.STARTED, started)], unit, None,
            unit + "/nop.log", "the run", stagger=True)
"""


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(os.path.join(self.unit, "vm"))
        self.note = os.path.join(self.unit, "vm", "volumes")
        self.calls = os.path.join(self.tmp, "calls")
        env = mock.patch.dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], FAKE_DOCKER_LOG=self.calls,
                              FAKE_UNIT=self.unit, FAKE_VOLUME=VOLUME, FAKE_DOCKER_DELAY="0",
                              FAKE_RELEASE=os.path.join(self.tmp, "release"))
        env.start()
        self.addCleanup(env.stop)
        threads = set(threading.enumerate())
        self.addCleanup(lambda: [t.join(30) for t in set(threading.enumerate()) - threads])
        self.addCleanup(lambda: open(os.environ["FAKE_RELEASE"], "w").close())
        self.driver = load(self.tmp)
        self.driver.START_LOCK = os.path.join(self.tmp, "start.lck")

    def lines(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def removed(self):
        return [ln for ln in self.lines() if ln.startswith(("rm ", "volume rm "))]

    def remove(self):
        t = threading.Thread(target=self.driver._remove_vms, args=(self.unit,), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "the removal waited")
        return self.removed()

    def run_unit(self, failed, command="sleep 30"):
        """The driver's run of a process that starts a VM, in a thread; every process it starts is waited for."""
        started = []

        def popen(*a, **kw):
            p = subprocess.Popen(*a, **kw)
            started.append(p)
            threading.Thread(target=p.wait, daemon=True).start()
            return p

        self.driver.subprocess = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess)
                                                          if not k.startswith("__") and k != "Popen"}, Popen=popen)

        def end():
            for p in started:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()

        self.addCleanup(end)

        def go():
            try:
                self.driver._run(["sh", "-c", f"echo '{self.driver.STARTED}'; {command}"], self.unit, dict(os.environ),
                                 os.path.join(self.unit, "nop.log"), "the run", stagger=True)
            except RuntimeError as e:
                failed.append(str(e))

        t = threading.Thread(target=go)
        t.start()
        return t

    def test_another_user_s_start_lock_serves_too(self):
        open(self.driver.START_LOCK, "w").close()
        os.chmod(self.driver.START_LOCK, 0o444)
        self.driver._started = self.driver._reaper = self.driver._remove_vms = lambda *a: None
        self.driver._run(["true"], self.unit, dict(os.environ), os.path.join(self.unit, "nop.log"), "the run", stagger=True)

    def test_a_slow_docker_does_not_hold_the_stop(self):
        os.environ["FAKE_DOCKER_DELAY"] = HANG
        reaped, failed = [], []
        self.driver._reaper = lambda pgid, *_: reaped.append(time.time())
        t = self.run_unit(failed)
        time.sleep(2.5)
        closed = time.time()
        self.driver.CLOSED.set()
        t.join(10)
        self.assertTrue(reaped and failed)
        self.assertLess(reaped[0] - closed, LIMIT)

    def test_a_stopped_unit_hands_the_reaper_the_volumes_it_saw(self):
        alive = os.path.join(self.tmp, "alive")
        open(alive, "w").close()
        os.makedirs(self.note)
        env = mock.patch.dict(os.environ, FAKE_ALIVE=alive, NULLIUS_UPSTREAM=self.tmp)
        env.start()
        self.addCleanup(env.stop)
        failed = []
        t = self.run_unit(failed)
        t0 = time.time()
        while not any(ln.startswith("inspect") for ln in self.lines()) and time.time() - t0 < LIMIT:
            time.sleep(0.05)
        time.sleep(0.5)
        os.remove(alive)
        self.driver.CLOSED.set()
        t.join(10)
        self.assertTrue(failed)
        t0 = time.time()
        while f"volume rm {VOLUME}" not in self.removed() and time.time() - t0 < LIMIT:
            time.sleep(0.1)
        self.assertEqual(self.removed(), [f"volume rm {VOLUME}"])

    def reapers(self):
        found = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if b"--reap" in argv and self.unit.encode() in argv:
                found.append(int(pid))
        return found

    @staticmethod
    def ignores_sigterm(pid):
        with open(f"/proc/{pid}/status") as f:
            ignored = next(ln.split()[1] for ln in f if ln.startswith("SigIgn:"))
        return bool(int(ignored, 16) & 1 << (signal.SIGTERM - 1))

    def test_a_driver_killed_mid_run_leaves_its_reaper_the_vm_and_its_volume(self):
        os.environ["NULLIUS_UPSTREAM"] = self.tmp
        started = os.path.join(self.tmp, "started")
        self.addCleanup(lambda: [os.kill(p, signal.SIGKILL) for p in self.reapers()])
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig.name):
                for path in (started, self.calls):
                    if os.path.exists(path):
                        os.remove(path)
                driver = subprocess.Popen([sys.executable, "-c", KILLED, HERE, self.unit, started],
                                          start_new_session=True)
                self.addCleanup(lambda d=driver: (d.poll() is None and os.killpg(d.pid, signal.SIGKILL), d.wait()))
                t0 = time.time()
                while not (os.path.exists(started) and os.path.getsize(started)) and time.time() - t0 < LIMIT:
                    time.sleep(0.05)
                with open(started) as f:
                    vm = int(f.read())
                while not (self.reapers() and self.ignores_sigterm(self.reapers()[0])) and time.time() - t0 < LIMIT:
                    time.sleep(0.05)
                self.assertEqual(len(self.reapers()), 1)
                os.killpg(driver.pid, sig)
                driver.wait(LIMIT)
                for pid in self.reapers():
                    os.kill(pid, signal.SIGTERM)
                t0 = time.time()
                while f"volume rm {VOLUME}" not in self.removed() and time.time() - t0 < 3 * LIMIT:
                    time.sleep(0.1)
                self.assertEqual(self.removed()[:2], ["rm -f -v cid1", f"volume rm {VOLUME}"])
                with self.assertRaises(ProcessLookupError):
                    os.killpg(vm, 0)
                for pid in self.reapers():
                    os.kill(pid, signal.SIGKILL)

    def test_a_reaper_that_cannot_start_ends_the_script_and_its_vm_at_once(self):
        reaped = []
        with mock.patch.object(self.driver, "_arm", mock.Mock(side_effect=OSError(11, "Resource temporarily unavailable"))), \
                mock.patch.object(self.driver, "reap", lambda *a: reaped.append(a)), \
                self.assertRaises(self.driver.EnvironmentSetupError):
            self.driver._run(["sh", "-c", "sleep 30"], self.unit, dict(os.environ), os.path.join(self.unit, "nop.log"),
                             "the run", stagger=True)
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0][1:], (self.unit, True))
        os.killpg(reaped[0][0], signal.SIGKILL)

    def test_a_run_cut_short_by_an_error_still_lets_its_reaper_go(self):
        released = []
        self.driver._reaper = lambda pgid, *a: (released.append(pgid), self.driver._ARMED.pop(pgid))
        self.driver._arm = lambda pgid, *a: self.driver._ARMED.__setitem__(pgid, -1)
        broken = types.SimpleNamespace(Thread=mock.Mock(side_effect=RuntimeError("can't start new thread")),
                                       Event=threading.Event)
        with mock.patch.object(self.driver, "threading", broken), self.assertRaises(RuntimeError):
            self.driver._run(["sh", "-c", f"echo '{self.driver.STARTED}'; sleep 30"], self.unit, dict(os.environ),
                             os.path.join(self.unit, "nop.log"), "the run", stagger=True)
        self.assertEqual(len(released), 1)
        self.assertEqual(self.driver._ARMED, {})
        os.killpg(released[0], signal.SIGKILL)

    def test_a_retried_setup_leaves_neither_vm_nor_either_disk(self):
        second, go = os.path.join(self.tmp, "second"), os.path.join(self.tmp, "go")
        env = mock.patch.dict(os.environ, FAKE_SECOND=second, FAKE_OTHER=OTHER)
        env.start()
        self.addCleanup(env.stop)
        self.driver.NOTE_SECONDS = 0.1
        self.driver._reaper = lambda *a: None
        failed = []

        def noted(name):
            t0 = time.time()
            while time.time() - t0 < LIMIT:
                try:
                    with open(self.note) as f:
                        if name in f.read().split():
                            return True
                except OSError:
                    pass
                time.sleep(0.05)
            return False

        t = self.run_unit(failed, f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.assertTrue(noted(VOLUME))
        open(second, "w").close()
        self.assertTrue(noted(OTHER))
        open(go, "w").close()
        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(failed, [])
        self.assertEqual(self.removed(), ["rm -f -v cid2", f"volume rm {VOLUME} {OTHER}"])

    def test_the_volumes_seen_are_noted(self):
        self.assertEqual(self.remove(), ["rm -f -v cid1", f"volume rm {VOLUME}"])
        with open(self.note) as f:
            self.assertEqual(f.read().split(), [VOLUME])

    def test_a_name_in_the_note_is_removed(self):
        with open(self.note, "w") as f:
            f.write(f"someones-volume\n{OTHER}\n{OTHER}\n../x\n{VOLUME}x\n")
        self.assertEqual(self.remove(), ["rm -f -v cid1", f"volume rm {OTHER} {VOLUME}"])

    def test_the_note_is_read_to_its_limit(self):
        with open(self.note, "w") as f:
            f.write(" " * self.driver.NOTE_BYTES + OTHER + "\n")
        self.assertEqual(self.remove(), ["rm -f -v cid1", f"volume rm {VOLUME}"])

    def test_a_note_not_the_units_own_is_not_evidence(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        with open(elsewhere, "w") as f:
            f.write(OTHER + "\n")
        for make in (lambda: os.symlink(elsewhere, self.note), lambda: os.link(elsewhere, self.note),
                     lambda: os.makedirs(self.note), lambda: os.mkfifo(self.note)):
            with self.subTest(make=make):
                make()
                self.assertEqual(self.remove()[-2:], ["rm -f -v cid1", f"volume rm {VOLUME}"])
                if os.path.isdir(self.note) and not os.path.islink(self.note):
                    os.rmdir(self.note)
                else:
                    os.remove(self.note)
        with open(elsewhere) as f:
            self.assertEqual(f.read(), OTHER + "\n")

    def test_a_full_disk_still_removes(self):
        write = os.write

        def full(fd, data):
            if os.readlink(f"/proc/self/fd/{fd}") == self.note:
                raise OSError(errno.ENOSPC, "No space left on device")
            return write(fd, data)

        with mock.patch.object(self.driver.os, "write", full):
            self.assertEqual(self.remove(), ["rm -f -v cid1", f"volume rm {VOLUME}"])

    def test_no_vm_folder_still_removes(self):
        os.rmdir(os.path.join(self.unit, "vm"))
        self.assertEqual(self.remove(), ["rm -f -v cid1", f"volume rm {VOLUME}"])

    def child(self, *alive):
        """A process group of its own whose SIGTERM is timed, and can remove a container (FAKE_ALIVE)."""
        signalled, ready = os.path.join(self.tmp, "signalled"), os.path.join(self.tmp, "ready")
        for path in (signalled, ready):
            if os.path.exists(path):
                os.remove(path)
        p = subprocess.Popen([sys.executable, "-c", CHILD, signalled, ready, *alive], start_new_session=True)
        threading.Thread(target=p.wait, daemon=True).start()
        self.addCleanup(lambda: (p.poll() is None and os.killpg(p.pid, signal.SIGKILL), p.wait()))
        t0 = time.time()
        while not os.path.exists(ready) and time.time() - t0 < 10:
            time.sleep(0.05)
        return p, signalled

    def test_a_hung_docker_does_not_hold_the_reapers_signal(self):
        os.environ["FAKE_DOCKER_DELAY"] = HANG
        p, signalled = self.child()
        self.driver.SNAPSHOT_SECONDS = 1
        self.driver.SWEEPS = ()
        t0 = time.time()
        self.driver.reap(p.pid, self.unit, True)
        p.wait(10)
        with open(signalled) as f:
            self.assertLess(float(f.read()) - t0, LIMIT)

    def test_a_volume_seen_before_the_signal_is_removed_when_the_note_cannot_keep_it(self):
        write = os.write

        def full(fd, data):
            if os.readlink(f"/proc/self/fd/{fd}") == self.note:
                raise OSError(errno.ENOSPC, "No space left on device")
            return write(fd, data)

        alive = os.path.join(self.tmp, "alive")
        os.environ["FAKE_ALIVE"] = alive
        self.driver.SWEEPS = (0,)
        for name, keep in (("refused", lambda: os.makedirs(self.note)), ("full", lambda: mock.patch.object(self.driver.os, "write", full))):
            with self.subTest(note=name):
                open(alive, "w").close()
                open(self.calls, "w").close()
                patch = keep()
                if patch is not None:
                    patch.start()
                p, _ = self.child(alive)
                try:
                    self.driver.reap(p.pid, self.unit, True)
                finally:
                    if patch is not None:
                        patch.stop()
                p.wait(10)
                self.assertFalse(os.path.exists(alive))
                self.assertEqual(self.removed(), [f"volume rm {VOLUME}"])
                if os.path.isdir(self.note):
                    os.rmdir(self.note)

    def test_the_reaper_notes_before_its_signal(self):
        seen = []

        def killpg(pgid, sig):
            with open(self.note) as f:
                seen.append(f.read())
            raise OSError(errno.ESRCH, "No such process")

        with mock.patch.object(self.driver.os, "killpg", killpg), mock.patch.object(self.driver, "SWEEPS", ()):
            self.driver.reap(1, self.unit, True)
        self.assertEqual(seen, [VOLUME + "\n"])


if __name__ == "__main__":
    unittest.main()
