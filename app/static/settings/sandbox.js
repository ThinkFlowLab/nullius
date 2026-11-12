// The Sandbox section: docker's state, asked once when the section is first shown.
(() => {
  const section = document.getElementById('sandbox');
  if (!section) return;

  let asked = false;
  const load = async () => {
    asked = true;
    let j;
    try { j = await (await fetch(section.dataset.sandbox)).json(); } catch (err) { j = {docker: {ok: false, error: String(err)}}; }
    const state = section.querySelector('[data-docker-state]');
    state.textContent = j.docker.ok ? '✓ ' + j.docker.version : '✕ ' + j.docker.error;
    state.className = 'st-row-value ' + (j.docker.ok ? 'ok' : 'bad');
  };

  // Asked the first time the section is shown, not on every visit to Settings.
  const ask = () => { if (!asked && !section.hidden) load(); };
  new MutationObserver(ask).observe(section, {attributes: true, attributeFilter: ['hidden']});
  ask();
})();
