/* Source evidence is always inserted as text, never interpreted as markup. */
(() => {
  'use strict';
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const button = (label, action, disabled = false, className = 'btn sm') => {
    const node = el('button', className, label);
    node.type = 'button'; node.disabled = disabled;
    node.addEventListener('click', action);
    return node;
  };
  const number = value => Number(value).toLocaleString();
  const readJSON = async (url, signal) => {
    const response = await fetch(url, {signal, headers: {'Accept': 'application/json'}, cache: 'no-store'});
    if (!response.ok) throw new Error(response.status === 404 ? 'This evidence is no longer in the current workspace or trace view.' : 'Could not load this evidence. Try again.');
    return response.json();
  };
  // A tool call's sections are keyed call-<n>-arguments / call-<n>-observation.
  const callOf = key => { const m = /^call-(\d+)-/.exec(key || ''); return m ? Number(m[1]) : null; };
  const one = text => String(text || '').split(/\s+/).filter(Boolean).join(' ');
  function renderContent(host, data, change) {
    const sections = el('div', 'reader-sections');
    sections.setAttribute('role', 'group'); sections.setAttribute('aria-label', 'Recorded content');
    // Each call is a block: the command it ran, whole, then its tabs at a fixed size.
    const calls = el('div', 'reader-calls');
    const blocks = new Map();
    const callCount = new Set(data.sections.map(s => callOf(s.key)).filter(n => n !== null)).size;
    const tab = section => {
      const item = button(section.label, () => change(section.key, 0), false, 'reader-section');
      item.setAttribute('aria-pressed', String(data.content?.key === section.key));
      return item;
    };
    data.sections.forEach(section => {
      const call = callOf(section.key);
      if (call === null) { sections.append(tab(section)); return; }
      if (!blocks.has(call)) {
        const block = el('div', 'reader-call');
        const tabs = el('div', 'reader-call-tabs');
        const name = callCount > 1 ? `Call ${call + 1}` : 'Call';
        tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', name);
        if (section.tool_call_id) block.title = `Tool call ${section.tool_call_id}`;
        const foot = el('div', 'reader-call-foot');
        foot.append(el('span', 'reader-call-name', name + (section.failure ? ' · failed' : '')), tabs);
        block.append(el('p', 'reader-call-command mono', section.tool), foot);
        calls.append(block); blocks.set(call, {block, tabs});
      }
      const {block, tabs} = blocks.get(call);
      const item = tab(section);
      item.setAttribute('aria-label', `${callCount > 1 ? `Call ${call + 1} ` : ''}${section.label}`);
      if (section.failure && section.key.endsWith('-observation')) item.classList.add('bad');
      if (data.content?.key === section.key) block.classList.add('selected');
      tabs.append(item);
    });
    if (sections.childElementCount) host.append(sections);
    if (calls.childElementCount) host.append(calls);
    const content = data.content;
    if (!content) { host.append(el('p', 'muted', 'No content recorded for this selection.')); return; }
    const call = callOf(content.key);
    const block = el('div', 'reader-content');
    const heading = el('div', 'reader-content-head');
    heading.append(el('h4', '', call !== null && callCount > 1 ? `Call ${call + 1} · ${content.label}` : content.label), el('span', 'mono', `${number(content.characters)} characters`));
    block.append(heading);
    if (content.tool_call_id !== undefined && content.tool_call_id !== null) block.append(el('p', 'reader-call-id mono', `Tool call ID · ${content.tool_call_id}`));
    // The target is left out when it only repeats the command shown above.
    const target = one(content.target).replace(/…$/, '');
    if (call !== null) block.append(el('p', 'reader-call-id', [content.category, target && !one(content.tool).startsWith(target) ? content.target : '', `${number(content.observation_characters)} result characters`].filter(Boolean).join(' · ')));
    if (content.failure) block.append(el('p', 'small bad', `Recorded failure signal · ${content.failure}`));
    if (content.text) {
      const pre = el('pre', '', content.text); pre.tabIndex = 0; pre.setAttribute('aria-label', content.label);
      block.append(pre);
    } else block.append(el('p', 'muted', content.label === 'Result' ? 'No result recorded for this call.' : 'No content recorded.'));
    const nav = el('nav', 'reader-content-nav'); nav.setAttribute('aria-label', 'Content pages');
    nav.append(button('← Previous', () => change(content.key, content.chunk - 1), content.chunk === 0),
      el('span', '', `Part ${number(content.chunk + 1)} / ${number(content.chunks)} · ${number(content.start + (content.characters ? 1 : 0))}–${number(content.end)}`),
      button('Next →', () => change(content.key, content.chunk + 1), content.chunk + 1 >= content.chunks));
    block.append(nav); host.append(block);
  }

  const reader = document.getElementById('conversation-reader');
  let conversationStarted = false;
  let loadConversation = () => {};
  if (reader?.querySelector('.reader-layout')) {
    const urlState = new URL(location.href).searchParams;
    let state = {q: urlState.get('c_q') || '', kind: urlState.get('c_kind') || 'all',
      page: urlState.get('c_page') || 1, position: urlState.get('c_position'),
      section: urlState.get('c_section'), chunk: urlState.get('c_chunk') || 0};
    let controller, latest;
    const search = reader.querySelector('.reader-search');
    const searchInput = search.querySelector('input'); searchInput.value = state.q;
    const status = reader.querySelector('.reader-status');
    const selectedHost = reader.querySelector('.reader-selected');
    const indexHost = reader.querySelector('.reader-rows');
    const pages = reader.querySelector('.reader-pages');
    const change = patch => { state = {...state, ...patch}; loadConversation(); };
    loadConversation = async () => {
      conversationStarted = true;
      controller?.abort(); controller = new AbortController();
      const signal = controller.signal;
      reader.setAttribute('aria-busy', 'true'); status.textContent = 'Loading recorded evidence…';
      const url = new URL(reader.dataset.endpoint, location.href);
      Object.entries(state).forEach(([key, value]) => { if (value !== null && value !== undefined && value !== '') url.searchParams.set(key, value); });
      try {
        const data = await readJSON(url, signal);
        if (signal.aborted) return;
        latest = data;
        state.q = data.query; state.kind = data.kind;
        state.page = data.page; state.position = data.selected?.position ?? null;
        state.section = data.selected?.content?.key ?? null; state.chunk = data.selected?.content?.chunk ?? 0;
        state.step = null;
        const current = new URL(location.href);
        Object.entries(state).forEach(([key, value]) => {
          if (key === 'step') return;
          if (value !== null && value !== undefined && value !== '') current.searchParams.set(`c_${key}`, value);
          else current.searchParams.delete(`c_${key}`);
        });
        history.replaceState(null, '', current);
        status.textContent = `${number(data.matches)} of ${number(data.total)} source steps${data.query ? ` match “${data.query}”` : ''}. ${data.notice || 'Select a row to keep its message and tools together.'}`;
        reader.querySelectorAll('[data-kind]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.kind === data.kind)));
        indexHost.replaceChildren();
        data.rows.forEach(row => {
          const item = button('', () => change({position: row.position, section: null, chunk: 0}), false, 'reader-row');
          item.setAttribute('aria-current', String(row.position === data.selected?.position));
          const meta = el('span', 'reader-row-meta');
          meta.append(el('span', 'mono', `#${row.step_id}`), el('span', '', row.source || 'Unspecified'));
          if (row.tools) meta.append(el('span', '', `${row.tools} tool${row.tools === 1 ? '' : 's'}`));
          if (row.errors) meta.append(el('span', 'reader-error', `${row.errors} !`));
          item.append(meta, el('span', 'reader-row-preview', row.preview)); indexHost.append(item);
        });
        pages.querySelector('span').textContent = `${data.page} / ${data.pages}`;
        pages.querySelector('[data-page=previous]').disabled = data.page <= 1;
        pages.querySelector('[data-page=next]').disabled = data.page >= data.pages;
        selectedHost.replaceChildren();
        const chosen = data.selected;
        if (!chosen) { selectedHost.append(el('p', 'muted', data.notice || 'No steps match.')); return; }
        const header = el('div', 'reader-step-nav');
        const info = el('div'); info.append(el('h3', '', `Step ${chosen.step_id} · ${chosen.source || 'Unspecified source'}`));
        const facts = [`${chosen.match_number} of ${data.matches} matching steps`];
        if (chosen.timestamp) facts.push(chosen.timestamp);
        if (chosen.duration !== null) facts.push(`${chosen.duration.toFixed(2)}s until the next source step`);
        info.append(el('p', '', facts.join(' · ')));
        const actions = el('div', 'reader-step-actions');
        actions.append(button('← Previous', () => change({position: chosen.previous, section: null, chunk: 0}), chosen.previous === null), button('Next →', () => change({position: chosen.next, section: null, chunk: 0}), chosen.next === null));
        header.append(info, actions); selectedHost.append(header);
        renderContent(selectedHost, chosen, (section, chunk) => change({section, chunk}));
      } catch (error) {
        if (error.name !== 'AbortError') {
          status.textContent = error.message; selectedHost.replaceChildren(el('p', 'muted', error.message), button('Retry', () => loadConversation()));
        }
      } finally { if (!signal.aborted) reader.removeAttribute('aria-busy'); }
    };
    search.addEventListener('submit', event => { event.preventDefault(); change({q: searchInput.value.trim(), position: null, page: 1, section: null, chunk: 0}); });
    reader.querySelector('[data-clear-search]').addEventListener('click', () => { searchInput.value = ''; change({q: '', position: null, page: 1, section: null, chunk: 0}); });
    reader.querySelectorAll('[data-kind]').forEach(node => node.addEventListener('click', () => change({kind: node.dataset.kind, position: null, page: 1, section: null, chunk: 0})));
    reader.querySelector('.reader-jump').addEventListener('submit', event => { event.preventDefault(); const value = event.currentTarget.querySelector('input').value.trim(); if (value) change({step: value, position: null, section: null, chunk: 0}); });
    pages.querySelectorAll('[data-page]').forEach(node => node.addEventListener('click', () => { if (latest) change({page: latest.page + (node.dataset.page === 'next' ? 1 : -1), position: null, section: null, chunk: 0}); }));
  }

  const tabs = Array.from(document.querySelectorAll('.trial-tabs [role=tab]'));
  function openTab(name, update = true) {
    if (!tabs.some(tab => tab.dataset.tab === name)) name = 'overview';
    tabs.forEach(tab => { const active = tab.dataset.tab === name; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; });
    document.querySelectorAll('.trial-tab').forEach(panel => { panel.hidden = panel.id !== `trial-${name}`; });
    if (update) { const url = new URL(location.href); url.searchParams.set('tab', name); if (name !== 'trace') url.hash = ''; history.replaceState(null, '', url); }
    if (name === 'conversation' && !conversationStarted) loadConversation();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => openTab(tab.dataset.tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); tabs[next].focus(); openTab(tabs[next].dataset.tab); }
    });
  });
  openTab(location.hash.startsWith('#span=') ? 'trace' : new URL(location.href).searchParams.get('tab') || 'overview', false);

  let spanController;
  window.NulliusEvidence = {
    selectSpan: async (host, id, section = null, chunk = 0) => {
      spanController?.abort(); spanController = new AbortController();
      const signal = spanController.signal;
      host.setAttribute('aria-busy', 'true'); host.replaceChildren(el('p', 'muted', 'Loading selected span…'));
      const url = new URL(host.dataset.endpoint, location.href);
      new URL(location.href).searchParams.forEach((value, key) => { if (['trace', 'hide', 'hide_span'].includes(key)) url.searchParams.append(key, value); });
      url.searchParams.set('span', id); if (section) url.searchParams.set('section', section); url.searchParams.set('chunk', chunk);
      const reload = (part, page) => window.NulliusEvidence.selectSpan(host, id, part, page);
      try {
        const data = await readJSON(url, signal); if (signal.aborted) return;
        host.replaceChildren();
        const badges = el('div', 'wf-badges');
        [data.kind, data.timing_quality, data.source, ...(data.merged ? ['merged'] : [])].filter(Boolean).forEach(text => badges.append(el('span', 'chip', text)));
        host.append(badges, el('div', 'wf-title mono', data.label));
        const facts = el('div', 'wf-facts');
        facts.append(el('span', '', `At ${(data.offset_ns / 1e9).toFixed(3)}s`), el('span', '', data.duration_ns === null ? 'Duration not recorded' : `Duration ${(data.duration_ns / 1e9).toFixed(3)}s`), el('span', 'mono', `Span id ${data.span_id}`));
        if (data.name !== data.label) facts.append(el('span', 'mono', `Instrumented as ${data.name}`));
        host.append(facts);
        const row = Array.from(document.querySelectorAll('.wf-row')).find(row => row.dataset.span === id);
        if (row) {
          const actions = el('div', 'wf-actions');
          const one = el('a', 'linkish', 'Leave this span out'); one.href = row.querySelector('.wf-hide').href;
          const label = el('a', 'linkish', 'Leave out every span with this label'); label.href = row.dataset.labelFilter;
          actions.append(one, label); host.append(actions);
        }
        renderContent(host, data, reload);
      } catch (error) { if (error.name !== 'AbortError') host.replaceChildren(el('p', 'muted', error.message), button('Retry', () => reload(section, chunk))); }
      finally { if (!signal.aborted) host.removeAttribute('aria-busy'); }
    }
  };
})();
