/* The table toolkit's script: each function is a feature a table.grid opts
   into (the macros of templates/_tables.html; their styles in style.css,
   "the table toolkit"), and the card toolkit's (templates/_cards.html). */
window.NulliusTable = {
  /* Whether `text` holds every word of `query`, in any order and case. */
  matches(text, query) {
    const t = (text ?? '').toLowerCase();
    return query.toLowerCase().split(/\s+/).every(w => t.includes(w));
  },

  /* A header with data-type sorts its column on a click or Enter; num sorts
     descending first. A cell's data-sort wins over its text. A header that
     carries data-sorted at load is the sort the rows came in. The sorted
     header carries aria-sort. `after` runs after each sort. Returns resort(),
     to sort rows put in since; resort.set(index, dir) sorts by that header
     (dir 1 up, -1 down), set(null) puts the rows back in the order they came
     in; resort.state() is {col, dir}. */
  sortable(table, after = () => {}) {
    const heads = [...table.tHead.rows[0].cells], body = table.tBodies[0], order = [...body.rows];
    const first = heads.find(th => th.dataset.sorted);
    let col = first ? heads.indexOf(first) : null, dir = first?.dataset.sorted === 'asc' ? 1 : -1;
    const key = (row, type) => {
      const cell = row.cells[col];
      const raw = cell.dataset.sort ?? cell.textContent.trim();
      return type === 'num' ? parseFloat(raw) || 0 : raw.toLowerCase();
    };
    const resort = () => {
      if (col === null) return;
      const type = heads[col].dataset.type || 'text';
      [...body.rows].sort((a, b) => {
        const x = key(a, type), y = key(b, type);
        return (x < y ? -1 : x > y ? 1 : 0) * dir;
      }).forEach(r => body.appendChild(r));
      after();
    };
    const mark = () => heads.forEach((th, idx) => {
      th.toggleAttribute('data-sorted', idx === col);
      th.toggleAttribute('aria-sort', idx === col);
      if (idx === col) { th.dataset.sorted = dir > 0 ? 'asc' : 'desc'; th.setAttribute('aria-sort', dir > 0 ? 'ascending' : 'descending'); }
    });
    resort.set = (idx, d = 1) => {
      col = idx; dir = d; mark();
      if (idx !== null) return resort();
      order.forEach(r => body.appendChild(r));
      after();
    };
    resort.state = () => ({col, dir});
    mark();
    heads.forEach((th, idx) => {
      if (!th.dataset.type) return;
      th.tabIndex = 0;
      const sort = () => resort.set(idx, col === idx ? -dir : (th.dataset.type === 'num' ? -1 : 1));
      th.addEventListener('click', sort);
      th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
    });
    return resort;
  },

  /* The filters under `scope`, the controls macro's: value lists
     (data-filter-menu) and rows of pills (data-pills). `allowed`: column ->
     the Set of values chosen, or null when all are; every list and row of a
     column shows that one state. A row passes when, for each column, one of
     its values (data-f-<col>, one per line) is chosen, or its data-unless-<col>
     names a column that narrows and passes it; a choice made at load
     filters from the start. `changed` runs after each change, a list's
     change event included. */
  filters(scope, changed) {
    const allowed = {}, views = [...scope.querySelectorAll('[data-filter-menu], [data-pills]')];
    const col = v => v.dataset.filterMenu ?? v.dataset.pills;
    const values = v => [...v.querySelectorAll('[data-value]')];
    const draw = () => views.forEach(v => {
      const set = allowed[col(v)];
      values(v).forEach(b => { if (b.matches('input')) b.checked = !set || set.has(b.dataset.value); else b.setAttribute('aria-pressed', String(Boolean(set?.has(b.dataset.value)))); });
      v.querySelector('.filter-pill[data-all]')?.setAttribute('aria-pressed', String(!set));
    });
    // All chosen means "no filter": keeps the active markers honest.
    const choose = (v, on) => { allowed[col(v)] = on && on.size < values(v).length ? on : null; };
    const set = (v, on) => { choose(v, on); draw(); changed(); };
    const chosen = v => new Set(values(v).filter(b => b.checked || b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.value));
    views.forEach(v => {
      if (!(col(v) in allowed)) { const on = chosen(v); choose(v, v.dataset.pills !== undefined && !on.size ? null : on); }
      if (v.dataset.pills !== undefined) {
        // a pill toggles its value; none left is All
        v.addEventListener('click', e => {
          const b = e.target.closest('[data-value]'), now = allowed[col(v)];
          if (e.target.closest('[data-all]')) return set(v, null);
          if (!b) return;
          const on = new Set(now || []);
          on.has(b.dataset.value) ? on.delete(b.dataset.value) : on.add(b.dataset.value);
          set(v, on.size ? on : null);
        });
        return;
      }
      v.addEventListener('change', () => set(v, chosen(v)));
      v.addEventListener('click', e => {
        if (e.target.closest('[data-all]')) set(v, null);
        if (e.target.closest('[data-none]')) set(v, new Set());
      });
    });
    draw();
    // data-unless-<col>="<other>": a row the other column narrows to passes this one whatever it holds
    const ok = (row, col) => !allowed[col] || (row.getAttribute('data-f-' + col) ?? '').split('\n').some(v => allowed[col].has(v));
    const passes = row => Object.keys(allowed).every(col => ok(row, col) || (by => !!by && !!allowed[by] && ok(row, by))(row.getAttribute('data-unless-' + col)));
    const clear = () => { Object.keys(allowed).forEach(c => { allowed[c] = null; }); draw(); };
    return {allowed, passes, clear};
  },

  /* A Columns menu (opened by `menu`): its checkboxes (data-col) show or
     hide the table's cells of that data-col, remembered per viewer under
     `key`; a disabled one stays on, a data-off one is off until the viewer
     chooses. Returns show(), to apply the choice to rows put in since. */
  columns(table, menu, key) {
    const boxes = [...menu.querySelectorAll('input[data-col]')];
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(key) || 'null'); } catch {}
    const show = () => boxes.forEach(cb =>
      table.querySelectorAll(`[data-col="${cb.dataset.col}"]`).forEach(el => { el.style.display = cb.checked ? '' : 'none'; }));
    boxes.forEach(cb => {
      cb.checked = cb.disabled || (stored ? stored.includes(cb.dataset.col) : !cb.hasAttribute('data-off'));
      cb.addEventListener('change', () => {
        show();
        try { localStorage.setItem(key, JSON.stringify(boxes.filter(b => b.checked).map(b => b.dataset.col))); } catch {}
      });
    });
    show();
    return show;
  },

  /* A toolbar's popover: `btn` opens `menu` and puts the focus in it; a click
     elsewhere or Tab out of it closes it, and Escape too, giving the focus
     back to `btn`. A click on a label's text moves the focus to a focusable
     ancestor (the page's <main>): that is not leaving. */
  menu(btn, menu) {
    const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
    // a modal over the page (the first-visit guide) takes the focus and the clicks without closing the menu under it
    const away = to => !menu.contains(to) && !btn.contains(to) && !to.closest?.('[aria-modal="true"]');
    btn.addEventListener('click', () => {
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
      // one that opens leftwards past the page's edge (a narrow screen) opens rightwards instead
      menu.style.left = menu.style.right = '';
      if (open && menu.getBoundingClientRect().left < 0) Object.assign(menu.style, {left: '0', right: 'auto'});
      if (open) menu.querySelector('input:not(:disabled), button, a[href]')?.focus();
    });
    document.addEventListener('click', e => { if (!menu.hidden && away(e.target)) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.hidden) { close(); btn.focus(); } });
    const left = e => { const to = e.relatedTarget; if (to && away(to) && !to.contains(menu)) close(); };
    menu.addEventListener('focusout', left);
    btn.addEventListener('focusout', left);
  },

  /* A controls row's search box and filters (the controls macro), under
     `tools`; each optional. Search keeps the rows (or cards) whose
     data-search holds every word of the query, in any order and case;
     Escape clears it. A column narrows when it does not show every value;
     the Filter button or a dropdown holding such a column is marked
     .narrowed and counts: the Filter button the columns it narrows, a
     dropdown the values chosen (after None, 0), or the one chosen by name. Clear filters clears every
     column. `changed` runs after any of them. Returns passes(row). */
  narrow(tools, changed) {
    const [fbtn, fmenu] = [tools.querySelector('[data-tools-btn="filter"]'), tools.querySelector('[data-tools="filter"]')];
    const q = tools.querySelector('[data-search]');
    if (q) {
      q.addEventListener('input', changed);
      q.addEventListener('keydown', e => { if (e.key === 'Escape' && q.value) { e.preventDefault(); q.value = ''; changed(); } });
    }
    const found = row => !q || this.matches(row.dataset.search, q.value);
    const menus = [...(fmenu ? [[fbtn, fmenu, () => 1]] : []),
                   ...[...tools.querySelectorAll('[data-dropdown]')].map(b => [b, b.parentNode.querySelector('.menu'),
                     set => set.size === 1 ? b.parentNode.querySelector(`[data-value="${CSS.escape([...set][0])}"]`)?.parentNode.textContent.trim() ?? 1 : set.size])];
    const mark = () => menus.forEach(([btn, menu, n]) => {
      const sets = [...menu.querySelectorAll('[data-filter-menu]')].map(v => f.allowed[v.dataset.filterMenu]).filter(Boolean);
      const count = btn.querySelector('.tools-count');
      btn.classList.toggle('narrowed', sets.length > 0);
      count.textContent = sets.length === 1 ? n(sets[0]) : sets.length ? sets.reduce((sum, set) => sum + n(set), 0) : '';
      count.hidden = !sets.length;
    });
    const narrowed = () => { mark(); changed(); };
    const f = this.filters(tools, narrowed);
    mark();
    menus.forEach(([btn, menu]) => this.menu(btn, menu));
    fmenu?.querySelector('[data-clear]').addEventListener('click', () => { f.clear(); narrowed(); });
    return row => f.passes(row) && found(row);
  },

  /* A table's controls row: narrow's search and Filter, and the Sort by
     menu, which sets the sort and follows a header click. Returns
     {passes, resort}, as narrow and sortable do. */
  toolbar(table, tools, changed) {
    const passes = this.narrow(tools, changed);
    const [sbtn, smenu] = [tools.querySelector('[data-tools-btn="sort"]'), tools.querySelector('[data-tools="sort"]')];
    const heads = [...table.tHead.rows[0].cells];
    const sync = () => {
      if (!smenu) return;
      const {col, dir} = resort.state(), on = col === null ? '' : heads[col].dataset.col;
      smenu.querySelectorAll('[data-sort-col]').forEach(r => { r.checked = r.value === on; });
      smenu.querySelectorAll('[data-sort-dir]').forEach(r => { r.checked = Boolean(on) && r.value === (dir > 0 ? 'asc' : 'desc'); r.disabled = !on; });
    };
    const resort = this.sortable(table, () => { sync(); changed(); });
    if (smenu) {
      smenu.addEventListener('change', e => {
        const idx = heads.findIndex(h => h.dataset.col === smenu.querySelector('[data-sort-col]:checked').value);
        if (idx < 0) return resort.set(null);
        const dir = e.target.matches('[data-sort-dir]') ? (e.target.value === 'asc' ? 1 : -1) : (heads[idx].dataset.type === 'num' ? -1 : 1);
        resort.set(idx, dir);
      });
      sync();
      this.menu(sbtn, smenu);
    }
    return {passes, resort};
  },

  /* A table of the table macro (data-table): its controls row (id
     <table id>-tools), its Columns menu and checkbox column when it has
     them, and its pages; choices kept per viewer under <table id>.cols and
     .per. A row holding a pick toggle (the cards macro's rows) toggles it
     and, chosen, shows whatever the search and filters say. */
  wire(table) {
    const tools = document.getElementById(table.id + '-tools');
    const cmenu = tools.querySelector('[data-tools="columns"]');
    if (cmenu) {
      this.columns(table, cmenu, table.id + '.cols');
      this.menu(tools.querySelector('[data-tools-btn="columns"]'), cmenu);
    }
    const ticks = table.querySelector('[data-check-all]') ? this.checks(table) : () => {};
    let first = () => {};
    const t = this.toolbar(table, tools, () => first());
    const pick = Boolean(table.querySelector('[data-pick]'));
    if (pick) this.picks(table, 'tr');
    first = this.pager(table, table.id + '.per', pick ? r => t.passes(r) || this.chosen(r) : t.passes, ticks);
  },

  /* A collection of the cards macro (data-cards): its controls row (id
     <id>-tools) shows the cards (data-card) that pass, the note when none
     does; a data-group holding cards hides when none of them shows, and its
     data-group-n counts those that do. After a change, a screen reader hears
     how many show once the changes pause, counted in the live region's
     data-noun. A click anywhere on a card but on a control of its own
     follows the card's link, in a new tab or window with the keys or the
     button a link takes; a click that ends a text selection does not. A pick
     card's title is a toggle (`picks`); the page keeps the choice, and a
     chosen card shows whatever the search and filters say. Sort by, when it
     has one, sorts the cards within their grids as a table's rows. */
  cards(root) {
    const items = [...root.querySelectorAll('[data-card]')], empty = document.querySelector(`[data-empty="${CSS.escape(root.id)}"]`);
    const live = document.querySelector(`[data-live="${CSS.escape(root.id)}"]`), say = this.voice(live);
    const follow = e => {
      const link = e.target.closest('[data-card]')?.querySelector('.card-link');
      if (!link || e.target.closest('a, button, input, select, textarea, label, summary, [role=button]') || !getSelection().isCollapsed) return;
      if (link.dataset.pick !== undefined) { if (e.button === 0) link.click(); return; }
      if (e.button === 1 || e.ctrlKey || e.metaKey || e.shiftKey) window.open(link.href, '_blank', 'noopener');
      else if (e.button === 0) link.click();
    };
    root.addEventListener('click', follow);
    root.addEventListener('auxclick', follow);
    this.picks(root);
    const draw = () => {
      items.forEach(c => { c.hidden = !passes(c) && !this.chosen(c); });
      root.querySelectorAll('[data-group]').forEach(g => {
        const n = [...g.querySelectorAll('[data-card]')].filter(c => !c.hidden).length, count = g.querySelector('[data-group-n]');
        g.hidden = !n;
        if (count) count.textContent = n;
      });
      const n = items.filter(c => !c.hidden).length;
      empty.hidden = n > 0;
      say(n ? `${n} of ${items.length} ${live.dataset.noun}` : empty.textContent);
    };
    const tools = document.getElementById(root.id + '-tools'), passes = this.narrow(tools, draw);
    const [sbtn, smenu] = [tools.querySelector('[data-tools-btn="sort"]'), tools.querySelector('[data-tools="sort"]')];
    if (smenu) {   // the table's Sort by, over each card's data-s-<field>
      const dirs = [...smenu.querySelectorAll('[data-sort-dir]')];
      const sort = e => {
        const c = smenu.querySelector('[data-sort-col]:checked'), on = c.value, num = c.dataset.type === 'num';
        const dir = e?.target.matches('[data-sort-dir]') ? (e.target.value === 'asc' ? 1 : -1) : (num ? -1 : 1);
        dirs.forEach(r => { r.checked = Boolean(on) && r.value === (dir > 0 ? 'asc' : 'desc'); r.disabled = !on; });
        const key = card => { const raw = card.getAttribute('data-s-' + on) ?? ''; return num ? parseFloat(raw) || 0 : raw.toLowerCase(); };
        (on ? [...items].sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * dir; }) : items).forEach(c => c.parentNode.appendChild(c));
      };
      smenu.addEventListener('change', sort);
      sort();
      this.menu(sbtn, smenu);
    }
    draw();
  },

  /* The pick toggles under `root`, buttons (data-pick, aria-pressed) or
     checkboxes (data-pick): a click, Space or Enter flips one and sends nullius-pick {value, pressed} from it. A single
     choice, a button with data-pick-one or a radio, is pressed again rather than let go, and lets the others go; with
     `row`, a click elsewhere in the row (a selector) that holds one, but on a
     control of its own or at the end of a text selection, clicks it. */
  picks(root, row) {
    root.addEventListener('click', e => {
      if (row && !e.target.closest('a, button, input, select, textarea, label, summary') && getSelection().isCollapsed) {
        e.target.closest(row)?.querySelector('[data-pick]')?.click();
        return;
      }
      const b = e.target.closest('[data-pick]');
      if (!b || b.disabled) return;
      const box = b.type === 'checkbox' || b.type === 'radio', one = b.dataset.pickOne !== undefined;
      const pressed = box ? b.checked : one || b.getAttribute('aria-pressed') !== 'true';   // a box has ticked itself
      if (one) root.querySelectorAll('[data-pick-one][aria-pressed="true"]').forEach(x => x.setAttribute('aria-pressed', 'false'));
      if (!box) b.setAttribute('aria-pressed', String(pressed));
      b.dispatchEvent(new CustomEvent('nullius-pick', {bubbles: true, detail: {value: b.dataset.pick, pressed}}));
    });
  },

  /* Whether a card or a row holds a pressed pick: chosen, it shows whatever the search and filters say. */
  chosen(el) { return Boolean(el.querySelector('[data-pick][aria-pressed="true"], [data-pick]:checked')); },

  /* A checkbox column (a `check` column, check_cell in each row): the header
     ticks or unticks every visible row, and shows all, none or some of them
     ticked. Returns sync(), to run when rows show or hide. */
  checks(table) {
    const all = table.querySelector('[data-check-all]');
    const shown = () => [...table.tBodies[0].rows].filter(r => !r.hidden).map(r => r.querySelector('[data-check]')).filter(Boolean);
    const sync = () => {
      const boxes = shown(), n = boxes.filter(b => b.checked).length;
      all.checked = boxes.length > 0 && n === boxes.length;
      all.indeterminate = n > 0 && n < boxes.length;
    };
    table.addEventListener('change', e => {
      if (e.target === all) shown().forEach(b => { b.checked = all.checked; });
      else if (!e.target.matches('[data-check]')) return;
      sync();
    });
    sync();
    return sync;
  },

  /* Sticky columns, those whose header is a .sticky-col: each cell under it
     pinned at the summed widths of the sticky columns before it. Sticky rows,
     the rows with data-sticky (data-sticky="wide": only from 900px wide, where
     a tall row leaves room for the rest): once the first scrolls above the
     table's controls row (id <table id>-tools), which sticks, they float under
     it in a box of the wrapper's classes until the table ends, its sideways
     scroll kept with the table's. The rows themselves move, so their controls
     keep working; a hidden copy holds each one's place. Again as they resize,
     hide or rows come in. A row without a cell under each header (a colspan,
     an empty row) is left as it is. */
  sticky(table) {
    const heads = [...table.tHead.rows[0].cells], pinned = heads.filter(th => th.classList.contains('sticky-col'));
    const wrap = table.closest('.table-wrap'), wide = matchMedia('(min-width: 900px)');
    const box = document.createElement('div'), float = table.cloneNode(false), cols = document.createElement('colgroup');
    const rows = () => [...table.querySelectorAll('tr[data-sticky]'), ...float.rows];
    const floats = rows().length > 0, tools = floats && table.id && document.getElementById(table.id + '-tools');
    const active = r => r.dataset.sticky !== 'wide' || wide.matches, ghosts = new Map();   // a floating row -> the copy holding its place
    const land = () => { ghosts.forEach((ghost, row) => ghost.replaceWith(row)); ghosts.clear(); };
    const lift = () => {
      const on = rows().filter(active), t = table.getBoundingClientRect();
      const under = tools ? tools.getBoundingClientRect().bottom : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-height'));
      const up = on.length > 0 && Math.min(...on.map(r => (ghosts.get(r) || r).getBoundingClientRect().top)) < under && t.bottom > under;
      if (!up || on.length !== ghosts.size || on.some(r => !ghosts.has(r))) {
        land();
        if (up) [...table.querySelectorAll('tr[data-sticky]')].filter(active).forEach(row => {
          const ghost = row.cloneNode(true), into = row.parentNode.tagName === 'THEAD' ? float.tHead : float.tBodies[0];
          [ghost, ...ghost.querySelectorAll('[id]')].forEach(e => e.removeAttribute('id'));
          ghost.removeAttribute('data-sticky');
          ghost.classList.add('sticky-ghost');
          ghost.setAttribute('aria-hidden', 'true');
          ghost.inert = true;
          row.replaceWith(ghost);
          into.appendChild(row);
          ghosts.set(row, ghost);
        });
        seen.takeRecords();   // its own moves are no change to the table
      }
      box.hidden = !up;
      if (!up) return;
      const widths = [...table.tHead.rows[0].cells].map(c => c.getBoundingClientRect().width);
      cols.replaceChildren(...widths.map(w => { const c = document.createElement('col'); c.style.width = w + 'px'; return c; }));
      float.style.width = t.width + 'px';
      Object.assign(box.style, {top: Math.min(under, t.bottom - box.offsetHeight) + 'px', left: wrap.getBoundingClientRect().left + wrap.clientLeft + 'px', width: wrap.clientWidth + 'px'});
      box.scrollLeft = wrap.scrollLeft;
    };
    const place = () => {
      let left = 0;
      pinned.forEach(th => {
        for (const row of [...table.rows, ...float.rows]) {
          if (row.cells.length !== heads.length) continue;
          const cell = row.cells[th.cellIndex];
          cell.classList.add('sticky-col');
          cell.classList.toggle('sticky-edge', th.classList.contains('sticky-edge'));
          cell.style.setProperty('--sticky-left', left + 'px');
        }
        left += th.getBoundingClientRect().width;
      });
      if (floats) lift();
    };
    const seen = new MutationObserver(place);
    if (tools) tools.classList.add('sticky-tools');
    if (floats) {
      float.removeAttribute('id');
      float.removeAttribute('data-table');
      float.append(cols, document.createElement('thead'), document.createElement('tbody'));
      box.className = wrap.className + ' table-float';
      box.hidden = true;
      box.appendChild(float);
      wrap.before(box);
      wrap.addEventListener('scroll', () => { box.scrollLeft = wrap.scrollLeft; });
      box.addEventListener('scroll', () => { wrap.scrollLeft = box.scrollLeft; });
      addEventListener('scroll', lift, {passive: true});
      addEventListener('resize', lift);
      wide.addEventListener('change', lift);
    }
    const resized = new ResizeObserver(place);
    [...pinned, table].forEach(el => resized.observe(el));
    seen.observe(table, {childList: true, subtree: true});
    place();
  },

  /* A live region: say(text) reads out the last text once the changes
     pause; the first, the page's own drawing at load, is not read out. */
  voice(live) {
    let t, loaded = false;
    return text => {
      clearTimeout(t);
      if (loaded) t = setTimeout(() => { live.textContent = text; }, 300);
      loaded = true;
    };
  },

  /* A table's pages (the pager macro named after its id): the rows `passes`
     lets through, a page of them at a time, the page's size remembered per
     viewer under `key`; its empty note shows when none passes. After a
     change, not at load, a screen reader hears what shows once the changes
     pause, counted in the live region's data-noun. `drawn` runs after each
     page. Returns first(), to run after a filter or a sort: it
     shows the first page. */
  pager(table, key, passes = () => true, drawn = () => {}) {
    const bar = document.querySelector(`[data-pager="${CSS.escape(table.id)}"]`), empty = document.querySelector(`[data-empty="${CSS.escape(table.id)}"]`);
    const [per, note, prev, next, live] = ['[data-per]', '[data-note]', '[data-prev]', '[data-next]', '[data-live]'].map(s => bar.querySelector(s));
    let page = 0;
    const say = this.voice(live);
    try { const n = localStorage.getItem(key); if (n !== null && [...per.options].some(o => o.value === n)) per.value = n; } catch {}
    const draw = () => {
      const rows = [...table.tBodies[0].rows], hit = rows.filter(passes), n = +per.value || hit.length || 1;
      page = Math.min(page, Math.max(0, Math.ceil(hit.length / n) - 1));
      rows.forEach(r => { r.hidden = true; });
      hit.slice(page * n, page * n + n).forEach(r => { r.hidden = false; });
      note.textContent = hit.length ? `${page * n + 1}–${Math.min(hit.length, page * n + n)} of ${hit.length}` : '';
      prev.disabled = page === 0;
      next.disabled = (page + 1) * n >= hit.length;
      empty.hidden = hit.length > 0;
      say(hit.length ? `${note.textContent} ${live.dataset.noun}` : empty.textContent);
      drawn();
    };
    const first = () => { page = 0; draw(); };
    per.addEventListener('change', () => {
      try { localStorage.setItem(key, per.value); } catch {}
      first();
    });
    prev.addEventListener('click', () => { page--; draw(); });
    next.addEventListener('click', () => { page++; draw(); });
    draw();
    return first;
  },
};

/* Every table with sticky columns or rows, every table of the table macro and every collection of the cards macro, set up
   once the page is read. */
const nulliusTables = () => {
  document.querySelectorAll('table.grid').forEach(t => {
    if (t.querySelector('.sticky-col, [data-sticky]')) NulliusTable.sticky(t);
    if (t.hasAttribute('data-table')) NulliusTable.wire(t);
  });
  document.querySelectorAll('[data-cards]').forEach(c => NulliusTable.cards(c));
};
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', nulliusTables);
else nulliusTables();
