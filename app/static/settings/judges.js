// Judge choices reuse configured connections; credentials live only in Model providers.
(() => {
  const cards = [...document.querySelectorAll('[data-judge-card]')];
  const writable = !!document.getElementById('settings-form');
  // The selected attribute as well as the value, so the shared select (controls.js) redraws.
  function choose(select, value) {
    for (const option of select.options) option.toggleAttribute('selected', option.value === value);
    select.value = value;
  }
  const chosen = card => { const select = card.querySelector('[data-picker]'); return select.value ? select.selectedOptions[0] : null; };
  function update(card, states) {
    const select = card.querySelector('[data-picker]');
    const choices = [...select.options].filter(o => o.value);
    for (const choice of choices) {
      if (states) choice.dataset.available = states.some(s => s.key === choice.value && s.ready === 'true') ? 'true' : 'false';
      choice.hidden = choice.dataset.available !== 'true';
    }
    const available = choices.filter(c => !c.hidden);
    if (select.value && select.selectedOptions[0].hidden) choose(select, '');
    if (available.length === 1) choose(select, available[0].value);
    select.disabled = !writable || !available.length;
    select.hidden = !available.length;
    card.querySelector('[data-no-judge-provider]').hidden = available.length > 0;
    const on = chosen(card);
    // The protocol: a select when the provider lists two this judge speaks, else a quiet line.
    const protocol = card.querySelector('[data-protocol-picker]');
    const offered = (on?.dataset.protocols || '').split('|').filter(Boolean);
    for (const option of protocol.options) option.hidden = !offered.includes(option.value);
    if (on && protocol.value !== on.dataset.protocol) {
      choose(protocol, on.dataset.protocol);
      protocol.removeAttribute('name');      // shown, not chosen: the saved setting stays as it is
    }
    protocol.closest('.st-field').hidden = !on || offered.length < 2;
    const via = card.querySelector('[data-judge-via]');
    via.hidden = !on || offered.length > 1;
    via.textContent = 'via ' + (protocol.selectedOptions[0]?.textContent || '');
    const modelField = card.querySelector('.st-field[id$="_MODEL"]');
    modelField.hidden = !on;
    const model = modelField.querySelector('input.st-input');
    if (model) model.placeholder = on?.dataset.default || '';
  }
  cards.forEach(card => {
    // Runs before autosave's listener on the form: a protocol picked here is named, so it is saved.
    card.addEventListener('change', event => {
      if (event.target.matches('[data-protocol-picker]')) {
        event.target.name = event.target.dataset.name;
        if (chosen(card)) chosen(card).dataset.protocol = event.target.value;
      }
      if (event.target.matches('[data-picker], [data-protocol-picker]')) update(card);
    });
  });
  // Initialize after autosave records the server state, so a sole replacement
  // for an unavailable saved provider is persisted rather than merely displayed.
  document.addEventListener('DOMContentLoaded', () => {
    cards.forEach(card => update(card));
    if (writable) document.getElementById('settings-form').dispatchEvent(new Event('change'));
  });
  document.addEventListener('nullius-settings-saved', event => {
    cards.forEach(card => update(card, event.detail?.providers));
  });
})();
