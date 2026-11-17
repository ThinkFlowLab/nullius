/* The table toolkit's script: each function is a feature a table.grid opts
   into (the macros of templates/_tables.html; their styles in style.css,
   "the table toolkit"). */
window.NulliusTable = {
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

  /* The value lists (data-filter-menu) under `scope`: the filter_button
     macro's, each opened by its caret, or the tools macro's. `allowed`:
     column -> the Set of values ticked, or null when all are. A row passes
     when, for each column, one of its values (data-f-<col>, one per line) is
     ticked; a box unticked at load filters from the start. `changed` runs
     after each change. */
  filters(scope, changed) {
    const allowed = {};
    scope.querySelectorAll('[data-filter-menu]').forEach(menu => {
      const col = menu.dataset.filterMenu;
      const boxes = () => [...menu.querySelectorAll('input[data-value]')];
      const btn = scope.querySelector(`.filter-btn[data-filter="${col}"]`);
      const read = () => {
        const on = boxes().filter(b => b.checked).map(b => b.dataset.value);
        // All checked means "no filter": keeps the active marker honest.
        allowed[col] = on.length === boxes().length ? null : new Set(on);
        btn?.classList.toggle('on', Boolean(allowed[col]));
      };
      const sync = () => { read(); changed(); };
      read();
      menu.addEventListener('change', sync);
      menu.addEventListener('click', e => {
        if (e.target.closest('[data-all]')) { boxes().forEach(b => b.checked = true); sync(); }
        if (e.target.closest('[data-none]')) { boxes().forEach(b => b.checked = false); sync(); }
      });
      if (!btn) return;
      btn.addEventListener('click', e => {
        e.stopPropagation();          // the th itself sorts; don't sort on filter
        const open = menu.hidden;
        scope.querySelectorAll('.filter-menu').forEach(m => m.hidden = true);
        menu.hidden = !open;
        btn.setAttribute('aria-expanded', String(open));
      });
      menu.addEventListener('click', e => e.stopPropagation());
      document.addEventListener('click', () => {
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
      });
    });
    const passes = row => Object.entries(allowed).every(([col, set]) =>
      !set || (row.getAttribute('data-f-' + col) ?? '').split('\n').some(v => set.has(v)));
    const clear = () => {
      scope.querySelectorAll('[data-filter-menu]').forEach(menu => {
        menu.querySelectorAll('input[data-value]').forEach(b => b.checked = true);
        allowed[menu.dataset.filterMenu] = null;
      });
      scope.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('on'));
    };
    return {allowed, passes, clear};
  },

  /* A Columns menu: `btn` opens `menu` (none: the caller opens it), whose
     checkboxes (data-col) show or hide the table's cells of that data-col,
     remembered per viewer under `key`; a disabled one stays on. `hidden`: the
     columns off until the viewer chooses. Returns show(), to apply the choice
     to rows put in since. */
  columns(table, btn, menu, key, hidden = []) {
    const boxes = [...menu.querySelectorAll('input[data-col]')];
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(key) || 'null'); } catch {}
    const show = () => boxes.forEach(cb =>
      table.querySelectorAll(`[data-col="${cb.dataset.col}"]`).forEach(el => { el.style.display = cb.checked ? '' : 'none'; }));
    boxes.forEach(cb => {
      cb.checked = cb.disabled || (stored ? stored.includes(cb.dataset.col) : !hidden.includes(cb.dataset.col));
      cb.addEventListener('change', () => {
        show();
        try { localStorage.setItem(key, JSON.stringify(boxes.filter(b => b.checked).map(b => b.dataset.col))); } catch {}
      });
    });
    show();
    if (!btn) return show;
    btn.addEventListener('click', () => {
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', e => {
      if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) {
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
      }
    });
    return show;
  },

  /* A toolbar's popover: `btn` opens `menu` and puts the focus in it; a click
     elsewhere or Tab out of it closes it, and Escape too, giving the focus
     back to `btn`. */
  menu(btn, menu) {
    const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
    btn.addEventListener('click', () => {
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
      if (open) menu.querySelector('input:not(:disabled), button')?.focus();
    });
    document.addEventListener('click', e => { if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.hidden) { close(); btn.focus(); } });
    const left = e => { if (e.relatedTarget && !menu.contains(e.relatedTarget) && !btn.contains(e.relatedTarget)) close(); };
    menu.addEventListener('focusout', left);
    btn.addEventListener('focusout', left);
  },

  /* A table's Filter and Sort by menus (the tools macro), under `tools`, in
     place of header carets. Filter counts the columns it narrows; Sort by
     sets the sort, and follows a header click. `changed` runs after either.
     A toolbar without Filter (the macro given no filters) only sorts.
     Returns {passes, resort}, as filters and sortable do. */
  toolbar(table, tools, changed) {
    const [fbtn, fmenu, sbtn, smenu] = ['filter', 'sort'].flatMap(k =>
      [tools.querySelector(`[data-tools-btn="${k}"]`), tools.querySelector(`[data-tools="${k}"]`)]);
    const count = fbtn?.querySelector('.tools-count');
    const mark = () => {
      const n = Object.values(f.allowed).filter(Boolean).length;
      count.textContent = n || '';
      count.hidden = !n;
    };
    const narrowed = () => { mark(); changed(); };
    const f = fmenu ? this.filters(fmenu, narrowed) : {allowed: {}, passes: () => true};
    if (fmenu) {
      mark();
      fmenu.querySelector('[data-clear]').addEventListener('click', () => { f.clear(); narrowed(); });
      this.menu(fbtn, fmenu);
    }
    const heads = [...table.tHead.rows[0].cells];
    const sync = () => {
      const {col, dir} = resort.state(), on = col === null ? '' : heads[col].dataset.col;
      smenu.querySelectorAll('[data-sort-col]').forEach(r => { r.checked = r.value === on; });
      smenu.querySelectorAll('[data-sort-dir]').forEach(r => { r.checked = Boolean(on) && r.value === (dir > 0 ? 'asc' : 'desc'); r.disabled = !on; });
    };
    const resort = this.sortable(table, () => { sync(); changed(); });
    smenu.addEventListener('change', e => {
      const idx = heads.findIndex(h => h.dataset.col === smenu.querySelector('[data-sort-col]:checked').value);
      if (idx < 0) return resort.set(null);
      const dir = e.target.matches('[data-sort-dir]') ? (e.target.value === 'asc' ? 1 : -1) : (heads[idx].dataset.type === 'num' ? -1 : 1);
      resort.set(idx, dir);
    });
    sync();
    this.menu(sbtn, smenu);
    return {passes: f.passes, resort};
  },

  /* A table with a toolbar (`tools`, as toolbar) and pages (as pager, the
     page size kept under `key`): back to the first page after each filter or
     sort. Returns toolbar's {passes, resort}. */
  paged(table, tools, key, empty = null) {
    let first = () => {};
    const t = this.toolbar(table, tools, () => first());
    first = this.pager(table, key, t.passes, empty);
    return t;
  },

  /* A table's pages (the pager macro named after its id): the rows `passes`
     lets through, a page of them at a time, the page's size remembered per
     viewer under `key`; `empty` shows when none passes. Returns first(), to
     run after a filter or a sort: it shows the first page. */
  pager(table, key, passes = () => true, empty = null) {
    const bar = document.querySelector(`[data-pager="${CSS.escape(table.id)}"]`);
    const [per, note, prev, next] = ['[data-per]', '[data-note]', '[data-prev]', '[data-next]'].map(s => bar.querySelector(s));
    let page = 0;
    try { const n = localStorage.getItem(key); if (n !== null && [...per.options].some(o => o.value === n)) per.value = n; } catch {}
    const draw = () => {
      const rows = [...table.tBodies[0].rows], hit = rows.filter(passes), n = +per.value || hit.length || 1;
      page = Math.min(page, Math.max(0, Math.ceil(hit.length / n) - 1));
      rows.forEach(r => { r.hidden = true; });
      hit.slice(page * n, page * n + n).forEach(r => { r.hidden = false; });
      note.textContent = hit.length ? `${page * n + 1}–${Math.min(hit.length, page * n + n)} of ${hit.length}` : '';
      prev.disabled = page === 0;
      next.disabled = (page + 1) * n >= hit.length;
      if (empty) empty.hidden = hit.length > 0;
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
