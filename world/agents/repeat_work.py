"""Stop unchanged failures across fresh subtask IDs without losing evidence."""
import hashlib
import json
import os
import re

# Owner direction (2026-11-11, revised): the 12-hour minimum is for CHANGE
# REQUESTS only (selfedit windows). A repeat-failure freeze is urgent
# governance, not a scheduled change: the veto window is 2h (four VP digest
# cycles), after which the story processes as planned -- one clean retry,
# then ship-what-exists. See _operator_veto_pass in sim_parts/020.
_OPERATOR_VETO_WINDOW_MS = 2 * 3600 * 1000


def evidence_digest(state, parent):
    """Hash the evidence a retry would actually see.

    Library outputs and known coding files are included. A retry that changes
    none of them is the same attempt with the same evidence, regardless of its
    task id.
    """
    import serve
    h = hashlib.sha256()
    for field in ('libraryPath', 'validatedArtifactPath', 'artifactPath'):
        value = parent.get(field)
        path = serve._safe_library_path(value) if value else None
        if path and os.path.isfile(path):
            h.update(field.encode())
            with open(path, 'rb') as stream:
                h.update(stream.read(200000))
    sandbox_ids = [parent.get('sandboxId'), f"spike-{parent.get('id')}",
                   f"sb-{parent.get('id')}", f"story-{parent.get('id')}"]
    names = sorted(set(parent.get('writtenPy') or []))
    for sandbox_id in sandbox_ids:
        if not sandbox_id:
            continue
        workspace = os.path.join(serve.SANDBOXES_DIR, str(sandbox_id))
        if not os.path.isdir(workspace):
            continue
        for name in names:
            path = os.path.join(workspace, name)
            if os.path.isfile(path):
                h.update(name.encode())
                with open(path, 'rb') as stream:
                    h.update(stream.read(200000))
        break
    if h.digest() == hashlib.sha256(b'').digest():
        h.update(b'no-code')
    return h.hexdigest()


def record(state, task, result, now):
    import agents.story_tracking as story_tracking
    parent = story_tracking.root(state, task)
    if not parent.get('id') or parent.get('status') in ('done', 'cancelled', 'abandoned'):
        return False
    failure = str(result.get('peerSummary') or result.get('note') or 'Failed acceptance check')
    normalized = re.sub(r'\btask-\d+\b|\b\d{10,}\b|:\d+:\d+:', '<id>', failure.lower())
    normalized = ' '.join(normalized.split())[:2500]
    plan = (parent.get('interventionHistory') or [{}])[-1].get('guidance') or ''
    missing = re.findall(r'Cited artifact is missing from Library: ([A-Za-z0-9_./-]+)', failure)
    if missing:
        import serve
        proof = []
        for name in sorted(set(missing)):
            path = serve._safe_library_path(name)
            proof.append((name, os.path.isfile(path) if path else False))
        evidence = json.dumps(proof)
    else:
        evidence = evidence_digest(state, parent)
    signature = hashlib.sha256(json.dumps([normalized, evidence, plan]).encode()).hexdigest()
    records = parent.setdefault('repeatWorkFailures', [])
    attempt = str((parent.get('_peerGate') or {}).get('enteredMs') or task.get('id')) if task.get('taskType') in ('review', 'qa') else str(task.get('id'))
    if any(r['signature'] == signature and r['attempt'] == attempt for r in records):
        return False
    records.append({'signature': signature, 'attempt': attempt, 'at': now,
                    'taskId': task.get('id'), 'failure': failure[:2000]})
    parent['repeatWorkFailures'] = records[-30:]
    authorizations = (parent.get('recoveryLedger') or {}).get('authorizations') or []
    authorized_at = max((a.get('at') or 0 for a in authorizations), default=0)
    matches = sum(r['signature'] == signature and r.get('at', 0) >= authorized_at for r in records)
    if matches < 2:
        return False
    parent.update(status='blocked', manualAttention=True, blockedReason='unchanged_failure',
                  failNote='The same failure recurred with unchanged evidence and repair plan. '
                           'Investigate the cause, supply a new input, or change the repair plan. '
                           'Latest: ' + failure[:1800])
    parent['_peerGate'] = dict(parent.get('_peerGate') or {})
    parent['_peerGate']['escalated'] = True
    parent['repeatWorkStop'] = {'signature': signature, 'at': now, 'attempts': matches}
    # Operator veto window (owner direction 2026-11-11): the operator must
    # never be a hard blocker. The freeze stands for a bounded window during
    # which the operator may veto it; with no operator action the story
    # processes as planned -- one clean retry, then ship-what-exists (see
    # _operator_veto_pass). Previously this froze forever pending a manual
    # look, which is what wedged the dankornas X-post change request.
    parent['_operatorVetoBy'] = now + _OPERATOR_VETO_WINDOW_MS
    parent['_operatorVetoExpiries'] = 0
    try:
        _notify_operator_veto_window(state, parent, now)
    except Exception as e:
        print(f'[repeat-work] operator veto notify failed: {e}', flush=True)
    return True


def _notify_operator_veto_window(state, parent, now):
    """Tell the operator a repeat-failure freeze is standing and when it
    self-resolves. Player-inbox for the record + the email/telegram outbox so
    the option is actually visible; text states the default explicitly."""
    tid = parent.get('id')
    title = parent.get('title') or tid
    hours = _OPERATOR_VETO_WINDOW_MS / 3600000.0
    text = (f'"{title}" froze: the same failure recurred with unchanged '
            f'evidence (task {tid}). You may veto this freeze through the '
            f'operator channel; if you take no action within {hours:g}h the '
            'story processes as planned (one clean retry, then ship what '
            'exists). No operator decision is required.')
    messages = state.setdefault('playerInbox', [])
    messages.append({'id': f'ask-veto-{tid}-{int(now)}', 'taskId': tid,
                     'question': f'Veto the freeze on "{title}"?',
                     'context': text[:2000], 'status': 'awaiting_input',
                     'createdAt': int(now)})
    try:
        import sim
        sim._queue_player_email(state, 'operator_veto_window',
                                f'[AI Think Tank] Operator veto window: {title}',
                                text, channels=None)
    except Exception as e:
        print(f'[repeat-work] veto email queue failed: {e}', flush=True)
