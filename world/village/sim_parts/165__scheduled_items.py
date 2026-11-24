"""Scheduled items engine (2026-11-07): owner-role recurring cards.

Counterpart to serve_parts/425_scheduled_items.py, which stores
state['scheduledItems'] ({title, ownerRole, cadence, deliverable, ...}).
Each sim pass this engine checks every enabled item: when its cadence is
due, it resolves the owner role to live agent(s), asks serve to land any
bound digest (the regional VP's Telegram alert scan reads a daily digest
file, so the card needs only read_library_file), and mints ONE directed
card per owner through queue_work -- so dedup, refinement, the assignment
lane, and the completion gate all apply unchanged. While the previous
instance is still open, queue_work's twin check skips the mint; when it
closes, the next due pass mints the next instance.

Operator direction (2026-11-07): the seeded standing item is each
regional VP's DAILY Telegram alert scan -- read the last 24h of alerts,
triage which were real issues the tank should have handled itself, and
file one issue per genuine problem for the owning director.
"""
import time

SCHEDULED_CADENCE_MS = {
    '30min': 30 * 60 * 1000,
    'hourly': 3600 * 1000,
    'daily': 24 * 3600 * 1000,
    'weekly': 7 * 24 * 3600 * 1000,
    'monthly': 30 * 24 * 3600 * 1000,
    'quarterly': 91 * 24 * 3600 * 1000,
}

# Operator-directed seed: created once on the first pass that sees NO
# scheduledItems key at all (the key's ABSENCE is the first-boot marker).
# After that the operator's list is authoritative -- deleting the seed in
# the UI keeps it deleted, because a present (even empty) list never
# re-seeds.
_SCHEDULED_SEED_ITEM = {
    'id': 'sch-rvp-telegram-digest',
    'title': ('[AUDIT] Daily Telegram alert review: triage the last 24h of '
              'alerts and file real issues'),
    'ownerRole': 'regional-vp',
    'cadence': 'daily',
    'deliverable': 'report',
    'taskType': 'report',
    'instructions': (
        'Read the Telegram alert digest in the Library (the source file '
        'is named in your task instructions; one file per scan, scans '
        'accumulate), then triage every alert class: '
        'which were REAL issues the tank should have handled itself (recurring '
        'failure classes, alerts that fired for conditions already resolved, '
        'alerts that should have routed to a director instead of the operator)? '
        'The digest also carries a "Internal tank signals" section -- '
        'governance events (pattern suppressions, repeat-failure freezes, cap '
        'closures, walk-churn storms, hollow grades, failed content threads, '
        'sweeps that could not run) that never paged the operator. Triage '
        'those the same way: file ONE issue per genuine problem for the owning '
        'director, with the action name and task id in the issue. '
        'Do not re-litigate one-off failures the tank '
        'already recovered from, and do not re-litigate alert classes already '
        'covered by an OPEN issue from a previous scan.\n\n'
        'Scan frequency: you own this decision. When alert volume stays '
        'quiet for a day, slow the scan (hourly/daily) with an api_call '
        'PATCH /api/schedules/item/sch-rvp-telegram-digest using your '
        'agent key (directors are operator-gated); if the gate refuses, '
        'retry with a clearer purpose, else file the request as an issue '
        'for the chief of staff. During an integration program or an '
        'alert fire, keep the scan fast.'),
    'enabled': True,
    'lastRunAtMs': 0,
}

# Operator direction (2026-11-20): the RVP is the village's LangSmith -- an
# HOURLY proactive hunt, not just the daily alert triage. Constitution rule 9
# names the regional VP the owner of the village surface (GET /api/ops/failures
# for machinery/systemic failures). This seed makes that scan a standing item:
# every hour the RVP reads the failure surface and routes each actionable
# pattern to the OWNING DIRECTOR as directed work (constitution: an issue the
# RVP noticed and did not route is the RVP's miss). Digest findings and scan
# findings must not duplicate: the scan instructions say to skip anything an
# open issue already covers.
_SCHEDULED_VILLAGE_SCAN_ITEM = {
    'id': 'sch-rvp-village-scan',
    'title': ('[AUDIT] Hourly village scan: read the failure surface and '
              'route real problems to the owning director'),
    'ownerRole': 'regional-vp',
    'cadence': 'hourly',
    'deliverable': 'report',
    'taskType': 'report',
    'instructions': (
        'Proactive village scan (constitution rule 9): call '
        'api_call GET /api/ops/failures?hours=24 (your regional VP key is '
        'the scan authority) and read every section it returns: '
        'executeBlocks (blocked-command patterns), classifierUnavailable, '
        'failedActions, healthAlerts. Also GET /api/ops/cards?hours=24 '
        '(scan authority lets you read every span) for stalled, failed, '
        'refused and abandoned cards.\n\n'
        'For each GENUINE problem -- a recurring blocked-command pattern, a '
        'stall pattern, a machinery failure, a director whose span keeps '
        'failing the same way -- route it: file ONE issue per problem for '
        'the OWNING DIRECTOR (api_call POST /api/intent/issues, teamId '
        'matching the failing team) with the concrete evidence (action '
        'names, task ids, counts) and what you want done. Do NOT route '
        'card-level coaching (that is the director\'s own daily scan); '
        'route village-level patterns. Skip anything already covered by an '
        'open issue from a previous scan or by a live [PATTERN] card. If '
        'the surface is clean, finish the scan with a one-line clean '
        'report -- a clean hour is a real result, not a failure.'),
    'enabled': True,
    'lastRunAtMs': 0,
    # The instructions above direct api_call GETs (failures/cards) and a
    # POST (intent/issues). api_call is commit-tier (fail-closed per task),
    # so without this grant every scan burned 2 tool_policy_denied refusals
    # plus a MEDIUM telegram alert before finishing via library tools alone
    # (2026-11-24, task-35908..35912). The GETs are read-only; the POST is
    # the scan's designed routing authority.
    'approvedTools': ['api_call'],
}


def _resolve_scheduled_owners(state, item):
    """Resolve an item's ownerRole to live agent ids. Returns a list; an
    empty list means the role has no live holder right now (log it, skip
    the period rather than broadcasting to whoever happens to be idle)."""
    role = item.get('ownerRole')
    roster = [r for r in (state.get('agentRoster') or []) if isinstance(r, dict)]
    if role == 'chief-of-staff':
        return [r.get('id') for r in roster if r.get('isAdmin') and r.get('id')]
    if role == 'regional-vp':
        vid = item.get('villageId')
        if vid:
            chief = governance.chief_of_village(state, vid)
            return [chief] if chief else []
        return [r.get('id') for r in roster
                if r.get('villageChief') and r.get('id')]
    if role == 'senior-director':
        team_id = item.get('teamId')
        if team_id:
            team = next((t for t in (state.get('teams') or [])
                         if isinstance(t, dict) and t.get('id') == team_id), None)
            return [team.get('directorId')] if team and team.get('directorId') else []
        return [t.get('directorId') for t in (state.get('teams') or [])
                if isinstance(t, dict) and t.get('directorId')]
    if role == 'recursive-director':
        # The emergent middle-management layer: direct reports of team
        # directors who themselves lead people. Empty when no subdirector
        # layer exists yet -- a standing item waits rather than degrading.
        out = []
        for dir_id in governance.team_director_ids(state):
            out.extend(governance.subdirectors_of(state, dir_id))
        return out
    if role == 'worker':
        aid = item.get('agentId')
        if aid and governance._entry(state, aid):
            return [aid]
        return []
    return []


def _scheduled_items_pass(state, now_ms=None):
    """Mint due scheduled-item cards. Runs inside the sim task-cycle pass
    (one shared read-modify-write of state). Returns the number of cards
    minted."""
    now_ms = int(now_ms or time.time() * 1000)
    if 'scheduledItems' not in state:
        state['scheduledItems'] = [dict(_SCHEDULED_SEED_ITEM)]
    items = state.get('scheduledItems') or []
    if not isinstance(items, list):
        return 0
    # Seed the hourly village scan once (operator direction 2026-11-20). The
    # v1 digest seed only ever fires on a first boot where the key was absent,
    # so a running tank would never see new seeds -- ensure-by-id instead,
    # guarded by a marker so an operator who deletes the scan keeps it
    # deleted (a present marker never re-seeds).
    if not state.get('_villageScanSeeded'):
        if not any(isinstance(i, dict) and i.get('id') == _SCHEDULED_VILLAGE_SCAN_ITEM['id']
                   for i in items):
            items.append(dict(_SCHEDULED_VILLAGE_SCAN_ITEM))
        state['_villageScanSeeded'] = True
    minted = 0
    for item in items:
        if not isinstance(item, dict) or item.get('enabled') is False:
            continue
        # One-shot item (owner direction 2026-11-11): fires once at
        # notBeforeMs. ownerRole 'player' routes to the operator's inbox and
        # the email/telegram outbox instead of minting an agent card -- that
        # is what a "scheduled operator backlog item" is: the operator sees
        # it at the scheduled time, no agent work is implied.
        if item.get('cadence') == 'once':
            if now_ms < (item.get('notBeforeMs') or 0):
                continue
            if item.get('ownerRole') == 'player':
                messages = state.setdefault('playerInbox', [])
                messages.append({
                    'id': f"ask-{item.get('id')}", 'taskId': None,
                    'question': item.get('title') or 'Scheduled item',
                    'context': item.get('instructions') or '',
                    'status': 'awaiting_input',
                    'createdAt': now_ms})
                try:
                    _queue_player_email(
                        state, 'scheduled_operator_backlog',
                        f"[AI Think Tank] Scheduled: {item.get('title')}",
                        (item.get('instructions')
                         or item.get('title') or ''), channels=None)
                except Exception:
                    pass
                item['enabled'] = False
                item['lastRunAtMs'] = now_ms
                _log_governance(state, None, 'scheduled_item_fired',
                                {'id': item.get('id'), 'ownerRole': 'player',
                                 'cadence': 'once'})
            else:
                owners = _resolve_scheduled_owners(state, item)
                if not owners:
                    continue
                for aid in owners:
                    minted += queue_work(state, [{
                        'title': item.get('title') or 'Scheduled item',
                        'room': None,
                        'instructions': (item.get('instructions')
                                         or f"Scheduled work: {item.get('title')}"),
                        'taskType': item.get('taskType') or 'report',
                        'deliverable': item.get('deliverable') or 'report',
                        'assignedTo': aid, 'directedTo': True,
                        'priority': item.get('priority') or 'low',
                        'feature': f"scheduled:{item.get('id')}"}])
                item['enabled'] = False
                item['lastRunAtMs'] = now_ms
                _log_governance(state, None, 'scheduled_item_fired',
                                {'id': item.get('id'), 'owners': owners,
                                 'cadence': 'once'})
            continue
        cadence_ms = SCHEDULED_CADENCE_MS.get(item.get('cadence'))
        if not cadence_ms:
            continue
        last = item.get('lastRunAtMs') or 0
        if now_ms - last < cadence_ms:
            continue
        owners = _resolve_scheduled_owners(state, item)
        if not owners:
            # No live holder for the role: log once per period and hold the
            # period's lastRunAtMs stamp so a chief-less org does not spam
            # the log every 6s pass.
            item['lastRunAtMs'] = now_ms
            item['lastRunSkipped'] = 'no-live-owner'
            _log_governance(state, None, 'scheduled_item_skipped',
                            {'id': item.get('id'),
                             'reason': 'no live holder for ownerRole '
                                       + str(item.get('ownerRole'))})
            continue
        item['lastRunAtMs'] = now_ms
        item.pop('lastRunSkipped', None)
        digest_path = None
        if item.get('digest') == 'telegram' or item.get('id') == 'sch-rvp-telegram-digest':
            try:
                import serve
                # `last` is the PRE-period stamp (read before we stamped
                # lastRunAtMs above): the writer reads only alerts fired
                # since the previous scan, so passing the just-now stamp
                # would collapse the window to zero.
                digest_path = serve.write_telegram_alert_digest(
                    since_ts=last / 1000.0 or None)
            except Exception:
                digest_path = None
            if not digest_path:
                # Quiet window or a write failure: nothing to triage, so
                # no card. The period still counts as scanned because
                # lastRunAtMs is stamped above.
                item['lastRunSkipped'] = 'digest-quiet'
                _log_governance(state, None, 'scheduled_item_skipped',
                                {'id': item.get('id'),
                                 'reason': 'telegram digest window was '
                                           'quiet (no alerts, no internal '
                                           'signals, no integrity '
                                           'anomalies)'})
                continue
        instructions = item.get('instructions') or f"Pick whoever is best suited for: {item['title']}"
        if digest_path:
            instructions += (f"\n\nToday's digest file: library/{digest_path} "
                             '(read it first with read_library_file; note the '
                             'stored path drops the library/ prefix).')
        source_paths = [digest_path] if digest_path else None
        for aid in owners:
            minted += queue_work(state, [{
                'title': item['title'],
                'room': None,
                'instructions': instructions,
                'taskType': item.get('taskType') or 'report',
                'deliverable': item.get('deliverable') or 'report',
                'assignedTo': aid,
                'directedTo': True,
                'sourcePaths': source_paths,
                # Commit-tool grants ride the minted card: the item-level
                # approvedTools (e.g. the scan's api_call authority) must
                # survive queue_work's whitelist to reach task_tool_policy,
                # or the card burns tool_policy_denied refusals (2026-11-24).
                'approvedTools': list(item.get('approvedTools') or []),
                'priority': item.get('priority') or 'low',
                'feature': item.get('feature')
                    or f"scheduled:{item.get('id')}",
            }])
        _log_governance(state, None, 'scheduled_item_minted',
                        {'id': item.get('id'), 'owners': owners,
                         'cadence': item.get('cadence'),
                         'digestPath': digest_path})
    return minted
