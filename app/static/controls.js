// Keep real selects as form/state owners; render a consistent, keyboard-accessible menu.
(() => {
  const controls = new Map(); let active = null, serial = 0, scheduled = false;
  function close(focus = false) {
    if (!active) return;
    const {menu, button} = active; active = null; menu.remove();
    button.setAttribute('aria-expanded', 'false'); button.removeAttribute('aria-activedescendant');
    if (focus && button.isConnected) button.focus();
  }
  function open(select, button) {
    close();
    const menu = document.createElement('div'); menu.className = 'ui-select-menu'; menu.id = button.getAttribute('aria-controls');
    menu.setAttribute('role', 'listbox'); menu.setAttribute('popover', 'manual');
    menu.setAttribute('aria-label', button.getAttribute('aria-label'));
    const rows = [];
    [...select.options].forEach((option, index) => {
      if (option.hidden) return;
      const row = document.createElement('div'); row.className = 'ui-select-option'; row.id = menu.id + '-' + index;
      row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(option.selected));
      row.setAttribute('aria-disabled', String(option.disabled || option.parentElement.disabled === true));
      row.textContent = option.textContent; row.dataset.index = index;
      row.addEventListener('pointerdown', event => event.preventDefault());
      row.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); choose(index); });
      menu.append(row); rows.push(row);
    });
    (select.closest('dialog[open]') || document.body).append(menu);
    active = {select, button, menu, rows, index: select.selectedIndex, typed: '', typedAt: 0};
    button.setAttribute('aria-expanded', 'true');
    const rect = button.getBoundingClientRect();
    menu.style.width = Math.min(Math.max(rect.width, 180), innerWidth - 16) + 'px';
    menu.style.left = Math.max(8, Math.min(rect.left, innerWidth - parseFloat(menu.style.width) - 8)) + 'px';
    const below = innerHeight - rect.bottom - 12, above = rect.top - 12;
    const down = below >= Math.min(260, rows.length * 34 + 10) || below >= above;
    menu.style.maxHeight = Math.max(64, Math.min(260, down ? below : above)) + 'px';
    if (down) menu.style.top = rect.bottom + 5 + 'px'; else menu.style.bottom = innerHeight - rect.top + 5 + 'px';
    if (menu.showPopover) menu.showPopover();
    highlight(select.selectedIndex);
  }
  function highlight(index) {
    if (!active) return;
    const row = active.rows.find(r => Number(r.dataset.index) === index && r.getAttribute('aria-disabled') !== 'true');
    if (!row) return;
    active.index = index;
    active.rows.forEach(r => r.classList.toggle('is-active', r === row));
    active.button.setAttribute('aria-activedescendant', row.id); row.scrollIntoView({block: 'nearest'});
  }
  function choose(index) {
    if (!active) return;
    const {select, rows} = active;
    if (!rows.some(r => Number(r.dataset.index) === index && r.getAttribute('aria-disabled') !== 'true')) return;
    close(true); select.selectedIndex = index;
    select.dispatchEvent(new Event('input', {bubbles: true})); select.dispatchEvent(new Event('change', {bubbles: true}));
    scan();
  }
  function keydown(event, select, button) {
    if (event.key === 'Escape' && active) { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === 'Tab') { close(); return; }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' '].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      if (!active || active.button !== button) { open(select, button); return; }
      if (event.key === 'Enter' || event.key === ' ') { choose(active.index); return; }
      const enabled = active.rows.filter(r => r.getAttribute('aria-disabled') !== 'true').map(r => Number(r.dataset.index));
      const i = enabled.indexOf(active.index);
      highlight(enabled[event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 : Math.max(0, Math.min(enabled.length - 1, i + (event.key === 'ArrowDown' ? 1 : -1)))]);
    } else if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault(); if (!active) open(select, button);
      active.typed = Date.now() - active.typedAt > 700 ? event.key : active.typed + event.key; active.typedAt = Date.now();
      const row = active.rows.find(r => r.getAttribute('aria-disabled') !== 'true' && r.textContent.trim().toLowerCase().startsWith(active.typed.toLowerCase()));
      if (row) highlight(Number(row.dataset.index));
    }
  }
  function scan() {
    scheduled = false;
    if (active && (!active.select.isConnected || active.select.disabled)) close();
    for (const [select, button] of controls) if (!select.isConnected) { button.remove(); controls.delete(select); }
    document.querySelectorAll('select:not([multiple])').forEach(select => {
      if (select.size > 1) return;
      let button = controls.get(select);
      if (!button) {
        button = document.createElement('button'); button.type = 'button'; button.className = 'ui-select-trigger';
        button.setAttribute('role', 'combobox'); button.setAttribute('aria-haspopup', 'listbox'); button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', 'ui-options-' + (++serial));
        const label = select.labels?.[0]?.cloneNode(true);
        label?.querySelectorAll('select, input, button, small').forEach(n => n.remove());
        button.setAttribute('aria-label', select.getAttribute('aria-label') || label?.textContent.trim() || select.name || 'Choose an option');
        select.classList.add('ui-select-native'); select.setAttribute('aria-hidden', 'true'); select.tabIndex = -1;
        select.after(button); controls.set(select, button);
        button.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); if (active?.button === button) close(); else open(select, button); });
        button.addEventListener('keydown', e => keydown(e, select, button));
        select.addEventListener('invalid', e => { e.preventDefault(); button.focus(); button.setAttribute('aria-invalid', 'true'); });
        select.addEventListener('change', () => { button.removeAttribute('aria-invalid'); scan(); });
      }
      const text = select.selectedOptions[0]?.textContent || 'Select…';
      if (button.textContent !== text) button.textContent = text;
      if (button.disabled !== select.disabled) button.disabled = select.disabled;
      if (button.hidden !== select.hidden) button.hidden = select.hidden;
    });
    // Labels and help stay together on the left; compact fields sit on the right.
    // Benchmark option controls already sit below their section heading.
    document.querySelectorAll('label.nb-field:not(.ui-field-row):not(.std-optselect)').forEach(label => {
      if (![...label.children].some(n => n.matches('select,input:not([type=checkbox]):not([type=radio]),textarea,.nb-model-control'))) return;
      const copy = document.createElement('span'); copy.className = 'ui-field-copy';
      [...label.childNodes].filter(n => n.nodeType === Node.TEXT_NODE || n.nodeType === Node.ELEMENT_NODE && n.matches('small')).forEach(n => copy.append(n));
      label.prepend(copy); label.classList.add('ui-field-row');
      // Keep the accessible label without making the entire form row a click target.
      label.addEventListener('click', event => {
        if (!event.target.closest('input, select, textarea, button, a')) event.preventDefault();
      });
    });
  }
  new MutationObserver(() => { if (!scheduled) { scheduled = true; requestAnimationFrame(scan); } }).observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'selected', 'hidden', 'label']});
  document.addEventListener('pointerdown', e => { if (active && !active.menu.contains(e.target) && !active.button.contains(e.target)) close(); });
  document.addEventListener('scroll', e => { if (active && !active.menu.contains(e.target)) close(); }, true);
  window.addEventListener('resize', () => close());
  document.addEventListener('reset', () => requestAnimationFrame(scan));
  scan();
})();
