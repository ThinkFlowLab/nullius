"""The table toolkit: its macros (templates/_tables.html) as a page renders them, and its script
(static/tables.js) run under Node on a small stand-in DOM."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import jinja2

APP = Path(__file__).resolve().parents[1]

# A stand-in DOM: elements built by el(tag, attrs, ...children), selectors of one compound each
# (tag, .class, [attr], [attr="v"] or [attr=v], :checked, :not(:disabled)), events that bubble to the document.
DOM = r'''
const camel = k => k.replace(/-(\w)/g, (_, c) => c.toUpperCase()), kebab = k => k.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
const simple = (e, s) => {
  if (s.startsWith(':not(')) return !simple(e, s.slice(5, -1));
  if (s === ':checked') return e.checked;
  if (s === ':disabled') return e.disabled;
  if (s[0] === '.') return e.classList.contains(s.slice(1));
  if (s[0] === '[') { const [, k, v] = s.match(/^\[([\w-]+)(?:="?([^"\]]*)"?)?\]$/); return e.hasAttribute(k) && (v === undefined || e.getAttribute(k) === v); }
  return e.tagName === s.toUpperCase();
};
const matches = (e, sel) => sel.split(',').some(one => (one.trim().match(/:not\([^)]*\)|\[[^\]]*\]|[.:]?[\w-]+/g) || []).every(s => simple(e, s)));
class El {
  constructor(tag, attrs = {}, kids = []) {
    Object.assign(this, {tagName: tag.toUpperCase(), attrs: {}, children: [], parentNode: null, on: {}, hidden: false, checked: false,
                         disabled: false, value: '', indeterminate: false, width: 0, height: 0, text: ''});
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
  appendChild(k) { if (k.parentNode) k.parentNode.children.splice(k.parentNode.children.indexOf(k), 1); k.parentNode = this; this.children.push(k); mutated(this); return k; }
  append(...ks) { ks.forEach(k => this.appendChild(k)); }
  replaceChildren(...ks) { this.children = []; this.append(...ks); }
  before(k) { const p = this.parentNode; if (k.parentNode) k.parentNode.children.splice(k.parentNode.children.indexOf(k), 1); p.children.splice(p.children.indexOf(this), 0, k); k.parentNode = p; mutated(p); }
  replaceWith(k) { this.before(k); this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; }
  cloneNode(deep) { return Object.assign(new El(this.tagName.toLowerCase(), this.attrs, deep ? this.children.map(k => k.cloneNode(true)) : []), {width: this.width, height: this.height, y: this.y, text: this.text}); }
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
  // laid out at `y` on the page, scrolled by scrollY unless `fixed`
  getBoundingClientRect() {
    const off = this.style.display === 'none', top = (this.y || 0) - (this.fixed ? 0 : scrollY);
    return {width: off ? 0 : this.width, height: off ? 0 : this.height, top, bottom: top + (off ? 0 : this.height), left: 0};
  }
  get offsetHeight() { return this.rows.reduce((n, r) => n + r.height, 0); }
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
document.createElement = tag => new El(tag);
global.window = global;
const on = {};
global.addEventListener = (t, f) => (on[t] ||= []).push(f);
global.scrollY = 0;
global.scrolled = y => { scrollY = y; (on.scroll || []).forEach(f => f()); };
global.CSS = {escape: s => s};
global.localStorage = {getItem: () => null, setItem() {}};
const observers = [];
global.ResizeObserver = class { constructor(f) { this.f = f; observers.push(this); } observe() {} };
// an observer is called once the script's turn ends, as a page's is; takeRecords drops what was due
const watched = [];
global.MutationObserver = class { constructor(f) { this.f = f; } observe(target) { watched.push([target, this]); } takeRecords() { this.due = false; return []; } };
const mutated = node => watched.forEach(([target, o]) => {
  if (!target.contains(node) || o.due) return;
  o.due = true;
  queueMicrotask(() => { if (o.due) { o.due = false; o.f(); } });
});
global.tick = () => new Promise(r => setTimeout(r));
global.media = {matches: true, on: [], addEventListener(t, f) { this.on.push(f); }};   // the window, 900px wide or more
global.matchMedia = () => media;
global.widen = on => { media.matches = on; media.on.forEach(f => f()); };
global.el = (tag, attrs, ...kids) => new El(tag, attrs, kids);
global.resized = () => observers.forEach(o => o.f());
require(process.argv[1]);
const T = window.NulliusTable;

// A column's choices as the controls macro writes them: `only` the value chosen at load, if any.
const fieldOf = ([col, values], only = {}) => el('fieldset', {'data-filter-menu': col}, el('span', {}, el('button', {'data-all': col}), el('button', {'data-none': col})),
  ...values.map(v => el('label', {}, el('input', {type: 'checkbox', 'data-value': v, ...(col in only && only[col] !== v ? {} : {checked: true})}), ' ' + v)));
const pillsOf = ([col, values], only = {}) => el('div', {'data-pills': col}, el('button', {class: 'filter-pill', 'data-all': col, 'aria-pressed': String(!(col in only))}),
  ...values.map(v => el('button', {class: 'filter-pill', 'data-value': v, 'aria-pressed': String(only[col] === v)}, v)));
const dropdownOf = (c, only) => el('div', {}, el('button', {'data-dropdown': ''}, c[0], el('span', {class: 'tools-count', hidden: true})), el('div', {class: 'menu', hidden: true}, fieldOf(c, only)));

// A table as the table macro writes it: `id`; columns [key, type]; rows, each {search, f-<col>, cells}; `filter`: a
// column and its values, or a list of them, in the Filter menu; `dropdowns` and `pills`: lists of them, in those forms;
// `only`: column -> its value chosen at load; `search`; `sort`; `noun`, what the live region counts.
global.build = ({id, cols, rows, filter, dropdowns = [], pills = [], only = {}, search = true, sort = true, check = false, noun = 'rows'}) => {
  const radio = (k, v) => el('label', {}, el('input', {type: 'radio', name: `${id}-sort-${k}`, [`data-sort-${k}`]: '', value: v}), ' ' + v);
  const button = !filter ? [] : typeof filter[0] === 'string' ? [filter] : filter;
  const tools = el('div', {id: id + '-tools'},
    ...(search ? [el('label', {}, el('input', {type: 'search', 'data-search': ''}))] : []),
    ...dropdowns.map(c => dropdownOf(c, only)),
    ...(button.length ? [el('button', {'data-tools-btn': 'filter'}, 'Filter', el('span', {class: 'tools-count', hidden: true})),
                         el('div', {'data-tools': 'filter', hidden: true}, el('button', {'data-clear': ''}), ...button.map(c => fieldOf(c, only)))] : []),
    ...(sort ? [el('button', {'data-tools-btn': 'sort'}),
                el('div', {'data-tools': 'sort', hidden: true}, el('fieldset', {}, radio('col', ''), ...cols.map(([k]) => radio('col', k))),
                   el('fieldset', {}, radio('dir', 'asc'), radio('dir', 'desc')))] : []),
    ...pills.map(c => pillsOf(c, only)));
  const head = el('tr', {}, ...(check ? [el('th', {}, el('input', {type: 'checkbox', 'data-check-all': ''}))] : []),
                  ...cols.map(([k, type]) => el('th', {'data-col': k, 'data-type': type}, k)));
  const body = el('tbody', {}, ...rows.map(r => el('tr', Object.fromEntries(Object.entries(r).filter(([k]) => k !== 'cells').map(([k, v]) => [k === 'search' ? 'data-search' : 'data-' + k, v])),
                  ...(check ? [el('td', {}, el('input', {type: 'checkbox', 'data-check': ''}))] : []), ...r.cells.map(c => el('td', {}, String(c))))));
  const table = el('table', {id, class: 'grid', 'data-table': ''}, el('thead', {}, head), body);
  const per = el('select', {'data-per': ''}, el('option', {}, '10'), el('option', {}, '25'));
  per.value = '25';
  const pager = el('div', {'data-pager': id}, per, el('button', {'data-prev': ''}), el('span', {'data-note': ''}), el('button', {'data-next': ''}));
  pager.appendChild(el('span', {'data-live': '', 'data-noun': noun}));
  const page = el('main', {}, tools, table, el('p', {'data-empty': id, hidden: true}, 'No rows match.'), pager);
  document.appendChild(page);
  T.wire(table);
  const shown = () => table.tBodies[0].rows.filter(r => !r.hidden).map(r => r.cells[check ? 1 : 0].textContent);
  const type = q => { const box = tools.querySelector('[data-search]'); box.value = q; box.dispatch('input'); };
  return {table, tools, page, per, pager, shown, type, note: () => pager.querySelector('[data-note]').textContent,
          empty: () => !page.querySelector('[data-empty]').hidden, live: () => pager.querySelector('[data-live]').textContent};
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


LAUNCHES = r'''
const rows = Array.from({length: 30}, (_, i) => ({search: `L${i} ${i % 2 ? 'Gamma' : 'delta'} agent`, 'f-state': i % 3 ? 'finished' : 'failed', cells: [`L${i}`]}));
const t = build({id: 'launches', cols: [['launch', 'text']], rows, filter: ['state', ['failed', 'finished']], noun: 'launches'});
'''


class Search(unittest.TestCase):
    def test_search_with_filters_and_pages(self):
        got = node(LAUNCHES + r'''
const out = {start: t.note()};
t.type('GAMMA'); out.typed = [t.note(), t.shown().length];                                // any case: 15 odd rows
t.per.value = '10'; t.per.dispatch('change'); out.per = t.note();
t.pager.querySelector('[data-next]').click(); out.next = [t.note(), t.shown()];
t.type('gamma agent'); out.again = t.note();                                              // a new query: page 1
t.tools.querySelector('[data-value="finished"]').click(); out.filtered = [t.note(), t.shown()];
t.tools.querySelector('[data-search]').dispatch('keydown', {key: 'Escape'});              // Escape clears the query, not the filter
out.cleared = [t.tools.querySelector('[data-search]').value, t.note()];
t.type('zzz'); out.none = [t.note(), t.shown(), t.empty()];
t.type(''); out.back = [t.note(), t.empty()];
t.type('AGENT l1'); out.words = t.shown();                                                // every word, in any order
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got["start"], "1–25 of 30")
        self.assertEqual(got["typed"], ["1–15 of 15", 15])
        self.assertEqual(got["per"], "1–10 of 15")
        self.assertEqual(got["next"], ["11–15 of 15", ["L21", "L23", "L25", "L27", "L29"]])
        self.assertEqual(got["again"], "1–10 of 15")
        self.assertEqual(got["filtered"], ["1–5 of 5", ["L3", "L9", "L15", "L21", "L27"]])   # only the failed ones left
        self.assertEqual(got["cleared"], ["", "1–10 of 10"])
        self.assertEqual(got["none"], ["", [], True])
        self.assertEqual(got["back"], ["1–10 of 10", False])
        self.assertEqual(got["words"], ["L12", "L15", "L18"])                                       # the failed ones, still filtered

    def test_a_screen_reader_hears_a_change_once_typing_pauses(self):
        got = node(LAUNCHES + r'''
const out = [];
setTimeout(() => {
  out.push(t.live());                                                                     // the page's load: nothing
  t.type('g'); t.type('ga'); t.type('gamma'); out.push(t.live());                         // still typing: nothing new
  setTimeout(() => { out.push(t.live()); t.type('zzz'); setTimeout(() => { out.push(t.live()); process.stdout.write(JSON.stringify(out)); }, 350); }, 350);
}, 350);
''')
        self.assertEqual(got, ["", "", "1–15 of 15 launches", "No rows match."])
        html = Macros().render("{% call(r, c) table('t', [dict(key='x', label='X')], [], none) %}{% endcall %}")
        self.assertEqual(html.count("aria-live"), 1)                                             # one live region a table
        self.assertIn('data-noun="rows"', html)
        self.assertNotIn('role="status"', html)


# Thirty rows of a state and a kind; the kind in pills, a dropdown and the Filter menu, the state in the Filter menu.
FORMS = r'''
const kinds = ['a', 'b', 'c'];
const rows = Array.from({length: 30}, (_, i) => ({search: `L${i}`, 'f-state': i % 2 ? 'finished' : 'failed', 'f-kind': kinds[i % 3], cells: [`L${i}`]}));
const make = (only = {}) => build({id: 't', cols: [['launch', 'text']], rows, only,
                                    filter: [['state', ['failed', 'finished']], ['kind', kinds]], dropdowns: [['kind', kinds]], pills: [['kind', kinds]]});
// each form's view of the kind: the pills pressed, the dropdown's and the Filter menu's boxes ticked; the dropdown's
// count, the Filter's; the pager's note
const look = t => {
  const row = t.tools.querySelector('[data-pills]'), [drop, menu] = t.tools.querySelectorAll('[data-filter-menu="kind"]');
  const ticked = v => v.querySelectorAll('input').filter(b => b.checked).map(b => b.dataset.value).join('');
  const count = b => b.querySelector('.tools-count').hidden ? '' : b.querySelector('.tools-count').textContent;
  return [row.querySelectorAll('button').filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.value ?? 'All').join(''),
          ticked(drop), ticked(menu), count(t.tools.querySelector('[data-dropdown]')), count(t.tools.querySelector('[data-tools-btn="filter"]')), t.note()];
};
const pill = (t, v) => t.tools.querySelector('[data-pills]').querySelector(v ? `[data-value="${v}"]` : '[data-all]').click();
const drop = t => t.tools.querySelector('[data-dropdown]').parentNode.querySelector('.menu');
'''


class FilterForms(unittest.TestCase):
    """Pills, a dropdown and the Filter menu: three views of one choice per column."""

    def test_the_forms_stay_in_step(self):
        got = node(FORMS + r'''
const t = make(), out = {start: look(t)};
t.per.value = '10'; t.per.dispatch('change'); t.pager.querySelector('[data-next]').click(); out.page2 = t.note();
pill(t, 'a'); out.a = [look(t), t.shown()[0]];                                            // back to the first page
pill(t, 'b'); out.ab = look(t);                                                           // pills add up
drop(t).querySelector('[data-value="a"]').click(); out.dropped = look(t);
pill(t, 'b'); out.none = look(t);                                                         // the last one off: All
pill(t, 'a'); pill(t, 'b'); pill(t, 'c'); out.every = look(t);                            // every one on: All
pill(t, 'c'); t.tools.querySelector('[data-tools="filter"]').querySelector('[data-value="finished"]').click(); out.two = look(t);
drop(t).querySelector('[data-all]').click(); out.all = look(t);
t.tools.querySelector('[data-tools="filter"]').querySelector('[data-none="kind"]').click(); out.nothing = [look(t), t.empty()];
pill(t, 'c'); t.tools.querySelector('[data-clear]').click(); out.clear = look(t);       // every column
pill(t, 'b'); pill(t, null); out.pillAll = look(t);
const dd = t.tools.querySelector('[data-dropdown]');
dd.click(); out.open = [drop(t).hidden, dd.getAttribute('aria-expanded'), document.activeElement === drop(t).querySelector('button')];
document.dispatch('keydown', {key: 'Escape'}); out.closed = [drop(t).hidden, document.activeElement === dd];
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got["start"], ["All", "abc", "abc", "", "", "1–25 of 30"])
        self.assertEqual(got["page2"], "11–20 of 30")
        self.assertEqual(got["a"], [["a", "a", "a", "1", "1", "1–10 of 10"], "L0"])
        self.assertEqual(got["ab"], ["ab", "ab", "ab", "2", "1", "1–10 of 20"])
        self.assertEqual(got["dropped"], ["b", "b", "b", "1", "1", "1–10 of 10"])
        self.assertEqual(got["none"], ["All", "abc", "abc", "", "", "1–10 of 30"])
        self.assertEqual(got["every"], got["none"])
        self.assertEqual(got["two"], ["c", "c", "c", "1", "2", "1–5 of 5"])                   # the Filter counts its columns
        self.assertEqual(got["all"], ["All", "abc", "abc", "", "1", "1–10 of 15"])
        self.assertEqual(got["nothing"], [["", "", "", "0", "2", ""], True])                  # None: no pill pressed, nothing shows
        self.assertEqual(got["clear"], ["All", "abc", "abc", "", "", "1–10 of 30"])
        self.assertEqual(got["pillAll"], got["clear"])
        self.assertEqual(got["open"], [False, "true", True])
        self.assertEqual(got["closed"], [True, True])

    def test_a_count_shows_on_a_narrowed_button_only(self):
        got = node(FORMS + r'''
const t = make(), dd = t.tools.querySelector('[data-dropdown]'), fb = t.tools.querySelector('[data-tools-btn="filter"]');
const mark = b => [b.classList.contains('narrowed'), b.querySelector('.tools-count').hidden ? null : b.querySelector('.tools-count').textContent];
const out = [[mark(dd), mark(fb)]];
drop(t).querySelector('[data-none]').click(); out.push([mark(dd), mark(fb)]);              // None: 0 chosen, narrowed
drop(t).querySelector('[data-all]').click(); out.push([mark(dd), mark(fb)]);
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [[[False, None], [False, None]], [[True, "0"], [True, "1"]], [[False, None], [False, None]]])

    def test_a_preset_shows_in_every_form(self):
        got = node(FORMS + r'''
const t = make({kind: 'b'}), out = [look(t)];
pill(t, null); out.push(look(t));
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [["b", "b", "b", "1", "1", "1–10 of 10"], ["All", "abc", "abc", "", "", "1–25 of 30"]])


class Checks(unittest.TestCase):
    def test_the_header_follows_the_visible_rows(self):
        got = node(r'''
const rows = ['a', 'b', 'c'].map(x => ({search: x, cells: [x]}));
const t = build({id: 't', cols: [['x', 'text']], rows, check: true, filter: null});
const all = t.table.querySelector('[data-check-all]'), boxes = t.table.querySelectorAll('[data-check]');
const state = () => [all.checked, all.indeterminate, boxes.map(b => b.checked)];
const out = [state()];
all.click(); out.push(state());                                     // every row ticked
boxes[1].click(); out.push(state());                                // some: the header is mixed
t.type('a'); out.push(state());                                     // only a shows, ticked: all
all.click(); out.push(state());                                     // unticks a alone
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [[False, False, [False, False, False]], [True, False, [True, True, True]], [False, True, [True, False, True]],
                               [True, False, [True, False, True]], [False, False, [False, False, True]]])


class Sticky(unittest.TestCase):
    def test_each_offset_is_the_widths_before_it(self):
        got = node(r"""(async () => {
const sticky = (k, w) => Object.assign(el('th', {class: 'sticky-col', 'data-col': k}), {width: w});
const head = el('tr', {}, sticky('a', 35.2), sticky('b', 120), el('th', {}), sticky('d', 50));
head.cells[3].classList.add('sticky-edge');
const row = () => el('tr', {}, el('td', {'data-col': 'a'}), el('td', {'data-col': 'b'}), el('td', {}), el('td', {'data-col': 'd'}));   // a page writes its cells plain
const first = row(), body = el('tbody', {}, first);
const table = el('table', {}, el('thead', {}, head), body);
const menu = el('div', {}, el('label', {}, el('input', {type: 'checkbox', 'data-col': 'b'})));
T.sticky(table);
T.columns(table, menu, 'k');
const at = r => r.cells.map(c => c.style['--sticky-left'] ?? null);
const out = [at(head), at(first), first.cells.map(c => ['sticky-col', 'sticky-edge'].filter(k => c.classList.contains(k)))];
head.cells[0].width = 40; resized(); out.push(at(first));                       // a column grows: those after it move
const added = body.appendChild(row()); await tick(); out.push(at(added));       // a row comes in: placed as it lands
body.appendChild(el('tr', {}, el('td', {colspan: 4}))); await tick();           // a row with fewer cells: left alone, no throw
menu.querySelector('input').click(); resized(); out.push(at(added));            // b hidden: d moves up to it
process.stdout.write(JSON.stringify(out));
})()""")
        self.assertEqual(got, [["0px", "35.2px", None, "155.2px"], ["0px", "35.2px", None, "155.2px"],
                               [["sticky-col"], ["sticky-col"], [], ["sticky-col", "sticky-edge"]],
                               ["0px", "40px", None, "160px"], ["0px", "40px", None, "160px"], ["0px", "40px", None, "40px"]])

    def test_the_rows_float_under_the_controls_until_the_table_ends(self):
        got = node(r"""(async () => {
const row = (y, h, sticky, ...cells) => Object.assign(el('tr', sticky === undefined ? {} : {'data-sticky': sticky}, ...cells.map(c => el('td', {}, c))), {y, height: h});
const head = Object.assign(el('tr', {'data-sticky': ''}, Object.assign(el('th', {class: 'sticky-col sticky-edge'}), {width: 80}), Object.assign(el('th', {}), {width: 200})), {y: 100, height: 30});
const ws = row(130, 40, 'wide', 'w', 'x'), run = row(170, 100, 'wide', 'r', 'x'), agent = row(270, 25, '', 'a', 'x'), model = row(295, 20, 'wide', 'm', 'x'), rest = row(315, 50, undefined, 'd', 'x');
const pick = run.cells[1].appendChild(el('button', {id: 'pick'}));
const body = el('tbody', {}, ws, run, agent, model, rest);
const table = Object.assign(el('table', {id: 'side', class: 'grid'}, el('thead', {}, head), body), {y: 100, height: 265, width: 280});
const tools = Object.assign(el('div', {id: 'side-tools'}), {height: 48, fixed: true}), wrap = Object.assign(el('div', {class: 'table-wrap'}, table), {clientWidth: 250, clientLeft: 0});
document.appendChild(el('main', {}, tools, wrap));
T.sticky(table);
const box = wrap.previousElementSibling, inner = box.querySelector('table');
const look = () => [box.hidden, inner.rows.map(r => r.cells[0].textContent || 'h'), box.style.top ?? null];
const out = [tools.classList.contains('sticky-tools'), look()];                // at the top: in the table
scrolled(60); out.push(look());                                                 // the header passes the controls: they float
const ghost = table.rows[3];
out.push([ghost.classList.contains('sticky-ghost'), ghost.getAttribute('aria-hidden'), ghost.inert, ghost.hasAttribute('data-sticky'),
          table.querySelectorAll('[id]').length, inner.getAttribute('id'), box.contains(pick), box.querySelector('colgroup').children.map(c => c.style.width)]);
wrap.scrollLeft = 30; wrap.dispatch('scroll'); out.push(box.scrollLeft);       // sideways with the table
box.scrollLeft = 10; box.dispatch('scroll'); out.push(wrap.scrollLeft);        // and the table with it
body.appendChild(row(365, 10)); await tick(); out.push(box.contains(pick));     // a row comes in: they stay up
widen(false); out.push(look());                                                 // narrow: the header and the Agent row alone
widen(true); scrolled(120); out.push(look());                                   // the table's end pushes them up
scrolled(320); out.push(look(), table.rows.map(r => r.height).slice(0, 6), body.contains(pick));   // past it: back in place
scrolled(0); out.push(look());
process.stdout.write(JSON.stringify(out));
})()""")
        self.assertEqual(got, [True, [True, [], None],
                               [False, ["h", "w", "r", "a", "m"], "48px"],
                               [True, "true", True, False, 0, None, True, ["80px", "200px"]],
                               30, 10, True,
                               [False, ["h", "a"], "48px"],
                               [False, ["h", "w", "r", "a", "m"], "30px"],
                               [True, [], "30px"], [30, 40, 100, 25, 20, 50], True,
                               [True, [], "30px"]])

    def test_a_table_without_sticky_rows_is_left_as_it_is(self):
        got = node(r"""
const head = el('tr', {}, Object.assign(el('th', {class: 'sticky-col'}), {width: 50}), el('th', {}));
const first = el('tr', {}, el('td', {}), el('td', {}));
const table = el('table', {id: 't'}, el('thead', {}, head), el('tbody', {}, first));
const tools = el('div', {id: 't-tools'}), wrap = el('div', {class: 'table-wrap'}, table), main = el('main', {}, tools, wrap);
document.appendChild(main);
T.sticky(table);
process.stdout.write(JSON.stringify([main.children.length, tools.classList.contains('sticky-tools')]));
""")
        self.assertEqual(got, [2, False])

class Menus(unittest.TestCase):
    def test_a_label_click_toggles_and_keeps_its_menu_open(self):
        got = node(LAUNCHES + r'''
const cols = el('div', {'data-tools': 'columns', hidden: true}, el('label', {}, el('input', {type: 'checkbox', 'data-col': 'launch', checked: true}), ' Launch'));
const cbtn = el('button', {});
t.page.appendChild(el('div', {}, cbtn, cols));
T.menu(cbtn, cols);
const out = {};
const pair = k => [t.tools.querySelector(`[data-tools-btn="${k}"]`), t.tools.querySelector(`[data-tools="${k}"]`)];
for (const [name, btn, menu] of [['filter', ...pair('filter')], ['sort', ...pair('sort')], ['columns', cbtn, cols]]) {
  btn.click();
  // a box, or a radio not chosen yet
  const input = menu.querySelectorAll('input').find(i => !i.disabled && !(i.getAttribute('type') === 'radio' && i.checked)), was = input.checked;
  // a press on the label's text moves the focus to the page's <main>, then the label clicks its input
  (document.activeElement || btn).dispatch('focusout', {relatedTarget: t.page});
  input.click();
  const kept = !menu.hidden;
  (document.activeElement).dispatch('focusout', {relatedTarget: el('a', {})});            // Tab out of it: closed
  out[name] = [was !== input.checked, kept, menu.hidden];
}
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, {"filter": [True, True, True], "sort": [True, True, True], "columns": [True, True, True]})

    def test_two_toolbars_on_one_page_sort_their_own_table(self):
        got = node(r'''
const make = id => build({id, cols: [['n', 'num']], rows: [1, 3, 2].map(n => ({search: '', cells: [n]})), filter: null});
const a = make('a'), b = make('b');
a.tools.querySelector('[data-sort-col][value="n"]').click();
b.type('');
process.stdout.write(JSON.stringify([a.shown(), b.shown(), b.tools.querySelector('[data-sort-col]:checked').value]));
''')
        self.assertEqual(got, [["3", "2", "1"], ["1", "3", "2"], ""])


class Macros(unittest.TestCase):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP / "templates"), autoescape=True)
    # a row's attributes, and its cells: each column's key and the row's value
    PAGE = ("{% macro tr(r) %} data-search=\"{{ r.name }}\"{% endmacro %}"
            "{% call(r, c) table('t', cols, rows, tr, columns=true) %}{% if c.check %}{{ check_cell('Select ' ~ r.name, r.name) }}"
            "{% else %}{% call cell(c, sort=r.name if c.key == 'a' else none) %}{{ c.key }}={{ r.name }}{% endcall %}{% endif %}{% endcall %}")

    def render(self, source, **data):
        return self.env.from_string("{% from '_tables.html' import table, cell, check_cell %}" + source).render(**data)

    def page(self, cols, rows=({"name": "x"},)):
        return re.sub(r">\s+<", "><", self.render(self.PAGE, cols=cols, rows=list(rows)))

    def test_two_tables_on_one_page_name_their_own_radios(self):
        html = self.render("{% for id in ['a', 'b'] %}{% call(r, c) table(id, [dict(key='x', label='X')], [], none, search='Find') %}{% endcall %}{% endfor %}")
        names = re.findall(r'type="radio" name="([\w-]+)"', html)
        self.assertEqual(sorted(set(names)), ["a-sort-col", "a-sort-dir", "b-sort-col", "b-sort-dir"])
        ids = re.findall(r' id="([\w-]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_named_column_heads_sorts_and_lists(self):
        html = self.page([dict(key="pick", label="Select all", check=True), dict(key="a", label="A", sticky=True, menu=False),
                          dict(key="b", label="B", sticky=True, type="num", off=True), dict(key="c", label="C", align="center"),
                          dict(key="acts", label="Actions", blank=True)])
        self.assertIn('<tr><th data-col="pick" scope="col" class="pick-col"><input type="checkbox" data-check-all aria-label="Select all" title="Select all"></th>'
                      '<th data-col="a" data-type="text" class="sticky-col">A</th><th data-col="b" data-type="num" class="align-right sticky-col sticky-edge">B</th>'
                      '<th data-col="c" data-type="text" class="align-center">C</th><th data-col="acts" scope="col"><span class="sr">Actions</span></th></tr>', html)
        self.assertEqual(re.findall(r'data-sort-col value="(\w*)"', html), ["", "a", "b", "c"])        # the check and the actions do not sort
        self.assertEqual(re.findall(r'data-col="(\w+)"( data-off)?> ', html), [("b", " data-off"), ("c", "")])   # a is not in the menu
        self.assertIn('<tr data-search="x"><td class="pick-col"><input type="checkbox" data-check value="x" aria-label="Select x"></td>'
                      '<td data-col="a" data-sort="x" class="sticky-col">a=x</td><td data-col="b" class="align-right sticky-col sticky-edge">b=x</td>'
                      '<td data-col="c" class="align-center">c=x</td><td data-col="acts">acts=x</td></tr>', html)   # each cell its column's
        self.assertIn('<p class="empty-note" data-empty="t" hidden>No rows match.</p>', html)

    def test_a_sticky_head_pins_its_row_and_holds_its_controls_to_the_table(self):
        source = "{% call(r, c) table('t', [dict(key='a', label='A')], [], none, sticky_head=on) %}{% endcall %}"
        plain, pinned = (re.sub(r">\s+<", "><", self.render(source, on=on)).strip() for on in (False, True))
        self.assertEqual(pinned.replace("<tr data-sticky>", "<tr>"), "<div>" + plain + "</div>")
        self.assertIn("<tr data-sticky>", pinned)

    def test_a_card_only_field_stays_off_the_table(self):
        html = self.page([dict(key="a", label="A", card="title"), dict(key="logo", label="Logo", card="icon", table=False)])
        self.assertNotIn('data-col="logo"', html)
        self.assertNotIn('value="logo"', html)
        self.assertIn('<td data-col="a" data-sort="x">a=x</td></tr>', html)

    def test_the_cells_follow_the_columns(self):
        cols = [dict(key=k, label=k.upper()) for k in "abc"]
        for order in (cols, cols[::-1]):
            with self.subTest(order=[c["key"] for c in order]):
                html = self.page(order)
                self.assertEqual(re.findall(r'<td data-col="(\w)"[^>]*>(\w)=x<', html), [(c["key"], c["key"]) for c in order])

    SLOTS = ("{% macro two(a, b) %}<button>{{ a }}</button><button>{{ b }}</button>{% endmacro %}"
             "{% macro views() %}{{ two('v1', 'v2') }}{% endmacro %}{% macro actions() %}{{ two('a1', 'a2') }}{% endmacro %}"
             "{% macro nothing() %} {% endmacro %}")

    def slotted(self, call):
        return re.sub(r">\s+<", "><", self.render("{% from '_tables.html' import controls %}" + self.SLOTS + call))

    def test_a_controls_row_places_its_slots_around_the_buttons(self):
        cols = "[dict(key='x', label='X')]"
        for call in (f"{{{{ controls('t', {cols}, search='Find', columns=true, choices={{'x': [('a', 'A')]}}, views=views, actions=actions) }}}}",
                     f"{{% call(r, c) table('t', {cols}, [], none, search='Find', columns=true, choices={{'x': [('a', 'A')]}}, views=views, actions=actions) %}}{{% endcall %}}"):
            with self.subTest(call=call[:12]):
                html = self.slotted(call)
                self.assertEqual(re.findall(r'data-search|data-slot="(\w+)"|data-tools-btn="(\w+)"|<button>(\w+)</button>', html),
                                 [("", "", ""), ("views", "", ""), ("", "", "v1"), ("", "", "v2"), ("", "columns", ""), ("", "filter", ""),
                                  ("", "sort", ""), ("actions", "", ""), ("", "", "a1"), ("", "", "a2")])
                self.assertEqual(html.count('class="table-slot"'), 2)                  # each slot one group, spaced as the buttons
                self.assertIn('<div class="table-buttons"><div class="table-slot" data-slot="views">', html)
        for call in ("{{ controls('t', [dict(key='x', label='X')]) }}", "{{ controls('t', [dict(key='x', label='X')], views=nothing, actions=nothing) }}"):
            self.assertNotIn("data-slot", self.slotted(call))                         # none asked, or one writing nothing: no group
        css = (APP / "static" / "style.css").read_text()
        self.assertIn(".table-buttons, .table-slot { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: .75rem; }", css)

    def controls(self, cols, **kw):
        return re.sub(r">\s+<", "><", self.env.from_string("{% from '_tables.html' import controls %}{{ controls('t', cols, sort=false, **kw) }}")
                      .render(cols=cols, kw=kw))

    def test_each_column_in_the_forms_it_names(self):
        cols = [dict(key="a", label="A"), dict(key="b", label="B", filter="dropdown"), dict(key="c", label="C", filter="pills"),
                dict(key="d", label="D", filter=["pills", "button"])]
        choices = {k: [("x", "X"), ("y", "Y")] for k in "abcd"}
        html = self.controls(cols, choices=choices)
        menu = html.split('data-tools="filter"')[1].split("</fieldset></div>")[0]
        self.assertEqual(re.findall(r'data-filter-menu="(\w)"', menu), ["a", "d"])                    # the Filter menu: a, by default, and d
        self.assertEqual(re.findall(r"<legend>(\w)</legend>", menu), ["A", "D"])                         # its groups keep their legends
        self.assertIn('<div class="menu-wrap"><button class="btn ghost sm" type="button" data-dropdown aria-haspopup="true" aria-expanded="false">'
                      'B<span class="tools-count" hidden></span><svg class="ic dd-chev" aria-hidden="true"><use href="#i-chev"/></svg></button><div class="menu tools-menu" hidden><fieldset data-filter-menu="b" aria-label="B"><span class="tools-all">', html)   # no legend: the button names it
        self.assertLess(html.index("data-dropdown"), html.index('data-tools-btn="filter"'))      # the dropdowns before the Filter
        self.assertEqual(html.count("dd-chev"), 1)                                                  # a chevron on the dropdown only
        self.assertNotIn("dd-chev", html.split('data-tools-btn="filter"')[1])
        self.assertEqual(html.count("<svg"), 2)                                                     # no filter_icon, no icon: the chevron and the Filter's
        self.assertEqual(re.findall(r'data-pills="(\w)"', html), ["c", "d"])
        self.assertIn('<div class="filter-pills" role="group" aria-label="C" data-pills="c"><button type="button" class="filter-pill" data-all="c" aria-pressed="true">All</button>'
                      '<button type="button" class="filter-pill" data-value="x" aria-pressed="false">X</button>'
                      '<button type="button" class="filter-pill" data-value="y" aria-pressed="false">Y</button></div>', html)
        self.assertLess(html.index('class="table-buttons"'), html.index("data-pills"))            # the pills under the buttons
        only = self.controls(cols, choices=choices, only={"c": "y", "b": "y"})
        self.assertIn('data-all="c" aria-pressed="false">All</button><button type="button" class="filter-pill" data-value="x" aria-pressed="false">X</button>'
                      '<button type="button" class="filter-pill" data-value="y" aria-pressed="true">Y</button>', only)
        self.assertIn('<input type="checkbox" data-value="x"> X</label><label><input type="checkbox" data-value="y" checked> Y', only.split('data-filter-menu="b"')[1])

    def test_a_filter_icon_leads_its_dropdown_and_its_pills(self):
        cols = [dict(key="b", label="B", filter="dropdown", filter_icon="origin"), dict(key="c", label="C", filter="pills", filter_icon="runner")]
        html = self.controls(cols, choices={k: [("x", "X")] for k in "bc"})
        self.assertIn('aria-expanded="false"><svg class="ic" aria-hidden="true"><use href="#i-origin"/></svg>B<span class="tools-count"', html)
        self.assertIn('<div class="filter-pills" role="group" aria-label="C" data-pills="c"><svg class="ic" aria-hidden="true"><use href="#i-runner"/></svg>'
                      '<button type="button" class="filter-pill" data-all="c"', html)
        bad = self.controls([dict(key="c", label="C", filter=["pills", "dropdown"], filter_icon='x"><b>')], choices={"c": [("x", "X")]})
        self.assertNotIn("<b>", bad)
        self.assertEqual(bad.count('<use href="#i-x&#34;&gt;&lt;b&gt;"/>'), 2)

    def test_without_a_filter_button_there_is_none(self):
        cols = [dict(key="c", label="C", filter="pills")]
        html = self.controls(cols, choices={"c": [("x", "X")]})
        self.assertNotIn("data-tools-btn", html)
        self.assertNotIn("table-buttons", html)                                                        # nothing to hold
        self.assertNotIn("data-filter-menu", html)
        with self.assertRaises(jinja2.UndefinedError):                                                 # a form it does not know
            self.controls([dict(key="c", label="C", filter="chips")], choices={"c": [("x", "X")]})

    def test_the_forms_escape_names_and_values(self):
        bad = '<b>"x"</b>'
        html = self.controls([dict(key="c", label=bad, filter=["pills", "dropdown"])], choices={"c": [(bad, bad)]}, only={"c": bad})
        self.assertNotIn("<b>", html)
        self.assertIn('role="group" aria-label="&lt;b&gt;&#34;x&#34;&lt;/b&gt;"', html)
        self.assertIn('data-value="&lt;b&gt;&#34;x&#34;&lt;/b&gt;" aria-pressed="true">&lt;b&gt;&#34;x&#34;&lt;/b&gt;</button>', html)
        self.assertIn('aria-expanded="false">&lt;b&gt;&#34;x&#34;&lt;/b&gt;<span class="tools-count"', html)

    def test_names_and_values_are_escaped(self):
        html = self.page([dict(key="a", label='<b>"A"</b>', title="x<y"), dict(key="n", label="N", sr=True)], rows=[{"name": '<i>&"'}])
        self.assertIn('<th data-col="a" data-type="text" title="x&lt;y">&lt;b&gt;&#34;A&#34;&lt;/b&gt;</th>', html)
        self.assertIn('<td data-col="a" data-sort="&lt;i&gt;&amp;&#34;">a=&lt;i&gt;&amp;&#34;</td>', html)
        self.assertIn('data-search="&lt;i&gt;&amp;&#34;"', html)
        self.assertIn('<label><input type="radio" name="t-sort-col" data-sort-col value="a" data-type="text"> &lt;b&gt;&#34;A&#34;&lt;/b&gt;</label>', html)


if __name__ == "__main__":
    unittest.main()
