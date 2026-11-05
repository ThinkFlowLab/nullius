(() => {
  const modal = document.getElementById('settings-modal');
  const frame = document.getElementById('settings-frame');
  const closeButton = modal.querySelector('[data-settings-close]');
  let opener, baseline = '', closing = false;
  const values = () => {
    const form = frame.contentDocument?.getElementById('settings-form');
    return form ? JSON.stringify([...new FormData(form)]) : '';
  };
  async function close() {
    if (closing) return;
    closing = true;
    try {
      const doc = frame.contentDocument;
      const hostDraft = doc?.querySelector('#add-host[open]');
      if (hostDraft) { hostDraft.close(); return; }
      const autosave = frame.contentWindow?.nulliusSettings;
      if (autosave && !await autosave.flush()) return;
      if ((!autosave && values() !== baseline) && !window.confirm('Discard unsaved settings changes?')) return;
      modal.close();
    } finally { closing = false; }
  }
  modal.addEventListener('click', event => {
    const box = modal.getBoundingClientRect();
    if (event.target === modal && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) void close();
  });
  closeButton.addEventListener('click', close);
  modal.addEventListener('cancel', event => { event.preventDefault(); close(); });
  modal.addEventListener('close', () => {
    frame.removeAttribute('data-ready');
    frame.removeAttribute('src');
    baseline = '';
    opener?.focus();
  });
  frame.addEventListener('load', () => {
    const doc = frame.contentDocument;
    if (!doc?.body.classList.contains('settings-embedded')) return;
    doc.documentElement.dataset.theme = document.documentElement.dataset.theme;
    doc.documentElement.dataset.palette = document.documentElement.dataset.palette;
    // Reveal only after the document and its styles have loaded in our theme.
    frame.dataset.ready = 'true';
    let onboarding = {};
    try { onboarding = JSON.parse(localStorage.getItem('nullius-onboarding')) || {}; } catch {}
    const restore = doc.getElementById('onb-restore');
    if (restore) restore.hidden = !onboarding.dismissed;
    const state = doc.getElementById('onb-state');
    if (state) state.textContent = onboarding.dismissed ? 'Removed from the rail.' : !onboarding.asked ? 'Not started yet.' : onboarding.open ? 'Open on the right.' : 'In the rail.';
    doc.addEventListener('nullius-theme-change', event => {
      document.dispatchEvent(new CustomEvent('nullius-theme-change', {detail: event.detail}));
    });
    baseline = values();
    closing = false;
    closeButton.disabled = false;
    doc.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !doc.querySelector('dialog[open]')) {
        event.preventDefault();
        close();
      }
    });
    // Onboarding belongs to the page beneath Settings, not the embedded document.
    doc.addEventListener('click', async event => {
      const control = event.target.closest('[data-onb]');
      if (!control) return;
      event.preventDefault();
      await close();
      if (modal.open) return;
      const proxy = document.createElement('button');
      proxy.dataset.onb = control.dataset.onb;
      proxy.hidden = true;
      document.body.append(proxy);
      proxy.click();
      proxy.remove();
    });
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.download) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.pathname !== '/settings' || location.pathname === '/settings') return;
    event.preventDefault();
    opener = link;
    baseline = '';
    closing = false;
    closeButton.disabled = false;
    url.searchParams.set('embedded', '1');
    frame.removeAttribute('data-ready');
    frame.src = url.href;
    modal.showModal();
    document.getElementById('settings-modal-title').focus();
  });
})();
