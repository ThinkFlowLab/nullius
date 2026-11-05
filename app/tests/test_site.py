"""The website publishes existing sources and works under a project URL prefix."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[1] / 'site'


def module(name):
    spec = importlib.util.spec_from_file_location('website_' + name, SITE / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Website(unittest.TestCase):
    def test_staging_preserves_authored_docs(self):
        build = module('build')
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'content'
            build.stage(out)
            for original in (build.ROOT / 'docs').rglob('*'):
                if original.is_file():
                    self.assertEqual(original.read_bytes(),
                                     (out / original.relative_to(build.ROOT / 'docs')).read_bytes())
            self.assertFalse((out / 'index.md').exists())   # the README is the front page
            start = (out / 'getting-started.md').read_text()
            self.assertIn('uv run --no-project --with-requirements app/requirements.txt app/app.py', start)
            self.assertIn('off by default', start)
            self.assertFalse((out / 'assets/fonts/README.md').exists())
            self.assertTrue((out / 'assets/fonts/dmsans-OFL.txt').exists())
            self.assertEqual((out / 'assets/LICENSE').read_bytes(), (build.ROOT / 'LICENSE').read_bytes())
            self.assertTrue((out / 'assets/brand/favicon.svg').is_file())
            self.assertTrue((out / 'assets/fonts/DMSans-Regular.woff2').is_file())

    def test_links_and_search_work_at_root_and_under_a_prefix(self):
        check = module('check').check
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'docs').mkdir()
            (root / 'search').mkdir()
            (root / 'index.html').write_text('<a href="docs/#section">Docs</a>')
            (root / 'docs/index.html').write_text('<h1 id="section">Docs</h1><a href="../">Home</a>')
            index = root / 'search/search_index.json'
            index.write_text(json.dumps({'docs': [{'location': 'docs/#section'}]}))
            for base in ('https://example.com/', 'https://example.com/nullius/'):
                self.assertEqual(check(root, base), [])
            (root / 'docs/index.html').write_text('<img src="missing.png"><a href="/#section">Wrong root</a>')
            errors = check(root, 'https://example.com/nullius/')
            self.assertTrue(any('missing target: missing.png' in e for e in errors))
            self.assertTrue(any('missing anchor: docs/#section' in e for e in errors))
            self.assertTrue(any('link escapes site base' in e for e in errors))

    def test_declared_landing_page_can_leave_the_docs_prefix(self):
        check = module('check').check
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'search').mkdir()
            (root / 'search/search_index.json').write_text(json.dumps({'docs': [{'location': ''}]}))
            (root / 'index.html').write_text(
                '<link rel="home" href="https://example.com/nullius/">'
                '<a href="https://example.com/nullius/">Home</a>'
                '<a href="https://example.com/unexpected/">Wrong</a>')
            self.assertEqual(check(root, 'https://example.com/nullius/docs/'),
                             ['index.html: link escapes site base: https://example.com/unexpected/'])

    def test_edit_links_target_original_sources(self):
        from types import SimpleNamespace
        hook = module('hooks').on_page_context
        for generated, original in [('README.md', 'docs/README.md'),
                                     ('getting-started.md', 'docs/getting-started.md'),
                                     ('cli/run.md', 'docs/cli/run.md')]:
            page = SimpleNamespace(file=SimpleNamespace(src_uri=generated))
            hook({}, page, {'repo_url': 'https://github.com/thinkflowlab/nullius'}, None)
            self.assertEqual(page.edit_url, 'https://github.com/thinkflowlab/nullius/edit/main/' + original)

    def test_figures_are_what_the_script_draws(self):
        diagrams = module('diagrams')
        self.assertEqual(diagrams.stale(), [])
        for slug, svg in diagrams.render().items():
            self.assertIn(f'aria-labelledby="{slug}-title {slug}-desc"', svg)
            self.assertIn(f'<title id="{slug}-title">', svg)

    def test_figures_are_inlined_so_they_follow_the_scheme(self):
        from types import SimpleNamespace
        hook = module('hooks').on_page_content
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'diagrams').mkdir(parents=True)
            (root / 'diagrams/flow.svg').write_text('<svg class="dg"><title id="flow-title">Flow</title></svg>\n')
            page = SimpleNamespace(file=SimpleNamespace(abs_src_path=str(root / 'settings/page.md'), src_uri='settings/page.md'))
            html = hook('<h1>Page</h1><p><img alt="Flow" src="../diagrams/flow.svg" /></p><p>after</p>',
                        page, {'docs_dir': str(root)}, None)
            self.assertEqual(html, '<h1>Page</h1><figure class="diagram"><svg class="dg">'
                                   '<title id="flow-title">Flow</title></svg></figure><p>after</p>')

    def test_a_folder_of_generated_pages_is_a_section_under_its_readme(self):
        hook = module('hooks').on_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'docs/things/supported').mkdir(parents=True)
            (root / 'docs/things/supported/README.md').write_text('# Supported things\n')
            (root / 'docs/things/supported/zeta.md').write_text('<!-- generated by x -->\n\n# Zeta\n')
            (root / 'docs/things/supported/tau.md').write_text('<!-- generated by x -->\n\n# τ³-thing\n')
            (root / 'docs/settings').mkdir()
            (root / 'docs/settings/README.md').write_text('# Overview\n')
            (root / 'docs/settings/keys.md').write_text('# Keys\n')           # written by hand: listed by hand
            nav = [{'Things': [{'Supported things': 'docs/things/supported/README.md'}, {'Add': 'docs/things/add.md'}]},
                   {'Settings': [{'Overview': 'docs/settings/README.md'}, {'Keys': 'docs/settings/keys.md'}]}]
            config = hook({'docs_dir': str(root), 'nav': nav})
            self.assertEqual(config['nav'][0], {'Things': [{'Supported things': ['docs/things/supported/README.md',
                                                            {'τ³-thing': 'docs/things/supported/tau.md'}, {'Zeta': 'docs/things/supported/zeta.md'}]},
                                                           {'Add': 'docs/things/add.md'}]})
            self.assertEqual(config['nav'][1], nav[1])

    def test_a_raw_link_to_a_sibling_page_is_rewritten_as_markdown_s_would_be(self):
        from types import SimpleNamespace
        hook = module('hooks').on_page_content
        with tempfile.TemporaryDirectory() as directory:
            config = {'docs_dir': directory}
            index = SimpleNamespace(file=SimpleNamespace(abs_src_path=directory + '/docs/x/README.md', src_uri='docs/x/README.md'))
            page = SimpleNamespace(file=SimpleNamespace(abs_src_path=directory + '/docs/x/a.md', src_uri='docs/x/a.md'))
            self.assertEqual(hook('<a href="gsm8k.md">G</a> <a href="https://e.com/x.md">E</a>', index, config, None),
                             '<a href="gsm8k/">G</a> <a href="https://e.com/x.md">E</a>')
            self.assertEqual(hook('<a href="gsm8k.md">G</a>', page, config, None), '<a href="../gsm8k/">G</a>')
            self.assertEqual(hook('<a href="../../b/supported/gsm8k.md">G</a>', index, config, None), '<a href="../../b/supported/gsm8k/">G</a>')
            self.assertEqual(hook('<a href="../../b/supported/gsm8k.md">G</a>', page, config, None), '<a href="../../../b/supported/gsm8k/">G</a>')
