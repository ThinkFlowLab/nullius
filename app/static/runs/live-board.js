/* The live board: the job page's Live view, drawn from the /job/<id>/live snapshot.
 *
 * Two halves, deliberately separable. `render(snapshot, opts)` is a pure
 * function from the snapshot to HTML strings — no DOM, no fetch — so the route
 * suite can execute it under Node against the hand-made fixtures and assert on
 * what a user would read. `mount(document)` is the only part that touches the
 * page: it polls every second while the job runs, keeps the reader's filter,
 * selected trial and tab, and stops when the feed records the end.
 *
 * One board, top to bottom: a header line, one stats strip, the outcome bar
 * with its filter chips (and, for more than one agent, the lanes and a winner
 * per task), every trial as a tile, one table with a row per trial in flight and, below them, each that ended,
 * and a drawer for one trial, where its phases are drawn on its own clock.
 *
 * Rules the fold already enforces are respected here rather than re-derived:
 * cost, tokens per second and every counter are shown only once reported,
 * never as 0; a column appears only when some trial reported it, and a single
 * missing cell reads "—".
 * The solve rates are the feed's outcomes so far, and say so: Results reads
 * the job's own files.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.NulliusLiveBoard = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const PHASES = ['env_setup', 'agent_setup', 'agent_run', 'verify'];
  const PHASE_LABEL = {env_setup: 'env setup', agent_setup: 'agent setup', agent_run: 'agent run', verify: 'verify'};
  const OUTCOMES = ['resolved', 'unresolved', 'scored', 'agent_error', 'infra_error', 'timeout', 'budget', 'cancelled'];
  const OUTCOME_LABEL = {resolved: 'Resolved', unresolved: 'Unresolved', scored: 'Scored', agent_error: 'Agent error', infra_error: 'Infra error', cancelled: 'Cancelled', timeout: 'Timeout', budget: 'Spending cap'};
  const STATE_LABEL = {preparing: 'Preparing', starting: 'Starting', running: 'Running', finished: 'Finished', unknown: 'Waiting for the feed'};
  const FLIGHT_SHOWN = 12;      // past this many in flight, the nearest their limit come first and the rest wait behind a button
  const MANY_TILES = 60;        // past this many trials, the tiles are drawn smaller

  function clock(sec) {
    if (sec == null || !isFinite(sec)) return '—';
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(s).padStart(2, '0');
  }
  function count(n) {
    if (n == null) return '—';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }
  const hms = iso => iso ? String(iso).slice(11, 19) : '—';
  const secs = iso => Date.parse(iso) / 1000;
  let NOUN = '';                // what the benchmark calls one item ("task", "question"), for a name that is a bare number
  const shortTask = t => { const n = String(t.task || t.trial || '').replace(/^.*?__/, ''); return NOUN && /^\d+$/.test(n) ? NOUN[0].toUpperCase() + NOUN.slice(1) + ' ' + n : n; };
  const redacted = s => esc(s).replace(/\[REDACTED\]/g, '<span class="lb-red">[REDACTED]</span>');
  function tokensOf(t) {
    if (t.n_input_tokens == null && t.n_output_tokens == null) return null;
    return (t.n_input_tokens || 0) + (t.n_output_tokens || 0);
  }
  const pressure = t => (t.timeout_sec && t.elapsed_sec != null ? t.elapsed_sec / t.timeout_sec : 0);
  function stateOf(s) {
    const C = s.counts || {};
    // Infer an omitted state from running and the counts.
    return s.state || (!s.running ? 'finished' : (C.running === 0 && C.completed === 0 && C.total > 0) ? 'starting' : 'running');
  }
  function outcomeCounts(trials) {
    const oc = {}; OUTCOMES.forEach(k => { oc[k] = 0; });
    trials.forEach(t => { if (t.state === 'done' && t.outcome) oc[t.outcome] = (oc[t.outcome] || 0) + 1; });
    return oc;
  }
  function trialHref(t, opts) {
    return opts && opts.trialBase ? `${opts.trialBase.replace(/\/job\//, '/trial/')}/${encodeURIComponent(t.trial)}` : '';
  }

  function renderHead(s, state) {
    const start = s.started_at || s.launched_at;
    const when = start ? `${s.started_at ? 'started' : 'launched'} ${hms(start)}` : '';
    const elapsed = s.elapsed_sec != null ? `${clock(s.elapsed_sec)} ${state === 'finished' ? 'in all' : 'elapsed'}` : '';
    return [when, elapsed].filter(Boolean).join(', ');
  }
  function renderMeta(s) {
    const T = s.totals || {}, agents = s.agents || [];
    const bits = [];
    if (agents.length) bits.push(`<span>${agents.length > 1 ? 'agents' : 'agent'} <b>${esc(agents.join(', '))}</b></span>`);
    if (s.n_concurrent) bits.push(`<span><b>${esc(s.n_concurrent)}</b> at a time</span>`);
    const tok = tokensOf(T);
    if (tok != null) {
      const parts = [T.n_cache_tokens != null ? `${count(T.n_cache_tokens)} cached` : '', T.n_output_tokens != null ? `${count(T.n_output_tokens)} out` : ''].filter(Boolean);
      bits.push(`<span><b>${count(tok)}</b> tokens${parts.length ? ' (' + parts.join(', ') + ')' : ''}</span>`);
    }
    // measured, or left out: a rate the window could not measure, and a cost nobody reported, are absent, never 0
    if (s.tokens_per_sec != null) bits.push(`<span><b>${count(Math.round(s.tokens_per_sec))}</b> tokens / s, last 30 s</span>`);
    // a run with a spending cap (job.start's budget) says it beside what was spent, or alone while nothing was
    // the cap as it was given: cents, and up to two digits more when it has them (0.395), never rounded to a cent it is not
    const capped = v => { const [whole, part = ''] = Number(v).toFixed(4).replace(/0+$/, '').split('.'); return whole + '.' + part.padEnd(2, '0'); };
    const cap = s.budget && Number(s.budget.max_usd) > 0 ? ` of the <b>$${capped(s.budget.max_usd)}</b> cap` : '';
    // under a cent, four decimals as Results shows it: beside a cap, $0.00 spent would read as nothing spent
    if (T.cost_usd != null) bits.push(`<span><b>$${Number(T.cost_usd).toFixed(cap && T.cost_usd < 0.01 ? 4 : 2)}</b> spent${cap}</span>`);
    else if (cap) bits.push(`<span>spending cap <b>$${capped(s.budget.max_usd)}</b></span>`);
    if (s.wall_sec) bits.push(`<span>wall <b>${clock(s.wall_sec)}</b> per trial</span>`);
    return bits.join('');
  }
  function stat(k, v, cls, na) {
    return `<div class="lb-stat${cls ? ' ' + cls : ''}"><span class="k">${k}</span><span class="v${na ? ' na' : ''}">${v}</span></div>`;
  }
  const pct = x => `${Math.round(100 * x)}<small>%</small>`;
  function renderStats(s, state, oc) {
    const C = s.counts || {};
    const queued = C.total != null ? Math.max(0, C.total - C.completed - C.running) : null;
    const rate = s.elapsed_sec > 60 && C.completed ? C.completed / (s.elapsed_sec / 60) : null;
    const ran = C.completed - (oc.infra_error || 0) - (oc.budget || 0);
    const when = state === 'finished' ? 'on the feed' : 'so far';   // the feed's outcomes, not the classifier's: Results has those
    // A benchmark that gives a score and no pass or fail has no solve rate: the mean score, over every part scored, stands there.
    const scored = (s.trials || []).filter(t => t.state === 'done' && t.outcome === 'scored' && t.score != null);
    const weight = scored.reduce((n, t) => n + (t.parts || 1), 0);
    const rates = scored.length && !oc.resolved && !oc.unresolved
      ? stat(`Mean score <small>${when}</small>`, (scored.reduce((x, t) => x + t.score * (t.parts || 1), 0) / weight).toFixed(2), 'rate')
      : stat(`Raw solve rate <small>${when}</small>`, C.completed ? pct(oc.resolved / C.completed) : '—', 'rate', !C.completed)
        + stat(`Adjusted <small>${when}</small>`, ran > 0 ? pct(oc.resolved / ran) : '—', 'rate', !(ran > 0));
    return stat('Done', `${C.completed}<small>/${C.total == null ? '?' : C.total}</small>`)
      + stat('Running', String(C.running))
      + stat('Queued', queued == null ? '—' : String(queued), '', queued == null)
      + stat('Rate', rate ? `${rate.toFixed(1)}<small>/min</small>` : '—', '', !rate)
      + rates;
  }
  function renderMix(s, oc, filter) {
    const total = (s.counts && s.counts.total) || (s.trials || []).length || 1;
    const graded = oc.scored && !oc.resolved && !oc.unresolved;       // scores alone: no empty Resolved and Unresolved beside them
    const shown = OUTCOMES.filter(k => oc[k] || (!graded && (k === 'resolved' || k === 'unresolved')));
    const bar = shown.map(k => `<span class="lbo-${k}" style="flex:0 0 ${(100 * oc[k] / total).toFixed(2)}%${filter && filter !== k ? ';opacity:.3' : ''}"></span>`).join('');
    const chips = shown.map(k => `<li><button type="button" class="lbo-${k}" data-filter="${k}" aria-pressed="${filter === k}"${oc[k] ? '' : ' disabled'}><i></i>${OUTCOME_LABEL[k]} <b>${oc[k]}</b></button></li>`).join('')
      + (filter ? '<li><button type="button" class="lb-clear" data-filter="">Show all</button></li>' : '');
    return {bar, chips};
  }
  // Several agents in one job: the fold groups the trials into one lane per
  // agent and names a winner per task; the board only lays them out. Nothing
  // unreported reads as 0.
  function renderLanes(lanes, contested) {
    if (!lanes || lanes.length < 2) return '';
    return lanes.map(l => {
      const C = l.counts, T = l.totals || {};
      const facts = [`<b>${C.completed} / ${C.total}</b> completed`,
        C.running ? `<b>${C.running}</b> running` : '',
        `<b>${l.mean_reward == null ? '—' : l.mean_reward.toFixed(2)}</b> mean reward`,
        T.cost_usd != null ? `<b>$${T.cost_usd.toFixed(2)}</b> spent` : '',
        contested ? `<b>${l.wins}</b> won` : ''].filter(Boolean).join(' · ');
      return `<span class="lb-lane" data-lane="${esc(l.agent == null ? '' : l.agent)}"><span class="agent">${esc(l.agent == null ? 'unnamed agent' : l.agent)}</span> ${facts}</span>`;
    }).join('');
  }
  function renderWinners(winners) {
    if (!winners || !winners.length) return '';
    const agents = [];
    winners.forEach(w => w.agents.forEach(a => { if (!agents.includes(a)) agents.push(a); }));
    const rows = winners.map(w => {
      const cells = agents.map(a => {
        if (!(a in w.rewards)) return '<td class="na">did not run</td>';
        const r = w.rewards[a];
        return `<td class="num">${r == null ? '<span class="na">—</span>' : esc(Number(r).toFixed(2))}</td>`;
      }).join('');
      const verdict = w.winner != null ? `<b class="lb-win">${esc(w.winner)}</b>` : (w.decided ? '<span class="na">tie</span>' : '<span class="lb-open">undecided</span>');
      return `<tr data-task="${esc(w.task)}"><td class="task" title="${esc(w.task)}">${esc(w.task)}</td>${cells}<td>${verdict}</td></tr>`;
    }).join('');
    return `<div class="lb-winners" data-winners="${winners.length}"><table><thead><tr><th>Winner per task</th>${agents.map(a => `<th class="num">${esc(a)}</th>`).join('')}<th>winner</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="lb-note">Each agent's best reward on the task so far; a winner is named once every trial of the task ended and one best is strictly higher. A tie names nobody.</p></div>`;
  }
  function renderTiles(trials, opts, agents) {
    return trials.map(t => {
      const st = t.state === 'done' ? 'done' : t.state === 'running' ? 'running' : 'queued';
      const cls = st === 'done' ? ` lbo-${esc(t.outcome || 'cancelled')}` : st === 'running' ? ` lbp-${esc(t.phase)}` : '';
      const w = st === 'running' && t.timeout_sec ? ` style="--p:${Math.min(1, pressure(t)).toFixed(3)}"` : '';
      const dim = opts.filter && t.outcome !== opts.filter ? ' dim' : '';
      const label = `${shortTask(t)}${agents.length > 1 ? ' · ' + (t.agent || '') : ''}: ${st === 'done' ? (OUTCOME_LABEL[t.outcome] || 'ended') : st === 'running' ? PHASE_LABEL[t.phase] || t.phase : 'queued'}`;
      return `<button type="button" class="lb-tile ${st}${cls}${dim}" data-trial="${esc(t.trial)}"${w} aria-current="${opts.selected === t.trial}" aria-label="${esc(label)}" title="${esc(label)}"><i></i></button>`;
    }).join('');
  }
  function renderFlight(s, state, opts, oc, agents) {
    const trials = s.trials || [], C = s.counts || {};
    const running = trials.filter(t => t.state === 'running');
    if (state === 'preparing' || (state === 'starting' && !running.length)) {
      const st = s.preparing ? s.preparing.stage : null;
      const head = state === 'preparing'
        ? `Preparing${st ? ' <b>' + esc(st) + '</b>' : ''}${s.preparing ? ' for ' + clock(s.preparing.elapsed_sec) : ''}. Nothing is scheduled yet.`
        : `Starting: ${C.total == null ? 'trials' : C.total + ' trial' + (C.total === 1 ? '' : 's')} queued, none has entered a phase yet.`;
      return `<p class="lb-quiet">${head}</p>`;
    }
    if (state === 'finished') {
      const how = s.ended_by ? s.ended_by : 'finished';
      return `<div class="lb-settle"><div><p><b>${esc(how[0].toUpperCase() + how.slice(1))} at ${hms(s.finished_at)}</b>${s.elapsed_sec != null ? ' after ' + clock(s.elapsed_sec) : ''}. The feed saw ${C.completed} of ${C.total == null ? '?' : C.total} trials end, ${oc.scored && !oc.resolved && !oc.unresolved ? oc.scored + ' scored' : oc.resolved + ' resolved'}.</p>
        <small>The feed's outcomes are provisional. Results reads the job's own files and the judge, and it is where the score lives.</small></div>
        <a class="btn sm" href="?view=results" data-open-results>Open Results</a></div>`;
    }
    const ended = (t) => ((s.timeline || {})[t.trial] || {}).ended || '';
    const done = trials.filter(t => t.state === 'done').sort((a, b) => ended(a).localeCompare(ended(b)));
    if (!running.length && !done.length) return '<p class="lb-quiet">Nothing is running at this poll.</p>';
    const busy = running.length > FLIGHT_SHOWN, many = done.length > FLIGHT_SHOWN;
    const order = busy ? running.slice().sort((a, b) => pressure(b) - pressure(a)) : running;
    const rows = opts.showAll ? order : order.slice(0, FLIGHT_SHOWN);
    const doneRows = opts.showAll ? done : done.slice(-FLIGHT_SHOWN);
    // a column appears once some trial reported it; a single missing cell is "—"
    const has = {steps: trials.some(t => t.step != null), tokens: trials.some(t => tokensOf(t) != null), line: trials.some(t => t.activity || t.last_line)};
    const body = rows.map(t => {
      const k = PHASES.indexOf(t.phase), p = pressure(t), tok = tokensOf(t), line = t.activity || t.last_line;
      const dim = opts.filter && t.outcome !== opts.filter ? ' dim' : '';
      return `<tr class="lb-row lbp-${esc(t.phase)}${dim}" data-trial="${esc(t.trial)}" tabindex="0" aria-current="${opts.selected === t.trial}">
        <td><span class="task" title="${esc(t.task || t.trial)}">${esc(shortTask(t))}</span>${agents.length > 1 ? `<small>${esc(t.agent || '')}</small>` : ''}${t.attempt > 1 ? `<small class="lb-retry" title="Harbor ran it again after: ${esc((t.retried || []).join(', '))}">attempt ${t.attempt}, after ${esc((t.retried || []).slice(-1)[0] || 'an error').replace('_', ' ')}</small>` : ''}</td>
        <td><span class="lb-phase"><span class="lb-ticks">${PHASES.map((_, j) => `<s class="${j < k ? 'done' : j === k ? 'now' : ''}"></s>`).join('')}</span>${esc(PHASE_LABEL[t.phase] || t.phase || '—')}</span></td>
        <td><span class="lb-limit"><span class="lb-bar${p > .6 ? ' near' : ''}"><i style="width:${(100 * Math.min(1, p)).toFixed(1)}%"></i></span><span><b>${clock(t.elapsed_sec)}</b> of ${t.timeout_sec ? clock(t.timeout_sec) : '—'}</span></span></td>
        ${has.steps ? `<td class="num${t.step == null ? ' na' : ''}">${t.step == null ? '—' : esc(t.step)}</td>` : ''}
        ${has.tokens ? `<td class="num${tok == null ? ' na' : ''}">${count(tok)}</td>` : ''}
        ${has.line ? `<td><span class="lb-line${line ? '' : ' na'}" title="${esc(line || '')}">${line ? redacted(line) : '—'}</span></td>` : ''}<td class="lb-go"></td></tr>`;
    }).join('') + doneRows.map(t => {
      // an ended trial stays, below the running ones: its outcome, its time in all, its tokens, and its page
      const tok = tokensOf(t), line = t.activity || t.last_line, href = trialHref(t, opts);
      const dim = opts.filter && t.outcome !== opts.filter ? ' dim' : '';
      return `<tr class="lb-row lb-ended${dim}" data-trial="${esc(t.trial)}" tabindex="0" aria-current="${opts.selected === t.trial}">
        <td><span class="task" title="${esc(t.task || t.trial)}">${esc(shortTask(t))}</span>${agents.length > 1 ? `<small>${esc(t.agent || '')}</small>` : ''}</td>
        <td><span class="lb-outcome lbo-${esc(t.outcome || 'cancelled')}"><i></i>${esc(OUTCOME_LABEL[t.outcome] || 'Ended')}</span></td>
        <td><span class="lb-took">${t.total_sec != null ? `<b>${clock(t.total_sec)}</b> in all` : '—'}</span></td>
        ${has.steps ? `<td class="num${t.step == null ? ' na' : ''}">${t.step == null ? '—' : esc(t.step)}</td>` : ''}
        ${has.tokens ? `<td class="num${tok == null ? ' na' : ''}">${count(tok)}</td>` : ''}
        ${has.line ? `<td><span class="lb-line${line ? '' : ' na'}" title="${esc(line || '')}">${line ? redacted(line) : '—'}</span></td>` : ''}<td class="lb-go">${href ? `<a class="lb-open-row" href="${esc(href)}">Open →</a>` : ''}</td></tr>`;
    }).join('');
    const said = [busy ? (opts.showAll ? `All ${running.length} in flight` : `${rows.length} of ${running.length} in flight`) + ', nearest their limit first' : '',
      many ? (opts.showAll ? `all ${done.length} ended` : `the last ${doneRows.length} of ${done.length} ended`) : ''].filter(Boolean).join(' · ');
    const more = busy || many ? `<div class="lb-more"><span>${said}</span><button type="button" class="btn sm ghost" data-show-all>${opts.showAll ? 'Show fewer' : 'Show all'}</button></div>` : '';
    return `<div class="lb-scroll"><table class="lb-table"><thead><tr><th>${busy ? 'Trial, the running nearest their limit first' : 'Trial'}</th><th>Phase, or how it ended</th><th>Time in phase, of its limit</th>${has.steps ? '<th class="num">Steps</th>' : ''}${has.tokens ? '<th class="num">Tokens</th>' : ''}${has.line ? '<th>Last line, redacted</th>' : ''}<th class="lb-go" aria-label="Trial page"></th></tr></thead><tbody>${body}</tbody></table></div>${more}`;
  }
  // One trial: its phases on its own clock, from the feed's own phase events
  // (the route carries them as `timeline`), the time left in the phase it is in,
  // the counters it reported, and its latest events.
  function renderDrawer(s, t, opts) {
    const now = secs(s.now);
    const tl = (s.timeline || {})[t.trial] || {starts: {}, ended: null};
    const starts = tl.starts || {}, keys = PHASES.filter(p => starts[p]);
    const t0 = keys.length ? secs(starts[keys[0]]) : null;
    const end = tl.ended ? secs(tl.ended) : now;
    const cur = t.state === 'running' ? t.phase : null;
    const limEnd = cur && t.timeout_sec && starts[cur] ? secs(starts[cur]) + t.timeout_sec : null;
    const span = t0 == null ? 1 : (Math.max(end, limEnd || 0) - t0) || 1;
    const X = v => Math.max(0, Math.min(100, 100 * (v - t0) / span));
    let segs = '';
    keys.forEach((p, k) => {
      const a = secs(starts[p]), b = k + 1 < keys.length ? secs(starts[keys[k + 1]]) : end;
      const isNow = p === cur && k === keys.length - 1;
      segs += `<span class="lb-seg lbp-${p}${isNow ? ' now' : ''}${isNow && pressure(t) > .6 ? ' near' : ''}" style="left:${X(a).toFixed(2)}%;width:${Math.max(.6, X(b) - X(a)).toFixed(2)}%"></span>`;
    });
    if (limEnd) segs += `<span class="lb-left lbp-${cur}" style="left:${X(now).toFixed(2)}%;width:${Math.max(0, X(limEnd) - X(now)).toFixed(2)}%" title="time left in ${esc(PHASE_LABEL[cur])}"></span>`;
    if (tl.ended && t.outcome) segs += `<span class="lb-cap lbo-${esc(t.outcome)}" style="left:${X(end).toFixed(2)}%"></span>`;
    const dur = p => {
      if (t.durations && t.durations[p] != null) return t.durations[p];
      const k = keys.indexOf(p); if (k < 0) return null;
      return (k + 1 < keys.length ? secs(starts[keys[k + 1]]) : end) - secs(starts[p]);
    };
    const phases = PHASES.map(p => { const d = dur(p); return `<span class="lbp-${p}${d == null ? ' later' : ''}"><i></i>${PHASE_LABEL[p]}<b>${d == null ? '—' : clock(d)}${p === cur ? ' so far' : ''}</b></span>`; }).join('');
    const facts = [['Steps', t.step, v => esc(v)], ['Input', t.n_input_tokens, count], ['Cached', t.n_cache_tokens, count], ['Output', t.n_output_tokens, count],
      ['Cost', t.cost_usd, v => '$' + Number(v).toFixed(2)], ['Reward', t.reward, v => Number(v).toFixed(2)], ['Score', t.score, v => Number(v).toFixed(2)]].filter(f => f[1] != null);
    const events = (s.tail || []).filter(e => e.trial === t.trial).slice(-6);
    const state = t.state === 'done' ? `${OUTCOME_LABEL[t.outcome] || 'Ended'}${t.total_sec != null ? ', ' + clock(t.total_sec) + ' in all' : ''}`
      : t.state === 'running' ? `${PHASE_LABEL[t.phase] || t.phase}, ${clock(t.elapsed_sec)} of ${t.timeout_sec ? clock(t.timeout_sec) : 'no limit'}`
      : `Queued${t.position != null ? ', position ' + (t.position + 1) : ''}`;
    const href = trialHref(t, opts);
    return `<header><h3>${esc(shortTask(t))}<small>${esc(state)}${t.agent ? ' · ' + esc(t.agent) : ''}</small></h3><button class="lb-x" type="button" data-close aria-label="Close trial detail">×</button></header>
      <div class="lb-river"><div class="lb-rl"><span>${t0 == null ? 'not started' : hms(starts[keys[0]])}</span><span>${tl.ended ? 'ended ' + hms(tl.ended) : limEnd ? 'limit ' + clock(limEnd - t0) + ' in' : ''}</span></div>
        <div class="lb-track">${segs}</div><div class="lb-pl">${phases}</div></div>
      ${t.state !== 'running' && (t.activity || t.last_line) ? `<p class="lb-last"><span>Last line, redacted</span>${redacted(t.activity || t.last_line)}</p>` : ''}
      ${facts.length ? `<dl class="lb-facts">${facts.map(([k, v, f]) => `<div><dt>${k}</dt><dd>${f(v)}</dd></div>`).join('')}</dl>` : ''}
      <div class="lb-evl"><span>events.jsonl, latest</span><span>${events.length} in the tail</span></div>
      <div class="lb-ev">${events.length ? events.map(e => `<div><span>${hms(e.ts)}</span><span class="e">${esc(e.event)}</span><span>${redacted(e.phase || e.outcome || e.activity || e.line || '')}</span></div>`).join('') : '<div><span>—</span></div>'}</div>
      ${href ? `<a class="lb-open-trial" href="${esc(href)}">Open the trial →</a>` : ''}`;
  }

  function render(snapshot, opts) {
    opts = opts || {};
    NOUN = opts.noun || '';
    const s = snapshot, trials = s.trials || [], state = stateOf(s), agents = s.agents || [];
    const oc = outcomeCounts(trials), mix = renderMix(s, oc, opts.filter);
    const lanes = s.lanes || [], winners = s.winners || [];
    const picked = opts.selected ? trials.find(t => t.trial === opts.selected) : null;
    const C = s.counts || {};
    return {
      state,
      stateLabel: state === 'finished' && s.ended_by ? s.ended_by[0].toUpperCase() + s.ended_by.slice(1) : STATE_LABEL[state] || state,
      finished: state === 'finished',
      // The footer says only what the board above does not: the stage a job is preparing, or that nothing has started.
      status: state === 'preparing' ? `preparing${s.preparing && s.preparing.stage ? ' — ' + s.preparing.stage : ''}${s.preparing ? ' · ' + clock(s.preparing.elapsed_sec) + ' in this stage' : ''}`
        : state === 'starting' ? `starting — ${C.total == null ? 'trials' : C.total + ' trial' + (C.total === 1 ? '' : 's')} queued, none has entered a phase yet`
        : '',
      head: renderHead(s, state),
      meta: renderMeta(s),
      stats: renderStats(s, state, oc),
      mixbar: mix.bar,
      legend: mix.chips,
      lanes: lanes.length > 1 ? lanes.length : 0,
      lanesLine: renderLanes(lanes, winners.length > 0),
      winners: renderWinners(winners),
      tilesLabel: `All ${trials.length} trial${trials.length === 1 ? '' : 's'}`,
      manyTiles: trials.length > MANY_TILES,
      tiles: renderTiles(trials, opts, agents),
      flight: renderFlight(s, state, opts, oc, agents),
      drawer: picked ? renderDrawer(s, picked, opts) : '',
    };
  }

  function mount(doc) {
    const board = doc.getElementById('live-board');
    if (!board) return;
    const $ = sel => board.querySelector(sel);
    const endpoint = board.dataset.endpoint, trialBase = board.dataset.trialBase;
    const opts = {trialBase, noun: board.dataset.noun || '', filter: null, selected: null, showAll: false};
    let snap = null, timer = null, misses = 0;
    // opened while the run was going: the Results rendered with the page are older than its end
    const startedLive = board.dataset.state !== 'finished';
    function paint() {
      if (!snap) return;
      const out = render(snap, opts);
      board.dataset.state = out.state;
      $('[data-lb-state]').textContent = out.stateLabel;
      $('[data-lb-head]').textContent = out.head;
      $('[data-lb-meta]').innerHTML = out.meta;
      $('[data-lb-stats]').innerHTML = out.stats;
      $('[data-lb-mixbar]').innerHTML = out.mixbar;
      $('[data-lb-legend]').innerHTML = out.legend;
      const lanes = $('[data-lb-lanes]'); lanes.innerHTML = out.lanesLine + out.winners; lanes.hidden = !out.lanes && !out.winners;
      $('[data-lb-tiles-label]').textContent = out.tilesLabel;
      const tiles = $('[data-lb-tiles]'); tiles.innerHTML = out.tiles; tiles.classList.toggle('many', out.manyTiles);
      $('[data-lb-flight]').innerHTML = out.flight;
      const drawer = $('[data-lb-drawer]'); drawer.innerHTML = out.drawer; drawer.hidden = !out.drawer;
      board.classList.toggle('open', !!out.drawer);
      const said = misses ? `feed unavailable (${misses}) — showing the last snapshot` : out.status;
      $('[data-lb-status]').textContent = said; $('[data-lb-foot]').hidden = !said;
      // Trials that ended since the page loaded are not in its Results yet: the badge counts them, and the tab reads them again
      const badge = doc.getElementById('results-badge'), shown = Number(badge?.dataset.trials || 0), done = (snap.counts || {}).completed || 0;
      if (badge && !out.finished && done > shown) { badge.textContent = `${done - shown} new`; badge.dataset.stale = '1'; }
      if (out.finished) {
        // The feed recorded the end: stop asking, and say Results is ready. No reload under the reader.
        if (timer) { clearInterval(timer); timer = null; }
        if (badge) { badge.textContent = 'ready'; badge.classList.add('ready'); if (startedLive) badge.dataset.stale = '1'; }
        const dot = doc.getElementById('live-dot'); if (dot) dot.classList.remove('on');
      }
    }
    async function tick() {
      try {
        const r = await fetch(endpoint, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        misses = 0; snap = await r.json();
      } catch (e) { misses += 1; }
      paint();
    }
    board.addEventListener('click', e => {
      const t = e.target;
      if (t.closest('[data-close]')) { opts.selected = null; return paint(); }
      if (t.closest('[data-show-all]')) { opts.showAll = !opts.showAll; return paint(); }
      const f = t.closest('[data-filter]'); if (f && !f.disabled) { opts.filter = f.dataset.filter && opts.filter !== f.dataset.filter ? f.dataset.filter : null; return paint(); }
      const tr = t.closest('[data-trial]'); if (tr && !t.closest('a')) { opts.selected = opts.selected === tr.dataset.trial ? null : tr.dataset.trial; return paint(); }
    });
    board.addEventListener('keydown', e => {
      const row = e.target.closest && e.target.closest('.lb-row');
      if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); row.click(); }
      if (e.key === 'Escape' && opts.selected) { opts.selected = null; paint(); }
    });
    const initial = doc.getElementById('live-board-initial');
    if (initial) { try { snap = JSON.parse(initial.textContent); } catch (e) { /* the first poll paints */ } }
    paint();
    if (!snap || stateOf(snap) !== 'finished') { timer = setInterval(tick, 1000); tick(); }
  }
  // The two views of the job page: switched in place, kept in the address as ?view=.
  function mountTabs(doc) {
    const tabs = Array.from(doc.querySelectorAll('[data-view-tab]'));
    if (!tabs.length) return;
    function show(view) {
      tabs.forEach(t => {
        const on = t.dataset.viewTab === view;
        t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1;
        doc.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
      const u = new URL(location.href); u.searchParams.set('view', view);
      // Results as rendered when the page loaded predate trials or a run that ended since: read them again
      const badge = doc.getElementById('results-badge');
      if (view === 'results' && badge && badge.dataset.stale) { location.assign(u); return; }
      history.replaceState(null, '', u);
    }
    tabs.forEach((t, i) => {
      t.addEventListener('click', () => show(t.dataset.viewTab));
      t.addEventListener('keydown', e => {
        const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
        if (d) { e.preventDefault(); const n = tabs[(i + d + tabs.length) % tabs.length]; n.focus(); show(n.dataset.viewTab); }
      });
    });
  }
  if (typeof document !== 'undefined') {
    const go = () => { mountTabs(document); mount(document); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
    else go();
  }
  return {render, renderHead, renderMeta, renderStats, renderMix, renderLanes, renderWinners, renderTiles, renderFlight, renderDrawer, clock, count, tokensOf, stateOf};
}));
