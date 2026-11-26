/* Shared row selection; filters affect the table alone. Text is always inserted as text. */
(() => {
  let data = JSON.parse(document.getElementById('lb-data').textContent);
  const marks = JSON.parse(document.getElementById('lb-marks').textContent), table = document.getElementById('leaderboard-table');
  let selected = data.selected, versus = 'average', family = 'capabilities', metric = 'time', generation = 0, focusBefore = null, projection = null, comparisonGeneration = 0, returningFocus = false, tableMotion = null;
  const motion=window.LeaderboardMotion;
  const percent = v => v == null ? '—' : `${Number((v * 100).toFixed(1))}%`;
  const element = (tag, text, attrs = {}) => { const e = document.createElement(tag); if (text != null) e.textContent = text; Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); return e; };
  const svg = (tag, attrs = {}, text) => { const e = document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); if (text != null) e.textContent = text; return e; };
  const row = key => data.rows.find(r => r.key === key);
  const compared = axis => versus === 'average' ? data.averages[axis]?.score : versus.startsWith('version:') ? projection?.rows.find(r=>r.key===selected)?.axes[axis]?.score : row(versus)?.axes[axis]?.score;
  const delta = (a,b) => a == null || b == null ? '—' : `${a >= b ? '+' : ''}${((a-b)*100).toFixed(1)} pp`;
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
  function updateTable() {
    tableMotion?.capture();
    const focused=document.activeElement;
    data.rows.forEach(r => {
      const tr = [...table.tBodies[0].rows].find(t => t.dataset.key === r.key); if (!tr) return;
      tr.dataset.sortTie = r.name + ' ' + r.version_label + ' ' + r.key;
      tr.dataset.sortGroup = r.rank ? 0 : r.overall != null ? 1 : 2;
      tr.dataset.fStatus = `${r.rank ? 'ranked' : 'not-ranked'}${!r.has_runs?'\nno-run':''}`;
      tr.querySelector('[data-col="rank"]').replaceChildren(medal(r.rank));
      tr.querySelector('[data-col="rank"]').dataset.sort=r.rank || '';
      const same = data.rows.filter(v=>v.agent===r.agent && v.version===r.version).length>1;
      tr.querySelector('[data-name]').textContent = r.name + (same ? ' · '+[r.identity.model,r.identity.effort].filter(Boolean).join(' ') : '');
      tr.querySelectorAll('[data-axis]').forEach(cell => {
        const a=cell.dataset.axis==='Overall'?{score:r.overall,unverified:r.unverified}:r.axes[cell.dataset.axis];
        let value=cell.querySelector('.lb-value');if(!value){cell.replaceChildren();value=element('span',null,{class:'lb-value'});cell.append(value);}
        value.textContent=percent(a.score);cell.dataset.sort=a.score??'';
        cell.classList.toggle('lb-unverified',Boolean(a.unverified));
        cell.style.setProperty('--lb-shade',`${a.score==null?0:Math.max(0,Math.min(1,(a.score-.2)/.6))*20}%`);
        let bar=cell.querySelector('.lb-bar');
        if(a.score!=null){if(!bar){bar=element('span',null,{class:'lb-bar'});cell.append(bar);}bar.style.setProperty('--lb-value',`${a.score*100}%`);}
        else bar?.remove();
      });
      for(const [field,m] of Object.entries(r.metrics)) { const cell=tr.querySelector(`[data-col="${field}"]`);cell.textContent=m.value==null?'—':field==='cost'?`$${m.value.toFixed(2)}`:Number(m.value.toFixed(1))+(field==='time'?'s':'');cell.dataset.sort=m.value??'';cell.title=`${m.count} observed`; }
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
    if(focused?.isConnected&&document.activeElement!==focused)focused.focus({preventScroll:true});
  }
  function applyFamily() {
    document.querySelectorAll('[data-family]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.family===family)));
    table.querySelectorAll('[data-col]').forEach(cell=> { if(data.axes.includes(cell.dataset.col)) cell.hidden=!data[family].includes(cell.dataset.col); });
  }
  function choose(key) { if(!row(key)) return; selected=key;document.querySelectorAll('[data-agent-select]').forEach(s=>s.value=selected);draw(); }
  document.addEventListener('click',e=>{const target=e.target.closest('[data-select]');if(target) choose(target.dataset.select);});
  function selectable(node,key,x,y) { node.setAttribute('tabindex','0');node.setAttribute('role','button');node.dataset.select=key;node.dataset.cx=x;node.dataset.cy=y;node.classList.add('lb-point');node.setAttribute('aria-label',`${row(key)?.name} ${row(key)?.version_label}`);node.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose(key);}});return node; }
  function nearest(plot) { plot.addEventListener('click',e=>{if(e.target.closest('text[data-select]'))return;const pt=plot.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const p=pt.matrixTransform(plot.getScreenCTM().inverse());const points=[...plot.querySelectorAll('[data-cx]')].map(n=>({n,d:Math.hypot(p.x-Number(n.dataset.cx),p.y-Number(n.dataset.cy))})).sort((a,b)=>a.d-b.d||a.n.dataset.select.localeCompare(b.n.dataset.select));if(points[0]&&points[0].d<30){e.stopPropagation();choose(points[0].n.dataset.select);}},true); }
  function drawScatter() {
    const live=document.getElementById('lb-scatter'),plot=svg('svg'), panel=data.scatter[metric];live.setAttribute('viewBox',`0 0 360 ${panel.height}`);
    const defs=svg('defs'), gradient=svg('linearGradient',{id:'lb-efficiency-tint',x1:0,y1:0,x2:1,y2:1});
    gradient.append(svg('stop',{offset:'0%','stop-color':'var(--good)','stop-opacity':'.12'}),svg('stop',{offset:'50%','stop-color':'var(--bg)','stop-opacity':'0'}),svg('stop',{offset:'100%','stop-color':'var(--bad)','stop-opacity':'.12'}));defs.append(gradient);plot.append(defs,svg('rect',{x:48,y:22,width:304,height:210,fill:'url(#lb-efficiency-tint)'}));
    plot.append(svg('text',{x:54,y:38,class:'lb-better'},'BETTER'));
    for(const m of panel.marks) {
      if(m.shape==='tile') {
        const group=selectable(svg('g',{'data-node':'point:'+m.key}),m.key,m.x,m.y), mark=marks[m.key];group.append(svg('title',{},m.title));
        group.append(svg('rect',{x:m.x-11,y:m.y-11,width:22,height:22,rx:4,fill:'var(--bg)',class:m.key===selected?'lb-selected':''}));
        if(mark?.includes('#')) {const icon=svg('svg',{x:m.x-9,y:m.y-9,width:18,height:18,viewBox:'0 0 24 24'});icon.append(svg('use',{href:mark}));group.append(icon);}
        else if(mark) group.append(svg('image',{x:m.x-9,y:m.y-9,width:18,height:18,href:mark}));
        else group.append(svg('text',{x:m.x,y:m.y+4,'text-anchor':'middle'},row(m.key)?.name.slice(0,1)));
        plot.append(group);
      } else plot.append(svg('line',{x1:m.x,y1:m.y,x2:m.x2,y2:m.y2,class:m.cls}));
    }
    for(const t of panel.texts) {const text=svg('text',{x:t.x,y:t.y,'text-anchor':t.anchor,class:t.cls});t.lines.forEach((line,i)=>text.append(svg('tspan',{x:t.x,dy:i?15:0,class:i?'small':''},line)));if(t.key) {text.dataset.node='name:'+t.key;text.dataset.select=t.key;text.setAttribute('tabindex','0');text.addEventListener('keydown',e=>{if(e.key==='Enter')choose(t.key);});}plot.append(text);}
    motion.morph(live,plot);
  }
  nearest(document.getElementById('lb-scatter'));
  function chart(kind,group) {
    const container=document.querySelector(`[data-chart="${kind}-${group}"]`), names=data[group], current=row(selected);if(!current)return;
    const plot=svg('svg',{viewBox:`0 0 360 ${kind==='strips'?names.length*42+20:320}`,role:'group','aria-label':`${group} ${kind}`});
    if(kind==='strips') names.forEach((name,i)=>{
      const y=30+i*42, label=svg('text',{x:0,y:y-10,tabindex:0,'data-axis':name},name);plot.append(label,svg('line',{x1:0,y1:y,x2:350,y2:y,class:'grid'}));
      data.rows.forEach(r=>{const score=r.axes[name].score;if(score==null)return;const node=selectable(svg('circle',{cx:score*340+5,cy:y,'data-node':name+':'+r.key,'data-reveal':i,r:r.key===selected?5:3,class:r.key===selected?'lb-selected':''}),r.key,score*340+5,y);node.append(svg('title',{},`${r.name} ${r.version_label} ${r.identity.model||''}: ${percent(score)}${r.axes[name].unverified?' · not verified':''}`));plot.append(node);});
      const score=compared(name);if(score!=null){const x=score*340+5;plot.append(versus==='average'?svg('line',{x1:x,y1:y-8,x2:x,y2:y+8,class:'lb-comparison','stroke-dasharray':'2 2'}):svg('path',{d:`M${x} ${y-6}l6 6-6 6-6-6Z`,class:'lb-comparison'}));}
      plot.append(svg('text',{x:350,y:y-10,'text-anchor':'end'},`${percent(current.axes[name].score)} / ${percent(score)}${versus==='average'?' (n='+data.averages[name].count+')':''}`));
    });
    else {
      const at=(i,value)=>[180+Math.sin(i/names.length*2*Math.PI)*90*value,155-Math.cos(i/names.length*2*Math.PI)*90*value];
      names.forEach((name,i)=>{const [x,y]=at(i,1),[lx,ly]=at(i,1.3);plot.append(svg('line',{x1:180,y1:155,x2:x,y2:y,class:'grid'}));const label=svg('text',{x:Math.max(name.length*4,Math.min(360-name.length*4,lx)),y:ly,tabindex:0,'text-anchor':'middle','data-axis':name},name);plot.append(label);});
      for(const compare of [false,true]) names.forEach((name,i)=>{const score=compare?compared(name):current.axes[name].score;if(score==null)return;const [x,y]=at(i,score),next=(i+1)%names.length,v=compare?compared(names[next]):current.axes[names[next]].score;if(v!=null){const [nx,ny]=at(next,v);plot.append(svg('line',{x1:x,y1:y,x2:nx,y2:ny,'data-node':'segment:'+compare+':'+name,'data-reveal':i,class:'lb-radar-line'+(compare?' lb-comparison':'')}));}const point=svg('circle',{cx:x,cy:y,r:4,tabindex:0,'data-node':'point:'+compare+':'+name,'data-reveal':i,'data-axis':name,class:'lb-point'+(compare?' lb-comparison':'')});point.append(svg('title',{},`${name}: ${percent(score)}`));plot.append(point);});
    }
    plot.querySelectorAll('text[data-axis]').forEach(label=>{
      const grow=on=>plot.querySelectorAll(`circle[data-axis="${label.dataset.axis}"]`).forEach(point=>point.classList.toggle('lb-hover',on));
      label.addEventListener('pointerenter',()=>grow(true));label.addEventListener('pointerleave',()=>grow(false));
      label.addEventListener('focus',()=>grow(true));label.addEventListener('blur',()=>grow(false));
    });
    const live=container.querySelector('svg');
    if(live)motion.morph(live,plot);
    else {container.append(plot);nearest(plot);motion.reveal(plot,kind);}
  }
  function draw() {
    const current=row(selected);if(!current)return;
    const card=document.getElementById('lb-card');card.replaceChildren(element('h2',current.name),element('p',`${percent(current.overall)}${!current.rank?' · not ranked':''}`),element('p',[current.company,current.version_label,current.identity.model,current.identity.effort].filter(Boolean).join(' · ')));
    card.append(element('p',`Covered: ${current.reasons.join('; ')||'All required entries'}`));
    const values=data.rows.map(r=>r.overall).filter(v=>v!=null), baseline=versus==='average'?(values.length?values.reduce((a,b)=>a+b,0)/values.length:null):versus.startsWith('version:')?projection?.rows.find(r=>r.key===selected)?.overall:row(versus)?.overall;
    card.append(element('p',`Overall against ${versus==='average'?'Average':versus.startsWith('version:')?'benchmark version':row(versus)?.name}: ${delta(current.overall,baseline)}`));
    const differences=data.capabilities.map(name=>({name,a:current.axes[name].score,b:compared(name)})).filter(v=>v.a!=null&&v.b!=null).sort((a,b)=>(b.a-b.b)-(a.a-a.b));
    if(differences.length) card.append(element('p',`Best: ${differences[0].name} ${delta(differences[0].a,differences[0].b)}`),element('p',`Weakest: ${differences.at(-1).name} ${delta(differences.at(-1).a,differences.at(-1).b)}`));
    if(current.custom_names.length)card.append(element('p',`Not verified: ${current.custom_names.join(', ')}`));
    drawScatter();for(const kind of ['strips','radars'])for(const group of ['domains','capabilities'])chart(kind,group);
  }
  document.querySelectorAll('[data-family]').forEach(b=>b.onclick=()=>{family=b.dataset.family;applyFamily();table.querySelectorAll('td[data-axis]:not([hidden])').forEach(cell=>{if(cell.dataset.axis!=='Overall')motion.fade(cell,[{opacity:0},{opacity:1}],200);});});
  document.querySelectorAll('[data-agent-select]').forEach(s=>s.onchange=()=>choose(s.value));
  async function compareChoice() { const turn=++comparisonGeneration;projection=null;if(versus.startsWith('version:')) {const [benchmark,version]=JSON.parse(versus.slice(8));const response=await nulliusFetch('/api/leaderboard/comparison?'+new URLSearchParams({benchmark,version,all_scores:data.all_scores?'1':'0'}));if(response.ok){const result=await response.json();if(turn!==comparisonGeneration)return;projection=result;}}draw();}
  document.querySelectorAll('[data-vs-select]').forEach(s=>{data.versions.forEach(v=>s.append(element('option',`Same agent: ${v.label}`,{value:'version:'+JSON.stringify([v.benchmark,v.version])})));s.onchange=()=>{versus=s.value;document.querySelectorAll('[data-vs-select]').forEach(v=>v.value=versus);compareChoice().catch(()=>{});};});
  document.getElementById('lb-metric').onchange=e=>{metric=e.target.value;drawScatter();};
  async function deferred(turn) { const response=await nulliusFetch(`/api/leaderboard/metrics?all_scores=${data.all_scores?1:0}&context=${encodeURIComponent(data.context)}`);if(!response.ok)return;const result=await response.json();if(turn!==generation||result.context!==data.context)return;result.rows.forEach(r=>{const existing=row(r.key);if(existing)existing.metrics=r.metrics;});updateTable(); }
  document.getElementById('lb-mode').onchange=async e=>{const turn=++generation;const response=await nulliusFetch(`/api/leaderboard?all_scores=${e.target.value}`);if(!response.ok)return;const next=await response.json();if(turn!==generation)return;data=next;updateTable();compareChoice().catch(()=>{});draw();deferred(turn).catch(()=>{});};
  updateTable();choose(selected);tableMotion=motion.table(table);deferred(generation).catch(()=>{});
})();
