// The catalogue and configured list share the existing settings fields and save path.
(() => {
  const section = document.getElementById('providers');
  if (!section) return;
  const cards = [...section.querySelectorAll('[data-provider-card]')];
  const catalogue = section.querySelector('.st-provider-catalogue');
  const add = section.querySelector('[data-provider-add]');
  const back = section.querySelector('[data-provider-back]');
  const search = section.querySelector('[data-provider-search]');
  const writable = !!document.getElementById('settings-form');
  let mode = 'list', selected = null;
  function render() {
    catalogue.hidden = mode !== 'catalogue';
    add.hidden = !writable || mode !== 'list';
    back.hidden = mode === 'list';
    section.querySelector('[data-provider-empty]').hidden = mode !== 'list' || cards.some(c => c.dataset.configured === 'true');
    cards.forEach(card => {
      card.hidden = mode === 'catalogue' || (mode === 'editor' ? card !== selected : card.dataset.configured !== 'true');
      card.querySelector('.st-provider-fields').hidden = mode !== 'editor';
      card.querySelector('[data-provider-edit]').hidden = mode !== 'list';
      card.querySelector('[data-provider-done]').hidden = mode !== 'editor';
    });
  }
  async function leave() {
    if (window.nulliusSettings && !await window.nulliusSettings.flush()) return;
    mode = 'list';
    render();
    (selected && !selected.hidden ? selected.querySelector('[data-provider-edit]') : add).focus({preventScroll: true});
  }
  function edit(key) {
    selected = cards.find(card => card.dataset.providerCard === key);
    if (!selected) return;
    mode = 'editor';
    render();
    const input = selected.querySelector('input.st-input:not([hidden])');
    (input || selected.querySelector('[data-replace]') || back).focus({preventScroll: true});
  }
  add.addEventListener('click', () => {
    mode = 'catalogue'; render(); search.value = ''; search.dispatchEvent(new Event('input')); search.focus();
  });
  back.addEventListener('click', leave);
  section.addEventListener('click', event => {
    const pick = event.target.closest('[data-provider-select], [data-provider-edit]');
    if (pick) edit(pick.dataset.providerSelect || pick.dataset.providerEdit);
    if (event.target.closest('[data-provider-done]')) void leave();
  });
  search.addEventListener('input', () => {
    const choices = [...catalogue.querySelectorAll('[data-provider-select]')];
    choices.forEach(choice => { choice.hidden = !choice.dataset.providerName.toLowerCase().includes(search.value.trim().toLowerCase()); });
    catalogue.querySelector('[data-provider-no-results]').hidden = choices.some(choice => !choice.hidden);
  });
  document.addEventListener('nullius-settings-saved', event => {
    const removedSelectedKey = mode === 'editor' && (event.detail?.removedKeys || []).some(name => selected?.contains(document.getElementById(name)));
    for (const state of event.detail?.providers || []) {
      const card = cards.find(c => c.dataset.providerCard === state.key);
      if (!card) continue;
      card.dataset.configured = state.configured;
      card.dataset.ready = state.ready;
      card.querySelector('[data-provider-state]').textContent = state.label;
    }
    if (removedSelectedKey) mode = 'list';
    render();
    if (removedSelectedKey) (selected && !selected.hidden ? selected.querySelector('[data-provider-edit]') : add).focus({preventScroll: true});
  });
  function fromHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    const card = target?.closest('[data-provider-card]');
    if (card) {
      edit(card.dataset.providerCard);
      target.scrollIntoView({block: 'nearest'});
    }
  }
  window.addEventListener('hashchange', fromHash);
  render();
  fromHash();
})();
