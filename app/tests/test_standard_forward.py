"""The licence server's forwarder (``forward.py``), run in-process: it
resolves its target once, before it listens, tries each address in order,
ends when the name does not resolve, and sends a door's nonce first."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from nullius.standard import forward


def _greeter(greeting: bytes, host: str = "127.0.0.1") -> int:
    family, _, _, _, addr = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)[0]   # where the name goes first
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.bind(addr[:2])
    srv.listen(8)

    def serve():
        while True:
            conn, _ = srv.accept()
            conn.sendall(greeting)
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recorder() -> tuple:
    """A server that keeps what each connection sent, once it closes: (port, what)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    got = []

    def keep(conn):
        data = b""
        with conn:
            while chunk := conn.recv(64):
                data += chunk
        got.append(data)

    def serve():
        while True:
            threading.Thread(target=keep, args=(srv.accept()[0],), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], got


def _forwarding(to: str, *extra: str) -> int:
    """A forwarder to ``to`` in a thread: the port it listens on, once it listens."""
    listen = _free_port()
    threading.Thread(target=forward.main, args=(["--listen", f"127.0.0.1:{listen}", "--to", to, *extra],), daemon=True).start()
    deadline = time.time() + 10
    while True:
        try:
            with socket.socket() as s:           # by address: no lookup, which a test may have patched
                s.settimeout(1)
                s.connect(("127.0.0.1", listen))
            return listen
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)


def _greeting(port: int) -> bytes:
    with socket.socket() as c:
        c.settimeout(10)
        c.connect(("127.0.0.1", port))
        return c.recv(64)


class ForwardTest(unittest.TestCase):
    def test_a_named_server_is_resolved_once_so_a_later_lookup_cannot_redirect_it(self):
        real = _greeter(b"LICENSE-OK", "localhost")
        listen = _forwarding(f"localhost:{real}")
        decoy = _greeter(b"SELF")                # the name now answers as the forwarder's own
        later = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", decoy))]
        with mock.patch("socket.getaddrinfo", return_value=later):
            self.assertEqual(_greeting(listen), b"LICENSE-OK")

    def test_each_connection_takes_the_names_first_address_that_accepts(self):
        first, second = _greeter(b"FIRST"), _greeter(b"SECOND")
        both = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", first)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", second))]
        with mock.patch("socket.getaddrinfo", return_value=both):
            listen = _forwarding("licence.example:27000")
        self.assertEqual([_greeting(listen) for _ in range(3)], [b"FIRST"] * 3)

    def test_a_refused_address_is_passed_over_for_the_next(self):
        real = _greeter(b"LICENSE-OK")
        dead = socket.socket()                   # bound, never listening: refused, and no one else can take it
        self.addCleanup(dead.close)
        dead.bind(("127.0.0.1", 0))
        both = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", dead.getsockname()),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", real))]
        with mock.patch("socket.getaddrinfo", return_value=both):
            listen = _forwarding("licence.example:27000")
        self.assertEqual(_greeting(listen), b"LICENSE-OK")

    def test_an_address_family_this_machine_lacks_is_skipped(self):
        real, make = _greeter(b"LICENSE-OK"), socket.socket

        def no_ipv6(family=-1, *args, **kwargs):     # the greeter's accept makes sockets too
            if family == socket.AF_INET6:
                raise OSError(97, "Address family not supported by protocol")
            return make(family, *args, **kwargs)
        with mock.patch("socket.socket", side_effect=no_ipv6):
            up = forward._connect([(socket.AF_INET6, ("::1", real)), (socket.AF_INET, ("127.0.0.1", real))])
        with up:
            self.assertEqual(up.recv(64), b"LICENSE-OK")

    def test_no_address_at_all_is_an_error_not_a_crash(self):
        with self.assertRaises(OSError):
            forward._connect([])

    def test_a_nonce_line_reaches_the_target_before_the_clients_bytes(self):
        port, got = _recorder()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "nonce"), "w") as f:
                f.write("n0nce\n")
            listen = _forwarding(f"127.0.0.1:{port}", "--nonce-file", os.path.join(d, "nonce"))
        with socket.create_connection(("127.0.0.1", listen), timeout=10) as c:   # the file is gone: read once
            c.sendall(b"ping")
        deadline = time.time() + 10
        while b"n0nce\nping" not in got and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn(b"n0nce\nping", got)
        self.assertTrue(all(data.startswith(b"n0nce\n") for data in got))      # the readiness probe's too

    def test_an_unreadable_nonce_file_ends_it_before_it_listens(self):
        out, ended = io.StringIO(), []
        argv = ["--listen", f"127.0.0.1:{_free_port()}", "--to", "127.0.0.1:9", "--nonce-file", "/nonexistent/nonce"]
        with contextlib.redirect_stdout(out):
            t = threading.Thread(target=lambda: ended.append(forward.main(argv)), daemon=True)
            t.start()
            t.join(10)
        self.assertEqual(ended, [1])
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["event"], "no-nonce")

    def test_an_unreachable_target_is_said_and_the_client_closed(self):
        dead = socket.socket()                   # bound, never listening: refused
        self.addCleanup(dead.close)
        dead.bind(("127.0.0.1", 0))
        conn, client = socket.socketpair()
        self.addCleanup(client.close)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            forward._serve(conn, [(socket.AF_INET, dead.getsockname())], "n0nce")    # raises nothing
        self.assertEqual(json.loads(out.getvalue())["event"], "unreachable")
        client.settimeout(10)
        self.assertEqual(client.recv(64), b"")

    def test_a_name_that_does_not_resolve_ends_it_with_the_reason(self):
        out, ended = io.StringIO(), []
        argv = ["--listen", f"127.0.0.1:{_free_port()}", "--to", "nowhere.invalid:27000"]
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such name")), \
                contextlib.redirect_stdout(out):
            t = threading.Thread(target=lambda: ended.append(forward.main(argv)), daemon=True)
            t.start()
            t.join(10)                           # a forwarder that listened anyway never returns
        self.assertEqual(ended, [1])
        event = json.loads(next(line for line in out.getvalue().splitlines() if '"unresolved"' in line))
        self.assertEqual((event["event"], event["host"]), ("unresolved", "nowhere.invalid"))


if __name__ == "__main__":
    unittest.main()
