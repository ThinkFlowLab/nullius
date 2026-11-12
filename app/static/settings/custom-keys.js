// Credential rows share one editor pattern; each keeps its existing save destination.
(() => {
  const section = document.getElementById('custom-keys');
  if (!section) return;
  const add = section.querySelector('[data-key-add]');
  const writable = !!document.getElementById('settings-form');
  const cards = () => [...section.querySelectorAll('[data-key-card]')];
  const newCard = () => section.querySelector('[data-key-new]');
  function reveal(element, open, animate = true) {
    if (element.dataset.keyOpen === String(open)) return;
    element.dataset.keyOpen = String(open);
    if (animate && window.NulliusDisclosure) window.NulliusDisclosure.setHidden(element, open);
    else { element.hidden = !open; element.inert = !open; }
  }
  function editor(card, open, animate = true) {
    const button = card.querySelector('[data-key-edit]');
    button.hidden = false;
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? (!writable ? 'Close' : card.dataset.keyKind === 'agent' ? 'Done' : 'Cancel') : button.dataset.keyLabel;
    reveal(card.querySelector('[data-key-editor]'), open, animate);
    card.dataset.keyReady = 'true';
  }
  function initialiseNew() {
    const panel = newCard();
    panel.querySelectorAll('button').forEach(button => { button.hidden = false; });
    panel.querySelector('[data-key-new-save]').disabled = true;
    panel.dataset.keyReady = 'true';
    reveal(panel, false, false);
    add.setAttribute('aria-expanded', 'false');
  }
  function initialise() {
    cards().forEach(card => editor(card, false, false));
    if (add) {
      add.hidden = false; add.setAttribute('aria-expanded', 'false');
      initialiseNew();
    }
    const empty = section.querySelector('[data-keys-empty]');
    if (empty) empty.hidden = cards().length > 0;
  }
  function closeNew() {
    const panel = newCard();
    panel.querySelectorAll('input').forEach(input => { input.value = ''; });
    panel.querySelector('[data-key-new-save]').disabled = true;
    // Clear any incomplete-name validation without saving a discarded draft.
    void window.nulliusSettings?.flush();
    reveal(panel, false);
    add.setAttribute('aria-expanded', 'false');
    add.focus({preventScroll: true});
  }
  section.addEventListener('input', event => {
    const panel = event.target.closest('[data-key-new]');
    if (panel) panel.querySelector('[data-key-new-save]').disabled = ![...panel.querySelectorAll('input')].every(input => input.value.trim());
  });
  section.addEventListener('click', async event => {
    const toggle = event.target.closest('[data-key-edit]');
    if (toggle) {
      const card = toggle.closest('[data-key-card]');
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      if (!open && card.dataset.keyKind === 'agent' && window.nulliusSettings && !await window.nulliusSettings.flush()) return;
      // A save may replace the agent card while flush resolves.
      if (!card.isConnected) { add?.focus({preventScroll: true}); return; }
      if (!open && card.dataset.keyKind === 'benchmark') card.querySelectorAll('input').forEach(input => { input.value = ''; });
      editor(card, open);
      if (open) card.querySelector('[data-key-editor] input:not([type=hidden])')?.focus({preventScroll: true});
      else toggle.focus({preventScroll: true});
    }
    if (event.target.closest('[data-key-add]')) {
      reveal(newCard(), true); add.setAttribute('aria-expanded', 'true');
      newCard().querySelector('input').focus({preventScroll: true});
    }
    if (event.target.closest('[data-key-new-cancel]')) closeNew();
    if (event.target.closest('[data-key-new-save]')) {
      if (window.nulliusSettings && await window.nulliusSettings.flush()) add.focus({preventScroll: true});
    }
  });
  // Add and Cancel control this draft. Moving within it must not save before Cancel runs.
  section.addEventListener('focusout', event => {
    const panel = event.target.closest('[data-key-new]');
    if (!panel || panel.contains(event.relatedTarget)) return;
    setTimeout(() => {
      if (panel.isConnected && !panel.hidden && !panel.contains(document.activeElement)) void window.nulliusSettings?.flush();
    }, 0);
  });
  document.addEventListener('nullius-settings-saved', () => {
    // Autosave replaces AGENT_KEYS, so initialise new nodes rather than retaining old cards.
    section.querySelectorAll('#AGENT_KEYS [data-key-card]:not([data-key-ready])').forEach(card => editor(card, false, false));
    if (add && !newCard().dataset.keyReady) initialiseNew();
    const empty = section.querySelector('[data-keys-empty]');
    if (empty) empty.hidden = cards().length > 0;
  });
  function fromHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    const card = target?.closest('[data-key-card]');
    if (card && section.contains(card)) editor(card, true, false);
  }
  window.addEventListener('hashchange', fromHash);
  initialise(); fromHash();
})();
