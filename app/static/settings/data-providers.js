// Provider credentials use their own stores, never the model settings form.
(() => {
  const section = document.getElementById('data-providers');
  if (!section) return;
  const $ = s => section.querySelector(s);
  const cards = [...section.querySelectorAll('[data-data-card]')];
  const choices = [...section.querySelectorAll('[data-data-select]')];
  const add = $('[data-data-add]'), search = $('[data-data-search]');
  let saved = {}, mode = 'list', selected = '', asked = false, busy = false;
  // Hugging Face is connected by its token, its endpoint, or both
  let hf = {token: false, endpoint: '', env: false};
  const hostOf = url => { try { return new URL(url).host; } catch { return url; } };
  const account = key => key === 'dockerhub' ? saved[key] || ''
    : [hf.token && 'Token saved', hf.endpoint && hostOf(hf.endpoint)].filter(Boolean).join(' · ');
  const notify = (message, error = false) => window.nulliusNotify?.(message, {error});
  const filter = () => {
    choices.forEach(c => { c.hidden = !!saved[c.dataset.dataSelect] || !c.dataset.name.toLowerCase().replaceAll(' ', '').includes(search.value.toLowerCase().replaceAll(' ', '').trim()); });
    $('[data-data-no-results]').hidden = choices.some(c => !c.hidden);
  };
  const render = () => {
    if (add) add.hidden = mode !== 'list' || cards.every(c => saved[c.dataset.dataCard]);
    $('[data-data-back]').hidden = mode === 'list';
    $('[data-data-empty]').hidden = mode !== 'list' || cards.some(c => saved[c.dataset.dataCard]);
    $('[data-data-catalogue]').hidden = mode !== 'catalogue';
    cards.forEach(card => {
      const key = card.dataset.dataCard, editing = mode === 'editor' && key === selected;
      card.hidden = !(editing || mode === 'list' && saved[key]);
      card.querySelector('[data-data-edit]').hidden = editing;
      card.querySelector('[data-data-fields]').hidden = !editing;
      card.querySelector('[data-data-account]').textContent = account(key);
      const remove = card.querySelector('[data-data-remove]'); if (remove) remove.hidden = !saved[key];
    });
    filter();
  };
  const leave = () => {
    cards.forEach(card => card.querySelector('form')?.reset());
    mode = 'list'; render();
    const target = saved[selected] ? $(`[data-data-card="${selected}"] [data-data-edit]`) : add;
    if (target && !target.hidden) target.focus();
  };
  const edit = key => {
    selected = key; mode = 'editor'; render();
    const card = $(`[data-data-card="${key}"]`);
    const username = card.querySelector('[name=username]'); if (username) username.value = saved[key] || '';
    const endpoint = card.querySelector('[name=endpoint]');
    if (endpoint) {
      endpoint.value = hf.endpoint;
      endpoint.readOnly = hf.env;
      endpoint.title = hf.env ? 'Set by HF_ENDPOINT in the environment' : '';
    }
    const token = card.querySelector('[name=token]');
    if (token && key === 'huggingface') token.placeholder = hf.token ? 'Saved' : '';
    (card.querySelector('input') || $('[data-data-back]')).focus();
  };
  if (add) add.addEventListener('click', () => { mode = 'catalogue'; search.value = ''; render(); search.focus(); });
  $('[data-data-back]').addEventListener('click', () => { if (!busy) leave(); });
  search.addEventListener('input', filter);
  choices.forEach(c => c.addEventListener('click', () => edit(c.dataset.dataSelect)));
  const change = async (card, remove) => {
    if (busy) return;
    busy = true;
    const key = card.dataset.dataCard, form = card.querySelector('form');
    const fields = new FormData(form);
    if (key === 'huggingface') fields.set('action', remove ? 'remove' : 'save');
    // Removing a connection never submits a replacement token.
    if (remove) { fields.delete('token'); fields.delete('endpoint'); }
    const url = key === 'huggingface' ? section.dataset.huggingface : remove ? section.dataset.dockerhubRemove : section.dataset.dockerhub;
    section.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const response = await nulliusFetch(url, {method: 'POST', headers: {'X-CSRF-Token': section.dataset.csrf}, body: fields});
      const result = await response.json();
      if (!response.ok || !result.ok) { notify(result.error || 'Could not save the connection.', true); return; }
      if (key === 'huggingface') {
        const endpoint = (fields.get('endpoint') || '').trim().replace(/\/+$/, '');
        if (remove) hf = {token: false, endpoint: hf.env ? hf.endpoint : '', env: hf.env};
        else hf = {token: hf.token || !!fields.get('token'), env: hf.env,
                   endpoint: hf.env ? hf.endpoint : endpoint === hf.fallback ? '' : endpoint, fallback: hf.fallback};
        saved[key] = hf.token || !!hf.endpoint;
      } else saved[key] = remove ? null : result.user;
      notify(remove ? 'Connection removed' : key === 'dockerhub' ? 'Signed in to Docker Hub' : 'Connection saved');
      leave();
    } catch { notify('Could not save the connection.', true); }
    finally {
      form.querySelector('[name=token]').value = '';
      section.querySelectorAll('button').forEach(b => { b.disabled = false; });
      busy = false;
    }
  };
  cards.forEach(card => {
    card.querySelector('[data-data-edit]').addEventListener('click', () => edit(card.dataset.dataCard));
    card.querySelector('form')?.addEventListener('submit', e => { e.preventDefault(); void change(card, false); });
    card.querySelector('[data-data-remove]')?.addEventListener('click', () => void change(card, true));
  });
  const load = async () => {
    if (asked || section.hidden) return;
    asked = true;
    try {
      const response = await nulliusFetch(section.dataset.state);
      if (!response.ok) throw new Error();
      const state = await response.json();
      hf = {token: !!state.huggingface, endpoint: state.huggingface_endpoint || '', env: !!state.huggingface_endpoint_env,
            fallback: state.huggingface_endpoint_default};
      saved = {huggingface: hf.token || !!hf.endpoint, dockerhub: state.dockerhub};
      render();
    } catch {
      const error = $('[data-data-error]'); error.textContent = 'Could not load data providers. Reopen Settings to retry.'; error.hidden = false;
    }
  };
  new MutationObserver(load).observe(section, {attributes: true, attributeFilter: ['hidden']});
  load();
})();
