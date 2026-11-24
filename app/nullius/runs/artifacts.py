"""Bounded, redacted views of named trial records. No writes or arbitrary paths.

Nullius's own records are ``FILES``. A benchmark may declare its own in its
``benchmark.json`` (``records``: each a ``label`` and a ``path`` under the
trial's ``verifier/``, a file or a folder whose files are listed, one level;
text files alone):
``declared`` finds them by the trial's source, and each is addressed by its
place in that list (``r0``, or ``r0-NAME`` for a file of a folder). A declared
record is opened one folder at a time from the trial's own, no link followed:
it never leads outside the trial."""
from __future__ import annotations

import os
import re
import stat

from nullius.redact import redact

FILES = {
    "result": ("Result record", "result.json"),
    "config": ("Run configuration", "config.json"),
    "exception": ("Exception", "exception.txt"),
    "trial-log": ("Trial log", "trial.log"),
    "gateway": ("Gateway log", "agent/gateway.log"),
    "reward": ("Recorded reward", "verifier/reward.txt"),
    "reward-json": ("Recorded reward (rewardkit)", "verifier/reward.json"),
    "reward-details": ("Reward breakdown (rewardkit)", "verifier/reward-details.json"),
    # Harbor's record of what it copied out of the container after the run —
    # one entry per requested path with its status (`ok`, `failed`, `empty`,
    # `skipped` — Harbor's `ArtifactManifestEntry`).
    "manifest": ("Artifact manifest", "artifacts/manifest.json"),
    "computer-calls": ("Computer tool calls", "verifier/computer-calls.jsonl"),
    "computer-events": ("Desktop events", "verifier/computer-events.jsonl"),
    "computer-boxes": ("Desktop targets", "verifier/computer-boxes.json"),
    "osworld-runner-calls": ("OSWorld runner tool calls", ".work/osworld/computer-calls.jsonl"),
    "osworld-result": ("OSWorld result", "verifier/osworld/result.txt"),
    "osworld-runner": ("OSWorld runner log", ".work/osworld/runner.log"),
}
CHUNK_SIZE = 8_000
MAX_READ = 16 * 1024 * 1024
FOLDER_FILES = 200                     # the most files a declared folder lists
SNIFF = 8192                           # a declared file with a NUL byte this far in is not text: not a record
DECLARED_KEY = re.compile(r"r(?P<n>0|[1-9][0-9]{0,2})(?:-(?P<name>.+))?", re.S)
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


_CACHE: dict = {}


def declared(source) -> list:
    """The records the benchmark of a trial's *source* declares, in its order:
    found by the benchmark's key, a dataset's or a source its report block
    names. Empty when it declares none."""
    from nullius.catalogue import catalog
    sig = catalog.signature()
    if _CACHE.get("sig") != sig:
        table = {}
        for b in catalog.load().benchmarks:
            records = [dict(label=r["label"].strip(), path=r["path"]) for r in b.records or []
                       if catalog.valid_record(r)] if isinstance(b.records, list) else []
            if records:
                for name in [b.key, *b.datasets, *((b.report or {}).get("sources") or [])]:
                    table.setdefault(str(name).strip().lower(), records)
        _CACHE.update(sig=sig, table=table)
    return list(_CACHE["table"].get((source or "").strip().lower(), ()))


def _declared(key, records):
    """(label, the path in the trial, the record's own path) of a declared record's key: a file of a declared folder
    is labelled by the folder's label and its own name."""
    m = DECLARED_KEY.fullmatch(key or "")
    if not m or int(m["n"]) >= len(records):
        raise LookupError(key)
    record = records[int(m["n"])]
    name = m["name"]
    if name is not None and (name in (".", "..") or "/" in name or not _plain_name(name)):
        raise LookupError(key)
    label = record["label"] if name is None else f"{record['label']}: {name}"
    return label, record["path"] + ("/" + name if name is not None else ""), record["path"]


def _plain_name(name) -> bool:
    """Whether a file's name in a declared folder can be listed and served: printable text alone. A name that is not
    valid UTF-8 comes back from the system with a surrogate in it, which is not printable, as no control character is;
    such a file is neither listed nor read."""
    return name.isprintable()


def _open_under(trial_dir, path, flags):
    """A descriptor of *path* under the trial's folder, each part opened from the one before it, no link followed."""
    *folders, last = path.split("/")
    fd = os.open(trial_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in folders:
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(last, flags | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def _open_record(trial_dir, key, records):
    """An open regular file of a key, fixed or declared."""
    if key in FILES:
        path = path_for(trial_dir, key)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    else:
        fd = _open_under(trial_dir, _declared(key, records)[1], os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or (key not in FILES and not _text(fd)):
        os.close(fd)
        raise LookupError(key)
    return fd


def _text(fd) -> bool:
    """Whether an open file reads as text: no NUL byte in its first ``SNIFF`` bytes. It is read from its start
    again after."""
    try:
        return b"\0" not in os.pread(fd, SNIFF, 0)
    except OSError:
        return False


def describe(key, records=()):
    """(label, filename) of a key: a fixed record's, or a declared one's, its path in the trial."""
    if key in FILES:
        return FILES[key]
    label, path, _ = _declared(key, records)
    return label, path


def path_for(trial_dir, key):
    if key not in FILES:
        raise LookupError(key)
    base = os.path.realpath(trial_dir)
    path = os.path.realpath(os.path.join(base, FILES[key][1]))
    if os.path.commonpath([base, path]) != base:
        raise LookupError(key)
    return path


def _declared_rows(trial_dir, records):
    """The declared records that exist: a file, or each file of a folder, by name, one level, no link; each row
    says it is ``declared``."""
    rows = []
    for n, record in enumerate(records):
        try:
            fd = _open_under(trial_dir, record["path"], os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        entries = None
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                if _text(fd):
                    rows.append(dict(key=f"r{n}", label=record["label"], filename=record["path"], part="",
                                     exists=True, size=info.st_size, declared=True))
            elif stat.S_ISDIR(info.st_mode):
                entries = os.scandir(fd)
                names = (e.name for e in entries if e.is_file(follow_symlinks=False))
                first, more = len(rows), False
                count = 0
                for name in names:
                    if not _plain_name(name):
                        continue
                    try:
                        one = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        info = os.fstat(one)
                        if stat.S_ISREG(info.st_mode) and _text(one):
                            if count == FOLDER_FILES:
                                more = True
                                break
                            rows.append(dict(key=f"r{n}-{name}", label=record["label"], part=name, exists=True,
                                             filename=f"{record['path']}/{name}", size=info.st_size, declared=True))
                            count += 1
                    finally:
                        os.close(one)
                rows[first:] = sorted(rows[first:], key=lambda row: row["part"])
                if more:
                    rows[-1]["more"] = True
        finally:
            if entries is not None:
                entries.close()
            os.close(fd)
    return rows


def listing(trial_dir, records=()):
    """Every fixed record, said whether it exists, then the declared ones that exist."""
    out = []
    for key, (label, filename) in FILES.items():
        try:
            path = path_for(trial_dir, key)
            exists = os.path.isfile(path)
            size = os.path.getsize(path) if exists else None
        except (OSError, LookupError):
            exists, size = False, None
        out.append(dict(key=key, label=label, filename=filename, part="", exists=exists, size=size))
    return out + _declared_rows(trial_dir, records)


def whole(trial_dir, key, records=()):
    """The record redacted, up to ``MAX_READ``, and whether it was cut there:
    what a download or a copy carries, never more than the reader shows."""
    fd = _open_record(trial_dir, key, records)
    with open(fd, encoding="utf-8", errors="replace") as handle:
        raw = handle.read(MAX_READ + 1)
    return redact(raw[:MAX_READ]), len(raw) > MAX_READ


def read(trial_dir, key, page=1, records=()):
    # Redact before slicing so credentials cannot straddle visible chunks.
    text, truncated = whole(trial_dir, key, records)
    pages = max(1, (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(pages, max(1, page))
    start = (page - 1) * CHUNK_SIZE
    label, filename = describe(key, records)
    return dict(key=key, label=label, filename=filename, page=page,
                pages=pages, text=text[start:start + CHUNK_SIZE], empty=not text,
                truncated=truncated, characters=len(text), start=start,
                end=min(start + CHUNK_SIZE, len(text)))
