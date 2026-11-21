// The Sandbox section: whether a Boat key is saved, asked once when the section is first shown.
(() => {
  const section = document.getElementById('sandbox');
  if (!section) return;
  const $ = s => section.querySelector(s);
  const state = $('[data-boat-state]'), form = $('[data-boat-form]');
  const edit = $('[data-boat-edit]'), remove = $('[data-boat-remove]'), cancel = $('[data-boat-cancel]'), removeAsk = $('[data-boat-ask]');
  const notify = (message, error = false) => window.nulliusNotify?.(message, {error});
  let boat = {set: false, env: false}, editing = false;

  const render = () => {
    state.textContent = boat.env ? 'Key from BOAT_API_KEY' : boat.set ? 'Key saved' : 'No key';
    if (form) form.hidden = !editing;
    if (edit) { edit.hidden = editing; edit.textContent = boat.set ? 'Edit' : 'Add key'; }
    if (remove) remove.hidden = editing || !boat.set;
    if (remove?.hidden) asking(false, false);
  };
  const asking = (open, focus = true) => {
    if (!removeAsk) return;
    removeAsk.hidden = !open;
    remove.setAttribute('aria-expanded', String(open));
    if (focus) (open ? $('[data-boat-remove-keep]') : remove).focus({preventScroll: true});
  };

  let asked = false;
  const load = async () => {
    asked = true;
    let j;
    try { j = await (await nulliusFetch(section.dataset.sandbox)).json(); } catch (err) { j = {boat: {set: false, env: false}}; }
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
  const open = () => { editing = true; render(); form.querySelector('input').focus(); };
  if (edit) edit.addEventListener('click', open);
  const fromHash = () => { if (form && location.hash === '#sandbox-boat-key') open(); };
  window.addEventListener('hashchange', fromHash);
  if (cancel) cancel.addEventListener('click', () => { form.reset(); editing = false; render(); edit.focus(); });
  if (form) form.addEventListener('submit', e => { e.preventDefault(); change('save'); });
  if (remove) remove.addEventListener('click', () => asking(removeAsk.hidden));
  $('[data-boat-remove-keep]')?.addEventListener('click', () => asking(false));
  $('[data-boat-remove-yes]')?.addEventListener('click', () => { asking(false, false); change('remove'); });
  removeAsk?.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); asking(false); } });

  // Asked the first time the section is shown, not on every visit to Settings.
  const ask = () => { if (!asked && !section.hidden) load(); };
  new MutationObserver(ask).observe(section, {attributes: true, attributeFilter: ['hidden']});
  ask();
  fromHash();
})();
