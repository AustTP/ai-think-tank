"""Durable story-wide recovery accounting and failure routing."""
import hashlib
import json
import time

import story_tracking


def classify(text):
    text = str(text or '').lower()
    if any(s in text for s in ('budget exhausted', 'model-spend budget', 'full model-spend budget')):
        return 'budget', 'change_budget'
    if any(s in text for s in ('blocked: unsure', 'blocked: block', 'policy denial', 'unauthorized')):
        return 'policy', 'resolve_policy'
    if any(s in text for s in ('cannot parse', 'syntaxerror', 'unterminated', 'incomplete', 'format (exit')):
        return 'syntax', 'repair_syntax'
    if any(s in text for s in ('docker', 'timed out', 'connection', 'service unavailable', 'request failed')):
        return 'infrastructure', 'restore_service'
    if any(s in text for s in ('missing input', 'missing data', 'credentials', 'not configured')):
        return 'missing_inputs', 'provide_inputs'
    return 'functional', 'repair_functionality'


def record(state, task, text, review_rejection=False):
    root = story_tracking.root(state, task) if state else task
    ledger = root.setdefault('recoveryLedger', {'attempts': 0, 'noProgress': 0, 'history': []})
    progress = hashlib.sha256(json.dumps([
        root.get('lastValidatedSourceDigest'),
        root.get('validatedArtifactSha256')], sort_keys=True).encode()).hexdigest()
    ledger['noProgress'] = ledger.get('noProgress', 0) + 1 if ledger.get('progress') == progress else 1
    ledger['progress'] = progress
    ledger['attempts'] += 1
    category, action = classify(text)
    if review_rejection and category == 'missing_inputs' and not any(
            word in str(text).lower() for word in ('credentials', 'not configured', 'missing human decision', 'missing input')):
        category, action = 'functional', 'repair_functionality'
    ledger['history'].append({'at': int(time.time()*1000), 'taskId': task.get('id'),
                             'category': category, 'action': action, 'failure': str(text or '')[:1000]})
    root['failureCategory'] = category
    root['recommendedRecoveryAction'] = action
    stopped = (ledger['noProgress'] >= 2 and not review_rejection) or ledger['attempts'] >= ledger.get('attemptLimit', 4) or category in ('policy', 'budget', 'infrastructure', 'missing_inputs')
    if stopped:
        root.update(manualAttention=True, status='blocked', blockedReason='story_recovery_limit',
                    failNote=str(text or '')[:4000],
                    nextAction='Resolve ' + category + ' before requesting another recovery attempt.')
        root.setdefault('_peerGate', {})['escalated'] = True
        if state:
            ids = {root.get('id'), task.get('id')}
            state['workQueue'] = [q for q in state.get('workQueue', []) if not any(q.get(k) in ids for k in story_tracking.LINKS)]
    return stopped


def authorize(root, guidance):
    """A real player decision grants one additional attempt, without erasing history."""
    ledger = root.get('recoveryLedger')
    if ledger and guidance.strip():
        ledger.setdefault('authorizations', []).append({'at': int(time.time()*1000), 'guidance': guidance})
        ledger['noProgress'] = 0
        ledger['attemptLimit'] = ledger['attempts'] + 1
