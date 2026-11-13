// Run hosts: a table, a row per machine, this machine first. A row's actions (Info, Connection, Constraints, Delete) open a panel
// in a row below it, one at a time, with the shared sliding disclosure. Test asks the machine and redraws its Info; Connection and
// Delete go through the settings save with the form token, then the list is drawn again from the answer.
(() => {
  const list = () => document.getElementById('HOSTS');
  const opened = {};                       // machine -> its open panel, kept over a list drawn again
  const rowOf = entry => [...list().querySelectorAll('[data-machine]')].find(r => r.dataset.machine === entry);
  const open = (row, name, animate = true) => {
    row.querySelectorAll('[data-panel]').forEach(panel => {
      const on = panel.dataset.panel === name, shown = !panel.hidden;
      if (on !== shown) {
        if (animate) NulliusDisclosure.setHidden(panel, on);
        else { panel.hidden = !on; panel.inert = !on; }
      }
    });
    row.querySelectorAll('[data-open]').forEach(b => {
      const on = b.dataset.open === name; b.setAttribute('aria-expanded', String(on)); b.classList.toggle('on', on);
    });
    if (name) opened[row.dataset.machine] = name; else delete opened[row.dataset.machine];
  };
  const reopen = () => Object.entries(opened).forEach(([entry, name]) => { const row = rowOf(entry); if (row) open(row, name, false); });
  const csrf = () => document.querySelector('#settings-form input[name=csrf]').value;
  // a row's status: its dot and the words a screen reader says for it
  const settle = (row, state, words) => {
    const dot = row.querySelector('.host-dot');
    dot.classList.remove('st-watch', 'st-runs', 'st-blocked', 'testing'); dot.classList.add(state);
    row.querySelector('[data-status]').textContent = words;
  };
  // the mark beside a machine's name: how many of its constraints a run there would be refused
  const mark = (row, n) => {
    const name = row.querySelector('.host-name'), said = n ? `${n} constraint${n === 1 ? ' is' : 's are'} not enforced here: see Constraints.` : '';
    let svg = name.querySelector('.host-warn');
    if (!n) { if (svg) svg.remove(); return; }
    if (!svg) {
      name.querySelector('.settings-test').insertAdjacentHTML('beforebegin', '<svg class="ic host-warn" role="img"><title></title><use href="#i-alert"/></svg>');
      svg = name.querySelector('.host-warn');
    }
    svg.setAttribute('aria-label', said); svg.querySelector('title').textContent = said;
  };
  let running = 0;                         // tests under way: Test all waits for every one
  const testAll = () => document.querySelector('#hosts [data-test-all]');
  const test = async row => {
    const out = row.querySelector('.host-line .settings-test'), button = row.querySelector('[data-test-machine]');
    running += 1; if (testAll()) testAll().disabled = true;
    if (button) button.disabled = true;
    out.textContent = ''; settle(row, 'testing', 'Testing');
    try {
      const r = await nulliusFetch(list().dataset.test, {method: 'POST', headers: {'X-CSRF-Token': csrf()}, body: new URLSearchParams({entry: row.dataset.machine})});
      const j = await r.json();
      const ok = j.ok;                     // reached; what runs there is in its Info
      out.textContent = ok ? '' : '✕ ' + (j.error || 'HTTP ' + r.status);
      settle(row, ok ? 'st-runs' : 'st-blocked', ok ? 'Reached' : 'Not reached');
      if (j.info) row.querySelector('[data-panel="info"]').innerHTML = j.info;
      Object.entries(j.cells || {}).forEach(([cell, text]) => { row.querySelector(`[data-cell="${cell}"]`).textContent = text; });
      mark(row, j.not_enforced || 0);
      const warn = (j.constraints || {}).warnings;
      if (warn) row.querySelectorAll('[data-key]').forEach(i => {
        const value = i.closest('.st-row-value');
        let note = value.querySelector('.st-warn');
        if (!warn[i.dataset.key]) { if (note) note.remove(); return; }
        if (!note) { note = document.createElement('p'); note.className = 'st-note st-warn'; value.append(note); }
        note.textContent = warn[i.dataset.key];
      });
    } catch (err) { out.textContent = '✕ ' + err; settle(row, 'st-blocked', 'Not reached'); }
    finally { running -= 1; if (testAll()) testAll().disabled = running > 0; if (button) button.disabled = false; }
  };
  // what the edit panel names now, as an entry: an alias, or [Name=]user@address[:port][:key]
  const entryOf = form => {
    const f = name => (form.querySelector(`[data-field="${name}"]`)?.value || '').trim();
    if (form.dataset.kind === 'alias') return f('alias');
    return (f('display_name') ? f('display_name') + '=' : '') + f('user') + '@' + f('host') + (f('port') ? ':' + f('port') : '') + (f('key') ? ':' + f('key') : '');
  };
  const change = async (row, to, said) => {
    if (!(await window.nulliusSettings.flush())) return;
    const body = new FormData();
    body.set('csrf', csrf()); body.set('host_from', row.dataset.machine);
    if (to !== null) body.set('host_to', to);
    try {
      const r = await nulliusFetch(document.getElementById('settings-form').action, {method: 'POST', headers: {Accept: 'application/json'}, body});
      const j = await r.json();
      if (!j.ok) { said.textContent = '✕ ' + (j.message || 'HTTP ' + r.status); return; }
      delete opened[row.dataset.machine];
      list().replaceWith(new DOMParser().parseFromString(j.html, 'text/html').getElementById('HOSTS'));
      window.nulliusSettings.rebase();
      reopen();
      document.dispatchEvent(new CustomEvent('nullius-hosts-replaced'));
      window.nulliusNotify(j.message, {key: 'settings'});
    } catch (err) { said.textContent = '✕ ' + err; }
  };
  document.addEventListener('click', event => {
    const t = event.target;
    const act = t.closest('#HOSTS [data-open]');
    if (act) { const row = act.closest('[data-machine]'); open(row, act.getAttribute('aria-expanded') === 'true' ? null : act.dataset.open); }
    const close = t.closest('#HOSTS [data-close]');
    if (close) { const row = close.closest('[data-machine]'); open(row, null); row.querySelector(`[data-open="${close.closest('[data-panel]').dataset.panel}"]`)?.focus(); }
    const one = t.closest('#HOSTS [data-test-machine]');
    if (one) test(one.closest('[data-machine]'));
    const all = t.closest('#hosts [data-test-all]');
    if (all) [...list().querySelectorAll('[data-machine]')].forEach(test);
    const save = t.closest('#HOSTS [data-host-save]');
    if (save) { const form = save.closest('[data-host-edit]'); change(save.closest('[data-machine]'), entryOf(form), form.querySelector('[data-host-said]')); }
    const del = t.closest('#HOSTS [data-host-delete]');
    if (del) change(del.closest('[data-machine]'), null, del.closest('.host-delete').querySelector('[data-host-said]'));
  });
  // Enter in an edit field saves; Escape closes a row's panel
  document.addEventListener('keydown', event => {
    const row = event.target.closest?.('#HOSTS [data-machine]');
    if (!row) return;
    if (event.key === 'Enter' && event.target.closest('[data-host-edit] input')) { event.preventDefault(); row.querySelector('[data-host-save]').click(); }
    if (event.key === 'Escape' && opened[row.dataset.machine]) {
      const name = opened[row.dataset.machine]; open(row, null); row.querySelector(`[data-open="${name}"]`)?.focus();
    }
  });
  document.addEventListener('nullius-settings-saved', reopen);
})();
