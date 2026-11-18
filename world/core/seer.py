"""Seer: failure investigations that read the tank's own data.

Sentry's Seer is an agent that sits NEXT TO the observability data and
root-causes failures with it: reads the traces, names the cause, files the
fix. The tank's equivalent edge is the moment work genuinely fails --
sim.record_agent_failure -- which today only bumps a fault score and
forgets why. The why is usually sitting in the action_log a few rows above
the failure: the browse that got rejected, the JEV budget denial, the
free-pool fallback that ate the last model call, the task that went stale
twice.

This module turns that tail into a structured investigation:

1. Classify the failure symptom from the failure kind + the action_log
   precursors (the last things that went wrong before the failure).
2. Record the finding as a QUESTION in the question registry ("why does
   <agent> keep <symptom>?") -- a question is the tank's durable form of
   "we don't know why yet", deduped by text so repeat failures converge
   on one investigation instead of spawning duplicates.
3. Anchor the open root cause as an epistemic unknown with a proposed
   validation, so fixing it is actionable work, not vibes.
4. When the same failure REPEATS past a threshold and the caller knows
   the owning team, file a remediation card (sim.file_issue) with
   acceptance criteria. One failure is noise; the card tax starts at the
   second occurrence.

Everything here is best-effort and read-mostly: the only writes are the
question, the epistemic records, and (past threshold) one card. The sim
hook wraps it in try/except -- an investigation failure must never turn
into a routing failure.
"""

import json
import time

_AUTO_FILE_THRESHOLD = 2
_TAIL_WINDOW_S = 6 * 3600
_TAIL_LIMIT = 120

_SYMPTOMS = {
    'infra': 'lose work to infrastructure (timeouts, transport, reclaim)',
    'refusal': 'fail on model or tool refusals instead of completing',
    'quality': 'complete work that fails review or verification',
}


def _tail(conn, agent_id, since_ts):
    rows = conn.execute(
        'SELECT ts, action, details FROM action_log '
        'WHERE agent_id = ? AND ts >= ? ORDER BY ts DESC LIMIT ?',
        (agent_id, since_ts, _TAIL_LIMIT)).fetchall()
    out = []
    for ts, action, details in rows:
        try:
            parsed = json.loads(details) if details else {}
        except Exception:
            parsed = {}
        out.append({'ts': ts, 'action': action, 'details': parsed})
    return out


def precursors(rows):
    """The notable things that happened to this agent shortly before the
    failure, newest first: denials, rejections, fallbacks, stale requeues.
    This is the part of Seer that makes it root-CAUSE work and not
    incident theater -- the evidence comes from the tank's own log."""
    out = []
    for row in rows:
        action = row['action'] or ''
        if ('rejected' in action or 'refused' in action
                or 'denied' in action or 'fallback' in action
                or 'stale' in action or 'unavailable' in action):
            d = row['details']
            summary = (d.get('reason') or d.get('from') or d.get('url')
                       or d.get('decision') or '') if isinstance(d, dict) \
                else str(d)[:120]
            out.append({'ts': row['ts'], 'action': action,
                        'summary': str(summary)[:160]})
        if len(out) >= 8:
            break
    return out


def classify(kind, pre):
    """Symptom label + a one-line hypothesis seeded by the failure kind
    and sharpened by the precursors (a budget-denial-heavy tail says
    budget; a browse-rejection tail says source discovery)."""
    symptom = _SYMPTOMS.get(kind or 'quality', _SYMPTOMS['quality'])
    joined = ' '.join(p['summary'].lower() for p in pre)
    if 'budget' in joined or 'cap' in joined:
        hypothesis = 'budget or spend-cap denials are blocking completion'
    elif 'not_discovered' in joined or 'browse' in joined:
        hypothesis = 'source discovery failures (rejected/undiscovered URLs) ' \
                     'are blocking completion'
    elif 'fallback' in joined or 'circuit' in joined:
        hypothesis = 'model-pool fallbacks/circuit breaks are degrading work'
    elif 'stale' in joined:
        hypothesis = 'work is going stale before completion'
    else:
        hypothesis = 'cause not yet localized -- needs the validation below'
    return symptom, hypothesis


def investigate_failure(agent_id, kind='quality', *, task_id=None,
                        failure_count=1, team_name=None, conn=None,
                        actor='seer'):
    """Run the four-step investigation for one real failure. `conn` is an
    action_log connection from the caller's seam (None opens the default
    DB best-effort). Returns a report dict; never raises to the caller."""
    try:
        return _investigate(agent_id, kind, task_id=task_id,
                            failure_count=failure_count,
                            team_name=team_name, conn=conn, actor=actor)
    except Exception as exc:  # noqa: BLE001 -- a seer failure must never
        return {'investigated': False, 'reason': 'seer_error',
                'error': str(exc)[:200]}


def _investigate(agent_id, kind, *, task_id, failure_count, team_name,
                 conn, actor):
    import core.questions as questions
    import core.epistemics as epistemics
    now = time.time()
    if conn is None:
        import serve as _serve
        with _serve._db() as conn:
            return _investigate(agent_id, kind, task_id=task_id,
                                failure_count=failure_count,
                                team_name=team_name, conn=conn, actor=actor)
    rows = _tail(conn, agent_id, now - _TAIL_WINDOW_S)
    pre = precursors(rows)
    symptom, hypothesis = classify(kind, pre)
    question_text = (f'Why does agent {agent_id} repeatedly {symptom}? '
                     f'Working hypothesis: {hypothesis}')
    q = questions.record_question(question_text, objective='failure_root_cause',
                                  people_who_may_know=[agent_id],
                                  actor=actor)
    finding = {'investigated': True, 'agentId': agent_id, 'kind': kind,
               'taskId': task_id, 'symptom': symptom,
               'hypothesis': hypothesis, 'question': q,
               'precursors': pre, 'failureCount': failure_count}
    anchor = epistemics.record_unknown(
        f'Whether "{hypothesis}" explains {agent_id} failures ({kind})',
        source='seer-investigation', owner=actor, actor=actor)
    if anchor.get('recorded'):
        epistemics.propose_validation(
            anchor['id'],
            f'Inspect {agent_id} action_log tail around the failure and '
            'confirm or refute the hypothesis with named rows',
            needed_evidence='specific action_log rows that show the cause',
            obtainment='seer sweep or operator review of the replay',
            estimated_cost_usd=0.0, worth_it=(failure_count >= 1),
            worth_it_note='root cause before a fix card is filed',
            actor=actor)
        finding['epistemicItemId'] = anchor['id']
    finding['remediationCard'] = None
    if (failure_count or 0) >= _AUTO_FILE_THRESHOLD and team_name:
        finding['remediationCard'] = _file_remediation(
            agent_id, kind, symptom, hypothesis, team_name, actor)
    return finding


def _file_remediation(agent_id, kind, symptom, hypothesis, team_name, actor):
    """File the fix card ONLY past the repeat threshold -- a single failure
    gets an investigation, not work-card spam in a 20k-card pipeline."""
    import sim as _sim
    import serve as _serve
    state = _serve.get_state_from_db() or {}
    team = next((t for t in (state.get('teams') or [])
                 if t.get('name') == team_name), None)
    if not team:
        return None
    issue = _sim.file_issue(
        state, team_id=team.get('id'), issue_type='bug',
        summary=f'Seer: fix recurring {kind} failures for {agent_id}',
        title=f'Seer: recurring {kind} failure ({agent_id})',
        feature=team_name,
        reporter_id=actor,
        description={
            'userStory': f'As the operator, I want {agent_id} to stop '
                         f'{symptom}, so that its cards stop failing review.',
            'acceptanceCriteria': [
                f'The root cause "{hypothesis}" is confirmed or refuted '
                'against the action_log',
                f'A repeat of the {kind} failure pattern for {agent_id} '
                'stops within the next batch of assigned work',
            ],
        })
    if issue:
        _serve.save_state_to_db(state)
        _serve.log_action(actor, 'seer_remediation_filed',
                          {'issueKey': issue.get('key'),
                           'agentId': agent_id, 'kind': kind},
                          authorized=True)
    return issue
