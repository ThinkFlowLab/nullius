// The run page's board and trials. Every number and row is rendered by the server; this adds the filters (outcome, judge
// cause and status, flag - ANDed, carried in the query string so a link reproduces the view), the linked hover between the
// board and the rows, Tokens/Total sorting, Esc, the live count, and one tooltip for hover and keyboard focus alike.
// A task attempted more than once is one row that opens onto its attempts; a filter opens the groups it matches, and
// clearing it brings back the groups the reader had opened.
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
  // groups: the task's row, its attempts, whether it is open, and whether the reader opened it
  const members = new Map(); rows.forEach(r => { const k = r.dataset.group; if (k) { if (!members.has(k)) members.set(k, []); members.get(k).push(r); } });
  const groups = new Map($$('#trial-rows tr.task-group').map(h => [h.dataset.group,
    { head: h, rows: members.get(h.dataset.group) || [], open: false, chosen: false, btn: $('.group-toggle', h) }]));
  const groupOf = r => groups.get(r.dataset.group);
  function reveal() {
    rows.forEach(r => { const g = groupOf(r); r.hidden = !r.matched || (!!g && !g.open); });
    groups.forEach(g => { g.head.hidden = !g.rows.some(r => r.matched); g.btn.setAttribute('aria-expanded', String(g.open)); });
  }
  groups.forEach(g => g.head.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    g.open = g.chosen = !g.open; reveal();
    if (g.open && !matchMedia('(prefers-reduced-motion: reduce)').matches)
      g.rows.forEach(r => { if (!r.hidden) r.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 240, easing: 'cubic-bezier(.2, 0, 0, 1)' }); });
  }));
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
    rows.forEach(r => { const on = hits(want, r); r.matched = on; if (on) { n++; if (F.get(r).o === 'resolved') solved++; } });
    const any = Object.keys(want).length > 0;
    groups.forEach(g => { g.open = any ? g.rows.some(r => r.matched) : g.chosen; });
    reveal();
    empty.hidden = n > 0; clear.hidden = !any;
    count.textContent = any ? `${n} of ${N} · ${solved} solved` : String(N);
    liveEl.textContent = any ? `${n} of ${N} trials shown, ${solved} solved` : `All ${N} trials shown`;
    fxs.forEach(el => el.classList.toggle('on', pressed(el)));
    facets.forEach(paint);
    sync();
  }
  // what lies open over the page: its own menus, and the header's cards above it
  const opened = () => [...$$('.facet details[open], .pop[open]', page), ...$$('.run-identity-more[open]')];
  // a legend row or segment sets the outcome; a cause outcome + cause + status; a flag line the flag. Pressed again: off.
  document.addEventListener('click', e => {
    opened().forEach(d => { if (!d.contains(e.target)) d.open = false; });
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
    const isPip = !!src && src.matches('.pip'), isHead = !!src && src.matches('tr.task-group');
    const isRow = !!src && !isPip && (isHead || src.matches('tr[data-outcome]'));
    const own = isHead ? groups.get(src.dataset.group).rows.filter(r => r.matched) : isRow ? [src] : [];
    const S = src && !isRow && !isPip ? rows.filter(r => r.matched && hits(src.dataset, r)) : [];
    tbody.classList.toggle('pv', !!src && !isRow && !isPip);
    rows.forEach(r => r.classList.toggle('lit', S.includes(r)));
    groups.forEach(g => g.head.classList.toggle('lit', g.rows.some(r => S.includes(r))));
    if (board) board.classList.toggle('pv', isRow || isPip);
    hls.forEach(e => { e.classList.toggle('lit', isPip ? e.dataset.f === src.dataset.f : isRow && own.some(r => hits(e.dataset, r)));
      e.classList.toggle('hot', !!src && !isRow && !isPip && inside(e, src)); });
    const fl = src && !isRow && !isPip && 'f' in src.dataset ? src.dataset.f : null;
    pips.forEach(p => p.classList.toggle('hot', p.dataset.f === fl));
  };
  const target = t => { const el = t && t.closest && t.closest('#trial-rows .pip, .fx, #trial-rows tr[data-outcome], #trial-rows tr.task-group'); return el && page.contains(el) ? el : null; };
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
    const all = [...groups.values()].map(g => g.head).concat(rows);
    const before = new Map(all.map(r => [r, r.getBoundingClientRect().top])), v = r => +r.dataset[k];
    const cmp = (x, y) => (!dir ? 0 : dir === 'ascending' ? v(x) - v(y) : v(y) - v(x)) || x.dataset.i - y.dataset.i;
    // a group sorts by its mean over the attempts, and its attempts sort among themselves
    const units = [], placed = new Set();
    rows.forEach(r => { const g = groupOf(r); if (!g) units.push({ lead: r, list: [r] });
      else if (!placed.has(g)) { placed.add(g); const list = [...g.rows].sort(cmp); units.push({ lead: g.head, list: [g.head, ...list] }); } });
    units.sort((a, b) => cmp(a.lead, b.lead));
    const order = units.flatMap(u => u.list);
    order.forEach(r => tbody.insertBefore(r, empty));
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    order.forEach(r => { const dy = before.get(r) - r.getBoundingClientRect().top; if (!dy) return;
      r.style.transition = 'none'; r.style.transform = `translateY(${dy}px)`; r.getBoundingClientRect();
      r.style.transition = 'transform .28s var(--ease)'; r.style.transform = ''; });
  }));

  // a split that would open past the window's right edge (a narrow screen) opens leftwards instead
  $$('.pop', page).forEach(d => d.addEventListener('toggle', () => {
    const m = $('.popm', d); m.style.left = m.style.right = '';
    if (d.open && m.getBoundingClientRect().right > innerWidth) Object.assign(m.style, {left: 'auto', right: '0'});
  }));

  // ---- Esc closes what is open, else clears the filters ----
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const open = opened();
    if (open.length) { open.forEach(d => { d.open = false; }); hide(); $('summary', open[0]).focus(); return; }
    if (tipFor) { hide(); return; }
    if (KEYS.some(k => st[k])) { KEYS.forEach(k => { st[k] = ''; }); apply(); light(null); }
  });

  // ---- one tooltip ----
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

  // ---- tasks to run again: a tick per trial, the rows the filters show at once, the count, then the question ----
  // a task with no result (data-first, "none" when its run ended with none) is run, not run again: the words say which
  const ticks = $$('#trial-rows .pick-row'), ask = $('#rerun-ask');
  if (ticks.length && ask) {
    const pickN = $('#pick-count'), go = $('#rerun-open'), form = $('form', ask), err = $('[data-error]', ask);
    const chosen = () => ticks.filter(x => x.checked), first = x => 'first' in x.dataset;
    const tasks = n => n + (n === 1 ? ' task' : ' tasks');
    const where = $('p.muted', ask), again = where.textContent;
    const recount = () => { const c = chosen(), n = c.length; pickN.textContent = n + ' chosen'; go.disabled = !n;
      go.textContent = n && c.every(first) ? 'Run selected' : 'Run selected again'; };
    ticks.forEach(x => x.addEventListener('change', recount));
    $('#pick-shown').addEventListener('click', () => { rows.forEach(r => { const x = $('.pick-row', r); if (x && r.matched) x.checked = true; }); recount(); });
    // the rest, or a set's tasks not yet run: those rows ticked alone, then the same question
    $$('#rerun-pick [data-pick]').forEach(b => b.addEventListener('click', () => {
      const want = new Set(b.dataset.pick.split(' ')); ticks.forEach(x => { x.checked = want.has(x.value); });
      const menu = b.closest('details'); if (menu) menu.open = false;
      recount(); go.click();
    }));
    go.addEventListener('click', () => {
      // tasks with no result alone: one short question, no names and no reason; else the names, and a reason asked
      const picked = chosen(), fresh = picked.filter(first).length, short = picked.length > 0 && fresh === picked.length;
      const words = short ? `Run the ${tasks(picked.length)} ${picked.some(x => x.dataset.first === 'none') ? 'with no result' : 'not yet run'}, on ${ask.dataset.where}?`
        : 'Run ' + tasks(picked.length) + (!fresh ? ' again' : fresh < picked.length ? ', ' + (picked.length - fresh) + ' again' : '');
      $('#rerun-ask-h').textContent = words;
      $('[data-go]', ask).textContent = short ? 'Run' : words;
      where.textContent = fresh && where.dataset.first ? where.dataset.first : again;
      const names = $('[data-names]', ask), reason = $('[data-reason]', ask);
      where.hidden = names.hidden = reason.hidden = short;
      if (short) form.elements.reason.value = '';
      names.replaceChildren(...(short ? [] : picked).map(x => { const li = document.createElement('li'); li.textContent = x.getAttribute('aria-label').replace(/^Choose /, ''); return li; }));
      err.hidden = true; $('[data-go]', ask).disabled = 'spent' in $('[data-go]', ask).dataset; ask.showModal();
    });
    form.addEventListener('submit', async e => {
      if (!e.submitter || e.submitter.value !== 'run') return;
      e.preventDefault();
      const b = e.submitter; b.disabled = true; err.hidden = true;
      try {
        const r = await nulliusFetch(ask.dataset.post, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': ask.dataset.csrf },
          body: JSON.stringify({ trials: chosen().flatMap(x => (x.dataset.trials || x.value).split(' ')), reason: form.elements.reason.value }) });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) { const u = new URL(location.href); u.searchParams.delete('view'); location.assign(u); return; }
        throw new Error(j.error || 'refused: HTTP ' + r.status);
      } catch (x) { err.textContent = String(x.message || x); err.hidden = false; b.disabled = false; }
    });
    recount();
  }

  apply();

  // ---- the job's one spending cap: Change asks for the cap from the job's next run on ----
  const capAsk = $('#cap-ask'), capOpen = $('#cap-change');
  if (capAsk && capOpen) {
    const capForm = $('form', capAsk), capErr = $('[data-error]', capAsk);
    capOpen.addEventListener('click', () => { capErr.hidden = true; capAsk.showModal(); });
    capForm.addEventListener('submit', async e => {
      if (!e.submitter || e.submitter.value !== 'save') return;
      e.preventDefault();
      const b = e.submitter; b.disabled = true; capErr.hidden = true;
      try {
        const r = await nulliusFetch(capAsk.dataset.post, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': capAsk.dataset.csrf },
          body: JSON.stringify({ max_usd: capForm.elements.max_usd.value }) });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) { location.reload(); return; }
        throw new Error(j.error || 'refused: HTTP ' + r.status);
      } catch (x) { capErr.textContent = String(x.message || x); capErr.hidden = false; b.disabled = false; }
    });
  }
})();

// Each checkbox independently excludes one flag kind. The GET form works without JavaScript.
(() => {
  const box = document.getElementById('filtered-score'); if (!box) return;
  const choices = [...box.querySelectorAll('input[data-kind]')], rates = [...box.querySelectorAll('.filtered-rate')];
  const said = document.getElementById('filtered-live');
  const show = () => {
    const on = choices.filter(c => c.checked).map(c => c.dataset.kind);
    let shown = null;
    rates.forEach(p => { const hit = p.dataset.kinds === on.join(','); p.hidden = !hit; if (hit) { p.id = 'filtered-rate'; shown = p; } else p.removeAttribute('id'); });
    const url = new URL(location.href);
    choices.forEach(c => { if (c.checked) url.searchParams.set(c.dataset.kind, '1'); else url.searchParams.delete(c.dataset.kind); });
    const table = document.querySelector('.tt[data-leave-out]'); if (table) table.dataset.leaveOut = on.join(' ');
    url.hash = ''; history.replaceState(null, '', url);
    if (said) said.textContent = shown ? shown.textContent.replace(/\s+/g, ' ').trim() : 'Filtered score off';
  };
  choices.forEach(c => c.addEventListener('change', show));
  box.addEventListener('submit', e => { e.preventDefault(); show(); });
})();
