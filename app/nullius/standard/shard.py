"""One run over many machines: shards of a dataset's units, and the launch they share.

``--shard I/N`` runs the units whose shard is ``I`` of ``N``. A unit's shard
is read off its id, not its position: ``sha256(id) mod N`` (``PARTITION``).
So every machine computes the same split from the ids alone, with no list to
hand around, and a dataset that gains units leaves every unit it had in the
shard it was in - only the new ones land somewhere. Changing ``N`` moves
most units; a launch keeps one ``N``.

The order inside a run is: the driver's ``units``, then the shard, then
``--limit`` - so ``--shard 1/2 --limit 6`` is the first six units of shard 1,
and the two shards of a ``--limit 6`` launch run up to twelve units between
them. The limit is a shard's own: a machine is told how much it takes on.

A trial keeps its unit's place in the whole dataset as its tag (``u0042`` is
the 43rd unit in every shard), so the shards' trials never share a name and
merge without renaming (``merge``).

Every shard of one launch records the same ``launch`` id: ``--launch``, or by
default the day (UTC), the benchmark, the agent or capability and the model, so
shards started separately - on other machines, minutes apart - still say
they belong together. Two launches of the same pair on the same day need
``--launch`` to stay apart.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import List, Optional, Tuple

PARTITION = "sha256(unit id) mod N"
_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_WORD = re.compile(r"[^A-Za-z0-9_.-]+")


def parse(spec: str) -> Tuple[int, int]:
    """``"I/N"`` as (I, N), 1 <= I <= N; ValueError otherwise."""
    m = _SPEC.match(str(spec or ""))
    if not m:
        raise ValueError(f"--shard {spec!r}: I/N, such as 1/4")
    i, n = int(m.group(1)), int(m.group(2))
    if n < 1 or not 1 <= i <= n:
        raise ValueError(f"--shard {spec!r}: I is from 1 to N")
    return i, n


def of(unit: str, n: int) -> int:
    """The shard (1..n) a unit falls in: by its id alone."""
    return int.from_bytes(hashlib.sha256(str(unit).encode("utf-8")).digest()[:8], "big") % n + 1


def select(units: List[str], index: int, n: int) -> List[Tuple[int, str]]:
    """The units of shard ``index`` of ``n``, each with its place in the whole
    list (what its trial's tag is made of), in the dataset's order."""
    return [(i, u) for i, u in enumerate(units) if n == 1 or of(u, n) == index]


def launch_id(bench_word: str, other_word: str, model: str, day: Optional[str] = None) -> str:
    """The default launch id shards started separately agree on."""
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    words = [day, bench_word, other_word] + ([model] if model else [])
    return "__".join(_WORD.sub("_", w).strip("_") for w in words)


def record(index: int, n: int, total: int, planned: List[str], limit: Optional[int] = None) -> dict:
    """What a shard's config says of it (``standard.shard``): which shard, of
    how many, over how many units, split how; the units it planned to run, so
    a merge can name a unit planned and never run; and its own ``limit``."""
    return {"index": index, "of": n, "units_total": total, "partition": PARTITION, "planned": list(planned),
            **({"limit": limit} if limit else {})}
