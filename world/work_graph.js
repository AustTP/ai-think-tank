/* Press Room: task lineage and dependency explorer. No external graph service. */
(function (root) {
  'use strict';
  const active = new Set(['working', 'walking', 'repairing', 'investigating', 'needs_review']);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list = value => Array.isArray(value) ? value : value ? [value] : [];
  const stamp = task => Number(task.createdAt || task.queuedAt || task.filedAt || 0);
  const hash = text => { let h = 2166136261; for (const c of text) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return (h >>> 0).toString(36); };
  const projectKey = task => task.workOrderId || task.requestId || task.productId || task.projectLabel || 'unassigned';
  const parentId = task => task.reviewOf || task.investigationOf || task.recoveryOf || (task.rootStoryId !== task.id && task.rootStoryId);
  const originalTasks = graph => graph.nodes.filter(task => !parentId(task)
    && !['review', 'qa', 'code_preflight', 'investigation', 'repair'].includes(task.taskType)
    && !/^\s*(?:fix|repair|code check|review)\s*:/i.test(task.title || ''));
  function originalId(graph, id) {
    const seen = new Set();
    let node = graph.nodes.find(n => n.id === id);
    while (node && parentId(node) && !seen.has(node.id)) {
      seen.add(node.id);
      const parent = graph.nodes.find(n => n.id === parentId(node));
      if (!parent) break;
      node = parent;
    }
    return node?.id || id;
  }
  function taskProgress(node) {
    const latest = new Map();
    for (const review of (node.peerReviews || []).slice().sort((a,b)=>(a.atMs||0)-(b.atMs||0)))
      latest.set(review.reviewer || review.reviewerId || review.agentId || 'reviewer', review);
    const roster = node._peerGate?.reviewerIds || [];
    let current = [...latest.values()].filter(r=>!roster.length || roster.includes(r.reviewer || r.reviewerId || r.agentId));
    current = current.sort((a,b)=>(b.atMs||0)-(a.atMs||0)).slice(0,2);
    const reviews = current.filter(r=>r.acceptanceAssessment?.checks?.length);
    const gate=node._peerGate || {};
    const modernAccepted=reviews.length>=2 && reviews.every(r=>r.acceptanceAssessment.verified===true);
    const legacyAccepted=gate.closed===true && new Set(gate.approvers || []).size>=2
      && current.length>=2 && current.every(r=>r.verdict==='clean' && !r.acceptanceAssessment)
      && (node.room!=='pressoffice' && node.taskType!=='code' || Boolean(node._codeDirectorPassed?.evidence));
    const accepted = node.status==='done' && Number(gate.approvals)>=2 && !gate.escalated
      && gate.acceptanceVerified!==false && (modernAccepted || legacyAccepted);
    if(accepted)return {percent:100,label:'Accepted',at:node.completedAt,reason:'Quality checks and independent acceptance reviews passed.'};
    if(!reviews.length){
      const planned=['planned','potential','queued'].includes(node.status);
      return {percent:planned?0:null,label:planned?'Not started':'Not yet assessed',at:null,
        reason:planned?'No implementation has started.':'No current criterion-based reviewer estimate is available. Work may already exist; this is not a zero-percent assessment.'};
    }
    const criteria=new Map();
    for(const review of reviews)for(const check of review.acceptanceAssessment.checks){
      const evidence=check.evidence?.length && check.evidence.every(e=>typeof e==='string' && e.trim());
      const credit=evidence && check.explanation ? check.status==='met'?100:check.status==='unmet' && Number.isFinite(check.completionPercent)?Math.max(0,Math.min(99,check.completionPercent)):0:0;
      const key=check.criterionId;
      criteria.set(key,Math.min(criteria.get(key)??100,credit));
    }
    // Missing criteria never disappear from the denominator.
    const required=Number(Boolean(node.reviewContract?.instructions || node.instructions))+Number(Boolean(node.reviewContract?.acceptanceCriteria || node.acceptanceCriteria));
    const denominator=Math.max(required,criteria.size,1);
    return {percent:Math.min(99,Math.floor([...criteria.values()].reduce((a,b)=>a+b,0)/denominator)),
      label:'Reviewer estimate',at:Math.max(...reviews.map(r=>r.atMs||0)),
      reason:'Equal weight per acceptance criterion using the current reviewers’ latest estimates. Changes since this review await reassessment.'};
  }
  function groupProgress(graph,type,id) {
    const members=originalTasks(graph).filter(n=>type==='task'?n.id===id:type==='project'?n.projectKey===id||n.productId===id:n.sprintId===id);
    const scores=members.map(taskProgress),accepted=scores.filter(s=>s.percent===100).length;
    const known=scores.filter(s=>s.percent!==null);
    const percent=known.length?Math.floor(scores.reduce((sum,s)=>sum+(s.percent || 0),0)/scores.length):null;
    return {percent:accepted===members.length&&members.length?100:percent===null?null:Math.min(99,percent),total:members.length,accepted,
      lowerBound:known.length>0 && known.length<scores.length,
      unassessed:scores.filter(s=>s.label==='Not yet assessed').length};
  }
  const progressText = progress => progress.percent===null ? 'Awaiting assessment' : `${progress.lowerBound?'At least ':''}${progress.percent}%`;

  function build(snapshot) {
    const nodes = new Map(), edges = [], edgeKeys = new Set(), anchors = new Map();
    const requests = new Map((snapshot.backlogRequests || []).map(r => [r.id, r]));
    const add = (source, target, kind, label) => {
      const key = `${source}|${target}|${kind}`;
      if (source !== target && nodes.has(source) && nodes.has(target) && !edgeKeys.has(key)) {
        edges.push({source, target, kind, label}); edgeKeys.add(key);
      }
    };
    const tasks = Object.values(snapshot.tasks || {}).filter(t => t && t.id);
    for (const task of tasks) nodes.set(task.id, {...task, projectKey: projectKey(task), source: 'task'});
    for (const task of tasks.slice().sort((a,b) => stamp(a)-stamp(b))) {
      if (task.workOrderId && task.workOrderStoryId && !task.reviewOf && !task.investigationOf && !task.recoveryOf)
        anchors.set(`${task.workOrderId}:${task.workOrderStoryId}`, task.id);
    }
    for (const request of requests.values()) for (const story of request.plan?.stories || []) {
      const key = `${request.id}:${story.id}`;
      if (anchors.has(key)) continue;
      const id = `plan:${key}`;
      nodes.set(id, {...story, id, title: story.title, status: story.potential && !story.activated ? 'potential' : 'planned',
        workOrderId: request.id, workOrderStoryId: story.id, projectKey: request.id, source: 'plan',
        assignedTo: request.scrumMasterId, planningStatus: story.planningStatus});
      anchors.set(key, id);
    }
    for (const item of snapshot.workQueue || []) {
      const structural = item.reviewOf || item.investigationOf || item.recoveryOf;
      const anchor = !structural && anchors.get(`${item.workOrderId}:${item.workOrderStoryId}`);
      const id = anchor || item.id || `queue:${hash(JSON.stringify([item.title,item.assignedTo,item.taskType,item.reviewOf,item.investigationOf,item.recoveryOf,item.workOrderId,item.workOrderStoryId,item.dependsOn,item.notBefore]))}`;
      const existing = nodes.get(id);
      if (!existing || existing.source === 'plan') nodes.set(id, {...existing, ...item, id, status:'queued', source:'queue',
        projectKey:projectKey(item)});
    }
    for(const [sid,sprint] of Object.entries(snapshot.sprints || {})) for(const rawItem of sprint.items || []) {
      const item=Array.isArray(rawItem)?{title:rawItem[0],room:rawItem[1]}:rawItem;
      if(!item?.title)continue;
      const match=[...nodes.values()].find(n=>n.sprintId===sid && n.title===item.title && (!item.room || n.room===item.room));
      if(match)continue;
      const linked=nodes.get(item.taskId);
      if(linked){linked.sprintId=sid;continue;}
      const id=`sprint-plan:${sid}:${hash(JSON.stringify([item.title,item.room,item.assignedTo]))}`;
      nodes.set(id,{...item,id,sprintId:sid,source:'plan',status:sprint.status==='closed'?'recorded':'planned',
        projectKey:projectKey(item)==='unassigned'?'sprint:'+sid:projectKey(item),projectLabel:item.projectLabel||sprint.name||sprint.goal,
        planningStatus:'Sprint item; no executed task is linked yet.'});
    }
    // A repair follows its parent's project even when historical metadata is absent.
    for (let pass=0; pass<3; pass++) for (const node of nodes.values()) {
      const parent = nodes.get(node.reviewOf || node.investigationOf || node.recoveryOf || node.rootStoryId);
      if (parent && parent.id !== node.id) {
        if (!node.workOrderId && !node.requestId && !node.productId) node.projectKey = parent.projectKey;
        if (!node.sprintId) node.sprintId = parent.sprintId;
      }
    }
    for (const node of nodes.values()) {
      for (const dep of list(node.dependsOn)) add(dep, node.id, 'dependency', 'Prerequisite');
      for (const dep of list(node.dependsOnTaskIds)) add(dep, node.id, 'dependency', 'Prerequisite');
      const parent = node.reviewOf || node.investigationOf || node.recoveryOf || (node.rootStoryId !== node.id && node.rootStoryId);
      if (parent) add(parent, node.id, 'lineage', ['review','qa'].includes(node.taskType) ? 'Review' : node.taskType === 'code_preflight' ? 'Validation' : node.investigationOf ? 'Investigation' : 'Repair');
    }
    for (const request of requests.values()) for (const story of request.plan?.stories || []) {
      const target = anchors.get(`${request.id}:${story.id}`);
      for (const dep of story.prerequisiteStoryIds || []) add(anchors.get(`${request.id}:${dep}`), target, 'dependency', 'Prerequisite');
    }
    const groups = new Map();
    for (const node of nodes.values()) if (stamp(node) && node.source === 'task') {
      const key = node.projectKey;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    }
    for (const group of groups.values()) {
      group.sort((a,b) => stamp(a)-stamp(b) || a.id.localeCompare(b.id));
      for (let i=1;i<group.length;i++) add(group[i-1].id, group[i].id, 'chronology', 'Time order');
    }
    const projects = new Map();
    for (const node of nodes.values()) {
      const request = requests.get(node.projectKey);
      const product = snapshot.products?.[node.projectKey];
      projects.set(node.projectKey, request?.title || product?.name || node.projectLabel || node.projectKey);
      if(node.productId)projects.set(node.productId,snapshot.products?.[node.productId]?.name || node.productId);
    }
    return {nodes:[...nodes.values()], edges, projects, snapshot};
  }

  function scope(graph, type, value, chronology) {
    let selected = new Set();
    if (type === 'task') {
      selected.add(value);
      const adjacent = new Map();
      for (const edge of graph.edges.filter(e => e.kind !== 'chronology')) {
        if (!adjacent.has(edge.source)) adjacent.set(edge.source, []);
        if (!adjacent.has(edge.target)) adjacent.set(edge.target, []);
        adjacent.get(edge.source).push(edge.target); adjacent.get(edge.target).push(edge.source);
      }
      const queue=[value];
      for (let i=0;i<queue.length;i++) for (const id of adjacent.get(queue[i]) || []) if (!selected.has(id)) { selected.add(id); queue.push(id); }
    } else {
      for (const node of graph.nodes) if (type === 'project' ? node.projectKey === value || node.productId === value : node.sprintId === value) selected.add(node.id);
      const members=new Set(selected);
      for(const edge of graph.edges.filter(e=>e.kind!=='chronology')) {
        if(members.has(edge.source))selected.add(edge.target);
        if(members.has(edge.target))selected.add(edge.source);
      }
    }
    return {nodes:graph.nodes.filter(n => selected.has(n.id)), edges:graph.edges.filter(e => selected.has(e.source) && selected.has(e.target) && (chronology || e.kind !== 'chronology'))};
  }

  function layout(graph) {
    const incoming = new Map(graph.nodes.map(n => [n.id,0])), outgoing = new Map(), levels = new Map();
    for (const edge of graph.edges.filter(e => e.kind !== 'chronology')) {
      incoming.set(edge.target, incoming.get(edge.target)+1);
      if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
      outgoing.get(edge.source).push(edge.target);
    }
    const queue=graph.nodes.filter(n => !incoming.get(n.id)).map(n => n.id);
    for (const id of queue) levels.set(id,0);
    for (let i=0;i<queue.length;i++) for (const target of outgoing.get(queue[i]) || []) {
      levels.set(target,Math.max(levels.get(target) || 0,levels.get(queue[i])+1));
      incoming.set(target,incoming.get(target)-1);
      if (!incoming.get(target)) queue.push(target);
    }
    const buckets=new Map(), positions=new Map();
    for (const node of graph.nodes.slice().sort((a,b) => stamp(a)-stamp(b) || a.id.localeCompare(b.id))) {
      const level=levels.get(node.id) || 0;
      if(!buckets.has(level))buckets.set(level,[]);
      buckets.get(level).push(node);
    }
    const maxRows=Math.max(1,...[...buckets.values()].map(b=>Math.min(10,b.length)));
    let x=50;
    for(const [,bucket] of [...buckets].sort((a,b)=>a[0]-b[0])) {
      const rows=Math.min(10,bucket.length);
      bucket.forEach((node,index)=>positions.set(node.id,{x:x+Math.floor(index/10)*270,y:50+(maxRows-rows)*56+(index%10)*112,cycle:incoming.get(node.id)>0}));
      x+=Math.ceil(bucket.length/10)*270+35;
    }
    return {positions, width:Math.max(650,...[...positions.values()].map(p => p.x+290)), height:Math.max(400,...[...positions.values()].map(p => p.y+125))};
  }

  function recent(graph, selected) {
    const keep=new Set(),groups=new Map();
    for(const node of graph.nodes) {
      const parent=node.reviewOf||node.investigationOf||node.recoveryOf;
      if(!parent || active.has(node.status) || ['queued','blocked'].includes(node.status) || node.id===selected || node.manualAttention)keep.add(node.id);
      if(parent){const key=parent+':'+node.taskType;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(node);}
    }
    for(const nodes of groups.values())nodes.sort((a,b)=>stamp(b)-stamp(a)).slice(0,2).forEach(n=>keep.add(n.id));
    // Never orphan a retained nested review or repair from its parent.
    let changed=true;
    while(changed){changed=false;for(const edge of graph.edges)if(edge.kind==='lineage'&&keep.has(edge.target)&&!keep.has(edge.source)){keep.add(edge.source);changed=true;}}
    return {nodes:graph.nodes.filter(n=>keep.has(n.id)),edges:graph.edges.filter(e=>keep.has(e.source)&&keep.has(e.target))};
  }

  function create(host, options={}) {
    host.innerHTML = `<div class="wg-shell"><header class="wg-header"><div><span class="wg-eyebrow">POST OFFICE</span><h2>Work connections</h2><p>Follow a story from its prerequisites to its results.</p></div><button data-close aria-label="Close Post Office">Close</button></header>
      <div class="wg-controls"><label>Explore<select data-scope><option value="project">Project</option><option value="sprint">Sprint</option><option value="task">Task</option></select></label><label class="wg-search">Find work<input data-search type="search" placeholder="Search titles or IDs"></label><label class="wg-picker">Selected work<select data-pick aria-label="Selected work"></select></label><label class="wg-check"><input data-time type="checkbox"> Time order</label><button data-refresh>Refresh</button></div>
      <div class="wg-legend"><span class="wg-key dependency">Prerequisite → task</span><span class="wg-key lineage">Repair / review</span><span class="wg-key chronology">Time order (not a prerequisite)</span><label><input type="checkbox" data-all> All historical attempts</label><span data-progress role="status"></span><span data-status role="status" aria-live="polite">Loading work…</span></div>
      <div class="wg-content"><div class="wg-canvas"><div class="wg-tools"><button data-fit>Fit graph</button><button data-zoom-in aria-label="Zoom in">+</button><button data-zoom-out aria-label="Zoom out">−</button></div><svg data-svg role="group" aria-label="Interactive task dependency graph" tabindex="0"></svg><p class="wg-hint">Click a task to zoom in · drag to pan · scroll to zoom</p></div><aside data-detail aria-label="Task details"><div class="wg-empty">Select a task node to inspect its work and history.</div></aside></div></div>`;
    const el = selector => host.querySelector(selector), svg=el('[data-svg]');
    let graph=null, visible=null, positions=null, camera={x:0,y:0,w:1000,h:650}, selected=null, timer=null, loading=false, stopped=false, drag=null, requestVersion=0;
    const date = value => value ? new Date(value).toLocaleString() : 'Not recorded';
    const setCamera = () => svg.setAttribute('viewBox',`${camera.x} ${camera.y} ${camera.w} ${camera.h}`);
    const fit = () => { if (positions) { const b=svg.getBoundingClientRect(), ratio=(b.width||800)/(b.height||600),w=Math.max(positions.width,positions.height*ratio),h=w/ratio;camera={x:(positions.width-w)/2,y:(positions.height-h)/2,w,h}; setCamera(); } };
    function zoom(factor, point) {
      const bounds=svg.getBoundingClientRect(), ratio=point ? {x:(point.x-bounds.left)/bounds.width,y:(point.y-bounds.top)/bounds.height} : {x:.5,y:.5};
      const w=Math.max(280,Math.min(30000,camera.w*factor)), h=w*(camera.h/camera.w);
      camera={x:camera.x+(camera.w-w)*ratio.x,y:camera.y+(camera.h-h)*ratio.y,w,h}; setCamera();
    }
    function choices() {
      if (!graph) return;
      const type=el('[data-scope]').value, query=el('[data-search]').value.toLowerCase(), current=el('[data-pick]').value;
      let items= type==='project' ? [...graph.projects].map(([id,title]) => ({id,title})) : type==='sprint' ? Object.entries(graph.snapshot.sprints || {}).map(([id,s]) => ({id,title:s.title || s.goal || s.name || id})) : originalTasks(graph).map(n => ({id:n.id,title:n.title,status:n.status,at:stamp(n)}));
      items=items.filter(i => `${i.title} ${i.id}`.toLowerCase().includes(query));
      const activity=id => Math.max(0,...graph.nodes.filter(n => type==='project' ? n.projectKey===id || n.productId===id : n.sprintId===id).map(n => active.has(n.status) ? Number(n.lastProgressAt || stamp(n)) : 0));
      items.sort((a,b) => type==='task' ? Number(active.has(b.status))-Number(active.has(a.status)) || (b.at || 0)-(a.at || 0) : activity(b.id)-activity(a.id) || a.title.localeCompare(b.title));
      el('[data-pick]').innerHTML = items.map(i => `<option value="${esc(i.id)}">${esc(i.title)} - ${progressText(groupProgress(graph,type,i.id))} - ${esc(i.id)}</option>`).join('');
      if (items.some(i => i.id===current)) el('[data-pick]').value=current;
    }
    function select(id, center=true) {
      const node=graph?.nodes.find(n => n.id===id); if (!node) return;
      selected=id;
      if(positions && !positions.positions.has(id)) {
        el('[data-all]').checked=true;
        const full=scope(graph,el('[data-scope]').value,el('[data-pick]').value,false);
        if(!full.nodes.some(n=>n.id===id)){el('[data-scope]').value='task';el('[data-search]').value='';choices();el('[data-pick]').value=originalId(graph,id);}
        render(true);
      }
      svg.querySelectorAll('[data-node]').forEach(g => g.classList.toggle('selected',g.dataset.node===id));
      if (center && positions?.positions.has(id)) {
        const p=positions.positions.get(id), bounds=svg.getBoundingClientRect(), ratio=(bounds.height || 500)/(bounds.width || 700);
        camera={x:p.x+121-325,y:p.y+41-325*ratio,w:650,h:650*ratio}; setCamera();
      }
      const progress=taskProgress(graph.nodes.find(n=>n.id===originalId(graph,node.id)) || node);
      const owner=graph.snapshot.agents?.[node.assignedTo]?.name || node.assignedTo || 'Unassigned';
      const related=graph.edges.filter(e => e.kind!=='chronology' && (e.source===id || e.target===id));
      const attempts=graph.nodes.filter(n => n.reviewOf===id || n.investigationOf===id || n.recoveryOf===id).sort((a,b)=>stamp(b)-stamp(a));
      const ongoing=attempts.filter(n=>active.has(n.status));
      related.sort((a,b)=>{const other=e=>graph.nodes.find(n=>n.id===(e.source===id?e.target:e.source));return Number(active.has(other(b).status))-Number(active.has(other(a).status))||stamp(other(b))-stamp(other(a));});
      const history=[...(node.recoveryLedger?.history || []),...(node.transportRecovery?.history || []),...(node.interventionHistory || []),...(node.decisionHistory || [])].sort((a,b)=>(a.at||0)-(b.at||0));
      const reviews=node.peerReviews || [];
      const artifacts=[...new Set([node.libraryPath,node.artifactPath,node.validatedArtifactPath,...list(node.sourceEvidencePaths),
        ...attempts.flatMap(n=>[n.libraryPath,n.artifactPath,n.validatedArtifactPath,...list(n.sourceEvidencePaths)])].filter(p=>typeof p==='string' && p))];
      const urls=Array.from(new Set((`${node.instructions || ''}\n${node.note || ''}`).match(/https?:\/\/[^\s<>"']+/g) || []));
      const pane=el('[data-detail]'),same=pane.dataset.taskId===id,scroll=same?pane.scrollTop:0;
      const expanded=same?[...pane.querySelectorAll('details[open]')].map(d=>d.querySelector('summary')?.textContent):[];
      const preview=same?pane.querySelector('[data-preview]'):null;
      el('[data-detail]').innerHTML = `<span class="wg-eyebrow">${esc(node.id)} · ${esc(node.source)}</span><h3>${esc(node.title)}</h3><p><span class="wg-badge ${esc(node.status)}">${esc(node.deliveryState || node.status)}</span> <span>${esc(owner)}</span></p>
        <div class="wg-progress"><strong>${progressText(progress)}${progress.percent!==null?' complete':''}</strong>${progress.percent!==null?`<progress max="100" value="${progress.percent}" aria-label="Task completion"></progress>`:''}<p>${esc(progress.label)}${progress.at ? ` - ${esc(date(progress.at))}` : ''}</p><p>${esc(progress.reason)}</p></div>
        <section><h4>Current work</h4><p>${esc(node.nextAction || node.note || node.planningStatus || 'No current progress note recorded.')}</p>${ongoing.map(n=>`<button class="wg-related" data-focus="${esc(n.id)}"><small>${esc(n.status)} · ${esc(n.assignedTo || 'Unassigned')} · ${esc(n.id)}</small>${esc(n.title)}</button>`).join('')}${node.failNote ? `<details><summary>Recorded failure</summary><p>${esc(node.failNote)}</p></details>`:''}<dl><dt>Created</dt><dd>${esc(date(node.createdAt))}</dd><dt>Completed</dt><dd>${esc(date(node.completedAt))}</dd><dt>Last progress</dt><dd>${esc(date(node.lastProgressAt))}</dd></dl></section>
        <section><h4>Required outcome</h4><p>${esc(node.acceptanceCriteria || 'No acceptance criteria recorded.')}</p><details><summary>Instructions</summary><p class="wg-pre">${esc(node.instructions || 'No instructions recorded.')}</p></details></section>
        <section><h4>Connected work (${related.length})</h4><details><summary>Explore connections</summary>${related.map(e=>{ const other=graph.nodes.find(n=>n.id===(e.source===id?e.target:e.source));return `<button class="wg-related" data-focus="${esc(other.id)}"><small>${esc(e.label)} · ${esc(other.status)} · ${esc(other.id)} · ${e.target===id?'incoming':'outgoing'}</small>${esc(other.title)}</button>`;}).join('') || '<p>No recorded connections.</p>'}</details></section>
        <section><h4>Attempts and reviews (${attempts.length})</h4>${attempts.map(n=>`<button class="wg-related" data-focus="${esc(n.id)}"><small>${esc(n.status)} · ${esc(n.assignedTo || 'Unassigned')}</small>${esc(n.title)}</button>`).join('') || '<p>No follow-up attempts recorded.</p>'}${reviews.map(r=>`<details><summary>${esc(r.verdict || 'Review')} · ${esc(r.reviewerId || r.agentId || 'Reviewer')}</summary><p>${esc(r.summary || r.note || '')}</p><ul>${list(r.checks).map(c=>`<li>${esc(typeof c==='string'?c:JSON.stringify(c))}</li>`).join('')}</ul></details>`).join('')}</section>
        <section><h4>History (${history.length})</h4><details><summary>Recovery and decisions</summary>${history.map(h=>`<div class="wg-history"><small>${esc(date(h.at))}</small><p>${esc(h.failure || h.guidance || h.reason || h.action || 'Recovery event')}</p></div>`).join('') || '<p>No additional recovery history recorded.</p>'}</details></section>
        <section><h4>Saved results and links</h4>${artifacts.map(path=>`<button class="wg-related" data-document="${esc(path)}">${esc(path)}</button>`).join('')}${urls.map(url=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`).join('')}${!artifacts.length&&!urls.length?'<p>No saved links recorded.</p>':''}${node.completionWriteup?.body?`<details><summary>Completion write-up</summary><p class="wg-pre">${esc(node.completionWriteup.body)}</p></details>`:''}</section>`;
      pane.dataset.taskId=id;
      const sections=pane.querySelectorAll(':scope > section');
      if(sections.length===6)pane.insertBefore(sections[5],sections[2]);
      if(attempts.length || reviews.length){
        const section=sections[3],details=document.createElement('details'),summary=document.createElement('summary');
        summary.textContent='Explore attempts and review findings';details.appendChild(summary);
        while(section.children.length>1)details.appendChild(section.children[1]);
        section.appendChild(details);
      }
      pane.querySelectorAll('details').forEach(d=>{if(expanded.includes(d.querySelector('summary')?.textContent))d.open=true;});
      if(preview)pane.appendChild(preview);
      pane.scrollTop=scroll;
      el('[data-detail]').querySelectorAll('[data-focus]').forEach(b=>b.onclick=()=>select(b.dataset.focus));
      el('[data-detail]').querySelectorAll('[data-document]').forEach(b=>b.onclick=async()=>{
        if(options.onDocument) {options.onDocument(b.dataset.document);return;}
        let viewer=el('[data-detail]').querySelector('[data-preview]');
        if(!viewer){viewer=document.createElement('section');viewer.dataset.preview='true';el('[data-detail]').appendChild(viewer);}
        viewer.innerHTML='<h4>Document preview</h4><p></p><pre class="wg-pre">Loading…</pre>';
        viewer.querySelector('p').textContent=b.dataset.document;
        viewer.scrollIntoView({block:'nearest',behavior:'smooth'});
        try{const response=await (options.fetch||fetch)('/api/library/document?path='+encodeURIComponent(b.dataset.document),{credentials:'same-origin'});const data=await response.json();if(!response.ok||data.error)throw Error(data.error||'Document is unavailable.');viewer.querySelector('pre').textContent=String(data.content||'(empty)').slice(0,100000);}
        catch(error){viewer.querySelector('pre').textContent=error.message;}
      });
    }
    function render(reset=false) {
      if (!graph) return;
      visible=scope(graph,el('[data-scope]').value,el('[data-pick]').value,el('[data-time]').checked);
      const total=visible.nodes.length;
      const aggregate=groupProgress(graph,el('[data-scope]').value,el('[data-pick]').value);
      el('[data-progress]').textContent=`${progressText(aggregate)}${aggregate.percent!==null?' complete':''} - ${aggregate.accepted}/${aggregate.total} original tasks accepted${aggregate.unassessed ? ` - ${aggregate.unassessed} awaiting assessment` : ''}`;
      el('[data-progress]').title='Equal weight per original task, including planned tasks. Repairs do not add to project progress.';
      if(!el('[data-all]').checked)visible=recent(visible,selected);
      if(el('[data-time]').checked){
        visible.edges=visible.edges.filter(e=>e.kind!=='chronology');
        const groups=new Map();for(const node of visible.nodes)if(stamp(node)&&node.source==='task'){if(!groups.has(node.projectKey))groups.set(node.projectKey,[]);groups.get(node.projectKey).push(node);}
        for(const nodes of groups.values()){nodes.sort((a,b)=>stamp(a)-stamp(b));for(let i=1;i<nodes.length;i++)visible.edges.push({source:nodes[i-1].id,target:nodes[i].id,kind:'chronology',label:'Time order'});}
      }
      positions=layout(visible);
      const definitions='<defs><marker id="wg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>';
      const edges=visible.edges.map(e=>{ const a=positions.positions.get(e.source),b=positions.positions.get(e.target),sx=a.x+242,sy=a.y+40,tx=b.x,ty=b.y+40;return `<path class="wg-edge ${e.kind}" d="M${sx},${sy} C${sx+50},${sy} ${tx-50},${ty} ${tx},${ty}" marker-end="url(#wg-arrow)"><title>${esc(e.label)}</title></path>`;}).join('');
      const nodes=visible.nodes.map(n=>{const p=positions.positions.get(n.id),owner=graph.snapshot.agents?.[n.assignedTo]?.name || n.assignedTo || 'Unassigned',title=String(n.title || n.id);return `<g data-node="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(title+' · '+n.status)}" class="wg-node ${esc(n.status)} ${selected===n.id?'selected':''}" transform="translate(${p.x},${p.y})"><rect width="242" height="82" rx="12"/><circle cx="17" cy="19" r="4"/><text x="29" y="23" class="wg-node-status">${esc(n.status)} - ${progressText(taskProgress(graph.nodes.find(root=>root.id===originalId(graph,n.id)) || n))}${p.cycle?' · cycle':''}</text><text x="14" y="46">${esc(title.slice(0,31))}${title.length>31?'…':''}</text><text x="14" y="65" class="wg-node-owner">${esc(owner)} · ${esc(n.id.slice(0,25))}</text><title>${esc(title)}</title></g>`;}).join('');
      svg.innerHTML=definitions+edges+nodes;
      svg.querySelectorAll('[data-node]').forEach(g=>{g.onclick=()=>select(g.dataset.node);g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();e.stopPropagation();select(g.dataset.node);}};});
      const cycles=[...positions.positions.values()].filter(p=>p.cycle).length;
      el('[data-status]').textContent=`${visible.nodes.length} of ${total} tasks · ${visible.edges.length} connections${cycles?' · dependency cycle detected':''} · updated ${new Date().toLocaleTimeString()}`;
      if(reset) fit();
      if(selected && visible.nodes.some(n=>n.id===selected)) select(selected,false);
      else {selected=null; el('[data-detail]').innerHTML='<div class="wg-empty">Select a task node to inspect its work and history.</div>';}
      if(!visible.nodes.length) el('[data-detail]').innerHTML='<div class="wg-empty">No tasks match this selection. Choose another project, sprint or task.</div>';
    }
    async function refresh(reset=false) {
      if(loading || stopped) return; loading=true; const version=++requestVersion;
      try { const response=await (options.fetch || fetch)('/api/state',{credentials:'same-origin'}); if(!response.ok) throw Error(response.status===401?'Sign in again to view work.':'Could not load work.'); const snapshot=await response.json(); if(stopped || version!==requestVersion)return; graph=build(snapshot || {}); choices(); render(reset); }
      catch(error){if(!stopped)el('[data-status]').textContent=error.message+' Use Refresh to try again.';}
      finally{loading=false;}
    }
    el('[data-scope]').onchange=()=>{selected=null;choices();render(true);};
    el('[data-search]').oninput=()=>{choices();render(true);};
    el('[data-pick]').onchange=()=>{selected=null;render(true);};
    el('[data-time]').onchange=()=>render(false);
    el('[data-all]').onchange=()=>render(true);
    el('[data-refresh]').onclick=()=>refresh(false);
    el('[data-fit]').onclick=fit;
    el('[data-zoom-in]').onclick=()=>zoom(.75);
    el('[data-zoom-out]').onclick=()=>zoom(1.33);
    const observer=typeof ResizeObserver!=='undefined'?new ResizeObserver(()=>{if(!stopped&&positions){if(selected)select(selected,true);else fit();}}):null;
    observer?.observe(svg);
    const close=()=>{stopped=true;clearInterval(timer);observer?.disconnect();options.onClose?.();};
    el('[data-close]').onclick=close;
    host.onkeydown=e=>{
      e.stopPropagation();
      if(e.key==='Escape'){e.preventDefault();close();}
      if(e.key==='Tab'){
        const controls=[...host.querySelectorAll('button,input,select,a[href],[tabindex="0"]')].filter(n=>n.getBoundingClientRect().width>0);
        const first=controls[0],last=controls[controls.length-1];
        if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
        else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
      }
    };
    svg.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?1.12:.88,{x:e.clientX,y:e.clientY});},{passive:false});
    svg.onpointerdown=e=>{if(e.target.closest('[data-node]'))return; drag={x:e.clientX,y:e.clientY,camera:{...camera}};svg.setPointerCapture(e.pointerId);};
    svg.onpointermove=e=>{if(!drag)return;const b=svg.getBoundingClientRect();camera={...drag.camera,x:drag.camera.x-(e.clientX-drag.x)*drag.camera.w/b.width,y:drag.camera.y-(e.clientY-drag.y)*drag.camera.h/b.height};setCamera();};
    svg.onpointerup=svg.onpointercancel=()=>{drag=null;};
    refresh(true); timer=setInterval(()=>refresh(false),10000);
    return {close,refresh};
  }
  const api={build,scope,layout,recent,originalTasks,originalId,taskProgress,groupProgress,progressText,create};
  if(typeof module!=='undefined' && module.exports) module.exports=api;
  if(root) root.WorkGraph=api;
})(typeof window!=='undefined'?window:null);
