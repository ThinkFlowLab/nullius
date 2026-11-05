(() => {
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const active = new WeakMap();
  function stop(element) {
    const running = active.get(element);
    if (!running) return;
    running.animation.onfinish = null;
    running.animation.cancel();
    element.style.overflow = running.overflow;
    active.delete(element);
  }
  function animate(element, start, end, finish) {
    const overflow = element.style.overflow;
    element.style.overflow = 'hidden';
    const animation = element.animate([{height: start + 'px'}, {height: end + 'px'}], {
      duration: 240, easing: 'cubic-bezier(.2, 0, 0, 1)'
    });
    active.set(element, {animation, overflow});
    animation.onfinish = () => {
      element.style.overflow = overflow;
      active.delete(element);
      finish();
    };
  }
  window.NulliusDisclosure = {
    setHidden(element, expanded) {
      const start = element.hidden ? 0 : element.getBoundingClientRect().height;
      stop(element);
      element.inert = !expanded;
      if (motion.matches) { element.hidden = !expanded; return; }
      element.hidden = false;
      const end = expanded ? element.getBoundingClientRect().height : 0;
      animate(element, start, end, () => { element.hidden = !expanded; });
    }
  };
  const eligible = details => !details.matches('.pop, .bench-delete, .facet details');
  function prepare(root) {
    const list = root.matches?.('details') ? [root] : [];
    list.push(...root.querySelectorAll('details'));
    list.filter(eligible).forEach(details => details.classList.add('slide-disclosure'));
  }
  prepare(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === 1) prepare(node);
  }))).observe(document.body, {childList: true, subtree: true});
  document.addEventListener('click', event => {
    const summary = event.target.closest('summary');
    const details = summary?.parentElement;
    if (!details?.matches('details') || !eligible(details) || event.defaultPrevented) return;
    if (event.target.closest('a, button, input, select, textarea')) return;
    event.preventDefault();
    const expanded = active.has(details) ? details.dataset.expanded !== 'true' : !details.open;
    const start = details.getBoundingClientRect().height;
    stop(details);
    details.dataset.expanded = String(expanded);
    summary.setAttribute('aria-expanded', String(expanded));
    if (motion.matches) { details.open = expanded; return; }
    details.open = expanded;
    const end = details.getBoundingClientRect().height;
    details.open = true;
    animate(details, start, end, () => { details.open = expanded; });
  });
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (details.matches?.('.slide-disclosure') && !active.has(details)) {
      details.dataset.expanded = String(details.open);
      details.querySelector(':scope > summary')?.setAttribute('aria-expanded', String(details.open));
    }
  }, true);
})();
