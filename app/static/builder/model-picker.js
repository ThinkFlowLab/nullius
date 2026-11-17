// Provider model IDs stay verbatim; browsing a catalogue never chooses a model.
// The same picker serves a model's Served by (list="nb-served"): its rows are the providers of the model the field
// names (data-served-model), and a field of several takes each pick, or a slug typed as is, into its list.
(() => {
  // the rows a typed query keeps: the query in the id or the name, case aside; the ones already chosen left out
  const matching = (items, query, chosen = []) => items.filter(m => !chosen.includes(m.id) && (m.id + ' ' + m.name).toLowerCase().includes(query.toLowerCase()));
  // what Enter takes: the highlighted row, else, in a field of several, the text as typed (a slug the list may lack)
  const entered = (items, index, text, several) => items[index] || (several && text.trim() ? {id: text.trim(), typed: true} : null);
  // The row and its menu share one request per model for five minutes, including failures. An in-flight request
  // stays shared until it settles; result is absent while the row is waiting to learn how many entries there are.
  function servingProviders(fetch, csrf) {
    const cache = new Map(), listeners = new Set();
    const providers = model => {
      let entry = cache.get(model);
      if (!entry || (entry.result && Date.now() - entry.at > 300000)) {
        entry = {at: Date.now(), result: null};
        entry.promise = fetch('/new-run/endpoints', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({provider: 'openrouter', model})})
          .then(r => r.json()).then(r => ({...r, models: (Array.isArray(r.endpoints) ? r.endpoints : []).map(e => ({id: e.slug, name: e.name + (e.quantization ? ' · ' + e.quantization : '')}))}))
          .catch(() => ({ok: false, models: [], message: "Provider list unavailable. Enter a provider's slug by hand."}))
          .then(result => { entry.at = Date.now(); entry.result = result; listeners.forEach(listener => listener(model)); return result; });
        cache.set(model, entry);
      }
      return entry;
    };
    providers.subscribe = listener => listeners.add(listener);
    return providers;
  }
  if (typeof module === 'object' && module.exports) { module.exports = {matching, entered, servingProviders}; return; }
  const root = document.getElementById('nullius-builder');
  if (!root) return;
  const schema = JSON.parse(document.getElementById('nb-schema').textContent);
  const providers = window.NulliusServingProviders = servingProviders(nulliusFetch, schema.csrf);
  const cache = new Map(); let active = null, serial = 0, choosing = false;
  const severalOf = input => input.dataset.pickerSeveral === 'true';
  function close() {
    if (!active) return;
    active.input.setAttribute('aria-expanded', 'false');
    active.input.removeAttribute('aria-activedescendant');
    active.menu.remove(); active = null;
  }
  function position() {
    if (!active) return;
    const {menu, wrap} = active, rect = wrap.getBoundingClientRect();
    menu.style.width = Math.min(Math.max(rect.width, 240), innerWidth - 16) + 'px';
    menu.style.left = Math.max(8, Math.min(rect.left, innerWidth - parseFloat(menu.style.width) - 8)) + 'px';
    const below = innerHeight - rect.bottom - 12, above = rect.top - 12, down = below >= 220 || below >= above;
    menu.style.maxHeight = Math.max(60, Math.min(320, down ? below : above)) + 'px';
    menu.style.top = down ? rect.bottom + 5 + 'px' : 'auto';
    menu.style.bottom = down ? 'auto' : innerHeight - rect.top + 5 + 'px';
  }
  function highlight(index) {
    if (!active) return;
    const rows = [...active.menu.querySelectorAll('[role=option]')];
    active.index = Math.max(0, Math.min(rows.length - 1, index));
    rows.forEach((row, i) => row.classList.toggle('is-active', i === active.index));
    const row = rows[active.index];
    if (row) { active.input.setAttribute('aria-activedescendant', row.id); row.scrollIntoView({block: 'nearest'}); }
  }
  function choose(model, input = active.input) {
    close(); choosing = true;
    // a field of several hands the pick to the page, which lists it, and is left empty for the next one
    if (severalOf(input)) { input.value = ''; input.dispatchEvent(new CustomEvent('nb-pick', {bubbles: true, detail: model.id})); }
    else {
      input.value = model.id;
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }
    input.focus(); choosing = false;
  }
  function paint() {
    if (!active) return;
    const {menu, input, result, query, several} = active;
    menu.replaceChildren(); active.items = []; active.index = -1;
    const status = document.createElement('div'); status.className = 'nb-model-status'; status.setAttribute('role', 'status');
    // no model yet, or the one the run sends not yet known (the server compiles it): no list is asked
    if (several && !input.dataset.servedModel) status.textContent = input.dataset.servedPending === 'true' ? 'Loading providers…' : 'Choose the model first';
    else if (!result) status.textContent = several ? 'Loading providers…' : 'Loading models…';
    else if (result.ok && input.dataset.field === 'model' && JSON.parse(root.dataset.paperModels || 'null') !== null && !result.models.length) status.textContent = 'No paper models available from this provider.';
    else if (!result.ok) status.textContent = result.message || (several ? "Provider list unavailable. Enter a provider's slug by hand." : 'Model list unavailable. Enter a model ID manually.');
    else {
      const matches = matching(result.models, query, several ? (input.dataset.chosen || '').split(',').filter(Boolean) : []);
      active.items = matches.slice(0, 100);
      const noun = several ? 'provider' : 'model';
      status.textContent = matches.length ? `${matches.length} ${noun}${matches.length === 1 ? '' : 's'}${matches.length > 100 ? ' · Type to narrow the list' : ''}`
        : several ? (result.models.length ? 'No matching provider. Enter takes the slug as typed.' : 'No provider listed: type a slug') : 'No matching models. You can enter an exact model ID.';
    }
    menu.append(status);
    active.items.forEach((model, i) => {
      const row = document.createElement('div'); row.className = 'ui-select-option nb-model-option';
      row.id = menu.id + '-' + i; row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(model.id === input.value));
      const name = document.createElement('span'); name.textContent = model.name;
      row.append(name);
      if (model.id !== model.name) { const id = document.createElement('small'); id.textContent = model.id; row.append(id); }
      row.addEventListener('pointerdown', e => e.preventDefault());
      row.addEventListener('click', () => choose(model)); menu.append(row);
    });
    position();
  }
  async function models(provider) {
    let entry = cache.get(provider);
    if (!entry || Date.now() - entry.at > 300000) {
      entry = {at: Date.now(), promise: nulliusFetch('/new-run/models', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': schema.csrf}, body: JSON.stringify({provider})})
        .then(r => r.json()).then(r => ({...r, models: Array.isArray(r.models) ? r.models : []}))
        .catch(() => ({ok: false, models: [], message: 'Model list unavailable. Enter a model ID manually.'}))};
      cache.set(provider, entry);
    }
    const result = await entry.promise;
    if (!result.ok) cache.delete(provider);
    return result;
  }
  async function open(input, all = false) {
    close();
    const menu = document.createElement('div'); menu.className = 'ui-select-menu nb-model-menu'; menu.id = input.getAttribute('aria-controls');
    const several = severalOf(input);
    menu.setAttribute('popover', 'manual'); menu.setAttribute('role', 'listbox'); menu.setAttribute('aria-label', several ? 'Serving providers' : 'Provider models');
    document.body.append(menu);
    const state = {input, menu, wrap: input.parentElement, several, provider: root.dataset.modelProvider, model: input.dataset.servedModel || '', query: all ? '' : input.value, result: null, items: [], index: -1};
    active = state; input.setAttribute('aria-expanded', 'true'); paint();
    if (menu.showPopover) menu.showPopover();
    if (several) {
      if (!state.model) return;
      const result = await providers(state.model).promise;
      if (active === state) { state.result = result; paint(); }
      return;
    }
    const paper = input.dataset.field === 'model' ? JSON.parse(root.dataset.paperModels || 'null') : null;
    const result = await models(state.provider);
    state.result = paper === null || !result.ok ? result : {...result, models:Object.entries(paper)
      .filter(([, served])=>result.models.some(m=>m.id === served))
      .map(([id, served])=>({id:root.dataset.standardModel === 'true' ? id : served, name:id === served ? id : id + ' · ' + served}))};
    if (active === state) paint();
  }
  function scan() {
    if (active && (!active.input.isConnected || active.input.closest('[hidden]') || active.provider !== root.dataset.modelProvider)) close();
    // the model a Served by lists the providers of has moved (the server named the one the run sends): its rows follow
    else if (active && active.several && active.model !== (active.input.dataset.servedModel || '')) void open(active.input, !active.query);
    root.querySelectorAll('input[list="nb-models"], input[list="nb-served"]').forEach(input => {
      if (input.getAttribute('list') === 'nb-served') input.dataset.pickerSeveral = 'true'; else input.dataset.providerModel = 'true';
      input.removeAttribute('list'); input.autocomplete = 'off';
      input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-controls', 'provider-models-' + (++serial));
      const wrap = document.createElement('span'); wrap.className = 'nb-model-control'; input.before(wrap); wrap.append(input);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'nb-model-open'; button.textContent = '⌄';
      button.setAttribute('aria-label', severalOf(input) ? 'Browse the serving providers' : 'Browse provider models');
      button.addEventListener('click', e => { e.preventDefault(); input.focus(); void open(input, true); }); wrap.append(button);
      input.addEventListener('focus', () => { if(!choosing)void open(input); });
      // a field of several keeps the keyboard after a pick: a click on it opens its menu again for the next one
      if (severalOf(input)) input.addEventListener('click', () => { if(active?.input !== input)void open(input); });
      input.addEventListener('input', () => { if(choosing)return; if (active?.input === input) { active.query = input.value; paint(); } else void open(input); });
      input.addEventListener('keydown', e => {
        if (e.key === 'Escape' || e.key === 'Tab') { if(e.key === 'Escape' && active){e.preventDefault();e.stopPropagation();} close(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if(active?.input !== input)void open(input, true); else highlight(active.index + (e.key === 'ArrowDown' ? 1 : -1)); }
        if (e.key === 'Enter') {
          const pick = entered(active?.input === input ? active.items : [], active?.input === input ? active.index : -1, input.value, severalOf(input));
          if (!pick) return;
          e.preventDefault(); choose(pick, input);
        }
      });
    });
  }
  new MutationObserver(scan).observe(root, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-model-provider', 'data-served-model']});
  document.addEventListener('pointerdown', e => { if(active && !active.wrap.contains(e.target) && !active.menu.contains(e.target))close(); });
  document.addEventListener('scroll', e => { if(active && !active.menu.contains(e.target))close(); }, true);
  window.addEventListener('resize', close);
  scan();
})();
