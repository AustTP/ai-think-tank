"""Company direction and asynchronous, question-first planning of player requests."""
import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='scrum-planner')
_FUTURES = {}
_POTENTIAL_FUTURES = {}


def _assess_potential(request, completed):
    import serve
    for task in completed[:8]:
        path=serve._safe_library_path(task.get('libraryPath') or '')
        if path:
            try:
                with open(path,encoding='utf-8') as stream:
                    task['artifactExcerpt']=stream.read(1500)
            except OSError:
                pass
    payload={'agentId':request['scrumMasterId'], 'taskId':'request/'+request['id'],
             'max_tokens':1000, 'response_format':{'type':'json_object'}, 'deliberate':False,
             'messages':[{'role':'system','content':
                 'As scrum master, activate only potential stories whose activationCondition is demonstrated '
                 'by supplied completed task evidence. Never activate speculative work. Return JSON '
                 '{"activate":[{"id":"story id","reason":"observed condition","evidenceTaskIds":["task id"]}]}.'},
                 {'role':'user','content':json.dumps({'stories':[s for s in request['plan']['stories'] if s.get('potential') and not s.get('activated')],
                                                    'completed':completed})}]}
    response=serve._http_json('POST',serve.SELF_BASE_URL,'/api/chat',payload,
                             serve.get_or_create_agent_key(request['scrumMasterId']),timeout=120)
    try: return json.loads((response or {}).get('reply') or '{}').get('activate',[])
    except (ValueError,AttributeError): return []


def assess_potential(state, request):
    potential=[s for s in request['plan']['stories'] if s.get('potential') and not s.get('activated')]
    if not potential or not request.get('scrumMasterId'): return
    completed=[{k:t.get(k) for k in ('id','title','note','libraryPath','workOrderStoryId')}
               for t in (state.get('tasks') or {}).values() if t.get('workOrderId')==request['id']
               and t.get('status')=='done' and not any(t.get(k) for k in ('reviewOf','investigationOf','recoveryOf'))]
    ids={t['id'] for t in completed}
    future=_POTENTIAL_FUTURES.get(request['id'])
    if future and future.done():
        try: decisions=future.result()
        except Exception: decisions=[]
        _POTENTIAL_FUTURES.pop(request['id'],None)
        for decision in decisions if isinstance(decisions,list) else []:
            if not isinstance(decision,dict): continue
            evidence=decision.get('evidenceTaskIds')
            if not isinstance(evidence,list) or not evidence or not all(isinstance(i,str) and i in ids for i in evidence): continue
            for story in potential:
                if story['id']==decision.get('id') and decision.get('reason'):
                    story.update(activated=True,activationEvidence=decision,activatedAt=int(time.time()*1000))
    signature=sorted(ids)
    if ids and request.get('potentialAssessmentEvidence')!=signature and request['id'] not in _POTENTIAL_FUTURES:
        request['potentialAssessmentEvidence']=signature
        _POTENTIAL_FUTURES[request['id']]=_POOL.submit(_assess_potential,copy.deepcopy(request),completed)


def direction_allowed(state, agent_id):
    role = str(((state.get('agents') or {}).get(agent_id) or {}).get('role', '')).lower()
    return 'review' in role or 'scrum master' in role or any(
        t.get('scrumMasterId') == agent_id for t in state.get('teams') or [])


def task_context(state, agent_id, value):
    """Remove previously embedded company direction from worker context."""
    if direction_allowed(state, agent_id):
        return value
    direction = north_star_context(state)
    if isinstance(value, str):
        import re
        value = value.replace(direction, '') if direction else value
        return re.sub(r'Company North Star:[^\n]*\nSuccess measures:[^\n]*\nConstraints:[^\n]*', '', value)
    if isinstance(value, dict):
        return {k: task_context(state, agent_id, v) for k, v in value.items()}
    if isinstance(value, list):
        return [task_context(state, agent_id, v) for v in value]
    return value


def north_star_context(state):
    star = state.get('northStar') or {}
    if not star.get('objective'): return ''
    return ('Company North Star: ' + star['objective'] + '\nSuccess measures: '
            + star.get('successMeasures','') + '\nConstraints: ' + star.get('constraints',''))


def _plan(request, star, master):
    import serve
    payload = {'agentId':master, 'taskId':'request/' + request['id'], 'max_tokens':2600, 'deliberate':False,
        'model':serve._mid_tier_slug() or serve._low_tier_slug(),
        'messages':[{'role':'system','content':
            'You are the scrum master completing an explicit player request. Never silently reject it. '
            'Break its overarching goal into testable stories, including an implementation milestone for code requests. '
            'Determine story types from the requested outcome: code means implement and execute working changes, '
            'research means produce evidence, and spike means resolve one specific unknown. Never replace an '
            'implementation request with only spikes or plans. Reuse existing integrations before researching replacements. '
            'Create ALL known stories now, including later implementation, integration and validation work. '
            'Label ordering with prerequisiteStoryIds: collect API documentation before implementing its call, '
            'implement before integration testing. Independent stories have an empty prerequisiteStoryIds array. '
            'Include potential follow-ups with potential=true and a specific activationCondition; these are visible '
            'but must not run until the scrum master confirms the condition from completed work. '
            'Inspect the existing Python app and known Coinbase decision; do not ask the player to choose APIs, '
            'data feeds or programming languages. Put technical unknowns in research stories. '
            'Ask at most three human questions ONLY for '
            'decisions or preferences the player must supply. Discover factual answers with research stories. '
            'Create useful discovery work while waiting for answers. Preserve the original acceptance criteria. '
            'Do not request passwords or API keys in answers. Do not infer live transaction authorization. '
            'Return only JSON {"summary":"planning reason","questions":[{"id":"q1","question":"specific question"}],'
            '"stories":[{"id":"s1","title":"story","taskType":"spike|research|code",'
            '"instructions":"specific work","acceptanceCriteria":"testable outcome","dependsOnQuestions":["q1"],'
            '"prerequisiteStoryIds":[],"potential":false,"activationCondition":""}]}.'},
            {'role':'user','content':json.dumps({'request':request, 'northStar':star,
                'knownTradingContext':{'exchange':'Coinbase','market':'spot','lossLimitUsd':50,
                    'executionMode':'paper','allocationAndSizing':'Agents should research proposals.'}})}]}
    response = serve._http_json('POST', serve.SELF_BASE_URL, '/api/chat', payload,
                               serve.get_or_create_agent_key(master), timeout=120)
    raw = (response or {}).get('reply') or ''
    if not isinstance(raw,str): raw=json.dumps(raw)
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', raw):
        try:
            data,_ = decoder.raw_decode(raw[match.start():])
        except ValueError:
            continue
        if isinstance(data,dict) and isinstance(data.get('stories'),list):
            data['diagnostics'] = {'model':payload['model'],'response':raw[:18000],'apiError':response.get('error')}
            return data
    return {'summary':'A discovery story will clarify the request; planner response was unavailable or invalid.',
        'questions':[],
        'stories':[{'id':'discover','title':'Discover requirements: '+request['title'], 'taskType':'spike',
            'instructions':'Investigate the request using existing Library artifacts and current code. Identify missing facts and propose implementation stories. '
                + request.get('reason',''),
            'acceptanceCriteria':'Save verified findings, a tool or artifact inventory, and a concrete implementation plan.',
            'dependsOnQuestions':[]}],
        'diagnostics':{'model':payload['model'],'response':raw[:18000],'apiError':(response or {}).get('error')}}


def begin(state, request, master):
    if request['id'] in _FUTURES or request.get('plan'):
        return
    request.update(status='planning', scrumMasterId=master, planningStartedAt=int(time.time()*1000))
    _FUTURES[request['id']] = _POOL.submit(_plan,copy.deepcopy(request),copy.deepcopy(state.get('northStar') or {}),master)


def apply_plan(state, request, plan, master):
    import sim
    if request.get('plan'): return
    questions = []
    for index, question in enumerate((plan.get('questions') or [])[:3]):
        if not isinstance(question,dict) or not isinstance(question.get('question'),str) or not question['question'].strip(): continue
        qid = str(question.get('id') or 'q'+str(index+1))[:60]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',qid) or any(q['id']==qid for q in questions): qid='q'+str(index+1)
        questions.append({'id':qid,
                          'question':question['question'][:2000], 'status':'awaiting_input'})
    stories = []
    for index, story in enumerate((plan.get('stories') or [])[:16]):
        if not isinstance(story,dict): continue
        if not all(isinstance(story.get(k),str) and story[k].strip() for k in ('title','instructions','acceptanceCriteria')): continue
        sid=str(story.get('id') or 's'+str(index+1))[:60]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',sid) or any(s['id']==sid for s in stories): sid='s'+str(index+1)
        dependencies=story.get('dependsOnQuestions')
        if not isinstance(dependencies,list): dependencies=[]
        prerequisites=story.get('prerequisiteStoryIds')
        if not isinstance(prerequisites,list): prerequisites=[]
        stories.append({**story,'id':sid,
                        'dependsOnQuestions':[v for v in dependencies if isinstance(v,str)],
                        'prerequisiteStoryIds':[v for v in prerequisites if isinstance(v,str)],
                        'dependenciesExplicit': 'prerequisiteStoryIds' in story,
                        'taskType':story.get('taskType') if story.get('taskType') in ('spike','research','code') else 'spike'})
    if not stories:
        stories=[{'id':'discover','title':'Discover requirements: '+request['title'],'taskType':'spike',
            'instructions':'Investigate '+request.get('reason','')+' and save concrete implementation stories.',
            'acceptanceCriteria':'Save sourced findings and testable implementation stories.','dependsOnQuestions':[]}]
    request.update(plan={'summary':str(plan.get('summary') or 'Planned by scrum master.')[:2000],
                         'questions':questions,'stories':stories,'diagnostics':plan.get('diagnostics') or {}},
                   scrumMasterId=master, budgetBand='standard')
    inbox=state.setdefault('playerInbox',[])
    for q in questions:
        message_id='work-order-'+request['id']+'-'+q['id']
        if not any(m.get('id')==message_id for m in inbox):
            inbox.append({'id':message_id,'kind':'work_order_question','workOrderId':request['id'],
                'questionId':q['id'],'agentId':master,'issueKey':request['id'],
                'question':q['question'],'status':'awaiting_input','createdAt':int(time.time()*1000)})
    release_stories(state,request)


def normalize_plan(state, request):
    plan=request['plan']
    if plan.get('version') == 3: return
    technical=[]
    for question in plan['questions']:
        prompt=question['question'].lower()
        if question.get('status') == 'awaiting_input' and any(term in prompt for term in ('programming language','specific apis','data feeds')):
            question['status']='delegated_to_research'
            technical.append(question)
            for message in state.get('playerInbox') or []:
                if message.get('workOrderId')==request['id'] and message.get('questionId')==question['id']:
                    message.update(status='superseded',answer='Technical choice delegated to the scrum master and research stories.')
    if technical and not any(story['taskType'] in ('spike','research') for story in plan['stories']):
        plan['stories'].append({'id':'technical-discovery','title':'Resolve technical requirements: '+request['title'],
            'taskType':'spike','instructions':'Inspect existing code and official documentation to answer: '
                + '; '.join(q['question'] for q in technical),'acceptanceCriteria':'Save verified technical choices and implementation prerequisites.',
            'dependsOnQuestions':[]})
    implementation = request.get('taskType') == 'code' or bool(re.search(
        r'\b(implement|build|integrate|automate|create (?:an? )?(?:app|application|script|service|system)|fix)\b',
        request.get('title','') + ' ' + request.get('reason',''), re.I))
    if implementation and not any(story['taskType']=='code' and not story.get('potential') for story in plan['stories']):
        discovery=[story['id'] for story in plan['stories']]
        plan['stories'].append({'id':'implementation','title':'Implement: '+request['title'],'taskType':'code',
            'instructions':'Complete the overarching request using the approved discovery findings and human answers. '
                + request.get('reason','') + '. Preserve quality gates and provide actual execution and test evidence.',
            'acceptanceCriteria':request.get('acceptanceCriteria') or 'Implement the requested capability and demonstrate it with passing tests.',
            'dependsOnQuestions':[q['id'] for q in plan['questions'] if q.get('status')=='awaiting_input'],
            'prerequisiteStoryIds':discovery})
    discovery_ids={story['id'] for story in plan['stories'] if story['taskType'] in ('research','spike')}
    for story in plan['stories']:
        if story['taskType']=='code' and not story.get('prerequisiteStoryIds') and not story.get('dependenciesExplicit'):
            story['prerequisiteStoryIds']=sorted(discovery_ids)
    plan['version']=3


def release_stories(state, request):
    import sim
    normalize_plan(state,request)
    plan=request['plan']
    unanswered={q['id'] for q in plan['questions'] if q.get('status')=='awaiting_input'}
    queued=request.setdefault('queuedStoryIds',[])
    by_id={story['id']:story for story in plan['stories']}
    def invalid_dependencies(sid, visiting=()):
        if sid not in by_id or sid in visiting:
            return True
        return any(invalid_dependencies(dep, visiting+(sid,)) for dep in by_id[sid].get('prerequisiteStoryIds') or [])
    for story in plan['stories']:
        if story['id'] in queued:
            story['planningStatus']='dispatched'
            continue
        if invalid_dependencies(story['id']):
            story['planningStatus']='blocked_invalid_dependencies'
            continue
        if story.get('potential') and not story.get('activated'):
            story['planningStatus']='potential'
            continue
        if unanswered.intersection(story.get('dependsOnQuestions') or []) or (story['taskType']=='code' and unanswered):
            story['planningStatus']='waiting_for_answers'
            continue
        completed={t.get('workOrderStoryId') for t in (state.get('tasks') or {}).values()
                   if t.get('workOrderId')==request['id'] and t.get('status')=='done'
                   and not any(t.get(k) for k in ('reviewOf','investigationOf','recoveryOf'))}
        if not set(story.get('prerequisiteStoryIds') or []) <= completed:
            story['planningStatus']='waiting_for_prerequisites'
            continue
        instructions=('Overarching player request: '+request.get('reason','')
            +'\nRelated earlier request context: '+json.dumps(request.get('relatedRequests') or [])
            +'\nParent request completion criteria (context; not requirements for each discovery story): '+str(request.get('acceptanceCriteria') or '')
            +'\nHuman answers: '+json.dumps({q['question']:q.get('answer') for q in plan['questions'] if q.get('answer')})
            +'\nStory: '+story['instructions'])
        prerequisites=[t for t in (state.get('tasks') or {}).values()
                       if t.get('workOrderId')==request['id'] and t.get('workOrderStoryId') in story.get('prerequisiteStoryIds',[])
                       and t.get('status')=='done' and not any(t.get(k) for k in ('reviewOf','investigationOf','recoveryOf'))]
        if prerequisites:
            instructions += '\nCompleted prerequisite artifacts: ' + json.dumps([
                {'storyId':t.get('workOrderStoryId'),'taskId':t['id'],'libraryPath':t.get('libraryPath'),
                 'sandboxId':t.get('_sandboxId') or t.get('sandboxId')} for t in prerequisites])
        sim.queue_work(state,[{'title':story['title'][:200], 'room':'pressoffice' if story['taskType']=='code' else 'observatory',
            'taskType':story['taskType'],'workOrderId':request['id'],'workOrderStoryId':story['id'],
            'scrumMasterId':request.get('scrumMasterId'), 'requestId':request['id'],
            'teamId':request.get('teamId'),'instructions':instructions,'acceptanceCriteria':story['acceptanceCriteria'][:6000],
            'budgetMs':120000 if story['taskType']=='spike' else None,'priority':'normal',
            'projectLabel':request['title'],
            'reviewContract':{'instructions':story['instructions'],'acceptanceCriteria':story['acceptanceCriteria'],
                              'parentWorkOrderId':request['id'],'scope':'work_order_story','version':1}}])
        queued.append(story['id'])
        story['planningStatus']='dispatched'
    request['status']='needs_input' if unanswered else 'accepted'
    request['groomingReason']=plan['summary']


def answer(state, order_id, question_id, text):
    request=next((r for r in state.get('backlogRequests') or [] if r['id']==order_id and r.get('origin')=='player_work_order'),None)
    if not request or not request.get('plan'): raise ValueError('Unknown planned work order.')
    question=next((q for q in request['plan']['questions'] if q['id']==question_id),None)
    if not question: raise ValueError('Unknown question.')
    question.update(status='answered',answer=text,answeredAt=int(time.time()*1000))
    for m in state.get('playerInbox') or []:
        if m.get('workOrderId')==order_id and m.get('questionId')==question_id:
            m.update(status='answered',answer=text)
    release_stories(state,request)
    return request


def step(state):
    orders = [r for r in state.get('backlogRequests') or [] if r.get('origin')=='player_work_order']
    latest = {}
    for request in sorted(orders,key=lambda r:r.get('filedAt',0)):
        if request.get('status') not in ('cancelled','superseded'):
            latest[(request.get('title','').strip().lower(),request.get('teamId'))] = request
    for request in orders:
        if request.get('status')!='rejected' or request.get('restoredByScrumPlanner'): continue
        replacement = latest.get((request.get('title','').strip().lower(),request.get('teamId')))
        request['restoredByScrumPlanner'] = True
        if replacement and replacement['id'] != request['id']:
            replacement.setdefault('relatedRequests',[]).append({'id':request['id'],'reason':request.get('reason'),
                                                               'acceptanceCriteria':request.get('acceptanceCriteria')})
            request.update(status='superseded',supersededBy=replacement['id'],
                groomingReason='Merged into the newer version of your request.')
        else:
            request.update(status='pending',groomingReason='Restored for question-first planning, rather than rejection.')
    for request in orders:
        target_id=request.get('supersededBy')
        target=next((r for r in orders if r['id']==target_id),None) if target_id else None
        if target and not any(r.get('id')==request['id'] for r in target.get('relatedRequests') or []):
            target.setdefault('relatedRequests',[]).append({'id':request['id'],'reason':request.get('reason'),
                                                        'acceptanceCriteria':request.get('acceptanceCriteria')})
    for request in state.get('backlogRequests') or []:
        if request.get('origin')!='player_work_order': continue
        future=_FUTURES.get(request['id'])
        if request.get('status') in ('cancelled','superseded','rejected'):
            if future: future.cancel(); _FUTURES.pop(request['id'],None)
            continue
        if future and future.done():
            try: plan=future.result()
            except Exception as error:
                plan={'summary':'Planner execution failed; discovery is queued. '+str(error)[:300],'stories':[]}
            _FUTURES.pop(request['id'],None)
            apply_plan(state,request,plan,request.get('scrumMasterId') or 'theo')
        elif request.get('status')=='planning' and not future and not request.get('plan'):
            begin(state,request,request.get('scrumMasterId') or 'theo')
        elif request.get('plan') and request.get('status') in ('accepted','needs_input'):
            assess_potential(state,request)
            release_stories(state,request)
        if request.get('plan'):
            for task in (state.get('tasks') or {}).values():
                if task.get('workOrderId') != request['id']: continue
                story = next((story for story in request['plan']['stories'] if story['id']==task.get('workOrderStoryId')),None)
                if story and not task.get('interventionHistory') and (task.get('reviewContract') or {}).get('scope') != 'work_order_story':
                    task['reviewContract']={'instructions':story['instructions'],'acceptanceCriteria':story['acceptanceCriteria'],
                        'parentWorkOrderId':request['id'],'scope':'work_order_story','version':1}



def router_for(serve):
    router=APIRouter(prefix='/api/intent')
    @router.get('/north-star')
    async def get_star(request:Request):
        if not serve._require_player_session(request): return JSONResponse({'error':'Player login required.'},status_code=401)
        return {'northStar':(serve.get_state_from_db() or {}).get('northStar') or {}}
    @router.post('/north-star')
    async def save_star(request:Request):
        if not serve._require_player_session(request): return JSONResponse({'error':'Player login required.'},status_code=401)
        try:
            body=await request.json()
            values={k:body.get(k,'').strip() for k in ('objective','successMeasures','constraints')}
            if not values['objective'] or any(len(v)>6000 for v in values.values()): raise ValueError('Provide an objective; each field is limited to 6000 characters.')
        except (ValueError,AttributeError,TypeError): return JSONResponse({'error':'Provide a valid North Star objective.'},status_code=400)
        with serve._STATE_UPDATE_LOCK:
            state=serve.get_state_from_db() or {}
            previous=state.get('northStar') or {}
            state.setdefault('northStarHistory',[]).append(previous)
            state['northStar']={**values,'version':previous.get('version',0)+1,'updatedAt':int(time.time()*1000)}
            serve.save_state_to_db(state)
        return {'ok':True,'northStar':state['northStar']}
    @router.post('/work-orders/{order_id}/answers')
    async def answer_order(order_id:str,request:Request):
        if not serve._require_player_session(request): return JSONResponse({'error':'Player login required.'},status_code=401)
        try:
            body=await request.json(); answers=body.get('answers')
            if not isinstance(answers,dict) or not answers: raise ValueError('Provide answers.')
            if any(not isinstance(v,str) or not v.strip() or len(v)>6000 for v in answers.values()): raise ValueError('Answers must be nonempty text, up to 6000 characters.')
            with serve._STATE_UPDATE_LOCK:
                state=serve.get_state_from_db() or {}
                for key,value in answers.items(): order=answer(state,order_id,key,value.strip())
                serve.save_state_to_db(state)
            return {'ok':True,'order':order}
        except (ValueError,AttributeError,TypeError) as error: return JSONResponse({'error':str(error)},status_code=400)
    return router
