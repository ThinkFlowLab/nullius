// A dataset's tasks (templates/catalogue/tasks.html): the server gives a page of 100 tasks for a search and category
// values (web/catalogue.py, standard/runner/units.py); this page keeps the chosen ids alone. The listing is asked for once the
// page has loaded: in the cache, it shows at once; one that can be made lightly is started out of the request and
// the page asks again until it is ready; one that needs the benchmark's full environment is never started here.
(() => {
  const page = document.getElementById('tasks-page');
  if (!page) return;
  const $ = id => document.getElementById(id), d = page.dataset, nouns = d.nouns || 'tasks';
  const writable = d.writable === 'true';
  const chosen = new Set(JSON.parse(($('tasks-chosen-ids') || {}).textContent || '[]'));
  // opened from a set: its members first; Show all browses the dataset
  const st = {q: '', cats: {}, page: 1, showing: !!d.set && chosen.size > 0, ready: false, total: 0, pages: 1};
  const label = field => field.charAt(0).toUpperCase() + field.slice(1).replace(/_/g, ' ');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  // the listing at the version the page shows (data-version, empty for the benchmark's default)
  const at = url => d.version ? url + (url.includes('?') ? '&' : '?') + 'version=' + encodeURIComponent(d.version) : url;
  const post = (url, body) => nulliusFetch(at(url), {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': d.csrf}, body: JSON.stringify(body)});
  const count = n => `${n.toLocaleString('en-US')} ${n === 1 ? nouns.replace(/s$/, '') : nouns}`;

  function say(text, warn) {
    const el = $('tasks-state'); el.hidden = !text; el.textContent = text || ''; el.classList.toggle('tasks-warn', !!warn);
  }
  function chosenLine() {
    $('tasks-chosen').textContent = `${chosen.size.toLocaleString('en-US')} chosen`;
    if ($('tasks-bar-chosen')) $('tasks-bar-chosen').textContent = $('tasks-chosen').textContent;
    $('tasks-show').disabled = !chosen.size && !st.showing;
  }
  // a task on one line: its title cut to the width, the whole text in the row's tooltip and, while its box (its text
  // where the box is disabled) has the keyboard's focus, in place; its id beside it on hover or focus. A click
  // anywhere on the row ticks it.
  function row(t) {
    const tags = Object.values(t.categories || {}).map(v => `<span class="cat-tag">${esc(v)}</span>`).join(' ');
    const whole = t.text || t.title || t.id;
    return `<tr data-id="${esc(t.id)}" title="${esc(whole)}"><td class="tasks-tick"><input type="checkbox" aria-label="Choose ${esc(t.id)}"${chosen.has(t.id) ? ' checked' : ''}${writable ? '' : ' disabled'}></td>`
      + `<td class="tasks-text"><span class="tasks-row"${writable ? '' : ' tabindex="0"'}><span class="tasks-title">${esc(t.title || t.id)}</span>`
      + `<span class="tasks-whole">${esc(whole)}</span>${t.title ? `<small class="tasks-id mono faint">${esc(t.id)}</small>` : ''}</span>`
      + `${t.unknown ? '<small class="warn">not in the listing</small>' : ''}`
      + (tags ? `<span class="tasks-tags">${tags}</span>` : '') + `</td><td class="tasks-catcol">${tags}</td></tr>`;
  }
  function draw(tasks) {
    $('tasks-rows').innerHTML = tasks.map(row).join('');
    $('tasks-table').hidden = !tasks.length;
    const fields = [...new Set(tasks.flatMap(t => Object.keys(t.categories || {})))];
    $('tasks-cat-head').hidden = !fields.length;
    $('tasks-cat-head').textContent = fields.map(label).join(' · ');
    document.querySelectorAll('#tasks-rows td.tasks-catcol').forEach(td => { td.hidden = !fields.length; });
  }
  function facets(cats) {
    $('tasks-cats').innerHTML = Object.entries(cats || {}).map(([field, values]) =>
      `<div class="filter-pills" role="group" aria-label="${esc(label(field))}"><span class="tasks-catname">${esc(label(field))}</span>`
      + values.map(v => `<button type="button" class="filter-pill" data-field="${esc(field)}" data-value="${esc(v.value)}" aria-pressed="${(st.cats[field] || []).includes(v.value)}">${esc(v.value)} <span class="tasks-n">${v.count}</span></button>`).join('')
      + '</div>').join('');
  }
  function pager() {
    const nav = $('tasks-pages');
    nav.hidden = st.showing || st.pages <= 1;
    $('tasks-page-of').textContent = `Page ${st.page} of ${st.pages}`;
    nav.querySelector('[data-page=prev]').disabled = st.page <= 1;
    nav.querySelector('[data-page=next]').disabled = st.page >= st.pages;
  }
  function query(extra) {
    const u = new URLSearchParams();
    if (st.q) u.set('q', st.q);
    Object.entries(st.cats).forEach(([f, vs]) => vs.forEach(v => u.append('cat', `${f}=${v}`)));
    Object.entries(extra || {}).forEach(([k, v]) => u.set(k, v));
    if (d.version) u.set('version', d.version);
    return d.listing + '?' + u;
  }
  let polling = null, asked = 0;
  // not in the cache: the server's line for its state; a light listing started once, out of any request, then asked
  // again every few seconds until it is ready
  function notReady(got) {
    if ($('tasks-match')) $('tasks-match').hidden = true;
    const light = got.state === 'light' && !got.error;
    say(got.say, !light);
    if (!light) return;
    clearTimeout(polling);
    if (!got.preparing && d.local !== 'true') return say('The list is prepared from a browser on the application host. Ids can still be pasted.', true);
    if (!got.preparing && !asked++)
      return post(d.prepare, {}).then(r => r.json()).then(g => { say(g.say || got.say); polling = setTimeout(load, 2500); })
        .catch(() => say('The list could not be started. Reload the page.', true));
    polling = setTimeout(load, 2500);
  }
  async function load() {
    let got;
    try { got = await (await nulliusFetch(query({page: st.page}))).json(); } catch { return say('The list could not be read. Reload the page.', true); }
    if (!got.ok) return say(got.error || 'The list could not be read.', true);
    if (!got.ready) { $('tasks-count').textContent = ''; draw([...chosen].map(id => ({id}))); pager(); chosenLine(); return notReady(got); }
    say('');
    st.ready = true; st.total = got.total; st.pages = got.pages; st.page = got.page;
    facets(got.categories);
    const m = $('tasks-match');
    if (m) { m.hidden = !writable || !got.total || st.showing; m.textContent = `Select the ${got.total.toLocaleString('en-US')} that match`; }
    if (st.showing) return showSelected();
    draw(got.tasks);
    $('tasks-count').textContent = `${count(got.total)}` + (got.total !== got.of ? ` of ${got.of.toLocaleString('en-US')}` : '')
      + (got.described ? '' : ' · its driver gives ids alone');
    pager(); chosenLine();
  }
  async function showSelected() {
    const ids = [...chosen];
    let got = {tasks: ids.map(id => ({id})), unknown: []};
    try { got = await (await post(d.rows, {units: ids})).json(); } catch { /* the ids alone */ }
    const unknown = new Set(got.unknown || []);
    draw([...got.tasks, ...[...unknown].map(id => ({id, unknown: true}))]);
    $('tasks-count').textContent = `${count(ids.length)} chosen` + (unknown.size ? ` · ${unknown.size} not in the listing` : '');
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
    st.cats[f] = vs.includes(v) ? vs.filter(x => x !== v) : [...vs, v];
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
    box.checked ? chosen.add(id) : chosen.delete(id); chosenLine();
  });
  $('tasks-pages').addEventListener('click', e => {
    const b = e.target.closest('[data-page]'); if (!b) return;
    st.page += b.dataset.page === 'next' ? 1 : -1; load();
  });
  $('tasks-show').addEventListener('click', () => {
    setShowing(!st.showing); load();
  });
  $('tasks-match')?.addEventListener('click', async () => {
    const got = await (await nulliusFetch(query({ids: 1}))).json();
    if (got.ok && got.ready) { got.ids.forEach(id => chosen.add(id)); load(); }
  });
  $('tasks-clear')?.addEventListener('click', () => { chosen.clear(); load(); });
  $('tasks-paste-add')?.addEventListener('click', async () => {
    const ids = [...new Set($('tasks-paste').value.split(/[\s,]+/).map(s => s.trim()).filter(s => s && !s.startsWith('#')))];
    ids.forEach(id => chosen.add(id));
    $('tasks-paste').value = ''; $('tasks-paste').closest('details').open = false;
    window.nulliusNotify?.(`${count(ids.length)} added.`);
    load();
  });

  const msg = (text, bad) => { const el = $('tasks-msg'); if (el) { el.textContent = text; el.classList.toggle('bad', !!bad); } };
  async function save(body) {
    if (!chosen.size) return msg('Tick at least one task.', true);
    let got;
    try { got = await (await post(d.save, {...body, units: [...chosen]})).json(); } catch { return msg('Not saved: the server did not answer.', true); }
    if (!got.ok) return msg(got.error, true);
    location.href = got.open;
  }
  $('tasks-create')?.addEventListener('submit', e => {
    e.preventDefault(); const f = e.target;
    save({name: f.elements.namedItem('name').value.trim(), description: f.elements.namedItem('description').value.trim(),
      ...(d.version ? {version: d.version} : {})});
  });
  // the count and the way to save stay in view under the rows; the bar steps aside once the form is in view
  const bar = $('tasks-savebar'), form = document.querySelector('.tasks-save');
  if (bar && form && 'IntersectionObserver' in window)
    new IntersectionObserver(([e]) => bar.classList.toggle('tasks-gone', e.isIntersecting)).observe(form);
  $('tasks-to-save')?.addEventListener('click', e => {
    const field = form?.querySelector('input'); if (!field) return;
    e.preventDefault(); form.scrollIntoView({block: 'nearest'}); field.focus({preventScroll: true});
  });
  setShowing(st.showing);
  chosenLine();
  load();
})();
