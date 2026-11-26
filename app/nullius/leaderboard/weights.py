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


def competition(rows, value):
    """Competition ranks (1, 1, 3) by a value from full precision; a row without one has no rank."""
    eligible = sorted((r for r in rows if value(r) is not None), key=lambda r: (-value(r), r["name"], r["key"]))
    ranks, previous, assigned = {}, None, None
    for i, row in enumerate(eligible, 1):
        if value(row) != previous:
            assigned = i
        ranks[row["key"]], previous = assigned, value(row)
    return ranks


def rank(rows, sort="overall", descending=True):
    """Rank every row with an Overall from full precision; incomplete coverage stays a note, never a gate."""
    rows = [dict(row, rank=None) for row in rows]
    ranks = competition(rows, lambda r: r["overall"])
    for row in rows:
        row["rank"] = ranks.get(row["key"])
    eligible = [r for r in rows if r["overall"] is not None]
    groups = [eligible, [r for r in rows if r["overall"] is None]]
    result = []
    for group in groups:
        group.sort(key=lambda r: (r["name"], r["key"]))
        present = [r for r in group if r.get(sort) is not None]
        absent = [r for r in group if r.get(sort) is None]
        result.extend(sorted(present, key=lambda r: r[sort], reverse=descending) + absent)
    return result
