/* The Recipes page (templates/builder/recipes.html): each saved recipe step by step, and what the builder's own
   check of a loaded recipe says this Nullius cannot run of it - the recipe core's for a Harbor recipe (fromJSON,
   validate, providerBlocker), the compile of its Review step for a standard one, done by the server for the page. */
(function () {
  'use strict';
  const STEPS = ['benchmark', 'runner', 'dataset', 'agent', 'model', 'limits', 'where'];
  // {rows: [[step, title, value]], problems: [[step, message]]}; a recipe the core cannot read is its one problem.
  function described(C, raw, compiled) {
    let r;
    const benchmark = typeof raw.benchmark === 'string' ? raw.benchmark : '';
    try { r = C.fromJSON(raw); } catch (e) { return {rows: [], benchmark, problems: [[C.STEP.runner, e.message]]}; }
    const out = C.isStandard(r) ? compiled || {review: [], errors: []} : {review: C.review(r), errors: C.validate(r).errors};
    const blocked = C.providerBlocker(r);
    return {rows: out.review, benchmark, problems: [...out.errors.map(e => [e.step, e.message]), ...(blocked ? [[C.STEP.model, blocked]] : [])]};
  }
  // A step's cell: the values of its review rows, in order; with none (a recipe its check stopped early), the benchmark it names.
  const cell = (C, d, key) => d.rows.length ? d.rows.filter(([s]) => s === C.STEP[key]).map(([, , v]) => v).join(' · ') : key === 'benchmark' ? d.benchmark : '';
  const said = (C, problems) => problems.map(([s, m]) => C.STEPS[s] + ': ' + m).join('\n');
  // The first free name of a copy of `name`, among `taken`.
  function copyName(name, taken) {
    for (let n = 1; ; n++) { const t = name + (n === 1 ? ' (copy)' : ' (copy ' + n + ')'); if (!taken.includes(t)) return t; }
  }
  if (typeof module === 'object' && module.exports) { module.exports = {described, cell, said, copyName, STEPS}; return; }

  const page = JSON.parse(document.getElementById('rcp-data').textContent), schema = page.schema;
  const C = window.NulliusRecipe(schema), byId = Object.fromEntries(page.recipes.map(x => [x.id, x]));
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  const notify = (m, error = false) => window.nulliusNotify(m, {error});
  // A write's notice survives the reload that shows its result.
  const KEY = 'nullius.recipes.said';
  const reload = m => { try { sessionStorage.setItem(KEY, m); } catch {} location.reload(); };
  try { const m = sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY); if (m) notify(m); } catch {}
  const call = async (url, method, body) => {
    const res = await nulliusFetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': schema.csrf}, body: JSON.stringify(body)});
    const j = await res.json().catch(() => ({}));
    if (!res.ok || j.ok === false) throw Error(j.error || 'HTTP ' + res.status);
    return j;
  };

  // The cells, before the table toolkit reads the rows (tables.js wires them once the page is read).
  const info = {};
  for (const tr of document.querySelectorAll('#recipes tbody tr')) {
    const item = byId[tr.dataset.recipe], d = info[item.id] = described(C, item.recipe, page.compiled[item.id]);
    for (const key of STEPS) { const span = tr.querySelector(`[data-col="${key}"] .rcp-val`), v = cell(C, d, key); span.textContent = v; span.title = v; }
    if (d.problems.length) { const w = tr.querySelector('.rcp-warn'); w.hidden = false; w.title = said(C, d.problems); }
    tr.dataset.search = [item.name, ...STEPS.map(k => cell(C, d, k))].join(' ');
  }

  const dialog = document.getElementById('rcp-dialog');
  let opener = null;
  const show = (html, wide) => { dialog.innerHTML = html; dialog.classList.toggle('wide', wide); dialog.showModal(); };
  dialog.addEventListener('close', () => opener?.focus());
  dialog.addEventListener('click', e => {   // the backdrop, not the dialog's own padding
    const r = dialog.getBoundingClientRect();
    if (e.target === dialog && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) dialog.close();
  });
  const head = (t, x) => `<div class="std-how-head"><h2 id="rcp-dialog-h">${esc(t)}</h2>${x ? '<button type="button" class="std-how-x" data-close aria-label="Close"><svg class="ic" aria-hidden="true"><use href="#i-x"/></svg></button>' : ''}</div>`;
  function details(item) {
    const d = info[item.id], shown = STEPS.map(k => C.STEP[k]);
    const warn = d.problems.length ? `<ul class="rcp-problems">${d.problems.map(([s, m]) => `<li><svg class="ic" aria-hidden="true"><use href="#i-alert"/></svg><span><b>${esc(C.STEPS[s])}</b> ${esc(m)}</span></li>`).join('')}</ul>` : '';
    const steps = shown.map((s, n) => `<section class="rcp-step"><h3><em>0${n + 1}</em>${esc(C.STEPS[s])}</h3>${d.rows.filter(([i]) => i === s).map(([, t, v]) => `<div class="rcp-row"><small>${esc(t)}</small><span>${esc(v)}</span></div>`).join('')}</section>`).join('');
    show(head(item.name, true) + warn + steps + `<h3 class="rcp-json-h">JSON</h3><pre class="rcp-json">${esc(JSON.stringify(item.recipe, null, 2))}</pre>`, true);
  }
  function ask(item) {
    show(head('Delete recipe ' + item.name) + `<div class="launch-delete-acts"><button type="button" class="btn ghost" data-close autofocus>Keep it</button><button type="button" class="btn danger" data-delete="${esc(item.id)}">Delete recipe</button></div>`, false);
  }
  function download(item) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(item.recipe, null, 2) + '\n'], {type: 'application/json'})), a = document.createElement('a');
    a.href = url; a.download = (item.recipe.name || 'recipe') + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-act], [data-close], [data-delete], #rcp-import');
    if (!b || b.getAttribute('aria-disabled') === 'true') return;
    const item = byId[b.closest('[data-recipe]')?.dataset.recipe];
    if (b.dataset.close !== undefined) return dialog.close();
    if (b.dataset.act === 'info') { opener = b; return details(item); }
    if (b.dataset.act === 'delete') { opener = b; return ask(item); }
    if (b.dataset.act === 'export') return download(item);
    try {
      if (b.dataset.act === 'duplicate') {
        const name = copyName(item.name, page.recipes.map(x => x.name));
        await call('/new-run/recipes', 'POST', {recipe: {...item.recipe, title: name}});
        reload('Saved “' + name + '”.');
      } else if (b.dataset.delete) {
        await call('/new-run/recipes/' + encodeURIComponent(b.dataset.delete), 'DELETE', {});
        reload('Deleted “' + byId[b.dataset.delete].name + '”.');
      } else if (b.id === 'rcp-import') document.getElementById('rcp-file').click();
    } catch (err) { notify((b.dataset.delete ? 'Not deleted: ' : 'Not saved: ') + err.message, true); }
  });
  document.getElementById('rcp-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    let body;
    try { body = JSON.parse(await file.text()); } catch { return notify('Not imported: ' + file.name + ' is not JSON.', true); }
    try {
      const j = await call('/new-run/recipes/import', 'POST', body), n = j.added.length;
      const skipped = j.skipped.map(x => '“' + x.name + '”');
      reload([n ? 'Imported ' + n + ' recipe' + (n > 1 ? 's' : '') + '.' : '', skipped.length ? 'Already saved: ' + skipped.join(', ') + '.' : ''].filter(Boolean).join(' '));
    } catch (err) { notify(err.message, true); }
  });
})();
