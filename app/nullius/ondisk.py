"""A file written whole, a lock, a digest."""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import uuid


def write_whole(path, data, mode=None) -> None:
    """``data`` as the whole of ``path``, or ``path`` left as it was: text, bytes, or a
    function that writes text into the file it is given.

    Written beside ``path`` under a name of this write's own, then renamed over it:
    of two writers at once, each writes a complete file and the last rename wins.
    ``mode`` holds from the first byte. The folder is the caller's to make."""
    path = os.fspath(path)
    part = os.path.join(os.path.dirname(path), f".{uuid.uuid4().hex}.part")     # the folder as the system resolves it
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        text = isinstance(data, str) or callable(data)
        with (os.fdopen(fd, "w", encoding="utf-8") if text else os.fdopen(fd, "wb")) as f:
            if mode is not None and stat.S_IMODE(os.fstat(fd).st_mode) != mode:
                os.fchmod(fd, mode)                   # the umask took bits off
            if callable(data):
                data(f)
            else:
                f.write(data)
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(part)
        raise


@contextlib.contextmanager
def locked(path):
    """One holder of ``path`` at a time, across processes. The file holds nothing."""
    import fcntl                                      # POSIX only: imported where the lock is taken
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
