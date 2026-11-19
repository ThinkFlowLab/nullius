"""The card toolkit: its macros (templates/_cards.html) as a page renders them, and its script
(NulliusTable.cards in static/tables.js) run under Node on the table toolkit's stand-in DOM."""
import re
import unittest

import jinja2

from .test_tables import APP, node

# One field of each slot, given in an order unlike the card's.
COLS = [dict(key='stat1', label='Tasks', card='stat'), dict(key='alert', label='Blocker', card='alert'),
        dict(key='title', label='Name', card='title'), dict(key='act', label='Open', card='action'),
        dict(key='stat2', label='Scored by', card_label='Scorer', card='stat', **{'class': 'word'}),
        dict(key='note', label='Needs', card='note', icon='wrench'), dict(key='badge', label='Runs with', card='badge', **{'class': 'run-tag'}),
        dict(key='text', label='About', card='text', style='code'), dict(key='icon', label='Logo', card='icon', **{'class': 'bench-mark'}),
        dict(key='check', label='Pick', card='check'), dict(key='meta', label='Version', card='meta'),
        dict(key='tag', label='Kind', card='tag', **{'class': 'cat-tag'}), dict(key='status', label='State', card='status'),
        dict(key='hidden', label='Only in a table'), dict(key='del', label='Delete', card='delete')]
FULL = {'href': '/a', 'id': 'a', 'search': 'Alpha one', 'keys': 'a', 'stat1': {'text': 5, 'label': 'Tasks each'}, 'alert': 'Blocked',
        'title': 'Alpha', 'act': {'text': 'Open', 'href': '/a/open'}, 'stat2': 'tests', 'note': {'text': 'needs docker', 'title': 'Install docker'},
        'badge': [{'text': 'Nullius', 'class': 'rt-standard'}, 'Harbor'], 'text': 'a.port/v1', 'icon': '/m.svg#bm-a',
        'check': {'text': 'Pick Alpha', 'value': 'a'}, 'meta': 'v1', 'tag': 'coding', 'status': 'ready', 'hidden': 'never on a card',
        'del': {'title': 'Delete Alpha', 'class': 'x-del', 'attrs': {'data-ask': 'del-a', 'aria-label': 'Delete Alpha'}}}


class Macros(unittest.TestCase):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP / "templates"), autoescape=True)

    def render(self, cols, items, **kw):
        return re.sub(r">\s+<", "><", self.env.from_string("{% from '_cards.html' import cards %}{{ cards('c', cols, items, **kw) }}")
                      .render(cols=cols, items=items, kw=kw))

    def test_slots_come_in_their_order_and_a_field_without_one_stays_off(self):
        html = self.render(COLS, [FULL])
        order = ['href="/m.svg#bm-a"', 'class="card-link" href="/a">Alpha<', 'aria-label="Pick Alpha"', '<code>a.port/v1</code>', 'class="run-tag rt-standard">Nullius<', 'class="run-tag">Harbor<',
                 'title="Install docker"><svg class="ic" aria-hidden="true"><use href="#i-wrench"/></svg>needs docker<',
                 '<dl class="bench-stats"><div><dt>Tasks each</dt><dd>5</dd></div><div><dt>Scorer</dt><dd class="word">tests</dd></div></dl>',
                 'class="bench-card-blocker">Blocked<', '<a class="btn ghost sm" href="/a/open">Open</a>', '>ready<', 'class="cat-tag">coding<', 'class="bench-harness">v1<']
        at = [html.index(part) for part in order]
        self.assertEqual(at, sorted(at))
        self.assertNotIn('never on a card', html)
        self.assertIn('<article class="bench-card" data-card id="a" data-search="Alpha one" data-keys="a">', html)
        self.assertEqual(html.count('<a '), 2)                                               # one link to the card, and its action
        self.assertIn('href="/a">Alpha</a><span class="bench-card-corner"><input type="checkbox" data-check value="a" aria-label="Pick Alpha">'
                      '<button type="button" class="icon-btn danger x-del" title="Delete Alpha" data-ask="del-a" aria-label="Delete Alpha">'
                      '<svg class="ic" aria-hidden="true"><use href="#i-trash"/></svg></button></span></div>', html)   # the title row's right: check, then delete
        self.assertIn('<div class="bench-card-foot"><span>ready</span><span class="bench-card-tags"><span class="cat-tag">coding</span></span>'
                      '<span class="bench-harness">v1</span></div></article>', html)                                       # the last row

    def test_empty_slots_take_no_space(self):
        html = self.render(COLS, [{'href': '/b', 'title': 'Beta', 'stat1': 0, 'badge': [], 'tag': '', 'note': None}])
        for absent in ('bench-card-foot', 'bench-card-corner', 'data-check', 'icon-btn', 'bench-card-domain', 'bench-card-runs', 'bench-card-setup', 'bench-card-blocker', 'bench-card-actions'):
            self.assertNotIn(absent, html)
        self.assertIn('<dl class="bench-stats"><div><dt>Tasks</dt><dd>0</dd></div><div><dt>Scorer</dt><dd class="word">—</dd></div></dl>', html)   # a zero is a value; a missing one keeps its place
        self.assertNotIn('bench-stats', self.render(COLS[2:3], [{'href': '/b', 'title': 'Beta'}]))

    def test_one_title_at_most_one_text_and_any_number_of_stats(self):
        title, stat, text = COLS[2], COLS[0], COLS[7]
        for cols in ([stat], [title, title], [title, text, text], [title, COLS[8], COLS[8]]):
            with self.subTest(cols=[c['card'] for c in cols]), self.assertRaises(jinja2.UndefinedError):
                self.render(cols, [FULL])
        stats = [dict(key='s%d' % i, label='S%d' % i, card='stat') for i in range(4)]
        html = self.render([title] + stats, [{'title': 'A', 's0': 1, 's1': '', 's3': 4}, {'title': 'B', 's2': 3}])
        self.assertIn('<div class="bench-grid" style="--stats: 4">', html)                  # a column each, every card alike
        self.assertIn('<dl class="bench-stats"><div><dt>S0</dt><dd>1</dd></div><div><dt>S1</dt><dd>—</dd></div><div><dt>S2</dt><dd>—</dd></div><div><dt>S3</dt><dd>4</dd></div></dl>', html)
        self.assertIn('<dl class="bench-stats"><div><dt>S0</dt><dd>—</dd></div><div><dt>S1</dt><dd>—</dd></div><div><dt>S2</dt><dd>3</dd></div><div><dt>S3</dt><dd>—</dd></div></dl>', html)
        self.assertNotIn('--stats', self.render([title], [FULL]))

    def test_the_corner_takes_a_check_a_delete_or_both(self):
        title, check, delete = COLS[2], COLS[9], COLS[-1]
        item = {'href': '/a', 'title': 'A', 'check': {'text': 'Pick A', 'value': 'a'}, 'del': {'title': 'Delete A'}}
        box, bin_ = '<input type="checkbox" data-check value="a" aria-label="Pick A">', '<button type="button" class="icon-btn danger" title="Delete A">'
        for cols, inside in (([title, check], box + '</span>'), ([title, delete], '<span class="bench-card-corner">' + bin_), ([title, delete, check], box + bin_)):
            with self.subTest(slots=[c['card'] for c in cols]):
                html = self.render(cols, [item])
                self.assertIn(inside, html)
                self.assertEqual(html.count('bench-card-corner'), 1)
        with self.assertRaises(jinja2.UndefinedError):
            self.render([title, delete, delete], [item])                                   # one delete a card

    def test_initials_only_beside_a_real_icon(self):
        cols = [COLS[8], COLS[2]]
        html = self.render(cols, [{'href': '/a', 'title': 'Alpha', 'icon': '/logo.svg'}, {'href': '/b', 'title': 'beta'}])
        self.assertIn('<span class="bench-mark" aria-hidden="true"><img class="agent-logo" src="/logo.svg" alt=""></span>', html)
        self.assertIn('<span class="ws-avatar bench-mark" aria-hidden="true">BE</span>', html)
        html = self.render(cols, [{'href': '/a', 'title': 'Alpha'}, {'href': '/b', 'title': 'Beta', 'icon': None}])
        self.assertNotIn('aria-hidden="true"><', html.split('data-cards>')[1])              # no icon slot at all

    def test_values_are_escaped(self):
        bad = '<script>x</script>"'
        html = self.render(COLS, [dict(FULL, title=bad, text=bad, search=bad, tag={'text': bad, 'title': bad}, stat2=bad, href='/"x')],
                           choices={'tag': [(bad, bad)]})
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;x&lt;/script&gt;&#34;', html)
        self.assertIn('href="/&#34;x"', html)
        self.assertIn('data-f-tag="&lt;script&gt;x&lt;/script&gt;&#34;&#10;"', html)

    def test_a_filter_reads_a_value_not_its_label(self):
        cols = [COLS[2], dict(COLS[11], filter='dropdown')]
        html = self.render(cols, [{'href': '/a', 'title': 'A', 'tag': [{'text': 'Coding', 'value': 'code'}, 'math']}],
                           choices={'tag': [('code', 'Coding'), ('math', 'math')]})
        self.assertIn('data-f-tag="code&#10;math&#10;"', html)                                 # the value given, else the text
        self.assertIn('<span class="cat-tag">Coding</span>', html)
        self.assertIn('<input type="checkbox" data-value="code" checked> Coding', html)

    def test_controls_search_and_filter_without_sort(self):
        html = self.render(COLS[2:3], [{'href': '/a', 'title': 'A', 'tag': 'x'}], search='Find', choices={'title': [('A', 'A')]},
                           empty='No card matches.')
        self.assertIn('id="c-tools"', html)
        self.assertIn('data-search placeholder="Find"', html)
        self.assertIn('data-filter-menu="title"', html)
        self.assertIn('data-f-title="A&#10;"', html)
        self.assertNotIn('data-tools-btn="sort"', html)
        self.assertIn('<p class="empty-note" data-empty="c" hidden>No card matches.</p>', html)

    def test_two_collections_with_every_form_keep_their_ids_apart(self):
        cols = [dict(COLS[2]), dict(COLS[11], filter='pills'), dict(COLS[10], filter=['dropdown', 'button'])]
        page = self.env.from_string("{% from '_cards.html' import cards %}{% for id in ['p', 'q'] %}{{ cards(id, cols, items, search='Find', choices=choices) }}{% endfor %}")
        html = page.render(cols=cols, items=[{'href': '/x', 'title': 'X', 'tag': 'coding', 'meta': 'v1'}],
                           choices={'tag': [('coding', 'Coding')], 'meta': [('v1', 'v1')]})
        self.assertEqual(re.findall(r' id="([^"]+)"', html), ['p-tools', 'p', 'q-tools', 'q'])
        self.assertEqual(html.count('aria-live="polite"'), 2)                                 # one live region a collection
        self.assertIn('<span class="sr" data-live="q" data-noun="items" aria-live="polite"></span>', html)
        self.assertEqual(html.count('data-pills="tag"'), 2)
        self.assertEqual(html.count('data-dropdown'), 2)


class Script(unittest.TestCase):
    def test_search_and_filter_hide_cards_and_their_empty_groups(self):
        got = node(r'''
const card = (name, search, kind) => el('article', {'data-card': '', 'data-search': search, 'data-f-kind': kind + '\n'}, name);
const group = (state, ...cards) => el('section', {'data-group': ''}, el('h2', {}, state, el('span', {'data-group-n': ''}, String(cards.length))), ...cards);
const a = group('official', card('A', 'Alpha coding', 'coding'), card('B', 'Beta math', 'math'));
const b = group('community', card('C', 'Gamma coding', 'coding'));
const tools = el('div', {id: 'c-tools'}, el('label', {}, el('input', {type: 'search', 'data-search': ''})),
  el('button', {'data-tools-btn': 'filter'}, 'Filter', el('span', {class: 'tools-count', hidden: true})),
  el('div', {'data-tools': 'filter', hidden: true}, el('button', {'data-clear': ''}),
     el('fieldset', {'data-filter-menu': 'kind'}, ...['coding', 'math'].map(v => el('label', {}, el('input', {type: 'checkbox', 'data-value': v, checked: true}))))));
const root = el('div', {id: 'c', 'data-cards': ''}, a, b), empty = el('p', {'data-empty': 'c', hidden: true}, 'No card matches.');
document.appendChild(el('main', {}, tools, root, empty, el('span', {'data-live': 'c'})));
T.cards(root);
const shown = () => root.querySelectorAll('[data-card]').filter(c => !c.hidden).map(c => c.textContent);
const state = () => [shown(), [a, b].map(g => [g.hidden, g.querySelector('[data-group-n]').textContent]), empty.hidden];
const type = q => { const box = tools.querySelector('[data-search]'); box.value = q; box.dispatch('input'); };
const out = [state()];
type('CODING'); out.push(state());                                      // any case
type('coding gamma'); out.push(state());                                // every word, in any order: an emptied group hides
type('zzz'); out.push(state());                                         // nothing: the note
type(''); tools.querySelector('[data-value="coding"]').click(); out.push(state());   // the filter
tools.querySelector('[data-clear]').click(); out.push(state());
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got, [
            [["A", "B", "C"], [[False, "2"], [False, "1"]], True],
            [["A", "C"], [[False, "1"], [False, "1"]], True],
            [["C"], [[True, "0"], [False, "1"]], True],
            [[], [[True, "0"], [True, "0"]], False],
            [["B"], [[False, "1"], [True, "0"]], True],
            [["A", "B", "C"], [[False, "2"], [False, "1"]], True]])

    def test_a_screen_reader_hears_how_many_show_once_changes_pause(self):
        got = node(r'''
const card = (name, kind) => el('article', {'data-card': '', 'data-search': name, 'data-f-kind': kind + '\n'}, name);
const tools = el('div', {id: 'c-tools'}, el('label', {}, el('input', {type: 'search', 'data-search': ''})),
  el('div', {'data-pills': 'kind'}, el('button', {class: 'filter-pill', 'data-all': 'kind', 'aria-pressed': 'true'}),
     ...['x', 'y'].map(v => el('button', {class: 'filter-pill', 'data-value': v, 'aria-pressed': 'false'}))));
const root = el('div', {id: 'c', 'data-cards': ''}, card('A', 'x'), card('B', 'y'), card('C', 'x')), live = el('span', {'data-live': 'c', 'data-noun': 'cards'});
document.appendChild(el('main', {}, tools, root, el('p', {'data-empty': 'c', hidden: true}, 'No card matches.'), live));
T.cards(root);
const out = [];
const type = q => { const box = tools.querySelector('[data-search]'); box.value = q; box.dispatch('input'); };
setTimeout(() => {
  out.push(live.textContent);                                                            // the page's load: nothing
  tools.querySelector('[data-value="x"]').click(); type('a'); out.push(live.textContent);  // still changing: nothing new
  setTimeout(() => { out.push(live.textContent); type('zzz'); setTimeout(() => { out.push(live.textContent); process.stdout.write(JSON.stringify(out)); }, 350); }, 350);
}, 350);
''')
        self.assertEqual(got, ["", "", "1 of 3 cards", "No card matches."])

    def test_a_click_anywhere_on_a_card_but_a_control_opens_it(self):
        got = node(r'''
let selected = '';
global.getSelection = () => ({isCollapsed: !selected});
const opened = [], followed = [];
window.open = (...a) => opened.push(a);
const link = el('a', {class: 'card-link', href: '/a'}, 'A'), pill = el('span', {title: 'Runs with Harbor'}, 'Harbor');
const box = el('input', {type: 'checkbox'}), btn = el('button', {}), lab = el('label', {}, el('span', {}, 'x'));
link.addEventListener('click', () => followed.push('link'));
const root = el('div', {id: 'c', 'data-cards': ''}, el('article', {'data-card': ''}, el('div', {}, link), pill, box, btn, lab));
document.appendChild(el('main', {}, el('div', {id: 'c-tools'}), root, el('p', {'data-empty': 'c', hidden: true}), el('span', {'data-live': 'c', 'data-noun': 'cards'})));
link.href = '/a';
T.cards(root);
const click = (target, props = {}) => { followed.length = 0; opened.length = 0; target.dispatch(props.type || 'click', {button: 0, ...props}); return [followed.length, opened.map(a => a.join(' '))]; };
const out = {
  pill: click(pill),                                          // a tooltip's pill: the card opens
  ctrl: click(pill, {ctrlKey: true}), meta: click(pill, {metaKey: true}), shift: click(pill, {shiftKey: true}),
  middle: click(pill, {type: 'auxclick', button: 1}), right: click(pill, {type: 'auxclick', button: 2}),
  box: click(box), button: click(btn), label: click(lab.children[0]),   // a control of its own: left to it
};
selected = 'Harbor'; out.selecting = click(pill);               // the end of a text selection
process.stdout.write(JSON.stringify(out));
''')
        self.assertEqual(got["pill"], [1, []])
        for k in ("ctrl", "meta", "shift", "middle"):
            self.assertEqual(got[k], [0, ["/a _blank noopener"]], k)
        for k in ("right", "box", "button", "label", "selecting"):
            self.assertEqual(got[k], [0, []], k)


if __name__ == "__main__":
    unittest.main()
