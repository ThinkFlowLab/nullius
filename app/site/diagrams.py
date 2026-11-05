"""Draw the documentation's figures, one SVG each under docs/diagrams/.

Every figure is laid out here by hand: nodes on a 4px grid, orthogonal
connectors with rounded elbows, each arrow label on a paper mask clear of
its line, zones painted first and nodes last. The pages embed the files as
images, which GitHub renders as they are; the website build inlines them
(`hooks.py`) so their colours follow the page's own variables in both
schemes. The fallbacks in each file's style are the light scheme's.

    python3 app/site/diagrams.py --write   # rewrite the files
    python3 app/site/diagrams.py --check   # exit 1 when a file is stale

`app/tests/test_site.py` runs the check.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / 'docs' / 'diagrams'

SANS, MONO = 0.60, 0.62           # em per character, the budget the layout is held to
NAME, SUB, LABEL, EYEBROW = 12, 9, 8, 8

STYLE = """
.dg{--p:var(--paper,#f7f8fa);--i:var(--ink,#25282e);--m:var(--muted,#596471);--a:var(--md-typeset-a-color,#36577d);--n:#fff;
 --sans:'DM Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace;--serif:'Instrument Serif',Georgia,serif}
[data-md-color-scheme=slate] .dg{--n:color-mix(in srgb,var(--i) 7%,var(--p))}
.dg .paper{fill:var(--p)}
.dg .zone{fill:color-mix(in srgb,var(--i) 3%,transparent);stroke:color-mix(in srgb,var(--i) 14%,transparent);stroke-width:.8}
.dg .eyebrow{fill:var(--m);font:500 8px var(--mono);letter-spacing:.14em}
.dg .box{fill:var(--n);stroke:var(--i);stroke-width:1}
.dg .focal{fill:color-mix(in srgb,var(--a) 12%,transparent);stroke:var(--a);stroke-width:1}
.dg .store{fill:color-mix(in srgb,var(--i) 5%,transparent);stroke:var(--m);stroke-width:1}
.dg .ext{fill:color-mix(in srgb,var(--i) 3%,transparent);stroke:color-mix(in srgb,var(--i) 40%,transparent);stroke-width:1}
.dg .input{fill:color-mix(in srgb,var(--m) 10%,transparent);stroke:var(--m);stroke-width:1}
.dg .name{fill:var(--i);font:500 12px var(--sans)}
.dg .sub{fill:var(--m);font:9px var(--mono)}
.dg .lbl{fill:var(--m);font:8px var(--mono);letter-spacing:.06em}
.dg .lbl.accent{fill:var(--a)}
.dg .arrow{fill:none;stroke:var(--m);stroke-width:1.2}
.dg .arrow.accent{stroke:var(--a)}
.dg .arrow.dashed{stroke-dasharray:5 4;stroke-width:1}
.dg .arrow.bus{stroke-width:1}
.dg .head{fill:var(--m)}
.dg .head.accent{fill:var(--a)}
.dg .dot{fill:var(--i)}
.dg .life{stroke:color-mix(in srgb,var(--i) 25%,transparent);stroke-width:1;stroke-dasharray:3 3}
.dg .act{fill:color-mix(in srgb,var(--i) 6%,transparent);stroke:var(--m);stroke-width:.8}
.dg .frame{fill:color-mix(in srgb,var(--i) 2%,transparent);stroke:color-mix(in srgb,var(--i) 22%,transparent);stroke-width:1}
.dg .tab{fill:var(--p);stroke:color-mix(in srgb,var(--i) 22%,transparent);stroke-width:1}
.dg .guard{fill:var(--m);font:8px var(--mono);letter-spacing:.04em}
.dg .note{fill:var(--m);font:13px var(--sans)}
.dg .rule{stroke:color-mix(in srgb,var(--i) 12%,transparent);stroke-width:.8}
.dg .legend{fill:var(--m);font:9px var(--sans)}
"""
# The light values the classes above resolve to, written as attributes too so a
# viewer without the stylesheet, or a checker reading attributes, sees the figure.
LIGHT = {'paper': '#f7f8fa', 'ink': '#25282e', 'muted': '#596471', 'accent': '#36577d', 'node': '#ffffff'}
FILL = {'box': ('#ffffff', LIGHT['ink']), 'focal': ('#e8edf4', LIGHT['accent']), 'store': ('#e0e1e3', LIGHT['muted']),
        'ext': ('#e4e5e7', '#7a7c80'), 'input': ('#dedfe1', LIGHT['muted'])}


def grid(*values: int) -> None:
    for v in values:
        if v % 4:
            raise ValueError(f'{v} is off the 4px grid')


def width(text: str, size: int, em: float) -> float:
    return len(text) * size * em


def fits(text: str, size: int, em: float, room: float) -> None:
    if width(text, size, em) > room:
        raise ValueError(f'{text!r} needs {width(text, size, em):.0f}px, has {room:.0f}')


def esc(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class Figure:
    """One SVG: its layers are emitted zones, connectors, labels, then nodes."""

    def __init__(self, slug: str, w: int, h: int, title: str, desc: str):
        grid(w, h)
        self.slug, self.w, self.h, self.title, self.desc = slug, w, h, title, desc
        self.zones: list[str] = []
        self.lines: list[str] = []
        self.labels: list[str] = []
        self.nodes: list[str] = []
        self.after: list[str] = []

    def node(self, x: int, y: int, w: int, h: int, name: str, subs=(), kind='box', rx=6) -> None:
        grid(x, y, w, h)
        subs = [subs] if isinstance(subs, str) else list(subs)
        fits(name, NAME, SANS, w - 16)
        for s in subs:
            fits(s, SUB, MONO, w - 16)
        fill, stroke = FILL[kind]
        cx, cy = x + w / 2, y + h / 2
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{LIGHT["paper"]}" class="paper"/>',
               f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" class="{kind}"/>']
        if not subs:
            out.append(self.text(cx, cy + 4, name, 'name'))
        else:
            base = round(cy - 2 - 5.5 * (len(subs) - 1))
            out.append(self.text(cx, base, name, 'name'))
            out += [self.text(cx, base + 14 + 11 * k, sub, 'sub') for k, sub in enumerate(subs)]
        self.nodes += out

    def diamond(self, cx: int, cy: int, w: int, h: int, name: str) -> None:
        grid(cx, cy, w, h)
        fits(name, NAME, SANS, w * 0.7)
        pts = f'{cx - w // 2},{cy} {cx},{cy - h // 2} {cx + w // 2},{cy} {cx},{cy + h // 2}'
        self.nodes += [f'<polygon points="{pts}" fill="{LIGHT["paper"]}" class="paper"/>',
                       f'<polygon points="{pts}" fill="#ffffff" stroke="{LIGHT["ink"]}" class="box"/>',
                       self.text(cx, cy + 4, name, 'name')]

    def merge(self, cx: int, cy: int) -> None:
        self.nodes.append(f'<circle cx="{cx}" cy="{cy}" r="4" fill="{LIGHT["ink"]}" class="dot"/>')

    @staticmethod
    def text(x: float, y: float, s: str, cls: str, anchor='middle') -> str:
        return f'<text x="{x:g}" y="{y:g}" text-anchor="{anchor}" class="{cls}">{esc(s)}</text>'

    def zone(self, x: int, y: int, w: int, h: int, label: str) -> None:
        grid(x, y, w, h)
        lw = round(len(label) * EYEBROW * (MONO + 0.14) + 16)
        self.zones += [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#e6e6e8" stroke="#cfd0d2" class="zone"/>',
                       f'<rect x="{x + 12}" y="{y + 4}" width="{lw}" height="12" rx="2" fill="{LIGHT["paper"]}" class="paper"/>',
                       self.text(x + 20, y + 13, label, 'eyebrow', 'start')]

    def path(self, points, kind='', head=True, r=8) -> None:
        """An orthogonal connector through `points`, elbows rounded at `r`."""
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if x1 != x2 and y1 != y2:
                raise ValueError(f'diagonal segment {(x1, y1)}-{(x2, y2)} in {self.slug}')
        d = [f'M{points[0][0]},{points[0][1]}']
        for i in range(1, len(points) - 1):
            (ax, ay), (vx, vy), (bx, by) = points[i - 1], points[i], points[i + 1]
            d1 = min(r, (abs(vx - ax) + abs(vy - ay)) / 2)
            d2 = min(r, (abs(bx - vx) + abs(by - vy)) / 2)
            ix, iy = (vx - ax) and (1 if vx > ax else -1), (vy - ay) and (1 if vy > ay else -1)
            ox, oy = (bx - vx) and (1 if bx > vx else -1), (by - vy) and (1 if by > vy else -1)
            d.append(f'L{vx - ix * d1:g},{vy - iy * d1:g} Q{vx},{vy} {vx + ox * d2:g},{vy + oy * d2:g}')
        d.append(f'L{points[-1][0]},{points[-1][1]}')
        self.lines.append(self._stroke(' '.join(d), kind, head))

    def _stroke(self, d: str, kind: str, head: bool) -> str:
        colour = LIGHT['accent'] if 'accent' in kind else LIGHT['muted']
        marker = ''
        if head:
            which = 'accent' if 'accent' in kind else ('open' if 'open' in kind else 'arrow')
            marker = f' marker-end="url(#{self.slug}-{which})"'
        dash = ' stroke-dasharray="5 4"' if 'dashed' in kind else ''
        cls = ' '.join(['arrow'] + [k for k in kind.split() if k in ('accent', 'dashed', 'bus')])
        return f'<path d="{d}" fill="none" stroke="{colour}"{dash}{marker} class="{cls}"/>'

    def label(self, cx: float, cy: float, s: str, kind='') -> None:
        """A mask and its caption centred at (cx, cy); the caller keeps it off the line."""
        w = -(-int(width(s, LABEL, MONO) + 8) // 4) * 4
        cls = 'lbl accent' if 'accent' in kind else 'lbl'
        self.labels += [f'<rect x="{cx - w / 2:g}" y="{cy - 6:g}" width="{w}" height="12" rx="2" fill="{LIGHT["paper"]}" class="paper"/>',
                        self.text(cx, cy + 4, s, cls)]

    def actor(self, cx: int, y: int, name: str, sub='', w=120, h=48, kind='box') -> None:
        self.node(cx - w // 2, y, w, h, name, [sub] if sub else (), kind)

    def lifeline(self, cx: int, top: int, bottom: int) -> None:
        self.zones.append(f'<line x1="{cx}" y1="{top}" x2="{cx}" y2="{bottom}" stroke="#b4b5b8" stroke-dasharray="3 3" class="life"/>')

    def activation(self, cx: int, top: int, bottom: int) -> None:
        grid(top, bottom)
        self.zones.append(f'<rect x="{cx - 4}" y="{top}" width="8" height="{bottom - top}" fill="#e0e1e3" stroke="{LIGHT["muted"]}" class="act"/>')

    def message(self, x1: int, x2: int, y: int, s: str, kind='', at=None) -> None:
        """A horizontal message; its caption sits 8px above the line, centred on
        the segment or at `at`, which keeps it off a lifeline the message crosses."""
        self.path([(x1, y), (x2, y)], kind)
        fits(s, LABEL, MONO, (abs(x2 - x1) if at is None else 104) - 24)
        self.label((x1 + x2) / 2 if at is None else at, y - 14, s, kind)

    def self_message(self, cx: int, y: int, s: str, kind='', room=160) -> None:
        """A loop out of and back into one lifeline, captioned to its right."""
        self.path([(cx + 4, y), (cx + 28, y), (cx + 28, y + 16), (cx + 8, y + 16)], kind)
        fits(s, LABEL, MONO, room - 8)
        w = -(-int(width(s, LABEL, MONO) + 8) // 4) * 4
        self.labels += [f'<rect x="{cx + 36}" y="{y + 2}" width="{w}" height="12" rx="2" fill="{LIGHT["paper"]}" class="paper"/>',
                        self.text(cx + 40, y + 12, s, 'lbl', 'start')]

    def fragment(self, x: int, y: int, w: int, h: int, op: str, guard: str) -> None:
        grid(x, y, w, h)
        self.zones += [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="#e7e7e9" stroke="#b9babd" class="frame"/>',
                       f'<rect x="{x}" y="{y}" width="40" height="16" rx="2" fill="{LIGHT["paper"]}" stroke="#b9babd" class="tab"/>',
                       self.text(x + 20, y + 12, op, 'eyebrow'),
                       self.text(x + 12, y + 32, guard, 'guard', 'start')]

    def note(self, cx: int, y: int, s: str) -> None:
        self.after.append(self.text(cx, y, s, 'note'))

    def legend(self, y: int, items) -> None:
        """A bottom strip: ('box'|'focal'|..., text) swatches and ('arrow kind', text) strokes."""
        grid(y)
        self.after += [f'<line x1="24" y1="{y - 12}" x2="{self.w - 24}" y2="{y - 12}" stroke="#d6d7d9" class="rule"/>',
                       self.text(24, y + 8, 'LEGEND', 'eyebrow', 'start')]
        x = 96
        for kind, caption in items:
            if kind in FILL:
                fill, stroke = FILL[kind]
                self.after.append(f'<rect x="{x}" y="{y}" width="16" height="10" rx="2" fill="{fill}" stroke="{stroke}" class="{kind}"/>')
            elif kind == 'frame':
                self.after.append(f'<rect x="{x}" y="{y - 2}" width="20" height="14" rx="2" fill="#e7e7e9" stroke="#b9babd" class="frame"/>')
            else:
                self.after.append(self._stroke(f'M{x},{y + 5} L{x + 28},{y + 5}', kind, True))
                x += 12
            self.after.append(self.text(x + 22, y + 9, caption, 'legend', 'start'))
            x += 22 + int(width(caption, 9, SANS)) + 24

    def svg(self) -> str:
        s, w, h = self.slug, self.w, self.h
        head = (f'<marker id="{s}-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
                f'<polygon points="0 0, 8 3, 0 6" fill="{LIGHT["muted"]}" class="head"/></marker>'
                f'<marker id="{s}-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
                f'<polygon points="0 0, 8 3, 0 6" fill="{LIGHT["accent"]}" class="head accent"/></marker>'
                f'<marker id="{s}-open" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
                f'<polyline points="0 0, 8 3, 0 6" fill="none" stroke="{LIGHT["muted"]}" stroke-width="1.2" class="arrow"/></marker>')
        body = '\n'.join(self.zones + self.lines + self.labels + self.nodes + self.after)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                f'role="img" aria-labelledby="{s}-title {s}-desc" class="dg">\n'
                f'<title id="{s}-title">{esc(self.title)}</title>\n<desc id="{s}-desc">{esc(self.desc)}</desc>\n'
                f'<defs>{head}</defs>\n<style>{STYLE}</style>\n'
                f'<rect width="100%" height="100%" fill="{LIGHT["paper"]}" class="paper"/>\n{body}\n</svg>\n')


# ---------------------------------------------------------------------------
# The figures. Coordinates are the layout; comments say what each decision keeps.

def pieces() -> Figure:
    f = Figure('pieces', 688, 480, 'The pieces of the application',
               'The browser talks to the Flask routes, the backend reads and writes the host’s disk, '
               'and reaches a run host, GitHub and PyPI, the model provider and the judge.')
    f.zone(24, 24, 304, 96, 'BROWSER · LOOPBACK')
    f.zone(24, 144, 304, 184, 'NULLIUS · FLASK · APP/WEB')
    f.zone(352, 24, 312, 312, 'ON THE APPLICATION HOST')
    f.zone(24, 360, 640, 96, 'ELSEWHERE')
    # The request path, top to bottom, then the backend fans out.
    f.path([(176, 104), (176, 176)])
    f.path([(176, 224), (176, 248)])
    f.path([(312, 260), (320, 260), (320, 80), (368, 80)])        # settings.json
    f.path([(312, 272), (332, 272), (332, 152), (368, 152)])      # workspaces
    f.path([(312, 284), (344, 284), (344, 224), (368, 224)])      # runs
    f.path([(312, 296), (368, 296)])                              # harbor-pins, run-part.sh
    f.path([(108, 312), (108, 392)])                              # GitHub, PyPI
    f.label(140, 346, 'REGISTRY')
    f.path([(200, 312), (200, 352), (264, 352), (264, 392)])      # judge
    f.label(232, 338, 'REDACTED')
    f.path([(280, 312), (280, 344), (420, 344), (420, 392)])      # ssh host
    f.label(324, 330, 'SSH · RSYNC')
    f.path([(508, 320), (508, 356), (580, 356), (580, 392)])      # the local Harbor calls the model
    f.label(544, 342, 'HARBOR RUN')
    f.path([(488, 416), (512, 416)])                              # the host's Harbor calls the model
    f.node(40, 56, 272, 48, 'Pages', 'runs, job, trial, compare, builder, settings', 'input')
    f.node(40, 176, 272, 48, 'Routes', 'app/web, one module per capability')
    f.node(40, 248, 272, 64, 'Backend', 'app/nullius, imports no Flask', 'focal')
    f.node(368, 56, 280, 48, 'settings.json', 'user configuration directory', 'store')
    f.node(368, 128, 280, 48, 'workspaces/<name>/', 'parse cache, imports, judgements', 'store')
    f.node(368, 200, 280, 48, 'runs/<name>/', 'run record, logs, jobs', 'store')
    f.node(368, 272, 280, 48, 'harbor-pins/<pin>/', 'built wheels, run with uvx, detached', 'store')
    f.node(40, 392, 136, 48, 'GitHub · PyPI', 'refs, releases', 'ext')
    f.node(196, 392, 136, 48, 'Judge', 'TypeSafe.ai', 'ext')
    f.node(352, 392, 136, 48, 'SSH run host', 'harbor run', 'ext')
    f.node(512, 392, 136, 48, 'Model provider', 'called by the run', 'ext')
    return f


def path_open() -> Figure:
    f = Figure('path-open', 640, 432, 'Path 1: open results that exist',
               'Roots given to the runs pages are recognised, converted when foreign, parsed into the '
               'workspace cache, and the pages read that cache back.')
    you, web, imp, par, ws = 80, 200, 320, 440, 560
    for cx in (you, web, imp, par, ws):
        f.lifeline(cx, 72, 384)
    f.activation(web, 104, 200)
    f.activation(imp, 144, 200)
    f.activation(par, 224, 280)
    f.activation(web, 224, 360)
    f.message(you, web - 4, 112, '/IMPORT · ROOTS')
    f.message(web + 4, imp - 4, 152, 'RECOGNISED?')
    f.message(imp + 4, ws, 192, 'INTO IMPORTED/', 'dashed', at=380)
    f.message(web + 4, par - 4, 232, 'PARSE EACH JOB', at=260)
    f.message(par + 4, ws, 272, 'PARSE CACHE', 'dashed')
    f.message(you, web - 4, 312, '/RUNS /JOB /TRIAL')
    f.message(web + 4, ws, 352, 'READ · RE-PARSE')
    f.actor(you, 24, 'You', kind='input')
    f.actor(web, 24, 'Runs pages', 'web/runs')
    f.actor(imp, 24, 'Importers', 'nullius/importers')
    f.actor(par, 24, 'Parser', 'parse, classify')
    f.actor(ws, 24, 'Workspace', 'workspaces/<name>/', kind='store')
    f.legend(408, [('arrow', 'Call'), ('dashed', 'Write')])
    return f


def path_run_here() -> Figure:
    f = Figure('path-run-here', 640, 528, 'Path 2: build a recipe and run it here',
               'The builder posts the recipe, execution records the run and starts each part through the '
               'pinned Harbor, which calls the model, and the job page follows it live.')
    you, bld, exe, hbr, llm = 80, 200, 320, 440, 560
    for cx in (you, bld, exe, hbr, llm):
        f.lifeline(cx, 72, 480)
    f.activation(bld, 104, 200)
    f.activation(exe, 184, 480)
    f.activation(hbr, 312, 408)
    f.fragment(296, 264, 288, 160, 'LOOP', '[nop, oracle, then the agents]')
    f.message(you, bld - 4, 112, 'EACH STEP, REVIEW')
    f.message(you, bld - 4, 152, 'RUN')
    f.message(bld + 4, exe - 4, 192, 'POST /RUN')
    f.self_message(exe, 224, 'RUN.JSON · KEY', room=84)
    f.message(exe + 4, hbr - 4, 320, 'RUN-PART.SH')
    f.message(hbr + 4, llm, 360, 'MODEL CALLS')
    f.message(hbr - 4, exe + 4, 400, 'JOBS/, EVERY 10 S', 'dashed')
    f.message(exe - 4, you, 464, 'JOB PAGE · LIVE', 'accent', at=260)
    f.actor(you, 24, 'You', kind='input')
    f.actor(bld, 24, 'Builder', 'page, recipe core')
    f.actor(exe, 24, 'Execution', 'nullius/execution', kind='focal')
    f.actor(hbr, 24, 'Harbor', 'pinned, via uvx')
    f.actor(llm, 24, 'Model provider', kind='ext')
    f.legend(504, [('arrow', 'Call'), ('dashed', 'Return'), ('accent', 'What you see'), ('frame', 'Loop')])
    return f


def path_run_host() -> Figure:
    f = Figure('path-run-host', 640, 416, 'Path 3: the same on an SSH host',
               'Execution probes the host, sends the part and the key, starts it detached, then tails '
               'the log and pulls the jobs back every ten seconds.')
    exe, host = 160, 480
    for cx in (exe, host):
        f.lifeline(cx, 72, 368)
    f.activation(exe, 104, 344)
    f.activation(host, 184, 344)
    f.fragment(136, 224, 368, 120, 'LOOP', '[every 10 s]')
    f.message(exe + 4, host - 4, 112, 'PROBE: DOCKER, UV (INSTALLED IF MISSING)')
    f.message(exe + 4, host - 4, 152, 'SEND WHEELS, TASKS, RUN-PART.SH · KEY OVER STDIN')
    f.message(exe + 4, host - 4, 192, 'START RUN-PART.SH, DETACHED')
    f.message(exe + 4, host - 4, 280, 'TAIL THE LOG, READ THE EXIT FILE')
    f.message(host - 4, exe + 4, 320, 'RSYNC JOBS/ INTO RUNS/<NAME>/JOBS/', 'dashed')
    f.note(320, 392, 'Nullius closed: the part keeps running, and a restart picks it up.')
    f.actor(exe, 24, 'Execution', 'nullius/execution', kind='focal')
    f.actor(host, 24, 'SSH host', '~/.nullius/', kind='ext')
    return f


def run_leaves() -> Figure:
    f = Figure('run-leaves', 640, 392, 'What a run leaves behind',
               'A run directory holds its record and one job directory, whose trials each keep the '
               'agent’s output, the verifier’s and the artifacts.')
    f.path([(320, 88), (320, 120)])
    f.path([(320, 184), (320, 216)])
    f.path([(320, 280), (320, 296)], 'bus', head=False)
    f.path([(120, 296), (520, 296)], 'bus', head=False)
    for cx in (120, 320, 520):
        f.path([(cx, 296), (cx, 312)])
    f.node(232, 24, 176, 64, 'runs/<name>/', ['run.json: plan and state', 'run-part.sh, log, exit file'], 'store')
    f.node(232, 120, 176, 64, 'jobs/<job-name>/', ['config.json, result.json,', 'events.jsonl'], 'store')
    f.node(232, 216, 176, 64, '<trial>/', ['result.json, trial.log,', 'exception.txt'], 'store')
    f.node(40, 312, 160, 56, 'agent/', ['trajectory.json, logs,', 'traces'], 'focal')
    f.node(240, 312, 160, 56, 'verifier/', 'reward, test output', 'store')
    f.node(440, 312, 160, 56, 'artifacts/', kind='store')
    return f


def pages() -> Figure:
    f = Figure('pages', 656, 296, 'Page by page',
               'The runs page opens a job, a job opens its trials, live board, reconcile and feasibility '
               'views, and the runs page also opens the comparison.')
    f.path([(160, 56), (208, 56)])
    f.path([(92, 80), (92, 104)])
    f.path([(384, 56), (456, 56)], 'accent')
    f.path([(224, 80), (224, 252)], 'bus', head=False)
    for y in (124, 188, 252):
        f.path([(224, y), (240, y)])
    f.node(24, 32, 136, 48, 'Runs', '/runs', 'input')
    f.node(24, 104, 136, 40, 'Compare', '/compare')
    f.node(208, 32, 176, 48, 'Job', '/job/<id>')
    f.node(240, 104, 176, 40, 'Live board', 'while it runs')
    f.node(240, 168, 176, 40, 'Reconcile', '/job/<id>/reconcile')
    f.node(240, 232, 176, 40, 'Feasibility', '/job/<id>/feasibility')
    f.node(456, 20, 176, 72, 'Trial', ['/trial/<job>/<name>', 'conversation, trace,', 'artifacts, judgement'], 'focal')
    return f


def builder_stages() -> Figure:
    f = Figure('builder-stages', 464, 520, 'The builder’s stages',
               'Eight stages in order, from the runner to the review, which runs the recipe here or on a '
               'host.')
    names = ['Runner', 'Benchmark', 'Dataset', 'Agent', 'Model', 'Limits', 'Where', 'Review']
    for i, name in enumerate(names):
        y = 24 + i * 56
        if i:
            f.path([(96, y - 20), (96, y)])
        f.node(24, y, 144, 36, name, kind='focal' if name == 'Review' else 'box')
    f.path([(168, 430), (212, 430), (212, 404), (240, 404)], 'accent')
    f.node(240, 380, 200, 48, 'Run', 'here, or on a host')
    return f


def builder_run() -> Figure:
    f = Figure('builder-run', 720, 600, 'Run',
               'The page posts the recipe, Nullius checks what the runner needs of the machine, prepares the '
               'target, runs each part detached while polling it, pauses on a failure, then shows the job.')
    page, nul, tgt = 120, 360, 600
    for cx in (page, nul, tgt):
        f.lifeline(cx, 72, 552)
    f.activation(nul, 104, 548)
    f.activation(tgt, 224, 344)
    f.fragment(96, 264, 588, 240, 'LOOP', '[nop, oracle, then the agents]')
    f.fragment(104, 392, 340, 96, 'OPT', '[a part failed, more to come]')
    f.message(page, nul - 4, 112, 'POST /RUN · SCRIPT, PARTS, RECIPE')
    f.self_message(nul, 136, 'HERE: DOCKER PS ANSWERS', room=200)
    f.self_message(nul, 176, 'RUNNER PREPARED ONCE', room=200)
    f.message(nul + 4, tgt - 4, 232, 'SSH: DOCKER, UV · THE RUNNER SENT')
    f.message(nul + 4, tgt - 4, 320, 'RUN-<PART>.SH DETACHED · PID, EXIT FILE')
    f.self_message(nul, 344, 'EVERY 10 S: LOG TAIL, RSYNC JOBS/', 'dashed', room=200)
    f.message(nul - 4, page, 456, 'PAUSE: CONTINUE OR STOP')
    f.message(nul - 4, page, 536, 'JOB PAGE: RUN LOG, THEN LIVE', 'accent')
    f.actor(page, 24, 'Page', 'the builder', kind='input')
    f.actor(nul, 24, 'Nullius', 'web/execute, execution', kind='focal', w=144)
    f.actor(tgt, 24, 'Target', 'here, or a host', kind='ext')
    f.legend(576, [('arrow', 'Call'), ('dashed', 'Poll'), ('accent', 'What you see'), ('frame', 'Fragment')])
    return f


def judge_flow() -> Figure:
    f = Figure('judge-flow', 720, 576, 'From a trial to an answer',
               'The trial’s files are redacted and trimmed into a state, the page’s questions and two '
               'hygiene checks go to the judge in one call, and the record is kept and shown.')
    f.zone(24, 136, 672, 96, '1 · BUILD A STATE')
    f.zone(24, 248, 672, 96, '2 · ONE CALL PER STATE')
    f.zone(24, 432, 672, 96, '3 · RECORD AND SHOW')
    f.after.append(f.text(24, 28, 'INPUT · THE TRIAL AS RECORDED', 'eyebrow', 'start'))
    f.path([(136, 88), (136, 124), (212, 124), (212, 168)])
    f.path([(360, 88), (360, 112), (256, 112), (256, 168)])
    f.path([(584, 88), (584, 132), (300, 132), (300, 168)])
    f.path([(344, 192), (376, 192)])
    f.path([(428, 216), (428, 244), (256, 244), (256, 280)])
    f.path([(464, 216), (464, 280)])
    f.path([(256, 328), (256, 348), (320, 348), (320, 368)], 'accent')
    f.path([(464, 328), (464, 348), (400, 348), (400, 368)], 'accent')
    f.path([(360, 416), (360, 440), (288, 440), (288, 464)])
    f.path([(408, 488), (448, 488)])
    f.node(40, 40, 192, 48, 'Trajectory', 'agent/trajectory.json', 'input')
    f.node(264, 40, 192, 48, 'Result', 'result.json, verifier output', 'input')
    f.node(488, 40, 192, 48, 'Task', 'its instruction', 'input')
    f.node(168, 168, 176, 48, 'Redact secrets')
    f.node(376, 168, 176, 48, 'Trim', 'what bears on the question')
    f.node(168, 280, 176, 48, 'The page’s questions')
    f.node(376, 280, 176, 48, 'Two hygiene checks')
    f.node(272, 368, 176, 48, 'Judge client', 'one call per state', 'focal')
    f.node(168, 464, 240, 48, 'Judgement record', 'workspaces/<name>/judgements/', 'store')
    f.node(448, 464, 192, 48, 'Shown on the page', 'answer, confidence, model, time')
    f.legend(552, [('input', 'The trial'), ('box', 'Step'), ('focal', 'The call'), ('store', 'Kept')])
    return f


def reconcile() -> Figure:
    f = Figure('reconcile', 640, 296, 'Reconcile',
               'The recorded outcome and the judged failure cause either agree, disagree ranked by '
               'confidence, or, with no outcome, leave a suggestion that is never written back.')
    f.path([(184, 112), (224, 112), (224, 140)], head=False)
    f.path([(184, 200), (224, 200), (224, 148)], head=False)
    f.path([(228, 144), (256, 144)])
    f.path([(384, 144), (456, 144)], 'accent')
    f.label(420, 130, 'YES', 'accent')
    f.path([(320, 184), (320, 224)])
    f.label(344, 204, 'NO')
    f.path([(320, 104), (320, 72)])
    f.label(368, 88, 'NO OUTCOME')
    f.node(24, 88, 160, 48, 'Recorded outcome', 'outcome', 'input')
    f.node(24, 176, 160, 48, 'Judged failure cause', 'failure_cause', 'input')
    f.merge(224, 144)
    f.diamond(320, 144, 128, 80, 'Compatible?')
    f.node(456, 120, 160, 48, 'Agree', kind='focal')
    f.node(240, 224, 160, 48, 'Disagreement', 'ranked by confidence')
    f.node(240, 24, 160, 48, 'Suggestion', 'never written back')
    return f


FIGURES = [pieces, path_open, path_run_here, path_run_host, run_leaves, pages,
           builder_stages, builder_run, judge_flow, reconcile]


def render() -> dict[str, str]:
    return {fig.slug: fig.svg() for fig in (make() for make in FIGURES)}


def stale() -> list[str]:
    return [slug for slug, svg in render().items()
            if not (OUT / f'{slug}.svg').exists() or (OUT / f'{slug}.svg').read_text() != svg]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.write:
        OUT.mkdir(exist_ok=True)
        for slug, svg in render().items():
            (OUT / f'{slug}.svg').write_text(svg)
            print('wrote', OUT / f'{slug}.svg')
    elif args.check:
        old = stale()
        print('stale: ' + ', '.join(old) if old else 'up to date')
        sys.exit(1 if old else 0)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
