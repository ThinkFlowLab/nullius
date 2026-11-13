// Notifications belong to the main viewport, including saves made in Settings.
(() => {
  function host() {
    return [...document.querySelectorAll('dialog[open]')].at(-1) || document.body;
  }
  window.nulliusNotify = (message, {error = false, key = ''} = {}) => {
    if (window.parent !== window) {
      try { if (window.parent.nulliusNotify) return window.parent.nulliusNotify(message, {error, key}); } catch (_) {}
    }
    let tray = document.querySelector('.toasts');
    if (!tray) {
      tray = document.createElement('div'); tray.className = 'toasts';
      tray.setAttribute('role', 'status'); tray.setAttribute('aria-live', 'polite');
    }
    tray.setAttribute('popover', 'manual');
    if (tray.parentElement !== host()) host().append(tray);
    if (key) [...tray.children].filter(t => t.dataset.noticeKey === key).forEach(t => t.remove());
    const toast = document.createElement('div'); toast.className = 'toast ' + (error ? 'bad' : 'ok'); toast.dataset.noticeKey = key;
    const text = document.createElement('span'); text.className = 'toast-text'; text.textContent = message;
    const close = document.createElement('button'); close.type = 'button'; close.className = 'toast-close'; close.setAttribute('aria-label', 'Dismiss'); close.textContent = '×';
    const bar = document.createElement('span'); bar.className = 'toast-bar'; bar.setAttribute('aria-hidden', 'true');
    const dismiss = () => { toast.remove(); if (!tray.children.length) tray.remove(); };
    close.addEventListener('click', dismiss); bar.addEventListener('animationend', dismiss);
    toast.append(text, close, bar); tray.append(toast);
    if (tray.showPopover) { if (tray.matches(':popover-open')) tray.hidePopover(); tray.showPopover(); }
  };
  // Every request of a page goes through here. A server that serves other code than the page was
  // rendered with, or no longer holds its form token (a restart), gets one notice: the page is out of date.
  const rendered = document.querySelector('meta[name=nullius-interface]')?.content;
  let told = false;
  const outOfDate = () => window.nulliusNotify('This page is out of date. Reload it.', {error: true, key: 'out-of-date'});
  window.nulliusFetch = async (url, options) => {
    const response = await fetch(url, options);
    const served = response.headers.get('X-Nullius-Interface');
    if (rendered && served && served !== rendered && !told) { told = true; outOfDate(); }
    if (response.status === 403 && response.headers.get('X-Nullius-Reason') === 'stale-form-token') {
      outOfDate();
      return new Promise(() => {});  // never settles: the caller's own error would only repeat the notice
    }
    return response;
  };
  // Keep a notification visible and operable when a dialog closes beneath it.
  new MutationObserver(() => {
    const tray = document.querySelector('.toasts');
    if (tray && tray.parentElement !== host()) {
      host().append(tray); if (tray.showPopover && !tray.matches(':popover-open')) tray.showPopover();
    }
  }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['open']});
})();
