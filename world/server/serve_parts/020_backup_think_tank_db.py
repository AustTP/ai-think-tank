

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
        if os.environ.get('THINK_TANK_DB_URL'):
            # Postgres backend (2026-11-01): the .db file is no longer the
            # live store; pg_dump produces the consistent snapshot instead.
            # Custom format (-Fc) keeps the file compact and restorable with
            # pg_restore; the .bak name is kept so the prune logic below is
            # unchanged.
            import subprocess
            dsn = os.environ['THINK_TANK_DB_URL']
            proc = subprocess.run(
                ['pg_dump', '--dbname=' + dsn, '-Fc', '-f', dest],
                capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(
                    f'pg_dump failed: {(proc.stderr or "")[:200]}')
        else:
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


# Disk hygiene (2026-11-05 emergency): task stage sandboxes and agent
# prototypes mirrors grew without bound -- every sandbox carries a git repo
# that only ever accumulated (2.2 GB of loose objects per sandbox; the
# prototypes mirror even copied the whole .git into agents/<id>/). The disk
# hit 403 MiB free and deliverable writes started failing with ENOSPC. The
# sweeper below runs on the log-prune cadence and enforces three rules:
#   1. A stage sandbox whose task is terminal or no longer known (pruned from
#      the archive) is scratch space: delete it after a grace window.
#   2. A live sandbox's git gets gc'd -- history survives, loose objects go.
#   3. Agent prototypes mirrors must never contain .git (sync_prototypes
#      now ignores it; this also strips any legacy copy).
_SANDBOX_GRACE_S = 6 * 3600
_SANDBOX_GC_PER_RUN = 6
_SANDBOX_MIN_FREE_GIB = 20.0
# Hard count ceiling for stage sandboxes. Liveness and grace windows bound
# STALE work; this bounds the LIVE tail too, so the directory can never grow
# unbounded again no matter how the tank is driven (2026-11-07: 3.8k entries,
# 61 GiB, was the second ENOSPC scare). Anything above the cap is reaped
# oldest-first the same way the grace window reaps, so a burst of fresh
# provisioning evicts the oldest idle workspace instead of filling the disk.
_SANDBOX_MAX_DIRS = 32


def _sandbox_task_status(workspace):
    """Terminal/absent status for a stage workspace's task id, or None when
    the task is live. Absent-from-everything counts as terminal: the task
    predates the archive's own retention."""
    if not workspace:
        return 'absent'
    try:
        state = get_state_from_db(archive_merge='none') or {}
        if workspace in (state.get('tasks') or {}):
            return None
    except Exception:
        return None  # can't judge -> never delete
    try:
        with _db() as conn:
            row = conn.execute('SELECT 1 FROM task_archive WHERE id = ?',
                               (workspace,)).fetchone()
        return 'terminal' if row else 'absent'
    except Exception:
        return None


def _prune_sandboxes():
    import shutil as _shutil
    sandboxes_dir = os.path.join(THINK_TANK_DIR, 'sandboxes')
    try:
        names = os.listdir(sandboxes_dir)
    except OSError:
        return
    now = time.time()
    reaped, gced = 0, 0
    stages = []
    story_paths = []
    previous_paths = []
    for name in names:
        path = os.path.join(sandboxes_dir, name)
        if not os.path.isdir(path):
            continue
        if name in ('workroom-shared',) or name.startswith(('hf-', 'direct-',
                                                            'headful', 'fb-',
                                                            'project-',
                                                            'vpn-')):
            continue
        if name.startswith('stage-'):
            stages.append((name, path))
            continue
        # Story/spike workspaces carry the same per-task git-repo cost as
        # stage dirs (2026-11-08: 1,063 story/spike dirs, ~13 GB) and the
        # original sweep never saw them, so every task's workspace outlived
        # its task forever. Same rule: a workspace whose task is terminal or
        # unknown is scratch once the grace window passes.
        task_id = None
        for prefix in ('story-', 'spike-'):
            if name.startswith(prefix):
                task_id = name[len(prefix):]
                break
        if task_id:
            story_paths.append((name, path, task_id))
            continue
        if name.startswith('previous-'):
            # Restart-debris snapshots of the shared workspace: superseded
            # the moment the server restarts, so plain mtime age reaps them.
            previous_paths.append(path)
            continue
    for name, path in stages:
        if name not in _SANDBOX_LIVE_IDS:
            # Not referenced by anything right now. Reap after the grace
            # window (mtime is the last real activity on the workspace).
            try:
                if now - os.path.getmtime(path) > _SANDBOX_GRACE_S:
                    _shutil.rmtree(path, ignore_errors=True)
                    reaped += 1
                    continue
            except OSError:
                pass
        # Live sandbox: keep the git history but collapse the loose objects.
        if gced < _SANDBOX_GC_PER_RUN:
            git_dir = os.path.join(path, '.git')
            if os.path.isdir(git_dir):
                try:
                    subprocess.run(
                        ['git', 'gc', '--prune=now', '--quiet'], cwd=path,
                        capture_output=True, timeout=600)
                    gced += 1
                except Exception:
                    pass
    # Count ceiling after the grace pass: even live-vouched workspaces must
    # fit under the cap. Oldest-first, so a burst of new provisioning evicts
    # the longest-idle survivor rather than the newest work.
    if len(stages) > _SANDBOX_MAX_DIRS:
        overflow = sorted(stages, key=lambda item: os.path.getmtime(item[1]))
        for name, path in overflow[:len(stages) - _SANDBOX_MAX_DIRS]:
            _shutil.rmtree(path, ignore_errors=True)
            reaped += 1
    # Story/spike workspaces: reap once their task is terminal/unknown and
    # the grace window has passed since the last real activity.
    for name, path, task_id in story_paths:
        try:
            if now - os.path.getmtime(path) <= _SANDBOX_GRACE_S:
                continue
            if _sandbox_task_status(task_id) is None:
                continue  # task still live (or unreadable): never delete
        except OSError:
            continue
        _shutil.rmtree(path, ignore_errors=True)
        reaped += 1
    # Superseded restart snapshots older than two days.
    for path in previous_paths:
        try:
            if now - os.path.getmtime(path) <= 48 * 3600:
                continue
        except OSError:
            continue
        _shutil.rmtree(path, ignore_errors=True)
        reaped += 1
    # Stray legacy .git mirrors inside agent prototypes.
    agents_dir = os.path.join(THINK_TANK_DIR, 'agents')
    try:
        roster = {r[0] for r in _db().__enter__().execute(
            'SELECT id FROM roster_agent').fetchall()}
    except Exception:
        roster = None
    try:
        for name in os.listdir(agents_dir):
            git_dir = os.path.join(agents_dir, name, 'prototypes', '.git')
            if os.path.isdir(git_dir) and (roster is None
                                           or name not in roster):
                _shutil.rmtree(git_dir, ignore_errors=True)
    except OSError:
        pass
    if reaped or gced:
        print(f'[disk-hygiene] sandboxes reaped={reaped} gc={gced}',
              flush=True)


def _refresh_sandbox_liveness():
    """The set of stage sandbox ids the live state or runtime pointers can
    vouch for. Everything else ages out through the grace window."""
    live = set()
    try:
        state = get_state_from_db(archive_merge='none') or {}
        sim_agents = (state.get('sim') or {}).get('agents') or {}
        for rec in sim_agents.values():
            if isinstance(rec, dict):
                for k in ('sandboxId', 'sandbox', 'sandbox_id'):
                    v = rec.get(k)
                    if isinstance(v, str) and v.startswith('stage-'):
                        live.add(v)
        agents = state.get('agents') or {}
        for rec in agents.values():
            if isinstance(rec, dict):
                for k in ('sandboxId', 'sandbox', 'sandbox_id'):
                    v = rec.get(k)
                    if isinstance(v, str) and v.startswith('stage-'):
                        live.add(v)
    except Exception:
        pass
    sandboxes_dir = os.path.join(THINK_TANK_DIR, 'sandboxes')
    try:
        names = os.listdir(sandboxes_dir)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith('stage-'):
            continue
        base = os.path.join(sandboxes_dir, name)
        for suffix in ('.runtime.json', '.repair.json'):
            fp = base + suffix
            if not os.path.exists(fp):
                continue
            if not os.path.isdir(base):
                # Orphaned marker: the workspace is already gone. The marker
                # file is the only thing left, and nothing ever unlinks it
                # (staged_work writes it on provision; no completion path
                # removes it), so it would otherwise pin nothing forever.
                # Reap the litter after the same grace window.
                try:
                    if now - os.path.getmtime(fp) > _SANDBOX_GRACE_S:
                        os.remove(fp)
                except OSError:
                    pass
                continue
            try:
                activity = now - os.path.getmtime(base)
            except OSError:
                activity = 0
            # A marker only vouches for liveness while the workspace shows
            # activity inside the grace window. Markers are written once at
            # provision and never removed, so an unbounded marker (as before
            # this fix) made every provisioned stage immortal: 178 stale
            # sandboxes, 59 GiB, most of it 2.3 GB git histories. An actively
            # working agent keeps touching its workspace; a finished or
            # stalled one ages out exactly like unmarked scratch space.
            if activity <= _SANDBOX_GRACE_S:
                live.add(name)
    _SANDBOX_LIVE_IDS.clear()
    _SANDBOX_LIVE_IDS.update(live)


_SANDBOX_LIVE_IDS = set()


def _disk_free_gib():
    try:
        usage = shutil.disk_usage(THINK_TANK_DIR)
        return usage.free / (1024 ** 3)
    except Exception:
        return None


def _disk_pressure_ok():
    """Guard for expensive provisioning: refuse when free disk is under the
    floor, so the tank degrades with a clear error instead of dying with
    ENOSPC mid-write."""
    free = _disk_free_gib()
    if free is None:
        return True
    return free >= _SANDBOX_MIN_FREE_GIB


async def _disk_hygiene_loop():
    while True:
        await asyncio.sleep(LOG_PRUNE_INTERVAL_S)
        try:
            await asyncio.to_thread(_refresh_sandbox_liveness)
            await asyncio.to_thread(_prune_sandboxes)
        except Exception as e:
            print(f'[disk-hygiene] sweep failed: {e}', flush=True)


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
                pools.get(band, []), scores, BAND_QUALITY_FLOOR_GAP.get(band),
                min_score=BAND_MIN_SCORE.get(band))
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


def _idle_should_sleep(now=None):
    """One poll of the idle watcher, factored out so tests can drive the
    decision without awaiting the real loop. True exactly when the idle
    auto-sleep is armed (--max-idle-minutes > 0), a request has been seen
    (_LAST_REQUEST_TIME is not None), the idle streak has crossed the
    threshold, and the tank is not already dormant."""
    if _MAX_IDLE_MINUTES <= 0:
        return False
    last = _LAST_REQUEST_TIME
    if last is None:
        return False
    idle_s = (time.time() if now is None else now) - last
    return idle_s >= _MAX_IDLE_MINUTES * 60 and not _dormant()


async def _idle_shutdown_loop(poll_s=30):
    # The mechanical enforcement of the "pause the think tank when idle" rule.
    # Watches for a live-but-unwatched server (no HTTP request for
    # _MAX_IDLE_MINUTES) and puts it DORMANT rather than exiting -- the process
    # stays alive and keeps the port bound so a remote wake request can reach
    # it, but the simulation loop stops spending and stops churning. Any
    # request flips it awake instantly (see no_store middleware). Only armed
    # when --max-idle-minutes is passed, so behavior never silently changes.
    _diag_logged = False
    while True:
        await asyncio.sleep(poll_s)
        idle_s = (time.time() - _LAST_REQUEST_TIME) if _LAST_REQUEST_TIME is not None else 0
        if _idle_should_sleep():
            # Transition awake -> dormant exactly once per idle streak (gate
            # inside _idle_should_sleep on not _dormant so an already-dormant
            # server doesn't re-log every poll). The process stays up and
            # port-bound; only the sim pauses.
            _set_dormant(True)
            print(f'[idle] no request for {int(idle_s)}s (>= {_MAX_IDLE_MINUTES}m) -- think tank dormant; waking on next request', flush=True)
        elif not _diag_logged and idle_s >= 60:
            # One-shot diagnostic: if a full quiet minute passed WITHOUT the
            # dormancy transition, name exactly which gate refused it, so an
            # armed-but-never-sleeping server is visible instead of silent.
            _diag_logged = True
            print(f'[idle][diag] idle_s={int(idle_s)}s but no dormancy: '
                  f'max_idle_minutes={_MAX_IDLE_MINUTES} '
                  f'last_request={"set" if _LAST_REQUEST_TIME is not None else "None"} '
                  f'dormant={_dormant()}', flush=True)


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


def get_state_from_db(archive_merge='copy'):
    """Read the whole state blob and merge the task archive back in.

    archive_merge:
      'copy'  (default) -- every archived task is deep-copied before merge,
              so callers may mutate the returned state freely without
              corrupting the archive cache. Costs ~1.2s at a 1.6k-task
              archive; every state WRITER must use this.
      'alias' -- archived entries are shared with the archive cache. Only
              for callers that provably never mutate any task object
              (serialize-and-return handlers, single-key readers). Cuts the
              merge to a dict spread; a mutating caller would silently
              corrupt the archive cache and lose changes on restart.
      'none'  -- the archive is NOT merged at all: state['tasks'] holds only
              the live blob's tasks. For callers that never read tasks
              (roster/village lookups, sandbox id remaps). Observed
              2026-11-04: the execute pipeline's _village_sandbox_id paid a
              ~1.9s whole-archive deepcopy ON THE EVENT LOOP per run while
              needing only the roster -- the loop stalled behind every
              execute and HTTP health checks timed out. A 'none' reader that
              DOES touch tasks sees a truncated view: never use it where
              task ids are resolved.
    """
    # Read cache (2026-11-05 batched saves): the merged state object is kept
    # in memory and served to every default-mode reader. Each uncached read
    # cost a 5.6 MB blob transfer + parse + a ~1.2s archived-task deepcopy,
    # all serialized behind _STATE_LOCK -- the 25-30s request stalls under
    # load came from WRITERS queueing behind each other's read-modify-write
    # cycles. The cache turns a writer's read into a dict lookup.
    #
    # The cached object is SHARED and mutable by design: every writer holds
    # _STATE_LOCK while mutating, so mutations accumulate on one object and
    # the next save persists all of them. A save always refreshes the cache
    # to the saved snapshot. 'copy' merge semantics are preserved where they
    # matter: the cache only ever stores 'copy'-merged states (private
    # deepcopies of archived tasks), so callers may still mutate freely
    # without corrupting the archive cache.
    #
    # Known staleness: a writer OUTSIDE this process (operator psql) is not
    # reflected until restart. That hazard predates the cache -- every full
    # blob save already clobbered external writes.
    global _STATE_READ_CACHE
    if archive_merge == 'copy' and _STATE_READ_CACHE['obj'] is not None \
            and not _state_io_test_mode():
        return _STATE_READ_CACHE['obj']
    with _db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        if not row:
            return None
        data = json.loads(row[0])
    _merge_extracted_sections(data)
    if archive_merge != 'none':
        _merge_archived_tasks(data, mode=archive_merge)
    if archive_merge == 'copy' and not _state_io_test_mode():
        _STATE_READ_CACHE['obj'] = data
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
    # would orphan its own repair chain. (DEV-31 probe finding F-C,
    # 2026-10-28: failed/quarantined cards were the only terminal statuses
    # never swept, so 267 dead tasks accumulated in the live blob.)
    if task.get('_peerRepairPending'):
        return False
    if task.get('manualAttention'):
        # The operator's flag holds a task out of the archive while it is
        # fresh -- that is the flag's whole job. But the flag is invisible on
        # the board (no JS reads it) and dead cards carried it for days, so
        # the operator asked (2026-10-28) for aged-out flags to stop holding
        # the blob: a week is far beyond any real attention window.
        if _task_attention_age_ms(task) < MANUAL_ATTENTION_ARCHIVE_TTL_MS:
            return False
    return True


def _task_attention_age_ms(task):
    """Milliseconds since the task's own last state change. lastProgressAt is
    NOT usable here: the watchdog's observe pass re-stamps it on every pass,
    which would make dead cards look forever-fresh. No anchor at all means
    unknown age: return 0 so the flag keeps doing its job (fail-safe); an
    explicit operator clear handles those."""
    anchors = [task.get(k) for k in ('createdAt', 'closedAt', 'blockedAt') if task.get(k)]
    if not anchors:
        return 0
    return max(0, int(time.time() * 1000) - int(max(anchors)))


MANUAL_ATTENTION_ARCHIVE_TTL_MS = 7 * 24 * 60 * 60 * 1000


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
    _bump_task_archive_rev()
    return cache['tasks']


def _archived_tasks():
    # Return a snapshot, not the live cache. The tick can rebuild the cache
    # while a request is merging archived tasks; iterating the live dict then
    # raises "dictionary changed size during iteration" and 500s /api/execute.
    # The values still get deep-copied by their callers before mutation.
    with _TASK_ARCHIVE_LOCK:
        return dict(_task_archive_load_locked())


def _merge_archived_tasks(data, mode='copy'):
    # state['tasks'] = archived done tasks, with the live blob winning on any
    # id overlap (an un-done task's active copy must override its stale
    # archived copy).
    archived = _archived_tasks()
    if not archived:
        return
    if mode == 'alias':
        # Caller contract (see get_state_from_db): never mutate any task
        # object. Aliasing the archive cache makes the merge a dict spread
        # instead of a whole-archive deepcopy on every read.
        merged = dict(archived)
    else:
        # Copy the archived task dicts before merging. The tick mutates the
        # state in place; if it mutated the cache's own dicts,
        # _reconcile_task_archive would see cached == task and skip the DB
        # write, silently losing the change on the next restart.
        #
        # The copy is a JSON round-trip of a revision-keyed snapshot, not a
        # per-task copy.deepcopy: at 6.8k archived tasks the deepcopy cost
        # ~1.9s per read (observed 2026-11-04 -- 60+ call sites, the tick
        # among them, so the loop and the workers thrashed on copies all
        # evening and the village starved). A JSON round-trip of the same
        # data costs ~0.1s and yields exactly what deepcopy yielded for
        # pure-JSON payloads: fresh, freely mutable objects.
        merged = json.loads(_archived_snapshot_json())
    merged.update(data.get('tasks') or {})
    data['tasks'] = merged


# Revision of the in-process archive cache. Bumped on every mutation so the
# serialized snapshot can be rebuilt lazily instead of per read.
_TASK_ARCHIVE_REV = 0
_TASK_ARCHIVE_SNAPSHOT: dict = {'rev': None, 'json': None}


def _bump_task_archive_rev():
    global _TASK_ARCHIVE_REV
    _TASK_ARCHIVE_REV += 1


def _archived_snapshot_json():
    global _TASK_ARCHIVE_REV
    with _TASK_ARCHIVE_LOCK:
        if _TASK_ARCHIVE_SNAPSHOT['rev'] != _TASK_ARCHIVE_REV \
                or _TASK_ARCHIVE_SNAPSHOT['json'] is None:
            _TASK_ARCHIVE_SNAPSHOT['json'] = json.dumps(
                _task_archive_load_locked())
            _TASK_ARCHIVE_SNAPSHOT['rev'] = _TASK_ARCHIVE_REV
        return _TASK_ARCHIVE_SNAPSHOT['json']


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
                # INSIDE the with-block: _db() closes the connection on exit,
                # so a prune placed after it would run against a dead handle
                # and silently no-op (caught 2026-11-04 before first fire).
                _prune_task_archive(conn)
        except sqlite3.OperationalError:
            return False
        cached.update(changed)
        _bump_task_archive_rev()
        return True


# Retention (2026-11-04): the archive only exists so state['tasks'] keeps its
# shape on read; a 6.2k-row archive made every 'copy'-mode read pay a ~1.9s
# deepcopy on every tick and execute pass. Done tasks older than this window
# leave the archive table AND the in-process cache. Their history is not
# lost: action_log retains every trail row, and an old done task's content
# lives in the Library when it produced one. The prune runs on the reconcile
# writer (the only writer), at most once per pass, bounded to one DELETE.
_TASK_ARCHIVE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000


def _prune_task_archive(conn):
    cutoff = time.time() * 1000 - _TASK_ARCHIVE_RETENTION_MS
    stale = [tid for tid, task in _TASK_ARCHIVE_CACHE.get('tasks', {}).items()
             if _task_attention_age_ms(task) > cutoff]
    if not stale:
        return
    try:
        conn.executemany('DELETE FROM task_archive WHERE id = ?', [(t,) for t in stale])
    except Exception:
        return  # a failed prune retries on the next reconcile; never fatal
    for tid in stale:
        _TASK_ARCHIVE_CACHE['tasks'].pop(tid, None)
    _bump_task_archive_rev()


# --- Scale slices: high-cardinality sections leave the whole blob ---------
#
# The task_archive pattern, applied to the three sections that scale with
# headcount/history and mutate constantly: state['agents'] (per-agent runtime
# rewritten by every tick), state['agentRoster'] (metadata list, order
# significant), state['playerInbox'] (append-mostly, unbounded). Each lives in
# its own per-row table; a save upserts only rows that actually changed and
# the blob holds none of the three. get_state_from_db merges the rows back in
# so every existing reader keeps its exact shape.
#
# Authority rule (mirrors task_archive's live-wins merge, inverted): once a
# section's table holds ANY rows, the table is authoritative and any legacy
# blob copy is ignored. The first reconciling save writes the full section to
# rows and drops the blob key in the same transaction, so a reader never sees
# both carrying different data: before that save the table is empty (blob
# legacy copy stands), after it the blob has no key. A reconcile failure keeps
# the key in the blob -- a table that cannot be written must never silently
# swallow its section.
EXTRACTED_SECTIONS = (
    # (state key, table name, shape): dict sections key rows by entry id;
    # list sections order rows by seq (list index) with id as tiebreaker.
    # A 'sim:' prefix marks a section nested under state['sim'] (the key
    # after the colon); the blob keeps sim's OTHER keys, and the nested
    # strip in save_state_to_db only touches a detached blob view.
    ('agents', 'live_agent', 'dict'),
    ('agentRoster', 'roster_agent', 'list'),
    ('playerInbox', 'inbox_message', 'list'),
    # The tick's per-agent spatial/activity snapshot: rebuilt every tick,
    # ~200 bytes x headcount, and persisted on EVERY save -- at 1,000 agents
    # that alone would re-serialize the whole roster into the blob each
    # time. Row-extracted, a save writes only the agents whose snapshot
    # actually changed (walkers move; working agents sit still).
    ('sim:agents', 'sim_agent_snapshot', 'dict'),
    # Round 2 (2026-11-01): the sections that dominated what remained of
    # the blob -- growthPlans alone was 1.7MB of 4.4MB, and
    # completedDeliverables / helpSignals grow without bound. memoryLedger
    # stays in the blob: its entries carry no stable id, so the rower
    # would fail closed on every save (harmless, but no win).
    ('growthPlans', 'growth_plan', 'dict-lists'),
    # shape notes: 'dict' rows are keyed by entry id (values must be dicts);
    # 'dict-lists' rows are keyed by the dict key with the entry's LIST as
    # the row blob (growthPlans maps agent id -> [plan revisions]);
    # 'list' rows keep order via seq and require every entry to carry an id.
    ('helpSignals', 'help_signal', 'list'),
    ('issues', 'tracked_issue', 'dict'),
    ('wakeQueue', 'wake_entry', 'list'),
    ('backlogRequests', 'backlog_request', 'list'),
    ('completedDeliverables', 'completed_deliverable', 'list'),
)
_EXTRACTED_LOCK = threading.Lock()
_EXTRACTED_CACHE = {key: {'db': None, 'loaded': False, 'rows': {}}
                    for key, _table, _shape in EXTRACTED_SECTIONS}


def _extracted_rows_load_locked(table, cache):
    # Per-DB cache keyed like _TASK_ARCHIVE_CACHE so a test that repoints
    # DB_PATH never reads another database's rows. An absent table (test
    # schema predating the split) is an empty row set, never an error: the
    # blob copy keeps standing and the first save recreates the table.
    if cache['loaded'] and cache['db'] == DB_PATH:
        return cache['rows']
    rows = {}
    try:
        with _db() as conn:
            for rid, seq, blob in conn.execute(
                    f'SELECT id, seq, blob FROM {table}'):
                rows[rid] = (int(seq), json.loads(blob))
    except sqlite3.OperationalError:
        rows = {}
    cache['rows'] = rows
    cache['db'] = DB_PATH
    cache['loaded'] = True
    return rows


def _merge_extracted_sections(data):
    # Always deep-copy, never alias: unlike the task archive (whose alias
    # mode is load-bearing for reader perf), these sections are read by
    # callers that heal/mutate them in place (190's identity heal runs on
    # every read), and an aliased cache entry would swallow those mutations
    # at the next diff -- silent data loss. The sections are small; the
    # scaling win this slice exists for is on the WRITE path.
    for key, table, shape in EXTRACTED_SECTIONS:
        with _EXTRACTED_LOCK:
            rows = _extracted_rows_load_locked(table, _EXTRACTED_CACHE[key])
        if not rows:
            # An emptied (or never-populated) rowed section leaves no rows.
            # The whole-blob replace used to keep `[]`/`{}` in place for an
            # empty section; give readers the shape's default when neither
            # the rows nor a legacy blob copy carries the key, so direct
            # indexing (`state['backlogRequests']`) keeps working. A blob
            # copy always wins (legacy authority rule).
            if key.startswith('sim:'):
                if isinstance(data.get('sim'), dict) \
                        and key[4:] not in data['sim']:
                    data['sim'][key[4:]] = [] if shape == 'list' else {}
            elif key not in data:
                data[key] = [] if shape == 'list' else {}
            continue
        if shape == 'list':
            merged = [obj for _rid, (_seq, obj)
                      in sorted(rows.items(), key=lambda kv: (kv[1][0], kv[0]))]
        else:
            merged = {rid: obj for rid, (_seq, obj) in rows.items()}
        merged = copy.deepcopy(merged)
        if key.startswith('sim:'):
            sim = data.get('sim')
            if not isinstance(sim, dict):
                sim = data['sim'] = {}
            sim[key[4:]] = merged
        else:
            data[key] = merged


def _reconcile_extracted_sections(conn, data):
    """Diff the three sections against their row tables inside the caller's
    transaction: upsert new/changed rows, delete removed ones. Returns the
    section keys that may be dropped from the blob (reconcile succeeded); a
    section that cannot be rowed (absent from the state, not a list/dict,
    entries without ids) or whose table write fails keeps its blob copy --
    never lose data. An ABSENT key means the saved state has no such section:
    the blob write it accompanies is a full replace (every writer passes the
    whole state), so absent rows are DELETED exactly as the blob replace would
    drop the key -- an empty section and a missing section are the same
    thing. A section that cannot be rowed keeps its blob copy untouched."""
    ok, now = [], time.time()
    for key, table, shape in EXTRACTED_SECTIONS:
        with _EXTRACTED_LOCK:
            cache = _EXTRACTED_CACHE[key]
            rows = _extracted_rows_load_locked(table, cache)
        if key.startswith('sim:'):
            sim = data.get('sim')
            entries = sim.get(key[4:]) if isinstance(sim, dict) else None
        else:
            entries = data.get(key)
        if entries is None:
            # Absent key in a full-state save == the section is empty: clear
            # the rows exactly as the blob replace drops the key.
            desired = {}
            unrowable = False
        elif not isinstance(entries, (list, dict)) or (
                shape == 'list' and not isinstance(entries, list)) or (
                shape in ('dict', 'dict-lists')
                and not isinstance(entries, dict)):
            # Present but unrowable (fail-closed): the blob keeps its copy,
            # the rows stay -- no information is lost either way.
            if rows:
                ok.append(key)
            continue
        else:
            desired = {}
            unrowable = False
            if shape in ('dict', 'dict-lists'):
                for rid, entry in entries.items():
                    if (not isinstance(rid, str) or not rid
                            or (shape == 'dict'
                                and not isinstance(entry, dict))):
                        unrowable = True
                        break
                    desired[rid] = (0, entry)
            else:
                for seq, entry in enumerate(entries):
                    if not isinstance(entry, dict) or not entry.get('id'):
                        unrowable = True
                        break
                    desired[str(entry['id'])] = (seq, entry)
        if unrowable:
            continue
        changed = {rid: (seq, obj) for rid, (seq, obj) in desired.items()
                   if rows.get(rid) != (seq, obj)}
        deleted = [rid for rid in rows if rid not in desired]
        try:
            for rid, (seq, obj) in changed.items():
                conn.execute(
                    f'INSERT INTO {table} (id, seq, blob, updated_at) '
                    f'VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET '
                    f'seq = excluded.seq, blob = excluded.blob, '
                    f'updated_at = excluded.updated_at',
                    (rid, seq, json.dumps(obj), now))
            for rid in deleted:
                conn.execute(f'DELETE FROM {table} WHERE id = ?', (rid,))
        except sqlite3.OperationalError:
            continue
        with _EXTRACTED_LOCK:
            cache['rows'] = {**{rid: val for rid, val in rows.items()
                                if rid not in deleted},
                             **changed}
            cache['db'] = DB_PATH
            cache['loaded'] = True
        ok.append(key)
    return ok


_DIR_SYNC_LOCK = threading.Lock()


def _save_phase(data, name):
    """Save-path breadcrumb (2026-11-02): two same-day tank freezes wedged
    with the tick breadcrumb at 'done' -- i.e. inside this save. Stamp the
    finer steps onto the same _tickPhase channel so the next freeze names
    the exact step. Best-effort; never breaks the save."""
    try:
        data['_tickPhase'] = {'phase': name, 'at': round(time.time(), 3),
                              'tick': int((data.get('sim') or {}).get('tick') or 0)}
    except Exception:
        pass


# Batched state persistence (2026-11-05): the request path stopped paying
# the full read-modify-write on every mutation. get_state_from_db serves a
# shared cached object; save_state_to_db queues the mutated state and a
# background saver persists at most one snapshot per coalescing window.
# Request latency drops from seconds (5.6 MB blob over the Tailscale DB
# link, serialized behind _STATE_LOCK) to milliseconds; the durability
# window grows to the coalescing window (default 3s), which the sim's own
# cadence and the client's 5s autosave make immaterial in practice.
_SAVE_COALESCE_S = 3.0
# Max time a continuously-refreshed pending queue may wait before the worker
# must persist anyway. The coalesce window above only closes when the queue
# goes quiet: every _queue_state_save resets 'at', so a caller that queues
# faster than _SAVE_COALESCE_S starves the saver forever. The north village's
# idle sim tick queues a fresh snapshot every ~1s, so once its engine stopped
# making model calls (budget critical, 2026-11-08 04:01) every pass queued
# inside the window, kv_state never updated again, and the whole village ran
# on stale state until the restart (2026-11-10 diagnosis). This cap bounds
# the durability window regardless of queue cadence.
_SAVE_MAX_PENDING_S = 10.0
_SAVE_PENDING: dict = {'snapshot': None, 'at': 0.0}
_SAVE_EVENT = threading.Event()
_SAVE_WORKER: list = []
# Failed-save retry bookkeeping, keyed by snapshot OBJECT IDENTITY and never
# stored inside the snapshot itself, so retry state can never leak into the
# saved blob (2026-11-07 zombie-retry fix).
_SAVE_RETRY_COUNTS: dict = {}
_STATE_READ_CACHE: dict = {'obj': None}
_SAVE_FLUSHED = threading.Event()


def _state_io_test_mode():
    """True when state I/O must behave exactly like the pre-batching code:
    synchronous saves, no read cache. Every pytest run and any explicit
    force-sqlite process gets the deterministic path."""
    try:
        import db_backend as _dbm
        return _dbm._test_sandboxed()
    except Exception:
        return False


def _queue_state_save(data):
    """Record the newest state snapshot and wake the saver. Called instead
    of the synchronous save on the request/tick paths in production. The
    snapshot is a TOP-LEVEL copy taken at queue time: later nested mutations
    may still be visible to the saver's serialize (harmless -- a serialize
    race raises, the saver re-queues, and the NEXT snapshot persists), and
    a newer queue always supersedes an older one."""
    _SAVE_PENDING['snapshot'] = dict(data)
    _SAVE_PENDING['at'] = time.time()
    _STATE_READ_CACHE['obj'] = data
    _SAVE_EVENT.set()


def _flush_pending_save(timeout=20.0):
    """Persist whatever is queued right now (shutdown path)."""
    snap = _SAVE_PENDING.get('snapshot')
    if snap is None or _SAVE_FLUSHED.is_set():
        return
    _SAVE_FLUSHED.set()
    try:
        _save_state_to_db_sync(snap)
    except Exception as e:
        print(f'[state-save] shutdown flush failed: {e}', flush=True)


def _save_snapshot_with_retry(snap):
    """Persist one snapshot with fast in-worker retries for transient
    serialize races, falling back to the re-queue cycle on repeated failure.

    The snapshot's nested structures are shared with the live state, so a sim
    thread mid-mutation can break a json walk (RuntimeError: dictionary
    changed size during iteration) or a task lookup (KeyError). Each retry
    re-reads the live structures a beat later, which is usually settled --
    far faster convergence than throwing the whole snapshot back into the 5s
    coalesce cycle (2026-11-08: 458 re-queues in a day). Returns True when the
    snapshot persisted; on failure it runs the re-queue/drop logic itself
    (newest-snapshot-wins, max three attempts per object) and returns False.
    """
    saved = False
    race_error = None
    for _attempt in range(3):
        try:
            _save_state_to_db_sync(snap)
            _SAVE_RETRY_COUNTS.clear()
            saved = True
            break
        except (RuntimeError, KeyError) as _race:
            race_error = _race
            time.sleep(0.1 * (_attempt + 1))
        except Exception as _fatal:
            race_error = _fatal
            break  # not a transient race: re-queue immediately
    if saved:
        return True
    e = race_error
    print(f'[state-save] coalesced save failed, re-queued: '
          f'{type(e).__name__}: {e}', flush=True)
    if _SAVE_PENDING.get('snapshot') is None:
        # Only retry a snapshot that is still the newest one, and never the
        # same object more than three times: a snapshot that keeps failing
        # (e.g. its nested structures raced the sim thread into an
        # unserializable shape) must yield to the next fresh snapshot instead
        # of wedging persistence on a zombie retry loop -- observed
        # 2026-11-07 when the same stamped snapshot was re-queued for many
        # minutes. Tracked by object identity OUTSIDE the snapshot, so no
        # retry bookkeeping ever leaks into the saved blob.
        attempts = _SAVE_RETRY_COUNTS.get(id(snap), 0) + 1
        _SAVE_RETRY_COUNTS[id(snap)] = attempts
        if attempts <= 3:
            _SAVE_PENDING['snapshot'] = snap
            _SAVE_PENDING['at'] = time.time() - _SAVE_COALESCE_S
        else:
            _SAVE_RETRY_COUNTS.pop(id(snap), None)
            print(f'[state-save] dropping snapshot after {attempts} '
                  f'failed attempts; the next queued save carries '
                  f'the newer state', flush=True)
    return False


def _save_worker_loop():
    # Coalescing saver: sleep through the quiet tail of a burst, then persist
    # the newest snapshot once. A failed serialize/save keeps its payload and
    # retries after a short backoff -- the tank never loses a queued mutation
    # to a transient race. pending_since tracks how long the queue has been
    # continuously non-empty (every queue resets 'at', so 'age' alone can
    # stay under the coalesce window forever); past _SAVE_MAX_PENDING_S the
    # worker persists anyway (2026-11-10 north starvation fix).
    pending_since = None
    while True:
        _SAVE_EVENT.wait(timeout=0.5)
        _SAVE_EVENT.clear()
        snap = _SAVE_PENDING.get('snapshot')
        if snap is None:
            pending_since = None
            continue
        now = time.time()
        if pending_since is None:
            pending_since = now
        age = now - float(_SAVE_PENDING.get('at') or 0)
        if (age < _SAVE_COALESCE_S
                and now - pending_since < _SAVE_MAX_PENDING_S
                and not _SAVE_FLUSHED.is_set()):
            continue
        pending_since = None
        _SAVE_PENDING['snapshot'] = None
        saved = _save_snapshot_with_retry(snap)
        if not saved:
            # _save_snapshot_with_retry already printed the failure and
            # re-queued (or dropped) the snapshot, keyed by object identity
            # OUTSIDE the snapshot so no retry bookkeeping leaks into the
            # saved blob (zombie-retry fix 2026-11-07). This worker only
            # wakes the event and backs off: a retry loop HERE would die on
            # the next failed save -- the pre-fix copy of this block
            # referenced a name that only exists inside the helper
            # (NameError -> silent saver death, the 2026-11-08 north
            # freeze) -- and a hot retry would spin instead of yielding to
            # the next fresh snapshot.
            _SAVE_EVENT.set()
            time.sleep(5.0)


def _ensure_save_worker():
    if _SAVE_WORKER:
        return
    t = threading.Thread(target=_save_worker_loop, name='state-save-worker',
                         daemon=True)
    _SAVE_WORKER.append(t)
    t.start()
    import atexit as _atexit
    _atexit.register(_flush_pending_save)


def save_state_to_db(data):
    # Production: queue and return. The caller's lock (held via
    # _state_begin) is released by the @_state_writer wrapper's
    # _state_abort, exactly as before -- only the DB I/O left the request
    # path. Tests and sandboxed processes keep the synchronous path so
    # write-then-read fixtures stay deterministic.
    if _state_io_test_mode():
        return _save_state_to_db_sync(data)
    _ensure_save_worker()
    _queue_state_save(data)


def _save_state_to_db_sync(data):
    # Pass 1 completion recovery: a stale whole-state save must not erase a
    # newer player recovery decision. Only tasks with newer intervention
    # history are protected; ordinary saves pass through unchanged.
    import server.state_updates as state_updates
    _save_phase(data, 'save_read_current')
    with _db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        if row:
            current = json.loads(row[0])
            # Archived done tasks are not in the blob. Include them so a stale
            # save cannot resurrect a completed task as live work.
            archived = _archived_tasks()
            if archived:
                # No deepcopy: `current` is a freshly-parsed json.loads
                # result with no shared references to protect, and
                # preserve_guidance copies whatever it lifts out of
                # `current` rather than mutating it. The deepcopy of the
                # whole live blob (412k copy calls, ~1.2s measured) ran on
                # EVERY save and was the largest GIL hog in the tick
                # thread -- the merged archived dicts alias the archive
                # cache, but this path only reads them.
                current['tasks'] = {**archived, **(current.get('tasks') or {})}
            data = state_updates.preserve_guidance(current, data)
    # Training capture (opt-in): link task outcomes to recorded
    # interactions whenever the sim state is committed.
    _save_phase(data, 'save_training_capture')
    training_capture.sync_outcomes(data)
    try:
        # Split completed tasks out of the blob: the archive table holds every
        # done task (upserted only when new/changed), and the blob holds only
        # the live tasks. state['tasks'] itself is left untouched -- this is a
        # view for the write, not a mutation of the caller's state.
        # The queued snapshot is a TOP-LEVEL copy only: the sim thread keeps
        # mutating the nested dicts while this worker walks them. Iterate over
        # LIST snapshots everywhere here and never re-index `tasks` by an id
        # computed earlier -- both produced intermittent save failures
        # ("dictionary changed size during iteration", KeyError 'task-19824')
        # that re-queued the same poisoned snapshot and stalled persistence
        # for minutes at a time (caught 2026-11-07).
        tasks = data.get('tasks') or {}
        task_items = list(tasks.items())
        done = {tid: t for tid, t in task_items if _is_archived_task(t)}
        # A surviving task may still point at a terminal one -- a repair child
        # mid-flight references its failed parent. Never archive a task a
        # surviving task references, or the child's completion lookup loses
        # its parent and the chain dead-ends. The blob filter below must use
        # this SAME final set, or a guarded task would be dropped from the
        # blob without ever having been archived (silent data loss).
        live = [(tid, t) for tid, t in task_items if tid not in done]
        referenced = set()
        for _tid, t in live:
            for field in ('reviewOf', 'rootStoryId', 'investigationOf',
                          'dependsOn', 'adversarialTaskId'):
                value = t.get(field)
                if isinstance(value, str) and value:
                    referenced.add(value)
                elif isinstance(value, (list, tuple)):
                    referenced.update(v for v in value if isinstance(v, str))
        if referenced:
            done = {tid: t for tid, t in done.items() if tid not in referenced}
        _save_phase(data, 'save_archive_reconcile')
        archived_ok = _reconcile_task_archive(done) if done else True
        if done and archived_ok:
            blob = {**data, 'tasks': {tid: t for tid, t in task_items
                                      if tid not in done}}
        else:
            # Nothing to archive, or the archive table is unavailable -- keep
            # every task in the blob so nothing is ever lost.
            blob = data
        _save_phase(data, 'save_blob_write')
        with _db() as conn:
            # Scale slices: reconcile the extracted sections (roster, live
            # agents, player inbox) in the SAME transaction as the blob write,
            # then drop the reconciled keys from the blob. A crash before this
            # transaction leaves the blob carrying the legacy copies with an
            # empty row table; a crash after it leaves rows authoritative with
            # no blob copy. Either state reads correctly.
            _save_phase(data, 'save_extracted_sections')
            extracted_ok = _reconcile_extracted_sections(conn, data)
            _save_phase(data, 'save_blob_serialize')
            if extracted_ok:
                blob = {k: v for k, v in blob.items() if k not in extracted_ok}
                for nkey in extracted_ok:
                    # Nested sections ('sim:agents'): strip the leaf from the
                    # BLOB VIEW only. This runs after the dict comprehension
                    # above detached `blob` from the caller's state, so the
                    # live in-memory snapshot is untouched.
                    if nkey.startswith('sim:'):
                        sim_blob = blob.get('sim')
                        if isinstance(sim_blob, dict) and nkey[4:] in sim_blob:
                            blob['sim'] = {k: v for k, v in sim_blob.items()
                                           if k != nkey[4:]}
            _save_phase(data, 'save_insert')
            # The blob VIEW is detached from the caller's state at the top
            # level, but its nested values are still shared with the live
            # state the sim thread mutates. json.dumps walking them can hit
            # "dictionary changed size during iteration"; a tiny bounded
            # retry rides out the transient resize (the sim's writes are
            # short), and a snapshot serialize NEVER fails the whole save
            # pass -- caught 2026-11-07 after minutes-long save stalls.
            blob_text = None
            for attempt in range(4):
                try:
                    blob_text = json.dumps(blob)
                    break
                except RuntimeError as _ser:
                    if attempt == 3 or 'changed size during iteration' \
                            not in str(_ser):
                        raise
                    time.sleep(0.05 * (attempt + 1))
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
                (blob_text, time.time()),
            )
            # Integrity pair (015): the checksum row must commit atomically
            # with the blob it covers. Reuse the serialized text -- a second
            # json.dumps walk of the same shared structures would race the
            # sim thread again and could disagree with the stored blob.
            _save_phase(data, 'save_checksum')
            _state_checksum_write(conn, blob_text)
        _save_phase(data, 'save_committed')
        # The per-agent mirror (agent.json/AGENTS.md/MEMORY.md/conversations)
        # runs OFF the tick thread (2026-11-02): it was the pass tail, and the
        # tick breadcrumb kept freezing at/after save_committed -- minutes-long
        # or hung mirror work held _PASS_LOCK and stopped the whole tank while
        # contributing no tick. The mirror only WRITES files from the saved
        # snapshot (never mutates state), so it is safe concurrently; a
        # non-blocking lock keeps at most one mirror run at a time and skips
        # when one is already running (the throttle fingerprint re-fires the
        # next tick anyway).
        try:
            import threading as _threading
            if _DIR_SYNC_LOCK.acquire(blocking=False):
                def _run_mirror():
                    try:
                        sync_agent_directories(data, throttle=True)
                    except Exception as e:
                        print(f'[agent-dirs] sync failed: {e}', flush=True)
                    finally:
                        _DIR_SYNC_LOCK.release()
                _threading.Thread(target=_run_mirror, name='agent-dir-sync',
                                  daemon=True).start()
        except Exception as e:
            print(f'[agent-dirs] spawn failed: {e}', flush=True)
    finally:
        # If this thread is inside a state transaction (see _state_begin /
        # _state_writer), the save is the commit point: release the blob lock so
        # the next writer can proceed. Non-transaction saves (boot, backfills)
        # never hold it, so this is a no-op for them.
        if getattr(_state_txn, 'held', False):
            _state_txn.held = False
            _state_txn.saved = True
            _STATE_LOCK.release()
            _state_lock_owner_clear()


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


_STATE_LOCK_TIMEOUT_S = 120
# Lock-owner forensics: threading.Lock has no owner, so a thread that dies
# between acquire and release leaves the lock forever-held with NO suspect in
# any thread dump (the 2026-11-08 22:20 freeze: every writer queued on
# _STATE_LOCK, no holder visible, tick stale > 50 min). Record who holds it;
# on timeout, if the recorded holder no longer exists, the lock is leaked --
# force-release it instead of freezing the tank behind a ghost.
_STATE_LOCK_OWNER = {'ident': None, 'name': '', 'at': 0.0}


def _state_lock_owner_set():
    import threading as _t
    cur = _t.current_thread()
    _STATE_LOCK_OWNER['ident'] = cur.ident
    _STATE_LOCK_OWNER['name'] = cur.name
    _STATE_LOCK_OWNER['at'] = time.time()


def _state_lock_owner_clear():
    _STATE_LOCK_OWNER['ident'] = None
    _STATE_LOCK_OWNER['name'] = ''
    _STATE_LOCK_OWNER['at'] = 0.0


def _state_lock_recover_leaked():
    '''If the recorded holder thread no longer exists, the lock is leaked:
    release it so the tank can proceed. Returns True when a leak was healed.'''
    import threading as _t
    owner = _STATE_LOCK_OWNER.get('ident')
    if owner is None:
        return False
    for t in _t.enumerate():
        if t.ident == owner:
            return False
    held_for = time.time() - float(_STATE_LOCK_OWNER.get('at') or 0)
    print(f'[state-lock] holder thread {_STATE_LOCK_OWNER["name"]!r} '
          f'is gone but held _STATE_LOCK for {held_for:.0f}s -- leaked lock '
          f'force-released', flush=True)
    try:
        _STATE_LOCK.release()
    except RuntimeError:
        pass
    _state_lock_owner_clear()
    return True


def _state_begin():
    """Acquire the state lock and return the latest blob. The caller (or its
    @_state_writer wrapper) is guaranteed to release via save or _state_abort.
    Reads through get_state_from_db() so tests that mock that reader to inject
    a state keep working."""
    # Bounded acquire, 2026-11-04: the DB rides a Tailscale link, and a
    # statement whose packets vanish mid-flight (black-holed send) waits on
    # libpq with NO client-side read timeout -- TCP keepalives never fire
    # because the connection is not idle while unacked data is pending, and
    # the server-side statement_timeout never triggers because the server
    # never received the query. One wedged writer holding _STATE_LOCK then
    # piled every other writer behind it and froze the whole tank (observed
    # twice today). A bounded acquire turns that freeze into a fast, clear
    # 503 for the waiters; the wedged holder still self-heals when the OS
    # retransmission timer (~15 min) finally kills its socket.
    if not _STATE_LOCK.acquire(timeout=_STATE_LOCK_TIMEOUT_S):
        _state_lock_recover_leaked()
        if not _STATE_LOCK.acquire(timeout=5):
            raise TimeoutError(
                f'state lock not acquired within {_STATE_LOCK_TIMEOUT_S}s '
                '-- a writer is wedged on the DB link; retry shortly')
    _state_lock_owner_set()
    _state_txn.held = True
    _state_txn.saved = False
    try:
        return get_state_from_db()
    except Exception:
        _state_txn.held = False
        _STATE_LOCK.release()
        _state_lock_owner_clear()
        raise


def _state_abort():
    """Release the state lock WITHOUT committing, if this thread still holds it.
    Idempotent; safe to call on every exit path (early return, exception)."""
    if getattr(_state_txn, 'held', False):
        _state_txn.held = False
        _STATE_LOCK.release()
        _state_lock_owner_clear()


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
    # Village-worker mode seeds its OWN roster from THINK_TANK_SEED_ROSTER
    # (passed on the launch command), so a village process does not inherit
    # the parent's roster identity through the shared .env file.
    raw = (os.environ.get('THINK_TANK_SEED_ROSTER')
           or _load_env().get('SEED_ROSTER', '')).strip()
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
