"""Agent-run replay (the Conversations/rrweb lesson, on the action log).

Sentry's Conversations view and LangSmith's trajectory view both sell the
same idea: an agent run is a TIMELINE you scrub, not a log you grep. The
tank already records every step -- browse decisions, JEV verdicts, tool
calls, evidence writes -- as action_log rows. What it lacked was the frame
model: rows grouped into typed, ordered frames with human summaries and a
run header, so an operator (or the agent itself) can replay what happened
without reading JSON.

This module is pure composition over action_log -- read-only, no state
mutation, no model calls. Frame kinds:

- browse      a page fetch attempt: URL, purpose, decision, final URL
- jev         a governance/budget verdict (denials carry the reason)
- tool        any other ask-loop tool call
- evidence    a claim or coverage write
- model       a model call, including free-pool fallbacks
- mail        mailbox traffic
- lifecycle   task/card state changes (assignment, reclaim, completion)
- other       everything else, summarized

Malformed detail rows still become frames (detail=None) -- a partial
replay beats none, the same rule the task replay endpoint follows.
"""

import json
import time

# action_log actions that carry PII or secret-shaped payloads get their
# string details length-capped harder; frames are display artifacts.
_MAX_DETAIL_CHARS = 400

_FRAME_RULES = (
    ('browse', 'browse'),
    ('jev_decision', 'jev'),
    ('task_completion_refused', 'lifecycle'),
    ('task_requeued_stale', 'lifecycle'),
    ('content_run_abandoned', 'lifecycle'),
    ('claim_recorded', 'evidence'),
    ('claim_observed', 'evidence'),
    ('epistemic_recorded', 'evidence'),
    ('epistemic_evidence', 'evidence'),
    ('free_pool_fallback', 'model'),
    ('mail_sent', 'mail'),
)


def _frame_kind(action):
    for prefix, kind in _FRAME_RULES:
        if action.startswith(prefix):
            return kind
    return 'other'


def _summarize(action, details):
    """One human line per frame, pulled from the common detail keys. The
    line is what you scan; the raw detail stays attached for the
    expandable view."""
    if not isinstance(details, dict):
        text = str(details or '')
        return text[:_MAX_DETAIL_CHARS]
    url = details.get('url') or details.get('finalUrl') or details.get('target')
    if url:
        purpose = (details.get('purpose') or details.get('decision')
                   or details.get('reason') or '')
        return f'{url}' + (f' -- {purpose}'[:160] if purpose else '')
    if 'from' in details and 'to' in details:
        return f"{details.get('from')} -> {details.get('to')} " \
               f"({details.get('reason') or 'fallback'})"
    for key in ('question', 'summary', 'title', 'reason', 'decision',
                'claimId', 'key'):
        if details.get(key):
            return f"{key}: {str(details[key])[:160]}"
    text = json.dumps(details)[:_MAX_DETAIL_CHARS]
    return text


def build_frames(rows):
    """Turn ordered (ts, agent_id, action, details) rows into typed replay
    frames. Malformed JSON becomes detail=None with the raw string kept --
    never dropped."""
    frames = []
    for ts, agent_id, action, details in rows:
        try:
            parsed = json.loads(details) if details else None
        except Exception:
            parsed = None
        frames.append({
            'ts': ts,
            'agent': agent_id,
            'kind': _frame_kind(action),
            'action': action,
            'summary': _summarize(action, parsed),
            'detail': parsed if isinstance(parsed, dict) else details,
        })
    return frames


def run_stats(frames):
    """The run header: span, frame counts by kind, and the error-ish
    frames an operator wants to jump to (denials, refusals, rejections)."""
    if not frames:
        return {'frames': 0, 'spanS': 0, 'byKind': {}, 'notable': []}
    start = min(f['ts'] for f in frames)
    end = max(f['ts'] for f in frames)
    by_kind = {}
    notable = []
    for f in frames:
        by_kind[f['kind']] = by_kind.get(f['kind'], 0) + 1
        action = f['action'] or ''
        if ('rejected' in action or 'refused' in action or 'denied' in action
                or (f['kind'] == 'browse' and isinstance(f['detail'], dict)
                    and 'reject' in str(f['detail'].get('decision', '')))):
            notable.append({'ts': f['ts'], 'action': action,
                            'summary': f['summary']})
    return {'frames': len(frames), 'spanS': round(end - start, 1),
            'startedTs': start, 'endedTs': end, 'byKind': by_kind,
            'notable': notable[:10]}


def replay(conn, *, agent_id=None, task_id=None, since_ts=None, limit=200):
    """Assemble a replay timeline from action_log for one agent, one task
    (substring match on details, the task-replay convention), or both.
    `conn` is a db connection from the caller's seam. Oldest first."""
    if not agent_id and not task_id:
        return {'error': 'agent_id or task_id is required'}
    since_ts = float(since_ts or (time.time() - 86400))
    limit = max(1, min(int(limit or 200), 1000))
    where = ['ts >= ?']
    params = [since_ts]
    if agent_id:
        where.append('agent_id = ?')
        params.append(agent_id)
    if task_id:
        where.append('details LIKE ?')
        params.append(f'%"{task_id}"%')
    rows = conn.execute(
        f"SELECT ts, agent_id, action, details FROM action_log "
        f"WHERE {' AND '.join(where)} ORDER BY ts ASC LIMIT ?",
        params + [limit]).fetchall()
    frames = build_frames(rows)
    out = {'frames': frames, 'stats': run_stats(frames),
           'truncated': len(rows) >= limit}
    out['subject'] = {'agentId': agent_id, 'taskId': task_id,
                      'sinceTs': since_ts}
    return out


def render_text(replay_result, max_frames=40):
    """The scrub-through text form: one line per frame. For ask-tool
    replies and terminal operators -- the API returns the structured
    frames for real UI rendering."""
    stats = replay_result.get('stats') or {}
    lines = [f"Replay {replay_result.get('subject')} -- "
             f"{stats.get('frames', 0)} frames over {stats.get('spanS', 0)}s "
             f"({', '.join(f'{k}:{v}' for k, v in (stats.get('byKind') or {}).items())})"]
    if stats.get('notable'):
        lines.append('Notable:')
        for n in stats['notable'][:5]:
            lines.append(f"  ! {n['action']}: {n['summary'][:140]}")
    for f in replay_result.get('frames', [])[:max_frames]:
        t = time.strftime('%H:%M:%S', time.localtime(f['ts']))
        lines.append(f"{t} [{f['kind']}] {f['agent']}: {f['summary'][:160]}")
    if replay_result.get('truncated'):
        lines.append('... truncated (raise the limit or narrow the window)')
    return '\n'.join(lines)
