"""A single decision queue with evidence, suggested actions and explicit repairs."""
import asyncio
import json
import subprocess
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def cards(state):
    result = []
    for task in (state.get('tasks') or {}).values():
        if task.get('boardClearedAt') or task.get('status') in ('done', 'cancelled', 'abandoned', 'walking', 'working'):
            continue
        if not task.get('manualAttention') and not (task.get('_peerGate') or {}).get('escalated'):
            continue
        evidence = ((task.get('reviewDiagnosis') or {}).get('failure') or task.get('failNote')
                    or task.get('note') or 'No detailed failure was recorded.')
        checks = [str(c) for review in (task.get('peerReviews') or [])[-2:]
                  for c in review.get('checks') or []]
        combined = (evidence + ' ' + ' '.join(checks)).lower()
        failure_kind = task.get('reviewFailureKind')
        if failure_kind == 'invalid_response':
            category, action = 'invalid_review_response', 'retry_review'
        elif failure_kind == 'service_unavailable':
            category, action = 'review_service_unavailable', 'retry_review'
        elif failure_kind == 'missing_inputs':
            category, action = 'review_missing_inputs', 'provide_inputs'
        elif any(word in combined for word in ('docker', 'http 500', 'connection', 'timed out', 'could not start')):
            category, action = 'infrastructure', 'retry_infrastructure'
        elif any(word in combined for word in ('missing input', 'missing data', 'credentials', 'not configured')):
            category, action = 'missing_inputs', 'provide_inputs'
        elif task.get('tradingStage'):
            category, action = 'acceptance_failure', 'assign_repair'
        elif not task.get('acceptanceCriteria') or 'acceptance' in combined:
            category, action = 'unclear_requirements', 'clarify_scope'
        elif task.get('room') == 'pressoffice' or task.get('productId'):
            category, action = 'code_failure', 'assign_repair'
        elif task.get('status') == 'needs_review' and not task.get('peerReviews'):
            category, action = 'reviewer_unavailable', 'assign_repair'
        else:
            category, action = 'deliverable_failure', 'assign_repair'
        team = next((t for t in state.get('teams', []) if t.get('id') == task.get('teamId')), {})
        recovery_owner = team.get('scrumMasterId') or task.get('assignedTo')
        repairs = [t for t in (state.get('tasks') or {}).values() if t.get('reviewOf') == task['id']
                   and t.get('taskType') not in ('review','qa')]
        next_action = ('Restore review service and retry the review; do not rewrite the deliverable.'
                       if failure_kind == 'service_unavailable' else
                       'Correct the structured review response and retry; no artifact repair required.'
                       if failure_kind == 'invalid_response' else
                       'Supply the missing inputs before retrying.' if failure_kind == 'missing_inputs' else
                       'Complete the repair stories and submit the corrected artifact for review.')
        result.append({'taskId': task['id'], 'title': task.get('title'), 'owner': task.get('assignedTo'),
            'category': category, 'evidence': evidence[:4000], 'findings': checks,
            'recoveryOwner': recovery_owner, 'nextAction': next_action, 'needsHumanInput': True,
            'diagnosticsTaskId':task.get('lastReviewDiagnosticsTask'),
            'repairs':[{'id':r['id'],'owner':r.get('assignedTo'),'status':r.get('status'),'criteria':r.get('acceptanceCriteria')} for r in repairs[-6:]],
            'recommendedAction': action, 'acceptanceCriteria': task.get('acceptanceCriteria'),
            'actions': (['retry_review'] if failure_kind in ('invalid_response','service_unavailable') or task.get('blockedReason') == 'review_model_response' else []) + ['provide_inputs', 'clarify_scope', 'assign_repair', 'retry_infrastructure', 'cancel']})
    return result


def reconcile_notices(state):
    open_cards = {c['taskId']: c for c in cards(state)}
    inbox = state.setdefault('playerInbox', [])
    for message in inbox:
        if message.get('kind') not in ('review_blocked', 'work_blocked') or message.get('status') != 'awaiting_guidance':
            continue
        if message.get('taskId') not in open_cards:
            message.update(status='resolved', answer='No longer awaiting intervention: task resumed, completed, cancelled or dismissed.')
    for tid, card in open_cards.items():
        question = (f"{card['title']}\nReason: {card['category']}\nEvidence: {card['evidence']}\n"
                    f"Recommended action: {card['recommendedAction']}\nResponsible agent: {card['owner']}")
        message = next((m for m in inbox if m.get('taskId') == tid and m.get('kind') in ('review_blocked', 'work_blocked')
                        and m.get('status') == 'awaiting_guidance'), None)
        if message:
            message.update(kind='work_blocked', question=question)
        else:
            inbox.append({'id': f'blocker-{tid}-{int(time.time()*1000)}', 'kind': 'work_blocked', 'taskId': tid,
                'agentId': card['owner'] or 'player', 'issueKey': tid, 'question': question,
                'status': 'awaiting_guidance', 'createdAt': int(time.time()*1000)})


def apply_action(serve, tid, body):
    import sim, trading_workflow, code_director, review_guidance
    if not isinstance(body, dict):
        raise ValueError('Expected a JSON object.')
    action = body.get('action')
    if action not in ('retry_review', 'provide_inputs', 'clarify_scope', 'assign_repair', 'retry_infrastructure', 'cancel'):
        raise ValueError('Choose a supported decision.')
    guidance = body.get('guidance') or ''
    criteria = body.get('acceptanceCriteria') or ''
    if not isinstance(guidance, str) or not isinstance(criteria, str):
        raise ValueError('Instructions and acceptance criteria must be text.')
    if action in ('provide_inputs', 'clarify_scope') and not guidance.strip():
        raise ValueError('Provide the inputs or replacement instructions.')
    if action == 'clarify_scope' and not criteria.strip():
        raise ValueError('Provide measurable acceptance criteria.')
    if action == 'retry_infrastructure':
        try:
            probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                   capture_output=True, encoding='utf-8', errors='replace', timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f'Docker is not ready: {error}') from None
        if probe.returncode:
            raise ValueError('Docker is still unavailable. Start Docker Desktop before retrying.')
    with serve._STATE_UPDATE_LOCK:
        state = serve.get_state_from_db() or {}
        task = (state.get('tasks') or {}).get(tid)
        if not task or tid not in {c['taskId'] for c in cards(state)}:
            raise ValueError('This task is no longer awaiting a decision.')
        old_gate = dict(task.get('_peerGate') or {})
        task.setdefault('decisionHistory', []).append({'action': action, 'at': int(time.time()*1000),
            'guidance': guidance, 'previousGate': old_gate})
        state['workQueue'] = [q for q in state.get('workQueue') or [] if q.get('reviewOf') != tid
                             and not (task.get('tradingStage') and q.get('tradingStage') == task['tradingStage'])]
        if action == 'retry_review':
            if task.get('reviewFailureKind') not in ('invalid_response','service_unavailable') and task.get('blockedReason') != 'review_model_response':
                raise ValueError('Retry review is only for a failed review request or invalid response; repair deliverable failures first.')
            gate = task.get('_peerGate') or {}
            gate.update(escalated=False, formatFailures=0, serviceFailures=0, inputFailures=0,
                        enteredMs=int(time.time()*1000))
            task.update(status='needs_review', manualAttention=False, blockedReason=None)
            if gate.get('reviewerIds'):
                sim._reenter_gate_review(state, task, gate['reviewerIds'])
            else:
                sim._enter_peer_review(state, task, int(time.time()*1000))
        elif action == 'cancel':
            task.update(status='cancelled', manualAttention=False)
            if task.get('tradingStage'):
                state['tradingWorkflow']['phase'] = 'cancelled'
        elif task.get('tradingStage'):
            stage = task['tradingStage']
            task.update(status='cancelled', manualAttention=False)
            state['tradingWorkflow']['phase'] = 'queued'
            instructions = trading_workflow.stage_instructions(state, stage)
            if guidance:
                instructions += '\nHuman instructions: ' + guidance
            sim.queue_work(state, [{'title': f'Coinbase spot workflow: {stage}', 'room': 'observatory',
                'taskType': 'spike', 'tradingStage': stage, 'assignedTo': trading_workflow.OWNERS[stage],
                'directRoute': True, 'instructions': instructions,
                'acceptanceCriteria': trading_workflow.CONTRACTS[stage], 'priority': 'high', 'budgetUsd': 0.50}])
        elif action in ('assign_repair', 'retry_infrastructure') and sim._peer_coding_class(task):
            task['_peerGate'] = {'approvals': 0, 'approvers': [], 'reviewerIds': old_gate.get('reviewerIds', []),
                                 'cycleCount': 0, 'escalated': False}
            task['manualAttention'] = False
            if code_director.queue_check(state, task, guidance or task.get('failNote')) is None:
                raise ValueError('No Code Director is available.')
        else:
            if action == 'clarify_scope':
                task['instructions'] = guidance.strip()
            review_guidance.apply_guidance(state, tid, {
                'action': 'edit_scope' if action == 'clarify_scope' else 'provide_inputs' if action == 'provide_inputs' else 'retry',
                'guidance': guidance or task.get('failNote') or 'Address the recorded reviewer findings.',
                'acceptanceCriteria': criteria, 'taskType': body.get('taskType') or task.get('taskType') or 'research'})
        reconcile_notices(state)
        serve.save_state_to_db(state)
        if hasattr(serve, '_load_escalations'):
            escalations = serve._load_escalations()
            for escalation in escalations.values():
                if escalation.get('kind') == 'stuck review loop' and escalation.get('status') == 'pending' \
                        and review_guidance.escalation_task_id(escalation, state) == tid:
                    escalation.update(status='guided', resolvedBy='player', guidanceAction=action, resolvedAt=time.time())
            serve._save_escalations(escalations)
        serve.log_action('player', 'blocker_decision', {'taskId': tid, 'action': action}, authorized=True)
        return {'ok': True, 'taskId': tid, 'action': action}


def router_for(serve):
    router = APIRouter(prefix='/api/intent/blockers')
    @router.get('')
    async def list_blockers(request: Request):
        if not serve._require_player_session(request):
            return JSONResponse({'error': 'Player login required.'}, status_code=401)
        state = serve.get_state_from_db() or {}
        return {'ok': True, 'blockers': cards(state), 'workflow': state.get('tradingWorkflow')}
    @router.post('/{task_id}')
    async def decide(task_id: str, request: Request):
        if not serve._require_player_session(request):
            return JSONResponse({'error': 'Player login required.'}, status_code=401)
        try:
            body = await request.json()
            return await asyncio.to_thread(apply_action, serve, task_id, body)
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)
    @router.post('/{task_id}/artifact')
    async def attach_artifact(task_id: str, request: Request):
        if not serve._require_player_session(request):
            return JSONResponse({'error': 'Player login required.'}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Expected a Library report path.')
            return await asyncio.to_thread(submit_artifact, serve, task_id, body.get('libraryPath'))
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)
    @router.post('/{task_id}/complete')
    async def accept_failures(task_id: str, request: Request):
        if not serve._require_player_session(request):
            return JSONResponse({'error': 'Player login required.'}, status_code=401)
        try:
            body = await request.json()
            return await asyncio.to_thread(complete_with_exceptions, serve, task_id, body.get('reason') if isinstance(body, dict) else None)
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)
    return router


def complete_with_exceptions(serve, task_id, reason):
    import copy
    import story_tracking
    import sim
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError('Explain which failed requirements you accept (maximum 4000 characters).')
    with serve._STATE_UPDATE_LOCK:
        state = serve.get_state_from_db() or {}
        task = (state.get('tasks') or {}).get(task_id)
        if not task or any(task.get(k) for k in story_tracking.LINKS):
            raise ValueError('Choose an original story, rather than its subtask or review.')
        if task.get('status') == 'done' and task.get('playerAcceptance'):
            return {'ok': True, 'taskId': task_id, 'alreadyAccepted': True}
        if task.get('status') not in ('blocked', 'failed', 'needs_review', 'repairing') or not (task.get('manualAttention') or (task.get('_peerGate') or {}).get('escalated') or task.get('status') in ('blocked','failed')):
            raise ValueError('Only blocked or failed stories can be completed with exceptions.')
        now = int(time.time()*1000)
        task['playerAcceptance'] = {'at':now, 'by':'player', 'reason':reason.strip(),
            'failedFindings':copy.deepcopy(task.get('peerReviews') or []),
            'diagnosis':copy.deepcopy(task.get('reviewDiagnosis')),
            'previousGate':copy.deepcopy(task.get('_peerGate')), 'failure':task.get('failNote'),
            'qualityStatus':'accepted_with_exceptions', 'tradingAuthorized':False}
        children = [t for t in state.get('tasks', {}).values() if t['id'] != task_id and story_tracking.root(state,t).get('id') == task_id]
        ids = {t['id'] for t in children} | {task_id}
        state['workQueue'] = [q for q in state.get('workQueue',[]) if not any(q.get(k) in ids for k in story_tracking.LINKS)]
        for child in children:
            if child.get('status') not in ('done','cancelled','abandoned'):
                child.update(status='cancelled', cancelledAt=now, note='Parent story accepted by player with recorded exceptions.')
            agent = state.get('agents',{}).get(child.get('assignedTo'))
            if agent and agent.get('task') == child['id']:
                if child.get('pairWith'):
                    sim._release_pair_navigator(state, child['pairWith'], child['id'])
                agent.update(task=None, busy=False, inRoom=None, path=None, offDuty=True, visible=False)
        task.update(status='done', completedAt=now, manualAttention=False,
                    blockedReason=None, deliveryState='accepted_with_exceptions',
                    nextAction='Completed by the player with recorded exceptions.')
        task.pop('_peerRepairPending', None)
        task['_peerGate'] = dict(task.get('_peerGate') or {})
        task['_peerGate']['escalated'] = True
        reconcile_notices(state)
        serve.save_state_to_db(state)
        serve.log_action('player','story_accepted_with_exceptions', {'taskId':task_id,'reason':reason.strip()}, authorized=True)
        return {'ok':True,'taskId':task_id,'status':'done','qualityStatus':'accepted_with_exceptions','tradingAuthorized':False}


def submit_artifact(serve, task_id, library_path):
    """Submit an inspected replacement deliverable to independent review, never approve it."""
    import os
    import sim
    import review_quality
    if not isinstance(library_path, str):
        raise ValueError('Provide a Library report path.')
    target = serve._safe_library_path(library_path)
    if not target or not os.path.isfile(target):
        raise ValueError('The submitted Library report does not exist.')
    with open(target, encoding='utf-8') as stream:
        report = stream.read(40000)
    with serve._STATE_UPDATE_LOCK:
        state = serve.get_state_from_db() or {}
        task = (state.get('tasks') or {}).get(task_id)
        if not task or not (task.get('manualAttention') or (task.get('_peerGate') or {}).get('escalated')) or task.get('status') in ('done', 'cancelled', 'working', 'walking'):
            raise ValueError('Only a blocked deliverable can receive replacement evidence.')
        if sim._peer_coding_class(task):
            raise ValueError('Code repairs must pass the code director and quality pipeline.')
        contract = review_quality.contract_for(task)
        checks = review_quality.artifact_checks(report, contract, serve._safe_library_path, task.get('notebookExecution'))
        if not checks['passed']:
            raise ValueError('Artifact checks failed: ' + '; '.join(checks['errors']))
        now = int(time.time()*1000)
        task.setdefault('evidenceSubmissionHistory', []).append({'at': now, 'previousLibraryPath': task.get('libraryPath'),
            'libraryPath': library_path, 'reportSha256': review_quality.digest(report), 'checks': checks})
        task.update(libraryPath=library_path, reviewContract=contract, manualAttention=False, blockedReason=None,
                    failNote=None, note='Saved Library artifacts inspected; independent peer review pending.')
        if task.get('reviewDiagnosis'):
            task.setdefault('reviewDiagnosisHistory', []).append(task.pop('reviewDiagnosis'))
        task.pop('_peerRepairPending', None)
        old_reviewers = (task.get('_peerGate') or {}).get('reviewerIds') or []
        task['_peerGate'] = {'approvals': 0, 'approvers': [], 'cycleCount': 0, 'escalated': False}
        state['workQueue'] = [q for q in state.get('workQueue') or [] if q.get('reviewOf') != task_id]
        sim._enter_peer_review(state, task, now, preferred=old_reviewers)
        reconcile_notices(state)
        serve.save_state_to_db(state)
        serve.log_action('player', 'replacement_artifact_submitted', {'taskId': task_id, 'libraryPath': library_path}, authorized=True)
        return {'ok': True, 'taskId': task_id, 'status': task.get('status'), 'checks': checks, 'approved': False}
