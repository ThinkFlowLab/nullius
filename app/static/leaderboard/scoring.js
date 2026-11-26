/* Scoring settings, the Leaderboard's other view: complete-document autosave, writes in turn, the latest draft kept. */
(() => {
  const data=JSON.parse(document.getElementById('scoring-data').textContent), form=document.getElementById('scoring-form');
  const board=JSON.parse(document.getElementById('lb-data').textContent), marks=document.getElementById('scoring-marks').content;
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
  const who=r=>{const twins=board.rows.filter(v=>v.agent===r.agent&&v.version===r.version).length>1;return [r.company,r.version_label,...(twins?[r.identity.model,r.identity.effort]:[])].filter(Boolean).join(' · ');};
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
      form.querySelector(`[data-overall-share="${name}"]`).textContent=`${overall[i]}%`;
      const shares=normalized(a.entries.map(e=>e.weight));
      form.querySelectorAll(`[data-scoring-axis="${name}"] [data-share]`).forEach((n,j)=>n.textContent=`${shares[j]}%`);
    });
  }
  function report(message,error=false) {status.textContent=message;status.closest('.lb-savebar').hidden=!error;}
  function edited() {resetting=false;dirty=true;shares();clearTimeout(timer);if(!valid())return;timer=setTimeout(save,300);}
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
  function custom() {
    const box=document.getElementById('scoring-custom');box.replaceChildren();
    draft.custom.forEach(c=>{
      const item=el('div',null,{class:'lb-custom'}), head=el('div',null,{class:'lb-custom-head'});
      const name=el('input',null,{type:'text',required:'',class:'lb-input','data-custom-name':c.id,placeholder:'Name','aria-label':'Custom benchmark name'});name.value=c.name;name.oninput=()=>{c.name=name.value;edited();};
      head.append(name,el('span','Not verified',{class:'lb-tag'}),remove(`Remove ${c.name||'this custom benchmark'}`,()=>{draft.custom=draft.custom.filter(x=>x.id!==c.id);Object.values(draft.axes).forEach(a=>a.entries=a.entries.filter(e=>e.custom!==c.id));render();edited();}));
      const scores=el('div',null,{class:'lb-custom-scores'});
      board.rows.forEach(r=>{
        const line=el('label',null,{class:'lb-custom-score'}), id=el('span',null,{class:'lb-who'});
        const mark=marks.querySelector(`[data-key="${CSS.escape(r.key)}"]`)?.cloneNode(true)||el('span',null,{class:'lb-mark'});
        const text=el('span');text.append(el('b',r.name),el('small',who(r)));id.append(mark,text);
        const value=el('span',null,{class:'lb-pct'});value.append(input(c.scores[r.key],v=>c.scores[r.key]=v,{max:100,placeholder:'–','aria-label':`${c.name||'Custom benchmark'}: ${r.name} ${who(r)}`}),el('span','%'));
        line.append(id,value);scores.append(line);
      });
      item.append(head,scores);box.append(item);
    });
  }
  function render() {
    custom();
    document.querySelectorAll('[data-scoring-axis]').forEach(section=>{
      const name=section.dataset.scoringAxis, axis=draft.axes[name], container=section.querySelector('[data-entries]');container.replaceChildren();
      axis.entries.forEach((entry,index)=>{
        const line=el('div',null,{class:'lb-entry'}), choices=[...new Map(data.options.map(o=>[o.key,o.label])).entries(),...draft.custom.map(c=>['custom:'+c.id,c.name||'Custom benchmark'])];
        const n=index+1, source=select(choices,entry.custom?'custom:'+entry.custom:entry.benchmark,value=>{if(value.startsWith('custom:'))axis.entries[index]={custom:value.slice(7),weight:entry.weight};else {const b=data.options.find(b=>b.key===value);axis.entries[index]={benchmark:value,dataset:'',version:b.version,metric:'Score',weight:entry.weight};}},`${name}, benchmark ${n}`);
        line.append(cell(source));
        if(entry.custom) line.append(none(),none(),none());
        else {
          const versions=data.options.filter(o=>o.key===entry.benchmark), bench=versions.find(o=>o.version===entry.version), sets=bench?.datasets||[], metrics=bench?.metrics||['Score'];
          line.append(sets.length||entry.dataset?cell(select([['','Whole benchmark'],...sets.map(d=>[d.id,d.label])],entry.dataset,v=>entry.dataset=v,`${name}, dataset ${n}`)):none());
          line.append(versions.length>1||!bench?cell(select(versions.map(v=>[v.version,v.version_label||'–']),entry.version,v=>entry.version=v,`${name}, version ${n}`)):none());
          line.append(metrics.length>1||entry.metric!=='Score'?cell(select(metrics.map(m=>[m,m]),entry.metric,v=>entry.metric=v,`${name}, metric ${n}`)):none());
        }
        line.append(cell(input(entry.weight,v=>entry.weight=v,{required:'','aria-label':`${name}, weight ${n}`})),el('output','',{'data-share':'',class:'lb-share'}),remove(`Remove benchmark ${n} from ${name}`,()=>{axis.entries.splice(index,1);render();edited();}));
        container.append(line);
      });
      section.querySelector('[data-count]').textContent=axis.entries.length||'';
      const weight=section.querySelector('[data-overall]');weight.value=axis.weight;weight.oninput=()=>{axis.weight=weight.value===''?null:Number(weight.value);edited();};
      section.querySelector('[data-add-entry]').onclick=()=>{const b=data.options[0];if(!b)return;axis.entries.push({benchmark:b.key,dataset:'',version:b.version,metric:'Score',weight:1});render();edited();};
    });shares();
  }
  /* each axis folds its benchmarks away; open by default */
  form.querySelectorAll('.lb-fold').forEach(b=>b.onclick=()=>{const open=b.getAttribute('aria-expanded')!=='true';b.setAttribute('aria-expanded',String(open));window.NulliusDisclosure.setHidden(document.getElementById(b.getAttribute('aria-controls')),open);});
  document.getElementById('custom-add').onclick=()=>{const c={id:crypto.randomUUID(),name:'',scores:{}};draft.custom.push(c);render();edited();form.querySelector(`[data-custom-name="${c.id}"]`)?.focus();};
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
    window.scrollTo({top:0});
    document.dispatchEvent(new CustomEvent('leaderboard:view',{detail:{view}}));
  }
  link.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;e.preventDefault();show(views.scoring.hidden?'scoring':'board',true);});
  addEventListener('popstate',()=>show(location.pathname.startsWith('/scoring')?'scoring':'board',false));
})();
