"""How fast a run starts its units when the provider says it is going too fast.

The relay passes a 429 to the caller unchanged and never answers for the
provider; a unit whose last model call was refused that way ends as
``RateLimitError``. Starting the next unit at once only sends it into the same
limit, so after such an end no new unit starts for a while: a pause that
doubles while units keep ending rate-limited (``FIRST`` seconds, up to
``LONGEST``) and resets on a unit that ends any other way. Units already
running are left alone; the concurrency asked for is unchanged, only how soon
the next unit starts.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

FIRST = 5.0
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

    def ended(self, exception_type: Optional[str]) -> Optional[float]:
        """A unit's end: the pause it starts, in seconds, when it ended
        rate-limited; None otherwise (and the pause is reset)."""
        with self._lock:
            if exception_type != RATE_LIMITED:
                self.pause = 0.0
                return None
            self.pause = min(self.longest, self.pause * 2 if self.pause else self.first)
            self.until = max(self.until, time.time() + self.pause)
            return self.pause

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
