/* Keep the desktop navigation rail still while Material swaps the article. */
(() => {
  const desktop = window.matchMedia('(min-width: 76.25em)');
  const rail = () => document.querySelector('.md-sidebar--primary .md-sidebar__scrollwrap');
  let pending = null;

  document.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!desktop.matches || !link?.closest('.md-nav--primary') ||
        event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey ||
        event.altKey || link.target === '_blank' || link.hasAttribute('download')) return;
    const destination = new URL(link.href);
    if (destination.origin !== location.origin || destination.pathname === location.pathname) return;
    const current = rail();
    if (current) pending = { path: destination.pathname, top: current.scrollTop };
  }, true);

  document$.subscribe(() => {
    const saved = pending;
    pending = null;
    if (!saved || !desktop.matches || saved.path !== location.pathname) return;
    // Material lays out and centres the new sidebar on its first animation
    // frame. Restore after that pass, without changing the article's scroll.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (desktop.matches && saved.path === location.pathname) {
        const current = rail();
        if (current) current.scrollTop = saved.top;
      }
    }));
  });
})();
