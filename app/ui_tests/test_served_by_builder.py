"""The Model step's Served by: the route that lists a model's providers on
OpenRouter (local, form-token guarded, asked as the model list is), and the
recipe's ``served_by`` in the builder's core script (its fields on
OpenRouter alone, kept by a saved recipe, dropped with another provider)."""
import json
import os
import unittest
from unittest.mock import patch

from .builder_support import CompilerCase
from .test_routes import web

LIST = [{'slug': 'deepinfra/fp8', 'name': 'DeepInfra', 'quantization': 'fp8'}, {'slug': 'together', 'name': 'Together', 'quantization': ''}]


class ProviderList(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session['csrf'] = 'served-test-token'
        self.headers = {'X-CSRF-Token': 'served-test-token'}

    def ask(self, body, **kw):
        return self.client.post('/new-run/endpoints', json=body, headers=kw.pop('headers', self.headers), **kw)

    def test_a_models_providers_on_openrouter(self):
        with patch('web.builder.providers.list_endpoints', return_value=LIST) as fetch:
            response = self.ask({'provider': 'openrouter', 'model': 'meta-llama/llama-3.3-70b-instruct'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok': True, 'endpoints': LIST})
        fetch.assert_called_once_with('openrouter', 'meta-llama/llama-3.3-70b-instruct')

    def test_a_list_that_cannot_be_had_is_no_empty_success(self):
        with patch('web.builder.providers.list_endpoints', side_effect=ValueError("Provider list unavailable. Enter a provider's slug by hand.")):
            response = self.ask({'provider': 'openrouter', 'model': 'meta-llama/llama-3.3-70b-instruct'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json, {'ok': False, 'endpoints': [], 'message': "Provider list unavailable. Enter a provider's slug by hand."})

    def test_no_key_asks_without_one(self):
        """OpenRouter's endpoint list is public: with no key set, it is asked with no Authorization header."""
        sent = []

        def get(url, headers, opener=None):
            sent.append((url, headers))
            return json.dumps({'data': {'endpoints': [{'provider_name': 'Together', 'tag': 'together'}]}}).encode()
        env = {k: v for k, v in os.environ.items() if k != 'OPENROUTER_API_KEY'}
        with patch.dict(os.environ, env, clear=True), patch('web.builder.providers._get', side_effect=get):
            response = self.ask({'provider': 'openrouter', 'model': 'meta-llama/llama-3.3-70b-instruct'})
        self.assertEqual(response.json['endpoints'], [{'slug': 'together', 'name': 'Together', 'quantization': ''}])
        self.assertEqual(sent, [('https://openrouter.ai/api/v1/models/meta-llama/llama-3.3-70b-instruct/endpoints', {})])

    def test_guards_and_another_provider_do_not_fetch(self):
        with patch('web.builder.providers.list_endpoints') as fetch:
            self.assertEqual(self.ask({'provider': 'openrouter', 'model': 'a/b'}, headers={}).status_code, 403)
            self.assertEqual(self.ask({'provider': 'openrouter', 'model': 'a/b'}, environ_overrides={'REMOTE_ADDR': '192.0.2.4'}).status_code, 403)
            for body in ({'provider': 'openai', 'model': 'gpt-4o'}, {'provider': 'openrouter'}, {'provider': 'openrouter', 'model': ' '}, []):
                with self.subTest(body=body):
                    self.assertEqual(self.ask(body).status_code, 400)
        fetch.assert_not_called()


class ServedInTheCore(CompilerCase):
    """command-builder-core.js: which model fields take a Served by, and the recipe's ``served_by``."""

    def test_the_fields_show_on_openrouter_alone(self):
        out = self.js('const b=C.standard().benchmarks.find(x=>x.simulator&&x.judge)||C.standard().benchmarks.find(x=>x.judge);'
                      'const r=C.createStandard(b.key);r.model_choice="any";const got={};'
                      'for(const p of ["openrouter","openai","custom"]){C.setChoice(r,p);got[p]=C.servedRoles(r);}'
                      'process.stdout.write(JSON.stringify([got,!!b.simulator]));')
        roles, simulated = out
        self.assertEqual(roles['openrouter'], ['model'] + (['simulator'] if simulated else []) + ['judge'])
        self.assertEqual((roles['openai'], roles['custom']), ([], []))

    def test_a_saved_recipe_keeps_its_pins_and_another_provider_drops_them(self):
        raw = {'version': 1, 'harness': 'standard', 'benchmark': '', 'provider': 'openrouter', 'model': 'meta-llama/llama-3.3-70b-instruct',
               'served_by': {'model': ['deepinfra/fp8', ' together '], 'judge': ['bad slug'], 'agent': ['x'], 'simulator': []}}
        out = self.js('const b=C.standard().benchmarks[0];const raw=' + json.dumps(raw) + ';raw.benchmark=b.key;'
                      'const r=C.fromJSON(JSON.stringify(raw));const kept=JSON.parse(JSON.stringify(r.served_by||null));'
                      'C.setServed(r,"model","a, b ,");const typed=r.served_by.model;C.setServed(r,"model","");const emptied=r.served_by===undefined;'
                      'C.setServed(r,"model",["a"]);C.setChoice(r,"openai");'
                      'process.stdout.write(JSON.stringify([kept,typed,emptied,r.served_by===undefined]));')
        self.assertEqual(out, [{'model': ['deepinfra/fp8', 'together']}, ['a', 'b'], True, True])

    def test_a_recipe_with_no_pin_is_saved_as_before(self):
        out = self.js('const b=C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
                      'const again=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify(["served_by" in r,"served_by" in again]));')
        self.assertEqual(out, [False, False])


if __name__ == '__main__':
    unittest.main()
