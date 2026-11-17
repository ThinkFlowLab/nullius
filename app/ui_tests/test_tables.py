"""The table toolkit: its macros (templates/_tables.html) as a page renders them, and its script
(static/tables.js) run under Node on a small stand-in DOM."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[1]

# A stand-in DOM: elements built by el(tag, attrs, ...children), selectors of one compound each
# (tag, .class, [attr], [attr="v"], :checked, :not(:disabled)), events that bubble to the document.
DOM = r'''
const camel = k => k.replace(/-(\w)/g, (_, c) => c.toUpperCase()), kebab = k => k.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
const simple = (e, s) => {
  if (s.startsWith(':not(')) return !simple(e, s.slice(5, -1));
  if (s === ':checked') return e.checked;
  if (s === ':disabled') return e.disabled;
  if (s[0] === '.') return e.classList.contains(s.slice(1));
  if (s[0] === '[') { const [, k, v] = s.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/); return e.hasAttribute(k) && (v === undefined || e.getAttribute(k) === v); }
  return e.tagName === s.toUpperCase();
};
const matches = (e, sel) => sel.split(',').some(one => (one.trim().match(/:not\([^)]*\)|\[[^\]]*\]|[.:]?[\w-]+/g) || []).every(s => simple(e, s)));
class El {
  constructor(tag, attrs = {}, kids = []) {
    Object.assign(this, {tagName: tag.toUpperCase(), attrs: {}, children: [], parentNode: null, on: {}, hidden: false, checked: false,
                         disabled: false, value: '', indeterminate: false, width: 0, text: ''});
    this.style = {setProperty(k, v) { this[k] = v; }};
    const cls = new Set();
    this.classList = {contains: c => cls.has(c), add: c => cls.add(c), remove: c => cls.delete(c), toggle: (c, on = !cls.has(c)) => (on ? cls.add(c) : cls.delete(c), on)};
    for (const [k, v] of Object.entries(attrs)) this.setAttribute(k, v === true ? '' : String(v));
    this.hidden = 'hidden' in attrs; this.checked = 'checked' in attrs; this.disabled = 'disabled' in attrs;
    kids.forEach(k => this.appendChild(typeof k === 'string' ? Object.assign(new El('#text'), {text: k}) : k));
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') v.split(' ').filter(Boolean).forEach(c => this.classList.add(c)); if (k === 'value') this.value = v; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  toggleAttribute(k, on) { on ? this.setAttribute(k, '') : this.removeAttribute(k); }
  get dataset() {
    return new Proxy({}, {get: (_, k) => this.getAttribute('data-' + kebab(k)) ?? undefined,
                          set: (_, k, v) => { this.setAttribute('data-' + kebab(k), v); return true; }});
  }
  get id() { return this.getAttribute('id'); }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.text = String(v); this.children = []; }
  appendChild(k) { if (k.parentNode) k.parentNode.children.splice(k.parentNode.children.indexOf(k), 1); k.parentNode = this; this.children.push(k); return k; }
  get all() { return this.children.flatMap(c => [c, ...c.all]); }
  querySelectorAll(sel) { return this.all.filter(e => e.tagName !== '#TEXT' && matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  matches(sel) { return matches(this, sel); }
  closest(sel) { for (let e = this; e; e = e.parentNode) if (e.tagName !== '#DOC' && matches(e, sel)) return e; return null; }
  contains(e) { for (; e; e = e.parentNode) if (e === this) return true; return false; }
  addEventListener(t, f) { (this.on[t] ||= []).push(f); }
  dispatch(type, props = {}) {
    const ev = {type, target: this, preventDefault() {}, stopPropagation() { this.stopped = true; }, ...props};
    for (let e = this; e && !ev.stopped; e = e.parentNode) (e.on[type] || []).forEach(f => f(ev));
    return ev;
  }
  click() {
    if (this.tagName === 'INPUT' && this.getAttribute('type') === 'checkbox') this.checked = !this.checked;
    if (this.tagName === 'INPUT' && this.getAttribute('type') === 'radio') {
      document.querySelectorAll(`[name="${this.getAttribute('name')}"]`).forEach(r => { r.checked = false; });
      this.checked = true;
    }
    this.dispatch('click');
    if (this.tagName === 'INPUT') this.dispatch('change');
  }
  focus() { document.activeElement = this; }
  getBoundingClientRect() { return {width: this.width}; }
  get previousElementSibling() { const s = this.parentNode.children; return s[s.indexOf(this) - 1] || null; }
  get cellIndex() { return this.parentNode.children.indexOf(this); }
  get rows() { return this.querySelectorAll('tr'); }
  get cells() { return this.children; }
  get tBodies() { return this.children.filter(c => c.tagName === 'TBODY'); }
  get tHead() { return this.children.find(c => c.tagName === 'THEAD'); }
  get options() { return this.children; }
}
global.document = new El('#doc');
document.readyState = 'complete';
document.getElementById = id => document.querySelector(`[id="${id}"]`);
global.window = global;
global.CSS = {escape: s => s};
global.localStorage = {getItem: () => null, setItem() {}};
const observers = [];
global.ResizeObserver = class { constructor(f) { this.f = f; observers.push(this); } observe() {} };
global.MutationObserver = class { constructor(f) { this.f = f; } observe() {} };
global.el = (tag, attrs, ...kids) => new El(tag, attrs, kids);
global.resized = () => observers.forEach(o => o.f());
require(process.argv[1]);
const T = window.NulliusTable;

// A table as the table macro writes it: `id`; columns [key, type]; rows, each {search, f-<col>, cells}; `filter`: a
// column and its values; `search`; `sort`.
global.build = ({id, cols, rows, filter, search = true, sort = true, check = false}) => {
  const radio = (k, v) => el('label', {}, el('input', {type: 'radio', name: `${id}-sort-${k}`, [`data-sort-${k}`]: '', value: v}), ' ' + v);
  const tools = el('div', {id: id + '-tools'},
    ...(search ? [el('label', {}, el('input', {type: 'search', 'data-search': ''}))] : []),
    ...(filter ? [el('button', {'data-tools-btn': 'filter'}, 'Filter', el('span', {class: 'tools-count', hidden: true})),
                  el('div', {'data-tools': 'filter', hidden: true}, el('button', {'data-clear': ''}),
                     el('fieldset', {'data-filter-menu': filter[0]}, ...filter[1].map(v => el('label', {}, el('input', {type: 'checkbox', 'data-value': v, checked: true}), ' ' + v))))] : []),
    ...(sort ? [el('button', {'data-tools-btn': 'sort'}),
                el('div', {'data-tools': 'sort', hidden: true}, el('fieldset', {}, radio('col', ''), ...cols.map(([k]) => radio('col', k))),
                   el('fieldset', {}, radio('dir', 'asc'), radio('dir', 'desc')))] : []));
  const head = el('tr', {}, ...(check ? [el('th', {}, el('input', {type: 'checkbox', 'data-check-all': ''}))] : []),
                  ...cols.map(([k, type]) => el('th', {'data-col': k, 'data-type': type}, k)));
  const body = el('tbody', {}, ...rows.map(r => el('tr', Object.fromEntries(Object.entries(r).filter(([k]) => k !== 'cells').map(([k, v]) => [k === 'search' ? 'data-search' : 'data-' + k, v])),
                  ...(check ? [el('td', {}, el('input', {type: 'checkbox', 'data-check': ''}))] : []), ...r.cells.map(c => el('td', {}, String(c))))));
  const table = el('table', {id, class: 'grid', 'data-table': ''}, el('thead', {}, head), body);
  const per = el('select', {'data-per': ''}, el('option', {}, '10'), el('option', {}, '25'));
  per.value = '25';
  const pager = el('div', {'data-pager': id}, per, el('button', {'data-prev': ''}), el('span', {'data-note': ''}), el('button', {'data-next': ''}));
  const page = el('main', {}, tools, table, el('p', {'data-empty': '', hidden: true}), pager);
  document.appendChild(page);
  T.wire(table);
  const shown = () => table.tBodies[0].rows.filter(r => !r.hidden).map(r => r.cells[check ? 1 : 0].textContent);
  const type = q => { const box = tools.querySelector('[data-search]'); box.value = q; box.dispatch('input'); };
  return {table, tools, page, per, pager, shown, type, note: () => pager.querySelector('[data-note]').textContent,
          empty: () => !page.querySelector('[data-empty]').hidden};
};
'''


def node(program: str) -> object:
    """``program``'s JSON output, run after the stand-in DOM and tables.js."""
    if not shutil.which("node"):
        raise unittest.SkipTest("Node is required for the table toolkit's script")
    out = subprocess.run(["node", "-e", DOM + program, str(APP / "static" / "tables.js")], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class Sticky(unittest.TestCase):
    def test_each_offset_is_the_widths_before_it(self):
        got = node(r'''
const sticky = (tag, w) => Object.assign(el(tag, {class: 'sticky-col'}), {width: w});
const head = el('tr', {}, sticky('th', 35.2), sticky('th', 120), el('th', {}), sticky('th', 50));
const row = el('tr', {}, sticky('td'), sticky('td'), el('td', {}), sticky('td'));
const table = el('table', {}, el('thead', {}, head), el('tbody', {}, row));
T.sticky(table);
const at = () => [...head.cells, ...row.cells].map(c => c.style['--sticky-left'] ?? null);
const out = [at()];
head.cells[0].width = 40; resized(); out.push(at());                // a column grows: those after it move
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [["0px", "35.2px", None, "155.2px"] * 2, ["0px", "40px", None, "160px"] * 2])



if __name__ == "__main__":
    unittest.main()
