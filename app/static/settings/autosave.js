// Serialize writes; refresh only unchanged fields so another edit is never lost.
(() => {
  const form = document.getElementById('settings-form');
  if (!form) return;
  const status = form.querySelector('[data-changes]');
  const retry = form.querySelector('[data-settings-retry]');
  status.closest('.st-savebar').hidden = true;
  const snapshot = () => new Map(new FormData(form));
  let saved = snapshot(), pending = null;
  const changes = current => [...current].filter(([key, value]) => !['csrf', 'embedded'].includes(key) && value !== (saved.get(key) ?? ''));
  function say(message, error = false, retryable = false) {
    status.textContent = message;
    retry.hidden = !retryable;
    status.closest('.st-savebar').hidden = !error;
    form.dataset.saveError = String(error);
  }
  function notify(message) {
    window.nulliusNotify(message, {key: 'settings'});
  }
  async function write() {
    const current = snapshot();
    const changed = changes(current);
    if (!changed.length) return true;
    const body = new FormData();
    body.set('csrf', current.get('csrf'));
    if (current.has('embedded')) body.set('embedded', '1');
    const names = new Set();
    for (const [key, value] of changed) {
      body.set(key, value);
      if (key.startsWith('clear:')) {
        names.add(key.slice(6));
        body.set('value:' + key.slice(6), current.get('value:' + key.slice(6)) || '');
      } else if (key.startsWith('value:')) names.add(key.slice(6));
    }
    // A model edit always carries the displayed provider, including a sole auto-selection.
    for (const prefix of ['SYSTEM_ONE', 'LLM_JUDGE']) {
      if (body.has('value:' + prefix + '_MODEL') || body.has('clear:' + prefix + '_MODEL')) {
        const provider = current.get('value:' + prefix + '_PROVIDER');
        if (provider) body.set('value:' + prefix + '_PROVIDER', provider);
      }
    }
    // Removing the final host is an explicit removal, not an empty "keep" input.
    if (body.has('value:HOSTS') && !body.get('value:HOSTS')) body.set('clear:HOSTS', '1');
    const agentContainer = document.getElementById('AGENT_KEYS');
    const agentFields = [...agentContainer.querySelectorAll('[name]')].map(input => input.name);
    const agentChanged = changed.some(([key]) => key.startsWith('agent_key_'));
    if (agentChanged) {
      body.set('agent_key_name', current.get('agent_key_name') || '');
      body.set('agent_key_value', current.get('agent_key_value') || '');
      if (!body.get('agent_key_name') || !body.get('agent_key_value')) {
        say('Enter both the key name and its value.', true);
        return false;
      }
    }
    // A new endpoint: its name, URL and optional key go together.
    const endpointChanged = changed.some(([key]) => key.startsWith('endpoint_'));
    if (endpointChanged) {
      for (const part of ['name', 'url', 'key']) body.set('endpoint_' + part, current.get('endpoint_' + part) || '');
      if (!body.get('endpoint_name') || !body.get('endpoint_url')) {
        say('Enter the endpoint name and its URL.', true);
        return false;
      }
    }
    say('');
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await nulliusFetch(form.action, {method: 'POST', headers: {Accept: 'application/json'}, body});
      if (!response.ok && !response.headers.get('content-type')?.includes('application/json')) throw new Error('Settings could not be saved. Try again.');
      const result = await response.json();
      if (!result.ok) {
        const error = new Error(result.message || 'Settings could not be saved.');
        error.retryable = result.retryable === true;
        throw error;
      }
      if (result.reload) {           // an endpoint added, renamed or removed: its cards and the judge pickers change
        saved = snapshot();
        history.replaceState(null, '', location.pathname + location.search + '#providers');
        location.reload();
        return true;
      }
      let message = 'Settings saved';
      if (names.has('HOSTS')) {
        const entries = value => (value || '').split(',').map(x => x.trim()).filter(Boolean);
        const before = entries(saved.get('value:HOSTS')), after = entries(current.get('value:HOSTS'));
        message = after.some(x => !before.includes(x)) ? 'Host added' : 'Host removed';
      } else if (agentChanged || [...names].some(name => document.getElementById(name)?.dataset.secret === 'yes')) {
        message = [...names].some(name => body.get('clear:' + name) === '1') ? 'Key removed' : 'Key saved';
      }
      const fresh = new DOMParser().parseFromString(result.html, 'text/html');
      const now = snapshot();
      for (const [key, value] of changed) saved.set(key, value);
      for (const name of names) {
        if (name === 'HOSTS') continue; // host-list listeners and chips stay in place
        const field = document.getElementById(name), replacement = fresh.getElementById(name);
        if (!field || !replacement || (field.contains(document.activeElement) && document.activeElement.matches('input:not([type=hidden]), textarea, select')) || field.querySelector('input[type=radio]')) continue;
        const keys = [...field.querySelectorAll('[name]')].map(input => input.name);
        if (keys.some(key => now.get(key) !== current.get(key))) continue;
        field.replaceWith(replacement);
        for (const input of replacement.querySelectorAll('[name]')) {
          if (input.type !== 'radio' || input.checked) saved.set(input.name, input.value);
        }
      }
      if ((agentChanged || [...names].some(name => agentContainer.contains(document.getElementById(name)))) && agentFields.every(key => now.get(key) === current.get(key))) {
        const target = document.getElementById('AGENT_KEYS'), replacement = fresh.getElementById('AGENT_KEYS');
        target.replaceWith(replacement);
        const updated = snapshot();
        for (const [key, value] of updated) {
          if (key.startsWith('agent_key_') || [...replacement.querySelectorAll('[name]')].some(input => input.name === key)) saved.set(key, value);
        }
      }
      say('');
      notify(result.message && result.message !== 'Saved' ? result.message : message);
      document.dispatchEvent(new CustomEvent('nullius-settings-saved', {detail: {
        removedKeys: [...names].filter(name => body.get('clear:' + name) === '1' && document.getElementById(name)?.dataset.secret === 'yes'),
        providers: [...fresh.querySelectorAll('[data-provider-card]')].map(card => ({
          key: card.dataset.providerCard, configured: card.dataset.configured, ready: card.dataset.ready,
          label: card.querySelector('[data-provider-state]')?.textContent ?? '',   // the new-endpoint form has no state line
          tested: card.querySelector('[data-provider-test-state]')?.textContent, mark: card.querySelector('[data-provider-test-state]')?.dataset.state ?? ''
        }))
      }}));
      return true;
    } catch (error) {
      say(error.message || 'Settings could not be saved. Try again.', true, error.retryable !== false);
      return false;
    } finally {
      form.removeAttribute('aria-busy');
    }
  }
  async function flush() {
    if (pending) { const ok = await pending; return ok ? flush() : false; }
    if (!changes(snapshot()).length) { say(''); return true; }
    pending = write();
    let ok;
    try { ok = await pending; } finally { pending = null; }
    return ok && changes(snapshot()).length ? flush() : ok;
  }
  window.nulliusSettings = {flush, dirty: () => changes(snapshot()).length > 0};
  // Replace, type, then Keep: moving to Keep blurs the input, whose change
  // would save the text Keep is about to throw away. A change whose focus
  // stays in the field (Keep, or Remove on the way to it) waits until focus
  // leaves the field; Keep has emptied the input by then.
  const replacing = input => input.name?.startsWith('value:') && !input.hidden
    && input.closest('.st-field')?.querySelector('[data-replace]') ? input.closest('.st-field') : null;
  form.addEventListener('change', event => {
    if (event.target.closest('[data-key-new]')) return; // Add/Cancel or leaving the key editor owns this draft.
    const field = replacing(event.target);
    if (!field) { void flush(); return; }
    setTimeout(() => {
      if (!field.contains(document.activeElement) || document.activeElement === event.target) { void flush(); return; }
      const leave = e => {
        if (field.contains(e.relatedTarget)) return;
        field.removeEventListener('focusout', leave);
        void flush();
      };
      field.addEventListener('focusout', leave);
    }, 0);
  });
  form.addEventListener('submit', event => { event.preventDefault(); void flush(); });
  window.addEventListener('beforeunload', event => {
    if (changes(snapshot()).length) { event.preventDefault(); event.returnValue = ''; }
  });
})();
