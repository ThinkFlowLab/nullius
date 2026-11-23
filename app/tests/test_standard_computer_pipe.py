"""computer_pipe.py, the stdio server ``computer`` an agent's session starts, against a socket of the test's or a
stand-in socket in the pipe's own process: both directions at once, what the server sent written out before the pipe
ends, the one deadline from the input's end, a bounded queue, and its exit status. Each run is bounded and its child
ended."""
from __future__ import annotations

import importlib.util
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PIPE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nullius", "standard",
                    "protocols", "computer_pipe.py")


def pipe_module():
    spec = importlib.util.spec_from_file_location("pipe_module", PIPE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Pipe(unittest.TestCase):
    def run_pipe(self, serve, data=b"request\n", seconds=30) -> tuple:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(listener.close)
        server = threading.Thread(target=lambda: serve(listener.accept()[0]), daemon=True)
        server.start()
        p = subprocess.Popen([sys.executable, PIPE, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        t0 = time.monotonic()
        try:
            out, _ = p.communicate(data, timeout=seconds)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        return out, p.returncode, time.monotonic() - t0

    def stand_in(self, socket_code: str, extra: str = "", stdin=subprocess.PIPE):
        """The pipe in a process of the test's, its socket a stand-in (``socket_code``: a class Connected), and
        ``extra`` run before main. sys.argv[2] is a file of the test's."""
        code = ("import importlib.util, os, sys, threading, time\n" + socket_code +
                "spec = importlib.util.spec_from_file_location('pipe', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                "module.socket.socket = lambda *a, **kw: Connected()\n" + extra +
                "os._exit(module.main())\n")
        p = subprocess.Popen([sys.executable, "-c", code, PIPE, self.tmp_file()], stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def tmp_file(self, name="sent"):
        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, self._tmp, True)
        return os.path.join(self._tmp, name)

    def test_a_reply_after_the_inputs_end_is_written_out(self):
        def serve(conn):
            conn.makefile("rb").readline()
            time.sleep(1)
            conn.sendall(b"late reply\n")
            conn.close()
        self.assertEqual(self.run_pipe(serve)[:2], (b"late reply\n", 0))

    def test_a_server_still_connected_gets_its_reply_written_and_a_cut_status(self):
        held = []

        def serve(conn):
            held.append(conn)
            conn.sendall(b"early reply\n")
        out, code, seconds = self.run_pipe(serve)
        self.assertEqual((out, code), (b"early reply\n", 3))
        self.assertLess(seconds, 15)
        for conn in held:
            conn.close()

    def test_a_server_that_writes_on_and_on_is_cut_at_the_deadline(self):
        stop = threading.Event()
        self.addCleanup(stop.set)

        def serve(conn):
            try:
                while not stop.is_set():
                    conn.sendall(b"x" * 1024)
                    time.sleep(0.01)
            except OSError:
                pass
        out, code, seconds = self.run_pipe(serve)
        self.assertEqual(code, 3)
        self.assertLess(seconds, 15)
        self.assertTrue(out and set(out) == {ord("x")})

    def test_a_server_that_always_has_more_to_send_is_cut_at_the_deadline(self):
        # The socket is never empty: the server writes with no pause and the test reads the pipe's output slowly, so
        # the pipe always finds more to read. The cut comes at the deadline all the same, and the run is bounded.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(listener.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def serve():
            conn = listener.accept()[0]
            try:
                while not stop.is_set():
                    conn.sendall(b"x" * 65536)
            except OSError:
                pass
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        p = subprocess.Popen([sys.executable, PIPE, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        t0, got = time.monotonic(), 0
        try:
            p.stdin.write(b"request\n")
            p.stdin.close()
            while time.monotonic() - t0 < 20:
                if not select.select([p.stdout], [], [], max(0.0, 20 - (time.monotonic() - t0)))[0]:
                    break
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    break
                got += len(chunk)
                time.sleep(0.02)
            code = p.wait(timeout=5) if p.poll() is None and time.monotonic() - t0 < 20 else p.poll()
        finally:
            stop.set()
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
        self.assertEqual(code, 3)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertGreater(got, 0)

    def test_the_cut_starts_at_the_inputs_end_while_a_send_waits(self):
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def sendall(self, data): time.sleep(30)\n"
                          "    def recv(self, count):\n        time.sleep(30)\n        return b''\n"
                          "    def shutdown(self, how): pass\n",
                          "module.END_SECONDS = 0.1\n")
        try:
            p.communicate(b"x" * 8192, timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.assertEqual(p.wait(timeout=5), 3, "the input ended, and the pipe was not cut")

    def test_input_still_being_sent_when_the_servers_side_ends_is_sent_before_an_exit_0(self):
        p = self.stand_in("started = threading.Event()\n"
                          "class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data):\n"
                          "        started.set()\n        time.sleep(0.5)\n"
                          "        open(sys.argv[2], 'ab').write(data)\n"
                          "    def recv(self, size):\n        started.wait(2)\n        return b''\n")
        p.communicate(b"request\n", timeout=10)
        self.assertEqual(p.returncode, 0)
        with open(self.tmp_file(), "rb") as f:
            self.assertEqual(f.read(), b"request\n")

    def test_input_left_unsent_when_the_servers_side_ends_is_an_exit_1(self):
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data): time.sleep(30)\n"
                          "    def recv(self, size):\n        time.sleep(0.3)\n        return b''\n",
                          "module.END_SECONDS = 0.2\n")
        p.stdin.write(b"request\n")
        p.stdin.flush()                                          # the input stays open: the server's side ends first
        code = p.wait(timeout=10)
        p.stdin.close()
        self.assertEqual(code, 1)
        self.assertIn(b"8 bytes of input not sent, its input not read to its end", p.stderr.read())

    def queue_scenario(self, extra=""):
        """The socket takes its first send, then stalls until the test lets it go: the input is held back meanwhile,
        and all of it reaches the socket once the socket takes more. No read's size is assumed."""
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def sendall(self, data):\n"
                          "        with open(sys.argv[2], 'ab') as f:\n            f.write(data)\n"
                          "        while not os.path.exists(sys.argv[2] + '.go'):\n            time.sleep(0.05)\n"
                          "    def shutdown(self, how):\n        open(sys.argv[2] + '.shut', 'w').close()\n"
                          "    def recv(self, size):\n"
                          "        while not os.path.exists(sys.argv[2] + '.shut'):\n            time.sleep(0.05)\n"
                          "        return b''\n", extra)
        chunk, written = b"q" * 65536, [0]
        p.stdin.write(chunk)
        p.stdin.flush()
        written[0] += len(chunk)
        deadline = time.monotonic() + 10
        while not self.sent() and time.monotonic() < deadline:  # the pipe runs and passes data on
            time.sleep(0.05)
        first = self.sent()
        self.assertGreater(first, 0, "nothing reached the socket")
        stop = threading.Event()

        def feed():
            try:
                while not stop.is_set() and written[0] < 64 * 1024 * 1024:
                    p.stdin.write(chunk)
                    p.stdin.flush()
                    written[0] += len(chunk)
            except (OSError, ValueError):
                pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        last = -1
        while time.monotonic() < deadline + 10 and written[0] != last:   # until the input is held back
            last = written[0]
            time.sleep(0.5)
        try:
            bound = 65536 * (3 + pipe_module().QUEUE_CHUNKS) + 4 * 1024 * 1024   # queued, one in each hand, the OS pipe
            self.assertLess(written[0], bound, "the pipe read on with nothing taken by the socket")
            self.assertEqual(self.sent(), first, "the stalled socket took more")
            stop.set()
            open(self.tmp_file() + ".go", "w").close()           # the socket takes more
            feeder.join(10)
            self.assertFalse(feeder.is_alive(), "the input was not drained once the socket took more")
        except BaseException:
            stop.set()
            p.kill()                                             # the feeder's write ends with the pipe's end
            p.wait()
            feeder.join(10)
            raise
        p.stdin.close()
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual(self.sent(), written[0])

    def test_input_waiting_for_a_stalled_socket_is_bounded_and_drained_once_it_takes_more(self):
        self.queue_scenario()

    def test_the_bounded_queue_holds_with_reads_of_any_size(self):
        self.queue_scenario("real_read = module.os.read\nmodule.os.read = lambda fd, n: real_read(fd, min(n, 4096))\n")

    def test_input_the_pipe_never_read_is_sent_or_its_exit_is_not_0(self):
        # The server's side ends at once; the input, a file, is read only after a delay. The pipe reads on to the
        # input's end and sends it, or does not exit 0.
        request = b"request that the half-closed server can still receive\n"
        with open(self.tmp_file("input"), "wb") as f:
            f.write(request)
        with open(self.tmp_file("input"), "rb") as src:
            p = self.stand_in("class Connected:\n"
                              "    def connect(self, path): pass\n"
                              "    def sendall(self, data):\n        open(sys.argv[2], 'ab').write(data)\n"
                              "    def recv(self, size): return b''\n"
                              "    def shutdown(self, how): pass\n",
                              "real_select = module.select.select\n"
                              "def delayed(*args):\n    time.sleep(0.2)\n    return real_select(*args)\n"
                              "module.select.select = delayed\n", stdin=src)
            code = p.wait(timeout=10)
        self.assertTrue(code != 0 or self.sent_bytes() == request, f"exit {code}, sent {self.sent_bytes()!r}")

    def sent_bytes(self):
        try:
            with open(self.tmp_file(), "rb") as f:
                return f.read()
        except OSError:
            return b""

    def sent(self):
        try:
            return os.path.getsize(self.tmp_file())
        except OSError:
            return 0

    def test_a_cut_counts_what_it_left_unwritten(self):
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data): pass\n"
                          "    def recv(self, size):\n"
                          "        if not hasattr(self, 'sent'):\n"
                          "            self.sent = True\n            return b'x' * 1048576\n"
                          "        time.sleep(30)\n        return b''\n",
                          "module.END_SECONDS = 0.2\n", stdin=subprocess.DEVNULL)
        code = p.wait(timeout=10)                     # nothing reads its output: most of what it read stays unwritten
        err = p.stderr.read()
        self.assertEqual(code, 3)
        self.assertRegex(err.decode(), r"cut 0\.2 s after the input's end, the server still connected: [1-9][0-9]* bytes "
                                       r"from the server not written")

    def test_both_directions_at_once_never_block_each_other(self):
        big = 2 * 1024 * 1024

        def serve(conn):
            conn.sendall(b"r" * big)                              # before reading any of the request
            got = 0
            while got < big:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                got += len(chunk)
            conn.close()
        out, code, _ = self.run_pipe(serve, data=b"q" * big)
        self.assertEqual((len(out), code), (big, 0))

    def test_the_inputs_end_is_seen_while_a_full_queue_holds_the_reader(self):
        # The socket never takes more, and the queue is full: the reader waits to put its last read and cannot read
        # on to the input's end. Its writer's hang-up is seen all the same, and the cut comes at the deadline.
        read, write = os.pipe()
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data): time.sleep(60)\n"
                          "    def recv(self, size):\n        time.sleep(60)\n        return b''\n",
                          "module.END_SECONDS = 0.5\nmodule.QUEUE_CHUNKS = 2\n", stdin=read)
        os.close(read)
        os.set_blocking(write, False)
        quiet_since, chunk = time.monotonic(), b"q" * 65536
        while time.monotonic() - quiet_since < 1.0:              # fill until the pipe takes nothing for a second
            try:
                os.write(write, chunk)
                quiet_since = time.monotonic()
            except BlockingIOError:
                time.sleep(0.05)
        os.close(write)                                           # the input ends, with bytes still waiting
        t0 = time.monotonic()
        code = p.wait(timeout=10)
        self.assertEqual(code, 3)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertRegex(p.stderr.read().decode(), r"[1-9][0-9]* bytes of input not sent")

    def test_bytes_read_while_the_servers_side_ends_are_sent_or_counted(self):
        # The input's read is held open across the socket's end: the read returns, and only then is it counted.
        # Exit 0 must mean that what was read was sent.
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data):\n        open(sys.argv[2], 'ab').write(data)\n"
                          "    def recv(self, size):\n"
                          "        while not os.path.exists(sys.argv[2] + '.read'):\n            time.sleep(0.01)\n"
                          "        return b''\n",
                          "real = os.read\n"
                          "def slow(fd, n):\n"
                          "    data = real(fd, n)\n"
                          "    if fd == 0 and data:\n"
                          "        open(sys.argv[2] + '.read', 'w').close()\n        time.sleep(0.5)\n"
                          "    return data\n"
                          "module.os.read = slow\n")
        p.stdin.write(b"request\n")
        p.stdin.close()                                           # the input ends once the request is read
        code = p.wait(timeout=10)
        err = p.stderr.read()
        sent = self.sent_bytes()
        self.assertEqual((code, sent), (0, b"request\n"), f"what was read was not sent: {err!r}")

    def test_the_end_after_the_inputs_end_has_one_deadline(self):
        # A write to stdout in progress (nothing reads the output) is not given a second period past the deadline.
        p = self.stand_in("class Connected:\n"
                          "    def connect(self, path): pass\n"
                          "    def shutdown(self, how): pass\n"
                          "    def sendall(self, data): pass\n"
                          "    def recv(self, size):\n"
                          "        if not hasattr(self, 'sent'):\n"
                          "            self.sent = True\n            return b'x' * 1048576\n"
                          "        time.sleep(30)\n        return b''\n",
                          "module.END_SECONDS = 2.0\n"
                          "open(sys.argv[2], 'w').write(repr(time.monotonic()))\n", stdin=subprocess.DEVNULL)
        code = p.wait(timeout=15)
        ended = time.monotonic()
        with open(self.tmp_file()) as f:
            started = float(f.read())
        self.assertEqual(code, 3)
        self.assertLess(ended - started, 3.0, "the pipe's end took a second period")


if __name__ == "__main__":
    unittest.main()
