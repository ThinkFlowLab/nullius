"""An own image's check's one way out: an HTTP(S) proxy to public addresses only.

A task's check may download what it runs (its test runner, a package) once
the agent has ended. Its container stays on the unit's internal network, which
has no route off it; this proxy, a container of its own on the default bridge
and on that network as ``egress`` (``container.Box.online``), is the check's
door, given to it as ``HTTP_PROXY`` and ``HTTPS_PROXY``. It resolves the
destination itself and connects only to a *global* address (``ipaddress``'s
``is_global``): never a private range (RFC 1918), loopback, link-local
(``169.254.0.0/16``, where cloud metadata services answer), the shared range
``100.64.0.0/10`` (where some clouds put theirs, ``100.100.100.200``), nor the
docker bridge and the machine behind it. It connects to the address it
checked, not to the name again, so a name that resolves differently the second
time gains nothing. ``CONNECT`` is passed through unread (TLS stays the
client's); a plain ``http://`` request is sent on with ``Connection: close``,
so one connection is one destination.

Why a proxy and not firewall rules: rules on docker's ``DOCKER-USER`` chain
need root on the machine, and a host without it would have to run the check
with no filter at all; a proxy needs nothing but docker, so every machine
runs the check the same way. What it costs is that a download must honour the
proxy variables - apt, curl, wget, git over HTTPS, pip, uv and npm do - and a
name cannot be resolved inside, which a client using the proxy never needs.

Run as a file on its own (``python3 egress.py --listen 0.0.0.0:3128``),
mounted alone into the base image: it imports nothing of this application. One
JSON line per request on its output (``allowed`` or ``refused``, host and
port), which the trial keeps (``verifier/egress.log``).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import threading
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

HEAD_LIMIT = 65536
HOP = ("connection", "proxy-connection", "keep-alive", "proxy-authorization", "te", "trailer", "upgrade")


def public(address: str) -> bool:
    """Whether an address is one the check may reach: global, and not multicast."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> Tuple[Optional[tuple], List[str]]:
    """(the first public address of ``host`` as a socket address, every
    address it resolved to). None when it has no public one."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None, []
    seen = [i[4][0] for i in infos]
    for family, _, _, _, sockaddr in infos:
        if public(sockaddr[0]):
            return (family, sockaddr), seen
    return None, seen


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


def _say(event: str, **kw) -> None:
    print(json.dumps(dict(event=event, **kw)), flush=True)


def _reply(conn: socket.socket, status: str, why: str = "") -> None:
    body = (why + "\n").encode()
    try:
        conn.sendall(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _read_head(conn: socket.socket) -> Tuple[bytes, bytes]:
    """(the request's head, up to its blank line; what came after it)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        data = conn.recv(65536)
        if not data:
            break
        buf += data
        if len(buf) > HEAD_LIMIT:
            break
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def target(head: bytes) -> Tuple[str, str, int, bytes]:
    """(method, host, port, the head as the origin is sent it) from a request
    head; ValueError when it is not a proxy request."""
    lines = head.decode("iso-8859-1").split("\r\n")
    method, uri, version = lines[0].split(" ", 2)
    if method.upper() == "CONNECT":
        host, _, port = uri.rpartition(":")
        return "CONNECT", host.strip("[]"), int(port), b""
    u = urlsplit(uri)
    if u.scheme != "http" or not u.hostname:
        raise ValueError("an absolute http:// URL, or CONNECT host:port")
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    kept = [l for l in lines[1:] if l and l.split(":", 1)[0].strip().lower() not in HOP]
    out = "\r\n".join([f"{method} {path} {version}"] + kept + ["Connection: close", "", ""])
    return method, u.hostname, u.port or 80, out.encode("iso-8859-1")


def _serve(conn: socket.socket) -> None:
    try:
        conn.settimeout(60)
        head, rest = _read_head(conn)
        try:
            method, host, port, origin_head = target(head)
            if not 0 < port < 65536:
                raise ValueError("a port")
        except (ValueError, IndexError):
            _reply(conn, "400 Bad Request", "not a proxy request")
            return
        found, seen = resolve(host, port)
        if found is None:
            _say("refused", host=host, port=port, addresses=seen[:4])
            _reply(conn, "403 Forbidden", f"{host} is not a public address")
            return
        family, sockaddr = found
        try:
            up = socket.socket(family, socket.SOCK_STREAM)
            up.settimeout(30)
            up.connect(sockaddr)
            up.settimeout(None)
        except OSError as e:
            _say("unreachable", host=host, port=port, error=str(e)[:200])
            _reply(conn, "502 Bad Gateway", f"{host}:{port} did not answer")
            return
        _say("allowed", host=host, port=port, method=method)
        conn.settimeout(None)
        if method == "CONNECT":
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
        else:
            up.sendall(origin_head + rest)
        threading.Thread(target=_pump, args=(up, conn), daemon=True).start()
        _pump(conn, up)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="egress", description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", required=True, help="address:port to listen on")
    a = ap.parse_args(argv)
    host, _, port = a.listen.rpartition(":")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(port)))
    srv.listen(128)
    _say("listening", port=int(port))
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
