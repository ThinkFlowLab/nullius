"""Build the public site from repository sources, without modifying those sources."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def stage(destination: Path) -> None:
    # docs/ is the site: its README is the front page, its folders the sections
    shutil.copytree(ROOT / 'docs', destination, dirs_exist_ok=True)
    shutil.copytree(HERE / 'assets', destination / 'assets', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'app/static/fonts', destination / 'assets/fonts',
                    ignore=shutil.ignore_patterns('*.md'))
    shutil.copy2(ROOT / 'app/static/fonts.css', destination / 'assets/fonts.css')
    shutil.copy2(ROOT / 'LICENSE', destination / 'assets/LICENSE')
    shutil.copytree(ROOT / 'app/static/brand', destination / 'assets/brand',
                    ignore=shutil.ignore_patterns('*.md'))


def colour_shell() -> None:
    """Pygments leaves a shell command's words and flags untagged, so a
    block of commands renders in one grey. Tag the programs these pages
    run as builtins and their flags as attributes; the site's CSS colours
    both. Patched before the first lexer is built, in this process only."""
    from pygments.lexers.shell import BashLexer
    from pygments.token import Name
    programs = r'\b(python3?|uv|uvx|pip|git|docker|harbor|mkdocs|cp|cat|cd|ls|mkdir|rm|curl|ssh|scp|sha256sum|sed|grep|tar|unzip)(?=[\s`]|$)'
    BashLexer.tokens['basic'][1:1] = [(programs, Name.Builtin), (r'(?<=\s)--?[A-Za-z][\w-]*', Name.Attribute)]
    if '_tokens' in BashLexer.__dict__:
        del BashLexer._tokens


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE / 'dist')
    parser.add_argument('--site-url', default=os.environ.get('SITE_URL', 'http://localhost/'))
    parser.add_argument('--style', type=Path, help='a stylesheet laid over the site, for a preview of a look')
    parser.add_argument('--scheme', choices=('default', 'slate'), default='default',
                        help='with --style: the one colour scheme the preview commits to')
    args = parser.parse_args()
    # Import build dependencies only here: staging is testable with Python alone.
    from mkdocs.config import load_config
    from mkdocs.commands.build import build
    colour_shell()
    with tempfile.TemporaryDirectory(prefix='nullius-site-') as temp:
        source = Path(temp) / 'content'
        stage(source)
        overrides = {}
        if args.style:
            # A preview of one look: its stylesheet after the site's, one scheme, no toggle.
            import yaml
            shutil.copy2(args.style, source / 'assets' / args.style.name)
            overrides['extra_css'] = ['assets/fonts.css', 'assets/site.css', 'assets/' + args.style.name]
            theme = yaml.safe_load((HERE / 'mkdocs.yml').read_text())['theme']
            theme['custom_dir'] = str(HERE / 'overrides')
            theme['palette'] = {'scheme': args.scheme, 'primary': 'custom', 'accent': 'custom'}
            overrides['theme'] = theme
        config = load_config(str(HERE / 'mkdocs.yml'), docs_dir=str(source),
                             site_dir=str(args.output.resolve()), site_url=args.site_url, **overrides)
        build(config)
    print(f'Built {args.output.resolve()}')


if __name__ == '__main__':
    main()
