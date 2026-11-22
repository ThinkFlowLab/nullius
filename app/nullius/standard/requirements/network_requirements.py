"""Fail closed on declared public-web requirements for evaluated agents.

Original setups and controls are separate protocols. A reachable HTTPS probe
proves connectivity, not that an agent can search, browse or interpret media.
"""
from urllib.request import Request, urlopen


def required(bench, other):
    if other.get("_kind") != "agent":
        return []
    # Environment selection may also be made per unit by the driver. Require
    # the declared connectivity before launching any evaluated agent.
    return sorted({e["network"]["probe"] for e in (bench.get("environments") or {}).values()
                   if e.get("network")})


def check(bench, other, opener=urlopen):
    # The same check on every backend: once, before any task. In a container the
    # run's first unit also checks the route through its public proxy.
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
