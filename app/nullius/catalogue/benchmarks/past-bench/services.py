"""The episode's mock services: one process each, on ports this driver picks.

The upstream declares, in each ``task.yaml``, the services an episode needs and
the port each one usually listens on (``notes`` on 9105, ``gmail`` on 9100).
Two units of one run would collide on those fixed ports, so the driver asks the
kernel for a free port and rewrites the service's health check, reset endpoint
and tool endpoints to it. Everything else is the upstream's: its server file,
its fixtures, its audit log, its error injection.

What the services record is also the benchmark's evidence: a grader reads
``audit`` to see whether a note was really shared with the named owners, not
merely described in the reply. ``audit()`` is therefore collected while the
services are still up, once per episode.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
SERVE = HERE / "serve.py"
READY_SECONDS = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _with_port(url: str, port: int) -> str:
    """The same URL on another port (the driver's), path and scheme unchanged."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    if not parts.scheme:
        return url
    netloc = parts.hostname or "127.0.0.1"
    return urlunsplit((parts.scheme, f"{netloc}:{port}", parts.path, parts.query, parts.fragment))


def call(url: str, method: str = "POST", body: Any = None, timeout: float = 10.0) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except (urllib.error.URLError, OSError) as e:      # not listening yet, or gone: the caller waits
        return {"status": 599, "body": {"error": str(e)}}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw[:2000]
    return {"status": status, "body": parsed}


class Service:
    """One upstream mock service, one process, one port, this episode."""

    def __init__(self, repo: Path, spec: Dict[str, Any], python: str):
        self.repo = repo
        self.spec = spec
        self.python = python
        self.name = str(spec.get("name") or "service")
        self.port = _free_port()
        self.process: Optional[subprocess.Popen] = None
        self.log = None

    @property
    def module(self) -> str:
        """The upstream's ``command`` names the server file; take that part."""
        words = str(self.spec.get("command") or "").split()
        for word in words:
            if word.endswith(".py"):
                return word
        raise ValueError(f"{self.name}: the service command names no .py file")

    def env(self) -> Dict[str, str]:
        out = dict(os.environ)
        for key, value in (self.spec.get("env") or {}).items():
            text = str(value)
            if text and not os.path.isabs(text) and "/" in text:
                text = str(self.repo / text)   # fixtures are written relative to the repository
            out[str(key)] = text
        out["PORT"] = str(self.port)
        out["PYTHONUNBUFFERED"] = "1"
        return out

    def start(self) -> None:
        log_path = self.repo.parent / "logs"
        self.process = subprocess.Popen(
            [self.python, str(SERVE), str(self.repo), self.module, str(self.port)],
            cwd=str(self.repo), env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        ready = self.spec.get("health_check")
        timeout = max(float(self.spec.get("ready_timeout") or READY_SECONDS), 30.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                tail = (self.process.stderr.read() or "")[-800:] if self.process.stderr else ""
                raise RuntimeError(f"{self.name}: the service exited at startup: {tail.strip()}")
            if not ready:
                time.sleep(0.5)
                return
            method = str(self.spec.get("health_check_method") or "GET").upper()
            got = call(_with_port(str(ready), self.port), method,
                       {} if method == "POST" else None, timeout=5.0)
            if got["status"] != 599:       # any HTTP answer means it is up; 422 is still an answer
                return
            time.sleep(0.3)
        raise RuntimeError(f"{self.name}: not ready in {int(timeout)} s on port {self.port}")

    def reset(self) -> None:
        url = self.spec.get("reset_endpoint")
        if url:
            call(_with_port(str(url), self.port), "POST", {})

    def audit(self) -> Dict[str, Any]:
        url = self.spec.get("reset_endpoint") or self.spec.get("health_check")
        if not url:
            return {}
        base = str(url).rsplit("/reset", 1)[0]
        got = call(_with_port(base + "/audit", self.port), "GET", None)
        return got["body"] if isinstance(got["body"], dict) else {"raw": got["body"]}

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        finally:
            self.process = None


class Episode:
    """The services one episode declares, started together and stopped together."""

    def __init__(self, repo: Path, task: Dict[str, Any], python: str):
        self.services: List[Service] = []
        for spec in task.get("services") or []:
            if isinstance(spec, dict) and spec.get("command"):
                self.services.append(Service(repo, spec, python))

    def start(self) -> Dict[str, int]:
        try:
            for svc in self.services:
                svc.start()
                svc.reset()
        except Exception:
            self.stop()
            raise
        return {svc.name: svc.port for svc in self.services}

    def endpoints(self, ports: Dict[str, int]) -> Dict[str, str]:
        """Tool endpoint URL -> the same URL on the port the service got."""
        out = {}
        for svc in self.services:
            out[svc.name] = svc.port
        return out

    def audit(self) -> Dict[str, Any]:
        return {svc.name: svc.audit() for svc in self.services}

    def stop(self) -> None:
        for svc in self.services:
            svc.stop()
        self.services = []


def rewrite(declared: str, services: List[Dict[str, Any]], ports: Dict[str, int]) -> str:
    """An upstream endpoint URL, on the port its service actually got.

    The declaration pairs each service with its port (``port: 9105``), and the
    tool endpoints name that port in their URLs; the driver looks the port up
    and swaps it, so a service on 41001 is what ``localhost:9105`` means now.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(declared)
    if not parts.port:
        return declared
    for spec in services:
        if int(spec.get("port") or 0) == parts.port and str(spec.get("name")) in ports:
            return _with_port(declared, ports[str(spec["name"])])
    return declared
