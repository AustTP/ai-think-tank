"""Retry transient model transport failures without treating them as code defects."""
import agents.story_tracking as story_tracking

TRANSIENT_MARKERS = (
    'remote end closed connection', 'connection reset', 'connection refused',
    'timed out', 'timeout', 'service unavailable', 'temporarily unavailable',
    'model call failed or returned nothing', 'request failed:',
)


def _is_transient(note, result):
    if result.get('infra') is True:
        return 'queued'
    lower = note.lower()
    return any(marker in lower for marker in TRANSIENT_MARKERS)


def queue(state, task, result, now_ms):
    control = state.get('completionControl') or {}
    note = str(result.get('note') or '')
    if result.get('ok') is not False or result.get('budgetExhausted'):
        return False
    if not _is_transient(note, result):
        return False
    if task.get('manualAttention') or task.get('blockedReason'):
        return False
    parent = story_tracking.root(state, task)
    if (not parent.get('id')
            or parent.get('status') in ('done', 'cancelled', 'abandoned', 'blocked')
            or parent.get('manualAttention') or parent.get('budgetExhausted')):
        return False
    coding = (task.get('taskType') in ('code', 'qa', 'bug')
              or parent.get('taskType') in ('code', 'qa', 'bug')
              or bool(task.get('writtenPy')) or bool(parent.get('writtenPy'))
              or 'coding model request failed' in note.lower())
    if not coding and result.get('infra') is not True:
        # A non-coding failure with no explicit infra tag is the agent's own
        # poor quality, not transport: let the normal failure path handle it.
        return False
    if any(q.get('reviewOf') == parent['id'] and q.get('taskType') not in ('review', 'qa')
           for q in state.get('workQueue', [])):
        return False
    max_attempts = int(control.get('transportRecoveryAttempts') or 3)
    retry = parent.setdefault('transportRecovery', {'attempts': 0, 'history': []})
    if retry['attempts'] >= max_attempts:
        parent.update(status='blocked', manualAttention=True,
                      blockedReason='transport_retry_limit',
                      failNote='Model transport failed repeatedly; retry limit reached. Latest: '
                               + note[:1800])
        return 'blocked'
    retry['attempts'] += 1
    attempt = retry['attempts']
    if coding:
        delay = 0 if attempt == 1 else min(900000, 15000 * 2 ** min(attempt - 2, 6))
    else:
        # Non-coding retries re-run the whole request, so they cost a full
        # model call: back off from the first attempt (60s doubling, 15m max).
        delay = 0 if attempt == 1 else min(900000, 60000 * 2 ** min(attempt - 2, 4))
    retry.update(lastAttemptAt=now_ms, nextAttemptAt=now_ms + delay)
    retry['history'].append({'taskId': task['id'], 'at': now_ms,
                             'failure': note[:800], 'retryAt': now_ms + delay})
    retry['history'] = retry['history'][-30:]
    if result.get('stagedWorkspace'):
        parent['lastFailedWorkspace'] = result['stagedWorkspace']
    parent.update(status='repairing', manualAttention=False, blockedReason=None,
                  _peerRepairPending=True, failureCategory='infrastructure',
                  deliveryState='queued',
                  note='Model connection failed; retry scheduled for this same story.',
                  nextAction=('Retry the model request now.' if not delay else
                              'Retry the model request after scheduled transport backoff.'))
    parent.setdefault('_peerGate', {}).update(escalated=False, approvals=0,
                                              approvers=[], acceptanceVerified=False)
    if coding:
        resume_instruction = ('The previous model connection ended without a response. Resume the saved '
                              'workspace; make small edits, preserve all acceptance criteria and rerun '
                              'quality checks before independent review.')
    else:
        resume_instruction = ('The previous model connection ended without a response. Redo the original '
                              'request from scratch; this is a transient infrastructure fault, not a '
                              'rejection of your work.')
    import sim
    sim.queue_work(state, [{
        'title': task.get('title') or 'Retry model request',
        'room': task.get('room') or 'pressoffice',
        'taskType': task.get('taskType') or 'code',
        'assignedTo': task.get('assignedTo'),
        'directRoute': True,
        'reviewOf': parent['id'],
        'reviewAuthorId': parent.get('originalAssignedTo') or parent.get('assignedTo'),
        'instructions': str(task.get('instructions') or parent.get('instructions') or '')
            + '\n' + resume_instruction,
        'reviewContract': parent.get('reviewContract'),
        'acceptanceCriteria': parent.get('acceptanceCriteria'),
        'workOrderId': parent.get('workOrderId'),
        'workOrderStoryId': parent.get('workOrderStoryId'),
        'requestId': parent.get('requestId'),
        'projectLabel': parent.get('projectLabel'),
        'budgetUsd': parent.get('budgetUsd'),
        'notBefore': now_ms + delay,
        'priority': 'high',
    }])
    return 'queued'
