// Add a Harbor benchmark: "Read from Harbor" fills what Harbor knows (the task
// count, the limits a task declares, a key and title from the name). The form
// saves as a plain post.
(() => {
  const form = document.getElementById('nb-form');
  if (!form) return;
  const ref = document.getElementById('nb-ref'), said = document.getElementById('nb-said'), button = document.getElementById('nb-read');
  const field = name => form.elements[name];
  const source = () => form.querySelector('input[name=source]:checked')?.value || 'hub';
  const fill = (name, value, always) => {
    const el = field(name);
    if (el && value != null && value !== '' && (always || !el.value)) el.value = value;
  };

  const placeholder = () => { ref.placeholder = source() === 'hub' ? 'org/name' : 'name@version'; };
  form.querySelectorAll('input[name=source]').forEach(r => r.addEventListener('change', placeholder));
  placeholder();

  button.addEventListener('click', async () => {
    button.disabled = true; said.className = 'nb-said'; said.textContent = 'Asking Harbor…';
    try {
      const r = await fetch(form.dataset.read, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': field('csrf').value},
                                                body: JSON.stringify({source: source(), ref: ref.value})});
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'HTTP ' + r.status);
      fill('key', j.key); fill('title', j.title); fill('domain', j.description);
      fill('tasks', j.tasks, true);
      for (const [k, s] of Object.entries(j.timeouts)) fill('timeout_' + k, s, true);
      said.className = 'nb-said ok';
      said.textContent = `${j.tasks} tasks${j.version ? ' · version ' + j.version : ''} · limits from ${j.task}'s task.toml; agent setup is Harbor's default`;
    } catch (e) {
      said.className = 'nb-said bad'; said.textContent = String(e.message || e);
    } finally { button.disabled = false; }
  });

  // A field speaks below itself only when it has something to say; Save stays blocked while one says it is wrong.
  const say = (el, msg, text, bad) => { msg.innerHTML = text; msg.hidden = !text; msg.classList.toggle('bad', !!bad); el.classList.toggle('invalid', !!bad); };
  const key = document.getElementById('nb-key'), keyMsg = document.getElementById('nb-key-msg');
  const taken = new Set(JSON.parse(form.dataset.keys || '[]'));
  const checkKey = () => {
    const v = key.value.trim(), was = key.dataset.was;
    if (key.disabled || !v) return say(key, keyMsg, '', false), true;
    if (!/^[a-z0-9][a-z0-9-]*$/.test(v)) return say(key, keyMsg, 'Lowercase letters, digits and dashes only, starting with a letter or digit.', true), false;
    if (v === 'new' || (v !== was && taken.has(v))) return say(key, keyMsg, `<b>${v}</b> is taken: another benchmark uses that key.`, true), false;
    say(key, keyMsg, was && v !== was ? `Saving moves the folder to benchmarks/${v}/.` : '', false);
    return true;
  };
  const logo = document.getElementById('nb-logo'), logoMsg = document.getElementById('nb-logo-msg'), logoSaid = logoMsg.innerHTML, logoHidden = logoMsg.hidden;
  const checkLogo = () => {
    const f = logo.files[0];
    if (!f) { logoMsg.innerHTML = logoSaid; logoMsg.hidden = logoHidden; logoMsg.classList.remove('bad'); logo.classList.remove('invalid'); return true; }
    if (!/\.(png|jpe?g|svg)$/i.test(f.name)) return say(logo, logoMsg, 'A PNG, JPEG or SVG file.', true), false;
    if (f.size > Number(logo.dataset.max)) return say(logo, logoMsg, `${Math.ceil(f.size / 1000)} KB: keep it under ${Number(logo.dataset.max) / 1000} KB (a 64 px image is plenty).`, true), false;
    return say(logo, logoMsg, '', false), true;
  };
  key.addEventListener('input', checkKey); logo.addEventListener('change', checkLogo);
  checkKey();
  form.addEventListener('submit', e => {
    if (!checkKey() | !checkLogo()) { e.preventDefault(); form.querySelector('.invalid')?.focus(); }
  });

  // A Harbor route for an existing benchmark: only the dataset's fields are asked; the benchmark's are its own
  // already, so they are hidden and disabled (a disabled field is neither required nor sent).
  const routeFor = document.getElementById('nb-route-for');
  if (routeFor) {
    const route = () => {
      const on = !!routeFor.value;
      form.querySelectorAll('.nb-entry').forEach(el => { el.hidden = on; el.querySelectorAll('input, select, textarea').forEach(i => { i.disabled = on; }); });
      document.getElementById('nb-route-msg').hidden = !on;
      if (on) form.querySelector('.nb-more')?.setAttribute('open', '');
      const title = routeFor.selectedOptions[0].textContent.replace(/: run it with Harbor too$/, '');
      form.elements.dataset_key.placeholder = on ? routeFor.value + '-harbor' : "the benchmark's key";
      form.elements.dataset_title.placeholder = on ? title + ' on Harbor' : "the benchmark's title";
    };
    routeFor.addEventListener('change', route);
    route();
  }
})();
