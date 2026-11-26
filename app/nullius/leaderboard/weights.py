"""Measured weight shares, percentage formatting and competition ranking."""
from __future__ import annotations


def combine(values):
    """A missing measurement has no effective share, including a positive weight."""
    values = list(values)
    scale = max((w for score, w in values if score is not None and w > 0), default=0)
    total = sum(w / scale for score, w in values if score is not None and w > 0) if scale else 0
    shares = [(w / scale) / total if total and score is not None and w > 0 else 0 for score, w in values]
    return (sum(score * share for (score, _), share in zip(values, shares) if share) if total else None), shares


def percent(value):
    return f"{100 * value:.1f}".rstrip("0").rstrip(".") + "%" if value is not None else "—"


def rank(rows, sort="overall", descending=True):
    """Rank every row with an Overall from full precision; incomplete coverage stays a note, never a gate."""
    rows = [dict(row, rank=None) for row in rows]
    eligible = sorted((r for r in rows if r["overall"] is not None),
                      key=lambda r: (-r["overall"], r["name"], r["key"]))
    previous, assigned = None, None
    for i, row in enumerate(eligible, 1):
        if row["overall"] != previous:
            assigned = i
        row["rank"], previous = assigned, row["overall"]
    groups = [eligible, [r for r in rows if r["overall"] is None]]
    result = []
    for group in groups:
        group.sort(key=lambda r: (r["name"], r["key"]))
        present = [r for r in group if r.get(sort) is not None]
        absent = [r for r in group if r.get(sort) is None]
        result.extend(sorted(present, key=lambda r: r[sort], reverse=descending) + absent)
    return result
