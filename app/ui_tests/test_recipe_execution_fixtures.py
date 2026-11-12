"""Recipe compilation and execution fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from nullius.recipes.launch import payload
from nullius.catalogue import catalog
APP = Path(__file__).resolve().parents[1]

class ReferenceRecipes(unittest.TestCase):
    """Existing reference recipes preserve their semantics.

    Every recipe document under ``app/tests/fixtures/recipes/<name>.json`` —
    the builder's own saved-recipe format, as Save writes it and Import reads
    it — is compiled here exactly as the page compiles it, and the result is
    compared byte for byte with the committed ``<name>.expected.sh``: every
    run artifact, the
    runner script Run posts when the recipe can run from the page, and the
    execution plan. A change in what an old recipe means is a diff in
    that file, seen and committed on purpose, never a silent reinterpretation.

    Nothing is normalised, and that is deliberate: the compiler emits no
    stamp, date or machine path of its own — what varies between machines is
    the schema, and the schema is pinned by this suite (``defaultModel``, the
    agent list, the two credential variable
    names) so the output is the recipe's alone. Set ``NULLIUS_WRITE_EXPECTED=1``
    to rewrite the expected files after a deliberate change, then read the diff.
    """

    FIXTURES = APP / 'tests' / 'fixtures' / 'recipes'

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is required for recipe compilation')
        cls.schema = payload(catalog.load(), [])
        cls.schema['defaultModel'] = 'openai/test-model'
        cls.schema['credentialVars'] = {'key': 'API_TOKEN', 'url': 'API_URL'}

    def compiled(self, recipe_json):
        program = ('const C=require(' + json.dumps(str(APP / 'static/builder/command-builder-core.js')) + ')(' + json.dumps(self.schema) + ');\n'
                   'const r=C.fromJSON(' + json.dumps(recipe_json) + ');'
                   'process.stdout.write(JSON.stringify({compiled:C.compile(r),runnable:C.runnable(r)}));')
        result = subprocess.run([self.node, '-e', program], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    @staticmethod
    def render(name, out, recipe=None):
        """One text for one recipe: sections in a fixed order, each headed by
        what it is, so a diff names the part that changed. A recipe that names
        its provider also pins the settings its run's key and endpoint are read
        from (``web.execute.credentials_for``), never their values."""
        parts = ['# Nullius reference recipe: ' + name, '# Compiled by app/static/builder/command-builder-core.js against the pinned test schema.',
                 '# Regenerate deliberately with NULLIUS_WRITE_EXPECTED=1; every difference is a change of meaning.']
        for a in out['compiled']['artifacts']:
            parts.append('\n# ===== artifact %s · %s · in: %s =====\n%s' % (a['id'], a['filename'], a['cwd'], a['text']))
        if out['runnable']['errors']:
            parts.append('\n# ===== runner: not from the page =====\n' + '\n'.join('# ' + e for e in out['runnable']['errors']))
        else:
            parts.append('\n# ===== runner: what Run posts (parts %s; jobs %s) =====\n%s' % (
                ', '.join(p['id'] for p in out['runnable']['parts']), ', '.join(j['name'] for j in out['runnable']['jobs']), out['runnable']['script']))
        if recipe and recipe.get('provider'):
            from web import execute
            c = execute.credentials_for(recipe['provider'], recipe.get('endpoint'))
            parts.append('\n# ===== credentials: the key from %s, the endpoint from %s =====' % (c['key_var'], c['url_var'] or c['base_url'] or "the client's default"))
        return '\n'.join(parts) + '\n'

    def test_every_reference_recipe_compiles_to_its_stored_script(self):
        recipes = sorted(self.FIXTURES.glob('*.json'))
        self.assertGreaterEqual(len(recipes), 4)
        for recipe in recipes:
            name = recipe.stem
            with self.subTest(recipe=name):
                out = self.compiled(recipe.read_text(encoding='utf-8'))
                self.assertEqual(out['compiled']['errors'], [], name)
                text = self.render(name, out, json.loads(recipe.read_text(encoding='utf-8')))
                expected = recipe.with_name(name + '.expected.sh')
                if os.environ.get('NULLIUS_WRITE_EXPECTED') == '1':
                    expected.write_text(text, encoding='utf-8')
                self.assertTrue(expected.exists(), 'no expected script for ' + name)
                self.assertEqual(expected.read_text(encoding='utf-8'), text,
                                 name + ': the compiled recipe differs from its stored script; a deliberate change is regenerated with NULLIUS_WRITE_EXPECTED=1')
                for a in out['compiled']['artifacts']:
                    if a['language'] == 'shell':
                        subprocess.run(['bash', '-n'], input=a['text'], text=True, check=True, capture_output=True)

    def test_the_reference_recipes_cover_the_harness_and_the_task_scopes(self):
        docs = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in self.FIXTURES.glob('*.json')}
        self.assertEqual({d["harness"] for d in docs.values()}, {"harbor"})
        scopes = {name: d['tasks'] for name, d in docs.items()}
        self.assertIn('', scopes.values())                                  # full scope: every task
        self.assertTrue(any(isinstance(v, int) and v > 0 for v in scopes.values()))   # a limited one
        self.assertTrue(any(d.get('include') for d in docs.values()))       # a selected-task one
        self.assertTrue(any(set(d['agents']) >= {'oracle', 'nop'} for d in docs.values()))   # a bracketed one

    def test_the_stored_scripts_carry_nothing_volatile(self):
        # No date, no absolute path of this machine, no credential value: the files are portable evidence.
        for expected in self.FIXTURES.glob('*.expected.sh'):
            text = expected.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'20\d\d-\d\d-\d\d', expected.name)
            self.assertNotIn(str(APP.parent), text, expected.name)
            self.assertNotIn('fixture-token', text, expected.name)


if __name__ == '__main__':
    unittest.main()
