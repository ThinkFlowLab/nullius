// A skeleton over the page while the next one loads: shown only when the document itself navigates,
// a moment after the click or submit, so a fast page never flashes it.
(() => {
  const main = document.getElementById('main-content');
  if (!main) return;
  const status = document.createElement('span');
  status.className = 'sr';
  status.setAttribute('role', 'status');
  document.body.append(status);
  let timer = 0, skeleton = null;
  const html = '<div class="skel skel-title"></div><div class="skel skel-line"></div>'
    + '<div class="skel-row"><div class="skel skel-btn"></div><div class="skel skel-btn"></div><div class="skel skel-btn"></div></div>'
    + '<div class="skel skel-block"></div>'.repeat(5);
  function show() {
    skeleton = document.createElement('div');
    skeleton.className = 'page-skeleton';
    skeleton.setAttribute('aria-hidden', 'true');
    skeleton.innerHTML = html;
    main.prepend(skeleton);
    main.setAttribute('aria-busy', 'true');
    status.textContent = 'Loading';
  }
  function hide() {
    clearTimeout(timer);
    skeleton?.remove();
    skeleton = null;
    main.removeAttribute('aria-busy');
    status.textContent = '';
  }
  // Whether going to `url` in this tab replaces this document: same origin, and not a jump within it.
  function leaves(url, target) {
    if (target && target !== '_self') return false;
    let to;
    try { to = new URL(url, location.href); } catch { return false; }
    if (to.origin !== location.origin) return false;
    return !(to.hash && to.pathname === location.pathname && to.search === location.search);
  }
  // Checked when the delay is up, so a handler anywhere that prevented the default has had its turn.
  function later(event) {
    hide();
    timer = setTimeout(() => { if (!event.defaultPrevented) show(); }, 180);
  }
  addEventListener('click', event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const a = event.target.closest?.('a[href]');
    if (a && !a.hasAttribute('download') && leaves(a.href, a.target)) later(event);
  });
  addEventListener('submit', event => {
    // Attributes, not properties: a field named "action" or "target" shadows the form's own.
    const form = event.target, by = event.submitter, attr = name => by?.getAttribute('form' + name) || form.getAttribute(name);
    if ((attr('method') || '').toLowerCase() === 'dialog') return;
    if (leaves(attr('action') || location.href, attr('target'))) later(event);
  });
  // A page that asks before leaving: whether the user stays or goes, this navigation shows no skeleton.
  addEventListener('beforeunload', event => setTimeout(() => { if (event.defaultPrevented || event.returnValue) hide(); }));
  addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });   // Escape stops the load
  addEventListener('pageshow', event => { if (event.persisted) hide(); });          // back to this page, from the cache
})();
