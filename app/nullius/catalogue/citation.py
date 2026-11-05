"""A benchmark's citation, read: its ``citation`` field is BibTeX, as the
benchmark's authors publish it; this turns each entry into the parts a page
shows - the title, the people, the venue and year, the link - beside the
entry itself, which is what a reader copies. Nothing is fetched and nothing
is rewritten: a field the entry lacks is left empty.
"""
from __future__ import annotations

import re

_ACCENTS = {'"': {"o": "ö", "a": "ä", "u": "ü", "e": "ë", "i": "ï", "O": "Ö", "A": "Ä", "U": "Ü"},
            "'": {"e": "é", "a": "á", "o": "ó", "i": "í", "u": "ú", "E": "É"},
            "`": {"e": "è", "a": "à"}, "^": {"o": "ô", "e": "ê"}, "~": {"n": "ñ"}, "c": {"c": "ç"}}
_SHORT = [(re.compile(r"International Conference on Learning Representations"), "ICLR"),
          (re.compile(r"Empirical Methods in Natural Language Processing"), "EMNLP"),
          (re.compile(r"Neural Information Processing Systems"), "NeurIPS"),
          (re.compile(r"International Conference on Machine Learning"), "ICML")]
KINDS = {"conference": "Conference paper", "preprint": "Preprint", "workshop": "Workshop paper"}


def delatex(s: str) -> str:
    """BibTeX's markup as text: the τ of τ-bench, dashes, accents; braces gone."""
    s = s.replace("$\\tau^2$", "τ²").replace("$\\tau^3$", "τ³").replace("$\\tau$", "τ")
    s = re.sub(r"\{?\\textemdash\}?", "—", s)
    s = re.sub(r"\{?\\textendash\}?", "–", s)
    s = re.sub(r"\{?\\([\"'`^~c])\{?([A-Za-z])\}?\}?", lambda m: _ACCENTS.get(m.group(1), {}).get(m.group(2), m.group(2)), s)
    return re.sub(r"\s+", " ", s.replace("{", "").replace("}", "")).strip()


def _value(s: str, j: int) -> tuple:
    if j < len(s) and s[j] == "{":
        d, k = 1, j + 1
        while k < len(s) and d:
            d += 1 if s[k] == "{" else -1 if s[k] == "}" else 0
            k += 1
        return s[j + 1:k - 1], k
    if j < len(s) and s[j] == '"':
        d, k = 0, j + 1
        while k < len(s) and not (s[k] == '"' and d == 0):
            d += 1 if s[k] == "{" else -1 if s[k] == "}" else 0
            k += 1
        return s[j + 1:k], k + 1
    k = j
    while k < len(s) and s[k] != ",":
        k += 1
    return s[j:k].strip(), k


def _name(n: str) -> str:
    n = delatex(n)
    if "," in n:
        last, first = (x.strip() for x in n.split(",", 1))
        return f"{first} {last}".strip()
    return n


def _paper(kind: str, key: str, f: dict, raw: str) -> dict:
    authors = [_name(a) for a in re.split(r"\s+and\s+", f.get("author", "")) if a.strip()]
    journal = delatex(f.get("journal", ""))
    m = re.search(r"arXiv:(\d{4}\.\d{4,5})", journal)
    arxiv = f.get("eprint", "") or (m.group(1) if m else "")
    venue = delatex(f["booktitle"]) if f.get("booktitle") else journal
    if not venue and arxiv:
        venue = "arXiv preprint arXiv:" + arxiv
    short = venue
    for pattern, name in _SHORT:
        if pattern.search(venue) and "Workshop" not in venue:
            short = name
    if venue.startswith("arXiv"):
        short = "arXiv"
    if "Workshop" in venue:
        short = re.sub(r" on .*", "", venue)
    year = delatex(f.get("year", ""))
    kind_of = "workshop" if "Workshop" in venue else "preprint" if short == "arXiv" else "conference"
    shown = len(authors) <= 4
    return {"key": key, "type": kind, "bibtex": raw, "title": delatex(f.get("title", "")), "authors": authors,
            "authors_short": (", ".join(authors[:-1]) + (" and " if len(authors) > 1 else "") + (authors[-1] if authors else "")) if shown
                             else f"{', '.join(authors[:3])} and {len(authors) - 3} others",
            "year": year, "venue": venue, "venue_short": "arXiv" if short == "arXiv" else re.sub(r" \d{4}.*", "", short),
            "kind": kind_of, "kind_label": KINDS[kind_of], "arxiv": arxiv,
            # a link is an http(s) one or none: an entry is text anyone may have written, and a page links it
            "url": (f.get("url", "").strip() if re.match(r"^https?://", f.get("url", "").strip(), re.I) else "")
                   or (f"https://arxiv.org/abs/{arxiv}" if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", arxiv) else "")}


def parse(text: str) -> list:
    """Every entry of a BibTeX text, in order: a dict of its parts (see
    :func:`_paper`) and ``bibtex``, the entry as written."""
    text = (text or "").replace("\r\n", "\n")
    out, i = [], 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        m = re.match(r"@(\w+)\s*\{", text[i:])
        if not m:
            i += 1
            continue
        j = start = i + m.end()
        d = 1
        while j < len(text) and d:
            d += 1 if text[j] == "{" else -1 if text[j] == "}" else 0
            j += 1
        body, raw = text[start:j - 1], text[i:j].strip()
        c = body.find(",")
        key, fields, k = body[:c].strip(), {}, c + 1
        while k < len(body):
            nm = re.match(r"[\s,]*([A-Za-z][\w-]*)\s*=\s*", body[k:])
            if not nm:
                break
            k += nm.end()
            value, k = _value(body, k)
            fields[nm.group(1).lower()] = value
        out.append(_paper(m.group(1).lower(), key, fields, raw))
        i = j
    return out
