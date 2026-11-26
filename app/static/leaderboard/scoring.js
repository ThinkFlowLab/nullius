/* Scoring settings, the Leaderboard's other view: complete-document autosave, writes in turn, the latest draft kept. */
(() => {
  const data=JSON.parse(document.getElementById('scoring-data').textContent), form=document.getElementById('scoring-form');
  let draft=data.settings, revision=data.revision, timer, writing=false, dirty=false, resetting=false;
  const writable=form.dataset.writable==='true', status=form.querySelector('[data-changes]'), retry=form.querySelector('[data-settings-retry]');
  const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));return n;};
  const icon=name=>{const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('class','ic');s.setAttribute('aria-hidden','true');const u=document.createElementNS('http://www.w3.org/2000/svg','use');u.setAttribute('href','#'+name);s.append(u);return s;};
  const input=(value,change,attrs={})=>{const n=el('input',null,{type:'number',min:0,step:'any',class:'lb-input',...attrs});n.value=value??'';n.oninput=()=>{change(n.value===''?null:Number(n.value));edited();};return n;};
  const select=(choices,value,change,label)=>{const n=el('select',null,{'aria-label':label});choices.forEach(([v,text])=>{const o=el('option',text,{value:v});o.selected=v===value;n.append(o);});if(!choices.some(([v])=>v===value))n.append(el('option',value||'Unavailable',{value,selected:''}));n.onchange=()=>{change(n.value);render();edited();};return n;};
  const cell=child=>{const c=el('span',null,{class:'lb-cell'});c.append(child);return c;};
  const none=()=>el('span','–',{class:'lb-cell lb-none'});
  const remove=(label,action)=>{const b=el('button',null,{type:'button',class:'btn ghost sm lb-icon','aria-label':label,title:'Remove'});b.append(icon('i-x'));b.onclick=action;return b;};
  const number=v=>Number.isFinite(v)&&v>=0;
  function valid() {
    let ok=true;
    form.querySelectorAll('input').forEach(n=>n.setCustomValidity(''));
    if(!Object.values(draft.axes).some(a=>a.weight>0)) {form.querySelector('[data-overall]')?.setCustomValidity('At least one Weight in Overall must be positive.');ok=false;}
    Object.values(draft.axes).forEach(a=>{if(!number(a.weight)||a.entries.some(e=>!number(e.weight)))ok=false;});
    form.querySelectorAll('input').forEach(n=>{n.setAttribute('aria-invalid',String(!n.checkValidity()));});
    return ok&&form.checkValidity();
  }
  /* each section opens on an overview: every benchmark's share of each of its axes */
  const names=section=>[...section.querySelectorAll('[data-scoring-axis]')].map(n=>n.dataset.scoringAxis);
  const keyOf=e=>e.benchmark;
  const labelOf=key=>data.options.find(o=>o.key===key)?.label||key;
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
  function report(message,error=false) {status.textContent=message;status.closest('.lb-savebar').hidden=!error;}
  function edited() {resetting=false;dirty=true;overview();clearTimeout(timer);if(!valid())return;timer=setTimeout(save,300);}
  async function save(reset=false) {
    if(!writable||writing||(!reset&&(!dirty||!valid())))return;
    writing=true;dirty=false;const body=reset?{reset:true}:structuredClone(draft);
    if(reset){resetting=true;form.querySelector('fieldset').disabled=true;}
    try {
      const response=await nulliusFetch('/api/scoring',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':form.elements.csrf.value,'If-Match':revision},body:JSON.stringify(body)});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'Scoring settings could not be saved. Retry.');
      revision=result.revision;if(reset){draft=result.settings;resetting=false;render();document.getElementById('reset-confirm').hidden=true;document.getElementById('reset-ask').hidden=false;}
      report('Scoring saved');window.nulliusNotify('Scoring saved',{key:'scoring'});
      document.dispatchEvent(new Event('leaderboard:rescored'));
    } catch(error) {dirty=true;report(error.message,true);return;}
    finally {writing=false;if(reset){form.querySelector('fieldset').disabled=!writable;}}
    if(dirty&&valid())save();
  }
  function render() {
    document.querySelectorAll('[data-scoring-axis]').forEach(section=>{
      const name=section.dataset.scoringAxis, axis=draft.axes[name], container=section.querySelector('[data-entries]');container.replaceChildren();
      axis.entries.forEach((entry,index)=>{
        const line=el('div',null,{class:'lb-entry'}), choices=[...new Map(data.options.map(o=>[o.key,o.label])).entries()];
        const n=index+1, source=select(choices,entry.benchmark,value=>{const b=data.options.find(b=>b.key===value);axis.entries[index]={benchmark:value,dataset:'',version:b.version,metric:'Score',weight:entry.weight};},`${name}, benchmark ${n}`);
        line.append(cell(source));
        const versions=data.options.filter(o=>o.key===entry.benchmark), bench=versions.find(o=>o.version===entry.version), sets=bench?.datasets||[], metrics=bench?.metrics||['Score'];
        line.append(sets.length||entry.dataset?cell(select([['','Whole benchmark'],...sets.map(d=>[d.id,d.label])],entry.dataset,v=>entry.dataset=v,`${name}, dataset ${n}`)):none());
        line.append(versions.length>1||!bench?cell(select(versions.map(v=>[v.version,v.version_label||'–']),entry.version,v=>entry.version=v,`${name}, version ${n}`)):none());
        line.append(metrics.length>1||entry.metric!=='Score'?cell(select(metrics.map(m=>[m,m]),entry.metric,v=>entry.metric=v,`${name}, metric ${n}`)):none());
        line.append(cell(input(entry.weight,v=>entry.weight=v,{required:'','aria-label':`${name}, weight ${n}`})),remove(`Remove benchmark ${n} from ${name}`,()=>{axis.entries.splice(index,1);if(!axis.entries.length)axis.weight=0;render();edited();}));
        container.append(line);
      });
      section.querySelector('[data-count]').textContent=axis.entries.length;
      const weight=section.querySelector('[data-overall]');if(weight){weight.value=axis.weight;weight.disabled=!axis.entries.length;weight.title=axis.entries.length?'':'Add a benchmark first';weight.oninput=()=>{axis.weight=weight.value===''?null:Number(weight.value);edited();};}
      section.querySelector('[data-add-entry]').onclick=()=>{const b=data.options[0];if(!b)return;
        /* an empty axis weighs 0 in Overall; its first benchmark brings back the axis's standard weight */
        if(!axis.entries.length&&!axis.weight)axis.weight=data.overall?.[name]||0;
        axis.entries.push({benchmark:b.key,dataset:'',version:b.version,metric:'Score',weight:1});render();edited();};
    });overview();
  }
  /* each axis folds its benchmarks away; rows open folded, the overview open */
  form.querySelectorAll('.lb-fold').forEach(b=>b.onclick=()=>{const open=b.getAttribute('aria-expanded')!=='true';b.setAttribute('aria-expanded',String(open));window.NulliusDisclosure.setHidden(document.getElementById(b.getAttribute('aria-controls')),open);});
  document.getElementById('reset-ask').onclick=()=>{document.getElementById('reset-confirm').hidden=false;document.getElementById('reset-ask').hidden=true;document.getElementById('reset-no').focus();};
  document.getElementById('reset-no').onclick=()=>{resetting=false;document.getElementById('reset-confirm').hidden=true;document.getElementById('reset-ask').hidden=false;document.getElementById('reset-ask').focus();};
  document.getElementById('reset-yes').onclick=()=>save(true);
  retry.onclick=()=>save(resetting);
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
