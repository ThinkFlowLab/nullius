// The Add dialog (catalogue/add_dialog.html): open and close, its two tabs, the prompt filled as it is typed.
(() => {
  const d = document.getElementById('std-how');
  if (!d) return;
  document.getElementById('std-how-open').addEventListener('click', () => d.showModal());
  document.getElementById('std-how-close').addEventListener('click', () => d.close());
  d.addEventListener('click', e => { if (e.target === d) d.close(); });   // a click on the backdrop closes it, as Esc does

  // The two ways in, as tabs: arrows move between them, as a tablist's do.
  const tabs = [...d.querySelectorAll('[role=tab]')];
  const pick = t => tabs.forEach(x => {
    const on = x === t;
    x.setAttribute('aria-selected', on); x.tabIndex = on ? 0 : -1;
    document.getElementById(x.getAttribute('aria-controls')).hidden = !on;
  });
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => pick(t));
    t.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      pick(n); n.focus(); e.preventDefault();
    });
  });

  // Fill the prompt as it is typed: trim values and omit an empty Remark line.

  const field = id => document.getElementById(id);
  const out = field('std-add-prompt'), copy = field('std-add-copy'), hint = field('std-add-hint');
  const build = () => {
    const tpl = d.querySelector(`template[data-add-kind="${field('std-add-kind').value}"]`).content.textContent;
    const v = { name: field('std-add-name').value.trim(), source: field('std-add-source').value.trim(), remark: field('std-add-remark').value.trim() };
    const lines = tpl.split('\n').filter(l => v.remark || l !== 'Remark: {remark}');
    out.textContent = lines.join('\n').replace(/\{(name|source|remark)\}/g, (_, k) => v[k]);
    const ready = !!(v.name && v.source);
    copy.disabled = !ready; hint.hidden = ready;
  };
  ['std-add-name', 'std-add-source', 'std-add-remark'].forEach(id => field(id).addEventListener('input', build));
  field('std-add-kind').addEventListener('change', build);
  build();

  d.querySelectorAll('.std-copy').forEach(b => b.addEventListener('click', async () => {
    const text = b.closest('.std-code').querySelector('code').textContent;
    try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; } catch { b.textContent = 'Select it'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1500);
  }));
})();
