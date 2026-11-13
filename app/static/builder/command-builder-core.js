/* Pure recipe compiler for Harbor recipes. It prints shell; it has no DOM, storage, or execution API.
   This file also creates, loads and saves Nullius standard recipes; the server compiles those
   (POST /new-run/runner, standard/recipe.py). Other harnesses are rejected. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else root.NulliusRecipe = factory;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (schema) {
  'use strict';
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const copy = value => JSON.parse(JSON.stringify(value));
  const q = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
  const path = value => String(value).startsWith('~/') ? '"$HOME"/' + q(String(value).slice(2)) : q(value);
  const env = name => '"${' + name + ':?Set ' + name + ' in your shell}"';
  const join = parts => parts.join(' \\\n  ');
  const textValue = value => typeof value === 'string' ? value : JSON.stringify(value);
  // Keep string literals (e.g. 'false' or '12') distinct from typed values
  // when the harness decodes key=value through JSON/YAML.
  const kwargValue = value => typeof value === 'string' && /^[A-Za-z_][A-Za-z0-9_./-]*$/.test(value) && !/^(?:true|false|null|none|yes|no|on|off)$/i.test(value) ? value : JSON.stringify(value);
  const parameters = () => schema.parameters;
  // Error step numbers match nullius/providers.py.
  // Setup is a reserved position in saved drafts; the Agent step selects setups, agents and checks.
  // The benchmark first, then what runs it: a runner is only a choice when the benchmark has both.
  const STEPS = ['Benchmark', 'Runner', 'Dataset', 'Setup', 'Agent', 'Model', 'Limits', 'Where', 'Review'];
  const STEP = {benchmark: 0, runner: 1, dataset: 2, setup: 3, agent: 4, model: 5, limits: 6, where: 7, review: 8};
  const runners = () => schema.runners || [{key: 'harbor', label: 'Harbor', builtin: true}];
  // Unsupported runners use the server's rejection message.
  const RUNNER_GONE = schema.runnerGone || 'Only Harbor and Nullius recipes are supported.';
  // The Nullius standard (nullius/standard/recipe.py): a benchmark, an agent or a capability that proved the ports it
  // needs, a model. Its choices and why a pair does not fit come with the schema; the server compiles the recipe.
  const isStandard = r => !!(r && r.harness === 'standard');
  const standard = () => schema.standard || {benchmarks: [], agents: [], capabilities: [], fits: {}, ports: {}};
  // A recipe names a benchmark by its key (its default version) or key@version, one of the page's versions.
  const standardBench = r => standard().benchmarks.find(b => b.key === r.benchmark || b.ref === r.benchmark
    || (b.versions || []).some(v => v.ref === r.benchmark)) || null;
  // Every agent (or capability) a benchmark of the standard could take, each with the reasons it does not fit: none, it fits.
  // Then the controls it declares (nop, oracle): no agent, no model, always runnable - each with its source
  // (official, adapter, nullius) and, per dataset, its proof.
  function standardOthers(r) {
    const b = standardBench(r);
    if (!b) return [];
    const why = standard().fits[r.benchmark] || standard().fits[b.key] || {};
    const controls = (b.controls || []).map(c => ({key: c.key, label: c.label, summary: c.from, protocol: 'no agent', source: c.source,
      version: '', offers: [], proven: {}, options: {}, control: true, why: []}));
    // its setups (recipe.py, _bench_page): the benchmark as published, run by its own code - beside the agents or capabilities
    const setups = (b.setups || []).map(x => ({...x, protocol: 'its own code', version: '', offers: [], proven: {}, options: {}, setup: true, why: []}));
    return (b.family === 'agent' ? standard().agents : standard().capabilities).map(o => ({...o, why: why[o.key] || []})).concat(setups, controls);
  }
  // A control that calls no model at all - on a benchmark with no judge - needs no key to run.
  // The agents, capabilities and controls a standard recipe runs, each its own job of the one run.
  const chosenOf = r => Array.isArray(r.others) ? r.others : (r.other ? [r.other] : []);
  // The datasets a standard recipe runs: each with each choice, a job each; dataset also accepts one key.
  const datasetsOf = r => Array.isArray(r.datasets) ? r.datasets : (r.dataset ? [r.dataset] : []);
  const standardKeyless = r => { const b = standardBench(r), c = chosenOf(r); return !!b && !b.judged && c.length > 0 && c.every(k => (b.controls || []).some(x => x.key === k)); };
  // Where a standard recipe runs: a list of machines, 'local' or a host's entry; one machine is kept as a word.
  const whereList = r => Array.isArray(r.where) ? r.where : [r.where || 'local'];
  // A step this recipe shows: every one but Setup.
  const stepShown = (r, i) => i !== STEP.setup;
  // Every participant except an unjudged control uses the one selected model.
  const paperModels = r => ((standardBench(r) || {}).setups || []).find(x => x.key === 'original') || {};
  // Like the paper keeps what the paper fixed: its models when it lists them, its judge and its user simulator.
  const paperListed = r => !!(paperModels(r).models || []).length;
  const paperOffered = r => { const b = standardBench(r) || {}; return paperListed(r) || !!(b.judge || b.simulator); };
  // Like the leaderboard: what the upstream's leaderboard fixes besides the ranked model, when the benchmark declares it.
  const leaderboard = r => (standardBench(r) || {}).leaderboard || null;
  // Choose how the model is chosen: paper or leaderboard keep their fixed models (the leaderboard also its options and
  // fewest attempts); leaving the leaderboard gives its options back to their defaults.
  function setModelChoice(r, choice) {
    const lb = leaderboard(r), was = r.model_choice;
    r.model_choice = choice;
    if (paperListed(r)) r.model = '';
    if (choice !== 'any') { delete r.judge_model; delete r.simulator_model; }
    if (choice !== was) delete r.served_by;     // its models change with it: a pin belongs to the model it was set for
    if (lb && was === 'leaderboard' && choice !== 'leaderboard') {
      r.bench_options = {...(r.bench_options || {})};
      for (const k of Object.keys(lb.bench_options || {})) delete r.bench_options[k];
    }
    if (lb && choice === 'leaderboard') {
      r.bench_options = {...(r.bench_options || {}), ...(lb.bench_options || {})};
      r.repeat = Math.max(Number(r.repeat) || 1, lb.repeat || 1);
    }
    return r;
  }
  // Served by (recipe.py, served_by): on OpenRouter, the serving providers each model field pins, in the order they are
  // tried; a field per model the run calls - the model, the user simulator's and the judge's when the benchmark has them.
  const SERVED_ROLES = ['model', 'simulator', 'judge'];
  function servedRoles(r) {
    const b = standardBench(r);
    if (!b || r.provider !== 'openrouter' || standardKeyless(r)) return [];
    return SERVED_ROLES.filter(k => k === 'model' || (k === 'simulator' && b.simulator) || (k === 'judge' && b.judge));
  }
  // A field's text, "deepinfra, together", as the pin's list, every part kept for the server to check ("a,,b" is
  // refused in sight, as the option refuses it); a field with nothing in it pins nothing.
  const servedList = text => { const parts = String(text || '').split(',').map(s => s.trim()); return parts.some(Boolean) ? parts : []; };
  // A model field's role, and its model changed: the pin set for the model before it goes with it.
  const SERVED_FIELD = {model: 'model', simulator_model: 'simulator', judge_model: 'judge'};
  function setModelOf(r, field, value) {
    if (SERVED_FIELD[field] && r[field] !== value && r.served_by) setServed(r, SERVED_FIELD[field], []);
    r[field] = value;
    return r;
  }
  // The model a field's Served by lists the providers of: the one the run sends, as the server compiled it
  // (recipe.py, served_models: a paper model's served name, an official one's with its maker), else the one shown.
  const servedModelOf = (role, shown, compiled) => ((compiled || {}).served_models || {})[role] || shown || '';
  function setServed(r, role, slugs) {
    const list = Array.isArray(slugs) ? slugs : servedList(slugs);
    r.served_by = {...(r.served_by || {})};
    if (list.length) r.served_by[role] = list; else delete r.served_by[role];
    if (!Object.keys(r.served_by).length) delete r.served_by;
    return r;
  }
  function createStandard(benchmark = '', other = '') {
    const st = standard(), b = st.benchmarks.find(x => x.key === benchmark) || st.benchmarks[0];
    const r = {version: 1, harness: 'standard', name: 'standard-experiment', title: 'Standard experiment', benchmark: '', datasets: [], others: [],
      model_choice: 'any', provider: firstAvailable(PROVIDERS, 'openai'), model: bareModel(schema.defaultModel || ''), limit: '', concurrency: '', unsandboxed: false, where: 'local',
      backend: 'local', options: {}};
    return b ? setStandardBenchmark(r, b.key, other) : r;
  }
  // Keep only options and values the benchmark accepts.
  const ownOptions = (b, opts) => Object.fromEntries(Object.entries(opts || {}).filter(([k, v]) => {
    const o = ((b || {}).options || {})[k]; return o && (o.values ? o.values.includes(v) : new RegExp('^(?:' + o.pattern + ')$').test(v)); }));
  // key: a benchmark's key, or one of its versions' refs (key@version)
  function setStandardBenchmark(r, key, other = '') {
    const b = standardBench({benchmark: key});
    if (!b) return r;
    const was = standardBench(r);
    if (was !== b) { r.model_choice = 'any'; delete r.served_by; }
    r.benchmark = key;
    const kept = was === b ? datasetsOf(r).filter(id => b.datasets.some(d => d.id === id)) : [];
    r.datasets = kept.length ? kept : [b.datasets[0].id];
    delete r.dataset;
    const fitting = standardOthers(r).filter(o => !o.why.length);
    const keys = fitting.map(o => o.key), first = fitting.find(o => o.setup && o.key === 'original')
      || fitting.find(o => !o.control && !o.setup);
    // Keep explicit choices that still fit; otherwise prefer the benchmark's reference.
    r.others = other && keys.includes(other) ? [other] : chosenOf(r).filter(k => keys.includes(k));
    delete r.other;
    if (!r.others.length && first) r.others = [first.key];
    r.options = Object.fromEntries(Object.entries(r.options || {}).filter(([k, v]) => r.others.includes(k) && v && typeof v === 'object'));   // options are each agent's own
    if (r.bench_options) r.bench_options = ownOptions(b, r.bench_options);
    if (was !== b || r.name === 'standard-experiment') { r.name = b.key + '-experiment'; r.title = b.label + ' experiment'; }
    return r;
  }
  // A saved standard recipe read back: known fields only, each of its type.
  function standardFromJSON(raw) {
    const r = createStandard(typeof raw.benchmark === 'string' ? raw.benchmark : '');
    for (const key of ['name', 'title', 'model', 'where', 'model_choice']) if (typeof raw[key] === 'string') r[key] = raw[key];
    const sets = Array.isArray(raw.datasets) ? raw.datasets.filter(k => typeof k === 'string') : typeof raw.dataset === 'string' && raw.dataset ? [raw.dataset] : null;
    if (sets && sets.length) r.datasets = [...new Set(sets)];
    const picked = Array.isArray(raw.others) ? raw.others.filter(k => typeof k === 'string') : typeof raw.other === 'string' && raw.other ? [raw.other] : null;
    if (picked) r.others = [...new Set(picked)];
    if (Array.isArray(raw.where) && raw.where.length && raw.where.every(w => typeof w === 'string')) r.where = raw.where.length > 1 ? [...raw.where] : raw.where[0];
    if (['local', 'container', 'docker'].includes(raw.backend)) r.backend = raw.backend === 'docker' ? 'container' : raw.backend;   // docker: its earlier name
    if (raw.bench_options && typeof raw.bench_options === 'object' && !Array.isArray(raw.bench_options))   // the benchmark's own options, a word each
      r.bench_options = ownOptions(standardBench(r), Object.fromEntries(Object.entries(raw.bench_options).filter(([, v]) => typeof v === 'string')));
    // Options per choice, {ref: {name: word}}; a single agent also accepts flat {name: word} options.
    if (raw.options && typeof raw.options === 'object' && !Array.isArray(raw.options)) {
      const words = o => Object.fromEntries(Object.entries(o).filter(([, v]) => typeof v === 'string'));
      const flat = !Object.values(raw.options).some(v => v && typeof v === 'object');
      r.options = flat ? (r.others.length === 1 ? {[r.others[0]]: words(raw.options)} : {})
        : Object.fromEntries(Object.entries(raw.options).filter(([k, v]) => r.others.includes(k) && v && typeof v === 'object').map(([k, v]) => [k, words(v)]));
    }
    if (PROVIDERS.includes(raw.provider)) r.provider = raw.provider;
    keepEndpoint(r, raw);
    if ((Number.isInteger(raw.limit) && raw.limit >= 1) || raw.limit === '') r.limit = raw.limit;
    if ((Number.isInteger(raw.concurrency) && raw.concurrency >= 1) || raw.concurrency === '') r.concurrency = raw.concurrency;
    // attempts per unit and the judge's and user simulator's models: a reopened recipe runs as it was saved
    if (Number.isInteger(raw.repeat) && raw.repeat >= 1 && raw.repeat <= 32) r.repeat = raw.repeat;
    // the run's spending cap in USD, shared by its jobs (recipe.py): an amount above zero, or none
    if (typeof raw.max_usd === 'number' && Number.isFinite(raw.max_usd) && raw.max_usd > 0) r.max_usd = raw.max_usd;
    // and each unit's own: a unit that reaches it is cut off there, the run going on
    if (typeof raw.max_usd_per_unit === 'number' && Number.isFinite(raw.max_usd_per_unit) && raw.max_usd_per_unit > 0) r.max_usd_per_unit = raw.max_usd_per_unit;
    for (const key of ['judge_model', 'simulator_model']) if (typeof raw[key] === 'string' && raw[key].trim()) r[key] = raw[key].trim();
    // Served by: each field's serving providers, slugs in their order (the server checks them as --served-by)
    if (raw.served_by && typeof raw.served_by === 'object' && !Array.isArray(raw.served_by))
      for (const role of SERVED_ROLES) { const v = raw.served_by[role];
        // kept as saved, a slug the server refuses included: a pin is refused in sight, never dropped in silence
        if (Array.isArray(v) && v.length) setServed(r, role, v.map(s => typeof s === 'string' ? s.trim() : String(s))); }
    r.unsandboxed = raw.unsandboxed === true;
    if(raw.modelFormat === 'provider-id')r.modelFormat='provider-id';
    else r.model = bareModel(r.model);
    if (typeof raw.title !== 'string' || !raw.title.trim()) r.title = r.name;
    return r;
  }
  // Custom agents are import paths typed per recipe: `custom-<n>` names the
  // nth entry of r.customAgents, and behaves as an agent under evaluation.
  const customIndex = id => /^custom-\d+$/.test(id) ? Number(id.slice(7)) : -1;
  const agentById = (id, r) => customIndex(id) >= 0
    ? (r && r.customAgents[customIndex(id)] !== undefined ? {id, label: 'Custom · ' + (r.customAgents[customIndex(id)] || 'import path'), arg: r.customAgents[customIndex(id)], control: false, kind: 'evaluated', custom: true} : undefined)
    : agents().find(a => a.id === id);
  // The agents on offer: the vocabulary (launch.py), each marked whether the
  // recipe's Harbor registers it, and - once that Harbor has been scanned
  // (GET/POST /new-run/harbor/agents) - every other name it registers, as a
  // Harbor built-in named as Harbor names it. Without a scan there are no
  // built-ins to offer, and the page says where they come from.
  let scannedAgents = null;
  function mergeAgents(names, source) {
    if (!Array.isArray(names)) return schema.agents.map(a => ({...a, scanned: false}));
    const has = new Set(names), vocab = new Set(schema.agents.map(a => a.arg));
    return [...schema.agents.map(a => ({...a, scanned: has.has(a.arg)})),
      ...[...has].filter(n => !vocab.has(n)).sort().map(n => ({id: n, label: n, arg: n, control: false, kind: 'builtin', scanned: true,
        summary: source ? 'Harbor built-in, as ' + source + ' registers it.' : 'Harbor built-in.'}))];
  }
  function setScanned(names, source) { scannedAgents = Array.isArray(names) ? mergeAgents(names, source) : null; return agents(); }
  const agents = () => scannedAgents || schema.agents;
  const secret = key => /(?:api[-_]?key|token|password|authorization|credential|secret)/i.test(key);
  const defaults = () => Object.fromEntries(parameters().map(p => [p.key, p.default]));
  // The provider is a recipe field and the model a bare name. Each consumer gets
  // the prefix matching its credential bindings; a typed model prefix takes precedence.
  const PROVIDERS = ['openrouter', 'openai', 'custom'];
  const PROVIDER_PREFIX = /^(openrouter|openai)\//;
  const bareModel = m => String(m || '').replace(PROVIDER_PREFIX, '');
  const providerOf = (r, m) => { if(r.modelFormat === 'provider-id' && PROVIDERS.includes(r.provider))return r.provider; const x = String(m || '').match(PROVIDER_PREFIX); return x ? x[1] : (PROVIDERS.includes(r.provider) ? r.provider : 'openrouter'); };
  // A Harbor recipe's provider and model settled once, the model as its provider names it (modelFormat
  // 'provider-id'): the page then reads the provider /run uses, the recipe's own. A leading openrouter/ or
  // openai/ says the provider only where the recipe names none; on OpenRouter openai/ is the model's own
  // name there (openai/gpt-4o), never another provider.
  function settleModel(provider, model) {
    const m = String(model || ''), x = m.match(PROVIDER_PREFIX), p = PROVIDERS.includes(provider) ? provider : '';
    if (!x) return [p || 'openrouter', m];
    if (x[1] === 'openai' && p === 'openrouter') return [p, m];
    const q = p || x[1], rest = m.slice(x[0].length);
    return [q, q === 'openrouter' ? rest : rest.replace(PROVIDER_PREFIX, '')];
  }
  // Into a recipe: a bare name as before, but an OpenRouter model named openai/... (which a bare name cannot
  // say, its prefix reading as the OpenAI provider) as the provider names it.
  function adoptModel(r, provider, model, key = 'model') {
    const [p, m] = settleModel(provider, model);
    r.provider = p;
    if (r.modelFormat === 'provider-id' || (p === 'openrouter' && PROVIDER_PREFIX.test(m))) {
      if (r.modelFormat !== 'provider-id') for (const k of ['model', 'userModel', 'evalModel']) if (k !== key && typeof r[k] === 'string') r[k] = settleModel(p, r[k])[1];
      r.modelFormat = 'provider-id'; r[key] = m;
    } else r[key] = bareModel(m);
  }
  // Where a run started here can go: the providers whose key - for an OpenAI-compatible endpoint, its URL, the key
  // being optional - is set in Settings (schema.credentials, runners.available), in the order the Model step prefers
  // them. With none, every provider stays visible. A schema that does not say (a test's) is
  // checked by the server alone.
  const PROVIDER_LABEL = {openrouter: 'OpenRouter', openai: 'OpenAI', custom: 'OpenAI-compatible'};
  const PROVIDER_NEEDS = {openrouter: 'OPENROUTER_API_KEY', openai: 'OPENAI_API_KEY', custom: 'MODEL_API_URL'};
  // A named OpenAI-compatible endpoint: the recipe keeps provider custom and the endpoint's id, which names its
  // settings (MODEL_<ID>_API_URL); the Model step offers it as the choice custom:<id> (nullius/providers.py).
  const ENDPOINT_ID = /^(?=.{1,32}$)[A-Z0-9]+(?:_[A-Z0-9]+)*$/;
  function keepEndpoint(r, raw) { if (r.provider === 'custom' && ENDPOINT_ID.test(raw.endpoint || '')) r.endpoint = raw.endpoint; else delete r.endpoint; }
  const kindOf = c => String(c).split(':')[0];
  const endpointOf = c => String(c).split(':')[1] || '';
  const labelOf = c => endpointOf(c) ? ((schema.credentials || {}).endpoints || {})[endpointOf(c)] || endpointOf(c) : PROVIDER_LABEL[c] || c;
  const needsOf = c => endpointOf(c) ? 'MODEL_' + endpointOf(c) + '_API_URL' : PROVIDER_NEEDS[c] || PROVIDER_NEEDS.custom;
  const available = () => ((schema.credentials || {}).available || []).filter(p => PROVIDERS.includes(kindOf(p)));
  const firstAvailable = (offered, otherwise) => available().find(p => offered.includes(p)) || otherwise;
  const providersOf = () => PROVIDERS;
  const recipeProvider = r => isStandard(r) ? r.provider : providerOf(r, r.model || r.userModel);
  const choiceOf = r => recipeProvider(r) === 'custom' && r.endpoint ? 'custom:' + r.endpoint : recipeProvider(r);
  // The Model step's choice into the recipe: its provider, and for a named endpoint its id.
  // a provider that routes no model to several takes no Served by: a pin made for OpenRouter is dropped with it
  function setChoice(r, c) { r.provider = kindOf(c); keepEndpoint(r, {endpoint: endpointOf(c)}); if (r.provider !== 'openrouter') delete r.served_by; }
  // Why Run cannot start this recipe here, or '': its provider is not one of those. A recipe naming one keeps it,
  // said, until another is chosen - never switched behind the person's back. The server refuses the same
  // (web/execute.py, provider_problem).
  function providerBlocker(r) {
    if (!schema.credentials || (!isStandard(r) && !needsCredentials(r))) return '';
    if (isStandard(r) && standardKeyless(r)) return '';      // a control of an unjudged benchmark is handed no key (execution.Plan.keyless)
    const offered = providersOf(r), have = available().filter(p => offered.includes(kindOf(p))), p = choiceOf(r);
    const or = xs => xs.slice(0, -1).join(', ') + (xs.length > 1 ? ' or ' : '') + xs[xs.length - 1];
    if (have.includes(p)) return '';
    if (!have.length) return 'Run needs a model key, and none is set in Settings: set ' + or(offered.map(x => PROVIDER_NEEDS[x])) + ' there.';
    if (((schema.credentials || {}).broken || []).includes(p))
      return 'This recipe runs on ' + labelOf(p) + ', and its URL in Settings holds a credential or is not an http(s) URL. Put the key in the Key field in Settings.';
    return 'This recipe runs on ' + labelOf(p) + ', and ' + needsOf(p) + ' is not set in Settings. Choose '
      + or(have.map(labelOf)) + ' on the Model step to run it, or set it in Settings.';
  }
  // Harbor's registry agents: routed by prefix; an OpenAI-compatible endpoint is spelled openai/.
  const agentModel = (r, m = r.model) => (providerOf(r, m) === 'openrouter' ? 'openrouter' : 'openai') + '/' + (r.modelFormat === 'provider-id' ? String(m || '') : bareModel(m));
  // The τ² simulated user and judge: litellm with OPENAI_API_KEY + OPENAI_BASE_URL, whatever the provider.
  const tau2Model = (m, r) => 'openai/' + (r.modelFormat === 'provider-id' ? String(m || '') : bareModel(m));
  // The τ³ reference agent: litellm-native, with the provider's own key name bound beside it (credentialBindings).
  // The τ³ runtime container reads that same name too: τ²'s OpenRouter embedder (dense
  // search in the alltools-qwen retrieval setup) takes OPENROUTER_API_KEY and nothing
  // else, so every part of a τ³ run on OpenRouter carries it, including the reference agent.
  const litellmModel = (r, m = r.model) => agentModel(r, m);
  const benchmarkCallsModel = r => r.benchmark === 'tau3-bench';
  const needsCredentials = r => benchmarkCallsModel(r) || r.agents.some(id => !agentById(id, r)?.control);
  function credentialBindings(r, agentId) {
    if (!needsCredentials(r)) return [];
    const agentCallsModel = agentId ? !agentById(agentId, r)?.control : r.agents.some(id => !agentById(id, r)?.control);
    if (!agentCallsModel && !benchmarkCallsModel(r)) return [];
    const keyTargets = [...(agentCallsModel ? ['LLM_API_KEY'] : []), 'OPENAI_API_KEY', ...((agentId === 'tau3-llm-agent' || benchmarkCallsModel(r)) && providerOf(r, r.model) === 'openrouter' ? ['OPENROUTER_API_KEY'] : [])];
    const urlTargets = [...(agentCallsModel ? ['LLM_BASE_URL'] : []), 'OPENAI_BASE_URL'];
    return [{source: r.keyEnv, targets: keyTargets}, ...(r.urlEnv ? [{source: r.urlEnv, targets: urlTargets}] : [])];
  }
  function benchmarks() {
    return schema.benchmarks.filter(b => b.harness.includes('harbor'));
  }
  // Why a benchmark cannot be launched on Harbor, or '' when it can.
  function routeBlocker(key) {
    if (key === 'custom') return '';
    if (benchmarks().some(b => b.key === key)) return '';
    const b = schema.benchmarks.find(b => b.key === key);
    if (!b) return 'This benchmark has no supported launch adapter.';
    return b.title + ' has no Harbor route; the harness it runs on is not available here.';
  }
  function dataset(r) { return schema.datasets.find(d => d.benchmark === r.benchmark) || null; }
  // How many tasks an unlimited recipe would run: the catalogue's count for the benchmark's dataset, 0 when it has none.
  const taskCount = r => Number(dataset(r)?.tasks) || 0;
  const datasetLabel = r => ['local','generate'].includes(r.source) ? r.path : r.dataset;
  // Where a reference agent's adapter checkout is: the recipe's own path for
  // it, else the vocabulary's default. `~` is the run target's home.
  const adapterPath = (r, id) => (r.adapterPaths && r.adapterPaths[id]) || agentById(id, r)?.pythonpath || '';
  function sources(r) {
    const d = dataset(r), options = [];
    if (d?.hub) options.push(['hub', d.hub.includes('(private)') ? 'Private hub' : 'Harbor hub']);
    else if (d?.registry) options.push(['registry', 'Versioned registry']);
    else options.push(['registry', 'Registry or hub']);
    options.push(['local', 'Local folder']);
    if (d?.generate) options.push(['generate', 'Generate tasks']);
    return options;
  }
  function setBenchmark(r, key) {
    r.model_choice = 'any';
    r.benchmark = key;
    const d = dataset(r);
    r.source = sources(r)[0][0];
    r.dataset = d?.registry || '';
    r.path = '';   // a Harbor folder is typed, never pre-filled: the usual one is the placeholder
    return r;
  }
  function create() {
    const r = {version: 1, harness: 'harbor', name: 'harbor-experiment', title: 'Harbor experiment', benchmark: '', source: '', dataset: '', path: '',
      generatorRoot: '', agents: [], harbor: {kind: 'release', version: '', extras: []},
      customAgents: [], model_choice: 'any', provider: firstAvailable(PROVIDERS, providerOf({}, schema.defaultModel || '')), model: '', userModel: '', evalModel: '', keyEnv: (schema.credentialVars && schema.credentialVars.key) || 'API_TOKEN', urlEnv: schema.credentialVars ? (schema.credentialVars.url || '') : 'API_URL',
      tasks: '', concurrency: 1, retries: 3,
      timeoutMultiplier: 1, agentMinutes: '', attempts: '', setupTimeoutMultiplier: '', verifierTimeoutMultiplier: '', buildTimeoutMultiplier: '', harborDev: '', include: '', output: 'jobs',
      where: 'local', adapterPaths: {}, params: defaults(), custom: []};
    adoptModel(r, r.provider, schema.defaultModel || '');
    const b = benchmarks().find(b => b.key === 'tau3-bench') || benchmarks()[0];
    return setBenchmark(r, b?.key || 'custom');
  }
  // Harbor's --agent-timeout-multiplier scales every per-phase timeout the
  // selected tasks declare. The catalogue records those declarations where a
  // real run has shown them (phase timeout_sec on the feed ÷ the run's
  // multiplier); a benchmark without a recording gets no numbers, only the rule.
  const PHASES = [['env_setup', 'Environment setup'], ['agent_setup', 'Agent setup'], ['agent_run', 'Agent'], ['verify', 'Verifier']];
  function declaredTimeouts(r) {
    const t = schema.benchmarks.find(b => b.key === r.benchmark)?.timeouts;
    return t && PHASES.every(([k]) => Number.isFinite(Number(t[k])) && Number(t[k]) > 0) ? t : null;
  }
  // Each phase has its own Harbor multiplier; the recipe keeps the multipliers
  // (that is what Harbor takes), and the page shows minutes where the
  // benchmark's declared timeouts are recorded.
  const PHASE_FIELD = {env_setup: 'buildTimeoutMultiplier', agent_setup: 'setupTimeoutMultiplier', agent_run: 'timeoutMultiplier', verify: 'verifierTimeoutMultiplier'};
  const phaseMultiplier = (r, key) => { const v = r[PHASE_FIELD[key]]; return v === '' || v == null ? 1 : Number(v); };
  function effectiveTimeouts(r) {
    const t = declaredTimeouts(r);
    if (!t) return null;
    return {phases: PHASES.map(([key, label]) => {
      const m = phaseMultiplier(r, key), ok = Number.isFinite(m) && m > 0;
      return {key, label, field: PHASE_FIELD[key], declared: Number(t[key]), multiplier: m, effective: ok ? Math.round(Number(t[key]) * m) : null,
              minutes: ok ? Math.round(Number(t[key]) * m / 60 * 100) / 100 : null};
    })};
  }
  // The absolute form of the same knob: minutes a phase may take per trial,
  // turned into that phase's multiplier against its declared timeout. The
  // agent phase keeps `agentMinutes` as typed; the others are derived.
  function setPhaseMinutes(r, key, minutes) {
    const t = declaredTimeouts(r), v = minutes === '' || minutes == null ? '' : Number(minutes);
    if (key === 'agent_run') r.agentMinutes = v;
    if (!t) return r;
    if (v === '') { if (key !== 'agent_run') r[PHASE_FIELD[key]] = ''; return r; }
    if (Number(v) > 0) r[PHASE_FIELD[key]] = Math.round(Number(v) * 60 / Number(t[key]) * 1000) / 1000;
    return r;
  }
  // Which Harbor runs a recipe.
  //
  // A recipe pins its Harbor (`r.harbor`, chosen on the Runner step): a
  // release of the official package on PyPI, `{kind: 'release', version,
  // extras}`, or a repository - a private fork included - at a commit,
  // `{kind: 'repo', repo, revisionKind, revision, commit, extras}`, the
  // commit being the pin a branch or a tag resolved to. Run builds a repository
  // on this machine and runs it here or on a host (nullius/harbor/source.py).
  //
  const HARBOR_VERSION = /^[0-9][0-9A-Za-z.+!-]{0,63}$/, HARBOR_EXTRA = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/, COMMIT = /^[0-9a-f]{40}$/;
  // A repository as git is given it: owner/name is GitHub over https; a URL or user@host:path as typed (nullius/gitrefs.py url_of).
  function harborRepoUrl(text) {
    const t = String(text || '').trim(), m = t.match(/^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/);
    if (!t || t.startsWith('-') || /[\s\x00-\x1f]/.test(t)) return '';
    if (m && !t.startsWith('.')) return 'https://github.com/' + m[1] + '/' + m[2].replace(/\.git$/, '') + '.git';
    return /^(?:https?|ssh|git|file):\/\/\S+$/.test(t) || /^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:\S+$/.test(t) ? t : '';
  }
  const repoLabel = url => (String(url).match(/^(?:https:\/\/github\.com\/|ssh:\/\/git@github\.com\/|git@github\.com:)([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?\/?$/) || [])[1] || url;
  // The recipe's pin when it names one completely, else null.
  function harborPin(r) {
    const h = r.harbor || {}, extras = [...new Set((Array.isArray(h.extras) ? h.extras : []).filter(x => HARBOR_EXTRA.test(x)))].sort();
    if (h.kind === 'release') return HARBOR_VERSION.test(h.version || '') ? {kind: 'release', version: h.version, extras} : null;
    if (h.kind !== 'repo' || !harborRepoUrl(h.repo) || !COMMIT.test(h.commit || '')) return null;
    const revisionKind = ['branch', 'tag', 'commit'].includes(h.revisionKind) ? h.revisionKind : 'commit';
    return {kind: 'repo', repo: String(h.repo).trim(), url: harborRepoUrl(h.repo), revisionKind, revision: h.revision || h.commit, commit: h.commit, extras};
  }
  // What is wrong with a pin half-chosen; '' for a complete pin and for none chosen at all (a release not picked yet).
  function harborProblem(r) {
    const h = r.harbor || {};
    if (h.kind === 'release') return !h.version || HARBOR_VERSION.test(h.version) ? '' : h.version + ' is not a Harbor release version.';
    if (h.kind !== 'repo') return '';
    if (!harborRepoUrl(h.repo)) return 'Enter the repository that holds the Harbor: owner/name on GitHub, or a git URL.';
    return COMMIT.test(h.commit || '') ? '' : 'Choose a branch, a tag or a full commit of ' + repoLabel(harborRepoUrl(h.repo)) + ': the pin is a commit.';
  }
  // The directory the server keeps a pin in (source.key_of): what its scan is asked by.
  const harborKey = pin => pin.kind === 'release' ? 'release-' + pin.version : 'git-' + pin.commit;
  // What `uvx --from` installs: the release from PyPI, or the repository at the pinned commit (a URL uv clones; scp-like becomes ssh://).
  function harborSpec(pin) {
    const ex = pin.extras.length ? '[' + pin.extras.join(',') + ']' : '';
    if (pin.kind === 'release') return 'harbor' + ex + '==' + pin.version;
    const scp = pin.url.match(/^([A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+):(.*)$/);
    return 'harbor' + ex + ' @ git+' + (scp ? 'ssh://' + scp[1] + '/' + scp[2].replace(/^\//, '') : pin.url) + '@' + pin.commit;
  }
  function harborLabel(pin) {
    const ex = pin.extras.length ? ' · extras ' + pin.extras.join(', ') : '';
    if (pin.kind === 'release') return 'harbor ' + pin.version + ' · PyPI' + ex;
    const at = pin.commit.slice(0, 12);
    return 'harbor from ' + repoLabel(pin.url) + ' · ' + (pin.revisionKind === 'commit' ? 'commit ' + at : pin.revisionKind + ' ' + pin.revision + ' at ' + at) + ex;
  }
  function harborExecutable(r) {
    if (r.harborDev) return {kind: 'development', executable: r.harborDev, command: path(r.harborDev), label: 'development · ' + r.harborDev + ' — not a release'};
    const pin = harborPin(r);
    if (pin) return {kind: pin.kind, executable: 'harbor', command: 'uvx --from ' + q(harborSpec(pin)) + ' harbor', label: harborLabel(pin), pin};
    return {kind: 'path', executable: 'harbor', command: 'harbor', label: 'harbor from PATH'};
  }
  function scopes() {
    return [['agent', 'Agent · --ak'], ['harness', 'Harness option'], ['environment', 'Environment · --ek'], ['verifier', 'Verifier kwarg']];
  }
  // `harbor run` options the named fields already write; a custom one of these would be a duplicate flag.
  const RESERVED = ['agent','a','model','m','dataset','d','path','p','n-tasks','l','n-concurrent','n','max-retries','r','env','e','job-name','jobs-dir','o','agent-timeout-multiplier','include-task-name','i','ak','agent-kwarg','ek','environment-kwarg','verifier-kwarg','agent-env','ae'];
  function validate(r) {
    const errors = [], notes = [];
    const add = (step, message, group) => errors.push({step, message, ...(group ? {group} : {})});
    if (isStandard(r)) return {errors, notes};   // compiled and checked by the server, from its definition files
    if (r.harness !== 'harbor') { add(STEP.runner, RUNNER_GONE); return {errors, notes}; }
    const blocker = routeBlocker(r.benchmark);
    if (blocker) add(STEP.benchmark, blocker);
    if (!sources(r).some(([s]) => s === r.source)) add(STEP.dataset, 'Choose a dataset source supported by this benchmark.');
    const local = r.source === 'local';
    if (local && !r.path.trim()) add(STEP.dataset, 'Enter the local task folder.');
    if (!local && r.source !== 'generate' && !r.dataset.trim()) add(STEP.dataset, 'Enter a dataset reference.');
    if (r.dataset.includes('<') || r.dataset.includes('>')) add(STEP.dataset, 'Replace the placeholder with a real dataset or task.');
    if (r.source === 'generate') {
      if (!r.generatorRoot.trim() || !r.path.trim()) add(STEP.dataset, 'Set the generator checkout and the folder the tasks are written to.');
    }
    if (!Array.isArray(r.agents) || !r.agents.length) add(STEP.agent, 'Choose at least one agent.');
    const hp = harborProblem(r);
    if (hp) add(STEP.runner, hp);
    for (const id of r.agents) if (!agentById(id, r)) add(STEP.agent, customIndex(id) >= 0 ? 'Custom agent ' + id + ' has no import path.' : id + ' is not an agent this builder offers' + (scannedAgents ? '' : ' until the recipe\'s Harbor is scanned (Runner step)') + '; or name it as a custom agent (module:Class) instead.');
    for (const id of r.agents) { const a = agentById(id, r); if (a?.benchmarks && !a.benchmarks.includes(r.benchmark)) add(STEP.agent, a.label + ' runs only on ' + a.benchmarks.join(', ') + '.'); }
    r.customAgents.forEach((p, i) => { if (r.agents.includes('custom-' + i) && !/^[\w.]+:[\w.]+$/.test(p)) add(STEP.agent, 'Custom agent ' + (i + 1) + ' must use module:Class syntax.'); });
    const callsModel = needsCredentials(r);
    // τ³ with control agents only: the customer model is the one that matters, and the form asks for that one.
    const named = r.model.trim() || (benchmarkCallsModel(r) && r.agents.every(id => agentById(id, r)?.control) ? (r.userModel || '').trim() : '');
    if (callsModel && r.model_choice === 'paper') {
      const allowed = Object.values(paperModels(r).sent?.[r.provider] || {});
      if (!allowed.includes(r.model)) add(STEP.model, 'Choose one of the paper models on this provider.');
    }
    if (callsModel && !named) add(STEP.model, benchmarkCallsModel(r) ? 'Enter a model for the agent and/or simulated user and evaluator.' : 'Enter a model.');
    if (callsModel && !PROVIDERS.includes(r.provider)) add(STEP.model, 'Choose a provider: OpenRouter, OpenAI or an OpenAI-compatible endpoint.');
    for (const [key,label] of [['keyEnv','API key source variable'],['urlEnv','Endpoint source variable']]) {
      if (callsModel && (key !== 'urlEnv' || r[key]) && !/^[A-Z_][A-Z0-9_]*$/.test(r[key])) add(STEP.model, label + ' must be an environment variable name.');
    }
    if (callsModel && benchmarkCallsModel(r) && providerOf(r, r.model || r.userModel) !== 'openai' && !r.urlEnv) add(STEP.model, 'A non-OpenAI model needs an endpoint source variable.');
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(r.name)) add(STEP.limits, 'Recipe name may contain letters, numbers, dots, underscores and hyphens.');
    if (!r.output.trim()) add(STEP.limits, 'Enter the output directory.');
    for (const key of ['tasks','concurrency','retries']) {
      const min = key === 'retries' ? 0 : 1;
      if (key === 'tasks' && r.tasks === '') continue;                    // empty: every task, no limit written
      if (!Number.isInteger(Number(r[key])) || Number(r[key]) < min) add(STEP.limits, key + ' must be an integer of at least ' + min + ', or empty for every task.');
    }
    if (!Number.isFinite(Number(r.timeoutMultiplier)) || !(Number(r.timeoutMultiplier) > 0)) add(STEP.agent, 'Timeout multiplier must be positive.', 'Common');
    // Common trial settings (Harbor's own per-trial flags): empty means the flag is not written.
    for (const [key, label] of COMMON) if (r[key] !== '' && (!Number.isFinite(Number(r[key])) || !(Number(r[key]) > 0))) add(STEP.agent, label + ' must be a positive number, or empty.', 'Common');
    if (r.attempts !== '' && (!Number.isInteger(Number(r.attempts)) || Number(r.attempts) < 1)) add(STEP.agent, 'Repeats per task must be a whole number of at least 1, or empty.', 'Common');
    if (r.agentMinutes !== '' && r.agentMinutes != null) {
      if (!Number.isFinite(Number(r.agentMinutes)) || !(Number(r.agentMinutes) > 0)) add(STEP.limits, 'Max agent time per trial must be a positive number of minutes.');
      else if (!declaredTimeouts(r)) add(STEP.limits, 'No run has recorded this benchmark\'s declared timeouts, so minutes cannot be turned into a multiplier here; set the multiplier directly.');
    }
    if (r.harborDev && !/^(\/|~\/|\.{1,2}\/)/.test(r.harborDev)) add(STEP.limits, 'A development Harbor executable must be a path (absolute, ~/ or ./), not a program name.');
    // The shared agent parameters (the MCP call timeout): each a valid value of its type, or left to its default.
    for (const p of parameters()) {
      const v = r.params[p.key];
      if (v == null || v === '') continue;
      if ((p.type === 'integer' || p.type === 'number') && (!Number.isFinite(Number(v)) || (p.type === 'integer' && !Number.isInteger(Number(v))) || (p.min != null && Number(v) < p.min) || (p.max != null && Number(v) > p.max)))
        add(STEP.agent, 'Enter a valid ' + p.label.toLowerCase() + '.', p.group);
    }
    const seen = new Set(), parameterNames = new Set(parameters().map(p => p.key));
    for (const p of r.custom) {
      const customError = message => add(STEP.agent, message, 'Extra parameters');
      if (!scopes().some(([s]) => s === p.scope)) { customError('Choose a valid custom-parameter scope.'); continue; }
      if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(p.key)) customError('Parameter names must contain letters, numbers, underscores or hyphens.');
      if (secret(p.key)) customError('Use environment references for credentials; do not put them in custom parameters.');
      const id = p.scope + ':' + (p.agent || '') + ':' + p.key;
      if (seen.has(id)) customError('Duplicate parameter: ' + p.key);
      seen.add(id);
      if (p.scope === 'agent' && (parameterNames.has(p.key) || ['model','api_key','base_url','agent_timeout_sec','mcp_servers'].includes(p.key))) customError('Use the named field or task definition for ' + p.key + '.');
      if (p.scope === 'harness' && RESERVED.includes(p.key)) customError('Use the named field for ' + p.key + '.');
      if (!['string','number','boolean','JSON','flag'].includes(p.type)) customError('Choose a parameter type.');
      if (p.type === 'number' && (String(p.value).trim() === '' || !Number.isFinite(Number(p.value)))) customError(p.key + ' requires a number.');
      if (p.type === 'boolean' && !['true','false'].includes(String(p.value))) customError(p.key + ' requires true or false.');
      if (p.type === 'flag' && p.scope !== 'harness') customError('Flag without value is only supported for harness CLI options.');
      if (p.type === 'JSON') { try { JSON.parse(p.value); } catch { customError(p.key + ' requires valid JSON.'); } }
      notes.push('Custom ' + p.scope + ' parameter ' + (p.key || '(unnamed)') + ': check support in your installed version.');
    }
    if (harborExecutable(r).kind === 'development') notes.push('Development Harbor executable: ' + r.harborDev + ' is a checkout, not an immutable release; runs from it are development evidence.');
    else if (harborExecutable(r).kind === 'path' && !hp) notes.push('Choose Harbor on the Runner step.');
    // an agent that calls its own vendor (Cursor's CLI agent) needs that vendor's key: Settings, Custom keys
    const held = (schema.credentials || {}).agent_keys || [];
    for (const id of r.agents) {
      const want = (schema.agentNeeds || {})[agentById(id, r)?.arg || id];
      if (want && !want.some(k => held.includes(k))) notes.push((agentById(id, r)?.label || id) + ' needs ' + want.join(' or ') + ' of its own: add it in Settings, Custom keys, or its trials fail at the start.');
    }
    return {errors, notes: [...new Set(notes)]};
  }
  const customValue = p => p.type === 'number' ? Number(p.value) : p.type === 'boolean' ? String(p.value) === 'true' : p.type === 'JSON' ? JSON.parse(p.value) : p.value;
  // Harbor's per-trial settings every selected agent shares; `timeoutMultiplier`
  // (the agent phase), retries, concurrency and the task limit are on Limits.
  const COMMON = [['setupTimeoutMultiplier', 'Agent setup timeout multiplier'], ['verifierTimeoutMultiplier', 'Verifier timeout multiplier'], ['buildTimeoutMultiplier', 'Environment build timeout multiplier']];
  const COMMON_FLAGS = {setupTimeoutMultiplier: '--agent-setup-timeout-multiplier', verifierTimeoutMultiplier: '--verifier-timeout-multiplier', buildTimeoutMultiplier: '--environment-build-timeout-multiplier'};
  const commonFlags = r => [...(r.attempts !== '' ? ['--n-attempts ' + Number(r.attempts)] : []), ...COMMON.filter(([k]) => r[k] !== '').map(([k]) => COMMON_FLAGS[k] + ' ' + Number(r[k]))];
  const agentCustom = (r, id) => r.custom.filter(p => p.scope === 'agent' && (!p.agent || p.agent === id));
  const cliExtras = r => r.custom.filter(p => p.scope === 'harness').map(p => '--' + p.key + (p.type === 'flag' ? '' : '=' + q(textValue(customValue(p)))));
  function compile(r, {runner = false} = {}) {
    const checks = validate(r);
    if (checks.errors.length) return {...checks, artifacts: []};
    const artifacts = [], add = (id, label, filename, language, cwd, text) => artifacts.push({id,label,filename,language,cwd,text});
    let run = '';
    {
      let taskPath = r.path;
      for (const id of r.agents) {
        const a = agentById(id, r), model = !a.control;
        const prefix = credentialBindings(r, id).flatMap(b => b.targets.map(target => target+'='+env(b.source)));
        // A custom module:Class agent is imported from the directory the script runs in.
        if (a.custom) prefix.push('PYTHONPATH=$PWD');
        const adapter = adapterPath(r, id);
        if (adapter) prefix.push('PYTHONPATH='+path(adapter));
        // τ³: the simulated user and the evaluator are model calls too; they follow the agent's model and endpoint.
        if (benchmarkCallsModel(r)) prefix.push('TAU2_USER_MODEL='+q(tau2Model(r.userModel || r.model, r)),'TAU2_NL_ASSERTIONS_MODEL='+q(tau2Model(r.evalModel || r.userModel || r.model, r)),"TAU2_USER_REASONING_EFFORT=''");
        const exe = harborExecutable(r);
        // A runner (Run from the Review step, here or on a host) sets the
        // credentials and the three paths as environment; the script names
        // them and nothing else about where it runs.
        const parts = [...prefix, runner ? '"$NULLIUS_HARBOR" run' : exe.command+' run',
          (['local','generate'].includes(r.source)?'-p '+(runner && r.source === 'local' ? '"$NULLIUS_TASKS"' : path(taskPath)):'-d '+q(r.dataset)),
          '--env docker','--agent '+q(a.arg),'--yes'];   // the tasks' environment variables were reviewed here; the script runs unattended
        if (model) parts.push('--model '+q(id === 'tau3-llm-agent' ? litellmModel(r) : agentModel(r)));
        parts.push(...agentCustom(r, id).map(p=>'--ak '+q(p.key+'='+kwargValue(customValue(p)))));
        // The reference agent's tool timeout follows the shared MCP timeout, so both agents wait the same.
        if (id === 'tau3-llm-agent' && r.params.mcp_timeout_s !== '' && r.params.mcp_timeout_s != null) parts.push('--ak '+q('tool_timeout_sec='+Number(r.params.mcp_timeout_s)));
        parts.push(...(r.tasks === '' ? [] : ['--n-tasks '+r.tasks]),'--n-concurrent '+r.concurrency,'--max-retries '+r.retries,
          '--agent-timeout-multiplier '+r.timeoutMultiplier,'--job-name '+q(r.name+(r.agents.length>1?'-'+id:'')), '--jobs-dir '+(runner ? '"$NULLIUS_JOBS"' : path(r.output)));
        if (r.include) parts.push('--include-task-name '+q(r.include));
        parts.push(...commonFlags(r));
        for (const p of r.custom.filter(p=>['environment','verifier'].includes(p.scope))) parts.push((p.scope==='environment'?'--ek ':'--verifier-kwarg ')+q(p.key+'='+kwargValue(customValue(p))));
        parts.push(...cliExtras(r));
        run = join(parts);
        // A runner part runs from the run directory on whatever machine was chosen: the
        // adapter directory is checked there before Harbor starts, and a missing one is
        // said by name instead of an import error from inside Harbor.
        if (runner && adapter) run = 'nullius_adapter='+path(adapter)+'\n[ -d "$nullius_adapter" ] || { printf \'%s\\n\' "'+a.label.replace(/"/g, '')+' needs its adapter directory on this machine: $nullius_adapter is missing" >&2; exit 2; }\n'+run;
        add('run-'+id,a.label+' run',r.name+'-'+id+'.sh','shell','Working directory',run);
      }
    }
    return {...checks, artifacts};
  }
  // What Run on the Review step posts: the run commands with the paths as
  // environment names (the harbor on the target's PATH among them) and the
  // task folder they read. Says plainly what cannot run from here yet.
  function runnable(r) {
    const errors = [];
    if (r.source === 'generate') errors.push('Generated tasks: run the generator first, then point the recipe at its output folder.');
    if (r.agents.some(id => agentById(id, r)?.custom)) errors.push('A custom agent is imported from its configured module.');
    if (harborExecutable(r).kind === 'development') errors.push('A development Harbor cannot run here. Choose a release or repository on the Runner step.');
    else if (!harborPin(r)) errors.push('Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.');
    if (providerBlocker(r)) errors.push(providerBlocker(r));
    if (errors.length) return {errors, script: '', parts: [], tasks: '', model: '', jobs: []};
    const compiled = compile(r, {runner: true});
    if (compiled.errors.length) return {errors: compiled.errors.map(e => e.message), script: '', parts: [], tasks: '', model: '', jobs: []};
    // Controls first - nop, then oracle - so the dataset and the environment are proven before an agent under
    // evaluation spends anything; the runner stops between agents on a failure and asks.
    const rank = id => id === 'nop' ? 0 : id === 'oracle' ? 1 : agentById(id, r)?.control ? 2 : 3;
    const order = [...r.agents].sort((a, b) => rank(a) - rank(b));
    const runs = order.map(id => compiled.artifacts.find(a => a.id === 'run-' + id)).filter(Boolean);
    const script = ['#!/usr/bin/env bash', '# ' + r.name + ' — generated by Nullius; NULLIUS_HARBOR, NULLIUS_TASKS, NULLIUS_JOBS and the credentials arrive as environment.',
      'set -uo pipefail', 'status=0', ...runs.map(a => '# ' + a.label + '\n' + a.text + ' || status=1'), 'exit "$status"'].join('\n\n') + '\n';
    const parts = runs.map(a => ({id: a.id.slice(4), label: a.label, script: '#!/usr/bin/env bash\nset -uo pipefail\n' + a.text + '\n'}));
    const jobs = order.map(id => ({name: r.name + (r.agents.length > 1 ? '-' + id : ''), agent: agentById(id, r)?.arg || id, dataset: r.source === 'local' ? r.path.replace(/\/$/, '').split('/').pop() : r.dataset}));
    // No task limit means the whole dataset - 375 tasks and five hours on τ³ - so Run asks first, with the count when the catalogue knows it.
    // A task-name pattern narrows it to the tasks that match, a number only Harbor knows, so the question names the pattern instead.
    const confirm = r.tasks !== '' ? '' : taskFilter(r)
      ? 'Run the tasks of ' + datasetLabel(r) + ' ' + taskFilter(r) + ' (no limit)?'
      : 'Run every task of ' + datasetLabel(r) + (taskCount(r) ? ' (' + taskCount(r) + ' tasks, no limit)?' : ' (no limit)?');
    return {errors: [], script, parts, tasks: r.source === 'local' ? r.path : '', model: needsCredentials(r) ? agentModel(r, r.model || r.userModel) : '', jobs, confirm};
  }
  // The task-name patterns a run is narrowed by: the Include field, and an exclude written as a custom harness option.
  // Harbor matches them as globs; how many tasks match is known only once it resolves the dataset.
  function taskFilter(r) {
    const exclude = r.custom.filter(p => p.scope === 'harness' && ['exclude-task-name', 'x'].includes(p.key) && p.value).map(p => p.value);
    return [r.include ? 'matching ' + r.include : '', exclude.length ? 'not matching ' + exclude.join(', ') : ''].filter(Boolean).join(' and ');
  }
  function scopeText(r) {
    const filter = taskFilter(r), count = taskCount(r);
    if (r.tasks !== '') return filter ? 'up to ' + r.tasks + ' tasks ' + filter : r.tasks + ' tasks';
    if (filter) return 'tasks ' + filter + (count ? ' (of ' + count + ' in this dataset)' : '');
    return 'every task' + (count ? ' (' + count + ' in this dataset)' : '');
  }
  // What the Review step shows: one row per thing a person is asked to
  // confirm - the harness and benchmark, the dataset or source, the agents,
  // the model, the task scope and the execution location (and, on Harbor,
  // which runtime and executable) - each with the step that edits it. The
  // page renders these rows as they are; the route suite reads them here.
  function review(r) {
    const b = benchmarks(r.harness).find(b => b.key === r.benchmark) || {title: 'Custom benchmark', category: 'Custom tasks'};
    const label = id => agentById(id, r)?.label || id;
    const exe = harborExecutable(r);
    return [[STEP.benchmark, 'Benchmark', 'Harbor / ' + b.title],
      [STEP.runner, 'Runner', 'Harbor'],
      [exe.kind === 'development' ? STEP.limits : STEP.runner, 'Harbor', exe.kind === 'path' ? 'not chosen' : exe.label],
      [STEP.dataset, 'Dataset', r.source + ' · ' + (r.source === 'generate' ? r.path : r.source === 'local' ? r.path : r.dataset)],
      [STEP.agent, 'Agents', r.agents.map(label).join(', ')],
      [STEP.model, 'Model', needsCredentials(r) ? (r.benchmark === 'tau3-bench' && r.userModel && r.userModel !== r.model ? (r.model ? agentModel(r) + ' · customer ' : 'customer ') + bareModel(r.userModel) : agentModel(r, r.model || r.userModel)) : 'No model required'],
      [STEP.limits, 'Scope', scopeText(r) + ' · ' + r.concurrency + ' concurrent'],
      [STEP.where, 'Where', whereLabel(r)]];
  }
  const machineLabel = w => w === 'local' ? 'This machine' : (schema.hosts || []).find(x => x.entry === w)?.label || w;
  const whereLabel = r => whereList(r).map(machineLabel).join(' + ');
  function fromJSON(input) {
    const raw = typeof input === 'string' ? JSON.parse(input) : input;
    if (raw && raw.version === 1 && isStandard(raw)) return standardFromJSON(raw);
    if (raw && raw.version === 1 && typeof raw.harness === 'string' && raw.harness && raw.harness !== 'harbor') throw Error(RUNNER_GONE);
    if (!raw || raw.version !== 1 || raw.harness !== 'harbor') throw Error('Unsupported recipe format.');
    const r=create();
    for (const key of Object.keys(r)) {
      if (['params','custom','agents'].includes(key)) continue;
      if (typeof r[key] === 'string' && typeof raw[key] === 'string') r[key]=raw[key];
      if (typeof r[key] === 'number' && typeof raw[key] === 'number') r[key]=raw[key];
      if (['agentMinutes','attempts','setupTimeoutMultiplier','verifierTimeoutMultiplier','buildTimeoutMultiplier'].includes(key) && typeof raw[key] === 'number') r[key]=raw[key];   // '' by default, a number once set
      if (key === 'tasks' && typeof raw[key] === 'number') r[key]=raw[key];                             // '' by default (every task), a number once set
    }
    if (Array.isArray(raw.customAgents)) r.customAgents=raw.customAgents.slice(0,20).map(p=>String(p||''));
    if (raw.adapterPaths && typeof raw.adapterPaths === 'object') for (const [k,v] of Object.entries(raw.adapterPaths)) if (typeof v === 'string' && v) r.adapterPaths[k]=v;
    if (Array.isArray(raw.agents)) r.agents=raw.agents.filter(id=>typeof id==='string');
    // Normalize the single customAgent field into the customAgents list.
    if (r.agents.includes('custom')) { r.customAgents.push(typeof raw.customAgent==='string'?raw.customAgent:''); r.agents=r.agents.map(id=>id==='custom'?'custom-'+(r.customAgents.length-1):id); }
    if (raw.params && typeof raw.params === 'object') for(const key of Object.keys(r.params)) if(own(raw.params,key)) r.params[key]=raw.params[key];
    if (Array.isArray(raw.custom)) r.custom=raw.custom.slice(0,100).map(p=>({scope:String(p.scope||''),key:String(p.key||''),type:String(p.type||'string'),value:String(p.value??''),...(p.agent?{agent:String(p.agent)}:{})}));
    // Infer an omitted provider from the model prefix, then keep the bare model name.
    r.provider = PROVIDERS.includes(raw.provider) ? raw.provider : providerOf({}, raw.model || raw.userModel || '');
    keepEndpoint(r, raw);
    if(raw.modelFormat === 'provider-id')r.modelFormat='provider-id';
    else { const typed = {model: r.model, userModel: r.userModel, evalModel: r.evalModel};
      for (const key of ['model', 'userModel', 'evalModel']) r[key] = bareModel(typed[key]);
      if (typed.model) adoptModel(r, r.provider, typed.model); }
    if (typeof raw.title !== 'string' || !raw.title.trim()) r.title = typeof raw.name === 'string' ? raw.name : r.name;
    // Read pin strings as written; validate checks them. An omitted pin selects no release.
    const h = raw.harbor && typeof raw.harbor === 'object' ? raw.harbor : {}, str = v => typeof v === 'string' ? v : '';
    const extras = Array.isArray(h.extras) ? h.extras.filter(x => typeof x === 'string').slice(0, 40) : [];
    if (h.kind === 'repo') r.harbor = {kind: 'repo', repo: str(h.repo), revisionKind: ['branch', 'tag', 'commit'].includes(h.revisionKind) ? h.revisionKind : 'commit', revision: str(h.revision), commit: str(h.commit), extras};
    else if (h.kind === 'release') r.harbor = {kind: 'release', version: str(h.version), extras};
    return r;
  }
  // "New run like this" for a run that kept no recipe: the recipe its job
  // recorded - parse.launch_of over Harbor's config.json, Harbor's defaults
  // filled in - with the Harbor its lock names. Whatever the record holds that a recipe
  // cannot say comes back as a note naming it; nothing is dropped silently.
  const KWARG_TYPE = v => typeof v === 'number' ? 'number' : typeof v === 'boolean' ? 'boolean' : typeof v === 'string' ? 'string' : 'JSON';
  const NAMED_KWARGS = ['model', 'api_key', 'base_url', 'agent_timeout_sec', 'mcp_servers'];   // validate's "use the named field" list
  function fromJob(like) {
    const L = (like && like.launch) || {}, r = create(), lost = [], notes = [];
    const recorded = (L.agents || []).filter(a => a && a.name);
    if (!recorded.length && like.agent) recorded.push({name: like.agent, model: '', kwargs: {}, env: {}, secret: []});
    // Agents by Harbor's own name for them - the registry name or the import path - never the display label.
    const ran = [];
    r.agents = [];
    for (const a of recorded) {
      const known = agents().find(x => [x.arg, x.id].includes(a.name));
      let id = known && known.id;
      if (!id && a.name.includes(':')) { r.customAgents.push(a.name); id = 'custom-' + (r.customAgents.length - 1); }
      if (!id) { lost.push('agent ' + a.name + ' (not offered here)'); continue; }
      if (!r.agents.includes(id)) r.agents.push(id);
      ran.push([id, a]);
    }
    const m = (recorded.find(a => a.model) || {}).model;
    if (m) adoptModel(r, PROVIDER_PREFIX.test(m) ? '' : r.provider, m);   // the recorded prefix names the provider it ran on
    // The benchmark: the catalogue dataset the job read, else the one its agent exists for.
    const ds = L.dataset || {}, ref = ds.path || (ds.name ? ds.name + (ds.version ? '@' + ds.version : '') : '');
    const match = ref ? schema.datasets.find(d => [d.registry, d.local, d.key].includes(ref) || (ds.name && [d.registry, d.key].includes(ds.name))) : null;
    const offered = key => benchmarks().some(b => b.key === key);
    const declared = [...new Set(r.agents.flatMap(id => agentById(id, r)?.benchmarks || []))].filter(offered);
    setBenchmark(r, match?.benchmark || (declared.length === 1 ? declared[0] : '') || 'custom');
    if (ds.path) { r.source = 'local'; r.path = ds.path; }
    else if (ref) { r.source = match?.hub ? 'hub' : 'registry'; r.dataset = ref; }
    else if (L.tasks) lost.push('its ' + L.tasks + ' tasks named one by one');
    const names = L.task_names || [], excluded = L.exclude_task_names || [];
    if (names.length === 1) r.include = names[0];
    else if (names.length > 1) lost.push('task names ' + names.join(', ') + ' (a recipe takes one include pattern)');
    if (excluded.length === 1) r.custom.push({scope: 'harness', key: 'exclude-task-name', type: 'string', value: excluded[0]});
    else if (excluded.length > 1) lost.push('excluded task names ' + excluded.join(', ') + ' (a recipe takes one)');
    // Limits and timeouts, as Harbor ran them: a key the config left out held Harbor's default, and launch_of filled it in.
    if (Object.keys(L).length) {
      r.tasks = Number(L.n_tasks) > 0 ? Number(L.n_tasks) : '';
      if (Number.isInteger(L.n_concurrent_trials)) r.concurrency = L.n_concurrent_trials;
      if (Number.isInteger(L.max_retries)) r.retries = L.max_retries;
      r.attempts = Number(L.n_attempts) > 1 ? Number(L.n_attempts) : '';
      const all = Number(L.timeout_multiplier ?? 1);
      r.timeoutMultiplier = L.agent_timeout_multiplier ?? all;
      for (const [k, f] of [['agent_setup_timeout_multiplier', 'setupTimeoutMultiplier'], ['verifier_timeout_multiplier', 'verifierTimeoutMultiplier'], ['environment_build_timeout_multiplier', 'buildTimeoutMultiplier']]) if (L[k] != null) r[f] = L[k];
      if (all !== 1) r.custom.push({scope: 'harness', key: 'timeout-multiplier', type: 'number', value: String(all)});
    } else notes.push('This job recorded no config.json: only its agent is carried.');
    // Each agent's parameters: the reference agent's tool timeout, every other one as an extra agent parameter.
    const fields = new Set(parameters().map(p => p.key));
    for (const [id, a] of ran) {
      for (const k of a.secret || []) lost.push(k + ' (a credential; Settings hands the run its key)');
      for (const [k, v] of Object.entries(a.kwargs || {})) {
        if (id === 'tau3-llm-agent' && k === 'tool_timeout_sec') r.params.mcp_timeout_s = v;
        else if (fields.has(k) || NAMED_KWARGS.includes(k) || !/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(k)) lost.push(k + '=' + textValue(v) + ' for ' + a.name);
        else r.custom.push({scope: 'agent', key: k, type: KWARG_TYPE(v), value: typeof v === 'string' ? v : JSON.stringify(v), agent: id});
      }
      for (const [k, v] of Object.entries(a.env || {})) {
        if (r.benchmark === 'tau3-bench' && k === 'TAU2_USER_MODEL') { if (bareModel(v) !== r.model) r.userModel = bareModel(v); }
        else if (r.benchmark === 'tau3-bench' && k === 'TAU2_NL_ASSERTIONS_MODEL') { if (bareModel(v) !== bareModel(r.userModel || r.model)) r.evalModel = bareModel(v); }
        else if (!(r.benchmark === 'tau3-bench' && k === 'TAU2_USER_REASONING_EFFORT')) lost.push('agent environment ' + k);
      }
      if (adapterPath(r, id)) notes.push('A job does not record ' + (agentById(id, r)?.label || id) + '’s adapter directory: ' + adapterPath(r, id) + ' is this builder’s.');
      if (a.model && !agentById(id, r)?.control) {
        const sent = id === 'tau3-llm-agent' ? litellmModel(r) : agentModel(r);
        if (sent !== a.model) notes.push((agentById(id, r)?.label || id) + ' is sent the model as ' + sent + '; the run recorded ' + a.model + '.');
      }
    }
    const env = like.env || L.environment;
    if (env && env !== 'docker') lost.push('environment ' + env + ' (a recipe runs docker)');
    const hv = like.harbor && like.harbor.version;
    // A plain version is what PyPI publishes, so it is pinned as that release and said; a local version (+label) is a fork's build, chosen by its repository.
    const released = hv && !like.harbor.editable && /^\d+(?:\.\d+)*$/.test(hv);
    if (released) { r.harbor = {kind: 'release', version: hv, extras: []}; notes.push('Harbor ' + hv + ' ran this job: this recipe pins harbor ' + hv + ' from PyPI. If that was a fork\'s build, choose its repository on the Runner step.'); }
    else if (hv) notes.push('Harbor ' + hv + ' ran this job, a build PyPI does not publish: choose its repository and commit on the Runner step.');
    if (hv && like.harbor.editable) notes.push('That Harbor was an editable checkout, not a release.');
    lost.push(...(L.unexpressed || []));
    return {recipe: r, notes: ['Opened from job “' + like.job + '”, as it recorded itself. Run gives it a new name.',
      ...(lost.length ? ['Not carried: ' + lost.join('; ') + '.'] : []), ...notes]};
  }
  function prefill(query) {
    // ?standard=<benchmark>[&other=<agent or capability>]: the standard's recipe for that pair (the Agents and Capabilities pages link here).
    if(query.standard)return createStandard(query.standard,query.other||'');
    // nothing asked for: the Nullius runner, chosen first; a Harbor recipe comes from its own card
    if(!query.agent&&!query.dataset&&!query.model&&!query.n_tasks&&standard().benchmarks.length)return createStandard();
    const r=create(), known=schema.agents.find(a=>[a.id,a.arg,a.label].includes(query.agent))||(query.agent&&schema.agents.find(a=>String(a.arg||'').includes(':')&&String(a.arg).split(':').pop()===query.agent));
    if(query.model)adoptModel(r,PROVIDER_PREFIX.test(query.model)?'':r.provider,query.model);
    if(known)r.agents=[known.id];
    if(query.n_tasks&&Number(query.n_tasks)>0)r.tasks=Number(query.n_tasks);
    if(query.dataset){
      const d=schema.datasets.find(d=>[d.registry,d.local,d.key].includes(query.dataset));
      // a dataset Harbor does not serve (no hub, no registry), of a benchmark the standard runs, opens the standard's recipe
      const b=d&&schema.benchmarks.find(x=>x.key===d.benchmark);
      if(b&&!(d.hub||d.registry)&&standard().benchmarks.some(x=>x.key===b.key))return createStandard(b.key);
      setBenchmark(r,d?.benchmark||'custom');
      if(query.dataset.startsWith('/')||query.dataset.startsWith('.')||query.dataset.startsWith('~')){r.source='local';r.path=query.dataset;}
      // a catalogue dataset named by its key runs under the name Harbor knows it by (its registry), not the key
      else{r.source=d?.hub?'hub':'registry';r.dataset=d&&d.key===query.dataset&&d.registry?d.registry:query.dataset;}
    }
    return r;
  }
  // A link that already names the benchmark (Build a run on its page) opens past it: on the runner when the
  // benchmark runs on both, so the choice is seen, else on its dataset. Anything else starts at the benchmark.
  const openingStep = (query, r) => !(query && (query.standard || query.dataset)) ? STEP.benchmark
    : r && runnersOf(benchKey(r)).length > 1 ? STEP.runner : STEP.dataset;
  // The runners a benchmark has here, Nullius first: its own Harbor folder ('custom') is Harbor's alone.
  const runnersOf = key => [...(standardBench({benchmark: key}) ? ['standard'] : []),
    ...(key === 'custom' || benchmarks().some(b => b.key === key) ? ['harbor'] : [])];
  // The runner a benchmark picked opens: the recipe's own when it has that benchmark, else Nullius before Harbor.
  const runnerFor = (r, key) => { const has = runnersOf(key); return has.includes(r.harness) ? r.harness : has[0] || r.harness; };
  // The benchmark a recipe names, by its key whichever runner it is for (a version of the standard's is its benchmark).
  const benchKey = r => isStandard(r) ? (standardBench(r) || {}).key || r.benchmark : r.benchmark;
  // The page keeps what is on screen as a draft in the browser, so leaving for
  // another page and coming back finds the recipe as it was left. Pure halves:
  // the page owns the storage. A draft does not come back when the page was
  // opened for something else - a prefill from a run, or a run's own recipe.
  const OPENED_FOR = ['run','like','dataset','model','agent','n_tasks','standard','other'];
  function packDraft(state) {
    return JSON.stringify({version: 1, at: new Date().toISOString(), recipe: state.recipe, step: state.step, steps: STEPS.length, first: 'benchmark', tab: state.tab});
  }
  function unpackDraft(text, query) {
    if (!text || Object.keys(query || {}).some(k => OPENED_FOR.includes(k))) return null;
    try {
      const d = JSON.parse(text);
      if (!d || d.version !== 1) return null;
      // Translate saved step indexes using the draft's recorded step count.
      const had = Number.isInteger(d.steps) ? d.steps : STEPS.length - 2;
      let step = Number.isInteger(d.step) ? d.step + (had < STEPS.length - 1 ? 1 : 0) : 0;
      if (Number.isInteger(d.step) && had < STEPS.length && step >= STEP.setup) step += 1;
      if (d.first !== 'benchmark' && (step === 0 || step === 1)) step = 1 - step;   // a draft from when the runner came first
      return {recipe: fromJSON(d.recipe), step: Math.min(step, STEP.review), tab: typeof d.tab === 'string' ? d.tab : 'select',
        at: String(d.at || '')};
    } catch (e) { return e && e.message === RUNNER_GONE ? {recipe: null, gone: RUNNER_GONE} : null; }
  }
  return {STEPS,STEP,runners,RUNNER_GONE,isStandard,standard,standardBench,standardOthers,standardKeyless,paperModels,paperListed,paperOffered,leaderboard,setModelChoice,servedRoles,servedList,setServed,setModelOf,servedModelOf,stepShown,chosenOf,datasetsOf,whereList,machineLabel,createStandard,setStandardBenchmark,openingStep,runnersOf,runnerFor,benchKey,whereLabel,create,benchmarks,routeBlocker,dataset,sources,setBenchmark,parameters,scopes,validate,compile,runnable,fromJSON,fromJob,prefill,review,copy,packDraft,unpackDraft,adapterPath,taskCount,PROVIDERS,PROVIDER_LABEL,PROVIDER_NEEDS,available,labelOf,needsOf,choiceOf,setChoice,providersOf,recipeProvider,providerBlocker,providerOf,bareModel,settleModel,agentModel,quote:q,path,defaults,needsCredentials,credentialBindings,declaredTimeouts,effectiveTimeouts,setPhaseMinutes,PHASES,harborExecutable,harborPin,harborProblem,harborKey,harborRepoUrl,harborLabel,repoLabel,agents,mergeAgents,setScanned,COMMON,agentById};
});
