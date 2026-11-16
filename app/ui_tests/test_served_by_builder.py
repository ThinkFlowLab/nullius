"""The Model step's Served by: the route that lists a model's providers on
OpenRouter (local, form-token guarded, asked as the model list is), the row
and its picker (the Model row's, taking several providers in order), and the
recipe's ``served_by`` in the builder's core script (its fields on
OpenRouter alone, kept by a saved recipe, dropped with another provider)."""
import json
import os
import shutil
import subprocess
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
    STATIC = Path(__file__).resolve().parents[1] / 'static'
    SCRIPT = (STATIC / 'builder' / 'command-builder.js').read_text()
    PICKER = (STATIC / 'builder' / 'model-picker.js').read_text()
    STYLE = (STATIC / 'builder' / 'command-builder.css').read_text()

    def test_served_by_holds_no_sentence_and_its_help_is_a_tooltip(self):
        field = self.SCRIPT[self.SCRIPT.index('const field = (rec, role)'):self.SCRIPT.index('function sync()')]
        self.assertNotIn('<small>', field)
        self.assertIn('title="', field)

    def test_served_by_is_a_row_with_the_model_rows_picker(self):
        """A row as the Model row is (a ``label.nb-field``, which the page sets as a settings row), its input one the
        Model row's picker takes (``list="nb-served"``), with no block set in under the model's row."""
        field = self.SCRIPT[self.SCRIPT.index('const field = (rec, role)'):self.SCRIPT.index('function sync()')]
        self.assertIn('<label class="nb-field std-served"', field)
        self.assertIn('<input list="nb-served"', field)
        self.assertNotIn('<select', field)
        self.assertIn('input[list="nb-models"], input[list="nb-served"]', self.PICKER)
        served_css = [line for line in self.STYLE.splitlines() if 'std-served' in line]
        self.assertTrue(served_css)
        self.assertFalse([line for line in served_css if 'border-left' in line or 'margin-left' in line])
        # a pick joins the list (never replaces it); a removal is one place of it; a field of several opens on a click
        self.assertIn("root.addEventListener('nb-pick',event=>{const role=event.target.dataset.servedBy;if(!role)return;C.addServed(r(),role,event.detail);", self.SCRIPT)
        self.assertIn('if(d.servedRemove!==undefined){event.preventDefault();C.removeServed(r(),d.servedRole,d.servedRemove);', self.SCRIPT)
        self.assertIn("if (severalOf(input)) input.addEventListener('click', () => { if(active?.input !== input)void open(input); });", self.PICKER)

    def test_the_compile_route_gives_each_fields_model_as_the_run_sends_it(self):
        client = web.app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'served-test-token'
        recipe = {'version': 1, 'harness': 'standard', 'name': 'x-0101', 'title': 'x', 'benchmark': 'longmemeval',
                  'datasets': ['longmemeval_s'], 'others': ['original'], 'model_choice': 'paper', 'provider': 'openrouter',
                  'model': 'gpt-4o', 'limit': 1, 'concurrency': 1, 'where': 'local'}
        response = client.post('/new-run/runner', json={'recipe': recipe}, headers={'X-CSRF-Token': 'served-test-token'})
        self.assertEqual(response.json['served_models']['model'], 'openai/gpt-4o-2024-08-06')

    def test_a_model_changed_draws_its_served_by_in_place_and_no_form(self):
        """A model typed or chosen clears its field's pin and draws that Served by again where it is: the form is not
        drawn again, so the model's menu does not open again, and a click on Served by lands there."""
        self.assertIn("else if(['model','simulator_model','judge_model'].includes(d.field)){C.setModelOf(rec,d.field,value);syncServed();}", self.SCRIPT)
        changed = self.SCRIPT[self.SCRIPT.index("root.addEventListener('change'"):]
        self.assertNotIn("'model','simulator_model','judge_model'", changed)
        self.assertNotIn('requestAnimationFrame', changed)

    def test_a_long_slug_wraps_inside_its_chip(self):
        """A name is never cut: a slug with no break wraps inside its chip, and the chip stays inside its row."""
        self.assertIn('<span class="std-served-slug">', self.SCRIPT)
        rules = {line.split('{')[0].strip(): line for line in self.STYLE.splitlines() if 'std-served' in line and '{' in line}
        self.assertIn('overflow-wrap: anywhere', rules['#nullius-builder .std-served-slug'])
        self.assertIn('min-width: 0', rules['#nullius-builder .std-served-slug'])
        self.assertIn('max-width: 100%', rules['#nullius-builder .std-served-pick'])
        self.assertIn('min-width: 0', rules['#nullius-builder .std-served-list'])


class TheRows(CompilerCase):
    """command-builder.js's servedRows, run under Node with stand-ins for the page's elements: what each Served by row
    shows after a change, drawn where it is, and the model its providers are listed for."""
    BUILDER = Path(__file__).resolve().parents[1] / 'static' / 'builder' / 'command-builder.js'
    SETUP = ('const {servedRows}=require(' + json.dumps(str(Path(__file__).resolve().parents[1] / 'static' / 'builder' / 'command-builder.js')) + ');'
             'const b=C.standard().benchmarks.find(x=>x.judge)||C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
             'r.model_choice="any";C.setModelOf(r,"model","gpt-4o");'
             'const input={dataset:{servedBy:"model"},placeholder:""},list={dataset:{},innerHTML:""},asked=[];'
             'const root={querySelectorAll:s=>{asked.push(s);return s==="input[data-served-by]"?[input]:[];},querySelector:s=>{asked.push(s);return s===\'[data-served-list="model"]\'?list:null;}};'
             'let compiled={out:{served_models:{model:"openai/gpt-4o-2024-08-06"}},key:C.servedKey(r)};'
             'const esc=x=>String(x??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));'
             'const rows=servedRows({C,root,r:()=>r,compiled:()=>compiled,esc,button:(t,a,c)=>`<button class="${c}" ${a}>${t}</button>`});'
             'const seen=()=>({model:input.dataset.servedModel,pending:input.dataset.servedPending,chosen:input.dataset.chosen,placeholder:input.placeholder,'
             'chips:(list.innerHTML.match(/std-served-slug">([^<]*)</g)||[]).map(x=>x.slice(17,-1))});')

    def test_a_change_draws_each_served_by_again_in_place(self):
        """A pick, a removal and a model change each draw that row's list and field again, in the elements already there,
        and touch nothing else of the page."""
        out = self.js(self.SETUP + 'const got=[];C.addServed(r,"model","deepinfra/fp8");C.addServed(r,"model","together");rows.sync();got.push(seen());'
                      'C.removeServed(r,"model",0);rows.sync();got.push(seen());C.setModelOf(r,"model","gpt-4o-mini");rows.sync();got.push(seen());'
                      'got.push([...new Set(asked)]);process.stdout.write(JSON.stringify(got));')
        self.assertEqual(out[0], {'model': 'openai/gpt-4o-2024-08-06', 'pending': 'false', 'chosen': 'deepinfra/fp8,together', 'placeholder': 'Add a provider', 'chips': ['deepinfra/fp8', 'together']})
        self.assertEqual(out[1]['chips'], ['together'])
        self.assertEqual((out[2]['chosen'], out[2]['placeholder'], out[2]['chips']), ('', 'any provider', []))
        self.assertEqual(out[3], ['input[data-served-by]', '[data-served-list="model"]'])

    def test_the_list_waits_for_the_model_the_run_sends(self):
        """A pin added or taken out keeps the model the server compiled (the run sends `openai/gpt-4o-2024-08-06`, the field
        shows `gpt-4o`); another model leaves none until the server has compiled it, and no list is asked meanwhile."""
        out = self.js(self.SETUP + 'const got=[];rows.sync();got.push(seen().model);C.addServed(r,"model","openai");rows.sync();got.push([seen().model,seen().pending]);'
                      'C.removeServed(r,"model",0);rows.sync();got.push(seen().model);C.setModelOf(r,"model","gpt-4o-mini");rows.sync();got.push([seen().model,seen().pending]);'
                      'got.push(/data-served-model="" data-served-pending="true"/.test(rows.field(r,"model")));'
                      'compiled={out:{served_models:{model:"openai/gpt-4o-mini-2024-07-18"}},key:C.servedKey(r)};rows.sync();got.push(seen().model);'
                      'got.push([C.servedRow(r,"judge","gpt-4.1",null),C.servedKey({a:1,served_by:{model:["x"]}})===C.servedKey({a:1})]);process.stdout.write(JSON.stringify(got));')
        self.assertEqual(out[:6], ['openai/gpt-4o-2024-08-06', ['openai/gpt-4o-2024-08-06', 'false'], 'openai/gpt-4o-2024-08-06', ['', 'true'], True,
                                   'openai/gpt-4o-mini-2024-07-18'])
        self.assertEqual(out[6], [{'shown': 'gpt-4.1', 'model': '', 'pending': True, 'chosen': [], 'placeholder': 'any provider'}, True])


    def test_a_failed_compile_keeps_the_model_the_server_named(self):
        """A compile that failed names no model: the row keeps the one the server last named for the same recipe (a pin
        added in between), waits while none is known (a failed first compile, or a compile that names none), and takes
        the one a later compile names. The name shown is never listed."""
        out = self.js(self.SETUP + 'const got=[],key=()=>JSON.stringify(r),failed={errors:[{message:"The recipe could not be compiled here"}],notes:[],review:[]};'
                      'const step=x=>{compiled=C.servedCompiled(compiled,x,key());rows.sync();got.push([seen().model,seen().pending]);};compiled=null;'
                      'step(failed);step({errors:[],served_models:{model:"openai/gpt-4o-2024-08-06"}});C.addServed(r,"model","openai");step(failed);'
                      'step({errors:[],served_models:{}});step({errors:[],served_models:{model:"openai/gpt-4o-2024-08-06"}});process.stdout.write(JSON.stringify(got));')
        sent = 'openai/gpt-4o-2024-08-06'
        self.assertEqual(out, [['', 'true'], [sent, 'false'], [sent, 'false'], ['', 'true'], [sent, 'false']])


class ThePicker(unittest.TestCase):
    """model-picker.js, as the Model row and each Served by use it: what typing keeps, and what Enter takes."""
    PICKER = Path(__file__).resolve().parents[1] / 'static' / 'builder' / 'model-picker.js'
    ROWS = [{'id': 'deepinfra/fp8', 'name': 'DeepInfra · fp8'}, {'id': 'together', 'name': 'Together · fp8'}, {'id': 'novita/bf16', 'name': 'Novita · bf16'}]

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is required for the picker')

    def js(self, code):
        program = 'const P=require(' + json.dumps(str(self.PICKER)) + ');const ROWS=' + json.dumps(self.ROWS) + ';\n' + code
        return json.loads(subprocess.run([self.node, '-'], input=program, check=True, capture_output=True, text=True).stdout)

    def test_typing_filters_a_models_providers(self):
        out = self.js('const ids=x=>x.map(m=>m.id);process.stdout.write(JSON.stringify([ids(P.matching(ROWS,"nov")),ids(P.matching(ROWS,"DEEP")),'
                      'ids(P.matching(ROWS,"fp8")),ids(P.matching(ROWS,"fp8",["deepinfra/fp8"])),ids(P.matching(ROWS,"")).length,ids(P.matching(ROWS,"zz"))]));')
        self.assertEqual(out, [['novita/bf16'], ['deepinfra/fp8'], ['deepinfra/fp8', 'together'], ['together'], 3, []])

    def test_enter_takes_a_slug_the_list_lacks_in_a_field_of_several(self):
        out = self.js('process.stdout.write(JSON.stringify([P.entered([],-1," my-provider/fp8 ",true),P.entered(ROWS,1,"x",true),'
                      'P.entered([],-1,"x",false),P.entered([],-1,"  ",true),P.entered(ROWS,-1,"gpt-4o",false)]));')
        self.assertEqual(out, [{'id': 'my-provider/fp8', 'typed': True}, self.ROWS[1], None, None, None])


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

    def test_a_pick_then_a_second_join_the_list_in_order(self):
        out = self.js('const b=C.standard().benchmarks.find(x=>x.judge)||C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
                      'const seen=[];C.addServed(r,"model","deepinfra/fp8");seen.push(r.served_by.model.slice());C.addServed(r,"model","together");seen.push(r.served_by.model.slice());'
                      'C.addServed(r,"model","deepinfra/fp8");seen.push(r.served_by.model.slice());C.addServed(r,"model","my-provider/fp8, novita ,");seen.push(r.served_by.model.slice());'
                      'C.addServed(r,"judge",["openai"]);C.addServed(r,"judge","");seen.push(r.served_by);process.stdout.write(JSON.stringify(seen));')
        self.assertEqual(out, [['deepinfra/fp8'], ['deepinfra/fp8', 'together'], ['deepinfra/fp8', 'together'],
                               ['deepinfra/fp8', 'together', 'my-provider/fp8', 'novita'],
                               {'model': ['deepinfra/fp8', 'together', 'my-provider/fp8', 'novita'], 'judge': ['openai']}])

    def test_a_removal_takes_out_the_one_at_its_place(self):
        out = self.js('const b=C.standard().benchmarks.find(x=>x.judge)||C.standard().benchmarks[0];const r=C.createStandard(b.key);C.setChoice(r,"openrouter");'
                      'C.setServed(r,"model",["a","b","a"]);C.setServed(r,"judge",["j"]);const seen=[];C.removeServed(r,"model",2);seen.push(r.served_by.model.slice());'
                      'C.removeServed(r,"model","0");seen.push(r.served_by.model.slice());C.removeServed(r,"model",0);seen.push(r.served_by);'
                      'C.removeServed(r,"judge",0);seen.push(r.served_by===undefined);process.stdout.write(JSON.stringify(seen));')
        self.assertEqual(out, [['a', 'b'], ['b'], {'judge': ['j']}, True])

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
