"""The first-open question, run under Node with a small stand-in DOM: Tab stays on its two buttons,
Escape answers as No does, and the focus goes back where it was. The First steps panel closes on Escape
from its toggle or from inside it, the focus on its toggle."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "static" / "onboarding.js"

ASK = r'''
const given = JSON.parse(require('fs').readFileSync(0, 'utf8')), store = () => { const m = new Map();
  return {getItem: k => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, v), removeItem: k => m.delete(k)}; };
class El {
  constructor(tag, attrs = {}) { this.tag = tag; this.attrs = attrs; this.c = []; this.on = {}; this.classList = {toggle() {}}; }
  set innerHTML(h) { this.c = [...h.matchAll(/data-a="(\w+)"/g)].map(m => Object.assign(new El('button', {'data-a': m[1]}), {parent: this, dataset: {a: m[1]}})); }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector(s) { return this.c.find(b => s === '[data-a=' + b.dataset.a + ']') || null; }
  querySelectorAll(s) { return s === 'button' ? this.c : []; }
  closest(s) { return s === '[data-a]' && this.dataset?.a ? this : null; }
  addEventListener(t, f) { (this.on[t] ||= []).push(f); }
  fire(t, e = {}) { (this.on[t] || []).forEach(f => f(Object.assign({type: t, preventDefault() { e.prevented = true; }}, e))); return e; }
  focus() { document.activeElement = this; }
  append(x) { this.c.push(x); x.parent = this; }
  remove() { this.removed = true; }
  showModal() { this.open = true; }
  close(v) { if (v !== undefined) this.returnValue = v; this.open = false; this.fire('close'); }
}
const replay = new El('button', {'data-onb': 'replay'}), made = [];
global.document = {currentScript: {dataset: {state: '/onboarding', tour: '[]'}}, body: new El('body'), activeElement: replay,
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
  createElement: tag => { const e = new El(tag); e.returnValue = ''; made.push(e); return e; }};
Object.assign(global, {localStorage: store(), sessionStorage: store(), location: {pathname: '/', search: '', href: '/'},
  addEventListener() {}, matchMedia: () => ({matches: false}), ResizeObserver: class { observe() {} disconnect() {} },
  nulliusFetch: () => Promise.resolve({ok: true, json: () => ({sections: []})})});
require(given.script);
setImmediate(() => {
  const d = made.find(e => e.tag === 'dialog'), at = () => document.activeElement.dataset?.a;
  const out = {modal: !!d.open, first: at(), tabs: []};
  for (const shift of [false, false, false, true, true, true]) {
    const e = d.fire('keydown', {key: 'Tab', shiftKey: shift}); out.tabs.push([at(), !!e.prevented]);
  }
  if (given.answer === 'escape') d.close();                                       // the browser's close request: no returnValue
  else d.fire('click', {target: d.querySelector('[data-a=' + given.answer + ']')});
  Object.assign(out, {closed: !d.open && !!d.removed, focus: document.activeElement === replay,
    state: JSON.parse(localStorage.getItem('nullius-onboarding'))});
  process.stdout.write(JSON.stringify(out));
});
'''


PANEL = r'''
const given = JSON.parse(require('fs').readFileSync(0, 'utf8')), store = () => { const m = new Map();
  return {getItem: k => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, v), removeItem: k => m.delete(k)}; };
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.c = []; this.classList = {toggle() {}}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector() { return {}; }
  contains(x) { return x === this || this.c.includes(x); }
  append(x) { this.c.push(x); }
  focus() { document.activeElement = this; }
  remove() { this.removed = true; }
}
const toggle = new El('button'), other = new El('a'), made = [], keys = [];
global.document = {currentScript: {dataset: {state: '/onboarding', tour: '[]'}}, body: new El('body'), activeElement: other,
  getElementById: id => id === 'onb-open' ? toggle : null, querySelector: () => null, querySelectorAll: () => [],
  addEventListener: (t, f) => { if (t === 'keydown') keys.push(f); }, createElement: tag => { const e = new El(tag); made.push(e); return e; }};
Object.assign(global, {localStorage: store(), sessionStorage: store(), location: {pathname: '/', search: '', href: '/'},
  addEventListener() {}, matchMedia: () => ({matches: false}), ResizeObserver: class { observe() {} disconnect() {} },
  nulliusFetch: () => Promise.resolve({ok: true, json: () => ({sections: []})})});
localStorage.setItem('nullius-onboarding', JSON.stringify({asked: true, open: true}));
require(given.script);
setImmediate(() => {
  const panel = made.find(e => e.tag === 'aside'), inside = new El('button');
  panel.append(inside);
  document.activeElement = {toggle, inside, other}[given.focus];
  keys.forEach(f => f({key: 'Escape'}));
  process.stdout.write(JSON.stringify({open: JSON.parse(localStorage.getItem('nullius-onboarding')).open,
    expanded: toggle.attrs['aria-expanded'], removed: !!panel.removed, focus: document.activeElement === toggle}));
});
'''


def node(program: str, given: dict) -> dict:
    if not shutil.which("node"):
        raise unittest.SkipTest("Node is required for the onboarding script")
    out = subprocess.run(["node", "-e", program], input=json.dumps({"script": str(SCRIPT), **given}),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def ask(answer: str) -> dict:
    return node(ASK, {"answer": answer})


class FirstOpenQuestion(unittest.TestCase):
    def test_it_is_modal_and_tab_stays_on_its_buttons(self):
        out = ask("no")
        self.assertEqual((out["modal"], out["first"]), (True, "yes"))
        self.assertEqual(out["tabs"], [["no", True], ["yes", True], ["no", True], ["yes", True], ["no", True], ["yes", True]])

    def test_escape_answers_as_no_does_and_the_focus_goes_back(self):
        no, escape, yes = ask("no"), ask("escape"), ask("yes")
        self.assertEqual(escape["state"], no["state"])
        self.assertEqual((no["state"]["asked"], no["state"]["open"]), (True, False))
        self.assertTrue(yes["state"]["open"])                                       # Yes still starts the guide
        for out in (no, escape, yes):
            self.assertTrue(out["closed"]); self.assertTrue(out["focus"])


class FirstStepsPanel(unittest.TestCase):
    def test_escape_on_its_toggle_or_inside_it_closes_it_and_focuses_the_toggle(self):
        for focus in ("toggle", "inside"):
            with self.subTest(focus=focus):
                self.assertEqual(node(PANEL, {"focus": focus}), {"open": False, "expanded": "false", "removed": True, "focus": True})

    def test_escape_elsewhere_leaves_it_open(self):
        self.assertEqual(node(PANEL, {"focus": "other"}), {"open": True, "expanded": "true", "removed": False, "focus": False})


if __name__ == "__main__":
    unittest.main()
