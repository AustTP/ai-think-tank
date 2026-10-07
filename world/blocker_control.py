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
        if any(word in combined for word in ('docker', 'http 500', 'connection', 'timed out', 'could not start')):
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
        result.append({'taskId': task['id'], 'title': task.get('title'), 'owner': task.get('assignedTo'),
            'category': category, 'evidence': evidence[:4000], 'findings': checks,
            'recommendedAction': action, 'acceptanceCriteria': task.get('acceptanceCriteria'),
            'actions': ['provide_inputs', 'clarify_scope', 'assign_repair', 'retry_infrastructure', 'cancel']})
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
    if action not in ('provide_inputs', 'clarify_scope', 'assign_repair', 'retry_infrastructure', 'cancel'):
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
        if action == 'cancel':
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
    return router
