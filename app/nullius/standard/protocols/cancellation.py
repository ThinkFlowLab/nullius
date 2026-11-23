"""Interrupt protocol waits when their run stops."""
import queue
import time
from concurrent.futures import CancelledError


def reply(messages, timeout, stopped=None):
    deadline = time.monotonic() + timeout
    while True:
        if stopped is not None and stopped.is_set():
            raise CancelledError("the run was stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return messages.get(timeout=min(remaining, 0.1) if stopped is not None else remaining)
        except queue.Empty:
            if time.monotonic() >= deadline:
                raise
