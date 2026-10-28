
_DEFAULT_PROFILE_BASE = {
    'mission': 'Support the think tank in this role.',
    'instructions': ["Check in with a chief of staff if you're unsure what to prioritize."],
    'notes': [],
}

_SEED_PROFILES = {}
_DEFAULT_PROFILE = {}


def _merge_profile(base, override):
    """Launch profile with an optional seed_profiles.json override merged in.
    An override replaces the mission and, when supplied, the instruction list;
    notes are always the override's (empty by default)."""
    out = {'mission': base['mission'],
           'instructions': list(base.get('instructions') or []),
           'notes': list(base.get('notes') or [])}
    if override:
        out['mission'] = override['mission']
        if 'instructions' in override:
            out['instructions'] = list(override['instructions'])
        out['notes'] = list(override.get('notes') or [])
    return out


def _rebuild_seed_profiles():
    """Rebuild the live profiles from the kernel defaults + JSON overrides.
    Mutates in place so `from serve import _SEED_PROFILES` consumers stay live."""
    _SEED_PROFILES.clear()
    for role, base in _SEED_PROFILE_DEFAULTS.items():
        _SEED_PROFILES[role] = _merge_profile(base, SEED_PROFILE_OVERRIDES.get(role))
    _DEFAULT_PROFILE.clear()
    _DEFAULT_PROFILE.update(
        _merge_profile(_DEFAULT_PROFILE_BASE, SEED_DEFAULT_PROFILE_OVERRIDE or None))


_rebuild_seed_profiles()


# ---------------------------------------------------------------------------
# Director-owned role templates. _SEED_PROFILES above is the *launch* content
# only: on first seed it is copied into DB state (kv_state.templates), and from
# then on the DB is authoritative so directors can create/edit/delete templates
# without a server restart. Seeding and hiring both resolve through the same
# DB-first helper below, so a director-authored template becomes a real role a
# future hire can be given. Any agent can READ the library; only directors and
# the chief of staff can write it (see _is_director / _is_director_or_admin).
# ---------------------------------------------------------------------------
def _templates_from_db(state):
    # Returns the live template dict {role: profile}, or {} when the DB has
    # none yet (pre-seed). Never falls back to constants here -- the caller
    # decides the fallback chain so intent is explicit.
    return (state or {}).get('templates') or {}


def _init_templates_in_db(state):
    # One-time backfill of the template library from the seed constants, so a
    # brand-new DB starts with every founder role defined and directors can then
    # amend/create from that base. Only fills roles that are missing (a director
    # may already have deleted or added entries); does not overwrite anything
    # a director has since changed.
    templates = (state or {}).get('templates')
    if templates is None:
        templates = {}
        for role, profile in _SEED_PROFILES.items():
            templates[role] = dict(profile)
        state['templates'] = templates
        return True
    return False


def _profile_for_role(state, role):
    # The resolution chain every profile assignment uses (seeding, hiring):
    # a director-authored DB template wins, then the seed constant, then the
    # generic fallback. Returns a fresh dict so callers never hand out the
    # shared constant by reference.
    templates = _templates_from_db(state)
    template = templates.get(role)
    if template is not None:
        return {
            'mission': template.get('mission', ''),
            'instructions': list(template.get('instructions', [])),
            'notes': list(template.get('notes', [])),
        }
    seed = _SEED_PROFILES.get(role)
    if seed is not None:
        return {'mission': seed['mission'], 'instructions': list(seed['instructions']), 'notes': list(seed.get('notes', []))}
    return {'mission': _DEFAULT_PROFILE['mission'], 'instructions': list(_DEFAULT_PROFILE['instructions']), 'notes': []}


def _is_director(state, agent_id):
    # Walk-the-chain definition, matching _director_chain's philosophy: any
    # agent who has one or more direct reports is a director, at any depth.
    # We cannot trust the isDirector boolean alone -- the backfill only stamps
    # admin/senior-roster with it, not every mid-level lead who earns the role
    # structurally by gaining a report. So director-ness is DERIVED here.
    if _is_admin(state, agent_id):
        return True
    if _direct_reports(state, agent_id):
        return True
    return False


def _is_director_or_admin(state, agent_id):
    # The gate for writing the template library: only directors (including the
    # admin, who is herself a director here by definition) may author templates.
    return _is_director(state, agent_id)


def _apply_template_to_role(state, role, profile, author_id):
    # Retroactive re-stamp: push an (edited or newly created) template to every
    # live agent currently holding that role, regenerating their AGENTS.md.
    # A director amending a role's obligations changes how current holders
    # operate, not just future hires. No-op when the role has no live holders.
    agents = state.get('agents', {})
    applied = []
    for aid, agent in list(agents.items()):
        if agent.get('role') != role:
            continue
        agent['profile'] = {
            'mission': profile.get('mission', ''),
            'instructions': list(profile.get('instructions', [])),
            'notes': list(profile.get('notes', [])),
        }
        applied.append(aid)
    if not applied:
        return applied
    save_state_to_db(state)  # materializes every updated AGENTS.md + agent.json
    for aid in applied:
        log_action(author_id, 'template_applied', {'role': role, 'agent': aid}, authorized=True)
    return applied


def _seed_default_roster():
    """Materialize the default roster into an empty database. Returns True if
    it seeded, False if the DB already had state (or seeding failed)."""
    if get_state_from_db() is not None:
        return False
    roster = _default_roster_definitions()
    agents = {}
    now_ms = int(time.time() * 1000)
    # Stand up the director-owned template library from the seed constants on a
    # cold start, so seeding and the /api/templates* endpoints agree from birth.
    state_for_templates = {'templates': None}
    _init_templates_in_db(state_for_templates)
    for d in roster:
        aid = d['id']
        profile = _profile_for_role(state_for_templates, d['role'])
        agents[aid] = {
            'id': aid, 'name': d['name'], 'color': d['color'], 'role': d['role'],
            'villageId': 'main',
            'profile': {'mission': profile['mission'], 'instructions': list(profile['instructions']), 'notes': list(profile['notes'])}, 'model': d['model'],
            'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0, 'mailbox': [], 'conversationLog': [],
            'lastContactedAt': None, 'hiredAt': now_ms,
            'elevatedAccess': False, 'accessGrant': None,
            # Placeholder world position; the client re-places agents into
            # walkable spots on first render (it always does this on boot).
            'x': 0, 'y': 0, 'dir': 'south', 'visible': True, 'busy': False, 'meetingId': None,
        }
    seed_room_defs = {r: {'label': d['label'], 'purpose': d['purpose']}
                      for r, d in _DEFAULT_ROOM_DEFINITIONS.items()}
    save_state_to_db({'agentRoster': roster, 'agents': agents, 'reports': [], 'nextReportId': 1, 'workQueue': [], 'researchTopics': [], 'templates': state_for_templates.get('templates') or {}, 'roomDefinitions': seed_room_defs, 'villages': [{'id': 'main', 'name': 'Main Village'}]})
    # Stamp admin/director tier (idempotent) so the DB owns the authority
    # graph from the very first read.
    _backfill_directors_in_db()
    _backfill_teams_in_db()
    return True


# Real per-agent directories on disk (the standard agent-folder convention):
# agent.json, AGENTS.md, conversations/, MEMORY.md, prototypes/, reports/,
# state.json -- one for every agent, materialized from the SAME state this
# whole think tank already treats as authoritative (think_tank.db), not a
# second, independently-mutated copy of it. Regenerated on every save
# (same cadence as the DB itself) rather than incrementally patched at
# every mutation site -- simpler, and it can never drift out of sync with
# what the game actually thinks is true. Deliberately outside world/,
# same reasoning as everything else that shouldn't be publicly fetchable.
AGENTS_DIR = os.path.join(THINK_TANK_DIR, 'agents')


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)


def _render_agents_md(name, role, profile):
    lines = [f'# {name} -- {role}', '', '## Mission', '', profile.get('mission', ''), '', '## Instructions', '']
    for instr in profile.get('instructions', []):
        lines.append(f'- {instr}')
    agreement = profile.get('workAgreement')
    if isinstance(agreement, dict) and agreement.get('text'):
        lines += ['', '## Work Agreement', '', agreement['text'], '',
                  '_Drafted by the agent, empowered by the admin._']
    lines += ['', '## Notes', '']
    for note in profile.get('notes', []):
        lines.append(f'- {note}')
    return '\n'.join(lines) + '\n'


# Solves the exact problem "which notes deserve to survive that cap" --
# researched against a memory_trust.py decay formula rather than guessed at.
# Fully portable to plain SQLite: no embeddings, no vector search, just
# arithmetic over what action_log already has. Records past
# MEMORY_MAX_AGE_DAYS are hard-excluded regardless of score, matching
# a hard age cutoff; everything else is ranked by a blend of
# recency and how consequential the action actually was, not recency
# alone -- so an old firing review can outrank a recent routine browse.
MEMORY_MAX_AGE_DAYS = 90
MEMORY_DECAY_FACTOR = 0.2
MEMORY_KEEP_COUNT = 20
# Not every logged action matters equally -- a joint firing review or a
# report filed against someone is a real decision worth remembering
# longer than a routine sandbox command or page fetch.
MEMORY_ACTION_BOOST = {
    'firing_review': 0.3, 'report_filed': 0.25, 'hire': 0.2, 'big_task_delegated': 0.2,
    'handoff': 0.1, 'task_completed': 0.05, 'task_assigned': 0.0,
    'execute': -0.05, 'browse': -0.05, 'chat': -0.1, 'decide': -0.15,
}


def _render_memory_md(agent_id):
    # A running history, distinct from AGENTS.md's identity/instructions --
    # this is what's actually HAPPENED to or around this agent, drawn
    # straight from the same action_log everything else logs into, not a
    # separately maintained diary that could drift from the real record.
    now = time.time()
    cutoff = now - MEMORY_MAX_AGE_DAYS * 86400
    with _db() as conn:
        rows = conn.execute(
            'SELECT action, details, ts FROM action_log WHERE agent_id = ? AND ts >= ? ORDER BY ts DESC',
            (agent_id, cutoff),
        ).fetchall()

    scored = []
    for action, details, ts in rows:
        age_days = (now - ts) / 86400
        decay_penalty = min(1.0, MEMORY_DECAY_FACTOR * (age_days / MEMORY_MAX_AGE_DAYS))
        score = 1.0 - decay_penalty + MEMORY_ACTION_BOOST.get(action, 0.0)
        scored.append((score, action, details, ts))
    scored.sort(key=lambda r: r[0], reverse=True)
    kept = scored[:MEMORY_KEEP_COUNT]
    kept.sort(key=lambda r: r[3], reverse=True)  # display newest-first once the keep-set is chosen

    lines = ['# Memory', '', f'Most consequential and recent activity involving this agent (kept {len(kept)} of {len(rows)} entries from the last {MEMORY_MAX_AGE_DAYS} days).', '']
    for _score, action, details, ts in kept:
        when = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f'- **{when}** -- {action}' + (f': `{details}`' if details else ''))
    return '\n'.join(lines) + '\n'


def _render_conversation_md(conversation_log):
    lines = ['# Conversation with the player', '']
    for entry in (conversation_log or []):
        who = 'You' if entry.get('fromId') == 'player' else 'Agent'
        lines.append(f"**{who}:** {entry.get('text', '')}")
        lines.append('')
    return '\n'.join(lines) + '\n'


def _render_report_md(report):
    ts = report.get('ts')
    when = datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S') if ts else 'unknown'
    return '\n'.join([
        f"# {report['id']}", '',
        f"Filed by: {report.get('fromId')}", f"Filed: {when}", '',
        '## Quoted evidence', '', f"> {report.get('quote', '')}", '',
        '## Note', '', report.get('note', ''),
    ]) + '\n'


# Throttle for the agent-directory filesystem mirror (see sync_agent_directories).
# The mirror is expensive (action_log scans + many file writes) and does not
# need per-tick freshness; save_state_to_db syncs it at most this often.
_AGENT_DIR_SYNC_ENV_S = str(_load_env().get('AGENT_DIR_SYNC_INTERVAL_S', '') or '')
AGENT_DIR_SYNC_INTERVAL_S = float(_AGENT_DIR_SYNC_ENV_S or 15)
_LAST_AGENT_DIR_SYNC_AT = 0.0
_LAST_AGENT_DIR_FINGERPRINT = None


def _agent_dir_fingerprint(state):
    # Cheap structural inputs of the filesystem mirror: who exists and which
    # reports exist. A change here (hire/fire, a newly filed report) forces an
    # immediate sync even inside the throttle window -- only the churny
    # memory/state files are allowed to lag by the interval.
    report_ids = tuple(sorted(r.get('id') for r in (state.get('reports') or [])
                              if isinstance(r, dict)))
    agent_ids = tuple(sorted((state.get('agents') or {}).keys()))
    return (agent_ids, report_ids)


def sync_agent_directories(state, throttle=False):
    # The per-agent filesystem mirror (agent.json / AGENTS.md / MEMORY.md /
    # conversation / state.json / reports) is expensive -- _render_memory_md
    # alone scans the action_log per agent -- and it was running on EVERY
    # save (every sim tick + every state write). Nothing it writes needs
    # sub-15s freshness, so save_state_to_db calls it throttled; a structural
    # change (roster or reports) still syncs immediately. Direct callers (and
    # tests) keep the unthrottled behavior.
    if throttle:
        global _LAST_AGENT_DIR_SYNC_AT, _LAST_AGENT_DIR_FINGERPRINT
        fp = _agent_dir_fingerprint(state)
        now = time.time()
        if fp == _LAST_AGENT_DIR_FINGERPRINT and (now - _LAST_AGENT_DIR_SYNC_AT) < AGENT_DIR_SYNC_INTERVAL_S:
            return
        _LAST_AGENT_DIR_SYNC_AT = now
        _LAST_AGENT_DIR_FINGERPRINT = fp
    roster = {d['id']: d for d in state.get('agentRoster', [])}
    live = state.get('agents', {})
    reports = state.get('reports', [])

    # Team-shared space: one directory per team, materialized (never clobbered)
    # on every save so a rename/repoint just re-labels, and the dir the agents
    # actually write to stays put. Members write via _can_write_team.
    for t in state.get('teams', []):
        shared = _team_shared_dir(t.get('id'))
        os.makedirs(os.path.join(shared, 'docs'), exist_ok=True)
        os.makedirs(os.path.join(shared, 'working'), exist_ok=True)
        os.makedirs(os.path.join(shared, 'products'), exist_ok=True)

    for agent_id, live_agent in live.items():
        base = os.path.join(AGENTS_DIR, agent_id)
        os.makedirs(os.path.join(base, 'conversations'), exist_ok=True)
        os.makedirs(os.path.join(base, 'prototypes'), exist_ok=True)
        os.makedirs(os.path.join(base, 'reports'), exist_ok=True)

        roster_def = roster.get(agent_id, {})
        identity = {
            'id': agent_id,
            'name': live_agent.get('name'),
            'color': live_agent.get('color'),
            'role': live_agent.get('role'),
            'model': live_agent.get('model'),
            'isAdmin': roster_def.get('isAdmin', False),
            'elevatedAccess': live_agent.get('elevatedAccess', False),
            'accessGrant': live_agent.get('accessGrant'),
        }
        _write_file(os.path.join(base, 'agent.json'), json.dumps(identity, indent=2))

        profile = live_agent.get('profile') or {}
        _write_file(os.path.join(base, 'AGENTS.md'), _render_agents_md(live_agent.get('name', agent_id), live_agent.get('role', ''), profile))
        _write_file(os.path.join(base, 'MEMORY.md'), _render_memory_md(agent_id))
        _write_file(os.path.join(base, 'conversations', 'player.md'), _render_conversation_md(live_agent.get('conversationLog')))

        # Everything else already has its own file above -- state.json is
        # just the remaining live runtime fields (position, busy, task,
        # counts), not a duplicate of the profile or conversation log.
        live_state = {k: v for k, v in live_agent.items() if k not in ('profile', 'conversationLog', 'mailbox')}
        _write_file(os.path.join(base, 'state.json'), json.dumps(live_state, indent=2))

        for report in reports:
            if report.get('aboutId') == agent_id:
                _write_file(os.path.join(base, 'reports', f"{report['id']}.md"), _render_report_md(report))


def sync_prototypes(agent_id, sandbox_dir):
    # A live mirror of whatever the shared Work Room sandbox currently
    # holds, not a growing pile of timestamped snapshots -- prototypes/ is
    # "what this agent is currently working on," and MEMORY.md/reports/
    # already cover history. Only for a real agent identity, never
    # 'player' or an unattributed call.
    if agent_id in (None, 'unknown', 'player') or not os.path.isdir(sandbox_dir):
        return
    dest = os.path.join(AGENTS_DIR, agent_id, 'prototypes')
    try:
        if os.path.exists(dest):
            shutil.rmtree(dest)
        # ignore .browser-act (transient driver state: shots, profile
        # cookies) -- the prototypes mirror is "what the agent is working on,"
        # not leftover browser session material.
        shutil.copytree(sandbox_dir, dest, ignore=shutil.ignore_patterns('.browser-act'))
    except OSError as e:
        print(f'[agent-dirs] prototypes sync failed for {agent_id}: {e}')


_FAILURE_NOTICE_STATE = {}
_FAILURE_NOTICE_LOCK = threading.Lock()
_FAILURE_NOTICE_FIELDS = ('taskId', 'windowId', 'id', 'path', 'service', 'host',
                          'url', 'title', 'reason', 'error', 'note', 'decision',
                          'summary', 'cost', 'confidence')


def _failure_spec(action, details):
    """Return (label, severity) for an operational failure, or None.

    The action log is the one place every subsystem already writes its
    outcome. This maps the failure-shaped actions to a player-facing notice
    without notifying ordinary successful work. Normal review rejections stay
    low severity and are throttled; they are still included.
    """
    a = str(action or '').lower()
    d = details if isinstance(details, dict) else {}
    decision = str(d.get('decision') or '').lower()
    if a == 'api_key_auth_failure' or 'auth_failure' in a:
        return 'API key authentication failed', 'critical'
    if 'rollback_failed' in a or 'kernel_apply_failed' in a:
        return 'A change request could not roll back or apply safely', 'critical'
    if 'self_edit_review_unavailable' in a:
        return 'The self-edit reviewer was unavailable', 'high'
    if 'post_apply_tests_failed' in a or 'pre_apply_failed' in a:
        return 'A change request failed the repo test gate', 'high'
    if 'apply_failed' in a or 'validation' in a and ('failed' in a or 'refused' in a):
        return 'A change request failed to apply or validate', 'high'
    if 'content_run_abandoned' in a:
        return 'A content run was abandoned', 'high'
    if 'work_item_abandoned' in a:
        return 'A queued work item was abandoned', 'high'
    if 'dormancy_window_refused' in a:
        return 'A change request window was refused', 'high'
    if 'telegram_failed' in a or 'mail_failed' in a:
        return 'A notification delivery failed', 'medium'
    if a == 'quality_gate_reject' or a.endswith('_reject') or 'rejected' in a:
        return 'A review or gate rejected work', 'low'
    if a.endswith('_denied') or 'denied' in a:
        return 'A policy or permission check denied a request', 'medium'
    if a.endswith('_refused') or 'refused' in a:
        return 'A request was refused', 'medium'
    if a.endswith('_failed') or '_failure' in a or a.endswith('_failure'):
        return 'An operation failed', 'medium'
    if decision in ('blocked', 'denied', 'allowed_but_fetch_failed',
                    'allowed_but_failed', 'failed', 'refused', 'rejected'):
        return 'An operation failed or was blocked', 'medium'
    return None


def _failure_notice_key(action, details):
    d = details if isinstance(details, dict) else {}
    for field in ('taskId', 'windowId', 'id', 'path', 'service', 'host',
                  'url', 'title', 'agentId', 'agent'):
        value = d.get(field)
        if value:
            return f'{action}|{field}|{str(value)[:180]}'
    return f'{action}|global'


def _failure_notice_message(agent_id, action, details, label, severity, count=1):
    d = details if isinstance(details, dict) else {'detail': details}
    when = time.strftime('%Y-%m-%d %H:%M:%S %Z')
    bits = []
    for field in _FAILURE_NOTICE_FIELDS:
        value = d.get(field)
        if value in (None, '', [], {}):
            continue
        if field in ('reason', 'error', 'note', 'decision', 'summary'):
            bits.append(f'{field}: {str(value)[:500]}')
        else:
            bits.append(f'{field}: {str(value)[:180]}')
    detail = '\n'.join(bits) or 'No detail was recorded.'
    detail = _redact_secrets(detail)[:1800]
    subject = f'[AI Think Tank] Failure: {label}'
    body = (
        f'Agent: {agent_id or "system"}\n'
        f'When: {when}\n'
        f'Severity: {severity.upper()}\n'
        f'Action: {action}\n'
        f'What happened: {label}\n\n'
        f'{detail}\n'
    )
    if count > 1:
        body += (f'\nThis failure repeated {count} times since the previous '
                 'notice; identical failures are grouped for one cooldown '
                 'window.')
    return subject, body[:4096]


def _send_failure_notice(subject, body):
    try:
        send_player_telegram_sync(subject, body)
    except Exception:
        pass


def _maybe_notify_failure(agent_id, action, details):
    spec = _failure_spec(action, details)
    if spec is None:
        return
    label, severity = spec
    cooldown = {'critical': 300, 'high': 300, 'medium': 900, 'low': 3600}.get(
        severity, 900)
    now = time.time()
    key = _failure_notice_key(action, details)
    with _FAILURE_NOTICE_LOCK:
        state = _FAILURE_NOTICE_STATE.get(key)
        if state and now - state['last'] < cooldown:
            state['suppressed'] = state.get('suppressed', 0) + 1
            return
        count = (state.get('suppressed', 0) + 1) if state else 1
        _FAILURE_NOTICE_STATE[key] = {'last': now, 'suppressed': 0}
    subject, body = _failure_notice_message(
        agent_id, action, details, label, severity, count)
    threading.Thread(target=_send_failure_notice,
                     args=(subject, body), daemon=True).start()


def log_action(agent_id, action, details=None, authorized=None, trace_id=None):
    # The single, comprehensive activity log that records all
    # actions, not just the ones a specific feature happened to write to
    # its own file. `authorized` is None for actions that have no
    # per-agent-key concept at all (state saves, the collision/door
    # editors); True/False once agent keys are actually checked (see
    # verify_agent_key() below).
    with _db() as conn:
        conn.execute(
            'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) VALUES (?, ?, ?, ?, ?, ?)',
            (agent_id, action, json.dumps(details) if details is not None else None, authorized, time.time(), trace_id),
        )
    try:
        _action_log_parity_maybe()
    except Exception:
        pass
    try:
        _maybe_notify_failure(agent_id, action, details)
    except Exception:
        # A notification can never break the action log itself.
        pass


# Heuristic: map each decision prompt's instruction text to a coarse kind label.
# Deliberately substring-based and tolerant -- the tape is observability, not a
# schema; an unrecognized prompt degrades to 'other' rather than erroring.
_DECISION_KIND_HINTS = (
    ('peer report', 'peer_report'),
    ('deserves a formal', 'peer_report'),
    ('worker to report', 'peer_report'),
    ('rate', 'grade'),
    ('0-10', 'grade'),
    ('escalation', 'escalation'),
    ('approve', 'escalation'),
    ('deny', 'escalation'),
    ('dismiss', 'personnel'),
    ('fire', 'personnel'),
    ('hire', 'personnel'),
    ('reject', 'groom'),
    ('accept', 'groom'),
    ('groom', 'groom'),
    ('runbook', 'runbook'),
    ('broke', 'runbook'),
    ('fixed', 'runbook'),
    ('story', 'triage'),
    ('spike', 'triage'),
)


def _decision_kind(instructions):
    text = (instructions or '').lower()
    for needle, kind in _DECISION_KIND_HINTS:
        if needle in text:
            return kind
    return 'other'


def _tape_row_hash(prev_hash, ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok, trace_id):
    # HMAC-SHA256 over prev_hash + the canonical row, keyed by the server's
    # boundary secret (the same trust root the external-data markers use). A
    # row edit, a reorder, or a truncation inside the chain changes this.
    blob = json.dumps([ts, kind, model, prompt, criteria, choice, confidence,
                       cost, raw, ok, trace_id], sort_keys=True, default=str)
    return hmac.new(_BOUNDARY_SECRET.encode(),
                    f'{prev_hash or ""}|{blob}'.encode('utf-8', errors='replace'),
                    hashlib.sha256).hexdigest()


def _backfill_decision_tape_hashes(conn):
    """Give legacy decision_tape rows their chain hashes, in id order. Only
    NULL-hash rows are touched -- a row that already carries a hash is trusted
    as-is (re-signing it would destroy its tamper evidence) and simply becomes
    the next row's anchor. Returns how many rows were hashed."""
    rows = conn.execute(
        'SELECT id, hash, ts, kind, model, prompt, criteria, choice, confidence, '
        'cost, raw, ok, trace_id FROM decision_tape ORDER BY id ASC').fetchall()
    prev_hash = ''
    changed = 0
    for r in rows:
        if r[1]:
            prev_hash = r[1]
            continue
        row_hash = _tape_row_hash(prev_hash, r[2], r[3], r[4], r[5], r[6], r[7],
                                  r[8], r[9], r[10], r[11], r[12])
        conn.execute('UPDATE decision_tape SET prev_hash = ?, hash = ? WHERE id = ?',
                     (prev_hash, row_hash, r[0]))
        prev_hash = row_hash
        changed += 1
    return changed


def _verify_decision_tape(limit=100000):
    """Walk the tape's hash chain and report any row that does not verify:
    a content edit (hash mismatch), a chain break (prev_hash doesn't match the
    previous row's hash), or a row with no hash at all. The FIRST row's
    prev_hash is an anchor to the (possibly pruned) past and is not itself
    checkable -- retention pruning of old head rows is expected. Returns
    (ok, problems) with problems a list of {id, issue} dicts."""
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT id, prev_hash, hash, ts, kind, model, prompt, criteria, '
                'choice, confidence, cost, raw, ok, trace_id FROM decision_tape '
                'ORDER BY id ASC LIMIT ?', (limit,)).fetchall()
    except Exception as e:
        return False, [{'id': None, 'issue': f'verify unavailable: {e}'}]
    problems = []
    prev_hash = ''
    for idx, r in enumerate(rows):
        rid, row_prev, row_hash = r[0], r[1], r[2]
        if not row_hash:
            problems.append({'id': rid, 'issue': 'unhashed row (legacy or tampered)'})
            continue
        if idx > 0 and row_prev != prev_hash:
            problems.append({'id': rid, 'issue': 'chain break (prev_hash mismatch)'})
        expected = _tape_row_hash(row_prev, r[3], r[4], r[5], r[6], r[7], r[8],
                                  r[9], r[10], r[11], r[12], r[13])
        if expected != row_hash:
            problems.append({'id': rid, 'issue': 'hash mismatch (content tampered)'})
        prev_hash = row_hash
    return (not problems, problems)


def _append_decision_tape(kind, model, prompt, criteria, choice, confidence, cost, raw, ok, trace_id=None):
    # Best-effort, bounded record of a Jev decision call. A tape-write failure
    # must never fail the caller (the decision already happened), so swallow it.
    # `trace_id`: correlates this decision with the resulting
    # action_log entry(s) for the same execution chain. Auto-generated here if
    # the caller didn't supply one; the caller should pass it through to
    # log_action so actions are traceable back to this decision.
    # Every row is chained to the previous row's hash (HMAC-SHA256), so the
    # tape is tamper-evident end to end.
    try:
        with _db() as conn:
            if trace_id is None:
                trace_id = secrets.token_hex(8)
            prev = conn.execute(
                'SELECT hash FROM decision_tape ORDER BY id DESC LIMIT 1').fetchone()
            prev_hash = prev[0] if prev else ''
            values = (
                time.time(),
                kind,
                model,
                prompt,
                json.dumps(criteria) if criteria is not None else None,
                choice,
                confidence,
                cost,
                raw if isinstance(raw, str) else json.dumps(raw),
                int(bool(ok)),
                trace_id,
            )
            row_hash = _tape_row_hash(prev_hash, *values)
            conn.execute(
                'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok, trace_id, prev_hash, hash) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (*values, prev_hash, row_hash),
            )
    except Exception:
        pass


def log_social_digest(state, people, decisions=None):
    """Write the weekly cross-team Knowledge Social digest to the shared library.
    It's the concrete, inspectable evidence that the conversation fanned real
    completed work across teams: who attended (they'd done week work), what each
    of them most recently delivered, and the per-attendee DECISION TAPE they left
    with (adopt/note/skip + confidence) -- the Jev-article lesson: a conversation
    that ends in logged typed decisions can steer downstream behavior, and gives
    the falsifiable before/after (did a team actually adopt something?).

    `people` is the {agent_id: snapshot} map the event snapshotted; `decisions`
    is the tape `_resolve_social` built (list of {agentId, choice, confidence}).
    Never load-bearing (a write failure must not block the event resolve)."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or []) if isinstance(d, dict)}
    ts = time.time()
    stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))
    ids = sorted(people.keys())
    lines = [f'# Knowledge Social -- {stamp}', '',
             f'Weekly cross-team conversation in the Hangout. {len(ids)} agent(s) attended'
             f' because they produced approved work this week.', '']
    decisions = {d.get('agentId'): d for d in (decisions or []) if isinstance(d, dict)}
    with _db() as conn:
        for aid in ids:
            name = roster.get(aid, {}).get('name') or (agents.get(aid) or {}).get('name') or aid
            tally = (agents.get(aid) or {}).get('weekApprovals') or 0
            # Most recent deliverable title from the durable log.
            row = conn.execute(
                'SELECT details, action FROM action_log WHERE agent_id = ? '
                'AND action IN (?) ORDER BY ts DESC LIMIT 1',
                (aid, 'task_completed')).fetchone()
            title = None
            if row and row[0]:
                try:
                    d = json.loads(row[0])
                    title = d.get('title') or (d.get('details') or {}).get('title')
                except Exception:
                    title = None
            line = f'- **{name}** (week work: {tally})'
            if title:
                line += f' -- recently delivered: "{title}"'
            dec = decisions.get(aid)
            if dec:
                conf = dec.get('confidence')
                conf_s = f' (conf {conf:.2f})' if isinstance(conf, (int, float)) else ''
                line += f' -- carry-away: **{dec.get("choice")}**{conf_s}'
            lines.append(line)
    try:
        os.makedirs(os.path.join(LIBRARY_DIR, 'social'), exist_ok=True)
        path = os.path.join(LIBRARY_DIR, 'social', f'social-{int(ts)}.md')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass


def log_refinement_digest(state, agents, groom):
    """Write the backlog-refinement ceremony digest to the shared library. It's
    the concrete, inspectable record that the scrum master turned agent-filed
    work-requests into REAL stories: who the scrum master was, what requests
    were groomed to accepted (became 'queued' stories) vs rejected, and each
    accepted card's room. `groom` is {'scrumMasterId': id, 'accepted': [req, ...]}.
    Never load-bearing (a write failure must not block the ceremony resolve)."""
    ts = time.time()
    stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))
    roster = {d.get('id'): d for d in (state.get('agentRoster') or []) if isinstance(d, dict)}
    sm_id = (groom or {}).get('scrumMasterId') or ''
    sm_name = roster.get(sm_id, {}).get('name') or (agents.get(sm_id) or {}).get('name') or sm_id
    accepted = (groom or {}).get('accepted') or []
    comprehensions = (groom or {}).get('comprehensions') or {}
    lines = [f'# Backlog Refinement -- {stamp}', '',
             f'Scrum master {sm_name} ({sm_id}) groomed {len(accepted)} agent-filed '
             f'work-request(s) into stories.', '']
    if accepted:
        lines.append(f'{len(accepted)} accepted (queued as stories):')
        for r in accepted:
            filer = roster.get(r.get('filedBy') or '', {}).get('name') or r.get('filedBy') or '?'
            lines.append(f'- [{r.get("room")}] "{r.get("title")}" -- filed by {filer}')
    else:
        lines.append('None accepted this ceremony -- every pending request was groomed out.')
    if comprehensions:
        lines.append('')
        lines.append('Read-backs (comprehension of each filed request):')
        for request_id in sorted(comprehensions):
            comp = comprehensions[request_id] or {}
            lines.append(f'- "{comp.get("title")}" ({comp.get("readiness")}): {comp.get("summary")}')
    try:
        os.makedirs(os.path.join(LIBRARY_DIR, 'refinement'), exist_ok=True)
        path = os.path.join(LIBRARY_DIR, 'refinement', f'refinement-{int(ts)}.md')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass


def log_escalation_digest(state, pending, outcome):
    """Write the on-call escalation digest to the shared library: the scrum
    master who groomed a failed restore, which product, why it escalated
    (assignment-abandoned vs unrestored), and whether they filed a STORY or a
    SPIKE back into the backlog. The inspectable record that an incident the
    on-call couldn't fix became real queue work instead of dying silently.
    Never load-bearing (a write failure must not block the ceremony resolve)."""
    ts = time.time()
    stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))
    pending = pending or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or []) if isinstance(d, dict)}
    sm_id = pending.get('scrumMasterId') or ''
    sm_name = roster.get(sm_id, {}).get('name') or sm_id
    product = pending.get('productId') or '?'
    source = pending.get('source') or 'unknown'
    title = pending.get('title') or 'broken product'
    lines = [f'# On-call Escalation -- {stamp}', '',
             f'Scrum master {sm_name} ({sm_id}) turned a failed restore on '
             f'{product} into a {outcome.upper()} in the backlog.', '',
             f'- Product: {product}', f'- Failed restore: "{title}"', f'- Why: {source}',
             f'- Filed as: {outcome}']
    try:
        os.makedirs(os.path.join(LIBRARY_DIR, 'escalations'), exist_ok=True)
        path = os.path.join(LIBRARY_DIR, 'escalations', f'escalation-{int(ts)}.md')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass


def _decide_allowed(agent_id, now=None):
    # Sliding-window gate for /api/decide. Returns (allowed, reason). Uses
    # time.monotonic so sleep/wall-clock changes don't distort the window.
    # Unknown/missing actors are coerced into a single "__system__" bucket:
    # this is the "give it a system story" fallback -- management-layer
    # calls (ambient task assignment, model-tier choice) that aren't made
    # "as" any one agent still work, but they share ONE throttle bucket,
    # so an unbounded unattributed flood (the ~48k-call leak this gated)
    # can't hide behind an infinite set of anonymous identities.
    now = time.monotonic() if now is None else now
    if agent_id in (None, '', 'unknown', 'player'):
        agent_id = '__system__'
    with _decide_lock:
        stamps = [t for t in _decide_stamps.get(agent_id, []) if now - t < DECIDE_BURST_WINDOW_S]
        if stamps and (now - stamps[-1]) < DECIDE_MIN_INTERVAL_S:
            _decide_stamps[agent_id] = stamps + [now]
            return False, 'decide rate limited (too fast)'
        if len(stamps) >= DECIDE_BURST_ALLOW:
            _decide_stamps[agent_id] = stamps + [now]
            return False, 'decide rate limited (burst exhausted)'
        stamps.append(now)
        _decide_stamps[agent_id] = stamps
        return True, None


def get_or_create_agent_key(agent_id):
    # A real per-agent secret (32 random bytes -- the same strength as an
    # AES-256 key, used the way authentication actually calls for: as a
    # bearer credential the agent presents, not something used to encrypt
    # traffic). Generated once, persisted, stable across restarts.
    with _db() as conn:
        row = conn.execute('SELECT secret_key FROM agent_keys WHERE agent_id = ?', (agent_id,)).fetchone()
        if row:
            return row[0]
        key = secrets.token_hex(32)
        conn.execute('INSERT INTO agent_keys (agent_id, secret_key, created_at) VALUES (?, ?, ?)', (agent_id, key, time.time()))
        return key


def verify_agent_key(agent_id, presented_key):
    # Attribution, not access control -- see the module-level notes on
    # SERVER_ACCESS_KEY vs. this. Returns None (not False) when there's no
    # agent identity to even check (e.g. 'player', or a Jev call with no
    # agentId at all), so the log can distinguish "not a real agent
    # action" from "claimed to be an agent but the key didn't match."
    if not agent_id or agent_id == 'player':
        return None
    if not presented_key:
        return False
    return secrets.compare_digest(presented_key, get_or_create_agent_key(agent_id))


def _actor_is_authorized(request, agent_id):
    """A player session may act as any agent; an agent key may act only as the
    agent it belongs to. Never trust a body agentId on its own."""
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is True:
        return True
    return _require_player_session(request)


# --------------------------------------------------------------------------
# Phase D: external-credential vault + capability handles (confused-deputy).
# The agent never holds an external service key -- only an opaque handle to a
# scoped grant. The server resolves the handle, enforces host/method/expiry,
# decrypts the real secret in-process, and injects it into the outbound
# request. The secret therefore never leaves this process and can't be
# exfiltrated by a persuaded agent.
# --------------------------------------------------------------------------
_FERNET_EDEK_DIR = os.path.join(THINK_TANK_DIR, '.secret_keys')
_FERNET_EDEK_PATH = os.path.join(_FERNET_EDEK_DIR, 'edek.key')
# The old location lived under ROOT (world/), which IS the static-served tree
# (app.mount('/', StaticFiles(directory=ROOT))) -- a plain GET served the
# master key unauthenticated. THINK_TANK_DIR (repo root) is NOT mounted, so
# the key is no longer reachable over HTTP.
_LEGACY_FERNET_EDEK_PATH = os.path.join(ROOT, '.secret_keys', 'edek.key')


def _fernet():
    # Lazy so serve.py still boots (and tests that don't hit keys still run)
    # if the `cryptography` package is absent. The key file is written on
    # first use (0600, gitignored), which keeps the master key out of .env
    # and off the served filesystem tree.
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    # Migrate a pre-existing key out of the (served) old location so any
    # vault entries already encrypted with it stay decryptable after the move.
    if not os.path.exists(_FERNET_EDEK_PATH) and os.path.exists(_LEGACY_FERNET_EDEK_PATH):
        try:
            os.makedirs(_FERNET_EDEK_DIR, exist_ok=True)
            shutil.copyfile(_LEGACY_FERNET_EDEK_PATH, _FERNET_EDEK_PATH)
            os.chmod(_FERNET_EDEK_PATH, 0o600)
        except OSError:
            pass
    if not os.path.exists(_FERNET_EDEK_PATH):
        os.makedirs(_FERNET_EDEK_DIR, exist_ok=True)
        with open(_FERNET_EDEK_PATH, 'w') as f:
            f.write(Fernet.generate_key().decode())
        os.chmod(_FERNET_EDEK_PATH, 0o600)
    with open(_FERNET_EDEK_PATH) as f:
        return Fernet(f.read().strip())


def _seal_secret(plaintext):
    f = _fernet()
    if f is None:
        raise RuntimeError('cryptography package not installed; cannot store external credentials')
    return f.encrypt(plaintext.encode()).decode()


def _open_secret(token):
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        return None
