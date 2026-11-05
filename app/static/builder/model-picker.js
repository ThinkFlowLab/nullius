// Provider model IDs stay verbatim; browsing a catalogue never chooses a model.
(() => {
  const root = document.getElementById('nullius-builder');
  if (!root) return;
  const schema = JSON.parse(document.getElementById('nb-schema').textContent);
  const cache = new Map(); let active = null, serial = 0, choosing = false;
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
  function choose(model) {
    const input = active.input;
    close(); choosing = true; input.value = model.id;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    input.focus(); choosing = false;
  }
  function paint() {
    if (!active) return;
    const {menu, input, result, query} = active;
    menu.replaceChildren(); active.items = []; active.index = -1;
    const status = document.createElement('div'); status.className = 'nb-model-status'; status.setAttribute('role', 'status');
    if (!result) status.textContent = 'Loading models…';
    else if (result.ok && input.dataset.field === 'model' && JSON.parse(root.dataset.paperModels || 'null') !== null && !result.models.length) status.textContent = 'No paper models available from this provider.';
    else if (!result.ok) status.textContent = result.message || 'Model list unavailable. Enter a model ID manually.';
    else {
      const matches = result.models.filter(m => (m.id + ' ' + m.name).toLowerCase().includes(query.toLowerCase()));
      active.items = matches.slice(0, 100);
      status.textContent = matches.length ? `${matches.length} model${matches.length === 1 ? '' : 's'}${matches.length > 100 ? ' · Type to narrow the list' : ''}` : 'No matching models. You can enter an exact model ID.';
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
      entry = {at: Date.now(), promise: fetch('/new-run/models', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': schema.csrf}, body: JSON.stringify({provider})})
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
    menu.setAttribute('popover', 'manual'); menu.setAttribute('role', 'listbox'); menu.setAttribute('aria-label', 'Provider models');
    document.body.append(menu);
    const state = {input, menu, wrap: input.parentElement, provider: root.dataset.modelProvider, query: all ? '' : input.value, result: null, items: [], index: -1};
    active = state; input.setAttribute('aria-expanded', 'true'); paint();
    if (menu.showPopover) menu.showPopover();
    const paper = input.dataset.field === 'model' ? JSON.parse(root.dataset.paperModels || 'null') : null;
    const result = await models(state.provider);
    state.result = paper === null || !result.ok ? result : {...result, models:Object.entries(paper)
      .filter(([, served])=>result.models.some(m=>m.id === served))
      .map(([id, served])=>({id:root.dataset.standardModel === 'true' ? id : served, name:id === served ? id : id + ' · ' + served}))};
    if (active === state) paint();
  }
  function scan() {
    if (active && (!active.input.isConnected || active.provider !== root.dataset.modelProvider)) close();
    root.querySelectorAll('input[list="nb-models"]').forEach(input => {
      input.removeAttribute('list'); input.dataset.providerModel = 'true'; input.autocomplete = 'off';
      input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-controls', 'provider-models-' + (++serial));
      const wrap = document.createElement('span'); wrap.className = 'nb-model-control'; input.before(wrap); wrap.append(input);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'nb-model-open'; button.textContent = '⌄'; button.setAttribute('aria-label', 'Browse provider models');
      button.addEventListener('click', e => { e.preventDefault(); input.focus(); void open(input, true); }); wrap.append(button);
      input.addEventListener('focus', () => { if(!choosing)void open(input); });
      input.addEventListener('input', () => { if(choosing)return; if (active?.input === input) { active.query = input.value; paint(); } else void open(input); });
      input.addEventListener('keydown', e => {
        if (e.key === 'Escape' || e.key === 'Tab') { if(e.key === 'Escape' && active){e.preventDefault();e.stopPropagation();} close(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if(active?.input !== input)void open(input, true); else highlight(active.index + (e.key === 'ArrowDown' ? 1 : -1)); }
        if (e.key === 'Enter' && active?.input === input && active.items[active.index]) { e.preventDefault(); choose(active.items[active.index]); }
      });
    });
  }
  new MutationObserver(scan).observe(root, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-model-provider']});
  document.addEventListener('pointerdown', e => { if(active && !active.wrap.contains(e.target) && !active.menu.contains(e.target))close(); });
  document.addEventListener('scroll', e => { if(active && !active.menu.contains(e.target))close(); }, true);
  window.addEventListener('resize', close);
  scan();
})();
