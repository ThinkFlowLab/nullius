// The API access section: the tokens of the HTTP API, a new one shown once, a revoke asked in place.
// The server sends the tokens with the page and again after each change; this draws them.
(() => {
  const section = document.getElementById('api-access');
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
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const ACCESS = {read: 'Read', run: 'Read and run'};
  const day = stamp => (stamp || '').slice(0, 10);

  const show = tokens => {
    $('[data-api-rows]').replaceChildren(...tokens.map(t => {
      const row = el('div', 'st-row'), value = el('span', 'st-value');
      const said = el('span', '', [ACCESS[t.scope] || t.scope, t.expired ? 'expired ' + day(t.expires) : t.expires ? 'until ' + day(t.expires) : ''].filter(Boolean).join(' · '));
      if (t.expired) said.classList.add('st-bad');
      value.append(said);
      if (csrf) {
        const revoke = el('button', 'btn sm ghost', 'Revoke'); revoke.type = 'button';
        revoke.setAttribute('aria-label', `Revoke ${t.name}`);
        // one line of yes or no, asked in place
        const ask = el('span', 'st-value st-api-ask'); ask.hidden = true; ask.setAttribute('role', 'group'); ask.setAttribute('aria-label', `Revoke ${t.name}`);
        const yes = el('button', 'btn sm danger', 'Yes, revoke'), keep = el('button', 'btn sm ghost', 'Keep it'); yes.type = keep.type = 'button';
        ask.append(yes, keep);
        revoke.addEventListener('click', () => { revoke.hidden = true; ask.hidden = false; keep.focus(); });
        keep.addEventListener('click', () => { ask.hidden = true; revoke.hidden = false; revoke.focus(); });
        yes.addEventListener('click', async () => {
          yes.disabled = true;
          const got = await post(section.dataset.revoke, {id: t.id});
          if (got.ok) show(got.tokens); else { yes.disabled = false; tell(got.error, true); }
        });
        value.append(revoke, ask);
      }
      row.append(el('span', 'st-row-label', t.name), value);
      return row;
    }));
    $('[data-api-list]').hidden = tokens.length === 0;      // nothing to list, no list
  };

  // Access and Expires: one choice each, as small cards
  const groups = [...section.querySelectorAll('[data-api-choice]')];
  const choose = (group, b) => group.querySelectorAll('.st-tab').forEach(x => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
  groups.forEach(group => group.addEventListener('click', e => { const b = e.target.closest('.st-tab'); if (b) choose(group, b); }));
  const chosen = field => $(`[data-api-choice="${field}"] .st-tab.on`).dataset.value;

  const fresh = $('[data-api-new]');
  const done = () => { fresh.hidden = true; $('[data-api-new-token]').textContent = ''; };
  $('[data-api-done]').addEventListener('click', done);
  $('[data-api-copy]').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('[data-api-new-token]').textContent); tell('Copied'); }
    catch (err) { tell('Select the token and copy it', true); }
  });

  const form = $('[data-api-add]');
  if (form) form.addEventListener('submit', async e => {
    e.preventDefault();
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    const got = await post(section.dataset.create, {name: new FormData(form).get('name'), scope: chosen('scope'), days: chosen('days')});
    button.disabled = false;
    if (!got.ok) return tell(got.error, true);
    form.reset();
    groups.forEach(group => choose(group, group.querySelector('.st-tab')));      // the next token starts from the least: read, never
    $('[data-api-new-name]').textContent = got.token.name;
    $('[data-api-new-token]').textContent = got.value;
    fresh.hidden = false;
    show(got.tokens);
    $('[data-api-copy]').focus();
  });

  try { show(JSON.parse(section.dataset.state)); } catch (err) { show([]); }
})();
