"""Issue engine: failure clusters -> root cause -> fix proposals.

LangSmith's Engine watches traces, clusters failures into issues, names
the root cause, recommends fixes, and builds evals so the failure stays
fixed. The tank's failure data is the failure-class action_log rows (the
same rows the failure-memory search reads). What it lacked was the
automatic cluster -> cause -> proposal step: the operator could SEARCH
failures, but nothing grouped "the same failure, twenty times" into one
investigation.

sweep() is that step, run on demand:

1. CLUSTER failure-class rows in the window by symptom signature
   (failure action + normalized reason keyword).
2. ROOT-CAUSE each cluster from the members' preceding notable actions
   (budget denials, browse rejections, model fallbacks -- whatever the
   failing agents kept hitting right before failing).
3. PROPOSE one fix per cluster past a size threshold: a spike card with
   acceptance criteria, deduped by cluster key, plus an epistemic
   validation proposal so "did the fix actually stop it" is checkable.

Read-mostly by design: with file_cards=False the sweep is a pure report
(the operator's dry run); with file_cards=True it files at most one card
per cluster per window.
"""

import json
import re
import time

# Same failure-class rows the failure-memory search reads (serve part 455);
# duplicated as a literal tuple so core stays seam-free of serve_parts.
FAILURE_ACTIONS = (
    'task_completion_refused', 'content_run_abandoned',
    'task_requeued_stale', 'card_pattern_suppressed',
    'repair_loop_stopped', 'infra_send_back', 'fix_send_back_deduped',
    'worker_stuck_help',
)

_MIN_CLUSTER = 2
_MAX_CLUSTERS = 5
_SWEEP_DEDUP_WINDOW_S = 12 * 3600

# Reason keyword -> symptom class. First match wins; order matters.
_SYMPTOM_RULES = (
    ('budget', 'budget-denial'),
    ('spend cap', 'budget-denial'),
    ('cap_exceeded', 'budget-denial'),
    ('not_discovered', 'source-discovery'),
    ('disallow', 'policy-block'),
    ('unauthorized', 'policy-block'),
    ('refus', 'model-refusal'),
    ('timeout', 'timeout'),
    ('stale', 'stale-work'),
    ('circuit', 'model-pool'),
    ('unavailable', 'model-pool'),
)


def _symptom(action, details):
    blob = json.dumps(details) if isinstance(details, dict) else str(details or '')
    low = blob.lower()
    for needle, name in _SYMPTOM_RULES:
        if needle in low:
            return name
    # fall back to the failure action itself, minus noise
    return re.sub(r'^(task_|card_|fix_|worker_)', '', action or 'failure')


def _load_rows(conn, since_ts):
    actions = ','.join("'%s'" % a for a in FAILURE_ACTIONS)
    rows = conn.execute(
        f'SELECT ts, agent_id, action, details FROM action_log '
        f'WHERE action IN ({actions}) AND ts >= ? '
        f'ORDER BY ts DESC LIMIT 2000', (since_ts,)).fetchall()
    out = []
    for ts, agent_id, action, details in rows:
        try:
            parsed = json.loads(details) if details else {}
        except Exception:
            parsed = {}
        out.append({'ts': ts, 'agent': agent_id, 'action': action,
                    'details': parsed, 'symptom': _symptom(action, parsed)})
    return out


def cluster(rows):
    """Group rows by symptom signature. Returns clusters sorted by size,
    each with members, distinct agents, and a sample detail."""
    groups = {}
    for row in rows:
        key = (row['symptom'], row['action'])
        groups.setdefault(key, []).append(row)
    out = []
    for (symptom, action), members in groups.items():
        agents = sorted({m['agent'] for m in members if m['agent']})
        out.append({'symptom': symptom, 'failureAction': action,
                    'size': len(members), 'agents': agents[:12],
                    'sample': members[0]['details'], 'members': members})
    out.sort(key=lambda c: -c['size'])
    return out


def root_cause(cluster_record, conn, now):
    """For one cluster: the notable (denial/rejection/fallback) actions its
    agents logged in the hour before their failures, most common first.
    Empty list means the failures carry their own cause (the sample detail
    IS the cause)."""
    agents = set(cluster_record['agents'])
    since = now - 3600
    rows = conn.execute(
        'SELECT ts, agent_id, action, details FROM action_log '
        'WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 2000',
        (since, now)).fetchall()
    counts = {}
    for ts, agent_id, action, details in rows:
        if agent_id not in agents:
            continue
        low = (action or '').lower()
        if not any(w in low for w in ('reject', 'refus', 'denied', 'fallback',
                                      'unavailable', 'blocked', 'stale')):
            continue
        try:
            parsed = json.loads(details) if details else {}
        except Exception:
            parsed = {}
        cause = (parsed.get('reason') if isinstance(parsed, dict) else None) \
            or action
        counts[cause] = counts.get(cause, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return [{'cause': str(c)[:160], 'count': n} for c, n in ranked]


def _already_filed(conn, key, now):
    since = now - _SWEEP_DEDUP_WINDOW_S
    like = f'%engine:{key}%'
    try:
        row = conn.execute(
            'SELECT 1 FROM action_log WHERE action = ? AND ts >= ? '
            'AND details LIKE ? LIMIT 1',
            ('issue_engine_fix_filed', since, like)).fetchone()
    except Exception:
        return False
    return row is not None


def _file_fix(cluster_record, causes, actor):
    """One spike card per cluster: acceptance criteria name the symptom,
    the suspected cause, and the check that closes it."""
    import sim as _sim
    import serve as _serve
    state = _serve.get_state_from_db() or {}
    team = next((t for t in (state.get('teams') or [])
                 if t.get('members')), None)
    if not team:
        return None
    symptom = cluster_record['symptom']
    cause_line = causes[0]['cause'] if causes else 'cause not localized'
    key = f'{symptom}:{cluster_record["failureAction"]}'
    issue = _sim.file_issue(
        state, team_id=team.get('id'), issue_type='spike',
        summary=f'Engine: investigate recurring {symptom} failures '
                f'({cluster_record["size"]}x, {len(cluster_record["agents"])} '
                'agents)',
        title=f'Engine: recurring {symptom} failures',
        feature=team.get('name') or team.get('id'),
        reporter_id=actor,
        description={
            'userStory': 'As the operator, I want the most common failure '
                         'cluster root-caused once, so that the same '
                         'failure stops being paid for card by card.',
            'acceptanceCriteria': [
                f'Top suspected cause is confirmed or refuted: {cause_line}',
                f'A fix or a documented refusal-to-fix exists for '
                f'{symptom} ({cluster_record["failureAction"]})',
                'A follow-up sweep of the same window shows the cluster '
                'size dropping',
            ],
        })
    if issue:
        _serve.save_state_to_db(state)
        _serve.log_action(actor, 'issue_engine_fix_filed',
                          {'issueKey': issue.get('key'), 'engine': key,
                           'size': cluster_record['size']},
                          authorized=True)
    return issue


def sweep(conn=None, *, window_s=86400, file_cards=False, actor='engine'):
    """Run the cluster -> root-cause -> proposal pass. Returns the report;
    never raises to the caller."""
    try:
        return _sweep(conn, window_s=window_s, file_cards=file_cards,
                      actor=actor)
    except Exception as exc:  # noqa: BLE001 -- sweep must not kill callers
        return {'swept': False, 'reason': 'engine_error', 'error': str(exc)[:200]}


def _sweep(conn, *, window_s, file_cards, actor):
    import core.epistemics as epistemics
    now = time.time()
    if conn is None:
        import serve as _serve
        with _serve._db() as conn:
            return _sweep(conn, window_s=window_s, file_cards=file_cards,
                          actor=actor)
    rows = _load_rows(conn, now - float(window_s or 86400))
    report = {'swept': True, 'windowS': int(window_s or 86400),
              'failureRows': len(rows), 'clusters': []}
    for c in cluster(rows)[:_MAX_CLUSTERS]:
        if c['size'] < _MIN_CLUSTER:
            continue
        key = f'{c["symptom"]}:{c["failureAction"]}'
        causes = root_cause(c, conn, now)
        entry = {'symptom': c['symptom'], 'failureAction': c['failureAction'],
                 'size': c['size'], 'agents': c['agents'],
                 'sample': c['sample'], 'causes': causes, 'fixCard': None}
        anchor = epistemics.record_unknown(
            f'Whether "{(causes[0]["cause"] if causes else "unlocalized")}" '
            f'explains the recurring {c["symptom"]} failures '
            f'({c["failureAction"]})',
            source='issue-engine-sweep', owner=actor, actor=actor)
        if anchor.get('recorded'):
            epistemics.propose_validation(
                anchor['id'],
                f'Re-run the failure sweep after the fix lands and confirm '
                f'the {c["symptom"]} cluster size drops',
                needed_evidence='a follow-up sweep report with a smaller '
                                'cluster',
                obtainment='schedule an engine sweep post-fix',
                estimated_cost_usd=0.0, worth_it=True,
                worth_it_note='closes the loop the eval way',
                actor=actor)
            entry['epistemicItemId'] = anchor['id']
        if file_cards and not _already_filed(conn, key, now):
            entry['fixCard'] = _file_fix(c, causes, actor)
        report['clusters'].append(entry)
    return report
