"""Check generated local links, assets, anchors and search results without a server."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.home: str = ''
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'link' and values.get('rel') == 'home':
            self.home = values.get('href', '')
        if values.get('id'):
            self.ids.add(values['id'])
        for key in ('href', 'src'):
            if values.get(key):
                self.links.append(values[key])


def check(root: Path, site_url: str) -> list[str]:
    root = root.resolve()
    base = site_url.rstrip('/') + '/'
    origin = urlsplit(base)
    pages = {p: Page(p.read_text()) for p in root.rglob('*.html')}
    failures = []

    def inspect(source: str, link: str, current: str, home: str = ''):
        url = urlsplit(urljoin(current, link))
        if (url.scheme, url.netloc) != (origin.scheme, origin.netloc):
            return
        if not url.path.startswith(origin.path):
            if home and url.geturl() == urljoin(current, home):
                return
            failures.append(f'{source}: link escapes site base: {link}')
            return
        path = root / unquote(url.path[len(origin.path):])
        if path.is_dir():
            path /= 'index.html'
        if not path.is_file():
            failures.append(f'{source}: missing target: {link}')
        elif url.fragment and path in pages and unquote(url.fragment) not in pages[path].ids:
            failures.append(f'{source}: missing anchor: {link}')

    for path, page in pages.items():
        relative = path.relative_to(root).as_posix()
        # A generated 404 is served at arbitrary URLs; MkDocs uses absolute URLs
        # there based on site_url. Treat its canonical location normally here.
        current = urljoin(base, relative)
        for link in page.links:
            inspect(relative, link, current, page.home)
    index = root / 'search/search_index.json'
    if not index.is_file():
        failures.append('Missing search index')
    else:
        entries = json.loads(index.read_text())['docs']
        if not entries:
            failures.append('Empty search index')
        for entry in entries:
            inspect('search index', entry['location'], base)
    return sorted(set(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--site-url', default='http://localhost/')
    args = parser.parse_args()
    errors = check(args.directory, args.site_url)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('All generated links, assets, anchors and search targets resolve.')


if __name__ == '__main__':
    main()
