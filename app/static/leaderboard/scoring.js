/* Complete-document autosave: serialize writes and retain the latest local draft. */
(() => {
  const data=JSON.parse(document.getElementById('scoring-data').textContent), form=document.getElementById('scoring-form');
  let draft=data.settings, revision=data.revision, timer, writing=false, dirty=false, resetting=false;
  const writable=form.dataset.writable==='true', status=form.querySelector('[data-changes]'), retry=form.querySelector('[data-settings-retry]');
  const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));return n;};
  const input=(value,change,attrs={})=>{const n=el('input',null,{type:'number',min:0,step:'any',...attrs});n.value=value??'';n.oninput=()=>{change(n.value===''?null:Number(n.value));edited();};return n;};
  const select=(choices,value,change)=>{const n=el('select');choices.forEach(([v,label])=>{const o=el('option',label,{value:v});o.selected=v===value;n.append(o);});if(!choices.some(([v])=>v===value))n.append(el('option',value||'Unavailable',{value,selected:''}));n.onchange=()=>{change(n.value);render();edited();};return n;};
  const labelled=(text,control)=>{const l=el('label',text);l.append(control);return l;};
  const button=(text,action)=>{const b=el('button',text,{type:'button',class:'btn ghost sm'});b.onclick=action;return b;};
  const number=v=>Number.isFinite(v)&&v>=0;
  function valid() {
    let ok=true;const names=new Set();
    form.querySelectorAll('input').forEach(n=>n.setCustomValidity(''));
    draft.custom.forEach(c=>{const normalized=c.name.trim().toLocaleLowerCase();const n=[...form.querySelectorAll('[data-custom-name]')].find(n=>n.dataset.customName===c.id);if(!normalized||names.has(normalized)){n?.setCustomValidity('Enter a unique name.');ok=false;}names.add(normalized);if(Object.values(c.scores).some(v=>v!=null&&(!number(v)||v>100)))ok=false;});
    if(!Object.values(draft.axes).some(a=>a.weight>0)) {form.querySelector('[data-overall]')?.setCustomValidity('At least one Weight in Overall must be positive.');ok=false;}
    Object.values(draft.axes).forEach(a=>{if(!number(a.weight)||a.entries.some(e=>!number(e.weight)))ok=false;});
    form.querySelectorAll('input').forEach(n=>{n.setAttribute('aria-invalid',String(!n.checkValidity()));});
    return ok&&form.checkValidity();
  }
  function shares() {
    const normalized = values => {
      const scale=Math.max(0,...values.filter(number));
      const total=scale?values.reduce((n,w)=>n+(number(w)?w/scale:0),0):0;
      return values.map(w=>total&&number(w)?Number((w/scale/total*100).toFixed(1)):0);
    };
    const overall=normalized(Object.values(draft.axes).map(a=>a.weight));
    Object.entries(draft.axes).forEach(([name,a],i)=>{
      form.querySelector(`[data-overall-share="${name}"]`).textContent=`share ${overall[i]}%`;
      const shares=normalized(a.entries.map(e=>e.weight));
      form.querySelectorAll(`[data-scoring-axis="${name}"] [data-share]`).forEach((n,j)=>n.textContent=`share ${shares[j]}%`);
    });
  }
  function report(message,error=false) {status.textContent=message;status.closest('.st-savebar').hidden=!error;}
  function edited() {resetting=false;dirty=true;shares();clearTimeout(timer);if(!valid())return;timer=setTimeout(save,300);}
  async function save(reset=false) {
    if(!writable||writing||(!reset&&(!dirty||!valid())))return;
    writing=true;dirty=false;const body=reset?{reset:true}:structuredClone(draft);
    if(reset){resetting=true;form.querySelector('fieldset').disabled=true;}
    try {
      const response=await nulliusFetch('/api/scoring',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':form.elements.csrf.value,'If-Match':revision},body:JSON.stringify(body)});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'Scoring settings could not be saved. Retry.');
      revision=result.revision;if(reset){draft=result.settings;resetting=false;render();document.getElementById('reset-confirm').hidden=true;}
      report('Scoring saved');window.nulliusNotify('Scoring saved',{key:'scoring'});
    } catch(error) {dirty=true;report(error.message,true);return;}
    finally {writing=false;if(reset){form.querySelector('fieldset').disabled=!writable;}}
    if(dirty&&valid())save();
  }
  function render() {
    const custom=document.getElementById('scoring-custom');custom.replaceChildren();
    draft.custom.forEach(c=>{
      const section=el('section'),name=el('input',null,{type:'text',required:'','data-custom-name':c.id});name.value=c.name;name.oninput=()=>{c.name=name.value;edited();};
      section.append(labelled('Name',name),button('Remove',()=>{draft.custom=draft.custom.filter(x=>x.id!==c.id);Object.values(draft.axes).forEach(a=>a.entries=a.entries.filter(e=>e.custom!==c.id));render();edited();}));
      const scores=el('div',null,{class:'scoring-custom-scores'});data.rows.forEach(r=>scores.append(labelled([r.name,r.version_label,r.identity.model,r.identity.effort].filter(Boolean).join(' · '),input(c.scores[r.key],v=>c.scores[r.key]=v,{max:100,'aria-label':`${c.name}: ${r.name} ${r.version_label}`}))));section.append(scores);custom.append(section);
    });
    document.querySelectorAll('[data-scoring-axis]').forEach(section=>{
      const axis=draft.axes[section.dataset.scoringAxis], container=section.querySelector('[data-entries]');container.replaceChildren();
      axis.entries.forEach((entry,index)=>{
        const line=el('div',null,{class:'scoring-entry'}), choices=[...new Map(data.options.map(o=>[o.key,o.label])).entries(),...draft.custom.map(c=>['custom:'+c.id,c.name])];
        const source=select(choices,entry.custom?'custom:'+entry.custom:entry.benchmark,value=>{if(value.startsWith('custom:'))axis.entries[index]={custom:value.slice(7),weight:entry.weight};else {const b=data.options.find(b=>b.key===value);axis.entries[index]={benchmark:value,dataset:'',version:b.version,metric:'Score',weight:entry.weight};}});
        line.append(labelled('Benchmark',source));
        if(entry.custom) for(const name of ['Dataset','Version','Metric'])line.append(el('span',`${name}: —`,{class:'scoring-no-choice'}));
        else {
          const versions=data.options.filter(o=>o.key===entry.benchmark), bench=versions.find(o=>o.version===entry.version);
          line.append(labelled('Dataset',select([['','Whole benchmark'],...(bench?.datasets||[]).map(d=>[d.id,d.label])],entry.dataset,v=>entry.dataset=v)));
          line.append(labelled('Version',select(versions.map(v=>[v.version,v.version_label||'—']),entry.version,v=>entry.version=v)));
          line.append(labelled('Metric',select((bench?.metrics||['Score']).map(m=>[m,m]),entry.metric,v=>entry.metric=v)));
        }
        const weight=labelled('Weight',input(entry.weight,v=>entry.weight=v,{required:''}));weight.append(el('output','',{'data-share':''}));line.append(weight,button('Remove',()=>{axis.entries.splice(index,1);render();edited();}));container.append(line);
      });
      const weight=section.querySelector('[data-overall]');weight.value=axis.weight;weight.oninput=()=>{axis.weight=weight.value===''?null:Number(weight.value);edited();};
      section.querySelector('[data-add-entry]').onclick=()=>{const b=data.options[0];if(!b)return;axis.entries.push({benchmark:b.key,dataset:'',version:b.version,metric:'Score',weight:1});render();edited();};
    });shares();
  }
  document.getElementById('custom-add').onclick=()=>{draft.custom.push({id:crypto.randomUUID(),name:'',scores:{}});render();edited();};
  document.getElementById('reset-ask').onclick=()=>document.getElementById('reset-confirm').hidden=false;
  document.getElementById('reset-no').onclick=()=>{resetting=false;document.getElementById('reset-confirm').hidden=true;};
  document.getElementById('reset-yes').onclick=()=>save(true);
  retry.onclick=()=>save(resetting);
  form.onsubmit=e=>e.preventDefault();render();
})();
