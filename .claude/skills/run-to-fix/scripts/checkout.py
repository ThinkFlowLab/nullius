"""The agent's git checkout: refs, diffs, and detached worktrees under the loop folder."""
from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
from typing import Dict, Iterator, List

from rounds import LoopError, fill


def git(checkout: str, args: List[str]) -> str:
    p = subprocess.run(["git", "-C", checkout] + args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise LoopError(f"git {' '.join(args)} in {checkout} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def resolve(checkout: str, ref: str) -> str:
    """The full commit id of `ref` in the checkout."""
    p = subprocess.run(["git", "-C", checkout, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0 or not p.stdout.strip():
        raise LoopError(f"{ref!r} is not a commit of {checkout}")
    return p.stdout.strip()


def is_ancestor(checkout: str, older: str, newer: str) -> bool:
    p = subprocess.run(["git", "-C", checkout, "merge-base", "--is-ancestor", older, newer],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode not in (0, 1):
        raise LoopError(f"git merge-base in {checkout} failed: {p.stderr.strip()}")
    return p.returncode == 0


def diff_size(checkout: str, old: str, new: str) -> Dict[str, int]:
    """Files and lines a commit range changes, from `git diff --numstat` (a binary file counts no lines)."""
    files, added, removed = 0, 0, 0
    for line in git(checkout, ["diff", "--numstat", old, new]).splitlines():
        a, r, _ = line.split("\t", 2)
        files += 1
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    return {"files": files, "added": added, "removed": removed}


@contextlib.contextmanager
def worktree(checkout: str, loop_dir: str, sha: str) -> Iterator[str]:
    """A detached worktree of `sha` under <loop folder>/worktrees/, removed when the block ends, on failure too."""
    base = os.path.join(loop_dir, "worktrees")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{sha[:12]}-{os.getpid()}")
    git(checkout, ["worktree", "add", "--detach", path, sha])
    try:
        yield path
    finally:
        git(checkout, ["worktree", "remove", "--force", path])


def write_paid_on_ref(path: str, checkout: str, loop_dir: str, sha: str, line: str, values: Dict[str, str]) -> None:
    """A paid command that needs a worktree, written as a script that makes the worktree, runs it, and removes it."""
    tree = os.path.join(loop_dir, "worktrees", f"{sha[:12]}-paid")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    q = shlex.quote
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/usr/bin/env bash\nset -euo pipefail\n")
        fh.write(f"git -C {q(checkout)} worktree add --detach {q(tree)} {q(sha)}\n")
        cleanup = f"git -C {q(checkout)} worktree remove --force {q(tree)}"
        fh.write(f"trap {q(cleanup)} EXIT\n")
        fh.write(f"cd {q(tree)}\n{fill(line, dict(values, worktree=tree))}\n")
