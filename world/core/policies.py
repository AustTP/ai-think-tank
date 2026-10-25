"""Deterministic routing, assignment, and retry policies for the think tank."""

import collections
import os
import threading
import time

MODE_ENV = 'OPTIMIZATION_POLICY_MODE'
DEFAULT_MODE = 'shadow'
_MODES = ('shadow', 'apply')

_LOCK = threading.Lock()
_SHADOW = collections.deque(maxlen=200)
_COUNTS = collections.Counter()

DEFINITIONS = {
    'routing': 'Choose a model band and test-time sample count from task type, purpose, and budget posture.',
    'assignment': 'Rank eligible agents by room experience, availability, pairing state, and role fit.',
    'retry': 'Keep first failures with the author; reassign after repeated cycles when a free alternative exists.',
}


def policy_mode():
    mode = (os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    return mode if mode in _MODES else DEFAULT_MODE


def apply_enabled():
    return policy_mode() == 'apply'


def choose_route(purpose, task_type=None, allow_high=False, best_effort=False):
    text = (purpose or '').lower()
    ttype = (task_type or '').lower()
    if ttype in ('code', 'review', 'qa'):
        return {'band': 'coding', 'best_of': 1,
                'reason': 'code/review/qa lane', 'confidence': 'high'}
    if any(word in text for word in ('pulse', 'triage', 'latency', 'monitor', 'ping', 'status')):
        return {'band': 'low', 'best_of': 1,
                'reason': 'latency-sensitive call', 'confidence': 'high'}
    if best_effort:
        # A best-effort request is not a high-tier grant. Keep the extra
        # sample budget, but leave the band to the JEV tier gate.
        return {'band': 'mid', 'best_of': 3,
                'reason': 'best-effort request; JEV decides the band',
                'confidence': 'medium'}
    if allow_high and any(word in text for word in
                          ('plan', 'synthesize', 'investigat', 'analy', 'design',
                           'debug', 'reason', 'break down', 'root cause')):
        return {'band': 'mid', 'best_of': 2,
                'reason': 'consequential reasoning work', 'confidence': 'medium'}
    return {'band': 'low', 'best_of': 1,
            'reason': 'routine work stays on the cheap band', 'confidence': 'medium'}


def rank_assignees(state, pick, candidates):
    agents = state.get('agents') or {}
    room = pick.get('room')
    task_type = (pick.get('taskType') or '').lower()
    order = {aid: i for i, aid in enumerate(candidates)}
    ranked = []
    for aid in candidates:
        agent = agents.get(aid) or {}
        score = 0.0
        reasons = []
        if room and room in (agent.get('completedRooms') or []):
            score += 4.0
            reasons.append('room_experience')
        if not agent.get('task') and not agent.get('busy'):
            score += 2.0
            reasons.append('free')
        if agent.get('offDuty'):
            score -= 3.0
            reasons.append('off_duty')
        if agent.get('pairWith') or agent.get('handoff'):
            score -= 2.0
            reasons.append('paired')
        if task_type in ('code', 'bug', 'qa') and agent.get('isDirector'):
            score -= 2.0
            reasons.append('director')
        score -= order.get(aid, 0) * 0.001
        ranked.append({'agentId': aid, 'score': round(score, 4),
                       'reasons': reasons})
    ranked.sort(key=lambda row: (-row['score'], order.get(row['agentId'], 0)))
    return ranked


def decide_retry(task, gate, note, is_infra=False, alternatives=None):
    author = (task or {}).get('assignedTo')
    alternatives = list(alternatives or [])
    if is_infra:
        return {'action': 'queue_fix', 'target': author,
                'reason': 'infrastructure fault, same work retry',
                'confidence': 'high'}
    cycles = int((gate or {}).get('cycleCount') or 0)
    if cycles >= 2 and alternatives:
        return {'action': 'reassign', 'target': alternatives[0],
                'reason': f'{cycles} failed cycles; try a different author',
                'confidence': 'medium'}
    return {'action': 'queue_fix', 'target': author,
            'reason': 'first failures stay with the original author',
            'confidence': 'medium'}


def record_shadow(kind, decision, context=None):
    entry = {'at': time.time(), 'kind': kind, 'decision': decision,
             'context': context or {}}
    with _LOCK:
        _SHADOW.append(entry)
        _COUNTS[kind] += 1


def clear_shadow():
    with _LOCK:
        _SHADOW.clear()
        _COUNTS.clear()


def snapshot():
    with _LOCK:
        recent = list(_SHADOW)[-50:]
        counts = dict(_COUNTS)
    return {'mode': policy_mode(), 'definitions': dict(DEFINITIONS),
            'counts': counts, 'recent': recent}
