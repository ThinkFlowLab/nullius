// The run page's board and trials. Every number and row is rendered by the server; this adds the filters (outcome, judge
// cause and status, flag - ANDed, carried in the query string so a link reproduces the view), the linked hover between the
// board and the rows, Tokens/Total sorting, Esc, the live count, and one tooltip for hover and keyboard focus alike.
(() => {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const page = $('#run-page'); if (!page) return;
  const board = $('#board'), tbody = $('#trial-rows'), empty = $('#no-trial-matches');
  const rows = $$('#trial-rows tr[data-outcome]'), N = rows.length;
  const count = $('#trial-count'), clear = $('#clear-filters'), liveEl = $('#trials-live'), heading = $('#trials-heading');
  const KEYS = ['o', 'c', 's', 'f'];
  const LABEL = {}; $$('.facet[data-k="o"] .fmenu [data-o]', page).forEach(a => { LABEL[a.dataset.o] = $('span', a).textContent; });
  const SL = {}; $$('[data-sl]', page).forEach(b => { SL[b.dataset.s] = b.dataset.sl; });
  const F = new Map(rows.map(r => [r, { o: r.dataset.outcome, c: r.dataset.cause, s: r.dataset.status, f: r.dataset.flags.split(' ').filter(Boolean), t: r.dataset.trial }]));
  // one predicate, both directions: a mark and a row match when every key the mark names agrees with the row
  const hits = (d, r) => { const x = F.get(r);
    return (!('o' in d) || x.o === d.o) && (!('c' in d) || x.c === d.c) && (!('s' in d) || x.s === d.s) && (!('f' in d) || x.f.includes(d.f)) && (!('t' in d) || x.t === d.t); };
  const fxs = $$('.fx', page), hls = board ? $$('.hl', board) : [];
  const q = new URLSearchParams(location.search);
  const st = { o: q.get('outcome') || '', c: q.get('cause') || '', s: q.get('status') || '', f: q.get('flag') || '' };

  // a facet's summary names its value; the ✕ beside it removes it. The cause facet also carries a status.
  const OWN = { o: ['o'], c: ['c', 's'], f: ['f'] };
  const facets = $$('.facet', page);
  facets.forEach(fc => $('.x', fc).addEventListener('click', () => { OWN[fc.dataset.k].forEach(k => { st[k] = ''; }); apply(); heading.focus(); }));
  const paint = fc => {
    const k = fc.dataset.k, on = !!st[k], v = $('.v', fc);
    fc.classList.toggle('set', on); $('.x', fc).hidden = !on; $('.car', fc).hidden = on; v.replaceChildren();
    if (on && k === 'o') { const i = document.createElement('i'); i.className = 'odot oc-' + st.o; v.append(i); }
    v.append(on ? (k === 'o' ? (LABEL[st.o] || st.o) : st[k]) : 'All');
    if (on && k === 'c' && st.s) { const x = document.createElement('span'); x.className = 'sr'; x.textContent = ' · ' + (SL[st.s] || st.s); v.append(x); }
  };
  const pressed = el => { const ks = KEYS.filter(k => k in el.dataset); return ks.length > 0 && ks.every(k => st[k] === el.dataset[k]) && (!ks.includes('c') || ks.includes('s') || !st.s); };
  function sync() {
    const u = new URL(location.href);
    [['o', 'outcome'], ['c', 'cause'], ['s', 'status'], ['f', 'flag']].forEach(([k, name]) => { if (st[k]) u.searchParams.set(name, st[k]); else u.searchParams.delete(name); });
    history.replaceState(null, '', u);
  }
  function apply() {
    const want = {}; KEYS.forEach(k => { if (st[k]) want[k] = st[k]; });
    let n = 0, solved = 0;
    rows.forEach(r => { const on = hits(want, r); r.hidden = !on; if (on) { n++; if (F.get(r).o === 'resolved') solved++; } });
    const any = Object.keys(want).length > 0;
    empty.hidden = n > 0; clear.hidden = !any;
    count.textContent = any ? `${n} of ${N} · ${solved} solved` : String(N);
    liveEl.textContent = any ? `${n} of ${N} trials shown, ${solved} solved` : `All ${N} trials shown`;
    fxs.forEach(el => el.classList.toggle('on', pressed(el)));
    facets.forEach(paint);
    sync();
  }
  // a legend row or segment sets the outcome; a cause outcome + cause + status; a flag line the flag. Pressed again: off.
  document.addEventListener('click', e => {
    $$('.facet details[open], .pop[open]', page).forEach(d => { if (!d.contains(e.target)) d.open = false; });
    const el = e.target.closest && e.target.closest('.fx'); if (!el || !page.contains(el)) return;
    e.preventDefault();
    const ks = KEYS.filter(k => k in el.dataset), menu = el.closest('details');
    const on = !menu && pressed(el);
    ks.forEach(k => { st[k] = on ? '' : el.dataset[k]; });
    if (ks.includes('c') && !ks.includes('s')) st.s = '';
    if (menu) menu.open = false;
    apply(); hide(); muted = el.closest('[data-tip]');
  });
  [clear, ...$$('[data-clear]', page)].forEach(b => b.addEventListener('click', () => { KEYS.forEach(k => { st[k] = ''; }); apply(); heading.focus(); }));

  // ---- linked preview: a mark lights the rows it would filter; a row lights its marks on the board; a flag slot lights
  //      its flag's line and a flag's line its slots. Counts never move. ----
  const inside = (el, src) => { const ks = KEYS.filter(k => k in src.dataset); return ks.length > 0 && ks.every(k => el.dataset[k] === src.dataset[k]); };
  let cur = null; const pips = $$('#trial-rows .pip');
  const light = src => {
    if (src === cur) return; cur = src;
    const isPip = !!src && src.matches('.pip'), isRow = !!src && !isPip && src.matches('tr[data-outcome]');
    const S = src && !isRow && !isPip ? rows.filter(r => !r.hidden && hits(src.dataset, r)) : [];
    tbody.classList.toggle('pv', !!src && !isRow && !isPip);
    rows.forEach(r => r.classList.toggle('lit', S.includes(r)));
    if (board) board.classList.toggle('pv', isRow || isPip);
    hls.forEach(e => { e.classList.toggle('lit', isPip ? e.dataset.f === src.dataset.f : isRow && hits(e.dataset, src));
      e.classList.toggle('hot', !!src && !isRow && !isPip && inside(e, src)); });
    const fl = src && !isRow && !isPip && 'f' in src.dataset ? src.dataset.f : null;
    pips.forEach(p => p.classList.toggle('hot', p.dataset.f === fl));
  };
  const target = t => { const el = t && t.closest && t.closest('#trial-rows .pip, .fx, #trial-rows tr[data-outcome]'); return el && page.contains(el) ? el : null; };
  document.addEventListener('pointerover', e => light(target(e.target)));
  document.addEventListener('focusin', e => light(target(e.target)));
  document.addEventListener('focusout', e => { if (!target(e.relatedTarget)) light(null); });

  // ---- sort Tokens / Total: descending, ascending, recorded order ----
  const sorts = $$('[data-sort]', page); let sort = { k: '', dir: '' };
  sorts.forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    const k = a.dataset.sort, cyc = ['descending', 'ascending', ''], dir = sort.k === k ? cyc[(cyc.indexOf(sort.dir) + 1) % 3] : cyc[0];
    sort = { k: dir ? k : '', dir };
    sorts.forEach(b => b.closest('th').removeAttribute('aria-sort'));
    if (dir) a.closest('th').setAttribute('aria-sort', dir);
    const before = new Map(rows.map(r => [r, r.getBoundingClientRect().top])), v = r => +r.dataset[k];
    const order = [...rows].sort((x, y) => (!dir ? 0 : dir === 'ascending' ? v(x) - v(y) : v(y) - v(x)) || x.dataset.i - y.dataset.i);
    order.forEach(r => tbody.insertBefore(r, empty));
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    order.forEach(r => { const dy = before.get(r) - r.getBoundingClientRect().top; if (!dy) return;
      r.style.transition = 'none'; r.style.transform = `translateY(${dy}px)`; r.getBoundingClientRect();
      r.style.transition = 'transform .28s var(--ease)'; r.style.transform = ''; });
  }));

  // ---- Esc closes what is open, else clears the filters ----
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const open = $$('.facet details[open], .pop[open]', page);
    if (open.length) { open.forEach(d => { d.open = false; }); hide(); $('summary', open[0]).focus(); return; }
    if (tipFor) { hide(); return; }
    if (KEYS.some(k => st[k])) { KEYS.forEach(k => { st[k] = ''; }); apply(); light(null); }
  });

  // ---- one tooltip: the same words on hover and on keyboard focus; it repeats the page, never gates it ----
  const tip = document.createElement('div'); tip.className = 'rp-tip'; tip.id = 'rp-tip'; tip.setAttribute('role', 'tooltip'); tip.hidden = true; document.body.append(tip);
  let tipFor = null, muted = null;
  const show = el => { tipFor = el; tip.textContent = el.dataset.tip; tip.hidden = false; el.setAttribute('aria-describedby', 'rp-tip');
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(r.left, innerWidth - 8 - w)) + 'px';
    tip.style.top = (r.bottom + 8 + h > innerHeight ? Math.max(8, r.top - h - 8) : r.bottom + 8) + 'px'; };
  function hide() { if (tipFor) tipFor.removeAttribute('aria-describedby'); tipFor = null; tip.hidden = true; }
  document.addEventListener('pointerover', e => { const el = e.target.closest && e.target.closest('[data-tip]');
    if (el !== muted) muted = null; if (el && el === muted) return;
    if (el && page.contains(el)) { if (el !== tipFor) show(el); } else if (tipFor) hide(); });
  document.addEventListener('focusin', e => { const el = e.target.closest && e.target.closest('[data-tip]'); if (el && page.contains(el)) show(el); });
  document.addEventListener('focusout', hide);
  addEventListener('scroll', hide, { passive: true });

  apply();
})();
