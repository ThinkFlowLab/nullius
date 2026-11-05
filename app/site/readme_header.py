"""Draw the static README header and badges using the bundled brand fonts.

Run with: uv run --with fonttools python app/site/readme_header.py --write
The outlined text needs no font service or embedded font at display time.
"""
from argparse import ArgumentParser
from html import escape
from pathlib import Path
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/assets/nullius/readme'
FONTS = ROOT / 'app/static/fonts'
SERIF = TTFont(FONTS / 'c1e7453ea26a1d1e.ttf')
SANS = TTFont(FONTS / '0569c822cab6c510.ttf')


def lettering(text, x, y, size, font=SANS, anchor='middle', color='#e1e5eb'):
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    scale = size / font['head'].unitsPerEm
    width = sum(font['hmtx'].metrics[cmap[ord(c)]][0] for c in text)
    x -= width * scale * ({'start': 0, 'middle': .5, 'end': 1}[anchor])
    paths = []
    cursor = 0
    for char in text:
        name = cmap[ord(char)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(pen)
        paths.append(f'<path d="{pen.getCommands()}" transform="translate({cursor} 0)"/>')
        cursor += font['hmtx'].metrics[name][0]
    return (f'<g aria-hidden="true" fill="{color}" transform="translate({x} {y}) '
            f'scale({scale} {-scale})">' + ''.join(paths) + '</g>')


def svg(width, height, title, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title">'
            f'<title id="title">{escape(title)}</title>'
            '<!-- Outlined Instrument Serif / DM Sans; licences: app/static/fonts/. -->'
            + body + '</svg>\n')


def render():
    body = ('<defs><pattern id="dots" x="300" y="12" width="7" height="7" '
            'patternUnits="userSpaceOnUse"><circle cx="0" cy="0" r="1.25" '
            'fill="#414955" opacity=".55"/></pattern>'
            '<mask id="halftone"><circle cx="500" cy="150" r="123" '
            'fill="none" stroke="white" stroke-width="56"/>'
            '<path d="M382.5 267.5 617.5 32.5" stroke="white" '
            'stroke-width="42.426"/></mask></defs>'
            '<rect width="1000" height="300" fill="#18191b"/>'
            '<rect x="300" width="401" height="300" fill="url(#dots)" mask="url(#halftone)"/>')
    # The canonical circle-and-slash geometry, scaled to the banner corner.
    body += ('<g transform="translate(38 33) scale(.48)" fill="none" stroke="#dce2eb" '
             'stroke-width="6.5" stroke-linecap="round"><circle cx="32" cy="32" r="17"/>'
             '<path d="M15 49 49 15"/></g>')
    body += lettering('nullius', 500, 172, 132, SERIF, color='#eef0f3')
    body += lettering('Bring anything. Run it. Prove it.', 500, 222, 24)
    body += lettering('nullius in verba', 947, 53, 16, anchor='end', color='#a6afbb')
    result = {'header.svg': svg(1000, 300, 'Nullius — Bring anything. Run it. Prove it.', body)}
    for key, label, value, color, width, split in [
        ('python', 'PYTHON', '3.12+', '#405b7e', 141, 77),
        ('license', 'LICENSE', 'Apache-2.0', '#3f635b', 188, 81),
        ('docs', 'DOCS', 'Read the guide', '#725d3f', 197, 63),
    ]:
        body = (f'<rect width="{width}" height="30" fill="#242a32"/>'
                f'<rect x="{split}" width="{width-split}" height="30" fill="{color}"/>'
                + lettering(label, split / 2, 19, 11)
                + lettering(value, split + (width - split) / 2, 19, 11))
        result[f'badge-{key}.svg'] = svg(width, 30, f'{label}: {value}', body)
    return result


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for name, content in render().items():
        path = OUT / name
        if args.write:
            path.write_text(content)
        elif not path.exists() or path.read_text() != content:
            stale.append(str(path.relative_to(ROOT)))
    if stale:
        raise SystemExit('Stale README header assets: ' + ', '.join(stale))
    print('README header: 4 static SVGs current')
