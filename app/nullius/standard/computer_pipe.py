"""The stdio MCP server ``computer`` an agent's session starts: a pipe to the computer port's server.

    computer_pipe.py SOCKET

The port's server (``computer_port.Server``) runs in the unit's runner and accepts the agent's session at SOCKET, a
Unix socket in a folder of the user's alone; it lives as long as the unit. An MCP client ends a stdio server it started
soon after closing its input: the server it starts is this pipe, and the port's server outlives it. Stdin is read apart
from the sends to the socket, and the socket is read apart from both, so that no direction waits on another: stdin to
the socket and the socket to stdout, unchanged, each write whole. At most QUEUE_CHUNKS reads of the input wait for the
socket: while they do, the input is not read, and its end is seen by its writer's hang-up instead.

The pipe exits 0 only when both directions are complete: every byte of the input it read was sent (and, once the input
ended, the socket's writing side closed), and every byte it read from the socket was written. A read of the input is
counted as it is taken, under the lock that the decision to exit takes, so that no byte read is ever left out of it.

- The end of its input (or its writer's hang-up) sets the pipe's one deadline, END_SECONDS later, and every wait of
  the pipe's end counts to it.
- Once the input ended and all it read is sent, the socket's writing side is closed; when the socket's side then ends,
  the pipe exits 0.
- When the socket's side ends first, the pipe reads on to its input's end and sends what it reads, by the deadline
  (the input's, else END_SECONDS from then). It exits 0 only when it saw its input's end and sent all of it; else it
  exits 1, the bytes left unsent on stderr.
- At the deadline with the socket's side still open, the pipe takes no more of the socket, sends no more to it, and
  exits 3: its end was cut with the server still connected. The bytes left either way are counted on stderr.

Standard library only.
"""
import os
import queue
import select
import socket
import sys
import threading
import time

END_SECONDS = 5
CUT = 3
QUEUE_CHUNKS = 64                                    # reads of the input, of up to 64 KiB each, waiting for the socket


def main():
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(sys.argv[1])
    except OSError as e:
        sys.stderr.write(f"computer_pipe: the server's socket {sys.argv[1]}: {e}\n")
        return 1
    input_ended, socket_ended, sent_all, cut, quiet = (threading.Event() for _ in range(5))
    chunks = queue.Queue(QUEUE_CHUNKS)
    failed, lock = [], threading.Lock()
    held = {"in": 0, "out": 0}                       # read and not yet sent; read from the socket and not yet written
    ended_at = []                                    # when the input ended, as the reader saw it
    hang_up = select.poll()
    hang_up.register(0, select.POLLHUP)              # the input's writer gone, even while its last bytes wait unread

    def count(way, n):
        with lock:
            held[way] += n

    def put(item):                                   # a full queue holds the input back until the socket takes more
        while not cut.is_set():
            try:
                chunks.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def from_input():
        try:
            while True:
                readable = select.select([0], [], [], 0.2)[0]
                with lock:                           # a read is counted before anyone can decide on what is held
                    if quiet.is_set() or cut.is_set():
                        return
                    if not readable:
                        continue
                    data = os.read(0, 65536)
                    held["in"] += len(data)
                if not data:
                    break
                put(data)
        except (OSError, ValueError) as e:
            failed.append(e)
        ended_at.append(time.monotonic())
        input_ended.set()
        put(None)

    def to_server():
        try:
            while not cut.is_set():
                try:
                    data = chunks.get(timeout=0.2)
                except queue.Empty:
                    continue
                if data is None:
                    conn.shutdown(socket.SHUT_WR)
                    sent_all.set()
                    return
                conn.sendall(data)
                count("in", -len(data))
        except OSError as e:
            if not cut.is_set():
                failed.append(e)

    def from_server():
        try:
            while not cut.is_set():
                data = conn.recv(65536)
                if not data:
                    break
                count("out", len(data))
                view = memoryview(data)
                while view:                          # whole: what was read is written, also after the cut
                    n = os.write(1, view)
                    view = view[n:]
                    count("out", -n)
        except OSError as e:
            if not cut.is_set():
                failed.append(e)
        socket_ended.set()

    def left():
        with lock:
            return held["in"], held["out"]

    reader = threading.Thread(target=from_server, daemon=True)
    for target in (from_input, to_server):
        threading.Thread(target=target, daemon=True).start()
    reader.start()
    deadline = None                                  # the pipe's one deadline, from the input's end
    while not socket_ended.wait(0.2 if deadline is None else max(0.0, min(0.2, deadline - time.monotonic()))):
        if deadline is None and (input_ended.is_set() or hang_up.poll(0)):
            deadline = (ended_at[0] if ended_at else time.monotonic()) + END_SECONDS
        if deadline is not None and time.monotonic() >= deadline:
            cut.set()                                # no more is taken or sent
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            unsent, unwritten = left()
            sys.stderr.write(f"computer_pipe: cut {END_SECONDS:g} s after the input's end, the server still connected: "
                             f"{unwritten} bytes from the server not written, {unsent} bytes of input not sent\n")
            return CUT
    until = deadline if deadline is not None else time.monotonic() + END_SECONDS
    while not failed and time.monotonic() < until and not (input_ended.is_set() and sent_all.is_set()):
        time.sleep(0.05)                             # the socket's side ended: the input is read on to its end, and sent
    with lock:                                       # no more input is taken
        quiet.set()
    if failed:
        sys.stderr.write(f"computer_pipe: {type(failed[0]).__name__}: {failed[0]}\n")
        return 1
    unsent = left()[0]
    if unsent or not input_ended.is_set() or not sent_all.is_set():
        sys.stderr.write(f"computer_pipe: the server's side ended with {unsent} bytes of input not sent"
                         + ("" if input_ended.is_set() else ", its input not read to its end") + "\n")
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
