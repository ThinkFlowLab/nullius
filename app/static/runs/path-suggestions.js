(() => {
  const list = document.getElementById('path-list');
  const menu = document.getElementById('path-suggestions');
  const csrf = document.querySelector('#pathform [name=csrf]').value;
  let field = null, timer, request, index = -1;

  function close() {
    clearTimeout(timer);
    request?.abort();
    request = null;
    menu.hidden = true;
    menu.replaceChildren();
    index = -1;
    if (field) {
      field.setAttribute('aria-expanded', 'false');
      field.removeAttribute('aria-activedescendant');
    }
  }

  function prepare(input) {
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', menu.id);
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  list.querySelectorAll('input').forEach(prepare);

  function highlight(next) {
    index = next;
    [...menu.children].forEach((row, i) => {
      row.classList.toggle('is-active', i === index);
      row.setAttribute('aria-selected', String(i === index));
    });
    const row = menu.children[index];
    field.setAttribute('aria-activedescendant', row.id);
    // Scroll the menu only, keeping the page and the input still.
    if (row.offsetTop < menu.scrollTop) menu.scrollTop = row.offsetTop;
    if (row.offsetTop + row.offsetHeight > menu.scrollTop + menu.clientHeight)
      menu.scrollTop = row.offsetTop + row.offsetHeight - menu.clientHeight;
  }

  function choose(row) {
    field.value = row.dataset.path;
    field.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function suggest(input, delay = 180, edge = 0) {
    close();
    field = input;
    prepare(input);
    const value = input.value;
    if (!value.trim()) return;
    timer = setTimeout(async () => {
      const controller = new AbortController();
      request = controller;
      try {
        const response = await nulliusFetch(list.dataset.suggestionsUrl, {
          method: 'POST', signal: controller.signal,
          headers: {'X-CSRF-Token': csrf}, body: new URLSearchParams({path: value})
        });
        if (!response.ok) return;
        const data = await response.json();
        if (controller.signal.aborted || field !== input || input.value !== value || document.activeElement !== input) return;
        if (!data.paths.length) return;
        menu.replaceChildren(...data.paths.map((path, i) => {
          const row = document.createElement('div');
          row.className = 'ui-select-option';
          row.id = `${menu.id}-${i}`;
          row.setAttribute('role', 'option');
          row.setAttribute('aria-selected', 'false');
          row.setAttribute('aria-label', path);
          row.dataset.path = path;
          row.title = path;
          const separator = path.slice(-1);
          row.textContent = path.slice(0, -1).split(separator).pop() + separator;
          return row;
        }));
        const rect = input.getBoundingClientRect();
        const below = window.innerHeight - rect.bottom - 12;
        const above = rect.top - 12;
        const up = below < 150 && above > below;
        menu.style.left = `${rect.left}px`;
        menu.style.width = `${rect.width}px`;
        menu.style.maxHeight = `${Math.min(260, Math.max(0, up ? above : below))}px`;
        menu.style.top = up ? 'auto' : `${rect.bottom + 4}px`;
        menu.style.bottom = up ? `${window.innerHeight - rect.top + 4}px` : 'auto';
        menu.hidden = false;
        field.setAttribute('aria-expanded', 'true');
        if (edge) highlight(edge < 0 ? menu.children.length - 1 : 0);
      } catch (_) { /* Typing can cancel a pending lookup; the path remains editable. */ }
    }, delay);
  }

  list.addEventListener('input', e => { if (e.target.matches('input')) suggest(e.target); });
  list.addEventListener('focusin', e => { if (e.target.matches('input')) suggest(e.target); });
  list.addEventListener('focusout', close);
  list.addEventListener('keydown', e => {
    if (!e.target.matches('input')) return;
    if (e.key === 'Escape') { close(); e.stopPropagation(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const direction = e.key === 'ArrowDown' ? 1 : -1;
      if (menu.hidden) { suggest(e.target, 0, direction); return; }
      highlight(index < 0 ? (direction < 0 ? menu.children.length - 1 : 0)
        : (index + direction + menu.children.length) % menu.children.length);
    } else if (e.key === 'Enter' && !menu.hidden && index >= 0) {
      e.preventDefault();
      choose(menu.children[index]);
    }
  });
  menu.addEventListener('pointerdown', e => e.preventDefault());
  menu.addEventListener('click', e => {
    const row = e.target.closest('[role=option]');
    if (row) choose(row);
  });
  window.addEventListener('resize', () => { if (!menu.hidden) close(); });
  document.addEventListener('scroll', e => { if (!menu.hidden && !menu.contains(e.target)) close(); }, true);
})();
