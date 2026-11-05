#!/usr/bin/env python3
"""Judge a pull request. Experimental, never a gate.

    python3 .github/scripts/judge_pr.py --pr 97 --repo thinkflowlab/nullius --out-json j.json --out-md j.md
    python3 .github/scripts/judge_pr.py --issue-file issue.md --pr-file pr.md --diff-file pr.diff --dry

The comment opens with two ratings, then a table of questions:

- **Merge risk** (minor, medium, significant, major, severe): what happens if
  the change merges and is wrong, read by the judge over the diff with the
  script's own signals, and floored by the script: a deleted test, a key-shaped
  string or a workflow change is severe at least; a change under the
  standard, execution, hosts or secrets is major at least.
- **Priority** (low, medium, high, extreme): how deep the diff reaches,
  computed from its paths alone (`priority_of`).
- The questions, each asked k times (default 3) over one state of issue,
  description and diff: the acceptance criteria of the linked issue, one row
  per box of its `## Acceptance` list (a body without one is judged whole);
  whether the issue and the pull request describe the same change; whether
  the description matches the diff; whether the diff stays in scope; whether
  tests cover the change or the description says how it was verified; whether
  the docs follow; whether the risk is named. A pull request that links no
  issue reads `–` on the first two. The row "before and after shown" is the
  script's: when the diff touches what is visible, it counts the images under
  the description's `## Before and after` heading.
- Folded: the per-criterion table, and the checks the script runs itself over
  the diff and the commits (no machine attribution trailer, new code under
  `app/`, no Flask import under the backend package, no key-shaped string).

The pick for a question is the status with the highest *median* probability
over the k asks, undecided when the top two medians are closer than the
spread the asks showed. Nothing re-asks to make a pick appear. The judge
reads text, not behaviour: a `yes` says the change *looks like* it does what
is asked; the tests say whether it does. Exit status 0 whenever the judge
answered, whatever it answered; 2 for a usage error, 3 when it could not be
asked (no key, refused, unreachable). The workflow wraps the call with
`continue-on-error` and stays green either way.

What leaves this machine: the issue, the description, the commit messages
and the diff, after `redact` has replaced every key-shaped string and every
email address, so a secret or a person's address pasted into a pull request
reaches neither the judge nor the comment. The key is `TYPESAFE_API_KEY` in
the environment (`API_KEY_TYPESAFE`, the older spelling, is read too); it is
sent in one header and appears in no output, argv or record. Cost is
estimated from the state sizes before any call and refused above
`--max-usd`; it is kept in the record and the step log, never in the comment.

`--comment` posts the summary on the PR as a new comment on every run (the
timeline reads in order); the earlier ones, found by the `<!-- judge-pr -->`
marker, are collapsed as outdated, never deleted. With `--pr` everything comes
from the `gh` CLI (`GH_TOKEN` in CI): the linked issue is the PR's closing
reference or the first `Closes/Fixes/Resolves/Refs #n` in the body; `--issue`
overrides. `--issue-file`, `--pr-file` and `--diff-file` replace `gh` entirely
(a test, an offline run). Standard library only; nothing of the application
is imported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PROVIDER = "typesafe.ai"
QUESTIONS_VERSION = "judgement.questions.pr.v2"
PRICE_PER_MTOK = 0.042          # USD per million input tokens (docs.typesafe.ai/models)
CHARS_PER_TOKEN = 4
FILE_LIMIT = 12_000             # characters of one file's diff kept (--file-limit)
STATE_LIMIT = 60_000            # characters of one state, all in (--state-limit)
DESCRIPTION_LIMIT = 6_000
STATUSES = ("met", "partial", "not_met", "no_evidence")
ANSWERS = ("yes", "partly", "no")
RISKS = ("minor", "medium", "significant", "major", "severe")
PRIORITIES = ("low", "medium", "high", "extreme")

QUESTIONS = {
    "criterion_shown": {
        "type": "choice",
        "instructions": "Does the pull request (its description and its diff) show this acceptance criterion done?",
        "criteria": {
            "met": "The diff and description show the criterion done as written",
            "partial": "Part of the criterion is shown done; a named part is not",
            "not_met": "The change contradicts the criterion or leaves it undone",
            "no_evidence": "The change does not speak to this criterion",
        },
    },
    "criterion_met": {
        "type": "noul",
        "instructions": "The pull request shows this acceptance criterion done.",
        "criteria": {"true": "What the criterion asks for is visible in the diff or the tests it adds",
                     "false": "It is not visible, or the change is about something else"},
    },
    "issue_same": {
        "type": "choice",
        "instructions": "Do the issue and the pull request describe the same change: the same problem, the same scope?",
        "criteria": {"yes": "The pull request addresses the issue's problem at the issue's scope, no more and no less",
                     "partly": "It addresses the issue's problem but widens or narrows the scope, or solves it in a way the issue rules out",
                     "no": "It is about something else, or contradicts the issue"},
    },
    "description_matches": {
        "type": "choice",
        "instructions": "Does the pull request description describe the change the diff makes, claiming nothing the diff does not do and leaving out nothing the diff does?",
        "criteria": {"yes": "What the description says the change does, the diff does, and nothing material is left unsaid",
                     "partly": "The description is right as far as it goes but leaves out a material part of the diff, or overstates a detail",
                     "no": "The description claims a change the diff does not make, or describes a different change"},
    },
    "scope": {
        "type": "choice",
        "instructions": "Does the diff stay within what the issue (if any) and the description call for: no unrelated edit, no drive-by refactor, no change of another area?",
        "criteria": {"yes": "Every hunk serves the change described",
                     "partly": "A few hunks serve something else (a tidy-up, a rename elsewhere), small beside the change",
                     "no": "A substantial part of the diff is a different change"},
    },
    "tests": {
        "type": "choice",
        "instructions": "Is the changed behaviour covered: tests added or changed in the diff for it, or the description's Verified section saying what was run and checked?",
        "criteria": {"yes": "The diff has tests for the behaviour it changes, or the description states how it was verified",
                     "partly": "Some of the change is covered or verified, some is not",
                     "no": "No test touches the change and the description says nothing about verification, or says it was not verified"},
    },
    "docs": {
        "type": "choice",
        "instructions": "When the change alters a page, a command, a setting, a rule or a file layout, do the documentation and the repository's own guides (README, AGENTS.md, docs/) follow in the same diff?",
        "criteria": {"yes": "What changed for a reader or an operator is reflected in the documentation in this diff",
                     "partly": "Some of it is reflected, some surfaces still describe the old behaviour",
                     "no": "Something a reader or an operator sees changed and no documentation follows",
                     "none_needed": "Nothing a reader or an operator sees changed"},
    },
    "risk_named": {
        "type": "choice",
        "instructions": "Does the description name what could break if this change is wrong and how it would be undone (a revert, a rebuild, a migration): deleted tests, changed defaults, data on disk, keys?",
        "criteria": {"yes": "The description's Risk section names what could break and how it is undone, and it matches the diff",
                     "partly": "It names a risk but misses one the diff plainly carries, or says nothing about undoing it",
                     "no": "No risk is named, or the one named is not the diff's"},
    },
    "merge_risk": {
        "type": "choice",
        "instructions": "If this change merges and is wrong, how far does it reach and how easily is it undone? Weigh how many pages, runs or people feel it and whether one revert undoes it.",
        "criteria": {"minor": "Nobody notices if wrong: documentation, a comment, a test alone, a generated page; one revert",
                     "medium": "One page, one command or one catalogue entry misbehaves",
                     "significant": "Every user of a page or a capability feels it: shared script or style, the builder, the trial page, a Settings page",
                     "major": "Runs or results change for everyone: the classifier, the parser, a recipe, the relay, the standard's run",
                     "severe": "Data, keys or history can be lost or wrong: a write into a source tree, keys or secrets, deleted tests, a changed on-disk format, a workflow with write permissions"},
    },
}
PR_QUESTIONS = ("issue_same", "description_matches", "scope", "tests", "docs", "risk_named", "merge_risk")


class JudgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- what leaves this machine ---------------------------------------------------

KEY_SHAPED = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*['\"]?(?:bearer\s+)?[A-Za-z0-9_\-./+=]{16,})"
    r"|(?i:bearer\s+[A-Za-z0-9_\-.=]{20,}))")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
NOREPLY = re.compile(r"\b\d+\+[A-Za-z0-9-]+@users\.noreply\.github\.com\b")


def redact(text: str) -> str:
    """The text with every key-shaped string and every email address replaced,
    so a secret or a person's address pasted into a pull request goes no
    further than this machine. GitHub's own noreply addresses are kept: they
    name no person."""
    kept = {}

    def keep(m):
        kept[f"\x00{len(kept)}\x00"] = m.group(0)
        return list(kept)[-1]
    text = NOREPLY.sub(keep, text or "")
    text = KEY_SHAPED.sub("[redacted]", text)
    text = EMAIL.sub("[email]", text)
    for mark, original in kept.items():
        text = text.replace(mark, original)
    return text


# --- inputs -------------------------------------------------------------------

def sh(args: list) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def gh_pr(repo: str, number: int) -> dict:
    data = json.loads(sh(["gh", "pr", "view", str(number), "--repo", repo, "--json",
                          "number,title,body,headRefOid,baseRefName,url,commits"]))
    data["diff"] = sh(["gh", "pr", "diff", str(number), "--repo", repo])
    data["commit_messages"] = [c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in data.pop("commits", None) or []]
    owner, name = repo.split("/", 1)
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
         "{closingIssuesReferences(first:5){nodes{number}}}}}")
    try:
        refs = json.loads(sh(["gh", "api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={number}"]))
        data["closing"] = [n["number"] for n in refs["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]]
    except (subprocess.CalledProcessError, KeyError, ValueError):
        data["closing"] = []
    return data


def gh_issue(repo: str, number: int) -> dict:
    return json.loads(sh(["gh", "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url"]))


ISSUE_REF = re.compile(r"\b(?:closes|closed|fixes|fixed|resolves|resolved|refs?)\s*:?\s*#(\d+)", re.I)


def linked_issue(pr: dict) -> Optional[int]:
    if pr.get("closing"):
        return int(pr["closing"][0])
    m = ISSUE_REF.search(pr.get("body") or "")
    return int(m.group(1)) if m else None


def read_pr_file(path: str) -> dict:
    """A PR as a file: first line the title, the rest the body."""
    text = open(path, encoding="utf-8").read()
    title, _, body = text.partition("\n")
    return {"number": 0, "title": title.strip().lstrip("# ").strip(), "body": body.strip(), "headRefOid": "", "baseRefName": "", "url": "",
            "commit_messages": []}


def read_issue_file(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    title, _, body = text.partition("\n")
    return {"number": 0, "title": title.strip().lstrip("# ").strip(), "body": body.strip(), "url": ""}


# --- the issue's criteria, the description's sections -----------------------------

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
CONTINUATION = re.compile(r"^\s{2,}\S")


def _list_items(lines: list) -> list:
    items: list = []
    for line in lines:
        b = BULLET.match(line)
        if b:
            items.append(b.group(1))
        elif items and CONTINUATION.match(line):
            items[-1] += " " + line.strip()
        elif items and not line.strip():
            continue
        elif items:
            break
    return items


def section(body: str, name: str) -> Optional[str]:
    """The text under the heading `name` (any level, case-insensitive), up to
    the next heading of the same or a higher level; None without one."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        h = HEADING.match(line)
        if h and h.group(2).strip().lower() == name.lower():
            level = len(h.group(1))
            out = []
            for l in lines[i + 1:]:
                h2 = HEADING.match(l)
                if h2 and len(h2.group(1)) <= level:
                    break
                out.append(l)
            return "\n".join(out).strip()
    return None


def criteria_of(body: str) -> tuple:
    """(criteria, source). An 'Acceptance' heading's list first; else the
    sentences of a paragraph that opens with 'Acceptance'; else the whole body
    as one criterion. Wrapped list items are joined."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        h = HEADING.match(line)
        if h and "acceptance" in h.group(2).lower():
            rest = []
            for l in lines[i + 1:]:
                if HEADING.match(l):
                    break
                rest.append(l)
            items = _list_items(rest)
            if items:
                return items, "acceptance heading"
    para = re.search(r"^\**\s*Acceptance[.:*]*\**[.:]?\s*(.+?)(?:\n\s*\n|\Z)", body or "", re.M | re.S)
    if para:
        text = " ".join(para.group(1).split())
        sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z`])", text) if len(s.strip()) >= 20]
        if sentences:
            return sentences, "acceptance paragraph"
    whole = " ".join((body or "").split())
    return ([whole] if whole else ["(the issue has no body)"]), "whole issue"


# --- what the script reads off the diff itself ------------------------------------

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.M)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*>|https?://\S+\.(?:png|jpe?g|gif|webp|svg)\b", re.I)
TRAILER = re.compile(r"^\s*(?:Co-Authored-By|Co-authored-by):\s*(?:claude|anthropic|chatgpt|openai|gpt|copilot|gemini|codex|cursor|devin|assistant)\b"
                     r"|^\s*(?:Claude-Session|Generated-by|Generated by|Signed-off-by: Claude)", re.I | re.M)
ALLOWED_ROOTS = ("app/", "docs/", ".github/", ".claude/")
ALLOWED_ROOT_FILES = ("README.md", "AGENTS.md", ".gitignore", ".gitattributes", "LICENSE", "LICENCE")
VISIBLE = ("app/templates/", "app/static/", "docs/field-guide.md", "docs/assets/", "docs/diagrams/", "app/site/assets/", "app/site/overrides/")
PRIORITY_PATHS = [   # the first match decides; deepest first
    ("extreme", ("app/nullius/standard/", "app/nullius/execution.py", "app/nullius/hosts.py", "app/nullius/secrets.py", ".github/workflows/", ".github/scripts/")),
    ("high", ("app/nullius/",)),
    ("medium", ("app/web/", "app/nullius/catalogue/", ".claude/", "app/app.py", "app/requirements.txt")),
    ("low", ()),
]
MAJOR_FLOOR = ("app/nullius/standard/", "app/nullius/execution.py", "app/nullius/hosts.py", "app/nullius/secrets.py",
               "app/nullius/runs/classify.py", "app/nullius/runs/parse.py", "app/nullius/standard/relay.py")


def split_diff(diff: str) -> list:
    """[(path, text)] in file order."""
    heads = list(DIFF_HEADER.finditer(diff))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(diff)
        out.append((h.group(2), diff[h.start():end].rstrip("\n")))
    return out


def is_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return base.startswith("test_") or base.endswith("_test.py") or "/tests/" in f"/{path}" or "/ui_tests/" in f"/{path}"


def priority_of(paths: list) -> tuple:
    """(level, the path that set it): how deep the diff reaches, from its paths
    alone. A catalogue entry is medium though it sits under the backend; a
    test counts as the area it sits in, so a test alone is low."""
    best, by = "low", ""
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    for path in paths:
        level = "low"
        if is_test_path(path):
            pass
        elif path.startswith("app/nullius/catalogue/"):
            level = "medium"
        else:
            for name, prefixes in PRIORITY_PATHS:
                if any(path.startswith(p) for p in prefixes):
                    level = name
                    break
        if rank[level] > rank[best]:
            best, by = level, path
    return best, by


def signals_of(diff: str, pr: dict) -> dict:
    """What the script reads off the diff and the commits without a model:
    the paths and their areas, the checks, the floors on merge risk, the
    before-and-after images."""
    files = split_diff(diff)
    paths = [p for p, _ in files]
    added = {p: [l[1:] for l in t.splitlines() if l.startswith("+") and not l.startswith("+++")] for p, t in files}
    deleted_tests = [p for p, t in files if is_test_path(p) and "\n+++ /dev/null" in t]
    secrets = [p for p, lines in added.items() if any(KEY_SHAPED.search(l) for l in lines)]
    workflows = [p for p in paths if p.startswith(".github/workflows/")]
    outside = [p for p in paths if not p.startswith(ALLOWED_ROOTS) and p not in ALLOWED_ROOT_FILES and "/dev/null" not in p]
    flask = [p for p, lines in added.items() if p.startswith("app/nullius/") and p.endswith(".py")
             and any(re.match(r"\s*(?:import flask|from flask\b)", l) for l in lines)]
    trailers = [m.group(0).strip() for text in [pr.get("body") or ""] + list(pr.get("commit_messages") or []) for m in TRAILER.finditer(text)]
    visible = [p for p in paths if p.startswith(VISIBLE)]
    shots = section(pr.get("body") or "", "Before and after")
    images = len(IMAGE.findall(shots or ""))
    floor, why = None, ""
    if secrets:
        floor, why = "severe", f"a key-shaped string in {secrets[0]}"
    elif deleted_tests:
        floor, why = "severe", f"a test file deleted: {deleted_tests[0]}"
    elif workflows:
        floor, why = "severe", f"a workflow changed: {workflows[0]}"
    else:
        hit = next((p for p in paths if p.startswith(MAJOR_FLOOR)), None)
        if hit:
            floor, why = "major", f"{hit} is in the path of every run"
    priority, by = priority_of(paths)
    checks = [
        {"name": "No machine attribution trailer in the commits or the description", "ok": not trailers, "detail": trailers[0] if trailers else ""},
        {"name": "New code under app/, docs/, .github/ or .claude/", "ok": not outside, "detail": ", ".join(outside[:3])},
        {"name": "No Flask import under the backend package", "ok": not flask, "detail": ", ".join(flask[:3])},
        {"name": "No key-shaped string in the diff", "ok": not secrets, "detail": ", ".join(secrets[:3])},
    ]
    return {"paths": paths, "files": len(files), "tests_in_diff": [p for p in paths if is_test_path(p)], "deleted_tests": deleted_tests,
            "visible": visible, "images": images, "priority": priority, "priority_by": by,
            "risk_floor": floor, "risk_floor_why": why, "checks": checks}


def signals_block(sig: dict) -> str:
    """The signals as the judge sees them, after the diff."""
    lines = [f"PATHS ({sig['files']}): " + ", ".join(sig["paths"][:60]) + (" …" if sig["files"] > 60 else ""),
             f"TESTS IN DIFF: {', '.join(sig['tests_in_diff']) or 'none'}",
             f"DELETED TESTS: {', '.join(sig['deleted_tests']) or 'none'}",
             f"VISIBLE PATHS: {', '.join(sig['visible']) or 'none'}; images under 'Before and after': {sig['images']}",
             f"PRIORITY FROM PATHS: {sig['priority']}" + (f" ({sig['priority_by']})" if sig["priority_by"] else "")]
    if sig["risk_floor"]:
        lines.append(f"MERGE RISK FLOOR: {sig['risk_floor']}: {sig['risk_floor_why']}")
    return "SIGNALS:\n" + "\n".join(lines)



def diff_block(diff: str, budget: int, file_limit: int = None) -> tuple:
    """The diff as the judge sees it: test files first, each file cut at
    FILE_LIMIT, the whole cut at `budget`; files that did not fit are named.
    Returns (text, truncated)."""
    file_limit = FILE_LIMIT if file_limit is None else file_limit
    files = split_diff(diff)
    files.sort(key=lambda f: (0 if is_test_path(f[0]) else 1))
    parts, used, truncated, left = [], 0, False, []
    for path, text in files:
        if len(text) > file_limit:
            text = text[:file_limit] + f"\n[… {len(text) - file_limit} more characters of this file]"
            truncated = True
        if used + len(text) > budget:
            left.append(path)
            continue
        parts.append(text)
        used += len(text) + 1
    if left:
        truncated = True
        parts.append(f"[diff omitted for {len(left)} more file(s): " + ", ".join(left) + "]")
    head = f"DIFF ({len(files)} files, tests first{', cut' if truncated else ''}):\n"
    return head + "\n".join(parts), truncated


def description_block(pr: dict) -> tuple:
    body = (pr.get("body") or "").strip() or "(no description)"
    truncated = len(body) > DESCRIPTION_LIMIT
    if truncated:
        body = body[:DESCRIPTION_LIMIT] + f"\n[… {len(body) - DESCRIPTION_LIMIT} more characters]"
    return f"PULL REQUEST #{pr.get('number', 0)}: {pr.get('title', '')}\nDESCRIPTION:\n{body}", truncated


def issue_block(issue: Optional[dict]) -> str:
    if not issue:
        return "ISSUE: none linked"
    body = (issue.get("body") or "").strip() or "(no body)"
    return f"ISSUE #{issue.get('number', 0)}: {issue.get('title', '')}\n{body[:DESCRIPTION_LIMIT]}"


def criterion_state(issue: dict, n: int, criterion: str, pr: dict, diff: str, state_limit: int = None, file_limit: int = None) -> tuple:
    state_limit = STATE_LIMIT if state_limit is None else state_limit
    head = f"ISSUE #{issue.get('number', 0)}: {issue.get('title', '')}\nACCEPTANCE CRITERION {n}:\n{criterion}\n\n"
    desc, t1 = description_block(pr)
    budget = state_limit - len(head) - len(desc) - 2
    block, t2 = diff_block(diff, max(budget, 2_000), file_limit)
    return head + desc + "\n\n" + block, (t1 or t2)


def pr_state(issue: Optional[dict], pr: dict, diff: str, sig: dict, state_limit: int = None, file_limit: int = None) -> tuple:
    """One state for every question about the pull request as a whole: the
    issue, the description, the signals, then the diff in what room is left."""
    state_limit = STATE_LIMIT if state_limit is None else state_limit
    head = issue_block(issue) + "\n\n"
    desc, t1 = description_block(pr)
    signals = signals_block(sig) + "\n\n"
    budget = state_limit - len(head) - len(desc) - len(signals) - 2
    block, t2 = diff_block(diff, max(budget, 2_000), file_limit)
    return head + desc + "\n\n" + signals + block, (t1 or t2)



def http_post(url: str, body: dict, key: str, timeout: float = 90.0) -> tuple:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            detail = {}
        return e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(0, f"no answer from the judge: {getattr(e, 'reason', e)}") from e


def request_questions(ids) -> dict:
    return {q: {k: QUESTIONS[q][k] for k in ("type", "instructions", "criteria")} for q in ids}


def ask(state: str, ids, key: str, post: Callable = http_post, model: str = MODEL, retries: int = 3,
        sleep: Callable = time.sleep) -> dict:
    """One call; the typed answers plus the model that answered and the usage."""
    if not key:
        raise JudgeError(401, "no TypeSafe key: TYPESAFE_API_KEY is not set")
    body = {"state": state, "model": model, "questions": request_questions(ids)}
    status, data = 0, {}
    for attempt in range(retries + 1):
        status, data = post(URL, body, key)
        if status in (429, 529) and attempt < retries:
            sleep(1.5 * (2 ** attempt))
            continue
        break
    if status == 401:
        raise JudgeError(401, "the judge refused the key (401)")
    if status == 422:
        raise JudgeError(422, "the judge refused the request (422): " + json.dumps(data)[:300])
    if status != 200:
        raise JudgeError(status, f"the judge answered {status}")
    answers = {}
    for qid, a in (data.get("answers") or {}).items():
        if qid not in QUESTIONS or not isinstance(a, dict):
            continue
        if QUESTIONS[qid]["type"] == "noul" and isinstance(a.get("noul"), (int, float)):
            answers[qid] = {"noul": float(a["noul"])}
        elif QUESTIONS[qid]["type"] == "choice" and isinstance(a.get("choice"), str):
            answers[qid] = {"choice": a["choice"],
                            "probabilities": {k: float(v) for k, v in (a.get("probabilities") or {}).items()},
                            "confidence": float(a["confidence"]) if isinstance(a.get("confidence"), (int, float)) else None}
    usage = data.get("usage") or {}
    return {"answers": answers, "model": str(data.get("model") or model),
            "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}}


def estimate(chars: int, calls: int) -> dict:
    tokens = chars // CHARS_PER_TOKEN + calls
    return {"calls": calls, "input_tokens": tokens, "usd": round(tokens * PRICE_PER_MTOK / 1_000_000, 6)}



def decide_choice(asks: list, qid: str, statuses: tuple) -> dict:
    """The k-ask rule over a choice question: medians per status, the pick is
    the highest median, undecided when the top two are closer than the
    spread (the widest max−min any status showed across the asks)."""
    probs = [a["answers"][qid]["probabilities"] for a in asks if qid in a["answers"]]
    if not probs:
        return {"pick": "unanswered", "runner_up": None, "medians": {}, "spread": None, "undecided": True, "confidence": None}
    medians = {s: round(statistics.median(p.get(s, 0.0) for p in probs), 3) for s in statuses}
    spread = round(max(max(p.get(s, 0.0) for p in probs) - min(p.get(s, 0.0) for p in probs) for s in statuses), 3)
    ranked = sorted(statuses, key=lambda s: -medians[s])
    pick, runner = ranked[0], ranked[1]
    undecided = (medians[pick] - medians[runner]) < spread and len(probs) > 1
    confs = [a["answers"][qid]["confidence"] for a in asks if qid in a["answers"] and a["answers"][qid].get("confidence") is not None]
    return {"pick": pick, "runner_up": runner, "medians": medians, "spread": spread, "undecided": undecided,
            "confidence": round(statistics.median(confs), 3) if confs else None}


def decide(asks: list) -> dict:
    """The criterion rule: `criterion_shown` as a choice, `criterion_met` as the paired noul."""
    out = decide_choice(asks, "criterion_shown", STATUSES)
    nouls = [a["answers"]["criterion_met"]["noul"] for a in asks if "criterion_met" in a["answers"]]
    out["met_p"] = round(statistics.median(nouls), 3) if nouls else None
    out["met_p_spread"] = round(max(nouls) - min(nouls), 3) if nouls else None
    return out


def verdict_of(dc: dict) -> str:
    if dc["pick"] == "unanswered":
        return "unanswered"
    return "undecided" if dc["undecided"] else dc["pick"]


def risk_of(dc: dict, sig: dict) -> tuple:
    """(level, why): the judge's pick, raised to the script's floor, never lowered."""
    pick = verdict_of(dc)
    judged = pick if pick in RISKS else None
    floor = sig.get("risk_floor")
    if floor and (judged is None or RISKS.index(floor) > RISKS.index(judged)):
        return floor, sig["risk_floor_why"]
    if judged is None:
        return ("undecided", "the judge gave no clear reading") if pick == "undecided" else ("not asked", "no call was made")
    return judged, "the judge's reading of the diff" + (f"; {sig['risk_floor_why']}" if floor else "")



def short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


VERDICT = {"met": "✅ met", "partial": "⚠️ partial", "not_met": "❌ unmet", "no_evidence": "❌ no evidence",
           "yes": "✅ yes", "partly": "⚠️ partly", "no": "❌ no", "none_needed": "–",
           "undecided": "❔ undecided", "unanswered": "not asked", "na": "–"}
RISK_MARK = {"minor": "🟢", "medium": "🟡", "significant": "🟠", "major": "🔴", "severe": "⛔", "undecided": "❔", "not asked": "⚪"}
PRIORITY_MARK = {"low": "🟢", "medium": "🟡", "high": "🟠", "extreme": "🔴"}
ROWS = [  # (question id or script row, the text of the row)
    ("criteria", "Changes do what the issue asks"),
    ("issue_same", "Issue and pull request describe the same change"),
    ("description_matches", "Description matches the diff, no more, no less"),
    ("scope", "Diff stays in scope"),
    ("tests", "Tests cover the change, or Verified says how"),
    ("docs", "Docs follow the change"),
    ("risk_named", "Risk named in the description"),
    ("before_after", "Before and after shown"),
]


def rows_of(rec: dict) -> list:
    """[(text, verdict key, note)] for the questions table."""
    no_issue = rec["criteria_source"] == "no linked issue"
    verdicts = [verdict_of(r["decision"]) for r in rec["criteria"]]
    met = sum(1 for v in verdicts if v == "met")
    out = []
    for qid, text in ROWS:
        if qid == "criteria":
            if no_issue:
                out.append((text, "na", ""))
            elif rec.get("dry"):
                out.append((text, "unanswered", f"{len(verdicts)} criteria"))
            else:
                v = "yes" if met == len(verdicts) else "no" if any(x in ("not_met", "no_evidence", "unanswered") for x in verdicts) else "partly"
                out.append((text, v, f"{met} of {len(verdicts)} criteria"))
        elif qid == "issue_same" and no_issue:
            out.append((text, "na", ""))
        elif qid == "before_after":
            sig = rec["signals"]
            if not sig["visible"]:
                out.append((text, "na", ""))
            else:
                out.append((text, "yes" if sig["images"] >= 2 else "partly" if sig["images"] == 1 else "no",
                            f"{sig['images']} image{'s' if sig['images'] != 1 else ''} · the diff touches what is visible"))
        else:
            dc = rec["questions"].get(qid)
            out.append((text, verdict_of(dc) if dc else "unanswered", ""))
    return out


def light_of(rec: dict) -> str:
    if rec.get("dry"):
        return "⚪"
    risk = rec["merge_risk"]["level"]
    verdicts = [v for _, v, _ in rows_of(rec)]
    if risk == "severe" or any(v in ("no", "unanswered") for v in verdicts):
        return "🔴"
    if risk == "major" or any(v in ("partly", "undecided") for v in verdicts):
        return "🟡"
    return "🟢"


def summary_md(rec: dict) -> str:
    """The PR comment and the step summary, one text: the light and the count
    in the header, the two ratings, the questions, then the per-criterion
    table and the script's checks, folded. No cost, no model, no counts of
    calls: those stay in the record."""
    pr, issue = rec["pr"], rec["issue"]
    no_issue = rec["criteria_source"] == "no linked issue"
    rows = rows_of(rec)
    answered = [v for _, v, _ in rows if v != "na"]
    yes = sum(1 for v in answered if v == "yes")
    light = light_of(rec)
    link = f"#{pr['number']}, no linked issue" if no_issue else f"#{pr['number']} ↔ #{issue['number']}"
    head = f"PR judge · dry run · {link}" if rec.get("dry") else f"PR judge · {yes} of {len(answered)} yes · {link}"
    lines = [f"## {light} {head}", ""]
    risk, prio, sig = rec["merge_risk"], rec["priority"], rec["signals"]
    lines += ["| | | |", "| --- | --- | --- |",
              f"| **Merge risk** | {RISK_MARK.get(risk['level'], '❔')} **{risk['level'].upper()}** | {risk['why']} |",
              f"| **Priority** | {PRIORITY_MARK.get(prio['level'], '❔')} **{prio['level'].upper()}** | {prio['why']} |", ""]
    lines += ["| Question | Verdict |", "| --- | :---: |"]
    for text, v, note in rows:
        lines.append(f"| {text}{' · ' + note if note else ''} | {VERDICT.get(v, v)} |")
    lines.append("")
    crit = rec["criteria"]
    if crit:
        met = sum(1 for r in crit if verdict_of(r["decision"]) == "met")
        title = f"Acceptance criteria, {len(crit)}" if rec.get("dry") else f"Acceptance criteria, {met} of {len(crit)} met"
        lines += ["<details>", f"<summary>{title}</summary>", "", "| # | Criterion | Verdict |", "| ---: | --- | :---: |"]
        for r in crit:
            v = verdict_of(r["decision"])
            lines.append(f"| {r['n']} | {short(r['text'], 120)} | {VERDICT[v]}{' (cut)' if r['truncated'] else ''} |")
        if rec["criteria_source"] == "whole issue":
            lines += ["", "The issue has no `## Acceptance` list, so it was judged whole."]
        lines += ["", "</details>", ""]
    passed = sum(1 for c in sig["checks"] if c["ok"])
    lines += ["<details>", f"<summary>Checks the script runs itself, {passed} of {len(sig['checks'])} pass</summary>", "", "| | |", "| --- | :---: |"]
    for c in sig["checks"]:
        lines.append(f"| {c['name']}{' · ' + c['detail'] if c['detail'] and not c['ok'] else ''} | {'✓' if c['ok'] else '✗'} |")
    lines += ["", "</details>"]
    if any(r["truncated"] for r in crit) or rec.get("pr_state_truncated"):
        lines += ["", "<sub>some of the diff was cut before the judge read it</sub>"]
    return "\n".join(lines)


COMMENT_MARKER = "<!-- judge-pr -->"


def post_comment(repo: str, number: int, body: str, run: Callable = sh) -> str:
    """A new comment on every run, so the timeline reads in order; the
    earlier judge comments (found by the marker) are collapsed as outdated,
    never deleted, so an old link still resolves. Returns "created" or
    "created, N earlier collapsed". Raises subprocess.CalledProcessError when
    the post is refused (no write permission is the usual reason); a refused
    collapse is ignored, the new comment is what matters."""
    full = COMMENT_MARKER + "\n" + body
    earlier = json.loads(run(["gh", "api", f"repos/{repo}/issues/{number}/comments", "--paginate", "--jq", "[.[] | {node_id, body}]"]) or "[]")
    if isinstance(earlier, list) and earlier and isinstance(earlier[0], list):
        earlier = [c for page in earlier for c in page]
    earlier = [c for c in earlier if COMMENT_MARKER in (c.get("body") or "")]
    run(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={full}"])
    collapsed = 0
    for c in earlier:
        try:
            run(["gh", "api", "graphql", "-f", "query=mutation($id:ID!){minimizeComment(input:{subjectId:$id,classifier:OUTDATED}){minimizedComment{isMinimized}}}",
                 "-f", f"id={c['node_id']}"])
            collapsed += 1
        except subprocess.CalledProcessError:
            pass
    return "created" + (f", {collapsed} earlier collapsed" if collapsed else "")


def run(issue: Optional[dict], pr: dict, diff: str, key: str, k: int = 3, max_usd: float = 0.10, dry: bool = False,
        post: Callable = http_post, model: str = MODEL, sleep: Callable = time.sleep,
        state_limit: int = None, file_limit: int = None) -> dict:
    """The record. `issue` None is a pull request that links no issue: no
    criteria, no question about the issue. Everything the judge reads has
    been through `redact` first."""
    pr = dict(pr, title=redact(pr.get("title", "")), body=redact(pr.get("body") or ""),
              commit_messages=[redact(m) for m in pr.get("commit_messages") or []])
    diff = redact(diff)
    if issue is None:
        criteria, source = [], "no linked issue"
        issue_r = None
        issue = {"number": 0, "title": "", "url": ""}
    else:
        issue = dict(issue, title=redact(issue.get("title", "")), body=redact(issue.get("body") or ""))
        issue_r = issue
        criteria, source = criteria_of(issue.get("body") or "")
    sig = signals_of(diff, pr)
    rows = []
    for n, text in enumerate(criteria, start=1):
        state, truncated = criterion_state(issue, n, text, pr, diff, state_limit, file_limit)
        rows.append({"n": n, "text": text, "state": state, "state_chars": len(state), "truncated": truncated})
    pstate, ptrunc = pr_state(issue_r, pr, diff, sig, state_limit, file_limit)
    asked = [q for q in PR_QUESTIONS if q != "issue_same" or issue_r is not None]
    chars = sum(r["state_chars"] for r in rows) * k + len(pstate) * k
    est = estimate(chars, calls=(len(rows) + 1) * k)
    rec = {"questions_version": QUESTIONS_VERSION, "judge": {"provider": PROVIDER, "model": None, "requested": model},
           "asked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "pr": {"number": pr.get("number", 0), "title": pr.get("title", ""), "head": pr.get("headRefOid", ""),
                  "base": pr.get("baseRefName", ""), "url": pr.get("url", "")},
           "issue": {"number": issue.get("number", 0), "title": issue.get("title", ""), "url": issue.get("url", "")},
           "k": k, "criteria_source": source, "estimate": est, "dry": dry,
           "limits": {"state_chars": STATE_LIMIT if state_limit is None else state_limit, "file_chars": FILE_LIMIT if file_limit is None else file_limit},
           "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0},
           "signals": sig, "priority": {"level": sig["priority"], "why": _priority_why(sig)},
           "criteria": [], "questions": {}, "pr_state_chars": len(pstate), "pr_state_truncated": ptrunc, "pr_asks": []}
    if not dry and est["usd"] > max_usd:
        raise JudgeError(2, f"estimate ${est['usd']:.4f} is above --max-usd {max_usd}; raise it or lower k")

    def count(a):
        rec["judge"]["model"] = a["model"]
        rec["usage"]["calls"] += 1
        rec["usage"]["input_tokens"] += a["usage"]["input_tokens"] or 0
        rec["usage"]["output_tokens"] += a["usage"]["output_tokens"] or 0

    for r in rows:
        asks = []
        if not dry:
            for _ in range(k):
                a = ask(r["state"], ("criterion_shown", "criterion_met"), key, post=post, model=model, sleep=sleep)
                asks.append(a)
                count(a)
        rec["criteria"].append({k2: r[k2] for k2 in ("n", "text", "state_chars", "truncated")} | {"asks": asks, "decision": decide(asks)})
    if not dry:
        for _ in range(k):
            a = ask(pstate, asked, key, post=post, model=model, sleep=sleep)
            rec["pr_asks"].append(a)
            count(a)
    for q in asked:
        rec["questions"][q] = decide_choice(rec["pr_asks"], q, RISKS if q == "merge_risk" else tuple(QUESTIONS[q]["criteria"]))
    level, why = risk_of(rec["questions"]["merge_risk"], sig)
    rec["merge_risk"] = {"level": level, "why": why}
    rec["usage"]["usd"] = round((rec["usage"]["input_tokens"] or 0) * PRICE_PER_MTOK / 1_000_000, 6)
    rec["summary_md"] = summary_md(rec)
    return rec


def _priority_why(sig: dict) -> str:
    if sig["priority_by"]:
        return f"the deepest path is `{sig['priority_by']}`"
    return "no path reaches past the surface" if sig["paths"] else "an empty diff"



def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pr", type=int, help="pull request number (read with gh)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name for --pr and --issue")
    ap.add_argument("--issue", type=int, help="issue number; default: the PR's closing reference or the first Closes/Refs #n in its body")
    ap.add_argument("--issue-file", help="the issue as a file: first line the title, the rest the body; without one the PR is judged as linking no issue")
    ap.add_argument("--pr-file", help="the PR as a file: first line the title, the rest the description")
    ap.add_argument("--diff-file", help="the diff as a file (git diff / gh pr diff output)")
    ap.add_argument("--k", type=int, default=3, help="asks per state (default 3)")
    ap.add_argument("--max-usd", type=float, default=0.10, help="refuse above this estimate (default 0.10)")
    ap.add_argument("--state-limit", type=int, default=STATE_LIMIT, help=f"characters per state (default {STATE_LIMIT})")
    ap.add_argument("--file-limit", type=int, default=FILE_LIMIT, help=f"characters per file in the diff (default {FILE_LIMIT})")
    ap.add_argument("--dry", action="store_true", help="build the states and the record; ask nothing")
    ap.add_argument("--out-json", help="write the record here")
    ap.add_argument("--out-md", help="write the summary here")
    ap.add_argument("--quiet", action="store_true", help="do not print the summary")
    ap.add_argument("--comment", action="store_true", help="post the summary on the PR as a new comment, earlier ones collapsed as outdated (needs --pr; gh with pull-requests: write)")
    args = ap.parse_args(argv)

    try:
        if args.pr_file and args.diff_file:
            pr, diff = read_pr_file(args.pr_file), open(args.diff_file, encoding="utf-8").read()
            issue = read_issue_file(args.issue_file) if args.issue_file else None
        elif args.pr:
            if not args.repo:
                print("judge_pr: --repo owner/name is required with --pr (or GITHUB_REPOSITORY)", file=sys.stderr)
                return 2
            pr = gh_pr(args.repo, args.pr)
            diff = pr.pop("diff")
            if args.issue_file:
                issue = read_issue_file(args.issue_file)
            else:
                n = args.issue or linked_issue(pr)
                issue = gh_issue(args.repo, n) if n else None
        else:
            print("judge_pr: pass --pr N --repo owner/name, or --pr-file and --diff-file (and --issue-file when there is an issue)", file=sys.stderr)
            return 2
    except subprocess.CalledProcessError as e:
        print(f"judge_pr: gh failed: {redact((e.stderr or '').strip()[:300])}", file=sys.stderr)
        return 2

    key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("API_KEY_TYPESAFE", "")
    try:
        rec = run(issue, pr, diff, key, k=args.k, max_usd=args.max_usd, dry=args.dry, state_limit=args.state_limit, file_limit=args.file_limit)
    except JudgeError as e:
        print(f"judge_pr: {e}", file=sys.stderr)
        return 3
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, ensure_ascii=False)
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as f:
            f.write(rec["summary_md"] + "\n")
    note = ""
    if args.comment:
        if not args.pr:
            note = "Comment not posted: --comment needs --pr."
        else:
            try:
                what = post_comment(args.repo, args.pr, rec["summary_md"])
                note = f"Comment {what} on PR #{args.pr}."
            except subprocess.CalledProcessError as e:
                note = f"Comment not posted: gh refused ({redact((e.stderr or '').strip()[:200]) or 'no detail'}). Needs pull-requests: write."
        print(f"judge_pr: {note}", file=sys.stderr)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        u = rec["usage"]
        with open(step, "a", encoding="utf-8") as f:
            f.write(rec["summary_md"] + "\n\n" + f"<sub>{rec['judge']['model'] or 'no call'} · k={rec['k']} · {u['calls']} calls · "
                    f"{u['input_tokens'] or 0:,} tokens · ≈${u['usd']:.4f}</sub>\n" + (note + "\n" if note else ""))
    if not args.quiet:
        print(rec["summary_md"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
