"""Compare's columns (static/compare/columns.js) run under Node on the table toolkit's stand-in DOM: the run list's
search and keys, a workspace picked, the URL each choice writes, and the empty column that opens its list."""
import json
import shutil
import subprocess
import unittest

from .test_tables import APP, DOM

SETUP = r'''
require(process.argv[2]);
document.createElement = tag => new El(tag);
global.innerWidth = 1280;
global.addEventListener = () => {};
global.location = {get href() { return 'http://nullius/compare?pick=a:r1&pick=b:&cohort=matched'; }, set href(url) { this.went = url; }};
const replaced = [], fetched = [];
global.history = {replaceState: (state, title, url) => replaced.push(url)};
let served = {runs: [], same: 0};
global.nulliusFetch = url => { fetched.push(url); return Promise.resolve({ok: true, json: () => Promise.resolve(served)}); };
const C = window.NulliusCompare;
const run = (id, agent, launch, extra = {}) => ({value: 'a:' + id, id, agent, model: 'm', where: 'Bench / set', date: 'Sep 01  10:00', launch, check: false, shards: 1, ...extra});
const RUNS = [run('r1', 'claude-code', 'L2'), run('r2', 'codex', 'L2', {shards: 3}), run('r3', 'nop', 'L1', {check: true, model: ''})];
// a column's cells as compare.html writes them, in its rows: its header, its workspace, its run (held together here)
const column = (k, run, ws) => {
  const select = el('select', {'data-ws': ''}, el('option', {value: 'a'}), el('option', {value: 'b'}));
  select.value = ws;
  return el('tbody', {}, el('th', {'data-column': k, 'data-run': run}), el('td', {'data-column': k}, select),
    el('td', {'data-column': k}, el('div', {class: 'menu-wrap'}, el('button', {'data-run-btn': ''}, el('span', {'data-run-label': ''})),
      el('div', {'data-run-menu': '', hidden: true}, el('input', {type: 'search'}), el('ul', {role: 'listbox', id: `runs-${k}`}),
         el('p', {'data-none': '', hidden: true})))));
};
const page = (cols, cfg) => { cols.forEach(th => document.appendChild(th)); C.wire(cfg); return cols; };
const shown = th => th.querySelectorAll('[role="option"]').filter(o => !o.hidden).map(o => o.getAttribute('title'));
const type = (th, q) => { const box = th.querySelector('input'); box.value = q; box.dispatch('input'); };
const key = (th, k) => th.querySelector('input').dispatch('keydown', {key: k});
'''


def node(program: str) -> object:
    if not shutil.which("node"):
        raise unittest.SkipTest("Node is required for Compare's columns")
    out = subprocess.run(["node", "-e", DOM + SETUP + program, str(APP / "static" / "tables.js"),
                          str(APP / "static" / "compare" / "columns.js")], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class RunList(unittest.TestCase):
    def test_entries_launches_and_the_baseline_s_dataset_first(self):
        got = node(r'''
const [th] = page([column(0, 'r1', 'a')], {values: ['a:r1'], lists: {a: {runs: RUNS, same: 1}}, dataset: 'set', runs: '/compare/runs'});
const list = th.querySelector('ul');
const out = {items: list.children.map(li => [li.getAttribute('role'), li.textContent]),
             button: th.querySelector('[data-run-label]').textContent, tip: th.querySelector('[data-run-label]').getAttribute('title'),
             open: !th.querySelector('[data-run-menu]').hidden};
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got["items"], [
            ["option", "claude-code · mSep 01  10:00Bench / set"],
            ["separator", "Other datasets"],                                   # the baseline's dataset first, then the rest
            ["option", "codex · m3 shardsSep 01  10:00Bench / set"],
            ["separator", ""],                                                 # a line between launches
            ["option", "nopcheckSep 01  10:00Bench / set"],                   # a check, said so
        ])
        self.assertEqual((got["button"], got["tip"]), ("claude-code · mSep 01  10:00Bench / set", "r1"))
        self.assertFalse(got["open"])                                          # a column with its run stays closed

    def test_search_words_in_any_order_and_the_keys_pick_a_run(self):
        got = node(r'''
const [, th] = page([column(0, 'r1', 'a'), column(1, '', 'b')], {values: ['a:r1', 'b:'], lists: {a: {runs: RUNS, same: 0}, b: {runs: RUNS, same: 0}}, dataset: '', runs: '/compare/runs'});
const out = {open: !th.querySelector('[data-run-menu]').hidden, focus: document.activeElement === th.querySelector('input')};
type(th, 'M CODEX'); out.words = shown(th);
type(th, 'shards'); out.shards = shown(th);
type(th, 'check'); out.checks = shown(th);

out.lines = th.querySelectorAll('[role="separator"]').every(s => s.hidden);
type(th, 'zzz'); out.none = [shown(th), !th.querySelector('[data-none]').hidden];
type(th, ''); out.all = shown(th);
key(th, 'ArrowDown'); key(th, 'ArrowDown'); key(th, 'ArrowDown'); key(th, 'ArrowUp');
out.active = th.querySelector('input').getAttribute('aria-activedescendant');
key(th, 'Enter'); out.picked = location.went;
th.querySelectorAll('[role="option"]')[2].click(); out.clicked = location.went;
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual((got["open"], got["focus"]), (True, True))           # the empty column opens its list
        self.assertEqual(got["words"], ["r2"])
        self.assertEqual((got["shards"], got["checks"]), (["r2"], ["r3"]))     # what an entry shows is searched

        self.assertTrue(got["lines"])
        self.assertEqual(got["none"], [[], True])
        self.assertEqual(got["all"], ["r1", "r2", "r3"])
        self.assertEqual(got["active"], "runs-1-1")
        self.assertEqual(got["picked"], "/compare?cohort=matched&pick=a%3Ar1&pick=a%3Ar2")
        self.assertEqual(got["clicked"], "/compare?cohort=matched&pick=a%3Ar1&pick=a%3Ar3")

    def test_a_modal_over_the_page_leaves_the_opened_list_open(self):
        got = node(r'''
const [th] = page([column(0, '', 'a')], {values: ['a:'], lists: {a: {runs: RUNS, same: 0}}, dataset: '', runs: '/compare/runs'});
const box = th.querySelector('input'), list = th.querySelector('[data-run-menu]');
// the first-visit guide: a modal dialog that takes the focus, then a click on its answer
const answer = el('button', {}), guide = el('div', {role: 'dialog', 'aria-modal': 'true'}, answer);
document.appendChild(guide);
box.dispatch('focusout', {relatedTarget: answer});
answer.click();
const out = {kept: !list.hidden};
box.dispatch('focusout', {relatedTarget: el('a', {})});                 // Tab out to the page: closed
out.closed = list.hidden;
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, {"kept": True, "closed": True})

    def test_a_workspace_picked_lists_its_runs_in_place(self):
        got = node(r'''
const [full, empty] = page([column(0, 'r1', 'a'), column(1, '', 'a')], {values: ['a:r1', 'a:'], lists: {a: {runs: RUNS, same: 0}}, dataset: 'set', runs: '/compare/runs'});
served = {runs: [run('x9', 'aider', 'L9', {value: 'b:x9'})], same: 0};
empty.querySelector('[data-ws]').value = 'b'; empty.querySelector('[data-ws]').dispatch('change');
full.querySelector('[data-ws]').value = 'b'; full.querySelector('[data-ws]').dispatch('change');
setTimeout(() => process.stdout.write(JSON.stringify({fetched, replaced, listed: shown(empty), kept: full.querySelector('[data-run-label]').getAttribute('title'),
                                                      open: !full.querySelector('[data-run-menu]').hidden})), 0);
''')
        self.assertEqual(got["fetched"], ["/compare/runs?workspace=b&dataset=set"] * 2)
        self.assertEqual(got["listed"], ["x9"])
        self.assertEqual(got["replaced"], ["/compare?cohort=matched&pick=a%3Ar1&pick=b%3A"])   # only the empty column's URL moves
        self.assertEqual(got["kept"], "r1")                                    # a column keeps its run until another is picked
        self.assertTrue(got["open"])                                           # its list opens on the new runs

