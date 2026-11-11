"""Template filters and the context every page renders with."""
from __future__ import annotations

import json
import re
from pathlib import Path

from nullius import importers as harnesses
from nullius.runs import classify
from nullius import workspace
from nullius.standard import ledger, ports, definitions

from .core import app
from . import shared


AGENT_MARKS = json.loads((Path(app.static_folder) / "catalogue/agents/marks.json").read_text())



@app.template_filter("pct")
def _pct(value):
    return "—" if value is None else f"{value * 100:.0f}%"


@app.template_filter("runtime")
def _runtime(minutes):
    """A run's start to finish: seconds under a minute, so a run of forty seconds is not "0m"."""
    if minutes is None:
        return "—"
    return f"{minutes * 60:.0f}s" if minutes < 1 else f"{minutes:.0f}m"


@app.template_filter("hitrate")
def _hitrate(value):
    """Cache hit share. One decimal — two when rounding would claim 100%.

    99.97% is the signature of an Anthropic-style denominator;
    flattening it to 100.0% would hide exactly the artifact worth seeing.
    """
    if value is None:
        return "—"
    text = f"{value * 100:.1f}%"
    return f"{value * 100:.2f}%" if text == "100.0%" and value < 1 else text


@app.template_filter("share")
def _share(value):
    """A segment's share of the wall clock.

    Never a bare "0%" for time that was really spent: a span worth 40µs of a
    six-second run is 0.0007% and rounding it away would read as "not measured".
    """
    if value is None:
        return "—"
    if value <= 0:
        return "0%"
    if value < 1:
        return "<1%"
    return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


@app.template_filter("nsdelta")
def _nsdelta(value):
    """A signed change in nanoseconds — "" when nothing moved.

    A true minus sign, not a hyphen: these sit next to durations in a legend and
    a hyphen there reads as part of the number.
    """
    if not value:
        return ""
    return ("−" if value < 0 else "+") + _nsdur(abs(value))


@app.template_filter("tokens")
def _tokens(value, kind="agent"):
    """Token count. For a control the zero is real, so render it as 0.

    For a real agent, 0/None means the run happened and we failed to capture
    it — that stays "—". Same glyph for both would make a control look broken
    and a broken measurement look deliberate.
    """
    if not value:
        return "0" if kind == "control" else "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


@app.template_filter("usd")
def _usd(value, kind="agent"):
    """Cost. A control genuinely spent $0.00; an agent with no figure shows —."""
    if value is None:
        return "$0.00" if kind == "control" else "—"
    return f"${value:.4f}" if value < 0.01 else f"${value:.2f}"


@app.template_filter("cap")
def _cap(value):
    """A spending cap as it was given, and what was spent of it: cents, and the digits past them it has
    ($0.395, $0.00005). The cost filter rounds to the cent, which would show a cap the run never had."""
    return "$" + ledger.usd(value)


@app.template_filter("dsname")
def _dsname(value):
    return shared.dataset_title(value)


@app.template_filter("dt")
def _dt(value):
    return value.strftime("%b %d  %H:%M") if value else "—"


@app.template_filter("ts")
def _ts(value):
    """Sort key for a datetime column; missing values sort first."""
    return f"{value.timestamp():.0f}" if value else "-1"


@app.template_filter("phase")
def _phase(value):
    """Flex weight for a phase segment, or 0 when the phase is effectively nil.

    Harbor records a control's agent setup as 4.2e-05s — truthy, but it renders
    as "0s" and would still cost a flex gap and a sub-pixel sliver, implying
    setup time that never happened. Anything under the 0.05s that `secs` rounds
    to zero paints nothing at all.
    """
    value = value or 0
    return value if value >= 0.05 else 0


@app.template_filter("secs")
def _secs(value):
    """Seconds with the precision the clock behind them has.

    Show tenths below ten seconds, whole seconds below a minute, and tenths
    of minutes above that.
    """
    if value is None:
        return "—"
    if value < 9.95:                       # what .1f would still show as one digit
        return f"{value:.1f}s"
    return f"{value:.0f}s" if value < 60 else f"{value / 60:.1f}m"


@app.template_filter("nsround")
def _nsround(value):
    """A duration for a chart's axis or headline: ``100ms``, not ``100.00ms``."""
    return re.sub(r"\.0+(?=\D|$)", "", _nsdur(value))


@app.template_filter("nsdur")
def _nsdur(value):
    """A span duration, scaled to its own magnitude.

    None is not zero: it means the span recorded one instant and has no known
    end. Rendering that as "0.00ms" would claim a measurement nobody made.
    """
    if value is None:
        return "—"
    if value < 1_000:
        return f"{value}ns"
    if value < 1_000_000:
        return f"{value / 1_000:.0f}µs"
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.2f}ms"
    if value < 60_000_000_000:
        return f"{value / 1_000_000_000:.2f}s"
    # Hands over to `secs`'s scale at the same minute mark, so a trace covering
    # an hour reads like the phase bar above it instead of as "3600.00s".
    return f"{value / 60_000_000_000:.1f}m"


@app.template_filter("nsoff")
def _nsoff(value):
    """An offset from the start of the trace — always relative.

    Never render the absolute epoch nanoseconds these are derived from: the
    trace's own clock anchor exists precisely because the container clock and
    the host clock disagree, so an absolute instant is not the exact thing its
    19 digits imply. A difference between two stamps from the same clock is.
    """
    if value is None:
        return "—"
    if not value:
        return "0"          # the axis origin, not "+0ns"
    return "+" + _nsdur(value)


@app.context_processor
def _inject():
    return {
        "LABELS": classify.LABELS,
        "agent_marks": AGENT_MARKS,
        "DESCRIPTIONS": classify.DESCRIPTIONS,
        "INFRA": classify.INFRA_OUTCOMES,
        "ORDER": classify.ORDER,
        "workspace": shared.STATE["workspace"],
        "record_workspace": shared._record_workspace(),
        # For the rail's switcher: the most recent few, and a stable hue per
        # name so a workspace keeps its colour across pages and restarts.
        "workspaces": workspace.list_all(shared.WS_HOME)[:8],
        "ws_hue": lambda name: sum(ord(c) * (i + 7) for i, c in enumerate(name or "")) % 360,
        # The open workspace's runs, for the rail's tree — from the cache index,
        # so listing them costs one small file, not a parse.
        "nav_runs": sorted(
            workspace.job_index(shared.STATE["workspace"]).values(), key=lambda e: e["job_id"]
        )[:12] if shared.STATE["workspace"] else [],
        # The catalogue's two trees in the rail: one small JSON read, cached
        # on its mtime so a page does not re-read it.
        "nav_catalog": shared._catalog_cached(),
        "nav_kits": [{"key": p["kit"], "label": p["label"]}
                     for p in ports.PORTS.values() if p.get("kit")],
        "nav_definitions": {
            kind: sorted(({"key": d["key"], "label": definitions.title(d), "example": bool(d.get("example"))}
                          for d in definitions.load(kind)[0]), key=lambda d: (not d["example"], d["label"].casefold()))
            for kind in ("agent", "capability")
        },
        # Named on the import page so what is readable stays discoverable
        # without asking anyone to choose it. Empty when no importer is
        # installed, and the sentence disappears with it.
        "IMPORTERS": [
            getattr(m, "LABEL", n.upper()) for n, m in harnesses.available().items()
        ],
        # The runs list's Columns menu; the template reads it by this name.
        "COLUMNS": shared.COLUMNS,
    }
