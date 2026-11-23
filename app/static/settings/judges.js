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
    const list = model?.list;
    if (list) {
      list.replaceChildren();
      for (const value of (on?.dataset.models || '').split('|').filter(Boolean)) {
        const option = document.createElement('option'); option.value = value; list.append(option);
      }
    }
    if (model) model.placeholder = on?.dataset.default || '';
  }
  // A provider with no list of its own here: its catalogue, asked once when the field is first used; a live list
  // replaces the one drawn with the page. Typing a model still works.
  const catalogues = new Map();
  async function offerCatalogue(card, model) {
    const on = chosen(card);
    if (!on || (on.dataset.models && !('live' in on.dataset)) || !model.list) return;
    const provider = on.value.replace(/^local(?=$|:)/, 'custom');   // local:<id>, a named endpoint, is the builder's custom:<id>
    const url = 'live' in on.dataset ? '/settings/decision/models' : '/new-run/models';
    if (!catalogues.has(url + provider)) {
      catalogues.set(url + provider, nulliusFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('#settings-form [name=csrf]').value}, body: JSON.stringify({provider})})
        .then(r => { if (!r.ok) throw new Error("Model catalogue unavailable"); return r.json(); }).then(j => { if (j.error) throw new Error(j.error); return j.models || []; }).catch(() => { catalogues.delete(url + provider); return []; }));
    }
    const models = await catalogues.get(url + provider);
    if (!models.length) return;
    if (chosen(card) !== on) return;
    model.list.replaceChildren(...models.map(m => {
      const option = document.createElement('option'); option.value = m.id; if (m.name !== m.id) option.label = m.name; return option;
    }));
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
