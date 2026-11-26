/* Persistent SVG nodes and cancellable geometry transitions for the composite view. */
window.LeaderboardMotion = (() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const active = new Map(), timing = {duration:320, easing:'cubic-bezier(.2,.7,.2,1)'};
  const numeric = new Set(['x','y','cx','cy','x1','y1','x2','y2','r','data-cx','data-cy']);
  function cancel(node, finish=false) { const old=active.get(node);if(old){old.cancel(finish);active.delete(node);} }
  function fade(node, frames, duration=320, delay=0) {
    cancel(node,true);if(reduced.matches)return;
    const animation=node.animate(frames,{...timing,duration,delay});
    const controller={cancel:()=>animation.cancel()};active.set(node,controller);
    animation.finished.then(()=>{if(active.get(node)===controller)active.delete(node);},()=>{});
  }
  /* a table cell's contents fade, never the cell: its background is the row's colour */
  function contents(cell, show=true, duration=240) {
    if(reduced.matches)return [];
    const options={duration,easing:show?'ease-out':'ease-in',fill:show?'none':'forwards'};
    return [cell.animate(show?[{color:'transparent'},{}]:[{},{color:'transparent'}],options),
            ...[...cell.children].map(child=>child.animate(show?[{opacity:0},{opacity:1}]:[{opacity:1},{opacity:0}],options))];
  }
  function geometry(node, from, to, delay=0) {
    cancel(node);const keys=Object.keys(to).filter(k=>from[k]!=null && from[k]!==to[k]);
    const set=values=>Object.entries(values).forEach(([k,v])=>node.setAttribute(k,v));
    if(!keys.length||reduced.matches){set(to);return;}
    const pairs=keys.map(k=>[k,Number(from[k]),Number(to[k])]).filter(([,a,b])=>Number.isFinite(a)&&Number.isFinite(b));
    if(!pairs.length){set(to);return;}
    set(from);
    const animation=node.animate([{opacity:.7},{opacity:1}],{...timing,delay});
    let frame, start=null;
    const controller={cancel:finish=>{cancelAnimationFrame(frame);animation.cancel();if(finish)set(to);}};
    active.set(node,controller);
    const draw=now=>{
      start??=now;const t=Math.max(0,Math.min(1,(now-start-delay)/timing.duration));
      // Use the Web Animation's computed easing for the same curve as table motion.
      const p=animation.effect.getComputedTiming().progress ?? t;
      pairs.forEach(([k,a,b])=>node.setAttribute(k,a+(b-a)*p));
      if(t<1)frame=requestAnimationFrame(draw);
      else {set(to);animation.cancel();if(active.get(node)===controller)active.delete(node);}
    };
    frame=requestAnimationFrame(draw);
  }
  function key(node, index) { return node.nodeType===1 ? node.getAttribute('data-node') || `${node.tagName}:${index}` : `text:${index}`; }
  function children(live, next) {
    const old=new Map([...live.childNodes].map((n,i)=>[key(n,i),n]));
    let cursor=live.firstChild;
    [...next.childNodes].forEach((fresh,i)=>{
      const id=key(fresh,i);let node=old.get(id);
      if(!node||node.nodeType!==fresh.nodeType||node.nodeName!==fresh.nodeName)node=fresh;
      else {
        old.delete(id);
        if(node.nodeType===3)node.textContent=fresh.textContent;
        else {
          const from={},to={};
          for(const a of fresh.attributes)if(numeric.has(a.name)){from[a.name]=node.getAttribute(a.name);to[a.name]=a.value;}
          cancel(node);
          for(const a of [...node.attributes])if(!fresh.hasAttribute(a.name))node.removeAttribute(a.name);
          for(const a of fresh.attributes)node.setAttribute(a.name,a.value);
          children(node,fresh);geometry(node,from,to);
        }
      }
      if(node!==cursor)live.insertBefore(node,cursor);
      cursor=node.nextSibling;
    });
    old.forEach(n=>{n.querySelectorAll?.('*').forEach(x=>cancel(x));cancel(n);n.remove();});
  }
  function morph(live,next) {
    const focus=document.activeElement,axis=focus?.dataset?.axis;
    children(live,next);
    if(focus!==document.activeElement){
      if(focus.isConnected)focus.focus({preventScroll:true});
      else if(axis)live.querySelector(`text[data-axis="${axis}"]`)?.focus({preventScroll:true});
    }
  }
  function reveal(plot,kind) {
    if(reduced.matches)return;
    const observer=new IntersectionObserver(entries=>{
      if(!entries.some(e=>e.isIntersecting))return;observer.disconnect();
      plot.querySelectorAll('[data-reveal]').forEach(node=>{
        if(kind==='strips')fade(node,[{transform:'translateX(-24px)',opacity:0},{transform:'translateX(0)',opacity:1}]);
        else {
          const from={},to={};
          for(const k of ['cx','cy','x1','y1','x2','y2'])if(node.hasAttribute(k)){to[k]=node.getAttribute(k);from[k]=k.startsWith('x')||k==='cx'?Number(plot.dataset.cx??180):Number(plot.dataset.cy??155);}
          geometry(node,from,to,Number(node.dataset.reveal)*35);
        }
      });
    });observer.observe(plot);
  }
  function table(table) {
    const measure=()=>new Map([...table.querySelectorAll('tr,th,td')].map(n=>[n,{rect:n.getBoundingClientRect(),text:n.textContent}]));
    const box=table.closest('.table-wrap')||table;
    let previous=measure(),height=box.offsetHeight,frame,easing=null;
    const capture=()=>{previous=measure();height=easing?height:box.offsetHeight;};
    /* rows coming or going change the table's height: it eases to its new height instead of jumping */
    const ease=(from,to)=>{
      if(reduced.matches||Math.abs(from-to)<2)return;
      easing?.cancel();box.style.overflowY='hidden';
      easing=box.animate([{height:`${from}px`},{height:`${to}px`}],{duration:280,easing:'cubic-bezier(.2,.7,.2,1)'});
      const done=easing;done.finished.then(()=>{if(easing===done){easing=null;box.style.overflowY='';}},()=>{});
    };
    const update=()=>{
      cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{
        const next=measure(),now=box.offsetHeight;ease(height,now);height=now;
        /* nothing in a table is moved by a transform, which some engines draw blank: a row that moved, a cell that
           appears or one whose text changed fades in where it now is */
        next.forEach(({rect,text},node)=>{
          const old=previous.get(node);if(!old||!rect.width||!rect.height)return;
          const appeared=!old.rect.width||!old.rect.height;
          if(node.tagName==='TR'){if(!appeared&&Math.abs(old.rect.y-rect.y)>.5)[...node.cells].forEach(cell=>contents(cell,true,260));return;}
          if(appeared||old.text!==text)contents(node);
        });previous=next;
      });
    };
    new MutationObserver(update).observe(table,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden','style']});
    for(const event of ['click','input','change','keydown'])document.addEventListener(event,e=>{
      if(e.target.closest(`#${table.id}, #${table.id}-tools`))capture();
    },true);
    return {capture};
  }
  reduced.addEventListener('change',()=>{if(reduced.matches)[...active.keys()].forEach(n=>cancel(n,true));});
  return {morph,reveal,table,fade,contents,reduced};
})();
