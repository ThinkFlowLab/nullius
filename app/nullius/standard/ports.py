"""The ports of version 1: what each one promises, and the kit that proves it.

A port's name carries its version. Its contract never changes under the same
name: a different contract is a new version, and a definition naming the old
one keeps meaning the old one. ``label`` is what a page calls it, in words a
person needs no glossary for; the port's name stays beside it. ``kit`` is the folder under ``standard/kits/``
holding the tiny benchmark that proves the port, or None while a port is
defined ahead of its first benchmark.
"""
from __future__ import annotations

import re

from . import names

NAME = re.compile(r"^(agent|cap)\.[a-z][a-z0-9-]{0,31}/v[1-9][0-9]{0,2}$")

PORTS = {
    "agent.task/v1": {
        "label": "One task",
        "family": "agent",
        "summary": "One prompt in, one turn; the answer is the agent's final message.",
        "kit": "agent-task-v1",
    },
    "agent.workspace/v1": {
        "label": "Workspace files",
        "family": "agent",
        "summary": "The session's working directory is the unit's workspace; the agent reads and writes files there.",
        "kit": "agent-workspace-v1",
    },
    "agent.tools/v1": {
        "label": "MCP tools",
        "family": "agent",
        "summary": "The agent calls the tools of the MCP servers given to its session.",
        "kit": "agent-tools-v1",
    },
    "agent.conversation/v1": {
        "label": "Conversation",
        "family": "agent",
        "summary": "Several prompts in one session, each answered in its own turn, the earlier ones remembered.",
        "kit": "agent-conversation-v1",
    },
    "agent.browser/v1": {
        "label": "Browser",
        "family": "agent",
        "summary": "A browser reached through an MCP server Nullius starts.",
        "kit": None,
    },
    "cap.memory/v1": {
        "label": "Memory",
        "family": "capability",
        "summary": "add {session} then recall {question} -> {memory}: what it kept that bears on the question.",
        "kit": "cap-memory-v1",
    },
}


def family(port: str) -> str:
    """``agent`` or ``capability``: the side of a pairing a port belongs to."""
    return "capability" if names.port(port).startswith("cap.") else port.split(".", 1)[0]


def known(port: str) -> bool:
    return names.port(port) in PORTS
