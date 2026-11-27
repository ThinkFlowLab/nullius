"""Harbor selection, runnable scripts and saved recipe drafts."""
import json
import re
import subprocess
import unittest

from ui_tests.builder_support import AGENT, APP, PIN, CompilerCase

PAGE = APP / "static/builder/command-builder.js"


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

        out = self.js('const old={...C.create("harbor"),model:"openai/x",agents:["tau3-llm-agent"]};delete old.harbor;const b=C.fromJSON(JSON.stringify(old));'
                      'process.stdout.write(JSON.stringify({h:b.harbor,e:C.validate(b).errors,u:C.runnable(b).errors,label:C.harborExecutable(b).label}));')
        self.assertEqual((out['h'], out['e'], out['label']), ({'kind': 'release', 'version': '', 'extras': []}, [], 'harbor from PATH'))
        self.assertIn('Choose the Harbor', out['u'][0])

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
        saved = self.js('const r=C.create("harbor");r.harborDev="~/h/bin/harbor";const b=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([b.harborDev]));')
        self.assertEqual(saved, ['~/h/bin/harbor'])                                         # a saved recipe keeps the path


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
        self.assertIn('LLM_API_KEY="${API_TOKEN', s)
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
        out = self.js('const r=C.create("harbor");r.tasks=9;r.title="mine";const text=C.packDraft({recipe:r,step:4,tab:"tau3-llm-agent",saved:"0123456789abcdef"});'
                      'const back=C.unpackDraft(text,{});const pre=C.unpackDraft(text,{dataset:"x"});const run=C.unpackDraft(text,{run:"j"});const bad=C.unpackDraft("{nope",{});const none=C.unpackDraft(null,{});'
                      'process.stdout.write(JSON.stringify([typeof text,back.recipe.tasks,back.recipe.title,back.step,back.tab,typeof back.at,pre,run,bad,none,back.saved]));')
        # `saved`: the saved recipe the page holds, so a save after a reload still updates (renames) it
        self.assertEqual(out, ['string', 9, 'mine', 4, 'tau3-llm-agent', 'string', None, None, None, None, '0123456789abcdef'])

    def test_start_over_and_delete_have_a_question_to_ask(self):
        out = self.js('const r=C.prefill({}),back=C.unpackDraft(C.packDraft({recipe:r,step:0,tab:"select"}),{}).recipe,edited=C.prefill({}),bad=C.prefill({});edited.title="mine";bad.repeat=33;'
                      'process.stdout.write(JSON.stringify([C.atDefaults(r),C.atDefaults(back),C.atDefaults(edited),C.atDefaults(C.create("harbor")),C.atDefaults(bad),'
                      'C.deleteAsk({id:"0123456789abcdef",name:"a<b"},"h",x=>String(x).replace(/</g,"&lt;"))]));')
        # an untouched recipe, a reloaded draft of it too, has nothing to lose; an edited one, another runner's and
        # one holding a value a saved recipe would not keep have
        self.assertEqual(out[:5], [True, True, False, False, False])
        self.assertEqual(out[5], '<div class="std-how-head"><h2 id="h">Delete recipe a&lt;b</h2></div><div class="launch-delete-acts">'
                                 '<button type="button" class="btn ghost" data-close autofocus>Keep it</button>'
                                 '<button type="button" class="btn danger" data-delete="0123456789abcdef">Delete recipe</button></div>')

    def test_start_over_resets_an_edited_recipe_on_yes_alone(self):
        """The page's own click handler and its own untouched check, on stand-in elements: what the draft holds after each click."""
        page = PAGE.read_text(encoding="utf-8")
        wiring = re.search(r"^  const startOverClick=.*$", page, re.M)
        clicks = page[page.index("  root.addEventListener('click',async event=>{"):page.index("  // a benchmark chosen on the Benchmark step's grid")]
        program = ('const C=require(' + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ')(' + json.dumps(self.schema) + ');'
                   'const {startOver}=require(' + json.dumps(str(PAGE)) + ');'
                   'const data=new Map(),localStorage={removeItem:k=>data.delete(k),setItem:(k,v)=>data.set(k,v)},draftKey="draft",S=C.STEP,REVIEW=S.review;'
                   'let recipe=C.prefill({}),step=S.limits,tab="select",savedId="",runnerOut,runnerKey,modelActed,click;recipe.repeat=33;'
                   'const r=()=>recipe,status=()=>{},render=()=>localStorage.setItem(draftKey,C.packDraft({recipe,step,tab,saved:savedId}));'
                   'const el=()=>({hidden:true,a:{"aria-expanded":"false"},getAttribute(k){return this.a[k];},setAttribute(k,v){this.a[k]=v;},focus(){}});'
                   'const els={"#nb-startover":el(),"#nb-startover-ask":el(),"#nb-startover-keep":el()},$=s=>els[s];'
                   'const root={addEventListener:(type,f)=>{click=f;}};\n' + (wiring.group(0) if wiring else "") + "\n" + clicks + '\n'
                   'const press=async id=>{await click({target:{closest:()=>({id,dataset:{}})}});const d=JSON.parse(data.get(draftKey));'
                   'return [d.recipe.repeat??null,d.step,els["#nb-startover-ask"].hidden];};'
                   '(async()=>{render();const got=[];for(const id of ["nb-startover","nb-startover-keep","nb-startover","nb-startover","nb-startover","nb-startover-yes"])got.push(await press(id));'
                   'step=S.limits;got.push(await press("nb-startover"));process.stdout.write(JSON.stringify({got,limits:S.limits,first:S.benchmark}));})();')
        out = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        asked, kept, reset = [33, out["limits"], False], [33, out["limits"], True], [None, out["first"], True]
        # Start over asks, Keep it closes; Start over asks, Start over again closes; Start over asks, Yes resets; then untouched: at once
        self.assertEqual(out["got"], [asked, kept, asked, kept, asked, reset, reset])

    def test_a_step_drawn_again_keeps_the_keyboard_on_its_control(self):
        """The page's own redraws (renderForm's line, the parameter Type handler) and controls.js' scan, on stand-in elements that
        match selectors and hold the focus: which control has it once its box is drawn again."""
        page = PAGE.read_text(encoding="utf-8")
        controls = (APP / "static/controls.js").read_text(encoding="utf-8")
        # controls.js' own scan, kept reachable: the frames that call it are not there
        controls = controls[:controls.index("\n// A .ui-range's filled part")].replace("  scan();\n})();", "  listeners.frame = scan; scan();\n})();")
        form = re.search(r"^    keepFocus\(\$\('#nb-form'\),.*$", page, re.M)
        family = re.search(r"^    else if\(d\.stdFamilySet!==undefined\).*$", page, re.M).group(0)
        change = page[page.index("  root.addEventListener('change',async event=>{"):page.index("  { const dl=document.getElementById('nb-models')")]
        program = r"""
const body = {name: 'body', matches: () => false}, listeners = {};
// elements that match the selectors the page uses, hold the focus, and lose it when hidden or taken out of the page
const el = (name, tagName, data = {}, more = {}) => Object.assign({name, tagName, id: '', dataset: data, size: 0, hidden: false, labels: [],
  attributes: [{name: 'class', value: 'x'}, ...Object.entries(data).map(([k, value]) => ({name: 'data-' + k, value}))],
  matches(s) { return s === '.ui-select-trigger' ? this.className === 'ui-select-trigger' : s[0] === '#' ? s === '#' + this.id
    : s.startsWith(this.tagName + '[') && s.slice(this.tagName.length + 1, -1).split('][').every(one => this.attributes.some(a => one === `${a.name}="${CSS.escape(a.value)}"`)); },
  get isConnected() { return Object.values(boxes).some(b => b.contains(this)); }, remove() {},
  focus() { if (!this.gone) document.activeElement = this; }, setSelectionRange(...a) { this.name += ' ' + a.join(' '); },
  classList: {add() {}}, setAttribute() {}, getAttribute: () => null, removeAttribute() {}, addEventListener() {}, closest: () => null, replaceChildren() {},
  after(b) { this.nextElementSibling = b; b.previousElementSibling = this; b.name = this.name + ' menu'; }, selectedOptions: [{textContent: 'a', dataset: {}}]}, more);
const hides = select => { select.classList.add = () => { select.gone = true; if (document.activeElement === select) document.activeElement = body; }; return select; };
const box = () => ({els: [], next: [], contains(e) { return this.els.includes(e) || this.els.some(x => x.nextElementSibling === e); },
  querySelector(s) { return this.els.find(e => e.matches(s)) || null; },
  set innerHTML(html) { if (this.contains(document.activeElement)) document.activeElement = body; this.els = this.next; }});
const boxes = {'#nb-form': box(), '#nb-parameters': box()}, $ = s => boxes[s];
globalThis.CSS = {escape: s => s.replace(/"/g, '\\$&')};
globalThis.document = {body, activeElement: body, addEventListener() {}, createElement: tag => el('', tag.toUpperCase()),
  querySelectorAll: s => s.startsWith('select') ? Object.values(boxes).flatMap(b => b.els).filter(e => e.tagName === 'SELECT') : []};
globalThis.window = {addEventListener() {}}; globalThis.MutationObserver = class { observe() {} }; globalThis.requestAnimationFrame = () => {};
const keepFocus = require(__PAGE__).keepFocus || ((box, draw) => draw());   // a page without it only draws
const root = {addEventListener: (type, f) => { listeners[type] = f; }}, customRows = () => '', placeGrids = () => {}, html = '', tab = 'extra';
__CONTROLS__
const scan = () => listeners.frame();
__CHANGE__
const got = [], say = () => got.push(document.activeElement.name);
(async () => {
  // a choice: the step drawn again by renderForm's own line
  const f = boxes['#nb-form'], oracle = el('old', 'BUTTON', {agent: 'oracle'});
  f.els = [oracle]; f.next = [el('nop', 'BUTTON', {agent: 'nop'}), el('oracle', 'BUTTON', {agent: 'oracle'})]; oracle.focus();
  __FORM__
  say();
  // the same id; a field on its selection; a choice whose other data changed with it; nothing to tell it by; outside the box
  const redraw = (on, next, inside = true) => { f.els = inside ? [on] : []; f.next = next; document.activeElement = on; keepFocus(f, () => { f.innerHTML = ''; }); say(); };
  redraw(el('old', 'BUTTON', {x: 'y'}, {id: 'nb-add'}), [el('other', 'BUTTON', {x: 'y'}), el('add', 'BUTTON', {x: 'z'}, {id: 'nb-add'})]);
  redraw(el('old', 'INPUT', {field: 'a"b'}, {selectionStart: 2, selectionEnd: 5, selectionDirection: 'backward'}), [el('field', 'INPUT', {field: 'a"b'})]);
  redraw(el('old', 'BUTTON', {family: 'small', was: 'all'}), [el('all', 'BUTTON', {family: 'all', was: 'small'}), el('small', 'BUTTON', {family: 'small', was: 'small'})]);
  // a set of two lists chosen in its second, by the page's own line: both lists' groups are drawn, the set in each of them
  const set = (was, to) => el('set ' + was + ' ' + to, 'BUTTON', {stdFamilySet: 'mine', was, in: to}), old = set('l3', 'l2');
  { const d = old.dataset, C = {swapDataset() {}, setTaskSet() {}}, r = () => ({}); if (0) {}
__FAMILY__
    old.attributes.find(a => a.name === 'data-was').value = d.was; }
  redraw(old, [set('l1', 'l1'), set('l1', 'l2'), set('l2', 'l1'), set('l2', 'l2')]);
  redraw(el('old', 'BUTTON'), [el('plain', 'BUTTON')]);
  redraw(el('outside', 'BUTTON', {agent: 'oracle'}), [el('oracle', 'BUTTON', {agent: 'oracle'})], false);
  // the second parameter's Type, by the page's own change handler: its select is drawn again, then controls.js draws its menu button
  const p = boxes['#nb-parameters'], type = hides(el('old type', 'SELECT', {custom: '1:type'}));
  f.els = []; p.els = [type]; scan(); type.nextElementSibling.focus(); say();
  p.next = [hides(el('type 0', 'SELECT', {custom: '0:type'})), hides(el('type 1', 'SELECT', {custom: '1:type'}))];
  await listeners.change({target: type}); say(); scan(); say();
  process.stdout.write(JSON.stringify(got));
})();
"""
        program = (program.replace("__PAGE__", json.dumps(str(PAGE))).replace("__CONTROLS__", controls).replace("__CHANGE__", change).replace("__FAMILY__", family)
                   .replace("__FORM__", form.group(0) if form else ""))
        out = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, ["oracle", "add", "field 2 5 backward", "small", "set l2 l2", "body", "outside",
                               "old type menu", "type 1", "type 1 menu"])   # the Type's menu button, its new select, that one's button

    def test_removing_a_row_puts_the_keyboard_on_add(self):
        """The page's own click handler: the row after a removed one takes its number, so the focus leaves Remove for Add."""
        page = PAGE.read_text(encoding="utf-8")
        clicks = page[page.index("  root.addEventListener('click',async event=>{"):page.index("  // a benchmark chosen on the Benchmark step's grid")]
        program = ('const got=[],rec={custom:[{key:"first"},{key:"second"}],customAgents:["a.A","b.B"],agents:["oracle","custom-0","custom-1"]};let click;'
                   'const r=()=>rec,render=()=>{},renderForm=()=>{},refreshPlan=()=>{},$=s=>({focus(){got.push(s);}}),root={addEventListener:(type,f)=>{click=f;}};\n' + clicks + '\n'
                   '(async()=>{await click({target:{closest:()=>({id:"",dataset:{remove:"0"}})}});await click({target:{closest:()=>({id:"",dataset:{removeCustomAgent:"0"}})}});'
                   'process.stdout.write(JSON.stringify([got,rec.custom,rec.customAgents,rec.agents]));})();')
        out = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, [["#nb-add", "#nb-add-custom-agent"], [{"key": "second"}], ["b.B"], ["oracle", "custom-0"]])


    def test_saving_closes_an_open_start_over_question(self):
        """Save swaps in its own copy of the draft, so a Yes left open would clear the saved recipe's successor."""
        page = PAGE.read_text(encoding="utf-8")
        drop = re.search(r"^  const dropStartOverAsk=.*$", page, re.M)
        save = page[page.index("  async function save(){"):page.index("  root.addEventListener('submit'")]
        program = ('const C=require(' + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ')(' + json.dumps(self.schema) + ');'
                   'let recipe=C.prefill({}),saved=[],savedId="",compiled={errors:[]};const r=()=>recipe,render=()=>{},renderSaved=()=>{},status=()=>{},recipeSlug=t=>t.toLowerCase();'
                   'const store={save:async c=>({id:"0123456789abcdef",recipe:c})};'
                   'const el=()=>({hidden:false,value:"Renamed",textContent:"",a:{"aria-expanded":"true"},setAttribute(k,v){this.a[k]=v;},close(){},focus(){}});'
                   'const els={"#nb-startover":el(),"#nb-startover-ask":el(),"#nb-recipe-title":el(),"#nb-save-dialog":el(),"#nb-save-message":el()},$=s=>els[s];\n'
                   + (drop.group(0) if drop else "") + "\n" + save + '\n'
                   '(async()=>{await save();process.stdout.write(JSON.stringify([recipe.title,els["#nb-startover-ask"].hidden,els["#nb-startover"].a["aria-expanded"]]));})();')
        out = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, ["Renamed", True, "false"])


if __name__ == "__main__":
    unittest.main()
