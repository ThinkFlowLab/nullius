"""Provider, model and credential contracts for compiled recipes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ui_tests.builder_support import AGENT, APP, PIN, CompilerCase


class ProviderAndModel(CompilerCase):
    """The provider is a recipe field and the model is a bare name. Each
    consumer receives the model spelling its client accepts and the matching
    credential variables."""

    def parts(self, edit):
        out = self.js('const r=C.create("harbor");' + AGENT + 'r.agents=["custom-0","tau3-llm-agent"];r.urlEnv="API_URL";' + edit + ';const o=C.compile(r);'
                      'process.stdout.write(JSON.stringify({errors:o.errors.map(e=>e.message),ours:o.artifacts.find(a=>a.id==="run-custom-0").text,ref:o.artifacts.find(a=>a.id==="run-tau3-llm-agent").text}));')
        self.assertEqual(out['errors'], [], out['errors'])
        return out['ours'], out['ref']

    def test_catalogue_model_id_keeps_its_provider_and_vendor_prefix(self):
        ours, ref = self.parts('r.provider="openrouter";r.model="openai/gpt-demo";r.modelFormat="provider-id"')
        for text in (ours, ref):
            self.assertIn("--model 'openrouter/openai/gpt-demo'", text)
            self.assertIn("TAU2_USER_MODEL='openai/openai/gpt-demo'", text)
        got = self.js('const r=C.create("harbor");r.provider="openrouter";r.model="openai/gpt-demo";r.modelFormat="provider-id";const saved=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([saved.model,C.recipeProvider(saved)]));')
        self.assertEqual(got, ["openai/gpt-demo", "openrouter"])

    def test_openrouter_each_consumer_gets_its_own_spelling(self):
        ours, ref = self.parts('r.provider="openrouter";r.model="meta/x"')
        self.assertIn("--model 'openrouter/meta/x'", ours)                            # routed by prefix
        for text in (ours, ref):
            self.assertIn("TAU2_USER_MODEL='openai/meta/x'", text)                     # τ² sides read OPENAI_API_KEY + OPENAI_BASE_URL only
            self.assertIn("TAU2_NL_ASSERTIONS_MODEL='openai/meta/x'", text)
        self.assertIn("--model 'openrouter/meta/x'", ref)                             # litellm-native for the reference agent …
        self.assertIn('OPENROUTER_API_KEY="${API_TOKEN', ref)                          # … with the key under the name litellm expects
        self.assertIn('OPENROUTER_API_KEY="${API_TOKEN', ours)                         # the τ³ runtime's OpenRouter embedder reads it too

    def test_openrouter_key_is_a_tau3_need_outside_the_reference_agent(self):
        out = self.js('const r=C.create("harbor");' + AGENT + 'r.urlEnv="API_URL";r.provider="openrouter";r.model="meta/x";'
                      'C.setBenchmark(r,"terminal-bench");r.source="local";r.path="/data/terminal tasks";const o=C.compile(r);'
                      'process.stdout.write(JSON.stringify({errors:o.errors.map(e=>e.message),ours:o.artifacts.find(a=>a.id==="run-custom-0").text}));')
        self.assertEqual(out['errors'], [], out['errors'])
        self.assertIn("--model 'openrouter/meta/x'", out['ours'])
        self.assertNotIn('OPENROUTER_API_KEY', out['ours'])                            # no τ³ runtime, no benchmark-side need

    def test_openai_and_custom_endpoints_spell_openai(self):
        ours, ref = self.parts('r.provider="openai";r.model="gpt-x"')
        self.assertIn("--model 'openai/gpt-x'", ours); self.assertIn("--model 'openai/gpt-x'", ref); self.assertNotIn('OPENROUTER', ref)
        ours, ref = self.parts('r.provider="custom";r.model="local-model"')
        self.assertIn("--model 'openai/local-model'", ours); self.assertIn("--model 'openai/local-model'", ref); self.assertIn("TAU2_USER_MODEL='openai/local-model'", ours)

    def test_a_prefix_typed_into_the_model_field_wins_and_old_recipes_split(self):
        out = self.js('const r=C.create("harbor");' + AGENT + 'r.provider="openai";r.model="openrouter/z";const a=C.compile(r).artifacts.find(x=>x.id==="run-custom-0").text;'
                      'const old=C.fromJSON(JSON.stringify({...C.create("harbor"),provider:undefined,model:"openrouter/meta/x",userModel:"openai/u"}));'
                      'const pre=C.prefill({model:"openrouter/meta/x"});const d=C.create("harbor");'
                      'process.stdout.write(JSON.stringify([a.includes("--model \'openrouter/z\'"),old.provider,old.model,old.userModel,pre.provider,pre.model,d.provider,d.model]));')
        self.assertEqual(out, [True, 'openrouter', 'meta/x', 'u', 'openrouter', 'meta/x', 'openai', 'test-model'])   # schema default openai/test-model splits too

    def test_an_openai_model_run_on_openrouter_stays_on_openrouter(self):
        """A job that ran openrouter/openai/gpt-4o, reopened or prefilled, and
        an old recipe {provider: openrouter, model: openai/gpt-4o}: the page
        read OpenAI from the model while /run used the recipe's OpenRouter."""
        out = self.js('const pre=C.prefill({model:"openrouter/openai/gpt-4o"});'
                      'const old=C.fromJSON(JSON.stringify({...C.create("harbor"),provider:"openrouter",model:"openai/gpt-4o",modelFormat:undefined}));'
                      'const job=C.fromJob({launch:{agents:[{name:"claude-code",model:"openrouter/openai/gpt-4o"}]}}).recipe||C.fromJob({launch:{agents:[{name:"claude-code",model:"openrouter/openai/gpt-4o"}]}});'
                      'process.stdout.write(JSON.stringify([pre,old,job].map(r=>[r.provider,C.providerOf(r,r.model),C.agentModel(r)])));')
        self.assertEqual(out, [["openrouter", "openrouter", "openrouter/openai/gpt-4o"]] * 3)

    def test_validation_knows_the_providers(self):
        errs = lambda edit: self.js('const r=C.create("harbor");r.model="x";' + edit + ';process.stdout.write(JSON.stringify(C.validate(r).errors.map(e=>e.message)));')
        self.assertTrue(any('provider' in e.lower() for e in errs('r.provider="bogus"')))
        self.assertTrue(any('endpoint' in e for e in errs('r.provider="openrouter";r.urlEnv=""')))
        self.assertEqual([e for e in errs('r.provider="openai";r.urlEnv=""') if 'endpoint' in e], [])
        self.assertEqual([e for e in errs('r.provider="openrouter";r.urlEnv="API_URL"') if 'provider/model' in e], [])   # a bare name is the norm now



class ProviderFromSettings(CompilerCase):
    """The provider follows the keys set in Settings: a new recipe starts on
    the first one set (OpenRouter, OpenAI, then a local endpoint, which its
    URL makes available); a recipe naming one that is not set keeps it and
    Run says why, until another is chosen; with none set, every provider stays
    visible and Run waits for a key."""

    def on(self, available, code, model_key=True, endpoints=None):
        schema = dict(self.schema, credentials={'available': available, 'model_key': model_key, 'endpoints': endpoints or {}})
        program = 'const C=require(' + json.dumps(str(APP / 'static/builder/command-builder-core.js')) + ')(' + json.dumps(schema) + ');\n' + code
        return json.loads(subprocess.run([self.node, '-'], input=program, check=True, capture_output=True, text=True).stdout)

    def test_a_recipe_names_its_endpoint_by_id_and_the_step_lists_each_by_name(self):
        """A named OpenAI-compatible endpoint: the recipe keeps provider custom
        and the endpoint's id, saved and read back; the Model step offers it by
        its name; one that is gone is said before Run, by its id."""
        endpoints = {'GPU': 'GPU box'}
        code = ("const out=[];for(const harness of ['harbor','standard']){const r=harness==='harbor'?C.create():C.createStandard('longmemeval');"
                + PIN.replace('%', '%%') + "r.model='m';C.setChoice(r,%s);const back=C.fromJSON(JSON.stringify(r));"
                "out.push([back.provider,back.endpoint||'',C.choiceOf(back),C.labelOf(C.choiceOf(back)),C.providerBlocker(back)]);}"
                "process.stdout.write(JSON.stringify(out))")
        on_gpu = self.on(['openrouter', 'custom', 'custom:GPU'], code % "'custom:GPU'", endpoints=endpoints)
        self.assertEqual(on_gpu, [['custom', 'GPU', 'custom:GPU', 'GPU box', '']] * 2)
        on_default = self.on(['openrouter', 'custom', 'custom:GPU'], code % "'custom'", endpoints=endpoints)
        self.assertEqual(on_default, [['custom', '', 'custom', 'OpenAI-compatible', '']] * 2)                # custom alone: MODEL_API_URL's
        gone = self.on(['openrouter', 'custom:GPU'], code % "'custom:GONE'", endpoints=endpoints)
        self.assertEqual(gone[0][:4], ['custom', 'GONE', 'custom:GONE', 'GONE'])
        self.assertEqual(gone[0][4], 'This recipe runs on GONE, and MODEL_GONE_API_URL is not set in Settings. '
                                     'Choose OpenRouter or GPU box on the Model step to run it, or set it in Settings.')
        self.assertEqual(self.on(['custom:GPU'], code % "'openai'", endpoints=endpoints)[0][:2], ['openai', ''])   # another provider drops the id
        bad = json.loads(subprocess.run([self.node, '-'], input='const C=require(' + json.dumps(str(APP / 'static/builder/command-builder-core.js')) + ')('
                                         + json.dumps(dict(self.schema, credentials={'available': ['custom:GPU'], 'endpoints': endpoints, 'broken': ['custom:BAD']})) + ');'
                                         + "const r=C.create();r.agents=['tau3-llm-agent'];" + PIN + "r.model='m';C.setChoice(r,'custom:BAD');process.stdout.write(JSON.stringify(C.providerBlocker(r)))",
                                        check=True, capture_output=True, text=True).stdout)
        self.assertEqual(bad, 'This recipe runs on BAD, and its URL in Settings holds a credential or is not an http(s) URL. Put the key in the Key field in Settings.')
        odd = self.on(['custom:GPU'], "const r=C.fromJSON(JSON.stringify({...C.create(),provider:'custom',endpoint:'gpu box'}));"
                                      "process.stdout.write(JSON.stringify(r.endpoint||''))", endpoints=endpoints)
        self.assertEqual(odd, '')                                                                           # not an id: not kept

    def test_a_new_recipe_starts_on_the_first_provider_set(self):
        new = "process.stdout.write(JSON.stringify([C.create().provider,C.prefill({model:'gpt-x'}).provider]))"
        self.assertEqual(self.on(['openai', 'custom'], new), ['openai', 'openai'])
        self.assertEqual(self.on(['openrouter', 'openai', 'custom'], new), ['openrouter', 'openrouter'])
        self.assertEqual(self.on(['custom'], new), ['custom', 'custom'])
        self.assertEqual(self.on([], new), ['openai', 'openai'])          # none set: as before, from the latest model seen (openai/test-model)

    def test_run_waits_for_a_provider_that_is_set_and_never_switches_one(self):
        state = ("const r=C.create();r.agents=['tau3-llm-agent'];" + PIN.replace('%', '%%') + "r.model='x';r.provider=%s;const back=C.fromJSON(JSON.stringify(r));"
                 "process.stdout.write(JSON.stringify([back.provider,C.providerBlocker(back),C.runnable(back).errors,C.compile(back).errors.length]))")
        self.assertEqual(self.on(['openrouter'], state % "'openrouter'"), ['openrouter', '', [], 0])
        provider, why, errors, compile_errors = self.on(['openrouter', 'custom'], state % "'openai'")       # a saved recipe on OpenAI
        self.assertEqual((provider, compile_errors), ('openai', 0))                                          # kept as it was; execution still requires credentials
        self.assertEqual(why, 'This recipe runs on OpenAI, and OPENAI_API_KEY is not set in Settings. '
                              'Choose OpenRouter or OpenAI-compatible on the Model step to run it, or set it in Settings.')
        self.assertEqual(errors, [why])
        provider, why, errors, compile_errors = self.on([], state % "'openrouter'")
        self.assertEqual((provider, compile_errors, errors), ('openrouter', 0, [why]))
        self.assertTrue(why.startswith('Run needs a model key, and none is set in Settings: set OPENROUTER_API_KEY, OPENAI_API_KEY or MODEL_API_URL there.'))
        # a local endpoint is available by its URL, and needs no key
        self.assertEqual(self.on(['custom'], state % "'custom'", model_key=False)[1:3], ['', []])
        # a recipe of controls alone calls no model: nothing to wait for
        self.assertEqual(self.on([], "const r=C.create();r.agents=['nop','oracle'];C.setBenchmark(r,'terminal-bench');r.source='local';r.path='/t';"
                                     "process.stdout.write(JSON.stringify(C.providerBlocker(r)))"), '')



class CredentialReferences(CompilerCase):
    def test_custom_source_names_match_run(self):
        out = self.compile(edit="r.keyEnv='MY_MODEL_KEY';r.urlEnv='MY_MODEL_ENDPOINT'")
        self.assertEqual(out['errors'], [])
        run = self.run_text(out)
        self.assertIn('LLM_API_KEY="${MY_MODEL_KEY:', run)
        self.assertIn('OPENAI_API_KEY="${MY_MODEL_KEY:', run)
        self.assertIn('OPENAI_BASE_URL="${MY_MODEL_ENDPOINT:', run)

    def test_default_endpoint_has_no_dangling_reference(self):
        out = self.compile(edit="r.urlEnv=''")
        self.assertEqual(out['errors'], [])
        for a in out['artifacts']:
            self.assertNotIn('API_URL', a['text'])
            self.assertNotIn('OPENAI_BASE_URL', a['text'])

    def test_tau3_controls_still_supply_customer_and_evaluator_credentials(self):
        out = self.compile(edit="r.agents=['oracle','nop'];r.keyEnv='CUSTOMER_KEY';r.urlEnv='CUSTOMER_ENDPOINT'")
        self.assertEqual(out['errors'], [])
        for a in out['artifacts']:
            if a['id'].startswith('run-'):
                self.assertIn('OPENAI_API_KEY="${CUSTOMER_KEY:', a['text'])
                self.assertIn('OPENAI_BASE_URL="${CUSTOMER_ENDPOINT:', a['text'])
                self.assertIn("TAU2_USER_MODEL='openai/test-model'", a['text'])
                self.assertIn('\n  harbor run', a['text']); self.assertNotIn('with_key', a['text'])   # the exported names, no wrapper
                self.assertNotIn('--model', a['text'])
                self.assertNotIn('LLM_API_KEY', a['text'])
        for edit in ["r.model=''", "r.keyEnv='INVALID NAME'", "r.urlEnv='not valid'", "r.model='openrouter/example';r.urlEnv=''"]:
            self.assertTrue(self.compile(edit="r.agents=['oracle'];"+edit)['errors'])

    def test_other_control_benchmarks_need_no_model(self):
        out = self.compile(edit="C.setBenchmark(r,'swe-bench-verified');r.agents=['oracle'];r.model='';r.keyEnv='';r.urlEnv=''")
        self.assertEqual(out['errors'], [])
        for a in out['artifacts']:
            self.assertNotIn('OPENAI_API_KEY', a['text'])
            self.assertNotIn('Credentials:', a['text'])



class TheRunGetsItsKeyOnly(CompilerCase):
    """A run from the page holds the key of its provider and nothing else of
    Settings. On OpenRouter the τ³ consumers need OPENROUTER_API_KEY by that
    name: the generated script binds it from API_TOKEN, which
    the launcher exports, so it still arrives - from there alone."""

    def test_an_openrouter_tau3_run_binds_its_key_and_holds_no_other(self):
        from unittest.mock import patch
        from nullius import execution
        from nullius import providers
        tmp = Path(tempfile.mkdtemp(prefix='nullius-key-')); self.addCleanup(shutil.rmtree, tmp, True)
        out = self.js('const r=C.create("harbor");' + PIN + 'r.provider="openrouter";r.model="meta/x";r.agents=["tau3-llm-agent"];r.adapterPaths={"tau3-llm-agent":' + json.dumps(str(tmp)) + '};'
                      'process.stdout.write(JSON.stringify(C.runnable(r)));')
        self.assertEqual(out['errors'], [])
        (tmp / 'bin').mkdir(); harbor = tmp / 'bin' / 'harbor'
        harbor.write_text('#!/usr/bin/env bash\nenv > "$NULLIUS_JOBS"/../harbor.env\n'); harbor.chmod(0o755)   # what Harbor, and every container it starts, could read
        held = {n: 'sk-held-' + n.lower() for n in execution.withheld()}
        plan = execution.Plan(name='tau3', script=out['script'], model=out['model'], parts=out['parts'], jobs=out['jobs'],
                              key_var='OPENROUTER_API_KEY', url_var='', base_url=providers.PROVIDER_BASE['openrouter'])
        with patch.dict(os.environ, dict(held, PATH=str(tmp / 'bin') + os.pathsep + os.environ['PATH'])), patch.object(execution, 'SYNC_EVERY', 0.05):
            ex = execution.Execution(plan, root=str(tmp / 'runs')).start()
            ex.thread.join(60)
        self.assertEqual((ex.state, ex.error), ('done', None), ex.lines)
        env = dict(l.split('=', 1) for l in (tmp / 'runs' / 'tau3' / 'harbor.env').read_text().splitlines() if '=' in l)
        key = held['OPENROUTER_API_KEY']
        self.assertEqual({k: v for k, v in env.items() if v.startswith('sk-held-')},
                         {'OPENROUTER_API_KEY': key, 'API_TOKEN': key, 'OPENAI_API_KEY': key, 'LLM_API_KEY': key})   # bound by the script, exported by the launcher


if __name__ == "__main__":
    unittest.main()
