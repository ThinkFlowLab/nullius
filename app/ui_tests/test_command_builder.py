"""Recipe and adapter contracts; never run a model or generated command."""
import json
import subprocess
import unittest

from ui_tests.builder_support import AGENT, APP, CompilerCase


class RecipeCompiler(CompilerCase):
    def test_the_recipe_compiles_and_shell_syntax_is_valid(self):
        out=self.compile();self.assertEqual(out['errors'],[])
        for a in out['artifacts']:
            if a['language']=='shell':
                subprocess.run(['bash','-n'],input=a['text'],text=True,check=True,capture_output=True)
        self.assertNotIn('read',[a['id'] for a in out['artifacts']])   # reading the output is the application's own doing
        self.assertNotIn('read',self.compile())                        # no import step is needed

    def test_harbor_recipes_reject_an_unsupported_harness(self):
        self.assertEqual(self.js("process.stdout.write(JSON.stringify(C.create().harness))"), 'harbor')
        self.assertEqual(set(self.js("process.stdout.write(JSON.stringify(C.scopes().map(([s])=>s)))")),{'agent','harness','environment','verifier'})
        self.assertEqual(self.schema['runnerGone'],self.js("try{C.fromJSON(JSON.stringify({...C.create(),harness:'other'}));process.stdout.write('\"loaded\"')}catch(e){process.stdout.write(JSON.stringify(e.message))}"))
        self.assertEqual(self.js("const r=C.create();r.harness='other';process.stdout.write(JSON.stringify(C.validate(r).errors.map(e=>e.message)))"), [self.schema['runnerGone']])
        self.assertNotIn('harnesses',self.schema['parameters'][0])
        for key in ('prompt_timeout','workspace_dir','agent_user','forward_generation_config','bridge_port','cua_bridge'):
            self.assertNotIn(key,[p['key'] for p in self.schema['parameters']],key)

    def test_harbor_owns_the_timeout(self):
        text=self.run_text(self.compile())
        self.assertIn('--agent-timeout-multiplier 1',text)
        self.assertNotIn("--ak 'prompt_timeout=",text)

    def test_multiple_agents_share_scope_and_controls_omit_model(self):
        out=self.compile(edit="r.customAgents=['my.pkg:Agent','other.mod:AgentB'];r.agents=['custom-1','oracle','nop','custom-0'];r.tasks=7;r.include='retail*'")
        runs=[a for a in out['artifacts'] if a['id'].startswith('run-')]
        self.assertEqual(len(runs),4)
        for a in runs:
            self.assertIn('--n-tasks 7',a['text'])
            self.assertIn("--include-task-name 'retail*'",a['text'])
            self.assertEqual('--model' in a['text'],a['id'] not in ('run-oracle','run-nop'))
        self.assertNotIn('tool_timeout_sec',next(a['text'] for a in runs if a['id']=='run-custom-0'))   # the reference agent's own parameter

    def test_custom_scope_type_and_shell_quotes(self):
        out=self.compile(edit="r.custom=[{scope:'agent',key:'extra_setting',type:'JSON',value:'{\"x\":\"a b\"}'},{scope:'environment',key:'split',type:'string',value:\"test's\"},{scope:'harness',key:'epochs',type:'number',value:'2'}]")
        text=self.run_text(out)
        self.assertIn('--ak \'extra_setting={"x":"a b"}\'',text)
        self.assertIn("--ek 'split=\"test'\\''s\"'",text)
        self.assertIn("--epochs='2'",text)
        self.assertEqual(len([n for n in out['notes'] if n.startswith('Custom ')]),3)

    def test_invalid_custom_values_and_reserved_fields_block_copy(self):
        for edit in [
            "r.custom=[{scope:'agent',key:'temperature',type:'number',value:'oops'}]",
            "r.custom=[{scope:'agent',key:'mcp_timeout_s',type:'number',value:'5'}]",   # a parameter the vocabulary names
            "r.custom=[{scope:'harness',key:'n-tasks',type:'number',value:'1'}]",
            "r.custom=[{scope:'agent',key:'api_key',type:'string',value:'example'}]",
            "r.custom=[{scope:'unsupported',key:'split',type:'string',value:'test'}]",
        ]:
            out=self.compile(edit=edit);self.assertTrue(out['errors']);self.assertEqual(out['artifacts'],[])

    def test_invalid_scope_numbers_and_names_do_not_silently_default(self):
        for edit in ['r.tasks=0','r.tasks=1.5','r.concurrency=-1','r.timeoutMultiplier=Infinity','r.params.mcp_timeout_s=-1',"r.name='../../oops'","r.keyEnv='KEY; echo hi'"]:
            self.assertTrue(self.compile(edit=edit)['errors'])

    def test_shell_metacharacters_and_home_paths(self):
        value="dir with 'quote' $(echo nope); `uname`"
        text=self.run_text(self.compile(edit='r.source="local";r.path='+json.dumps(value)))
        self.assertIn("'\\''",text)
        subprocess.run(['bash','-n'],input=text,text=True,check=True,capture_output=True)
        self.assertEqual(self.js('process.stdout.write(JSON.stringify(C.path("~/my tasks")))'),'"$HOME"/\'my tasks\'')

    def test_recipe_round_trip_keeps_false_and_custom_values(self):
        data=self.js("const r=C.create();"+AGENT+"r.params.mcp_timeout_s=0;r.custom=[{scope:'agent',key:'verbose',type:'boolean',value:'false'},{scope:'environment',key:'split',type:'string',value:'test'}];const imported=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify({same:JSON.stringify(r)===JSON.stringify(imported),out:C.compile(imported)}));")
        self.assertTrue(data['same']);self.assertEqual(data['out']['errors'],[])

    def test_custom_text_false_stays_text(self):
        out=self.compile(edit="r.custom=[{scope:'environment',key:'example',type:'string',value:'false'}]")
        self.assertIn("--ek 'example=\"false\"'",self.run_text(out))

    def test_a_dataset_harbor_does_not_serve_opens_the_standards_recipe(self):
        """A link to a dataset Harbor does not serve, of a benchmark the
        standard runs, opens the standard's recipe on that benchmark."""
        out = self.js("process.stdout.write(JSON.stringify(['longmemeval-s','locomo10'].map(d=>{const r=C.prefill({dataset:d});return [r.harness,r.benchmark];})))")
        self.assertEqual(out, [["standard", "longmemeval"], ["standard", "locomo"]])

    def test_the_builder_opens_on_the_nullius_runner(self):
        # nothing asked for: the standard's recipe; a link that names a Harbor dataset, agent or model still opens Harbor's
        out = self.js("process.stdout.write(JSON.stringify([C.prefill({}).harness,C.prefill({model:'gpt-x'}).harness,C.prefill({dataset:'terminal-bench@2.0'}).harness]))")
        self.assertEqual(out, ["standard", "harbor", "harbor"])

    def test_the_runner_step_offers_nullius_then_harbor_and_a_native_recipe_does_not_open(self):
        self.assertEqual([r["key"] for r in self.schema["runners"]], ["standard", "harbor"])
        gone = self.schema["runnerGone"]
        self.assertIn("Original setup", gone)
        out = self.js("const raw={version:1,harness:'longmemeval',name:'lme'};let said='';try{C.fromJSON(raw)}catch(e){said=e.message}"
                      "const draft=C.unpackDraft(JSON.stringify({version:1,recipe:raw,step:2,steps:C.STEPS.length}),{});"
                      "process.stdout.write(JSON.stringify([said,draft,C.validate(raw).errors.map(e=>e.message)]))")
        self.assertEqual(out, [gone, {"recipe": None, "gone": gone}, [gone]])          # the page shows it as its toast

    def test_existing_run_link_prefill(self):
        r=self.js("process.stdout.write(JSON.stringify(C.prefill({dataset:'../my tasks',model:'openai/test',n_tasks:'42',agent:'oracle'})))")
        self.assertEqual(r['benchmark'],'custom');self.assertEqual(r['path'],'../my tasks')
        self.assertEqual(r['tasks'],42);self.assertEqual(r['agents'],['oracle'])

    def test_blocked_benchmark_has_no_launch(self):
        out=self.js("const r=C.create('harbor');C.setBenchmark(r,'blocked-example');process.stdout.write(JSON.stringify(C.compile(r)));",
                    dict(self.schema, benchmarks=self.schema['benchmarks']+[self.BLOCKED]))
        self.assertTrue(out['errors']);self.assertEqual(out['artifacts'],[])



class RouteCases(CompilerCase):
    """The benchmark-discovery cases the preparation spec names that still
    exist with one harness: Harbor-only (terminal-bench) and no usable route
    (a blocked entry, given since none ships), each over a catalogue entry. A route the catalogue does
    not name is never inferred, and a benchmark without a route is an error
    with the catalogue's reason and no artifact — never a runnable-looking
    recipe."""

    def with_blocked(self):
        return dict(self.schema, benchmarks=self.schema['benchmarks'] + [self.BLOCKED])

    def offered(self):
        return self.js('process.stdout.write(JSON.stringify(C.benchmarks().map(b=>b.key)));', self.with_blocked())

    def route(self, key):
        return self.js('const r=C.create();' + AGENT + 'C.setBenchmark(r,%s);const c=C.compile(r);process.stdout.write(JSON.stringify({blocker:C.routeBlocker(%s),errors:c.errors.map(e=>e.message),n:c.artifacts.length}));'
                       % (json.dumps(key), json.dumps(key)), self.with_blocked())

    def test_harbor_only_compiles(self):
        self.assertIn('terminal-bench', self.offered())
        ok = self.route('terminal-bench')
        self.assertEqual((ok['blocker'], ok['errors']), ('', []))
        # every offered benchmark is one the catalogue names on Harbor, official or an experiment
        usable = {b['key'] for b in self.schema['benchmarks'] if 'harbor' in b['harness']}
        self.assertEqual(set(self.offered()), usable)

    def test_a_benchmark_the_catalogue_does_not_put_on_harbor_is_not_inferred(self):
        # No catalogue entry is off Harbor now, so the case is pinned on a schema edit:
        # an entry naming no harness is offered nowhere and blocked by name.
        schema = dict(self.schema, benchmarks=self.schema['benchmarks'] + [{'key': 'elsewhere', 'title': 'Elsewhere', 'category': 'x', 'harness': [], 'origin': 'official', 'datasets': []}])
        program = ('const C=require(' + json.dumps(str(APP/'static/builder/command-builder-core.js')) + ')(' + json.dumps(schema) + ');'
                   'const r=C.create();C.setBenchmark(r,"elsewhere");const c=C.compile(r);'
                   'process.stdout.write(JSON.stringify({offered:C.benchmarks().map(b=>b.key).includes("elsewhere"),blocker:C.routeBlocker("elsewhere"),errors:c.errors.map(e=>e.message),n:c.artifacts.length}));')
        out = json.loads(subprocess.run([self.node, '-e', program], check=True, capture_output=True, text=True).stdout)
        self.assertFalse(out['offered'])
        self.assertEqual(out['blocker'], 'Elsewhere has no Harbor route; the harness it runs on is not available here.')
        self.assertEqual(out['errors'][0], out['blocker']); self.assertEqual(out['n'], 0)

    def test_an_agent_that_needs_its_own_vendor_key_is_named_on_review(self):
        schema = dict(self.schema, agentNeeds={"cursor-cli": ["CURSOR_API_KEY"]}, credentials={"available": ["openrouter"], "agent_keys": []})
        code = 'const r=C.create("harbor");C.setBenchmark(r,"terminal-bench");r.customAgents=["x:Y"];r.agents=["custom-0"];'
        def notes(held):
            sch = dict(schema, credentials=dict(schema["credentials"], agent_keys=held))
            return self.js(code + 'C.agentById=C.agentById;process.stdout.write(JSON.stringify(C.validate(Object.assign(r,{agents:["cursor-cli"]})).notes));', sch)
        missing = notes([])
        self.assertTrue(any("needs CURSOR_API_KEY of its own: add it in Settings, Custom keys" in n for n in missing), missing)
        self.assertFalse(any("CURSOR_API_KEY" in n for n in notes(["CURSOR_API_KEY"])))

    def test_the_catalogue_page_link_opens_the_route_it_names(self):
        # a benchmark's page links a usable route as ?dataset=<key>; prefill lands on that benchmark
        out = self.js('process.stdout.write(JSON.stringify([C.prefill({dataset:"terminal-bench"}).benchmark,C.prefill({dataset:"swe-bench-verified"}).benchmark,C.prefill({harness:"harbor",dataset:"swe-bench-verified"}).harness]));')
        self.assertEqual(out, ['terminal-bench', 'swe-bench-verified', 'harbor'])
        # and runs the dataset under the name Harbor knows it by, not the catalogue's key
        refs = self.js('process.stdout.write(JSON.stringify(["terminal-bench","tau3-bench","terminal-bench@2.0"].map(d=>{const r=C.prefill({dataset:d});return [r.source,r.dataset];})));')
        self.assertEqual(refs, [['registry', 'terminal-bench@2.0'], ['hub', 'sierra-research/tau3-bench'], ['registry', 'terminal-bench@2.0']])


if __name__ == "__main__":
    unittest.main()
