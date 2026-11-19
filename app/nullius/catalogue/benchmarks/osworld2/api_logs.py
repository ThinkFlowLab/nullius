"""OSWorld2's api_logs with each image in them kept once.

The upstream's M3 agent logs each model call's request with its screenshots as
base64, so one image repeats across files. pack(folder) writes each image once
as images/<sha256>.<ext> and puts that path in its place; restore(folder) gives
each file back byte for byte. Each write is a temporary file renamed over its
name, so a failure leaves every file whole and running again changes nothing.

python3 api_logs.py pack|restore FOLDER... prints what it did, as JSON."""
import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile

IMAGES = "images"
TEMPORARY = ".nullius-tmp"
SHORTEST = 256
VALUE = rb'(?:(?<=: )|(?<=\[)|(?<=, ))"'
BASE64 = re.compile(VALUE + rb'([A-Za-z0-9+/]{%d,}={0,2})"' % SHORTEST)
MARK = b'{"_packed_images": "' + IMAGES.encode() + b'", '
LINK = re.compile(VALUE + IMAGES.encode() + rb'/([0-9a-f]{64}\.(?:jpg|png|gif|webp))"')


def _kind(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for magic, ext in ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"), (b"GIF89a", "gif")):
        if data.startswith(magic):
            return ext
    return None


def _image(text):
    """The image a base64 value holds, as (its file name, its bytes), or None."""
    if len(text) % 4:
        return None
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    ext = _kind(data)
    if ext is None or base64.b64encode(data) != text:
        return None
    return f"{hashlib.sha256(data).hexdigest()}.{ext}", data


def _expand(text, image):
    """A packed file's text as it was, with ``image`` giving a file name's bytes."""
    return b"{" + LINK.sub(lambda m: b'"' + base64.b64encode(image(m.group(1).decode())) + b'"', text[len(MARK):])


def _read(path, folder=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a plain file")
        return f.read(), st


def _write(path, data, like):
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=TEMPORARY, dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), stat.S_IMODE(like.st_mode))
            os.fsync(f.fileno())
        os.utime(tmp, ns=(like.st_atime_ns, like.st_mtime_ns))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sync(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _held(path, name, folder=None):
    """The bytes of ``path``, a plain file holding the image ``name`` names; None for anything else."""
    try:
        data = _read(path, folder)[0]
    except OSError:
        return None
    return data if f"{hashlib.sha256(data).hexdigest()}.{_kind(data)}" == name else None


def _inner(fd, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)


def _tidy(fd):
    for name in os.listdir(fd):
        if name.startswith(".") and name.endswith(TEMPORARY):
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=fd)


@contextlib.contextmanager
def _alone(folder):
    """The folder locked for this process, cleared of a stopped one's half-written files; None when another holds it."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        _tidy(fd)
        with contextlib.suppress(OSError):
            images = _inner(fd, IMAGES)
            try:
                _tidy(images)
            finally:
                os.close(images)
        yield fd
    finally:
        os.close(fd)


def _files(folder):
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json") and not name.startswith("."):
            path = os.path.join(folder, name)
            try:
                yield path, *_read(path)
            except OSError:
                continue


def _images(folder):
    images = os.path.join(folder, IMAGES)
    with contextlib.suppress(FileExistsError):
        os.mkdir(images)
    if not stat.S_ISDIR(os.lstat(images).st_mode):
        raise OSError(f"{images} is not a folder")
    return images


def pack(folder):
    done = dict(files=0, packed=0, images=0, written=0, before=0, after=0)
    with _alone(folder) as mine:
        if mine is None:
            return dict(done, busy=True)
        images, kept = None, set()
        for path, raw, st in _files(folder):
            done["files"] += 1
            done["before"] += len(raw)
            found = {}

            def link(m):
                image = _image(m.group(1))
                if image is None:
                    return m.group(0)
                found[image[0]] = image[1]
                return b'"' + f"{IMAGES}/{image[0]}".encode() + b'"'

            packed = raw if not raw.startswith(b'{"') or raw.startswith(MARK) or LINK.search(raw) \
                else MARK + BASE64.sub(link, raw[1:])
            if not found or _expand(packed, found.__getitem__) != raw:
                done["after"] += len(raw)
                continue
            images = images or _images(folder)
            new = [name for name in found if name not in kept and _held(os.path.join(images, name), name) is None]
            for name in new:
                _write(os.path.join(images, name), found[name], st)
            if new:
                _sync(images)
            kept.update(found)
            done["written"] += len(new)
            _write(path, packed, st)
            done["packed"] += 1
            done["after"] += len(packed)
        if done["packed"]:
            _sync(folder)
        done["images"] = len(kept)
    return done


def restore(folder):
    """A file whose image is missing or does not match its name stays packed."""
    done = dict(restored=0, failed=0)
    with _alone(folder) as mine, contextlib.ExitStack() as stack:
        if mine is None:
            return dict(done, busy=True)
        try:
            images = _inner(mine, IMAGES)
            stack.callback(os.close, images)
        except OSError:
            images = None
        cache = {}

        def image(name):
            if name not in cache:
                cache[name] = None if images is None else _held(name, name, images)
            if cache[name] is None:
                raise ValueError(f"no image {name}")
            return cache[name]

        for path, raw, st in _files(folder):
            if not raw.startswith(MARK):
                continue
            try:
                text = _expand(raw, image)
            except ValueError:
                done["failed"] += 1
                continue
            _write(path, text, st)
            done["restored"] += 1
        if done["restored"]:
            _sync(folder)
    return done


if __name__ == "__main__":
    act = {"pack": pack, "restore": restore}.get(sys.argv[1] if len(sys.argv) > 2 else "")
    if act is None:
        sys.exit("usage: api_logs.py pack|restore FOLDER...")
    print(json.dumps({folder: act(folder) for folder in sys.argv[2:]}))
