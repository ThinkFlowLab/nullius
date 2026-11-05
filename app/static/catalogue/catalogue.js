(() => {
  const hash = () => { try { return decodeURIComponent(location.hash.slice(1)); } catch { return ''; } };

  // Hash links (/benchmarks#key, /benchmarks#dataset) open the benchmark's page; category or origin chips and the
  // Find box keep the cards that match, and a shelf left empty folds away.
  const cards = [...document.querySelectorAll('.bench-card')];
  if (cards.length) {
    const key = hash();
    const owner = key && cards.find(c => c.dataset.keys.split(' ').includes(key));
    if (owner) {
      location.replace(owner.href + (key === owner.dataset.keys.split(' ')[0] ? '' : '#' + encodeURIComponent(key)));
      return;
    }
    const find = document.getElementById('bench-find');
    const chips = [...document.querySelectorAll('[data-filter] .bench-cat')];
    const none = document.getElementById('bench-none');
    // one value per filter group (category, origin); '' keeps every card
    const picked = {};
    const apply = () => {
      const words = (find?.value || '').toLowerCase().split(/\s+/).filter(Boolean);
      cards.forEach(c => {
        // Only runners are a space-separated list; categories may contain spaces.
        c.hidden = !(Object.entries(picked).every(([f, v]) => !v || (f === 'runs' ? (c.dataset[f] || '').split(' ').includes(v) : c.dataset[f] === v)) && words.every(w => c.dataset.find.includes(w)));
      });
      document.querySelectorAll('[data-shelf]').forEach(shelf => {
        const shown = shelf.querySelectorAll('.bench-card:not([hidden])').length;
        shelf.hidden = !shown;
        shelf.querySelector('.shelf-n').textContent = shown;
      });
      chips.forEach(chip => {
        const on = chip.dataset.value === (picked[chip.closest('[data-filter]').dataset.filter] || '');
        chip.classList.toggle('on', on);
        chip.setAttribute('aria-pressed', String(on));
      });
      if (none) none.hidden = cards.some(c => !c.hidden);
    };
    chips.forEach(chip => chip.addEventListener('click', () => { picked[chip.closest('[data-filter]').dataset.filter] = chip.dataset.value; apply(); }));
    find?.addEventListener('input', apply);
    return;
  }

  // A benchmark's datasets are tabs, with #<dataset key> selecting one.
  const lists = [...document.querySelectorAll('.bench-tabs')];
  const select = (list, tab, focus) => {
    list.querySelectorAll('[role=tab]').forEach(t => {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  };
  lists.forEach(list => {
    const tabs = [...list.querySelectorAll('[role=tab]')];
    list.hidden = false;
    list.closest('section')?.classList.add('tabbed');
    select(list, tabs.find(t => t.getAttribute('aria-selected') === 'true') || tabs[0]);
    list.addEventListener('click', e => {
      const tab = e.target.closest('[role=tab]');
      if (!tab) return;
      select(list, tab);
      history.replaceState(null, '', '#' + tab.getAttribute('aria-controls'));
    });
    list.addEventListener('keydown', e => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const at = tabs.indexOf(document.activeElement);
      select(list, tabs[(at + step + tabs.length) % tabs.length], true);
    });
  });
  const fromHash = () => {
    const key = hash();
    const tab = key && [...document.querySelectorAll('.bench-tabs [role=tab]')].find(t => t.getAttribute('aria-controls') === key);
    if (tab) select(tab.closest('.bench-tabs'), tab);
  };
  window.addEventListener('hashchange', fromHash);
  fromHash();
})();

// A code block's Copy button (a benchmark's machine setup): its code to the clipboard.
document.querySelectorAll('.bench-page .std-copy').forEach(b => b.addEventListener('click', async () => {
  const text = b.closest('.std-code').querySelector('code').textContent;
  try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; } catch { b.textContent = 'Select it'; }
  setTimeout(() => { b.textContent = 'Copy'; }, 1500);
}));
