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
  appendChild(k) { if (k.parentNode) k.parentNode.children.splice(k.parentNode.children.indexOf(k), 1); k.parentNode = this; this.children.push(k); mutated(this); return k; }
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
  getBoundingClientRect() { return {width: this.style.display === 'none' ? 0 : this.width}; }
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
const watched = [];
global.MutationObserver = class { constructor(f) { this.f = f; } observe(target) { watched.push([target, this.f]); } };
const mutated = node => watched.forEach(([target, f]) => target.contains(node) && f());
global.el = (tag, attrs, ...kids) => new El(tag, attrs, kids);
global.resized = () => observers.forEach(o => o.f());
require(process.argv[1]);
const T = window.NulliusTable;

// A table as the table macro writes it: `id`; columns [key, type]; rows, each {search, f-<col>, cells}; `filter`: a
// column and its values; `search`; `sort`; `noun`, what the live region counts.
global.build = ({id, cols, rows, filter, search = true, sort = true, check = false, noun = 'rows'}) => {
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
        got = node(r'''
const sticky = (k, w) => Object.assign(el('th', {class: 'sticky-col', 'data-col': k}), {width: w});
const head = el('tr', {}, sticky('a', 35.2), sticky('b', 120), el('th', {}), sticky('d', 50));
head.cells[3].classList.add('sticky-edge');
const row = () => el('tr', {}, el('td', {'data-col': 'a'}), el('td', {'data-col': 'b'}), el('td', {}), el('td', {'data-col': 'd'}));   // a page writes its cells plain
const first = row(), body = el('tbody', {}, first);
const table = el('table', {}, el('thead', {}, head), body);
const menu = el('div', {}, el('label', {}, el('input', {type: 'checkbox', 'data-col': 'b'})));
T.sticky(table);
T.columns(table, null, menu, 'k');
const at = r => r.cells.map(c => c.style['--sticky-left'] ?? null);
const out = [at(head), at(first), first.cells.map(c => ['sticky-col', 'sticky-edge'].filter(k => c.classList.contains(k)))];
head.cells[0].width = 40; resized(); out.push(at(first));                       // a column grows: those after it move
const added = body.appendChild(row()); out.push(at(added));                     // a row comes in: placed as it lands
body.appendChild(el('tr', {}, el('td', {colspan: 4})));                         // a row with fewer cells: left alone, no throw
menu.querySelector('input').click(); resized(); out.push(at(added));            // b hidden: d moves up to it
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [["0px", "35.2px", None, "155.2px"], ["0px", "35.2px", None, "155.2px"],
                               [["sticky-col"], ["sticky-col"], [], ["sticky-col", "sticky-edge"]],
                               ["0px", "40px", None, "160px"], ["0px", "40px", None, "160px"], ["0px", "40px", None, "40px"]])


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

    def test_the_cells_follow_the_columns(self):
        cols = [dict(key=k, label=k.upper()) for k in "abc"]
        for order in (cols, cols[::-1]):
            with self.subTest(order=[c["key"] for c in order]):
                html = self.page(order)
                self.assertEqual(re.findall(r'<td data-col="(\w)"[^>]*>(\w)=x<', html), [(c["key"], c["key"]) for c in order])

    def test_names_and_values_are_escaped(self):
        html = self.page([dict(key="a", label='<b>"A"</b>', title="x<y"), dict(key="n", label="N", sr=True)], rows=[{"name": '<i>&"'}])
        self.assertIn('<th data-col="a" data-type="text" title="x&lt;y">&lt;b&gt;&#34;A&#34;&lt;/b&gt;</th>', html)
        self.assertIn('<td data-col="a" data-sort="&lt;i&gt;&amp;&#34;">a=&lt;i&gt;&amp;&#34;</td>', html)
        self.assertIn('data-search="&lt;i&gt;&amp;&#34;"', html)
        self.assertIn('<label><input type="radio" name="t-sort-col" data-sort-col value="a"> &lt;b&gt;&#34;A&#34;&lt;/b&gt;</label>', html)


if __name__ == "__main__":
    unittest.main()
