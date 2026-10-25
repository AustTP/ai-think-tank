"""Durable wake queue for off-duty agents.

`_deliver_mail` used to rely only on the `_mailAwake` marker. The marker is
durable because it lives in saved state, but a crash between filing mail and
the next action tick left no ordered record and no recovery path. This queue
is the source of truth for those wakes: requested, claimed, completed, with
coalescing and a stale-claim recovery pass. The marker remains the fast path
`_mail_action_step` already understands.
"""

WAKE_QUEUE_KEY = 'wakeQueue'
WAKE_QUEUE_MAX = 500
WAKE_CLAIM_TIMEOUT_MS = 30 * 60 * 1000


def _wake_identity(agent_id, kind, entry):
    entry = entry or {}
    about = entry.get('issueKey') or entry.get('taskId') or entry.get('about') or ''
    return f'{agent_id}|{kind}|{about}'


def _wake_queue(state):
    queue = state.get(WAKE_QUEUE_KEY)
    if not isinstance(queue, list):
        queue = []
        state[WAKE_QUEUE_KEY] = queue
    return queue


def _wake_trim(queue):
    if len(queue) <= WAKE_QUEUE_MAX:
        return
    # Drop the oldest completed entries first; never drop live work.
    excess = len(queue) - WAKE_QUEUE_MAX
    kept = []
    for wake in queue:
        if excess and wake.get('status') == 'done':
            excess -= 1
            continue
        kept.append(wake)
    queue[:] = kept[-WAKE_QUEUE_MAX:]


def _wake_enqueue(state, agent_id, kind, entry, now_ms):
    """Queue one wake, coalescing an existing live wake for the same target."""
    queue = _wake_queue(state)
    key = _wake_identity(agent_id, kind, entry)
    for wake in queue:
        if wake.get('key') == key and wake.get('status') in ('queued', 'claimed'):
            wake['entry'] = entry
            wake['requestedAt'] = now_ms
            return wake['id']
    seq = int(state.get('_wakeSeq') or 0) + 1
    state['_wakeSeq'] = seq
    wake_id = f'wake-{seq}'
    queue.append({
        'id': wake_id,
        'agentId': agent_id,
        'kind': kind,
        'entry': entry,
        'key': key,
        'status': 'queued',
        'requestedAt': now_ms,
        'claimedAt': None,
        'completedAt': None,
        'attempts': 0,
        'outcome': None,
    })
    _wake_trim(queue)
    return wake_id


def _wake_claim(state, agent_id, now_ms):
    for wake in _wake_queue(state):
        if wake.get('agentId') == agent_id and wake.get('status') == 'queued':
            wake['status'] = 'claimed'
            wake['claimedAt'] = now_ms
            wake['attempts'] = int(wake.get('attempts') or 0) + 1
            return wake
    return None


def _wake_complete(state, wake_id, outcome, now_ms):
    for wake in _wake_queue(state):
        if wake.get('id') == wake_id:
            wake['status'] = 'done'
            wake['outcome'] = outcome
            wake['completedAt'] = now_ms
            return True
    return False


def _wake_complete_for_agent(state, agent_id, kind, entry, outcome, now_ms):
    key = _wake_identity(agent_id, kind, entry)
    for wake in _wake_queue(state):
        if wake.get('key') == key and wake.get('status') in ('queued', 'claimed'):
            return _wake_complete(state, wake['id'], outcome, now_ms)
    return False


def _wake_recover_stale(state, now_ms):
    """Requeue a claimed wake whose owner never acted before the timeout."""
    recovered = 0
    for wake in _wake_queue(state):
        if wake.get('status') != 'claimed':
            continue
        claimed_at = wake.get('claimedAt') or now_ms
        if now_ms - claimed_at >= WAKE_CLAIM_TIMEOUT_MS:
            wake['status'] = 'queued'
            wake['claimedAt'] = None
            recovered += 1
    return recovered


def _wake_restore_markers(state, now_ms):
    """Recreate a missing `_mailAwake` marker from durable queue entries.

    A crash between the queue write and the next tick cannot lose the wake:
    the next action tick claims it and restores the marker the mailbox pass
    already knows how to route.
    """
    restored = 0
    for wake in _wake_queue(state):
        if wake.get('status') not in ('queued', 'claimed'):
            continue
        agent = (state.get('agents') or {}).get(wake.get('agentId'))
        if not isinstance(agent, dict):
            continue
        if not agent.get('_mailAwake'):
            agent['_mailAwake'] = {
                'kind': wake.get('kind'),
                'entry': wake.get('entry') or {},
                'at': wake.get('requestedAt') or now_ms,
            }
            restored += 1
        if wake.get('status') == 'queued':
            wake['status'] = 'claimed'
            wake['claimedAt'] = now_ms
            wake['attempts'] = int(wake.get('attempts') or 0) + 1
    return restored
