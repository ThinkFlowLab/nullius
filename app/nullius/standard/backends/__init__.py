"""Where a unit runs when not among this machine's processes, and the doors through.

``container`` runs each unit's agent or capability in a container of its own,
its relay beside it; ``setup_container`` runs an Original setup's driver in
one. ``boat`` runs each unit's agent or capability in a boat.dev VM of its
own, its relay still on this machine. The rest is what crosses a container's
boundary, one address at a time: ``forward`` (a licence server, or a server on
the machine), ``egress`` (public addresses only: an own image's check, or an
agent whose environment declares public web access), ``mcp_machine`` and
``mcp_bridge`` (a benchmark's MCP server run on the machine, and the agent's
bridge to it) and ``web_services`` (the HTTP fixtures a driver's setup
declares).

``forward.py``, ``egress.py`` and ``mcp_bridge.py`` are each mounted alone into
a container and run there: they import nothing of this application, and
``container`` and ``mcp_machine`` find them beside themselves.
"""
