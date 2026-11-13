// The Sandbox section: docker's state and whether a Boat key is saved, asked once when the section is first shown.
(() => {
  const section = document.getElementById('sandbox');
  if (!section) return;
  const $ = s => section.querySelector(s);
  const state = $('[data-boat-state]'), form = $('[data-boat-form]');
  const edit = $('[data-boat-edit]'), remove = $('[data-boat-remove]'), cancel = $('[data-boat-cancel]');
  const notify = (message, error = false) => window.nulliusNotify?.(message, {error});
  let boat = {set: false, env: false}, editing = false;

  const render = () => {
    state.textContent = boat.env ? 'Key from BOAT_API_KEY' : boat.set ? 'Key saved' : 'No key';
    if (form) form.hidden = !editing;
    if (edit) { edit.hidden = editing; edit.textContent = boat.set ? 'Edit' : 'Add key'; }
    if (remove) remove.hidden = editing || !boat.set;
  };

  let asked = false;
  const load = async () => {
    asked = true;
    let j;
    try { j = await (await nulliusFetch(section.dataset.sandbox)).json(); } catch (err) { j = {docker: {ok: false, error: String(err)}, boat: {set: false, env: false}}; }
    const docker = $('[data-docker-state]');
    docker.textContent = j.docker.ok ? '✓ ' + j.docker.version : '✕ ' + j.docker.error;
    docker.className = 'st-row-value ' + (j.docker.ok ? 'ok' : 'bad');
    boat = j.boat || {set: false, env: false};
    render();
  };

  const change = async action => {
    const fields = new FormData(form);
    fields.set('action', action);
    if (action === 'remove') fields.delete('key');
    section.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const response = await nulliusFetch(section.dataset.boat, {method: 'POST', headers: {'X-CSRF-Token': section.dataset.csrf}, body: fields});
      const result = await response.json();
      if (!response.ok || !result.ok) { notify(result.error || 'Could not save the key.', true); return; }
      form.reset();
      editing = false;
      notify(action === 'remove' ? 'Boat key removed.' : 'Boat key saved.');
      await load();
    } catch (err) {
      notify(String(err), true);
    } finally {
      section.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
  };
  if (edit) edit.addEventListener('click', () => { editing = true; render(); form.querySelector('input').focus(); });
  if (cancel) cancel.addEventListener('click', () => { form.reset(); editing = false; render(); edit.focus(); });
  if (form) form.addEventListener('submit', e => { e.preventDefault(); change('save'); });
  if (remove) remove.addEventListener('click', () => change('remove'));

  // Asked the first time the section is shown, not on every visit to Settings.
  const ask = () => { if (!asked && !section.hidden) load(); };
  new MutationObserver(ask).observe(section, {attributes: true, attributeFilter: ['hidden']});
  ask();
})();
