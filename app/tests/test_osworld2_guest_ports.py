"""OSWorld2's guest ports on 127.0.0.1, one VM at a time per machine, locked until the VM is gone."""
from __future__ import annotations

import fcntl
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "benchmarks", "osworld2")


def load(up):
    spec = importlib.util.spec_from_file_location("osworld2_driver_ports", os.path.join(HERE, "driver.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]), \
            mock.patch.dict(os.environ, NULLIUS_UPSTREAM=up):
        spec.loader.exec_module(module)
    return module


def unused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GuestPorts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load(self.tmp)
        self.port = unused()
        self.driver.PORT_LOCK = os.path.join(self.tmp, "port-{}.lck")
        self.driver.GUEST_PORTS = {"082": (self.port,)}
        self.driver.CLOSED = threading.Event()
        self.lock = os.path.join(self.tmp, f"port-{self.port}.lck")

    def held(self):
        fd = os.open(self.lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def test_another_task_takes_no_lock_and_names_no_port(self):
        with self.driver._published("001") as env:
            self.assertEqual(env, {})
            self.assertEqual(self.driver._PORTS, [])
        self.assertFalse(os.path.exists(self.lock))

    def test_the_tasks_ports_are_named_and_locked_until_the_unit_ends(self):
        with self.driver._published("082") as env:
            self.assertEqual(env, {"OSWORLD2_GUEST_PORTS": str(self.port)})
            self.assertTrue(self.held())
            self.assertEqual(len(self.driver._PORTS), 1)
        self.assertFalse(self.held())
        self.assertEqual(self.driver._PORTS, [])

    def test_the_reaper_keeps_the_lock(self):
        with mock.patch.object(self.driver.subprocess, "Popen") as popen, self.driver._published("082"):
            held = tuple(self.driver._PORTS)
            self.driver._reaper(1, self.tmp, True)
        self.assertEqual(len(held), 1)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], held)

    def test_a_listener_on_loopback_is_refused_and_the_lock_let_go(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", self.port))
            s.listen()
            with self.assertRaisesRegex(RuntimeError, rf"task 082 .* port {self.port} .*127\.0\.0\.1"):
                with self.driver._published("082"):
                    self.fail("published while another process listens")
        self.assertFalse(self.held())
        self.assertEqual(self.driver._PORTS, [])

    def test_a_listener_on_every_interface_is_refused(self):
        with socket.socket() as s:
            s.bind(("0.0.0.0", self.port))
            s.listen()
            with self.assertRaisesRegex(RuntimeError, rf"task 082 .* port {self.port} "):
                with self.driver._published("082"):
                    self.fail("published while another process listens")
        self.assertFalse(self.held())

    def test_a_listener_on_ipv6_loopback_is_refused(self):
        try:
            s = socket.socket(socket.AF_INET6)
            s.bind(("::1", self.port))
        except OSError:
            self.skipTest("no IPv6 loopback here")
        with s:
            s.listen()
            with self.assertRaisesRegex(RuntimeError, r"listens on ::1"):
                with self.driver._published("082"):
                    self.fail("published while another process listens")

    def test_a_connection_closing_on_the_port_is_no_listener(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", self.port))
            server.listen()
            client = socket.create_connection(("127.0.0.1", self.port))
            accepted, _ = server.accept()
            accepted.close()
            client.close()
        with self.driver._published("082") as env:
            self.assertEqual(env, {"OSWORLD2_GUEST_PORTS": str(self.port)})

    def test_another_unit_waits_until_its_time_runs_out(self):
        fd = os.open(self.lock, os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(1.5, self.driver.CLOSED.set).start()
        with self.assertRaisesRegex(RuntimeError, rf"time ran out while another unit's VM held port {self.port}"):
            with self.driver._published("082"):
                self.fail("published while another unit holds the port")
        self.assertEqual(self.driver._PORTS, [])

    def test_a_fifo_in_the_locks_place_is_refused_at_once(self):
        os.mkfifo(self.lock)
        with self.assertRaisesRegex(RuntimeError, "cannot be opened"):
            with self.driver._published("082"):
                self.fail("published through a FIFO")
        self.assertEqual(self.driver._PORTS, [])

    def test_the_lock_is_readable_by_every_user_whatever_the_umask(self):
        mask = os.umask(0o077)
        try:
            with self.driver._published("082"):
                pass
        finally:
            os.umask(mask)
        self.assertEqual(os.stat(self.lock).st_mode & 0o777, 0o644)

    def test_a_link_in_the_locks_place_is_refused(self):
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.lock)
        with self.assertRaisesRegex(RuntimeError, "cannot be opened"):
            with self.driver._published("082"):
                self.fail("published through a link")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere")))


if __name__ == "__main__":
    unittest.main()
