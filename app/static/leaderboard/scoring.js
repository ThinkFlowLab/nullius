/* Scoring settings, the Leaderboard's other view: complete-document autosave, writes in turn, the latest draft kept. */
(() => {
  const data=JSON.parse(document.getElementById('scoring-data').textContent), form=document.getElementById('scoring-form');
  let draft=data.settings, revision=data.revision, timer, writing=false, dirty=false, resetting=false, queued=false;
  const writable=form.dataset.writable==='true', status=form.querySelector('[data-changes]'), retry=form.querySelector('[data-settings-retry]'), bar=status.closest('.lb-savebar');
  const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));return n;};
  const icon=name=>{const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('class','ic');s.setAttribute('aria-hidden','true');const u=document.createElementNS('http://www.w3.org/2000/svg','use');u.setAttribute('href','#'+name);s.append(u);return s;};
  const input=(value,change,attrs={})=>{const n=el('input',null,{type:'number',min:0,step:'any',class:'lb-input',...attrs});n.value=value??'';n.oninput=()=>{change(n.value===''?null:Number(n.value));edited();};return n;};
  const select=(choices,value,change,label)=>{const n=el('select',null,{'aria-label':label});choices.forEach(([v,text])=>{const o=el('option',text,{value:v});o.selected=v===value;n.append(o);});if(!choices.some(([v])=>v===value))n.append(el('option',value||'Unavailable',{value,selected:''}));n.onchange=()=>{const held=document.activeElement===n.nextElementSibling;change(n.value);render();edited();if(held)refocus(label);};return n;};
  /* a select that changed is drawn again with its row: focus goes to the one that took its place, once its button is built */
  const refocus=label=>requestAnimationFrame(()=>requestAnimationFrame(()=>[...form.querySelectorAll('select')].find(n=>n.getAttribute('aria-label')===label)?.nextElementSibling?.focus()));
  const cell=child=>{const c=el('span',null,{class:'lb-cell'});c.append(child);return c;};
  const none=()=>el('span','–',{class:'lb-cell lb-none'});
  const remove=(label,action)=>{const b=el('button',null,{type:'button',class:'btn ghost sm lb-icon','aria-label':label,title:'Remove'});b.append(icon('i-x'));b.onclick=action;return b;};
  const number=v=>Number.isFinite(v)&&v>=0;
  /* each section opens on an overview: every benchmark's share of each of its axes */
  const names=section=>[...section.querySelectorAll('[data-scoring-axis]')].map(n=>n.dataset.scoringAxis);
  const keyOf=e=>e.benchmark;
  const labelOf=key=>data.options.find(o=>o.key===key)?.label||key;
  /* a benchmark's default version: the one a new row reads */
  const versionOf=key=>data.options.find(o=>o.key===key&&o.default)||data.options.find(o=>o.key===key);
  const identity=e=>[e.benchmark,e.dataset,e.version,e.metric].join('\n');
  const families=[...form.querySelectorAll('section.lb-axes')].map(section=>({title:section.querySelector('h2').textContent,names:names(section)}));
  /* what keeps the draft from being saved: a field marked where it is wrong, or one line saying what to do.
     Each family has its own Overall, so each needs a weighted row among those that have a benchmark. */
  function check() {
    form.querySelectorAll('input').forEach(n=>{n.setCustomValidity('');n.setAttribute('aria-invalid',String(!n.checkValidity()));});
    if(!form.checkValidity()||Object.values(draft.axes).some(a=>!number(a.weight)||a.entries.some(e=>!number(e.weight))))return {ok:false,message:''};
    for(const [name,axis] of Object.entries(draft.axes)){const seen=new Set();for(const e of axis.entries){if(seen.has(identity(e)))return {ok:false,message:`${name}: ${labelOf(e.benchmark)} is listed twice. Change or remove one.`};seen.add(identity(e));}}
    for(const family of families){const mapped=family.names.filter(n=>draft.axes[n].entries.length);if(mapped.length&&!mapped.some(n=>draft.axes[n].weight>0))return {ok:false,message:`${family.title}: give at least one row a weight above 0.`};}
    if(!Object.values(draft.axes).some(a=>a.weight>0))return {ok:false,message:'Give at least one row a weight above 0.'};
    return {ok:true,message:''};
  }
  const shareIn=(axis,key)=>{const a=draft.axes[axis],total=a.entries.reduce((n,e)=>n+(number(e.weight)?e.weight:0),0);return total?a.entries.filter(e=>keyOf(e)===key).reduce((n,e)=>n+(number(e.weight)?e.weight:0),0)/total:0;};
  function overview() {
    form.querySelectorAll('[data-overview]').forEach(table=>{
      const mine=names(table.closest('section'));
      const keys=[...new Set(mine.flatMap(a=>draft.axes[a].entries.map(keyOf)))];
      const head=el('tr');head.append(el('th','Benchmark'),...mine.map(a=>el('th',a,{class:'lb-ov-num'})));
      const body=el('tbody');
      keys.forEach(key=>{
        const tr=el('tr');tr.append(el('td',labelOf(key)));
        mine.forEach(a=>{const v=shareIn(a,key),td=el('td',null,{class:'lb-ov-num'});if(v>0){const chip=el('span',v.toFixed(2),{class:'lb-ov-chip'});chip.style.setProperty('--v',v.toFixed(3));td.append(chip);}else td.append(el('span','·',{class:'lb-ov-none'}));tr.append(td);});
        body.append(tr);
      });
      if(!keys.length){const tr=el('tr');tr.append(el('td','No benchmark',{colspan:mine.length+1,class:'lb-ov-none'}));body.append(tr);}
      const thead=el('thead');thead.append(head);table.replaceChildren(thead,body);
    });
  }
  /* the bar under the form: a save that failed offers Retry, one refused because the settings changed elsewhere offers
     Reload, and what keeps the draft from being saved is said with no button */
  const FAILED='Scoring settings could not be saved. Retry.';
  function report(message,kind='') {status.textContent=message;bar.hidden=!kind;bar.dataset.kind=kind;retry.hidden=kind==='invalid';retry.textContent=kind==='conflict'?'Reload':'Retry';}
  function edited() {
    resetting=false;dirty=true;overview();clearTimeout(timer);
    const {ok,message}=check();
    if(message)report(message,'invalid');else if(bar.dataset.kind==='invalid')report('');
    if(ok)timer=setTimeout(save,300);
  }
  async function save(reset=false) {
    if(!writable)return;
    if(writing){queued=queued||reset;return;}                           // a reset asked during a write runs after it
    if(!reset&&(!dirty||!check().ok))return;
    writing=true;dirty=false;const body=reset?{reset:true}:structuredClone(draft);
    if(reset){resetting=true;form.querySelector('fieldset').disabled=true;}
    let failed=false;
    try {
      const response=await nulliusFetch('/api/scoring',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':form.elements.csrf.value,'If-Match':revision},body:JSON.stringify(body)});
      const result=await response.json().catch(()=>({}));
      if(!response.ok||!result.revision){const refused=new Error(result.error||FAILED);refused.kind=response.status===409?'conflict':'error';throw refused;}
      revision=result.revision;if(reset){draft=result.settings;resetting=false;render();document.getElementById('reset-confirm').hidden=true;document.getElementById('reset-ask').hidden=false;}
      report('Scoring saved');window.nulliusNotify('Scoring saved',{key:'scoring'});
      document.dispatchEvent(new Event('leaderboard:rescored'));
    } catch(error) {dirty=true;failed=true;report(error.kind?error.message:FAILED,error.kind||'error');}
    finally {writing=false;if(reset){form.querySelector('fieldset').disabled=!writable;}}
    if(queued){queued=false;save(true);}
    else if(!failed&&dirty&&check().ok)save();
  }
  function render() {
    document.querySelectorAll('[data-scoring-axis]').forEach(section=>{
      const name=section.dataset.scoringAxis, axis=draft.axes[name], container=section.querySelector('[data-entries]');container.replaceChildren();
      axis.entries.forEach((entry,index)=>{
        const line=el('div',null,{class:'lb-entry'}), choices=[...new Map(data.options.map(o=>[o.key,o.label])).entries()];
        const n=index+1, source=select(choices,entry.benchmark,value=>{axis.entries[index]={benchmark:value,dataset:'',version:versionOf(value).version,metric:'Score',weight:entry.weight};},`${name}, benchmark ${n}`);
        line.append(cell(source));
        const versions=data.options.filter(o=>o.key===entry.benchmark), bench=versions.find(o=>o.version===entry.version), sets=bench?.datasets||[], metrics=bench?.metrics||['Score'];
        line.append(sets.length||entry.dataset?cell(select([['','Whole benchmark'],...sets.map(d=>[d.id,d.label])],entry.dataset,v=>entry.dataset=v,`${name}, dataset ${n}`)):none());
        line.append(versions.length>1||!bench?cell(select(versions.map(v=>[v.version,v.version_label||'–']),entry.version,v=>entry.version=v,`${name}, version ${n}`)):none());
        line.append(metrics.length>1||entry.metric!=='Score'?cell(select(metrics.map(m=>[m,m]),entry.metric,v=>entry.metric=v,`${name}, metric ${n}`)):none());
        line.append(cell(input(entry.weight,v=>entry.weight=v,{required:'','aria-label':`${name}, weight ${n}`})),remove(`Remove benchmark ${n} from ${name}`,()=>{axis.entries.splice(index,1);if(!axis.entries.length)axis.weight=0;render();edited();(container.querySelectorAll('.lb-entry > .lb-icon')[index]||section.querySelector('[data-add-entry]')).focus();}));
        container.append(line);
      });
      section.querySelector('[data-count]').textContent=axis.entries.length;
      const weight=section.querySelector('[data-overall]');if(weight){weight.value=axis.weight;weight.disabled=!axis.entries.length;weight.title=axis.entries.length?'':'Add a benchmark first';weight.oninput=()=>{axis.weight=weight.value===''?null:Number(weight.value);edited();};}
      section.querySelector('[data-add-entry]').onclick=()=>{
        /* a new row reads the first benchmark the axis does not list yet, as a whole; when it lists them all, the first */
        const whole=o=>({benchmark:o.key,dataset:'',version:o.version,metric:'Score',weight:1}), listed=new Set(axis.entries.map(identity));
        const firsts=[...new Set(data.options.map(o=>o.key))].map(versionOf), fresh=firsts.map(whole).find(e=>!listed.has(identity(e)))||(firsts[0]&&whole(firsts[0]));
        if(!fresh)return;
        /* an empty axis weighs 0 in Overall; its first benchmark brings back the axis's standard weight */
        if(!axis.entries.length&&!axis.weight)axis.weight=data.overall?.[name]||0;
        axis.entries.push(fresh);render();edited();};
    });overview();
  }
  /* each axis folds its benchmarks away; rows open folded, the overview open */
  form.querySelectorAll('.lb-fold').forEach(b=>b.onclick=()=>{const open=b.getAttribute('aria-expanded')!=='true';b.setAttribute('aria-expanded',String(open));window.NulliusDisclosure.setHidden(document.getElementById(b.getAttribute('aria-controls')),open);});
  document.getElementById('reset-ask').onclick=()=>{document.getElementById('reset-confirm').hidden=false;document.getElementById('reset-ask').hidden=true;document.getElementById('reset-no').focus();};
  document.getElementById('reset-no').onclick=()=>{resetting=false;document.getElementById('reset-confirm').hidden=true;document.getElementById('reset-ask').hidden=false;document.getElementById('reset-ask').focus();};
  document.getElementById('reset-yes').onclick=()=>save(true);
  retry.onclick=()=>bar.dataset.kind==='conflict'?location.reload():save(resetting);
  form.onsubmit=e=>e.preventDefault();render();

  /* the switch between the two views: no page load; the address follows, so a reload or Back lands on the same view */
  const views={board:document.getElementById('lb-board-view'),scoring:form}, link=document.getElementById('lb-switch'), title=document.getElementById('lb-title');
  const paths={board:'/leaderboard',scoring:'/scoring'};
  function show(view, push) {
    const other=view==='scoring'?'board':'scoring';
    views[view].hidden=false;views[other].hidden=true;
    title.textContent=view==='scoring'?'Scoring settings':'Leaderboard';document.title=`${title.textContent} · Nullius`;
    link.textContent=view==='scoring'?'Leaderboard':'Scoring settings';link.href=paths[other];
    if(push)history.pushState({view},'',paths[view]);
    document.dispatchEvent(new CustomEvent('leaderboard:view',{detail:{view}}));
  }
  link.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;e.preventDefault();show(views.scoring.hidden?'scoring':'board',true);});
  addEventListener('popstate',()=>show(location.pathname.startsWith('/scoring')?'scoring':'board',false));
})();
