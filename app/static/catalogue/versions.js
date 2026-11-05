/* Version links still load the server's complete definition. Preserve the
   reader's position only for this explicit navigation, not unrelated visits. */
(() => {
  const nav = document.querySelector('.std-versions');
  if (!nav) return;
  const key = 'nullius-version-scroll';
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
    sessionStorage.removeItem(key);
    if (saved && saved.url === location.href && Number.isFinite(saved.y)) {
      const restore = () => window.scrollTo({left: saved.x, top: saved.y, behavior: 'instant'});
      restore();
      // Font loading can change the height above the selected version.
      document.fonts.ready.then(restore);
    }
  } catch (_) { /* Storage restrictions must not break ordinary links. */ }
  nav.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    try {
      sessionStorage.setItem(key, JSON.stringify({
        url: link.href, x: window.scrollX, y: window.scrollY
      }));
    } catch (_) { /* The version remains accessible without storage. */ }
  });
})();
