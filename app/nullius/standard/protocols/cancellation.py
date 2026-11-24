"""Interrupt protocol waits when their run stops."""
import queue
import time
from concurrent.futures import CancelledError


def reply(messages, timeout, stopped=None, cut=None):
    """The next message within ``timeout`` seconds; ``queue.Empty`` past them, and as soon as ``cut`` is set: the
    wait then ends as if its time had run out."""
    deadline = time.monotonic() + timeout
    while True:
        if stopped is not None and stopped.is_set():
            raise CancelledError("the run was stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (cut is not None and cut.is_set()):
            raise queue.Empty
        try:
            return messages.get(timeout=min(remaining, 0.1) if stopped is not None or cut is not None else remaining)
        except queue.Empty:
            if time.monotonic() >= deadline:
                raise
