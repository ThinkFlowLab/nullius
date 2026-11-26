// Judgements kept in workspaces before they were kept by job: the dialog that moves them into the
// user's store, opened from Settings › Judges ([data-move-judgements]) and once per browser at a first page load.
(() => {
  const me = document.currentScript, URL_ = me.dataset.url, CSRF = me.dataset.csrf, KEY = 'nullius-judgements-move';
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const remember = () => { try { localStorage.setItem(KEY, '1'); } catch {} };
  const notify = (m, o) => window.nulliusNotify && window.nulliusNotify(m, o);

  async function ask({later = false} = {}) {
    const got = await nulliusFetch(URL_, {headers: {Accept: 'application/json'}}).then(r => r.ok ? r.json() : {workspaces: []}).catch(() => ({workspaces: []}));
    if (!got.workspaces.length) { if (later) remember(); return; }      // nothing to move: not asked again here
    // a native modal, as the welcome: it keeps the focus, makes the page behind it inert and closes on Escape
    const d = document.createElement('dialog');
    d.className = 'onb-dialog';
    d.setAttribute('aria-labelledby', 'jm-h');
    d.innerHTML = `<h2 id="jm-h">Move judgements to your profile</h2>
      <p>They are now kept by job, so every workspace shows them. The old files stay.</p>
      <ul>${got.workspaces.map(w => `<li>${esc(w.label)}: ${w.n}</li>`).join('')}</ul>
      <div class="onb-actions"><button class="btn ghost" type="button" data-a="later">Not now</button><button class="btn" type="button" data-a="move">Move</button></div>`;
    const back = document.activeElement;
    document.body.append(d);
    d.showModal();
    d.querySelector('[data-a=move]').focus();
    d.addEventListener('click', async e => {
      const a = e.target.closest('[data-a]')?.dataset.a;
      if (a === 'later') return d.close();
      if (a !== 'move') return;
      e.target.disabled = true;
      const r = await nulliusFetch(URL_, {method: 'POST', headers: {'X-CSRF-Token': CSRF, Accept: 'application/json'}}).catch(() => null);
      const j = r ? await r.json().catch(() => ({})) : {};
      d.returnValue = 'move'; d.close();
      if (j.ok) {
        notify('Judgements moved.');
        document.querySelectorAll('[data-move-judgements-row]').forEach(x => x.remove());
      } else notify(j.error || 'The judgements could not be moved.', {error: true});
    });
    // Escape, like Not now
    d.addEventListener('close', () => {
      remember(); d.remove(); back?.focus();
      if (d.returnValue !== 'move' && later) notify('Move them later in Settings › Judges.');
    });
  }

  document.addEventListener('click', e => { if (e.target.closest('[data-move-judgements]')) ask(); });

  // shortcut: this first-load dialog is temporary migration UI, against "a notice is a toast"; remove it after a few versions.
  if (window.parent !== window) return;
  let seen = true;
  try { seen = !!localStorage.getItem(KEY) || !(JSON.parse(localStorage.getItem('nullius-onboarding')) || {}).asked; } catch {}
  if (!seen && !document.querySelector('dialog[open]')) ask({later: true});
})();
