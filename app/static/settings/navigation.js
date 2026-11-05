// Category search never inspects field values or credentials.
(() => {
  const sections = [...document.querySelectorAll('.st-section')];
  const links = [...document.querySelectorAll('.st-index [data-section]')];
  const search = document.getElementById('settings-search');
  function show(id) {
    sections.forEach(section => { section.hidden = section.id !== id; });
    links.forEach(link => link.setAttribute('aria-current', link.dataset.section === id ? 'page' : 'false'));
  }
  function fromHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { id = ''; }
    id = {other: 'general', file: 'storage', 'other-vars': 'providers'}[id] || id;
    const target = document.getElementById(id);
    const section = target?.closest('.st-section');
    show(section?.id || 'providers');
    if (target && target !== section) target.scrollIntoView({block: 'center'});
  }
  search.addEventListener('input', () => {
    const words = search.value.trim().toLowerCase().split(/\s+/);
    links.forEach(link => {
      const haystack = (link.textContent + ' ' + link.dataset.search).toLowerCase();
      link.hidden = !words.every(word => haystack.includes(word));
    });
    document.querySelectorAll('.st-nav-group').forEach(group => {
      group.hidden = [...group.querySelectorAll('[data-section]')].every(link => link.hidden);
    });
    document.querySelector('.st-search-empty').hidden = links.some(link => !link.hidden);
  });
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const first = links.find(link => !link.hidden);
      first?.click();
      first?.focus();
    }
  });
  window.addEventListener('hashchange', fromHash);
  fromHash();

  const choices = [...document.querySelectorAll('[data-theme-choice]')];
  function themeState() {
    let saved;
    try { saved = localStorage.getItem('theme'); } catch {}
    const selected = saved === 'dark' ? 'charcoal' : ['light', 'charcoal', 'graphite', 'midnight'].includes(saved) ? saved : 'system';
    choices.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === selected)));
  }
  choices.forEach(button => button.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('nullius-theme-change', {detail: button.dataset.themeChoice}));
    themeState();
  }));
  document.addEventListener('nullius-theme-updated', themeState);
  window.addEventListener('storage', themeState);
  themeState();
})();
