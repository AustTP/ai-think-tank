

def _backup_think_tank_db():
    # sqlite3.Connection.backup() produces a consistent-on-disk snapshot even
    # under WAL (-wal/-shm live beside the main file), which a naive file copy
    # would NOT be. Written to a timestamped file so corruption has a history
    # to fall back to, then pruned to DB_BACKUP_KEEP newest. Callable from an
    # asyncio task (via asyncio.to_thread -- backup() is synchronous IO) and
    # from the synchronous post-run shutdown hook.
    if not os.path.isdir(DB_BACKUP_DIR):
        os.makedirs(DB_BACKUP_DIR, exist_ok=True)
        try:
            # snapshots inherit the DB's sensitivity
            os.chmod(DB_BACKUP_DIR, 0o700)
        except OSError:
            pass
    stamp = time.strftime('%Y%m%d-%H%M%S') + f'-{int(time.time() * 1000000) % 1000000:06d}'
    dest = os.path.join(DB_BACKUP_DIR, f'think_tank.db-{stamp}.bak')
    try:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(dest)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as e:
        print(f'[backup] snapshot failed: {e}', flush=True)
        return
    _harden_db_perms(dest)
    # Prune to the newest DB_BACKUP_KEEP by MTIME. The filenames are not
    # sortable as a proxy for age (think_tank.db-* vs the legacy village.db-*
    # the rename left behind: 't' < 'v' alphabetically, which used to keep the
    # orphaned old files and delete the fresh checkpoints). Sort on the real
    # file mtime so the newest survive and the oldest go.
    try:
        files = sorted(
            (f for f in os.listdir(DB_BACKUP_DIR) if f.endswith('.bak')),
            key=lambda f: os.path.getmtime(os.path.join(DB_BACKUP_DIR, f)),
        )
    except OSError:
        files = []
    for old in files[:-DB_BACKUP_KEEP]:
        try:
            os.remove(os.path.join(DB_BACKUP_DIR, old))
        except OSError:
            pass
    print(f'[backup] checkpoint -> {dest} ({len(files)} kept, newest {DB_BACKUP_KEEP})', flush=True)


async def _backup_loop():
    while True:
        await asyncio.sleep(DB_BACKUP_INTERVAL_S)
        await asyncio.to_thread(_backup_think_tank_db)


def _prune_logs():
    """Rolling retention prune for the two heavy append-only tables
    (decision_tape, action_log). Deletes rows older than the
    retention window (default 7 days; override LOG_RETENTION_DAYS in .env),
    capped per run so a large pre-existing backlog is drained over a few runs
    instead of one giant delete. Also prunes RESOLVED escalations from
    escalations.json once they pass the same window -- their approve/deny
    decision is already recorded in action_log, so the file only needs to keep
    them while they're still fresh. Best-effort: a prune failure must never
    take down the server. Runs on its own cadence via _log_prune_loop."""
    try:
        days = float(_load_env().get('LOG_RETENTION_DAYS', LOG_RETENTION_DAYS_DEFAULT) or LOG_RETENTION_DAYS_DEFAULT)
    except Exception:
        days = LOG_RETENTION_DAYS_DEFAULT
    cutoff = time.time() - max(0.0, days) * 86400
    deleted = 0
    try:
        with _db() as conn:
            # action_log keeps the noisy per-agent feed; pruned as before.
            cur = conn.execute(
                'DELETE FROM action_log WHERE id IN '
                '(SELECT id FROM action_log WHERE ts < ? LIMIT ?)',
                (cutoff, LOG_PRUNE_MAX_ROWS),
            )
            deleted += cur.rowcount
            # decision_tape is ARCHIVED, never just deleted: distil each old row
            # into decision_archive (the expensive prompt/raw columns are
            # dropped; the distilled decision is the institutional memory that
            # must survive) and then remove it from the tape. Same LIMIT
            # subquery cap as action_log so a large backlog is drained over
            # several runs, and the archive copy is bounded to the same batch.
            cur = conn.execute(
                'INSERT INTO decision_archive (ts, kind, model, choice, confidence, cost, ok, trace_id, tape_hash) '
                'SELECT ts, kind, model, choice, confidence, cost, ok, trace_id, hash '
                'FROM decision_tape WHERE ts < ? LIMIT ?',
                (cutoff, LOG_PRUNE_MAX_ROWS),
            )
            conn.execute(
                'DELETE FROM decision_tape WHERE id IN '
                '(SELECT id FROM decision_tape WHERE ts < ? LIMIT ?)',
                (cutoff, LOG_PRUNE_MAX_ROWS),
            )
            deleted += cur.rowcount
    except Exception as e:
        print(f'[prune] failed: {e}', flush=True)
        return 0
    pruned_escalations = 0
    try:
        escalations = _load_escalations()
        stale = [esc_id for esc_id, esc in escalations.items()
                 if esc.get('status') != 'pending'
                 and (esc.get('resolvedAt') or esc.get('ts') or 0) < cutoff]
        if stale:
            for esc_id in stale:
                del escalations[esc_id]
            _save_escalations(escalations, replace_missing=True)
            pruned_escalations = len(stale)
    except Exception as e:
        print(f'[prune] escalation prune failed: {e}', flush=True)
    try:
        memory_pruned = cleanup_expired_sessions()
    except Exception as e:
        memory_pruned = 0
        print(f'[prune] memory cleanup failed: {e}', flush=True)
    if deleted or pruned_escalations or memory_pruned:
        print(f'[prune] removed {deleted} rows older than {days}d from decision_tape/action_log '
              f'and {pruned_escalations} resolved escalations '
              f'and {memory_pruned} expired conversation-memory sessions', flush=True)
    return deleted


async def _log_prune_loop():
    global _LAST_LOG_PRUNE
    while True:
        await asyncio.sleep(LOG_PRUNE_INTERVAL_S)
        await asyncio.to_thread(_prune_logs)
        _LAST_LOG_PRUNE = time.time()


def _wiki_page_body(page_id, category):
    """Read a wiki page's current BODY straight off disk (the server-authority
    write path persists `library/wiki/{category}/{page_id}.md`), or '' when it
    does not exist yet. Used by the archive-distill step to merge a new weekly
    section into the existing decision-archive page without a DB round trip.
    Best-effort: any read failure returns '' so a caller always has a string."""
    try:
        path = os.path.join(LIBRARY_DIR, 'wiki', category or '', f'{page_id}.md')
        with open(path, encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


def _decision_archive_summary(since_ts):
    """Aggregate the decision_archive table since `since_ts` into a short digest:
    per-kind decision counts with ok-count + total spend, plus the overall
    totals. The raw material for the weekly decision-archive wiki page. Returns
    a dict, or None on any DB failure (callers treat that as 'skip this week')."""
    try:
        with _db() as conn:
            kinds = conn.execute(
                'SELECT kind, COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(cost),0) '
                'FROM decision_archive WHERE ts >= ? GROUP BY kind ORDER BY kind',
                (since_ts,)).fetchall()
            total = conn.execute(
                'SELECT COUNT(*), COALESCE(SUM(ok=0),0), COALESCE(SUM(cost),0) '
                'FROM decision_archive WHERE ts >= ?',
                (since_ts,)).fetchone()
        return {
            'kinds': [{'kind': r[0], 'count': r[1], 'ok': r[2], 'cost': r[3]} for r in kinds],
            'total': total[0] if total else 0,
            'failed': total[1] if total else 0,
            'cost': total[2] if total else 0.0,
        }
    except Exception:
        return None


async def _tier_refresh_logged(reason):
    """Run one full tier re-pick (refresh_model_tiers) and log it through the
    governance/audit path ONLY when a band's model actually CHANGED -- a cycle
    where every band held its model is a no-op check, not a governance event,
    so frequent runs don't bury the activity feed in identical entries. Logged
    attributed to the chief of staff (or the senior-most director standing in), so the
    player can see "Theo re-picked the tiers" like a bank review. `reason`
    records WHY this cycle ran (schedule vs price-watch trigger). Always runs
    the Jev decision-model failover check, which self-gates on availability.
    Returns (fresh, changed)."""
    before = {band: m['slug'] for band, m in get_cached_model_tiers().items()}
    fresh = await refresh_model_tiers()
    if not fresh:
        # OpenRouter outage shape: catalog reachable but every verify probe
        # failed, so refresh skipped every band and wrote NOTHING (rows keep
        # the last good picks). Keep them and say so -- and never log this as
        # a re-pick (comparing refresh's empty RETURN value against the table
        # would write a bogus "tiers changed to {}" governance entry).
        print('[model-tiers] re-pick found no live candidates (all models unavailable?) -- keeping current tiers', flush=True)
    state = get_state_from_db()
    actor = _admin_agent_id(state) or _senior_most_director_id(state)
    # Compare the TABLE after the write, not refresh's return value: a
    # partial refresh (some bands picked, others skipped by failed verify)
    # leaves skipped rows on their old picks, and the table is what calls
    # actually resolve against -- the return value can understate it.
    after = {band: m['slug'] for band, m in get_cached_model_tiers().items()}
    changed = after != before
    if changed:
        if actor:
            log_action(actor, 'model_tiers_refreshed',
                       {'tiers': after, 'reason': reason,
                        'note': 'scheduled re-pick: best scored value within each band\'s quality floor, then cheapest'},
                       authorized=False)
        else:
            print(f'[model-tiers] tier refresh changed picks, no admin/director to attribute to: {after}', flush=True)
    await _auto_failover_jev_if_gone(fresh, actor)
    return fresh, changed


async def _model_tier_boot_backfill():
    """One-shot, shortly after boot: if the fallback table is empty (first
    boot after the feature shipped, or a DB restored from before it), run the
    normal tier refresh so a band has alternates before its first availability
    failure. No-op once populated; the scheduled loop owns later refreshes."""
    await asyncio.sleep(5)
    try:
        with _db() as conn:
            _ensure_tier_fallbacks_table(conn)
            have = conn.execute(
                'SELECT COUNT(*) FROM model_tier_fallbacks').fetchone()[0]
        if not have:
            print('[model-tiers] no tier fallbacks stored; refreshing once to '
                  'backfill them', flush=True)
            await _tier_refresh_logged('boot backfill: tier fallbacks missing')
    except Exception as e:
        print(f'[model-tiers] fallback backfill skipped: {e}', flush=True)


async def _model_tier_refresh_loop():
    """Full model-tier re-pick of every band, every
    MODEL_TIER_REFRESH_INTERVAL_S (every other hour), run AS a governance
    action (see _tier_refresh_logged). Catches what a price watch cannot:
    a new/better model entering the catalog, an availability change, or a
    benchmark score update while the selected models' own prices hold."""
    while True:
        await asyncio.sleep(MODEL_TIER_REFRESH_INTERVAL_S)
        try:
            await _tier_refresh_logged('scheduled full re-pick')
        except Exception as e:
            # A refresh failure is not fatal -- keep the previous tiers and
            # retry on the next cycle (fails open to the last good choice).
            print(f'[model-tiers] tier refresh failed (keeping current tiers): {e}', flush=True)


def _tier_watch_moves_sync():
    """Bands whose pick should re-run NOW, for either reason:
      (a) the SELECTED model's live catalog price no longer matches its stored
          price_per_m (or its slug vanished from the catalog entirely), or
      (b) a CHEAPER in-floor qualifier is live -- a value window opened (a
          model got cheaper than usual for its tier), so the pick should move
          to it within the watch interval instead of waiting for the next
          full sweep. This is the half the old selected-only watch missed: a
          non-selected model dipping cheap was invisible until the 2h sweep,
          and a window shorter than that was missed entirely.
    Uses the exact price metric _bucket_models_by_price stores --
    (prompt + completion) * 1e6 -- and the SAME candidate pools + quality
    floor the pick uses, so a "cheaper qualifier" here is exactly one the
    next pick would choose. Free: one catalog read + local score math, no
    verify probes (the re-pick it triggers does the probing). Returns a set
    of band names; empty = nothing moved."""
    models = _fetch_openrouter_catalog_sync()
    catalog = {}
    for m in models:
        if ':batch' in m['id']:
            continue
        try:
            p, c = float(m['pricing']['prompt']), float(m['pricing']['completion'])
        except (KeyError, TypeError, ValueError):
            continue
        catalog[m['id']] = (p + c) * 1_000_000
    selected = get_cached_model_tiers()
    benchmark_rows = get_model_benchmark_scores()
    pools = _band_candidate_pools(models)
    moves = set()
    for band, meta in selected.items():
        if _provider_spec_for_band(band):
            continue  # operator-pinned custom endpoint: never a value window
        slug, price = meta['slug'], meta['price']
        live = catalog.get(slug)
        if live is None or abs(live - (price or 0.0)) > 1e-9:
            moves.add(band)  # selected model repriced or delisted
            continue
        for benchmark in BAND_BENCHMARK.get(band, ()):
            scores = {r['model_id']: r['score'] for r in benchmark_rows if r['benchmark'] == benchmark}
            qualifiers = _best_value_qualifiers_sync(
                pools.get(band, []), scores, BAND_QUALITY_FLOOR_GAP.get(band))
            if not qualifiers:
                continue
            # Mirror the pick's own order (BP-63 speed tie-break inside the
            # cheapest qualifier's price parity band): the watch fires only
            # on a candidate a fresh pick would try FIRST. Without the
            # mirror, a speed pick (slightly pricier, clearly faster) would
            # look like a perpetually available "cheaper qualifier" and
            # re-trigger a probe + re-pick on every watch cycle. A within-
            # band speed pick is a policy choice, not an open value window.
            # The band passes through untouched, so for any band outside
            # TIER_SPEED_BANDS both sides see the plain price order.
            top = _speed_tie_order(qualifiers, band)[0]
            if top['id'] != slug and top['price'] < price - 1e-9:
                moves.add(band)  # a better-in-policy qualifier appeared -- take the window
            break
    return moves


async def _tier_price_watch_loop():
    """Every TIER_PRICE_WATCH_INTERVAL_S (30 min), re-read the catalog and
    decide whether any band's pick should change: a selected model repricing/
    delisting, OR a cheaper in-floor qualifier appearing. In either case run
    the full re-pick so the value is taken now rather than at the next
    every-other-hour sweep. A cycle where every selected price held and no
    cheaper qualifier appeared is a free catalog read and a silent no-op --
    no probes, no log entry."""
    while True:
        await asyncio.sleep(TIER_PRICE_WATCH_INTERVAL_S)
        try:
            moved = await asyncio.to_thread(_tier_watch_moves_sync)
            if moved:
                print(f'[model-tiers] price watch: {sorted(moved)} moved to a better value; re-running pick', flush=True)
                await _tier_refresh_logged(f'price-watch: {sorted(moved)} moved')
        except Exception as e:
            # Same fail-open rule as the full loop: keep current tiers, retry
            # on the next cycle -- a watch failure never drops a working pick.
            print(f'[model-tiers] price watch failed (keeping current tiers): {e}', flush=True)


# --- Third re-pick trigger: a selected model becoming UNAVAILABLE ----------
# The catalog price watch catches a slug vanishing from /api/v1/models, and
# the every-other-hour sweep catches scores/new models -- but a model can be
# LISTED with valid pricing and zero serving endpoints (openai/gpt-5.2-chat
# 404'd with "No endpoints found" despite a normal catalog entry; see the
# note at _verify_model_works_sync). Real agent calls are the authoritative
# signal for that case: when a chat-completions call fails with an
# availability-class error, the CURRENT band for that slug is queued here
# and the drain loop runs a re-pick within a minute. Queue is keyed by band
# (concurrent failures of one model collapse to one entry) with a per-band
# cooldown from the last drain so a chronically flaky model cannot stampede
# the refresh into a loop.
_TIER_RECHECK_QUEUE = {}      # band -> ts queued (pending drain)
_TIER_RECHECK_DRAINED: dict = {}    # band -> ts of last drain (cooldown source)
TIER_RECHECK_COOLDOWN_S = 15 * 60
TIER_RECHECK_DRAIN_INTERVAL_S = 60

_AVAILABILITY_ERROR_MARKERS = (
    'no endpoints found', 'model_not_found', 'model not found', 'model does not exist',
    # DeepInfra returns this 400 when the model emits malformed tool-call
    # arguments. It is a provider/model failure, not a caller bug, so the
    # band must fall over to its next ranked model instead of surfacing an
    # empty completion that looks like the agent refused to work.
    'arguments must be valid json',
)


def _queue_tier_recheck_if_availability(model_slug, code, detail):
    """Queue a tier re-pick when a chat-completions call failed because the
    MODEL is unavailable (endpoints gone / delisted while still in catalog),
    as opposed to a transient error (timeout, 429, 5xx) which the normal
    retry/circuit-breaker paths already own. Only a slug that is a CURRENT
    tier pick maps to a band; anything else (a spike's one-off model) is not
    a tier problem and is ignored. Safe to call from any call thread."""
    if not model_slug:
        return
    detail = (detail or '').lower()
    if not any(marker in detail for marker in _AVAILABILITY_ERROR_MARKERS):
        return
    try:
        with _db() as conn:
            bands = [r[0] for r in conn.execute(
                'SELECT band FROM model_tiers WHERE slug = ?', (model_slug,)).fetchall()]
    except Exception:
        return  # never let queueing break an in-flight error path
    now = time.time()
    for band in bands:
        if now - _TIER_RECHECK_DRAINED.get(band, 0.0) < TIER_RECHECK_COOLDOWN_S:
            continue  # this band was already re-picked recently; let it settle
        if band not in _TIER_RECHECK_QUEUE:
            _TIER_RECHECK_QUEUE[band] = now
            print(f'[model-tiers] queued re-pick: {band} band model {model_slug} failed with an availability error', flush=True)


async def _tier_failure_recheck_loop():
    """Drains _TIER_RECHECK_QUEUE every TIER_RECHECK_DRAIN_INTERVAL_S (1 min):
    if a selected model was reported unavailable by a real call, run the
    shared re-pick path so the band lands on a live, verified alternative
    within a minute instead of at the next scheduled sweep. One refresh
    covers every queued band at once; queued bands drain together, and the
    per-band cooldown (set at drain time) suppresses repeat refreshes while a
    flaky model keeps failing."""
    while True:
        await asyncio.sleep(TIER_RECHECK_DRAIN_INTERVAL_S)
        try:
            if _TIER_RECHECK_QUEUE:
                queued = sorted(_TIER_RECHECK_QUEUE)
                _TIER_RECHECK_QUEUE.clear()
                for band in queued:
                    _TIER_RECHECK_DRAINED[band] = time.time()
                print(f'[model-tiers] availability re-pick for queued bands: {queued}', flush=True)
                await _tier_refresh_logged(f'model-unavailable: queued bands {queued}')
        except Exception as e:
            # Fail-open like the other loops -- keep current tiers, the queue
            # admission cooldown still bounds how often this can retry.
            print(f'[model-tiers] availability re-pick failed (keeping current tiers): {e}', flush=True)


async def _auto_failover_jev_if_gone(fresh, actor):
    """Jev's decision model is normally a deliberate, player-set choice that is
    never auto-updated (see _decision_model_chain). The exception: the periodic
    tier refresh should auto-determine it when the CURRENT choice disappears
    from availability -- if the primary slug no longer verifies AND no later
    OpenRouter chain entry verifies either (the Colab/Laya standby is excluded:
    it is a loopback that only serves when the CLI runs, not a real test here),
    fall back to the freshly-picked 'high' (planning) model, which
    refresh_model_tiers already live-verified. Logged through the same
    governance path so it shows up in the activity feed, not a silent swap."""
    current = _jev_model()
    if not current or '://' in current:
        return  # nothing to re-derive, or already pointing at the loopback standby
    if await asyncio.to_thread(_verify_decision_model_works_sync, current):
        return  # still available; leave the player's deliberate choice alone
    for slug in _decision_model_chain():
        if slug == current or '://' in slug:
            continue
        if await asyncio.to_thread(_verify_decision_model_works_sync, slug):
            return  # an existing chain fallback still works; failover already covers it
    replacement = fresh.get('high')
    if not replacement or replacement['id'] == current:
        return
    _set_setting('jev_model', replacement['id'])
    if actor:
        log_action(actor, 'jev_model_auto_failover',
                   {'from': current, 'to': replacement['id'],
                    'note': 'scheduled refresh: current decision model no longer available; fell back to the verified planning-band pick'},
                   authorized=False)
    print(f'[model-tiers] Jev decision model {current} unavailable; auto-fell-back to {replacement["id"]}', flush=True)


async def _idle_shutdown_loop(poll_s=30):
    # The mechanical enforcement of the "pause the think tank when idle" rule.
    # Watches for a live-but-unwatched server (no HTTP request for
    # _MAX_IDLE_MINUTES) and puts it DORMANT rather than exiting -- the process
    # stays alive and keeps the port bound so a remote wake request can reach
    # it, but the simulation loop stops spending and stops churning. Any
    # request flips it awake instantly (see no_store middleware). Only armed
    # when --max-idle-minutes is passed, so behavior never silently changes.
    global _LAST_REQUEST_TIME
    while True:
        await asyncio.sleep(poll_s)
        last = _LAST_REQUEST_TIME
        if last is None:
            continue
        idle_s = time.time() - last
        if idle_s >= _MAX_IDLE_MINUTES * 60 and not _dormant():
            # Transition awake -> dormant exactly once per idle streak (gate
            # on not _dormant so an already-dormant server doesn't re-log every
            # poll). The process stays up and port-bound; only the sim pauses.
            _set_dormant(True)
            print(f'[idle] no request for {int(idle_s)}s (>= {_MAX_IDLE_MINUTES}m) -- think tank dormant; waking on next request', flush=True)


async def _dormancy_window_loop(poll_s=30):
    # The ALWAYS-RUNNING owner of two self-edit contracts:
    #   1. Firing approved one-shot dormancy windows (the change-request
    #      process: conflict scan -> supervisor JEV judgment -> player link).
    #      Where the idle watcher only sleeps the tank AFTER inactivity, a
    #      window sleeps it AT an approved wall-clock slot.
    #   2. apply_pass for armed self-edits, DORMANT ONLY (selfedit docstring
    #      contract). This loop must not depend on --max-idle-minutes: the
    #      idle loop is only armed when that flag is passed, but an armed
    #      edit must always land at its dormancy path.
    # A request arriving mid-window flips the tank awake (no_store
    # middleware); apply_pass re-checks _dormant() so nothing applies awake,
    # and the next poll re-forces dormancy while the slot is still open.
    while True:
        await asyncio.sleep(poll_s)
        try:
            fired = await asyncio.to_thread(_selfedit.window_tick)
            if fired:
                print(f'[selfedit] dormancy window(s) fired: {", ".join(fired)}', flush=True)
        except Exception as exc:
            print(f'[selfedit] window_tick failed: {exc}', flush=True)
            continue
        if _dormant():
            try:
                applied = await asyncio.to_thread(_selfedit.apply_pass)
                if applied:
                    print(f'[selfedit] applied {applied} armed edit(s) while dormant', flush=True)
            except Exception as exc:
                print(f'[selfedit] apply_pass failed: {exc}', flush=True)
            # The sim tick is paused, so the normal mailbox -> task path cannot
            # reach the named validator. Run ONE narrow decision turn for each
            # window holding for validation; the turn calls validate_window()
            # with the window's own validator identity.
            try:
                pending = await asyncio.to_thread(
                    _selfedit.pending_validator_windows)
            except Exception as exc:
                pending = []
                print(f'[selfedit] validator queue read failed: {exc}', flush=True)
            for window_id in pending:
                try:
                    ok, payload = await asyncio.to_thread(
                        _selfedit.run_validator_review, window_id)
                    if ok:
                        print(f'[selfedit] validator decided {window_id}: '
                              f'{payload.get("status")}', flush=True)
                except Exception as exc:
                    print(f'[selfedit] validator turn failed for '
                          f'{window_id}: {exc}', flush=True)


def get_state_from_db():
    with _db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        if not row:
            return None
        data = json.loads(row[0])
    _merge_archived_tasks(data)
    return data


# Completed tasks are historical: once a task is done it is never worked
# again (the orphan/stale reclaim loops only ever touch 'walking'/'working'
# tasks, so a done task is never deleted). They are kept OUT of the whole-blob
# kv_state and merged back in on read, so state['tasks'] keeps its exact shape
# for every existing reader while the per-tick serialize shrinks from MBs to
# the handful of live tasks.
TASK_ARCHIVE_STATUSES = frozenset(
    {'done', 'cancelled', 'failed', 'quarantined', 'abandoned'})
_TASK_ARCHIVE_LOCK = threading.Lock()
_TASK_ARCHIVE_CACHE: dict[str, object] = {'db': None, 'loaded': False, 'tasks': {}}


def _is_archived_task(task):
    if not task or task.get('status') not in TASK_ARCHIVE_STATUSES:
        return False
    # A failed task mid-recovery must stay in the blob: the completion
    # watchdog flips it back to 'repairing' and the retry child's completion
    # looks the parent up in the live blob -- an archived recovering parent
    # would orphan its own repair chain. Same for anything the operator has
    # flagged for manual attention. (DEV-31 probe finding F-C, 2026-10-28:
    # failed/quarantined cards were the only terminal statuses never swept,
    # so 267 dead tasks accumulated in the live blob.)
    if task.get('_peerRepairPending') or task.get('manualAttention'):
        return False
    return True


def _task_archive_load_locked():
    # Load the archive into the in-process cache once per DB. Keyed on DB_PATH
    # so a test that repoints DB_PATH never reads another database's cache. A
    # DB that predates the table (or a test that built its own schema) is
    # treated as an empty archive, never an error.
    cache = _TASK_ARCHIVE_CACHE
    if cache['loaded'] and cache['db'] == DB_PATH:
        return cache['tasks']
    try:
        with _db() as conn:
            rows = conn.execute('SELECT id, blob FROM task_archive').fetchall()
        cache['tasks'] = {tid: json.loads(blob) for tid, blob in rows}
    except sqlite3.OperationalError:
        cache['tasks'] = {}
    cache['db'] = DB_PATH
    cache['loaded'] = True
    return cache['tasks']


def _archived_tasks():
    # Return a snapshot, not the live cache. The tick can rebuild the cache
    # while a request is merging archived tasks; iterating the live dict then
    # raises "dictionary changed size during iteration" and 500s /api/execute.
    # The values still get deep-copied by their callers before mutation.
    with _TASK_ARCHIVE_LOCK:
        return dict(_task_archive_load_locked())


def _merge_archived_tasks(data):
    # state['tasks'] = archived done tasks, with the live blob winning on any
    # id overlap (an un-done task's active copy must override its stale
    # archived copy).
    archived = _archived_tasks()
    if not archived:
        return
    # Copy the archived task dicts before merging. The tick mutates the state
    # in place; if it mutated the cache's own dicts, _reconcile_task_archive
    # would see cached == task and skip the DB write, silently losing the
    # change on the next restart.
    merged = {tid: copy.deepcopy(task) for tid, task in archived.items()}
    merged.update(data.get('tasks') or {})
    data['tasks'] = merged


def _reconcile_task_archive(done):
    # Make the archive table hold exactly the currently-done tasks: upsert any
    # that are new or changed. No deletes -- done tasks are never removed, and
    # a task that leaves the done set is carried by the blob (which wins on
    # merge) until it is done again. Returns True when the archive is usable
    # (so the caller may safely omit done tasks from the blob); False when the
    # table is missing, so the caller must keep them in the blob.
    with _TASK_ARCHIVE_LOCK:
        cached = _task_archive_load_locked()
        changed = {tid: t for tid, t in done.items() if cached.get(tid) != t}
        if not changed:
            return True
        now = time.time()
        try:
            with _db() as conn:
                for tid, t in changed.items():
                    conn.execute(
                        'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?) '
                        'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
                        (tid, json.dumps(t), now))
        except sqlite3.OperationalError:
            return False
        cached.update(changed)
        return True


def save_state_to_db(data):
    # Pass 1 completion recovery: a stale whole-state save must not erase a
    # newer player recovery decision. Only tasks with newer intervention
    # history are protected; ordinary saves pass through unchanged.
    import server.state_updates as state_updates
    with _db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        if row:
            current = json.loads(row[0])
            # Archived done tasks are not in the blob. Include them so a stale
            # save cannot resurrect a completed task as live work.
            archived = _archived_tasks()
            if archived:
                current = copy.deepcopy(current)
                current['tasks'] = {**archived, **(current.get('tasks') or {})}
            data = state_updates.preserve_guidance(current, data)
    # Training capture (opt-in): link task outcomes to recorded
    # interactions whenever the sim state is committed.
    training_capture.sync_outcomes(data)
    try:
        # Split completed tasks out of the blob: the archive table holds every
        # done task (upserted only when new/changed), and the blob holds only
        # the live tasks. state['tasks'] itself is left untouched -- this is a
        # view for the write, not a mutation of the caller's state.
        tasks = data.get('tasks') or {}
        done = {tid: t for tid, t in tasks.items() if _is_archived_task(t)}
        # A surviving task may still point at a terminal one -- a repair child
        # mid-flight references its failed parent. Never archive a task a
        # surviving task references, or the child's completion lookup loses
        # its parent and the chain dead-ends. The blob filter below must use
        # this SAME final set, or a guarded task would be dropped from the
        # blob without ever having been archived (silent data loss).
        live = {tid: t for tid, t in tasks.items() if tid not in done}
        referenced = set()
        for t in live.values():
            for field in ('reviewOf', 'rootStoryId', 'investigationOf',
                          'dependsOn', 'adversarialTaskId'):
                value = t.get(field)
                if isinstance(value, str) and value:
                    referenced.add(value)
                elif isinstance(value, (list, tuple)):
                    referenced.update(v for v in value if isinstance(v, str))
        if referenced:
            done = {tid: t for tid, t in done.items() if tid not in referenced}
        archived_ok = _reconcile_task_archive(done) if done else True
        if done and archived_ok:
            keep = (set(tasks) - set(done))
            blob = {**data, 'tasks': {tid: tasks[tid] for tid in keep}}
        else:
            # Nothing to archive, or the archive table is unavailable -- keep
            # every task in the blob so nothing is ever lost.
            blob = data
        with _db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
                (json.dumps(blob), time.time()),
            )
            # Integrity pair (015): the checksum row must commit atomically
            # with the blob it covers.
            _state_checksum_write(conn, json.dumps(blob))
        try:
            sync_agent_directories(data, throttle=True)
        except Exception as e:
            print(f'[agent-dirs] sync failed: {e}')  # best-effort -- a filesystem hiccup shouldn't break autosave
    finally:
        # If this thread is inside a state transaction (see _state_begin /
        # _state_writer), the save is the commit point: release the blob lock so
        # the next writer can proceed. Non-transaction saves (boot, backfills)
        # never hold it, so this is a no-op for them.
        if getattr(_state_txn, 'held', False):
            _state_txn.held = False
            _state_txn.saved = True
            _STATE_LOCK.release()


# ---------------------------------------------------------------------------
# Whole-blob read-modify-write serialization.
#
# The entire kv_state blob is read, mutated, and written back as one unit by
# MANY writers: the continuous sim tick (sim.py `_sim_loop_pass`), the email
# drain thread, and every state-mutating HTTP handler. Two of those doing an
# unlocked get->mutate->save concurrently means the LAST save silently
# clobbers the other's changes (client data loss). SQLite serializes the final
# write, but not the read-modify-write that precedes it, so that alone doesn't
# help. A single reentrant lock held across the whole get->mutate->save span
# serializes them.
#
# Writers opt in via `_state_begin()` (acquire + read) instead of
# `get_state_from_db()`, and `save_state_to_db` is the commit point (release).
# The `@_state_writer` decorator guarantees the lock is released even on an
# early return or an exception (its finally calls _state_abort), so a leak --
# which would freeze the sim tick forever -- is impossible.
# ---------------------------------------------------------------------------
_STATE_LOCK = threading.RLock()
_state_txn = threading.local()


def _state_begin():
    """Acquire the state lock and return the latest blob. The caller (or its
    @_state_writer wrapper) is guaranteed to release via save or _state_abort.
    Reads through get_state_from_db() so tests that mock that reader to inject
    a state keep working."""
    _STATE_LOCK.acquire()
    _state_txn.held = True
    _state_txn.saved = False
    try:
        return get_state_from_db()
    except Exception:
        _state_txn.held = False
        _STATE_LOCK.release()
        raise


def _state_abort():
    """Release the state lock WITHOUT committing, if this thread still holds it.
    Idempotent; safe to call on every exit path (early return, exception)."""
    if getattr(_state_txn, 'held', False):
        _state_txn.held = False
        _STATE_LOCK.release()


def _state_writer(fn):
    """Decorate an async state-writing handler so the lock taken by
    _state_begin is ALWAYS released -- on a normal return after save, on an
    early return, and on an exception -- no matter which path runs."""
    import functools
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        finally:
            _state_abort()
    return wrapper


# ---------------------------------------------------------------------------
# The Bank: a spend ledger for the model/API budget. Every model call in the
# think tank flows through /api/chat (or /api/decide for Jev); OpenRouter returns
# each request's USD cost in usage.cost. Those costs are accrued here, grouped
# by service, so a director at a bank teller can see used / cap / left / a
# forecasted exhaustion date for every service (and cumulatively across them)
# and coordinate with other directors not to exceed the budget. Caps are the
# per-service fixed-$ figures; the ledger is just state accrued over time.
# Budget is deliberately recorded, not enforced: enforcement is the directors'
# job, not the ledger's.
#
# Implemented in bank.py (extracted 2026-10-05) and re-exported at the top of
# this file; the constants BELOW are the env-derived caps this file still owns,
# while the ledger/budget/forecast functions live in world/bank.py and read
# them through `serve.` at call time.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Server-owned seed (no static agent names in any JS
# file -- the roster lives in the database). The seed that used to live in
# agents.js's AGENT_ROSTER now lives HERE, on the server, and is pushed into
# villa.db on a genuinely empty boot, so a fresh database comes up with real
# agents and the browser never generates or hardcodes a name. The client's
# only job after this is to hydrate from /api/state -- names, roles, colors,
# director pointers are all taken from the DB, never from a JS literal.
# Idempotent: only seeds when kv_state is completely empty, and stamps the
# director tier (Theo admin, Nora senior director, walk-the-chain pointers)
# immediately so the DB is authoritative before any client reads it.
# ---------------------------------------------------------------------------
def _default_roster_definitions():
    # id/name/color/role/model mirror the pre-seed roster. Deliberately NO
    # isAdmin/isDirector/director fields here -- the server stamps those from
    # ADMIN_IDS / _SENIOR_DIRECTOR_ID / backfill below, so this list stays
    # purely descriptive and the authority graph moves with the DB constants,
    # not with this literal.
    #
    # NO agent names are hardcoded: the seed roster comes from the
    # per-install SEED_ROSTER env key, so a clone of this repo carries zero
    # agent identities -- they only ever exist in that instance's .env and,
    # once seeded, in think_tank.db. Format per entry (comma-separated):
    #   id|name|color|role|model
    # e.g.  ada|Ada|#e06666|Research|small
    raw = _load_env().get('SEED_ROSTER', '').strip()
    roster = []
    for entry in (raw.split(',') if raw else []):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split('|')]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue  # every entry needs at least id + name
        aid, name = parts[0], parts[1]
        roster.append({
            'id': aid,
            'name': name,
            'color': parts[2] if len(parts) > 2 and parts[2] else _stable_fallback_color(aid),
            'role': parts[3] if len(parts) > 3 and parts[3] else 'Worker',
            'model': parts[4] if len(parts) > 4 and parts[4] else 'small',
        })
    return roster


# Per-role founder profiles. The founders are the think tank's most
# distinctive members, not a stamped-out identical template -- each gets a
# role-specific mission + operating instructions (the same shape real hired
# agents get, from seed rather than a later LLM call). Keyed by role so a
# definition stays readable and additive. Fallback stays the generic line
# for any role without a bespoke entry, so adding a roster role can never
# crash the seed.
_SEED_PROFILE_DEFAULTS = {
    'Research': {
        'mission': 'Investigate assigned topics from primary sources and file grounded research briefs the think tank can act on.',
        'instructions': [
            'Cite real sources in every research brief; flag thin or uncertain findings rather than presenting them as settled.',
            'Route requests for elevated access or gated sources through the senior-most director.',
            'Check in with the chief of staff if a topic needs a decision before it can be researched further.',
        ],
        'notes': [],
    },
    'Banking': {
        'mission': 'Manage the think tank treasury and its ledger with accuracy above all else.',
        'instructions': [
            'Never move funds without a clear, recorded reason; reconcile the ledger before closing out a period.',
            'Flag any discrepancy immediately rather than burying it in a later report.',
            'Route purchase or grant approvals through the senior-most director.',
        ],
        'notes': [],
    },
    'Post Office': {
        'mission': 'Handle the think tank mail and package flows reliably so nothing gets lost in transit.',
        'instructions': [
            'Delivery records must match what actually shipped; note exceptions rather than smoothing them over.',
            'Prioritize call-outs from other agents about misroutes or late packages.',
            'Keep the shared queue moving -- hand off to a peer when a backlog forms.',
        ],
        'notes': [],
    },
    'Studio': {
        'mission': 'Design and build working artifacts and projects the think tank actually uses.',
        'instructions': [
            'A finished piece you cannot demonstrate working is not finished; verify before declaring done.',
            'Prefer clear, maintainable work over clever one-offs.',
            'Give peers a genuine review when asked, looking for real problems, not rubber-stamps.',
        ],
        'notes': [],
    },
    'Weather Station': {
        'mission': 'Research open-ended questions and topics on request, using the internet access this room provides to ground findings in real sources. The room\'s name is cosmetic and does not limit what may be researched.',
        'instructions': [
            'Ground every finding in real, verifiable sources; never present a guess as a verified fact.',
            'The room\'s name does not restrict the topic -- research whatever the task or player asks for.',
            'Distinguish observed/verified data from inference in every report.',
            'Flag uncertainty honestly rather than padding a conclusion.',
        ],
        'notes': [],
    },
    'Control Room': {
        'mission': ('Run the control room as the owner\'s chief of staff: '
                    'coordinate the directors, keep commitments and schedules '
                    'on track, see that CRON and standing work actually run, '
                    'and stand in for the owner on routine approvals.'),
        'instructions': [
            'Approve what is clearly legitimate and in scope; defer anything uncertain to the owner.',
            'Delegation down the chain should match the walk-the-chain authority, never leap past it.',
            'Check the operating rhythm: confirm scheduled and CRON work ran, and surface missed or noisy automation.',
            'Do not write or self-edit code; route code work through the director chain to a worker.',
            'Monitor the whole tank on a routine cadence; when something is unusual, instruct the owning director to card it immediately.',
            'A consequential call deserves an audit trail -- record it, don\'t just make it.',
        ],
        'notes': [],
    },
    'Personnel': {
        'mission': 'Oversee hiring, morale, and personnel matters fairly across the think tank.',
        'instructions': [
            'Judge people on real evidence of work, never on reputation or hearsay alone.',
            'A firing or a hire must follow the consultation and review steps before being acted on.',
            'Surface the most consequential personnel decisions to the chief of staff for a final call.',
        ],
        'notes': [],
    },
    'Supervisor': {
        'mission': ('Facilitate one team through its sprint: watch its budget and its '
                    'process failures, keep work moving, and leave the actual work to the workers.'),
        'instructions': [
            "Scope is your own team. Never inspect, comment on, or reassign another team's work; route cross-team issues to the chief of staff.",
            'Use the shell: check task status, the team sandbox paths, and the team-scoped DB rows before drawing a conclusion.',
            'Do not do the work yourself. You are the facilitator, not the implementer.',
            'Budget: if a card spent its ceiling before close, say what the spend bought and whether more is justified; never silently retry.',
            'Process: a failure, a stuck review, or a missing deliverable is yours to escalate or file as a failure review.',
            'Read the supervisor brief first; it names the team, the sprint, the cards, the failures, and where to look.',
        ],
        'notes': [],
    },
    'Supporting Director': {
        'mission': ('Support the other directors as a senior peer: share context, cover '
                    'gaps, and keep the director group coherent without owning their teams.'),
        'instructions': [
            'Support the directors; do not reassign or override their teams unilaterally.',
            'Route org-level and cross-team decisions to the chief of staff.',
            'Keep your own team supervised to the same standard as every other director.',
        ],
        'notes': [],
    },
    'Chief of Staff': {
        'mission': ('Coordinate the directors and hold org-level authority: approvals, '
                    'cross-team decisions, schedules, and the operating rhythm.'),
        'instructions': [
            'Directors own their teams; you own the coordination between them.',
            'Route code work through the director chain to a worker; do not self-edit code.',
            'Monitor the whole tank on a routine cadence; when something is unusual, instruct the owning director to card it immediately.',
            'For a concrete record fix, you may read or modify non-secret database records; keep every write auditable.',
            'Keep decisions auditable: record who approved what and why.',
        ],
        'notes': [],
    },
}
