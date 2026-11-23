// Save only the submitted section; drafts in the other section stay in the DOM.
(() => {
  const panel = document.getElementById('failure-analysis');
  if (!panel) return;
  panel.addEventListener('change', event => {
    const input = event.target;
    if (!input.matches('input[type=radio]')) return;
    input.closest('.fa-choices').querySelectorAll('label').forEach(label => {
      label.classList.toggle('active', label.querySelector('input').checked);
    });
  });
  panel.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('.fa-form')) return;
    event.preventDefault();
    if (form.getAttribute('aria-busy') === 'true') return;
    const section = form.closest('.fa-part');
    const data = new FormData(form);
    data.set('action', event.submitter ? event.submitter.value : form.querySelector('button[name=action]').value);
    const controls = [...form.elements].filter(control => !control.disabled);
    controls.forEach(control => { control.disabled = true; });
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await nulliusFetch(form.getAttribute('action'), {method: 'POST', body: data});
      if (!response.ok) throw new Error('Save failed');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updated = doc.getElementById(section.id);
      if (!updated) throw new Error('Missing saved section');
      section.replaceWith(updated);
      nulliusNotify('Saved', {key: section.id});
      updated.querySelector('button[type=submit]')?.focus({preventScroll: true});
    } catch (_) {
      nulliusNotify('Could not save. Your edits are still here; try again.', {error: true, key: section.id});
    } finally {
      controls.forEach(control => { control.disabled = false; });
      form.removeAttribute('aria-busy');
    }
  });
})();
