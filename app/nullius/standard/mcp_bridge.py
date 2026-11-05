"""A stdio MCP server that is a benchmark's, run on the machine: this joins
its stdin and stdout to one TCP address, the unit's door to that server
(``NULLIUS_MCP_AT``, ``host:port``), and passes the bytes both ways
unchanged, reading none of them.

Mounted alone into the agent's container on the docker backend and started
by the agent as any stdio MCP server (``mcp_machine``); it imports nothing
of this application, only the standard library, so the container needs
nothing but ``python3``.
"""
import os
import socket
import sys
import threading


def main() -> int:
    at = os.environ.get("NULLIUS_MCP_AT", "")
    host, _, port = at.rpartition(":")
    if not host or not port.isdigit():
        sys.stderr.write("mcp_bridge: NULLIUS_MCP_AT, host:port of the server's door\n")
        return 2
    try:
        s = socket.create_connection((host, int(port)), timeout=30)
    except OSError as e:
        sys.stderr.write(f"mcp_bridge: the server's door {at}: {e}\n")
        return 1
    s.settimeout(None)
    out = sys.stdout.buffer

    def down():                                  # the server's replies, to the agent
        try:
            while True:
                data = s.recv(65536)
                if not data:
                    break
                out.write(data)
                out.flush()
        except OSError:
            pass
        os._exit(0)

    threading.Thread(target=down, daemon=True).start()
    src = sys.stdin.buffer
    try:
        while True:
            data = src.read1(65536) if hasattr(src, "read1") else src.read(1)
            if not data:
                break
            s.sendall(data)
    except OSError:
        pass
    try:
        s.shutdown(socket.SHUT_WR)               # the agent is done: the server sees its stdin end
    except OSError:
        pass
    threading.Event().wait(5)                    # what the server still says, then go
    return 0


if __name__ == "__main__":
    sys.exit(main())
