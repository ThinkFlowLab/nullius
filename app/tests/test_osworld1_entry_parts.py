"""OSWorld1's agent side in parts, with the standard library alone: what the driver's score takes from a runner and how
it reads it, how it starts and reaps one, the bounds that follow from the unit's limits, and what the runner does
around the upstream's loop. Child processes of the test and files in temporary folders only; no upstream, no VM, no
docker."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import select
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

from nullius.standard import computer_port

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nullius", "catalogue", "benchmarks",
                     "osworld1")


def load(name: str, upstream: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ENTRY, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"NULLIUS_UPSTREAM": upstream}), mock.patch.object(sys, "argv", [name + ".py"]):
        spec.loader.exec_module(module)
    return module


def child(code: str = "import time; time.sleep(60)", **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdin=subprocess.DEVNULL, **kw)


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


def quiet(d) -> None:
    """The driver's removal of a unit's VMs stubbed: no docker command, no wait for a late create."""
    d._remove_vms = lambda where, seconds=None: None
    d._remove_passes = lambda where, seconds, again=True: None


def line_of(p: subprocess.Popen, seconds: float = 10) -> bytes:
    """The child's next line on its output, waited for ``seconds`` at most."""
    got = b""
    deadline = time.monotonic() + seconds
    while not got.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([p.stdout], [], [], left)[0]:
            raise AssertionError("the child said nothing in time")
        chunk = os.read(p.stdout.fileno(), 1)
        if not chunk:
            raise AssertionError("the child ended before its line")
        got += chunk
    return got


class Score(unittest.TestCase):
    """score tells the runner the agent's turn has ended, and takes a result only from a runner this driver started,
    that ended by itself with status 0, through folders opened without following a link, from plain files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = load("driver", self.tmp)
        quiet(self.d)
        self.ws = os.path.join(self.tmp, "trial", "workspace")
        os.makedirs(self.ws)
        self.where = self.d._runner_dir(self.ws)
        self.task = os.path.join(self.where, "results", "os", "t1")
        os.makedirs(self.task)
        self.d._task = lambda unit: ("os", "t1", {"instruction": "x"})

    def result(self, value="1.0\n"):
        with open(os.path.join(self.task, "result.txt"), "w") as f:
            f.write(value)

    def owned(self, p: subprocess.Popen, life=None) -> None:
        self.d.RUNNERS[self.where] = {"p": p, "sock_dir": tempfile.mkdtemp(dir=self.tmp), "life": life}
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))

    def test_a_runner_that_ended_with_0_gives_its_result_and_its_call_log(self):
        self.result()
        with open(os.path.join(self.where, "computer-calls.jsonl"), "w") as f:
            f.write('{"tool": "click"}\n{"tool": "done"}\n')
        self.owned(child("pass"))
        got = self.d.score("os__t1", self.ws, answer="done")
        self.assertEqual(got, {"reward": 1.0, "detail": {"result": 1.0, "tool_calls": 2,
                                                         "by": "runner.py, run_single_example, DesktopEnv.evaluate"}})
        self.assertNotIn(self.where, self.d.RUNNERS)
        out = os.path.join(self.tmp, "trial", "verifier", "osworld")
        self.assertEqual(sorted(os.listdir(out)), ["computer-calls.jsonl", "result.txt"])

    def test_score_tells_the_runner_the_turn_has_ended(self):
        self.result()
        read, write = os.pipe()
        waits = ("import os, sys\nfd, data = int(sys.argv[1]), b''\n"
                 "while b'turn-end\\n' not in data:\n    chunk = os.read(fd, 64)\n    if not chunk: sys.exit(5)\n"
                 "    data += chunk\n")
        p = subprocess.Popen([sys.executable, "-c", waits, str(read)], pass_fds=(read,), start_new_session=True,
                             stdin=subprocess.DEVNULL)
        os.close(read)
        self.owned(p, life=write)
        self.d.SCORE_SECONDS, self.d.REAP_SECONDS = 10, 0.5     # a runner never told would be let go at 10 s
        self.assertEqual(self.d.score("os__t1", self.ws, answer="")["reward"], 1.0)

    def test_a_planted_runner_record_is_no_runner_of_this_drivers(self):
        self.result()
        with open(os.path.join(self.where, "runner.json"), "w") as f:
            json.dump({"pid": os.getpid()}, f)
        with self.assertRaisesRegex(RuntimeError, "no runner of this driver's for this unit"):
            self.d.score("os__t1", self.ws, answer="done")

    def test_a_linked_result_is_not_read(self):
        forged = os.path.join(self.ws, "forged.txt")
        with open(forged, "w") as f:
            f.write("1.0\n")
        os.symlink(forged, os.path.join(self.task, "result.txt"))
        self.owned(child("pass"))
        with self.assertRaisesRegex(RuntimeError, "the runner's result could not be read"):
            self.d.score("os__t1", self.ws, answer="done")

    def test_a_linked_folder_anywhere_on_the_way_is_not_followed(self):
        for linked in (("results",), ("results", "os"), ()):           # () : the trial's own .work folder
            with self.subTest(linked=linked):
                shutil.rmtree(os.path.join(self.tmp, "trial", ".work"), ignore_errors=True)
                elsewhere = os.path.join(self.tmp, "elsewhere")
                shutil.rmtree(elsewhere, ignore_errors=True)
                real = os.path.join(elsewhere, ".work", "osworld", "results", "os", "t1")
                os.makedirs(real)
                with open(os.path.join(real, "result.txt"), "w") as f:
                    f.write("1.0\n")
                if linked:
                    os.makedirs(os.path.join(self.where, *linked[:-1]), exist_ok=True)
                    os.symlink(os.path.join(elsewhere, ".work", "osworld", *linked), os.path.join(self.where, *linked))
                else:
                    os.symlink(os.path.join(elsewhere, ".work"), os.path.join(self.tmp, "trial", ".work"))
                self.owned(child("pass"))
                with self.assertRaisesRegex(RuntimeError, "the runner's result could not be read"):
                    self.d.score("os__t1", self.ws, answer="done")

    def test_a_folder_swapped_for_a_link_after_it_was_opened_is_not_followed(self):
        self.result()
        results = os.path.join(self.where, "results")
        real_open, swapped = os.open, []

        def opening(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "results" and not swapped:
                os.rename(results, results + ".moved")
                forged = os.path.join(self.tmp, "forged", "os", "t1")
                os.makedirs(forged)
                with open(os.path.join(forged, "result.txt"), "w") as f:
                    f.write("0.5\n")
                os.symlink(os.path.join(self.tmp, "forged"), results)
                swapped.append(True)
            return fd

        with mock.patch.object(self.d.os, "open", opening):
            got = self.d._plain(os.path.join(self.tmp, "trial"), ".work", "osworld", "results", "os", "t1", "result.txt")
        self.assertEqual((swapped, got), ([True], b"1.0\n"))

    def test_a_fifo_in_place_of_the_result_is_refused_at_once(self):
        os.mkfifo(os.path.join(self.task, "result.txt"))
        self.owned(child("pass"))
        t0 = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "is not a plain file"):
            self.d.score("os__t1", self.ws, answer="done")
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_killed_runner_gives_no_score_whatever_it_left(self):
        self.result()
        p = child()
        p.kill()
        self.owned(p)
        with self.assertRaisesRegex(RuntimeError, "the runner was killed: its result is not read"):
            self.d.score("os__t1", self.ws, answer="done")

    def test_a_runner_that_exited_1_gives_no_score(self):
        self.result()
        self.owned(child("raise SystemExit(1)"))
        with self.assertRaisesRegex(RuntimeError, "the runner exited 1"):
            self.d.score("os__t1", self.ws, answer="done")

    def test_a_runner_that_ends_well_while_score_lets_it_go_is_scored_from_its_files(self):
        self.result()
        self.owned(child("import time; time.sleep(1.5)"))
        self.d.SCORE_SECONDS, self.d.REAP_SECONDS = 0.3, 15
        self.assertEqual(self.d.score("os__t1", self.ws, answer="done")["reward"], 1.0)

    def test_an_answer_with_no_runner_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "no runner was set up for this unit"):
            self.d.score("os__t1", self.ws, answer="done")


class StartAndReap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = load("driver", self.tmp)
        quiet(self.d)

    def test_the_start_lock_is_shared_by_every_run_on_the_machine(self):
        folder = os.path.join(self.tmp, "run-tmp")
        os.mkdir(folder)
        code = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('driver', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\nsys.argv = ['driver.py']\n"
                "spec.loader.exec_module(module)\nprint(module.START_LOCK)\n")
        got = subprocess.run([sys.executable, "-c", code, os.path.join(ENTRY, "driver.py")], capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL,
                             env=dict(os.environ, TMPDIR=folder, NULLIUS_UPSTREAM=self.tmp))
        self.assertEqual(got.stdout.strip(), "/tmp/nullius-osworld1-vm-start.lck", got.stderr[-2000:])

    def test_an_already_reaped_runner_gets_no_signal(self):
        p = child("pass")
        p.wait()
        self.d.RUNNERS["w"] = {"p": p, "sock_dir": tempfile.mkdtemp(dir=self.tmp), "life": None}
        with mock.patch.object(self.d.os, "killpg") as kill:
            self.d._reap_runner("w")
        kill.assert_not_called()
        self.assertNotIn("w", self.d.RUNNERS)

    def test_a_runner_reaped_elsewhere_gets_no_signal(self):
        p = child("pass")
        os.waitpid(p.pid, 0)                                    # as a parent ignoring its children's signals would
        self.d.RUNNERS["w"] = {"p": p, "sock_dir": tempfile.mkdtemp(dir=self.tmp), "life": None}
        with mock.patch.object(self.d.os, "killpg") as kill:
            self.d._reap_runner("w", seconds=0)
        kill.assert_not_called()

    def test_a_running_runner_is_signalled_while_its_leader_is_unreaped(self):
        p = child()
        seen, real = [], os.killpg

        def killpg(pgid, sig):
            seen.append((pgid, p.returncode))
            real(pgid, sig)

        self.d.RUNNERS["w"] = {"p": p, "sock_dir": tempfile.mkdtemp(dir=self.tmp), "life": None}
        with mock.patch.object(self.d.os, "killpg", killpg):
            self.d._reap_runner("w", seconds=0.2)
        self.assertEqual(seen, [(p.pid, None)])
        self.assertIsNotNone(p.returncode)

    def test_a_runner_that_does_not_start_leaves_no_pipe_end_and_no_folder(self):
        self.d._task = lambda unit: ("os", "a", {"instruction": "x"})
        self.d._workspace = lambda where, vm_root=None: "vm"
        self.d.START_LOCK = os.path.join(self.tmp, "start.lock")
        sock = os.path.join(self.tmp, "socket")
        os.mkdir(sock)
        made, real = [], os.pipe

        def pipe():
            made.extend(real())
            return made[-2], made[-1]

        with mock.patch.object(self.d.tempfile, "mkdtemp", return_value=sock), \
                mock.patch.object(self.d.os, "pipe", pipe), \
                mock.patch.object(self.d.subprocess, "Popen", side_effect=OSError("cannot start")):
            with self.assertRaisesRegex(OSError, "cannot start"):
                self.d._start_runner("os__a", os.path.join(self.tmp, "job", "trial", "workspace"), "x", time.time() + 30)
        still = []
        for fd in made:
            try:
                os.fstat(fd)
                still.append(fd)
            except OSError:
                pass
        self.assertEqual((os.path.exists(sock), still, self.d.RUNNERS), (False, [], {}))


class Bounds(unittest.TestCase):
    """The unit's budget and the bounds that follow from it (the driver's module text): each derived from the unit's
    own limits, the clean-up inside the reap, the reap inside the core's bounds. No test sleeps a fixed time to see a
    bound."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = load("driver", self.tmp)
        self.r = load("runner", self.tmp)

    def test_the_clean_up_fits_inside_the_reap_and_the_reap_inside_the_cores_bounds(self):
        d, r = self.d, self.r
        self.assertEqual(r.STOP_SECONDS, r.FINISH_SECONDS + r.CLOSE_SECONDS)
        self.assertEqual(d.RUNNER.STOP_SECONDS, r.STOP_SECONDS)
        self.assertLess(r.STOP_SECONDS, d.REAP_SECONDS, "the driver kills a runner still in its own clean-up")
        reap = d.REAP_SECONDS + d.REMOVE_SECONDS + d.ANSWER_SECONDS
        self.assertLessEqual(d.SCORE_SECONDS + reap, d.CORE_DRIVER_SECONDS, "score's wait and its reap overrun the core's")
        self.assertLessEqual(reap, d.CORE_END_SECONDS, "end's reap overruns the core's")

    def test_setups_deadline_is_the_definitions_limit_less_the_reap(self):
        with open(os.path.join(ENTRY, "standard.json")) as f:
            limit = json.load(f)["limits"]["setup_seconds"]
        self.assertEqual(self.d.SETUP_SECONDS, limit)
        seen = []
        self.d._task = lambda unit: ("os", "a", {"instruction": "x"})
        self.d._start_runner = lambda unit, dir, instruction, until: seen.append(until - time.time())
        self.d.setup("os__a", os.path.join(self.tmp, "trial", "workspace"), participant="agent")
        reap = self.d.REAP_SECONDS + self.d.REMOVE_SECONDS + self.d.ANSWER_SECONDS
        self.assertAlmostEqual(seen[0], limit - reap, delta=5)

    # -- the gate ---------------------------------------------------------------------------------------------------
    def stand_in_runner(self):
        """A runner of the test's in place of runner.py: it logs the provider's start line once the test lets it
        (the file go beside it), says ready, and waits on its lifeline."""
        path = os.path.join(self.tmp, "runner.py")
        self.go = os.path.join(self.tmp, "go")
        with open(path, "w") as f:
            f.write("import json, os, sys, time\n"
                    "a = sys.argv[1:]\nget = lambda k: a[a.index(k) + 1]\n"
                    f"json.dump(a, open({os.path.join(self.tmp, 'begun')!r}, 'w'))\n"
                    f"while not os.path.exists({self.go!r}):\n    time.sleep(0.02)\n"
                    "print('INFO: Started container with ports - VNC: 1', flush=True)\n"
                    "json.dump({'ok': True, 'password': 'pw'}, open(get('--ready') + '.part', 'w'))\n"
                    "os.replace(get('--ready') + '.part', get('--ready'))\n"
                    "os.read(int(get('--lifeline')), 1)\n")
        self.d.RUNNER_FILE = path
        self.d._task = lambda unit: ("os", "a", {"instruction": "x"})
        self.d._workspace = lambda where, vm_root=None: "vm"
        quiet(self.d)
        self.d.START_LOCK = os.path.join(self.tmp, "start.lock")
        self.addCleanup(lambda: [self.d._reap_runner(w, seconds=0) for w in list(self.d.RUNNERS)])

    def test_the_step_option_reaches_the_runner_with_the_port_default(self):
        from nullius.standard import definitions
        self.stand_in_runner()
        open(self.go, "w").close()
        ws = os.path.join(self.tmp, "job", "trial", "workspace")
        bench = definitions.get("benchmark", "osworld1")
        for given, expected in (({}, "100"), ({"max_steps": "default"}, "100"),
                                ({"max_steps": "1"}, "1"), ({"max_steps": "1500"}, "1500"),
                                ({"max_steps": "9999"}, "9999")):
            with self.subTest(given=given):
                values, bad = definitions.bench_options_of(bench, "test_small", given, kind="agent")
                self.assertEqual(bad, [])
                with mock.patch.dict(os.environ, {"NULLIUS_OPTION_MAX_STEPS": values["max_steps"]}):
                    self.d._start_runner("os__a", ws, "x", time.time() + 30)
                with open(os.path.join(self.tmp, "begun")) as f:
                    argv = json.load(f)
                self.assertEqual(argv[argv.index("--steps") + 1], expected)
                self.d._reap_runner(self.d._runner_dir(ws))

    def test_a_late_reaper_removes_only_its_setup_at_the_same_trial_path(self):
        workspace, remove, passes = self.d._workspace, self.d._remove_vms, self.d._remove_passes
        self.stand_in_runner()
        self.d._workspace, self.d._remove_vms, self.d._remove_passes = workspace, remove, passes
        open(self.go, "w").close()
        image = os.path.join(self.tmp, "Ubuntu.qcow2")
        open(image, "w").close()
        self.d.vm_image = lambda: image
        ws = os.path.join(self.tmp, "job", "trial", "workspace")
        where = self.d._runner_dir(ws)
        self.d._start_runner("os__a", ws, "x", time.time() + 30)
        first = self.d.RUNNERS[where]
        with mock.patch.object(self.d, "_spawn_reaper") as spawn:
            self.d._hand_over(where)
        first["p"].wait(timeout=10)
        self.d._start_runner("os__a", ws, "x", time.time() + 30)
        second = self.d.RUNNERS[where]
        with open(os.path.join(self.tmp, "begun")) as f:
            argv = json.load(f)
        current_vm = argv[argv.index("--vm") + 1]
        self.assertTrue(current_vm.startswith(second["sock_dir"] + os.sep))
        self.assertNotEqual(first["sock_dir"], second["sock_dir"])
        mounts = {key: os.path.join(entry["sock_dir"], "vm", "Ubuntu.qcow2")
                  for key, entry in (("first", first), ("second", second))}
        removed = []

        def docker(argv, **kw):
            if argv[1] == "ps":
                out = " ".join(mounts)
            elif argv[1] == "inspect":
                out = mounts[argv[-1]]
            else:
                self.assertEqual(argv[1:3], ["rm", "-f"])
                for key in argv[3:]:
                    removed.append(key)
                    mounts.pop(key)
                out = ""
            return subprocess.CompletedProcess(argv, 0, stdout=out)

        with mock.patch.object(self.d.shutil, "which", return_value="docker"), \
                mock.patch.object(self.d.subprocess, "run", side_effect=docker), \
                mock.patch.object(self.d.time, "sleep"):
            self.d.reap_runner(*spawn.call_args.args)
        self.assertEqual(removed, ["first"])
        self.assertEqual(mounts, {"second": current_vm})
        self.assertFalse(os.path.exists(first["sock_dir"]))
        self.assertTrue(os.path.exists(current_vm))
        self.assertIsNone(second["p"].poll())
        self.assertIs(self.d.RUNNERS[where], second)

    def gate_free(self):
        with open(self.d.START_LOCK, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    def test_the_start_gate_is_held_until_the_vm_start_has_ended(self):
        self.stand_in_runner()
        ws = os.path.join(self.tmp, "job", "trial", "workspace")
        got = []
        worker = threading.Thread(target=lambda: got.append(self.d._start_runner("os__a", ws, "x", time.time() + 30)),
                                  daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while not os.path.exists(os.path.join(self.tmp, "begun")) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "begun")), "the runner did not start")
        self.assertFalse(self.gate_free(), "the gate was let go before the VM's start ended")
        open(self.go, "w").close()                    # the VM's start ends
        worker.join(15)
        self.assertTrue(got and got[0]["prompt"].startswith("x"))
        self.assertTrue(self.gate_free())

    def test_a_start_line_of_an_earlier_launch_does_not_free_the_gate(self):
        self.stand_in_runner()
        ws = os.path.join(self.tmp, "job", "trial", "workspace")
        where = self.d._runner_dir(ws)
        os.makedirs(where)
        with open(os.path.join(where, "runner.log"), "w") as f:
            f.write("INFO: " + self.d.STARTED + " - VNC: 1\n")      # an earlier launch's
        got = []
        worker = threading.Thread(target=lambda: got.append(self.d._start_runner("os__a", ws, "x", time.time() + 30)),
                                  daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while not os.path.exists(os.path.join(self.tmp, "begun")) and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)                               # the driver's wait has read the log more than once
        self.assertFalse(self.gate_free(), "an earlier launch's start line let the gate go")
        open(self.go, "w").close()
        worker.join(15)
        self.assertTrue(got and self.gate_free())

    def test_a_unit_waiting_for_the_gate_fails_at_its_setups_deadline(self):
        self.stand_in_runner()
        held = open(self.d.START_LOCK, "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "was not free before the end of this unit's setup"):
            self.d._start_runner("os__a", os.path.join(self.tmp, "job", "trial", "workspace"), "x", time.time() + 0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.d.RUNNERS, {})

    # -- the reap ---------------------------------------------------------------------------------------------------
    def runner_with_child(self):
        """A runner of the test's that never ends by itself, and a child of it in its process group."""
        where = os.path.join(self.tmp, "trial", ".work", "osworld")
        os.makedirs(where)
        p = child("import subprocess, sys, time\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "print(c.pid, flush=True)\ntime.sleep(60)\n", stdout=subprocess.PIPE)
        kid = int(line_of(p))
        self.addCleanup(lambda: [alive(pid) and os.kill(pid, 9) for pid in (kid, p.pid)])
        self.addCleanup(p.stdout.close)
        self.d.RUNNERS[where] = {"p": p, "sock_dir": tempfile.mkdtemp(dir=self.tmp), "life": None,
                                 "start": self.d._start_time(p.pid)}
        return where, p, kid

    def test_the_reap_ends_the_runners_whole_group(self):
        quiet(self.d)
        where, p, kid = self.runner_with_child()
        self.d._reap_runner(where, seconds=0.2)
        self.assertTrue(gone(kid), "a process of the runner's group outlived its reap")
        self.assertIsNotNone(p.returncode)

    def test_a_reap_cut_by_the_drivers_end_is_handed_over(self):
        where, p, kid = self.runner_with_child()
        handed = []
        self.d._hand_over = lambda w: (handed.append(w), self.d.RUNNERS.pop(w, None))
        self.d.CLOSED.set()                           # the driver's input ends while the reap waits
        t0 = time.monotonic()
        self.assertIsNone(self.d._reap_runner(where, seconds=30))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((handed, self.d.RUNNERS), ([where], {}))

    def test_a_runner_handed_over_is_reaped_after_the_driver_was_killed(self):
        # The driver in a child of the test's: it starts a runner with a child of its own, hands it over to a reaper,
        # says so and waits; the test kills the driver's whole group while the reaper's wait is pending.
        where = os.path.join(self.tmp, "trial", ".work", "osworld")
        os.makedirs(where)
        empty = os.path.join(self.tmp, "no-programs")
        os.mkdir(empty)
        code = ("import importlib.util, os, subprocess, sys, tempfile, time\n"
                "sys.argv = ['driver.py']\n"
                "spec = importlib.util.spec_from_file_location('driver', os.environ['DRIVER'])\n"
                "d = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(d)\n"
                "d.REAP_SECONDS = 0.5\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time\\n"
                "c = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"])\\n"
                "print(c.pid, flush=True)\\ntime.sleep(60)\\n'], stdout=subprocess.PIPE, start_new_session=True)\n"
                "kid = int(p.stdout.readline())\n"
                "d.RUNNERS[os.environ['WHERE']] = {'p': p, 'sock_dir': tempfile.mkdtemp(), 'life': None,"
                " 'start': d._start_time(p.pid)}\n"
                "d._hand_over(os.environ['WHERE'])\n"
                "print(p.pid, kid, flush=True)\n"
                "time.sleep(60)\n")
        driver = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  start_new_session=True,
                                  env=dict(os.environ, DRIVER=os.path.join(ENTRY, "driver.py"), WHERE=where,
                                           NULLIUS_UPSTREAM=self.tmp, PATH=empty))
        self.addCleanup(lambda: (driver.poll() is None and os.killpg(driver.pid, 9), driver.wait(), driver.stdout.close()))
        runner, kid = map(int, line_of(driver, 20).split())
        self.addCleanup(lambda: [alive(pid) and os.kill(pid, 9) for pid in (runner, kid)])
        os.killpg(driver.pid, signal.SIGKILL)         # the driver and its group, the reaper's wait still pending
        driver.wait(timeout=10)
        self.assertTrue(gone(runner, 15), "the runner outlived its driver's death")
        self.assertTrue(gone(kid, 15), "the runner's group outlived its driver's death")

    def owned_runner(self, code="pass"):
        where = os.path.join(self.tmp, "trial", ".work", "osworld")
        os.makedirs(where, exist_ok=True)
        p = child(code)
        if code == "pass":
            p.wait()
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        sock = tempfile.mkdtemp(dir=self.tmp)
        self.d.RUNNERS[where] = {"p": p, "sock_dir": sock, "life": None, "start": self.d._start_time(p.pid)}
        return where, p, sock

    def test_unit_end_reports_a_vm_removal_timeout_and_keeps_it_for_the_reaper(self):
        where, p, sock = self.owned_runner()
        ws = os.path.join(self.tmp, "trial", "workspace")
        with mock.patch.object(self.d.shutil, "which", return_value="docker"), \
                mock.patch.object(self.d.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 0.1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.d.end("os__a", ws)
        self.assertIn(where, self.d.RUNNERS)
        self.assertTrue(os.path.isdir(sock))
        with mock.patch.object(self.d, "_spawn_reaper") as reap:
            self.d._reap_at_exit()
        self.assertEqual(reap.call_args.args[2:4], (where, sock))
        self.assertEqual(self.d.RUNNERS, {})

    def test_unit_end_reports_a_failed_vm_removal_command(self):
        where, p, sock = self.owned_runner()
        run = subprocess.run

        def docker(argv, **kw):
            if argv[1] == "ps":
                return subprocess.CompletedProcess(argv, 0, stdout="container\n")
            if argv[1] == "inspect":
                return subprocess.CompletedProcess(argv, 0, stdout=os.path.join(sock, "vm", "Ubuntu.qcow2"))
            self.assertEqual(argv[1:3], ["rm", "-f"])
            return run([sys.executable, "-c", "raise SystemExit(1)"], **kw)

        with mock.patch.object(self.d.shutil, "which", return_value="docker"), \
                mock.patch.object(self.d.subprocess, "run", side_effect=docker):
            with self.assertRaises(subprocess.CalledProcessError):
                self.d.end("os__a", os.path.join(self.tmp, "trial", "workspace"))
        self.assertIn(where, self.d.RUNNERS)

    def test_a_removal_the_drivers_end_cuts_is_handed_to_a_reaper(self):
        where, p, sock = self.owned_runner()
        release = threading.Event()
        self.addCleanup(release.set)

        def removing(dir, seconds=None):              # the core closes the driver's input while docker works
            self.d.CLOSED.set()
            release.wait(10)

        self.d._remove_vms = removing
        t0 = time.monotonic()
        with mock.patch.object(self.d.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(self.d.subprocess, "Popen") as spawn:
            self.d._reap_runner(where)
        self.assertLess(time.monotonic() - t0, 3, "the driver waited on the removal after its input ended")
        argv = spawn.call_args[0][0]
        self.assertEqual(argv[2:5], ["--reap-runner", "0", "None"])
        self.assertEqual((argv[5:7], self.d.RUNNERS), ([where, sock], {}))

    def test_the_units_containers_are_looked_for_again_after_a_kill_and_by_the_reaper(self):
        removals = []
        self.d._remove_vms = lambda dir, seconds=None: removals.append(dir)
        with mock.patch.object(self.d.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(self.d.time, "sleep"):
            where, p, sock = self.owned_runner()          # ended by itself: one look
            self.d._reap_runner(where)
            self.assertEqual(len(removals), 1)
            where, p, sock = self.owned_runner("import time; time.sleep(60)")     # killed: looked for again
            self.d._reap_runner(where, seconds=0.1)
            self.assertGreaterEqual(len(removals), 1 + 2)
            removals.clear()
            self.d.reap_runner(123456789, None, where, os.path.join(self.tmp, "gone"), 0)
            self.assertGreaterEqual(len(removals), 2, "the reaper looked for the unit's containers once")

    def test_a_reaper_that_cannot_start_leaves_the_runner_reaped_here(self):
        where, p, sock = self.owned_runner("import time; time.sleep(60)")
        self.d._remove_vms = lambda dir, seconds=None: None
        with mock.patch.object(self.d.subprocess, "Popen", side_effect=OSError("no process can start")):
            self.d._hand_over(where)
        self.assertEqual(self.d.RUNNERS, {})
        self.assertTrue(gone(p.pid, 5), "the runner outlived a reaper that could not start")
        self.assertFalse(os.path.exists(sock))

    def test_the_vms_removal_has_its_bound(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write("#!/bin/sh\nexec sleep 30\n")                # a docker command of the test's that never answers
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.d._remove_vms(self.tmp, 0.5)
        self.assertLess(time.monotonic() - t0, 5)


class Texts(unittest.TestCase):
    """What the definition says of the two ways a task is run, and of each one's time."""

    def test_the_definition_describes_both_ways_a_task_runs_and_each_ones_time(self):
        with open(os.path.join(ENTRY, "standard.json")) as f:
            summary = json.load(f)["summary"]
        self.assertIn("OSWorld's own agents", summary)
        self.assertIn("computer port", summary)
        with open(os.path.join(ENTRY, "benchmark.json")) as f:
            limits = json.load(f)["limitations"]
        line = next(x for x in limits if x.startswith("A task run by one of OSWorld's own agents"))
        self.assertIn("the task's setup may take 1 hour, the agent's turn 2 hours and the scoring 10 minutes", line)
        self.assertNotIn("A task still running after 2 hours is stopped and gets no score.", limits)
        page = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(ENTRY))), "..", "..", "docs", "benchmarks",
                            "supported", "osworld1.md")
        if os.path.exists(page):
            with open(page) as f:
                self.assertIn(line, f.read())


class Snapshot(unittest.TestCase):
    """The copy Nullius makes of the entry for a run on a host carries the runner, and the port's shared code with
    the package."""

    def test_the_entrys_copy_holds_the_runner_and_the_package_the_ports_code(self):
        from pathlib import Path
        from nullius.standard import payload
        files = set(payload._entry(Path(ENTRY)))
        self.assertTrue({"driver.py", "agents.json", "nop.py", "runner.py"} <= files, sorted(files))
        package = set(payload._python(payload.PACKAGE))
        self.assertTrue({"standard/computer_port.py", "standard/computer_pipe.py"} <= package)


class FakeEnv:
    """The upstream's DesktopEnv as the runner's Desktop uses it: its controllers, recorded."""

    def __init__(self, close_seconds=0.0):
        self.seen, self.files, self.close_seconds = [], {}, close_seconds
        self.client_password = "password"
        self.controller = types.SimpleNamespace(
            execute_python_command=lambda code: self.seen.append(("python", code)) or {"status": "success"},
            get_file=lambda path: self.files.get(path),
            get_screenshot=lambda: b"png")
        self.setup_controller = types.SimpleNamespace(_upload_file_setup=self.upload)

    def upload(self, files):
        for f in files:
            with open(f["local_path"], "rb") as src:
                self.files[f["path"]] = src.read()
            self.seen.append(("upload", f["path"], f["local_path"]))

    def close(self):
        time.sleep(self.close_seconds)
        self.seen.append(("close",))


class Runner(unittest.TestCase):
    """runner.py around the upstream's loop, with stand-ins for the upstream's modules in the test's process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.r = load("runner", self.tmp)

    def test_the_desktop_is_the_upstreams_controllers(self):
        env = FakeEnv()
        desktop = self.r.Desktop(env, self.tmp)
        self.assertEqual(desktop.python("print(1)"), {"status": "success"})
        desktop.upload("/home/user/a.bin", b"bytes")
        self.assertEqual((desktop.read("/home/user/a.bin"), desktop.read("/none")), (b"bytes", None))
        self.assertEqual((desktop.capture(), desktop.password()), (b"png", "password"))
        local = env.seen[1][2]
        self.assertEqual((env.seen[0], env.seen[1][:2]), (("python", "print(1)"), ("upload", "/home/user/a.bin")))
        self.assertFalse(os.path.exists(local), "the upload's local file was left")
        env.controller.get_screenshot = lambda: None
        with self.assertRaisesRegex(RuntimeError, "no screenshot"):
            desktop.capture()

    def args(self):
        folder = tempfile.mkdtemp(dir=self.tmp)
        example = os.path.join(self.tmp, "os__a.json")
        with open(example, "w") as f:
            json.dump({"id": "a", "instruction": "x"}, f)
        return types.SimpleNamespace(example=example, result_dir=os.path.join(self.tmp, "results"),
                                     socket=os.path.join(folder, "s"), ready=os.path.join(folder, "ready.json"),
                                     vm="vm", calls=os.path.join(self.tmp, "calls.jsonl"), held=None, lifeline=-1,
                                     steps=3, pause=0.0)

    def run_with(self, env, loop):
        a = self.args()
        with mock.patch.dict(sys.modules, {"lib_run_single": types.SimpleNamespace(run_single_example=loop),
                                           "desktop_env": mock.Mock(),
                                           "desktop_env.desktop_env": types.SimpleNamespace(DesktopEnv=lambda **kw: env)}):
            code = self.r.run(a, computer_port, computer_port.Stop(30), self.r.Turn())
        with open(a.ready) if os.path.exists(a.ready) else open(os.devnull) as f:
            said = f.read()
        return code, said

    def test_a_loop_that_ends_is_an_exit_0_and_the_desktop_is_closed(self):
        env = FakeEnv()

        def loop(agent, env_, example, steps, instruction, args, where, scores):
            agent.reset(None, vm_ip="x")
            agent.predict(instruction, {})              # the first step: ready; then the turn's end
            open(os.path.join(where, "result.txt"), "w").write("1.0\n")

        real = computer_port.Bridge.predict

        def predict(bridge, instruction, obs):
            bridge.end_turn()
            return real(bridge, instruction, obs)

        with mock.patch.object(computer_port.Bridge, "predict", predict):
            code, said = self.run_with(env, loop)
        self.assertEqual((code, json.loads(said)), (0, {"ok": True, "password": "password"}))
        self.assertEqual(env.seen[-1], ("close",))

    def test_a_loop_that_fails_before_the_first_step_says_not_ready_and_closes_the_desktop(self):
        env = FakeEnv()

        def loop(agent, env_, example, steps, instruction, args, where, scores):
            raise RuntimeError("the task's config failed")

        code, said = self.run_with(env, loop)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(said), {"ok": False, "error": "the task's setup: RuntimeError: the task's config failed"})
        self.assertEqual(env.seen[-1], ("close",))

    def test_a_desktop_that_does_not_close_in_its_time_is_a_failure(self):
        self.r.CLOSE_SECONDS = 0.3
        env = FakeEnv(close_seconds=5)

        def loop(agent, env_, example, steps, instruction, args, where, scores):
            pass

        t0 = time.monotonic()
        code, _ = self.run_with(env, loop)
        self.assertEqual(code, 1)
        self.assertLess(time.monotonic() - t0, 3)


if __name__ == "__main__":
    unittest.main()
