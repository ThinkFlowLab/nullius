"""A one-address door: a TCP forwarder to a single ``host:port``, or to a
single Unix socket (``unix:/path``).

On the docker backend the agent's container sits on an internal network with
no route out; its relay is its way to the model. A licence of kind
``server`` needs one more address: this forwarder, a container of its own
beside the relay (``container.Unit``), on the default bridge and on the
unit's network under the server's own name, listening on the server's port
and passing every connection to that one address - and to nothing else,
whatever the agent sends. It reads nothing of what it passes.

A benchmark's MCP server run on the machine (``mcp_machine``) is reached the
same way: a forwarder on the unit's internal network only, the server's
socket mounted into it alone, ``--to unix:<socket>``.

Run as a file on its own (``python3 forward.py --listen 0.0.0.0:27000``,
the server in ``NULLIUS_FORWARD_TO``), mounted alone into the base image: it imports
nothing of this application, so its container is given nothing of it.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (dst, src):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve(conn: socket.socket, host: str, port) -> None:
    try:
        if port is None:                         # a Unix socket: host is its path
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.settimeout(30)
            up.connect(host)
        else:
            up = socket.create_connection((host, port), timeout=30)
        up.settimeout(None)
    except OSError as e:
        print(json.dumps({"event": "unreachable", "error": str(e)[:200]}), flush=True)
        conn.close()
        return
    threading.Thread(target=_pump, args=(up, conn), daemon=True).start()
    _pump(conn, up)


def _split(addr: str):
    if addr.startswith("unix:"):
        return addr[len("unix:"):], None
    host, _, port = addr.rpartition(":")
    return host, int(port)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forward", description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", required=True, help="address:port to listen on")
    ap.add_argument("--to", default=os.environ.get("NULLIUS_FORWARD_TO", ""),
                    help="the one host:port, or unix:/path, every connection goes to (default: NULLIUS_FORWARD_TO)")
    a = ap.parse_args(argv)
    if not a.to:
        ap.error("--to, or NULLIUS_FORWARD_TO: where to forward")
    lhost, lport = _split(a.listen)
    host, port = _split(a.to)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lhost, lport))
    srv.listen(64)
    print(json.dumps({"event": "listening", "port": lport}), flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve, args=(conn, host, port), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
