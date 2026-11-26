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
  const scored = () => data.rows.filter(r => r.overall != null);
  /* a name says only as much as tells two rows apart: the version when an agent has several, the model when a version has several */
  const label = r => { const same=data.rows.filter(v=>v.agent===r.agent); const twin=same.filter(v=>v.version===r.version);
    return [r.name, same.length>1 ? r.version_label : '', twin.length>1 ? [r.identity.model,r.identity.effort].filter(Boolean).join(' ') : ''].filter(Boolean).join(' '); };
  const compared = axis => versus === 'average' ? data.averages[axis]?.score : versus.startsWith('version:') ? projection?.rows.find(r=>r.key===selected)?.axes[axis]?.score : row(versus)?.axes[axis]?.score;
  const comparedName = () => versus === 'average' ? null : versus.startsWith('version:') ? 'benchmark version' : row(versus) ? label(row(versus)) : null;
  const signed = v => `${v < 0 ? '−' : '+'}${Math.abs(v*100).toFixed(1)}`;
  const pop = document.getElementById('lb-evidence');
  function dismiss() { pop.hidden = true; }
  function evidence(target) {
    const current = row(target.closest('tr')?.dataset.key || selected); if (!current) return;
    const axisName = target.dataset.axis; if (!axisName) return;
    pop.replaceChildren(element('strong', axisName));
    if (axisName === 'Overall') {
      current.reasons.forEach(reason => pop.append(element('p', `Not ranked: ${reason}`)));
      pop.append(element('p',[current.identity.model,current.identity.effort].filter(Boolean).join(' · ')));
      data.axes.map(name=>[name,current.axes[name]]).filter(([,a]) => a.weight > 0).forEach(([name,a]) => {
        const names = a.entries.filter(e => e.unverified && e.share > 0).map(e => e.name);
        pop.append(element('p', `${name} · ${percent(a.score)} · weight ${a.weight} · share ${percent(a.share)}${names.length ? ' · '+names.join(', ')+' · not verified' : ''}`));
      });
    } else {
      const axis = current.axes[axisName];
      if (!axis.entries.length) pop.append(element('p','No benchmark'));
      axis.entries.forEach(e => {
        const line = element('p', `${e.name}${e.version ? ' · '+(e.version_label || e.version) : ''} · ${e.score == null ? e.reason || 'not run' : percent(e.score)}${e.unverified ? ' · not verified' : ''} · ${innerWidth <= 700 ? 'x' : 'weight'} ${e.weight} · share ${percent(e.share)}`);
        if (!e.custom && e.tasks != null) line.append(element('span', ` · ${e.covered}/${e.tasks} tasks`, {class:e.covered === e.tasks ? 'lb-complete-count' : ''}));
        (e.links || []).forEach(link => { line.append(document.createTextNode(' · '),element('a',link.id,{href:link.url})); });
        if (e.note) line.append(document.createTextNode(` · ${e.note}`));
        pop.append(line);
      });
    }
    focusBefore = target; pop.hidden = false;
    const box = target.getBoundingClientRect();
    pop.style.left = `${Math.max(12,Math.min(box.left,innerWidth-pop.offsetWidth-12))}px`;
    pop.style.top = `${Math.max(12,Math.min(box.bottom+6,innerHeight-pop.offsetHeight-12))}px`;
  }
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { dismiss(); returningFocus=true;focusBefore?.focus();returningFocus=false; } });
  document.addEventListener('pointerdown', e => { if (!pop.contains(e.target) && !e.target.closest('[data-axis]')) dismiss(); });
  document.addEventListener('focusin', e => { const target=e.target.closest('[data-axis]'); if(target&&!returningFocus) evidence(target); });
  document.addEventListener('pointerover', e => { const target=e.target.closest('[data-axis]'); if(target) evidence(target); });
  document.addEventListener('click', e => { const target=e.target.closest('[data-axis]'); if(target) evidence(target); });
  function medal(rank) {
    if (!rank) return document.createTextNode('');
    if (rank > 4) return document.createTextNode(String(rank));
    const names=['gold','silver','bronze','chocolate'];
    const span=element('span',null,{class:`lb-medal lb-medal-${rank}`,role:'img','aria-label':`Rank ${rank}, ${names[rank-1]}`});
    const icon=svg('svg',{'aria-hidden':'true',viewBox:'0 0 24 24'});icon.append(svg('use',{href:rank===4?'#i-medal-bitten':'#i-medal'}));span.append(icon,element('span',rank));return span;
  }
  /* a score's chip: its shade climbs from the surface to the series colour between 20% and 80% */
  const heat = v => Math.max(0,Math.min(1,(v-.2)/.6));
  function chip(cell, value, unverified, axis) {
    cell.replaceChildren();
    if (value == null) { cell.append(element('span','–',{class:'lb-none'})); return; }
    const t=heat(value), span=element('span',whole(value),{class:`lb-chip${t>.5?' lb-hot':''}${unverified?' lb-unverified':''}`,tabindex:'0','data-axis':axis});
    span.style.setProperty('--lb-heat',`${Math.round(t*100)}%`);cell.append(span);
  }
  function overall(cell, r) {
    cell.replaceChildren();
    if (r.overall == null) { cell.append(element('span','–',{class:'lb-none'})); return; }
    cell.append(element('span',`${(r.overall*100).toFixed(1)}%`,{class:`lb-overall${r.unverified?' lb-unverified':''}`,tabindex:'0','data-axis':'Overall'}));
    const bar=element('span',null,{class:'lb-bar'}),fill=element('i');fill.style.width=`${r.overall*100}%`;bar.append(fill);cell.append(bar);
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
      tr.querySelector('[data-col="rank"]').replaceChildren(medal(r.rank));
      tr.querySelector('[data-col="rank"]').dataset.sort=r.rank || '';
      const same = data.rows.filter(v=>v.agent===r.agent && v.version===r.version).length>1;
      tr.querySelector('[data-name]').textContent = r.name + (same ? ' · '+[r.identity.model,r.identity.effort].filter(Boolean).join(' ') : '');
      const o=tr.querySelector('[data-col="overall"]');overall(o,r);o.dataset.sort=r.overall??'';
      data.axes.forEach(name=>{const cell=tr.querySelector(`[data-col="${name}"]`),a=r.axes[name];chip(cell,a.score,a.unverified,name);cell.dataset.sort=a.score??'';});
      for(const [field,m] of Object.entries(r.metrics)) { const cell=tr.querySelector(`[data-col="${field}"]`);cell.textContent=measure(field,m.value);cell.dataset.sort=m.value??'';cell.title=`${m.count} observed`; }
      table.tBodies[0].append(tr);
    });
    let explicit=false;try {explicit=localStorage.getItem(table.id+'.cols')!==null;} catch {}
    if(!explicit) data.axes.forEach(name=>{
      const cb=document.querySelector(`#${table.id}-tools input[data-col="${name}"]`);
      if(cb) {cb.checked=data.averages[name].score!=null;table.querySelectorAll(`[data-col="${name}"]`).forEach(cell=>cell.style.display=cb.checked?'':'none');}
    });
    table.dispatchEvent(new Event('table:update'));
    document.getElementById('lb-trust-key').hidden=!data.rows.some(r=>r.custom_names.length);
    applyFamily();
    const again=focusAxis&&focusRow?table.querySelector(`tr[data-key="${CSS.escape(focusRow)}"] [data-axis="${CSS.escape(focusAxis)}"]`):focused;
    if(again?.isConnected&&document.activeElement!==again)again.focus({preventScroll:true});
  }
  function applyFamily() {
    document.querySelectorAll('[data-family]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.family===family)));
    table.querySelectorAll('[data-col]').forEach(cell=> { if(data.axes.includes(cell.dataset.col)) cell.hidden=!data[family].includes(cell.dataset.col); });
    /* each shown column keeps room for its name; past the card's width the table scrolls */
    const shown=[...table.tHead.rows[0].cells].filter(th=>!th.hidden&&th.style.display!=='none');
    const fixed={rank:48,agent:innerWidth<=700?152:238,overall:innerWidth<=700?92:104,steps:66,time:84,calls:92,cost:72};
    const width=th=>fixed[th.dataset.col]??Math.max(64,Math.round(th.textContent.trim().length*6.4+24));
    shown.forEach(th=>{if(!(th.dataset.col in fixed))th.style.width=`${width(th)}px`;});
    table.style.minWidth=`${shown.reduce((sum,th)=>sum+width(th),0)}px`;
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
  function fit(values,steps,n,floor) {
    let a=Math.min(...values),b=Math.max(...values);const pad=Math.max((b-a)*.12,steps[0]);a=Math.max(floor,a-pad);b=b+pad;
    const q=steps.find(q=>(b-a)/q<=n)||steps.at(-1);return {step:q,lo:Math.max(floor,Math.floor(a/q+1e-9)*q),hi:Math.ceil(b/q-1e-9)*q};
  }
  const ruler=document.createElement('canvas').getContext('2d');
  const textWidth=(t,weight=600)=>{ruler.font=`${weight} 12px system-ui, sans-serif`;return Math.ceil(ruler.measureText(t).width);};
  function drawScatter() {
    const live=document.getElementById('lb-scatter'),plot=svg('svg');
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

  /* strips: every agent's dot on one line per axis; the chosen one larger, the average a dashed tick, the compared one a diamond */
  function strips(group) {
    const container=document.querySelector(`[data-chart="strips-${group}"]`), current=row(selected); if(!current) return;
    const other=versus!=='average';
    data[group].forEach((name,i)=>{
      let line=container.children[i];
      if(!line){line=element('div',null,{class:'lb-srow'});line.append(element('span',name,{class:'lb-axis-name',tabindex:'0','data-axis':name}),element('span',null,{class:'lb-track'}),element('span',null,{class:'lb-sval'}));container.append(line);}
      const track=line.children[1], keep=new Map([...track.children].map(n=>[n.dataset.node,n])), want=[];
      const put=(id,tag,cls,left,attrs={})=>{let n=keep.get(id);if(!n||n.tagName.toLowerCase()!==tag){n=element(tag,null,{'data-node':id});n.style.left=`${left}%`;}keep.delete(id);n.className=cls;Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));requestAnimationFrame(()=>{n.style.left=`${left}%`;});want.push(n);};
      const average=data.averages[name]?.score;
      if(average!=null)put('average','u','',average*100,{title:`Average ${whole(average)}`});
      data.rows.filter(r=>r.axes[name].score!=null&&r.key!==selected).forEach(r=>put('r:'+r.key,'i','',r.axes[name].score*100,{role:'button',tabindex:'0','data-select':r.key,'aria-label':`${label(r)} ${whole(r.axes[name].score)}`,title:`${label(r)} ${whole(r.axes[name].score)}`}));
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
    const cx=200, cy=160, n=names.length, at=(i,v)=>{const a=-Math.PI/2+i*2*Math.PI/n;return [cx+Math.cos(a)*v*110,cy+Math.sin(a)*v*110];};
    const points=list=>list.map(p=>p.map(v=>v.toFixed(1)).join(',')).join(' ');
    const plot=svg('svg',{viewBox:'-40 -5 480 345',role:'group','aria-label':`${group} profile`,'data-cx':cx,'data-cy':cy});
    for(const v of [.5,1])plot.append(svg('polygon',{points:points(names.map((_,i)=>at(i,v))),class:'lb-ring','data-node':'ring'+v}));
    names.forEach((name,i)=>{
      const [ex,ey]=at(i,1),[lx,ly]=at(i,1.18),anchor=lx<cx-10?'end':lx>cx+10?'start':'middle';
      plot.append(svg('line',{x1:cx,y1:cy,x2:ex,y2:ey,class:'lb-ring','data-node':'spoke'+i}));
      const text=svg('text',{x:lx,y:ly-4,'text-anchor':anchor,tabindex:0,'data-axis':name,'data-node':'label:'+name,class:'lb-axis-label'});
      text.append(document.createTextNode(name),svg('tspan',{x:lx,dy:15,class:'lb-me'},whole(current.axes[name].score)));
      const v=compared(name);if(versus!=='average'&&v!=null)text.append(svg('tspan',{dx:5,class:'lb-versus'},whole(v)));
      plot.append(text);
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
      label.addEventListener('pointerenter',()=>grow(true));label.addEventListener('pointerleave',()=>grow(false));
      label.addEventListener('focus',()=>grow(true));label.addEventListener('blur',()=>grow(false));
    });
    const live=container.querySelector('svg');
    if(live){live.classList.toggle('lb-versus-agent',versus!=='average');motion.morph(live,plot);}
    else {container.append(plot);motion.reveal(plot,'radars');}
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
    const ranked=data.rows.filter(r=>r.rank).length;
    const figure=element('div',null,{class:'lb-figure'});figure.append(element('span',current.overall==null?'–':`${(current.overall*100).toFixed(1)}%`,{class:'lb-fig'}),element('span',current.rank?`rank ${current.rank} of ${ranked}`:'not ranked',{class:'muted'}));
    const bar=element('span',null,{class:'lb-bar lb-bar-wide'}),fill=element('i');fill.style.width=`${(current.overall||0)*100}%`;bar.append(fill);
    const against=comparedName()||'the field';
    const differences=data.capabilities.map(name=>({name,d:current.axes[name].score!=null&&compared(name)!=null?current.axes[name].score-compared(name):null})).filter(v=>v.d!=null).sort((a,b)=>b.d-a.d);
    const baseline=versus==='average'?null:versus.startsWith('version:')?projection?.rows.find(r=>r.key===selected)?.overall:row(versus)?.overall;
    const lines=[];
    const model=[current.identity.model,current.identity.effort].filter(Boolean).join(' · ');if(model)lines.push(['Model',model]);
    if(!current.rank&&current.reasons.length)lines.push(['Not ranked',current.reasons.length>1?`${current.reasons[0]} and ${current.reasons.length-1} more`:current.reasons[0],current.reasons.join('\n')]);
    if(baseline!=null&&current.overall!=null)lines.push([`Overall against ${against}`,signed(current.overall-baseline)]);
    lines.push([`Best against ${against}`,differences.length?`${differences[0].name} ${signed(differences[0].d)}`:'–'],[`Weakest against ${against}`,differences.length?`${differences.at(-1).name} ${signed(differences.at(-1).d)}`:'–']);
    lines.push(['Time per task',measure('time',current.metrics?.time?.value)],['Cost per task',measure('cost',current.metrics?.cost?.value)]);
    if(current.custom_names.length)lines.push(['Not verified',current.custom_names.join(', ')]);
    box.replaceChildren(who,figure,bar,...lines.map(([k,v,title])=>{const line=element('div',null,{class:'lb-kv'});line.append(element('span',k),element('b',v,title?{title}:{}));return line;}));
  }
  function draw() {
    if(!row(selected))return;
    swatches();card();drawScatter();for(const group of ['domains','capabilities']){strips(group);radar(group);}
  }
  function press(group,attr,value){document.querySelectorAll(`#${group} [${attr}]`).forEach(b=>b.setAttribute('aria-pressed',String(b.getAttribute(attr)===value)));}
  document.querySelectorAll('[data-family]').forEach(b=>b.onclick=()=>{family=b.dataset.family;applyFamily();table.querySelectorAll('td[data-col]:not([hidden]) .lb-chip').forEach(cell=>motion.fade(cell,[{opacity:0},{opacity:1}],200));});
  document.querySelectorAll('[data-agent-select]').forEach(s=>s.onchange=()=>choose(s.value));
  async function compareChoice() { const turn=++comparisonGeneration;projection=null;if(versus.startsWith('version:')) {const [benchmark,version]=JSON.parse(versus.slice(8));const response=await nulliusFetch('/api/leaderboard/comparison?'+new URLSearchParams({benchmark,version,all_scores:data.all_scores?'1':'0'}));if(response.ok){const result=await response.json();if(turn!==comparisonGeneration)return;projection=result;}}draw();}
  document.querySelectorAll('[data-vs-select]').forEach(s=>{data.versions.forEach(v=>s.append(element('option',`Same agent: ${v.label}`,{value:'version:'+JSON.stringify([v.benchmark,v.version])})));s.onchange=()=>{versus=s.value;document.querySelectorAll('[data-vs-select]').forEach(v=>setSelect(v,versus));compareChoice().catch(()=>{});};});
  document.querySelectorAll('[data-metric]').forEach(b=>b.onclick=()=>{metric=b.dataset.metric;press('lb-metric','data-metric',metric);drawScatter();});
  async function deferred(turn) { const response=await nulliusFetch(`/api/leaderboard/metrics?all_scores=${data.all_scores?1:0}&context=${encodeURIComponent(data.context)}`);if(!response.ok)return;const result=await response.json();if(turn!==generation||result.context!==data.context)return;result.rows.forEach(r=>{const existing=row(r.key);if(existing)existing.metrics=r.metrics;});updateTable();card();drawScatter(); }
  document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=async()=>{press('lb-mode','data-mode',b.dataset.mode);const turn=++generation;const response=await nulliusFetch(`/api/leaderboard?all_scores=${b.dataset.mode}`);if(!response.ok)return;const next=await response.json();if(turn!==generation)return;data=next;if(!row(selected))selected=data.selected;updateTable();compareChoice().catch(()=>{});draw();deferred(turn).catch(()=>{});});
  document.getElementById(`${table.id}-tools`)?.addEventListener('change',e=>{if(e.target.matches('input[data-col]'))requestAnimationFrame(applyFamily);});
  updateTable();choose(selected);tableMotion=motion.table(table);deferred(generation).catch(()=>{});
})();
