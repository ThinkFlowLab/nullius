"""A file written whole, a file held by one process at a time, a file's digest.

Three things many modules need, each done here once. Standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import uuid


def write_whole(path, data, mode=None) -> None:
    """``data``, text or bytes, as the whole of ``path`` - or ``path`` left as it was.

    It is written under a name of its own beside ``path``, then renamed over it:
    a reader never finds half a file. The name is this write's alone, because two
    writers of one file at once are ordinary here (a job's page and its live poll
    both re-parse a job whose files keep changing): with one shared temporary
    name the first rename took the file away and the second raised
    FileNotFoundError, a 500 on the page. Whichever rename lands last wins, and
    both wrote a complete file. A write that fails leaves nothing behind.

    The name is as long for every ``path``, so a file whose own name is as long
    as a name may be is still written.

    ``mode``: the file's permissions, from its first byte (0o600 for what only
    its owner reads); this process's default when not given.

    The folder is the caller's to make. A folder that is gone is an error, and
    is not made again: a user entry removed while its proof ran stays removed."""
    path = os.fspath(path)
    part = os.path.join(os.path.dirname(os.path.abspath(path)), f".{uuid.uuid4().hex}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with (os.fdopen(fd, "w", encoding="utf-8") if isinstance(data, str) else os.fdopen(fd, "wb")) as f:
            if mode is not None and stat.S_IMODE(os.fstat(fd).st_mode) != mode:
                os.fchmod(fd, mode)                   # the umask took bits off: the mode asked for, exactly
            f.write(data)
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(part)
        raise


@contextlib.contextmanager
def locked(path):
    """One holder of ``path`` at a time, across processes: taken on the way in,
    given back on the way out. The file is the lock alone and holds nothing."""
    import fcntl                                      # POSIX; the pages import this module and take no lock
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sha256_of(path) -> str:
    """The sha256 of a file's bytes, read a piece at a time."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
