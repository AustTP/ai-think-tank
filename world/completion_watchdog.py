"""Keep unfinished delivery connected to work, review, or an explicit blocker."""
import time

ACTIVE = ('walking', 'working', 'needs_review')
TERMINAL = ('done', 'cancelled', 'abandoned')


def related(state, task):
    tid = task['id']
    children = [t for t in (state.get('tasks') or {}).values()
                if t.get('reviewOf') == tid or t.get('investigationOf') == tid]
    queued = [q for q in state.get('workQueue') or []
              if q.get('reviewOf') == tid or q.get('investigationOf') == tid]
    return children, queued


def block(task, reason):
    task.update(status='blocked', manualAttention=True, blockedReason='completion_recovery',
                failNote=reason, nextAction=reason)
    task.setdefault('_peerGate', {})['escalated'] = True


def step(state, now_ms=None):
    import sim
    if not any(t.get('status') not in TERMINAL for t in (state.get('tasks') or {}).values()):
        return
    now = int(time.time() * 1000) if now_ms is None else now_ms
    control = state.setdefault('completionControl', {})
    control.setdefault('enabled', True)
    control.setdefault('maxConcurrentStories', 5)
    control.setdefault('maxRecoveryAttempts', 3)
    control.setdefault('retryBaseMs', 30000)
    control.setdefault('maxRecoveriesPerTick', 2)
    recovered = 0
    for task in list((state.get('tasks') or {}).values()):
        status = task.get('status')
        if status in TERMINAL:
            continue
        children, queued = related(state, task)
        active = [t for t in children if t.get('status') in ACTIVE]
        if task.get('investigationOf') and status in ('repairing', 'investigating') and not active and not queued:
            completed_repairs = [t for t in children if (t.get('reviewOf') == task['id'] or t.get('investigationOf') == task['id'])
                                 and t.get('status') == 'done' and t.get('libraryPath')
                                 and t.get('taskType') not in ('review', 'qa')]
            if completed_repairs:
                latest = max(completed_repairs, key=lambda t: t.get('createdAt') or 0)
                task.update(status='done', libraryPath=latest['libraryPath'],
                            deliveryState='completed', manualAttention=False,
                            note='Investigation follow-up completed; saved report still does not approve the original deliverable.')
                continue
            replacements = [t for t in (state.get('tasks') or {}).values()
                            if t.get('investigationOf') == task['investigationOf']
                            and t.get('status') == 'done' and t.get('libraryPath')
                            and (t.get('createdAt') or 0) > (task.get('createdAt') or 0)]
            if replacements:
                latest = max(replacements, key=lambda t: t.get('createdAt') or 0)
                task.update(status='cancelled', supersededBy=latest['id'],
                            deliveryState='superseded', manualAttention=False,
                            note='Superseded by completed investigation ' + latest['id'] + '; original story still requires review.')
                continue
        signature = (status, task.get('libraryPath'), tuple((t['id'], t.get('status')) for t in children), len(queued))
        stamp = repr(signature)
        if task.get('_completionSignature') != stamp:
            task.update(_completionSignature=stamp, lastProgressAt=now)
        frozen = task.get('manualAttention') or (task.get('_peerGate') or {}).get('escalated') or status == 'blocked'
        task['deliveryState'] = ('blocked' if frozen else 'awaiting_review' if status == 'needs_review'
                                 else 'actively_working' if status in ('walking','working') or active
                                 else 'queued' if queued else 'stalled')
        task['nextAction'] = ('Resolve the recorded blocker using story guidance.' if frozen else
                              'Finish the current work and run its acceptance checks.' if active or status in ('walking','working') else
                              'Dispatch the queued follow-up.' if queued else
                              'Obtain independent reviewer decisions.' if status == 'needs_review' else
                              'Recover the missing follow-up for this story.')
        if not control['enabled'] or frozen or task.get('reviewOf') or task.get('investigationOf') or queued or active:
            continue
        if status not in ('investigating', 'recovery_planned', 'repairing') and not (status == 'failed' and task.get('_peerRepairPending')):
            if status == 'failed' and not task.get('boardClearedAt'):
                block(task, 'Choose a recovery action for the failed story. ' + str(task.get('failNote') or 'Inspect its execution evidence.'))
            continue
        record = task.setdefault('completionRecovery', {'attempts': 0})
        if task.get('budgetExhausted') or any(t.get('budgetExhausted') for t in children):
            block(task, 'Recovery needs a budget decision; automatic retries cannot increase the task budget.')
            continue
        if record['attempts'] >= control['maxRecoveryAttempts']:
            block(task, 'Automatic recovery made no completion after the retry limit. Inspect evidence, supply inputs, change owner or split the story.')
            continue
        if record.get('nextAttemptAt', 0) > now or recovered >= control['maxRecoveriesPerTick']:
            task['nextAction'] = 'Retry recovery after the scheduled backoff.'
            continue
        kind = task.get('reviewFailureKind')
        failure = str(task.get('failNote') or '').lower()
        if kind == 'missing_inputs' or any(w in failure for w in ('credentials', 'not configured', 'missing human decision')):
            block(task, 'Supply the missing inputs before this story can continue. ' + str(task.get('failNote') or ''))
            continue
        if status == 'investigating':
            investigations = [t for t in children if t.get('investigationOf') == task['id']]
            latest = max(investigations, key=lambda t: t.get('createdAt') or 0) if investigations else None
            if latest and latest.get('status') == 'done':
                task.update(status='recovery_planned', investigationReport=latest.get('libraryPath'))
            else:
                task.pop('recoveryStaffed', None)
                import recovery_staffing
                recovery_staffing.step(state)
                if not related(state, task)[1]:
                    block(task, 'Assign an available recovery owner and worker to investigate this story.')
                record.update(attempts=record['attempts'] + 1, nextAttemptAt=now + control['retryBaseMs'] * 2 ** record['attempts'])
                recovered += 1
                continue
        owner = task.get('originalAssignedTo') or task.get('assignedTo')
        if owner not in (state.get('agents') or {}):
            block(task, 'Assign an existing worker to recover this story.')
            continue
        task['assignedTo'] = owner
        if not task.get('_peerGate'):
            task['_peerGate'] = {'escalated': False}
        if kind in ('invalid_response', 'service_unavailable') or 'review validation failed' in failure:
            gate = task.setdefault('_peerGate', {})
            if gate.get('formatFailures', 0) >= 3:
                block(task, 'Review response formatting failed three times. Fix the review service and explicitly retry review; another investigation is not new validation evidence.')
                continue
            gate.update(formatFailures=0, serviceFailures=0, inputFailures=0)
            task.update(status='needs_review', reviewFailureKind=kind or 'invalid_response')
            sim._enter_peer_review(state, task, now)
        else:
            rationale = str(task.get('failNote') or 'Complete the original acceptance criteria.')
            if task.get('investigationReport'):
                rationale += '\nRead the recovery diagnosis: ' + task['investigationReport']
            sim._queue_peer_repair(state, task, rationale)
            task['status'] = 'repairing'
        children, queued = related(state, task)
        delay = control['retryBaseMs'] * 2 ** record['attempts']
        for item in queued:
            item['notBefore'] = max(item.get('notBefore') or 0, now + delay)
            item['taskMasterWork'] = True
        record.update(attempts=record['attempts'] + 1, nextAttemptAt=now + delay)
        recovered += 1
        if not queued and not any(t.get('status') in ACTIVE for t in children):
            block(task, 'Recovery could not queue work; assign an available worker or reviewer.')
        else:
            task.update(deliveryState='queued', nextAction='Run the linked recovery and submit the original story for review.')
    control.update(lastCheckedAt=now, recoveredThisTick=recovered)


def can_start(state, item):
    """Recovery/review can finish admitted stories even when the WIP cap is full."""
    if item.get('reviewOf') or item.get('investigationOf'):
        return True
    if not (state.get('completionControl') or {}).get('enabled'):
        return True
    limit = (state.get('completionControl') or {}).get('maxConcurrentStories', 5)
    count = sum(t.get('status') in ('walking', 'working', 'needs_review', 'repairing', 'investigating')
                and not t.get('reviewOf') and not t.get('investigationOf') and not t.get('manualAttention')
                for t in (state.get('tasks') or {}).values())
    return count < limit


def complete_spike_repair(parent, result, now_ms):
    """Spikes finish with saved findings; delivery stories still require review."""
    if (parent.get('taskType') != 'spike' or not result.get('ok', True)
            or not result.get('libraryPath') or not str(result.get('note') or '').strip()
            or parent.get('manualAttention') or (parent.get('_peerGate') or {}).get('escalated')):
        return False
    parent.update(status='done', completedAt=now_ms, failNote=None,
                  libraryPath=result['libraryPath'], note=result['note'])
    parent.pop('_peerRepairPending', None)
    return True
