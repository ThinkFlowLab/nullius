"""Fail closed on declared public-web requirements for evaluated agents.

Original setups and controls are separate protocols. A reachable HTTPS probe
proves connectivity, not that an agent can search, browse or interpret media.
"""
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def problems(value):
    if not isinstance(value, dict) or set(value) != {"access", "probe"}:
        return ["network: {access: public, probe: an HTTPS URL}"]
    try:
        u = urlsplit(value["probe"])
        valid = (value["access"] == "public" and u.scheme == "https" and u.hostname
                 and not u.username and not u.password and not u.query and not u.fragment
                 and u.port in (None, 443))
    except (TypeError, ValueError, AttributeError):
        valid = False
    return [] if valid else ["network: public access with an HTTPS probe URL without credentials, query or fragment"]


def required(bench, other):
    if other.get("_kind") != "agent":
        return []
    # Environment selection may also be made per unit by the driver. Require
    # the declared connectivity before launching any evaluated agent.
    return sorted({e["network"]["probe"] for e in (bench.get("environments") or {}).values()
                   if e.get("network")})


def check(bench, other, backend, opener=urlopen):
    # Docker probes run inside the unit network, through its public proxy.
    if backend == "docker":
        return []
    for url in required(bench, other):
        try:
            # Do not send provider credentials or benchmark access tokens.
            with opener(Request(url, headers={"User-Agent": "Nullius-connectivity-check"}), timeout=10) as response:
                if not 200 <= response.status < 400:
                    raise OSError(f"HTTP {response.status}")
        except Exception as exc:
            return [f"{bench['label']} requires public HTTPS access; connectivity check failed "
                    f"for {url}: {type(exc).__name__}. No tasks were started."]
    return []
