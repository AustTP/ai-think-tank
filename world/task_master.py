"""Demand-based work discovery for idle delivery workers."""
import copy
import math
import time

AGENT_ID = 'task-master'
COOLDOWN_MS = 5 * 60 * 1000
BATCH_SIZE = 2


def create_role(state):
    agents = state.setdefault('agents', {})
    roster = state.setdefault('agentRoster', [])
    definition = {'id': AGENT_ID, 'name':'Atlas', 'role':'Task Master', 'model':'small', 'color':'#f97316',
        'taskMaster':True, 'villageId':'main', 'approvedCount':0, 'droppedCount':0,
        'profile':{'mission':'Find useful work when at least half of delivery workers are idle.',
            'instructions':['Staff queued work first. Preserve task scope and acceptance criteria.',
                'Create concrete repair or workflow improvement stories from observed failures.',
                'Avoid duplicates and filler. Keep quality checks, independent review and trading restrictions.'], 'notes':[]}}
    if not any(d.get('id') == AGENT_ID for d in roster): roster.append(copy.deepcopy(definition))
    if AGENT_ID not in agents:
        agents[AGENT_ID] = {**definition, 'x':510.0,'y':272.0,'dir':'south','offDuty':True,'visible':False,
            'busy':False,'task':None,'inRoom':None,'roomX':None,'roomY':None,'meetingId':None,
            'mailbox':[],'conversationLog':[],'hiredAt':int(time.time()*1000)}
    return AGENT_ID


def workers(state):
    masters = {t.get('scrumMasterId') for t in state.get('teams') or []}
    excluded = ('isAdmin','isDirector','dedicatedReviewer','codeDirector','tradingSpecialist','taskMaster','librarian')
    return [d['id'] for d in state.get('agentRoster') or [] if d.get('id') in (state.get('agents') or {})
            and d['id'] not in masters and not any(d.get(flag) for flag in excluded)]


def step(state, now_ms=None):
    import sim
    create_role(state)
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    control = state.setdefault('taskMasterControl', {'enabled':True, 'idleThreshold':0.5, 'history':[]})
    pool = workers(state)
    agents = state.get('agents') or {}
    active = {t.get('assignedTo') for t in (state.get('tasks') or {}).values() if t.get('status') in ('walking','working')}
    idle = [aid for aid in pool if aid not in active and not agents[aid].get('busy')
            and not agents[aid].get('task') and not agents[aid].get('pairWith')]
    control.update(totalWorkers=len(pool), idleWorkers=len(idle), observedAt=now_ms)
    if not control.get('enabled',True) or not pool or len(idle)/len(pool) < .5:
        control['status'] = 'monitoring'
        return []
    if control.get('history') and now_ms - control.get('lastRunAt',0) < COOLDOWN_MS:
        control['status'] = 'cooldown'
        return []
    control['lastRunAt'] = now_ms
    count = min(BATCH_SIZE, max(1,len(idle) - math.ceil(len(pool)/2) + 1))
    assigned = []
    for item in state.get('workQueue') or []:
        if len(assigned) >= count: break
        if item.get('reviewOf') or item.get('sprintId') or not sim.is_work_item_due(item,now_ms) or not sim._work_item_dependency_met(state,item): continue
        pinned = item.get('assignedTo')
        if pinned and (pinned not in idle or pinned in assigned): continue
        owner = pinned or next((aid for aid in idle if aid not in assigned),None)
        if not owner: break
        item.update(assignedTo=owner,directRoute=True,taskMasterWork=True)
        assigned.append(owner)
    pending = [r for r in state.get('backlogRequests') or []
               if r.get('status') == 'pending' and sim._refinement_team(state,r)]
    for request in pending:
        sim.kick_refinement_now(state,(sim._refinement_team(state,request) or {}).get('id'),now_ms)
    if len(assigned) < count and not pending and not state.get('workQueue'):
        candidates = [t for t in (state.get('tasks') or {}).values() if t.get('manualAttention')
                      and t.get('status') not in ('done','cancelled','abandoned') and not t.get('boardClearedAt')]
        if candidates:
            target = candidates[-1]
            key = 'diagnose:' + target['id'] + ':' + str(target.get('failedAt') or (target.get('_peerGate') or {}).get('enteredMs') or 0)
            title = 'Resolve workflow bottleneck: ' + target.get('title','blocked work')
            instructions = ('Investigate the observed blocker for ' + target['id'] + ': '
                + str(target.get('failNote') or target.get('note') or 'No failure details recorded.')
                + '. Read the saved report and actual artifacts. Do not restart or approve the blocked task. '
                'Save a scoped repair plan with concrete evidence, root cause, owner and measurable acceptance criteria. '
                'If historical data is missing, use coinbase_collect_history to create an actual dated CSV and cite its path. '
                'No live transactions are permitted.')
        elif any(t.get('status') in ('investigating', 'recovery_planned', 'repairing', 'needs_review')
                 and not t.get('manualAttention') for t in (state.get('tasks') or {}).values()):
            control['status'] = 'finishing_existing_work'
            return assigned
        else:
            key = 'workflow-audit:' + str(now_ms//86400000)
            title = 'Improve delivery workflow from recent task outcomes'
            instructions = ('Read existing Library reports and identify one measurable workflow bottleneck relevant to '
                + str((state.get('northStar') or {}).get('objective') or state.get('researchFocus') or 'current queued projects')
                + '. Save sourced findings and up to three concrete improvement stories with owners and acceptance criteria. '
                'Do not invent issues or create filler; report insufficient evidence when necessary. No live transactions.')
        if key not in control.setdefault('discovered',[]):
            owner = next((aid for aid in idle if aid not in assigned),None)
            if owner:
                sim.queue_work(state,[{'title':title,'room':'observatory','taskType':'spike','budgetMs':120000,
                    'budgetUsd':0.30,'assignedTo':owner,'directRoute':True,'taskMasterWork':True,
                    'instructions':__import__('company_planning').north_star_context(state) + '\n' + instructions,'acceptanceCriteria':'Save evidence of the bottleneck and actionable repair or improvement stories with explicit owners and testable completion checks.',
                    'projectLabel':'Workflow improvement','priority':'normal'}])
                control['discovered'].append(key)
                assigned.append(owner)
    control['status'] = 'staffing' if assigned else 'waiting_for_existing_work'
    if assigned:
        agents[AGENT_ID].setdefault('profile',{}).setdefault('notes',[]).append(
            {'at':now_ms,'text':f'Assigned {len(assigned)} workers because {len(idle)}/{len(pool)} delivery workers were idle.'})
        agents[AGENT_ID]['profile']['notes'] = agents[AGENT_ID]['profile']['notes'][-30:]
        control.setdefault('history',[]).append({'at':now_ms,'workers':assigned,'idleWorkers':len(idle),'totalWorkers':len(pool)})
        control['history'] = control['history'][-30:]
    return assigned
