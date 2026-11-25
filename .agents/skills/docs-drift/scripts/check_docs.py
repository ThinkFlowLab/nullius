"""Does the documentation still describe the application?

The mechanical half of the /docs-drift skill, run by hand. It checks the things that rot
silently when files move — links that no longer resolve, paths named in prose
that no longer exist — and nothing that needs judgement.

    python3 .agents/skills/docs-drift/scripts/check_docs.py

Prints every finding and exits 1 if there are any.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

# Paths intentionally absent from a checkout, with the reason for each.
KNOWN_ABSENT = {
    "app/nullius/catalogue/agents/my-agent/agent.json":
        "docs/agents/add-manually.md: created by the preceding worked-copy command",
    "app/nullius/catalogue/benchmarks/my-benchmark/standard.json":
        "docs/benchmarks/add-manually.md: created by the preceding new benchmark command",
    "app/nullius/catalogue/capabilities/my-memory/capability.py":
        "docs/capabilities/add-manually.md: created by the preceding new capability command",
    "app/nullius/catalogue/capabilities/my-memory/capability.json":
        "docs/capabilities/add-manually.md: created by the preceding new capability command",
    ".claude/environment.local.md":
        "AGENTS.md: gitignored and per checkout, so absent by design",
    "app/.env":
        ".claude/rules/running.md: gitignored credentials file, absent by design",
}

# Things that look like repository paths but are not: trial-directory sidecars
# inside a job tree.
NOT_REPO_PATHS = (
    re.compile(r"^agent/[\w.-]+\.(json|jsonl|txt)$"),
    re.compile(r"^steps/"),
    re.compile(r"^verifier/"),
    re.compile(r"^user-agent/"),
)

MD_PATH = re.compile(r"`([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)/?`")
HREF = re.compile(r'href="([^"#?]+)')
# A file of this repository named inside a fenced code block - a command a
# reader would paste. Only files under the application and the docs, with an
# extension, so a job directory or an output path in an example is not one.
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
FENCED_PATH = re.compile(r"(?<![\w/.-])((?:app|docs)/[\w./-]+\.(?:py|sh|json|md|html|txt))(?![\w/.-])")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#?\s]+)")


def tracked_md() -> list[Path]:
    """Tracked .md files that are still on disk.

    `git ls-files` lists the index, which still names a file deleted in the
    working tree.
    """
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO,
                         capture_output=True, text=True).stdout.split()
    return [REPO / p for p in out if (REPO / p).is_file()]


def check_hrefs() -> list[str]:
    """Every relative href in docs/**/*.html resolves to a file."""
    bad = []
    for page in sorted((REPO / "docs").rglob("*.html")):
        for href in HREF.findall(page.read_text(encoding="utf-8")):
            if href.startswith(("http://", "https://", "mailto:", "/", "data:")):
                continue
            if not (page.parent / href).resolve().exists():
                bad.append(f"{page.relative_to(REPO)}: href {href!r} does not resolve")
    return bad


def _top_level() -> set:
    return {p.name for p in REPO.iterdir()}


def check_md_paths() -> list[str]:
    """Every repository path named in a tracked .md exists.

    Only paths *rooted in this repository* are checked, which is decided by
    whether the first segment is a top-level entry. Without that rule the
    pattern also matches things shaped like paths and meaning something else —
    `thinkflowlab/nullius` is a repository slug.
    """
    roots = _top_level()
    bad = []
    for doc in tracked_md():
        for match in MD_PATH.findall(doc.read_text(encoding="utf-8")):
            if any(p.match(match) for p in NOT_REPO_PATHS):
                continue
            if match.split("/", 1)[0] not in roots:
                continue
            if match in KNOWN_ABSENT:
                continue
            if not (REPO / match).exists():
                bad.append(f"{doc.relative_to(REPO)}: `{match}` does not exist")
    return bad


def check_fenced_paths() -> list[str]:
    """Every file of this repository named in a fenced command exists.

    `check_md_paths` reads inline code; the README's commands sit in fences.
    """
    bad = []
    for doc in tracked_md():
        for fence in FENCE.findall(doc.read_text(encoding="utf-8")):
            for match in FENCED_PATH.findall(fence):
                if match in KNOWN_ABSENT or (REPO / match).exists():
                    continue
                bad.append(f"{doc.relative_to(REPO)}: fenced `{match}` does not exist")
    return bad


def check_md_links() -> list[str]:
    """Every relative link in a tracked .md resolves to a file or a directory."""
    bad = []
    for doc in tracked_md():
        for target in MD_LINK.findall(doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "/")):
                continue
            if not (doc.parent / target).resolve().exists():
                bad.append(f"{doc.relative_to(REPO)}: link {target!r} does not resolve")
    return bad


def check_map() -> list[str]:
    """AGENTS.md's "Where code lives" names every folder of modules directly under
    app/nullius and its standard/, and none that does not exist. A module is
    described by its docstring."""
    agents = REPO / "AGENTS.md"
    if not agents.exists():
        return []
    part = re.search(r"^## Where code lives$.*?(?=^## |\Z)", agents.read_text(encoding="utf-8"), re.M | re.S)
    if not part:
        return ["AGENTS.md: no \"Where code lives\""]
    root = REPO / "app/nullius"
    folders = {p.name for top in (root, root / "standard") for p in top.iterdir() if p.is_dir() and any(p.rglob("*.py"))}
    entries = {p.name for p in (root / "catalogue").iterdir() if p.is_dir()}
    named = set(re.findall(r"`(?:app/nullius/)?([\w-]+)/`", part.group(0)))
    return ([f"AGENTS.md: \"Where code lives\" does not name `{name}/`" for name in sorted(folders - named)]
            + [f"AGENTS.md: \"Where code lives\" names `{name}/`, which is not under app/nullius/"
               for name in sorted(named - folders - entries) if not (REPO / name).is_dir()])


def check_importers() -> list[str]:
    """The README's ways-in table names every importer of the registry."""
    # The registry is read in a subprocess so this process's import path stays
    # what it was: the test suite runs these checks over copies of the tree.
    proc = subprocess.run([sys.executable, "-c", "import json, sys; from nullius import importers as r; "
                           "print(json.dumps(sorted(m.LABEL for m in r.available().values())))"],
                          cwd=REPO, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "app")})
    if proc.returncode != 0:
        return [f"app/nullius/importers: the registry could not be read: {proc.stderr.strip()[-200:]}"]
    labels = set(json.loads(proc.stdout))
    bad = []
    readme = (REPO / "README.md").read_text(encoding="utf-8") if (REPO / "README.md").exists() else ""
    table = re.search(r"^\| Way in \| Runs \| Reads \|.*?(?=\n\n)", readme, re.M | re.S)
    if not table:
        bad.append("README.md: no ways-in table (`| Way in | Runs | Reads |`)")
    else:
        rows = table.group(0)
        for label in sorted(labels):
            if label not in rows:
                bad.append(f"README.md: the ways-in table does not name the {label} importer")
    return bad


def check_folders() -> list[str]:
    """docs/README.md names the folders that are actually under docs/."""
    readme = REPO / "docs/README.md"
    if not readme.exists():
        return []
    text = readme.read_text(encoding="utf-8")
    bad = []
    for child in sorted((REPO / "docs").iterdir()):
        if child.is_dir() and child.name not in text:
            bad.append(f"docs/README.md: does not mention docs/{child.name}/")
    return bad


def checks() -> list[str]:
    """Every finding, in one list."""
    return (check_hrefs() + check_md_links() + check_md_paths() + check_fenced_paths()
            + check_folders() + check_map() + check_importers())


def main() -> int:
    findings = checks()
    for line in findings:
        print(f"DRIFT {line}")
    print(f"docs drift: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
