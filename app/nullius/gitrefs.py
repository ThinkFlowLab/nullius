"""What a git repository offers - its default branch, its branches and its
tags, each at its commit - read with ``git ls-remote``.

One lister for the picker that names a version of a repository: the Harbor a
recipe pins (``harbor/source.py``). It asks the repository itself, as this machine's git
would: with the machine's own credentials - an ssh key, a credential helper -
and never a token of its own, so a private repository lists when this
machine can clone it, and there is no API rate limit to meet. Nothing is
prompted for (``GIT_TERMINAL_PROMPT=0``, ssh in batch mode) and a repository
that does not answer within the timeout is said to, not waited on.

``ls-remote`` carries no dates, so the order is the names': tags by version,
newest first, an annotated tag read through to the commit it points at
(its ``^{}`` line); branches with the default first, then by name.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, List, Optional

GIT = "git"                 # the program; a test or a probe points it elsewhere
TIMEOUT = 30.0
LISTED = 50                 # the most of each kind a picker is given; the totals say how many there are
COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_URL = re.compile(r"^(?:https?|ssh|git|file)://\S+$")
_SCP = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:\S+$")
_GITHUB = re.compile(r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def url_of(text: str) -> Optional[str]:
    """The URL git is given for what a person typed: ``owner/name`` is that
    repository on GitHub over https; an https, ssh, git or file URL, or an
    scp-like ``user@host:path``, is taken as it is. Anything else - an
    option, a word with spaces - is None."""
    text = (text or "").strip()
    if not text or text.startswith("-") or any(c.isspace() or ord(c) < 32 for c in text):
        return None
    m = _SLUG.match(text)
    if m and not text.startswith("."):
        name = m.group(2)[:-4] if m.group(2).endswith(".git") else m.group(2)
        return f"https://github.com/{m.group(1)}/{name}.git"
    return text if _URL.match(text) or _SCP.match(text) else None


def label_of(url: str) -> str:
    """How a repository is named on a page: ``owner/name`` on GitHub, else its URL."""
    m = _GITHUB.match(url or "")
    return m.group(1) if m else url


def env() -> dict:
    """The environment git runs in: this process's, with no prompt possible."""
    out = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not out.get("GIT_SSH_COMMAND") and not out.get("GIT_SSH"):
        out["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return out


def run_git(args: List[str], timeout: float = TIMEOUT) -> tuple:
    """``git <args>``: (returncode, stdout, stderr); a missing git and a
    timeout are return codes of their own."""
    try:
        p = subprocess.run([GIT, *args], capture_output=True, text=True, timeout=timeout, env=env(), stdin=subprocess.DEVNULL)
    except OSError as e:
        return 127, "", f"{GIT}: {e.strerror or e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {int(timeout)} s"
    return p.returncode, p.stdout, p.stderr


def version_key(name: str):
    """A tag or a version name as something to sort by: its numbers as
    numbers, from the first digit on (``v1.10`` after ``v1.9``, ``harbor-0.3``
    beside ``0.3``), a pre-release before its release; a name with no digit
    sorts below every one that has one."""
    m = re.search(r"\d", name)
    if not m:
        return (0, [], name)
    parts = [(1, int(p)) if p.isdigit() else (0, p.lower()) for p in re.findall(r"\d+|[A-Za-z]+", name[m.start():])]
    return (1, parts + [(0.5, "")], name)


def parse(text: str) -> dict:
    """``git ls-remote --symref`` output as the default branch, the branches
    and the tags, each ``{name, commit}``, in the order a picker shows them."""
    default, branches, tags, peeled = "", {}, {}, {}
    for line in text.splitlines():
        left, _, ref = line.partition("\t")
        if left.startswith("ref: ") and ref.strip() == "HEAD":
            target = left[5:].strip()
            if target.startswith("refs/heads/"):
                default = target[len("refs/heads/"):]
            continue
        sha, ref = left.strip(), ref.strip()
        if not COMMIT.match(sha):
            continue
        if ref.startswith("refs/heads/"):
            branches[ref[len("refs/heads/"):]] = sha
        elif ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/"):]
            if name.endswith("^{}"):
                peeled[name[:-3]] = sha          # an annotated tag: the commit it names
            else:
                tags[name] = sha
    tags.update({k: v for k, v in peeled.items() if k in tags})
    order = sorted(branches, key=lambda n: (n != default, n))
    return {"default_branch": default,
            "branches": [{"name": n, "commit": branches[n]} for n in order],
            "tags": [{"name": n, "commit": tags[n]} for n in sorted(tags, key=version_key, reverse=True)]}


def ls_remote(url: str, run: Optional[Callable] = None, listed: int = LISTED) -> dict:
    """What ``url`` offers: ``{default_branch, branches, branches_total,
    tags, tags_total, error}``, at most ``listed`` of each kind. On any
    failure the lists are empty and ``error`` is git's own last word."""
    out = {"default_branch": "", "branches": [], "branches_total": 0, "tags": [], "tags_total": 0, "error": ""}
    if url_of(url) is None:
        return dict(out, error="not a repository URL")
    rc, text, err = (run or run_git)(["ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*"])
    if rc != 0:
        said = [l.strip() for l in (err or text or "").splitlines() if l.strip()]
        return dict(out, error=(said[-1] if said else f"git exited {rc}").removeprefix("fatal: "))
    got = parse(text)
    return dict(out, default_branch=got["default_branch"], branches=got["branches"][:listed], branches_total=len(got["branches"]),
                tags=got["tags"][:listed], tags_total=len(got["tags"]))
