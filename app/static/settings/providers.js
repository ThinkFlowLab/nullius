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
      const remove = card.querySelector('[data-provider-remove-ask]'), ask = card.querySelector('.st-provider-ask');
      if (remove) remove.hidden = mode !== 'list';
      if (ask && mode !== 'list') { ask.hidden = true; remove.setAttribute('aria-expanded', 'false'); }
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
  // Remove asks first, in place: Yes saves the removal, Keep it or Escape closes the question.
  const setAsk = (card, open) => {
    const button = card.querySelector('[data-provider-remove-ask]');
    card.querySelector('.st-provider-ask').hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    (open ? card.querySelector('[data-provider-remove-keep]') : button).focus({preventScroll: true});
  };
  section.addEventListener('keydown', event => {
    const card = event.target.closest('[data-provider-card]'), ask = card?.querySelector('.st-provider-ask');
    if (event.key === 'Escape' && ask && !ask.hidden) { event.preventDefault(); event.stopPropagation(); setAsk(card, false); }
  });
  section.addEventListener('click', event => {
    const ask = event.target.closest('[data-provider-remove-ask]');
    if (ask) { setAsk(ask.closest('[data-provider-card]'), ask.getAttribute('aria-expanded') !== 'true'); return; }
    const keep = event.target.closest('[data-provider-remove-keep]');
    if (keep) { setAsk(keep.closest('[data-provider-card]'), false); return; }
    const yes = event.target.closest('[data-provider-remove-yes]');
    if (yes) {
      const field = document.querySelector('[name="remove_provider"]');
      field.value = yes.dataset.providerRemoveYes;
      field.dispatchEvent(new Event('change', {bubbles: true}));
      return;
    }
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
      const said = card.querySelector('[data-provider-state]');
      if (said) said.textContent = state.label;
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
