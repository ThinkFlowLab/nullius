// The Model prices section: the price list's state, its Update, and the user's own prices.
// The server sends the state with the page and again after each change; this draws it.
(() => {
  const section = document.getElementById('prices');
  if (!section) return;
  const $ = sel => section.querySelector(sel);
  const csrf = section.dataset.csrf;
  const tell = (message, error = false) => window.nulliusNotify ? window.nulliusNotify(message, {error}) : null;
  const post = async (url, fields) => {
    try {
      const r = await nulliusFetch(url, {method: 'POST', headers: {'X-CSRF-Token': csrf}, body: new URLSearchParams(fields)});
      const j = await r.json();
      return j.ok ? j : {ok: false, error: j.error || 'HTTP ' + r.status};
    } catch (err) { return {ok: false, error: String(err)}; }
  };
  const rate = v => String(Number(v));

  const show = state => {
    $('[data-prices-state]').textContent = state.n
      ? `${state.n.toLocaleString('en-US')} models · ${state.on}` : 'No price list yet';
    const own = Object.entries(state.own || {}).sort(([a], [b]) => a.localeCompare(b));
    $('[data-own-rows]').replaceChildren(...own.map(([model, r]) => {
      const row = document.createElement('div'); row.className = 'st-row';
      const name = document.createElement('span'); name.className = 'st-row-label mono'; name.textContent = model;
      const value = document.createElement('span'); value.className = 'st-row-value st-value';
      value.append(`input ${rate(r.input)} · output ${rate(r.output)}` + (r.cached === undefined ? '' : ` · cached ${rate(r.cached)}`));
      for (const [key, label] of [['cache_write', 'cache write'], ['cache_write_1h', 'cache write 1h']]) {
        if (r[key] !== undefined) value.append(` · ${label} ${rate(r[key])}`);
      }
      if (csrf) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn sm ghost'; button.textContent = 'Remove';
        button.setAttribute('aria-label', `Remove the price of ${model}`);
        button.addEventListener('click', async () => {
          button.disabled = true;
          const got = await post(section.dataset.remove, {model});
          if (got.ok) show(got); else { button.disabled = false; tell(got.error, true); }
        });
        value.append(' ', button);
      }
      row.append(name, value);
      return row;
    }));
    $('[data-own-none]').hidden = own.length > 0;
  };

  const update = $('[data-prices-update]');
  if (update) update.addEventListener('click', async () => {
    const label = update.querySelector('span');
    update.disabled = true; update.classList.add('busy'); label.textContent = 'Updating…';
    const got = await post(section.dataset.update, {});
    update.disabled = false; update.classList.remove('busy'); label.textContent = 'Update';
    tell(got.ok ? `${got.n.toLocaleString('en-US')} models priced` : got.error, !got.ok);
    if (got.ok) show(got);
  });

  const form = $('[data-own-add]');
  if (form) form.addEventListener('submit', async e => {
    e.preventDefault();
    const f = new FormData(form), button = form.querySelector('[type=submit]');
    button.disabled = true;
    const got = await post(section.dataset.set, {model: f.get('model'), input: f.get('input'), output: f.get('output'), cached: f.get('cached'),
      cache_write: f.get('cache_write'), cache_write_1h: f.get('cache_write_1h')});
    button.disabled = false;
    if (got.ok) { form.reset(); show(got); } else tell(got.error, true);
  });

  try { show(JSON.parse(section.dataset.state)); } catch (err) { show({n: 0, own: {}}); }
})();
