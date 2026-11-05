"""Text and action roles stay aligned; documentation surfaces have their own palette."""
import json
from pathlib import Path
import re
import unittest

from . import palette

APP = Path(__file__).resolve().parents[1]


class Identity(unittest.TestCase):
    def test_app_and_docs_agree_on_text_and_action_roles(self):
        reference = json.loads((APP / 'static/brand/tokens.json').read_text())['themes']
        app_roles = {'bg': 'background', 'surface': 'surface', 'surface-2': 'raised',
                     'text': 'text', 'text-dim': 'muted', 'text-faint': 'faint',
                     'border': 'border', 'accent': 'accent', 'accent-strong': 'accentStrong',
                     'accent-soft': 'accentSurface', 'btn-fg': 'onAccent'}
        docs = (APP / 'site/assets/site.css').read_text()
        for theme, tokens in palette.parse_scopes().items():
            for name, role in app_roles.items():
                self.assertEqual(tokens[name], reference[theme][role], (theme, name))
            scheme = 'slate' if theme == 'dark' else 'default'
            selector = r'\[data-md-color-scheme="' + scheme + r'"\]\s*\{([^}]+)\}'
            block = re.search(selector, docs)[1]
            for name, role in {'ink': 'text', 'muted': 'muted',
                               'deep': 'accent', 'pill': 'accentSurface'}.items():
                self.assertIn('--' + name + ': ' + reference[theme][role], block)

    def test_local_font_sources_and_licenses_are_present(self):
        css = (APP / 'static/fonts.css').read_text()
        for source in re.findall(r'url\(([^)]+)\)', css):
            self.assertTrue((APP / 'static' / source).is_file(), source)
        for family in ('dmsans', 'ibmplexmono', 'instrumentserif'):
            license = APP / f'static/fonts/{family}-OFL.txt'
            self.assertIn('SIL OPEN FONT LICENSE', license.read_text())
