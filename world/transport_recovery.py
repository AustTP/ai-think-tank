"""Retry transient coding transport failures without treating them as code defects."""
import story_tracking


def queue(state, task, result, now_ms):
    control = state.get('completionControl') or {}
    note = str(result.get('note') or '')
    if (control.get('approvalPreset') != 'always' or result.get('ok') is not False
            or result.get('budgetExhausted') or 'Coding model request failed:' not in note
            or not any(text in note.lower() for text in ('remote end closed connection', 'connection reset',
                'connection refused', 'timed out', 'service unavailable'))):
        return False
    parent = story_tracking.root(state, task)
    if not __import__('completion_watchdog').can_start(state, {'reviewOf':parent['id']}):
        return False
    if any(q.get('reviewOf') == parent['id'] and q.get('taskType') not in ('review', 'qa')
           for q in state.get('workQueue', [])):
        return False
    retry = parent.setdefault('transportRecovery', {'attempts':0, 'history':[]})
    retry['attempts'] += 1
    attempt = retry['attempts']
    delay = 0 if attempt == 1 else min(900000, 15000 * 2 ** min(attempt-2, 6))
    retry.update(lastAttemptAt=now_ms, nextAttemptAt=now_ms+delay)
    retry['history'].append({'taskId':task['id'],'at':now_ms,'failure':note[:800],'retryAt':now_ms+delay})
    retry['history'] = retry['history'][-30:]
    if result.get('stagedWorkspace'):
        parent['lastFailedWorkspace'] = result['stagedWorkspace']
    parent.update(status='repairing', manualAttention=False, blockedReason=None,
        _peerRepairPending=True,
        failureCategory='infrastructure', deliveryState='queued',
        note='Model connection failed; retry queued with the existing workspace.',
        nextAction='Resume the saved task immediately.' if not delay else 'Retry the model request after scheduled transport backoff.')
    parent.setdefault('_peerGate', {}).update(escalated=False, approvals=0, approvers=[], acceptanceVerified=False)
    import sim
    sim.queue_work(state,[{'title':task.get('title') or 'Retry coding request', 'room':task.get('room') or 'pressoffice',
        'taskType':task.get('taskType') or 'code','assignedTo':task.get('assignedTo'),'directRoute':True,
        'reviewOf':parent['id'],'reviewAuthorId':parent.get('originalAssignedTo') or parent.get('assignedTo'),
        'instructions':str(task.get('instructions') or parent.get('instructions') or '')
            +'\nThe previous model connection ended without a response. Resume the saved workspace; '
            'make small edits, preserve all acceptance criteria and rerun quality checks before independent review.',
        'reviewContract':parent.get('reviewContract'),'acceptanceCriteria':parent.get('acceptanceCriteria'),
        'workOrderId':parent.get('workOrderId'),'workOrderStoryId':parent.get('workOrderStoryId'),
        'requestId':parent.get('requestId'),'projectLabel':parent.get('projectLabel'),
        'budgetUsd':parent.get('budgetUsd'),'notBefore':now_ms+delay,'priority':'high'}])
    return True
