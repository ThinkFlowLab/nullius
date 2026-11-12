// Judge choices reuse configured connections; credentials live only in Model providers.
(() => {
  const cards = [...document.querySelectorAll('[data-judge-card]')];
  const writable = !!document.getElementById('settings-form');
  function update(card, states) {
    const choices = [...card.querySelectorAll('[data-provider]')];
    for (const choice of choices) {
      if (states) choice.dataset.available = states.some(s => s.key === choice.dataset.provider && s.ready === 'true') ? 'true' : 'false';
      choice.hidden = choice.dataset.available !== 'true';
      const input = choice.querySelector('input');
      input.disabled = !writable || choice.hidden;
      if (choice.hidden) input.checked = false;
    }
    const available = choices.filter(c => !c.hidden);
    if (available.length === 1) available[0].querySelector('input').checked = true;
    card.querySelector('[data-picker]').classList.toggle('st-judge-single', available.length === 1);
    card.querySelector('[data-no-judge-provider]').hidden = available.length > 0;
    const modelField = card.querySelector('.st-field[id$="_MODEL"]');
    const on = card.querySelector('[data-picker] input:checked');
    modelField.hidden = !on;
    const model = modelField.querySelector('input.st-input');
    const list = model?.list;
    if (list) {
      list.replaceChildren();
      for (const value of (on?.dataset.models || '').split('|').filter(Boolean)) {
        const option = document.createElement('option'); option.value = value; list.append(option);
      }
    }
    if (model) model.placeholder = on?.dataset.default || '';
  }
  // A provider with no list of its own here: its catalogue, asked once when the field is first used. Typing a model still works.
  const catalogues = new Map();
  async function offerCatalogue(card, model) {
    const on = card.querySelector('[data-picker] input:checked');
    if (!on || on.dataset.models || !model.list) return;
    const provider = on.value.replace(/^local(?=$|:)/, 'custom');   // local:<id>, a named endpoint, is the builder's custom:<id>
    if (!catalogues.has(provider)) {
      catalogues.set(provider, fetch('/new-run/models', {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('#settings-form [name=csrf]').value}, body: JSON.stringify({provider})})
        .then(r => { if (!r.ok) throw new Error("Model catalogue unavailable"); return r.json(); }).then(j => { if (j.error) throw new Error(j.error); return j.models || []; }).catch(() => { catalogues.delete(provider); return []; }));
    }
    const models = await catalogues.get(provider);
    if (card.querySelector('[data-picker] input:checked') !== on) return;
    model.list.replaceChildren(...models.map(m => {
      const option = document.createElement('option'); option.value = m.id; if (m.name !== m.id) option.label = m.name; return option;
    }));
  }
  cards.forEach(card => {
    card.addEventListener('change', event => { if (event.target.matches('[data-picker] input')) update(card); });
    card.addEventListener('focusin', event => { if (event.target.matches('input.st-input')) offerCatalogue(card, event.target); });
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
