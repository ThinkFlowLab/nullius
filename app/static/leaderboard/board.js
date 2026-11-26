/* Shared row selection; filters affect the table alone. Text is always inserted as text. */
(() => {
  let data = JSON.parse(document.getElementById('lb-data').textContent);
  const marks = JSON.parse(document.getElementById('lb-marks').textContent), table = document.getElementById('leaderboard-table');
  let selected = data.selected, versus = 'average', family = 'capabilities', metric = 'time', generation = 0, focusBefore = null, projection = null, comparisonGeneration = 0, returningFocus = false, tableMotion = null;
  const motion=window.LeaderboardMotion;
  const percent = v => v == null ? '–' : `${Number((v * 100).toFixed(1))}%`;
  const whole = v => v == null ? '–' : `${Math.round(v * 100)}%`;
  const minutes = s => `${Math.floor(s/60)}m ${String(Math.round(s%60)).padStart(2,'0')}s`;
  const measure = (field,v) => v == null ? '–' : field==='cost' ? `$${v.toFixed(2)}` : field==='time' ? minutes(v) : String(Math.round(v));
  const element = (tag, text, attrs = {}) => { const e = document.createElement(tag); if (text != null) e.textContent = text; Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); return e; };
  const svg = (tag, attrs = {}, text) => { const e = document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); if (text != null) e.textContent = text; return e; };
  const row = key => data.rows.find(r => r.key === key);
  /* the charts, the card and the pickers show who is on the board: an agent filtered out of the table leaves them too */
  let onTable = null;
  const shown = r => !onTable || onTable.has(r.key);
  /* the average is the server's own mean, taken over the agents on the board */
  const fieldAverage = axis => { const v=data.rows.filter(shown).map(r=>r.axes[axis]?.score).filter(x=>x!=null); return v.length?v.reduce((a,b)=>a+b,0)/v.length:null; };
  const scored = () => data.rows.filter(r => r.overall != null && shown(r));
  /* a name says only as much as tells two rows apart: the version when an agent has several, the model when a version has several */
  const label = r => { const same=data.rows.filter(v=>v.agent===r.agent); const twin=same.filter(v=>v.version===r.version);
    return [r.name, same.length>1 ? r.version_label : '', twin.length>1 ? [r.identity.model,r.identity.effort].filter(Boolean).join(' ') : ''].filter(Boolean).join(' '); };
  const compared = axis => versus === 'average' ? fieldAverage(axis) : versus.startsWith('version:') ? projection?.rows.find(r=>r.key===selected)?.axes[axis]?.score : row(versus)?.axes[axis]?.score;
  const comparedName = () => versus === 'average' ? null : versus.startsWith('version:') ? 'benchmark version' : row(versus) ? label(row(versus)) : null;
  const signed = v => `${v < 0 ? '−' : '+'}${Math.abs(v*100).toFixed(1)}`;
  const pop = document.getElementById('lb-evidence');
  function dismiss() { pop.hidden = true; }
  function evidence(target) {
    const current = row(target.closest('tr')?.dataset.key || selected); if (!current) return;
    const axisName = target.dataset.axis; if (!axisName) return;
    /* a score's evidence: what it is made of, each part's share, score and what it adds, and the total */
    const shareOf=v=>v==null?'–':v.toFixed(2), adds=(score,share)=>score==null||!share?'–':(score*share*100).toFixed(1);
    const scoreCell=(score,reason)=>{const td=element('td',null,{class:'lb-ev-score'}),box=element('span',null,{class:'lb-ev-scorebox'});td.append(box);if(score==null){box.append(element('span',reason||'not run',{class:'muted'}));return td;}const bar=element('span',null,{class:'lb-ev-bar'}),fill=element('i');fill.style.width=`${score*100}%`;bar.append(fill);box.append(bar,element('span',whole(score),{class:'lb-ev-pct'}));return td;};
    const table=element('table',null,{class:'lb-ev-table'}), head=element('tr'), body=element('tbody');
    [axisName==='Overall'?(family==='domains'?'Domain':'Capability'):'Benchmark · dataset','Weight','Score','Adds'].forEach(t=>head.append(element('th',t)));
    const thead=element('thead');thead.append(head);table.append(thead,body);
    const line=(label,sub,share,score,reason)=>{const tr=element('tr'),name=element('td');name.append(...label);if(sub.length){const small=element('small');small.append(...sub);name.append(small);}tr.append(name,element('td',shareOf(share),{class:'lb-ev-num'}),scoreCell(score,reason),element('td',adds(score,share),{class:'lb-ev-num'}));body.append(tr);};
    const total=(label,score,share)=>{const foot=element('tfoot'),tr=element('tr');tr.append(element('td',label),element('td',shareOf(share),{class:'lb-ev-num'}),element('td',score==null?'–':percent(score),{class:'lb-ev-num'}),element('td'));foot.append(tr);table.append(foot);};
    const field=axisName==='Overall'?null:fieldAverage(axisName);
    pop.replaceChildren(element('strong', `${axisName} · ${current.name}`));
    if (axisName === 'Overall') {
      pop.append(element('p',`Overall is the weighted mean of the ${family} measured`));
      const parts=data[family].map(name=>[name,current.axes[name]]).filter(([,a]) => a.weight > 0);
      parts.forEach(([name,a]) => line([name],[],a.share,a.score,'not measured'));
      total('Overall',current.overall,parts.reduce((n,[,a])=>n+(a.share||0),0));
    } else {
      const axis = current.axes[axisName];
      pop.append(element('p',`${axisName} is the weighted mean of its benchmarks run${field!=null?` · field average ${whole(field)}`:''}`));
      if (!axis.entries.length) pop.append(element('p','No benchmark'));
      else {
        axis.entries.forEach(e => {
          const [bench,...slice]=e.name.split(' · '), label=[e.note?element('span',bench,{title:e.note}):bench];if(slice.length)label.push(element('span',` · ${slice.join(' · ')}${e.metric&&e.metric!=='Score'?' · '+e.metric:''}`,{class:'muted'}));
          const sub=[];
          if(e.version)sub.push(e.version_label||e.version);
          if(e.tasks!=null)sub.push(`${e.covered} of ${e.tasks} tasks`);
          const small=[sub.join(' · ')];
          (e.links||[]).forEach((link,i,all)=>small.push(' · ',element('a',all.length>1?`Open run ${i+1}`:'Open run',{href:link.url,title:link.id})));
          line(label,small,e.share,e.score,e.reason);
        });
        total(axisName,axis.score,axis.entries.reduce((n,e)=>n+(e.share||0),0));
      }
    }
    if(body.children.length)pop.append(table);
    focusBefore = target; pop.hidden = false;
    /* the card never covers what it explains: a radar's whole chart, beside it, else the score itself: below it, else
       above, else beside it; only a window too small for any overlaps */
    const box = target.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight, gap=8, edge=12;
    const clamp=(v,lo,hi)=>Math.max(lo,Math.min(v,hi));
    const chart=target.closest('[data-chart^="radars-"]')?.querySelector('svg')?.getBoundingClientRect();
    if (chart) {
      const top=clamp(box.top+box.height/2-h/2,edge,innerHeight-h-edge);
      const side=innerWidth-chart.right-gap-edge>=w?chart.right+gap:chart.left-gap-edge>=w?chart.left-gap-w:null;
      if (side!=null) {pop.style.left=`${side}px`;pop.style.top=`${top}px`;return;}
    }
    let left=clamp(box.left,edge,innerWidth-w-edge), top;
    if (innerHeight-box.bottom-gap-edge>=h) top=box.bottom+gap;
    else if (box.top-gap-edge>=h) top=box.top-gap-h;
    else {
      top=clamp(box.top+box.height/2-h/2,edge,innerHeight-h-edge);
      if (innerWidth-box.right-gap-edge>=w) left=box.right+gap;
      else if (box.left-gap-edge>=w) left=box.left-gap-w;
      else top=clamp(box.bottom+gap,edge,innerHeight-h-edge);
    }
    pop.style.left=`${left}px`;pop.style.top=`${top}px`;
  }
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { dismiss(); returningFocus=true;focusBefore?.focus();returningFocus=false; } });
  /* hovering shows a score's evidence while the pointer is on it or on the card; a click or keyboard focus keeps it open */
  let pinned=false;
  document.addEventListener('pointerdown', e => { if (!pop.contains(e.target) && !e.target.closest('[data-axis]')) { pinned=false; dismiss(); } });
  document.addEventListener('focusin', e => { const target=e.target.closest('[data-axis]'); if(target&&!returningFocus) { pinned=target.matches(':focus-visible'); evidence(target); } });
  let leaving=0;
  document.addEventListener('pointerover', e => { const target=e.target.closest('[data-axis]'); if(target||pop.contains(e.target))clearTimeout(leaving); if(target&&!pinned) evidence(target); });
  document.addEventListener('pointerout', e => { if(pinned||pop.hidden)return; if(!e.target.closest('[data-axis]')&&!pop.contains(e.target))return; clearTimeout(leaving); leaving=setTimeout(()=>{if(!pinned)dismiss();},180); });
  document.addEventListener('click', e => { const target=e.target.closest('[data-axis]'); if(target) { pinned=true; evidence(target); } });
  function medal(rank) {
    if (!rank) return document.createTextNode('');
    if (rank > 4) return document.createTextNode(String(rank));
    const names=['gold','silver','bronze','chocolate'];
    const span=element('span',null,{class:`lb-medal lb-medal-${rank}`,role:'img','aria-label':`Rank ${rank}, ${names[rank-1]}`});
    span.append(laurel(rank));return span;
  }
  /* a laurel medal: the rank on a struck disc inside a wreath of eight leaves; the 4th, chocolate, has a bite out of
     its upper right and two crumbs. Its colours are its metal's own, the same in both themes. */
  const METAL={1:['#fff1b8','#e8c24a','#9a6b12','#4a3205'],2:['#ffffff','#dde3ea','#8f9aa6','#232a31'],3:['#ffd9b5','#d68a4c','#8a4a1c','#3a1d08'],4:['#a8714c','#6e4027','#3e2112','#f4dcc6']};
  let medals=0;
  function laurel(rank) {
    const [light,mid,dark,ink]=METAL[rank], id=`lb-medal-${++medals}`, plot=svg('svg',{'aria-hidden':'true',viewBox:'0 0 40 48'}), defs=svg('defs');
    const shade=svg('radialGradient',{id:id+'-disc',cx:'35%',cy:'30%',r:'80%'});
    [[0,light],[.55,mid],[1,dark]].forEach(([at,color])=>shade.append(svg('stop',{offset:at,'stop-color':color})));defs.append(shade);
    const body=svg('g');
    if(rank===4){const mask=svg('mask',{id:id+'-bite'});mask.append(svg('rect',{x:-10,y:-10,width:60,height:68,fill:'#fff'}),svg('circle',{cx:31,cy:15,r:4}),svg('circle',{cx:34.6,cy:20,r:3.7}),svg('circle',{cx:26.8,cy:12,r:2.4}));defs.append(mask);body.setAttribute('mask',`url(#${id}-bite)`);}
    for(const side of [-1,1])for(let i=0;i<4;i++){
      const deg=124+i*30,a=deg*Math.PI/180,x=20+Math.cos(a)*15.6,y=25+Math.sin(a)*15.6;
      const leaf=svg('g',{transform:`translate(${(side<0?x:40-x).toFixed(1)} ${y.toFixed(1)}) rotate(${side<0?deg+110:-(deg+110)})`});
      const blade=svg('ellipse',{class:'lb-leaf',cx:0,cy:0,rx:2.9,ry:5.2,fill:i%2?mid:dark});blade.style.setProperty('--i',i);leaf.append(blade);body.append(leaf);
    }
    body.append(svg('circle',{class:'lb-disc',cx:20,cy:25,r:12,'stroke-width':1.4,fill:`url(#${id}-disc)`,stroke:dark}));
    plot.append(defs,body,svg('path',{d:'M14 41l6-3 6 3',fill:'none',stroke:dark,'stroke-width':1.6,'stroke-linecap':'round'}),svg('text',{x:20,y:30.6,'text-anchor':'middle','font-size':16,'font-weight':800,fill:ink},rank));
    if(rank===4)plot.append(svg('circle',{cx:36,cy:26,r:1,fill:mid}),svg('circle',{cx:33.8,cy:29.4,r:.75,fill:mid}));
    return plot;
  }
  /* a score's chip: one blue whose perceived lightness moves in even steps from 50% to 100%, so every point of
     score is the same step of shade; at or below 50% it is the darkest */
  /* one colour per rate: below 60% a navy that deepens with the rate, from 60% one strong colour */
  const heat = v => Math.max(0,Math.min(1,v/.6));
  function chip(cell, value, axis) {
    cell.replaceChildren();
    if (value == null) { cell.append(element('span','–',{class:'lb-none'})); return; }
    /* lightness runs .30 to .80 in the dark theme and .90 to .42 in the light one; past .62 dark text reads best */
    const t=heat(value), ink=value>=.6?' lb-top':'';
    const span=element('span',whole(value),{class:`lb-chip${ink}`,tabindex:'0','data-axis':axis});
    span.style.setProperty('--lb-t',t.toFixed(3));span.style.setProperty('--lb-t2',Math.max(0,(value-.6)/.4).toFixed(3));cell.append(span);
  }
  function overall(cell, r) {
    cell.replaceChildren();
    if (r.overall == null) { cell.append(element('span','–',{class:'lb-none'})); return; }
    cell.append(element('span',`${(r.overall*100).toFixed(1)}%`,{class:'lb-overall',tabindex:'0','data-axis':'Overall'}));
    const bar=element('span',null,{class:'lb-bar'}),fill=element('i');fill.style.width=`${r.overall*100}%`;bar.append(fill);cell.append(bar);
  }
  /* the medals and ranks follow who is on the board: a filter or a search ranks the rows that pass among themselves */
  let onBoard=null;
  const shownRank=r=>onBoard?onBoard.get(r.key)??null:r.rank;
  table.addEventListener('table:drawn',e=>{
    const keys=new Set(e.detail.rows.map(tr=>tr.dataset.key)), ranked=data.rows.filter(r=>keys.has(r.key)&&r.rank).sort((a,b)=>a.rank-b.rank);
    const next=new Map();let previous=null,at=0;ranked.forEach((r,i)=>{if(r.overall!==previous)at=i+1;next.set(r.key,at);previous=r.overall;});
    const moved=!onTable||keys.size!==onTable.size||[...keys].some(k=>!onTable.has(k));onTable=keys;
    if(moved)follow();
    const changed=!onBoard||ranked.length!==onBoard.size||ranked.some(r=>onBoard.get(r.key)!==next.get(r.key));
    onBoard=next;if(!changed)return;
    table.querySelectorAll('tbody tr').forEach(tr=>{const r=row(tr.dataset.key);if(!r)return;const cell=tr.querySelector('[data-col="rank"]');cell.replaceChildren(medal(shownRank(r)));cell.dataset.sort=shownRank(r)||'';});
    card();
  });
  function follow() {
    document.querySelectorAll('[data-agent-select] option, [data-vs-select] option').forEach(o=>{const r=row(o.value);if(r)o.hidden=!shown(r);});
    const first=scored().sort((a,b)=>(a.rank??1e9)-(b.rank??1e9))[0];
    if(versus!=='average'&&!versus.startsWith('version:')&&row(versus)&&!shown(row(versus))){versus='average';document.querySelectorAll('[data-vs-select]').forEach(v=>setSelect(v,versus));}
    if(row(selected)&&!shown(row(selected))&&first)choose(first.key);else draw();
  }
  function updateTable() {
    tableMotion?.capture();
    const focused=document.activeElement, focusAxis=focused?.dataset?.axis, focusRow=focused?.closest?.('tr')?.dataset.key;
    data.rows.forEach(r => {
      const tr = [...table.tBodies[0].rows].find(t => t.dataset.key === r.key); if (!tr) return;
      tr.dataset.sortTie = r.name + ' ' + r.version_label + ' ' + r.key;
      tr.dataset.sortGroup = r.rank ? 0 : r.overall != null ? 1 : 2;
      tr.dataset.fStatus = `${r.rank ? 'ranked' : 'not-ranked'}${!r.has_runs?'\nno-run':''}`;
      tr.classList.toggle('lb-sel', r.key === selected);
      tr.querySelector('[data-col="rank"]').replaceChildren(medal(shownRank(r)));
      tr.querySelector('[data-col="rank"]').dataset.sort=shownRank(r) || '';
      const same = data.rows.filter(v=>v.agent===r.agent && v.version===r.version).length>1;
      tr.querySelector('[data-name]').textContent = r.name + (same ? ' · '+[r.identity.model,r.identity.effort].filter(Boolean).join(' ') : '');
      const o=tr.querySelector('[data-col="overall"]');overall(o,r);o.dataset.sort=r.overall??'';
      data.axes.forEach(name=>{const cell=tr.querySelector(`[data-col="${name}"]`),a=r.axes[name];chip(cell,a.score,name);cell.dataset.sort=a.score??'';});
      for(const [field,m] of Object.entries(r.metrics)) { const cell=tr.querySelector(`[data-col="${field}"]`);cell.replaceChildren(element('span',measure(field,m.value),{class:'lb-m'}));cell.dataset.sort=m.value??'';cell.title=`${m.count} observed`; }
      table.tBodies[0].append(tr);
    });
    /* a column no agent has a score in is hidden, whatever was saved, and cannot be ticked; a saved choice holds for the others */
    let saved=null;try {saved=JSON.parse(localStorage.getItem(table.id+'.cols')||'null');} catch {}
    data.axes.forEach(name=>{
      const cb=document.querySelector(`#${table.id}-tools input[data-col="${name}"]`);if(!cb)return;
      const empty=data.averages[name].score==null;
      cb.disabled=empty;cb.closest('label')?.setAttribute('title',empty?'No score yet':'');
      cb.checked=!empty&&(saved?saved.includes(name):true);
      table.querySelectorAll(`[data-col="${name}"]`).forEach(cell=>cell.style.display=cb.checked?'':'none');
    });
    table.dispatchEvent(new Event('table:update'));
    applyFamily();
    const again=focusAxis&&focusRow?table.querySelector(`tr[data-key="${CSS.escape(focusRow)}"] [data-axis="${CSS.escape(focusAxis)}"]`):focused;
    if(again?.isConnected&&document.activeElement!==again)again.focus({preventScroll:true});
  }
  function applyFamily() {
    document.querySelectorAll('[data-family]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.family===family)));
    table.querySelectorAll('[data-col]').forEach(cell=> { if(data.axes.includes(cell.dataset.col)) cell.hidden=!data[family].includes(cell.dataset.col); });
    size();
  }
  /* each shown column keeps room for its name; past the card's width the table scrolls */
  const heads=()=>[...table.tHead.rows[0].cells];
  const shownHeads=()=>heads().filter(th=>!th.hidden&&th.style.display!=='none');
  function columnWidth(th) {
    const fixed={rank:48,agent:innerWidth<=700?152:238,overall:innerWidth<=700?92:104,steps:66,time:84,calls:92,cost:72};
    return fixed[th.dataset.col]??Math.max(88,Math.round(th.textContent.trim().length*7.4+26));
  }
  /* the columns fill the card exactly: room to spare goes to the score columns alone, so #, Agent, Overall and the
     measures keep their width whatever is shown */
  const wrap=table.closest('.table-wrap');
  function size(except=null) {
    const shown=shownHeads().filter(th=>th.dataset.col!==except), axes=shown.filter(th=>data.axes.includes(th.dataset.col));
    const base=new Map(shown.map(th=>[th,columnWidth(th)])), total=[...base.values()].reduce((a,b)=>a+b,0);
    const spare=Math.max(0,(wrap?.clientWidth||0)-total), each=axes.length?Math.floor(spare/axes.length):0;
    shown.forEach(th=>{th.style.width=`${base.get(th)+(axes.includes(th)?each:0)}px`;});
    if(except)heads().find(th=>th.dataset.col===except).style.width='0px';
    table.style.minWidth=`${total+each*axes.length}px`;
  }
  if(wrap){let seen=wrap.clientWidth;new ResizeObserver(()=>{if(wrap.clientWidth&&Math.abs(wrap.clientWidth-seen)>1){seen=wrap.clientWidth;size();}}).observe(wrap);}
  /* a column chosen in Columns grows from nothing and fades in; one taken away fades and narrows to nothing first */
  const cellsOf=col=>[...table.querySelectorAll(`td[data-col="${CSS.escape(col)}"]`)];
  let passing=false;
  function grow(col) {
    const th=heads().find(h=>h.dataset.col===col);if(!th||th.hidden)return size();
    const cells=[th,...cellsOf(col)];
    cells.forEach(c=>c.classList.add('lb-instant','lb-closing'));th.style.width='0px';table.classList.add('lb-instant');table.offsetWidth;
    cells.forEach(c=>c.classList.remove('lb-instant','lb-closing'));table.classList.remove('lb-instant');size();
    cellsOf(col).forEach(c=>motion.contents(c,true,280));
  }
  function shrink(box) {
    const col=box.dataset.col,th=heads().find(h=>h.dataset.col===col);
    const pass=()=>{passing=true;box.dispatchEvent(new Event('change',{bubbles:true}));passing=false;size();};
    if(!th||th.hidden||motion.reduced.matches){pass();return;}
    const cells=[th,...cellsOf(col)];
    const fades=cells.flatMap(c=>{c.classList.add('lb-closing');return motion.contents(c,false,200);});
    size(col);
    setTimeout(()=>{pass();cells.forEach(c=>c.classList.remove('lb-closing'));fades.forEach(a=>a.cancel());},290);
  }
  /* a select changed from here: its selected attribute moves too, which the page's own select button watches */
  function setSelect(s,value) { if(![...s.options].some(o=>o.value===value))return; [...s.options].forEach(o=>o.toggleAttribute('selected',o.value===value)); s.value=value; }
  function choose(key) {
    if(!row(key)) return; selected=key;
    document.querySelectorAll('[data-agent-select]').forEach(s=>setSelect(s,key));
    table.querySelectorAll('tbody tr').forEach(tr=>tr.classList.toggle('lb-sel',tr.dataset.key===selected));
    draw();
  }
  document.addEventListener('click',e=>{const target=e.target.closest('[data-select]');if(target){choose(target.dataset.select);return;}const tr=e.target.closest('#leaderboard-table tbody tr');if(tr&&!e.target.closest('a'))choose(tr.dataset.key);});
  function selectable(node,key,x,y) { node.setAttribute('tabindex','0');node.setAttribute('role','button');node.dataset.select=key;node.dataset.cx=x;node.dataset.cy=y;node.classList.add('lb-point');node.setAttribute('aria-label',label(row(key)));node.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose(key);}});return node; }

  /* the efficiency scatter, on the mockup's 640 by 426 grid: Overall up, time or cost per task across */
  const PL={l:46,r:618,t:30,b:380};
  /* the scatter is drawn at its box's own width, so its text keeps its size at any width and only the plot grows */
  const scatterBox=document.getElementById('lb-scatter').parentElement;
  const plotWidth=()=>Math.max(520,Math.round(scatterBox.clientWidth||640));
  function fit(values,steps,n,floor) {
    let a=Math.min(...values),b=Math.max(...values);const pad=Math.max((b-a)*.12,steps[0]);a=Math.max(floor,a-pad);b=b+pad;
    const q=steps.find(q=>(b-a)/q<=n)||steps.at(-1);return {step:q,lo:Math.max(floor,Math.floor(a/q+1e-9)*q),hi:Math.ceil(b/q-1e-9)*q};
  }
  const ruler=document.createElement('canvas').getContext('2d');
  const textWidth=(t,weight=600)=>{ruler.font=`${weight} 12px system-ui, sans-serif`;return Math.ceil(ruler.measureText(t).width);};
  function drawScatter() {
    const live=document.getElementById('lb-scatter'),plot=svg('svg'),W=plotWidth();
    PL.r=W-22;live.setAttribute('viewBox',`0 0 ${W} 426`);
    const time=metric==='time', value=r=>r.metrics?.[metric]?.value, x=r=>time?value(r)/60:value(r);
    const points=scored().filter(r=>value(r)!=null);
    const defs=svg('defs');
    const better=svg('linearGradient',{id:'lb-better',x1:0,y1:0,x2:1,y2:1}),worse=svg('linearGradient',{id:'lb-worse',x1:1,y1:1,x2:0,y2:0});
    better.append(svg('stop',{offset:0,'stop-color':'var(--lb-good)','stop-opacity':.26}),svg('stop',{offset:.5,'stop-color':'var(--lb-good)','stop-opacity':0}));
    worse.append(svg('stop',{offset:0,'stop-color':'var(--bad)','stop-opacity':.17}),svg('stop',{offset:.5,'stop-color':'var(--bad)','stop-opacity':0}));
    const clip=svg('clipPath',{id:'lb-logo-clip',clipPathUnits:'objectBoundingBox'});clip.append(svg('rect',{width:1,height:1,rx:.26}));
    defs.append(better,worse,clip);plot.append(defs);
    const frame={x:PL.l,y:PL.t,width:PL.r-PL.l,height:PL.b-PL.t,rx:6};
    plot.append(svg('rect',{...frame,fill:'url(#lb-better)','data-node':'tint-a'}),svg('rect',{...frame,fill:'url(#lb-worse)','data-node':'tint-b'}));
    if(!points.length){plot.append(svg('text',{x:(PL.l+PL.r)/2,y:(PL.t+PL.b)/2,'text-anchor':'middle'},`No ${time?'time':'cost'} recorded yet`));motion.morph(live,plot);return;}
    const fx=fit(points.map(x),time?[.1,.2,.5,1,2,5,10,20,30,60]:[.01,.02,.05,.1,.2,.5,1,2,5],7,0), fy=fit(points.map(r=>r.overall*100),[1,2,5,10,20],6,0);
    fy.hi=Math.min(100,fy.hi);
    const X=v=>PL.l+(v-fx.lo)/((fx.hi-fx.lo)||1)*(PL.r-PL.l), Y=v=>PL.b-(v*100-fy.lo)/((fy.hi-fy.lo)||1)*(PL.b-PL.t);
    const places=q=>q<.1?2:q<1?1:0;
    for(let i=0,v;(v=fy.lo+i*fy.step)<=fy.hi+1e-6;i++)plot.append(svg('line',{x1:PL.l,x2:PL.r,y1:Y(v/100),y2:Y(v/100),class:'lb-grid','data-node':'gy'+i}),svg('text',{x:PL.l-8,y:Y(v/100)+4,'text-anchor':'end','data-node':'ty'+i},`${v}%`));
    for(let i=0,v;(v=fx.lo+i*fx.step)<=fx.hi+1e-6;i++)plot.append(svg('line',{x1:X(v),x2:X(v),y1:PL.t,y2:PL.b,class:'lb-grid','data-node':'gx'+i}),svg('text',{x:X(v),y:PL.b+18,'text-anchor':'middle','data-node':'tx'+i},time?v.toFixed(places(fx.step)):'$'+v.toFixed(Math.max(1,places(fx.step)))));
    plot.append(svg('text',{x:PL.l+10,y:PL.t+20,class:'lb-better','data-node':'better'},'BETTER'),svg('text',{x:PL.l-34,y:PL.t-14,'data-node':'ylabel'},'Overall'),svg('text',{x:(PL.l+PL.r)/2,y:PL.b+36,'text-anchor':'middle','data-node':'xlabel'},`${time?'Minutes':'Cost'} per task`));
    /* each name sits beside its own logo, on whichever side is free of other logos and names */
    const R=13, H=27, spots=points.map(r=>({r,x:X(x(r)),y:Y(r.overall)})), taken=spots.map(s=>({x:s.x-R,y:s.y-R,w:2*R,h:2*R}));
    const clash=(a,b)=>a.x<b.x+b.w+4&&b.x<a.x+a.w+4&&a.y<b.y+b.h+2&&b.y<a.y+a.h+2;
    const ordered=[...spots].sort((a,b)=>(a.r.key===selected)-(b.r.key===selected));
    const placed=new Map();
    [...spots].sort((a,b)=>a.y-b.y).forEach(s=>{
      const w=Math.max(textWidth(s.r.name),textWidth(s.r.version_label,400))+3;
      const options=[{x:s.x+R+5,y:s.y-H/2,anchor:'start'},{x:s.x-R-5-w,y:s.y-H/2,anchor:'end'},{x:s.x-w/2,y:s.y-R-2-H,anchor:'middle'},{x:s.x-w/2,y:s.y+R+2,anchor:'middle'},{x:s.x+R+5,y:s.y-H,anchor:'start'},{x:s.x+R+5,y:s.y,anchor:'start'},{x:s.x-R-5-w,y:s.y-H,anchor:'end'},{x:s.x-R-5-w,y:s.y,anchor:'end'}]
        .map(o=>({...o,w,h:H})).filter(o=>o.x>=PL.l+3&&o.x+w<=PL.r-3&&o.y>=PL.t+2&&o.y+H<=PL.b-2&&!(o.x<PL.l+74&&o.y<PL.t+28));
      const own=taken.find(t=>Math.abs(t.x+R-s.x)<.01&&Math.abs(t.y+R-s.y)<.01);
      const best=options.find(o=>!taken.some(t=>t!==own&&clash(o,t)))||options[0]||{x:s.x+R+5,y:s.y-H/2,anchor:'start',w,h:H};
      taken.push(best);placed.set(s.r.key,best);
    });
    for(const s of ordered){
      const r=s.r,me=r.key===selected,box=placed.get(r.key),tx=box.anchor==='start'?box.x:box.anchor==='end'?box.x+box.w:box.x+box.w/2;
      const group=selectable(svg('g',{'data-node':'point:'+r.key,class:`lb-dot${me?' lb-current':''}`}),r.key,s.x,s.y);
      group.append(svg('title',{},`${label(r)} · ${percent(r.overall)} · ${measure(metric,value(r))}`));
      const logo=svg('g',{class:'lb-logo','data-node':'logo'});
      logo.append(svg('rect',{class:'lb-tile',x:s.x-R,y:s.y-R,width:2*R,height:2*R,rx:7,'data-node':'tile'}));
      const mark=marks[r.key];
      if(mark?.includes('#')){const icon=svg('svg',{x:s.x-R+3,y:s.y-R+3,width:2*R-6,height:2*R-6,viewBox:'0 0 24 24','data-node':'icon'});icon.append(svg('use',{href:mark}));logo.append(icon);}
      else if(mark)logo.append(svg('image',{href:mark,x:s.x-R,y:s.y-R,width:2*R,height:2*R,'clip-path':'url(#lb-logo-clip)','data-node':'image'}));
      else logo.append(svg('text',{x:s.x,y:s.y+4,'text-anchor':'middle','data-node':'initial'},r.name.slice(0,1)));
      logo.append(svg('rect',{class:'lb-edge',x:s.x-R,y:s.y-R,width:2*R,height:2*R,rx:7,'data-node':'edge'}));
      group.append(logo,svg('text',{x:tx,y:box.y+12,'text-anchor':box.anchor,class:`lb-name${me?' lb-me':''}`,'data-node':'name'},r.name),svg('text',{x:tx,y:box.y+24.5,'text-anchor':box.anchor,class:'lb-version','data-node':'version'},r.version_label));
      plot.append(group);
    }
    motion.morph(live,plot);
  }
  function nearest(plot) { plot.addEventListener('click',e=>{if(e.target.closest('[data-select]'))return;const pt=plot.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const p=pt.matrixTransform(plot.getScreenCTM().inverse());const points=[...plot.querySelectorAll('[data-cx]')].map(n=>({n,d:Math.hypot(p.x-Number(n.dataset.cx),p.y-Number(n.dataset.cy))})).sort((a,b)=>a.d-b.d||a.n.dataset.select.localeCompare(b.n.dataset.select));if(points[0]&&points[0].d<30){e.stopPropagation();choose(points[0].n.dataset.select);}},true); }
  nearest(document.getElementById('lb-scatter'));
  let drawnWidth=0;new ResizeObserver(()=>{const W=plotWidth();if(W!==drawnWidth&&drawnWidth){drawnWidth=W;drawScatter();}else drawnWidth=W;}).observe(scatterBox);

  /* strips: every agent's dot on one line per axis; the chosen one larger, the average a dashed tick, the compared one a diamond */
  function strips(group) {
    const container=document.querySelector(`[data-chart="strips-${group}"]`), current=row(selected); if(!current) return;
    const other=versus!=='average';
    data[group].forEach((name,i)=>{
      let line=container.children[i];
      if(!line){line=element('div',null,{class:'lb-srow'});line.append(element('span',name,{class:'lb-axis-name',tabindex:'0','data-axis':name}),element('span',null,{class:'lb-track'}),element('span',null,{class:'lb-sval'}));container.append(line);}
      const track=line.children[1], keep=new Map([...track.children].map(n=>[n.dataset.node,n])), want=[];
      const put=(id,tag,cls,left,attrs={})=>{let n=keep.get(id);if(!n||n.tagName.toLowerCase()!==tag){n=element(tag,null,{'data-node':id});n.style.left=`${left}%`;}keep.delete(id);n.className=cls;Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));requestAnimationFrame(()=>{n.style.left=`${left}%`;});want.push(n);};
      const average=fieldAverage(name);
      if(average!=null)put('average','u','',average*100,{title:`Average ${whole(average)}`});
      data.rows.filter(r=>r.axes[name].score!=null&&r.key!==selected&&shown(r)).forEach(r=>put('r:'+r.key,'i','',r.axes[name].score*100,{role:'button',tabindex:'0','data-select':r.key,'aria-label':`${label(r)} ${whole(r.axes[name].score)}`,title:`${label(r)} ${whole(r.axes[name].score)}`}));
      if(current.axes[name].score!=null)put('r:'+selected,'i','lb-me',current.axes[name].score*100,{role:'button',tabindex:'0','data-select':selected,'aria-label':`${label(current)} ${whole(current.axes[name].score)}`,title:`${label(current)} ${whole(current.axes[name].score)}`});
      const versusScore=compared(name);
      if(other&&versusScore!=null)put('versus','em','',versusScore*100,{title:`${comparedName()} ${whole(versusScore)}`});
      keep.forEach(n=>n.remove());want.forEach(n=>track.append(n));
      const value=line.children[2];value.replaceChildren(document.createTextNode(whole(current.axes[name].score)));
      if(other&&versusScore!=null)value.append(element('small',whole(versusScore)));
    });
  }
  /* radars: rings at half and whole, a spoke per axis, the chosen agent filled, the comparison dashed */
  function radar(group) {
    const container=document.querySelector(`[data-chart="radars-${group}"]`), names=data[group], current=row(selected);if(!current)return;
    const cx=200, cy=178, R=126, n=names.length, at=(i,v)=>{const a=-Math.PI/2+i*2*Math.PI/n;return [cx+Math.cos(a)*v*R,cy+Math.sin(a)*v*R];};
    const points=list=>list.map(p=>p.map(v=>v.toFixed(1)).join(',')).join(' ');
    const plot=svg('svg',{viewBox:'-90 -34 552 424',role:'group','aria-label':`${group} profile`,'data-cx':cx,'data-cy':cy});
    for(const v of [.5,1])plot.append(svg('polygon',{points:points(names.map((_,i)=>at(i,v))),class:'lb-ring','data-node':'ring'+v}));
    names.forEach((name,i)=>{
      /* a label's nearest edge sits the same gap from its corner whichever way it faces: above a corner it ends there,
         below it starts there, beside it it is centred on it */
      const [ex,ey]=at(i,1),ux=(ex-cx)/R,uy=(ey-cy)/R,lx=ex+ux*30,anchor=ux<-.2?'end':ux>.2?'start':'middle';
      const block=36, top=ey+uy*30-(uy<-.3?block:uy>.3?0:block/2), ly=top+14+4;
      plot.append(svg('line',{x1:cx,y1:cy,x2:ex,y2:ey,class:'lb-ring','data-node':'spoke'+i}));
      const text=svg('text',{x:lx,y:ly-4,'text-anchor':anchor,tabindex:0,'data-axis':name,'data-node':'label:'+name,class:'lb-axis-label'});
      text.append(document.createTextNode(name),svg('tspan',{x:lx,dy:19,class:'lb-me'},whole(current.axes[name].score)));
      const v=compared(name);if(versus!=='average'&&v!=null)text.append(svg('tspan',{dx:5,class:'lb-versus'},whole(v)));
      const label=svg('g',{class:'lb-label','data-node':'pill:'+name});label.append(svg('rect',{class:'lb-pill',rx:8,'data-axis':name}),text);plot.append(label);
    });
    const series=(value,comparison)=>names.forEach((name,i)=>{
      const a=value(name),j=(i+1)%n,b=value(names[j]);
      if(a!=null&&b!=null){const p=at(i,a),q=at(j,b);
        if(!comparison)plot.append(svg('polygon',{points:points([[cx,cy],p,q]),class:'lb-wedge','data-node':'wedge:'+name,'data-reveal':i}));
        plot.append(svg('line',{x1:p[0],y1:p[1],x2:q[0],y2:q[1],class:'lb-radar-line'+(comparison?' lb-comparison':''),'data-node':`segment:${comparison}:${name}`,'data-reveal':i}));}
      if(a!=null){const [x,y]=at(i,a),point=svg('circle',{cx:x,cy:y,r:comparison?2.5:5,tabindex:comparison?-1:0,'data-node':`point:${comparison}:${name}`,'data-reveal':i,'data-axis':comparison?null:name,class:'lb-point'+(comparison?' lb-comparison':'')});if(comparison)point.removeAttribute('data-axis');point.append(svg('title',{},`${name}: ${whole(a)}`));plot.append(point);}
    });
    series(name=>compared(name),true);series(name=>current.axes[name].score,false);
    plot.classList.toggle('lb-versus-agent',versus!=='average');
    plot.querySelectorAll('text[data-axis]').forEach(label=>{
      const grow=on=>plot.querySelectorAll(`circle[data-axis="${label.dataset.axis}"]`).forEach(point=>point.classList.toggle('lb-hover',on));
      /* the whole box answers the pointer, not only the letters */
      const box=label.parentNode;box.addEventListener('pointerenter',()=>grow(true));box.addEventListener('pointerleave',()=>grow(false));
      label.addEventListener('focus',()=>grow(true));label.addEventListener('blur',()=>grow(false));
    });
    const live=container.querySelector('svg');
    if(live){live.classList.toggle('lb-versus-agent',versus!=='average');motion.morph(live,plot);}
    else {container.append(plot);motion.reveal(plot,'radars');}
    pills(container.querySelector('svg'));
  }
  /* a label's hover pill is sized on the drawn text; a hidden board has none to measure, so it is sized again once shown */
  function pills(plot) {
    plot?.querySelectorAll('.lb-label').forEach(g=>{
      const b=g.querySelector('text').getBBox(), r=g.querySelector('rect');if(!b.width)return;
      Object.entries({x:b.x-11,y:b.y-7,width:b.width+22,height:b.height+14}).forEach(([k,v])=>r.setAttribute(k,v.toFixed(1)));
    });
  }
  function swatches() {
    const agent=versus!=='average';
    document.querySelectorAll('.lb-swatch').forEach(s=>{
      const second=s.dataset.swatch==='1', line=s.closest('#leaderboard-radars'), color=second?(agent?'var(--series-2)':'var(--text-dim)'):'var(--accent)';
      s.replaceChildren();
      if(line){s.append(svg('line',{x1:1,x2:21,y1:7,y2:7,stroke:color,'stroke-width':2,'stroke-dasharray':second?'4 3':''}));if(!second)s.append(svg('circle',{cx:11,cy:7,r:4,fill:color}));}
      else if(!second)s.append(svg('circle',{cx:11,cy:7,r:6,fill:color}));
      else if(agent)s.append(svg('path',{d:'M11 0l7 7-7 7-7-7z',fill:color}));
      else s.append(svg('line',{x1:11,x2:11,y1:0,y2:14,stroke:color,'stroke-width':1.5,'stroke-dasharray':'3 2'}));
    });
  }
  function card() {
    const current=row(selected), box=document.getElementById('lb-card');if(!current)return;
    const who=element('div',null,{class:'lb-who-big'}), mark=element('span',null,{class:'lb-mark lb-mark-big'}), name=element('span');
    const url=marks[current.key];
    if(url&&!url.includes('#'))mark.append(element('img',null,{src:url,alt:''}));
    else if(url){const icon=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true'});icon.append(svg('use',{href:url}));mark.append(icon);}
    name.append(element('h2',current.name),element('small',[current.company,current.version_label].filter(Boolean).join(' · ')));who.append(mark,name);
    const ranked=onBoard?onBoard.size:data.rows.filter(r=>r.rank).length;
    const figure=element('div',null,{class:'lb-figure'});figure.append(element('span',current.overall==null?'–':`${(current.overall*100).toFixed(1)}%`,{class:'lb-fig'}),element('span',shownRank(current)?`rank ${shownRank(current)} of ${ranked}`:'not ranked',{class:'muted'}));
    const bar=element('span',null,{class:'lb-bar lb-bar-wide'}),fill=element('i');fill.style.width=`${(current.overall||0)*100}%`;bar.append(fill);
    const against=comparedName()||'the field';
    const differences=data[family].map(name=>({name,d:current.axes[name].score!=null&&compared(name)!=null?current.axes[name].score-compared(name):null})).filter(v=>v.d!=null).sort((a,b)=>b.d-a.d);
    const baseline=versus==='average'?null:versus.startsWith('version:')?projection?.rows.find(r=>r.key===selected)?.overall:row(versus)?.overall;
    const lines=[];
    /* a difference reads green above the reference and red below it */
    const change=d=>element('span',signed(d),{class:d>0?'lb-up':d<0?'lb-down':''});
    const named=v=>v?[`${v.name} `,change(v.d)]:['–'];
    if(baseline!=null&&current.overall!=null)lines.push([`Overall against ${against}`,[change(current.overall-baseline)]]);
    lines.push([`Best against ${against}`,named(differences[0])],[`Weakest against ${against}`,named(differences.at(-1))]);
    lines.push(['Time per task',[measure('time',current.metrics?.time?.value)]],['Cost per task',[measure('cost',current.metrics?.cost?.value)]]);
    box.replaceChildren(who,figure,bar,...lines.map(([k,v,title])=>{const line=element('div',null,{class:'lb-kv'});const value=element('b',null,title?{title}:{});value.append(...v);line.append(element('span',k),value);return line;}));
  }
  function draw() {
    if(!row(selected))return;
    swatches();card();drawScatter();for(const group of ['domains','capabilities']){strips(group);radar(group);}
  }
  function press(group,attr,value){document.querySelectorAll(`#${group} [${attr}]`).forEach(b=>b.setAttribute('aria-pressed',String(b.getAttribute(attr)===value)));}
  /* switching families fades what the changing columns hold, never the cells, so a row's colour and Overall stay put */
  let swapping=0;
  document.querySelectorAll('[data-family]').forEach(b=>b.onclick=()=>{
    if(b.dataset.family===family)return; family=b.dataset.family;
    document.querySelectorAll('[data-family]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.family===family)));
    clearTimeout(swapping);
    useFamily();
    if(motion.reduced.matches){updateTable();draw();return;}
    table.classList.add('lb-swap');
    /* the columns that come in take their widths at once, behind the faded contents: widths easing while the table's
       own width has already changed would hand the difference to Agent and Overall */
    swapping=setTimeout(()=>{table.classList.add('lb-instant');updateTable();table.offsetWidth;table.classList.remove('lb-instant','lb-swap');draw();},150);
  });
  document.querySelectorAll('[data-agent-select]').forEach(s=>s.onchange=()=>choose(s.value));
  async function compareChoice() { const turn=++comparisonGeneration;projection=null;if(versus.startsWith('version:')) {const [benchmark,version]=JSON.parse(versus.slice(8));const response=await nulliusFetch('/api/leaderboard/comparison?'+new URLSearchParams({benchmark,version}));if(response.ok){const result=await response.json();if(turn!==comparisonGeneration)return;projection=result;}}draw();}
  document.querySelectorAll('[data-vs-select]').forEach(s=>{data.versions.forEach(v=>s.append(element('option',`Same agent: ${v.label}`,{value:'version:'+JSON.stringify([v.benchmark,v.version])})));s.onchange=()=>{versus=s.value;document.querySelectorAll('[data-vs-select]').forEach(v=>setSelect(v,versus));compareChoice().catch(()=>{});};});
  document.querySelectorAll('[data-metric]').forEach(b=>b.onclick=()=>{metric=b.dataset.metric;press('lb-metric','data-metric',metric);drawScatter();});
  async function deferred(turn) { const response=await nulliusFetch(`/api/leaderboard/metrics?context=${encodeURIComponent(data.context)}`);if(!response.ok)return;const result=await response.json();if(turn!==generation||result.context!==data.context)return;result.rows.forEach(r=>{const existing=row(r.key);if(existing)existing.metrics=r.metrics;});updateTable();card();drawScatter(); }
  async function fetchBoard(){const turn=++generation;const response=await nulliusFetch('/api/leaderboard');if(!response.ok)return null;const next=await response.json();return turn===generation?{next,turn}:null;}
  /* each family has its own Overall and ranks: the shown family's stand in a row's overall and rank, its ranking orders the rows */
  function useFamily(){data.rows.forEach(r=>{const f=r.families?.[family];if(f){r.overall=f.overall;r.rank=f.rank;}});data.rows.sort((a,b)=>(a.rank??1e9)-(b.rank??1e9)||(b.overall??-1)-(a.overall??-1)||a.name.localeCompare(b.name));}
  function apply({next,turn}){if(turn!==generation)return;const kept=new Map(data.rows.map(r=>[r.key,r.metrics]));next.rows.forEach(r=>{const m=kept.get(r.key);if(!m)return;r.metrics={...m,...Object.fromEntries(Object.entries(r.metrics||{}).filter(([,v])=>v?.value!=null))};});data=next;useFamily();if(!row(selected))selected=data.selected;updateTable();compareChoice().catch(()=>{});draw();deferred(turn).catch(()=>{});}
  /* scores saved in Scoring settings reach the board when it is shown again */
  /* each save fetches the new board at once, so it is ready when the board is shown; it is drawn then, at its real size */
  let fresh=null;
  document.addEventListener('leaderboard:rescored',()=>{fresh=null;fetchBoard().then(got=>{if(!got)return;if(table.getClientRects().length)apply(got);else fresh=got;}).catch(()=>{});});
  document.addEventListener('leaderboard:view',e=>{if(e.detail.view!=='board')return;if(fresh){const got=fresh;fresh=null;apply(got);}document.querySelectorAll('[data-chart^="radars-"] svg').forEach(pills);});
  document.getElementById(`${table.id}-tools`)?.addEventListener('change',e=>{
    const box=e.target;if(!box.matches('input[data-col]')||passing)return;
    if(!box.checked){e.stopPropagation();shrink(box);}
    else requestAnimationFrame(()=>motion.reduced.matches?size():grow(box.dataset.col));
  },true);
  useFamily();updateTable();choose(selected);tableMotion=motion.table(table);deferred(generation).catch(()=>{});
})();
