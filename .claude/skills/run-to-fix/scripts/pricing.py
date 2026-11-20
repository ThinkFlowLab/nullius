"""The price of a run of chosen tasks, from what each task's trials cost when they were recorded."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from trials import TrialRow


@dataclass(frozen=True)
class Price:
    usd: Optional[float]
    tasks: int
    trials: int
    unpriced_tasks: int


def task_costs(rows: List[TrialRow]) -> Dict[str, float]:
    """The mean cost of each task's trials, over the trials that report a cost."""
    by_task: Dict[str, List[float]] = {}
    for r in rows:
        if r.cost_usd is not None:
            by_task.setdefault(r.task, []).append(r.cost_usd)
    return {t: sum(v) / len(v) for t, v in by_task.items()}


def price_of(tasks: List[str], trials_each: int, sources: List[Dict[str, float]]) -> Price:
    """Each task at its cost in the first source that holds it, times its trials. A task no source holds takes the
    mean of the priced tasks; with none priced, the price is unknown."""
    known: Dict[str, float] = {}
    for t in tasks:
        found = next((s[t] for s in sources if t in s), None)
        if found is not None:
            known[t] = found
    n = len(tasks)
    if not tasks:
        return Price(usd=0.0, tasks=0, trials=0, unpriced_tasks=0)
    if not known:
        return Price(usd=None, tasks=n, trials=n * trials_each, unpriced_tasks=n)
    mean = sum(known.values()) / len(known)
    return Price(usd=sum(known.get(t, mean) for t in tasks) * trials_each, tasks=n, trials=n * trials_each,
                 unpriced_tasks=n - len(known))


def usd(value: float) -> str:
    return f"-${-value:.2f}" if value < 0 else f"${value:.2f}"


def describe(price: Price) -> str:
    if price.usd is None:
        return "price unknown: no trial of these tasks reports a cost"
    text = usd(price.usd)
    if price.unpriced_tasks:
        noun = "task" if price.unpriced_tasks == 1 else "tasks"
        text += f", {price.unpriced_tasks} {noun} priced at the mean of the others"
    return text
