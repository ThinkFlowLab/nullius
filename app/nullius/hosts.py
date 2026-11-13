"""The remote machines a recipe can run on.

A host is one of two things, both spelled in the ``HOSTS`` setting, comma
separated:

- an alias from ``~/.ssh/config`` — ``bench`` — where ssh itself knows the
  address, user, port and key. Nullius reads the config for the *names* and
  what it says beside them (HostName, User, Port); it never opens a key.
- a manual entry — ``user@host:port`` optionally followed by ``:/path/to/key``
  — for a machine the config does not name. An optional ``Name=`` prefix
  supplies its display label without changing the SSH target. The key stays a path.

``test`` asks the host what a run needs: that ssh gets in without a prompt,
and whether ``uv``, ``docker``, ``podman`` and ``harbor`` answer there, and
whether its docker has ``userns-remap``. It runs ssh with
``BatchMode=yes`` so a password prompt is a refusal, not a hang, and it never
sends anything but that one command. Nothing is stored beyond the host list.

``measure`` asks a machine, a host or this one, for its capacity and which
constraints its engine and its systemd enforce (``nullius.constraints``): a
second command, which starts throwaway containers and scopes there.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from nullius import constraints, paths

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MANUAL = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?(?::(?P<key>(?:/|~/)[^\s,:]+))?$")
PROBE = ("printf 'ok\\n'; for c in uv docker podman harbor; do command -v \"$c\" >/dev/null 2>&1 && printf '%s=yes\\n' \"$c\" || printf '%s=no\\n' \"$c\"; done; "
         "command -v harbor >/dev/null 2>&1 && printf 'harbor_version=%s\\n' \"$(harbor --version 2>/dev/null | sed -n 1p)\"; "
         "for c in docker podman; do command -v $c >/dev/null 2>&1 && ($c ps >/dev/null 2>&1 && printf '%s_ok=yes\\n' $c || printf '%s_ok=no\\n' $c); done; "
         "docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q name=userns && printf 'docker_userns=yes\\n'; "
         "printf 'hostname=%s\\n' \"$(hostname)\"")


@dataclass(frozen=True)
class Host:
    entry: str                      # as written in the setting
    kind: str                       # "alias" | "manual"
    alias: Optional[str] = None
    user: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    key: Optional[str] = None       # a path, never its contents
    display_name: Optional[str] = None  # optional name for a manual connection

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if self.kind == "alias":
            return self.alias or self.entry
        return f"{self.user}@{self.host}" + (f":{self.port}" if self.port else "")

    def argv(self, command: str, timeout: float = 8.0) -> List[str]:
        """The ssh argv: BatchMode so a prompt is a refusal, a connect
        timeout, and the one command."""
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", "-o", "StrictHostKeyChecking=accept-new"]
        if self.kind == "alias":
            target = self.alias
        else:
            if self.port:
                argv += ["-p", str(self.port)]
            if self.key:
                # that key alone: an agent holding several offers each first, and a server that counts
                # them (MaxAuthTries, six by default) hangs up before the named one is tried
                argv += ["-i", os.path.expanduser(self.key), "-o", "IdentitiesOnly=yes"]
            target = f"{self.user}@{self.host}"
        return argv + [target, command]

    def to_dict(self) -> dict:
        return {"entry": self.entry, "kind": self.kind, "label": self.label, "alias": self.alias, "user": self.user,
                "host": self.host, "port": self.port, "key": self.key}


def parse(entry: str) -> Host:
    """One entry of the setting → a Host; ValueError when it is neither form."""
    e = (entry or "").strip()
    target, display_name = e, None
    if "=" in e.split("@", 1)[0]:
        display_name, target = e.split("=", 1)
        display_name, target = display_name.strip(), target.strip()
        if not display_name or len(display_name) > 64 or any(c in ",=@" or not c.isprintable() for c in display_name):
            raise ValueError("Host name must be 1–64 printable characters without commas, @ or =")
    m = _MANUAL.match(target)
    if m:
        port = int(m.group("port")) if m.group("port") else None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(f"{e!r}: port out of range")
        return Host(entry=e, kind="manual", user=m.group("user"), host=m.group("host"), port=port, key=m.group("key"), display_name=display_name)
    if display_name is None and _ALIAS.match(e):
        return Host(entry=e, kind="alias", alias=e)
    raise ValueError(f"{e!r} is not a host: an alias from ~/.ssh/config, or user@host[:port][:/path/to/key]")


def parse_list(text: str) -> List[Host]:
    out, seen = [], set()
    for raw in (text or "").split(","):
        if not raw.strip():
            continue
        h = parse(raw)
        if h.entry not in seen:
            seen.add(h.entry)
            out.append(h)
    return out


def validate_list(text: str) -> Optional[str]:
    """None when every entry parses, else what was wrong."""
    try:
        parse_list(text)
        return None
    except ValueError as e:
        return str(e)


# --- ~/.ssh/config --------------------------------------------------------

def ssh_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def scan_ssh_config(path: Optional[str] = None) -> List[dict]:
    """The ``Host`` aliases of the ssh config with what the config says beside
    them - hostname, user, port. Patterns (``*``, ``?``, ``!``) are skipped:
    they are not machines. Identity files are not read and not reported."""
    path = path or ssh_config_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    hosts: List[dict] = []
    current: List[dict] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^(\w+)\s*[=\s]\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2).strip()
        if key == "host":
            current = []
            for name in value.split():
                if any(c in name for c in "*?!"):
                    continue
                if _ALIAS.match(name):
                    entry = {"alias": name, "hostname": None, "user": None, "port": None}
                    hosts.append(entry)
                    current.append(entry)
        elif key == "match":
            current = []
        elif key in ("hostname", "user", "port") and current:
            for entry in current:
                if entry[key] is None:
                    entry[key] = value
    return hosts



def subprocess_run(argv: List[str], timeout: float, env: Optional[dict] = None) -> tuple:
    """(returncode, stdout, stderr); a timeout is a return code of its own."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {int(timeout)} s"
    except OSError as e:
        return 127, "", str(e)


_PROGRAM = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,39}$")


def test(host: Host, run: Optional[Callable] = None, timeout: float = 12.0, programs: Optional[List[str]] = None) -> dict:
    """What the host answers: reachable or why not, and whether uv, docker,
    podman and harbor are there (``engine``: which of docker and podman the
    standard's container backend runs there, or None) - and, given ``programs`` (a benchmark's host
    requirements), which of them are not on its PATH (``missing``). Nothing
    but the probe command is sent. ``run`` is looked up at call time so a
    test can stand in for ssh."""
    programs = [p for p in programs or [] if _PROGRAM.match(p)]
    probe = PROBE + ("; for p in " + " ".join(programs) + '; do command -v "$p" >/dev/null 2>&1 || printf \'missing=%s\\n\' "$p"; done'
                     if programs else "")
    started = time.monotonic()
    rc, out, err = (run or subprocess_run)(host.argv(probe, timeout=min(timeout, 8.0)), timeout)
    ms = int((time.monotonic() - started) * 1000)
    result: Dict = {"entry": host.entry, "label": host.label, "ok": False, "latency_ms": ms, "uv": None, "docker": None,
                    "docker_ok": None, "docker_userns": False, "podman": None, "podman_ok": None, "engine": None,
                    "harbor": None, "harbor_version": None, "hostname": None, "error": None,
                    **({"missing": []} if programs else {})}
    if rc != 0 or "ok" not in out.split():
        text = (err or out or "").strip().splitlines()
        result["error"] = _reason(rc, text[-1] if text else "")
        return result
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in ("uv", "docker", "podman", "harbor", "docker_ok", "podman_ok", "docker_userns"):
            result[k] = v == "yes"
        elif k == "harbor_version":
            result["harbor_version"] = v.strip().removeprefix("harbor ").strip() or None
        elif k == "hostname":
            result["hostname"] = v.strip() or None
        elif k == "missing" and programs and v.strip() in programs:
            result["missing"].append(v.strip())
    # the standard's container backend's engine there: docker when the probe finds it, else podman
    result["engine"] = "docker" if result["docker"] else "podman" if result["podman"] else None
    result["ok"] = True
    return result


def measure(host: Optional[Host], run: Optional[Callable] = None, timeout: float = 300.0, engine: Optional[str] = None) -> dict:
    """The machine's capacity and which constraints its engine and a systemd
    scope enforce (``constraints.read``), from ``constraints.probe`` and
    ``constraints.scope_probe`` run there: over ssh on
    ``host`` with ``engine``, the program ``test`` found there (docker by
    default); or here when it is None, with the standard's own engine and the
    options its units start with (``container``). ``run`` as in ``test``."""
    if host:
        program = engine or "docker"
        options = ["--security-opt", "no-new-privileges", *(["--security-opt", "label=disable"] if program == "podman" else [])]
    else:
        from nullius.standard import container
        program, options = container.DOCKER, [*container._userns(), "--user", container._user(), *container._security_opt()]
        # with the engine program's environment, as a run's check and its units: what one sees, the others do
        run = run or (lambda argv, t: subprocess_run(argv, t, env=container.client_env()))
    script = constraints.probe(constraints.samples(), docker=program, options=options) + "; " + constraints.scope_probe(constraints.samples())
    argv = host.argv(script) if host else ["sh", "-c", script]
    rc, out, err = (run or subprocess_run)(argv, timeout)
    found = constraints.read(out, constraints.samples())
    if rc != 0:
        # cut short (a timeout, nothing printed): what it did not measure keeps the last test's (constraints.save)
        found = {k: v for k, v in found.items() if v}
        found["error"] = _reason(rc, ((err or out).strip().splitlines() or [""])[-1])
    return found


def _reason(rc: int, last: str) -> str:
    low = last.lower()
    if rc == 124:
        return last or "no answer"
    if "permission denied" in low:
        return "refused: no usable key"
    if "could not resolve" in low or "name or service not known" in low:
        return "the host name does not resolve"
    if "connection refused" in low:
        return "connection refused"
    if "host key verification failed" in low:
        return "host key verification failed"
    if rc == 255:
        return last or "ssh could not connect"
    return last or f"ssh exited {rc}"


# --- getting a key onto a host ------------------------------------------
#
# A manual host needs a key ssh can use without a prompt. Three ways in, none
# of which stores a password: a key already on this machine (a path), a key
# file handed to the page (saved under the user's Nullius config, mode 0600), or a
# password used once to put Nullius's own public key into the host's
# authorized_keys — the password goes to ssh through a pty and nowhere else,
# and the entry that comes back names the key, not the password.

# Optional explicit overrides; defaults are resolved at use time so a
# NULLIUS_HOME profile does not share the real user's key store.
KEY_DIR: Optional[str] = None
OWN_KEY: Optional[str] = None
_KEY_HEADER = re.compile(rb"^-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----")


def _key_name(user: str, host: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", f"{user}@{host}")


def save_key(user: str, host: str, data: bytes, key_dir: Optional[str] = None) -> str:
    """A private key handed to the page, written where ssh can read it and
    nobody else: <config>/ssh/<user@host>, mode 0600. The path it is at."""
    if not _KEY_HEADER.match(data.lstrip()) or len(data) > 20000:
        raise ValueError("not a private key file (expected -----BEGIN … PRIVATE KEY-----)")
    if b"ENCRYPTED" in data[:200]:
        raise ValueError("the key has a passphrase; ssh could not use it without a prompt")
    key_dir = key_dir or KEY_DIR or os.path.join(paths.config_dir(), "ssh")
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    path = os.path.join(key_dir, _key_name(user, host))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data if data.endswith(b"\n") else data + b"\n")
    return path


def forget_keys(old: str, new: str, key_dir: Optional[str] = None) -> List[tuple]:
    """Delete each key file that ``save_key`` wrote for an entry of the host
    list ``old`` when no entry of the list ``new`` names it. Only the file
    <config>/ssh/<user@host> of that entry goes: Nullius's own key and a key
    the user named by path stay. The files not deleted, each as (path, reason)."""
    key_dir = os.path.realpath(key_dir or KEY_DIR or os.path.join(paths.config_dir(), "ssh"))
    try:
        before = parse_list(old)
    except ValueError:              # a list edited by hand into a wrong one names no file to delete
        return []
    kept = {os.path.realpath(os.path.expanduser(h.key)) for h in parse_list(new) if h.key}
    failed = []
    for h in before:
        path = h.key and os.path.realpath(os.path.expanduser(h.key))
        if not path or path in kept or path != os.path.join(key_dir, _key_name(h.user, h.host)):
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            failed.append((path, e.strerror or str(e)))
    return failed


def own_key(path: Optional[str] = None, run: Optional[Callable] = None) -> str:
    """Nullius's own key pair on this machine, made on first use with no
    passphrase. The private key's path; the public key sits beside it."""
    path = path or OWN_KEY or os.path.join(KEY_DIR or os.path.join(paths.config_dir(), "ssh"), "id_ed25519")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        rc, out, err = (run or subprocess_run)(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nullius", "-f", path], 30)
        if rc != 0:
            raise RuntimeError(f"ssh-keygen failed: {(err or out).strip() or rc}")
    return path


def pty_run(argv: List[str], answer: str, timeout: float = 30.0) -> tuple:
    """Run argv on a pty, type ``answer`` at the first password prompt, and
    return (returncode, what was printed after that). Nothing typed is kept."""
    import pty
    import select
    import signal
    import termios
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    try:                                                     # no echo of what is typed, whatever the child does
        attrs = termios.tcgetattr(fd); attrs[3] &= ~termios.ECHO; termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    deadline = time.monotonic() + timeout
    seen, after, sent = b"", b"", False
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                if sent:
                    after += chunk
                else:
                    seen += chunk
                    if b"assword" in seen:
                        os.write(fd, answer.encode() + b"\n"); sent = True
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                after += b"\nno answer within %d s" % int(timeout)
                break
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    rc = os.waitstatus_to_exitcode(status)
    if not sent:
        after = seen
    return rc, after.replace(answer.encode(), b"").decode("utf-8", "replace")


def enrol(user: str, host: str, port: Optional[int], password: str, key: Optional[str] = None, run_pty: Optional[Callable] = None) -> Host:
    """Put Nullius's public key on the host, using the password once, and
    return the entry that reaches the host with that key from now on."""
    key = key or own_key()
    with open(key + ".pub", encoding="utf-8") as fh:
        pub = fh.read().strip()
    argv = ["ssh", "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
    if port:
        argv += ["-p", str(port)]
    cmd = f"umask 077; mkdir -p ~/.ssh; grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys; echo enrolled"
    rc, out = (run_pty or pty_run)(argv + [f"{user}@{host}", cmd], password)
    if rc != 0 or "enrolled" not in out:
        last = [l for l in out.strip().splitlines() if l.strip()]
        reason = _reason(rc, last[-1] if last else "")
        if "refused" in reason:
            reason = "the password was not accepted"
        raise RuntimeError(reason)
    return parse(f"{user}@{host}" + (f":{port}" if port else "") + f":{key}")
