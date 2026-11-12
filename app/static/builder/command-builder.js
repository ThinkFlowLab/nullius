/* Builder page controller: edit recipes, resolve their dependencies, and start an explicit Run. */
(() => {
  'use strict';
  const root = document.getElementById('nullius-builder');
  if (!root) return;
  const schema = JSON.parse(document.getElementById('nb-schema').textContent);
  const C = window.NulliusRecipe(schema), $ = s => root.querySelector(s);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const agentMarks = JSON.parse(root.dataset.agentMarks || '{}');
  const agentMark = (key, label) => {
    const source = agentMarks[String(key).split('@')[0]];
    if (!source) return esc(label[0]);
    return source.includes('#')
      ? `<svg class="agent-logo" aria-hidden="true"><use href="/static/${esc(source)}"/></svg>`
      : `<img class="agent-logo" src="/static/${esc(source)}" alt="" aria-hidden="true">`;
  };
  const steps = C.STEPS, S = C.STEP, REVIEW = S.review;
  const query = Object.fromEntries(new URLSearchParams(location.search));
  let recipe = C.prefill(query);
  let step = C.openingStep(query, recipe), tab = 'select';
  // What the page shows is kept in the browser as a draft on every change, so
  // leaving for another page and coming back finds the recipe as it was left.
  // A run's own recipe (?run=) or a prefill from a run's fields comes first.
  // A draft comes back silently: the page simply is as it was left.
  const draftKey = 'nullius.recipe-draft.v1';
  let opened = '';
  if (schema.loadRecipe) { try { recipe = C.fromJSON(schema.loadRecipe); step = REVIEW; opened = 'Opened the recipe of run “' + schema.loadRun + '”. Run gives it a new name.'; } catch (e) { opened = 'This run’s recipe cannot be read: ' + e.message; } }
  else if (schema.loadLike) { const o = C.fromJob(schema.loadLike); recipe = o.recipe; step = REVIEW; opened = o.notes.join(' '); }
  // An unsupported draft opens nothing; its rejection is shown as a toast.
  else { let d = null; try { d = C.unpackDraft(localStorage.getItem(draftKey), query); } catch {} if (d && d.recipe) { recipe = d.recipe; step = d.step; tab = d.tab; } else if (d && d.gone) opened = d.gone; }
  const saveDraft = () => { try { localStorage.setItem(draftKey, C.packDraft({recipe, step, tab})); } catch {} };
  let compiled, pendingDirs, dirsSequence = 0;
  const r = () => recipe;
  const storageKey = 'nullius.recipes.v1';   // one list for this browser; a run may open another workspace and the recipes stay
  // A message is a toast at the bottom right: a new one replaces it, its bar runs down and it goes, or its × closes it.
  // Shown again on each message, so it lands above a dialog opened since.
  const status = text => {
    const t = $('#nb-status'), bar = t && t.querySelector('.nb-toast-bar');
    if (!t) return;
    const show = on => {
      if (t.showPopover) { if (t.matches(':popover-open')) t.hidePopover(); if (on) t.showPopover(); }
      else t.hidden = !on;
    };
    if (!t.dataset.wired) {
      t.dataset.wired = '1';
      t.querySelector('.nb-toast-close').addEventListener('click', () => show(false));
      bar.addEventListener('animationend', () => show(false));
    }
    if (!text) { show(false); return; }
    show(true);
    t.querySelector('.nb-toast-text').textContent = text;
    bar.style.animation = 'none'; void bar.offsetWidth; bar.style.animation = '';
  };
  const button = (label,attrs='',cls='nb-button') => `<button type="button" class="${cls}" ${attrs}>${label}</button>`;
  // Which Harbor runs this recipe: the one it pins (Runner step), or a
  // development checkout typed as a path (a field appears) - labelled as
  // such; Run does not support them.
  const harborExecutableFields = rec => {
    const current = rec.harborDev ? 'dev' : 'path';
    const opts = [['path', 'The Harbor this recipe pins (Runner step)'], ['dev', 'Development checkout · a path, not a release']];
    return `<label class="nb-field">Harbor executable<select data-harbor-choice aria-label="Harbor executable">${opts.map(([v, t]) => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>`
      + (current === 'dev' ? field('Path to the development harbor', 'harborDev', rec.harborDev, {placeholder: '~/harbor-dev/.venv/bin/harbor', hint: 'Runs from it are labelled development, not acceptance evidence.'}) : '');
  };
  // The Harbor agents as cards, grouped by what they are for. Every selected
  // agent becomes one run command; the line above the cards says how many.
  const agentLabel = (id, rec = r()) => C.agentById(id, rec)?.label || id;
  const chip = (id, label, on, extra = '') => `<button type="button" class="nb-agentchip" data-agent="${esc(id)}" aria-pressed="${on}" ${extra}>${esc(label)}${on ? ' <span aria-hidden="true">✓</span>' : ''}</button>`;
  // The agent list: the vocabulary as chips, and custom agents as typed
  // import paths (each one an agent under evaluation).
  const agentCards = rec => {
    const on = id => rec.agents.includes(id);
    const n = rec.agents.length;
    const published = (schema.benchmarks.find(b => b.key === rec.benchmark)?.published || {}).agent;
    const vocab = C.agents().filter(a => !a.benchmarks || a.benchmarks.includes(rec.benchmark)).map(a => a.id === published ? {...a, kind:'reference'} : a);
    let html = `<div class="nb-agentline"><strong>${n} selected${n ? ' · ' + rec.agents.map(id => agentLabel(id, rec)).map(esc).join(', ') : ''}</strong></div>`;
    const ref = vocab.filter(a => a.kind === 'reference'), ctl = vocab.filter(a => a.kind === 'control');
    if (ref.length) html += `<div class="nb-agentrow"><h4>Benchmark reference</h4><div class="nb-chips">${ref.map(a => chip(a.id, a.label, on(a.id), `title="${esc(a.summary || '')}"`)).join('')}</div></div>`;
    html += `<div class="nb-agentrow"><h4>Under evaluation</h4><div class="nb-chips">${vocab.filter(a => a.kind === 'evaluated').map(a => chip(a.id, a.label, on(a.id), `title="${esc(a.summary || '')}"`)).join('')}`
      + rec.customAgents.map((p, i) => `<span class="nb-customagent">${chip('custom-' + i, 'custom', on('custom-' + i))}<input data-custom-agent="${i}" value="${esc(p)}" placeholder="package.module:AgentClass" spellcheck="false" aria-label="Custom agent ${i + 1} import path">${button('×', `data-remove-custom-agent="${i}"`, 'nb-remove')}</span>`).join('')
      + button('+ Custom agent', 'id="nb-add-custom-agent"', 'nb-subtab') + '</div></div>';
    // Harbor's own agents: what the recipe's Harbor registers, once it has been scanned (Runner step).
    const builtin = vocab.filter(a => a.kind === 'builtin');
    html += builtin.length ? `<div class="nb-agentrow"><h4>Harbor built-in</h4><div class="nb-chips">${builtin.map(a => chip(a.id, a.label, on(a.id), `title="${esc(a.summary || '')}"`)).join('')}</div></div>`
      : `<p class="nb-note">Harbor's own agents are offered once the Harbor this recipe pins has been scanned: Runner step, <em>Install</em>.</p>`;

    html += `<div class="nb-agentrow"><h4>Controls</h4><div class="nb-chips">${ctl.map(a => chip(a.id, a.label, on(a.id), `title="${esc(a.summary || '')}"`)).join('')}</div></div>`;
    return html;
  };
  // Harbor's per-trial settings, shared by every selected agent.
  // Time limits per phase, in minutes, when the benchmark's declared
  // timeouts are recorded; the multipliers Harbor takes are derived from them
  // and kept out of sight. A benchmark with nothing recorded gets the multipliers.
  const timeLimits = rec => {
    const t = C.effectiveTimeouts(rec);
    if (!t) return `<div class="nb-two">${field('Agent timeout multiplier','timeoutMultiplier',rec.timeoutMultiplier,{type:'number',min:0.01,hint:'This benchmark has no recorded timeouts, so only the multiplier can be set: 1 keeps what each task declares.'})}${field('Agent setup timeout multiplier', 'setupTimeoutMultiplier', rec.setupTimeoutMultiplier, {type: 'number', min: 0.01, placeholder: '1'})}</div>`
      + `<div class="nb-two">${field('Verifier timeout multiplier', 'verifierTimeoutMultiplier', rec.verifierTimeoutMultiplier, {type: 'number', min: 0.01, placeholder: '1'})}${field('Environment build timeout multiplier', 'buildTimeoutMultiplier', rec.buildTimeoutMultiplier, {type: 'number', min: 0.01, placeholder: '1'})}</div>`;
    const fmt=s=>s==null?'—':s>=3600?(s/3600).toFixed(s%3600?1:0)+' h':s>=60&&s%60===0?(s/60)+' min':s>=600?(s/60).toFixed(1)+' min':s+' s';
    return `<div class="nb-note"><table class="nb-timeouts nb-timelimits"><thead><tr><th>Phase</th><th>Declared by the tasks</th><th>Time limit · minutes</th></tr></thead><tbody>${t.phases.map(p=>`<tr><td>${esc(p.label)}</td><td>${fmt(p.declared)}</td><td><input type="number" min="0.1" step="any" data-minutes="${p.key}" value="${p.minutes ?? ''}" aria-label="${esc(p.label)} time limit in minutes"></td></tr>`).join('')}</tbody></table></div>`;
  };
  const commonSettings = rec => `<div class="nb-categoryhead"><h3>Common settings</h3><p>Every trial, whichever agent runs it.</p></div>`
    + `<div id="nb-timeouts">${timeLimits(rec)}</div>`
    + `<div class="nb-two">${field('Repeats per task', 'attempts', rec.attempts, {type: 'number', min: 1, placeholder: '1', hint: 'Every task is run this many times, pass or fail (Harbor\'s --n-attempts). Retries after a failure are on Limits.'})}<span></span></div>`
;
  // One tab per selected agent: the parameters this catalogue knows for it,
  // its extra --ak list, and what else it needs.
  const agentTab = (rec, id) => {
    const a = C.agentById(id, rec);
    let html = `<div class="nb-categoryhead"><h3>${esc(agentLabel(id, rec))}</h3><p>${esc(a?.summary || (a?.custom ? 'Your own agent class, passed to Harbor by import path.' : ''))}</p></div>`;
    if (id === 'tau3-llm-agent') html += parameterRows(id)
      + field('Adapter directory · on the run target', 'adapterPath:' + id, C.adapterPath(rec, id), {list: 'nb-paths', hint: 'Harbor’s source tree holds this agent (adapters/tau3-bench). The run checks this directory before it starts; ~ is the run target’s home.'});
    if (!a?.custom && id !== 'tau3-llm-agent') html += `<p class="nb-note">Harbor lists this agent\'s own options with <code>harbor agents schema ${esc(a?.arg || id)}</code>; this catalogue records none for it.</p>`;
    html += `<div class="fl">Extra --ak for ${esc(agentLabel(id, rec))} only</div><div id="nb-parameters">${customRows(id)}</div>`;
    return html;
  };
  // The standard's recipe as the server compiles it from the definitions (POST
  // /new-run/runner). Nothing typed here is a command; Run sends the choices.
  let runnerOut = null, runnerKey = '', runnerTimer;
  // Switching runner keeps the recipe being edited, one per runner, for as long as the page is open: switching back finds it as it was.
  const keptByRunner = {};
  // Nullius's own mark for the standard; Harbor's for Harbor
  const runnerMark = x => x.standard ? '<span class="brand-mark nb-nullius-mark" aria-hidden="true"></span>'
    : '<svg aria-hidden="true"><use href="/static/brand/marks.svg#harbor"/></svg>';
  // Two cards: Harbor, and Nullius (the standard) - a benchmark's own scripts run there, as its Original setup.
  const runnerCard = (x, on, label, summary, mark) => tile({attr: 'runner', key: x.key, on, mark, title: label, tip: summary || ''});
  const runnerStep = rec => {
    const has = C.runnersOf(C.benchKey(rec)), list = C.runners().filter(x => x.builtin && has.includes(x.key));
    const cards = list.length > 1 ? `<div class="nb-pick-grid">` + list.map(x => runnerCard(x, x.key === rec.harness, x.label, x.summary, runnerMark(x))).join('') + '</div>'
      : `<label class="nb-field ui-field-row"><span class="ui-field-copy">Runner</span><span class="nb-fixed">${esc((C.runners().find(x => x.key === rec.harness) || {}).label || rec.harness)}</span></label>`;
    return `<div class="nb-sectionhead"><h2>What runs it?</h2></div>` + cards + (rec.harness === 'harbor' ? harborPanel(rec) : '');
  };
  // Every benchmark of both runners in one list; picking one sets the runner that runs it (Nullius when both do).
  const benchmarkStep = rec => {
    const st = C.standard(), sb = C.standardBench(rec), on = C.benchKey(rec), seen = new Set(), tiles = [];
    const add = (key, title, fam, kind, summary) => { if (seen.has(key)) return; seen.add(key); const c = catOf(key);
      tiles.push({attr: 'bench', key, on: key === on, title: c.title || title,
        mark: `<svg aria-hidden="true"><use href="/static/catalogue/marks.svg#bm-${esc(key)}"/></svg>`, cat: c.category, origin: c.origin,
        fam, famLabel: fam === 'agent' ? 'Agents' : 'Capabilities · ' + kind, tip: c.domain || summary || '', meta: tasksOf(key),
        find: [c.title || title, key, c.category, c.domain, kind].join(' ')}); };
    st.benchmarks.forEach(x => add(x.key, x.label, x.family, x.kind, x.summary));
    C.benchmarks().forEach(x => add(x.key, x.title, 'agent', '', x.domain));
    tiles.sort((a, b) => a.title.localeCompare(b.title, 'en', {sensitivity: 'base'}));
    const versions = C.isStandard(rec) && sb && (sb.versions || []).length > 1
      ? `<label class="nb-field">Version of ${esc(sb.label)}<select data-std-version>${sb.versions.map(v => `<option value="${esc(v.ref)}" ${v.ref === rec.benchmark || (v.default && rec.benchmark === sb.key) ? 'selected' : ''}>${esc(v.version)}${v.default ? ' (the default)' : ''}</option>`).join('')}</select><small>Each version pins its own upstream, datasets and proofs.</small></label>` : '';
    return `<div class="nb-sectionhead"><h2>Choose a benchmark.</h2></div>` + picker(tiles, [['agent', 'Agents'], ['capability', 'Capabilities']]) + versions;
  };
  // Put the recipe on a runner, keeping the one each runner had: the benchmark follows.
  const toRunner = (target, key) => {
    if (target !== r().harness) { keptByRunner[r().harness] = r(); recipe = keptByRunner[target] || (target === 'harbor' ? C.create() : C.createStandard(key)); runnerOut = null; runnerKey = ''; tab = 'select'; }
    if (C.benchKey(r()) !== key) { if (C.isStandard(r())) C.setStandardBenchmark(r(), key); else C.setBenchmark(r(), key); }
  };
  // Branch, tag or commit first; then that kind's list, or the commit typed, for the
  // repository a Harbor is pinned from: `o.kindAttr` and `o.refAttr` name the data
  // attributes its two controls answer to.
  const KIND_LABEL = {branch: 'Branch', tag: 'Tag', commit: 'Commit'};
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const refPicker = o => {
    const got = o.got || {loading: true}, kind = o.kind || 'commit';
    const lists = {branch: got.branches || [], tag: got.tags || []}, totals = {branch: got.branches_total || 0, tag: got.tags_total || 0};
    // A segmented control: one pill slides to the kind chosen (placeThumb), a kind with nothing to offer is dimmed.
    const segments = ['branch', 'tag', 'commit'].map(k => {
      const unknown = k !== 'commit' && (got.loading || got.error), none = k !== 'commit' && !unknown && !totals[k];
      const count = k === 'commit' ? '' : `<span class="nb-seg-count" aria-label="${totals[k] || 'none'}">${totals[k] || '0'}</span>`;
      return `<button type="button" class="nb-seg-btn" role="radio" aria-checked="${k === kind}" data-${o.kindAttr}="${k}" ${(unknown || none) && k !== kind ? 'disabled' : ''}>${KIND_LABEL[k]}${count}</button>`;
    }).join('');
    const said = got.loading ? 'Reading branches and tags…' : got.error ? cap(got.error) + '.' : '';
    let body;
    if (kind === 'commit') body = o.commitField;
    else {
      const list = lists[kind], extra = list.some(x => x.name === o.revision) ? [] : [{name: o.revision, date: '', commit: ''}];
      const option = x => `<option value="${esc(x.name)}" ${x.name === o.revision ? 'selected' : ''}>${esc(x.name)}${kind === 'branch' && x.name === got.default_branch ? ' · default' : ''}${x.date ? ' · ' + esc(x.date) : ''}${x.commit ? ' · ' + esc(x.commit.slice(0, 12)) : ''}</option>`;
      body = `<label class="nb-field">${KIND_LABEL[kind]}<select data-${o.refAttr} aria-label="${KIND_LABEL[kind]}">${extra.concat(list).map(option).join('')}</select>`
        + `<small>${totals[kind] > list.length ? (kind === 'tag' ? 'The ' + list.length + ' newest of ' + totals[kind] + '. ' : 'The first ' + list.length + ' of ' + totals[kind] + ', the default first. ') : ''}${o.notes[kind]}</small></label>`;
    }
    return `<div class="nb-field"><span>Version of ${esc(o.source)}</span>${segmented(o.seg, 'Version kind', segments)}${said || o.after ? `<small>${esc(said)}${o.after || ''}</small>` : ''}</div>` + body;
  };
  // Which Harbor runs a Harbor recipe (C.harborPin): one of the five newest
  // releases of the official package on PyPI, the newest preselected, or a repository
  // - a private fork included - at a branch, tag or commit, listed with this
  // machine's git credentials. Read after the page loads, never blocking it.
  // Its agents are asked of it on an explicit choice (a release or a ref
  // picked, or Install): the first ask builds a repository's commit
  // here, later asks read what was kept. Nothing is polled.
  const harbor = {releases: null, refs: {}, scans: {}, kept: {}};
  const harborPanel = rec => {
    const h = rec.harbor || {kind: 'release', version: '', extras: []};
    const kinds = [['release', 'Release'], ['repo', 'Repository']].map(([k, t]) => `<button type="button" class="nb-seg-btn" role="radio" aria-checked="${h.kind === k}" data-harbor-kind="${k}">${t}</button>`).join('');
    let body = '';
    if (h.kind === 'repo') {
      const url = C.harborRepoUrl(h.repo), got = url ? harbor.refs[url] : null;
      body = `<label class="nb-field">Repository<input data-harbor-repo aria-label="Harbor repository" value="${esc(h.repo || '')}" autocomplete="off" spellcheck="false"><small>owner/name or a git URL</small></label>`;
      if (url) {
        const head = ((got && got.branches) || []).find(x => x.name === h.revision)?.commit;
        body += refPicker({got, kind: h.revisionKind, revision: h.revision, source: C.repoLabel(url), seg: 'harbor-ref', kindAttr: 'harbor-ref-kind', refAttr: 'harbor-ref',
          after: got && !got.loading ? ' ' + button('↻ List again', 'data-harbor-refs-fresh', 'nb-inlinebutton') : '',
          commitField: `<label class="nb-field">Commit<input data-harbor-commit aria-label="Harbor commit" value="${esc(h.commit || '')}" autocomplete="off" spellcheck="false"><small>The full 40-character commit: it is the pin.</small></label>`,
          notes: {branch: 'Pinned at the commit shown' + (h.commit ? ', ' + h.commit.slice(0, 12) : '') + ': the recipe keeps it, and a later push to the branch changes nothing until it is chosen again.' + (head && h.commit && head !== h.commit ? ' The branch has moved to ' + head.slice(0, 12) + ' since; choose it again to move the pin.' : ''),
            tag: 'Pinned at the commit the tag names' + (h.commit ? ', ' + h.commit.slice(0, 12) : '') + '.'}});
      }
    } else {
      const got = harbor.releases || {loading: true}, versions = got.versions || [];
      if (got.loading) body = `<p class="nb-note">Reading Harbor's releases on PyPI…</p>`;
      else if (!versions.length) body = `<label class="nb-field">Version<input data-harbor-version aria-label="Harbor version" value="${esc(h.version || '')}" autocomplete="off" spellcheck="false"><small>${esc(cap(got.error || 'PyPI lists no release'))}. Type a version of the harbor package on PyPI.</small></label>`;
      else {
        // the five newest, and the recipe's own version when it is older than them
        const shown = versions.slice(0, 5), extra = !h.version || shown.some(v => v.version === h.version) ? [] : [{version: h.version, date: ''}];
        body = `<label class="nb-field">Version<select data-harbor-version aria-label="Harbor version">${shown.concat(extra).map(v => `<option value="${esc(v.version)}" ${v.version === h.version ? 'selected' : ''}>${esc(v.version)}${v === versions[0] ? ' · newest' : ''}${v.date ? ' · ' + esc(v.date) : ''}</option>`).join('')}</select>`
          + `<small>The five newest releases on PyPI.</small></label>`;
      }
    }
    return `<div class="nb-harbor"><div class="nb-sectionhead"><h3>Which Harbor?</h3></div><div class="nb-field"><span>Harbor</span>${segmented('harbor-kind', 'Harbor source', kinds)}</div>${body}${harborScanLine(rec)}</div>`;
  };
  // The pinned Harbor: installed here once (a repository's commit built), then asked for the agents it registers.
  const harborScanLine = rec => {
    const pin = C.harborPin(rec);
    if (!pin) return '';
    const s = harbor.scans[C.harborKey(pin)] || {state: 'absent'}, ask = t => schema.local ? ' ' + button(t, 'data-harbor-scan', 'nb-inlinebutton') : '';
    let html;
    if (s.state === 'scanning') html = `<p class="nb-note nb-harbor-busy" role="status">${esc(pin.kind === 'repo' ? 'Building harbor from ' + C.repoLabel(pin.url) + ' at ' + pin.commit.slice(0, 12) + ' here, then asking it for its agents…' : 'Installing harbor ' + pin.version + ' and asking it for its agents…')} The first time takes a minute or two.</p>`;
    else if (s.state === 'asking') html = `<p class="nb-note">Looking for what was kept of this Harbor…</p>`;
    else if (s.state === 'failed') html = `<p class="nb-error">✕ ${esc(s.error || 'the scan failed')}</p><p class="nb-note">The builder offers its own agents until this Harbor answers.${ask('Try again')}</p>`;
    else if (s.state === 'ready') {
      const n = (s.agents || []).length;
      html = `<p class="nb-note">✓ ${esc(C.harborLabel({...pin, extras: []}))} installed · ${n} agent${n === 1 ? '' : 's'} on the Agent step</p>`;
    } else html = `<p class="nb-note">${esc(C.harborLabel({...pin, extras: []}))} is not installed here yet.${schema.local ? ask(pin.kind === 'repo' ? 'Build and install' : 'Install') : ' It is installed from a browser on the application host.'}</p>`;
    return html;
  };
  // The scan for the recipe's pin, when there is one, is what the agent list is merged with.
  const applyScan = () => {
    const rec = r(), pin = rec.harness === 'harbor' ? C.harborPin(rec) : null, s = pin ? harbor.scans[C.harborKey(pin)] : null;
    C.setScanned(s && s.state === 'ready' ? s.agents : null, pin && C.harborLabel({...pin, extras: []}));
  };
  const harborRedraw = () => { if (step === S.runner || step === S.agent || step === REVIEW) renderForm(); refreshPlan(); };
  async function askScan(build) {
    const rec = r(), pin = rec.harness === 'harbor' ? C.harborPin(rec) : null;
    applyScan();
    if (!pin) return;
    const key = C.harborKey(pin), had = harbor.scans[key];
    if (had && (['scanning', 'asking', 'ready'].includes(had.state) || !build)) return;
    if (build && !schema.local) return;
    harbor.scans[key] = {state: build ? 'scanning' : 'asking'};
    harborRedraw();
    let got;
    try {
      const res = build
        ? await fetch('/new-run/harbor/agents', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': schema.csrf}, body: JSON.stringify({harbor: pin})})
        : await fetch('/new-run/harbor/agents?' + new URLSearchParams({kind: pin.kind, version: pin.version || '', repo: pin.repo || '', revisionKind: pin.revisionKind || '', revision: pin.revision || '', commit: pin.commit || ''}));
      got = await res.json().catch(() => ({}));
      if (!got.state) got = {state: build ? 'failed' : 'absent', error: got.error || 'HTTP ' + res.status};
      if (!build && got.state === 'failed') got = {state: 'absent'};
    } catch (e) { got = {state: build ? 'failed' : 'absent', error: String(e)}; }
    harbor.scans[key] = got;
    applyScan();
    harborRedraw();
  }
  async function loadReleases() {
    if (harbor.releases) return;
    harbor.releases = {loading: true};
    let got;
    try { const res = await fetch('/new-run/harbor/releases'); got = await res.json(); } catch { got = {versions: [], error: 'PyPI could not be read'}; }
    harbor.releases = got;
    if (step === S.runner) { renderForm(); refreshPlan(); }
  }
  async function loadRefs(url, fresh = false) {
    if (!url || (harbor.refs[url] && !fresh) || !schema.local) return;
    harbor.refs[url] = {loading: true};
    if (step === S.runner) renderForm();
    let got;
    try {
      const res = await fetch('/new-run/harbor/refs?' + new URLSearchParams({repo: url, ...(fresh ? {fresh: '1'} : {})}));
      got = await res.json();
    } catch { got = {error: 'the branches and tags could not be read'}; }
    harbor.refs[url] = got;
    // a repository just named: its default branch, at its commit, to start with
    const h = r().harbor;
    if (h && h.kind === 'repo' && C.harborRepoUrl(h.repo) === url && !h.commit && h.revisionKind !== 'commit' && !got.error) {
      const list = h.revisionKind === 'tag' ? got.tags || [] : got.branches || [], pick = list.find(x => x.name === (h.revision || got.default_branch)) || list[0];
      if (pick) { h.revision = pick.name; h.commit = pick.commit; }
    }
    if (step === S.runner) renderForm();
    refreshPlan();
    askScan(false);
  }
  // On every render: what the recipe's Harbor needs read, each read once a page.
  const harborFollow = () => {
    const rec = r();
    if (rec.harness !== 'harbor') return applyScan();
    loadReleases();
    if (rec.harbor && rec.harbor.kind === 'repo') loadRefs(C.harborRepoUrl(rec.harbor.repo));
    askScan(false);
  };
  // A release not chosen yet becomes the newest, in front of the person: on the Runner step only.
  const preselectRelease = () => {
    const rec = r(), h = rec.harbor, versions = (harbor.releases && harbor.releases.versions) || [];
    if (step === S.runner && rec.harness === 'harbor' && h && h.kind === 'release' && !h.version && versions.length) h.version = versions[0].version;
  };
  // A segmented control: the segments are buttons with role radio; one pill
  // slides under the chosen one. A re-render draws a new control, so the pill
  // starts where that control's last one was and slides from there.
  // Harbor's own mark where Harbor fetches the tasks; a plain icon where no one's mark belongs. Square tiles, so
  // a mark is never read as a second radio button.
  const HARBOR_MARK = '<svg class="nb-radio-mark" aria-hidden="true"><use href="/static/brand/marks.svg#harbor"/></svg>';
  const iconTile = d => `<span class="nb-radio-mark nb-radio-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="${d}"/></svg></span>`;
  const SOURCE_MARK = { hub: HARBOR_MARK, registry: HARBOR_MARK,
    local: iconTile('M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'),
    generate: iconTile('M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M15 9h.01M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5') };
  const SOURCE_HINT = { hub: 'Harbor fetches the tasks when the run starts.', registry: 'Harbor fetches the tasks when the run starts.',
    local: 'Task folders already on disk. A run on a host is sent them.', generate: "Built first with the benchmark's own generator, into a folder you name." };
  const segmented = (name, label, segments) => `<div class="nb-seg" role="radiogroup" aria-label="${esc(label)}" data-seg="${name}"><span class="nb-seg-thumb" aria-hidden="true"></span>${segments}</div>`;
  const thumbAt = {};
  const placeThumb = () => {
    for (const seg of root.querySelectorAll('.nb-seg')) {
      const th = seg.querySelector('.nb-seg-thumb'), on = seg.querySelector('[aria-checked="true"]'), was = thumbAt[seg.dataset.seg];
      if (!th || !on) continue;
      const to = {left: on.offsetLeft, width: on.offsetWidth};
      if (was) { th.style.transition = 'none'; th.style.left = was.left + 'px'; th.style.width = was.width + 'px'; th.getBoundingClientRect(); th.style.transition = ''; }
      th.style.left = to.left + 'px'; th.style.width = to.width + 'px'; thumbAt[seg.dataset.seg] = to;
    }
  };
  // The provider is one whose key (a local endpoint: its URL) is set in Settings. One is said, nothing to choose;
  // several are a select of those alone; none, every provider. A recipe naming one
  // whose key is not set keeps it, marked, until another is chosen.
  const providerField = (rec, offered) => {
    const have = C.available().filter(p => offered.some(([x]) => x === p)), current = C.recipeProvider(rec);
    if (have.length === 1 && current === have[0])
      return `<div class="nb-field"><span>Provider</span><span class="nb-fixed">${esc(offered.find(([p]) => p === current)[1])}</span><small>The only one set in Settings.</small></div>`;
    return select('Provider', 'provider', current, !have.length ? offered
      : offered.filter(([p]) => have.includes(p) || p === current).map(([p, t]) => [p, have.includes(p) ? t : t + ' · not set in Settings']));
  };
  // Show only a missing-provider blocker; configured credentials need no confirmation line.
  const keyLine = rec => {
    const p = C.recipeProvider(rec), blocked = C.providerBlocker(rec), at = name => button('Settings', `data-href="/settings#${name}"`, 'nb-inlinebutton');
    if (blocked) return note('Not set in Settings', esc(blocked) + ' ' + at(C.PROVIDER_NEEDS[p] || 'MODEL_API_URL'));
    return '';
  };
  // The Nullius standard (C.isStandard): a benchmark, then an agent or a capability among every one there is - the
  // ones that have not proved the ports it needs shown greyed, with the reasons - a model and a scope. The server
  // compiles the choices (POST /new-run/runner, nullius/standard/recipe.py).
  // what a unit is: the benchmark's, or a setup's own when only setups run (a conversation, where the paper batches its questions)
  const unitsOf = rec => { const b = C.standardBench(rec) || {}, own = (b.setups || []).filter(x => C.chosenOf(rec).includes(x.key));
    return own.length && own.length === C.chosenOf(rec).length ? own[0].units : b.units || {noun: 'unit', nouns: 'units'}; };
  // the first clause of a long description, for a line; the whole goes in its title
  // settings as sub-tabs, one a chosen way of running that has any: [key, label, html]; the open one kept while it is there
  let setTab = '';
  const settingsTabs = panes => { panes = panes.filter(([, , h]) => h); if (!panes.length) return '';
    const open = panes.some(([k]) => k === setTab) ? setTab : panes[0][0];
    return `<div class="nb-sectionhead std-controlshead std-sethead"><h3>Settings</h3></div><nav class="nb-subtabs" aria-label="Settings">${panes.map(([k, l]) => button(esc(l), `data-set-tab="${esc(k)}" aria-pressed="${k === open}"`, 'nb-subtab')).join('')}</nav>`
      + panes.map(([k, , html]) => `<div data-settings-pane="${esc(k)}"${k === open ? '' : ' hidden'}>${html}</div>`).join(''); };
  // an option's key read as words: reasoning_effort -> Reasoning effort
  const words = k => { const t = String(k).replace(/[_-]+/g, ' ').trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
  // A control's proof on a dataset: ✓ with what passed (when, in its title), ✕ with the value the last try
  // reached against its bound, or not run. Its source (nullius/standard/definitions.py, SOURCES) is in its title.
  const SOURCE_SAYS = {official: 'from the authors', adapter: 'from Harbor\'s adapter', nullius: 'made by Nullius'};
  const bound = e => { const [k, v] = Object.entries(e || {})[0] || ['at_least', 1]; return [k === 'at_most' ? '>' : '<', v]; };
  function proofOf(proven, failed, expect) {
    if (proven) return {state: 'ok', text: '✓ ' + (proven.passed || '') + (proven.limit ? ' · first ' + proven.limit : ''), title: 'proven on ' + (proven.on || '?') + (proven.shard ? ', shard ' + proven.shard : '')};
    if (failed && failed.value !== undefined) { const [op, v] = bound(failed.expect || expect); return {state: 'no', text: '✕ ' + failed.value + ' ' + op + ' ' + v, title: 'not proven: ' + [failed.passed, failed.on, ...(failed.problems || [])].filter(Boolean).join(' · ')}; }
    if (failed) return {state: 'no', text: '✕ not proven', title: (failed.problems || []).join(' · ')};
    return {state: 'none', text: 'not run'};
  }
  const proofSpan = p => `<span class="std-proof std-proof-${p.state}"${p.title ? ` title="${esc(p.title)}"` : ''}>${esc(p.text)}</span>`;
  // A control or a check as a plain chip: its name and its proof; where the control comes from is in its title.
  const controlChip = (c, proof = true) => `<span class="std-ctl" title="${esc(c.key === 'nop' ? 'no answer at all: must score nothing' : c.key === 'oracle' ? 'the reference answers: must score everything' : '')} - ${esc(SOURCE_SAYS[c.source] || c.source)}: ${esc(c.from)}"><b>${esc(c.key)}</b>`
    + (proof ? proofSpan(proofOf(c.proven, c.failed, c.expect)) : '') + '</span>';
  const checkChip = (name, proven, failed, says) => { const p = proofOf(proven, failed); if (p.state === 'ok') { p.title = [says, proven.passed, p.title].filter(Boolean).join(' · '); p.text = '✓'; }
    return `<span class="std-ctl std-checkchip"><b>${esc(name)}</b>${proofSpan(p)}</span>`; };
  // The yes to an agent running as you: asked only for the local backend, where it is no sandbox.
  const consentBox = rec => `<label class="nb-field nb-consent"><span><input type="checkbox" data-std-unsandboxed ${rec.unsandboxed ? 'checked' : ''}> Run it as me, not sandboxed</span></label>`;
  // The standard's Where step: one machine or several (one shard each), and the backend on them.
  function standardWhere(rec) {
    const here = schema.standardHere || {}, list = C.whereList(rec), H = schema.hosts || [], b = C.standardBench(rec);
    const backend = rec.backend || 'local';
    const selected = C.standardOthers(rec).filter(o => C.chosenOf(rec).includes(o.key));
    const dockerWhy = list.includes('local') && here.docker ? 'Not on this machine: ' + here.docker : '';
    const agentRun = b && b.family === 'agent' && selected.some(o => !o.control && !o.setup);
    const machine = (v, t, sub) => machineTile('std-where', v, t, sub, list.includes(v));
    const how = (v, t, sub, why) => tile({attr: 'std-backend', key: v, on: backend === v && !why, disabled: !!why, why, note: why ? 'Not usable here' : '', mark: v === 'local' ? '$' : '▣', title: t, tip: sub});
    const dockerOnly = Object.entries((b || {}).environments || {}).filter(([, e]) => !e.local_ok).map(([n]) => n);
    const web = agentRun && Object.values((b || {}).environments || {}).some(e => e.network);   // an agent's public web proxy
    return '<div class="nb-sectionhead"><h2>Where does it run?</h2></div>'
      + `<div class="nb-pick-grid nb-where">${machine('local', 'This machine', 'Nullius runs it here, in runs/' + rec.name + '.')}${H.map(h => machine(h.entry, h.label, 'Over ssh: the standard\'s code at this commit is sent there, uv put there if missing; its job comes back as it runs.')).join('')}</div>`
      + (H.length ? (list.some(w => w !== 'local') ? button('Test connection', 'id="nb-test-host"') + ' <span id="nb-hosttest" class="nb-note"></span>' : '') : `<p class="nb-note">Hosts are named in ${button('Settings → Run hosts', 'data-href="/settings#HOSTS"', 'nb-inlinebutton')}; this machine is the only choice until then.</p>`)
      + '<div class="nb-sectionhead std-wherehead"><h3>How it runs there</h3></div>'
      + `<div class="nb-pick-grid std-backends">${how('local', 'Local processes', 'Its commands run as you, not sandboxed.', '')}`
      + how('docker', 'Docker', web ? 'Sandboxed: each unit in a container whose only ways out are its relay and a public web proxy.'
        : 'Sandboxed: each unit in a container whose only way out is its relay.', dockerWhy) + '</div>'
      + (agentRun && backend === 'local' ? consentBox(rec) : '')
      + (dockerOnly.length && backend === 'local' && agentRun ? `<p class="nb-note nb-error">${esc(dockerOnly.join(', '))}: ${dockerOnly.length > 1 ? 'these environments need' : 'this environment needs'} docker - on local processes their units are recorded as not run.</p>` : '')
      + hostSetup(rec);
  }
  // What the chosen options of the benchmark need: an embedding model, requirements of the machine (standard.json host).
  function optionNeeds(rec) {
    const b = C.standardBench(rec), out = {embeddings: [], host: []};
    if (!b) return out;
    for (const [n, o] of Object.entries(b.options || {})) {
      if (o.datasets.length && !C.datasetsOf(rec).some(id => o.datasets.includes(id))) continue;
      const v = (rec.bench_options || {})[n] ?? o.default, need = (o.needs || {})[v] || {};
      if (need.embeddings && !out.embeddings.includes(need.embeddings)) out.embeddings.push(need.embeddings);
      for (const h of need.host || []) if (!out.host.includes(h)) out.host.push(h);
    }
    return out;
  }
  // The machines' setup for those requirements: each chosen machine's state, a check, an install on an explicit
  // click (sudo -n: a machine whose sudo asks for a password is told the commands instead), the commands to copy.
  const hostState = {};                                       // entry -> {missing, error, said}
  function hostSetup(rec) {
    const b = C.standardBench(rec), need = optionNeeds(rec).host;
    if (!b || !need.length) return '';
    const reqs = need.map(n => [n, (b.host || {})[n]]).filter(([, r]) => r);
    const local = ((schema.standardHere || {}).host || {})[b.key] || {};
    // what installs them; none for a machine's own hardware (a GPU), which no package puts there
    const lines = reqs.flatMap(([n]) => b.host[n].install).filter((x, i, a) => a.indexOf(x) === i);
    const line = w => {
      const st = hostState[w] || (w === 'local' ? {missing: Object.fromEntries(need.filter(n => local[n]).map(n => [n, local[n]]))} : null);
      const gone = st && st.missing ? Object.entries(st.missing).filter(([n]) => need.includes(n)) : [];
      const state = !st ? '<span class="nb-tick">not checked</span>' : st.error ? `<span class="nb-tick no">${esc(st.error)}</span>`
        : gone.length ? `<span class="nb-tick no">missing ${esc(gone.map(([, ps]) => ps.join(', ')).join(', '))}</span>` : '<span class="nb-tick ok">ready ✓</span>';
      return `<li><b>${esc(w === 'local' ? 'This machine' : C.machineLabel(w))}</b> ${state} ${button('Check', `data-hostreq-check="${esc(w)}"`, 'nb-inlinebutton')}${lines.length && (gone.length || (st && st.error)) ? ' ' + button('Install for me', `data-hostreq-install="${esc(w)}"`, 'nb-inlinebutton') : ''}${st && st.said ? ` <small>${esc(st.said)}</small>` : ''}</li>`;
    };
    return `<div class="nb-sectionhead std-wherehead"><h3>Machine setup</h3><small>Needed by the search you chose, on each machine it runs on</small></div>`
      + reqs.map(([n, r]) => `<p class="nb-note"><b>${esc(n)}</b>: ${esc(r.summary)}</p>`).join('')
      + `<ul class="nb-hostreq">${C.whereList(rec).map(line).join('')}</ul>`
      + (lines.length ? `<div class="std-code"><div class="std-code-head"><span>or run it yourself, on each machine</span><button type="button" class="std-copy" data-copy-code>Copy</button></div><pre><code>${esc(lines.join('\n'))}</code></pre></div>` : '');
  }
  async function hostReq(entry, what) {
    const rec = r(), b = C.standardBench(rec);
    hostState[entry] = {...(hostState[entry] || {}), said: what === 'install' ? 'installing…' : 'checking…', error: ''};
    renderForm();
    try {
      const res = await fetch('/new-run/host/' + what, {method: 'POST', headers: {'X-CSRF-Token': schema.csrf},
        body: new URLSearchParams({entry, benchmark: b.key, requirements: optionNeeds(rec).host.join(',')})});
      const j = await res.json();
      if (what === 'install' && j.ok) return hostReq(entry, 'check');
      hostState[entry] = j.ok ? {missing: j.missing || {}} : {...(hostState[entry] || {}), error: j.error || ('HTTP ' + res.status), said: j.manual ? 'run the commands below by hand' : ''};
    } catch (e) { hostState[entry] = {error: String(e)}; }
    renderForm();
  }
  // A benchmark's own options (a search, a mode), for the chosen datasets they apply to, and what a value needs of this
  // machine. Those an agent's or capability's run takes ('yours') on the Agent step, with the agents; those only a setup
  // takes (a retriever, a top-k of the paper's pipeline) in that setup's tab, once it is chosen.
  function benchOptions(rec, b, scope = 'yours') {
    const local = ((schema.standardHere || {}).host || {})[b.key] || {};
    const chosen = C.chosenOf(rec);
    return Object.entries(b.options || {}).filter(([, o]) => !o.datasets.length || C.datasetsOf(rec).some(id => o.datasets.includes(id)))
      // one that applies only under another's values (a top-k, only with a retrieval) is left out otherwise
      .filter(([, o]) => Object.entries(o.when || {}).every(([k, vs]) => vs.includes((rec.bench_options || {})[k] ?? (b.options[k] || {}).default)))
      .filter(([, o]) => {
        const on = o.setups || ['yours'];
        const shared = on.includes('yours') && on.includes('original');
        if (scope === 'shared') return shared;
        if (shared) return false;
        return scope === 'setup' ? !on.includes('yours') && on.some(k => chosen.includes(k)) : on.includes('yours');
      })
      .map(([n, o]) => {
      const cur = (rec.bench_options || {})[n] ?? o.default;
      if (o.pattern) return `<label class="nb-field std-optselect" title="${esc(o.summary)}">${esc(o.label)}<input data-bench-opt-select="${esc(n)}" value="${esc(cur === o.default ? '' : cur)}" placeholder="${esc(n === 'temperature' && o.default === 'default' ? 'Provider default' : o.default)}" aria-label="${esc(o.label)}" pattern="${esc(o.pattern)}" autocomplete="off" spellcheck="false"></label>`;
      const tileOf = v => { const need = (o.needs || {})[v] || {};
        const foot = [need.embeddings ? `<span class="nb-tick" title="its embedding calls go through the run's relay, with your OpenAI key">OpenAI key</span>` : '',
          ...(need.host || []).filter(h => local[h]).map(h => `<span class="nb-tick no" title="${esc(((b.host || {})[h] || {}).summary || '')}">needs ${esc(h)}</span>`),
          v === o.default ? '<span class="nb-tag">default</span>' : ''].join('');
        return tile({attr: 'bench-opt', key: n, data: ` data-bench-val="${esc(v)}"`, on: v === cur, title: v, mark: '', tip: (o.about || {})[v] || '', foot}); };
      return `<div class="nb-sectionhead std-wherehead"><h3 title="${esc(o.summary)}">${esc(o.label)}${o.datasets.length ? ' · ' + esc(o.datasets.join(', ')) : ''}</h3></div>`
        // by the data: every value with its line (or a need) - cards of one size, the line whole; plain values - one small
        // card each, all one size, or a select past five of them (a line some have is their tooltip)
        + (o.values.some(v => ((o.needs || {})[v] || {}).embeddings || (((o.needs || {})[v] || {}).host || []).some(h => local[h]))
          ? `<div class="nb-pick-grid std-optgrid" role="radiogroup" aria-label="${esc(o.label)}">${o.values.map(tileOf).join('')}</div>`
          : o.values.length > 5
          ? `<label class="nb-field std-optselect"><select data-bench-opt-select="${esc(n)}" aria-label="${esc(o.label)}">${o.values.map(v => `<option value="${esc(v)}"${v === cur ? ' selected' : ''} title="${esc((o.about || {})[v] || '')}">${esc(v === 'default' ? 'Default' : v + (v === o.default ? ' (default)' : ''))}</option>`).join('')}</select></label>`
          : `<div class="std-valgrid" style="--w:${Math.max(7, ...o.values.map(v => String(v).length))}ch" role="radiogroup" aria-label="${esc(o.label)}">${o.values.map(v => `<button class="nb-tile std-val" data-bench-opt="${esc(n)}" data-bench-val="${esc(v)}" aria-pressed="${v === cur}" title="${esc((o.about || {})[v] || (v === o.default ? 'default' : v))}"><strong>${esc(v)}</strong>${v === o.default ? '<small>default</small>' : ''}</button>`).join('')}</div>`);
    }).join('');
  }
  // Only credentials needing attention belong in the builder. Without a dataset's default
  // environment, a credential may be needed; task setup decides. Shared requirements appear once.
  // A data provider's token (its provider, not its name) is set in Data providers; the rest in Custom keys.
  function credentialWarnings(b, datasets) {
    const needed = new Map(), states = ((schema.standardHere || {}).licences || {})[b.key] || {};
    datasets.forEach(ds => Object.entries(b.environments || {}).filter(([n]) => !ds.environment || n === ds.environment)
      .forEach(([, e]) => (e.needs_secrets || []).forEach(s => { const had = needed.get(s.name) || {};
        needed.set(s.name, {required: had.required || !!ds.environment, provider: had.provider || s.provider || ''}); })));
    return [...needed].map(([name, {required, provider}]) => {
      const state = states[name];
      if (state && !state.expired) return '';
      const settings = provider
        ? button('Data providers', 'data-href="/settings#data-providers"', 'nb-inlinebutton')
        : button('Custom keys', `data-href="/settings#access-${esc(b.key)}"`, 'nb-inlinebutton');
      const what = provider ? `${esc(({huggingface: 'Hugging Face'})[provider] || provider)} token` : esc(name);
      return `<p class="nb-note nb-error">${state ? `Replace the expired ${what}` : `Add ${provider ? 'a ' : ''}${what}`} in ${settings}${required ? '' : ' (some tasks may need it)'}.</p>`;
    }).join('');
  }
  // The benchmark step, both ways (Harbor's catalogue, the standard's benchmarks): a search, filter chips and a grid
  // of tiles, as the Benchmarks page draws them. The filter hides tiles in place (applyPick), so typing keeps focus.
  const picks = {}, pickOf = () => (picks[step] ||= {q: '', cat: '', fam: ''});   // each step its own search and filters
  const catOf = key => (schema.benchmarks || []).find(b => b.key === key) || {};
  const tasksOf = key => { const total = catOf(key).task_count;
    if (Number.isInteger(total) && total > 0) return `${total.toLocaleString('en-US')} tasks`;
    const t = schema.datasets.filter(d => d.benchmark === key && d.tasks).map(d => d.tasks);
    return !t.length ? '' : t.every(n => n === t[0]) && t.length > 1 ? `${t[0]} tasks each` : `${t.reduce((a, n) => a + n, 0)} tasks`; };
  const pickChip = (attr, v, label, on) => `<button type="button" class="nb-pick-chip" data-${attr}="${esc(v)}" aria-pressed="${on}">${esc(label)}</button>`;
  // one tile: {attr, key, on, title, mark, cat, origin, line, meta, foot, find, fam, data (more attributes), tip (its title)}
  const tile = t => `<button class="nb-tile${t.disabled ? ' nb-tile-unfit' : ''}" data-${t.attr}="${esc(t.key)}" aria-pressed="${t.on}"${t.data || ''}${t.disabled ? ` disabled aria-disabled="true" title="${esc(t.why || '')}"` : t.tip ? ` title="${esc(t.tip)}"` : ''} data-cat="${esc(t.cat || '')}" data-fam="${esc(t.fam || '')}" data-find="${esc((t.find || t.title).toLowerCase())}">`
    + `<span class="nb-tile-head">${t.mark ? `<span class="nb-mark">${t.mark}</span>` : ''}<strong>${esc(t.title)}</strong><span class="nb-tile-check" aria-hidden="true">${t.on ? '✓' : ''}</span></span>`
    + (t.cat || t.origin || t.fam ? `<span class="nb-tile-tags">${t.cat ? `<span class="nb-tile-cat">${esc(t.cat)}</span>` : ''}${t.origin === 'experiment' ? '<span class="nb-tile-exp">Experiment</span>' : ''}${t.famLabel ? `<span class="nb-tile-fam">${esc(t.famLabel)}</span>` : ''}</span>` : '')
    + (t.line ? `<span class="nb-tile-line">${esc(t.line)}</span>` : '') + (t.note ? `<span class="nb-tile-note">${esc(t.note)}</span>` : '')
    + (t.meta || t.foot ? `<span class="nb-tile-foot">${t.meta ? `<span>${esc(t.meta)}</span>` : ''}${t.foot || ''}</span>` : '') + '</button>';
  // a machine to run on: this one or a host, its connection state filled in by Test connection
  const machineTile = (attr, v, t, sub, on) => tile({attr, key: v, on, mark: v === 'local' ? '⌂' : '⇄', title: t, tip: sub,
    foot: v !== 'local' ? `<span class="nb-state nb-hoststate" data-host-state="${esc(v)}"></span>` : ''});
  const picker = (tiles, fams, noun = 'benchmark') => {
    const pick = pickOf(), cats = [...new Set(tiles.map(t => t.cat).filter(Boolean))].sort();
    return `<div class="nb-pick"><div class="nb-pick-bar">`
      + `<label class="nb-pick-find"><svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg><input type="search" data-pick-find value="${esc(pick.q)}" placeholder="Search ${noun === 'capability' ? 'capabilities' : noun + 's'}" aria-label="Search ${noun === 'capability' ? 'capabilities' : noun + 's'}" autocomplete="off"></label>`
      + (cats.length > 1 ? `<div class="nb-pick-chips" role="group" aria-label="Category">${pickChip('pick-cat', '', 'All', !pick.cat)}${cats.map(c => pickChip('pick-cat', c, c, pick.cat === c)).join('')}</div>` : '')
      + (fams ? `<div class="nb-pick-chips" role="group" aria-label="Runs">${pickChip('pick-fam', '', 'Any', !pick.fam)}${fams.map(([v, l]) => pickChip('pick-fam', v, l, pick.fam === v)).join('')}</div>` : '')
      + `</div><div class="nb-pick-grid">${tiles.map(tile).join('')}</div><p class="nb-pick-none" hidden>No ${noun} matches.</p></div>`;
  };
  function applyPick() {
    const box = root.querySelector('.nb-pick'), pick = pickOf(); if (!box) return;
    const words = pick.q.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    box.querySelectorAll('.nb-tile').forEach(t => {
      const on = t.getAttribute('aria-pressed') === 'true';
      const fits = (!pick.cat || t.dataset.cat === pick.cat) && (!pick.fam || t.dataset.fam === pick.fam) && words.every(w => t.dataset.find.includes(w));
      t.hidden = !fits && !on; if (!t.hidden) shown++;
    });
    box.querySelectorAll('[data-pick-cat]').forEach(b => b.setAttribute('aria-pressed', b.dataset.pickCat === pick.cat));
    box.querySelectorAll('[data-pick-fam]').forEach(b => b.setAttribute('aria-pressed', b.dataset.pickFam === pick.fam));
    box.querySelector('.nb-pick-none').hidden = shown > 0;
  }
  function modelChoiceCards(rec) {
    const hasPaper = C.paperOffered(rec), on = rec.model_choice || 'any';
    return `<div class="nb-pick-grid std-optgrid" role="group" aria-label="Model choice">${tile({attr:'model-choice',key:'paper',title:'Like the paper',on:on==='paper',disabled:!hasPaper,why:'Paper models unavailable',note:hasPaper?'':'Paper models unavailable'})}`
      + (C.leaderboard(rec) ? tile({attr:'model-choice',key:'leaderboard',title:'Like the leaderboard',on:on==='leaderboard'}) : '')
      + `${tile({attr:'model-choice',key:'any',title:'Any of your choice',on:on==='any'})}</div>`;
  }
  function standardForm(rec) {
    const st = C.standard(), b = C.standardBench(rec);
    if (!b) return note('Choose a benchmark first', button('Benchmark step', `data-step="${S.benchmark}"`, 'nb-inlinebutton'));
    const u = unitsOf(rec);
    if (step === S.dataset)
      return `<div class="nb-sectionhead"><h2>Which datasets?</h2></div>`
        + `<div class="nb-pick-grid std-optgrid">${b.datasets.map(x => tile({attr: 'std-dataset', key: x.id, on: C.datasetsOf(rec).includes(x.id), title: x.label,
          tip: x.summary || '', meta: x.units ? x.units + ' ' + u.nouns : '',
          foot: (x.controls || []).map(c => controlChip(c)).join('') + checkChip('stable', (x.proofs || {}).units, (x.failed || {}).units, 'the same ids twice, each unique')
            + ((b.controls || []).some(c => c.key === 'oracle') ? checkChip('no leak', (x.proofs || {}).leak, (x.failed || {}).leak, 'nothing of the reference shows before the run') : '')})).join('')}</div>`
        + credentialWarnings(b, b.datasets.filter(x => C.datasetsOf(rec).includes(x.id)));
    if (step === S.agent) {
      const agent = b.family === 'agent', all = C.standardOthers(rec), others = all.filter(o => !o.control && !o.setup);
      const setups = all.filter(o => o.setup), controls = all.filter(o => o.control);
      const ds = b.datasets.find(x => C.datasetsOf(rec).includes(x.id)) || b.datasets[0];
      // an agent or a capability as a tile: its name, a skill it failed or a fidelity that differs as a tick; one that has not
      // proved a port the benchmark needs greyed, the ports it lacks named (the reasons whole in its title)
      const tileOf = o => {
        const ok = !o.why.length, lacks = b.needs.filter(p => !o.proven[p]);
        const skills = b.needs.flatMap(p => o.proven[p] ? o.proven[p].skills || [] : []).filter(s => !s.shown).map(s => `<span class="nb-tick no" title="${esc(s.says)}">${esc(s.id)} ✗</span>`).join('');
        const fid = o.fidelity && o.fidelity.verdict !== 'same' ? `<span class="nb-tick ${o.fidelity.verdict === 'same' ? 'ok' : 'no'}" title="ACP ${o.fidelity.verdict === 'same' ? 'gives its model the same requests as its own CLI' : 'differs from its own CLI: ' + esc(o.fidelity.verdict)}">ACP ${o.fidelity.verdict === 'same' ? '= CLI ✓' : '≠ CLI'}</span>` : '';
        const older = b.needs.some(p => o.proven[p] && o.proven[p].older) ? '<span class="nb-tick no" title="proven on an older kit: worth running again">older kit</span>' : '';
        return {attr: 'std-other', key: o.key, on: ok && C.chosenOf(rec).includes(o.key), disabled: !ok, why: o.why.join('; '), title: o.label + (o.version && !o.label.includes(o.version) ? ' ' + o.version : ''),
          mark: agent ? agentMark(o.key, o.label) : esc(o.label[0]), cat: o.protocol, fam: ok ? 'fit' : 'unfit', tip: o.summary, note: ok ? '' : 'Lacks ' + (lacks.map(p => p.replace(/^[^.]+\./, '').replace(/\/v\d+$/, '').replace(/[-_]/g, ' ')).join(', ') || 'a port it needs'),
          foot: fid + skills + older, find: [o.label, o.key, o.version, o.protocol, o.summary].join(' ')};
      };
      // each chosen agent's options, at its vendor's default unless changed: {ref: {name: word}}
      const chosen = others.filter(o => C.chosenOf(rec).includes(o.key) && !o.why.length);
      const optionsOf = o => { const mine = (rec.options || {})[o.key] || {}, opts = Object.entries(o.options || {});
        return !opts.length ? '' : `<div class="std-options">` + opts.map(([n, x]) => {
          const cur = mine[n] ?? x.default;
          return x.values
            ? `<label class="nb-field std-optselect" title="${esc(x.summary)}">${esc(words(n))}<select data-std-option="${esc(n)}" data-std-option-of="${esc(o.key)}">${x.values.map(v => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(v === 'default' ? 'Default' : v + (v === x.default ? ' (its default)' : ''))}</option>`).join('')}</select></label>`
            : `<label class="nb-field std-optselect" title="${esc(x.summary)}">${esc(words(n))}<input data-std-option="${esc(n)}" data-std-option-of="${esc(o.key)}" value="${esc(mine[n] ?? '')}" placeholder="${esc(x.default ?? '')} (its default)" spellcheck="false" autocomplete="off"></label>`;
        }).join('') + '</div>'; };
      // the chosen ones' settings, one tab each: the benchmark's own way, the benchmark's settings every agent takes, each one's own
      const yoursWord = agent ? 'agent' : (b.kind || 'capability');
      // the benchmark's own way of running it (its reference, as published) is one more choice beside yours
      const setupTile = o => ({attr: 'std-other', key: o.key, on: C.chosenOf(rec).includes(o.key), mark: o.key === 'original' ? '◆' : '◇',
        title: o.reference_agent || (agent ? 'No agent · Original' : 'Original ' + (b.kind || 'capability')), cat: 'reference', fam: 'fit', tip: o.summary, find: [b.label, o.label, 'reference'].join(' ')});
      const ds2 = ds;
      const controlTile = o => { const c = ((ds2 || {}).controls || []).find(x => x.key === o.key) || {...o, from: o.summary};
        return tile({attr: 'std-other', key: o.key, on: C.chosenOf(rec).includes(o.key), mark: o.key === 'nop' ? '∅' : '≡', title: o.label,
          tip: o.summary + (b.judged ? ' The judge still runs.' : ''), foot: controlChip(c)}); };
      const setupsNamed = ls => ls.filter(l => !ls.some(m => m !== l && l.startsWith(m))).join(' and ');
      const onSetups = setups.filter(o => C.chosenOf(rec).includes(o.key));
      return `<div class="nb-sectionhead"><h2>Who runs it?</h2></div>`
        + picker([...setups.map(setupTile), ...others.map(tileOf)], [['fit', 'Can run it'], ['unfit', 'Cannot']], agent ? 'agent' : 'capability')
        + (others.length || setups.length ? '' : note('None yet', 'Bring one in with the /import-standard skill, or by hand: docs/standard.md.'))
        + settingsTabs([
            ...(chosen.length || onSetups.length ? [['shared', 'Every ' + yoursWord, benchOptions(rec, b, 'shared')]] : []),
            ...(onSetups.length ? [['setup', b.label + ' · ' + setupsNamed(onSetups.map(o => o.label)), benchOptions(rec, b, 'setup')]] : []),
            ...(chosen.length ? [['yours', agent ? 'Selected agents' : 'Selected capabilities', benchOptions(rec, b)]] : []),
            ...chosen.map(o => [o.key, o.label, optionsOf(o)])])
        + (controls.length ? `<div class="nb-sectionhead std-controlshead std-sethead"><h3>Checks</h3></div><div class="nb-pick-grid">${controls.map(controlTile).join('')}</div>` : '')
        ;
    }
    if (step === S.model && C.standardKeyless(rec))
      return `<div class="nb-sectionhead"><h2>Choose the model.</h2></div>` + note('No model needed', 'A control calls no model, and its run is handed no key at all.');
    if (step === S.model) {
      const lb = rec.model_choice === 'leaderboard' ? C.leaderboard(rec) : null, paper = rec.model_choice === 'paper' || !!lb;
      return `<div class="nb-sectionhead"><h2>Choose the model.</h2></div>` + modelChoiceCards(rec)
        + providerField(rec, C.PROVIDERS.map(p => [p, C.PROVIDER_LABEL[p]]))
        + field('Model', 'model', rec.model, {list:'nb-models', placeholder:paper && C.paperListed(rec) ? 'Choose a paper model' : 'Search or enter a model ID'})
        // the benchmark's other models: the user simulator its conversations need, the judge that grades - each its official one
        // unless named; like the paper, the official one; like the leaderboard, its own and the options it fixes
        + (b.simulator ? (paper ? fixed('Model for the user simulator', lb && lb.simulator || b.simulator, lb && lb.simulator ? 'leaderboard' : 'official') : field('Model for the user simulator', 'simulator_model', rec.simulator_model || '', {list: 'nb-models', placeholder: b.simulator + ' (official)',
            hint: 'Plays the user in every conversation. Empty: ' + esc(b.label) + "'s official one; another compares only with runs that used it."})) : '')
        + (b.judge ? (paper ? fixed('Model for the judge', lb && lb.judge || b.judge, lb && lb.judge ? 'leaderboard' : 'official') : field('Model for the judge', 'judge_model', rec.judge_model || '', {list: 'nb-models', placeholder: b.judge + ' (official)',
            hint: 'Grades the answers. Empty: ' + esc(b.label) + "'s official judge; another compares only with runs that used it."})) : '')
        + (lb ? Object.entries(lb.bench_options || {}).map(([k, v]) => fixed((b.options[k] || {}).label || k, v, 'leaderboard')).join('') : '')
        + keyLine(rec);
    }
    if (step === S.limits) {
      const sets = b.datasets.filter(x => C.datasetsOf(rec).includes(x.id)), per = sets.length > 1 ? ' of each dataset' : '';
      // empty is the default, shown as the placeholder: every unit, one at a time, one attempt
      const ds = sets.length === 1 ? sets[0] : null, all = ds && ds.units ? String(ds.units) : 'all';
      return '<div class="nb-sectionhead"><h2>Scope</h2></div><div class="nb-two">'
        + field('Number of ' + u.nouns, 'limit', rec.limit, {type: 'number', min: 1, placeholder: all, hint: 'The first ones' + per + ', in its order.'})
        + field('Concurrent ' + u.nouns, 'concurrency', rec.concurrency, {type: 'number', min: 1, max: 32, placeholder: '1'}) + '</div>'
        + field('Attempts per ' + u.noun, 'repeat', rec.repeat || '', {type: 'number', min: 1, max: 32, placeholder: '1'})
        // the whole run's, shared by its jobs (recipe.py); a run that calls no model spends nothing: no cap to offer
        + (C.standardKeyless(rec) ? '' : field('Spending cap (USD)', 'max_usd', rec.max_usd ?? '', {type: 'number', min: 0, placeholder: 'none'})
          + field('Spending cap per ' + u.noun + ' (USD)', 'max_usd_per_unit', rec.max_usd_per_unit ?? '', {type: 'number', min: 0, placeholder: 'none'}));
    }
    return '';
  }
  function compileRunner() {
    const rec = r(), key = JSON.stringify(rec);
    if (key === runnerKey) return;
    runnerKey = key;
    clearTimeout(runnerTimer);
    runnerTimer = setTimeout(async () => {
      let out;
      try {
        const res = await fetch('/new-run/runner', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': schema.csrf}, body: JSON.stringify({recipe: rec})});
        out = await res.json();
      } catch (e) { out = {errors: [{step: REVIEW, message: 'The recipe could not be compiled here: ' + e}], notes: [], review: []}; }
      if (key !== runnerKey) return;
      runnerOut = out;
      if (step === REVIEW) renderForm();
      refreshPlan();
    }, 120);
  }
  const field = (label,key,value,{type='text',hint='',list='',min='',max='',placeholder=''}={}) => `<label class="nb-field">${label}<input aria-label="${esc(label)}" data-field="${key}" type="${type}" value="${esc(value)}" ${list?`list="${list}"`:''} ${min!==''?`min="${min}"`:''} ${max!==''?`max="${max}"`:''} ${type==='number'?'step="any"':''} placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">${hint?`<small>${hint}</small>`:''}</label>`;
  const select = (label,key,value,options) => `<label class="nb-field">${label}<select data-field="${key}">${options.map(([v,t])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(t)}</option>`).join('')}</select></label>`;
  const fixed = (label,value,whose='official') => `<label class="nb-field ui-field-row"><span class="ui-field-copy">${esc(label)}</span><span class="nb-fixed">${esc(value)} (${whose})</span></label>`;
  const note = (title,text) => `<div class="nb-notice"><strong>${title}</strong>${text}</div>`;

  function customRows(agent = '') {
    // '' lists the shared entries (every agent, and the other owners); an id lists that agent's own --ak.
    const rows = r().custom.map((p, i) => [p, i]).filter(([p]) => agent ? p.scope === 'agent' && p.agent === agent : !p.agent);
    return (agent ? '' : `<p class="nb-note">Name the parameter, choose its owner and type. An agent parameter here reaches every selected agent; one agent's own go on its tab. Unknown options remain marked for review.</p>`) + rows.map(([p,i])=>`<div class="nb-custom"><div class="nb-rowhead"><span class="nb-badge">Custom override${p.agent ? ' · ' + esc(agentLabel(p.agent)) : ''}</span>${button('Remove',`data-remove="${i}"`,'nb-remove')}</div><div class="nb-two"><label class="nb-field">Owner<select data-custom="${i}:scope" ${p.agent?'disabled':''}>${C.scopes().map(([v,t])=>`<option value="${v}" ${p.scope===v?'selected':''}>${t}</option>`).join('')}</select></label><label class="nb-field">Parameter name<input data-custom="${i}:key" value="${esc(p.key)}" placeholder="custom_setting" spellcheck="false"></label></div><div class="nb-two"><label class="nb-field">Type<select data-custom="${i}:type">${[['string','Text'],['number','Number'],['boolean','Boolean'],['JSON','JSON'],['flag','Flag without value']].map(([v,t])=>`<option value="${v}" ${p.type===v?'selected':''}>${t}</option>`).join('')}</select></label><label class="nb-field">Value<input data-custom="${i}:value" value="${esc(p.value)}" placeholder="${p.type==='boolean'?'true or false':'Value'}" ${p.type==='flag'?'disabled':''} spellcheck="false"></label></div></div>`).join('')+button('+ Add parameter',`id="nb-add" data-add-agent="${esc(agent)}"`);
  }
  // The parameters the launch vocabulary files under one agent (launch.py: a parameter's group is the agent it is sent to).
  function parameterRows(agent) {
    return C.parameters().filter(p=>p.group===agent).map(p=>{
      const value=r().params[p.key];
      const control=field(p.label,'param:'+p.key,value,{type:['number','integer'].includes(p.type)?'number':'text',min:p.min??'',max:p.max??'',hint:p.help,placeholder:p.default==null?'Use agent default':''});
      return `<div class="nb-parameter">${control}</div>`;
    }).join('');
  }
  function categoryContent(category) {
    const rec=r(); let html='';
    if(category==='Model'){
      html='<div class="nb-categoryhead"><h3>Model</h3><p>The model as the provider lists it, e.g. <code>gpt-5.2</code>.</p></div>';
      if(C.needsCredentials(rec)){
        const controlOnly=!rec.agents.some(id=>!schema.agents.find(a=>a.id===id)?.control);
        const tau3=rec.benchmark==='tau3-bench';
        html+=providerField(rec,C.PROVIDERS.map(p=>[p,C.PROVIDER_LABEL[p]]));
        if(tau3&&controlOnly)html+=`<div class="nb-two">${field('Model · customer','userModel',rec.userModel,{list:'nb-models',placeholder:'vendor/model, as the provider lists it'})}${field('Model · evaluator','evalModel',rec.evalModel,{list:'nb-models',placeholder:'same as the customer'})}</div>`;
        else if(tau3)html+=field('Model · agent','model',rec.model,{list:'nb-models',placeholder:'vendor/model, as the provider lists it'})+`<div class="nb-two">${field('Model · customer','userModel',rec.userModel,{list:'nb-models',placeholder:'same as the agent'})}${field('Model · evaluator','evalModel',rec.evalModel,{list:'nb-models',placeholder:'same as the customer'})}</div>`;
        else html+=field('Model','model',rec.model,{list:'nb-models',placeholder:'vendor/model, as the provider lists it'});
        html+=keyLine(rec);
      }else html+=note('No model connection needed','The selected control agents and benchmark do not require one.');
    }
    if(category==='Extra parameters')html='<div class="nb-categoryhead"><h3>Extra parameters</h3><p>Pass additional typed options to the agent, to Harbor, to the environment or to the verifier.</p></div>';
    return html;
  }
  function renderForm() {
    preselectRelease();
    root.dataset.modelProvider = C.recipeProvider(r());
    root.dataset.paperModels = JSON.stringify(r().model_choice === 'paper' && C.paperListed(r()) ? ((C.paperModels(r()).sent || {})[r().provider] || {}) : null);
    root.dataset.standardModel = String(C.isStandard(r()));
    const rec=r(), d=C.dataset(rec);
    let html='';
    const own=C.isStandard(rec);
    if(step===S.benchmark)html=benchmarkStep(rec);
    else if(step===S.runner)html=runnerStep(rec);
    else if(own&&step<S.where)html=standardForm(rec);
    if(!own&&step===S.dataset){
      html='<div class="nb-sectionhead"><h2>Where do the tasks come from?</h2></div>';
      if(C.sources(rec).length>1)html+=`<div class="nb-pick-grid" role="radiogroup" aria-label="Where the tasks come from">${C.sources(rec).map(([v,t])=>tile({attr:'harbor-source',key:v,on:v===rec.source,mark:SOURCE_MARK[v],title:t,tip:SOURCE_HINT[v]||''})).join('')}</div>`;
      if(rec.source==='generate'){
        const g=d?.generate||{};
        html+=field(g.checkout||'Generator checkout','generatorRoot',rec.generatorRoot,{list:'nb-paths',placeholder:g.example||''})+field('Generated task destination','path',rec.path,{list:'nb-paths',placeholder:(d?.local)||''});
      }else if(rec.source==='local')html+=field('Task folder','path',rec.path,{list:'nb-paths',placeholder:(d?.local)||'../harbor-datasets/my-tasks'});
      else html+=field('Dataset reference','dataset',rec.dataset,{list:'nb-registry',placeholder:'name@version or org/name'})+button('Refresh Harbor catalogue','id="nb-refresh"');
    }
    if(!own&&step===S.agent){
      const tabs=[['select','Select agents'],['common','Common settings'],...rec.agents.map(id=>[id,agentLabel(id,rec)]),['extra','Extra parameters'+(rec.custom.filter(p=>!p.agent).length?' · '+rec.custom.filter(p=>!p.agent).length:'')]];
      if(!tabs.some(([t])=>t===tab))tab='select';
      html=`<div class="nb-sectionhead"><h2>Configure your agents.</h2></div>`;
      html+=`<nav class="nb-subtabs nb-categories" aria-label="Agent tabs">${tabs.map(([t,label])=>button(esc(label),`data-tab="${esc(t)}" aria-pressed="${tab===t}"`,'nb-subtab')).join('')}</nav>`;
      if(tab==='select')html+=agentCards(rec);
      else if(tab==='common')html+=commonSettings(rec);
      else if(tab==='extra')html+=categoryContent('Extra parameters')+`<div id="nb-parameters">${customRows()}</div>`;
      else html+=agentTab(rec,tab);
    }
    if(!own&&step===S.model){
      html='<div class="nb-sectionhead"><h2>Choose your model.</h2></div>'+modelChoiceCards(rec)+categoryContent('Model');
    }
    if(!own&&step===S.limits){
      html='<div class="nb-sectionhead"><h2>Scope & execution</h2></div>';
      const allTasks=(()=>{const ds=schema.datasets.find(d=>d.benchmark===rec.benchmark);return ds&&ds.tasks?'all '+ds.tasks:'all';})();
      html+=`<div class="nb-two">${field('Number of tasks','tasks',rec.tasks,{type:'number',min:1,placeholder:allTasks,hint:'Empty: '+allTasks+'.'})}${field('Concurrent trials','concurrency',rec.concurrency,{type:'number',min:1})}</div>`;
      html+=`<div class="nb-two">${field('Retries per trial','retries',rec.retries,{type:'number',min:0,hint:'Run a trial again after an error, up to this many times.'})}${field('Include task names · optional glob','include',rec.include,{placeholder:'django*'})}</div>`
        +harborExecutableFields(rec);
      html+=field('Output directory','output',rec.output,{});
    }
    if(step===S.where&&C.isStandard(rec))html=standardWhere(rec);
    else if(step===S.where){
      // Where the recipe runs when Run is pressed: this machine, or a host from Settings.
      const H=schema.hosts||[], rec2=r(), on=v=>rec2.where===v;
      html='<div class="nb-sectionhead"><h2>Where does it run?</h2></div>'
        +`<div class="nb-pick-grid nb-where">${[['local','This machine','Nullius runs the script here, in runs/'+esc(rec2.name)+'.'],...H.map(h=>[h.entry,h.label,own?'Over ssh. uv and a snapshot of this runner are put there if missing; results and proofs come back as it runs.':'Over ssh. The tasks are sent there, and a repository’s Harbor as the wheels built here; uv runs the Harbor the recipe pins; the job comes back as it runs.'])].map(([v,t,sub])=>machineTile('where',v,t,sub,on(v))).join('')}</div>`
        +(H.length?button('Test connection','id="nb-test-host"'+(rec2.where==='local'?' disabled':''))+' <span id="nb-hosttest" class="nb-note"></span>':note('No host yet','Hosts are named in Settings → Run hosts; this machine is the only choice until then.'));
    }
    if(step===REVIEW){
      // the standard's review names Where itself (recipe.py): its machines and its backend
      const rows=own?[...((runnerOut&&runnerOut.review)||[]),...(C.isStandard(rec)?[]:[[S.where,'Where',C.whereLabel(rec)]])]:C.review(rec);
      html='<div class="nb-sectionhead"><h2>Review the experiment.</h2></div>'+rows.map(([i,t,v])=>`<div class="nb-reviewrow"><div><small>${t}</small><strong>${esc(v)}</strong></div>${button('Edit',`data-step="${i}"`)}</div>`).join('')+'<div id="nb-errors" class="nb-errorlist" hidden></div><div id="nb-reviewnotes"></div>'+(schema.local?'<section id="nb-prereq" class="nb-prereq" aria-live="polite"></section>':'')+runPanel();
    }
    $('#nb-form').innerHTML=html+'<p id="nb-localerrors" class="nb-error" role="status"></p>';applyPick();
    if(step===REVIEW&&schema.local){drawPrereq();checkPrereq();}
    $('#nb-back').hidden=step===S.benchmark;
    $('#nb-next').textContent=step===REVIEW?'Export recipe JSON':stepName(steps[nextShown(step,1)],nextShown(step,1))+' →';
    placeThumb();
  }
  function refreshPlan() {
    const rec=r();
    if(C.isStandard(rec)){
      compileRunner();
      const out=runnerOut||{errors:[],notes:[]};
      compiled={errors:out.errors||[],notes:out.notes||[]};
    }else{compiled=C.compile(rec);}
    $('#nb-localerrors').textContent=compiled.errors.filter(e=>e.step===step).map(e=>e.message).join(' ');
    const errors=$('#nb-errors');
    if(errors){
      errors.hidden=!compiled.errors.length;
      errors.innerHTML=compiled.errors.map(e=>button(esc(e.message),`data-step="${e.step}" ${e.step===S.agent&&!C.isStandard(rec)?`data-config-group="${esc(e.group||'')}"`:""}`,'')).join('');
    }
    $('#nb-next').disabled=step===REVIEW&&compiled.errors.length>0;
    if($('#nb-reviewnotes'))$('#nb-reviewnotes').innerHTML=compiled.notes.map(t=>`<p class="nb-note">${esc(t)}</p>`).join('');
    saveDraft();
  }
  // The underline under the active step is one element that slides to it. It follows the nav's own width, not
  // the window's: the Getting started panel narrows the page without a resize event.
  const moveUnderline=()=>{const nav=$('.nb-steps'),on=nav.querySelector('.nb-step[aria-pressed=true]'),bar=nav.querySelector('.nb-stepbar');if(!on||!bar)return;bar.style.left=on.offsetLeft+'px';bar.style.width=on.offsetWidth+'px';};
  new ResizeObserver(moveUnderline).observe($('.nb-steps'));
  function stepName(t,i){const rec=r(),b=C.isStandard(rec)&&C.standardBench(rec);
    return i===S.agent&&b?(b.family==='agent'?'Agent':(b.kind||'capability').replace(/^./,c=>c.toUpperCase())):t;}
  // Next and Back skip a step no recipe shows (Setup, its number kept for saved drafts)
  const nextShown=(i,dir)=>{let j=i+dir;while(j>S.benchmark&&j<REVIEW&&!C.stepShown(r(),j))j+=dir;return Math.max(S.benchmark,Math.min(REVIEW,j));};
  function render(focus=false) {
    const nav=$('.nb-steps');
    if(!nav.querySelector('.nb-stepbar'))nav.innerHTML='<span class="nb-stepbar" aria-hidden="true"></span>';
    nav.querySelectorAll('.nb-step').forEach(b=>b.remove());
    if(!C.stepShown(r(),step))step=nextShown(step,step>S.setup?-1:1);
    const shown=steps.map((t,i)=>i).filter(i=>C.stepShown(r(),i));
    nav.insertAdjacentHTML('afterbegin',shown.map((i,n)=>button(`<em>0${n+1}</em>${stepName(steps[i],i)}`,`data-step="${i}" aria-pressed="${step===i}"`,'nb-step')).join(''));
    renderForm();refreshPlan();moveUnderline();saveDraft();harborFollow();
    if(focus){const heading=$('#nb-form h2');if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});heading.scrollIntoView({block:'nearest'});}}
  }
  function readSaved(){
    try{
      const ok=list=>Array.isArray(list)?list.filter(x=>x&&x.recipe&&typeof x.id==='string'):[];
      let list=ok(JSON.parse(localStorage.getItem(storageKey)||'[]'));
      // Merge workspace-scoped recipe lists into this browser's shared list.
      for(const k of Object.keys(localStorage))if(k.startsWith(storageKey+'.')){const old=ok(JSON.parse(localStorage.getItem(k)||'[]'));list=list.concat(old.filter(o=>!list.some(x=>x.id===o.id)));localStorage.removeItem(k);localStorage.setItem(storageKey,JSON.stringify(list));}
      return list;
    }catch{return [];}
  }
  const workspaceLabel = () => schema.workspace && schema.workspace!=='unassigned' ? 'Workspace: '+schema.workspace : 'Personal recipes · no workspace selected';
  const recipeSlug = title => title.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^[^a-z0-9]+|[-.]+$/g,'').slice(0,72) || 'recipe';
  function savedState(recipe){try{return C.validate(C.fromJSON(recipe)).errors.length?' · Draft':'';}catch{return ' · Needs review';}}
  function renderSaved(){
    const saved=readSaved();
    $('#nb-saved-list').innerHTML='<p class="nb-note">'+esc(workspaceLabel())+' · stored in this browser</p>'+(saved.length?saved.map(s=>`<div class="nb-savedrow"><span><strong>${esc(s.recipe.title||s.recipe.name)}</strong><small>${esc(s.recipe.harness)} · ${esc(s.updated)}${savedState(s.recipe)}</small></span><div>${button('Load',`data-load="${esc(s.id)}"`)} ${button('Delete',`data-delete="${esc(s.id)}"`)}</div></div>`).join(''):'<p class="nb-note">Save your first recipe to find it here.</p>');
  }
  function download(text,filename,type){const url=URL.createObjectURL(new Blob([text],{type})),link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function updateSavePreview(){
    const title=$('#nb-recipe-title').value.trim(),name=recipeSlug(title),existing=readSaved().find(s=>s.id===(r().harness||'harbor')+':'+name);
    $('#nb-recipe-title').setCustomValidity(title?'':'Enter a name for this recipe.');
    $('#nb-save-submit').textContent=existing?'Update recipe':'Save recipe';
    $('#nb-save-message').textContent=existing?'A recipe with this name exists. Saving will update it.':workspaceLabel()+'. You can save unfinished recipes as drafts.';
  }
  function openSave(){
    $('#nb-recipe-title').value=r().title||r().name.replace(/-/g,' ');
    updateSavePreview();$('#nb-save-dialog').showModal();$('#nb-recipe-title').focus();$('#nb-recipe-title').select();
  }
  function save(){
    const title=$('#nb-recipe-title').value.trim();if(!title){$('#nb-recipe-title').focus();return;}
    const candidate={...C.copy(r()),title,name:recipeSlug(title)};
    if(C.validate(candidate).errors.some(e=>/environment variable name|credentials; do not put/.test(e.message))){$('#nb-save-message').textContent='Use environment variable names for credentials before saving. Their values belong in your shell.';return;}
    try{
      const list=readSaved(),id=(candidate.harness||'harbor')+':'+candidate.name;   // the harness prefix earlier versions wrote; a runner's key for its recipes
      const saved={id,updated:new Date().toLocaleDateString(),recipe:candidate};
      localStorage.setItem(storageKey,JSON.stringify([saved,...list.filter(s=>s.id!==id)].slice(0,50)));
      recipe=candidate;$('#nb-save-dialog').close();render();renderSaved();
      status('Saved “'+title+'”'+(compiled.errors.length?' as a draft.':'.'));
    }catch{ $('#nb-save-message').textContent='Browser storage is unavailable. Close this dialog and export the recipe as JSON instead.'; }
  }
  root.addEventListener('submit',event=>{if(event.target.id==='nb-save-form'){event.preventDefault();save();}});
  // Running from here: the runner script is posted with the target; the page follows the log until the run ends.
  const runPanel=()=>{
    const rec=r(), where=C.whereList(rec).map(w=>w==='local'?'this machine':C.machineLabel(w)).join(' + ');
    if(!schema.local)return note('Run from the application host','Runs start from a browser on the machine Nullius runs on.');
    // A provider not set in Settings: Run waits and says why.
    const blocked=C.providerBlocker(rec);
    if(blocked)return `<div class="nb-runbox" id="nb-runbox"><div class="nb-runline">${button('▶ Run on '+esc(where),'id="nb-run" disabled data-held title="'+esc(blocked)+'"','nb-button nb-primary')}</div><p class="nb-note nb-error">${esc(blocked)} ${button('Model step','data-step="'+S.model+'"','nb-inlinebutton')} · ${button('Settings','data-href="/settings#'+(C.PROVIDER_NEEDS[C.recipeProvider(rec)]||'MODEL_API_URL')+'"','nb-inlinebutton')}</p></div>`;
    if(C.isStandard(rec)){
      const out=runnerOut||{errors:[{message:'Compiling the recipe…'}]}, noun=C.isStandard(rec)?unitsOf(rec).noun:'item';
      return `<div class="nb-runbox" id="nb-runbox">${(out.errors||[]).length?out.errors.map(e=>`<p class="nb-note">${esc(e.message)}</p>`).join(''):`<div class="nb-runline">${button('▶ Run on '+esc(where),'id="nb-run"','nb-button nb-primary')}<span id="nb-runstate" class="nb-note"></span></div>`+(out.confirm?`<p class="nb-note nb-nolimit">No ${esc(noun)} limit: ${esc(out.confirm.replace(/^Run /,'').replace(/\?$/,''))}. Run asks before it starts.</p>`:'')}</div>`;
    }
    const plan=C.runnable(rec);
    return `<div class="nb-runbox" id="nb-runbox">${plan.errors.length?plan.errors.map(e=>`<p class="nb-note">${esc(e)}</p>`).join(''):`<div class="nb-runline">${button('▶ Run on '+esc(where),'id="nb-run"'+(C.validate(rec).errors.length?' disabled data-held':''),'nb-button nb-primary')}<span id="nb-runstate" class="nb-note"></span></div>`+(plan.confirm?`<p class="nb-note nb-nolimit">No task limit: ${esc(plan.confirm.replace(/^Run /,'').replace(/\?$/,''))}. Run asks before it starts.</p>`:'')}</div>`;
  };
  // Prerequisites: what the recipe needs of each machine it runs on, asked when Review opens and on Check again
  // (/new-run/prerequisites, nullius/prerequisites.py). Run waits until every row is ready; the run asks again as it starts.
  let prereq={sig:'',busy:false,out:null};
  const prereqSig=rec=>JSON.stringify({...rec,name:'',title:''});
  async function checkPrereq(force=false){
    const rec=r(), sig=prereqSig(rec);
    if(!force&&prereq.sig===sig)return;
    prereq={sig,busy:true,out:null};drawPrereq();
    let out;
    try{const res=await fetch('/new-run/prerequisites',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':schema.csrf},body:JSON.stringify({recipe:rec})});
      out=res.ok?await res.json():{machines:[],ready:false,errors:['The machines could not be checked: HTTP '+res.status]};}
    catch(e){out={machines:[],ready:false,errors:['The machines could not be checked: '+e]};}
    if(prereq.sig!==sig)return;   // the recipe changed while it was asked: the newer check stands
    prereq={sig,busy:false,out};drawPrereq();
  }
  function drawPrereq(){
    const el=$('#nb-prereq'); if(!el)return;
    const out=prereq.out, mark={ok:'✓',missing:'✕',install:'↓'};
    const rows=m=>m.rows.map(x=>`<li class="nb-prereq-${x.state}"><span class="nb-prereq-mark" aria-hidden="true">${mark[x.state]}</span><span><strong>${esc(x.label)}</strong>${x.detail?` <small>${esc(x.detail)}</small>`:''}${x.state==='missing'&&x.install.length?`<code class="nb-prereq-fix">${x.install.map(esc).join('<br>')}</code>`:''}</span></li>`).join('');
    const head=prereq.busy?'<span class="nb-note">checking…</span>':out&&out.ready?'<span class="nb-note nb-ok">Ready</span>':out?'<span class="nb-note nb-error">Missing something: Run waits</span>':'';
    el.innerHTML=`<div class="nb-prereq-head"><h3>Prerequisites</h3>${head}${button('Check again','id="nb-prereq-again"'+(prereq.busy?' disabled':''),'nb-inlinebutton')}</div>`
      +(out?(out.errors||[]).map(e=>`<p class="nb-note nb-error">${esc(e)}</p>`).join('')
        +out.machines.map(m=>`<div class="nb-prereq-machine">${out.machines.length>1?`<small>${esc(m.label)}</small>`:''}<ul>${rows(m)}</ul></div>`).join(''):'');
    const b=$('#nb-run'); if(b&&!b.hasAttribute('data-held'))b.disabled=!(out&&out.ready&&!prereq.busy);
  }
  const say=(t,cls='')=>{const el=$('#nb-runstate');if(el){el.textContent=t;el.className='nb-note '+cls;}};
  async function testHost(entry){
    if(entry==='local')return;
    const out=$('#nb-hosttest'), st=root.querySelector(`[data-host-state="${CSS.escape(entry)}"]`);
    out.textContent='testing…';
    try{const res=await fetch('/settings/hosts/test',{method:'POST',headers:{'X-CSRF-Token':schema.csrf},body:new URLSearchParams({entry})});const j=await res.json();
      // only what can stop this recipe there: docker, for a Harbor run or the standard's docker backend (uv is installed when missing)
      const rec=r(),needsDocker=rec.harness==='harbor'||(C.isStandard(rec)&&rec.backend==='docker'),dockerOk=j.docker&&j.docker_ok;
      out.textContent=!j.ok?'✕ '+(j.error||('HTTP '+res.status)):needsDocker&&!dockerOk?'✓ Connected · ✕ docker '+(j.docker?'not usable by this user':'missing')+': this run needs it':'✓ Connected';
      if(st){st.textContent=j.ok?'connected':'unreachable';st.className='nb-state nb-hoststate '+(j.ok?'':'warn');}
      if(j.ok&&C.isStandard(r())&&optionNeeds(r()).host.length)hostReq(entry,'check');   // and what the chosen search needs of it
    }catch(e){out.textContent='✕ '+e;}
  }
  async function startRun(){
    // Each Run is a new job: the recipe's name plus the second, so a second press does not resume the first (Harbor refuses a job directory whose config differs and resumes one whose config matches).
    const now=new Date(), pad=n=>String(n).padStart(2,'0'), stamp=pad(now.getMonth()+1)+pad(now.getDate())+'-'+pad(now.getHours())+pad(now.getMinutes())+pad(now.getSeconds());
    const rec={...r(),name:r().name+'-'+stamp};
    let body, ask;
    if(C.providerBlocker(rec))return;   // the button is disabled then; /run refuses the same
    if(!(prereq.out&&prereq.out.ready))return;   // Run waits for the prerequisites; the run asks again as it starts
    // A runner of its own sends its choices only: /run compiles them again from the definition file.
    if(C.isStandard(rec)){body={name:rec.name,where:rec.where,recipe:rec};ask=runnerOut&&runnerOut.confirm;}
    else{const plan=C.runnable(rec); if(plan.errors.length)return; body={name:rec.name,script:plan.script,where:rec.where,tasks:plan.tasks,model:plan.model,jobs:plan.jobs,parts:plan.parts,recipe:rec}; ask=plan.confirm;}   // the recipe travels with the run
    if(ask&&!window.confirm(ask)){say('Not started.');return;}   // the whole dataset is a decision, not a default
    const b=$('#nb-run'); b.disabled=true; say('starting…');
    try{
      const res=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':schema.csrf},body:JSON.stringify(body)});
      const j=await res.json(); if(!j.ok){say('✕ '+(j.error||('HTTP '+res.status)),'nb-error');b.disabled=false;return;}
      location.href=j.open||'/runs?view=launch';   // the Runs page, by launch: this launch's runs, each opening its job
    }catch(e){say('✕ '+e,'nb-error');b.disabled=false;}
  }
  // The button says it is reading until the answer's toast replaces it: a toast alone would be gone before a slow answer.
  async function refreshRegistry(){const b=$('#nb-refresh'),label=b.textContent;b.disabled=true;b.classList.add('nb-loading');b.setAttribute('aria-busy','true');b.textContent='Reading Harbor’s catalogue…';status('');try{const response=await fetch('/new-run/registry');if(!response.ok)throw Error();const data=await response.json(),names=[...new Set([...schema.datasets.map(d=>d.registry),...schema.seenRegistry,...(data.names||[]),...(data.hub||[])])].filter(Boolean);$('#nb-registry').innerHTML=names.map(n=>`<option value="${esc(n)}"></option>`).join('');status(names.length+' catalogue references available. You can also enter one directly.');}catch{status('Catalogue refresh unavailable. Existing references and custom entry still work.');}finally{if(b.isConnected){b.disabled=false;b.classList.remove('nb-loading');b.removeAttribute('aria-busy');b.textContent=label;}}}
  function completeDirectory(value){clearTimeout(pendingDirs);const seq=++dirsSequence;pendingDirs=setTimeout(async()=>{try{const response=await fetch('/new-run/dirs?path='+encodeURIComponent(value));if(!response.ok)return;const data=await response.json();if(seq===dirsSequence)$('#nb-paths').innerHTML=(data.dirs||[]).map(p=>`<option value="${esc(p)}"></option>`).join('');}catch{}},180);}
  // Saved recipes: a dialog that a click on its backdrop closes, as Esc does (a button's click, by key too, is not one)
  $('#nb-saved').addEventListener('click',event=>{if(event.target!==event.currentTarget)return;const box=event.currentTarget.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)event.currentTarget.close();});
  root.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b)return;const d=b.dataset;
    if(d.modelChoice){C.setModelChoice(r(),d.modelChoice);renderForm();refreshPlan();return;}
    if(d.benchOpt!==undefined){const rec=r(),o=((C.standardBench(rec)||{}).options||{})[d.benchOpt]||{};rec.bench_options={...(rec.bench_options||{})};if(d.benchVal===o.default)delete rec.bench_options[d.benchOpt];else rec.bench_options[d.benchOpt]=d.benchVal;renderForm();refreshPlan();return;}
    if(d.hostreqCheck!==undefined){hostReq(d.hostreqCheck,'check');return;}
    if(d.hostreqInstall!==undefined){hostReq(d.hostreqInstall,'install');return;}
    if(d.copyCode!==undefined){const t=b.closest('.std-code').querySelector('code').textContent;try{await navigator.clipboard.writeText(t);b.textContent='Copied';}catch{b.textContent='Select it';}setTimeout(()=>{b.textContent='Copy';},1500);return;}
    if(d.pickCat!==undefined){pickOf().cat=d.pickCat;applyPick();return;}
    if(d.pickFam!==undefined){pickOf().fam=d.pickFam;applyPick();return;}
    if(d.agent){const on=r().agents.includes(d.agent);r().agents=on?r().agents.filter(id=>id!==d.agent):[...r().agents,d.agent];renderForm();refreshPlan();return;}
    else if(d.href){location.href=d.href;return;}
    else if(b.id==='nb-add-custom-agent'){r().customAgents.push('');r().agents.push('custom-'+(r().customAgents.length-1));renderForm();refreshPlan();root.querySelector('[data-custom-agent="'+(r().customAgents.length-1)+'"]')?.focus();return;}
    else if(d.removeCustomAgent!==undefined){const i=Number(d.removeCustomAgent),rec=r(),kept=rec.agents.filter(id=>id.startsWith('custom-')&&id!=='custom-'+i).map(id=>Number(id.slice(7)));rec.customAgents.splice(i,1);rec.agents=rec.agents.filter(id=>!id.startsWith('custom-')).concat(kept.map(k=>'custom-'+(k>i?k-1:k)));renderForm();refreshPlan();return;}
    // The Harbor panel (Runner step): a release or a repository, a ref of it, installed and asked for its agents.
    else if(d.harborKind){const rec=r(),h=rec.harbor||{},k=d.harborKind;if(k===h.kind)return;harbor.kept[h.kind]=h;rec.harbor=harbor.kept[k]||(k==='repo'?{kind:'repo',repo:'',revisionKind:'branch',revision:'',commit:'',extras:[]}:{kind:'release',version:'',extras:[]});applyScan();renderForm();refreshPlan();harborFollow();root.querySelector(k==='repo'?'[data-harbor-repo]':'[data-harbor-kind="release"]')?.focus();return;}
    else if(d.harborRefKind){const h=r().harbor,got=harbor.refs[C.harborRepoUrl(h.repo)]||{},k=d.harborRefKind;if(k===h.revisionKind)return;h.revisionKind=k;const pick=k==='branch'?(got.branches||[]).find(x=>x.name===got.default_branch)||(got.branches||[])[0]:k==='tag'?(got.tags||[])[0]:null;if(pick){h.revision=pick.name;h.commit=pick.commit;}else if(k==='commit')h.revision=h.commit;else{h.revision='';h.commit='';}applyScan();renderForm();refreshPlan();askScan(false);root.querySelector(`[data-harbor-ref-kind="${k}"]`)?.focus();return;}
    else if(d.harborScan!==undefined){askScan(true);return;}
    else if(d.harborRefsFresh!==undefined){loadRefs(C.harborRepoUrl(r().harbor.repo),true);return;}
    else if(d.runner){toRunner(d.runner,C.benchKey(r()));}
    else if(d.bench){toRunner(C.runnerFor(r(),d.bench),d.bench);}
    else if(d.stdBench){C.setStandardBenchmark(r(),d.stdBench);}
    else if(d.stdDataset){const rec=r(),c=C.datasetsOf(rec);rec.datasets=c.includes(d.stdDataset)?(c.length>1?c.filter(k=>k!==d.stdDataset):c):[...c,d.stdDataset];delete rec.dataset;}
    else if(d.stdOther){const rec=r(),c=C.chosenOf(rec);rec.others=c.includes(d.stdOther)?c.filter(k=>k!==d.stdOther):[...c,d.stdOther];delete rec.other;if(rec.options)delete rec.options[d.stdOther];}
    // the standard's machines: each a toggle, one at least; several split the run into shards
    else if(d.stdWhere){const rec=r(),list=C.whereList(rec),on=list.includes(d.stdWhere),next=on?list.filter(w=>w!==d.stdWhere):[...list,d.stdWhere];if(!next.length)return;rec.where=next.length>1?next:next[0];}
    else if(d.stdBackend){r().backend=d.stdBackend;}
    else if(d.harborSource){r().source=d.harborSource;}
    else if(d.benchmark){C.setBenchmark(r(),d.benchmark);}
    else if(d.where!==undefined){r().where=d.where;}
    else if(b.id==='nb-test-host'){for(const w of C.whereList(r()))if(w!=='local')await testHost(w);return;}
    else if(b.id==='nb-run'){await startRun();return;}
    else if(b.id==='nb-prereq-again'){checkPrereq(true);return;}
    else if(d.step!==undefined){step=Number(d.step);if(d.configGroup!==undefined){const g=d.configGroup;tab=g==='Extra parameters'?'extra':r().agents.includes(g)?g:'select';}}
    else if(d.tab){tab=d.tab;step=S.agent;}
    else if(d.setTab!==undefined){
      setTab=d.setTab;
      // Keep the surrounding form and focused tab in place when changing settings.
      root.querySelectorAll('[data-set-tab]').forEach(t=>t.setAttribute('aria-pressed',t.dataset.setTab===setTab));
      root.querySelectorAll('[data-settings-pane]').forEach(p=>{p.hidden=p.dataset.settingsPane!==setTab;});
      return;
    }
    else if(d.remove!==undefined)r().custom.splice(Number(d.remove),1);
    else if(d.load){try{const saved=readSaved().find(s=>s.id===d.load);recipe=C.fromJSON(saved.recipe);step=REVIEW;$('#nb-saved').close();status('Loaded “'+(recipe.title||recipe.name)+'”.');}catch(error){status('This saved recipe cannot be read: '+error.message);return;}}
    else if(d.delete){try{localStorage.setItem(storageKey,JSON.stringify(readSaved().filter(s=>s.id!==d.delete)));renderSaved();status('Saved recipe deleted.');}catch{status('Browser storage is unavailable.');}return;}
    else if(b.id==='nb-add'){r().custom.push({scope:'agent',key:'',type:'string',value:'',...(d.addAgent?{agent:d.addAgent}:{})});}
    else if(b.id==='nb-next'){if(step===REVIEW){if(!compiled.errors.length)download(JSON.stringify(r(),null,2)+'\n',r().name+'.json','application/json');return;}step=nextShown(step,1);}
    else if(b.id==='nb-back')step=nextShown(step,-1);
    else if(b.id==='nb-save'){openSave();return;}
    else if(b.id==='nb-startover'){try{localStorage.removeItem(draftKey);}catch{}recipe=C.prefill({});runnerOut=null;runnerKey='';step=S.benchmark;tab='select';status('Started over from the defaults.');}
    else if(b.id==='nb-save-cancel'){$('#nb-save-dialog').close();return;}
    else if(b.id==='nb-saved-toggle'){renderSaved();$('#nb-saved').showModal();return;}
    else if(b.id==='nb-saved-close'){$('#nb-saved').close();return;}
    else if(b.id==='nb-import'){$('#nb-import-file').click();return;}
    else if(b.id==='nb-refresh'){await refreshRegistry();return;}
    else return;
    render(d.step!==undefined||['nb-next','nb-back'].includes(b.id));
  });
  root.addEventListener('input',event=>{
    const el=event.target,d=el.dataset,rec=r();
    if(el.id==='nb-recipe-title'){updateSavePreview();return;}
    if(d.pickFind!==undefined){pickOf().q=el.value;applyPick();return;}
    if(d.providerModel && rec.modelFormat !== 'provider-id'){for(const key of ['model','userModel','evalModel'])if(typeof rec[key]==='string')rec[key]=C.bareModel(rec[key]);rec.modelFormat='provider-id';}
    if(d.field){const p=d.field.startsWith('param:')?C.parameters().find(p=>p.key===d.field.slice(6)):null;
      const value=el.type==='number'?(el.value===''?'':Number(el.value)):el.value;
      if(p)rec.params[p.key]=p.type==='optional-boolean'?(value==='__default__'?null:value==='true'):value;
      else if(d.field==='timeoutMultiplier'){rec.timeoutMultiplier=value;rec.agentMinutes='';}
      else if(d.field.startsWith('adapterPath:')){const id=d.field.slice(12);if(String(value).trim())rec.adapterPaths[id]=String(value).trim();else delete rec.adapterPaths[id];}
      else if(!d.providerModel&&['model','userModel','evalModel'].includes(d.field)&&/^(openrouter|openai)\//.test(String(value))){rec.provider=C.providerOf(rec,value);rec[d.field]=C.bareModel(value);
        renderForm();refreshPlan();const f=root.querySelector(`[data-field="${d.field}"]`);if(f){f.focus();f.setSelectionRange(f.value.length,f.value.length);}return;}   // the provider the prefix named, and whether it is set
      else{rec[d.field]=value;if(d.field==='provider'){renderForm();refreshPlan();return;}}   // whether it is set in Settings, and the names a runner's models are sent as, follow the provider
      if(el.list?.id==='nb-paths')completeDirectory(el.value);
    }else if(d.minutes){C.setPhaseMinutes(rec,d.minutes,el.value===''?'':Number(el.value));const row=el.closest('tr');if(row){const m=row.querySelector('.nb-mono');const t=C.effectiveTimeouts(rec);const p=t&&t.phases.find(x=>x.key===d.minutes);if(m&&p)m.textContent='×'+p.multiplier;}refreshPlan();return;}
    else if(d.customAgent!==undefined){rec.customAgents[Number(d.customAgent)]=el.value;refreshPlan();return;}
    else if(d.harborRepo!==undefined){const h=rec.harbor;h.repo=el.value;h.commit='';h.revision='';applyScan();}
    else if(d.harborCommit!==undefined){rec.harbor.commit=rec.harbor.revision=el.value.trim();applyScan();}
    else if(d.harborVersion!==undefined){rec.harbor.version=el.value.trim();applyScan();}
    else if(d.custom){const [i,k]=d.custom.split(':');rec.custom[i][k]=el.value;}
    else return;
    refreshPlan();
  });
  // a number field left holding what is no number ("1e", "-") reads as empty to the page: a cap typed that way would be
  // no cap, unsaid. It is said, and the field emptied, so that it shows what the recipe holds
  root.addEventListener('focusout',event=>{
    const el=event.target;
    if(el.type==='number'&&el.dataset.field&&el.validity&&el.validity.badInput){el.value='';window.nulliusNotify?.(`${el.getAttribute('aria-label')}: enter a number.`,{error:true,key:'bad-number'});}
  });
  root.addEventListener('change',async event=>{
    const el=event.target,d=el.dataset;
    if(d.custom?.endsWith(':type')){$('#nb-parameters').innerHTML=customRows(!['common','extra'].includes(tab)?tab:'');}
    else if(d.stdUnsandboxed!==undefined){r().unsandboxed=el.checked;refreshPlan();}
    else if(d.stdVersion!==undefined){C.setStandardBenchmark(r(),el.value);render();}
    // an option at its default is left out: the recipe names only what was changed
    else if(d.benchOptSelect!==undefined){const rec=r(),o=((C.standardBench(rec)||{}).options||{})[d.benchOptSelect]||{};rec.bench_options={...(rec.bench_options||{})};const v=el.value.trim();if(!v||v===o.default)delete rec.bench_options[d.benchOptSelect];else rec.bench_options[d.benchOptSelect]=v;refreshPlan();}
    else if(d.stdOption!==undefined){const rec=r(),k=d.stdOptionOf,o=(C.standardOthers(rec).find(x=>x.key===k)||{}).options||{},x=o[d.stdOption]||{},v=el.value.trim();rec.options={...(rec.options||{})};const mine={...(rec.options[k]||{})};if(v===''||v===x.default)delete mine[d.stdOption];else mine[d.stdOption]=v;if(Object.keys(mine).length)rec.options[k]=mine;else delete rec.options[k];refreshPlan();}
    // a release or a ref picked, a commit typed: an explicit choice, so its agents are asked for (built here the first time)
    else if(d.harborVersion!==undefined){r().harbor.version=el.value.trim();applyScan();renderForm();refreshPlan();askScan(true);}
    else if(d.harborRef!==undefined){const h=r().harbor,got=harbor.refs[C.harborRepoUrl(h.repo)]||{},x=((h.revisionKind==='tag'?got.tags:got.branches)||[]).find(y=>y.name===el.value);h.revision=el.value;h.commit=x?x.commit:'';applyScan();renderForm();refreshPlan();askScan(true);}
    else if(d.harborCommit!==undefined){const h=r().harbor;h.commit=h.revision=el.value.trim();applyScan();renderForm();refreshPlan();if(C.harborPin(r()))askScan(true);}
    else if(d.harborRepo!==undefined){renderForm();refreshPlan();loadRefs(C.harborRepoUrl(r().harbor.repo));}
    else if(d.harborChoice!==undefined){const rec=r(),v=el.value;if(v==='path')rec.harborDev='';else if(v==='dev')rec.harborDev=rec.harborDev||'~/harbor-dev/.venv/bin/harbor';renderForm();refreshPlan();if(v==='dev')root.querySelector('[data-field="harborDev"]')?.select();}
    else if(el.id==='nb-import-file'){
      const file=el.files[0];if(!file)return;
      try{if(file.size>100000)throw Error('Recipe file is too large.');recipe=C.fromJSON(await file.text());step=REVIEW;render(true);status('Imported “'+(recipe.title||recipe.name)+'”. Review the recipe before copying commands.');}catch(error){status('Could not import recipe: '+error.message);}el.value='';
    }
  });
  { const dl=document.getElementById('nb-models'); if(dl){const seen=new Set(); for(const o of [...dl.options]){const v=C.bareModel(o.value); if(!v||seen.has(v)){o.remove();continue;} seen.add(v); o.value=v;}} }
  render();
  status(opened||'');
})();
