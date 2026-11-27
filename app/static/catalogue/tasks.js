// A dataset's tasks (templates/catalogue/tasks.html): the server gives a page of 100 tasks for a search and category
// values (web/catalogue.py, standard/runner/units.py); this page keeps the chosen ids alone. The listing is asked for once the
// page has loaded: in the cache, it shows at once; one that can be made lightly is started out of the request and
// the page asks again until it is ready; one that needs the benchmark's full environment is started on a click
// (List questions), the same way. Until there is a list, the search is hidden.
// The run builder mounts the same list in a dialog (nulliusTasks, data-manual): the tasks of a dataset or of one of
// its sets (data-within), its ticks given back through onChange.
(() => {
function mount(page, opts = {}) {
  const $ = id => page.querySelector('#' + id), d = page.dataset, nouns = d.nouns || 'tasks';
  const writable = d.writable === 'true';
  // The chosen of each dataset ({dataset: ids}): a set's own, or what this tab ticked on the benchmark's other
  // datasets, kept while it goes from one to another and saved together; this page's are ``chosen``.
  const json = (id, empty) => { try { return JSON.parse((document.getElementById(id) || {}).textContent || empty); } catch { return JSON.parse(empty); } };
  // each dataset's [id, {label, href}], in the benchmark's order
  const pages = Object.fromEntries(json('tasks-dataset-pages', '[]'));
  let here = d.dataset || '';
  // the dataset's own routes: listing, prepare, rows, save
  const route = kind => `${d.base || ''}${encodeURIComponent(here)}/${kind}`;
  const draftKey = `nullius-task-set:${d.bench}:${d.version || ''}:${d.set || ''}`;
  // the builder's dialog gives its ticks back (onChange) and keeps no draft of a set
  const draft = !opts.onChange;
  let parts = draft ? json('tasks-chosen-parts', '{}') : {};
  if (draft) try { const kept = JSON.parse(globalThis.sessionStorage.getItem(draftKey) || 'null'); if (kept && typeof kept === 'object') parts = kept; } catch { /* none kept */ }
  const chosen = new Set(opts.chosen || parts[here] || []);
  const changed = () => opts.onChange && opts.onChange([...chosen]);
  const keep = () => {
    if (chosen.size) parts[here] = [...chosen]; else delete parts[here];
    if (draft) try { globalThis.sessionStorage.setItem(draftKey, JSON.stringify(parts)); } catch { /* this page's alone */ }
  };
  const total = () => Object.values(parts).reduce((n, ids) => n + ids.length, 0);
  // opened from a set: its members first; Show all browses the dataset
  const st = {q: '', cats: {}, page: 1, size: 25, showing: !!d.set && chosen.size > 0, ready: false, total: 0, pages: 1, fields: {}};
  // the last draw of Pick at random ({ids, seed, note}), while the chosen are the ones it drew
  let drawn = null;
  const picked = () => !!drawn && drawn.ids.size === chosen.size && [...chosen].every(id => drawn.ids.has(id));
  // a category field and its values as the definition names them (the listing's `fields`), else their keys
  const label = field => st.fields[field]?.label || field.charAt(0).toUpperCase() + field.slice(1).replace(/_/g, ' ');
  const named = (field, value) => st.fields[field]?.names?.[value] || value;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  // the listing at the version the page shows (data-version, empty for the benchmark's default)
  const at = url => {
    const params = new URLSearchParams();
    if (d.version) params.set('version', d.version);
    if (d.options) params.set('options', d.options);
    return String(params) ? url + (url.includes('?') ? '&' : '?') + params : url;
  };
  const post = (url, body) => nulliusFetch(at(url), {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': d.csrf}, body: JSON.stringify(body)});
  const count = n => `${n.toLocaleString('en-US')} ${n === 1 ? nouns.replace(/s$/, '') : nouns}`;

  // the notice: its line, and a link to where a licence is set or the button that starts a full listing
  function say(text, warn, got) {
    if (page.isConnected === false) return;
    const el = $('tasks-state'), link = $('tasks-state-link'), list = $('tasks-list');
    el.hidden = !text; $('tasks-state-text').textContent = text || ''; el.classList.toggle('tasks-warn', !!warn);
    el.classList.toggle('tasks-busy', !!text && !warn && !!got?.preparing);
    link.hidden = !(got && got.link);
    if (got && got.link) { link.href = got.link.href; link.textContent = got.link.text; }
    const startable = !!got && got.state === 'full' && !got.preparing && d.local === 'true';
    if (list) { list.hidden = !startable; $('tasks-list-about').textContent = startable ? got.about || '' : ''; }
  }
  const inDatasets = n => `${n} dataset${n === 1 ? '' : 's'}`;
  function chosenLine() {
    keep();
    const others = Object.keys(parts).filter(k => k !== here), all = total().toLocaleString('en-US');
    $('tasks-chosen').textContent = `${chosen.size.toLocaleString('en-US')} chosen` + (others.length ? ' here' : '');
    if ($('tasks-bar-chosen')) $('tasks-bar-chosen').textContent = others.length
      ? `${all} chosen in ${inDatasets(others.length + (chosen.size ? 1 : 0))}` : $('tasks-chosen').textContent;
    // each dataset with something chosen, this one pressed: a click opens the other's page, its chosen kept
    const strip = $('tasks-parts');
    if (strip) {
      const shown = [...new Set([...Object.keys(pages), ...Object.keys(parts)])].filter(k => k === here ? chosen.size : parts[k]);   // the benchmark's order
      strip.hidden = !others.length;
      strip.innerHTML = !others.length ? '' : '<span class="tasks-catname muted small">Chosen</span>'
        + shown.map(k => `<button type="button" class="filter-pill" data-part="${esc(k)}" aria-pressed="${k === here}">`
          + `${esc((pages[k] || {}).label || k)} <span class="tasks-n">${(k === here ? chosen.size : parts[k].length).toLocaleString('en-US')}</span></button>`).join('')
        + `<b class="tasks-parts-all">${all} chosen in ${inDatasets(shown.length)}</b>`;
    }
    // Show selected, Clear and the save bar once something is chosen; Show all while the chosen are shown
    $('tasks-show').hidden = !chosen.size && !st.showing;
    $('tasks-clear').hidden = !chosen.size;
    if ($('tasks-savebar')) $('tasks-savebar').hidden = !total();
  }
  // a task on one line: its title cut to the width, the whole text in the row's tooltip and, while its box (its text
  // where the box is disabled) has the keyboard's focus, in place; its id beside it on hover or focus. A click
  // anywhere on the row ticks it.
  function row(t) {
    const tags = Object.entries(t.categories || {}).map(([f, v]) => `<span class="cat-tag">${esc(named(f, v))}</span>`).join(' ');
    const whole = t.text || t.title || t.id;
    return `<tr data-id="${esc(t.id)}" title="${esc(whole)}"><td class="tasks-tick"><input type="checkbox" aria-label="Choose ${esc(t.id)}"${chosen.has(t.id) ? ' checked' : ''}${writable ? '' : ' disabled'}></td>`
      + `<td class="tasks-text"><span class="tasks-row"${writable ? '' : ' tabindex="0"'}><span class="tasks-title">${esc(t.title || t.id)}</span>`
      + `<span class="tasks-whole">${esc(whole)}</span>${t.title ? `<small class="tasks-id mono faint small">${esc(t.id)}</small>` : ''}</span>`
      + `${t.unknown ? '<small class="warn small">not in the listing</small>' : ''}`
      + (tags ? `<span class="tasks-tags">${tags}</span>` : '') + `</td><td class="tasks-catcol">${tags}</td></tr>`;
  }
  function draw(tasks) {
    $('tasks-rows').innerHTML = tasks.map(row).join('');
    $('tasks-table').hidden = !tasks.length;
    const fields = [...new Set(tasks.flatMap(t => Object.keys(t.categories || {})))];
    $('tasks-cat-head').hidden = !fields.length;
    $('tasks-cat-head').textContent = fields.map(label).join(' · ');
    page.querySelectorAll('#tasks-rows td.tasks-catcol').forEach(td => { td.hidden = !fields.length; });
  }
  function facets(cats) {
    $('tasks-cats').innerHTML = Object.entries(cats || {}).map(([field, values]) =>
      `<div class="filter-pills" role="group" aria-label="${esc(label(field))}"><span class="tasks-catname muted small">${esc(label(field))}</span>`
      + `<button type="button" class="filter-pill" data-field="${esc(field)}" data-all aria-pressed="${!(st.cats[field] || []).length}">All</button>`
      + values.map(v => `<button type="button" class="filter-pill" data-field="${esc(field)}" data-value="${esc(v.value)}" aria-pressed="${(st.cats[field] || []).includes(v.value)}">${esc(v.name || v.value)} <span class="tasks-n">${v.count}</span></button>`).join('')
      + '</div>').join('');
  }
  function pager() {
    const nav = $('tasks-pages');
    nav.hidden = st.showing || st.total <= 10;
    const prev = nav.querySelector('[data-page=prev]'), next = nav.querySelector('[data-page=next]');
    prev.hidden = next.hidden = $('tasks-page-of').hidden = st.pages <= 1;       // Show stays while one page holds all
    $('tasks-page-of').textContent = `Page ${st.page} of ${st.pages}`;
    prev.disabled = st.page <= 1;
    next.disabled = st.page >= st.pages;
  }
  function query(extra) {
    const u = new URLSearchParams();
    if (st.q) u.set('q', st.q);
    Object.entries(st.cats).forEach(([f, vs]) => vs.forEach(v => u.append('cat', `${f}=${v}`)));
    Object.entries(extra || {}).forEach(([k, v]) => u.set(k, v));
    if (d.version) u.set('version', d.version);
    if (d.within) u.set('within', d.within);
    if (d.options) u.set('options', d.options);
    if (d.limit) u.set('limit', d.limit);
    return route('listing') + '?' + u;
  }
  let polling = null, asked = 0;
  // not in the cache: the server's line for its state; a light listing started once, out of any request, then asked
  // again every few seconds until it is ready
  function prepare(got) {
    return post(route('prepare'), {}).then(r => r.json()).then(g => {
      if (page.isConnected === false) return;
      say(g.say || got.say, false, g); polling = setTimeout(load, 2500);
    })
      .catch(() => say('The list could not be started. Reload the page.', true));
  }
  function notReady(got) {
    if ($('tasks-match')) $('tasks-match').hidden = true;
    $('tasks-bar').hidden = true;
    const light = got.state === 'light' && !got.error, full = got.state === 'full';
    say(got.say, !light && !(full && !got.error), got);
    clearTimeout(polling);
    const elsewhere = () => say('The list is prepared from a browser on the application host. Ids can still be pasted.', true);
    if (!light && !(full && got.preparing)) return full && !got.error && d.local !== 'true' ? elsewhere() : undefined;
    if (!got.preparing && d.local !== 'true') return elsewhere();
    if (!got.preparing && !asked++) return prepare(got);
    polling = setTimeout(load, 2500);
  }
  $('tasks-list-go')?.addEventListener('click', () => { $('tasks-list').hidden = true; asked++; prepare({say: ''}); });
  async function load() {
    if (page.isConnected === false) return;            // a list the builder's dialog has since replaced asks nothing more
    let got;
    try { got = await (await nulliusFetch(query({page: st.page, size: st.size}))).json(); } catch { return say('The list could not be read. Reload the page.', true); }
    if (page.isConnected === false) return;
    if (!got.ok) return say(got.error || 'The list could not be read.', true);
    if (!got.ready) { $('tasks-count').textContent = ''; draw([...chosen].map(id => ({id}))); pager(); chosenLine(); return notReady(got); }
    say('');
    $('tasks-bar').hidden = false;
    st.ready = true; st.total = got.total; st.pages = got.pages; st.page = got.page;
    st.fields = got.fields || {};
    facets(got.categories);
    const m = $('tasks-match');
    if (m) { m.hidden = !writable || !got.total || st.showing; const n = got.total.toLocaleString('en-US'); m.textContent = got.total === got.of ? `Select all ${n}` : `Select the ${n} found`; }
    if ($('tasks-pick')) $('tasks-pick').hidden = !writable || !got.total || st.showing;
    if (st.showing) return showSelected();
    draw(got.tasks);
    $('tasks-count').textContent = `${count(got.total)}` + (got.total !== got.of ? ` of ${got.of.toLocaleString('en-US')}` : '')
      + (got.described ? '' : ' · its driver gives ids alone');
    pager(); chosenLine();
  }
  async function showSelected() {
    const ids = [...chosen];
    let got = {tasks: ids.map(id => ({id})), unknown: []};
    try { got = await (await post(route('rows'), {units: ids})).json(); } catch { /* the ids alone */ }
    if (page.isConnected === false) return;
    const unknown = new Set(got.unknown || []);
    draw([...got.tasks, ...[...unknown].map(id => ({id, unknown: true}))]);
    $('tasks-count').textContent = `${count(ids.length)} chosen` + (unknown.size ? ` · ${unknown.size} not in the listing` : '')
      + (picked() ? ` · picked at random with seed ${drawn.seed}` : '');
    pager(); chosenLine();
  }

  function setShowing(showing) {
    st.showing = showing;
    $('tasks-show').setAttribute('aria-pressed', String(showing));
    $('tasks-show').textContent = showing ? 'Show all' : 'Show selected';
  }
  let typing = null;
  $('tasks-q').addEventListener('input', e => { clearTimeout(typing); typing = setTimeout(() => { st.q = e.target.value.trim(); setShowing(false); st.page = 1; load(); }, 250); });
  $('tasks-cats').addEventListener('click', e => {
    const b = e.target.closest('[data-field]'); if (!b) return;
    const f = b.dataset.field, v = b.dataset.value, vs = st.cats[f] || [];
    st.cats[f] = b.dataset.all !== undefined ? [] : vs.includes(v) ? vs.filter(x => x !== v) : [...vs, v];
    if (!st.cats[f].length) delete st.cats[f];
    setShowing(false); st.page = 1; load();
  });
  // a click anywhere on a row ticks it, as its box would; the box, a link or a button keeps its own action, and a
  // drag that selects words is no click
  $('tasks-rows').addEventListener('click', e => {
    if (e.target.closest('input, a, button') || String(globalThis.getSelection?.() || '')) return;
    e.target.closest('tr[data-id]')?.querySelector('input[type=checkbox]')?.click();
  });
  $('tasks-rows').addEventListener('change', e => {
    const box = e.target.closest('input[type=checkbox]'); if (!box) return;
    const id = box.closest('tr').dataset.id;
    box.checked ? chosen.add(id) : chosen.delete(id); chosenLine(); changed();
  });
  // Another dataset in place: its tasks listed, its filters and its ticks; what this one ticked is kept for the set.
  // The address follows (back returns), and Build a run takes it.
  function switchTo(id, push = true) {
    if (!id || id === here || !pages[id]) return;
    keep();
    here = d.dataset = id;
    chosen.clear(); (parts[id] || []).forEach(u => chosen.add(u));
    drawn = null; clearTimeout(polling); asked = 0;
    Object.assign(st, {q: '', cats: {}, page: 1, ready: false, total: 0, pages: 1, fields: {}});
    setShowing(!!d.set && chosen.size > 0);
    $('tasks-q').value = ''; $('tasks-cats').innerHTML = ''; $('tasks-bar').hidden = true; $('tasks-rows').innerHTML = '';
    const select = $('tasks-dataset');
    if (select && select.value !== id) { select.value = id; select.dispatchEvent(new Event('change', {bubbles: true})); }
    const build = $('tasks-build');
    if (build) { const u = new URL(build.href, location.href); u.searchParams.set('dataset', id); build.href = u.pathname + u.search; }
    if (push) history.pushState({dataset: id}, '', pages[id].href);
    say(''); load();
  }
  $('tasks-dataset')?.addEventListener('change', e => switchTo(e.target.value));
  $('tasks-parts')?.addEventListener('click', e => { const b = e.target.closest('[data-part]'); if (b) switchTo(b.dataset.part); });
  globalThis.addEventListener?.('popstate', () => switchTo(location.pathname.split('/').filter(Boolean).pop(), false));
  $('tasks-size').addEventListener('change', e => { st.size = +e.target.value; st.page = 1; load(); });
  $('tasks-pages').addEventListener('click', e => {
    const b = e.target.closest('[data-page]'); if (!b) return;
    st.page += b.dataset.page === 'next' ? 1 : -1; load();
  });
  $('tasks-show').addEventListener('click', () => {
    setShowing(!st.showing); load();
  });
  $('tasks-match')?.addEventListener('click', async () => {
    const got = await (await nulliusFetch(query({ids: 1}))).json();
    if (page.isConnected === false) return;
    if (got.ok && got.ready) { got.ids.forEach(id => chosen.add(id)); changed(); load(); }
  });
  // Pick N at random: N of the matches, drawn on the server as set create --sample draws them; its seed is shown, and
  // kept in the set's note while the chosen are the ones drawn
  $('tasks-pick-go')?.addEventListener('click', async () => {
    let got;
    const seed = ($('tasks-seed') || {}).value?.trim();        // set create --seed: the same seed, the same draw
    try { got = await (await nulliusFetch(query({ids: 1, sample: $('tasks-n').value.trim(), ...(seed ? {seed} : {})}))).json(); }
    catch { return window.nulliusNotify?.('The tasks could not be drawn. Reload the page.', {error: true}); }
    if (!got.ok || !got.ready) return window.nulliusNotify?.(got.error || 'The list is not ready.', {error: true});
    chosen.clear(); got.ids.forEach(id => chosen.add(id));
    drawn = {ids: new Set(got.ids), seed: got.seed, note: got.note};
    if ($('tasks-seed')) $('tasks-seed').value = got.seed;                  // the draw's seed, to draw it again
    setShowing(true); load();
  });
  $('tasks-clear')?.addEventListener('click', () => { chosen.clear(); changed(); load(); });
  $('tasks-paste-add')?.addEventListener('click', async () => {
    const ids = [...new Set($('tasks-paste').value.split(/[\s,]+/).map(s => s.trim()).filter(s => s && !s.startsWith('#')))];
    ids.forEach(id => chosen.add(id)); changed();
    $('tasks-paste').value = ''; $('tasks-paste-open').open = false;
    window.nulliusNotify?.(`${count(ids.length)} added.`);
    load();
  });

  const msg = (text, bad) => { const el = $('tasks-msg'); if (el && page.isConnected !== false) { el.textContent = text; el.classList.toggle('bad', !!bad); } };
  async function save(body) {
    keep();
    if (!total()) return msg('Tick at least one task.', true);
    let got;
    if (picked() && Object.keys(parts).length === 1) body = {...body, note: drawn.note};
    try { got = await (await post(route('save'), {...body, parts})).json(); } catch { return msg('Not saved: the server did not answer.', true); }
    if (page.isConnected === false) return;
    if (!got.ok) return msg(got.error, true);
    try { globalThis.sessionStorage.removeItem(draftKey); } catch { /* none kept */ }
    location.href = got.open;
  }
  $('tasks-create')?.addEventListener('submit', e => {
    e.preventDefault(); const f = e.target;
    save({name: f.elements.namedItem('name').value.trim(), description: f.elements.namedItem('description').value.trim(),
      ...(d.version ? {version: d.version} : {})});
  });
  // the count and the way to save stay in view under the rows; the bar steps aside once the form is in view
  const bar = $('tasks-savebar'), form = page.querySelector('.tasks-save');
  if (bar && form && 'IntersectionObserver' in window)
    new IntersectionObserver(([e]) => bar.classList.toggle('tasks-gone', e.isIntersecting)).observe(form);
  $('tasks-to-save')?.addEventListener('click', e => {
    const field = form?.querySelector('input'); if (!field) return;
    e.preventDefault(); form.scrollIntoView({block: 'nearest'}); field.focus({preventScroll: true});
  });
  // a licence added in Settings, opened from the warning, counts as soon as Settings closes
  document.addEventListener('close', e => { if (e.target.id === 'settings-modal' && !st.ready) load(); }, true);
  setShowing(st.showing);
  chosenLine();
  load();
  return {chosen};
}
globalThis.nulliusTasks = mount;
const page = document.getElementById('tasks-page');
if (page && !page.dataset.manual) mount(page);
})();
