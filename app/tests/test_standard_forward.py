"""The licence server's forwarder (``forward.py``), run in-process: it
resolves its target once, before it listens, tries each address in order, and
ends when the name does not resolve."""
from __future__ import annotations

import contextlib
import io
import json
import socket
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


def _forwarding(to: str) -> int:
    """A forwarder to ``to`` in a thread: the port it listens on, once it listens."""
    listen = _free_port()
    threading.Thread(target=forward.main, args=(["--listen", f"127.0.0.1:{listen}", "--to", to],), daemon=True).start()
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
