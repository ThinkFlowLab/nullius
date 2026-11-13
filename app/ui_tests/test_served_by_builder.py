"""The Model step's Served by: the route that lists a model's providers on
OpenRouter (local, form-token guarded, asked as the model list is), and the
recipe's ``served_by`` in the builder's core script (its fields on
OpenRouter alone, kept by a saved recipe, dropped with another provider)."""
import json
import os
import unittest
from pathlib import Path
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


class TheStep(unittest.TestCase):
    """What the Model step's script draws and what the compile route hands it."""
    SCRIPT = (Path(__file__).resolve().parents[1] / 'static' / 'builder' / 'command-builder.js').read_text()

    def test_served_by_holds_no_sentence_and_its_help_is_a_tooltip(self):
        field = self.SCRIPT[self.SCRIPT.index('const servedField'):self.SCRIPT.index('const servedCache')]
        self.assertNotIn('<small>', field)
        self.assertIn('title="', field)

    def test_the_compile_route_gives_each_fields_model_as_the_run_sends_it(self):
        client = web.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'served-test-token'
        recipe = {'version': 1, 'harness': 'standard', 'name': 'x-0101', 'title': 'x', 'benchmark': 'longmemeval',
                  'datasets': ['longmemeval_s'], 'others': ['original'], 'model_choice': 'paper', 'provider': 'openrouter',
                  'model': 'gpt-4o', 'limit': 1, 'concurrency': 1, 'where': 'local'}
        response = client.post('/new-run/runner', json={'recipe': recipe}, headers={'X-CSRF-Token': 'served-test-token'})
        self.assertEqual(response.json['served_models']['model'], 'openai/gpt-4o-2024-08-06')

    def test_the_keyboard_stays_on_the_model_field_once_the_form_is_drawn_again(self):
        self.assertIn("requestAnimationFrame(()=>{const f=root.querySelector(`[data-field=\"${d.field}\"]`);if(f&&document.activeElement!==f)f.focus();})",
                      self.SCRIPT)


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
                      'C.setServed(r,"model","a, b");const typed=r.served_by.model;C.setServed(r,"model","");const emptied=!(r.served_by||{}).model;'
                      'C.setServed(r,"model",["a"]);C.setChoice(r,"openai");'
                      'process.stdout.write(JSON.stringify([kept,typed,emptied,r.served_by===undefined]));')
        # kept as written, the judge's bad slug included: the server refuses it in sight (test_a_pin_is_kept_as_written...)
        self.assertEqual(out, [{'model': ['deepinfra/fp8', 'together'], 'judge': ['bad slug']}, ['a', 'b'], True, True])

    def test_a_pin_goes_with_the_model_it_was_set_for(self):
        out = self.js('const b=C.standard().benchmarks.find(x=>x.judge)||C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
                      'const seen=[];C.setServed(r,"model",["a"]);C.setServed(r,"judge",["j"]);'
                      'C.setModelOf(r,"model",r.model);seen.push(JSON.stringify(r.served_by));'
                      'C.setModelOf(r,"model","meta-llama/llama-3.3-70b-instruct");seen.push(JSON.stringify(r.served_by));'
                      'C.setModelOf(r,"judge_model","openai/gpt-4o");seen.push(JSON.stringify(r.served_by||null));'
                      'C.setServed(r,"model",["a"]);C.setModelChoice(r,r.model_choice);seen.push(JSON.stringify(r.served_by));'
                      'C.setModelChoice(r,C.paperOffered(r)?"paper":"leaderboard");seen.push(JSON.stringify(r.served_by||null));'
                      'C.setServed(r,"model",["a"]);const other=C.standard().benchmarks.find(x=>x.key!==b.key);C.setStandardBenchmark(r,other.key);seen.push(JSON.stringify(r.served_by||null));'
                      'process.stdout.write(JSON.stringify(seen));')
        self.assertEqual(out, ['{"model":["a"],"judge":["j"]}', '{"judge":["j"]}', 'null', '{"model":["a"]}', 'null', 'null'])

    def test_a_pin_is_kept_as_written_for_the_server_to_refuse(self):
        raw = {'version': 1, 'harness': 'standard', 'benchmark': '', 'provider': 'openrouter', 'served_by': {'model': ['deepinfra', 'bad slug']}}
        out = self.js('const raw=' + json.dumps(raw) + ';raw.benchmark=C.standard().benchmarks[0].key;const r=C.fromJSON(JSON.stringify(raw));'
                      'process.stdout.write(JSON.stringify([r.served_by,C.servedList("a,,b"),C.servedList(" , "),C.servedList("")]));')
        self.assertEqual(out, [{'model': ['deepinfra', 'bad slug']}, ['a', '', 'b'], [], []])

    def test_the_list_is_of_the_model_the_run_sends(self):
        out = self.js('process.stdout.write(JSON.stringify([C.servedModelOf("model","gpt-4o",{served_models:{model:"openai/gpt-4o-2024-08-06"}}),'
                      'C.servedModelOf("judge","gpt-4o",null),C.servedModelOf("judge","gpt-4o",{served_models:{judge:null}})]));')
        self.assertEqual(out, ['openai/gpt-4o-2024-08-06', 'gpt-4o', 'gpt-4o'])

    def test_a_recipe_with_no_pin_is_saved_as_before(self):
        out = self.js('const b=C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
                      'const again=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify(["served_by" in r,"served_by" in again]));')
        self.assertEqual(out, [False, False])


if __name__ == '__main__':
    unittest.main()
