const assert=require('node:assert/strict');
const {build,scope,layout,recent,originalTasks,originalId,taskProgress,groupProgress}=require('../work_graph.js');
const snapshot={tasks:{
 a:{id:'a',title:'Collect inputs',status:'done',workOrderId:'p',workOrderStoryId:'s1',createdAt:1,sprintId:'sprint'},
 b:{id:'b',title:'Format report',status:'repairing',workOrderId:'p',workOrderStoryId:'s2',createdAt:2},
 fix:{id:'fix',title:'Fix: report',reviewOf:'b',taskType:'code',status:'working',createdAt:3},
 review:{id:'review',title:'Review',reviewOf:'b',taskType:'review',status:'done',createdAt:4},
 external:{id:'external',title:'External prerequisite',workOrderId:'other',status:'done'},
 },backlogRequests:[{id:'p',title:'Portfolio',plan:{stories:[
 {id:'s1',title:'Collect inputs'}, {id:'s2',title:'Format report',prerequisiteStoryIds:['s1']},
 {id:'s3',title:'Deliver email',prerequisiteStoryIds:['s2']} ]}}],
 workQueue:[{title:'Deliver email',workOrderId:'p',workOrderStoryId:'s3',dependsOn:'b'}]};
const graph=build(snapshot);
assert(originalTasks(graph).some(n=>n.id==='b'),'original task stays selectable while being repaired');
assert(!originalTasks(graph).some(n=>['fix','review'].includes(n.id)),'repairs and reviews are excluded from the selector');
assert.equal(originalId(graph,'fix'),'b','repair navigation selects the original task');
const nested=build({tasks:{root:{id:'root',title:'Build report'},fix:{id:'fix',recoveryOf:'root'},review:{id:'review',reviewOf:'fix'},legacy:{id:'legacy',title:'Fix: report'},self:{id:'self',rootStoryId:'self',title:'Original'}}});
assert.equal(originalId(nested,'review'),'root','nested follow-ups resolve to the original task');
assert.deepEqual(originalTasks(nested).map(n=>n.id),['root','self'],'legacy fixes are excluded and self-rooted originals remain');
assert.equal(graph.nodes.filter(n=>n.workOrderId==='p'&&n.workOrderStoryId==='s2').length,1,'planned and real task deduplicate');
assert.equal(graph.nodes.find(n=>n.id==='fix').projectKey,'p','repair inherits project');
assert.equal(graph.nodes.find(n=>n.id==='plan:p:s3').status,'queued');
assert(graph.edges.some(e=>e.source==='a'&&e.target==='b'&&e.kind==='dependency'));
assert(graph.edges.some(e=>e.source==='b'&&e.target==='fix'&&e.kind==='lineage'));
const taskView=scope(graph,'task','b',false);
assert(taskView.nodes.some(n=>n.id==='a')&&taskView.nodes.some(n=>n.id==='fix'));
assert(!taskView.edges.some(e=>e.kind==='chronology'));
assert(scope(graph,'project','p',true).edges.some(e=>e.kind==='chronology'));
assert(scope(graph,'sprint','sprint',false).nodes.some(n=>n.id==='b'),'sprint includes boundary dependency');
const positioned=layout(taskView);
assert(positioned.positions.get('a').x<positioned.positions.get('b').x);
assert.equal(layout({nodes:[{id:'x'},{id:'y'}],edges:[{source:'x',target:'y'},{source:'y',target:'x'}]}).positions.get('x').cycle,true);
const empty=scope(graph,'project','missing',false);assert.equal(empty.nodes.length,0);
const queued=[{title:'Same title',reviewOf:'b',assignedTo:'one'},{title:'Same title',reviewOf:'b',assignedTo:'two'}];
const first=build({tasks:snapshot.tasks,workQueue:queued}),second=build({tasks:snapshot.tasks,workQueue:queued.slice().reverse()});
assert.deepEqual(first.nodes.filter(n=>n.source==='queue').map(n=>n.id).sort(),second.nodes.filter(n=>n.source==='queue').map(n=>n.id).sort());
const sprint=build({tasks:{},sprints:{s:{name:'Upcoming sprint',items:[{title:'Build report',room:'pressoffice'}]}}});
assert.equal(scope(sprint,'sprint','s',false).nodes.length,1,'undispatched sprint items are visible');
assert.equal(scope(build({sprints:{old:{items:[['Legacy sprint task','pressoffice']]}}}),'sprint','old',false).nodes.length,1,'legacy sprint item tuples are supported');
const history=build({tasks:{p:{id:'p',status:'repairing'},...Object.fromEntries(Array.from({length:10},(_,i)=>['f'+i,{id:'f'+i,reviewOf:'p',taskType:'code',status:i===0?'working':'failed',createdAt:i+1}]))}});
assert.equal(recent(history).nodes.length,4,'current work and latest attempts remain visible');
assert(recent(history,'f1').nodes.some(n=>n.id==='f1'),'selected historical task remains visible');
const product=build({tasks:{a:{id:'a',productId:'product',workOrderId:'request-a'},b:{id:'b',productId:'product',workOrderId:'request-b'}},products:{product:{name:'Trading app'}}});
assert.equal(scope(product,'project','product',false).nodes.length,2);
console.log('Work graph: dependency, lineage, scope, ordering, identity and cycle checks passed.');
const assessed=(reviewer,credit,verified=false)=>({reviewer,atMs:10,acceptanceAssessment:{verified,checks:[
 {criterionId:'C1',status:credit===100?'met':'unmet',completionPercent:credit,evidence:['Actual inspected source'],explanation:'Implemented portion'}]}});
const story={id:'original',status:'repairing',workOrderId:'project',acceptanceCriteria:'Deliver result',peerReviews:[assessed('a',70),assessed('b',50)]};
assert.equal(taskProgress(story).percent,50,'conservative independent reviewer estimate');
assert.equal(taskProgress({...story,peerReviews:[assessed('a',100,true)]}).percent,99,'review alone cannot complete a story');
const accepted={...story,status:'done',_peerGate:{approvals:2},peerReviews:[assessed('a',100,true),assessed('b',100,true)]};
assert.equal(taskProgress(accepted).percent,100,'accepted work reaches 100');
assert.equal(taskProgress({status:'working'}).label,'Not yet assessed');
const aggregate=build({tasks:{original:accepted,fix:{id:'fix',reviewOf:'original',status:'done'}},backlogRequests:[{id:'project',plan:{stories:[{id:'future',title:'Next task'}]}}]});
assert.equal(groupProgress(aggregate,'project','project').percent,50,'planned work counts and fixes do not inflate progress');
assert.equal(groupProgress(aggregate,'project','project').total,2);
assert.equal(taskProgress({...story,instructions:'Criterion C1 also required'}).percent,25,'missing criteria remain in denominator');
const obsolete=assessed('old-reviewer',0);obsolete.atMs=1;
assert.equal(taskProgress({...story,peerReviews:[obsolete,...story.peerReviews]}).percent,50,'obsolete reviewers cannot pin newer progress at zero');
assert.equal(taskProgress({...story,_peerGate:{reviewerIds:['a','b']},peerReviews:[...story.peerReviews,{...obsolete,atMs:20}]}).percent,50,'only current reviewer roster contributes');
const legacy={id:'legacy',status:'done',room:'pressoffice',taskType:'code',
 _peerGate:{closed:true,approvals:2,approvers:['a','b'],reviewerIds:['a','b']},
 _codeDirectorPassed:{evidence:'pytest 10 passed; all quality checks passed'},
 peerReviews:[{reviewer:'a',verdict:'clean',atMs:1},{reviewer:'b',verdict:'clean',atMs:2}]};
assert.equal(taskProgress(legacy).percent,100,'older accepted work uses recorded reviews and quality evidence');
assert.equal(taskProgress({...legacy,_codeDirectorPassed:null}).percent,null,'completed code without quality evidence is not assumed accepted');
assert.equal(taskProgress({...legacy,_peerGate:{...legacy._peerGate,acceptanceVerified:false}}).percent,null,'known acceptance failure overrides historical completion');
assert.equal(taskProgress({status:'working'}).percent,null,'unknown progress is not zero');
assert.equal(taskProgress({status:'planned'}).percent,0,'unstarted planned work is zero');
const incompleteProject={nodes:[{...legacy,projectKey:'p'},{id:'unknown',projectKey:'p',status:'working'}]};
assert.equal(groupProgress(incompleteProject,'project','p').percent,50);
assert.equal(groupProgress(incompleteProject,'project','p').lowerBound,true,'unknown work is disclosed in project totals');
