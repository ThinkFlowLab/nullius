"""How fast a run starts its units when the provider says it is going too fast.

A unit whose last model call got a 429 ends as
``RateLimitError``. Starting the next unit at once only sends it into the same
limit, so after such an end no new unit starts for a while: a pause that
doubles while units keep ending rate-limited (``FIRST`` seconds, a provider's
limits being mostly per minute, up to ``LONGEST``) and resets on a unit that
ends any other way - or the pause the provider said (``Retry-After``) when it
is longer. Units already running are left alone; the concurrency asked for is
unchanged, only how soon the next unit starts. A retry of a rate-limited
attempt waits the same pause from that attempt's end (``run.run_again``).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

FIRST = 60.0
LONGEST = 300.0
RATE_LIMITED = "RateLimitError"


class Pace:
    def __init__(self, *stops: threading.Event, first: Optional[float] = None, longest: Optional[float] = None):
        self.stops = [s for s in stops if s is not None]
        self.first = FIRST if first is None else first
        self.longest = LONGEST if longest is None else longest
        self.pause = 0.0
        self.until = 0.0
        self._lock = threading.Lock()

    def ended(self, exception_type: Optional[str], said: Optional[float] = None) -> Optional[float]:
        """A unit's end: the pause it starts, in seconds, when it ended
        rate-limited - ``said``, the provider's own, when that is longer; None
        otherwise (and the pause is reset)."""
        with self._lock:
            if exception_type != RATE_LIMITED:
                self.pause = 0.0
                return None
            self.pause = min(self.longest, self.pause * 2 if self.pause else self.first)
            wait = max(self.pause, said or 0.0)
            self.until = max(self.until, time.time() + wait)
            return wait

    def due(self, ended_at: float, said: Optional[float] = None) -> float:
        """A rate-limited unit tried again: nothing starts before its pause,
        counted from its end (``ended_at``), is over - the doubled pause, at
        least ``FIRST``, or ``said`` when longer. The seconds left."""
        with self._lock:
            self.until = max(self.until, ended_at + max(self.first, self.pause, said or 0.0))
            return max(0.0, self.until - time.time())

    def wait(self) -> None:
        """Until the pause is over, or the run is stopped."""
        while not any(s.is_set() for s in self.stops):
            with self._lock:
                left = self.until - time.time()
            if left <= 0:
                return
            if self.stops:
                self.stops[0].wait(min(left, 1.0))
            else:
                time.sleep(min(left, 1.0))
