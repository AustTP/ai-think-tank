"""Document custody: persist supplied artifacts, retain provenance, never approve claims."""
import copy
import csv
import hashlib
import io
import json
import os
import re
import time

AGENT_ID = 'librarian'
REUSE_INSTRUCTION = ('Read upcoming task instructions and acceptance criteria, then inspect existing Library documents. '
    'Attach relevant document paths and concrete evidence to the task and identify what can be reused, '
    'what remains unfinished, and whether an approved artifact may already satisfy the task. '
    'Have workers read and build on those artifacts before repeating research. Never approve a task '
    'from a title match; existing documents can be stale or unverified and still require acceptance checks.')
_INDEX = {}


def relevant_documents(state, task, serve):
    root = serve.LIBRARY_DIR
    now = time.monotonic()
    cached = _INDEX.get(root)
    if not cached or now-cached[0] > 60:
        entries=[]
        for directory, dirs, files in os.walk(root):
            dirs[:]=[d for d in dirs if not any(w in d.lower() for w in ('backup','pending_review')) and not d.startswith('.')]
            for name in files:
                if not name.endswith(('.md','.csv')) or name.startswith('.'): continue
                path=os.path.join(directory,name)
                if os.path.islink(path): continue
                try:
                    with open(path,encoding='utf-8',errors='replace') as stream: text=stream.read(24000)
                except OSError: continue
                entries.append((os.path.relpath(path,root).replace('\\','/'),text))
        _INDEX[root]=(now,entries)
    entries=_INDEX[root][1]
    stop={'research','implement','existing','story','task','report','with','from','that','this','code','save','review','techniques'}
    words=set(re.findall(r'[a-z]{4,}',task.get('title','').lower()))-stop
    if not words: return []
    candidates=[]
    sources={t.get('libraryPath'):t for t in (state.get('tasks') or {}).values() if t.get('libraryPath') and t.get('status')=='done'}
    for path,text in entries:
        if path==task.get('libraryPath'): continue
        found=words.intersection(set(re.findall(r'[a-z]{4,}',(path+' '+text).lower())))
        if len(found)<min(2,len(words)): continue
        score=len(found)/len(words)+sum(w in path.lower() for w in found)
        source=sources.get(path)
        same_contract=bool(source and task.get('acceptanceCriteria') and source.get('acceptanceCriteria')==task['acceptanceCriteria'])
        candidates.append((score,{'path':path,'matchedTerms':sorted(found),'excerpt':text[:800],
                                 'sourceTaskId':source.get('id') if source else None,
                                 'assessment':'potentially_fulfills' if same_contract else 'may_help',
                                 'requiresVerification':True}))
    return [entry for _,entry in sorted(candidates,key=lambda x:(-x[0],x[1]['path']))[:4]]


def attach_reuse(state, task, serve):
    signature=hashlib.sha256(json.dumps([task.get('title'),task.get('instructions'),task.get('acceptanceCriteria')]).encode()).hexdigest()
    if (task.get('libraryReuse') or {}).get('taskSignature')==signature and int(time.time()*1000)-(task.get('libraryReuse') or {}).get('checkedAt',0)<60000: return
    task['libraryReuse']={'taskSignature':signature,'checkedBy':AGENT_ID,
                          'documents':relevant_documents(state,task,serve),'checkedAt':int(time.time()*1000)}
WRITE_TOOL = {'type':'function','function':{'name':'save_library_document',
    'description':'Save an actual document or CSV in the shared Library. Supply full contents; a filename mentioned in a report does not save a file. Returns its saved path.',
    'parameters':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content']}}}


def create_role(state):
    definition = {'id':AGENT_ID,'name':'Lyra','role':'Librarian','model':'small','color':'#a78bfa',
        'librarian':True,'villageId':'main','approvedCount':0,'droppedCount':0,
        'profile':{'mission':'Save and catalog real documents and resolve missing Library artifacts before review.',
            'instructions':['Save actual file contents, not just names or promises.',
                'Preserve source and uncertainty. Never invent datasets or mark research claims verified.',
                'Recover only explicitly labeled supplied artifacts. Report missing content to its author.',
                'Keep independent review and quality gates.'], 'notes':[]}}
    if not any(d.get('id') == AGENT_ID for d in state.setdefault('agentRoster',[])):
        state['agentRoster'].append(copy.deepcopy(definition))
    if AGENT_ID not in state.setdefault('agents',{}):
        state['agents'][AGENT_ID] = {**copy.deepcopy(definition),'x':510.,'y':272.,'dir':'south',
            'offDuty':True,'visible':False,'busy':False,'task':None,'inRoom':None,
            'roomX':None,'roomY':None,'meetingId':None,'mailbox':[],'conversationLog':[],
            'hiredAt':int(time.time()*1000)}
    for agent in [state['agents'][AGENT_ID]]+[d for d in state['agentRoster'] if d.get('id')==AGENT_ID]:
        instructions=agent.setdefault('profile',{}).setdefault('instructions',[])
        if REUSE_INSTRUCTION not in instructions: instructions.append(REUSE_INSTRUCTION)
    return AGENT_ID


def embedded_csv(document, path):
    """Extract only a table/fence immediately following an explicit CSV filename."""
    label = re.search(r'(?m)^.*(?<![\w/.-])' + re.escape(path) + r'(?![\w/.-]).*$', document)
    if not label:
        return None
    tail = document[label.end():].lstrip()
    fence = re.match(r'```(?:csv)?[ \t]*\r?\n(.*?)\r?\n```', tail, re.S)
    if fence:
        rows = list(csv.reader(io.StringIO(fence.group(1))))
    elif tail.startswith('|'):
        lines = []
        for line in tail.splitlines():
            if not line.strip().startswith('|'): break
            lines.append([v.strip() for v in line.strip().strip('|').split('|')])
        if len(lines) < 3 or not all(re.fullmatch(r':?-+:?', v.replace(' ','')) for v in lines[1]):
            return None
        rows = [lines[0]] + lines[2:]
    else:
        return None
    if len(rows) < 2 or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        return None
    output = io.StringIO(newline='')
    csv.writer(output).writerows(rows)
    return output.getvalue()


def inspect_report(task, serve):
    """Idempotently file supplied CSV tables. Missing content remains an explicit failure."""
    report_path = task.get('libraryPath')
    target = serve._safe_library_path(report_path) if report_path else None
    if not target or not os.path.isfile(target): return {'saved':[], 'missing':[]}
    with open(target,encoding='utf-8',errors='replace') as stream:
        document = stream.read(200000)
    paths = list(dict.fromkeys(re.findall(r'[A-Za-z0-9_./-]+\.(?:csv|ipynb)',document)))[:12]
    saved, missing = [], []
    for path in paths:
        dest = serve._safe_library_path(path)
        # Never use a report to overwrite an existing file or a reserved namespace.
        if dest and os.path.isfile(dest): continue
        if (not dest or path.startswith(('wiki/','pending_review/','downloads/'))
                or '..' in path.split('/')):
            missing.append(path); continue
        contents = embedded_csv(document,path) if path.endswith('.csv') else None
        if contents is None:
            missing.append(path); continue
        os.makedirs(os.path.dirname(dest),exist_ok=True)
        try:
            with open(dest,'x',encoding='utf-8',newline='') as stream: stream.write(contents)
        except FileExistsError:
            continue
        provenance = {'sourceReport':report_path,'taskId':task.get('id'),
            'filedBy':AGENT_ID,'savedAt':int(time.time()*1000),
            'sha256':hashlib.sha256(contents.encode('utf-8')).hexdigest(),
            'method':'Exact transcription of labeled report table; claims remain unverified.'}
        with open(dest+'.provenance.json','w',encoding='utf-8') as stream:
            json.dump(provenance,stream,indent=2)
        serve.log_action(AGENT_ID,'library_write',{'path':path,'source':report_path,'method':'table_transcription'},authorized=True)
        saved.append(path)
    result = {'saved':saved,'missing':missing,'reportPath':report_path,
        'reportSha256':hashlib.sha256(document.encode('utf-8')).hexdigest()}
    task['libraryCustody'] = result
    return result


def step(state):
    import serve, sim
    create_role(state)
    pending=list(state.get('workQueue') or [])+[t for t in (state.get('tasks') or {}).values()
        if t.get('status') in ('walking','working','repairing') and not t.get('reviewOf')]
    for task in pending:
        attach_reuse(state,task,serve)
    for task in list((state.get('tasks') or {}).values()):
        if not task.get('libraryPath') or task.get('reviewOf') or task.get('status') not in ('needs_review','blocked','failed'):
            continue
        result = inspect_report(task,serve)
        if result['saved']:
            state['agents'][AGENT_ID].setdefault('profile',{}).setdefault('notes',[]).append(
                {'at':int(time.time()*1000),'text':f"Filed {', '.join(result['saved'])} from {task['id']}; independent review still required."})
        if not result['missing']: continue
        if task.get('manualAttention') or (task.get('_peerGate') or {}).get('escalated'):
            continue
        import story_tracking
        root = story_tracking.root(state, task)
        repairs = root.setdefault('artifactRecoveryAttempts', {})
        artifact_key = ','.join(sorted(result['missing']))
        pending = any(q.get('recoveryOf') == root['id'] for q in state.get('workQueue') or [])
        active = any(t.get('recoveryOf') == root['id'] and t.get('status') in ('walking','working') for t in state.get('tasks', {}).values())
        if pending or active or repairs.get(artifact_key, 0) >= 1:
            continue
        if sum(repairs.values()) >= 2:
            root.update(status='blocked', manualAttention=True, blockedReason='artifact_recovery_limit',
                        failNote='Two artifact recoveries have already been attempted. Supply actual contents or change the recovery plan before opening another job.')
            root['_peerGate'] = dict(root.get('_peerGate') or {})
            root['_peerGate']['escalated'] = True
            continue
        key = result['reportSha256'] + ':' + ','.join(result['missing'])
        if task.get('_librarianRepairKey') == key: continue
        task['_librarianRepairKey'] = key
        repairs[artifact_key] = repairs.get(artifact_key, 0) + 1
        message = ('Library custody: missing ' + ', '.join(result['missing'])
            + '. The report names these files but contains no complete labeled contents to save. '
            'Lyra will investigate and request or create the actual deliverables; peer approval remains required.')
        state['agents'][AGENT_ID].setdefault('profile',{}).setdefault('notes',[]).append({'at':int(time.time()*1000),'text':message})
        author = (state.get('agents') or {}).get(task.get('assignedTo'))
        if author is not None:
            author.setdefault('mailbox',[]).append({'kind':'library_missing_artifact','from':AGENT_ID,'text':message,'taskId':task['id']})
        sim.queue_work(state,[{'title':'Recover Library documents: '+task.get('title','deliverable'),
            'taskType':'spike','room':'observatory','assignedTo':AGENT_ID,'directRoute':True,
            'taskMasterWork':True,'budgetMs':120000,'budgetUsd':0.30,
            'recoveryOf':root['id'], 'scrumMasterId':root.get('scrumMasterId'),
            'instructions':message+' Read '+result['reportPath']+'. Preserve original requirements: '
                +str(task.get('acceptanceCriteria') or '')+'. Use save_library_document to save actual contents. '
                'Do not invent evidence, change existing files, or approve the parent story. If unavailable, save a precise recovery plan with owner and missing inputs.',
            'acceptanceCriteria':'Save recovered documents with sources and exact Library paths, or an evidence-based recovery plan identifying missing inputs and owner.',
            'projectLabel':task.get('projectLabel') or task.get('title')}])
    notes = state['agents'][AGENT_ID]['profile']['notes']
    state['agents'][AGENT_ID]['profile']['notes'] = notes[-50:]
