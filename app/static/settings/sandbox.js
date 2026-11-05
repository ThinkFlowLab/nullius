// The Sandbox section: docker's state, asked once when the section is first shown;
// the Docker Hub login (its username only comes back); the images Nullius fetched, per benchmark.
(() => {
  const section = document.getElementById('sandbox');
  if (!section) return;
  const $ = sel => section.querySelector(sel);
  const csrf = section.dataset.csrf;
  const tell = (message, error = false) => window.nulliusNotify ? window.nulliusNotify(message, {error}) : null;
  const size = bytes => bytes >= 1e12 ? (bytes / 1e12).toFixed(1) + ' TB' : bytes >= 1e9 ? (bytes / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(bytes / 1e6)) + ' MB';
  const post = async (url, fields) => {
    try {
      const r = await fetch(url, {method: 'POST', headers: {'X-CSRF-Token': csrf}, body: new URLSearchParams(fields)});
      const j = await r.json();
      return j.ok ? j : {ok: false, error: j.error || (j.failed && j.failed.length ? j.failed.length + ' still in use by a container' : 'HTTP ' + r.status)};
    } catch (err) { return {ok: false, error: String(err)}; }
  };

  const showUser = user => {
    $('[data-hub-user]').hidden = !user;
    $('[data-hub-name]').textContent = user || '';
    const form = $('[data-hub-login]'); if (form) form.hidden = !!user;
    const none = $('[data-hub-none]'); if (none) none.hidden = !!user;
  };

  const showImages = groups => {
    const rows = $('[data-image-rows]');
    rows.replaceChildren(...groups.map(g => {
      const row = document.createElement('div'); row.className = 'st-row';
      const name = document.createElement('span'); name.className = 'st-row-label'; name.textContent = g.label;
      const value = document.createElement('span'); value.className = 'st-row-value st-value';
      value.append(`${g.images} image${g.images === 1 ? '' : 's'} · ${size(g.bytes)}`);
      if (csrf) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn sm ghost'; button.textContent = 'Remove';
        button.addEventListener('click', async () => {
          if (!window.confirm(`Remove the ${g.images} image${g.images === 1 ? '' : 's'} of ${g.label}? The next run pulls them again.`)) return;
          button.disabled = true; button.textContent = 'Removing…';
          const r = await post(section.dataset.remove, {benchmark: g.key});
          tell(r.ok ? `${g.label}: ${r.removed} image${r.removed === 1 ? '' : 's'} removed` : `${g.label}: ${r.error}`, !r.ok);
          load();
        });
        value.append(' ', button);
      }
      row.append(name, value);
      return row;
    }));
    $('[data-images-none]').hidden = groups.length > 0;
  };

  let asked = false;
  const load = async () => {
    asked = true;
    let j;
    try { j = await (await fetch(section.dataset.sandbox)).json(); } catch (err) { j = {docker: {ok: false, error: String(err)}}; }
    const state = $('[data-docker-state]');
    state.textContent = j.docker.ok ? '✓ ' + j.docker.version : '✕ ' + j.docker.error;
    state.className = 'st-row-value ' + (j.docker.ok ? 'ok' : 'bad');
    $('[data-hub]').hidden = !j.docker.ok;
    $('[data-images]').hidden = !j.docker.ok;
    if (j.docker.ok) { showUser(j.user); showImages(j.images || []); }
  };

  const form = $('[data-hub-login]');
  if (form) form.addEventListener('submit', async e => {
    e.preventDefault();
    const f = new FormData(form), button = form.querySelector('[type=submit]');
    button.disabled = true;
    const r = await post(section.dataset.login, {username: f.get('username'), token: f.get('token')});
    form.querySelector('[name=token]').value = ''; button.disabled = false;
    tell(r.ok ? 'Logged in to Docker Hub' : r.error, !r.ok);
    if (r.ok) showUser(r.user);
  });
  const out = $('[data-hub-logout]');
  if (out) out.addEventListener('click', async () => {
    const r = await post(section.dataset.logout, {});
    tell(r.ok ? 'Logged out of Docker Hub' : r.error, !r.ok);
    if (r.ok) showUser(null);
  });

  // Asked the first time the section is shown, not on every visit to Settings.
  const ask = () => { if (!asked && !section.hidden) load(); };
  new MutationObserver(ask).observe(section, {attributes: true, attributeFilter: ['hidden']});
  ask();
})();
