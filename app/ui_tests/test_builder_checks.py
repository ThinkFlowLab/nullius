"""The preparation statements the run builder answers, one test each.

Each test asserts one sentence of the builder's preparation rules against the
builder core driven from Node through `builder_support.py` - over recipes
the test builds itself. Unsupported benchmarks, sources and parameters must
not produce a script. Nothing here runs a model or a generated command.
"""
import json
import re
import unittest

from ui_tests.builder_support import AGENT, APP, CompilerCase


class BenchmarkDiscovery(CompilerCase):
    def test_incompatible_benchmarks_and_sources_do_not_produce_scripts(self):
        out = self.js('''
            const check=edit=>{const r=C.create();''' + AGENT + '''r.model="openai/x";edit(r);const c=C.compile(r);
              return {errors:c.errors.map(e=>e.message),n:c.artifacts.length};};
            const out={
              ok:check(r=>{C.setBenchmark(r,"terminal-bench");r.source="local";r.path="/data/tb2";}),
              blocked:check(r=>{r.benchmark="blocked-example";}),               // the catalogue names no harness
              unknown:check(r=>{r.benchmark="not-a-benchmark";}),               // no catalogue entry at all
              source:check(r=>{C.setBenchmark(r,"terminal-bench");r.source="bridge";r.path="/data/tb2";}),   // another harness's source
              generate:check(r=>{C.setBenchmark(r,"terminal-bench");r.source="generate";r.path="/data/tb2";}),   // a source this benchmark does not offer
              offered:C.benchmarks().map(b=>b.key),
              sources:(()=>{const r=C.create();C.setBenchmark(r,"terminal-bench");return C.sources(r).map(s=>s[0]);})()};
            process.stdout.write(JSON.stringify(out));''', dict(self.schema, benchmarks=self.schema['benchmarks'] + [self.BLOCKED]))
        self.assertEqual(out['ok']['errors'], [])
        self.assertNotIn('blocked-example', out['offered']); self.assertNotIn('bridge', out['sources']); self.assertNotIn('generate', out['sources'])
        # a benchmark the catalogue does not put on Harbor: an error with the catalogue's reason, no execution artifacts
        self.assertTrue(out['blocked']['errors'][0].startswith('Blocked Example has no Harbor route'), out['blocked']['errors'])
        self.assertEqual(out['unknown']['errors'][0], 'This benchmark has no supported launch adapter.')
        # a source the harness does not offer for this benchmark: the same
        self.assertIn('Choose a dataset source supported by this benchmark.', out['source']['errors'])
        self.assertIn('Choose a dataset source supported by this benchmark.', out['generate']['errors'])
        for case in ('blocked', 'unknown', 'source', 'generate'):
            self.assertEqual(out[case]['n'], 0, case)


class RunRecipe(CompilerCase):
    def everything_set(self, extra=''):
        """A Harbor recipe with every optional field, every parameter and a
        custom parameter of every Harbor scope set."""
        edit = ('C.setBenchmark(r,"swe-bench-verified");r.source="local";r.path="/data/swe";r.model="openai/x";r.urlEnv="API_URL";'
                'r.tasks=3;r.include="x*";r.concurrency=2;r.retries=2;r.attempts=2;r.setupTimeoutMultiplier=2;r.verifierTimeoutMultiplier=1.5;r.buildTimeoutMultiplier=3;r.timeoutMultiplier=1.5;'
                'r.params.mcp_timeout_s=120;'
                'r.custom=[{scope:"agent",key:"extra",type:"string",value:"a"},{scope:"harness",key:"quiet",type:"flag",value:""},{scope:"environment",key:"env_k",type:"string",value:"e"},{scope:"verifier",key:"ver_k",type:"number",value:"1"}];')
        return self.js('const r=C.create();' + AGENT + edit + extra
                       + 'const c=C.compile(r);process.stdout.write(JSON.stringify({errors:c.errors.map(e=>e.message),texts:c.artifacts.map(a=>a.text),'
                       'own:C.parameters().map(p=>p.key),scopes:C.scopes().map(s=>s[0])}));')

    def test_each_harness_recipe_contains_only_parameters_scoped_to_that_harness(self):
        out = self.everything_set()
        self.assertEqual(out['errors'], [])
        text = '\n'.join(out['texts'])
        # every agent parameter the script writes is one of Harbor's (`--ak`), plus the custom one typed for it
        written = re.findall(r"--ak '([A-Za-z_]+)=", text)
        self.assertTrue(written)
        self.assertTrue(set(written) <= set(out['own']) | {'extra'}, sorted(set(written) - set(out['own'])))
        self.assertNotIn('mcp_timeout_s', text); self.assertNotIn('tool_timeout_sec', text)   # the reference agent's, and it is not selected
        # the other lanes' parameters and flags are not in the vocabulary and not in the script
        for key in ('workspace_dir', 'prompt_timeout', 'prompt_timeout_s', 'agent_version', 'agent_user', 'forward_generation_config', 'bridge_port', 'cua_bridge'):
            self.assertNotIn(key, out['own']); self.assertNotRegex(text, r'\b' + key + r'\s*[=:]')
        # a custom parameter with a scope Harbor does not have is refused, not written
        self.assertEqual(out['scopes'], ['agent', 'harness', 'environment', 'verifier'])
        for scope in ('task', 'experiment'):
            with self.subTest(scope=scope):
                bad = self.everything_set('r.custom.push({scope:%s,key:"split",type:"string",value:"test"});' % json.dumps(scope))
                self.assertIn('Choose a valid custom-parameter scope.', bad['errors']); self.assertEqual(bad['texts'], [])

    def test_full_limited_and_selected_task_scopes_survive_save_and_reopen(self):
        cases = {'full': 'r.tasks="";', 'limited': 'r.tasks=5;', 'selected': 'r.tasks="";r.include="retail*";'}
        out = self.js('const out={};' + ''.join(
            ('out[%s]=(()=>{const r=C.create();' + AGENT.replace('%', '%%') + 'r.model="openai/x";%s'
            # Save: the recipe as JSON with its title and name, as the page writes it; Import: read back through fromJSON
            'const saved=JSON.stringify({...C.copy(r),title:"My recipe",name:"my-recipe"},null,2)+"\\n";const back=C.fromJSON(saved);'
            'const run=x=>C.compile(x).artifacts.find(a=>a.id==="run-custom-0").text;'
            'return {tasks:[r.tasks,back.tasks],include:[r.include,back.include],before:run(r),after:run(back),errors:C.compile(back).errors};})();')
            % (json.dumps(name), edit) for name, edit in cases.items())
            + 'process.stdout.write(JSON.stringify(out));')
        for name, r in out.items():
            with self.subTest(scope=name):
                self.assertEqual(r['errors'], [])
                self.assertEqual(r['tasks'][0], r['tasks'][1]); self.assertEqual(r['include'][0], r['include'][1])
                self.assertEqual(r['after'].replace('my-recipe', 'harbor-experiment'), r['before'])
        self.assertNotIn('--n-tasks', out['full']['after']); self.assertNotIn('--include-task-name', out['full']['after'])
        self.assertIn('--n-tasks 5', out['limited']['after']); self.assertNotIn('--include-task-name', out['limited']['after'])
        self.assertIn("--include-task-name 'retail*'", out['selected']['after']); self.assertNotIn('--n-tasks', out['selected']['after'])


class Review(CompilerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = dict(cls.schema, hosts=[{'entry': 'bench', 'label': 'Bench host'}])

    def test_the_review_identifies_framework_dataset_scope_agent_model_limits_and_where(self):
        out = self.js('const r=C.create();C.setBenchmark(r,"swe-bench-verified");r.source="local";r.path="/data/swe";r.customAgents=["my_agent:MyAgent"];r.agents=["custom-0","oracle"];r.model="openai/gpt-x";r.tasks=12;r.concurrency=3;r.where="bench";'
                      'const e=C.create();e.model="openai/gpt-y";'
                      'process.stdout.write(JSON.stringify({errors:C.compile(r).errors,rows:C.review(r),every:C.review(e)}));')
        self.assertEqual(out['errors'], [])
        rows = {title: value for _, title, value in out['rows']}
        self.assertEqual(rows['Benchmark'], 'Harbor / SWE-bench Verified')                # framework and benchmark
        self.assertEqual(rows['Dataset'], 'local · /data/swe')                            # dataset / source
        self.assertEqual(rows['Scope'], '12 tasks · 3 concurrent')                        # task scope and limits
        self.assertEqual(rows['Agents'], 'Custom · my_agent:MyAgent, oracle')             # agent
        self.assertEqual(rows['Model'], 'openai/gpt-x')
        self.assertEqual(rows['Where'], 'Bench host')                                     # execution location
        self.assertEqual([t for _, t, _ in out['rows']], ['Benchmark', 'Runner', 'Harbor', 'Dataset', 'Agents', 'Model', 'Scope', 'Where'])
        self.assertEqual(rows['Runner'], 'Harbor')
        every = {title: value for _, title, value in out['every']}
        self.assertEqual((every['Scope'], every['Where']), ('every task (375 in this dataset) · 1 concurrent', 'This machine'))
        # each row names the step that edits it - Runner first, Review last - and the page renders exactly these rows
        self.assertEqual(sorted({step for step, _, _ in out['rows']}), [0, 1, 2, 4, 5, 6, 7])         # no Setup row: Harbor has none
        page = (APP / 'static/builder/command-builder.js').read_text(encoding='utf-8')
        self.assertIn(':C.review(rec);', page); self.assertIn('rows.map(', page)

if __name__ == "__main__":
    unittest.main()
