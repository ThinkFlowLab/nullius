// First steps. On a first open the page asks whether to be guided; yes runs a short tour that moves
// from page to page, then opens the checklist docked on the right. A section or a sub-task opens the page
// that does it and points at the thing to do there. What is done comes from /onboarding (facts this
// machine can check) and from the pages this browser has been on (visits, kept here, nowhere else).
(() => {
  const me = document.currentScript, STATE_URL = me.dataset.state, TOUR = JSON.parse(me.dataset.tour || '[]');
  const KEY = 'nullius-onboarding';
  const read = (store, k, fallback) => { try { return JSON.parse(store.getItem(k)) ?? fallback; } catch { return fallback; } };
  const write = (store, k, v) => { try { v == null ? store.removeItem(k) : store.setItem(k, JSON.stringify(v)); } catch {} };
  const S = Object.assign({asked: false, open: false, dismissed: false, visits: {}}, read(localStorage, KEY, {}));
  const save = () => write(localStorage, KEY, S);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  let sections = [];

  const isDone = x => x.done === true || (x.done === null && !!S.visits[x.id]);
  const required = s => s.subs.filter(x => !x.optional);
  const sectionDone = s => required(s).every(isDone);
  const current = () => sections.find(s => !sectionDone(s));

  const railItem = document.getElementById('onb-open'), rail = document.getElementById('onb-rail'), restore = document.getElementById('onb-restore');
  let panel = null;
  function renderPanel() {
    const all = sections.flatMap(s => s.subs), n = all.filter(isDone).length;
    if (restore) restore.hidden = !S.dismissed;
    const said = document.getElementById('onb-state');
    if (said) said.textContent = S.dismissed ? 'Removed from the rail.' : !S.asked ? 'Not started yet.' : S.open ? 'Open on the right.' : 'In the rail.';
    if (rail) rail.hidden = S.dismissed;
    if (S.dismissed) { S.open = false; unpoint(); }
    if (railItem) {
      railItem.querySelector('.onb-count').textContent = sections.length ? `${n}/${all.length}` : '';
      railItem.setAttribute('aria-expanded', String(S.open));
    }
    document.body.classList.toggle('onb-open', S.open);
    if (!S.open) { panel?.remove(); panel = null; return; }
    if (!panel) { panel = document.createElement('aside'); panel.className = 'onb-panel'; panel.id = 'onb-panel'; panel.setAttribute('aria-label', 'First steps'); document.body.append(panel); }
    const cur = current();
    panel.innerHTML = `<header><h2>First steps</h2><button class="onb-hide" type="button" data-onb="hide">Hide</button></header>
      <div class="onb-prog"><span class="onb-bar"><i style="width:${all.length ? 100 * n / all.length : 0}%"></i></span>${n}/${all.length}</div>
      ${cur || !sections.length ? '' : '<div class="onb-done">All done: Nullius is set up and you have read and compared runs.</div>'}
      ${sections.map((s, i) => {
        const subs = s.subs, done = sectionDone(s), next = subs.find(x => !isDone(x)), open = s === cur || location.pathname === new URL(s.url, location).pathname;
        return `<div class="onb-cat${done ? ' done' : ''}${s === cur ? ' now' : ''}">
          <button type="button" data-onb-cat="${s.id}"><span class="onb-ring">${done ? '✓' : i + 1}</span><span><span class="onb-name">${esc(s.name)}</span><span class="onb-where">${esc(s.where)} →</span></span><span class="onb-cnt">${subs.filter(isDone).length}/${subs.length}</span></button>
          ${open ? `<ul class="onb-subs">${subs.map(x => `<li class="${isDone(x) ? 'done' : ''}${x === next ? ' next' : ''}"><button type="button" data-onb-sub="${s.id}:${x.id}"><span class="onb-tick" aria-hidden="true">${isDone(x) ? '✓' : ''}</span><span>${esc(x.text)}${x.optional ? ' <span class="onb-opt">optional</span>' : ''}</span></button></li>`).join('')}</ul>` : ''}
        </div>`;
      }).join('')}
      <div class="onb-hint">Click a step: Nullius opens its page and points at what to do.</div>
      <button class="onb-restart" type="button" data-onb="tour">Replay the tour</button>`;
  }

  // ---- pointing at what to do: the first target on this page, else its page opened and pointed at there
  let pointed = null, label = null;
  // The label sits on the element's top edge, so a tall element (a whole form) keeps it in view, and it
  // follows the element as the page around it grows or shrinks (a builder step changing, say).
  const watch = new ResizeObserver(() => place());
  function unpoint() { pointed?.classList.remove('onb-todo'); label?.remove(); watch.disconnect(); pointed = label = null; }
  function place() {
    if (!pointed || !label) return;
    if (!pointed.isConnected) return unpoint();
    const r = pointed.getBoundingClientRect(), h = label.offsetHeight;
    label.style.left = Math.max(8, Math.min(r.left + scrollX, scrollX + innerWidth - label.offsetWidth - 16)) + 'px';
    label.style.top = (r.top + scrollY - h - 8 >= 8 ? r.top + scrollY - h - 8 : r.bottom + scrollY + 8) + 'px';
  }
  function point(sub, navigate = true) {
    unpoint();
    if (!sub) return;
    for (const [sel, text] of sub.target) {
      const el = [...document.querySelectorAll(sel)].find(e => e.getClientRects().length);
      if (!el) continue;
      // where the panel covers the page, it steps aside for what it points at
      if (S.open && matchMedia('(max-width: 1100px)').matches) { S.open = false; save(); renderPanel(); }
      pointed = el; el.classList.add('onb-todo');
      label = document.createElement('div'); label.className = 'onb-label'; label.textContent = text; document.body.append(label);
      place(); watch.observe(el); watch.observe(document.body); el.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
      write(sessionStorage, 'onb-point', null);
      return;
    }
    if (navigate && new URL(sub.url, location).pathname !== location.pathname) { write(sessionStorage, 'onb-point', sub.id); location.href = sub.url; }
  }
  addEventListener('resize', place);
  const firstToDo = s => required(s).find(x => !isDone(x)) || s.subs.find(x => !isDone(x)) || s.subs[0];

  // ---- the first-open question
  function welcome() {
    const layer = document.createElement('div');
    layer.className = 'onb-scrim';
    layer.innerHTML = `<div class="onb-dialog" role="dialog" aria-modal="true" aria-labelledby="onb-w-h"><div class="onb-mark" aria-hidden="true">∅</div>
      <h2 id="onb-w-h">Welcome to Nullius. Want a guided start?</h2>
      <p>A one-minute look at the main pages, then a checklist that takes you to a first result:</p>
      <ul><li>Set up a key and where runs happen</li><li>A free check run: oracle and nop call no model</li><li>Read the result, then compare an agent's run</li></ul>
      <div class="onb-actions"><button class="btn ghost" type="button" data-a="no">No, I'll explore</button><button class="btn" type="button" data-a="yes">Yes, guide me</button></div></div>`;
    document.body.append(layer);
    layer.querySelector('[data-a=yes]').focus();
    layer.addEventListener('click', e => {
      const a = e.target.closest('[data-a]')?.dataset.a;
      if (!a) return;
      S.asked = true; S.open = a === 'yes'; save(); layer.remove();
      if (a === 'yes') startTour(); else renderPanel();
    });
  }

  // ---- the tour: one stop per page, the page opened by the tour itself
  function startTour() { write(sessionStorage, 'onb-tour', 0); stop(0); }
  function stop(i) {
    const s = TOUR[i];
    document.querySelectorAll('.onb-spot, .onb-pop').forEach(e => e.remove());
    if (!s) { write(sessionStorage, 'onb-tour', null); S.open = true; save(); renderPanel(); const c = current(); if (c) point(firstToDo(c)); return; }
    write(sessionStorage, 'onb-tour', i);
    if (s.url && new URL(s.url, location).pathname !== location.pathname) { location.href = s.url; return; }
    if (s.sel === '#onb-panel') { S.open = true; save(); renderPanel(); }
    let el = document.querySelector(s.sel);
    if (!el || !el.getClientRects().length) el = document.getElementById('main-content') || document.body;
    const r = el.getBoundingClientRect(), pad = 6;
    const spot = document.createElement('div'); spot.className = 'onb-spot';
    Object.assign(spot.style, {top: r.top - pad + 'px', left: r.left - pad + 'px', width: r.width + 2 * pad + 'px', height: Math.min(r.height, innerHeight - r.top - 16) + 2 * pad + 'px'});
    const pop = document.createElement('div'); pop.className = 'onb-pop'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', s.k);
    pop.innerHTML = `<span class="onb-k">${i + 1} of ${TOUR.length} · ${esc(s.k)}</span><h3>${esc(s.h)}</h3><p>${esc(s.p)}</p>
      <div class="onb-actions"><span class="onb-dots">${TOUR.map((_, j) => `<i class="${j === i ? 'on' : ''}"></i>`).join('')}</span>
      <span class="onb-actions"><button class="btn ghost sm" type="button" data-t="skip">Skip tour</button><button class="btn sm" type="button" data-t="next">${i === TOUR.length - 1 ? 'Start the checklist' : 'Next'}</button></span></div>`;
    document.body.append(spot, pop);
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = s.sel === '#onb-panel' ? r.left - pw - 16 : r.right + 16, top = Math.max(16, Math.min(r.top, innerHeight - ph - 16));
    if (left + pw > innerWidth - 16 || left < 16) { left = Math.max(16, Math.min(r.left, innerWidth - pw - 16)); top = Math.max(16, Math.min(r.bottom + 16, innerHeight - ph - 16)); }
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
    pop.querySelector('[data-t=next]').focus();
    pop.addEventListener('click', e => {
      const t = e.target.closest('[data-t]')?.dataset.t;
      if (t === 'next') stop(i + 1); else if (t === 'skip') stop(TOUR.length);
    });
  }

  document.addEventListener('click', e => {
    const cat = e.target.closest('[data-onb-cat]'), sub = e.target.closest('[data-onb-sub]'), act = e.target.closest('[data-onb]')?.dataset.onb;
    if (cat) { const s = sections.find(x => x.id === cat.dataset.onbCat); return point(firstToDo(s)); }
    if (sub) { const [c, id] = sub.dataset.onbSub.split(':'); return point(sections.find(x => x.id === c).subs.find(x => x.id === id)); }
    if (act === 'dismiss') { S.dismissed = true; save(); return renderPanel(); }
    if (act === 'restore') { S.dismissed = false; S.open = true; save(); return renderPanel(); }
    if (act === 'hide') { S.open = false; save(); unpoint(); return renderPanel(); }
    if (act === 'replay') { S.asked = false; S.dismissed = false; save(); renderPanel(); return welcome(); }
    if (act === 'tour') { S.dismissed = false; save(); return startTour(); }
    if (e.target.closest('#onb-open')) { S.open = !S.open; save(); return renderPanel(); }
    if (pointed && pointed.contains(e.target)) unpoint();
  });

  // ---- on load: the checklist read, this page's visit kept, then whatever was under way carried on
  nulliusFetch(STATE_URL, {headers: {Accept: 'application/json'}}).then(r => r.ok ? r.json() : {sections: []}).catch(() => ({sections: []})).then(j => {
    sections = j.sections || [];
    const here = location.pathname + location.search, tour = read(sessionStorage, 'onb-tour', null);
    // a page the tour opened was shown, not used: it counts as no visit
    if (tour == null) for (const x of sections.flatMap(s => s.subs)) if (x.visit && new RegExp(x.visit).test(here)) S.visits[x.id] = 1;
    save();
    renderPanel();
    if (!S.asked) return welcome();
    if (tour != null) return stop(tour);
    const want = read(sessionStorage, 'onb-point', null), sub = want && sections.flatMap(s => s.subs).find(x => x.id === want);
    if (sub) point(sub, false);
  });
})();
