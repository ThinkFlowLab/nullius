// Constraints, in each machine's Constraints panel of the Run hosts list. Each edit rewrites the hidden JSON, which
// autosave saves. × empties one field, Remove all a machine's; an empty field is no constraint. Memory is kept in MB
// and shown in MB or GB. Where the capacity is known, a slider moves with CPUs and Memory: continuous from one step
// to a tick at the capacity, then one jump to No limit (the field empty) at the far end. A value above the capacity
// rests on the capacity. A save replaces the list (its warnings come from the server): the units are set again.
(() => {
  const GB = 1024;                         // Docker's m is MiB: 1 GB is 1024 MB
  const card = () => document.getElementById('HOSTS');
  const unitKey = input => 'nullius-constraint-unit:' + input.closest('[data-machine]').dataset.machine + ':' + input.dataset.key;
  const remembered = input => { try { return localStorage.getItem(unitKey(input)); } catch (e) { return null; } };
  const trim = n => String(Math.round(n * 1000) / 1000);
  const rangeOf = input => document.querySelector(`[data-range-for="${input.id}"]`);
  // the slider, in the field's unit: lo to the capacity, then a gap of a tenth of the track to No limit at `top`
  const scale = (input, range) => {
    const gb = input.dataset.unit === 'GB', step = gb ? 0.1 : Number(range.dataset.step);
    const cap = Number(input.dataset.capacity) / (gb ? GB : 1), lo = Math.min(step, cap), gap = Math.max(step, (cap - lo) / 9);
    // at or past the capacity: the capacity itself, which is not always on a step
    const value = p => p > cap + gap / 2 ? null : p >= cap ? cap : Math.max(lo, Math.min(cap, Math.round(p / step) * step));
    return {step, cap, lo, top: cap + gap, value};
  };
  // × shows while a value is set, as the value changes, not only once it is saved
  const clearOf = t => { const row = t.closest('.st-constraint'), input = row.querySelector('[data-key]'), x = row.querySelector('[data-clear-constraint]'); if (x) x.classList.toggle('off', !input.value.trim()); };
  const said = (input, v) => v === null ? 'No limit' : trim(v) + ' ' + (input.dataset.unit || rangeOf(input).getAttribute('aria-label'));
  const syncRange = input => {
    const range = rangeOf(input); if (!range) return;
    const s = scale(input, range), text = input.value.trim(), v = Number(text), ends = range.closest('[data-slider]');
    Object.assign(range, {min: String(s.lo), max: String(s.top), step: 'any'});
    range.value = String(text ? Math.min(v, s.cap) : s.top);
    range.setAttribute('aria-valuetext', text ? said(input, v) : 'No limit');
    ends.style.setProperty('--mark', String((s.cap - s.lo) / (s.top - s.lo || 1)));
    ends.classList.toggle('unset', !text);
    ends.hidden = false;
    window.nulliusRangeFill?.(range);
    ends.querySelector('[data-range-min]').textContent = said(input, s.lo);
    ends.querySelector('[data-range-max]').textContent = said(input, s.cap);
  };
  // the field and the slider at `v` (null: No limit)
  const slide = (range, v) => {
    const input = document.getElementById(range.dataset.rangeFor), s = scale(input, range);
    input.value = v === null ? '' : trim(v);
    range.value = String(v === null ? s.top : v);
    range.setAttribute('aria-valuetext', said(input, v));
    range.closest('[data-slider]').classList.toggle('unset', v === null);
    window.nulliusRangeFill?.(range);
    clearOf(input);
  };
  // a field kept in MB shown in `unit`, its tooltip's capacity too
  const setUnit = (input, unit) => {
    const text = input.value.trim();
    input.dataset.min ??= input.min;
    if (text && input.dataset.unit !== unit) input.value = unit === 'GB' ? trim(Number(text) / GB) : String(Math.round(Number(text) * GB));
    input.dataset.unit = unit;
    input.step = unit === 'GB' ? 'any' : '1';
    input.min = unit === 'GB' ? '0' : input.dataset.min;
    const cap = input.dataset.capacity;
    const has = cap ? 'This machine has ' + (unit === 'GB' ? trim(Number(cap) / GB) : cap) + ' ' + unit + '.' : '';
    input.title = [input.dataset.tip, has].filter(Boolean).join(' ');
    syncRange(input);
    input.closest('.st-constraint').querySelectorAll('[data-unit-choice]').forEach(b => {
      const on = b.dataset.unitChoice === unit; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on));
    });
  };
  // each field in the unit last chosen for it, else GB for whole gigabytes
  const units = () => {
    const c = card(); if (!c) return;
    c.querySelectorAll('input[data-unit]').forEach(i => setUnit(i, remembered(i) || (i.value && Number(i.value) % GB === 0 ? 'GB' : 'MB')));
    c.querySelectorAll('[data-range-for]').forEach(r => syncRange(document.getElementById(r.dataset.rangeFor)));
  };
  const value = input => {
    const text = input.value.trim();
    if (!text) return undefined;
    if (input.dataset.type === 'list') return text.toLowerCase() === 'none' ? [] : text.split(',').map(i => Number(i.trim()));
    if (input.dataset.unit === 'GB') return Math.round(Number(text) * GB);
    return input.dataset.type === 'str' ? text : Number(text);
  };
  const write = () => {
    const c = card(), hidden = c.querySelector('input[name="value:HOST_CONSTRAINTS"]');
    let doc = {};
    try { doc = JSON.parse(hidden.value || '{}'); } catch (e) { doc = {}; }
    c.querySelectorAll('[data-machine]').forEach(p => {
      const values = {};
      p.querySelectorAll('[data-key]').forEach(i => {
        const v = value(i);
        if (v !== undefined) values[i.dataset.key] = v;
        const clear = i.closest('.st-constraint').querySelector('[data-clear-constraint]');
        if (clear) clear.classList.toggle('off', v === undefined);
        syncRange(i);
      });
      const all = p.querySelector('[data-clear-machine]');
      if (all) all.hidden = !Object.keys(values).length;
      if (Object.keys(values).length) doc[p.dataset.machine] = values; else delete doc[p.dataset.machine];
    });
    hidden.value = JSON.stringify(doc);
    hidden.dispatchEvent(new Event('change', {bubbles: true}));
  };
  document.addEventListener('click', event => {
    const unit = event.target.closest('#HOSTS [data-unit-choice]');
    if (unit) {
      const input = unit.closest('.st-constraint').querySelector('[data-key]');     // the field, not the slider before it
      setUnit(input, unit.dataset.unitChoice);
      try { localStorage.setItem(unitKey(input), unit.dataset.unitChoice); } catch (e) {}
    }
    const clear = event.target.closest('#HOSTS [data-clear-constraint], #HOSTS [data-clear-machine]');
    if (clear) {
      const scope = clear.hasAttribute('data-clear-machine') ? clear.closest('[data-machine]') : clear.closest('.st-constraint');
      scope.querySelectorAll('[data-key]').forEach(i => { i.value = ''; });
      write();
    }
  });
  // before the form's own listener: the hidden value is current when autosave reads it
  document.addEventListener('change', event => { if (event.target.matches('#HOSTS [data-key], #HOSTS [data-range-for]')) write(); }, true);
  document.addEventListener('input', event => {
    const t = event.target;
    if (t.matches('#HOSTS [data-key]')) { clearOf(t); syncRange(t); }
    if (t.matches('#HOSTS [data-range-for]')) slide(t, scale(document.getElementById(t.dataset.rangeFor), t).value(Number(t.value)));
  });
  // keys move one step, and jump between the capacity and No limit
  document.addEventListener('keydown', event => {
    const t = event.target, d = {ArrowRight: 1, ArrowUp: 1, PageUp: 10, ArrowLeft: -1, ArrowDown: -1, PageDown: -10}[event.key];
    if (!d || !t.matches('#HOSTS [data-range-for]')) return;
    event.preventDefault();
    const input = document.getElementById(t.dataset.rangeFor), s = scale(input, t), text = input.value.trim();
    const v = text ? Math.min(Number(text), s.cap) : null;
    slide(t, v === null ? (d < 0 ? s.cap : null) : d > 0 && v >= s.cap ? null : s.value(Math.min(s.cap, v + d * s.step)));
    t.dispatchEvent(new Event('change', {bubbles: true}));
  });
  document.addEventListener('nullius-settings-saved', units);
  document.addEventListener('nullius-hosts-replaced', units);
  units();
})();
