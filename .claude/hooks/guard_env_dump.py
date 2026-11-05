#!/usr/bin/env python3
"""Refuse Bash commands that would print a credential file into a transcript.

A file an assistant reads directly lands in its transcript whole.
This runs as a PreToolUse hook on Bash and exits 2 (block) when a command would dump a
secret-bearing file, naming the safe alternative in the refusal.

Writing to those files stays allowed — the hazard is reading one *out*, not
putting one in. So does anything that reports presence without content
(`test -f`, `ls`, `wc -l`, `grep -c/-q/-l`), which is how the repo already
says to check a key exists: by length, never by value.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

# Files that hold live credentials. `.env` at any depth (the gitignore rule),
# a `_secrets.json` sidecar, and the checkout's secret/ directory.
SECRET_PATH = re.compile(
    r"""(?<![\w.-])(
          [\w./~-]*\.env(\.[\w-]+)?      # .env, agent/.env, .env.local
        | [\w./~-]*_secrets\.json        # a read-once secrets sidecar
        | [\w./~-]*secrets?/[\w./-]+     # secret/.env and friends
        )(?![\w-])""",
    re.VERBOSE,
)

# Verbs that put file content on stdout. `grep` and `sed` are handled
# separately: both have forms that report or edit without printing, and those
# are the ones worth keeping.
DUMP_VERBS = frozenset("""
    cat tac head tail less more bat nl od xxd strings base64 hexdump
    awk cut paste rev sort uniq tr column jq yq diff vimcat view
""".split())

# Only for the fallback path, where a command would not lex.
DUMP = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(DUMP_VERBS)) + r")(?![\w-])"
)

# grep that only reports, never prints a matching line.
QUIET_GREP = re.compile(r"(?<![\w-])grep\b[^|;&]*?\s-[a-zA-Z]*[qclL]")

# `sed -i` edits in place; `sed 's/x/y/' file` prints. Only the latter dumps.
SED_INPLACE = re.compile(r"(?<![\w-])sed\b[^|;&]*?\s-i\b")

ADVICE = (
    "Blocked: this would print a credential file into the transcript.\n\n"
    "Instead:\n"
    "  grep -c NAME= / grep -q / test -f   presence without content\n"
    "  a script that loads the file into a command's environment and prints nothing\n\n"
    "Writing to the file is fine — it is reading one out that is refused."
)


OPERATORS = {"|", "||", "&&", ";", "&", "\n"}
INTERPRETERS = {"python", "python3", "perl", "ruby", "node"}


def _tokenize(command: str) -> list[str] | None:
    """Shell tokens, or None if the command will not lex.

    Quoting is the whole point: a secret path *mentioned* inside a quoted
    argument stays one opaque token and never looks like a filename being
    read, for example in `git commit -m "... tail .env ..."`.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list at shell operators into pipeline segments."""
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok in OPERATORS:
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _segment_reads_a_secret(seg: list[str]) -> list[str]:
    """Secret paths this segment would read out. Redirect targets excluded."""
    if not seg:
        return []

    # Anything before the command word that looks like FOO=bar is an env
    # assignment, not the verb.
    idx = 0
    while idx < len(seg) and re.fullmatch(r"[A-Za-z_]\w*=.*", seg[idx]):
        idx += 1
    if idx >= len(seg):
        return []
    verb = seg[idx].rsplit("/", 1)[-1]
    args = seg[idx + 1:]

    written = {
        args[i + 1]
        for i, tok in enumerate(args)
        if tok in (">", ">>") and i + 1 < len(args)
    }
    if verb == "tee":
        written.update(a for a in args if not a.startswith("-"))

    paths = [
        a for a in args
        if SECRET_PATH.fullmatch(a) and a not in written
    ]

    # An interpreter given inline source reads whatever that source names, so
    # look inside the code string too — `python3 -c "open('.env').read()"`.
    if verb in INTERPRETERS and any(a in ("-c", "-e") for a in args):
        if any(SECRET_PATH.search(a) for a in args):
            return paths or [
                m.group(0) for a in args for m in SECRET_PATH.finditer(a)
            ]

    if not paths:
        return []

    joined = " ".join(seg)
    if verb in ("grep", "rg", "ag", "ack"):
        return [] if QUIET_GREP.search(joined) else paths
    if verb == "sed":
        return [] if SED_INPLACE.search(joined) else paths
    if verb in DUMP_VERBS:
        return paths
    return []


def should_block(command: str) -> str | None:
    """Return a refusal message, or None when the command is safe to run."""
    if not command:
        return None

    tokens = _tokenize(command)
    if tokens is None:
        # Unlexable (unbalanced quotes). Fall back to the blunt text scan
        # rather than waving it through: a command this guard cannot read is
        # exactly the one not to trust with a credential file.
        if SECRET_PATH.search(command) and DUMP.search(command):
            return ADVICE
        return None

    found: list[str] = []
    for seg in _segments(tokens):
        found.extend(_segment_reads_a_secret(seg))
    if not found:
        return None
    return f"{ADVICE}\n\nRefused command touched: {', '.join(sorted(set(found)))}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never fail a turn over an unparseable hook payload
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    refusal = should_block(command)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
