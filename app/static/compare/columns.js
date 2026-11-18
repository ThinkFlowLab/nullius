/* Compare's columns (templates/compare/compare.html): each header's workspace and run. The page's URL is the
   comparison, its ordered pick= values: a run picked opens it with that run in its column; a workspace picked
   lists its runs in place (/compare/runs). The run list searches as the table toolkit does. */
window.NulliusCompare = {
  /* This page's URL with column `k` holding `value` (`<workspace>:<run id>`, `<workspace>:` for none). */
  url(values, k, value) {
    const u = new URL(location.href);
    u.searchParams.delete('pick');
    u.searchParams.delete('job');
    values.forEach((v, i) => u.searchParams.append('pick', i === k ? value : v));
    return u.pathname + u.search;
  },

  /* `node` showing the run `r`: agent · model, a check or its shards marked, the date at the right, where it
     ran from below; its id in the tooltip. */
  entry(node, r) {
    const span = (cls, text) => { const s = document.createElement('span'); s.setAttribute('class', cls); s.textContent = text; return s; };
    const line = span('run-line', [r.agent, r.model].filter(Boolean).join(' · '));
    if (r.check) line.appendChild(span('chip-control', 'check'));
    if (r.shards > 1) line.appendChild(span('run-shards', `${r.shards} shards`));
    node.textContent = '';
    [line, span('run-date', r.date), span('run-where', r.where)].forEach(s => node.appendChild(s));
    node.setAttribute('title', r.id);
  },

  /* The listbox `list` of `runs`, the `same` first (the baseline's dataset) and a labelled line after them, a
     line between launches. `n` names the options. */
  fill(list, runs, same, n) {
    const line = text => {
      const li = document.createElement('li');
      li.setAttribute('role', 'separator');
      li.setAttribute('class', text ? 'run-sep labelled' : 'run-sep');
      li.textContent = text;
      return li;
    };
    list.textContent = '';
    runs.forEach((r, i) => {
      if (i && i === same) list.appendChild(line('Other datasets'));
      else if (i && r.launch !== runs[i - 1].launch) list.appendChild(line(''));
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.setAttribute('id', `${n}-${i}`);
      li.setAttribute('class', 'run-entry');
      li.dataset.value = r.value;
      li.dataset.search = [r.agent, r.model, r.where, r.date, r.id, r.check ? 'check' : '', r.shards > 1 ? `${r.shards} shards` : ''].join(' ');

      this.entry(li, r);
      list.appendChild(li);
    });
  },

  /* One column's `cells`, its header `th` first: its workspace select, its run button and the searchable list it
     opens. */
  column(cells, cfg) {
    const th = cells[0], find = s => cells.map(c => c.querySelector(s)).find(Boolean);
    const k = +th.dataset.column, ws = find('[data-ws]'), btn = find('[data-run-btn]');
    const menu = find('[data-run-menu]'), q = menu.querySelector('input'), list = menu.querySelector('[role="listbox"]');
    const none = menu.querySelector('[data-none]');
    let at = -1;
    const shown = () => [...list.querySelectorAll('[role="option"]')].filter(o => !o.hidden);
    const able = () => shown().filter(o => o.getAttribute('aria-disabled') !== 'true');
    const mark = i => {
      const os = able();
      at = Math.max(Math.min(i, os.length - 1), os.length ? 0 : -1);
      os.forEach((o, j) => o.setAttribute('aria-selected', String(j === at)));
      if (at < 0) q.removeAttribute('aria-activedescendant');
      else { q.setAttribute('aria-activedescendant', os[at].id); os[at].scrollIntoView?.({block: 'nearest'}); }
    };
    const search = () => {
      list.querySelectorAll('[role="option"]').forEach(o => { o.hidden = !NulliusTable.matches(o.dataset.search, q.value); });
      list.querySelectorAll('[role="separator"]').forEach(s => { s.hidden = Boolean(q.value.trim()); });
      none.hidden = shown().length > 0;
      mark(0);
    };
    const show = data => {
      this.fill(list, data.runs, data.same, list.id);
      // the page's picks name whole runs (a shard's id stands for its run): another column's is greyed, this one's checked
      list.querySelectorAll('[role="option"]').forEach(o => {
        const j = cfg.values.indexOf(o.dataset.value);
        if (j === k) o.setAttribute('aria-checked', 'true');
        else if (j >= 0) { o.setAttribute('aria-disabled', 'true'); o.setAttribute('title', `Already in column ${j + 1}`); }
      });
      search();
    };
    const pick = o => { location.href = this.url(cfg.values, k, o.dataset.value); };
    q.addEventListener('input', search);
    q.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); mark(at + (e.key === 'ArrowDown' ? 1 : -1)); }
      else if (e.key === 'Enter' && able()[at]) { e.preventDefault(); pick(able()[at]); }
    });
    list.addEventListener('click', e => { const o = e.target.closest('[role="option"]'); if (o && o.getAttribute('aria-disabled') !== 'true') pick(o); });
    // fixed under its button (the table's scroll box would clip it), placed before it opens and takes the focus,
    // so the focus never scrolls the table
    const place = () => {
      const b = btn.getBoundingClientRect(), w = Math.min(Math.max(b.width, 320), innerWidth - 16);
      Object.assign(menu.style, {top: `${b.bottom + 4}px`, left: `${Math.max(8, Math.min(b.left, innerWidth - w - 8))}px`, width: `${w}px`});
    };
    btn.addEventListener('click', place);
    NulliusTable.menu(btn, menu);

    addEventListener('resize', place);
    document.addEventListener('scroll', e => { if (!menu.contains(e.target)) place(); }, true);

    ws.addEventListener('change', () => {
      nulliusFetch(cfg.runs + '?' + new URLSearchParams({workspace: ws.value, dataset: cfg.dataset}))
        .then(r => r.ok ? r.json() : Promise.reject(r.status))
        .then(data => {
          show(data);
          // an empty column keeps its workspace in the URL; one with a run keeps it until another is picked
          if (!th.dataset.run) { cfg.values[k] = ws.value + ':'; history.replaceState(null, '', this.url(cfg.values, k, cfg.values[k])); }
          if (menu.hidden) btn.click();
        });
    });
    show(cfg.lists[ws.value] || {runs: [], same: 0});
    const mine = (cfg.lists[ws.value]?.runs || []).find(r => r.value === cfg.values[k]);
    if (mine) this.entry(btn.querySelector('[data-run-label]'), mine);
  },

  /* Every column of the page, its cells in the rows they share, the Order menu and each chart card's menus. A list
     opens only on its button. */
  wire(cfg) {
    document.querySelectorAll('[data-order-btn], [data-chart-btn]').forEach(b => NulliusTable.menu(b, b.nextElementSibling));
    const cols = [];
    document.querySelectorAll('[data-column]').forEach(c => (cols[+c.dataset.column] ||= []).push(c));
    cols.forEach(cells => this.column(cells, cfg));
  },
};
