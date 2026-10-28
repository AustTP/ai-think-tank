

@asynccontextmanager
async def _lifespan(app):
    try:
        _ensure_runtime_schemas()
    except Exception as e:
        print(f'[schema] runtime ensure failed: {e}', flush=True)
    # The admin/manager/director information should
    # live in the DATABASE, not be baked into a JS file. It does -- the
    # kv_state blob is authoritative and agents/*.json are only materialized
    # mirrors of it. This migration is the one-time backfill that gets the
    # director tier onto the EXISTING, already-seeded roster in the DB (the
    # seed agents.js array doesn't reach a running DB, and hired agents never
    # had a director). Idempotent: only stamps fields that are missing, so it
    # can run on every boot without clobbering a director the client set later.
    # Drop decision-key measurements older than the rolling window so the
    # instrumentation table stays bounded across restarts.
    _prune_decision_key_stats()
    try:
        _backfill_directors_in_db()
    except Exception as e:
        print(f'[director] backfill failed: {e}', flush=True)
    try:
        _backfill_teams_in_db()
    except Exception as e:
        print(f'[teams] backfill failed: {e}', flush=True)
    # One-time migration of the director-owned template library onto an EXISTING
    # (already-seeded) DB -- seeding writes it only on a cold start, so a live
    # think_tank.db gets its initial copy of _SEED_PROFILES here. Idempotent: only
    # fills roles that are missing and never overwrites a director's edits.
    try:
        # Integrity gate before the first read: flag a torn or edited
        # whole-state blob (see 015) instead of booting half a village.
        try:
            _verify_state_checksum_at_boot()
        except Exception:
            pass
        startup_state = get_state_from_db()
        if startup_state is not None and _init_templates_in_db(startup_state):
            save_state_to_db(startup_state)
    except Exception as e:
        print(f'[templates] backfill failed: {e}', flush=True)
    # Must run after the templates backfill above -- it resolves missing
    # per-agent profiles through the same DB-backed template chain.
    try:
        _backfill_agent_identity_in_db()
    except Exception as e:
        print(f'[identity] backfill failed: {e}', flush=True)
    # The Research Desk's standing watchlist (world/watchlist.json) seeds
    # researchTopics on boot. This is the REAL boot path, so it also authorizes
    # write-back of villager-added topics into that file -- tests never run
    # lifespan, so a test or stray import can never mutate operator-owned JSON.
    import core.config as config
    config.ALLOW_WATCHLIST_WRITE = True
    config.ALLOW_WORKSPACE_SYNC = True
    try:
        startup_state = get_state_from_db()
        if startup_state is not None and _seed_watchlist_topics(startup_state):
            save_state_to_db(startup_state)
    except Exception as e:
        print(f'[watchlist] seed failed: {e}', flush=True)
    # External-feed registries live in the DB (rss_feeds / ws_feeds /
    # api_services) but their IN-MEMORY copies are loaded by init_db -- and
    # init_db only runs under `python3 serve.py`. This process is started as
    # `uvicorn serve:app`, so _WS_FEEDS/_RSS_FEEDS stayed empty and the
    # supervisor iterated nothing: read_ais_feed answered "connection=down,
    # 0 frames" forever even with a healthy key and an ACTIVE row in ws_feeds
    # (2026-10-13 review; GET /api/rss/recent likewise returned {"feeds": {}}).
    # _load_api_services already runs at import -- that is why api_call still
    # worked. Load all three here so every boot path starts with warm
    # registries. Idempotent: each loader upserts into module-level dicts.
    for _label, _loader in (('api', _load_api_services),
                            ('rss', _load_rss_feeds),
                            ('ws', _load_ws_feeds)):
        try:
            _loader()
        except Exception as e:
            print(f'[{_label}] registry load failed: {e}', flush=True)
    health_task = asyncio.create_task(_health_check_loop())
    credits_probe_task = asyncio.create_task(_openrouter_credits_probe_loop())
    director_task = asyncio.create_task(_director_approval_loop())
    backup_task = asyncio.create_task(_backup_loop())
    prune_task = asyncio.create_task(_log_prune_loop())
    tier_refresh_task = asyncio.create_task(_model_tier_refresh_loop())
    tier_boot_backfill_task = asyncio.create_task(_model_tier_boot_backfill())
    photo_watch_task = asyncio.create_task(_photo_album_watch_loop())
    tier_price_watch_task = asyncio.create_task(_tier_price_watch_loop())
    service_price_drift_task = asyncio.create_task(_service_price_drift_loop())
    tier_failure_recheck_task = asyncio.create_task(_tier_failure_recheck_loop())
    calibration_task = asyncio.create_task(_calibration_loop())
    weekly_review_task = asyncio.create_task(_weekly_review_loop())
    colab_task = asyncio.create_task(_colab_failover_loop())
    colab_compute_task = asyncio.create_task(_colab_compute_idle_loop()) \
        if COLAB_CLI_AVAILABLE else None
    telegram_task = None
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS:
        telegram_task = asyncio.create_task(_telegram_poll_loop())
        print(f'[telegram] bridge active for {len(TELEGRAM_ALLOWED_CHAT_IDS)} allowlisted chat(s)', flush=True)
    webhook_task = None
    _webhook_active = active_webhook_channels()
    if _webhook_active:
        webhook_task = asyncio.create_task(_webhook_poll_loop())
        print(f'[webhook] consumer active for {", ".join(_webhook_active)}', flush=True)
    ask_drain_task = asyncio.create_task(_pending_ask_drain_loop())
    avatar_task = asyncio.create_task(_avatar_task_loop())
    rss_task = asyncio.create_task(_rss_poll_loop())
    ws_supervisor_task = asyncio.create_task(_ws_supervisor_loop())
    # Sim task + watchdog. sim_holder is shared with _sim_watchdog_loop so a
    # dead or frozen loop can be recreated mid-run (2026-10-13 freeze: the
    # task died silently and nothing restarted it). shutting_down stops the
    # watchdog's recycle loop before teardown cancels the final task.
    sim_holder = {'task': None, 'shutting_down': False}
    watchdog_task = asyncio.create_task(_sim_watchdog_loop(sim_holder))
    try:
        import sim as _sim_module
        # Phase 3 slice 2: register the research content executor so an arriving
        # observatory/research task performs REAL work (crawl > save > chat >
        # Library write) instead of the slice-1 workUntil placeholder. Runs on a
        # background thread fired by sim._dispatch_content_work; results merge
        # back through the next task_cycle pass (single read-modify-write).
        # The executors live in content.py (consolidation); imported lazily here
        # so serve never imports content at module top (content imports serve,
        # so a top-level import would be the import-time cycle).
        from content import _server_content_dispatcher
        _sim_module._content_executor = _server_content_dispatcher
        # A content executor is a process-local thread. Any task left in flight
        # by the previous process has a frozen heartbeat; attach a report it
        # already wrote, or clear the marker so the normal fallback handles it.
        try:
            _startup_state = get_state_from_db()
            if (_startup_state is not None
                    and _sim_module._reconcile_restarted_content_runs(_startup_state)):
                save_state_to_db(_startup_state)
        except Exception as e:
            print(f'[sim] content restart reconcile failed: {e}', flush=True)
        sim_holder['task'] = asyncio.create_task(_sim_module._sim_loop())
    except Exception as e:
        print(f'[sim] failed to start loop: {e}', flush=True)
    yield
    health_task.cancel()
    credits_probe_task.cancel()
    director_task.cancel()
    backup_task.cancel()
    calibration_task.cancel()
    weekly_review_task.cancel()
    colab_task.cancel()
    if colab_compute_task is not None:
        colab_compute_task.cancel()
    prune_task.cancel()
    tier_refresh_task.cancel()
    tier_boot_backfill_task.cancel()
    photo_watch_task.cancel()
    tier_price_watch_task.cancel()
    service_price_drift_task.cancel()
    tier_failure_recheck_task.cancel()
    if telegram_task is not None:
        telegram_task.cancel()
    if webhook_task is not None:
        webhook_task.cancel()
    ask_drain_task.cancel()
    if avatar_task is not None:
        avatar_task.cancel()
    rss_task.cancel()
    ws_supervisor_task.cancel()
    for _fp, task in list(_WS_TASKS.values()):
        task.cancel()
    # Stop the sim watchdog's recycle loop FIRST, then cancel the final sim
    # task it owns -- otherwise a mid-teardown done-check could spawn a new
    # loop we never cancel.
    sim_holder['shutting_down'] = True
    watchdog_task.cancel()
    if sim_holder.get('task') is not None:
        sim_holder['task'].cancel()
    # Shutdown restores the writeback gate: the boot path above set
    # config.ALLOW_WATCHLIST_WRITE = True for the server's whole run, and a test
    # that enters this lifespan (`async with serve._lifespan(...)`) must not
    # leave the operator-owned writeback permanently authorized after it exits.
    import core.config as _config
    _config.ALLOW_WATCHLIST_WRITE = False
    _config.ALLOW_WORKSPACE_SYNC = False


# Workers report to a DIRECTOR -- never directly to the admin. Kept in sync
# with the DB backfill so a fresh start and a repaired DB agree. The invariant
# (enforced by the repair sweep in _backfill_directors_in_db and by
# _safe_director on every write): an admin's direct reports must themselves be
# directors (isDirector, or anyone with their own reports). Pure workers with
# a chief of staff as director are re-pointed to the senior director.
#
# The chief of staff (theo) stays top-level (director=None) with authority via isAdmin,
# and the senior director (nora) is the other top-level root. A single agent
# can hold both roles, but R2 keeps the authority path explicit:
#   admin (theo) --> director(s) (nora, dev once it has reports, ...) --> workers
# A REAL admin -> director -> director -> employee nesting only exists once a
# mid-level lead (e.g. dev, the Studio lead) has its own reports AND is
# explicitly re-moved under the chief of staff in the roster:
#   theo -> dev -> nadia/priya/omar/yuki/greta/sam2/mira
# (theo no longer carries any seed workers; those report to nora.)
# and the rest of the roster reports straight up to a director.
def _director_backfill_map():
    """Return the per-install director reporting map.

    No agent names are hardcoded: the map comes from the per-install
    SEED_DIRECTOR_MAP env key so a cloned repo carries zero identities.
    Format: comma-separated id:directorId pairs, for example
    ada:nora,dev:nora,eli:nora,ben:nora,cora:nora.

    A chief of staff id is an invalid director target. The boot repair sweep
    re-points any pure worker under a chief of staff to the senior director.
    """
    raw = _load_env().get('SEED_DIRECTOR_MAP', '').strip()
    out = {}
    for pair in (raw.split(',') if raw else []):
        pair = pair.strip()
        if ':' not in pair:
            continue
        aid, did = (p.strip() for p in pair.split(':', 1))
        if aid and did:
            out[aid] = did
    return out


# The DB is the single, authoritative home for EVERY bit of admin/director
# identity. This backfill is where it all gets stamped -- it runs on every
# boot, idempotently, so whether the roster came from agents.js's bare seed (a
# fresh start) or from a prior session, the server is the one name for who is
# admin (isAdmin), who is a director (isDirector), and who each worker reports
# to (director). Nothing admin/director-related lives in the JS files anymore
# (agents.js carries no isAdmin/isDirector/director fields on purpose -- see
# its header comment).
#
# ONE admin agent. The chief of staff approves/denies
# requests and passes everything else down to the directors; the senior-most
# director approves/denies on the chief of staff's behalf. So:
#   - Theo (renamed from Faye, its original id/name) stays the single admin (isAdmin: true). He is
#     also a director, sits at the top of the chain (no own director), and
#     under R2 his direct reports are always other directors -- never pure
#     workers (the repair sweep enforces this on every boot).
#   - Nora is demoted from admin to the SENIOR-MOST DIRECTOR (isDirector: true,
#     no isAdmin, no own `director`), standing in for Theo on approvals.
#   - Everything below reports up to a director.
# NO agent identities hardcoded: the single admin and the
# senior-most director come from the per-install SEED_ADMIN_IDS /
# SEED_SENIOR_DIRECTOR env keys, so a cloned repo carries zero agent names.
# They are only evaluated at backfill time (via the helpers below), not at
# import time, because _load_env is defined later in this module.
def _admin_ids():
    raw = _load_env().get('SEED_ADMIN_IDS', '').strip()
    return {x.strip() for x in raw.split(',') if x.strip()}


def _senior_director_id():
    return _load_env().get('SEED_SENIOR_DIRECTOR', '').strip() or None

def _backfill_directors_in_db():
    state = get_state_from_db()
    if not state:
        return
    roster = state.get('agentRoster', [])
    admin_ids = _admin_ids()
    senior_dir = _senior_director_id()
    backfill_map = _director_backfill_map()
    changed = False
    for d in roster:
        aid = d.get('id')
        is_admin = aid in admin_ids
        is_senior_dir = (aid == senior_dir)
        if bool(d.get('isAdmin')) != is_admin:
            d['isAdmin'] = is_admin
            changed = True
        # Theo (admin) and Nora (senior director) are both directors.
        if (is_admin or is_senior_dir) and not d.get('isDirector'):
            d['isDirector'] = True
            changed = True
        if is_admin or is_senior_dir:
            d.setdefault('director')  # a top-level approver/director has no own director
        elif d.get('director') is None:
            d['director'] = backfill_map.get(aid, senior_dir)
            changed = True
    # R2 repair sweep: a chief of staff must never DIRECTLY director a pure worker.
    # A pure worker = no direct reports AND not isDirector. Runs AFTER the
    # stamping loop above (not mid-loop) because it needs every isAdmin /
    # isDirector / director pointer stamped before it can tell a pure worker
    # from a mid-level director. Re-points any pure worker whose director is
    # a chief of staff up to the senior director (or any non-chief-of-staff director) so there
    # is always a director between the chief of staff and every worker. Mid-level
    # directors (someone who HAS reports) are left under the chief of staff -- that is
    # the legal admin -> director -> worker shape.
    report_counts = {}
    for d in roster:
        report_counts[d.get('director')] = report_counts.get(d.get('director'), 0) + 1
    for d in roster:
        aid = d.get('id')
        if aid in admin_ids:
            continue
        if d.get('isDirector') or report_counts.get(aid, 0) > 0:
            continue  # a director (flagged or has reports): admin->director is fine
        if d.get('director') in admin_ids:
            target = senior_dir if senior_dir and senior_dir not in admin_ids else None
            if target is None:
                for other in roster:
                    oid = other.get('id')
                    if (oid and oid != aid and oid not in admin_ids and
                            (other.get('isDirector') or report_counts.get(oid, 0) > 0)):
                        target = oid
                        break
            if target and target != d.get('director'):
                d['director'] = target
                changed = True
    # Flat-chart repair: every director answers to the chief of staff directly,
    # never to another director. A director (flagged, or with reports)
    # whose own pointer names a non-chief-of-staff is re-pointed at the chief of staff --
    # the standing every director shares. A missing pointer is already
    # compliant (it answers to the chief of staff by fallback). Pure workers
    # keep pointing at directors.
    reports_now = {}
    for d in roster:
        reports_now[d.get('director')] = reports_now.get(d.get('director'), 0) + 1
    admin_agent = next((d.get('id') for d in roster if d.get('isAdmin')), None)
    for d in roster:
        aid = d.get('id')
        if aid in admin_ids:
            continue
        if not (d.get('isDirector') or reports_now.get(aid, 0) > 0):
            continue  # a pure worker: any director pointer is legal
        parent = d.get('director')
        if parent is None or parent in admin_ids:
            continue  # already directly under the chief of staff (or equivalent)
        d['director'] = admin_agent
        changed = True
    if changed:
        save_state_to_db(state)


# ---------------------------------------------------------------------------
# Flat-chart director model ("admin -> director -> employee", with every
# director sitting DIRECTLY under the admin). Authority is NOT a boolean
# stamped per-agent; it is DERIVED from the `director` reporting pointers
# the backfill above assigns. ANY agent who has one or more direct reports
# (someone whose `director` points at them) is a director. A lead like dev
# or sam becomes a director for free the moment someone reports to them, and
# restructuring a team is just re-pointing a `director` -- no flag to
# maintain. Directors never director other directors: the repair sweep below
# flattens any such pointer up to the admin, and POST /api/state rejects a
# roster that would create one.
#
# The three derivations every approval/write check in this file needs:
#   _direct_reports(state, id)  -- who reports directly to this agent
#   _director_chain(state, id)  -- [self, my director, their director, ..., admin]
#   _can_write_agent(state, writer, target)
#       -- writer === target, OR the writer is one of target's directors/above,
#          OR the writer is an admin.
# Plus _is_admin(state, id) so a chief of staff check works off the same state object
# instead of a module constant that can drift.
# ---------------------------------------------------------------------------
def _is_admin(state, agent_id):
    roster = state.get('agentRoster', []) if state else []
    for d in roster:
        if d.get('id') == agent_id:
            return bool(d.get('isAdmin'))
    return False


def _admin_agent_id(state):
    """The current admin's real id -- never hardcoded (names are server-
    seeded and can change), so anything that wants "the admin" specifically
    (e.g. the Telegram bridge) resolves it fresh each time."""
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            return d.get('id')
    return None


def _require_player_or_admin(request):
    """Operator gate: the player's real session, OR a verified agent key whose
    roster entry is isAdmin. Widens the four player-only operator endpoints
    (/api/policy/reload, /api/budget, /api/env/reload,
    /api/sandbox-backups/restore) to the chief of staff agent -- the two operator
    identities -- without opening them to ordinary agents. Fail closed."""
    if _require_player_session(request):
        return True
    requester = _resolve_requester(request)
    if not requester:
        return False
    if verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
        return False
    return _is_admin(get_state_from_db() or {}, requester)


def _direct_reports(state, agent_id):
    roster = state.get('agentRoster', []) if state else []
    return [d.get('id') for d in roster if d.get('director') == agent_id]


def _supervisor_of(state, agent_id):
    """Who supervises `agent_id`: the roster `director` field, else
    the chief of staff (the chain root) when the entry names no director, else
    the player. 'player' means the human is the decision authority --
    true for the player itself, for the chief of staff (whose requests route
    to the player), and when no admin exists."""
    if not agent_id or agent_id == 'player':
        return 'player'
    admin = _admin_agent_id(state)
    if agent_id == admin:
        return 'player'
    if state:
        for d in state.get('agentRoster') or []:
            if isinstance(d, dict) and d.get('id') == agent_id:
                return d.get('director') or admin or 'player'
    return admin or 'player'


def _is_direct_supervisor(state, supervisor_id, agent_id):
    """True when `supervisor_id` is the DIRECT supervisor of
    `agent_id` -- the roster `director` field names them exactly.
    Never a grand-supervisor, a sibling director, or a worker."""
    if not supervisor_id or not agent_id or supervisor_id == agent_id:
        return False
    if not state:
        return False
    for d in state.get('agentRoster') or []:
        if isinstance(d, dict) and d.get('id') == agent_id:
            return d.get('director') == supervisor_id
    return False


def _subtree_agent_ids(state, root_id):
    """`root_id` plus every agent below it in the roster chain (BFS
    over `director` fields) -- the full team a director's slice
    covers, for subtree spend rollups. Cycle-safe via a seen set."""
    reports = {}
    if state:
        for d in state.get('agentRoster') or []:
            if isinstance(d, dict) and d.get('id'):
                reports.setdefault(d.get('director'), []).append(d.get('id'))
    seen = [root_id]
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        for child in reports.get(current, []):
            if child not in seen:
                seen.append(child)
                queue.append(child)
    return seen


def _director_parent_ok(state, proposed_director_id):
    """Flat org chart guard for a DIRECTOR's own parent: the only
    legal parent is the chief of staff (or nobody -- a missing pointer answers
    to the chief of staff by fallback). Any other parent is a
    director-under-director, the one shape the flattened chart
    forbids. Pure workers are not judged here: they may name any
    director."""
    if not proposed_director_id:
        return True
    roster = state.get('agentRoster', []) if state else []
    parent = next((d for d in roster if isinstance(d, dict)
                   and d.get('id') == proposed_director_id), None)
    return bool(parent and parent.get('isAdmin'))


def _flat_director_chart_violations(state):
    """Ids of every director (flagged, or with direct reports) whose
    own `director` pointer climbs to a non-chief-of-staff -- the shape the
    flattened org chart forbids. A missing pointer is compliant: it
    answers to the chief of staff by fallback."""
    if not state:
        return []
    roster = state.get('agentRoster', [])
    reports = {}
    for d in roster:
        if isinstance(d, dict) and d.get('id'):
            reports[d.get('director')] = reports.get(d.get('director'), 0) + 1
    bad = []
    for d in roster:
        if not isinstance(d, dict) or not d.get('id'):
            continue
        if not (d.get('isDirector') or reports.get(d.get('id'), 0) > 0):
            continue  # a pure worker: any parent is legal
        if d.get('director') is None:
            continue  # answers to the chief of staff by fallback
        if not _director_parent_ok(state, d.get('director')):
            bad.append(d.get('id'))
    return bad


def _safe_director(state, agent_id, proposed_director_id):
    """R2 + flat-chart guard for director-pointer writes. A chief of staff must
    never DIRECTLY director a pure worker (no reports and not
    isDirector) -- such a pointer falls back to the senior director
    (or any non-chief-of-staff director), so a director always sits between the
    admin and every worker. A DIRECTOR's own parent must be the admin
    (or nobody): a director proposed under another director is
    flattened to the admin, so every director sits directly under the
    admin. A worker proposed under a director passes through."""
    if not proposed_director_id:
        return proposed_director_id
    roster = state.get('agentRoster', []) if state else []
    admins = {d.get('id') for d in roster if d.get('isAdmin')}
    agent = next((d for d in roster if d.get('id') == agent_id), None)
    if agent and (agent.get('isDirector') or _direct_reports(state, agent_id)):
        # a director: the chief of staff is the only legal parent
        if proposed_director_id in admins:
            return proposed_director_id
        admin_agent = next((d.get('id') for d in roster if d.get('isAdmin')), None)
        return admin_agent if admin_agent else proposed_director_id
    if proposed_director_id not in admins:
        return proposed_director_id  # worker under a director: fine
    # pure worker under an admin: re-point to a non-chief-of-staff director
    senior = next((d.get('id') for d in roster
                   if d.get('isDirector') and d.get('id') not in admins), None)
    if senior:
        return senior
    for other in roster:
        oid = other.get('id')
        if (oid and oid != agent_id and oid not in admins and
                (other.get('isDirector') or _direct_reports(state, oid))):
            return oid
    return proposed_director_id  # nothing better available


def _director_chain(state, agent_id):
    # Walk up the reporting tree from an agent to the top. Returns the list
    # [agent_id, their director, that director's director, ...] in ascending
    # order of seniority, so an intermediate director appears in the chain of
    # everyone below them. Cycle-safe: bails if we ever revisit an id.
    chain = []
    seen = set()
    cur = agent_id
    roster_lookup = {}
    if state:
        for d in state.get('agentRoster', []):
            roster_lookup[d.get('id')] = d.get('director')
    while cur and cur not in seen and cur in roster_lookup:
        seen.add(cur)
        chain.append(cur)
        cur = roster_lookup[cur]
    if cur and cur not in seen:
        chain.append(cur)
    return chain


def _can_write_agent(state, writer_id, target_id):
    # The ACL rule for per-agent files: an agent can write their OWN files,
    # their DIRECTORS (and any director above them, all the way to the admin)
    # can write too, and the chief of staff can write everything. Walking the target's
    # reporting chain upward and testing whether the writer sits on it IS the
    # whole rule -- no flags needed.
    if _is_admin(state, writer_id):
        return True
    if writer_id == target_id:
        return True
    return writer_id in _director_chain(state, target_id)


# ---------------------------------------------------------------------------
# First-class teams. A team is a decorative label over the EXISTING
# walk-the-chain `director` graph -- it does NOT replace the reporting tree.
# An agent belongs to a team iff their `director` line resolves (recursively)
# to that team's `directorId`. So `members` is always DERIVED from the
# `director` pointers, never stored separately -- no membership list to drift.
# A team only exists for a director who has at least one report, so promoting
# an employee to director (they gain a report) spawns a team for them, and an
# empty director spawns nothing.
# ---------------------------------------------------------------------------
def _director_ids(state):
    """Who counts as a director: anyone with >=1 direct report, plus an
    explicit isDirector flag (a director who's mid-restructure with no live
    report yet still leads a team). Used to resolve team membership to the
    NEAREST director a member reports into; team LEADERSHIP is a separate
    question and excludes the admin (see _backfill_teams_in_db)."""
    roster = state.get('agentRoster', []) if state else []
    counts = {}
    for d in roster:
        counts[d.get('director')] = counts.get(d.get('director'), 0) + 1
    ids = {aid for aid, n in counts.items() if n and aid}
    for d in roster:
        if d.get('isDirector') or d.get('isAdmin'):
            ids.add(d.get('id'))
    return ids


def _derive_team_members(state, director_id):
    """Who belongs to director_id's team, DERIVED from the `director` graph:
    a WORKER (not a director) belongs to the team of the NEAREST director they
    report into. Directors themselves are never counted as members of their
    parent's team -- a mid-director (dev under theo) or a promoted employee
    (maya) leads their OWN team, so they drop out of the parent team the moment
    they gain the director stamp. Thus every worker has exactly one team."""
    directors = _director_ids(state)
    out = []
    for d in (state.get('agentRoster') or []):
        aid = d.get('id')
        if aid == director_id:
            continue
        if aid in directors:
            continue  # directors lead their own team, they're not members here
        worker_chain = _director_chain(state, aid)
        nearest = next((x for x in worker_chain if x in directors), None)
        if nearest == director_id:
            out.append(aid)
    return sorted(out)


def _backfill_teams_in_db():
    """Idempotent boot migration: materialize `state['teams']` from the
    existing `director` graph so every director with reports gets a team.
    Never clobbers an existing team's funny `name`/`purpose`; only adds teams
    that are missing and drops teams whose director vanished. Re-run safe."""
    state = get_state_from_db()
    if not state:
        return
    roster = state.get('agentRoster', [])
    teams = state.get('teams') or []
    by_id = {t.get('id'): t for t in teams}

    # Which roster ids are directors with at least one report?
    reporter_counts = {}
    for d in roster:
        reporter_counts[d.get('director')] = reporter_counts.get(d.get('director'), 0) + 1
    director_ids = {aid for aid, n in reporter_counts.items() if n and aid}
    # Explicit isDirector flags also qualify (a very senior director with no
    # live report yet shouldn't lose their team mid-restructure).
    for d in roster:
        if d.get('isDirector'):
            director_ids.add(d.get('id'))
    # The admin is a director for authority and membership purposes, but the
    # chief of staff coordinates the directors; it does not lead a team. Drop
    # it from the leadership set so it never gets (or keeps) a phantom team.
    admin_ids = _admin_ids()
    director_ids -= set(admin_ids or ())

    changed = False
    fresh = []
    # Deterministic order: roster order (admin/directors first), then alpha.
    for d in roster:
        aid = d.get('id')
        if aid not in director_ids:
            continue
        existing = by_id.get(aid)
        if existing:
            # Keep a live team whose director is still present; refresh purpose.
            freed = dict(existing)
            freed['directorId'] = existing.get('directorId', aid)
            freed['members'] = _derive_team_members(state, aid)
            fresh.append(freed)
            continue
        name = _default_team_names().get(aid) or f"{d.get('role') or d.get('name') or aid}'s Team"
        fresh.append({
            'id': aid,
            'name': name,
            'directorId': aid,
            'purpose': f"Team directed by {d.get('name', aid)}.",
            'members': _derive_team_members(state, aid),
            'createdAt': time.time(),
        })
        changed = True

    # Drop teams whose director no longer has reports and isn't a director.
    new_teams = []
    for t in fresh:
        # keep if director still on roster AND (has reports OR isDirector/admin)
        dir_id = t.get('directorId')
        is_dir = dir_id in director_ids
        if is_dir:
            new_teams.append(t)
        else:
            changed = True
    if new_teams != teams or changed:
        state['teams'] = new_teams
        save_state_to_db(state)


_TEAM_FUNNY_NAMES: dict[str, str] = {}  # no agent-identity literals -- team names derive from the roster below


def _default_team_names():
    # Deterministic, identity-free team display names. NO agent names are
    # hardcoded: the fallback uses the roster's own role label
    # (or a generic name) so a cloned repo carries zero identities.
    return dict(_TEAM_FUNNY_NAMES)


# Mirrors hiring.js/sim.py's own HIRE_COLOR_POOL -- kept as a separate literal
# rather than importing sim.py here purely for a fallback color, since this
# backfill only ever reaches for it when even the roster has no color on file
# (see below).
_FALLBACK_COLOR_POOL = ['#f6b26b', '#76a5af', '#a4c2f4', '#d5a6bd', '#b6d7a8', '#ffe599']


def _stable_fallback_color(agent_id):
    # A plain hash(str) is per-process randomized (PYTHONHASHSEED), which
    # would reshuffle an agent's placeholder color on every restart -- use a
    # deterministic sum instead so it's at least stable across boots.
    return _FALLBACK_COLOR_POOL[sum(ord(c) for c in agent_id) % len(_FALLBACK_COLOR_POOL)]


# Idempotent boot migration, same shape as _backfill_directors_in_db/
# _backfill_teams_in_db above. Theo's nameplate on
# the map rendered as the literal text "undefined" and the HUD's morale meter
# read NaN. Both traced to the same root cause -- the original three seeded
# agents (theo, ada, ben) predate `name`/`color`/`role`/`approvedCount`/
# `droppedCount`/`profile` existing on the per-agent record at all, and
# nothing ever backfilled them onto a live DB the way director/team state
# already gets backfilled. `ctx.fillText(a.name, ...)` draws `undefined`
# verbatim when `a.name` is missing, and `moraleFor()` does
# `a.approvedCount * WEIGHT` with no null guard, poisoning the whole-think tank
# average with a single NaN. Only fills what's missing; never overwrites a
# real value (including a real 0) with a default, so this is safe to run on
# every boot.
def _heal_agent_identity(state):
    """Pure in-place repair: ensures every agent/roster record has the identity
    fields the rest of the app assumes exist (see _backfill_agent_identity_in_db
    for the full history). Returns True if it changed anything. Split out as a
    pure function (state in, bool out, no DB I/O) so BOTH the one-time boot
    migration below AND SimEngine.tick() (sim.py) can call it -- a boot-only
    fix wasn't durable: something (a stale open browser tab's 5s autosave,
    confirmed via a save_state_to_db stack trace) kept re-POSTing an
    old, pre-fix snapshot over the top. Running this as a per-tick invariant
    (same shape as _reconcile_stranded_agents/_repair_stalled_walkers already
    do for other drift) makes it self-healing regardless of the source."""
    roster = state.get('agentRoster', [])
    roster_by_id = {d.get('id'): d for d in roster}
    # Per-tick invariant: tolerate a malformed `agents` value (e.g. a stale
    # autosave shipping a non-dict) instead of crashing the whole tick -- the
    # point of running this heal every tick is self-healing, so a broken shape
    # must be normalized, not fatal.
    agents = state.get('agents', {})
    if not isinstance(agents, dict):
        state['agents'] = agents = {}
    defaults_by_id = {d['id']: d for d in _default_roster_definitions()}
    changed = False

    # The roster entries themselves can predate `color`/`model` too (ada/
    # ben/theo's roster defs have neither) -- fix those first since the
    # agents loop below sources its own backfill from the roster.
    for d in roster:
        aid = d.get('id')
        seed = defaults_by_id.get(aid, {})
        if not d.get('color'):
            d['color'] = seed.get('color') or _stable_fallback_color(aid)
            changed = True
        if not d.get('model'):
            d['model'] = seed.get('model') or 'small'
            changed = True

    for aid, a in agents.items():
        d = roster_by_id.get(aid, {})
        seed = defaults_by_id.get(aid, {})
        if not a.get('name'):
            a['name'] = d.get('name') or seed.get('name') or aid.title()
            changed = True
        if not a.get('color'):
            a['color'] = d.get('color') or seed.get('color') or _stable_fallback_color(aid)
            changed = True
        if not a.get('role'):
            a['role'] = d.get('role') or seed.get('role') or 'Researcher'
            changed = True
        if not a.get('model'):
            a['model'] = d.get('model') or seed.get('model') or 'small'
            changed = True
        if a.get('approvedCount') is None:
            a['approvedCount'] = 0
            changed = True
        if a.get('droppedCount') is None:
            a['droppedCount'] = 0
            changed = True
        if a.get('weekApprovals') is None:
            a['weekApprovals'] = 0
            changed = True
        if not a.get('mailbox'):
            a['mailbox'] = []
            changed = True
        if not a.get('conversationLog'):
            a['conversationLog'] = []
            changed = True
        if a.get('elevatedAccess') is None:
            a['elevatedAccess'] = False
            changed = True
        if 'accessGrant' not in a:
            a['accessGrant'] = None
            changed = True
        if not a.get('profile'):
            profile = _profile_for_role(state, a['role'])
            a['profile'] = {
                'mission': profile['mission'],
                'instructions': list(profile['instructions']),
                'notes': list(profile['notes']),
            }
            changed = True

    return changed


def _backfill_agent_identity_in_db():
    state = get_state_from_db()
    if not state:
        return
    if _heal_agent_identity(state):
        save_state_to_db(state)


def _promote_to_director(state, promotee_id, promoter_id):
    """Promote an employee to director: they stop reporting into the old team's
    daily work and instead become the director of a NEW team they can hire for.
    They sit DIRECTLY under the admin, in equal standing with every other
    director -- the org chart shows no director-to-director edge (the operator
    keeps the chart flat; a promoting director gains no subordinate director).
    Authority still resolves to the chief of staff because the chief of staff is the chain root.
    With no admin in the roster this falls back to the promoter's chain.
    Mutates `state` in place; returns the new team dict."""
    roster = state.get('agentRoster', [])
    promotee = next((d for d in roster if d.get('id') == promotee_id), None)
    if not promotee:
        return None
    # Stamp isDirector first so the R2 guard below sees the promotee as a
    # director (a chief of staff MAY directly director another director).
    promotee['isDirector'] = True
    admin_id = _admin_agent_id(state)
    proposed = admin_id if admin_id else promoter_id
    promotee['director'] = _safe_director(state, promotee_id, proposed)
    promotee['directorSince'] = time.time()
    # Spawn a fresh team for the new director (no reports yet, so the backfill
    # would skip them -- the explicit record is what keeps it alive).
    teams = state.setdefault('teams', [])
    existing = next((t for t in teams if t.get('directorId') == promotee_id), None)
    if existing:
        existing['directorId'] = promotee_id
        existing['name'] = existing.get('name') or f"{promotee.get('name', promotee_id).title()}'s Crew"
        existing['purpose'] = existing.get('purpose') or f"New team directed by {promotee.get('name', promotee_id)}."
        new_team = existing
    else:
        new_team = {
            'id': promotee_id,
            'name': f"{promotee.get('name', promotee_id).title()}'s Crew",
            'directorId': promotee_id,
            'purpose': f"New team directed by {promotee.get('name', promotee_id)}.",
            'members': [],
            'createdAt': time.time(),
        }
        teams.append(new_team)
    return new_team


def _team_dirs():
    """Filesystem root for team-shared space, outside world/ like agents/."""
    return os.path.join(AGENTS_DIR, 'teams')


def _team_shared_dir(team_id):
    return os.path.join(_team_dirs(), team_id, 'shared')


def _can_write_team(state, writer_id, team_id):
    """A team member or any of that team director's superiors may write the
    team's shared dir. The chief of staff writes everything. Reuses the reporting ACL."""
    teams = state.get('teams', []) if state else []
    t = next((x for x in teams if x.get('id') == team_id), None)
    if not t:
        return False
    if _is_admin(state, writer_id):
        return True
    # The team director themself, or a member (derived live, so a promotion
    # immediately strips the old team's membership from the ACL).
    member_ok = writer_id == team_id or writer_id in _derive_team_members(state, team_id)
    if member_ok:
        return True
    return writer_id in _director_chain(state, t.get('directorId'))


def _can_set_team_agreement(state, writer_id, team_id):
    """Who may edit a team's teamwork agreement: the player, the admin, or
    THIS team's director only. Deliberately stricter than _can_write_team --
    shared-space writes also let chain-above directors and members through,
    but an agreement changes that team's sim behavior, so it stays with the
    team's own director (plus the operator)."""
    if writer_id == 'player' or _is_admin(state, writer_id):
        return True
    t = next((x for x in ((state or {}).get('teams') or []) if x.get('id') == team_id), None)
    if not t:
        return False
    return writer_id in (t.get('directorId'), t.get('id'))


def _team_record(state, team_id):
    """The live team record for team_id, refreshed with DERIVED members (never
    the stored snapshot -- same invariant as list_teams)."""
    for t in (state.get('teams') or []):
        if t.get('id') == team_id:
            tcopy = dict(t)
            tcopy['members'] = _derive_team_members(state, team_id)
            return tcopy
    return None
