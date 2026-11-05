"""Where a dataset's data comes from, one row for each way the benchmark runs.

A benchmark that runs with Harbor and the Nullius standard reads its data two
ways: Harbor from its registry or hub, the standard from the upstream it pins
(a repository at a commit, or a file at a revision). Each row names the source, the pin and who fetches it - always
the harness, on a run, never the person - and the files a definition
checks by their sha256. Nothing here reads the network: it reads the
catalogue entry and the definitions beside it.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

_GITHUB = re.compile(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")
_HF = re.compile(r"^https://huggingface\.co/datasets/([^/]+/[^/]+)/resolve/([^/]+)/")


def _size(n) -> str:
    if not isinstance(n, int) or n <= 0:
        return ""
    for unit, scale in (("GB", 10 ** 9), ("MB", 10 ** 6), ("kB", 10 ** 3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}".replace(".0 ", " ")
    return f"{n} B"


def _repo(upstream: dict) -> dict:
    """The upstream repository as a row's source: GitHub's owner/repo, linked at
    the pinned commit; its release tag, when it has one, beside the commit."""
    url, commit = upstream.get("repository", ""), upstream.get("tested", "")
    m = _GITHUB.match(url)
    pin = " · ".join(x for x in (upstream.get("tag"), commit[:7]) if x)
    return {"from": "GitHub" if m else "Repository", "name": m.group(1) if m else url,
            "href": f"{url.rstrip('/')}/tree/{commit}" if m and commit else url, "pin": pin}


def _files(defined: List[dict]) -> List[dict]:
    """The files a definition's datasets name, each with its size when known."""
    out = []
    for d in defined:
        where = d.get("path") or d.get("url") or ""
        if where:
            out.append({"name": d.get("path") or os.path.basename(where), "size": _size(d.get("bytes")),
                        "checked": bool(d.get("sha256"))})
    return out


def _own(upstream: dict, defined: List[dict], who: str) -> dict:
    """A row for a harness that reads the upstream itself: from Hugging Face when
    every file it names is there, else from the repository it clones."""
    hf = [_HF.match(d.get("url") or "") for d in defined]
    if defined and all(hf):
        name, rev = hf[0].group(1), hf[0].group(2)
        row = {"from": "Hugging Face", "name": name, "href": f"https://huggingface.co/datasets/{name}/tree/{rev}",
               "pin": rev[:7], "by": f"{who} downloads it on the first run and keeps it"}
    else:
        row = dict(_repo(upstream), by=f"{who} fetches the repository at this commit on the first run and keeps it")
    row["files"] = _files(defined)
    return row


def _matching(defined: List[dict], key: str, only: bool) -> List[dict]:
    """The datasets of a definition that are this catalogue dataset: by id
    (``-`` and ``_`` alike), or all of them when the entry has a single dataset."""
    norm = key.replace("-", "_")
    hit = [d for d in defined if str(d.get("id", "")).replace("-", "_") == norm]
    return hit or (list(defined) if only else [])


def of(dataset, harness: List[str], standard: Optional[dict] = None, only: bool = True) -> List[dict]:
    """One row per way ``dataset`` runs, in the order the page names them
    (Nullius, Harbor): ``way``, ``label``, ``from``, ``name``, ``href``,
    ``pin``, ``by`` and ``files``. ``standard`` is the raw definition beside
    the entry; ``only`` says the entry has one dataset."""
    rows = []
    if standard:
        rows.append(dict(_own(standard.get("upstream") or {}, _matching(standard.get("datasets") or [], dataset.key, only), "Nullius"),
                         way="standard", label="Nullius"))
    if "harbor" in harness and (dataset.hub or dataset.registry):
        rows.append({"way": "harbor", "label": "Harbor", "from": "Harbor hub" if dataset.hub else "Harbor registry",
                     "name": dataset.hub or dataset.registry, "href": "", "pin": "",
                     "by": "Harbor downloads it when a run needs it", "files": []})
    return rows
