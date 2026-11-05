"""Draw the README's six approved animated SVGs in one fixed palette.

    python3 app/site/readme_media.py --write
    python3 app/site/readme_media.py --check

The scenes are conceptual, not interface captures. Compare uses one recorded
SWE-bench Verified task's values: Codex resolved in 3.1m with 178.9k tokens;
mini-swe-agent returned an agent error in 4.7m, tokens not recorded.
The visible task and run labels are generic. No aggregate rate is implied.
The accepted simpler baseline is preserved under previews/readme-motion/baseline/.
"""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / 'docs/assets/nullius/readme'

STYLE = '''
text{font-family:Arial,sans-serif;fill:INK;font-size:17px} .muted{fill:MUTED;font-size:13px}.line{stroke:LINE;stroke-width:1.5;fill:none}.ink{stroke:INK;stroke-width:1.6;fill:none}.blue{stroke:BLUE;stroke-width:2;fill:none}.fill{fill:SURFACE;stroke:LINE;stroke-width:1.5}.dot{fill:BLUE}.ok{stroke:GREEN;fill:none;stroke-width:2}.warn{stroke:AMBER;fill:none;stroke-width:2} .float{animation:float 6s cubic-bezier(.22,1,.36,1) infinite}.arrive{animation:arrive 6s cubic-bezier(.22,1,.36,1) infinite}.reveal{animation:reveal 6s ease infinite}.scan{animation:scan 6s ease-in-out infinite}.pulse{animation:pulse 6s ease infinite}.draw{stroke-dasharray:200;animation:draw 6s ease infinite}.left{animation:left 6s cubic-bezier(.22,1,.36,1) infinite}.right{animation:right 6s cubic-bezier(.22,1,.36,1) infinite}.delay{animation-delay:-2s}.delay2{animation-delay:-4s}.packet{animation:packet 6s ease-in-out infinite} .expand{animation:expand 6s cubic-bezier(.22,1,.36,1) infinite;transform-box:fill-box;transform-origin:center}.slide{animation:slide 6s ease-in-out infinite}
@keyframes float{0%,12%,92%,100%{transform:translateY(-12px)}40%,72%{transform:translateY(0)}}
@keyframes arrive{0%,10%,94%,100%{transform:translateX(-70px);opacity:.65}38%,76%{transform:translateX(0);opacity:1}}
@keyframes reveal{0%,24%,90%,100%{opacity:.4}40%,78%{opacity:1}}
@keyframes scan{0%,12%,100%{transform:translateX(-110px)}65%,82%{transform:translateX(110px)}}
@keyframes pulse{0%,25%,90%,100%{opacity:.5}42%,78%{opacity:1}}
@keyframes draw{0%,22%,100%{stroke-dashoffset:200;opacity:.2}50%,82%{stroke-dashoffset:0;opacity:1}}
@keyframes left{0%,10%,95%,100%{transform:translateX(-45px)}38%,80%{transform:translateX(0)}}
@keyframes right{0%,10%,95%,100%{transform:translateX(45px)}38%,80%{transform:translateX(0)}}
@keyframes packet{0%,8%{offset-distance:0%;opacity:0}15%{opacity:1}70%{offset-distance:100%;opacity:1}83%,100%{offset-distance:100%;opacity:0}}
@keyframes expand{0%,12%,96%,100%{transform:scaleX(.75);opacity:.6}42%,78%{transform:scaleX(1);opacity:1}}
@keyframes slide{0%,10%,95%,100%{transform:translateY(16px);opacity:.4}40%,80%{transform:translateY(0);opacity:1}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;opacity:1!important}}
.tone-blue{stroke:BLUE!important;fill:BLUEWASH!important}.tone-teal{stroke:TEAL!important;fill:TEALWASH!important}.tone-amber{stroke:AMBER!important;fill:AMBERWASH!important}.signal{fill:BLUE}.signal-teal{fill:TEAL}.signal-amber{fill:AMBER}.tone-green{stroke:GREEN!important}.ring{fill:none;stroke:GREEN;stroke-width:1.4;transform-box:fill-box;transform-origin:center;animation:ring 6s ease-out infinite}.flowline{stroke-dasharray:5 9;animation:flowline 6s linear infinite}.ink-in{stroke-dasharray:70;animation:ink-in 6s ease infinite}.settle{animation:settle 6s ease infinite}.late{animation-delay:-.7s}.later{animation-delay:-1.4s}
@keyframes ring{0%,32%,100%{transform:scale(.8);opacity:0}42%{transform:scale(1);opacity:.6}72%,90%{transform:scale(1.45);opacity:0}}
@keyframes flowline{0%{stroke-dashoffset:0}100%{stroke-dashoffset:-84}}
@keyframes ink-in{0%,30%,98%,100%{stroke-dashoffset:70;opacity:.3}52%,82%{stroke-dashoffset:0;opacity:1}}
@keyframes settle{0%,32%,95%,100%{opacity:.25}52%,82%{opacity:1}}
.evidence-focus{fill:BLUE;fill-opacity:.07;stroke:BLUE;stroke-width:1.5;animation:evidence-focus 8s cubic-bezier(.22,1,.36,1) infinite}.difference-focus{stroke:AMBER;stroke-width:1.3;animation:difference-focus 6s ease infinite}
@keyframes evidence-focus{0%,18%{transform:translateY(0);stroke:BLUE}25%,43%{transform:translateY(62px);stroke:TEAL}50%,68%{transform:translateY(124px);stroke:AMBER}75%,93%{transform:translateY(186px);stroke:GREEN}100%{transform:translateY(0);stroke:BLUE}}
@keyframes difference-focus{0%,15%,95%,100%{opacity:.1}35%,80%{opacity:.8}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;opacity:1!important}.evidence-focus{stroke:LINE;fill:none}.difference-focus{opacity:.3!important}}
'''

SCENES = {'bring': ('Bring anything',
           'Different inputs. One catalogue.',
           '<path d="M280 110C375 110 375 170 440 170M280 170H440M280 230C375 230 375 170 440 170" '
           'class="line"/>\n'
           '<g class="arrive"><rect x="150" y="88" width="130" height="44" rx="4" class="fill '
           'tone-blue"/><text x="215" y="116" text-anchor="middle">Benchmark</text></g>\n'
           '<g class="arrive" style="animation-delay:-.3s"><rect x="150" y="148" width="130" height="44" '
           'rx="4" class="fill tone-teal"/><text x="215" y="176" text-anchor="middle">Agent</text></g>\n'
           '<g class="arrive" style="animation-delay:-.6s"><rect x="150" y="208" width="130" height="44" '
           'rx="4" class="fill tone-amber"/><text x="215" y="236" text-anchor="middle">Capability</text></g>\n'
           '<path d="M440 132v-18h220v18M440 208v18h220v-18" class="ink"/>\n'
           '<g class="slide"><path d="M463 144h174M463 170h174M463 196h174" class="line"/><circle cx="468" '
           'cy="144" r="3" class="dot"/><circle cx="468" cy="170" r="3" class="signal-teal"/><circle '
           'cx="468" cy="196" r="3" class="signal-amber"/></g>\n'
           '<text x="550" y="270" text-anchor="middle" class="muted">Catalogue</text><circle cx="360" '
           'cy="170" r="4" class="dot pulse"/><circle r="3.5" class="signal packet" '
           'style="offset-path:path(\'M280 110C375 110 375 170 440 170\')"/><circle r="3.5" '
           'class="signal-teal packet delay" style="offset-path:path(\'M280 170H440\')"/><circle r="3.5" '
           'class="signal-amber packet delay2" style="offset-path:path(\'M280 230C375 230 375 170 440 '
           '170\')"/>'),
 'fit': ('Prove it fits',
         'A required port meets a proven port.',
         '<text x="240" y="90" text-anchor="middle">A benchmark</text><text x="570" y="90" '
         'text-anchor="middle">An agent</text>\n'
         '<g class="left"><path d="M150 127H305V151H326V191H305V215H150Z" class="fill tone-blue"/><path '
         'd="M175 151H263M175 170H250M175 189H263" class="line"/></g>\n'
         '<g class="right"><path d="M490 127H645V215H490V191H469V151H490Z" class="fill tone-teal"/><path '
         'd="M531 151H620M543 170H620M531 189H620" class="line"/></g>\n'
         '<path d="M332 170H463" class="blue draw"/><g class="reveal"><circle cx="398" cy="170" r="19" '
         'fill="BG" stroke="GREEN"/><path d="m389 170 6 6 12-13" class="ok ink-in"/></g>\n'
         '<text x="400" y="271" text-anchor="middle" class="muted">agent.workspace/v1</text><circle cx="398" '
         'cy="170" r="24" class="ring"/><circle r="3" class="signal packet" style="offset-path:path(\'M332 '
         '170H463\')"/>'),
 'checks': ('Check the benchmark',
            'The empty answer fails. The reference answer passes.',
            '<text x="230" y="93" text-anchor="middle">nop</text><text x="570" y="93" '
            'text-anchor="middle">oracle</text>\n'
            '<path d="M140 135H320V213H140ZM480 135H660V213H480Z" class="line"/>\n'
            '<g class="float"><path d="M203 148h54v48h-54z" class="fill"/><path d="M543 148h54v48h-54z" '
            'class="fill"/><path d="M552 160h36M552 171h29M552 182h34" class="blue"/></g>\n'
            '<g class="reveal"><path d="m220 239 20 20m0-20-20 20" class="warn ink-in"/><path d="m557 248 9 '
            '9 18-21" class="ok ink-in"/></g>\n'
            '<text x="230" y="293" text-anchor="middle" class="muted">Should score nothing</text><text '
            'x="570" y="293" text-anchor="middle" class="muted">Should pass</text><path d="M150 213H310" '
            'class="warn ink-in"/><path d="M490 213H650" class="ok ink-in"/><circle cx="570" cy="248" r="24" '
            'class="ring"/>'),
 'run': ('Run anywhere',
         'Split the work. Bring the evidence back together.',
         '<path d="M165 171H320L420 105H530M320 171H530M320 171 420 237H530M620 105H680V171H714M620 '
         '171H714M620 237H680V171" class="line"/>\n'
         '<rect x="95" y="145" width="70" height="52" rx="3" class="fill"/><path d="M114 159h31M114 '
         '170h31M114 181h20" class="ink"/><text x="130" y="240" text-anchor="middle" '
         'class="muted">Recipe</text>\n'
         '<g><rect x="530" y="79" width="90" height="52" rx="3" class="fill"/><rect x="530" y="145" '
         'width="90" height="52" rx="3" class="fill"/><rect x="530" y="211" width="90" height="52" rx="3" '
         'class="fill"/><text x="575" y="110" text-anchor="middle" class="muted">Here</text><text x="575" '
         'y="176" text-anchor="middle" class="muted">Host</text><text x="575" y="242" text-anchor="middle" '
         'class="muted">Host</text></g>\n'
         '<rect x="715" y="151" width="30" height="40" class="fill"/>\n'
         '<circle r="4" class="dot packet" style="offset-path:path(\'M165 171H320L420 105H575\')"/><circle '
         'r="4" class="signal-teal packet delay" style="offset-path:path(\'M165 171H575\')"/><circle r="4" '
         'class="signal-amber packet delay2" style="offset-path:path(\'M165 171H320L420 237H575\')"/>\n'
         '<text x="575" y="304" text-anchor="middle" class="muted">One shard per machine</text><text x="730" '
         'y="240" text-anchor="middle" class="muted">Job</text><circle r="3" fill="GREEN" class="packet" '
         'style="animation-delay:-2.8s;offset-path:path(\'M620 105H680V171H730\')"/><circle r="3" '
         'fill="GREEN" class="packet" style="animation-delay:-3.5s;offset-path:path(\'M620 '
         '171H730\')"/><circle r="3" fill="GREEN" class="packet" '
         'style="animation-delay:-4.2s;offset-path:path(\'M620 237H680V171H730\')"/><path d="M734 157v27" '
         'class="ok ink-in"/>'),
 'evidence': ('Read the evidence',
              'Open one trial to inspect its tests, model calls, agent steps and output files.',
              '<g class="left"><rect x="75" y="107" width="170" height="130" rx="4" class="fill '
              'tone-blue"/><text x="160" y="145" text-anchor="middle">One trial</text><path d="M102 '
              '165h114M102 181h81" class="line"/><text x="160" y="214" text-anchor="middle" '
              'class="muted">Open evidence</text></g>\n'
              '<path d="M245 171H310M310 72V258M310 72H350M310 134H350M310 196H350M310 258H350" '
              'class="line"/>\n'
              '<g><rect x="350" y="44" width="365" height="55" rx="3" class="fill"/><text x="370" '
              'y="68">Verifier</text><text x="370" y="88" class="muted">Test results and the benchmark’s '
              'verdict</text><path d="m680 61 6 6-6 6" class="blue"/></g>\n'
              '<g><rect x="350" y="106" width="365" height="55" rx="3" class="fill"/><text x="370" '
              'y="130">Trace</text><text x="370" y="150" class="muted">Model calls, timing and '
              'failures</text><path d="m680 123 6 6-6 6" stroke="TEAL" fill="none"/></g>\n'
              '<g><rect x="350" y="168" width="365" height="55" rx="3" class="fill"/><text x="370" '
              'y="192">Conversation</text><text x="370" y="212" class="muted">Agent messages and tool '
              'use</text><path d="m680 185 6 6-6 6" stroke="AMBER" fill="none"/></g>\n'
              '<g><rect x="350" y="230" width="365" height="55" rx="3" class="fill"/><text x="370" '
              'y="254">Artifacts</text><text x="370" y="274" class="muted">Output files, patches and '
              'logs</text><path d="m680 247 6 6-6 6" class="ok"/></g>\n'
              '<rect x="350" y="44" width="365" height="55" rx="3" class="evidence-focus"/>\n'
              '<circle data-evidence="verifier" r="3.5" class="signal packet" '
              'style="animation-duration:8s;offset-path:path(\'M245 171H310V72H350\')"/>\n'
              '<circle data-evidence="trace" r="3.5" class="signal-teal packet" '
              'style="animation-duration:8s;animation-delay:-.5s;offset-path:path(\'M245 '
              '171H310V134H350\')"/>\n'
              '<circle data-evidence="conversation" r="3.5" class="signal-amber packet" '
              'style="animation-duration:8s;animation-delay:-1s;offset-path:path(\'M245 '
              '171H310V196H350\')"/>\n'
              '<circle data-evidence="artifacts" r="3.5" fill="GREEN" class="packet" '
              'style="animation-duration:8s;animation-delay:-1.5s;offset-path:path(\'M245 '
              '171H310V258H350\')"/>\n'
              '<text x="400" y="325" text-anchor="middle" class="muted">Follow the evidence behind the '
              'outcome</text>'),
 'compare': ('Compare',
             'Compare the same task, spot the different outcome, then open each trial’s evidence.',
             '<text x="400" y="33" text-anchor="middle" class="muted">Same benchmark · same task</text>\n'
             '<rect x="65" y="53" width="670" height="217" rx="4" class="fill"/>\n'
             '<path d="M65 95H735M65 142H735M65 187H735M65 229H735M226 53V270M480 53V270" class="line"/>\n'
             '<text x="85" y="80" class="muted">Shared task</text><text x="353" y="80" '
             'text-anchor="middle">Run A</text><text x="608" y="80" text-anchor="middle">Run B</text>\n'
             '<text x="85" y="124" style="font-size:14px">A shared task</text>\n'
             '<path d="M251 119H456M505 119H710" class="blue flowline"/>\n'
             '<text x="85" y="170" class="muted">Outcome</text><g><circle cx="259" cy="165" r="4" '
             'fill="GREEN"/><text x="275" y="171">Resolved</text><circle cx="514" cy="165" r="4" '
             'fill="AMBER"/><text x="530" y="171">Agent error</text></g>\n'
             '<text x="85" y="214" class="muted">Total time</text><text x="253" y="215">3.1m</text><text '
             'x="508" y="215">4.7m</text>\n'
             '<text x="85" y="256" class="muted">Tokens</text><text x="253" y="256">178.9k</text><text '
             'x="508" y="256">—</text>\n'
             '<rect x="227" y="143" width="507" height="43" fill="AMBERWASH" fill-opacity=".12" '
             'class="difference-focus"/>\n'
             '<path d="M353 272V294M608 272V294" class="blue ink-in"/><text x="353" y="315" '
             'text-anchor="middle" class="muted">Open trial evidence</text><text x="608" y="315" '
             'text-anchor="middle" class="muted">Open trial evidence</text>')}

PALETTE = {'BG': '#18191b',
 'INK': '#dedfe2',
 'MUTED': '#a1a7b1',
 'LINE': '#474d56',
 'BLUE': '#96b8e8',
 'TEAL': '#89c8bd',
 'GREEN': '#a1cca9',
 'AMBER': '#dfb680',
 'SURFACE': '#202328',
 'BLUEWASH': '#222d3c',
 'TEALWASH': '#20312f',
 'AMBERWASH': '#332d24'}


def render() -> dict[str, str]:
    figures = {}
    for name, (title, description, body) in SCENES.items():
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 350" '
               f'width="800" height="350" role="img" '
               f'aria-labelledby="{name}-title {name}-desc">'
               f'<title id="{name}-title">{escape(title)}</title>'
               f'<desc id="{name}-desc">{escape(description)} '
               'Conceptual animation, not an evaluation result.</desc>'
               f'<style>{STYLE}</style><rect width="800" height="350" fill="BG"/>{body}</svg>')
        for token in sorted(PALETTE, key=len, reverse=True):
            svg = svg.replace(token, PALETTE[token])
        figures[f'{name}.svg'] = svg + '\n'
    return figures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='rewrite the SVGs')
    parser.add_argument('--check', action='store_true', help='fail when an SVG is stale')
    args = parser.parse_args()
    figures = render()
    if args.write:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, svg in figures.items():
            (OUT / name).write_text(svg)
    stale = [name for name, svg in figures.items()
             if not (OUT / name).exists() or (OUT / name).read_text() != svg]
    if stale:
        raise SystemExit('Stale README media: ' + ', '.join(stale))
    print(f'README media: {len(figures)} SVGs current')


if __name__ == '__main__':
    main()
