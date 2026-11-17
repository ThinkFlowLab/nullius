"""Exact tests of D7: a binomial test on won and lost tasks, Fisher's test on passes of trials."""
from __future__ import annotations

import math
from typing import Optional

ALPHA = 0.05
#: probabilities within this relative distance of the observed table's count as equally extreme
TIE = 1e-7


def standard_error(rate: float, n: int) -> Optional[float]:
    return math.sqrt(rate * (1 - rate) / n) if n else None


def binomial_two_sided(won: int, lost: int) -> float:
    """p of an exact two-sided binomial test of won against lost, with even odds."""
    n = won + lost
    if n == 0:
        return 1.0
    k = min(won, lost)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def fisher_two_sided(before_pass: int, before_fail: int, after_pass: int, after_fail: int) -> float:
    """p of Fisher's exact two-sided test on a 2 x 2 table: the tables no more likely than the one observed."""
    n = before_pass + before_fail + after_pass + after_fail
    rows = before_pass + before_fail
    passes = before_pass + after_pass
    if n == 0 or rows in (0, n) or passes in (0, n):
        return 1.0
    total = math.comb(n, passes)

    def prob(x: int) -> float:
        return math.comb(rows, x) * math.comb(n - rows, passes - x) / total

    observed = prob(before_pass)
    lo, hi = max(0, passes - (n - rows)), min(rows, passes)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + TIE)))


def verdict(p: float, better: bool, worse: bool) -> str:
    """"improved" or "regressed" when p is under ALPHA, in the direction the counts point, else "noise"."""
    if p < ALPHA and better:
        return "improved"
    if p < ALPHA and worse:
        return "regressed"
    return "noise"
