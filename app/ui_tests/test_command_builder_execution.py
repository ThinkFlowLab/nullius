"""Harbor selection, runnable scripts and saved recipe drafts."""
import json
import subprocess
import unittest

from ui_tests.builder_support import AGENT, PIN, CompilerCase


class HarborPin(CompilerCase):
    """The Harbor pin passed to execution: a release or a repository commit."""

    def out(self, edit='', extra=''):
        return self.js('const r=C.create("harbor");r.agents=["tau3-llm-agent"];r.model="openai/x";r.source="local";r.path="/d";' + edit + ';const o=C.compile(r),u=C.runnable(r);'
                       'process.stdout.write(JSON.stringify({run:(o.artifacts.find(a=>a.id==="run-tau3-llm-agent")||{}).text||"",errors:o.errors.map(e=>[e.step,e.message]),notes:o.notes,'
                       'runnable:u.errors,review:C.review(r).find(x=>x[1]==="Harbor"),exe:C.harborExecutable(r),back:C.fromJSON(JSON.stringify(r)).harbor' + extra + '}));')

    def test_a_new_recipe_pins_nothing_until_a_release_is_chosen_and_says_so(self):
        out = self.out()
        self.assertEqual(out['back'], {'kind': 'release', 'version': '', 'extras': []})
        self.assertEqual(out['errors'], [])
        self.assertIn('Choose Harbor on the Runner step.', out['notes'])
        self.assertEqual(out['review'], [1, 'Harbor', 'not chosen'])
        self.assertEqual(out['runnable'], ['Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.'])

    def test_a_release_runs_from_pypi_through_uvx(self):
        out = self.out('r.harbor={kind:"release",version:"0.24.0",extras:["dspy"]}')
        self.assertEqual((out['errors'], out['runnable']), ([], []))
        self.assertIn("\n  uvx --from 'harbor[dspy]==0.24.0' harbor run \\", out['run'])
        self.assertEqual(out['review'], [1, 'Harbor', 'harbor 0.24.0 · PyPI · extras dspy'])
        self.assertEqual(out['back'], {'kind': 'release', 'version': '0.24.0', 'extras': ['dspy']})       # kept in the saved recipe
        self.assertTrue(any(e[1] == '1.0; x is not a Harbor release version.' and e[0] == 1 for e in self.out('r.harbor={kind:"release",version:"1.0; x",extras:[]}')['errors']))

    def test_a_repository_is_pinned_at_its_commit(self):
        c = 'c' * 40
        out = self.out('r.harbor={kind:"repo",repo:"owner/harbor",revisionKind:"branch",revision:"main",commit:"%s",extras:[]}' % c)
        self.assertEqual((out['errors'], out['runnable']), ([], []))
        self.assertIn("uvx --from 'harbor @ git+https://github.com/owner/harbor.git@%s' harbor run" % c, out['run'])
        self.assertEqual(out['review'], [1, 'Harbor', 'harbor from owner/harbor · branch main at cccccccccccc'])
        self.assertEqual(out['back'], {'kind': 'repo', 'repo': 'owner/harbor', 'revisionKind': 'branch', 'revision': 'main', 'commit': c, 'extras': []})
        scp = self.out('r.harbor={kind:"repo",repo:"me@host.example:forks/harbor.git",revisionKind:"commit",revision:"%s",commit:"%s",extras:["dspy"]}' % (c, c))
        self.assertIn("'harbor[dspy] @ git+ssh://me@host.example/forks/harbor.git@%s'" % c, scp['run'])  # an scp-like address, as uv takes it
        self.assertEqual(self.out('r.harbor={kind:"repo",repo:"",revisionKind:"branch",revision:"",commit:"",extras:[]}')['errors'][0],
                         [1, 'Enter the repository that holds the Harbor: owner/name on GitHub, or a git URL.'])
        self.assertEqual(self.out('r.harbor={kind:"repo",repo:"owner/harbor",revisionKind:"commit",revision:"abc1234",commit:"abc1234",extras:[]}')['errors'][0],
                         [1, 'Choose a branch, a tag or a full commit of owner/harbor: the pin is a commit.'])

    def test_a_recipe_from_before_the_pin_loads_and_asks(self):
        out = self.js('const old={...C.create("harbor"),model:"openai/x",agents:["tau3-llm-agent"]};delete old.harbor;const b=C.fromJSON(JSON.stringify(old));'
                      'process.stdout.write(JSON.stringify({h:b.harbor,e:C.validate(b).errors,u:C.runnable(b).errors,label:C.harborExecutable(b).label}));')
        self.assertEqual((out['h'], out['e'], out['label']), ({'kind': 'release', 'version': '', 'extras': []}, [], 'harbor from PATH'))
        self.assertIn('Choose the Harbor', out['u'][0])

    def test_the_key_is_the_server_s(self):
        from nullius.harbor import source
        for pin in ({'kind': 'release', 'version': '0.24.0', 'extras': []}, {'kind': 'repo', 'repo': 'owner/h', 'commit': 'a' * 40, 'extras': []}):
            js = self.js('process.stdout.write(JSON.stringify(C.harborKey(C.harborPin({harbor:%s}))))' % json.dumps(pin))
            self.assertEqual(js, source.key_of(source.pin_of(pin)[0]))

    def test_a_scanned_harbor_offers_its_own_agents_beside_the_vocabulary(self):
        out = self.js('const r=C.create("harbor");r.model="openai/x";r.agents=["claude-code"];const before=C.validate(r).errors.map(e=>e.message);'
                      'const list=C.setScanned(["nop","oracle","claude-code","opencode"],"harbor 0.24.0 · PyPI");const o=C.compile(r);'
                      'process.stdout.write(JSON.stringify({before,list:list.map(a=>[a.id,a.kind,a.scanned]),errors:o.errors,run:o.artifacts.find(a=>a.id==="run-claude-code").text,'
                      'cleared:C.setScanned(null).some(a=>a.kind==="builtin")}));')
        self.assertTrue(any("claude-code is not an agent this builder offers until the recipe's Harbor is scanned (Runner step)" in e for e in out['before']))
        self.assertIn(['claude-code', 'builtin', True], out['list']); self.assertIn(['oracle', 'control', True], out['list'])   # a vocabulary agent the scan lists is marked
        self.assertIn(['tau3-llm-agent', 'reference', False], out['list'])                                  # the vocabulary stays, marked
        self.assertEqual(out['errors'], [])
        self.assertIn("--agent 'claude-code'", out['run']); self.assertIn("--model 'openai/x'", out['run'])
        self.assertFalse(out['cleared'])

    def test_new_run_like_this_pins_a_release_a_job_recorded(self):
        out = self.js('const o=C.fromJob({job:"j",agent:"nop",launch:{},harbor:{version:"0.24.0",editable:false}}),f=C.fromJob({job:"k",agent:"nop",launch:{},harbor:{version:"0.22.0+fork.1"}});'
                      'process.stdout.write(JSON.stringify([o.recipe.harbor,f.recipe.harbor]));')
        self.assertEqual(out, [{'kind': 'release', 'version': '0.24.0', 'extras': []}, {'kind': 'release', 'version': '', 'extras': []}])



class HarborExecutableAndTools(CompilerCase):
    """Which Harbor a recipe runs, said exactly: whatever `harbor` is first on
    the target's PATH, or a development executable typed as a path - and
    nothing around it."""

    def run_line(self, edit='', artifact='run-custom-0'):
        out = self.js('const r=C.create("harbor");r.model="openai/x";r.source="local";r.path="/data/tau3 demo";'+AGENT+edit+';const o=C.compile(r);process.stdout.write(JSON.stringify({run:(o.artifacts.find(a=>a.id==="'+artifact+'")||{}).text||"",notes:o.notes,errors:o.errors.map(e=>e.message),label:C.harborExecutable(r).label}));')
        out['line'] = next((l.strip() for l in out['run'].split('\n') if ' run ' in l or l.strip().endswith(' run \\')), '')
        return out

    def test_the_harbor_on_path_runs_the_recipe_and_it_is_said(self):
        out = self.run_line()
        self.assertEqual(out['line'].strip(), 'harbor run \\', out['line'])                 # the harbor on PATH, no wrapper around it
        self.assertEqual(out['label'], 'harbor from PATH')
        self.assertEqual(out['errors'], [])

    def test_a_development_executable_is_a_path_and_is_labelled(self):
        out = self.run_line("r.harborDev='~/dev harbor/bin/harbor'")
        self.assertEqual('"$HOME"/\'dev harbor/bin/harbor\' run \\', out['line'].strip())
        self.assertTrue(out['label'].startswith('development · '))
        self.assertTrue(any('development evidence' in n for n in out['notes']))
        bad = self.run_line("r.harborDev='harbor'")
        self.assertTrue(any('must be a path' in e for e in bad['errors']))

    def test_unknown_recipe_fields_are_ignored_without_changing_the_runner(self):
        out = self.js('const raw={...C.create("harbor"),model:"openai/x",customAgents:["my_agent:MyAgent"],agents:["custom-0"],extraText:"ignored",extraConfig:{x:"ignored"}};const b=C.fromJSON(JSON.stringify(raw));'
                      'process.stdout.write(JSON.stringify({keys:Object.keys(b),errors:C.validate(b).errors,label:C.harborExecutable(b).label}));')
        self.assertNotIn('extraText', out['keys']); self.assertNotIn('extraConfig', out['keys'])
        self.assertEqual((out['errors'], out['label']), ([], 'harbor from PATH'))

    def test_the_script_needs_no_checkout_but_its_own_directory(self):
        out = self.run_line()
        for gone in ('agent/scripts', 'TOOLS_ROOT', 'with_key', 'sweep'):
            self.assertNotIn(gone, out['run'])

    def test_the_choice_persists_in_a_saved_recipe(self):
        out = self.js('const r=C.create("harbor");r.harborDev="~/h/bin/harbor";const b=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([b.harborDev]));')
        self.assertEqual(out, ['~/h/bin/harbor'])



class RunFromHere(CompilerCase):
    """What Run on the Review step posts: the same run commands with the
    Harbor, the tasks and the jobs directory as environment names, and a plain
    refusal for what cannot run from here yet."""

    def runnable(self, edit=''):
        return self.js('const r=C.create("harbor");r.agents=["tau3-llm-agent"];r.model="openai/x";r.source="local";r.path="/data/tau3 demo";' + PIN + edit
                       + ';process.stdout.write(JSON.stringify(C.runnable(r)));')

    def test_the_runner_script_names_the_environment_and_nothing_about_where(self):
        out = self.runnable()
        self.assertEqual(out['errors'], [])
        self.assertEqual((out['tasks'], out['model']), ('/data/tau3 demo', 'openai/x'))
        self.assertNotIn('harborVersion', out)                                                           # the pin travels in the recipe; the script names $NULLIUS_HARBOR only
        s = out['script']
        self.assertIn('"$NULLIUS_HARBOR" run', s); self.assertIn('-p "$NULLIUS_TASKS"', s); self.assertIn('--jobs-dir "$NULLIUS_JOBS"', s)
        self.assertNotIn('with_key', s); self.assertNotIn('/data/tau3 demo', s)
        self.assertIn('LLM_API_KEY="${API_TOKEN', s)                                                     # the credentials map as before
        self.assertTrue(s.startswith('#!/usr/bin/env bash')); self.assertIn('exit "$status"', s)
        subprocess.run(['bash', '-n'], input=s, text=True, check=True)
        # a registry dataset: no task folder to send
        reg = self.runnable('C.setBenchmark(r,"custom");r.agents=["nop"];r.source="registry";r.dataset="terminal-bench@2.0"')
        self.assertEqual(reg['tasks'], ''); self.assertIn("-d 'terminal-bench@2.0'", reg['script'])
        # several agents: controls first (nop, then oracle), one part each, and the whole script in that order too
        three = self.runnable('r.agents=["tau3-llm-agent","oracle","nop"]')
        self.assertEqual([p['id'] for p in three['parts']], ['nop', 'oracle', 'tau3-llm-agent']); self.assertEqual([j['name'] for j in three['jobs']], ['harbor-experiment-nop', 'harbor-experiment-oracle', 'harbor-experiment-tau3-llm-agent'])
        self.assertEqual(three['script'].count('"$NULLIUS_HARBOR" run'), 3); self.assertLess(three['script'].index("--agent 'nop'"), three['script'].index("--agent 'tau3_llm_agent:Tau3LLMAgent'"))
        for p in three['parts']:
            subprocess.run(['bash', '-n'], input=p['script'], text=True, check=True)

    def test_what_cannot_run_from_here_is_said(self):
        self.assertIn('generator', self.runnable('r.source="generate";r.generatorRoot="../tau2";r.path="/t"')['errors'][0])
        self.assertIn('custom agent', self.runnable('r.customAgents=["pkg.mod:Agent"];r.agents=["custom-0"]')['errors'][0])
        self.assertIn('development', self.runnable('r.harborDev="~/dev/harbor"')['errors'][0])
        self.assertEqual(self.runnable('r.harbor={kind:"release",version:"",extras:[]}')['errors'],
                         ['Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.'])

    def test_where_travels_with_the_recipe(self):
        out = self.js('const r=C.create("harbor");r.where="bench";const back=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([C.create("harbor").where, back.where]));')
        self.assertEqual(out, ['local', 'bench'])



class DraftBetweenPages(CompilerCase):
    """A browser draft preserves the recipe, step and tab between page visits.
    Opening a run's recipe or an explicit prefill takes precedence."""

    def test_a_draft_packs_and_comes_back_unless_the_page_was_opened_for_something_else(self):
        out = self.js('const r=C.create("harbor");r.tasks=9;r.title="mine";const text=C.packDraft({recipe:r,step:4,tab:"tau3-llm-agent"});'
                      'const back=C.unpackDraft(text,{});const pre=C.unpackDraft(text,{dataset:"x"});const run=C.unpackDraft(text,{run:"j"});const bad=C.unpackDraft("{nope",{});const none=C.unpackDraft(null,{});'
                      'process.stdout.write(JSON.stringify([typeof text,back.recipe.tasks,back.recipe.title,back.step,back.tab,typeof back.at,pre,run,bad,none]));')
        self.assertEqual(out, ['string', 9, 'mine', 4, 'tau3-llm-agent', 'string', None, None, None, None])


if __name__ == "__main__":
    unittest.main()
