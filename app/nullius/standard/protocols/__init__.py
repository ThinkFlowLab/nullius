"""How the programs of a run speak to each other.

``acp`` is the ACP client that drives an agent. ``jsonl`` is the JSON lines a
benchmark's driver and a capability are spoken to in, and ``serve``, the other
side, for one written in Python. ``cancellation`` is the wait both give up when
their run stops. ``computer_port`` is the computer port's MCP server, which a
desktop benchmark's runner serves over its own loop, and ``computer_pipe`` the
stdio server an agent's session starts to reach it, found beside it; neither
imports anything of this application.
"""
