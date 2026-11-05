"""A program spoken to in JSON lines: one request, one reply.

The benchmark's driver and a capability are both this: a process started from
its definition's command, a request written as one JSON line on its stdin
(``{"op": ..., ...}``), one JSON line back on its stdout. A reply carrying
``error`` is raised as :class:`Failed`; anything that is not one JSON object
line, or no reply within the timeout, is raised too, naming what it got.
Its stderr goes to a log file, never into the reply.
"""
from __future__ import annotations

import json
import os
import queue
from concurrent.futures import CancelledError
from nullius.standard.cancellation import reply as wait_reply
import signal
import subprocess
import threading
from typing import List, Optional


class Failed(Exception):
    """The program answered with an error, or did not answer as it should."""


class Proc:
    def __init__(self, cmd: List[str], cwd: str, env: dict, log: str, stopped=None):
        self.cmd, self.cwd, self.env, self.log = cmd, cwd, env, log
        self.stopped = stopped
        self.p: Optional[subprocess.Popen] = None
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def start(self) -> "Proc":
        if self.stopped is not None and self.stopped.is_set():
            raise CancelledError("the run was stopped")
        self.err = open(self.log, "a")
        self.p = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.err, text=True, bufsize=1,
                                  start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def request(self, op: str, timeout: float = 600, **kw) -> dict:
        if self.stopped is not None and self.stopped.is_set():
            raise CancelledError("the run was stopped")
        if self.p is None:
            self.start()
        try:
            self.p.stdin.write(json.dumps(dict(kw, op=op)) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise Failed(f"{op}: the program had already ended ({self._tail()})")
        try:
            line = wait_reply(self.lines, timeout, self.stopped)
        except queue.Empty:
            self.close()
            raise Failed(f"{op}: no reply in {int(timeout)} s")
        if line is None:
            raise Failed(f"{op}: the program ended without replying ({self._tail()})")
        try:
            reply = json.loads(line)
        except ValueError:
            raise Failed(f"{op}: the reply is not JSON: {line[:200]!r}")
        if not isinstance(reply, dict):
            raise Failed(f"{op}: the reply is not an object")
        if reply.get("error"):
            raise Failed(f"{op}: {reply['error']}")
        return reply

    def _tail(self) -> str:
        try:
            self.err.flush()
            with open(self.log, errors="replace") as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            return lines[-1][:240] if lines else "nothing on stderr"
        except OSError:
            return "no log"

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.p:
            for f in (self.p.stdin, self.p.stdout, self.err):
                try:
                    f.close()
                except Exception:
                    pass
            try:
                self.p.wait(timeout=3)
            except Exception:
                pass


def serve(handlers: dict, out=None) -> None:
    """The other side, for a driver or a capability written in Python: read
    requests from stdin, answer each with ``handlers[op](**request)``.
    An exception becomes ``{"error": ...}`` and the loop goes on.

    ``out``: where the replies go, ``sys.stdout`` when not given - a program
    that sends ``sys.stdout`` elsewhere for good (threads printing while a
    request is answered) passes the protocol's own stream here."""
    import sys
    out = out or sys.stdout
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            op = req.pop("op")
            if op not in handlers:
                raise ValueError(f"unknown op {op!r}")
            reply = handlers[op](**req)
        except Exception as e:                       # the error is the reply
            reply = {"error": f"{type(e).__name__}: {e}"}
        out.write(json.dumps(reply) + "\n")
        out.flush()
